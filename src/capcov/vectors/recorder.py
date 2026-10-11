"""Record boundary vectors against the incumbent: the loop, the layout, the seams.

``record`` is the whole recording design in one loop. For every operation the
consumer's loader hands over, the fixed template yields cells; for every cell
the driver builds requests; then, per vector: restore the fixture to the
snapshot, inspect, send every request through the oracle, drain after each
step unless the cell says not to, inspect again, and diff the two inspections
into the declared delta and the informational remainder. What could not be
driven is a gap beside the vectors, never a shorter list.

The three seams are objects the consumer supplies, by plugin string from the
CLI or directly from Python:

* **fixture**  -- ``vectors.fixture.Fixture``: ``snapshot`` / ``restore`` /
  ``inspect`` / ``drain``.
* **oracle**   -- the incumbent. ``base_url`` (``None`` when it serves no HTTP,
  so every HTTP vector is a gap rather than a connection error), ``send(Request)
  -> (status, body)``, ``run_shell(Request) -> (status, body)``. Optionally
  ``credential(actor) -> dict | None`` (``{"bearer"}``, ``{"cookie"}``,
  ``{"headers"}``, ``{"secret"}``); when the oracle has no such method the
  manifest's ``credentials`` table, keyed by ``str(actor)``, is read instead.
  An actor with neither is a driver gap. Optionally ``drain()``, used in place
  of the fixture's when the incumbent's queues are worked by its own tooling.
* **candidate** -- the rebuild, same surface, in ``replay``.

Layout: ``<out_dir>/<operation>/vectors.json`` (``replay.json`` beside it), one
directory per operation, the directory named by the operation id percent-
encoded so any id round-trips (``op_dir`` / ``op_id_of``). Both files are
capcov artifacts: ``kind`` and ``derived_from`` on top, the versioned vectors
or replay document beneath, so ``artifacts.read`` refuses the wrong kind.

Refusals. An oracle whose ``send`` returns something other than a two-tuple is
a wiring error naming the seam. An id the layout cannot hold (empty, ``.`` or
``..``) is refused. Nothing here catches an exception from the fixture: a
restore that fails is not a vector that passed.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from .. import artifacts
from .fixture import ObservingFixture
from . import diff, drivers, normalize, templates
from .schema import Cell, DriverGap, Operation, Request, StepSpec, Vector, VectorsArtifact, gap

VECTORS_FILE = "vectors.json"
REPLAY_FILE = "replay.json"
VECTORS_KIND = "vectors"
REPLAY_KIND = "vectors-replay"
EXTRACTOR = "capcov.vectors"


class SeamError(RuntimeError):
    """A consumer-supplied object does not honour its contract; the message names the seam."""


def op_dir(out_dir: Path, op_id: str) -> Path:
    """The per-operation directory: the id percent-encoded so it round-trips."""
    if not op_id or op_id in (".", ".."):
        raise ValueError(f"operation id {op_id!r} cannot name a directory")
    return Path(out_dir) / urllib.parse.quote(op_id, safe="")


def op_id_of(directory: Path) -> str:
    """The operation id a directory under ``out_dir`` stands for."""
    return urllib.parse.unquote(Path(directory).name)


def credential_for(actor, endpoint, manifest: Mapping | None) -> dict | None:
    """The credential for an actor: the endpoint's own lookup, else the manifest table."""
    if actor is None:
        return None
    lookup = getattr(endpoint, "credential", None)
    if callable(lookup):
        found = lookup(actor)
        if found is not None:
            return dict(found)
    table = (manifest or {}).get("credentials") or {}
    found = table.get(str(actor), table.get(actor))
    if isinstance(found, str):
        # A bare string is the common shorthand for a cookie header value.
        return {"cookie": found}
    return dict(found) if isinstance(found, Mapping) else None


def _drain_of(endpoint, fixture) -> Callable[[], None]:
    own = getattr(endpoint, "drain", None)
    return own if callable(own) else fixture.drain


def _exchange(endpoint, request: Request) -> tuple[object, object]:
    """Send one request through the endpoint; ``(status, body)`` or a named refusal."""
    if request.kind == "http":
        result = endpoint.send(request)
    else:
        result = endpoint.run_shell(request)
    if not isinstance(result, tuple) or len(result) != 2:
        which = "send" if request.kind == "http" else "run_shell"
        raise SeamError(
            f"{type(endpoint).__name__}.{which} must return (status, body), got {type(result).__name__}"
        )
    return result


