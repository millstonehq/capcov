"""Evaluator-independent conditional diagnostic output rendering."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .ir import (Atom, Bundle, Claim, Constant, Evidence, EvidenceMapping,
                 OutputKind, OutputTemplate, Variable, canonical_dict)


@dataclass(frozen=True)
class VerifiedProofEvidence:
    """Evaluator-issued ground proof leaves; not a certificate verifier."""
    claim_id: str
    leaf_ids: frozenset[str]
    claim_row_digest: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.claim_id, str) or not self.claim_id:
            raise ValueError("verified proof requires a claim id")
        object.__setattr__(self, "leaf_ids", frozenset(self.leaf_ids))

    @classmethod
    def from_bundle(cls, bundle: Bundle, claim_id: str, leaf_ids: set[str] | frozenset[str], claim_row_digest: str | None = None) -> "VerifiedProofEvidence":
        known = {record.id for record in bundle.evidence}
        unknown = set(leaf_ids) - known
        if unknown:
            raise ValueError(f"unknown proof leaves: {sorted(unknown)}")
        if not any(claim.id == claim_id for claim in bundle.claims):
            raise ValueError(f"unknown proof claim: {claim_id}")
        return cls(claim_id, frozenset(leaf_ids), claim_row_digest)


def _claim(bundle: Bundle, claim_id: str) -> Claim | None:
    return next((claim for claim in bundle.claims if claim.id == claim_id), None)


def _evidence_values(bundle: Bundle, evidence_id: str) -> dict[str, Any]:
    record = next((item for item in bundle.evidence if item.id == evidence_id), None)
    if record is None:
        return {}
    relation = next((item for item in bundle.relations if item.name == record.atom.relation), None)
    values = {"id": evidence_id}
    if relation:
        for column, term in zip(relation.columns, record.atom.terms):
            if isinstance(term, Constant):
                values[column.name] = term.value
    return values


def _predicate_matches(value: Any, operator: str, expected: Any) -> bool:
    if operator == "=": return value == expected
    if operator == "!=": return value != expected
    if operator == "in": return value in expected if isinstance(expected, (list, tuple)) else False
    if operator == "not-in": return value not in expected if isinstance(expected, (list, tuple)) else False
    return False


def _mapping_environment(
        bundle: Bundle, claim: Claim, evidence: Evidence,
        mapping: EvidenceMapping, variables: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Unify one mapped evidence row, optionally extending shared variables."""
    claim_relation = next(
        (relation for relation in bundle.relations
         if relation.name == claim.relation), None)
    evidence_relation = next(
        (relation for relation in bundle.relations
         if relation.name == evidence.atom.relation), None)
    if (claim_relation is None or evidence_relation is None
            or mapping.claim_relation != claim.relation):
        return None
    claim_names = [column.name for column in claim_relation.columns]
    evidence_values = _evidence_values(bundle, evidence.id)
    claim_values = dict(claim.context.as_dict())
    claim_values.update(
        (column.name, term.value)
        for column, term in zip(claim_relation.columns, claim.terms)
        if isinstance(term, Constant))
    unified = dict(variables or {})
    for left, right in mapping.bindings:
        if left not in claim_names or right not in evidence_values:
            return None
        value = evidence_values[right]
        term = claim.terms[claim_names.index(left)]
        if isinstance(term, Constant) and term.value != value:
            return None
        if isinstance(term, Variable):
            previous = unified.setdefault(term.name, value)
            if previous != value:
                return None
        elif not isinstance(term, Constant):
            return None
        if left in claim_values and claim_values[left] != value:
            return None
    # Output mappings intentionally scope only their declared context indices.
    # Observation mappings may compare a differing claim/evidence dimension in
    # order to render that mismatch; treating every shared column as an
    # implicit equality would hide the very diagnostic they declare.
    evidence_context = evidence.context.as_dict()
    if any(evidence_context.get(index) != claim_values.get(index)
           for index in mapping.context_indices):
        return None
    return unified


def _mapping_unifies(bundle: Bundle, claim: Claim, evidence: Evidence,
                      mapping: EvidenceMapping) -> bool:
    """Match one mapped evidence row against a possibly open claim tuple."""
    return _mapping_environment(bundle, claim, evidence, mapping) is not None


