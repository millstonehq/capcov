"""Stage C ground why / why-not over engine-independent certificates.

``certify`` / ``recheck`` already replay one ground derivation without search.
This module answers the complementary questions against the same closure:

* ``why`` — a linearised support walk of the certificate, when the row holds;
* ``why_not`` — the closest failed rule instantiations when it does not;
* ``impact`` — which certified conclusions fall if a set of leaves is revoked;
* ``shared_assumptions`` — assumption leaves every support path of a
  certificate rests on.

Nothing here launches a process, collects evidence, or folds claims.  A missing
row without a completeness witness is explained as unresolved, never as
refuted (soundiness).  Every query is bounded and reports ``truncated`` when
it stops short.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from ..ir import Atom, Bundle, Comparison, Modality

from .certificate import (DEFAULT_MAX_DEPTH, DEFAULT_MAX_NODES, CertificateError,
                          _Certifier, _compare, _ground, _key, _plain, _unify,
                          certify, normalize_relations, recheck)

WHY_VERSION = "capcov-static-why-v1"
WHY_NOT_VERSION = "capcov-static-why-not-v1"
IMPACT_VERSION = "capcov-static-impact-v1"
DEFAULT_MAX_RESULTS = 16


def _linearize(node: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not node:
        return []
    if node.get("kind") == "fact":
        return [{"kind": "fact", "relation": node["relation"], "row": list(node["row"]),
                 "evidence": list(node.get("evidence") or [])}]
    walk = [{"kind": "rule", "rule": node.get("rule"), "relation": node.get("relation"),
             "row": list(node.get("row") or [])}]
    for premise in node.get("premises") or ():
        walk.extend(_linearize(premise))
    for absent in node.get("absent") or ():
        walk.append({"kind": "absent", "relation": absent.get("relation"),
                     "row": list(absent.get("row") or [])})
    return walk


def completeness_targets(bundle: Bundle) -> dict[str, tuple[str, ...]]:
    """``completed_relation -> completeness witness names``."""
    out: dict[str, list[str]] = {}
    for decl in bundle.relations:
        if decl.modality == Modality.COMPLETENESS and decl.completes:
            out.setdefault(decl.completes, []).append(decl.name)
    return {name: tuple(sorted(witnesses)) for name, witnesses in out.items()}


def why(bundle: Bundle, relations: Any, relation: str, row: Iterable[Any], *,
        max_depth: int = DEFAULT_MAX_DEPTH, max_nodes: int = DEFAULT_MAX_NODES) -> dict[str, Any]:
    """Engine-independent explanation of why a ground row holds.

    Operates on a kernel's relation closure, not on that kernel's proof
    objects, so Python and Soufflé closures that agree yield the same why.
    """
    closure = normalize_relations(relations)
    ground = tuple(_plain(value) for value in row)
    present = _key(ground) in {_key(item) for item in closure.get(relation, [])}
    base = {"why_version": WHY_VERSION, "relation": relation, "row": list(ground),
            "bounds": {"max_depth": max_depth, "max_nodes": max_nodes}}
    if not present:
        return {**base, "holds": False, "truncated": False, "certificate": None,
                "walk": [], "leaves": [], "reason": "row is not in the closure"}
    certificate = certify(bundle, closure, relation, ground,
                          max_depth=max_depth, max_nodes=max_nodes)
    if certificate["truncated"]:
        return {**base, "holds": True, "truncated": True, "certificate": certificate,
                "walk": [], "leaves": [], "reason": certificate["truncation"]}
    return {**base, "holds": True, "truncated": False, "certificate": certificate,
            "walk": _linearize(certificate.get("derivation")),
            "leaves": list(certificate.get("leaves") or []),
            "witnesses": list(certificate.get("witnesses") or []),
            "reason": None}


def _first_failure(certifier: _Certifier, body: tuple[Any, ...], env: dict[str, Any],
                   position: int, budget: list[int]) -> dict[str, Any] | None:
    """Walk one instantiation; return the first missing/blocking item, or None if it succeeds."""
    if budget[0] <= 0:
        return {"status": "truncated", "reason": "why-not exceeded max_nodes"}
    if position == len(body):
        return None
    item = body[position]
    budget[0] -= 1
    if isinstance(item, Comparison):
        if _compare(item, env):
            return _first_failure(certifier, body, env, position + 1, budget)
        return {"status": "comparison-failed", "operator": item.operator,
                "left": _ground(item.left, env), "right": _ground(item.right, env)}
    if not isinstance(item, Atom):
        return {"status": "unsupported-body-item", "item": type(item).__name__}
    if item.negated:
        row = tuple(_ground(term, env) for term in item.terms)
        if any(value is None for value in row):
            return {"status": "negated-not-ground", "relation": item.relation}
        if certifier._present(item.relation, row):
            return {"status": "blocked-by-presence", "relation": item.relation, "row": list(row)}
        return _first_failure(certifier, body, env, position + 1, budget)
    candidates = []
    for row in certifier._candidates(item, env):
        next_env = _unify(item.terms, row, env)
        if next_env is not None:
            candidates.append((row, next_env))
    if not candidates:
        expected = [_ground(term, env) for term in item.terms]
        return {"status": "missing-premise", "relation": item.relation,
                "row": expected, "bound": [value is not None for value in expected]}
    # Prefer the candidate that gets furthest; a total success wins immediately.
    best = None
    for row, next_env in candidates:
        failure = _first_failure(certifier, body, next_env, position + 1, budget)
        if failure is None:
            return None
        if best is None or failure.get("status") != "missing-premise":
            best = failure
    return best


def why_not(bundle: Bundle, relations: Any, relation: str, row: Iterable[Any], *,
            max_depth: int = DEFAULT_MAX_DEPTH, max_nodes: int = DEFAULT_MAX_NODES,
            max_results: int = DEFAULT_MAX_RESULTS) -> dict[str, Any]:
    """Engine-independent explanation of why a ground row does not hold.

    Absence of a premise is never reported as refutation unless a completeness
    witness for that premise is in the closure.  Claim folding stays in the
    evaluator; this query only names missing or blocking ground items.
    """
    if max_results < 1:
        raise ValueError("max_results must be positive")
    ground = tuple(_plain(value) for value in row)
    base = {"why_not_version": WHY_NOT_VERSION, "relation": relation, "row": list(ground),
            "bounds": {"max_depth": max_depth, "max_nodes": max_nodes, "max_results": max_results}}
    try:
        certifier = _Certifier(bundle, relations, max_depth, max_nodes)
    except ValueError as exc:
        return {**base, "holds": False, "truncated": True, "attempts": [],
                "refuted": False, "reason": str(exc)}
    if certifier._present(relation, ground):
        return {**base, "holds": True, "truncated": False, "attempts": [],
                "refuted": False, "reason": "row is in the closure; use why"}
    decl = certifier.decls.get(relation)
    witnesses = completeness_targets(bundle)
    if decl is None:
        return {**base, "holds": False, "truncated": False, "attempts": [],
                "refuted": False, "reason": f"unknown relation {relation!r}"}
    if decl.primitive:
        ids = certifier.evidence.get((relation, _key(ground)), [])
        missing = {"status": "missing-premise", "relation": relation, "row": list(ground),
                   "reason": "no attesting evidence" if not ids else "not in the closure"}
        return {**base, "holds": False, "truncated": False, "attempts": [missing],
                "refuted": False,
                "completeness_witnesses": list(witnesses.get(relation, ())),
                "soundiness": "unresolved: absence without a completeness witness is not refutation"}
    attempts: list[dict[str, Any]] = []
    truncated = False
    budget = [max_nodes]
    for rule in certifier.rules_by_head.get(relation, ()):
        if len(attempts) >= max_results:
            truncated = True
            break
        env = _unify(rule.head.terms, ground, {})
        if env is None:
            attempts.append({"rule": rule.name, "status": "head-mismatch"})
            continue
        failure = _first_failure(certifier, rule.body, env, 0, budget)
        if failure is None:
            # A rule derives the row from the closure but the row is absent:
            # that is a kernel inconsistency, same finding certify would raise.
            raise CertificateError(
                f"no closure row {relation}{_key(ground)} but a rule derives it")
        if failure.get("status") == "truncated":
            truncated = True
        witnesses_for = list(witnesses.get(failure.get("relation"), ())) if failure.get("relation") else []
        present_witnesses = [name for name in witnesses_for if certifier.rows.get(name)]
        attempts.append({"rule": rule.name, **failure,
                         "completeness_witnesses": witnesses_for,
                         "completeness_present": present_witnesses})
    return {**base, "holds": False, "truncated": truncated, "attempts": attempts,
            "refuted": False,
            "soundiness": "unresolved: absence without a completeness witness is not refutation"}


def shared_assumptions(bundle: Bundle, certificate: Mapping[str, Any]) -> tuple[str, ...]:
    """Assumption evidence ids among a certificate's leaves, sorted."""
    kinds = {record.id: record.kind for record in bundle.evidence}
    return tuple(sorted(leaf for leaf in certificate.get("leaves") or ()
                        if kinds.get(leaf) == "assumption"))


