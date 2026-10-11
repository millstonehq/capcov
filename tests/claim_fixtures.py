"""Attribute bare test fixtures so they meet the unconditional evidence check.

``claims.validation`` reports ``fact-without-evidence`` for every fact that has
no evidence record, whether or not the bundle carries any evidence at all (an
evidence-free fact-bearing bundle used to validate with zero issues and so
bypassed ``evidence-producer``).  Hand-built kernel fixtures that only care
about closure semantics use this module's ``Bundle`` factory, which attaches
one synthesized record per fact when -- and only when -- the fixture passes no
evidence of its own.  The synthesized id is the evaluator's legacy
``fact:<relation>:<canonical row>`` leaf id, so leaf assertions written
against the old fallback still hold; the source names this module, or the
relation's first admitted producer class when it declares any, so the record
says where it came from.  Fixtures that pass ``evidence=`` explicitly (partial
attribution, producer tests) are returned untouched.
"""
from __future__ import annotations

from dataclasses import replace

from capcov.claims import ir

SOURCE = "tests.claim_fixtures attribution v1"


def attribute(bundle: ir.Bundle, source: str = SOURCE) -> ir.Bundle:
    """``bundle`` with one synthesized evidence record per unattributed ground fact."""
    relations = {relation.name: relation for relation in bundle.relations
                 if isinstance(relation, ir.RelationDecl)}
    attributed = {ir.canonical_json(record.atom) for record in bundle.evidence
                  if isinstance(record, ir.Evidence)}
    records = list(bundle.evidence)
    for fact in bundle.facts:
        if not isinstance(fact, ir.Atom) or fact.negated or ir.canonical_json(fact) in attributed:
            continue
        if any(not isinstance(term, ir.Constant) for term in fact.terms):
            continue
        relation = relations.get(fact.relation)
        row = [term.value for term in fact.terms]
        context = {}
        kind = "fact"
        record_source = source
        if relation is not None:
            names = [column.name for column in relation.columns]
            context = {name: row[names.index(name)] for name in relation.context_indices if name in names
                       and names.index(name) < len(row)}
            if relation.modality.value == "assumption":
                kind = "assumption"
            if relation.producer_classes:
                record_source = f"{relation.producer_classes[0]} {source}"
        records.append(ir.Evidence("fact:" + fact.relation + ":" + ir.canonical_json(row), fact,
                                   ir.Context.from_mapping(context), record_source, (), kind))
        attributed.add(ir.canonical_json(fact))
    return replace(bundle, evidence=tuple(records))


def identify_claims(bundle: ir.Bundle) -> ir.Bundle:
    """Give id-less claims the positional id the kernels fall back to (``str(index)``).

    Evidence-bearing bundles require claim ids (``claim-id``); ``KernelClaim``
    keys an id-less claim by ``str(index)``, so using that string keeps every
    differential key and report unchanged.
    """
    if all(claim.id for claim in bundle.claims if isinstance(claim, ir.Claim)):
        return bundle
    claims = tuple(replace(claim, id=str(index)) if isinstance(claim, ir.Claim) and not claim.id else claim
                   for index, claim in enumerate(bundle.claims))
    return replace(bundle, claims=claims)


def with_facts(bundle: ir.Bundle, facts) -> ir.Bundle:
    """``bundle`` with ``facts`` swapped in and its synthesized attribution rebuilt.

    ``dataclasses.replace(bundle, facts=...)`` on an attributed fixture leaves
    the old records behind (``evidence-without-fact`` / ``fact-without-evidence``);
    this drops every synthesized record and attributes the new fact set.
    """
    kept = tuple(record for record in bundle.evidence if not record.id.startswith("fact:"))
    return identify_claims(attribute(replace(bundle, facts=tuple(facts), evidence=kept)))


def Bundle(*args, **kwargs) -> ir.Bundle:  # noqa: N802 - drop-in for ir.Bundle in fixtures
    """``ir.Bundle`` whose facts are attributed (and claims identified) unless
    the caller passed evidence of its own."""
    bundle = ir.Bundle(*args, **kwargs)
    if bundle.facts and not bundle.evidence:
        return identify_claims(attribute(bundle))
    return bundle


__all__ = ["SOURCE", "attribute", "identify_claims", "with_facts", "Bundle"]