def plan_cell(op: Operation, cell: Cell, endpoint, manifest: Mapping | None, inputs_entry: Mapping):
    """Build every step's requests up front; the first gap is the cell's gap."""
    planned: list[tuple[StepSpec, list[Request]]] = []
    base_url = getattr(endpoint, "base_url", None)
    for step in cell.steps:
        built = drivers.drive(op, step, credential_for(step.actor, endpoint, manifest), base_url or "", inputs_entry)
        if isinstance(built, DriverGap):
            return built
        if base_url is None and any(r.kind == "http" for r in built):
            return DriverGap(f"endpoint {type(endpoint).__name__} serves no HTTP (base_url is None)", op.driver)
        planned.append((step, built))
    return planned


class ActorLost(RuntimeError):
    """The run's own credentialed actor stopped being accepted mid-run.

    One refusal on a permitted cell is a fact about one operation. A refusal
    on a cell that the SAME credential passed earlier in the run is a fact
    about the fixture -- a token that lapsed, a session the restore did not
    bring back -- and every vector recorded after it would be the incumbent
    refusing a dead credential. The recorder stops and names the vector.
    """


class Session:
    """Per-run state the recorder threads through every vector: whether the
    fixture is known to be at the snapshot, so a clean read does not pay a
    restore before the next vector; and which actors have been accepted, so a
    later refusal of the same actor halts the run instead of poisoning it."""

    REFUSED = (401,)

    def __init__(self, fixture, token, halt_on_actor_loss: bool = True,
                 credential_lost: Callable[[int, object], bool] | None = None) -> None:
        self.fixture = fixture
        self.token = token
        self.pristine = False
        self.restores = 0
        self.halt_on_actor_loss = halt_on_actor_loss
        # Systems reuse 401 for "who are you" and for "you may not"; only the
        # first means the fixture lost a credential. The consumer knows its
        # system's phrases and says which refusals are credential loss. With
        # no predicate every 401 counts, which is the conservative reading.
        self.credential_lost = credential_lost or (lambda status, body: status in self.REFUSED)
        self.accepted_actors: set = set()
        # A fixture says whether it can observe (a composition whose stores all
        # can); a bare protocol check would pass for any composition, observing
        # or not, because the methods exist and refuse at call time.
        flag = getattr(fixture, "observing", None)
        self.observing = bool(flag) if flag is not None else isinstance(fixture, ObservingFixture)

    def note_actor(self, actor, statuses: list[int], label: str, bodies: list | None = None) -> None:
        """Record that ``actor`` was accepted, or halt if it was accepted before and is refused now.

        The halt carries the system's own refusal text: a lapsed session, a
        revoked token and a permission denial can all be 401 and each points
        at a different fault in the fixture."""
        if actor is None or not statuses:
            return
        bodies = list(bodies or [None] * len(statuses))
        if all(status in self.REFUSED for status in statuses):
            lost = all(self.credential_lost(status, body) for status, body in zip(statuses, bodies))
            if lost and self.halt_on_actor_loss and actor in self.accepted_actors:
                said = json.dumps(bodies[0], default=str)[:300] if bodies else ""
                raise ActorLost(f"actor {actor!r} was accepted earlier in this run and is now refused "
                                f"({statuses}) on {label}; the system said {said}; "
                                f"the fixture no longer holds a live credential for it")
            return
        self.accepted_actors.add(actor)

    def ensure_pristine(self) -> None:
        if not self.pristine:
            self.fixture.restore(self.token)
            self.restores += 1
            self.pristine = True


def _split_observed(changes: dict, declared, informational: tuple[str, ...]) -> tuple[dict, dict]:
    """Route an observed delta into (compared, informational) by the same rules
    ``diff.store_delta`` applies to an inspection pair."""
    compared, info = {}, {}

    def keep(kind: str, name: str) -> bool:
        names = diff._declared_names(declared, diff.ROW_KINDS if kind == "rows" else diff.COLLECTION_KINDS)
        return names is None or name in names

    for table, change in sorted(changes.get("rows", {}).items()):
        target = info if table in informational or not keep("rows", table) else compared
        target[f"rows.{table}"] = change
    for collection, change in sorted(changes.get("collections", {}).items()):
        (compared if keep("collections", collection) else info)[f"collections.{collection}"] = change
    for key, value in (changes.get("informational") or {}).items():
        info[key] = value
    if changes.get("unbound"):
        info["unbound_tables"] = list(changes["unbound"])
    return compared, info


