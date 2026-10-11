"""Combine an exported static-facts bundle with the reviewed rule pack.

The exporter (``scip_facts``) produces primitives only; the rule pack
(``experiments/claim-semantics/static/rules-static-v1.json``, loaded through
``tests/claim_semantics/static_rules/adapter.pack_bundle``) produces relation
declarations and rules only.  Both must validate alone, so both declare the
handful of relations the frozen schema points at (``STUB_RELATIONS``).
``combine`` merges the two declaration sets *by name* and refuses a duplicate
that is not byte-identical: a stub can never silently widen, narrow or retype
what the reviewed pack says (section 29, reconciliation item 2).

Nothing here evaluates anything.  The result is a validated ``Bundle`` whose
facts and evidence are the union of every input, whose rules are the pack's,
and whose claims/mappings/diagnostics/outputs are whatever the caller adds.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping

from ..ir import (Atom, Bundle, Claim, DiagnosticRule, Evidence, EvidenceMapping,
                  OutputTemplate, RelationDecl, Rule, canonical_json)
from ..validation import assert_valid


class CombineError(ValueError):
    """Two inputs disagree on something a merge may not decide."""


def merge_relations(*declaration_sets: Iterable[RelationDecl]) -> tuple[RelationDecl, ...]:
    """Union declarations by name; a non-identical duplicate is an error."""
    merged: dict[str, RelationDecl] = {}
    for declarations in declaration_sets:
        for declaration in declarations:
            existing = merged.get(declaration.name)
            if existing is None:
                merged[declaration.name] = declaration
            elif existing != declaration:
                raise CombineError(
                    f"relation {declaration.name!r} is declared twice with different shapes: "
                    f"{canonical_json(existing)} != {canonical_json(declaration)}")
    return tuple(merged.values())


def combine(*bundles: Bundle, claims: Iterable[Claim] = (), facts: Iterable[Atom] = (),
            evidence: Iterable[Evidence] = (), rules: Iterable[Rule] = (),
            mappings: Iterable[EvidenceMapping] = (),
            diagnostics: Iterable[DiagnosticRule] = (),
            outputs: Iterable[OutputTemplate] = (),
            metadata: Mapping[str, Any] | None = None, validate: bool = True) -> Bundle:
    """Merge bundles (exported facts, the rule pack, claim-time additions).

    Relations merge by name (identical or error); facts, rules, claims,
    mappings, diagnostics and outputs are unioned as sets; evidence ids must
    be unique across inputs.  Every input's ``diagnostic_policy`` must agree.
    Metadata is the union of the inputs' metadata under ``sources[i]`` plus the
    caller's ``metadata`` at the top level, so the combined digest still binds
    each input's export identity.
    """
    if not bundles:
        raise CombineError("combine needs at least one bundle")
    policy = bundles[0].diagnostic_policy
    for bundle in bundles[1:]:
        if bundle.diagnostic_policy != policy:
            raise CombineError("inputs declare different diagnostic policies")
    relations = merge_relations(*(bundle.relations for bundle in bundles))
    seen_evidence: dict[str, Evidence] = {}
    for record in (*(r for b in bundles for r in b.evidence), *evidence):
        existing = seen_evidence.get(record.id)
        if existing is None:
            seen_evidence[record.id] = record
        elif existing != record:
            raise CombineError(f"evidence id {record.id!r} is declared twice with different content")
    combined_metadata: dict[str, Any] = {}
    for position, bundle in enumerate(bundles):
        if bundle.metadata:
            combined_metadata[f"source_{position}"] = dict(bundle.metadata)
    combined_metadata.update(dict(metadata or {}))
    result = Bundle(
        relations,
        facts=tuple({*(f for b in bundles for f in b.facts), *facts}),
        rules=tuple({*(r for b in bundles for r in b.rules), *rules}),
        claims=tuple({*(c for b in bundles for c in b.claims), *claims}),
        metadata=tuple(combined_metadata.items()),
        evidence=tuple(seen_evidence.values()),
        mappings=tuple({*(m for b in bundles for m in b.mappings), *mappings}),
        diagnostic_policy=policy,
        diagnostics=tuple({*(d for b in bundles for d in b.diagnostics), *diagnostics}),
        outputs=tuple({*(o for b in bundles for o in b.outputs), *outputs}),
    )
    if validate:
        assert_valid(result)
    return result


__all__ = ["CombineError", "merge_relations", "combine"]
