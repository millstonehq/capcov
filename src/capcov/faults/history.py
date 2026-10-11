"""An operation history: what was invoked, and what is known about its outcome.

Every operation writes two records, an invocation and a completion.  A
completion carries one of three results:

* ``ok``: the operation happened.
* ``fail``: the operation was definitely refused before any side effect.
* ``info``: the outcome is unknown.  It may or may not have happened.

``info`` is the default for every exception.  A timeout, a dropped connection
or a killed process says nothing about whether the effect landed, so none of
them may be recorded as ``fail``.  A checker that reads an unknown outcome as a
refusal passes a program that applies an effect twice.

Timestamps are logical: a :class:`LogicalClock` counts, it never reads the wall
clock, so a history is reproducible.  Values are stored as the sha256 of their
canonical JSON, so a history can be written to disk without the payloads.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
from typing import Any, Callable, Iterator

SCHEMA_VERSION = 1

#: ``kind`` of the records a crash boundary writes for one side effect.
EFFECT_KIND = "effect"


class Result(str, Enum):
    OK = "ok"
    FAIL = "fail"
    INFO = "info"

    def __str__(self) -> str:
        return self.value


class HistoryError(ValueError):
    """A history document or a call sequence that cannot be recorded."""


class Digest(str):
    """A value that is already a sha256 digest and must not be hashed again."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def value_digest(value: Any) -> Digest:
    """sha256 of the canonical JSON of ``value``; a :class:`Digest` passes through."""
    if isinstance(value, Digest):
        return value
    return Digest(hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest())


class LogicalClock:
    """A counter.  ``tick`` returns the next instant; ``now`` reads the current one."""

    def __init__(self, start: int = 0) -> None:
        self._now = start

    def now(self) -> int:
        return self._now

    def tick(self, ticks: int = 1) -> int:
        if ticks < 0:
            raise ValueError("a logical clock cannot run backwards")
        self._now += ticks
        return self._now


def classify_unknown(exc: BaseException) -> Result:
    """The default classifier: every exception leaves the outcome unknown."""
    return Result.INFO


@dataclass(frozen=True)
class Record:
    """One line of a history.  ``type`` is ``invoke`` or ``complete``."""
    type: str
    token: int
    at: int
    op: str = ""
    actor: str = ""
    kind: str = ""
    target: str = ""
    attempt: int = 0
    result: str = ""
    value: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        if self.type == "invoke":
            return {"type": "invoke", "token": self.token, "at": self.at, "op": self.op,
                    "actor": self.actor, "kind": self.kind, "target": self.target,
                    "attempt": self.attempt}
        return {"type": "complete", "token": self.token, "at": self.at,
                "result": self.result, "value": self.value, "error": self.error}