def run_vector(
    planned: list[tuple[StepSpec, list[Request]]],
    fixture,
    token,
    endpoint,
    declared,
    informational: Iterable[str] = (),
    session: "Session | None" = None,
) -> dict:
    """Run every step from the snapshot, drain, and record what changed.

    With an ``ObservingFixture`` the delta comes from the stores' own change
    logs (mark, run, changes_since) and the restore is skipped when the vector
    changed nothing. Otherwise it is the full inspect-before/inspect-after
    diff. The result carries ``observed_by`` so the provenance says which.
    """
    session = session or Session(fixture, token)
    session.ensure_pristine()
    drain = _drain_of(endpoint, fixture)
    steps = []
    actor = planned[0][0].actor if planned else None
    label = f"{planned[0][1][0].method} {planned[0][1][0].path}" if planned and planned[0][1] else "<no request>"
    if session.observing:
        mark = fixture.mark()
        for step, requests in planned:
            for request in requests:
                status, body = _exchange(endpoint, request)
                steps.append({"request": request.to_json(), "status": status, "body": body})
            if step.drain:
                drain()
        session.note_actor(actor, [step["status"] for step in steps], label, [step["body"] for step in steps])
        changes = fixture.changes_since(mark)
        compared, info = _split_observed(changes, declared, tuple(informational))
        # Pristine means nothing that matters changed: a row in a table the run
        # declared informational (a token's expiry bump on every request) is
        # reported but does not force a restore before the next vector.
        substantive = any(key.startswith(("rows.", "collections.")) and key.split(".", 1)[1] not in informational
                          for key in list(compared) + list(info))
        session.pristine = not substantive and not changes.get("unbound")
        if changes.get("unbound"):
            # A change the observer could not attribute: fall back to a full
            # diff next time by forcing a restore, and say so in the record.
            info["observed_fallback"] = "unbound tables; restore forced"
        return {"steps": steps, "delta": compared, "informational": info, "observed_by": "change-log"}
    before = fixture.inspect()
    for step, requests in planned:
        for request in requests:
            status, body = _exchange(endpoint, request)
            steps.append({"request": request.to_json(), "status": status, "body": body})
        if step.drain:
            drain()
    session.note_actor(actor, [step["status"] for step in steps], label, [step["body"] for step in steps])
    after = fixture.inspect()
    compared, info = diff.store_delta(before, after, declared, tuple(informational))
    session.pristine = False
    return {"steps": steps, "delta": compared, "informational": info, "observed_by": "inspect-diff"}


def informational_for(op: Operation, inputs: Mapping, informational: Iterable[str] = ()) -> tuple[str, ...]:
    """Never-compared tables: the run-wide list plus the operation's own."""
    given = templates.inputs_for(op, inputs).get("informational") or ()
    return tuple(dict.fromkeys([*informational, *given]))


