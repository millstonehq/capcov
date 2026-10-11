"""Export an observation receipt directory as claim facts + evidence (Lane A, judge side).

A check harness is a *producer of runtime observations*, never an oracle.  This
module turns one receipt directory -- written by a check that put one
pre-registered scenario set to an incumbent implementation and a candidate
implementation under one nonce, one fixture and one comparison policy -- into a
validated claims ``Bundle`` whose every fact carries the receipt's ``run``
context (or the ``policy`` / ``scenario_set`` context for the relations scoped
to those), whose every fact has an ``Evidence`` record with a content-derived
id, an explicit ``depends_on`` chain and a ``source`` naming the *producer
class* the row is attributed to, and whose completeness witnesses are emitted
only where the receipt's ``completeness`` block says the corresponding table is
closed.  The bundle's identity is a pure function of the exported rows.

It mirrors ``capcov.claims.replay.replay_facts`` deliberately -- same
``ExportResult``, same ``_Facts`` accumulator, same identity/receipt separation
-- so a reader of one can read the other.

NO MODEL.  Neither the schema nor this exporter knows what a model is.  There
is no model column, no model producer class and no way to ingest one, so the
claim built on these rows is model-free *by construction*.  What that costs is
stated, not hidden: ``model_conformance`` is a mandatory ``unassessed`` row and
a positive, closed premise of the claim, so the claim does not derive at all
unless the receipt states in exported rows that it assessed no model.

RECEIPT DIRECTORY CONTRACT
--------------------------
``<root>/.work/observation/<check>/`` holds ``receipt.json`` and required
``log.txt``, plus a receipt-local ``observation_admissions.json`` only for
fixture tests. Production calls
must pass admissions as an external file with an independently captured digest;
the receipt-local copy is available only with the explicit fixture-test option.
``receipt.json`` is ONE canonical JSON document (sorted keys, UTF-8) with the
blocks of ``REQUIRED_BLOCKS``; ``repeat`` is the only optional block.  Every
digest is lowercase 64-hex sha256 and ``run.id`` is its first 16 hex.

Why one document and not a directory of ``<relation>.json`` files as the replay
receipt uses: 49 heterogeneous checks cannot each hand-write 17 relation files
without the emitter becoming the only real author anyway, and one document
keeps a single place where a lie must be told -- which this exporter can then
cross-check against itself.  ``results`` and ``agreement`` are the harness's own
summary and are RECOMPUTED here from ``observations``; a receipt whose summary
disagrees with its own rows is refused (R-4, R-10).  The directory envelope is
kept so ``log.txt``'s bytes can be hashed and verified.

``observation_admissions.json`` is the reviewer's ledger, the same file shape
and staleness binding ``model_scope_exclusions.json`` has for the replay pack::

    {"producer": "reviewer <name>",      # optional; first token must be reviewer
     "reviewer": "<name>", "reviewed_at": "<date>",
     "reviewed_against": {"policy": "<digest>", "scenario_set": "<digest>",
                          "check": "<check id>"},
     "rows": [{"relation": "policy_admitted", "version": "<v>"},
              {"relation": "scenario_set_admitted", "version": "<v>"},
              {"relation": "masked_difference_admitted",
               "normalization": "<id>", "field_path": "<path>"}, ...]}

Every row may carry ONE optional extra key, ``note``: free text for a human
reviewer.  It is validated as a non-empty string and then DROPPED.  No rule
reads it, no relation carries it, it reaches no ``Evidence.source`` and no
bundle metadata, so two ledgers that differ only in their notes export the same
bundle with the same ``observation_digest``.  It is the ledger's ONLY tolerance:
every other unknown key is still refused (R-2).  Nothing a reviewer writes there
can widen a mask, admit a difference or change a verdict -- a note that needed
to be read would have to become a relation of its own first.

``reviewed_against`` that does not match the receipt's digests is ``stale``:
the review is real, it is simply not a review of this policy or this set.  A
missing ledger is not a refusal -- it is an absent premise, and the judge then
withholds the claim rather than inferring an admission.

NON-WAIVABLE REFUSALS
---------------------
``REFUSALS`` names them.  Every refusal message begins with its rule id
(``"R-7: ..."``), which is the stable part of the contract: the prose after the
id names the offending row.  All of R-1..R-15 are ``invalid-input`` -- a
contract finding the caller reports, never something to work around.  They are
where the *facet gaps* are stopped: an observation with no status, a digest
that does not recompute, a normalization that changed a value without being
declared.  Datalog cannot see a field that was never written down, so nothing
downstream tries to.

R-EFF is a NAMED RESIDUAL, not a solved problem: effect closure is asserted
with no independent list to check it against.  This exporter's only defence is
recomputing ``effects_digest`` from the ``effects`` rows (R-3), which catches
inconsistency, never under-observation.  The per-effect rows themselves are
receipt evidence for that digest and are NOT exported as relations: the digest
is the unit ``effects_disagree`` compares, no rule read a row, and the schema
declares no relation for one (its original ``observation_effect`` /
``observation_effect_rows_closed`` were removed as coverage-shaped dead
weight; the receipt's ``effects`` rows and ``by_side.<side>.effect_rows`` box
are unchanged on the wire).

R-9 has a ONE-SIDED form (``_read_firings``): a normalization that fired on one
side only, where the two sides' raw digests for that facet differ and their
normalized digests match, has erased a real wire difference and must be
exported as a ``masked_differences`` row -- which the judge then refuses to
negate over without a reviewer admission -- exactly as a two-sided mask must.
The judge carries its own copy of that rule over ``observation_body_raw``
(``body_masked`` / ``observation_mask_undisclosed``), so an erased body
difference blocks the claim even if a row reached the bundle without this
refusal.

IDENTITY
--------
The bundle's identity (metadata ``observation_digest``, kind
``OBSERVATION_IDENTITY = "observation-relations-v1"``) is a content digest of
the normalized relations the exporter emits, never of the files' bytes::

    observation_digest = sha256("observation-relations-v1:" + canonical_json({
        "relations": sorted(<every relation name the bundle declares>),
        "rows": {relation: sorted(canonical rows) for every exported relation
                 with at least one row}}))

Facts are assembled under a placeholder identity, the identity is computed from
the finished rows, then the evidence ids (which embed
``observation_digest[:12]`` and the row digest) are computed -- content digest,
then ids.  ``reproduce``, the receipt directory and the log's path are *run
receipts*: carried in bundle metadata under ``RECEIPT_METADATA_KEYS`` and
excluded from ``bundle_digest``, so where and when the check ran never becomes
identity.

EVIDENCE
--------
Ids are ``<prefix>:<observation12>:<relation>:<row12>`` where ``prefix`` is the
evidence-id prefix of the relation's producer class (``EVIDENCE_PREFIXES``:
``observe`` for the check harness, ``reviewer`` for the reviewer).  There are
only those two classes anywhere in the pack -- no ``shen``, ``modelcheck``,
``mut``, ``php-census``, ``replay``, ``php`` or ``go`` -- so a model row cannot
be ingested at all, and a file that claims one of those classes is refused at
ingestion (``evidence-producer``), which is the single boundary at which
producer authority is enforced.  ``Evidence.source``'s first whitespace token
is the producer class; the default is ``"<class> capcov.claims.observation.observation_facts v1"``
and a completeness witness carries the predicate whose emission condition was
checked.  Every row depends on the ``observation_run`` row and on
``external:source:<source_digest>``, ``external:fixture:<fixture>``,
``external:policy:<policy>`` and ``external:scenario-set:<scenario_set>``; a
stability row also depends on ``external:run:<run_a>`` / ``<run_b>``, the two
repeat runs, which are outside this receipt.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping

from ..ir import (Atom, Bundle, Column, Constant, Context, Evidence,
                  RelationDecl, TypeName, canonical_json, digest as ir_digest)
from ..validation import ValidationError, assert_valid
from . import load_observation_schema

EXPORT_VERSION = "v1"
EXPORTER = "capcov.claims.observation.observation_facts"
PRODUCER = f"{EXPORTER} {EXPORT_VERSION}"

STATUS_COMPLETE = "complete"
STATUS_RESOURCE_EXHAUSTED = "resource-exhausted"
STATUS_INVALID_INPUT = "invalid-input"
STATUS_STALE = "stale"

#: The one contract string this exporter accepts.  There is no forward-compat
#: guessing: another value is refused rather than interpreted (R-1).
CONTRACT = "observation-receipt/v1"
RECEIPT_FILE = "receipt.json"
LOG_FILE = "log.txt"
ADMISSIONS_FILE = "observation_admissions.json"
#: Reviewer strings that name nobody.  A ledger signed by one of these is treated
#: exactly like an absent ledger.
UNSIGNED_REVIEWERS = frozenset({"unassigned", "unsigned", "none", "nobody", "tbd", "placeholder", ""})
#: The ONE optional per-row key of the admissions ledger.  Free text for a human
#: reviewer: validated as a non-empty string, then dropped.  It is read by no
#: rule, carried into no relation, and absent from the exported bundle, so it
#: cannot reach a verdict by any path.  Every other unknown row key is R-2.
ADMISSION_ROW_NOTE = "note"
#: The keys each admission relation REQUIRES.  ``ADMISSION_ROW_NOTE`` is the only
#: key beyond these that a row may carry.
ADMISSION_ROW_KEYS = {
    "policy_admitted": ("relation", "version"),
    "scenario_set_admitted": ("relation", "version"),
    "masked_difference_admitted": ("relation", "normalization", "field_path"),
}

OBSERVATION_IDENTITY = "observation-relations-v1"
_IDENTITY_PREFIX = OBSERVATION_IDENTITY + ":"
_PLACEHOLDER_IDENTITY = "0" * 64
#: Carried in metadata, excluded from ``bundle_digest``: where and when, not what.
#: The log's DIGEST stays in the identity -- it is content; only the reproduce
#: block and the directory the receipt was read from are run receipts.
RECEIPT_METADATA_KEYS = ("reproduce", "receipt_dir")

EVIDENCE_PREFIXES = {"observe": "observe", "reviewer": "reviewer"}
_UNOWNED_PREFIX = "observe"

SIDES = ("incumbent", "candidate")
CLASSES = ("2xx", "3xx", "4xx", "5xx")
SCENARIO_KINDS = ("success", "denial", "malformed", "effect", "repeat")
UNOBSERVED_REASONS = ("setup_failed", "timeout", "error", "skipped")
OBSERVATION_OUTCOMES = ("observed", "setup_failed", "timeout", "error")
OBSERVATION_FAILURE_STAGES = ("preflight", "fixture", "seed", "serve", "request",
                              "compare", "teardown")
FAILURE_PHASES = ("preflight", "fixture", "seed", "incumbent_start", "candidate_start",
                  "observe", "compare", "teardown")
FAILURE_CLASSES = ("setup", "timeout", "difference", "internal")
#: A failure of any of these classes forces a non-``compared`` terminal (R-10).
BLOCKING_FAILURE_CLASSES = ("setup", "timeout", "internal")
TERMINAL_OUTCOMES = ("compared", "setup_failed", "timed_out", "aborted")
NORMALIZATION_KINDS = ("canonicalize", "sort", "round", "mask", "drop")
PRESERVES = ("identity", "presence", "none")
STORE_KINDS = ("mysql", "redis", "mongo", "smtp", "fs")
EFFECT_KINDS = ("insert", "update", "delete")
RESULT_OUTCOMES = ("agree", "differ", "not_observed", "degenerate")
FACETS = ("status", "body", "effects")

REQUIRED_BLOCKS = ("contract", "check", "sources", "run", "configuration", "fixture",
                   "policy", "scenario_set", "observations", "normalization_firings",
                   "masked_differences", "results", "unobserved", "failures", "terminal",
                   "agreement", "completeness", "unassessed", "reproduce", "log",
                   "receipt_digest")
OPTIONAL_BLOCKS = ("repeat",)

COMPLETENESS_KEYS = ("scenarios", "unobserved", "normalizations", "masked_differences",
                     "failures", "unassessed", "stability", "policy")
BY_SIDE_KEYS = ("observed", "statuses", "classes", "bodies", "effects", "effect_rows")

#: ``unassessed`` is a closed vocabulary.  Every ALWAYS dimension is mandatory on
#: every receipt; the CONDITIONAL ones are mandatory when their condition holds
#: (R-12).  ``model_conformance`` is the one without which the claim does not
#: derive at all -- it is a positive premise, not an omission.
UNASSESSED_ALWAYS = ("model_conformance", "fault_sensitivity", "scenario_coverage",
                     "write_set_declaration", "producer_independence", "deployed_parity",
                     "concurrency", "performance", "effect_ordering")
UNASSESSED_CONDITIONAL = ("persisted_effects", "oracle_stability")
UNASSESSED_VOCABULARY = UNASSESSED_ALWAYS + UNASSESSED_CONDITIONAL

#: R-13.  The receipt and the log are scanned for these verbatim.  The list is
#: also recorded in the receipt's ``log.redaction.markers_checked`` so a
#: weakened scan is visible in the diff rather than only in behaviour.
CREDENTIAL_MARKERS = ("Bearer ", "X-AUTH-", '"access"', '"second"', "password",
                      "secret", "token=")

#: R-14.  A receipt, log or ledger that names a string its producer must not
#: publish is refused rather than quietly republished.  The package names no
#: such string itself: a private harness passes its own list to
#: ``export_bundle(..., forbidden_strings=...)``, and the default is empty.
FORBIDDEN_FORK_STRINGS: tuple[str, ...] = ()

#: The non-waivable exporter refusals.  Keys are the stable prefix of every
#: message; tests pin the prefix, humans read the prose after it.
REFUSALS = {
    "R-1": "the contract string is not observation-receipt/v1",
    "R-2": "a required block is missing, or the candidate tree was dirty",
    "R-3": "a declared digest does not recompute from the content it names",
    "R-4": "results is not a bijection with the scenario set, or disagrees with recomputation",
    "R-5": "observations is not a bijection with (scenarios x sides) minus unobserved",
    "R-6": "an observed entry has no status, no digests, or a class inconsistent with its status",
    "R-7": "a normalized value differs from its raw value with no normalization named",
    "R-8": "a normalization or policy target is undeclared, malformed, or normalizes status",
    "R-9": "a normalization masked a real difference that masked_differences does not export",
    "R-10": "agreement does not recompute, or a compared terminal carries a blocking failure",
    "R-11": "a completeness entry is open with no reason, or closed against the declared set",
    "R-12": "a mandatory unassessed dimension is missing, or unassessed is not closed",
    "R-13": "a credential marker appears in the receipt or the log",
    "R-14": "a string forbidden in the public fork appears in the receipt",
    # A receipt that does not claim two distinct sides is not a comparison: an
    # agreement it reports may be one process answering twice.  That is refused
    # here rather than noted in a message the reader may never print.
    "R-15": "the receipt does not claim, or contradicts, two distinct sides",
}

_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")
_HEX40 = re.compile(r"\A[0-9a-f]{40}\Z")
_HEX16 = re.compile(r"\A[0-9a-f]{16}\Z")


# ---------------------------------------------------------------------------
# Public parameter / result types


@dataclass(frozen=True)
class ExportLimits:
    """Finite execution bars.  Tripping one is ``resource-exhausted``, never an
    exception and never silently raised.  ``rows`` mirrors the Soufflé kernel's
    100k-row cap over every relation, inputs included; ``file_bytes`` bounds
    what is read from ``receipt.json``, ``log.txt`` or the admissions ledger."""
    rows: int = 100_000
    file_bytes: int = 64 * 1024 * 1024


@dataclass(frozen=True)
class ExportResult:
    status: str
    bundle: Bundle | None
    counts: dict[str, int] = field(default_factory=dict)
    messages: tuple[str, ...] = ()


class ExportInputError(ValueError):
    """A receipt block or row broke an exporter precondition (R-1..R-15)."""


class StaleReceiptError(ExportInputError):
    """The reviewer's admissions ledger reviewed something other than this receipt."""


