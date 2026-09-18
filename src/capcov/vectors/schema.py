"""The shapes every vectors module speaks: operations, steps, requests, artifacts.

Plain dataclasses with ``to_json`` / ``from_json``. The on-disk artifacts are
the contract between ``record`` (run against the incumbent) and ``replay`` (run
against the candidate), and between ``replay`` and ``rollup``; a consumer's
tooling reads them too. So the JSON shape is fixed here, versioned, and a reader
refuses a version it does not know rather than guessing at fields.

An ``Operation`` is what a consumer's operations loader hands the engine. The
engine knows nothing about where it came from: ``id`` is the consumer's stable
identity (a census row, a route name), ``keys()`` is every alias under which a
manifest or inputs file may address it, and ``request`` is whatever the named
``driver`` needs. That is the plugin seam: the engine never inspects a product's
census, it only asks for operations.

Credentials never reach an artifact. ``Request.to_json`` redacts the headers
that carry them, so a recorded vector holds the STEP (actor id, params, body)
and replay re-drives it through the same driver with the candidate's own
credentials.
"""

from __future__ import annotations

from dataclasses import dataclass, field

ACCESS_CLASSES = ("read", "write", "webhook", "scheduled", "queued", "command")
REQUEST_KINDS = ("http", "shell")

VECTORS_ARTIFACT_VERSION = 2
REPLAY_ARTIFACT_VERSION = 3

# Header names whose values are credentials. Redacted from every serialized
# request; the set is by NAME, generic across products, and deliberately small.
CREDENTIAL_HEADERS = frozenset(
    {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key", "x-auth-token"}
)


@dataclass
class Operation:
    """One boundary operation, as the consumer's loader describes it.

    ``access`` is normally one of ``ACCESS_CLASSES``; any other string is
    accepted so a consumer can carry a non-drivable row (a hook, an effect) in
    the denominator, and ``templates.cells_for`` records it as a gap.
    ``driver`` is ``None`` when the consumer knows the operation cannot be
    driven; ``notes`` may say why, and the recorder carries the last note as
    the gap reason.
    """

    id: str
    kind: str
    access: str
    driver: str | None
    request: dict = field(default_factory=dict)
    feature: str | None = None
    label: str = ""
    aliases: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("Operation.id must be a non-empty string")
        if not self.kind:
            raise ValueError(f"Operation {self.id!r}: kind must be a non-empty string")
        if not isinstance(self.access, str) or not self.access:
            raise ValueError(f"Operation {self.id!r}: access must be a non-empty string")
        if not isinstance(self.request, dict):
            raise ValueError(f"Operation {self.id!r}: request must be a dict")

    def keys(self) -> list[str]:
        """Every key a manifest or inputs override may address this operation by.

        The id first, then the label, then the consumer's aliases; duplicates
        and empties removed, order kept (a later key overrides an earlier one
        in ``templates.inputs_for``).
        """
        seen: list[str] = []
        for key in [self.id, self.label, *self.aliases]:
            if key and key not in seen:
                seen.append(key)
        return seen

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "access": self.access,
            "driver": self.driver,
            "request": dict(self.request),
            "feature": self.feature,
            "label": self.label,
            "aliases": list(self.aliases),
            "notes": list(self.notes),
        }

    @classmethod
    def from_json(cls, data: dict) -> "Operation":
        return cls(
            id=data["id"],
            kind=data["kind"],
            access=data["access"],
            driver=data.get("driver"),
            request=dict(data.get("request") or {}),
            feature=data.get("feature"),
            label=data.get("label") or "",
            aliases=list(data.get("aliases") or []),
            notes=list(data.get("notes") or []),
        )


@dataclass
class Request:
    """One concrete thing a driver built: an HTTP request or a shell invocation.

    ``kind`` is ``http`` (``method``, ``path`` with query, ``headers``, ``body``)
    or ``shell`` (``method`` is the helper mode, ``path`` a label, ``argv`` the
    arguments). ``body`` is bytes; serialization decodes it as UTF-8 and names
    a binary body by length rather than embedding it.
    """

    kind: str
    method: str
    path: str
    headers: dict = field(default_factory=dict)
    body: bytes | None = None
    argv: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.kind not in REQUEST_KINDS:
            raise ValueError(f"Request.kind must be one of {REQUEST_KINDS}, got {self.kind!r}")
        if self.body is not None and not isinstance(self.body, (bytes, bytearray)):
            raise ValueError("Request.body must be bytes or None")

    def to_json(self) -> dict:
        body: str | None = None
        if self.body is not None:
            try:
                body = bytes(self.body).decode()
            except UnicodeDecodeError:
                body = "<binary %d bytes>" % len(self.body)
        return {
            "kind": self.kind,
            "method": self.method,
            "path": self.path,
            "headers": {k: v for k, v in self.headers.items() if k.lower() not in CREDENTIAL_HEADERS},
            "body": body,
            "argv": list(self.argv),
        }

    @classmethod
    def from_json(cls, data: dict) -> "Request":
        body = data.get("body")
        return cls(
            kind=data["kind"],
            method=data["method"],
            path=data["path"],
            headers=dict(data.get("headers") or {}),
            body=body.encode() if isinstance(body, str) else None,
            argv=list(data.get("argv") or []),
        )


