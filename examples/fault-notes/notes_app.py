"""Publishing a note across three stores.

Publishing writes to:

* a primary store (sqlite3): the note row, and one intent row per effect;
* an object store: a create-only snapshot of the note body;
* an external index service: a visibility value, written with an
  unconditional replace because the service offers nothing else.

The program records an intent before it sends anything.  When the outcome of a
send is unknown it leaves the intent pending, and recovery settles it from the
service's change log.  Recovery sends only an intent whose write the log does
not show.  It must run after the service's propagation delay has passed, which
the scenario models by healing the fake service first.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
from typing import Any

from capcov.faults.crash import Boundaries, Step, boundary_context, instrument
from capcov.faults.history import History, Result, value_digest
from capcov.faults.service import FakeService, ServiceError

PRIMARY, OBJECTS, INDEX = "primary", "objects", "index"

SCHEMA = """
CREATE TABLE notes (id TEXT PRIMARY KEY, body TEXT NOT NULL, state TEXT NOT NULL);
CREATE TABLE intents (
    id TEXT PRIMARY KEY, op TEXT NOT NULL, store TEXT NOT NULL, key TEXT NOT NULL,
    value TEXT NOT NULL, sender TEXT NOT NULL DEFAULT '', seen_seq INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending', outcome TEXT);
CREATE TABLE overwrites (store TEXT NOT NULL, seq INTEGER NOT NULL, PRIMARY KEY (store, seq));
"""


class AlreadyExists(Exception):
    """A create-only put found the key taken."""


class ObjectStore:
    """A dict with a create-only put."""

    def __init__(self) -> None:
        self.objects: dict[str, str] = {}

    def put(self, key: str, body: str) -> None:
        if key in self.objects:
            raise AlreadyExists(key)
        self.objects[key] = body

    def get(self, key: str) -> str | None:
        return self.objects.get(key)


@dataclass
class World:
    """The three stores, the history, and the open operations."""
    history: History
    db: sqlite3.Connection
    objects: ObjectStore
    index: FakeService
    tokens: dict[str, int]
    unknown: set[str]

    @classmethod
    def new(cls, history: History) -> "World":
        db = sqlite3.connect(":memory:")
        db.executescript(SCHEMA)
        return cls(history, db, ObjectStore(), FakeService(history.clock, name=INDEX), {}, set())


def publish_op(note: str) -> str:
    return f"publish:{note}"


class Publisher:
    """One actor's view of the stores, with every side effect a crash boundary."""

    def __init__(self, world: World, actor: str, boundaries: Boundaries) -> None:
        self.world, self.actor, self.boundaries = world, actor, boundaries
        self.objects = instrument(world.objects, ["put"], boundaries, OBJECTS)
        self.index = instrument(world.index, ["write"], boundaries, INDEX)

    def commit(self) -> Any:
        return boundary_context("commit", self.world.db, self.boundaries, PRIMARY)

    # --- the publish operation, one method per step ----------------------------

    def record(self, note: str, body: str) -> None:
        """Write the note and both intents in one transaction, before any send."""
        op = publish_op(note)
        self.world.tokens[op] = self.world.history.invoke(op, self.actor, "publish", note)
        with self.commit() as db:
            db.execute("INSERT INTO notes VALUES (?, ?, 'publishing')", (note, body))
            db.execute("INSERT INTO intents (id, op, store, key, value) VALUES (?, ?, ?, ?, ?)",
                       (f"{note}/snapshot", op, OBJECTS, f"snapshots/{note}", json.dumps(body)))
            db.execute("INSERT INTO intents (id, op, store, key, value) VALUES (?, ?, ?, ?, ?)",
                       (f"{note}/visibility", op, INDEX, f"visibility/{note}", json.dumps("public")))

    def snapshot(self, note: str) -> None:
        self._put_snapshot(self._intent(f"{note}/snapshot"))

    def send(self, note: str) -> None:
        """Send the visibility write.  An unknown outcome leaves the intent pending."""
        intent = self._intent(f"{note}/visibility")
        try:
            self._send(intent)
        except ServiceError:
            self.world.unknown.add(publish_op(note))

    def settle(self, note: str) -> None:
        """Settle what is known, then acknowledge only if nothing is left pending."""
        op = publish_op(note)
        if op not in self.world.unknown:
            self._settle_from_log(self._intent(f"{note}/visibility"))
        self._finish_note(note)
        pending = self.world.db.execute(
            "SELECT count(*) FROM intents WHERE op = ? AND status = 'pending'", (op,)).fetchone()[0]
        self.world.history.complete(self.world.tokens.pop(op),
                                    Result.INFO if pending else Result.OK)

    # --- recovery -------------------------------------------------------------

    def recover(self) -> None:
        """Settle every pending intent.  Send only what the change log does not show."""
        pending = self.world.db.execute(
            "SELECT id FROM intents WHERE status = 'pending' ORDER BY id").fetchall()
        notes = set()
        for (intent_id,) in pending:
            note = intent_id.split("/")[0]
            token = self.world.history.invoke(f"recover:{intent_id}", self.actor, "recover", note)
            settled = self._recover_intent(intent_id)
            self.world.history.complete(token, Result.OK if settled else Result.INFO)
            notes.add(note)
        for note in sorted(notes):
            self._finish_note(note)

    def _recover_intent(self, intent_id: str) -> bool:
        intent = self._intent(intent_id)
        if intent["store"] == OBJECTS:
            self._put_snapshot(intent)
            return True
        own = self._own_entry(intent)
        if own is None:
            try:
                self._send(intent)
            except ServiceError:
                return False
        return self._settle_from_log(self._intent(intent_id))

    # --- helpers ----------------------------------------------------------------

    def _intent(self, intent_id: str) -> dict[str, Any]:
        row = self.world.db.execute(
            "SELECT id, op, store, key, value, sender, seen_seq, status FROM intents WHERE id = ?",
            (intent_id,)).fetchone()
        names = ("id", "op", "store", "key", "value", "sender", "seen_seq", "status")
        intent = dict(zip(names, row))
        intent["value"] = json.loads(intent["value"])
        return intent

    def _put_snapshot(self, intent: dict[str, Any]) -> None:
        with self.boundaries.scope(self.actor, intent["op"]):
            try:
                self.objects.put(intent["key"], intent["value"])
            except AlreadyExists:
                if self.world.objects.get(intent["key"]) != intent["value"]:
                    raise
            with self.commit() as db:
                db.execute("UPDATE intents SET status = 'settled', outcome = 'applied' WHERE id = ?",
                           (intent["id"],))

    def _send(self, intent: dict[str, Any]) -> None:
        """Read the key, record what was seen and who sends, then write."""
        with self.boundaries.scope(self.actor, intent["op"]):
            entries = self.world.index.changes(intent["key"]).entries
            seen = entries[-1].seq if entries else 0
            self.world.index.read(intent["key"], self.actor)
            with self.commit() as db:
                db.execute("UPDATE intents SET sender = ?, seen_seq = ? WHERE id = ?",
                           (self.actor, seen, intent["id"]))
            self.index.write(intent["key"], intent["value"], self.actor)

    def _own_entry(self, intent: dict[str, Any]) -> Any:
        """The first log entry this intent's sender wrote after its read, if visible."""
        digest = value_digest(intent["value"])
        for entry in self.world.index.changes(intent["key"]).entries:
            if (entry.seq > intent["seen_seq"] and entry.actor == intent["sender"]
                    and entry.value_digest == digest):
                return entry
        return None

    def _settle_from_log(self, intent: dict[str, Any]) -> bool:
        """Mark the intent applied once its write is visible; report what it replaced unread."""
        own = self._own_entry(intent)
        if own is None:
            return False
        replaced = [entry for entry in self.world.index.changes(intent["key"]).entries
                    if intent["seen_seq"] < entry.seq < own.seq]
        with self.boundaries.scope(self.actor, intent["op"]), self.commit() as db:
            db.execute("UPDATE intents SET status = 'settled', outcome = 'applied' WHERE id = ?",
                       (intent["id"],))
            if replaced:
                db.execute("INSERT OR IGNORE INTO overwrites VALUES (?, ?)", (INDEX, own.seq))
        return True

    def _finish_note(self, note: str) -> None:
        pending = self.world.db.execute(
            "SELECT count(*) FROM intents WHERE op = ? AND status = 'pending'",
            (publish_op(note),)).fetchone()[0]
        state = self.world.db.execute("SELECT state FROM notes WHERE id = ?", (note,)).fetchone()
        if pending or state is None or state[0] == "published":
            return
        with self.boundaries.scope(self.actor, publish_op(note)), self.commit() as db:
            db.execute("UPDATE notes SET state = 'published' WHERE id = ?", (note,))


def publish_steps(note: str, body: str) -> list[Step]:
    """The publish operation as steps a schedule can interleave and crash between."""
    def step(name: str, call: Any) -> Step:
        def run(world: World, actor: str, boundaries: Boundaries) -> None:
            with boundaries.scope(actor, publish_op(note)):
                call(Publisher(world, actor, boundaries))
        return Step(name, run)

    return [step("record", lambda publisher: publisher.record(note, body)),
            step("snapshot", lambda publisher: publisher.snapshot(note)),
            step("send", lambda publisher: publisher.send(note)),
            step("settle", lambda publisher: publisher.settle(note))]


def recover(world: World, actor: str, boundaries: Boundaries) -> None:
    Publisher(world, actor, boundaries).recover()