def _refuse(rule: str, detail: str) -> ExportInputError:
    return ExportInputError(f"{rule}: {detail}")


def refusal_rule(message: str) -> str | None:
    """The refusal rule a message belongs to (``"R-7"``), or ``None``."""
    head = message.split(":", 1)[0]
    return head if head in REFUSALS else None


# ---------------------------------------------------------------------------
# Identity helpers


def row_digest(relation: str, row: list[Any]) -> str:
    return hashlib.sha256(canonical_json([relation, list(row)]).encode("utf-8")).hexdigest()


def evidence_prefix(relation: RelationDecl) -> str:
    """The evidence-id prefix of a relation's producer class."""
    if relation.producer_classes:
        return EVIDENCE_PREFIXES.get(relation.producer_classes[0], _UNOWNED_PREFIX)
    return _UNOWNED_PREFIX


def evidence_id(observation_digest: str, relation: RelationDecl, row: list[Any]) -> str:
    """``<prefix>:<observation12>:<relation>:<row12>``."""
    return (f"{evidence_prefix(relation)}:{observation_digest[:12]}:{relation.name}:"
            f"{row_digest(relation.name, row)[:12]}")


def observation_relations_identity(rows_by_relation: Mapping[str, Iterable[list[Any]]],
                                   relation_names: Iterable[str]) -> str:
    """The ``observation-relations-v1`` identity of a set of exported rows."""
    payload = {
        "relations": sorted(set(relation_names)),
        "rows": {relation: sorted((list(row) for row in rows), key=canonical_json)
                 for relation, rows in rows_by_relation.items() if rows},
    }
    return hashlib.sha256((_IDENTITY_PREFIX + canonical_json(payload)).encode("utf-8")).hexdigest()


def _relation_from_json(raw: dict) -> RelationDecl:
    return RelationDecl(
        raw["name"],
        tuple(Column(c["name"], c["type"], c["context"]) for c in raw["columns"]),
        raw["modality"], raw["polarity"], raw["binding"], raw["primitive"],
        tuple(raw["producer_classes"]), tuple(raw["context_indices"]), raw["completes"],
        raw["finite"], raw["nonempty"], tuple(raw["compatibility_targets"]),
        tuple(raw["compatibility_context_indices"]),
    )


def observation_relations() -> tuple[RelationDecl, ...]:
    """The primitive declarations the exporter produces rows for."""
    return tuple(_relation_from_json(item) for item in load_observation_schema()["relations"])


primitive_relations = observation_relations


def default_source(relation: RelationDecl) -> str:
    """``"<class> <PRODUCER>"`` for an owned relation, ``PRODUCER`` otherwise."""
    if relation.producer_classes:
        return f"{relation.producer_classes[0]} {PRODUCER}"
    return PRODUCER


def witness_source(relation: RelationDecl, predicate: str) -> str:
    """``"<class> <EXPORTER> <predicate>"`` for an owned witness, else without the class."""
    if relation.producer_classes:
        return f"{relation.producer_classes[0]} {EXPORTER} {predicate}"
    return f"{EXPORTER} {predicate}"


# ---------------------------------------------------------------------------
# Row accumulator (the replay exporter's, unchanged in behaviour)


class _Facts:
    """Deduplicating fact/evidence accumulator.

    A row is identified by ``(relation, values)``; adding it twice merges the
    dependency sets, so a row reported twice yields one fact with one
    content-derived evidence id.
    """

    def __init__(self, observation_digest: str, relations: dict[str, RelationDecl]) -> None:
        self.observation_digest = observation_digest
        self.relations = relations
        self.rows: dict[tuple[str, tuple], dict] = {}

    def add(self, relation: str, values: dict[str, Any], *, source: str,
            depends_on: Iterable[str] = (), kind: str = "fact") -> str:
        decl = self.relations[relation]
        row: list[Any] = []
        for column in decl.columns:
            if column.name not in values:
                raise _refuse("R-2", f"{relation}: missing column {column.name!r}")
            row.append(_checked(relation, column, values[column.name]))
        key = (relation, tuple(row))
        eid = evidence_id(self.observation_digest, decl, row)
        entry = self.rows.get(key)
        if entry is None:
            entry = {"id": eid, "row": row, "source": source, "kind": kind, "depends_on": set()}
            self.rows[key] = entry
        entry["depends_on"].update(d for d in depends_on if d and d != eid)
        return eid

    def count(self, relation: str) -> int:
        return sum(1 for rel, _ in self.rows if rel == relation)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for rel, _ in self.rows:
            out[rel] = out.get(rel, 0) + 1
        return dict(sorted(out.items()))

    def __len__(self) -> int:
        return len(self.rows)

    def identity(self) -> str:
        by_relation: dict[str, list[list[Any]]] = {}
        for (relation, _), entry in self.rows.items():
            by_relation.setdefault(relation, []).append(list(entry["row"]))
        return observation_relations_identity(by_relation, self.relations)

    def rebase(self, observation_digest: str) -> None:
        """Recompute every evidence id under ``observation_digest``."""
        rename: dict[str, str] = {}
        rebased: dict[tuple[str, tuple], dict] = {}
        for (relation, _), entry in self.rows.items():
            row = list(entry["row"])
            eid = evidence_id(observation_digest, self.relations[relation], row)
            rename[entry["id"]] = eid
            rebased[(relation, tuple(row))] = {**entry, "id": eid, "row": row}
        for entry in rebased.values():
            entry["depends_on"] = {rename.get(d, d) for d in entry["depends_on"]} - {entry["id"]}
        self.rows = rebased
        self.observation_digest = observation_digest

    def materialize(self) -> tuple[tuple[Atom, ...], tuple[Evidence, ...]]:
        facts: list[Atom] = []
        evidence: list[Evidence] = []
        for (relation, _), entry in self.rows.items():
            decl = self.relations[relation]
            atom = Atom(relation, tuple(Constant(value, column.type)
                                        for value, column in zip(entry["row"], decl.columns)))
            names = [c.name for c in decl.columns]
            context = {name: entry["row"][names.index(name)] for name in decl.context_indices}
            facts.append(atom)
            evidence.append(Evidence(entry["id"], atom, Context.from_mapping(context),
                                     entry["source"], tuple(sorted(entry["depends_on"])),
                                     entry["kind"]))
        return tuple(facts), tuple(evidence)


def _checked(relation: str, column: Column, value: Any) -> Any:
    t = column.type
    if t in (TypeName.SYMBOL, TypeName.DIGEST):
        if not isinstance(value, str):
            raise _refuse("R-2", f"{relation}.{column.name}: expected str, got {value!r}")
        return value
    if t == TypeName.UNSIGNED:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise _refuse("R-6", f"{relation}.{column.name}: expected unsigned, got {value!r}")
        return value
    raise _refuse("R-2", f"{relation}.{column.name}: unsupported column type {t}")


# ---------------------------------------------------------------------------
# Typed readers.  Every one names the rule it enforces, so a refusal tells a
# reviewer which contract clause the receipt broke, not merely that it broke.


def _obj(value: Any, path: str, rule: str = "R-2") -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _refuse(rule, f"{path} must be an object")
    return value


def _arr(value: Any, path: str, rule: str = "R-2") -> list:
    if not isinstance(value, list):
        raise _refuse(rule, f"{path} must be an array")
    return value


def _text(value: Any, path: str, rule: str = "R-2", *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not allow_empty):
        raise _refuse(rule, f"{path} must be a non-empty string")
    return value


def _flag(value: Any, path: str, rule: str = "R-2") -> bool:
    if not isinstance(value, bool):
        raise _refuse(rule, f"{path} must be a boolean")
    return value


def _count(value: Any, path: str, rule: str = "R-2") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _refuse(rule, f"{path} must be a non-negative integer")
    return value


def _one_of(value: Any, allowed: tuple[str, ...], path: str, rule: str) -> str:
    if value not in allowed:
        raise _refuse(rule, f"{path} must be one of {list(allowed)}, got {value!r}")
    return value


def _sha256(value: Any, path: str, rule: str = "R-3") -> str:
    if not isinstance(value, str) or not _HEX64.match(value):
        raise _refuse(rule, f"{path} must be a lowercase 64-hex sha256, got {value!r}")
    return value


def _commit(value: Any, path: str) -> str:
    if not isinstance(value, str) or not _HEX40.match(value):
        raise _refuse("R-2", f"{path} must be a lowercase 40-hex commit, got {value!r}")
    return value


def _known_keys(value: Mapping[str, Any], allowed: tuple[str, ...], required: tuple[str, ...],
                path: str, rule: str = "R-2") -> None:
    """Unknown keys are refused; ``required`` must be present, the rest may be absent.

    Used where a member is genuinely absent rather than present-and-null -- an
    entry that was not observed carries no status, and demanding ``"status":
    null`` would only be a shape nobody writes.
    """
    unknown = set(value) - set(allowed)
    if unknown:
        raise _refuse(rule, f"{path} has unknown keys {sorted(unknown)}")
    missing = [key for key in required if key not in value]
    if missing:
        raise _refuse(rule, f"{path} is missing {missing}")


