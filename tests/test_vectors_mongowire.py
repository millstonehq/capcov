"""The stdlib Mongo wire client: BSON round-trips, frames a fake server accepts, refusals."""

from __future__ import annotations

import socket
import struct
import threading
import unittest

from capcov.vectors.stores.mongowire import MongoWireClient, MongoWireError, decode, encode


class BsonTests(unittest.TestCase):
    def test_round_trip_of_the_covered_types(self) -> None:
        doc = {"s": "it's", "i": 7, "big": 2 ** 40, "f": 1.5, "b": True, "n": None,
               "d": {"x": [1, "two", {"three": 3}]}, "empty": {}, "list": []}
        out, end = decode(encode(doc))
        self.assertEqual(out, doc)
        self.assertEqual(end, len(encode(doc)))

    def test_objectid_and_datetime_decode_to_stable_strings(self) -> None:
        oid = bytes(range(12))
        raw = b"\x07" + b"_id\x00" + oid + b"\x09" + b"at\x00" + struct.pack("<q", 1_700_000_000_000)
        frame = struct.pack("<i", len(raw) + 5) + raw + b"\x00"
        out, _ = decode(frame)
        self.assertEqual(out, {"_id": oid.hex(), "at": "2023-11-14T22:13:20Z"})

    def test_unhandled_element_type_is_refused_not_dropped(self) -> None:
        raw = b"\x05" + b"blob\x00" + struct.pack("<i", 1) + b"\x00" + b"\x01"
        frame = struct.pack("<i", len(raw) + 5) + raw + b"\x00"
        with self.assertRaisesRegex(MongoWireError, "unhandled element type 0x05"):
            decode(frame)

    def test_truncated_frame_is_refused(self) -> None:
        with self.assertRaisesRegex(MongoWireError, "exceeds frame"):
            decode(struct.pack("<i", 999) + b"\x00")


class FakeServer:
    """Answers every OP_MSG with a canned reply; records the command documents it saw."""

    def __init__(self, reply: dict) -> None:
        self.reply, self.seen = reply, []
        self.sock = socket.socket(); self.sock.bind(("127.0.0.1", 0)); self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        conn, _ = self.sock.accept()
        while True:
            head = conn.recv(16)
            if len(head) < 16:
                return
            length, request_id, _, opcode = struct.unpack("<iiii", head)
            body = b""
            while len(body) < length - 16:
                body += conn.recv(length - 16 - len(body))
            doc, _ = decode(body, 5)
            self.seen.append(doc)
            reply = encode(self.reply)
            payload = struct.pack("<I", 0) + b"\x00" + reply
            conn.sendall(struct.pack("<iiii", 16 + len(payload), 1, request_id, 2013) + payload)


class ClientTests(unittest.TestCase):
    def test_command_sends_an_op_msg_and_returns_the_reply(self) -> None:
        server = FakeServer({"ok": 1, "n": 3})
        client = MongoWireClient("127.0.0.1", server.port)
        self.assertEqual(client.count("app", "audit"), 3)
        self.assertEqual(server.seen[0]["count"], "audit")
        self.assertEqual(server.seen[0]["$db"], "app")
        client.close()

    def test_server_refusal_is_raised_with_its_message(self) -> None:
        server = FakeServer({"ok": 0, "errmsg": "not authorized on app"})
        client = MongoWireClient("127.0.0.1", server.port)
        with self.assertRaisesRegex(MongoWireError, "not authorized"):
            client.collections("app")
        client.close()

    def test_find_walks_first_batch(self) -> None:
        server = FakeServer({"ok": 1, "cursor": {"id": 0, "firstBatch": [{"id": 1}, {"id": 2}]}})
        client = MongoWireClient("127.0.0.1", server.port)
        self.assertEqual(client.find("app", "audit", skip=5), [{"id": 1}, {"id": 2}])
        self.assertEqual(server.seen[0]["skip"], 5)
        client.close()


if __name__ == "__main__":
    unittest.main()
