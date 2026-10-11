"""The fixture seam: snapshot, restore, inspect, drain -- and one composition of stores.

The recorder needs four verbs from whatever holds the system's state, and it
needs them before EVERY vector: restore to the snapshot, inspect, run the
steps, drain the queues, inspect again. ``Fixture`` is that contract as a
Protocol so a consumer can supply its own (a product may inspect through its
own application code, which sees typed rows the plain-text readers do not).
``ComposedFixture`` is the stdlib one: a set of store adapters and an injected
drain callable.

The inspection shape is fixed so the diff can be generic:

    {"rows": {table: [row, ...]},           relational tables
     "collections": {name: [doc, ...]},     document collections
     "queues": {name: depth},               work still pending
     "redis_keys": [key, ...],              cache/key-value keys present
     "objects": {store: {key: etag}}}       object-store listings

Every key is always present, empty when no store reports it. Two stores
reporting the SAME table/collection/queue name is ``FixtureError``: a silent
overwrite would hide one store's delta behind the other's.

Two observation modes. ``inspect`` before and after every vector costs the
size of the database each time -- a minute on a snapshot with a few hundred
thousand rows even when one row changed. A store that can OBSERVE its own
changes (a relational server's binary log, a document store's oplog) offers
``mark()`` and ``changes_since(mark)`` instead, and the fixture exposes them
as ``ObservingFixture``: the recorder marks, runs the steps, drains, and reads
exactly what changed. Cost follows what changed, not what exists. The delta it
returns is the same shape ``diff.store_delta`` produces, so the comparison is
unchanged; only how it was obtained differs, and the vector's provenance
records which.

An observing fixture also knows whether a vector wrote anything. A read
vector that changed nothing needs no restore before the next one; the
recorder asks ``dirty`` and skips the restore, which is most vectors.

Draining is bounded and honest. The fixture calls the injected drain callable,
re-inspects, and stops when every queue reports empty; after ``max_drain_rounds``
it raises ``QueuesNotDrained`` naming the queues still holding work. A queue
that never empties is a finding about the system, not something to wait out
forever or to report as drained. With no drain callable and work pending, it
raises at once: nothing can drain it.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol, runtime_checkable

INSPECTION_KEYS = ("rows", "collections", "queues", "redis_keys", "redis", "objects")


class FixtureError(RuntimeError):
    """The composition is inconsistent (a name reported by two stores, a bad token)."""


class QueuesNotDrained(RuntimeError):
    """Queues still held work after the bounded drain; names them."""

    def __init__(self, queues: dict[str, int], rounds: int) -> None:
        pending = ", ".join(f"{name}={depth}" for name, depth in sorted(queues.items()) if depth)
        super().__init__(f"queues not drained after {rounds} round(s): {pending}")
        self.queues = dict(queues)
        self.rounds = rounds


@runtime_checkable
class Store(Protocol):
    def snapshot(self) -> Any: ...
    def restore(self, token: Any) -> None: ...
    def inspect(self) -> dict: ...


@runtime_checkable
class Fixture(Protocol):
    def snapshot(self) -> Any:
        """Capture the current state of every store; the token is opaque to the recorder."""

    def restore(self, token: Any) -> None:
        """Return every store to the state the token captured."""

    def inspect(self) -> dict:
        """The five-key inspection described in the module docstring."""

    def drain(self) -> None:
        """Work every queue until all report empty, or raise QueuesNotDrained."""


@runtime_checkable
class ObservingStore(Protocol):
    def mark(self) -> Any: ...
    def changes_since(self, mark: Any) -> dict:
        """``{"rows": {table: {"inserted", "updated", "deleted"}}, "collections": {...}}``
        plus ``"unbound": [names]`` for changes it saw but could not attribute."""


@runtime_checkable
class ObservingFixture(Protocol):
    def snapshot(self) -> Any: ...
    def restore(self, token: Any) -> None: ...
    def inspect(self) -> dict: ...
    def drain(self) -> None: ...
    def mark(self) -> Any:
        """Position every observing store; opaque to the recorder."""

    def changes_since(self, mark: Any) -> dict:
        """Every observing store's changes since the mark, merged; ``dirty`` is
        True when any store changed."""


def empty_inspection() -> dict:
    return {"rows": {}, "collections": {}, "queues": {}, "redis_keys": [], "redis": {}, "objects": {}}


def merge_inspections(parts: dict[str, dict]) -> dict:
    """Fold per-store inspections into one; a name reported twice is an error naming both stores."""
    merged = empty_inspection()
    owners: dict[tuple[str, str], str] = {}
    for store_name, part in parts.items():
        for key, value in part.items():
            if key not in INSPECTION_KEYS:
                raise FixtureError(f"store {store_name!r} reported unknown inspection key {key!r}")
            if key == "redis_keys":
                merged[key] = sorted(set(merged[key]) | set(value))
                continue
            for name, item in value.items():
                owner = owners.get((key, name))
                if owner is not None:
                    raise FixtureError(
                        f"{key}[{name!r}] reported by both {owner!r} and {store_name!r}"
                    )
                owners[(key, name)] = store_name
                merged[key][name] = item
    return merged


class ComposedFixture:
    """Stores by name plus a drain callable. Restore order is the given order.

    When every store also implements ``ObservingStore`` the composition is an
    ``ObservingFixture``; when only some do, ``mark`` and ``changes_since``
    raise ``FixtureError`` naming the stores that cannot observe, because a
    delta missing one store's changes is not a delta.
    """

    def __init__(
        self,
        stores: dict[str, Store],
        drain: Callable[[], None] | None = None,
        max_drain_rounds: int = 6,
    ) -> None:
        if not stores:
            raise FixtureError("ComposedFixture: no stores")
        for name, store in stores.items():
            if not isinstance(store, Store):
                raise FixtureError(f"ComposedFixture: store {name!r} lacks snapshot/restore/inspect")
        if max_drain_rounds < 1:
            raise FixtureError("ComposedFixture: max_drain_rounds must be at least 1")
        self.stores = dict(stores)
        self._drain = drain
        self.max_drain_rounds = max_drain_rounds

    def snapshot(self) -> dict[str, Any]:
        return {name: store.snapshot() for name, store in self.stores.items()}

    def restore(self, token: dict[str, Any]) -> None:
        if not isinstance(token, dict) or set(token) != set(self.stores):
            have = sorted(token) if isinstance(token, dict) else type(token).__name__
            raise FixtureError(
                f"ComposedFixture.restore: token covers {have}, stores are {sorted(self.stores)}"
            )
        for name, store in self.stores.items():
            store.restore(token[name])

    def inspect(self) -> dict:
        return merge_inspections({name: store.inspect() for name, store in self.stores.items()})

    @property
    def observing(self) -> bool:
        """True when every store can report its own changes."""
        return all(isinstance(store, ObservingStore) for store in self.stores.values())

    def _observers(self) -> dict[str, ObservingStore]:
        cannot = [name for name, store in self.stores.items() if not isinstance(store, ObservingStore)]
        if cannot:
            raise FixtureError(f"stores cannot observe their own changes: {cannot}; use inspect() diffing")
        return self.stores  # type: ignore[return-value]

    def mark(self) -> dict[str, Any]:
        return {name: store.mark() for name, store in self._observers().items()}

    def changes_since(self, mark: dict[str, Any]) -> dict:
        merged = {"rows": {}, "collections": {}, "unbound": [], "dirty": False}
        for name, store in self._observers().items():
            part = store.changes_since(mark[name])
            for key in ("rows", "collections"):
                for table, change in part.get(key, {}).items():
                    if table in merged[key]:
                        raise FixtureError(f"{key}[{table!r}] changed in two stores")
                    merged[key][table] = change
                    merged["dirty"] = True
            merged["unbound"].extend(part.get("unbound", []))
            if part.get("redis"):
                merged.setdefault("informational", {})[f"{name}.keys"] = part["redis"]
        merged["unbound"] = sorted(set(merged["unbound"]))
        return merged

    def pending(self) -> dict[str, int]:
        """Queue depths only. A store that can report them cheaply (``queue_depths``)
        is asked that way; ``inspect`` on a large relational store would read every
        table just to learn whether a queue is empty."""
        depths: dict[str, int] = {}
        for name, store in self.stores.items():
            cheap = getattr(store, "queue_depths", None)
            part = cheap() if callable(cheap) else store.inspect().get("queues", {})
            for queue, depth in part.items():
                if queue in depths:
                    raise FixtureError(f"queue {queue!r} reported by two stores")
                depths[queue] = depth
        return {name: depth for name, depth in depths.items() if depth}

    def drain(self) -> None:
        rounds = 0
        while True:
            pending = self.pending()
            if not pending:
                return
            if self._drain is None:
                raise QueuesNotDrained(pending, rounds)
            if rounds >= self.max_drain_rounds:
                raise QueuesNotDrained(pending, rounds)
            self._drain()
            rounds += 1