def _joint_environments(
        bundle: Bundle, claim: Claim, evidence_ids: tuple[str, ...],
        variables: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], ...]:
    """Enumerate shared environments for mapped trigger evidence."""
    environment = dict(variables or {})
    if not evidence_ids:
        return (environment,)
    evidence_id, *rest = evidence_ids
    evidence = next((item for item in bundle.evidence
                     if item.id == evidence_id), None)
    if evidence is None:
        return ()
    mappings = tuple(
        mapping for mapping in bundle.mappings
        if mapping.claim_id == claim.id
        and mapping.evidence_relation == evidence.atom.relation)
    # Evidence relevant only through a diagnostic or proof path has no mapping
    # variables to constrain.  Once an applicable mapping exists, however, it
    # is a real constraint: if every mapping fails, this evidence cannot be
    # silently discarded from the joint trigger environment.
    if not mappings:
        return _joint_environments(bundle, claim, tuple(rest), environment)
    results = []
    for mapping in mappings:
        candidate = _mapping_environment(
            bundle, claim, evidence, mapping, environment)
        if candidate is not None:
            results.extend(_joint_environments(
                bundle, claim, tuple(rest), candidate))
    return tuple(results)


def _jointly_unifies(bundle: Bundle, claim: Claim,
                      evidence_ids: tuple[str, ...],
                      variables: dict[str, Any] | None = None) -> bool:
    """Require mapped trigger rows to describe one shared claim instance."""
    return bool(_joint_environments(bundle, claim, evidence_ids, variables))


def _relevant(bundle: Bundle, claim_id: str, evidence_id: str, proof: set[str],
              scoped_claim: Claim | None = None) -> bool:
    evidence = next((item for item in bundle.evidence if item.id == evidence_id), None)
    claim = scoped_claim or _claim(bundle, claim_id)
    if evidence is None or claim is None:
        return False
    values = _evidence_values(bundle, evidence_id)
    claim_context = claim.context.as_dict()
    for mapping in bundle.mappings:
        if mapping.claim_id != claim_id or mapping.evidence_relation != evidence.atom.relation:
            continue
        if _mapping_unifies(bundle, claim, evidence, mapping):
            return True
    for diagnostic in bundle.diagnostics:
        if diagnostic.claim_id != claim_id or diagnostic.trigger_relation != evidence.atom.relation or diagnostic.when_missing:
            continue
        if any(evidence.context.as_dict().get(index) != claim_context.get(index) for index in diagnostic.context_indices):
            continue
        predicate = dict(diagnostic.predicate)
        if predicate and not _predicate_matches(values.get(predicate.get("column")), predicate.get("operator"), predicate.get("value")):
            continue
        return True
    if evidence_id in proof:
        claim_context = claim.context.as_dict()
        if any(evidence.context.as_dict().get(name) != value for name, value in claim_context.items() if name in evidence.context.as_dict()):
            return False
        claim_relation = next((r for r in bundle.relations if r.name == claim.relation), None)
        claim_values = dict(claim.context.as_dict())
        if claim_relation:
            claim_values.update(
                (column.name, term.value)
                for column, term in zip(claim_relation.columns, claim.terms)
                if isinstance(term, Constant))
        if any(name in values and values[name] != value for name, value in claim_values.items()):
            return False
        # a rule body also holds Comparison items, which name no relation
        return any(rule.head.relation == claim.relation
                   and any(isinstance(item, Atom) and item.relation == evidence.atom.relation
                           for item in rule.body)
                   for rule in bundle.rules)
    return False


def relevant_evidence_ids(bundle: Bundle, claim_id: str,
                          active_evidence: set[str] | frozenset[str],
                          proof_evidence: set[str] | frozenset[str] = frozenset(),
                          *, scoped_claim: Claim | None = None) -> set[str]:
    """Return active Evidence ids scoped to one claim instance.

    ``scoped_claim`` is used for grounded members of a universal claim; looking
    the id up in the bundle would recover the open aggregate claim instead.
    """
    proof = set(proof_evidence)
    return {evidence_id for evidence_id in active_evidence
            if _relevant(bundle, claim_id, evidence_id, proof, scoped_claim)}


