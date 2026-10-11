"""Fixed vector templates per access class.

The design rules fix the cells; nothing here is per-operation. A template turns
one ``Operation`` plus a manifest (actors and ids) plus that operation's inputs
(bodies, args) into ``Cell``s. A cell is driver-neutral: each step says which
actor, which params, which body, and the recorder asks the driver to turn it
into requests at run time, once credentials exist.

    read:      permitted, no-permission, other-tenant, missing-record, anonymous
    write:     authorized, no-permission, other-tenant, invalid-input, retry, anonymous
    webhook:   valid-signature, invalid-signature, duplicate
    scheduled: due, not-due, retry-after-failure
    queued:    dispatched, undrained, retry-after-failure
    command:   run, retry

If a case is not in the vectors it is not required; new cases go here, not to
one operation. A cell the manifest cannot fill (no actor for the role, no
``missing`` id for a path parameter, no ``fault`` for retry-after-failure) is
returned as a gap with the reason, never guessed.

Two deliberate substitutions, both labelled so a reader sees what actually ran:

* ``other-tenant`` needs an actor in another tenant. When the manifest has no
  such actor but has ``other_tenant`` ids, the permitted actor against the
  foreign ids is the nearest honest cell, and it is emitted as ``other-scope``.
* ``invalid-input`` for a write with no ``invalid`` body in the inputs sends an
  empty body; that IS the invalid input, and the cell is not a gap.

Manifest shape (every section: defaults at the top level, ``overrides`` keyed
by any of ``Operation.keys()``)::

    {"actors":       {"permitted": 11, "no_permission": 12, "other_tenant": null,
                      "overrides": {"orders.delete": {"permitted": 13}}},
     "params":       {"order": 31, "overrides": {}},
     "missing":      {"order": 999, "overrides": {}},
     "other_tenant": {"order": 39, "overrides": {}}}

Inputs shape (keyed like overrides; later keys in ``Operation.keys()`` override
earlier ones; every field optional)::

    {"orders.update": {"body": {...}, "invalid": {...}, "query": {...},
                       "params": {...}, "headers": {...}, "args": {...},
                       "access": "write", "declared": {...}, "fault": {...}}}
"""

from __future__ import annotations

from .schema import Cell, Operation, StepSpec, gap

TEMPLATE_VERSION = "v2"

# Roles that never need an actor id from the manifest.
ACTORLESS_ROLES = ("anonymous", "system", "webhook")

TEMPLATES: dict[str, list[dict]] = {
    "read": [
        {"id": "permitted", "note": "permitted actor", "actor": "permitted", "target": "primary"},
        {"id": "no-permission", "note": "actor without the permission", "actor": "no_permission", "target": "primary"},
        {"id": "other-tenant", "note": "actor in another tenant", "actor": "other_tenant", "target": "primary"},
        {"id": "missing-record", "note": "missing record", "actor": "permitted", "target": "missing"},
        {"id": "anonymous", "note": "no session", "actor": "anonymous", "target": "primary"},
    ],
    "write": [
        {"id": "authorized", "note": "authorized happy path", "actor": "permitted", "target": "primary"},
        {"id": "no-permission", "note": "actor without the permission", "actor": "no_permission", "target": "primary"},
        {"id": "other-tenant", "note": "actor in another tenant", "actor": "other_tenant", "target": "primary"},
        {"id": "invalid-input", "note": "invalid input", "actor": "permitted", "target": "primary", "body": "invalid"},
        {"id": "retry", "note": "retry of the same write", "actor": "permitted", "target": "primary", "repeat": 2},
        {"id": "anonymous", "note": "no session", "actor": "anonymous", "target": "primary"},
    ],
    "webhook": [
        {"id": "valid-signature", "note": "valid signature", "actor": "webhook", "target": "primary", "signature": "valid"},
        {"id": "invalid-signature", "note": "invalid signature", "actor": "webhook", "target": "primary", "signature": "invalid"},
        {"id": "duplicate", "note": "duplicate delivery", "actor": "webhook", "target": "primary", "signature": "valid", "repeat": 2},
    ],
    "scheduled": [
        {"id": "due", "note": "due at its next run time", "actor": "system", "target": "primary", "when": "due"},
        {"id": "not-due", "note": "not due", "actor": "system", "target": "primary", "when": "not-due"},
        {"id": "retry-after-failure", "note": "retry after injected failure", "actor": "system", "target": "primary", "when": "due", "fault": True},
    ],
    "queued": [
        {"id": "dispatched", "note": "dispatched and drained", "actor": "system", "target": "primary", "drain": True},
        {"id": "undrained", "note": "dispatched, left on the queue", "actor": "system", "target": "primary", "drain": False},
        {"id": "retry-after-failure", "note": "retry after injected failure", "actor": "system", "target": "primary", "drain": True, "fault": True},
    ],
    "command": [
        {"id": "run", "note": "run once", "actor": "system", "target": "primary"},
        {"id": "retry", "note": "run twice", "actor": "system", "target": "primary", "repeat": 2},
    ],
}


