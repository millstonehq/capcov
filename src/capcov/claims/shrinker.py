"""Validity-preserving bounded ddmin for differential claim bundles."""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from .ir import Atom, Bundle, Constant, bundle_from_json, canonical_json, digest
from .validation import assert_valid


MAX_SHRINK_STEPS = 200


@dataclass(frozen=True)
class ShrinkResult:
    bundle: Bundle
    path: Path
    steps: int
    truncated: bool
    reproduced: bool


class ReplayPersistenceError(RuntimeError):
    """A differential mismatch could not be persisted and reload-checked."""


def persist_bundle(bundle: Bundle, replay_root: str | Path) -> Path:
    """Persist and independently reload-check one canonical replay bundle."""
    root = Path(replay_root)
    path = None
    try:
        path = root / f"{digest(bundle)}.json"
        root.mkdir(parents=True, exist_ok=True)
        path.write_text(canonical_json(bundle) + "\n", encoding="utf-8")
        # Strict parsing and digest identity apply even when the differential
        # input is itself invalid.  Candidate validity is checked separately.
        reloaded = bundle_from_json(path.read_text(encoding="utf-8"), validate=False)
    except (OSError, TypeError, ValueError) as exc:
        location = path or root / "<no-canonical-digest>"
        raise ReplayPersistenceError(
            f"differential replay persistence failed: {location}: {exc}") from exc
    if digest(reloaded) != digest(bundle):
        raise ReplayPersistenceError("persisted replay digest changed on reload")
    return path


def _key(atom: Atom) -> tuple[str, str]:
    return atom.relation, canonical_json(tuple(term.value if isinstance(term, Constant) else None
                                                for term in atom.terms))


Unit = tuple[str, str, str]


def _fact_unit(atom: Atom) -> Unit:
    relation, row = _key(atom)
    return "fact", relation, row


def _evidence_unit(record: Any) -> Unit:
    return "evidence", record.id, ""


def _units(bundle: Bundle) -> set[Unit]:
    return ({_fact_unit(fact) for fact in bundle.facts}
            | {_evidence_unit(record) for record in bundle.evidence})


def _candidate(bundle: Bundle, selected: set[Unit]) -> Bundle | None:
    """Build a valid candidate while preserving fact/evidence dependencies."""
    facts = tuple(fact for fact in bundle.facts if _fact_unit(fact) in selected)
    fact_keys = {_key(fact) for fact in facts}
    original_ids = {record.id for record in bundle.evidence}
    evidence = [record for record in bundle.evidence
                if _evidence_unit(record) in selected and _key(record.atom) in fact_keys]

    # A retained evidence record must retain every in-bundle dependency.
    # Removing a dependency therefore removes its dependants rather than
    # manufacturing an independent producer.
    changed = True
    while changed:
        retained_ids = {record.id for record in evidence}
        next_evidence = [record for record in evidence
                         if all(dependency.startswith("external:")
                                or dependency not in original_ids
                                or dependency in retained_ids
                                for dependency in record.depends_on)]
        changed = len(next_evidence) != len(evidence)
        evidence = next_evidence

    if bundle.evidence:
        attributed = {_key(record.atom) for record in evidence}
        facts = tuple(fact for fact in facts if _key(fact) in attributed)
        fact_keys = {_key(fact) for fact in facts}
        evidence = [record for record in evidence if _key(record.atom) in fact_keys]

    # Facts and their producer records are the reducer's only dimensions.
    # In particular, never "repair" a candidate by rewriting reviewed output
    # policy.  If a retained OutputTemplate references removed evidence,
    # validation rejects that candidate and the evidence is not removable.
    candidate = replace(bundle, facts=facts, evidence=tuple(evidence))
    try:
        assert_valid(candidate)
    except (ValueError, OverflowError, RecursionError):
        return None
    return candidate


def _difference_shape(left: Any, right: Any) -> tuple[Any, ...]:
    """Return the exact values that constitute this disagreement.

    Equal relation payloads and equal claim fields may change while unrelated
    facts are removed.  Values that differ may not: accepting merely the same
    relation or field names could turn one bug into a different bug while
    labelling the replay a reproduction.
    """
    if left.operational_failure or right.operational_failure:
        return "operational", left.operational_failure, right.operational_failure

    absent = ("absent",)
    left_relations = dict(left.relations)
    right_relations = dict(right.relations)
    relation_values = []
    for name in sorted(set(left_relations) | set(right_relations)):
        a = ("present", left_relations[name]) if name in left_relations else absent
        b = ("present", right_relations[name]) if name in right_relations else absent
        if a != b:
            relation_values.append((name, a, b))

    claim_values = []
    claim_fields = ("key", "index", "semantic", "operational", "basis",
                    "missing_premises")
    for index in range(max(len(left.claims), len(right.claims))):
        if index >= len(left.claims):
            claim_values.append((index, absent, ("present", right.claims[index])))
            continue
        if index >= len(right.claims):
            claim_values.append((index, ("present", left.claims[index]), absent))
            continue
        a, b = left.claims[index], right.claims[index]
        differences = tuple((field, getattr(a, field), getattr(b, field))
                            for field in claim_fields
                            if getattr(a, field) != getattr(b, field))
        if differences:
            claim_values.append((index, differences))
    return "semantic", tuple(relation_values), tuple(claim_values)