def _exact_keys(value: Mapping[str, Any], keys: tuple[str, ...], path: str,
                rule: str = "R-2") -> None:
    present, wanted = set(value), set(keys)
    if present - wanted:
        raise _refuse(rule, f"{path} has unknown keys {sorted(present - wanted)}")
    if wanted - present:
        raise _refuse(rule, f"{path} is missing {sorted(wanted - present)}")


def _digest_of(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _class_of(status: int, path: str) -> str:
    """The HTTP class of a status, recomputed at ingest -- never read from the receipt."""
    if 200 <= status < 300:
        return "2xx"
    if 300 <= status < 400:
        return "3xx"
    if 400 <= status < 500:
        return "4xx"
    if 500 <= status < 600:
        return "5xx"
    raise _refuse("R-6", f"{path}: status {status} has no comparison class")


# ---------------------------------------------------------------------------
# The policy target grammar (spec 1.7), closed on purpose.


def _check_target(target: Any, path: str) -> str:
    """``<relation>.<column>`` | ``body:<json-pointer>`` | ``effect:<store>.<table>.<column>``.

    A whole-document mask is spellable only as an explicit enumeration: ``"*"``,
    ``"$"``, ``""``, a pointer of ``"/"`` and any wildcard are refused, so
    "we masked the body" cannot be written as one character.
    """
    if not isinstance(target, str) or not target:
        raise _refuse("R-8", f"{path} must be a non-empty target string")
    if target in ("*", "$"):
        raise _refuse("R-8", f"{path}: {target!r} is a whole-document target; enumerate instead")
    if "*" in target:
        raise _refuse("R-8", f"{path}: wildcards are not a target grammar, got {target!r}")
    if target.startswith("body:"):
        pointer = target[len("body:"):]
        if not pointer.startswith("/") or pointer == "/":
            raise _refuse("R-8", f"{path}: {target!r} is not an RFC6901 pointer below the root")
        if any(not segment for segment in pointer.split("/")[1:]):
            raise _refuse("R-8", f"{path}: {target!r} has an empty pointer segment")
        return target
    if target.startswith("effect:"):
        parts = target[len("effect:"):].split(".")
        if len(parts) != 3 or not all(parts):
            raise _refuse("R-8", f"{path}: {target!r} is not effect:<store>.<table>.<column>")
        return target
    parts = target.split(".")
    if len(parts) != 2 or not all(parts):
        raise _refuse("R-8", f"{path}: {target!r} is not <relation>.<column>")
    return target


# ---------------------------------------------------------------------------
# Receipt reading


def _read_text(path: Path, limit: int) -> str:
    size = path.stat().st_size
    if size > limit:
        raise _refuse("R-2", f"{path.name}: {size} bytes exceed the {limit}-byte file limit")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise _refuse("R-2", f"{path.name}: not valid UTF-8 ({exc})") from exc


def _scan_text(text: str, where: str, forbidden: tuple[str, ...]) -> None:
    """R-13 and R-14 over one file's bytes, before anything is believed."""
    for marker in CREDENTIAL_MARKERS:
        if marker in text:
            raise _refuse("R-13", f"{where} contains the credential marker {marker!r}")
    _scan_fork(text, where, forbidden)


def _scan_fork(text: str, where: str, forbidden: tuple[str, ...]) -> None:
    """R-14 alone: the strings this public fork may not carry."""
    lowered = text.lower()
    for banned in forbidden:
        if banned in lowered:
            raise _refuse("R-14", f"{where} contains {banned!r}, forbidden in the public fork")


def _scan_receipt(receipt: Mapping[str, Any], forbidden: tuple[str, ...]) -> None:
    """R-13/R-14 over the parsed receipt, with the marker LIST itself excised.

    The receipt records which markers the harness scanned for
    (``log.redaction.markers_checked``) precisely so a weakened scan shows up in
    a diff -- so that array contains the marker strings by design, and scanning
    the raw bytes would make every conforming receipt fail its own rule.  Only
    the entries that ARE this module's markers are excised: a credential
    smuggled into that array is still scanned like anything else.
    """
    probe = json.loads(json.dumps(receipt))
    redaction = probe.get("log")
    if isinstance(redaction, dict):
        inner = redaction.get("redaction")
        if isinstance(inner, dict) and isinstance(inner.get("markers_checked"), list):
            inner["markers_checked"] = [marker for marker in inner["markers_checked"]
                                        if marker not in CREDENTIAL_MARKERS]
    _scan_text(json.dumps(probe, sort_keys=True, ensure_ascii=False), RECEIPT_FILE,
               forbidden)


def _read_receipt(receipt_dir: Path, limits: ExportLimits,
                  forbidden: tuple[str, ...]) -> dict[str, Any]:
    path = receipt_dir / RECEIPT_FILE
    if not receipt_dir.is_dir():
        raise _refuse("R-2", f"receipt directory {str(receipt_dir)!r} does not exist")
    if not path.is_file():
        raise _refuse("R-2", f"{RECEIPT_FILE} is missing from {str(receipt_dir)!r}")
    text = _read_text(path, limits.file_bytes)
    # R-14 can be judged on the raw bytes; R-13 cannot, because the receipt
    # legitimately records the marker list it scanned for (see _scan_receipt).
    _scan_fork(text, RECEIPT_FILE, forbidden)
    try:
        receipt = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _refuse("R-2", f"{RECEIPT_FILE}: not valid JSON ({exc})") from exc
    if not isinstance(receipt, Mapping):
        raise _refuse("R-2", f"{RECEIPT_FILE}: must be an object")
    _scan_receipt(receipt, forbidden)
    contract = receipt.get("contract")
    if contract != CONTRACT:
        raise _refuse("R-1", f"{RECEIPT_FILE}: contract must be {CONTRACT!r}, got {contract!r}")
    unknown = set(receipt) - set(REQUIRED_BLOCKS) - set(OPTIONAL_BLOCKS)
    if unknown:
        raise _refuse("R-2", f"{RECEIPT_FILE}: unknown blocks {sorted(unknown)}")
    missing = [block for block in REQUIRED_BLOCKS if block not in receipt]
    if missing:
        raise _refuse("R-2", f"{RECEIPT_FILE}: missing required blocks {missing}")
    return dict(receipt)


# ---------------------------------------------------------------------------
# Block readers.  Each returns the shape the projector needs and refuses on the
# spot; none of them believes a derived field it can recompute.


def _read_check(receipt: Mapping[str, Any]) -> dict[str, Any]:
    check = _obj(receipt["check"], "check")
    _exact_keys(check, ("id", "title", "script_path", "script_sha256", "scope", "limitations"),
                "check")
    return {
        "id": _text(check["id"], "check.id"),
        "title": _text(check["title"], "check.title"),
        "script_path": _text(check["script_path"], "check.script_path"),
        "script_sha256": _sha256(check["script_sha256"], "check.script_sha256"),
        "scope": _text(check["scope"], "check.scope"),
        "limitations": [_text(item, f"check.limitations[{i}]")
                        for i, item in enumerate(_arr(check["limitations"], "check.limitations"))],
    }


def _read_sources(receipt: Mapping[str, Any]) -> dict[str, Any]:
    sources = _obj(receipt["sources"], "sources")
    _exact_keys(sources, ("candidate", "incumbent", "toolchain", "source_digest"), "sources")
    candidate = _obj(sources["candidate"], "sources.candidate")
    _exact_keys(candidate, ("repo", "commit", "tree", "dirty", "inputs"), "sources.candidate")
    # A dirty candidate tree is an ingest refusal and never a waiver: the tree
    # digest below would name a tree nobody can check out again.
    if _flag(candidate["dirty"], "sources.candidate.dirty"):
        raise _refuse("R-2", "sources.candidate.dirty is true; a dirty tree is refused, never waived")
    incumbent = _obj(sources["incumbent"], "sources.incumbent")
    _exact_keys(incumbent, ("commit", "runtime_commit", "manifest_sha256", "runtime_contract",
                            "baseline_contract", "source_behavior_differences", "inputs"),
                "sources.incumbent")
    header = {
        "candidate": {
            "repo": _text(candidate["repo"], "sources.candidate.repo"),
            "commit": _commit(candidate["commit"], "sources.candidate.commit"),
            # THE TREE, NOT THE LIST: the whole tracked tree, so a curated input
            # list can never be the thing the source identity rests on.
            "tree": _commit(candidate["tree"], "sources.candidate.tree"),
        },
        "incumbent": {
            "commit": _commit(incumbent["commit"], "sources.incumbent.commit"),
            "runtime_commit": _commit(incumbent["runtime_commit"], "sources.incumbent.runtime_commit"),
            "manifest_sha256": _sha256(incumbent["manifest_sha256"], "sources.incumbent.manifest_sha256"),
        },
    }
    declared = _sha256(sources["source_digest"], "sources.source_digest")
    recomputed = _digest_of(header)
    if declared != recomputed:
        raise _refuse("R-3", f"sources.source_digest {declared[:12]!r} does not recompute "
                             f"from the candidate tree and incumbent manifest ({recomputed[:12]!r})")
    # ``inputs`` is human evidence and contributes NOTHING to the identity.
    for side, block in (("candidate", candidate), ("incumbent", incumbent)):
        for index, item in enumerate(_arr(block["inputs"], f"sources.{side}.inputs")):
            entry = _obj(item, f"sources.{side}.inputs[{index}]")
            _exact_keys(entry, ("path", "sha256"), f"sources.{side}.inputs[{index}]")
            _text(entry["path"], f"sources.{side}.inputs[{index}].path")
            _sha256(entry["sha256"], f"sources.{side}.inputs[{index}].sha256")
    header["source_digest"] = declared
    header["toolchain"] = dict(_obj(sources["toolchain"], "sources.toolchain"))
    return header


def _read_fixture(receipt: Mapping[str, Any]) -> dict[str, Any]:
    fixture = _obj(receipt["fixture"], "fixture")
    _exact_keys(fixture, ("name", "tenant", "schema_sha256", "seed_sha256", "seed_identities",
                          "recipe_sha256", "stores", "digest"), "fixture")
    stores = []
    for index, item in enumerate(_arr(fixture["stores"], "fixture.stores")):
        entry = _obj(item, f"fixture.stores[{index}]")
        _exact_keys(entry, ("kind", "reset"), f"fixture.stores[{index}]")
        stores.append({"kind": _one_of(entry["kind"], STORE_KINDS,
                                       f"fixture.stores[{index}].kind", "R-2"),
                       "reset": _flag(entry["reset"], f"fixture.stores[{index}].reset")})
    identities = [_text(item, f"fixture.seed_identities[{i}]")
                  for i, item in enumerate(_arr(fixture["seed_identities"], "fixture.seed_identities"))]
    if identities != sorted(identities):
        raise _refuse("R-2", "fixture.seed_identities must be sorted")
    payload = {
        "name": _text(fixture["name"], "fixture.name"),
        "tenant": _text(fixture["tenant"], "fixture.tenant"),
        "schema_sha256": _sha256(fixture["schema_sha256"], "fixture.schema_sha256"),
        "seed_sha256": _sha256(fixture["seed_sha256"], "fixture.seed_sha256"),
        "seed_identities": identities,
        "recipe_sha256": _sha256(fixture["recipe_sha256"], "fixture.recipe_sha256"),
        "stores": stores,
    }
    declared = _sha256(fixture["digest"], "fixture.digest")
    recomputed = _digest_of(payload)
    if declared != recomputed:
        raise _refuse("R-3", f"fixture.digest {declared[:12]!r} does not recompute from the "
                             f"fixture block ({recomputed[:12]!r})")
    return {"digest": declared, **payload}


def _read_policy(receipt: Mapping[str, Any]) -> dict[str, Any]:
    policy = _obj(receipt["policy"], "policy")
    _exact_keys(policy, ("facets", "comparator", "status_normalized", "normalizations",
                         "exclusions", "digest"), "policy")
    if _flag(policy["status_normalized"], "policy.status_normalized"):
        raise _refuse("R-8", "policy.status_normalized is true; a normalized status is not a status")
    facets = _arr(policy["facets"], "policy.facets")
    for index, facet in enumerate(facets):
        _one_of(facet, FACETS, f"policy.facets[{index}]", "R-8")
    if len(set(facets)) != len(facets):
        raise _refuse("R-8", "policy.facets repeats a facet")
    if not facets:
        raise _refuse("R-8", "policy.facets is empty; a comparison of nothing is not a comparison")
    normalizations, exclusions = [], []
    for index, item in enumerate(_arr(policy["normalizations"], "policy.normalizations")):
        entry = _obj(item, f"policy.normalizations[{index}]")
        _exact_keys(entry, ("id", "facet", "target", "kind", "preserves", "reason",
                            "source_sha256"), f"policy.normalizations[{index}]")
        normalizations.append({
            "id": _text(entry["id"], f"policy.normalizations[{index}].id"),
            "facet": _one_of(entry["facet"], FACETS, f"policy.normalizations[{index}].facet", "R-8"),
            "target": _check_target(entry["target"], f"policy.normalizations[{index}].target"),
            "kind": _one_of(entry["kind"], NORMALIZATION_KINDS,
                            f"policy.normalizations[{index}].kind", "R-8"),
            "preserves": _one_of(entry["preserves"], PRESERVES,
                                 f"policy.normalizations[{index}].preserves", "R-8"),
            "reason": _text(entry["reason"], f"policy.normalizations[{index}].reason"),
            # inside the policy digest: changing a normalizer's implementation
            # stales the reviewer's admission without a second relation
            "source_sha256": _sha256(entry["source_sha256"],
                                     f"policy.normalizations[{index}].source_sha256"),
        })
    for index, item in enumerate(_arr(policy["exclusions"], "policy.exclusions")):
        entry = _obj(item, f"policy.exclusions[{index}]")
        _exact_keys(entry, ("id", "facet", "target", "reason", "ticket", "source_sha256"),
                    f"policy.exclusions[{index}]")
        exclusions.append({
            "id": _text(entry["id"], f"policy.exclusions[{index}].id"),
            "facet": _one_of(entry["facet"], FACETS, f"policy.exclusions[{index}].facet", "R-8"),
            "target": _check_target(entry["target"], f"policy.exclusions[{index}].target"),
            "reason": _text(entry["reason"], f"policy.exclusions[{index}].reason"),
            "ticket": _text(entry["ticket"], f"policy.exclusions[{index}].ticket"),
            "source_sha256": _sha256(entry["source_sha256"],
                                     f"policy.exclusions[{index}].source_sha256"),
        })
    ids = [entry["id"] for entry in normalizations]
    if len(set(ids)) != len(ids):
        raise _refuse("R-8", "policy.normalizations repeats an id")
    declared = _sha256(policy["digest"], "policy.digest")
    recomputed = _digest_of({"facets": list(facets), "normalizations": normalizations,
                             "exclusions": exclusions})
    if declared != recomputed:
        raise _refuse("R-3", f"policy.digest {declared[:12]!r} does not recompute from the "
                             f"facets, normalizations and exclusions ({recomputed[:12]!r})")
    _text(policy["comparator"], "policy.comparator")
    return {"digest": declared, "facets": list(facets), "normalizations": normalizations,
            "exclusions": exclusions}


def _read_scenario_set(receipt: Mapping[str, Any], check_id: str) -> dict[str, Any]:
    block = _obj(receipt["scenario_set"], "scenario_set")
    _exact_keys(block, ("digest", "count", "declared_at", "required_kinds", "scenarios"),
                "scenario_set")
    if block["declared_at"] != "pre-observation":
        raise _refuse("R-2", "scenario_set.declared_at must be 'pre-observation'; a set "
                             "declared after a result is not a pre-registration")
    required_kinds = [_one_of(kind, SCENARIO_KINDS, f"scenario_set.required_kinds[{i}]", "R-2")
                      for i, kind in enumerate(_arr(block["required_kinds"], "scenario_set.required_kinds"))]
    scenarios = []
    for index, item in enumerate(_arr(block["scenarios"], "scenario_set.scenarios")):
        entry = _obj(item, f"scenario_set.scenarios[{index}]")
        _exact_keys(entry, ("id", "kind", "required", "actor", "target", "expected_class",
                            "intent"), f"scenario_set.scenarios[{index}]")
        scenarios.append({
            "id": _text(entry["id"], f"scenario_set.scenarios[{index}].id"),
            "kind": _one_of(entry["kind"], SCENARIO_KINDS,
                            f"scenario_set.scenarios[{index}].kind", "R-2"),
            "required": _flag(entry["required"], f"scenario_set.scenarios[{index}].required"),
            "actor": _text(entry["actor"], f"scenario_set.scenarios[{index}].actor", allow_empty=True),
            "target": _text(entry["target"], f"scenario_set.scenarios[{index}].target"),
            "expected_class": _one_of(entry["expected_class"], CLASSES,
                                      f"scenario_set.scenarios[{index}].expected_class", "R-2"),
            "intent": _text(entry["intent"], f"scenario_set.scenarios[{index}].intent"),
        })
    ids = [entry["id"] for entry in scenarios]
    if len(set(ids)) != len(ids):
        raise _refuse("R-2", "scenario_set.scenarios repeats a scenario id")
    count = _count(block["count"], "scenario_set.count")
    if count <= 0:
        raise _refuse("R-2", "scenario_set.count must be > 0; an empty set agrees for free")
    if count != len(scenarios):
        raise _refuse("R-2", f"scenario_set.count is {count} but {len(scenarios)} scenarios "
                             f"are declared")
    missing = [kind for kind in required_kinds if kind not in {s["kind"] for s in scenarios}]
    if missing:
        raise _refuse("R-2", f"scenario_set.required_kinds names {missing} that no scenario has")
    declared = _sha256(block["digest"], "scenario_set.digest")
    recomputed = _digest_of({"check": check_id, "required_kinds": required_kinds,
                             "scenarios": scenarios})
    if declared != recomputed:
        raise _refuse("R-3", f"scenario_set.digest {declared[:12]!r} does not recompute from "
                             f"the check id, required kinds and scenarios ({recomputed[:12]!r})")
    return {"digest": declared, "count": count, "required_kinds": required_kinds,
            "scenarios": scenarios}


def _read_run(receipt: Mapping[str, Any], check_id: str, source_digest: str,
              fixture_digest: str, set_digest: str, policy_digest: str) -> dict[str, Any]:
    block = _obj(receipt["run"], "run")
    _exact_keys(block, ("id", "nonce", "started_at", "recorded_at", "elapsed_seconds",
                        "deadline_seconds"), "run")
    nonce = _sha256(block["nonce"], "run.nonce")
    started_at = _text(block["started_at"], "run.started_at")
    run_id = block["id"]
    if not isinstance(run_id, str) or not _HEX16.match(run_id):
        raise _refuse("R-3", f"run.id must be 16 lowercase hex, got {run_id!r}")
    recomputed = _digest_of([check_id, source_digest, fixture_digest, set_digest,
                             policy_digest, nonce, started_at])[:16]
    if run_id != recomputed:
        raise _refuse("R-3", f"run.id {run_id!r} does not recompute from the check, source, "
                             f"fixture, set, policy, nonce and start ({recomputed!r})")
    for key in ("elapsed_seconds", "deadline_seconds"):
        value = block[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise _refuse("R-2", f"run.{key} must be a non-negative number")
    return {"id": run_id, "nonce": nonce, "started_at": started_at,
            "recorded_at": _text(block["recorded_at"], "run.recorded_at"),
            "elapsed_seconds": block["elapsed_seconds"],
            "deadline_seconds": block["deadline_seconds"]}


def _read_configuration(receipt: Mapping[str, Any]) -> dict[str, Any]:
    block = _obj(receipt["configuration"], "configuration")
    _exact_keys(block, ("config_digest", "env_names", "command_incumbent", "command_candidate",
                        "side_identity", "sides_distinct"), "configuration")
    identity = _obj(block["side_identity"], "configuration.side_identity")
    _exact_keys(identity, SIDES, "configuration.side_identity")
    return {
        "config_digest": _sha256(block["config_digest"], "configuration.config_digest"),
        "env_names": [_text(n, f"configuration.env_names[{i}]")
                      for i, n in enumerate(_arr(block["env_names"], "configuration.env_names"))],
        "sides_distinct": _flag(block["sides_distinct"], "configuration.sides_distinct"),
        "side_identity": {side: dict(_obj(identity[side], f"configuration.side_identity.{side}"))
                          for side in SIDES},
    }


def _read_observations(receipt: Mapping[str, Any], scenarios: list[dict[str, Any]],
                       policy: dict[str, Any]) -> dict[str, Any]:
    """The per-(scenario, side) rows, with every facet gap stopped here.

    The bijection is the whole point.  ``observations`` with outcome
    ``observed`` and ``unobserved`` must together cover (scenarios x sides)
    exactly once each; an entry whose outcome is not ``observed`` carries the
    failure detail and MUST have the matching ``unobserved`` row, so a scenario
    that did not run can never be invisible to the gap rule.
    """
    declared = {entry["id"] for entry in scenarios}
    entries: dict[tuple[str, str], dict[str, Any]] = {}
    declared_ids = {entry["id"] for entry in policy["normalizations"]}
    for index, item in enumerate(_arr(receipt["observations"], "observations")):
        path = f"observations[{index}]"
        entry = _obj(item, path)
        _known_keys(entry, ("scenario", "side", "outcome", "status", "class", "raw_digest",
                            "body_digest", "normalizations_applied", "effects_observed",
                            "effects", "effects_digest", "duration_ms", "failure"),
                    ("scenario", "side", "outcome"), path)
        scenario = _text(entry["scenario"], f"{path}.scenario")
        if scenario not in declared:
            raise _refuse("R-5", f"{path} names scenario {scenario!r}, which the admitted set "
                                 f"does not declare")
        side = _one_of(entry["side"], SIDES, f"{path}.side", "R-5")
        if (scenario, side) in entries:
            raise _refuse("R-5", f"{path} is a second entry for ({scenario!r}, {side!r})")
        outcome = _one_of(entry["outcome"], OBSERVATION_OUTCOMES, f"{path}.outcome", "R-6")
        record: dict[str, Any] = {"scenario": scenario, "side": side, "outcome": outcome,
                                  "duration_ms": _count(entry.get("duration_ms", 0),
                                                        f"{path}.duration_ms")}
        if outcome == "observed":
            for key in ("status", "raw_digest", "body_digest", "normalizations_applied",
                        "effects_observed"):
                if key not in entry:
                    raise _refuse("R-6", f"{path} is observed and carries no {key!r}; a facet "
                                         f"that was never written down cannot be compared")
            status = entry["status"]
            if isinstance(status, bool) or not isinstance(status, int) or status <= 0:
                raise _refuse("R-6", f"{path}.status must be a positive integer for an observed "
                                     f"entry, got {status!r}")
            recomputed = _class_of(status, f"{path}.status")
            if entry.get("class") != recomputed:
                raise _refuse("R-6", f"{path}.class is {entry.get('class')!r} but status "
                                     f"{status} is {recomputed!r}")
            raw = _sha256(entry["raw_digest"], f"{path}.raw_digest", "R-6")
            body = _sha256(entry["body_digest"], f"{path}.body_digest", "R-6")
            applied = [_text(n, f"{path}.normalizations_applied[{i}]") for i, n in
                       enumerate(_arr(entry["normalizations_applied"], f"{path}.normalizations_applied"))]
            for name in applied:
                if name not in declared_ids:
                    raise _refuse("R-8", f"{path}.normalizations_applied names {name!r}, which "
                                         f"the admitted policy does not declare")
            if raw != body and not applied:
                raise _refuse("R-7", f"{path}: body_digest differs from raw_digest with no "
                                     f"normalization named; a silent mask is not a comparison")
            record.update({"status": status, "class": recomputed, "raw_digest": raw,
                           "body_digest": body, "normalizations_applied": applied})
            observed_effects = _flag(entry["effects_observed"], f"{path}.effects_observed")
            effects = []
            for position, raw_effect in enumerate(_arr(entry.get("effects", []),
                                                        f"{path}.effects")):
                effect_path = f"{path}.effects[{position}]"
                effect = _obj(raw_effect, effect_path)
                _exact_keys(effect, ("store", "table", "kind", "key", "cols_digest"), effect_path)
                effects.append({
                    "store": _one_of(effect["store"], STORE_KINDS, f"{effect_path}.store", "R-2"),
                    "table": _text(effect["table"], f"{effect_path}.table"),
                    "kind": _one_of(effect["kind"], EFFECT_KINDS, f"{effect_path}.kind", "R-2"),
                    "key": _sha256(effect["key"], f"{effect_path}.key"),
                    "cols_digest": _sha256(effect["cols_digest"], f"{effect_path}.cols_digest"),
                })
            record["effects_observed"] = observed_effects
            record["effects"] = effects
            if observed_effects:
                folded = sorted([[e["store"], e["table"], e["kind"], e["key"], e["cols_digest"]]
                                 for e in effects], key=canonical_json)
                recomputed_effects = _digest_of(folded)
                declared_effects = _sha256(entry.get("effects_digest"), f"{path}.effects_digest")
                if declared_effects != recomputed_effects:
                    raise _refuse("R-3", f"{path}.effects_digest {declared_effects[:12]!r} does "
                                         f"not recompute from its {len(effects)} effect rows "
                                         f"({recomputed_effects[:12]!r})")
                record["effects_digest"] = declared_effects
            elif entry.get("effects_digest") is not None:
                raise _refuse("R-2", f"{path}.effects_digest is present with "
                                     f"effects_observed false")
            if effects and not observed_effects:
                raise _refuse("R-2", f"{path} lists effects with effects_observed false")
            if entry.get("failure") is not None:
                raise _refuse("R-6", f"{path} is observed and carries a failure")
        else:
            failure = _obj(entry.get("failure"), f"{path}.failure", "R-6")
            _exact_keys(failure, ("stage", "code", "message_digest"), f"{path}.failure")
            _one_of(failure["stage"], OBSERVATION_FAILURE_STAGES, f"{path}.failure.stage", "R-6")
            record["failure"] = dict(failure)
        entries[(scenario, side)] = record

    unobserved: dict[tuple[str, str], str] = {}
    for index, item in enumerate(_arr(receipt["unobserved"], "unobserved")):
        path = f"unobserved[{index}]"
        entry = _obj(item, path)
        _exact_keys(entry, ("scenario", "side", "reason"), path)
        scenario = _text(entry["scenario"], f"{path}.scenario")
        if scenario not in declared:
            raise _refuse("R-5", f"{path} names scenario {scenario!r}, which the admitted set "
                                 f"does not declare")
        side = _one_of(entry["side"], SIDES, f"{path}.side", "R-5")
        if (scenario, side) in unobserved:
            raise _refuse("R-5", f"{path} is a second unobserved row for ({scenario!r}, {side!r})")
        unobserved[(scenario, side)] = _one_of(entry["reason"], UNOBSERVED_REASONS,
                                               f"{path}.reason", "R-5")

    observed = {key for key, entry in entries.items() if entry["outcome"] == "observed"}
    for scenario in sorted(declared):
        for side in SIDES:
            key = (scenario, side)
            if key in observed and key in unobserved:
                raise _refuse("R-5", f"({scenario!r}, {side!r}) is both observed and unobserved")
            if key not in observed and key not in unobserved:
                raise _refuse("R-5", f"({scenario!r}, {side!r}) is neither observed nor "
                                     f"recorded as unobserved; an omission is not a pass")
    for key, entry in entries.items():
        if entry["outcome"] == "observed":
            continue
        if key not in unobserved:
            raise _refuse("R-5", f"({key[0]!r}, {key[1]!r}) reports outcome "
                                 f"{entry['outcome']!r} with no unobserved row")
        if unobserved[key] != entry["outcome"]:
            raise _refuse("R-5", f"({key[0]!r}, {key[1]!r}) reports outcome "
                                 f"{entry['outcome']!r} but is unobserved for "
                                 f"{unobserved[key]!r}")
    return {"entries": entries, "observed": observed, "unobserved": unobserved}


def _read_firings(receipt: Mapping[str, Any], policy: dict[str, Any],
                  observations: dict[str, Any]) -> dict[str, Any]:
    declared_ids = {entry["id"] for entry in policy["normalizations"]}
    firings = []
    for index, item in enumerate(_arr(receipt["normalization_firings"], "normalization_firings")):
        path = f"normalization_firings[{index}]"
        entry = _obj(item, path)
        _exact_keys(entry, ("scenario", "side", "facet", "normalization", "before_digest",
                            "after_digest"), path)
        name = _text(entry["normalization"], f"{path}.normalization")
        if name not in declared_ids:
            raise _refuse("R-8", f"{path} fires {name!r}, which the admitted policy does not "
                                 f"declare as a normalization")
        facet = _one_of(entry["facet"], FACETS, f"{path}.facet", "R-8")
        if facet == "status":
            # A firing on the status facet is a normalized status whatever the
            # policy flag says (R-8): a status is a number, not a document.
            raise _refuse("R-8", f"{path} fires {name!r} on the status facet; a normalized "
                                 f"status is not a status")
        firings.append({
            "scenario": _text(entry["scenario"], f"{path}.scenario"),
            "side": _one_of(entry["side"], SIDES, f"{path}.side", "R-8"),
            "facet": facet,
            "normalization": name,
            "before_digest": _sha256(entry["before_digest"], f"{path}.before_digest"),
            "after_digest": _sha256(entry["after_digest"], f"{path}.after_digest"),
        })
    masked = []
    for index, item in enumerate(_arr(receipt["masked_differences"], "masked_differences")):
        path = f"masked_differences[{index}]"
        entry = _obj(item, path)
        _exact_keys(entry, ("scenario", "facet", "normalization", "field_path",
                            "incumbent_before_digest", "candidate_before_digest"), path)
        name = _text(entry["normalization"], f"{path}.normalization")
        if name not in declared_ids:
            raise _refuse("R-8", f"{path} names {name!r}, which the admitted policy does not "
                                 f"declare as a normalization")
        masked.append({
            "scenario": _text(entry["scenario"], f"{path}.scenario"),
            "facet": _one_of(entry["facet"], FACETS, f"{path}.facet", "R-8"),
            "normalization": name,
            "field_path": _text(entry["field_path"], f"{path}.field_path"),
            "incumbent_before_digest": _sha256(entry["incumbent_before_digest"],
                                               f"{path}.incumbent_before_digest"),
            "candidate_before_digest": _sha256(entry["candidate_before_digest"],
                                               f"{path}.candidate_before_digest"),
        })
    for entry in firings:
        key = (entry["scenario"], entry["side"])
        if key not in observations["observed"]:
            raise _refuse("R-8", f"a normalization fired on ({entry['scenario']!r}, "
                                 f"{entry['side']!r}), which was not observed")
    # R-9: a firing over values that differed BEFORE it fired is a masked
    # difference, whether or not the harness liked the word.  Two forms:
    #
    #   two-sided -- the same normalization fired on both sides and their
    #   before-digests differ;
    #
    #   one-sided -- the normalization fired on ONE side only, the two sides'
    #   RAW digests for that facet differ, and their NORMALIZED digests match.
    #
    # The one-sided form is the reproduced false green: a ``drop`` that fires
    # on the candidate alone and takes its body back to the incumbent's is an
    # agreement with no disclosure and nothing to admit unless it is caught
    # here.  Both forms demand a ``masked_differences`` row -- which then
    # demands a reviewer admission downstream -- and a receipt that lacks it is
    # REFUSED, never downgraded to a message.  ``_facet_digests`` is what makes
    # "raw" and "normalized" precise per facet.
    by_key: dict[tuple[str, str, str], dict[str, str]] = {}
    for firing in firings:
        key = (firing["scenario"], firing["facet"], firing["normalization"])
        by_key.setdefault(key, {})[firing["side"]] = firing["before_digest"]
    exported = {(entry["scenario"], entry["facet"], entry["normalization"]) for entry in masked}
    for key, sides in sorted(by_key.items()):
        scenario, facet, name = key
        if len(sides) == 2:
            if sides["incumbent"] == sides["candidate"] or key in exported:
                continue
            raise _refuse("R-9", f"normalization {name!r} fired on both sides of scenario "
                                 f"{scenario!r} over differing {facet} values and "
                                 f"masked_differences does not export it")
        (fired,) = sides
        other = "candidate" if fired == "incumbent" else "incumbent"
        if (scenario, other) not in observations["observed"]:
            # Nothing to compare against: the scenario is not_observed and no
            # agreement derives from it (R-5 has already bound the gap).
            continue
        digests = {side: _facet_digests(observations["entries"][(scenario, side)], facet,
                                        firings, scenario, side)
                   for side in SIDES}
        if any(d is None for d in digests.values()):
            # The facet was not observed on one side (no effects digest); the
            # comparison never happened there and persisted_effects is a
            # mandatory unassessed row saying so (R-12).
            continue
        raw_differ = digests["incumbent"][0] != digests["candidate"][0]
        normalized_match = digests["incumbent"][1] == digests["candidate"][1]
        if raw_differ and normalized_match and key not in exported:
            raise _refuse("R-9", f"normalization {name!r} fired on the {fired} side only of "
                                 f"scenario {scenario!r} and erased a {facet} difference "
                                 f"(raw digests differ, normalized digests match) that "
                                 f"masked_differences does not export")
    return {"firings": firings, "masked": masked}


def _facet_digests(entry: dict[str, Any], facet: str, firings: list[dict[str, Any]],
                   scenario: str, side: str) -> tuple[str, str] | None:
    """``(raw, normalized)`` for one facet of one observed side, or ``None``.

    ``body``: the entry's own ``raw_digest`` / ``body_digest`` (R-7 already binds
    the two).  ``effects``: the entry carries only the normalized
    ``effects_digest`` -- there is no raw effects digest on the wire -- so the
    raw value is recovered from the side's effects firings as the head of
    their before/after chain, and is the normalized digest itself where nothing
    fired.  A chain with no single head cannot be resolved and is refused
    rather than guessed at: the missing premise is named.  ``None`` means the
    facet was not observed on this side at all.  ``status`` never reaches here:
    R-8 refuses a normalized status.
    """
    if facet == "body":
        return entry["raw_digest"], entry["body_digest"]
    assert facet == "effects", facet  # status firings are refused before this is reached
    normalized = entry.get("effects_digest")
    if normalized is None:
        return None
    own = [f for f in firings
           if f["scenario"] == scenario and f["side"] == side and f["facet"] == "effects"]
    if not own:
        return normalized, normalized
    afters = {f["after_digest"] for f in own}
    heads = sorted({f["before_digest"] for f in own} - afters)
    if len(heads) != 1:
        raise _refuse("R-9", f"the {len(own)} effects normalizations that fired on "
                             f"({scenario!r}, {side!r}) do not form one before/after chain "
                             f"({len(heads)} heads), so the raw effects digest cannot be "
                             f"recovered and a masked difference cannot be ruled out")
    return heads[0], normalized


def _read_failures(receipt: Mapping[str, Any], declared: set[str]) -> list[dict[str, Any]]:
    failures = []
    for index, item in enumerate(_arr(receipt["failures"], "failures")):
        path = f"failures[{index}]"
        entry = _obj(item, path)
        _known_keys(entry, ("phase", "class", "scenario", "detail", "at"),
                    ("phase", "class", "at"), path)
        scenario = entry.get("scenario")
        if scenario is not None:
            scenario = _text(scenario, f"{path}.scenario")
            if scenario not in declared:
                raise _refuse("R-5", f"{path} names scenario {scenario!r}, which the admitted "
                                     f"set does not declare")
        failures.append({
            "phase": _one_of(entry["phase"], FAILURE_PHASES, f"{path}.phase", "R-2"),
            "class": _one_of(entry["class"], FAILURE_CLASSES, f"{path}.class", "R-2"),
            "scenario": scenario,
            "detail": _text(entry.get("detail", ""), f"{path}.detail", allow_empty=True),
            "at": _text(entry["at"], f"{path}.at"),
        })
    return failures


def _read_completeness(receipt: Mapping[str, Any]) -> dict[str, Any]:
    block = _obj(receipt["completeness"], "completeness")
    _exact_keys(block, COMPLETENESS_KEYS + ("by_side",), "completeness")

    def entry(raw: Any, path: str) -> dict[str, Any]:
        item = _obj(raw, path)
        _exact_keys(item, ("closed", "reason"), path)
        closed = _flag(item["closed"], f"{path}.closed")
        reason = item["reason"]
        if not isinstance(reason, str):
            raise _refuse("R-11", f"{path}.reason must be a string")
        # An open box with no reason is the silence this block exists to stop.
        if not closed and not reason.strip():
            raise _refuse("R-11", f"{path} is open with no reason; nothing downstream may "
                                  f"negate over an unexplained gap")
        return {"closed": closed, "reason": reason}

    out = {key: entry(block[key], f"completeness.{key}") for key in COMPLETENESS_KEYS}
    by_side_raw = _obj(block["by_side"], "completeness.by_side")
    _exact_keys(by_side_raw, SIDES, "completeness.by_side")
    by_side = {}
    for side in SIDES:
        side_block = _obj(by_side_raw[side], f"completeness.by_side.{side}")
        _exact_keys(side_block, BY_SIDE_KEYS, f"completeness.by_side.{side}")
        by_side[side] = {key: entry(side_block[key], f"completeness.by_side.{side}.{key}")
                         for key in BY_SIDE_KEYS}
    out["by_side"] = by_side
    return out


def _read_unassessed(receipt: Mapping[str, Any]) -> list[dict[str, str]]:
    rows = []
    seen = set()
    for index, item in enumerate(_arr(receipt["unassessed"], "unassessed")):
        path = f"unassessed[{index}]"
        entry = _obj(item, path)
        _exact_keys(entry, ("dimension", "reason"), path)
        dimension = _one_of(entry["dimension"], UNASSESSED_VOCABULARY, f"{path}.dimension", "R-12")
        if dimension in seen:
            raise _refuse("R-12", f"{path} repeats the dimension {dimension!r}")
        seen.add(dimension)
        rows.append({"dimension": dimension,
                     "reason": _text(entry["reason"], f"{path}.reason")})
    if not rows:
        raise _refuse("R-12", "unassessed is empty; a receipt that assessed everything has not "
                              "been written")
    return rows


def _read_repeat(receipt: Mapping[str, Any], declared: set[str]) -> dict[str, Any] | None:
    if "repeat" not in receipt or receipt["repeat"] is None:
        return None
    block = _obj(receipt["repeat"], "repeat")
    _exact_keys(block, ("side", "run_a", "run_b", "stable", "compared_scenarios"), "repeat")
    scenarios = [_text(s, f"repeat.compared_scenarios[{i}]")
                 for i, s in enumerate(_arr(block["compared_scenarios"], "repeat.compared_scenarios"))]
    for scenario in scenarios:
        if scenario not in declared:
            raise _refuse("R-2", f"repeat.compared_scenarios names {scenario!r}, which the "
                                 f"admitted set does not declare")
    if len(scenarios) != len(set(scenarios)):
        raise _refuse("R-2", "repeat.compared_scenarios contains a duplicate scenario")
    if set(scenarios) != declared:
        missing = sorted(declared - set(scenarios))
        raise _refuse("R-2", f"repeat.compared_scenarios does not cover the admitted set; "
                               f"missing {missing}")
    return {"side": _one_of(block["side"], SIDES, "repeat.side", "R-2"),
            "run_a": _text(block["run_a"], "repeat.run_a"),
            "run_b": _text(block["run_b"], "repeat.run_b"),
            # the symbol "true"/"false", never a boolean: a datalog column is not a flag
            "stable": "true" if _flag(block["stable"], "repeat.stable") else "false",
            "compared_scenarios": scenarios}


# ---------------------------------------------------------------------------
# Recomputation: the harness's own summary is never believed.


def _recompute_results(scenarios: list[dict[str, Any]], observations: dict[str, Any],
                       policy: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The verdict per scenario, recomputed from the per-side rows alone.

    Precedence is ``not_observed`` > ``degenerate`` > ``differ`` > ``agree``:
    a scenario whose incumbent did not answer in its pre-registered class is
    reported as degenerate even when the two sides matched, because two sides
    matching on a broken answer is exactly the thing the class check exists to
    name.
    """
    entries = observations["entries"]
    out: dict[str, dict[str, Any]] = {}
    for scenario in scenarios:
        name = scenario["id"]
        left = entries.get((name, "incumbent"))
        right = entries.get((name, "candidate"))
        if (name, "incumbent") not in observations["observed"] or \
           (name, "candidate") not in observations["observed"]:
            out[name] = {"outcome": "not_observed", "facets": []}
            continue
        if left["class"] != scenario["expected_class"]:
            out[name] = {"outcome": "degenerate", "facets": []}
            continue
        differing = []
        for facet in policy["facets"]:
            if facet == "status" and left["status"] != right["status"]:
                differing.append(facet)
            elif facet == "body" and left["body_digest"] != right["body_digest"]:
                differing.append(facet)
            elif facet == "effects":
                # Effects are compared only where both sides observed them; where
                # one did not, the facet is unobserved, and persisted_effects is
                # a mandatory unassessed row saying so (R-12).
                if "effects_digest" in left and "effects_digest" in right and \
                        left["effects_digest"] != right["effects_digest"]:
                    differing.append(facet)
        out[name] = {"outcome": "differ" if differing else "agree", "facets": differing}
    return out


def _check_results(receipt: Mapping[str, Any], scenarios: list[dict[str, Any]],
                   recomputed: dict[str, dict[str, Any]]) -> None:
    rows = _arr(receipt["results"], "results")
    seen = {}
    for index, item in enumerate(rows):
        path = f"results[{index}]"
        entry = _obj(item, path)
        _known_keys(entry, ("scenario", "outcome", "difference"), ("scenario", "outcome"),
                    path, "R-4")
        scenario = _text(entry["scenario"], f"{path}.scenario")
        if scenario in seen:
            raise _refuse("R-4", f"{path} is a second result row for {scenario!r}")
        if scenario not in recomputed:
            raise _refuse("R-4", f"{path} names scenario {scenario!r}, which the admitted set "
                                 f"does not declare")
        outcome = _one_of(entry["outcome"], RESULT_OUTCOMES, f"{path}.outcome", "R-4")
        expected = recomputed[scenario]
        if outcome != expected["outcome"]:
            raise _refuse("R-4", f"{path} reports {outcome!r} for {scenario!r}; recomputing "
                                 f"from the observations gives {expected['outcome']!r}")
        difference = entry.get("difference")
        if outcome == "differ":
            block = _obj(difference, f"{path}.difference", "R-4")
            _exact_keys(block, ("facet", "field_path", "incumbent_digest", "candidate_digest"),
                        f"{path}.difference")
            if block["facet"] not in expected["facets"]:
                raise _refuse("R-4", f"{path}.difference blames facet {block['facet']!r}; the "
                                     f"facets that actually differ are {expected['facets']}")
        elif difference is not None:
            raise _refuse("R-4", f"{path} carries a difference with outcome {outcome!r}")
        seen[scenario] = outcome
    missing = sorted(set(recomputed) - set(seen))
    if missing:
        raise _refuse("R-4", f"results does not cover {missing}; it must be a bijection with "
                             f"the declared scenario set")


def _check_agreement(receipt: Mapping[str, Any], recomputed: dict[str, dict[str, Any]],
                     observations: dict[str, Any], terminal: str, count: int,
                     failures: list[dict[str, Any]]) -> dict[str, Any]:
    block = _obj(receipt["agreement"], "agreement")
    _exact_keys(block, ("compared", "agree", "differ", "not_observed", "degenerate", "complete"),
                "agreement")
    outcomes = [entry["outcome"] for entry in recomputed.values()]
    compared = sum(1 for scenario in recomputed
                   if (scenario, "incumbent") in observations["observed"]
                   and (scenario, "candidate") in observations["observed"])
    expected = {
        "compared": compared,
        "agree": outcomes.count("agree"),
        "differ": outcomes.count("differ"),
        "not_observed": outcomes.count("not_observed"),
        "degenerate": outcomes.count("degenerate"),
    }
    expected["complete"] = (terminal == "compared" and expected["not_observed"] == 0
                            and expected["degenerate"] == 0 and expected["differ"] == 0
                            and compared == count)
    for key, value in expected.items():
        if block[key] != value:
            raise _refuse("R-10", f"agreement.{key} is {block[key]!r} but recomputing from the "
                                  f"observations gives {value!r}")
    blocking = sorted({entry["class"] for entry in failures
                       if entry["class"] in BLOCKING_FAILURE_CLASSES})
    if terminal == "compared" and blocking:
        raise _refuse("R-10", f"terminal.outcome is 'compared' with failure rows of class "
                              f"{blocking}; a run that failed to set up did not compare")
    return expected


def _check_closures(completeness: dict[str, Any], scenarios: list[dict[str, Any]],
                    observations: dict[str, Any], unassessed: list[dict[str, str]],
                    repeat: dict[str, Any] | None, count: int) -> None:
    """R-11 and R-12: a closure is only believed where it can be contradicted.

    Scenario closure is cross-checked against the declared set, so "drop the
    failing scenario and assert closure" is caught.  Effect closure has no
    independent list to check against -- see R-EFF in the module docstring; the
    only defence is the ``effects_digest`` recomputation in ``_read_observations``.
    """
    if completeness["scenarios"]["closed"] and len(scenarios) != count:
        raise _refuse("R-11", "completeness.scenarios is closed but the scenario array and "
                              "scenario_set.count disagree")
    declared = [entry["id"] for entry in scenarios]
    for side in SIDES:
        side_block = completeness["by_side"][side]
        if side_block["observed"]["closed"]:
            gap = [name for name in declared
                   if (name, side) not in observations["observed"]
                   and (name, side) not in observations["unobserved"]]
            if gap:
                raise _refuse("R-11", f"completeness.by_side.{side}.observed is closed but "
                                      f"{gap} have neither an observation nor an unobserved row")
        if side_block["effects"]["closed"]:
            gap = [name for name in declared
                   if (name, side) in observations["observed"]
                   and "effects_digest" not in observations["entries"][(name, side)]]
            if gap:
                raise _refuse("R-11", f"completeness.by_side.{side}.effects is closed but "
                                      f"{gap} report no effects on that side")
        if side_block["effect_rows"]["closed"] and not side_block["effects"]["closed"]:
            raise _refuse("R-11", f"completeness.by_side.{side}.effect_rows is closed while "
                                  f"effects is not; rows cannot be complete for a set that is not")
    if not completeness["unassessed"]["closed"]:
        raise _refuse("R-12", "completeness.unassessed is not closed; an open list of what was "
                              "not assessed is not a disclosure")
    dimensions = {row["dimension"] for row in unassessed}
    missing = [name for name in UNASSESSED_ALWAYS if name not in dimensions]
    if missing:
        raise _refuse("R-12", f"unassessed is missing the mandatory dimensions {missing}")
    partial_effects = any((name, side) in observations["observed"]
                          and "effects_digest" not in observations["entries"][(name, side)]
                          for name in declared for side in SIDES)
    if partial_effects and "persisted_effects" not in dimensions:
        raise _refuse("R-12", "unassessed is missing 'persisted_effects'; a side that inspected "
                              "no store must say so, not merely show no rows")
    if repeat is None and "oracle_stability" not in dimensions:
        raise _refuse("R-12", "unassessed is missing 'oracle_stability'; the receipt carries no "
                              "repeat block, so nothing bounds the incumbent's nondeterminism")


def _check_log(receipt: Mapping[str, Any], receipt_dir: Path, limits: ExportLimits,
               messages: list[str], forbidden: tuple[str, ...]) -> dict[str, Any]:
    block = _obj(receipt["log"], "log")
    _exact_keys(block, ("path", "sha256", "bytes", "redaction"), "log")
    redaction = _obj(block["redaction"], "log.redaction")
    _exact_keys(redaction, ("markers_checked", "hits"), "log.redaction")
    markers = [_text(m, f"log.redaction.markers_checked[{i}]") for i, m in
               enumerate(_arr(redaction["markers_checked"], "log.redaction.markers_checked"))]
    if _count(redaction["hits"], "log.redaction.hits") != 0:
        raise _refuse("R-13", "log.redaction.hits is not zero; a log with a credential hit is "
                              "not publishable evidence")
    declared = _sha256(block["sha256"], "log.sha256")
    path = receipt_dir / LOG_FILE
    if not path.is_file():
        # A required log block whose bytes are absent cannot support a live
        # observation. Do not let a disclosure message coexist with a passing
        # claim that silently ignored the unverifiable log.
        raise _refuse("R-3", f"{LOG_FILE} is absent; declared digest {declared[:12]!r} "
                             "was not verified against any bytes")
    text = _read_text(path, limits.file_bytes)
    _scan_text(text, LOG_FILE, forbidden)
    actual = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if actual != declared:
        raise _refuse("R-3", f"log.sha256 {declared[:12]!r} does not match the committed "
                             f"{LOG_FILE} ({actual[:12]!r})")
    size = len(text.encode("utf-8"))
    if _count(block["bytes"], "log.bytes") != size:
        raise _refuse("R-3", f"log.bytes is {block['bytes']} but {LOG_FILE} is {size} bytes")
    return {"verified": True, "sha256": declared, "markers_checked": len(markers)}


def _read_admissions(receipt_dir: Path, policy_digest: str, set_digest: str, check_id: str,
                     limits: ExportLimits, messages: list[str],
                     forbidden: tuple[str, ...], *, admissions_path: Path | None,
                     admissions_bytes: bytes | None,
                     allow_receipt_admissions: bool) -> tuple[str | None, list[dict[str, Any]]]:
    """Read only externally supplied admissions unless fixture access is explicit."""
    if admissions_path is not None and admissions_bytes is not None:
        raise _refuse("R-2", "external admissions must use a path or a byte snapshot, not both")
    path = admissions_path
    if path is None and admissions_bytes is None and allow_receipt_admissions:
        path = receipt_dir / ADMISSIONS_FILE
    if admissions_bytes is None and (path is None or not path.is_file()):
        messages.append("external observation admission record absent: no reviewer admitted "
                        "this comparison policy or scenario set, so the claim has no admission "
                        "to rest on")
        return None, []
    if admissions_bytes is not None:
        if len(admissions_bytes) > limits.file_bytes:
            raise _refuse("R-2", f"{ADMISSIONS_FILE}: {len(admissions_bytes)} bytes exceed the "
                                   f"{limits.file_bytes}-byte file limit")
        try:
            text = admissions_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _refuse("R-2", f"{ADMISSIONS_FILE}: not valid UTF-8 ({exc})") from exc
    else:
        assert path is not None
        text = _read_text(path, limits.file_bytes)
    _scan_text(text, ADMISSIONS_FILE, forbidden)

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        ledger = json.loads(text, object_pairs_hook=unique_object)
    except (json.JSONDecodeError, ValueError) as exc:
        raise _refuse("R-2", f"{ADMISSIONS_FILE}: not valid JSON ({exc})") from exc
    ledger = _obj(ledger, ADMISSIONS_FILE)
    unknown = set(ledger) - {"producer", "reviewer", "reviewed_at", "reviewed_against", "rows"}
    if unknown:
        raise _refuse("R-2", f"{ADMISSIONS_FILE}: unknown keys {sorted(unknown)}")
    reviewer = _text(ledger.get("reviewer"), f"{ADMISSIONS_FILE}.reviewer")
    # A placeholder reviewer is nobody.  A ledger that says so admits nothing:
    # the claim rests on the same absent premise as a missing ledger, so it goes
    # unresolved at policy_bound rather than deriving on a name nobody signed.
    # This is a substantive control, not a formality -- the row takes seconds to
    # write, deciding whether to trust the policy is the work.
    if reviewer.strip().lower() in UNSIGNED_REVIEWERS:
        messages.append(f"{ADMISSIONS_FILE}: reviewer is {reviewer!r}, a placeholder; an "
                        f"unsigned ledger admits nothing, so the claim has no admission "
                        f"to rest on")
        return None, []
    reviewed_at = _text(ledger.get("reviewed_at"), f"{ADMISSIONS_FILE}.reviewed_at")
    against = _obj(ledger.get("reviewed_against"), f"{ADMISSIONS_FILE}.reviewed_against")
    _exact_keys(against, ("policy", "scenario_set", "check"),
                f"{ADMISSIONS_FILE}.reviewed_against")
    # A review of another policy or another set is a real review of something
    # else: stale, not malformed.
    for key, actual in (("policy", policy_digest), ("scenario_set", set_digest),
                        ("check", check_id)):
        if against[key] != actual:
            raise StaleReceiptError(
                f"{ADMISSIONS_FILE}: reviewed_against.{key} is {str(against[key])[:12]!r}, the "
                f"receipt's is {str(actual)[:12]!r}; this review is not a review of this receipt")
    # The producer string is carried VERBATIM and is deliberately NOT checked
    # here.  Producer authority has exactly one boundary -- ``claims.validation``
    # at evidence ingestion, which compares the first whitespace token against
    # the relation's declared ``producer_classes`` and raises ``evidence-producer``.
    # Pre-checking it here would put a second, weaker gate in front of the real
    # one, and a ledger claiming ``shen`` or ``replay`` would be refused for the
    # wrong reason.  No model class is declared anywhere in this pack, so such a
    # ledger cannot be ingested at all.
    producer = _text(ledger.get("producer", f"reviewer {reviewer}"),
                     f"{ADMISSIONS_FILE}.producer")
    source = f"{producer} {reviewed_at} policy:{policy_digest[:12]} set:{set_digest[:12]}"
    rows = []
    for index, item in enumerate(_arr(ledger.get("rows"), f"{ADMISSIONS_FILE}.rows")):
        row_path = f"{ADMISSIONS_FILE}.rows[{index}]"
        entry = _obj(item, row_path)
        relation = _text(entry.get("relation"), f"{row_path}.relation")
        required = ADMISSION_ROW_KEYS.get(relation)
        if required is None:
            raise _refuse("R-2", f"{row_path}.relation {relation!r} is not an admission relation")
        # The row's required keys, plus the one optional reviewer note.  The note
        # is type-checked HERE and read NOWHERE else: it is never put in a row's
        # values below, so it reaches no relation, no evidence source and no
        # metadata, and two ledgers differing only in their notes export the same
        # bundle.  A reviewer's prose must never be able to move a verdict.
        _known_keys(entry, (*required, ADMISSION_ROW_NOTE), required, row_path)
        if ADMISSION_ROW_NOTE in entry:
            _text(entry[ADMISSION_ROW_NOTE], f"{row_path}.{ADMISSION_ROW_NOTE}")
        if relation == "policy_admitted":
            rows.append(("policy_admitted", {"policy": policy_digest, "reviewer": reviewer,
                                             "version": _text(entry["version"], f"{row_path}.version")}))
        elif relation == "scenario_set_admitted":
            rows.append(("scenario_set_admitted",
                         {"scenario_set": set_digest, "check": check_id, "reviewer": reviewer,
                          "version": _text(entry["version"], f"{row_path}.version")}))
        else:
            rows.append(("masked_difference_admitted",
                         {"policy": policy_digest,
                          "normalization": _text(entry["normalization"], f"{row_path}.normalization"),
                          "field_path": _text(entry["field_path"], f"{row_path}.field_path"),
                          "reviewer": reviewer}))
    return source, rows


# ---------------------------------------------------------------------------
# The exporter


def export_bundle(receipt_dir: str | Path, *, run: str | None = None,
                  limits: ExportLimits | None = None,
                  forbidden_strings: tuple[str, ...] | None = None,
                  admissions_path: str | Path | None = None,
                  admissions_bytes: bytes | None = None,
                  allow_receipt_admissions: bool = False) -> ExportResult:
    """Export one observation receipt directory as a validated ``Bundle``.

    ``run`` is the run the caller expects; ``None`` takes the receipt's own
    ``run.id``, and a receipt for another run is ``invalid-input``.  Returns
    ``ExportResult`` with status ``complete`` (bundle present),
    ``resource-exhausted`` (a limit tripped), ``stale`` (the reviewer's ledger
    reviewed another policy, set or check) or ``invalid-input`` (any of
    R-1..R-15, or bundle validation failed).  Never raises for a limit or a
    malformed receipt. Receipt-local admissions are ignored by default. Tests
    for committed golden receipts must opt in with
    ``allow_receipt_admissions=True``; live callers should instead supply an
    external ``admissions_path`` or a previously captured ``admissions_bytes``
    snapshot whose exact bytes they bind independently.
    """
    receipt_dir = Path(receipt_dir)
    if ((admissions_path is not None or admissions_bytes is not None)
            and allow_receipt_admissions):
        return ExportResult(STATUS_INVALID_INPUT, None, {},
                            ("external admissions and fixture receipt admissions are mutually exclusive",))
    if admissions_path is not None and admissions_bytes is not None:
        return ExportResult(STATUS_INVALID_INPUT, None, {},
                            ("external admissions must use a path or a byte snapshot, not both",))
    limits = limits or ExportLimits()
    forbidden = (FORBIDDEN_FORK_STRINGS if forbidden_strings is None
                 else tuple(s.lower() for s in forbidden_strings))
    messages: list[str] = []
    relations = {decl.name: decl for decl in observation_relations()}
    facts = _Facts(_PLACEHOLDER_IDENTITY, relations)
    header: dict[str, Any] = {}
    try:
        receipt = _read_receipt(receipt_dir, limits, forbidden)

        # --- the blocks, each refusing on the spot ---------------------------
        check = _read_check(receipt)
        sources = _read_sources(receipt)
        fixture = _read_fixture(receipt)
        policy = _read_policy(receipt)
        scenario_set = _read_scenario_set(receipt, check["id"])
        run_block = _read_run(receipt, check["id"], sources["source_digest"], fixture["digest"],
                              scenario_set["digest"], policy["digest"])
        if run is not None and run != run_block["id"]:
            raise _refuse("R-2", f"{RECEIPT_FILE}: receipt is for run {run_block['id']!r}, "
                                 f"caller asked for {run!r}")
        configuration = _read_configuration(receipt)
        # R-15.  Two distinct sides are the premise of the whole comparison: with
        # one process answering twice, "the two sides agreed" is not an
        # observation about two implementations at all.  A receipt that does not
        # claim distinct sides is REFUSED -- it was previously only a message in
        # a list a reader could drop, which is the same as no disclosure.
        if not configuration["sides_distinct"]:
            raise _refuse("R-15", "configuration.sides_distinct is false: the receipt does not "
                                  "claim the two sides were distinct processes, so an agreement "
                                  "it reports may be one process answering twice")
        # And the flag is cross-checked against the identities it summarises: a
        # receipt that claims distinct sides while describing one is refused for
        # the same reason, not believed because it said the right word.
        if configuration["side_identity"]["incumbent"] == configuration["side_identity"]["candidate"]:
            raise _refuse("R-15", "configuration.sides_distinct is true but the two sides declare "
                                  "identical side_identity, so nothing distinguishes them")
        declared_ids = {entry["id"] for entry in scenario_set["scenarios"]}
        observations = _read_observations(receipt, scenario_set["scenarios"], policy)
        normalization = _read_firings(receipt, policy, observations)
        failures = _read_failures(receipt, declared_ids)
        terminal = _obj(receipt["terminal"], "terminal")
        _exact_keys(terminal, ("outcome", "exit_code", "blocking_stage", "reason"), "terminal")
        terminal_outcome = _one_of(terminal["outcome"], TERMINAL_OUTCOMES, "terminal.outcome", "R-2")
        completeness = _read_completeness(receipt)
        unassessed = _read_unassessed(receipt)
        repeat = _read_repeat(receipt, declared_ids)

        recomputed = _recompute_results(scenario_set["scenarios"], observations, policy)
        _check_results(receipt, scenario_set["scenarios"], recomputed)
        agreement = _check_agreement(receipt, recomputed, observations, terminal_outcome,
                                     scenario_set["count"], failures)
        _check_closures(completeness, scenario_set["scenarios"], observations, unassessed,
                        repeat, scenario_set["count"])
        log = _check_log(receipt, receipt_dir, limits, messages, forbidden)

        # --- the receipt's own digest, over 1.1-1.21 -------------------------
        declared_receipt_digest = _sha256(receipt["receipt_digest"], "receipt_digest")
        body = {key: value for key, value in receipt.items() if key != "receipt_digest"}
        recomputed_receipt_digest = _digest_of(body)
        if declared_receipt_digest != recomputed_receipt_digest:
            raise _refuse("R-3", f"receipt_digest {declared_receipt_digest[:12]!r} does not "
                                 f"recompute from blocks 1.1-1.21 "
                                 f"({recomputed_receipt_digest[:12]!r})")

        run_id = run_block["id"]
        header = {"run": run_id, "nonce": run_block["nonce"], "check": check["id"],
                  "source_digest": sources["source_digest"], "fixture": fixture["digest"],
                  "scenario_set": scenario_set["digest"], "policy": policy["digest"],
                  "terminal": terminal_outcome, "agreement": agreement,
                  "receipt_digest": declared_receipt_digest}

        externals = [f"external:source:{sources['source_digest']}",
                     f"external:fixture:{fixture['digest']}",
                     f"external:policy:{policy['digest']}",
                     f"external:scenario-set:{scenario_set['digest']}"]

        # --- the run row -----------------------------------------------------
        run_eid = facts.add("observation_run", {
            "run": run_id, "nonce": run_block["nonce"], "check": check["id"],
            "candidate_commit": sources["candidate"]["commit"],
            "incumbent_commit": sources["incumbent"]["commit"],
            "source_digest": sources["source_digest"], "fixture": fixture["digest"],
            "scenario_set": scenario_set["digest"], "policy": policy["digest"],
        }, source=default_source(relations["observation_run"]), depends_on=[
            *externals,
            f"external:git-commit:{sources['candidate']['commit']}",
            f"external:git-commit:{sources['incumbent']['commit']}",
        ])
        deps = [run_eid, *externals]

        def observe(relation: str, values: dict[str, Any], *, extra: Iterable[str] = ()) -> str:
            return facts.add(relation, values, source=default_source(relations[relation]),
                             depends_on=[*deps, *extra])

        def witness(relation: str, values: dict[str, Any], predicate: str) -> str:
            return facts.add(relation, values,
                             source=witness_source(relations[relation], predicate),
                             depends_on=deps)

        # --- the scenario set ------------------------------------------------
        for scenario in scenario_set["scenarios"]:
            observe("observation_scenario", {
                "run": run_id, "scenario": scenario["id"], "kind": scenario["kind"],
                "required": "true" if scenario["required"] else "false",
                "expected_class": scenario["expected_class"]})

        # --- the observations ------------------------------------------------
        for (scenario, side), entry in sorted(observations["entries"].items()):
            if entry["outcome"] != "observed":
                continue
            observe("observation_observed", {"run": run_id, "scenario": scenario, "side": side})
            observe("observation_status", {"run": run_id, "scenario": scenario, "side": side,
                                           "status": entry["status"]})
            observe("observation_class", {"run": run_id, "scenario": scenario, "side": side,
                                          "class": entry["class"]})
            observe("observation_body", {"run": run_id, "scenario": scenario, "side": side,
                                         "body_digest": entry["body_digest"]})
            observe("observation_body_raw", {"run": run_id, "scenario": scenario, "side": side,
                                             "raw_digest": entry["raw_digest"]})
            if "effects_digest" in entry:
                # The digest is the unit of effect comparison; the receipt's
                # per-effect rows are its evidence for that digest, recomputed
                # at ingest (R-3) and not exported as rows -- no rule reads a
                # row, and the schema declares no relation for one.
                observe("observation_effects", {"run": run_id, "scenario": scenario, "side": side,
                                                "effects_digest": entry["effects_digest"]})
        for (scenario, side), reason in sorted(observations["unobserved"].items()):
            observe("observation_unobserved", {"run": run_id, "scenario": scenario,
                                               "side": side, "reason": reason})

        # --- normalizations, masks, failures, terminal, unassessed ------------
        for firing in normalization["firings"]:
            observe("observation_normalization", {"run": run_id, **firing})
        for masked in normalization["masked"]:
            observe("observation_masked_difference", {
                "run": run_id, "scenario": masked["scenario"], "facet": masked["facet"],
                "normalization": masked["normalization"], "field_path": masked["field_path"]})
        for entry in policy["normalizations"]:
            observe("observation_policy_entry", {
                "policy": policy["digest"], "entry_kind": "normalization", "id": entry["id"],
                "facet": entry["facet"], "target": entry["target"],
                "preserves": entry["preserves"], "source_sha256": entry["source_sha256"]})
        for entry in policy["exclusions"]:
            # An exclusion declares no ``preserves``: it does not transform a
            # value, it removes one from the comparison entirely.
            observe("observation_policy_entry", {
                "policy": policy["digest"], "entry_kind": "exclusion", "id": entry["id"],
                "facet": entry["facet"], "target": entry["target"],
                "preserves": "not-applicable", "source_sha256": entry["source_sha256"]})
        observe("observation_terminal", {"run": run_id, "outcome": terminal_outcome})
        for entry in failures:
            observe("observation_failure", {"run": run_id, "phase": entry["phase"],
                                            "failure_class": entry["class"]})
        for entry in unassessed:
            observe("observation_unassessed", {"run": run_id, "dimension": entry["dimension"],
                                               "reason": entry["reason"]})
        if repeat is not None:
            observe("observation_stability", {
                "run": run_id, "run_a": repeat["run_a"], "run_b": repeat["run_b"],
                "side": repeat["side"], "stable": repeat["stable"]},
                extra=[f"external:run:{repeat['run_a']}", f"external:run:{repeat['run_b']}"])
        else:
            messages.append("no repeat block: the incumbent ran once, so incumbent_stable is "
                            "unresolved and oracle_stability is a mandatory unassessed row")

        # --- the reviewer's admissions ---------------------------------------
        source, admissions = _read_admissions(
            receipt_dir, policy["digest"], scenario_set["digest"], check["id"], limits,
            messages, forbidden,
            admissions_path=Path(admissions_path) if admissions_path is not None else None,
            admissions_bytes=admissions_bytes,
            allow_receipt_admissions=allow_receipt_admissions)
        for relation, values in admissions:
            facts.add(relation, values, source=source, depends_on=deps, kind="assumption")
        if source is not None and not any(r == "masked_difference_admitted" for r, _ in admissions):
            messages.append("the admissions ledger admits no masked difference")

        # --- completeness witnesses, emitted only where the receipt closes ----
        run_closures = (
            ("scenarios", "observation_scenarios_closed", "scenarios-closed-v1"),
            ("unobserved", "observation_unobserved_closed", "unobserved-closed-v1"),
            ("normalizations", "observation_normalizations_closed", "normalizations-closed-v1"),
            ("masked_differences", "observation_masked_closed", "masked-differences-closed-v1"),
            ("failures", "observation_failures_closed", "failures-closed-v1"),
            ("unassessed", "observation_unassessed_closed", "unassessed-closed-v1"),
            ("stability", "observation_stability_closed", "stability-closed-v1"),
        )
        for key, relation, predicate in run_closures:
            if completeness[key]["closed"]:
                witness(relation, {"run": run_id}, predicate)
            else:
                messages.append(f"completeness.{key} is open ({completeness[key]['reason']}): "
                                f"no {relation} witness")
        if completeness["policy"]["closed"]:
            witness("observation_policy_closed", {"policy": policy["digest"]}, "policy-closed-v1")
        else:
            messages.append(f"completeness.policy is open ({completeness['policy']['reason']}): "
                            f"no observation_policy_closed witness")
        side_closures = (
            ("observed", "observation_observed_closed", "observed-closed-v1"),
            ("statuses", "observation_statuses_closed", "statuses-closed-v1"),
            ("classes", "observation_classes_closed", "classes-closed-v1"),
            ("bodies", "observation_bodies_closed", "bodies-closed-v1"),
            ("effects", "observation_effects_closed", "effects-closed-v1"),
        )
        for side in SIDES:
            for key, relation, predicate in side_closures:
                entry = completeness["by_side"][side][key]
                if entry["closed"]:
                    witness(relation, {"run": run_id, "side": side}, predicate)
                else:
                    messages.append(f"completeness.by_side.{side}.{key} is open "
                                    f"({entry['reason']}): no {relation} witness for {side}")
            # ``effect_rows`` has no witness relation: the per-effect rows are
            # not exported (see the effects digest above), and R-11 has already
            # refused a rows box closed over an open effects box.  An OPEN box
            # is still disclosed here, so the reader learns the rows were not
            # enumerated even though nothing downstream negates over them.
            rows_box = completeness["by_side"][side]["effect_rows"]
            if not rows_box["closed"]:
                messages.append(f"completeness.by_side.{side}.effect_rows is open "
                                f"({rows_box['reason']}); effect rows are not exported as "
                                f"relations, so no witness is withheld by this")
        # The reviewer's masked-admission closure travels with the ledger: an
        # absent ledger closes nothing, which is why the gate then withholds.
        if source is not None:
            facts.add("masked_admissions_closed", {"policy": policy["digest"]},
                      source=witness_source(relations["masked_admissions_closed"],
                                            "masked-admissions-closed-v1"),
                      depends_on=deps)
        if len(facts) > limits.rows:
            return ExportResult(STATUS_RESOURCE_EXHAUSTED, None, facts.counts(),
                                (f"rows {len(facts)} exceed limit {limits.rows}",))
    except StaleReceiptError as exc:
        return ExportResult(STATUS_STALE, None, facts.counts(), (str(exc),))
    except ExportInputError as exc:
        return ExportResult(STATUS_INVALID_INPUT, None, facts.counts(), (str(exc),))
    except OSError as exc:
        return ExportResult(STATUS_INVALID_INPUT, None, facts.counts(),
                            (f"R-2: receipt directory unreadable: {exc}",))

    if len(facts) > limits.rows:
        return ExportResult(STATUS_RESOURCE_EXHAUSTED, None, facts.counts(),
                            (f"rows {len(facts)} exceed limit {limits.rows}",))
    identity = facts.identity()
    facts.rebase(identity)
    messages.append(f"observation identity {identity} ({OBSERVATION_IDENTITY}); the receipt "
                    f"directory {str(receipt_dir)!r} and its reproduce block are run receipts, "
                    f"not identity")
    fact_atoms, evidence = facts.materialize()
    metadata = {
        "export_version": EXPORT_VERSION,
        "exporter": EXPORTER,
        "run": header["run"],
        "observation_digest": identity,
        "observation_digest_kind": OBSERVATION_IDENTITY,
        "contract": CONTRACT,
        "check": header["check"],
        "nonce": header["nonce"],
        "source_digest": header["source_digest"],
        "fixture": header["fixture"],
        "scenario_set": header["scenario_set"],
        "policy": header["policy"],
        "terminal": header["terminal"],
        "agreement": header["agreement"],
        "receipt_digest": header["receipt_digest"],
        "log": log,
        "row_counts": facts.counts(),
        "producers": {"exporter": PRODUCER},
        "reproduce": dict(_obj(receipt["reproduce"], "reproduce")),
        "receipt_dir": str(receipt_dir),
    }
    bundle = Bundle(tuple(relations.values()), fact_atoms, (), (), tuple(metadata.items()),
                    evidence=evidence)
    try:
        assert_valid(bundle)
    except ValidationError as exc:
        return ExportResult(STATUS_INVALID_INPUT, None, facts.counts(),
                            tuple(f"{i.code}: {i.message} at {i.path}" for i in exc.issues)
                            if hasattr(exc, "issues") else (str(exc),))
    return ExportResult(STATUS_COMPLETE, bundle, facts.counts(), tuple(messages))


def _without_receipts(items: Any) -> Any:
    pairs = list(items.items()) if isinstance(items, Mapping) else list(items)
    return tuple((k, v) for k, v in pairs if k not in RECEIPT_METADATA_KEYS)


def bundle_digest(bundle: Bundle) -> str:
    """The canonical digest of an exported bundle with the run receipts removed.

    ``RECEIPT_METADATA_KEYS`` -- the reproduce block, the receipt directory and
    the log's path -- say where and when the check ran, never what it observed,
    so they are excluded and a receipt never becomes identity.
    """
    metadata = []
    for key, value in _without_receipts(bundle.metadata):
        if key.startswith("source_") and isinstance(value, (Mapping, tuple, list)) \
                and all(isinstance(item, (tuple, list)) and len(item) == 2 for item in
                        (value.items() if isinstance(value, Mapping) else value)):
            value = _without_receipts(value)
        metadata.append((key, value))
    return ir_digest(replace(bundle, metadata=tuple(metadata)))


__all__ = [
    "EXPORT_VERSION", "EXPORTER", "PRODUCER", "CONTRACT", "OBSERVATION_IDENTITY",
    "RECEIPT_FILE", "LOG_FILE", "ADMISSIONS_FILE", "ADMISSION_ROW_NOTE",
    "ADMISSION_ROW_KEYS", "RECEIPT_METADATA_KEYS",
    "EVIDENCE_PREFIXES", "REQUIRED_BLOCKS", "OPTIONAL_BLOCKS", "COMPLETENESS_KEYS",
    "BY_SIDE_KEYS", "UNASSESSED_ALWAYS", "UNASSESSED_CONDITIONAL", "UNASSESSED_VOCABULARY",
    "CREDENTIAL_MARKERS", "FORBIDDEN_FORK_STRINGS", "REFUSALS", "refusal_rule",
    "SIDES", "CLASSES", "SCENARIO_KINDS", "FACETS", "STATUS_COMPLETE",
    "STATUS_RESOURCE_EXHAUSTED", "STATUS_INVALID_INPUT", "STATUS_STALE",
    "ExportLimits", "ExportResult", "ExportInputError", "StaleReceiptError",
    "evidence_id", "evidence_prefix", "row_digest", "observation_relations_identity",
    "observation_relations", "primitive_relations", "default_source", "witness_source",
    "export_bundle", "bundle_digest",
]
