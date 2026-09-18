"""Bind boundary-vector replay to the independent claim kernels.

This is deliberately an adapter, not another vector roll-up.  It validates
that a replay is the exact counterpart of one recorded artifact, emits typed
evidence, and asks the explicitly selected Python or Souffle kernel to judge
it. Differential execution is an opt-in qualification gate, not the runtime
default.

Two claims stay separate:

* ``vector_cell_recorded`` says a required template cell produced a vector.
  A named gap therefore remains unresolved.
* ``vector_matches`` says that exact vector matched the candidate.  A failed
  comparison is refutation, never missing evidence and never support.

An operation is not declared complete here.  Consumers may close an operation
only after every required cell claim is supported and every vector-match claim
is supported.  Keeping that roll-up outside this adapter avoids silently
inventing a closed census from one operation artifact.

Serialized replay is local evidence, not an authenticated producer
attestation.  Replay leaves are therefore assumptions and metadata says
``local-unattested``.  A later trust-boundary adapter may promote them only
after verifying a reviewed external attestation; this module exposes no flag
that lets a caller self-promote them.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import re
from pathlib import Path
from typing import Any, Callable

from ..claims import assumptions
from ..claims import shen
from ..claims.differential import (KernelReport, compare_three, run_python,
                                   run_souffle_compiled)
from ..claims.souffle.compile import CompiledChecker
from ..claims.ir import (Atom, Bundle, Claim, Column, Constant, Context, Evidence,
                         EvidenceMapping, RelationDecl, Rule, Variable, digest)
from ..claims.validation import assert_valid
from . import normalize
from .schema import ReplayArtifact, VectorsArtifact

RECORDER_PRODUCER = "capcov-vectors-recorder-v1"
REPLAY_PRODUCER = "capcov-vectors-replay-v1"
_SHA256 = re.compile(r"[0-9a-f]{64}")


class VectorClaimError(ValueError):
    """The artifacts cannot safely be joined."""


@dataclass(frozen=True)
class VectorJudgment:
    bundle: Bundle
    authority: Any
    result: Any
    certificates: dict[str, dict[str, Any]]
    row_certificates: dict[str, list[dict[str, Any]]]

    @property
    def report(self) -> KernelReport:
        """The report used for verdicts, independent of execution mode."""
        if isinstance(self.result, KernelReport):
            return self.result
        return self.result.compiled

    def verdicts(self) -> dict[str, dict[str, Any]]:
        return {
            claim.key: {
                "semantic": claim.semantic,
                "operational": claim.operational,
                "basis": claim.basis,
                "missing_premises": list(claim.missing_premises),
                "certificate_sha256": (assumptions.certificate_sha256(self.certificates[claim.key])
                                       if claim.key in self.certificates else None),
            }
            for claim in self.report.claims
        }


def artifact_digest(artifact: VectorsArtifact) -> str:
    """Canonical digest consumers must put in ``ReplayArtifact.vectors_sha256``."""
    return digest(artifact.to_json())


def _digest(value: str | None, name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise VectorClaimError(f"{name} must be a lowercase sha256 digest")
    return value


def _atom(relation: str, values: tuple[str, ...], types: tuple[str, ...]) -> Atom:
    return Atom(relation, tuple(Constant(value, kind) for value, kind in zip(values, types)))


def _context(recording: str, candidate: str, operation: str, cell: str) -> Context:
    return Context.from_mapping({"recording": recording, "candidate": candidate,
                                 "operation": operation, "cell": cell})


def _evidence(relation: RelationDecl, values: tuple[str, ...], source: str,
              *, kind: str = "fact") -> tuple[Atom, Evidence]:
    atom = _atom(relation.name, values, tuple(str(column.type) for column in relation.columns))
    names = [column.name for column in relation.columns]
    context = Context.from_mapping({name: values[names.index(name)] for name in relation.context_indices})
    identity = digest({"relation": relation.name, "row": values, "source": source})
    return atom, Evidence(f"ev:vector:{identity}", atom, context, source, kind=kind)


def build_bundle(vectors: VectorsArtifact, replay: ReplayArtifact) -> Bundle:
    """Validate and convert one vectors/replay pair into a claim bundle."""
    if vectors.operation != replay.operation:
        raise VectorClaimError("vectors and replay name different operations")
    if replay.mutant is not None:
        raise VectorClaimError("mutant replay is diagnostic and cannot qualify a candidate")
    if replay.operational_failure:
        raise VectorClaimError(f"replay has an operational failure: {replay.operational_failure}")
    if not isinstance(replay.run_id, str) or not replay.run_id:
        raise VectorClaimError("replay must carry a non-empty run_id")
    candidate = _digest(replay.candidate_sha256, "candidate_sha256")
    recording = artifact_digest(vectors)
    if replay.vectors_sha256 != recording:
        raise VectorClaimError("replay is not bound to the exact vectors artifact")
    if replay.vectors_provenance != vectors.provenance:
        raise VectorClaimError("replay and vectors provenance differ")
    if not normalize.validate_comparison_policy_identity(replay.comparison_policy):
        raise VectorClaimError(
            "replay comparison-policy identity is missing, malformed, or internally inconsistent")

    expected = {vector.id: vector for vector in vectors.vectors}
    if len(expected) != len(vectors.vectors):
        raise VectorClaimError("vectors contain duplicate ids")
    results = {result.id: result for result in replay.results}
    if len(results) != len(replay.results):
        raise VectorClaimError("replay contains duplicate result ids")
    if set(results) != set(expected):
        raise VectorClaimError("replay result ids do not exactly cover recorded vectors")
    for vector_id, result in results.items():
        if result.cell != expected[vector_id].cell:
            raise VectorClaimError(f"replay result {vector_id!r} changed its cell")
        if result.passed == (result.difference is not None):
            raise VectorClaimError(f"replay result {vector_id!r} has inconsistent pass/difference fields")
        recorded_vector = expected[vector_id]
        information = (recorded_vector.expected.get("informational")
                       if isinstance(recorded_vector.expected, dict) else None)
        if isinstance(information, dict) and (information.get("unbound_tables")
                                               or information.get("observed_fallback")):
            raise VectorClaimError(f"recorded vector {vector_id!r} has unbound store changes")
    passing = sum(result.passed for result in results.values())
    if replay.vectors_recorded != len(expected) or replay.vectors_passing != passing:
        raise VectorClaimError("replay summary counts disagree with its results")
    if replay.gaps_recorded != len(vectors.gaps):
        raise VectorClaimError("replay gap count disagrees with vectors")
    required = vectors.required_cells
    if (not required or len(required) != len(set(required))
            or any(not isinstance(cell, str) or not cell for cell in required)):
        raise VectorClaimError("vectors must carry a unique non-empty required cell set")
    vector_cells = {vector.cell for vector in vectors.vectors}
    gap_cells = {item.get("cell") for item in vectors.gaps}
    overlap = vector_cells & gap_cells
    if overlap:
        raise VectorClaimError(f"cells cannot be both vectors and gaps: {sorted(overlap)}")
    represented = vector_cells | gap_cells
    if set(required) != represented:
        raise VectorClaimError("vectors and gaps do not exactly cover the required cell set")

    common = (Column("recording", "digest", True), Column("candidate", "digest", True),
              Column("operation", "symbol", True), Column("cell", "symbol", True),
              Column("vector", "symbol"))
    passed = RelationDecl("vector_replay_passed", common,
                          producer_classes=(REPLAY_PRODUCER,),
                          context_indices=("recording", "candidate", "operation", "cell"))
    failed = RelationDecl("vector_replay_failed", common,
                          producer_classes=(REPLAY_PRODUCER,),
                          context_indices=("recording", "candidate", "operation", "cell"))
    matched = RelationDecl("vector_matches", common, modality="claim", primitive=False,
                           context_indices=("recording", "candidate", "operation", "cell"))
    cell_columns = common[:-1]
    recorded = RelationDecl("vector_cell_recorded", cell_columns, modality="claim", primitive=False,
                            context_indices=("recording", "candidate", "operation", "cell"))
    gap_columns = (Column("recording", "digest", True), Column("operation", "symbol", True),
                   Column("cell", "symbol", True), Column("reason", "digest"))
    gap = RelationDecl("vector_recording_gap", gap_columns,
                       producer_classes=(RECORDER_PRODUCER,),
                       context_indices=("recording", "operation", "cell"))

    variables = tuple(Variable(name) for name in ("Recording", "Candidate", "Operation", "Cell", "Vector"))
    cell_head = Atom("vector_cell_recorded", variables[:-1])
    rules = (
        Rule(Atom("vector_matches", variables), (Atom("vector_replay_passed", variables),), "passing_vector_matches"),
        Rule(cell_head, (Atom("vector_replay_passed", variables),), "passing_vector_was_recorded"),
        Rule(cell_head, (Atom("vector_replay_failed", variables),), "failing_vector_was_recorded"),
    )
    facts: list[Atom] = []
    evidence: list[Evidence] = []
    claims: list[Claim] = []
    mappings: list[EvidenceMapping] = []
    operation = vectors.operation
    required_cells: set[str] = set()
    for vector in vectors.vectors:
        result = results[vector.id]
        relation = passed if result.passed else failed
        values = (recording, candidate, operation, vector.cell, vector.id)
        atom, record = _evidence(relation, values, f"{REPLAY_PRODUCER} {digest(replay.to_json())}",
                                 kind="assumption")
        facts.append(atom); evidence.append(record)
        context = _context(recording, candidate, operation, vector.cell)
        claim_id = f"vector-match:{digest(values)}"
        claims.append(Claim("vector_matches", tuple(atom.terms), context, id=claim_id))
        if not result.passed:
            mappings.append(EvidenceMapping(
                "vector_matches", "vector_replay_failed", "refutation",
                bindings=(("recording", "recording"), ("candidate", "candidate"),
                          ("operation", "operation"), ("cell", "cell"), ("vector", "vector")),
                claim_id=claim_id))
        required_cells.add(vector.cell)
    for item in vectors.gaps:
        cell = item.get("cell")
        reason = item.get("reason")
        if not isinstance(cell, str) or not cell or not isinstance(reason, str) or not reason:
            raise VectorClaimError("every gap must have a non-empty cell and reason")
        reason_digest = digest(reason)
        atom, record = _evidence(gap, (recording, operation, cell, reason_digest),
                                 f"{RECORDER_PRODUCER} {recording}")
        facts.append(atom); evidence.append(record); required_cells.add(cell)
    for cell in sorted(required_cells):
        values = (recording, candidate, operation, cell)
        claims.append(Claim("vector_cell_recorded",
                            _atom("vector_cell_recorded", values,
                                  ("digest", "digest", "symbol", "symbol")).terms,
                            _context(recording, candidate, operation, cell),
                            id=f"vector-cell-recorded:{digest(values)}"))

    bundle = Bundle(
        (passed, failed, matched, recorded, gap), tuple(facts), rules, tuple(claims),
        metadata=(("candidate_sha256", candidate), ("operation", operation),
                  ("comparison_policy", replay.comparison_policy),
                  ("comparison_policy_sha256", replay.comparison_policy["policy_sha256"]),
                  ("producer_authority", "local-unattested"),
                  ("replay_sha256", digest(replay.to_json())), ("vectors_sha256", recording)),
        evidence=tuple(evidence), mappings=tuple(mappings))
    return assert_valid(bundle)


def judge(vectors: VectorsArtifact, replay: ReplayArtifact, *,
          replay_root: str | Path = ".capcov/vector-claims",
          kernel: str = "souffle", checker: CompiledChecker | None = None,
          cache_dir: str | Path = ".capcov/compiled", executable: str = "souffle",
          python_runner: Callable[[Bundle], KernelReport] | None = None,
          compiled_runner: Callable[[Bundle], KernelReport] | None = None,
          authority_runner: Callable[[Bundle], Any] = shen.authority) -> VectorJudgment:
    """Judge with one selected kernel, or opt into the differential gate.

    ``python`` executes only the Python evaluator. ``souffle`` executes only
    the compiled Souffle checker. ``differential`` is the explicit publication
    gate and runs Python, interpreted Souffle, and compiled Souffle.
    """
    bundle = build_bundle(vectors, replay)
    if kernel not in ("python", "souffle", "differential"):
        raise ValueError("kernel must be 'python', 'souffle', or 'differential'")
    bundle = replace(bundle, metadata=(*bundle.metadata, ("claim_kernel", kernel)))
    authority = authority_runner(bundle)
    if not authority.ok:
        raise VectorClaimError(f"Shen authority rejected vector rules: {authority.failed_checks()}")
    selected_python = python_runner or run_python
    if compiled_runner is None:
        selected_compiled = lambda candidate: run_souffle_compiled(
            candidate, checker=checker, cache_dir=cache_dir, executable=executable)
    else:
        selected_compiled = compiled_runner
    if kernel == "python":
        result = selected_python(bundle)
    elif kernel == "souffle":
        result = selected_compiled(bundle)
    else:
        result = compare_three(
            bundle, replay_root=replay_root, checker=checker, cache_dir=cache_dir,
            executable=executable, python_runner=selected_python,
            compiled_runner=selected_compiled)
    if isinstance(result, KernelReport) and result.operational_failure:
        raise VectorClaimError(
            f"{result.backend} kernel failed: {result.operational_failure}: {result.message}")
    certificates, rows = assumptions.certify_claims(bundle, result)
    return VectorJudgment(bundle, authority, result, certificates, rows)


__all__ = ["RECORDER_PRODUCER", "REPLAY_PRODUCER", "VectorClaimError",
           "VectorJudgment", "artifact_digest", "build_bundle", "judge"]