def shrink_mismatch(bundle: Bundle, *, python_runner: Callable[[Bundle], Any],
                    souffle_runner: Callable[[Bundle], Any], replay_root: str | Path,
                    max_steps: int = MAX_SHRINK_STEPS,
                    baseline_left: Any | None = None,
                    baseline_right: Any | None = None) -> ShrinkResult:
    """Minimise fact/Evidence units within at most 200 comparisons.

    One step invokes both kernel runners.  The caller's initial comparison is
    outside ``steps``.  The first step reruns the persisted original bytes;
    when reduction occurs, one final step reruns the persisted candidate.
    """
    if max_steps < 1:
        raise ValueError("max_steps must be positive")
    if max_steps > MAX_SHRINK_STEPS:
        raise ValueError(f"max_steps cannot exceed {MAX_SHRINK_STEPS}")
    from .differential import _invoke_runner, reports_match

    def run_pair(candidate: Bundle):
        return (_invoke_runner(python_runner, candidate, "python"),
                _invoke_runner(souffle_runner, candidate, "souffle"))

    if (baseline_left is None) != (baseline_right is None):
        raise ValueError("baseline reports must be provided together")

    # Persist before trusting another producer execution.  The first shrink
    # step reruns the strict replay bytes and establishes that the caller's
    # original disagreement is stable.  If it has disappeared (or changed),
    # the original input remains the blocking replay instead of raising or
    # minimizing a different disagreement.
    path = persist_bundle(bundle, replay_root)
    original = bundle_from_json(path.read_text(encoding="utf-8"), validate=False)
    replay_left, replay_right = run_pair(original)
    steps = 1
    if baseline_left is None:
        baseline_left, baseline_right = replay_left, replay_right
    baseline_shape = _difference_shape(baseline_left, baseline_right)
    if (reports_match(baseline_left, baseline_right)
            or reports_match(replay_left, replay_right)
            or _difference_shape(replay_left, replay_right) != baseline_shape):
        return ShrinkResult(original, path, steps, True, False)

    current = _units(bundle)
    truncated = False
    # Reserve the last execution for the persisted minimized replay.  With a
    # one-step budget, the strict baseline execution above is also the final
    # replay check.
    minimization_limit = max(1, max_steps - 1)

    def mismatch(candidate: Bundle) -> bool:
        nonlocal steps
        if steps >= minimization_limit:
            return False
        steps += 1
        left, right = run_pair(candidate)
        return (not reports_match(left, right)
                and _difference_shape(left, right) == baseline_shape)

    # Generalized ddmin first removes large chunks.
    granularity = 2
    while len(current) > 1 and steps < minimization_limit:
        ordered = sorted(current)
        size = max(1, (len(ordered) + granularity - 1) // granularity)
        chunks = [set(ordered[i:i + size]) for i in range(0, len(ordered), size)]
        reduced = False
        for chunk in chunks:
            if steps >= minimization_limit:
                truncated = True
                break
            trial = _candidate(bundle, current - chunk)
            if trial is not None and mismatch(trial):
                current = _units(trial)
                granularity = max(2, granularity - 1)
                reduced = True
                break
        if reduced:
            continue
        if granularity >= len(current):
            break
        granularity = min(len(current), granularity * 2)

    # Certify one-minimality over both facts and individual producer records.
    # Restart after every deletion because dependency pruning can expose a new
    # removable unit. If the execution budget ends, the replay is explicitly
    # bounded rather than labelled minimized.
    while True:
        removed = False
        checked_all = True
        for unit in sorted(current):
            if steps >= minimization_limit:
                truncated = True
                checked_all = False
                break
            trial = _candidate(bundle, current - {unit})
            if trial is not None and mismatch(trial):
                current = _units(trial)
                removed = True
                break
        if not removed:
            if not checked_all:
                truncated = True
            break

    minimized = _candidate(bundle, current)
    if minimized is None:
        minimized = bundle
        truncated = True
    if max_steps == 1:
        # The strict baseline replay is the only permitted execution.  It is
        # already a reproduction, but any unchecked unit makes minimization
        # explicitly truncated.
        truncated = truncated or bool(current)
        return ShrinkResult(original, path, steps, truncated, True)

    # Persistence is not enough: execute the strictly reloaded bytes.  This
    # catches transient/nondeterministic external failures instead of calling a
    # digest match reproduction evidence.
    path = persist_bundle(minimized, replay_root)
    reloaded = bundle_from_json(path.read_text(encoding="utf-8"), validate=False)
    steps += 1
    replay_left, replay_right = run_pair(reloaded)
    reproduced = (not reports_match(replay_left, replay_right)
                  and _difference_shape(replay_left, replay_right) == baseline_shape)
    if not reproduced:
        # Preserve the originally observed disagreement input.  It remains a
        # blocking replay, but minimization/reproduction is explicitly not
        # established and must never be described as minimal.
        path = persist_bundle(bundle, replay_root)
        reloaded = bundle_from_json(path.read_text(encoding="utf-8"), validate=False)
        truncated = True
    return ShrinkResult(reloaded, path, steps, truncated, reproduced)


__all__ = ["MAX_SHRINK_STEPS", "ReplayPersistenceError", "ShrinkResult",
           "persist_bundle", "shrink_mismatch"]
