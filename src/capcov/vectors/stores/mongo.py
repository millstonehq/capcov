"""MongoDB store adapter: inspect through a shell eval, restore by dropping.

Inspection runs one JavaScript expression through the shell client (``mongosh``
by default) that serialises every collection of the database to JSON, so the
adapter needs no driver. Documents come back as the shell's JSON: ``_id`` and
``$oid``/``$date`` wrappers are present here and it is the normalizer's job to
drop them at compare time.

Snapshot has two modes, and the DEFAULT is the honest one:

* ``drop`` (no ``dump_argv``): the snapshot records the collection counts and
  ``restore`` drops the database. That is only a restore when the snapshot was
  EMPTY, so ``snapshot()`` refuses -- ``MongoSnapshotUnsupported`` naming the
  non-empty collections -- rather than let a later restore erase data it cannot
  put back.
* ``archive`` (``dump_argv`` + ``restore_argv`` given, e.g. ``mongodump
  --archive --db app`` and ``mongorestore --archive --drop``): the snapshot is
  the archive bytes on stdout and restore pipes them back on stdin.

Refusal: a shell eval that prints something other than JSON is
``MongoInspectError`` with the head of what it printed.
"""

from __future__ import annotations

import json

from .runner import CommandRunner

DEFAULT_CLIENT_ARGV = ("mongosh", "--quiet")

# One expression: every collection of the named database, each as a JSON array
# of its documents in natural order. EJSON keeps ObjectId/Date as {$oid}/{$date}
# wrappers rather than failing on them.
_INSPECT_JS = (
    "const d = db.getSiblingDB({database}); const out = {{}}; "
    "for (const name of d.getCollectionNames().sort()) {{ out[name] = d.getCollection(name).find().toArray(); }} "
    "print(EJSON.stringify(out));"
)
_DROP_JS = "db.getSiblingDB({database}).dropDatabase();"


class MongoSnapshotUnsupported(RuntimeError):
    """Drop-mode snapshot of a non-empty database: restore could not recreate it."""


class MongoInspectError(RuntimeError):
    """The shell eval did not print a JSON object of collections."""


