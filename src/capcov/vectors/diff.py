"""Deltas between two store inspections, and the first difference of two results.

``store_delta`` turns a before/after pair of fixture inspections into what
changed, split into the stores the operation DECLARES it touches (compared at
replay) and everything else (carried as informational, so an undeclared touch
becomes a finding rather than vanishing). ``first_difference`` names the first
node at which a candidate's result departs from the recorded one; replay stops
at the first because one path a person can read beats a wall of diff.

An inspection is the shape ``fixture.Fixture.inspect`` returns::

    {"rows":        {table: [row, ...]},        # relational stores
     "collections": {name: [document, ...]},    # document stores
     "queues":      {name: depth},              # pending work
     "redis_keys":  [key, ...]}                 # cache key set

Rows are keyed by their ``id`` when they have one, else by a hash of the row, so
an insert, an update and a delete are told apart. Documents are compared as an
append-only log (appended tail, removed count, rewritten prefix), because that
is what an audit collection is; a collection that is not append-only shows a
``rewritten`` count rather than a false "nothing appended". Queue depths and
cache keys are always informational: they are how work moves, not what it did.

``declared`` is ``"*"`` (compare every touched store) or a table of names by
store kind, ``{"rows": [...], "collections": [...]}``. The store-technology
spellings ``tables``/``mysql`` (rows) and ``mongo``/``documents`` (collections)
are accepted so a consumer's inputs may name the technology it declared.
``informational`` is a list of table or collection names that are never
compared even when declared: bookkeeping tables (job status, failed jobs, an
outbox) that change on every run.
"""

from __future__ import annotations

import hashlib
import json

from .normalize import normalize

ROW_KINDS = ("rows", "tables", "mysql", "sql")
COLLECTION_KINDS = ("collections", "documents", "mongo")


def row_key(row: dict) -> str:
    """The identity of a row: its ``id`` if present, else a hash of its content."""
    if isinstance(row, dict) and row.get("id") is not None:
        return str(row["id"])
    digest = hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode())
    return digest.hexdigest()[:16]


def _declared_names(declared, kinds: tuple[str, ...]) -> set[str] | None:
    """The declared names for a store kind; ``None`` means every name."""
    if declared == "*" or declared is None:
        return None
    if not isinstance(declared, dict):
        raise ValueError(f"declared must be '*' or a table of names by store kind, got {declared!r}")
    names: set[str] = set()
    for kind in kinds:
        names.update(declared.get(kind) or [])
    return names


def _rows_delta(before: list, after: list, extra: tuple[str, ...]) -> dict | None:
    b = {row_key(row): normalize(row, extra) for row in before}
    a = {row_key(row): normalize(row, extra) for row in after}
    inserted = [a[i] for i in a if i not in b]
    deleted = [b[i] for i in b if i not in a]
    updated = []
    for i in a:
        if i in b and a[i] != b[i]:
            changed = {k: [b[i].get(k), a[i].get(k)] for k in sorted(set(a[i]) | set(b[i])) if a[i].get(k) != b[i].get(k)}
            updated.append({"id": a[i].get("id", i), "changed": changed})
    if not (inserted or deleted or updated):
        return None
    return {"inserted": inserted, "updated": updated, "deleted": deleted}


def _collection_delta(before: list, after: list, extra: tuple[str, ...]) -> dict | None:
    b = [normalize(doc, extra) for doc in before]
    a = [normalize(doc, extra) for doc in after]
    if a == b:
        return None
    common = min(len(a), len(b))
    rewritten = sum(1 for x, y in zip(a[:common], b[:common]) if x != y)
    return {
        "appended": a[len(b):],
        "removed": max(0, len(b) - len(a)),
        "rewritten": rewritten,
        "count": [len(b), len(a)],
    }


def store_delta(before: dict, after: dict, declared="*", informational=(), extra_volatile_keys=()) -> tuple[dict, dict]:
    """``(declared_delta, informational)`` between two inspections.

    Keys are ``rows.<table>``, ``collections.<name>``, ``redis.keys`` and
    ``queues``. A table or collection appears in ``declared_delta`` when it
    changed, it is declared (or ``declared == "*"``) and it is not listed as
    informational; every other change appears in ``informational``.
    """
    extra = tuple(extra_volatile_keys)
    never = set(informational or ())
    declared_rows = _declared_names(declared, ROW_KINDS)
    declared_collections = _declared_names(declared, COLLECTION_KINDS)
    compared: dict = {}
    info: dict = {}

    before_rows, after_rows = before.get("rows") or {}, after.get("rows") or {}
    for table in sorted(set(before_rows) | set(after_rows)):
        change = _rows_delta(before_rows.get(table) or [], after_rows.get(table) or [], extra)
        if change is None:
            continue
        keep = table not in never and (declared_rows is None or table in declared_rows)
        (compared if keep else info)[f"rows.{table}"] = change

    before_docs, after_docs = before.get("collections") or {}, after.get("collections") or {}
    for name in sorted(set(before_docs) | set(after_docs)):
        change = _collection_delta(before_docs.get(name) or [], after_docs.get(name) or [], extra)
        if change is None:
            continue
        keep = name not in never and (declared_collections is None or name in declared_collections)
        (compared if keep else info)[f"collections.{name}"] = change

    b_keys, a_keys = set(before.get("redis_keys") or []), set(after.get("redis_keys") or [])
    if b_keys != a_keys:
        info["redis.keys"] = {"added": sorted(a_keys - b_keys), "removed": sorted(b_keys - a_keys)}
    before_redis, after_redis = before.get("redis") or {}, after.get("redis") or {}
    if before_redis != after_redis:
        changed = sorted(key for key in set(before_redis) & set(after_redis)
                         if before_redis[key] != after_redis[key])
        if changed:
            info["redis.values"] = {"changed": changed}

    queues = after.get("queues") or {}
    if any(queues.values()):
        info["queues"] = dict(queues)
    return compared, info


def _short(value) -> str:
    return json.dumps(value, sort_keys=True, default=str)[:120]


def first_difference(expected, actual, path: str = "") -> str | None:
    """The path of the first node where ``actual`` departs from ``expected``.

    ``None`` when they are equal. A type mismatch is a difference at that node
    (``1`` and ``1.0``, ``True`` and ``1`` differ). Dict keys are visited in
    sorted order so the answer is stable; list items in order, a length
    mismatch reported before the items.
    """
    if type(expected) is not type(actual):
        return f"{path or '/'}: expected {_short(expected)} got {_short(actual)}"
    if isinstance(expected, dict):
        for key in sorted(set(expected) | set(actual), key=str):
            if key not in actual:
                return f"{path}/{key}: missing in candidate"
            if key not in expected:
                return f"{path}/{key}: unexpected in candidate: {_short(actual[key])}"
            found = first_difference(expected[key], actual[key], f"{path}/{key}")
            if found:
                return found
        return None
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return f"{path or '/'}: expected {len(expected)} items got {len(actual)}"
        for index, (e, a) in enumerate(zip(expected, actual)):
            found = first_difference(e, a, f"{path}[{index}]")
            if found:
                return found
        return None
    if expected != actual:
        return f"{path or '/'}: expected {_short(expected)} got {_short(actual)}"
    return None