def output_triggered(
        output: OutputTemplate, relevant: set[str],
        claim_state: str | Iterable[str] | None, *, bundle: Bundle | None = None,
        scoped_claim: Claim | None = None,
) -> bool:
    """Apply every declarative output condition to a reviewed evidence set.

    When the bundle and claim are supplied, all mapped evidence consumed by a
    trigger or field must jointly unify.  Individual relevance is insufficient:
    two rows assigning different values to one open claim variable cannot
    jointly describe a single claim instance.
    """
    required = set(output.requires_all_evidence)
    if output.evidence_id:
        required.add(output.evidence_id)
    required.update(
        template.evidence_id for _, template in output.fields
        if template.source == "evidence" and template.evidence_id)
    if not required.issubset(relevant):
        return False
    any_relevant = set(output.requires_any_evidence) & relevant
    if output.requires_any_evidence and not any_relevant:
        return False
    excluded_relevant = set(output.excludes_evidence) & relevant
    states = ({claim_state} if isinstance(claim_state, str)
              else set(claim_state or ()))
    if output.when_claim not in (None, "always") and output.when_claim not in states:
        return False
    if bundle is None:
        # Preserve the context-free helper contract: without IR mappings there
        # is no environment in which an exclusion can be interpreted.
        return not excluded_relevant
    claim = scoped_claim or _claim(bundle, output.claim_id)
    if claim is None:
        return False
    alternatives = tuple(sorted(any_relevant)) if any_relevant else (None,)
    for alternative in alternatives:
        selected = required | ({alternative} if alternative else set())
        for environment in _joint_environments(
                bundle, claim, tuple(sorted(selected))):
            # Exclusion is an anti-join over the same claim instance.  An
            # excluded X=b row must not suppress a required X=a candidate,
            # while another excluded X=a producer still suppresses it.
            if not any(_joint_environments(
                    bundle, claim, (evidence_id,), environment)
                       for evidence_id in excluded_relevant):
                return True
    return False


def render_outputs(bundle: Bundle, claim_id: str, active_evidence: set[str] | frozenset[str], proof_evidence: VerifiedProofEvidence | None = None, claim_state: str | None = None, missing_relations: set[str] | frozenset[str] = frozenset()) -> tuple[dict[str, Any], ...]:
    """Render active, typed output templates for one claim deterministically."""
    active = set(active_evidence)
    if proof_evidence is not None and not isinstance(proof_evidence, VerifiedProofEvidence):
        raise TypeError("proof_evidence must be VerifiedProofEvidence")
    if proof_evidence is not None and proof_evidence.claim_id != claim_id:
        raise ValueError("proof claim does not match rendered claim")
    known_ids = {record.id for record in bundle.evidence}
    if proof_evidence and not proof_evidence.leaf_ids.issubset(known_ids):
        raise ValueError("proof contains unknown evidence leaves")
    proof = set(proof_evidence.leaf_ids) if proof_evidence else set()
    relevant = relevant_evidence_ids(bundle, claim_id, active, proof)
    rendered = []
    for output in bundle.outputs:
        if (output.claim_id != claim_id
                or not output_triggered(
                    output, relevant, claim_state, bundle=bundle,
                    scoped_claim=_claim(bundle, claim_id))):
            continue
        if output.evidence_id and output.evidence_id not in relevant:
            continue
        if output.kind == OutputKind.MISSING_PREMISE and output.relation not in set(missing_relations):
            continue
        item: dict[str, Any] = {"kind": output.kind.value, "claim_id": output.claim_id}
        if output.evidence_id:
            item["evidence_id"] = output.evidence_id
        if output.relation:
            item["relation"] = output.relation
        fields = {}
        claim = _claim(bundle, claim_id)
        claim_values = {}
        if claim:
            relation = next((r for r in bundle.relations if r.name == claim.relation), None)
            if relation:
                claim_values = {column.name: term.value for column, term in zip(relation.columns, claim.terms) if isinstance(term, Constant)}
        for name, template in output.fields:
            if template.source == "constant":
                # Template constants are frozen on entry so producer-owned
                # lists and mappings cannot mutate a Bundle after hashing.
                # Render their ordinary JSON shape at the public boundary.
                value = canonical_dict(template.value)
            elif template.source == "claim":
                value = claim_values.get(template.column)
            else:
                evidence_id = template.evidence_id or output.evidence_id
                if not evidence_id or evidence_id not in relevant:
                    break
                value = _evidence_values(bundle, evidence_id).get(template.column)
            fields[name] = value
        else:
            if fields:
                item["fields"] = dict(sorted(fields.items()))
            rendered.append(item)
    unique = {repr(item): item for item in rendered}
    return tuple(unique[key] for key in sorted(unique))