class MongoStore:
    def __init__(
        self,
        runner: CommandRunner,
        database: str,
        client_argv: tuple[str, ...] | list[str] = DEFAULT_CLIENT_ARGV,
        dump_argv: tuple[str, ...] | list[str] | None = None,
        restore_argv: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        if not database:
            raise ValueError("MongoStore: database name is empty")
        if (dump_argv is None) != (restore_argv is None):
            raise ValueError("MongoStore: dump_argv and restore_argv must be given together")
        self.runner = runner
        self.database = database
        self.client_argv = list(client_argv)
        self.dump_argv = list(dump_argv) if dump_argv else None
        self.restore_argv = list(restore_argv) if restore_argv else None

    @property
    def mode(self) -> str:
        return "archive" if self.dump_argv else "drop"

    def _eval(self, script: str) -> bytes:
        return self.runner.run([*self.client_argv, "--eval", script])

    def inspect(self) -> dict:
        """``{"collections": {name: [doc, ...]}}``; empty collections are omitted."""
        out = self._eval(_INSPECT_JS.format(database=json.dumps(self.database)))
        text = out.decode(errors="replace")
        try:
            parsed = json.loads(text)
        except ValueError:
            raise MongoInspectError(
                f"{self.database}: inspect did not print JSON: {text[:400]!r}"
            ) from None
        if not isinstance(parsed, dict):
            raise MongoInspectError(f"{self.database}: inspect printed {type(parsed).__name__}, not an object")
        return {"collections": {name: docs for name, docs in parsed.items() if docs}}

    def snapshot(self) -> dict:
        if self.mode == "archive":
            return {"mode": "archive", "archive": self.runner.run(self.dump_argv)}
        counts = {name: len(docs) for name, docs in self.inspect()["collections"].items()}
        if counts:
            raise MongoSnapshotUnsupported(
                f"{self.database}: drop-mode snapshot of a non-empty database "
                f"({', '.join(f'{k}={v}' for k, v in sorted(counts.items()))}); "
                "pass dump_argv/restore_argv for an archive snapshot"
            )
        return {"mode": "drop"}

    def restore(self, token: dict) -> None:
        if not isinstance(token, dict) or token.get("mode") != self.mode:
            raise ValueError(
                f"MongoStore.restore: token mode {token.get('mode') if isinstance(token, dict) else token!r} "
                f"does not match the adapter's {self.mode!r}"
            )
        if self.mode == "archive":
            self.runner.run(self.restore_argv, input=token["archive"])
            return
        self._eval(_DROP_JS.format(database=json.dumps(self.database)))


_COUNTS_JS = (
    "const d = db.getSiblingDB({database}); const out = {{}}; "
    "d.getCollectionNames().forEach(n => {{ out[n] = d.getCollection(n).countDocuments(); }}); "
    "print(JSON.stringify(out));"
)
_TAIL_JS = (
    "const d = db.getSiblingDB({database}); const out = {{}}; "
    "const marks = {marks}; "
    "d.getCollectionNames().forEach(n => {{ const skip = marks[n] || 0; "
    "out[n] = d.getCollection(n).find().skip(skip).toArray(); }}); "
    "print(JSON.stringify(out));"
)


class ObservingMongoStore(MongoStore):
    """Observe appends per collection by document count.

    Document stores used as audit trails only ever append, and this fixture's
    Mongo starts from a snapshot the recorder controls, so a count before and
    the documents past that count after is the delta -- without serialising
    what was already there. An in-place update or a removal is NOT observed
    this way: a collection whose count went down is reported as ``unbound``
    so the fixture falls back to full inspection, never to silence.
    """

    def queue_depths(self) -> dict[str, int]:
        return {}

    def mark(self) -> dict[str, int]:
        out = self._eval(_COUNTS_JS.format(database=json.dumps(self.database)))
        try:
            return {name: int(count) for name, count in json.loads(out.decode(errors="replace")).items()}
        except (ValueError, AttributeError):
            raise MongoInspectError(f"{self.database}: count did not print JSON") from None

    def changes_since(self, mark: dict[str, int]) -> dict:
        counts = self.mark()
        shrunk = sorted(name for name, n in counts.items() if n < mark.get(name, 0))
        out = self._eval(_TAIL_JS.format(database=json.dumps(self.database), marks=json.dumps(mark)))
        try:
            tails = json.loads(out.decode(errors="replace"))
        except ValueError:
            raise MongoInspectError(f"{self.database}: tail did not print JSON") from None
        collections = {}
        for name, docs in tails.items():
            if docs and name not in shrunk:
                collections[name] = {"appended": docs, "removed": 0, "count": [mark.get(name, 0), counts.get(name, 0)]}
        return {"rows": {}, "collections": collections, "unbound": shrunk}


class WireMongoStore:
    """The observing document store over the wire protocol: no shell process.

    Same contract as ``ObservingMongoStore`` -- inspect, snapshot (drop mode),
    restore, mark, changes_since -- through ``mongowire.MongoWireClient``. A
    connection is opened per operation so a server restart between vectors is
    never a stale socket, and each operation is a few milliseconds instead of a
    shell start-up. The archive snapshot mode needs the dump tools and is not
    offered here; a non-empty database at snapshot time is refused as in the
    drop-mode store.
    """

    def __init__(self, host: str, port: int, database: str, timeout: float = 30.0) -> None:
        if not database:
            raise ValueError("WireMongoStore: database name is empty")
        self.host, self.port, self.database, self.timeout = host, int(port), database, timeout

    def _client(self):
        from .mongowire import MongoWireClient
        return MongoWireClient(self.host, self.port, self.timeout)

    def queue_depths(self) -> dict[str, int]:
        return {}

    def inspect(self) -> dict:
        client = self._client()
        try:
            collections = {}
            for name in client.collections(self.database):
                docs = client.find(self.database, name)
                if docs:
                    collections[name] = docs
        finally:
            client.close()
        return {"collections": collections}

    def snapshot(self) -> dict:
        counts = {name: len(docs) for name, docs in self.inspect()["collections"].items()}
        if counts:
            raise MongoSnapshotUnsupported(
                f"{self.database}: drop-mode snapshot of a non-empty database "
                f"({', '.join(f'{k}={v}' for k, v in sorted(counts.items()))})"
            )
        return {"mode": "drop"}

    def restore(self, token: dict) -> None:
        if not isinstance(token, dict) or token.get("mode") != "drop":
            raise ValueError("WireMongoStore.restore: token is not a drop-mode snapshot")
        client = self._client()
        try:
            client.drop_database(self.database)
        finally:
            client.close()

    def mark(self) -> dict[str, int]:
        client = self._client()
        try:
            return {name: client.count(self.database, name) for name in client.collections(self.database)}
        finally:
            client.close()

    def changes_since(self, mark: dict[str, int]) -> dict:
        client = self._client()
        try:
            names = client.collections(self.database)
            counts = {name: client.count(self.database, name) for name in names}
            shrunk = sorted(name for name, n in counts.items() if n < mark.get(name, 0))
            collections = {}
            for name in names:
                if name in shrunk:
                    continue
                skip = mark.get(name, 0)
                if counts[name] <= skip:
                    continue
                docs = client.find(self.database, name, skip=skip)
                if docs:
                    collections[name] = {"appended": docs, "removed": 0, "count": [skip, counts[name]]}
        finally:
            client.close()
        return {"rows": {}, "collections": collections, "unbound": shrunk}
