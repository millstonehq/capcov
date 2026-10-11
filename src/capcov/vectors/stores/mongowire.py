"""A stdlib MongoDB wire client: enough of OP_MSG and BSON to observe a database.

Driving ``mongosh`` per call costs a JavaScript shell start-up each time --
about 400 ms -- and an observing fixture asks the document store twice per
vector. The wire protocol is small: one message kind (OP_MSG, opcode 2013)
carrying one BSON document, and the handful of commands a fixture needs
(``listCollections``, ``count``, ``find`` with ``skip``, ``dropDatabase``,
``ping``). This client implements exactly that, with a BSON codec for the
types documents in an audit collection actually carry. Nothing else.

BSON coverage is deliberately partial and says so: ``double``, ``string``,
``document``, ``array``, ``ObjectId`` (decoded to its hex string), ``bool``,
``null``, ``int32``, ``int64``, ``datetime`` (decoded to an ISO-8601 string in
UTC). An element of any other type is decoded as ``{"$bsonType": <byte>}`` so
the delta shows an unhandled type instead of silently dropping the field.

Refusal: a reply whose ``ok`` is not 1 raises ``MongoWireError`` with the
server's ``errmsg``; a truncated or malformed frame raises the same error
naming the offset. The client never retries and never reconnects on its own.
"""

from __future__ import annotations

import socket
import struct
from datetime import datetime, timezone
from typing import Any

OP_MSG = 2013


class MongoWireError(RuntimeError):
    """The server refused a command, or a frame could not be parsed."""


# --- BSON encode ------------------------------------------------------------

def _cstring(value: str) -> bytes:
    return value.encode() + b"\x00"


def _encode_value(value: Any) -> tuple[int, bytes]:
    if value is None:
        return 0x0A, b""
    if value is True or value is False:
        return 0x08, b"\x01" if value else b"\x00"
    if isinstance(value, int):
        if -(2 ** 31) <= value < 2 ** 31:
            return 0x10, struct.pack("<i", value)
        return 0x12, struct.pack("<q", value)
    if isinstance(value, float):
        return 0x01, struct.pack("<d", value)
    if isinstance(value, str):
        data = value.encode()
        return 0x02, struct.pack("<i", len(data) + 1) + data + b"\x00"
    if isinstance(value, dict):
        return 0x03, encode(value)
    if isinstance(value, (list, tuple)):
        return 0x04, encode({str(i): v for i, v in enumerate(value)})
    raise TypeError(f"bson: cannot encode {type(value).__name__}")


def encode(document: dict) -> bytes:
    body = b""
    for key, value in document.items():
        kind, payload = _encode_value(value)
        body += bytes([kind]) + _cstring(str(key)) + payload
    return struct.pack("<i", len(body) + 5) + body + b"\x00"


# --- BSON decode ------------------------------------------------------------

def decode(data: bytes, offset: int = 0) -> tuple[dict, int]:
    """Decode one document at ``offset``; return (document, offset after it)."""
    if offset + 4 > len(data):
        raise MongoWireError(f"bson: truncated document header at {offset}")
    size = struct.unpack_from("<i", data, offset)[0]
    end = offset + size
    if end > len(data) or size < 5:
        raise MongoWireError(f"bson: document size {size} at {offset} exceeds frame")
    pos = offset + 4
    out: dict = {}
    while pos < end - 1:
        kind = data[pos]
        pos += 1
        name_end = data.index(b"\x00", pos)
        name = data[pos:name_end].decode()
        pos = name_end + 1
        if kind == 0x01:
            out[name] = struct.unpack_from("<d", data, pos)[0]; pos += 8
        elif kind == 0x02:
            ln = struct.unpack_from("<i", data, pos)[0]
            out[name] = data[pos + 4:pos + 4 + ln - 1].decode(errors="replace"); pos += 4 + ln
        elif kind == 0x03:
            out[name], pos = decode(data, pos)
        elif kind == 0x04:
            arr, pos = decode(data, pos)
            out[name] = [arr[k] for k in sorted(arr, key=int)]
        elif kind == 0x07:
            out[name] = data[pos:pos + 12].hex(); pos += 12
        elif kind == 0x08:
            out[name] = data[pos] == 1; pos += 1
        elif kind == 0x09:
            millis = struct.unpack_from("<q", data, pos)[0]; pos += 8
            out[name] = datetime.fromtimestamp(millis / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        elif kind == 0x0A:
            out[name] = None
        elif kind == 0x10:
            out[name] = struct.unpack_from("<i", data, pos)[0]; pos += 4
        elif kind == 0x11:
            out[name] = {"$timestamp": struct.unpack_from("<Q", data, pos)[0]}; pos += 8
        elif kind == 0x12:
            out[name] = struct.unpack_from("<q", data, pos)[0]; pos += 8
        else:
            raise MongoWireError(f"bson: unhandled element type 0x{kind:02x} for field {name!r} at {pos}")
    return out, end


# --- client ---------------------------------------------------------------

class MongoWireClient:
    """One connection; ``command(db, doc)`` sends OP_MSG and returns the reply document."""

    def __init__(self, host: str, port: int, timeout: float = 30.0) -> None:
        self.sock = socket.create_connection((host, int(port)), timeout=timeout)
        self._request_id = 0

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def _recv(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise MongoWireError("connection closed mid-frame")
            buf += chunk
        return buf

    def command(self, database: str, document: dict) -> dict:
        self._request_id += 1
        body = encode({**document, "$db": database})
        section = b"\x00" + body
        payload = struct.pack("<I", 0) + section  # flagBits, then one kind-0 section
        header = struct.pack("<iiii", 16 + len(payload), self._request_id, 0, OP_MSG)
        self.sock.sendall(header + payload)
        length, _, _, opcode = struct.unpack("<iiii", self._recv(16))
        frame = self._recv(length - 16)
        if opcode != OP_MSG:
            raise MongoWireError(f"unexpected opcode {opcode}")
        if frame[4] != 0:
            raise MongoWireError(f"unexpected section kind {frame[4]}")
        reply, _ = decode(frame, 5)
        if reply.get("ok") != 1:
            raise MongoWireError(f"{document and next(iter(document))}: {reply.get('errmsg', reply)}")
        return reply

    # the few operations an observing fixture needs
    def ping(self) -> bool:
        return self.command("admin", {"ping": 1}).get("ok") == 1

    def collections(self, database: str) -> list[str]:
        reply = self.command(database, {"listCollections": 1, "nameOnly": True})
        return sorted(c["name"] for c in reply["cursor"]["firstBatch"])

    def count(self, database: str, collection: str) -> int:
        return int(self.command(database, {"count": collection})["n"])

    def find(self, database: str, collection: str, skip: int = 0, limit: int = 0) -> list[dict]:
        reply = self.command(database, {"find": collection, "skip": skip, "limit": limit, "batchSize": 10_000})
        docs = list(reply["cursor"]["firstBatch"])
        cursor_id = reply["cursor"]["id"]
        while cursor_id:
            more = self.command(database, {"getMore": cursor_id, "collection": collection, "batchSize": 10_000})
            docs.extend(more["cursor"]["nextBatch"])
            cursor_id = more["cursor"]["id"]
        return docs

    def drop_database(self, database: str) -> None:
        self.command(database, {"dropDatabase": 1})