def closed_revocation(bundle: Bundle, revoked: Iterable[str]) -> set[str]:
    """``revoked`` plus every evidence id that transitively depends on it."""
    out = set(revoked)
    changed = True
    while changed:
        changed = False
        for record in bundle.evidence:
            if record.id not in out and set(record.depends_on) & out:
                out.add(record.id)
                changed = True
    return out


def impact(bundle: Bundle, relations: Any,
           revoked: Iterable[str],
           conclusions: Iterable[tuple[str, Iterable[Any]]], *,
           max_depth: int = DEFAULT_MAX_DEPTH, max_nodes: int = DEFAULT_MAX_NODES) -> dict[str, Any]:
    """Which certified conclusions fall when ``revoked`` leaves are withdrawn.

    A conclusion falls only if every remaining support path uses a revoked
    leaf (this extractor records one canonical path; alternative support is
    reported when ``why`` still certifies after the closed revocation is
    removed from the leaf set).  The query does not re-evaluate the kernels.
    """
    closed = closed_revocation(bundle, revoked)
    closure = normalize_relations(relations)
    fallen: list[dict[str, Any]] = []
    survived: list[dict[str, Any]] = []
    truncated = False
    for relation, row in conclusions:
        ground = tuple(_plain(value) for value in row)
        if _key(ground) not in {_key(item) for item in closure.get(relation, [])}:
            continue
        certificate = certify(bundle, closure, relation, ground,
                              max_depth=max_depth, max_nodes=max_nodes)
        if certificate["truncated"]:
            truncated = True
            continue
        leaves = set(certificate.get("leaves") or ())
        entry = {"relation": relation, "row": list(ground),
                 "revoked_leaves": sorted(leaves & closed)}
        if leaves & closed:
            fallen.append(entry)
        else:
            survived.append(entry)
    return {"impact_version": IMPACT_VERSION, "revoked": sorted(closed),
            "fallen": fallen, "survived": survived, "truncated": truncated}


__all__ = [
    "WHY_VERSION", "WHY_NOT_VERSION", "IMPACT_VERSION", "DEFAULT_MAX_RESULTS",
    "completeness_targets", "why", "why_not", "shared_assumptions",
    "closed_revocation", "impact", "recheck",
]