@dataclass
class DriverGap:
    """A driver's refusal: the step cannot be built mechanically, and this is why."""

    reason: str
    driver: str | None = None

    def to_json(self) -> dict:
        return {"gap": self.reason, "driver": self.driver}


@dataclass
class StepSpec:
    """One step of a vector, driver-neutral.

    The template fills it from the manifest and the inputs; the driver turns it
    into requests at run time once credentials exist. ``method``/``path`` are
    optional overrides (an HTTP verb and path, or a shell mode and target); when
    ``None`` the driver derives them from the operation's ``request``.

    * ``actor`` -- the manifest's actor id, ``None`` for anonymous and system.
    * ``role`` -- the template role that chose the actor (``permitted``,
      ``no_permission``, ``other_tenant``, ``anonymous``, ``system``, ``webhook``).
    * ``params`` / ``body`` / ``query`` / ``headers`` / ``args`` -- the inputs.
    * ``when`` -- ``due`` / ``not-due`` for scheduled cells.
    * ``signature`` -- ``valid`` / ``invalid`` for webhook cells.
    * ``fault`` -- the consumer-defined fault for retry-after-failure cells.
    * ``drain`` -- whether the recorder drains queues after this step.
    """

    actor: str | int | None = None
    role: str = ""
    method: str | None = None
    path: str | None = None
    params: dict = field(default_factory=dict)
    body: object = None
    query: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)
    args: dict = field(default_factory=dict)
    when: str | None = None
    signature: str | None = None
    fault: object = None
    drain: bool = True

    def to_json(self) -> dict:
        return {
            "actor": self.actor,
            "role": self.role,
            "method": self.method,
            "path": self.path,
            "params": dict(self.params),
            "body": self.body,
            "query": dict(self.query),
            "headers": dict(self.headers),
            "args": dict(self.args),
            "when": self.when,
            "signature": self.signature,
            "fault": self.fault,
            "drain": self.drain,
        }

    @classmethod
    def from_json(cls, data: dict) -> "StepSpec":
        return cls(
            actor=data.get("actor"),
            role=data.get("role") or "",
            method=data.get("method"),
            path=data.get("path"),
            params=dict(data.get("params") or {}),
            body=data.get("body"),
            query=dict(data.get("query") or {}),
            headers=dict(data.get("headers") or {}),
            args=dict(data.get("args") or {}),
            when=data.get("when"),
            signature=data.get("signature"),
            fault=data.get("fault"),
            drain=bool(data.get("drain", True)),
        )


@dataclass
class Cell:
    """One template cell instantiated for one operation: the steps to drive."""

    id: str
    steps: list[StepSpec] = field(default_factory=list)
    note: str = ""

    @property
    def actor(self) -> str | int | None:
        return self.steps[0].actor if self.steps else None

    def to_json(self) -> dict:
        return {"id": self.id, "note": self.note, "steps": [s.to_json() for s in self.steps]}

    @classmethod
    def from_json(cls, data: dict) -> "Cell":
        return cls(
            id=data["id"],
            steps=[StepSpec.from_json(s) for s in data.get("steps") or []],
            note=data.get("note") or "",
        )


def gap(cell: str, reason: str) -> dict:
    """The one gap shape: which cell, and why it could not be produced."""
    return {"cell": cell, "reason": reason}


@dataclass
class Vector:
    """One recorded vector: the input (actor + steps) and what the oracle did.

    ``expected`` is ``{"steps": [{"status", "body"}, ...], "delta": {...},
    "informational": {...}}`` exactly as recorded -- raw, not normalized.
    """

    id: str
    cell: str
    actor: str | int | None
    steps: list[StepSpec]
    expected: dict
    seconds: float = 0.0
    note: str = ""

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "cell": self.cell,
            "input": {"actor": self.actor, "steps": [s.to_json() for s in self.steps]},
            "expected": self.expected,
            "seconds": self.seconds,
            "note": self.note,
        }

    @classmethod
    def from_json(cls, data: dict) -> "Vector":
        given = data.get("input") or {}
        return cls(
            id=data["id"],
            cell=data.get("cell") or "",
            actor=given.get("actor"),
            steps=[StepSpec.from_json(s) for s in given.get("steps") or []],
            expected=dict(data.get("expected") or {}),
            seconds=float(data.get("seconds") or 0.0),
            note=data.get("note") or "",
        )