def cell_ids(access: str) -> list[str]:
    """The cell ids the template for ``access`` produces, in order."""
    return [cell["id"] for cell in TEMPLATES.get(access, [])]


def _scoped(section: dict, keys: list[str]) -> dict:
    """Merge a manifest section's defaults with per-operation overrides.

    Defaults are the section's top-level entries (or its ``defaults`` table
    when it has one); ``overrides`` is applied for every key of the operation
    in ``keys`` order, so a later key wins.
    """
    if not isinstance(section, dict):
        raise ValueError(f"manifest section must be a table, got {type(section).__name__}")
    base = section.get("defaults", section) if "defaults" in section else section
    out = {k: v for k, v in base.items() if k != "overrides"}
    overrides = section.get("overrides") or {}
    for key in keys:
        out.update(overrides.get(key, {}))
    return out


def manifest_for(op: Operation, manifest: dict) -> dict:
    """Actors, params, missing ids and other-tenant ids as they apply to ``op``."""
    keys = op.keys()
    return {
        "actors": _scoped(manifest.get("actors", {}), keys),
        "params": _scoped(manifest.get("params", {}), keys),
        "missing": _scoped(manifest.get("missing", {}), keys),
        "other_tenant": _scoped(manifest.get("other_tenant", {}), keys),
    }


def inputs_for(op: Operation, inputs: dict) -> dict:
    """The inputs entry for ``op``: every matching key merged, later keys override."""
    out: dict = {}
    for key in op.keys():
        entry = inputs.get(key)
        if entry is None:
            continue
        if not isinstance(entry, dict):
            raise ValueError(f"inputs[{key!r}] must be a table, got {type(entry).__name__}")
        out.update(entry)
    return out


def declared_for(op: Operation, inputs: dict) -> object:
    """The stores compared at replay for ``op``: the inputs' ``declared``, else ``"*"``."""
    return inputs_for(op, inputs).get("declared", "*")


def cells_for(op: Operation, manifest: dict, inputs: dict) -> tuple[list[Cell], list[dict]]:
    """Instantiate the template for ``op``: ``(cells, gaps)``.

    Every cell of the template appears in exactly one of the two lists, so
    ``len(cells) + len(gaps)`` is the template's size and the denominator is
    visible. An access class with no template (a hook, an effect, a row the
    consumer could not classify) yields one gap named ``none``.
    """
    scoped = manifest_for(op, manifest)
    given = inputs_for(op, inputs)
    access = given.get("access", op.access)
    if access not in TEMPLATES:
        return [], [gap("none", f"no template for access {access!r}")]

    actors = scoped["actors"]
    cells: list[Cell] = []
    gaps: list[dict] = []
    for template in TEMPLATES[access]:
        role = template["actor"]
        actor = actors.get(role)
        cell_id, note = template["id"], template["note"]
        params = dict(scoped["params"])
        params.update(given.get("params") or {})

        if role == "other_tenant" and actor is None and scoped["other_tenant"]:
            actor, role = actors.get("permitted"), "permitted"
            cell_id, note = "other-scope", "permitted actor against another scope"
            params.update(scoped["other_tenant"])
        if role not in ACTORLESS_ROLES and actor is None:
            gaps.append(gap(cell_id, f"manifest has no actor for role {role!r}"))
            continue
        if role in ("anonymous", "system"):
            actor = None

        if template["target"] == "missing":
            if not scoped["missing"]:
                gaps.append(gap(cell_id, "manifest has no `missing` ids"))
                continue
            params.update(scoped["missing"])

        if template.get("fault") and not given.get("fault"):
            gaps.append(gap(cell_id, "retry-after-failure needs a `fault` in inputs (no generic fault injection)"))
            continue

        body = given.get("body")
        if template.get("body") == "invalid":
            body = given.get("invalid")
            if body is None:
                body = {} if access == "write" else None

        step = StepSpec(
            actor=actor,
            role=role,
            params=params,
            body=body,
            query=dict(given.get("query") or {}),
            headers=dict(given.get("headers") or {}),
            args=dict(given.get("args") or {}),
            when=template.get("when"),
            signature=template.get("signature"),
            fault=given.get("fault") if template.get("fault") else None,
            drain=bool(template.get("drain", True)),
        )
        repeat = int(template.get("repeat", 1))
        cells.append(Cell(id=cell_id, note=note, steps=[StepSpec.from_json(step.to_json()) for _ in range(repeat)]))
    return cells, gaps