class History:
    """Append-only record of invocations and completions.

    ``classify`` maps an exception raised inside :meth:`operation` to a
    :class:`Result`.  Supply one to mark the refusals a service is known to make
    before any side effect as ``fail``; everything it does not recognise must
    stay ``info``.
    """

    def __init__(self, clock: LogicalClock | None = None, *,
                 classify: Callable[[BaseException], Result] = classify_unknown) -> None:
        self.clock = clock if clock is not None else LogicalClock()
        self.classify = classify
        self._records: list[Record] = []
        self._open: dict[int, Record] = {}
        self._attempts: dict[str, int] = {}
        self._next_token = 1
        self._closed = False

    @property
    def records(self) -> tuple[Record, ...]:
        return tuple(self._records)

    @property
    def complete_flag(self) -> bool:
        """True only after :meth:`close`: no invocation is left without a completion."""
        return self._closed

    def invoke(self, op: str, actor: str, kind: str, target: str,
               attempt: int | None = None) -> int:
        if self._closed:
            raise HistoryError("history is closed")
        if attempt is None:
            attempt = self._attempts.get(op, 0) + 1
        if type(attempt) is not int or attempt < 0:
            raise HistoryError("attempt must be a non-negative integer")
        self._attempts[op] = max(self._attempts.get(op, 0), attempt)
        token = self._next_token
        self._next_token += 1
        record = Record("invoke", token, self.clock.tick(), op=str(op), actor=str(actor),
                        kind=str(kind), target=str(target), attempt=attempt)
        self._records.append(record)
        self._open[token] = record
        return token

    def complete(self, token: int, result: Result | str, value: Any = None,
                 error: str | None = None) -> None:
        if token not in self._open:
            raise HistoryError(f"token {token} is not an open invocation")
        del self._open[token]
        self._records.append(Record(
            "complete", token, self.clock.tick(), result=Result(result).value,
            value="" if value is None else value_digest(value), error=error or ""))

    @contextmanager
    def operation(self, op: str, actor: str, kind: str, target: str,
                  attempt: int | None = None) -> Iterator[int]:
        """Record one operation: ``ok`` on return, the classifier's result on an exception.

        ``BaseException`` is caught on purpose so a simulated process death is
        recorded (as ``info`` by default) before it propagates.
        """
        token = self.invoke(op, actor, kind, target, attempt)
        try:
            yield token
        except BaseException as exc:
            if token in self._open:
                self.complete(token, self.classify(exc), error=type(exc).__name__)
            raise
        else:
            if token in self._open:
                self.complete(token, Result.OK)

    def close(self) -> None:
        """Complete every open invocation as ``info`` and mark the history complete."""
        for token in sorted(self._open):
            self.complete(token, Result.INFO, error="open-at-close")
        self._closed = True

    def operations(self) -> tuple[dict[str, Any], ...]:
        """Invocations joined to their completions, in invocation order.

        An invocation with no completion yet has ``result`` ``None``.
        """
        completions = {record.token: record for record in self._records
                       if record.type == "complete"}
        joined = []
        for record in self._records:
            if record.type != "invoke":
                continue
            done = completions.get(record.token)
            joined.append({"token": record.token, "op": record.op, "actor": record.actor,
                           "kind": record.kind, "target": record.target,
                           "attempt": record.attempt, "invoked_at": record.at,
                           "result": done.result if done else None,
                           "value": done.value if done else "",
                           "error": done.error if done else "",
                           "completed_at": done.at if done else None})
        return tuple(joined)

    def to_json(self) -> str:
        """Schema version 1: a header line, then one record per line."""
        header = {"schema_version": SCHEMA_VERSION, "complete": self._closed}
        lines = [canonical_json(header)]
        lines.extend(canonical_json(record.as_dict()) for record in self._records)
        return "\n".join(lines) + "\n"

    @classmethod
    def from_json(cls, text: str) -> "History":
        lines = [line for line in text.splitlines() if line.strip()]
        if not lines:
            raise HistoryError("history document is empty")
        try:
            documents = [json.loads(line) for line in lines]
        except json.JSONDecodeError as exc:
            raise HistoryError(f"history line is not JSON: {exc}") from exc
        header = documents[0]
        if not isinstance(header, dict) or header.get("schema_version") != SCHEMA_VERSION:
            raise HistoryError("unsupported history schema version")
        if not isinstance(header.get("complete"), bool):
            raise HistoryError("history header needs a boolean 'complete'")
        history = cls()
        last_at = 0
        for document in documents[1:]:
            try:
                record = _record(document)
            except (KeyError, TypeError, ValueError) as exc:
                raise HistoryError(f"malformed history record: {exc}") from exc
            if record.at < last_at:
                raise HistoryError("history timestamps must not decrease")
            last_at = record.at
            if record.type == "invoke":
                if record.token in history._open or any(
                        r.token == record.token for r in history._records):
                    raise HistoryError(f"token {record.token} is invoked twice")
                history._open[record.token] = record
                history._attempts[record.op] = max(
                    history._attempts.get(record.op, 0), record.attempt)
            else:
                if record.token not in history._open:
                    raise HistoryError(f"completion of unknown token {record.token}")
                del history._open[record.token]
            history._records.append(record)
            history._next_token = max(history._next_token, record.token + 1)
        history.clock = LogicalClock(last_at)
        if header["complete"] and history._open:
            raise HistoryError("a complete history cannot hold an open invocation")
        history._closed = header["complete"]
        return history


def _record(document: Any) -> Record:
    if not isinstance(document, dict):
        raise TypeError("record must be an object")
    kind = document["type"]
    token, at = document["token"], document["at"]
    if type(token) is not int or type(at) is not int:
        raise TypeError("token and at must be integers")
    if kind == "invoke":
        if set(document) != {"type", "token", "at", "op", "actor", "kind", "target", "attempt"}:
            raise ValueError("invoke record has unknown or missing fields")
        if type(document["attempt"]) is not int or document["attempt"] < 0:
            raise TypeError("attempt must be a non-negative integer")
        for name in ("op", "actor", "kind", "target"):
            if not isinstance(document[name], str):
                raise TypeError(f"{name} must be a string")
        return Record("invoke", token, at, op=document["op"], actor=document["actor"],
                      kind=document["kind"], target=document["target"],
                      attempt=document["attempt"])
    if kind == "complete":
        if set(document) != {"type", "token", "at", "result", "value", "error"}:
            raise ValueError("complete record has unknown or missing fields")
        if not isinstance(document["value"], str) or not isinstance(document["error"], str):
            raise TypeError("value and error must be strings")
        return Record("complete", token, at, result=Result(document["result"]).value,
                      value=document["value"], error=document["error"])
    raise ValueError(f"unknown record type {kind!r}")


__all__ = ["SCHEMA_VERSION", "EFFECT_KIND", "Result", "HistoryError", "Digest",
           "canonical_json", "value_digest", "LogicalClock", "classify_unknown",
           "Record", "History"]