def _require_version(data: dict, expected: int, what: str) -> None:
    found = data.get("version")
    if found != expected:
        raise ValueError(f"{what}: version must be {expected}, got {found!r}")


@dataclass
class VectorsArtifact:
    """``<out_dir>/<operation>/vectors.json``: what record wrote for one operation."""

    operation: str
    template_version: str
    declared_stores: object
    provenance: dict
    vectors: list[Vector] = field(default_factory=list)
    gaps: list[dict] = field(default_factory=list)
    required_cells: list[str] = field(default_factory=list)
    version: int = VECTORS_ARTIFACT_VERSION

    def to_json(self) -> dict:
        return {
            "version": self.version,
            "operation": self.operation,
            "template_version": self.template_version,
            "declared_stores": self.declared_stores,
            "provenance": dict(self.provenance),
            "vectors": [v.to_json() for v in self.vectors],
            "gaps": [dict(g) for g in self.gaps],
            "required_cells": list(self.required_cells),
        }

    @classmethod
    def from_json(cls, data: dict) -> "VectorsArtifact":
        _require_version(data, VECTORS_ARTIFACT_VERSION, "vectors artifact")
        return cls(
            operation=data["operation"],
            template_version=data.get("template_version") or "",
            declared_stores=data.get("declared_stores", "*"),
            provenance=dict(data.get("provenance") or {}),
            vectors=[Vector.from_json(v) for v in data.get("vectors") or []],
            gaps=[dict(g) for g in data.get("gaps") or []],
            required_cells=list(data.get("required_cells") or []),
            version=data["version"],
        )


@dataclass
class ReplayResult:
    """One vector replayed: pass, or the first difference that failed it."""

    id: str
    cell: str
    passed: bool
    difference: str | None = None

    def to_json(self) -> dict:
        return {"id": self.id, "cell": self.cell, "pass": self.passed, "difference": self.difference}

    @classmethod
    def from_json(cls, data: dict) -> "ReplayResult":
        passed = data.get("pass")
        if type(passed) is not bool:
            raise ValueError("replay result pass must be a boolean")
        return cls(
            id=data["id"],
            cell=data.get("cell") or "",
            passed=passed,
            difference=data.get("difference"),
        )


@dataclass
class ReplayArtifact:
    """``<out_dir>/<operation>/replay.json``: what replay found for one operation.

    ``gaps_recorded`` is carried from the vectors artifact so the rollup can
    show the cells that never ran beside the ones that did: a passing count
    over a shrunken denominator is not a pass.

    Version 3 adds ``comparison_policy``: the exact normalizer source/config
    identity used by the replay, separate from copied vectors recording
    provenance. Older replay artifacts are refused by version check.
    """

    operation: str
    mutant: str | None
    vectors_recorded: int
    vectors_passing: int
    gaps_recorded: int
    candidate_sha256: str | None
    vectors_provenance: dict
    comparison_policy: dict
    vectors_sha256: str | None = None
    run_id: str | None = None
    operational_failure: str | None = None
    results: list[ReplayResult] = field(default_factory=list)
    version: int = REPLAY_ARTIFACT_VERSION

    def to_json(self) -> dict:
        return {
            "version": self.version,
            "operation": self.operation,
            "mutant": self.mutant,
            "vectors_recorded": self.vectors_recorded,
            "vectors_passing": self.vectors_passing,
            "gaps_recorded": self.gaps_recorded,
            "candidate_sha256": self.candidate_sha256,
            "vectors_provenance": dict(self.vectors_provenance),
            "comparison_policy": dict(self.comparison_policy),
            "vectors_sha256": self.vectors_sha256,
            "run_id": self.run_id,
            "operational_failure": self.operational_failure,
            "results": [r.to_json() for r in self.results],
        }

    @classmethod
    def from_json(cls, data: dict) -> "ReplayArtifact":
        _require_version(data, REPLAY_ARTIFACT_VERSION, "replay artifact")
        if not isinstance(data.get("comparison_policy"), dict):
            raise ValueError("replay artifact comparison_policy must be an object")
        for name in ("vectors_recorded", "vectors_passing", "gaps_recorded"):
            if type(data.get(name)) is not int or data[name] < 0:
                raise ValueError(f"replay artifact {name} must be a non-negative integer")
        return cls(
            operation=data["operation"],
            mutant=data.get("mutant"),
            vectors_recorded=data["vectors_recorded"],
            vectors_passing=data["vectors_passing"],
            gaps_recorded=data["gaps_recorded"],
            candidate_sha256=data.get("candidate_sha256"),
            vectors_provenance=dict(data.get("vectors_provenance") or {}),
            comparison_policy=dict(data["comparison_policy"]),
            vectors_sha256=data.get("vectors_sha256"),
            run_id=data.get("run_id"),
            operational_failure=data.get("operational_failure"),
            results=[ReplayResult.from_json(r) for r in data.get("results") or []],
            version=data["version"],
        )
