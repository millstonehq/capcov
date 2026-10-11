"""An in-memory external service with ground truth and a fault menu.

``FakeService`` models an external service without conditional writes or
idempotency keys.  ``write`` replaces a value unconditionally and takes no
precondition, ``create`` never deduplicates, and no call accepts a token that
would let the service recognise a repeat.  A caller that needs exactly-once
behaviour has to build it from what the service does offer: reads, and a change
log.

The service keeps what a real one never shows its callers: every applied write
with the request that caused it, every write that replaced a value its sender
had not read, and every accepted write it has not applied yet.
:meth:`FakeService.ground_truth` returns that record, and the checker compares
the caller's own bookkeeping against it.

A write replaced an unread value when the key changed after the last read or
write its sender had made of that key when it sent the request.  A delayed
write that lands after its sender has read or written a newer value is
therefore reported.

Faults are queued with :meth:`FakeService.inject` and consumed by the next call
they match, in the order they were queued.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, NamedTuple

from .history import Digest, History, LogicalClock, Result, canonical_json, value_digest

MUTATIONS = frozenset({"write", "add", "remove", "create"})


class ServiceError(Exception):
    """Base class of every exception the fake service raises to a caller."""


class Refused(ServiceError):
    """A definite refusal.  Nothing was applied."""

    def __init__(self, status: int) -> None:
        super().__init__(f"refused with status {status}")
        self.status = status


class ResponseLost(ServiceError):
    """The request was sent and no response arrived.  The outcome is unknown."""


class Ambiguous(ServiceError):
    """An error status that does not say whether the request was applied."""

    def __init__(self, status: int) -> None:
        super().__init__(f"ambiguous error with status {status}")
        self.status = status


def classify(exc: BaseException) -> Result:
    """History classifier for this service: only :class:`Refused` is a ``fail``."""
    return Result.FAIL if isinstance(exc, Refused) else Result.INFO


# --- fault menu -----------------------------------------------------------

@dataclass(frozen=True)
class Refuse:
    """Refuse the request with ``status``.  Nothing is applied."""
    status: int = 409


@dataclass(frozen=True)
class LoseResponse:
    """Apply the request, then raise :class:`ResponseLost`."""


@dataclass(frozen=True)
class DelayApply:
    """Accept the request, raise :class:`ResponseLost`, apply it ``ticks`` later."""
    ticks: int = 1


@dataclass(frozen=True)
class ApplyTwice:
    """Apply one request twice and answer normally."""


@dataclass(frozen=True)
class EditBeforeNextWrite:
    """An outside editor sets ``key`` after the caller's read and before its next write."""
    key: str
    value: Any
    actor: str = "outside-editor"


@dataclass(frozen=True)
class DelayChangeEntry:
    """Apply the request, but withhold its change-log entry for ``ticks``."""
    ticks: int = 1


@dataclass(frozen=True)
class AmbiguousError:
    """Raise :class:`Ambiguous` with ``status``; ``applied`` says whether it landed."""
    status: int = 500
    applied: bool = True


@dataclass(frozen=True)
class DuplicateOnConcurrentCreate:
    """A second actor's identical ``create`` lands with the caller's.  Both are kept."""
    actor: str = "concurrent-actor"


Fault = (Refuse | LoseResponse | DelayApply | ApplyTwice | EditBeforeNextWrite
         | DelayChangeEntry | AmbiguousError | DuplicateOnConcurrentCreate)


# --- records --------------------------------------------------------------

@dataclass(frozen=True)
class Request:
    """One call a caller made.  ``value`` is the argument, not the stored result.

    ``seen`` is the sequence number of the last write to ``key`` that ``actor``
    had read or made when it sent this request, and 0 when it had seen none.
    """
    id: int
    method: str
    key: str
    actor: str
    at: int
    value: Any = None
    seen: int = 0


@dataclass(frozen=True)
class Applied:
    """One write the service applied.

    ``value`` is the digest of the request's argument (the written value, the
    member, the attributes).  ``result`` is the digest of the value the key
    holds afterwards.  ``origin`` is ``request`` for a caller's write and
    ``outside`` for a write a fault made on another party's behalf.
    """
    seq: int
    request: int
    key: str
    value: Digest
    result: Digest
    actor: str
    at: int
    origin: str
    method: str


class ChangeEntry(NamedTuple):
    seq: int
    actor: str
    at: int
    key: str
    value_digest: str


@dataclass(frozen=True)
class ChangeRead:
    """One read of the change log.

    ``complete`` is false while any entry is withheld or any accepted write is
    still to be applied.  A real service does not report this; it is the
    harness's knowledge, and the checker uses it to decide whether the absence
    of an entry means anything.
    """
    entries: tuple[ChangeEntry, ...]
    complete: bool


@dataclass(frozen=True)
class GroundTruth:
    """``pending`` holds the requests the service accepted and has not applied."""
    final: Mapping[str, Any]
    applied: tuple[Applied, ...]
    overwrote_unread: tuple[Applied, ...]
    requests: tuple[Request, ...]
    pending: tuple[Request, ...]


