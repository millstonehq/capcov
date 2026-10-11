"""Replay recorded incumbent vectors against a candidate.

This module is the sole producer of ``vectors-replay`` artifacts.  It binds
each result to the canonical digest of the exact vectors artifact, a candidate
build digest, and a fresh run id.  Exceptions from fixtures and endpoints are
not converted to mismatches: infrastructure failure is not behavioral proof.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import uuid

from .. import artifacts
from . import diff, normalize
from .recorder import (EXTRACTOR, REPLAY_FILE, REPLAY_KIND, VECTORS_FILE, Session,
                       op_dir, plan_cell, run_vector)
from .schema import Cell, DriverGap, Operation, ReplayArtifact, ReplayResult
from .recorder import read_vectors


def _sha256(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                 ensure_ascii=False, default=str).encode()).hexdigest()


def _candidate_digest(candidate, explicit: str | None) -> str:
    declared = getattr(candidate, "candidate_sha256", None)
    if declared is None:
        raise ValueError("candidate endpoint must declare candidate_sha256")
    if explicit is not None and explicit != declared:
        raise ValueError("candidate_sha256 disagrees with the candidate endpoint identity")
    value = declared
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("candidate_sha256 must be a lowercase sha256 digest")
    return value


def replay(operations, fixture, candidate, out_dir, mutant=None, *,
           candidate_sha256: str | None = None, run_id: str | None = None,
           manifest=None, informational=(), log=None):
    """Replay every recorded vector for ``operations`` and write replay artifacts."""
    out_dir = Path(out_dir)
    candidate_digest = _candidate_digest(candidate, candidate_sha256)
    run = run_id or uuid.uuid4().hex
    if not isinstance(run, str) or not run:
        raise ValueError("run_id must be a non-empty string")
    say = log or (lambda line: None)
    policy = normalize.freeze_comparison_policy()
    # Capture the actual recursive closure, not the method/global resolver.
    normalizer = policy._normalizer
    comparison_policy = policy.identity()
    normalize.assert_comparison_policy_source_unchanged(policy)
    token = fixture.snapshot()
    session = Session(fixture, token)
    summary = {"run_id": run, "operations": [], "vectors_recorded": 0,
               "vectors_passing": 0, "gaps_recorded": 0, "restores": 0}
    for operation in operations:
        if not isinstance(operation, Operation):
            raise TypeError("operations must contain Operation values")
        vector_path = op_dir(out_dir, operation.id) / VECTORS_FILE
        vectors = read_vectors(vector_path)
        if vectors.operation != operation.id:
            raise ValueError(f"vectors artifact operation {vectors.operation!r} does not match {operation.id!r}")
        results = []
        for vector in vectors.vectors:
            planned = plan_cell(operation, Cell(vector.cell, vector.steps), candidate, manifest, {})
            if isinstance(planned, DriverGap):
                raise ValueError(f"candidate cannot drive recorded vector {vector.id!r}: {planned.reason}")
            actual = run_vector(planned, fixture, token, candidate, vectors.declared_stores,
                                informational, session)
            expected_normal = normalizer(vector.expected)
            actual_normal = normalizer(actual)
            difference = diff.first_difference(expected_normal, actual_normal)
            result = ReplayResult(vector.id, vector.cell, difference is None, difference)
            results.append(result)
            say(f"replayed {operation.id} {vector.cell}: {'pass' if result.passed else difference}")
        artifact = ReplayArtifact(
            operation=operation.id, mutant=mutant, vectors_recorded=len(vectors.vectors),
            vectors_passing=sum(item.passed for item in results), gaps_recorded=len(vectors.gaps),
            candidate_sha256=candidate_digest, vectors_provenance=dict(vectors.provenance),
            comparison_policy=comparison_policy,
            vectors_sha256=_sha256(vectors.to_json()), run_id=run, results=results)
        write_replay(out_dir, operation, artifact, policy)
        summary["operations"].append({"id": operation.id, "vectors": len(results),
                                      "passing": artifact.vectors_passing, "gaps": len(vectors.gaps)})
        summary["vectors_recorded"] += len(results)
        summary["vectors_passing"] += artifact.vectors_passing
        summary["gaps_recorded"] += len(vectors.gaps)
    summary["restores"] = session.restores
    return summary


def write_replay(out_dir: Path, operation: Operation, replay: ReplayArtifact,
                 policy: normalize.FrozenComparisonPolicy) -> Path:
    body = replay.to_json()
    body["operation_record"] = operation.to_json()
    body["publication"] = "private-evidence"
    derived_from = artifacts.provenance(
        f"vectors-replay:{operation.id}", replay.vectors_sha256 or "", EXTRACTOR + ".replay",
        len(replay.results))
    path = op_dir(Path(out_dir), operation.id) / REPLAY_FILE
    normalize.assert_comparison_policy_source_unchanged(policy)
    artifacts.write(path, REPLAY_KIND, derived_from, body)
    path.chmod(0o600)
    return path


def read_replay(path: Path) -> ReplayArtifact:
    doc = artifacts.read(Path(path), expect_kind=REPLAY_KIND)
    return ReplayArtifact.from_json(doc)


__all__ = ["replay", "write_replay", "read_replay"]