def _sha256_json(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def record(
    operations: Iterable[Operation],
    fixture,
    oracle,
    manifest: Mapping,
    inputs: Mapping,
    out_dir: Path,
    provenance: Mapping | None = None,
    informational: Iterable[str] = (),
    log: Callable[[str], None] | None = None,
) -> dict:
    """Record every operation's vectors against the oracle; one artifact per operation.

    Returns a summary ``{"operations": [{id, vectors, gaps}], "vectors_recorded",
    "gaps_recorded"}``. ``provenance`` is whatever the consumer knows about the
    incumbent (a snapshot hash, a seed hash, a version); the recorder adds the
    template and normalization versions and hashes of the manifest and inputs.
    """
    out_dir = Path(out_dir)
    say = log or (lambda line: None)
    prov = dict(provenance or {})
    prov.update(
        {
            "template_version": templates.TEMPLATE_VERSION,
            "normalization_version": normalize.NORMALIZATION_VERSION,
            "manifest_sha256": _sha256_json(manifest),
            "inputs_sha256": _sha256_json(inputs),
        }
    )
    token = fixture.snapshot()
    session = Session(fixture, token)
    prov["observed_by"] = "change-log" if session.observing else "inspect-diff"
    summary: dict = {"operations": [], "vectors_recorded": 0, "gaps_recorded": 0, "restores": 0}
    for op in operations:
        cells, gaps = templates.cells_for(op, manifest, inputs)
        declared = templates.declared_for(op, inputs)
        never = informational_for(op, inputs, informational)
        inputs_entry = templates.inputs_for(op, inputs)
        vectors: list[Vector] = []
        for cell in cells:
            planned = plan_cell(op, cell, oracle, manifest, inputs_entry)
            if isinstance(planned, DriverGap):
                gaps.append(gap(cell.id, planned.reason))
                continue
            started = time.monotonic()
            result = run_vector(planned, fixture, token, oracle, declared, never, session)
            vectors.append(
                Vector(
                    id=cell.id,
                    cell=cell.id,
                    actor=cell.actor,
                    steps=list(cell.steps),
                    expected=result,
                    seconds=round(time.monotonic() - started, 3),
                    note=cell.note,
                )
            )
            say(
                f"recorded {op.id} {cell.id:<20} status={[s['status'] for s in result['steps']]} "
                f"delta={sorted(result['delta'])} info={sorted(result['informational'])}"
            )
        for g in gaps:
            say(f"gap      {op.id} {g['cell']:<20} {g['reason']}")
        artifact = VectorsArtifact(
            operation=op.id,
            template_version=templates.TEMPLATE_VERSION,
            declared_stores=declared,
            provenance=prov,
            vectors=vectors,
            gaps=gaps,
            required_cells=sorted({cell.id for cell in cells} | {item["cell"] for item in gaps}),
        )
        write_vectors(out_dir, op, artifact)
        summary["operations"].append({"id": op.id, "vectors": len(vectors), "gaps": [g["reason"] for g in gaps]})
        summary["vectors_recorded"] += len(vectors)
        summary["gaps_recorded"] += len(gaps)
    summary["restores"] = session.restores
    return summary


def write_vectors(out_dir: Path, op: Operation, artifact: VectorsArtifact) -> Path:
    """Publish one operation's vectors as a capcov artifact of kind ``vectors``."""
    body = artifact.to_json()
    body["operation_record"] = op.to_json()
    body["publication"] = "private-evidence"
    derived_from = artifacts.provenance(
        f"vectors:{op.id}", _sha256_json(artifact.provenance), EXTRACTOR + ".record", len(artifact.vectors)
    )
    path = op_dir(out_dir, op.id) / VECTORS_FILE
    artifacts.write(path, VECTORS_KIND, derived_from, body)
    path.chmod(0o600)
    return path


def read_vectors(path: Path) -> VectorsArtifact:
    """Read a vectors artifact; the wrong kind or an unknown version is refused."""
    doc = artifacts.read(Path(path), expect_kind=VECTORS_KIND)
    return VectorsArtifact.from_json(doc)


def plan(
    operations: Iterable[Operation], manifest: Mapping, inputs: Mapping, placeholder_credential: Mapping | None = None
) -> list[dict]:
    """What record WOULD drive, with no oracle: per operation, the cells and the gaps.

    Steps with an actor are built against a placeholder credential (a bearer
    token by default) so a driver's own refusals -- a missing path parameter,
    an unresolved selector -- show up before anything is recorded.
    """
    credential = dict(placeholder_credential or {"bearer": "plan"})

    class _Planner:
        base_url = ""

        @staticmethod
        def credential(actor):
            return credential

    out = []
    for op in operations:
        cells, gaps = templates.cells_for(op, manifest, inputs)
        inputs_entry = templates.inputs_for(op, inputs)
        rows = []
        for cell in cells:
            planned = plan_cell(op, cell, _Planner, None, inputs_entry)
            if isinstance(planned, DriverGap):
                gaps.append(gap(cell.id, planned.reason))
                continue
            rows.append({"id": cell.id, "requests": [r.to_json() for _, built in planned for r in built]})
        out.append(
            {
                "id": op.id,
                "label": op.label,
                "kind": op.kind,
                "access": op.access,
                "driver": op.driver,
                "cells": rows,
                "gaps": gaps,
            }
        )
    return out


def replay(operations, fixture, candidate, out_dir, mutant=None, **options):
    """``replay.replay``, re-exported: the other half of the loop, in its own file."""
    from .replay import replay as _replay

    return _replay(operations, fixture, candidate, out_dir, mutant, **options)


def read_replay(path):
    """``replay.read_replay``, re-exported."""
    from .replay import read_replay as _read

    return _read(path)