@dataclass
class _Queued:
    fault: Any
    match: Callable[[Request], bool]


@dataclass
class _Pending:
    due: int
    request: Request
    origin: str = "request"
    extra: dict[str, Any] = field(default_factory=dict)


def _matcher(fault: Any, match: Any) -> Callable[[Request], bool]:
    if callable(match):
        return match
    criteria = dict(match or {})
    unknown = set(criteria) - {"method", "key", "actor"}
    if unknown:
        raise ValueError(f"unknown match fields: {sorted(unknown)}")
    if isinstance(fault, EditBeforeNextWrite):
        criteria.setdefault("method", "write")
        criteria.setdefault("key", fault.key)
    elif isinstance(fault, DuplicateOnConcurrentCreate):
        criteria.setdefault("method", "create")
    elif "method" not in criteria:
        return lambda request: (request.method in MUTATIONS
                                and all(getattr(request, name) == value
                                        for name, value in criteria.items()))
    return lambda request: all(getattr(request, name) == value
                               for name, value in criteria.items())


class FakeService:
    """In-memory external service.  ``name`` is the store name the checker reports."""

    def __init__(self, clock: LogicalClock | None = None, history: History | None = None,
                 *, name: str = "service") -> None:
        self.clock = clock if clock is not None else LogicalClock()
        self.history = history
        self.name = name
        self._values: dict[str, Any] = {}
        self._last_seq: dict[str, int] = {}
        self._last_applied: dict[str, Applied] = {}
        self._seen: dict[tuple[str, str], int] = {}
        self._requests: list[Request] = []
        self._applied: list[Applied] = []
        self._overwrote: list[Applied] = []
        self._log: list[tuple[int, ChangeEntry]] = []
        self._faults: list[_Queued] = []
        self._pending: list[_Pending] = []
        self._hold_next_entry: int | None = None
        self._created = 0

    # --- caller API ---------------------------------------------------------

    def read(self, key: str, actor: str) -> Any:
        """The current value of ``key`` (``None`` when unset)."""
        return self._call("read", key, None, actor)

    def write(self, key: str, value: Any, actor: str) -> None:
        """Replace the value of ``key``.  There is no precondition to pass."""
        self._call("write", key, value, actor)

    def add(self, key: str, member: Any, actor: str) -> None:
        """Add ``member`` to the set stored at ``key``."""
        self._call("add", key, member, actor)

    def remove(self, key: str, member: Any, actor: str) -> None:
        """Remove ``member`` from the set stored at ``key``."""
        self._call("remove", key, member, actor)

    def create(self, kind: str, attributes: Mapping[str, Any], actor: str) -> str:
        """Create a record and return its id.  Identical attributes create a second record."""
        return self._call("create", kind, dict(attributes), actor)

    def search(self, kind: str, **attributes: Any) -> list[tuple[str, dict[str, Any]]]:
        """Records of ``kind`` whose attributes include every given pair."""
        prefix = kind + "/"
        found = []
        for key in sorted(self._values):
            record = self._values[key]
            if (key.startswith(prefix) and isinstance(record, dict)
                    and all(record.get(name) == value for name, value in attributes.items())):
                found.append((key[len(prefix):], dict(record)))
        return found

    def changes(self, key: str | None = None) -> ChangeRead:
        """The visible change log, oldest first, optionally for one key."""
        now = self.clock.now()
        visible = tuple(entry for due, entry in self._log
                        if due <= now and (key is None or entry.key == key))
        return ChangeRead(visible, not self._pending and all(due <= now for due, _ in self._log))

    # --- harness API --------------------------------------------------------

    def inject(self, fault: Fault, match: Any = None) -> None:
        """Queue ``fault`` for the next call that matches.

        ``match`` is a predicate over :class:`Request`, or a mapping with any of
        ``method``, ``key`` and ``actor``.  Without a ``method`` a fault matches
        mutating calls only.
        """
        self._faults.append(_Queued(fault, _matcher(fault, match)))

    def advance(self, ticks: int = 1) -> None:
        """Move the clock forward and apply every delayed write that is now due."""
        self.clock.tick(ticks)
        self._release(self.clock.now())

    def heal(self) -> None:
        """Drop queued faults and release every delayed write and withheld log entry."""
        self._faults.clear()
        latest = max([self.clock.now(), *(pending.due for pending in self._pending),
                      *(due for due, _ in self._log)])
        self.clock.tick(latest - self.clock.now())
        self._release(latest)

    def ground_truth(self) -> GroundTruth:
        return GroundTruth(dict(self._values), tuple(self._applied),
                           tuple(self._overwrote), tuple(self._requests),
                           tuple(pending.request for pending in self._pending))

    # --- internals ----------------------------------------------------------

    def _release(self, now: int) -> None:
        due = sorted((p for p in self._pending if p.due <= now), key=lambda p: (p.due, p.request.id))
        self._pending = [p for p in self._pending if p.due > now]
        for pending in due:
            self._apply(pending.request, pending.origin)

    def _take_fault(self, request: Request) -> Any:
        for index, queued in enumerate(self._faults):
            if queued.match(request):
                return self._faults.pop(index).fault
        return None

    def _call(self, method: str, key: str, value: Any, actor: str) -> Any:
        request = Request(len(self._requests) + 1, method, key, actor, self.clock.tick(), value,
                          self._seen.get((actor, key), 0))
        self._requests.append(request)
        token = None
        if self.history is not None:
            token = self.history.invoke(f"{self.name}:{request.id}", actor, method, key)
        try:
            answer = self._serve(request)
        except BaseException as exc:
            if token is not None:
                self.history.complete(token, classify(exc), error=type(exc).__name__)
            raise
        if token is not None:
            self.history.complete(token, Result.OK, answer)
        return answer

    def _serve(self, request: Request) -> Any:
        fault = self._take_fault(request)
        if isinstance(fault, Refuse):
            raise Refused(fault.status)
        if request.method == "read":
            if isinstance(fault, (LoseResponse, DelayApply)):
                raise ResponseLost("no response")
            if isinstance(fault, AmbiguousError):
                raise Ambiguous(fault.status)
            self._seen[(request.actor, request.key)] = self._last_seq.get(request.key, 0)
            value = self._values.get(request.key)
            return list(value) if isinstance(value, list) else value
        if fault is None:
            return self._apply(request)
        if isinstance(fault, LoseResponse):
            self._apply(request)
            raise ResponseLost("no response")
        if isinstance(fault, DelayApply):
            self._pending.append(_Pending(self.clock.now() + max(fault.ticks, 1), request))
            raise ResponseLost("no response")
        if isinstance(fault, ApplyTwice):
            self._apply(request)
            return self._apply(request)
        if isinstance(fault, EditBeforeNextWrite):
            outside = Request(request.id, "write", fault.key, fault.actor,
                              self.clock.now(), fault.value)
            self._apply(outside, "outside")
            return self._apply(request)
        if isinstance(fault, DelayChangeEntry):
            self._hold_next_entry = self.clock.now() + max(fault.ticks, 1)
            return self._apply(request)
        if isinstance(fault, AmbiguousError):
            if fault.applied:
                self._apply(request)
            raise Ambiguous(fault.status)
        if isinstance(fault, DuplicateOnConcurrentCreate):
            answer = self._apply(request)
            concurrent = Request(request.id, "create", request.key, fault.actor,
                                 self.clock.now(), request.value)
            self._apply(concurrent, "outside")
            return answer
        raise TypeError(f"unknown fault: {fault!r}")

    def _apply(self, request: Request, origin: str = "request") -> Any:
        key, answer = request.key, None
        if request.method == "write":
            result = request.value
        elif request.method in {"add", "remove"}:
            current = self._values.get(key) or []
            if not isinstance(current, list):
                raise TypeError(f"{key!r} does not hold a set")
            members = {canonical_json(member): member for member in current}
            if request.method == "add":
                members[canonical_json(request.value)] = request.value
            else:
                members.pop(canonical_json(request.value), None)
            result = [members[name] for name in sorted(members)]
        elif request.method == "create":
            self._created += 1
            answer = f"{request.key}-{self._created}"
            key = f"{request.key}/{answer}"
            result = dict(request.value)
        else:
            raise TypeError(f"{request.method!r} is not a mutation")
        seq = len(self._applied) + 1
        at = self.clock.tick()
        applied = Applied(seq, request.id, key, value_digest(request.value),
                          value_digest(result), request.actor, at, origin, request.method)
        previous = self._last_applied.get(key)
        # Compared with what the sender had seen when it sent the request, not
        # with what it has seen by now.  A second apply of the same request
        # (ApplyTwice) replaces the request's own value and is not reported.
        if (origin == "request" and request.method == "write" and previous is not None
                and previous.seq > request.seen
                and not (previous.origin == "request" and previous.request == request.id)):
            self._overwrote.append(applied)
        self._values[key] = result
        self._last_seq[key] = seq
        self._last_applied[key] = applied
        self._seen[(request.actor, key)] = seq
        self._applied.append(applied)
        visible_at, self._hold_next_entry = self._hold_next_entry or at, None
        self._log.append((visible_at, ChangeEntry(seq, request.actor, at, key, applied.result)))
        return answer


__all__ = ["MUTATIONS", "ServiceError", "Refused", "ResponseLost", "Ambiguous", "classify",
           "Refuse", "LoseResponse", "DelayApply", "ApplyTwice", "EditBeforeNextWrite",
           "DelayChangeEntry", "AmbiguousError", "DuplicateOnConcurrentCreate", "Fault",
           "Request", "Applied", "ChangeEntry", "ChangeRead", "GroundTruth", "FakeService"]
