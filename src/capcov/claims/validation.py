"""Static validation for the restricted, finite claim-rule language."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from .ir import (Aggregation, Atom, BindingTime, Bundle, Claim, Column,
                 Comparison, Constant, Context, DiagnosticRule, Evidence,
                 EvidenceEffect, EvidenceMapping, Modality, OutputKind,
                 OutputTemplate, Polarity, Quantifier, RelationDecl, Rule,
                 TemplateValue, TypeName, Variable, canonical_json)

DIAGNOSTIC_VOCABULARY = frozenset({"same_surface", "row_committed", "mail_sent", "same_context", "independent_support", "all_compatible_histories_agree", "model_complete", "sql_terminal_ack", "no_resend_forever"})

# Validation is part of both untrusted kernel boundaries.  Keep dependency
# traversal below the interpreter recursion limit and report deeper producer
# graphs as resource exhaustion rather than leaking RecursionError.
MAX_RULE_DEPENDENCY_DEPTH = 256

# These names are identity/correlation dimensions in the frozen Stage B
# contract.  A support/refutation mapping is not aggregation authority and may
# not silently project them away when both relations carry them.
_CAUSAL_IDENTITY_COLUMNS = frozenset({
    "tenant", "actor", "run", "request", "event", "notification",
    "recipient", "message", "attempt", "interval", "environment",
    "configuration", "candidate_build", "reference_build", "source_digest",
    "model_digest",
})

# The reviewed bounded-history domain carries an explanatory outcome label in
# addition to its ``history`` member key. It is payload, not another identity
# that the Boolean aggregate source must duplicate.
_AGGREGATE_DOMAIN_PAYLOAD_COLUMNS = frozenset({("compatible_history", "outcome")})

# Static identity omission is an exact schema fingerprint, never a naming
# convention.  Section 29 declares only source_tree_observed as genuinely
# context-free; tenant/event/surface keyed legacy observations still need an
# index because those payloads do not identify the source snapshot.
_CONTEXT_FREE_STATIC_DECLARATIONS = frozenset({
    ("source_tree_observed", (("tree_digest", TypeName.DIGEST, False),), ()),
})


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    path: str = ""
    def __str__(self): return f"{self.code}: {self.message}" + (f" ({self.path})" if self.path else "")


class ValidationError(ValueError):
    def __init__(self, issues: Iterable[ValidationIssue]):
        self.issues = tuple(issues)
        super().__init__("; ".join(map(str, self.issues)))


class ValidationResourceError(OverflowError):
    """Validation exceeded a deterministic structural resource bound."""


def _variables(term): return {term.name} if isinstance(term, Variable) else set()
def _atom_vars(atom):
    if not isinstance(atom, Atom) or not isinstance(atom.terms, tuple):
        return set()
    return set().union(*(_variables(term) for term in atom.terms)) if atom.terms else set()


def _ground_atom_key(atom):
    if (not isinstance(atom, Atom) or not isinstance(atom.relation, str)
            or not isinstance(atom.terms, tuple)):
        return None
    values = tuple(term.value if isinstance(term, Constant) else None
                   for term in atom.terms)
    return canonical_json((atom.relation, values))


def _canonical_value(value):
    try:
        canonical_json(value)
    except (TypeError, ValueError):
        return False
    return True


def _context_values(context):
    """Return a checked Context mapping without trusting mutated internals."""
    if (not isinstance(context, Context)
            or not isinstance(context.values, tuple)
            or any(not isinstance(pair, tuple) or len(pair) != 2
                   or not isinstance(pair[0], str)
                   or not _canonical_value(pair[1])
                   for pair in context.values)):
        return None
    return dict(context.values)


def _typed_compatibility_binding(left, right, expected):
    """Require exact identity without inferring a witness literal's type."""
    if isinstance(left, Variable) and isinstance(right, Variable):
        return left.name == right.name
    if isinstance(left, Constant) and isinstance(right, Constant):
        return (left.value == right.value
                and left.type == expected and right.type == expected)
    return False


def _structural_issues(bundle: Bundle) -> tuple[ValidationIssue, ...]:
    """Check nested dataclass shapes before semantic passes dereference them."""
    issues = []

    def atom(value, path):
        if not isinstance(value, Atom):
            issues.append(ValidationIssue("atom-type", "expected atom", path))
            return
        if not isinstance(value.relation, str):
            issues.append(ValidationIssue(
                "atom-type", "atom relation must be a string", path))
        if not isinstance(value.negated, bool):
            issues.append(ValidationIssue(
                "atom-type", "atom negation flag must be boolean", path))
        if not isinstance(value.terms, tuple):
            issues.append(ValidationIssue(
                "atom-type", "atom terms must be a tuple", path))
        else:
            for index, term in enumerate(value.terms):
                if not isinstance(term, (Variable, Constant)):
                    issues.append(ValidationIssue(
                        "term-type", "term must be Variable or Constant",
                        f"{path}.terms[{index}]"))
                elif isinstance(term, Variable) and not isinstance(term.name, str):
                    issues.append(ValidationIssue(
                        "term-type", "variable name must be a string",
                        f"{path}.terms[{index}]"))
                elif (isinstance(term, Constant)
                      and (not _canonical_value(term.value)
                           or (term.type is not None
                               and not isinstance(term.type, TypeName)))):
                    issues.append(ValidationIssue(
                        "term-type", "constant value/type is malformed",
                        f"{path}.terms[{index}]"))

    collections = (
        ("relations", bundle.relations, RelationDecl, "relation-type"),
        ("facts", bundle.facts, Atom, "atom-type"),
        ("rules", bundle.rules, Rule, "rule-type"),
        ("claims", bundle.claims, Claim, "claim-type"),
        ("evidence", bundle.evidence, Evidence, "evidence-type"),
        ("mappings", bundle.mappings, EvidenceMapping, "mapping-type"),
        ("diagnostics", bundle.diagnostics, DiagnosticRule, "diagnostic-type"),
        ("outputs", bundle.outputs, OutputTemplate, "output-type"),
    )
    for name, values, expected, code in collections:
        if not isinstance(values, tuple):
            issues.append(ValidationIssue(
                code, f"{name} must be a tuple", name))
            continue
        for index, value in enumerate(values):
            if not isinstance(value, expected):
                issues.append(ValidationIssue(
                    code, f"expected {expected.__name__}", f"{name}[{index}]"))

    if (not isinstance(bundle.metadata, tuple)
            or any(not isinstance(pair, tuple) or len(pair) != 2
                   or not isinstance(pair[0], str)
                   or not _canonical_value(pair[1])
                   for pair in bundle.metadata)):
        issues.append(ValidationIssue(
            "metadata-type", "metadata must contain canonical string-keyed pairs",
            "metadata"))
    if not isinstance(bundle.schema_version, int) or isinstance(bundle.schema_version, bool):
        issues.append(ValidationIssue(
            "schema-version", "schema version must be an integer",
            "schema_version"))

    for index, relation in enumerate(bundle.relations if isinstance(bundle.relations, tuple) else ()):
        if not isinstance(relation, RelationDecl):
            continue
        path = f"relations[{index}]"
        if (not isinstance(relation.columns, tuple)
                or not all(isinstance(column, Column)
                           and isinstance(column.name, str)
                           and isinstance(column.type, TypeName)
                           and isinstance(column.context, bool)
                           for column in relation.columns)):
            issues.append(ValidationIssue(
                "column-type", "columns must be well-formed Column values", path))
        if (not isinstance(relation.name, str)
                or not isinstance(relation.modality, Modality)
                or not isinstance(relation.polarity, Polarity)
                or not isinstance(relation.binding, BindingTime)
                or not isinstance(relation.primitive, bool)
                or not isinstance(relation.finite, bool)
                or not isinstance(relation.nonempty, bool)
                or (relation.completes is not None
                    and not isinstance(relation.completes, str))):
            issues.append(ValidationIssue(
                "relation-type", "relation scalar fields are malformed", path))
        for field_name in ("producer_classes", "context_indices",
                           "compatibility_targets",
                           "compatibility_context_indices"):
            values = getattr(relation, field_name)
            if (not isinstance(values, tuple)
                    or not all(isinstance(item, str) for item in values)):
                issues.append(ValidationIssue(
                    "relation-type", f"{field_name} must be a tuple of strings",
                    path))
    for index, fact in enumerate(bundle.facts if isinstance(bundle.facts, tuple) else ()):
        atom(fact, f"facts[{index}]")
    for index, record in enumerate(bundle.evidence if isinstance(bundle.evidence, tuple) else ()):
        if not isinstance(record, Evidence):
            continue
        atom(record.atom, f"evidence[{index}].atom")
        if _context_values(record.context) is None:
            issues.append(ValidationIssue(
                "evidence-context", "evidence context must be a well-formed Context",
                f"evidence[{index}]"))
        if (not isinstance(record.id, str)
                or not isinstance(record.source, str)
                or not isinstance(record.kind, str)):
            issues.append(ValidationIssue(
                "evidence-type", "evidence scalar fields must be strings",
                f"evidence[{index}]"))
        if (not isinstance(record.depends_on, tuple)
                or not all(isinstance(item, str) for item in record.depends_on)):
            issues.append(ValidationIssue(
                "evidence-dependency", "dependencies must be a tuple of strings",
                f"evidence[{index}]"))
    for index, rule in enumerate(bundle.rules if isinstance(bundle.rules, tuple) else ()):
        if not isinstance(rule, Rule):
            continue
        atom(rule.head, f"rules[{index}].head")
        if not isinstance(rule.body, tuple):
            issues.append(ValidationIssue(
                "rule-body", "rule body must be a tuple", f"rules[{index}]"))
        else:
            for body_index, item in enumerate(rule.body):
                path = f"rules[{index}].body[{body_index}]"
                if isinstance(item, Atom):
                    atom(item, path)
                elif isinstance(item, Comparison):
                    if not isinstance(item.left, (Variable, Constant)):
                        issues.append(ValidationIssue(
                            "term-type", "comparison left must be a term", path))
                    if not isinstance(item.right, (Variable, Constant)):
                        issues.append(ValidationIssue(
                            "term-type", "comparison right must be a term", path))
                    if not isinstance(item.operator, str):
                        issues.append(ValidationIssue(
                            "operator", "comparison operator must be a string", path))
                else:
                    issues.append(ValidationIssue(
                        "atom-type", "expected atom or comparison", path))
        if rule.aggregation is not None and not isinstance(rule.aggregation, Aggregation):
            issues.append(ValidationIssue(
                "aggregation-type", "expected Aggregation", f"rules[{index}]"))
        elif isinstance(rule.aggregation, Aggregation):
            aggregation = rule.aggregation
            if (not isinstance(aggregation.group_by, tuple)
                    or not all(isinstance(item, str)
                               for item in aggregation.group_by)
                    or not all(value is None or isinstance(value, str)
                               for value in (
                                   aggregation.name, aggregation.relation,
                                   aggregation.value_variable,
                                   aggregation.operator, aggregation.domain,
                                   aggregation.closure_witness))):
                issues.append(ValidationIssue(
                    "aggregation-type", "malformed Aggregation", f"rules[{index}]"))
    for index, claim in enumerate(bundle.claims if isinstance(bundle.claims, tuple) else ()):
        if not isinstance(claim, Claim):
            continue
        path = f"claims[{index}]"
        if not isinstance(claim.relation, str):
            issues.append(ValidationIssue(
                "claim-type", "claim relation must be a string", path))
        if not isinstance(claim.terms, tuple):
            issues.append(ValidationIssue(
                "claim-type", "claim terms must be a tuple", path))
        else:
            for term_index, term in enumerate(claim.terms):
                if not isinstance(term, (Variable, Constant)):
                    issues.append(ValidationIssue(
                        "term-type", "term must be Variable or Constant",
                        f"{path}.terms[{term_index}]"))
        if _context_values(claim.context) is None:
            issues.append(ValidationIssue(
                "claim-context", "claim context must be a well-formed Context",
                f"claims[{index}]"))
        if (not isinstance(claim.id, str)
                or not isinstance(claim.quantifier, Quantifier)
                or (claim.domain is not None
                    and not isinstance(claim.domain, str))):
            issues.append(ValidationIssue(
                "claim-type", "claim id/quantifier/domain is malformed", path))
    for index, mapping in enumerate(bundle.mappings if isinstance(bundle.mappings, tuple) else ()):
        if not isinstance(mapping, EvidenceMapping):
            continue
        path = f"mappings[{index}]"
        if not all(isinstance(value, str) for value in (
                mapping.claim_relation, mapping.evidence_relation,
                mapping.claim_id)):
            issues.append(ValidationIssue(
                "mapping-type", "mapping relation/id fields must be strings", path))
        if (not isinstance(mapping.context_indices, tuple)
                or not all(isinstance(item, str)
                           for item in mapping.context_indices)
                or not isinstance(mapping.bindings, tuple)
                or any(not isinstance(pair, tuple) or len(pair) != 2
                       or not all(isinstance(item, str) for item in pair)
                       for pair in mapping.bindings)
                or not isinstance(mapping.required, bool)
                or not isinstance(mapping.allow_out_of_scope, bool)):
            issues.append(ValidationIssue(
                "mapping-type", "mapping indices/bindings/options are malformed", path))
    for index, diagnostic in enumerate(bundle.diagnostics if isinstance(bundle.diagnostics, tuple) else ()):
        if not isinstance(diagnostic, DiagnosticRule):
            continue
        if (not all(isinstance(value, str) for value in (
                diagnostic.trigger_relation, diagnostic.operational_status,
                diagnostic.message, diagnostic.claim_id))
                or not isinstance(diagnostic.when_missing, bool)
                or not isinstance(diagnostic.required, bool)
                or not isinstance(diagnostic.context_indices, tuple)
                or not all(isinstance(item, str)
                           for item in diagnostic.context_indices)):
            issues.append(ValidationIssue(
                "diagnostic-type", "diagnostic scalar/scope fields are malformed",
                f"diagnostics[{index}]"))
        pairs = diagnostic.predicate
        if (not isinstance(pairs, tuple)
                or any(not isinstance(pair, tuple) or len(pair) != 2
                       or not isinstance(pair[0], str)
                       or not _canonical_value(pair[1]) for pair in pairs)):
            issues.append(ValidationIssue(
                "diagnostic-predicate", "predicate must contain string-keyed pairs",
                f"diagnostics[{index}]"))
    for index, output in enumerate(bundle.outputs if isinstance(bundle.outputs, tuple) else ()):
        if not isinstance(output, OutputTemplate):
            continue
        path = f"outputs[{index}]"
        if (not isinstance(output.fields, tuple)
                or any(not isinstance(pair, tuple) or len(pair) != 2
                       or not isinstance(pair[0], str)
                       or not isinstance(pair[1], TemplateValue)
                       or not isinstance(pair[1].source, str)
                       or not isinstance(pair[1].column, str)
                       or not isinstance(pair[1].type, TypeName)
                       or not _canonical_value(pair[1].value)
                       or (pair[1].evidence_id is not None
                           and not isinstance(pair[1].evidence_id, str))
                       for pair in output.fields)):
            issues.append(ValidationIssue(
                "output-field", "fields must contain name/TemplateValue pairs", path))
        if (not isinstance(output.claim_id, str)
                or not all(value is None or isinstance(value, str)
                           for value in (output.evidence_id, output.relation,
                                         output.when_claim))
                or any(not isinstance(values, tuple)
                       or not all(isinstance(item, str) for item in values)
                       for values in (output.requires_all_evidence,
                                      output.requires_any_evidence,
                                      output.excludes_evidence))):
            issues.append(ValidationIssue(
                "output-type", "output scalar/trigger fields are malformed", path))

    return tuple(issues)


def validate_bundle(bundle: Bundle) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    policy = bundle.diagnostic_policy
    policy_fields = {
        "missing_premises": {"unresolved", "refuted", "invalid-input", "out-of-scope"},
        "inconsistent_premises": {"inconsistent-premises"},
        "out_of_scope": {"out-of-scope"},
        "forbidden_evidence": {"invalid-input", "out-of-scope"},
        "revocation": {"refutation", "stale"},
        "completeness": {"required", "unresolved"},
    }
    for field_name, allowed in policy_fields.items():
        value = getattr(policy, field_name, None)
        if not isinstance(value, str) or value not in allowed:
            issues.append(ValidationIssue("diagnostic-policy", f"invalid {field_name} policy value", f"diagnostic_policy.{field_name}"))
    structural = _structural_issues(bundle)
    if structural:
        return tuple((*issues, *structural))
    # Bundle construction enforces these top-level member types.  Keep the
    # filters here as defense in depth for callers that deliberately mutate a
    # frozen instance with ``object.__setattr__`` while testing the untrusted
    # validation boundary.  No second pass below may dereference an entry that
    # failed its structural type check.
    relation_records = tuple(
        relation for relation in bundle.relations
        if isinstance(relation, RelationDecl))
    relation_names = tuple(
        relation.name for relation in relation_records
        if isinstance(relation.name, str))
    if len(relation_names) != len(set(relation_names)):
        issues.append(ValidationIssue(
            "duplicate-relation", "relation names must be unique", "relations"))
    structurally_typed_relations = tuple(
        relation for relation in relation_records
        if isinstance(relation.name, str)
        and isinstance(relation.columns, tuple)
        and all(isinstance(column, Column) for column in relation.columns))
    relations = {relation.name: relation
                 for relation in structurally_typed_relations}
    for relation in bundle.relations:
        if isinstance(relation, RelationDecl):
            _validate_relation(relation, relations, issues)
        else:
            issues.append(ValidationIssue(
                "relation-type", "expected RelationDecl", "relations"))
    for i, fact in enumerate(bundle.facts):
        _validate_atom(fact, relations, issues, f"facts[{i}]", {}, fact_only=True)
        if isinstance(fact, Atom) and fact.negated: issues.append(ValidationIssue("negative-fact", "facts must be positive", f"facts[{i}]"))
    evidence_ids = set()
    for i, record in enumerate(bundle.evidence):
        path = f"evidence[{i}]"
        if not isinstance(record, Evidence):
            issues.append(ValidationIssue("evidence-type", "expected Evidence", path)); continue
        if not isinstance(record.id, str) or not record.id:
            issues.append(ValidationIssue(
                "evidence-id", "evidence id must be a non-empty string", path))
        else:
            if record.id in evidence_ids:
                issues.append(ValidationIssue(
                    "duplicate-evidence-id", record.id, path))
            evidence_ids.add(record.id)
        _validate_atom(record.atom, relations, issues, path + ".atom", {}, fact_only=True)
        if (not isinstance(record.atom, Atom)
                or not isinstance(record.atom.relation, str)
                or not isinstance(record.atom.terms, tuple)):
            continue
        if record.atom.negated: issues.append(ValidationIssue("negative-evidence", "evidence atoms must be positive", path))
        declared = (relations.get(record.atom.relation)
                    if isinstance(record.atom.relation, str) else None)
        context = _context_values(record.context)
        if context is None:
            issues.append(ValidationIssue(
                "evidence-context", "evidence context must be a well-formed Context", path))
            context = {}
        if declared and set(context) != set(declared.context_indices):
            issues.append(ValidationIssue("evidence-context", "evidence context must exactly match relation context indices", path))
        if not isinstance(record.source, str) or not record.source: issues.append(ValidationIssue("evidence-source", "evidence source must be a non-empty string", path))
        elif declared and declared.producer_classes:
            # Producer-class authority is enforced here, at evidence ingestion,
            # and nowhere else: every fact is already tied to an evidence record
            # (``fact-without-evidence``), so this is the single boundary.  The
            # producer class of a record is the first whitespace-delimited token
            # of ``Evidence.source`` (``"php target-cloud 1a2b3c"`` -> ``php``).  An
            # empty ``producer_classes`` tuple leaves the relation unconstrained.
            producer_class = record.source.split(" ", 1)[0]
            if producer_class not in declared.producer_classes:
                issues.append(ValidationIssue(
                    "evidence-producer",
                    f"{record.atom.relation!r} admits {declared.producer_classes}, got {record.source!r}",
                    path))
        if not isinstance(record.kind, str) or not record.kind: issues.append(ValidationIssue("evidence-kind", "evidence kind must be a non-empty string", path))
        if declared:
            names = [c.name for c in declared.columns]
            for name, value in context.items():
                if name in names:
                    index = names.index(name)
                    if (index >= len(record.atom.terms)
                            or not isinstance(record.atom.terms[index], Constant)
                            or record.atom.terms[index].value != value):
                        issues.append(ValidationIssue("evidence-context", f"context value does not equal atom column {name!r}", path))
    fact_keys = {key for fact in bundle.facts
                 if (key := _ground_atom_key(fact)) is not None}
    evidence_keys = {key for record in bundle.evidence
                     if isinstance(record, Evidence)
                     and (key := _ground_atom_key(record.atom)) is not None}
    for i, record in enumerate(bundle.evidence):
        key = (_ground_atom_key(record.atom)
               if isinstance(record, Evidence) else None)
        if key is not None and key not in fact_keys:
            issues.append(ValidationIssue(
                "evidence-without-fact", str(record.id), f"evidence[{i}]"))
    # Attribution is unconditional: a fact-bearing bundle with an empty
    # evidence tuple used to validate with zero issues, which bypassed
    # ``evidence-producer`` (and every other evidence check) entirely.
    for i, fact in enumerate(bundle.facts):
        key = _ground_atom_key(fact)
        if key is not None and key not in evidence_keys:
            issues.append(ValidationIssue("fact-without-evidence", "every fact requires an evidence record", f"facts[{i}]"))
    for i, rule in enumerate(bundle.rules):
        if isinstance(rule, Rule): _validate_rule(rule, relations, issues, f"rules[{i}]")
        else: issues.append(ValidationIssue("rule-type", "expected Rule", f"rules[{i}]"))
    for i, claim in enumerate(bundle.claims):
        if isinstance(claim, Claim): _validate_claim(claim, relations, issues, f"claims[{i}]")
        else: issues.append(ValidationIssue("claim-type", "expected Claim", f"claims[{i}]"))
    seen_claim_ids = set()
    requires_claim_ids = bool(bundle.evidence or bundle.mappings or bundle.diagnostics)
    for i, claim in enumerate(bundle.claims):
        if requires_claim_ids and isinstance(claim, Claim) and (not isinstance(claim.id, str) or not claim.id):
            issues.append(ValidationIssue("claim-id", "claim id is mandatory", f"claims[{i}]"))
        if isinstance(claim, Claim) and claim.id:
            if claim.id in seen_claim_ids: issues.append(ValidationIssue("duplicate-claim-id", claim.id, f"claims[{i}]"))
            seen_claim_ids.add(claim.id)
    claim_by_id = {claim.id: claim for claim in bundle.claims
                   if isinstance(claim, Claim) and claim.id}
    if claim_by_id:
        for i, mapping in enumerate(bundle.mappings):
            if (isinstance(mapping, EvidenceMapping)
                    and (not mapping.claim_id
                         or mapping.claim_id not in seen_claim_ids)):
                issues.append(ValidationIssue("mapping-claim-id", "mapping must name an existing claim id", f"mappings[{i}]"))
        for i, diagnostic in enumerate(bundle.diagnostics):
            if (isinstance(diagnostic, DiagnosticRule)
                    and (not diagnostic.claim_id
                         or diagnostic.claim_id not in seen_claim_ids)):
                issues.append(ValidationIssue("diagnostic-claim-id", "diagnostic must name an existing claim id", f"diagnostics[{i}]"))
    join_eligible_mappings = []
    for i, mapping in enumerate(bundle.mappings):
        path = f"mappings[{i}]"
        if not isinstance(mapping, EvidenceMapping):
            issues.append(ValidationIssue("mapping-type", "expected EvidenceMapping", path)); continue
        if not isinstance(mapping.effect, EvidenceEffect): issues.append(ValidationIssue("mapping-effect", "unsupported evidence effect", path))
        if mapping.claim_relation not in relations: issues.append(ValidationIssue("mapping-claim", mapping.claim_relation, path))
        if mapping.evidence_relation not in relations: issues.append(ValidationIssue("mapping-evidence", mapping.evidence_relation, path))
        named_claim = claim_by_id.get(mapping.claim_id)
        if named_claim is not None and mapping.claim_relation != named_claim.relation:
            issues.append(ValidationIssue(
                "mapping-claim-relation",
                f"mapping relation {mapping.claim_relation!r} does not match claim {mapping.claim_id!r} relation {named_claim.relation!r}",
                path))
            # Never use a decoy declaration to authorize projection coverage or
            # mixed-binding compatibility for the claim selected by id.
            continue
        join_eligible_mappings.append(mapping)
        for context_name in mapping.context_indices:
            claim_decl = relations.get(mapping.claim_relation)
            evidence_decl = relations.get(mapping.evidence_relation)
            if not claim_decl or context_name not in claim_decl.context_indices: issues.append(ValidationIssue("mapping-context", context_name, path))
            if not evidence_decl or context_name not in evidence_decl.context_indices: issues.append(ValidationIssue("mapping-context", context_name, path))
        claim_decl = relations.get(mapping.claim_relation); evidence_decl = relations.get(mapping.evidence_relation)
        if claim_decl and evidence_decl:
            c_names = {c.name for c in claim_decl.columns}; e_names = {c.name for c in evidence_decl.columns}
            c_types = {c.name: c.type for c in claim_decl.columns}
            e_types = {c.name: c.type for c in evidence_decl.columns}
            for left, right in mapping.bindings:
                if left not in c_names or right not in e_names:
                    issues.append(ValidationIssue("mapping-binding", f"unknown projection {left!r}->{right!r}", path))
                elif c_types[left] != e_types[right]:
                    issues.append(ValidationIssue("mapping-binding", f"projection {left!r}->{right!r} changes type", path))
            if len({left for left, _ in mapping.bindings}) != len(mapping.bindings) or len({right for _, right in mapping.bindings}) != len(mapping.bindings):
                issues.append(ValidationIssue("mapping-binding", "duplicate projection binding", path))
            covered = {left for left, _ in mapping.bindings}
            if not set(mapping.context_indices).issubset(covered):
                issues.append(ValidationIssue("mapping-coverage", "context mapping is not covered by bindings", path))
            if mapping.required or mapping.allow_out_of_scope:
                issues.append(ValidationIssue(
                    "mapping-option",
                    "required/allow_out_of_scope mappings are reserved until their semantics are implemented",
                    path))
            if isinstance(mapping.effect, EvidenceEffect) and mapping.effect.value in {"support", "refutation"}:
                omitted = (c_names & e_names & _CAUSAL_IDENTITY_COLUMNS) - covered
                if omitted:
                    issues.append(ValidationIssue(
                        "mapping-coverage",
                        "support/refutation mapping omits causal identities without aggregation authority: "
                        + ", ".join(sorted(omitted)), path))
    _validate_mapping_context_joins(join_eligible_mappings, relations, issues)
    allowed_status = {"complete", "invalid-input", "inconsistent-premises", "resource-exhausted", "unsupported-construct", "stale", "out-of-scope"}
    for i, diagnostic in enumerate(bundle.diagnostics):
        path = f"diagnostics[{i}]"
        if not isinstance(diagnostic, DiagnosticRule): issues.append(ValidationIssue("diagnostic-type", "expected DiagnosticRule", path)); continue
        if not isinstance(diagnostic.effect, EvidenceEffect): issues.append(ValidationIssue("diagnostic-effect", "unsupported diagnostic effect", path))
        predicate_pairs = diagnostic.predicate
        if (not isinstance(predicate_pairs, tuple)
                or any(not isinstance(pair, tuple) or len(pair) != 2
                       or not isinstance(pair[0], str)
                       for pair in predicate_pairs)):
            issues.append(ValidationIssue(
                "diagnostic-predicate",
                "predicate must contain string-keyed pairs", path))
            predicate = {}
        else:
            predicate = dict(predicate_pairs)
        if predicate:
            operator = predicate.get("operator")
            relation = relations.get(diagnostic.trigger_relation)
            column = next((c for c in relation.columns if c.name == predicate.get("column")), None) if relation else None
            if operator not in {"=", "!=", "in", "not-in", "exists"}:
                issues.append(ValidationIssue("diagnostic-predicate", "unsupported diagnostic predicate operator", path))
            if column is None: issues.append(ValidationIssue("diagnostic-predicate", "predicate column is not declared by trigger relation", path))
            value = predicate.get("value")
            values = value if operator in {"in", "not-in"} and isinstance(value, (list, tuple)) else ([] if operator in {"in", "not-in"} else [value])
            if operator in {"in", "not-in"} and not isinstance(value, (list, tuple)): issues.append(ValidationIssue("diagnostic-predicate", "set predicate value must be a list", path))
            if operator in {"=", "!=", "in", "not-in"} and column:
                for item in values:
                    inferred = TypeName.BOOLEAN if isinstance(item, bool) else TypeName.INTEGER if isinstance(item, int) else TypeName.SYMBOL if isinstance(item, str) else None
                    if inferred != column.type: issues.append(ValidationIssue("diagnostic-predicate", "predicate value type does not match column", path))
        if diagnostic.trigger_relation not in relations: issues.append(ValidationIssue("diagnostic-trigger", diagnostic.trigger_relation, path))
        if diagnostic.operational_status not in allowed_status: issues.append(ValidationIssue("diagnostic-status", diagnostic.operational_status, path))
        relation = relations.get(diagnostic.trigger_relation)
        if relation and not set(diagnostic.context_indices).issubset(set(relation.context_indices)):
            issues.append(ValidationIssue("diagnostic-context", "diagnostic context is not declared by trigger relation", path))
    known_ids = set(evidence_ids)
    for i, record in enumerate(bundle.evidence):
        if not isinstance(record, Evidence):
            continue
        for dependency in record.depends_on:
            if not isinstance(dependency, str) or not dependency: issues.append(ValidationIssue("evidence-dependency", "dependency ids must be non-empty strings", f"evidence[{i}]"))
            elif dependency not in known_ids and not dependency.startswith("external:"):
                issues.append(ValidationIssue("unknown-evidence-dependency", dependency, f"evidence[{i}]"))
    claim_ids = {claim.id for claim in bundle.claims if isinstance(claim, Claim) and claim.id}
    evidence_by_id = {record.id: record for record in bundle.evidence if isinstance(record, Evidence)}
    for i, output in enumerate(bundle.outputs):
        path = f"outputs[{i}]"
        if not isinstance(output, OutputTemplate): issues.append(ValidationIssue("output-type", "expected OutputTemplate", path)); continue
        if not isinstance(output.kind, OutputKind): issues.append(ValidationIssue("output-kind", "unknown output kind", path)); continue
        if output.claim_id not in claim_ids: issues.append(ValidationIssue("output-claim", output.claim_id, path))
        explicit_triggers = (*output.requires_all_evidence, *output.requires_any_evidence, *output.excludes_evidence)
        all_triggers = (*explicit_triggers, *((output.evidence_id,) if output.evidence_id else ()))
        if len(set(explicit_triggers)) != len(explicit_triggers): issues.append(ValidationIssue("output-trigger", "duplicate evidence trigger", path))
        # The output's payload evidence_id is the value being rendered; only
        # explicit trigger declarations are causal prerequisites.
        for evidence_id in explicit_triggers:
            if not isinstance(evidence_id, str) or not evidence_id or evidence_id not in evidence_by_id:
                issues.append(ValidationIssue("output-trigger", "trigger evidence is not declared", path))
        if output.requires_any_evidence is not None and not isinstance(output.requires_any_evidence, tuple): issues.append(ValidationIssue("output-trigger", "any-evidence trigger must be a sequence", path))
        if output.when_claim not in {None, "derived", "underived", "supported", "refuted", "unresolved", "conflicting", "always"}:
            issues.append(ValidationIssue("output-trigger", "unknown claim trigger", path))
        if output.when_claim == "always" and output.kind in {OutputKind.DISCREPANCY, OutputKind.MISSING_PREMISE}:
            issues.append(ValidationIssue("output-trigger", "diagnostic output cannot use always claim trigger", path))
        if output.relation and output.kind != OutputKind.MISSING_PREMISE:
            issues.append(ValidationIssue("output-relation", "relation is only valid for missing premise outputs", path))
        if output.kind in {OutputKind.DISCREPANCY, OutputKind.MISSING_PREMISE} and not (output.requires_all_evidence or output.requires_any_evidence or output.when_claim or output.relation):
            issues.append(ValidationIssue("output-trigger", "diagnostic output has no trigger", path))
        if output.kind in {OutputKind.OBSERVED, OutputKind.FORBIDDEN} and (not output.evidence_id or output.evidence_id not in evidence_by_id):
            issues.append(ValidationIssue("output-evidence", "observed/forbidden output must reference evidence", path))
        if output.kind == OutputKind.MISSING_PREMISE and not output.relation: issues.append(ValidationIssue("output-relation", "missing premise output needs a relation", path))
        if output.kind == OutputKind.MISSING_PREMISE and output.relation not in relations and output.relation not in DIAGNOSTIC_VOCABULARY:
            issues.append(ValidationIssue("output-relation", "unknown diagnostic vocabulary relation", path))
        causal_relations = {
            mapping.evidence_relation for mapping in bundle.mappings
            if isinstance(mapping, EvidenceMapping)
            and mapping.claim_id == output.claim_id}
        causal_relations.update(
            diagnostic.trigger_relation for diagnostic in bundle.diagnostics
            if isinstance(diagnostic, DiagnosticRule)
            and diagnostic.claim_id == output.claim_id)
        claim = next((claim for claim in bundle.claims
                      if isinstance(claim, Claim)
                      and claim.id == output.claim_id), None)
        if claim:
            causal_relations.update(
                atom.relation for rule in bundle.rules
                if isinstance(rule, Rule) and isinstance(rule.head, Atom)
                and rule.head.relation == claim.relation
                and isinstance(rule.body, tuple)
                for atom in rule.body if isinstance(atom, Atom))
        for evidence_id in explicit_triggers:
            evidence = evidence_by_id.get(evidence_id)
            if evidence and evidence.atom.relation not in causal_relations:
                issues.append(ValidationIssue("output-trigger", "trigger evidence is not causally connected to claim", path))
        for field in output.fields:
            if (not isinstance(field, tuple) or len(field) != 2
                    or not isinstance(field[1], TemplateValue)):
                issues.append(ValidationIssue(
                    "output-field", "fields must contain name/TemplateValue pairs", path))
                continue
            name, value = field
            if not isinstance(name, str) or not name: issues.append(ValidationIssue("output-field", "field names must be non-empty strings", path))
            if value.source not in {"constant", "claim", "evidence"}: issues.append(ValidationIssue("output-source", "unknown template value source", path)); continue
            if value.source == "constant":
                _validate_term(Constant(value.value, value.type), value.type, {}, issues, f"{path}.fields.{name}")
                continue
            relation = None
            if value.source == "claim":
                claim = next((c for c in bundle.claims if isinstance(c, Claim) and c.id == output.claim_id), None)
                relation = relations.get(claim.relation) if claim else None
            elif value.source == "evidence":
                evidence = evidence_by_id.get(value.evidence_id or output.evidence_id or "")
                relation = relations.get(evidence.atom.relation) if evidence else None
            if value.source != "constant" and not relation: issues.append(ValidationIssue("output-reference", "template relation reference is unavailable", path)); continue
            if value.source != "constant":
                if value.column == "id" and value.type != TypeName.SYMBOL: issues.append(ValidationIssue("output-type", "evidence id references are symbols", path))
                elif value.column not in {column.name for column in relation.columns}: issues.append(ValidationIssue("output-column", value.column, path))
                elif value.type != next(column.type for column in relation.columns if column.name == value.column): issues.append(ValidationIssue("output-type", "template reference type mismatch", path))
    output_keys = []
    for output in bundle.outputs:
        if not isinstance(output, OutputTemplate):
            continue
        output_keys.append(repr((output.kind, output.claim_id, output.evidence_id, output.relation, output.fields, output.requires_all_evidence, output.requires_any_evidence, output.excludes_evidence, output.when_claim)))
    if len(output_keys) != len(set(output_keys)):
        issues.append(ValidationIssue("output-duplicate", "duplicate canonical output template", "outputs"))
    _validate_recursion(bundle, relations, issues)
    return tuple(issues)


def assert_valid(bundle: Bundle) -> Bundle:
    issues = validate_bundle(bundle)
    if issues: raise ValidationError(issues)
    return bundle


def validate(bundle: Bundle): return validate_bundle(bundle)


def _validate_relation(r, relations, issues):
    if (not isinstance(r.name, str) or not r.name
            or not r.name.replace("_", "a").isalnum()
            or r.name[0].isdigit()):
        issues.append(ValidationIssue("name", "relation name must be an identifier", f"relations.{r.name}"))
    if (not isinstance(r.columns, tuple)
            or not all(isinstance(c, Column) for c in r.columns)):
        issues.append(ValidationIssue("column-type", "columns must be Column values", f"relations.{r.name}")); return
    names = [c.name for c in r.columns]
    if len(names) != len(set(names)): issues.append(ValidationIssue("duplicate-column", "column names must be unique", f"relations.{r.name}"))
    if len(r.context_indices) != len(set(r.context_indices)): issues.append(ValidationIssue("duplicate-context", "context positions must be unique", f"relations.{r.name}"))
    for index in r.context_indices:
        if index not in names: issues.append(ValidationIssue("context-index", f"unknown context column {index!r}", f"relations.{r.name}"))
        elif not r.columns[names.index(index)].context: issues.append(ValidationIssue("context-index", f"column {index!r} is not marked context", f"relations.{r.name}"))
    marked_context = {column.name for column in r.columns if column.context}
    if (r.context_indices or r.binding.value == "static") and marked_context != set(r.context_indices):
        # Runtime declarations predating explicit context_indices retain their
        # legacy column flags.  New scoped declarations and all static inputs
        # must make the two representations agree exactly.
        issues.append(ValidationIssue("context-index", "context flags must exactly equal declared context indices", f"relations.{r.name}"))
    if r.binding.value == "static" and r.modality.value in {"observation", "assumption", "completeness"}:
        index_column = next((column for column in r.columns if column.name == "index"), None)
        fingerprint = (r.name, tuple((column.name, column.type, column.context)
                                     for column in r.columns), r.context_indices)
        explicitly_context_free = fingerprint in _CONTEXT_FREE_STATIC_DECLARATIONS
        if index_column is None and not explicitly_context_free:
            issues.append(ValidationIssue(
                "static-context",
                "static observations require an index digest unless they match a frozen context-free declaration",
                f"relations.{r.name}",
            ))
        elif index_column is not None and (r.context_indices != ("index",)
                                           or index_column.type != TypeName.DIGEST
                                           or not index_column.context):
            issues.append(ValidationIssue("static-context", "indexed static observations use one digest context column named 'index'", f"relations.{r.name}"))
    if r.modality.value == "completeness":
        if not r.completes: issues.append(ValidationIssue("completeness-target", "completeness relation must name its target relation", f"relations.{r.name}"))
        elif r.completes not in relations: issues.append(ValidationIssue("completeness-target", f"unknown target {r.completes!r}", f"relations.{r.name}"))
        elif r.binding != relations[r.completes].binding:
            issues.append(ValidationIssue(
                "completeness-binding",
                "completeness relation binding must match its target relation",
                f"relations.{r.name}"))
    elif r.completes is not None: issues.append(ValidationIssue("completeness-target", "only completeness relations may name a target", f"relations.{r.name}"))
    if r.modality.value == "compatibility":
        if len(r.compatibility_targets) < 2: issues.append(ValidationIssue("compatibility-target", "compatibility relation must name at least two target relations", f"relations.{r.name}"))
        for target in r.compatibility_targets:
            if target not in relations: issues.append(ValidationIssue("compatibility-target", f"unknown target {target!r}", f"relations.{r.name}"))
        for index in r.compatibility_context_indices:
            if index not in names:
                issues.append(ValidationIssue("compatibility-context", f"payload position {index!r} is not a declared column", f"relations.{r.name}"))
        if len(r.compatibility_context_indices) != len(set(r.compatibility_context_indices)):
            issues.append(ValidationIssue("compatibility-context", "compatibility payload positions must be unique", f"relations.{r.name}"))
        if len(r.compatibility_targets) != len(set(r.compatibility_targets)): issues.append(ValidationIssue("compatibility-target", "compatibility targets must be unique", f"relations.{r.name}"))
    elif r.compatibility_targets or r.compatibility_context_indices:
        issues.append(ValidationIssue("compatibility-target", "only compatibility relations may declare targets/positions", f"relations.{r.name}"))


def _python_type(value):
    if isinstance(value, bool): return TypeName.BOOLEAN
    if isinstance(value, int): return TypeName.INTEGER
    if isinstance(value, float): return None
    if isinstance(value, str): return TypeName.SYMBOL
    if isinstance(value, (tuple, list, Mapping)): return TypeName.JSON_METADATA_ONLY
    return None


def _validate_term(term, expected, env, issues, path, fact_only=False):
    if isinstance(term, Variable):
        if fact_only: issues.append(ValidationIssue("fact-variable", "facts must contain constants", path)); return
        old = env.setdefault(term.name, expected)
        if old != expected: issues.append(ValidationIssue("type-mismatch", f"variable {term.name!r} has incompatible types", path))
        return
    if not isinstance(term, Constant):
        issues.append(ValidationIssue("term-type", "term must be Variable or Constant", path)); return
    inferred = _python_type(term.value)
    declared = term.type
    actual = declared or inferred
    declared_ok = declared is None or inferred == declared or (declared == TypeName.UNSIGNED and inferred == TypeName.INTEGER and isinstance(term.value, int) and term.value >= 0) or (declared == TypeName.TIMESTAMP and inferred == TypeName.INTEGER and isinstance(term.value, int) and term.value >= 0) or (declared == TypeName.DIGEST and inferred == TypeName.SYMBOL) or (declared == TypeName.JSON_METADATA_ONLY and inferred == TypeName.JSON_METADATA_ONLY)
    if expected == TypeName.UNSIGNED and isinstance(term.value, int) and term.value < 0: declared_ok = False
    compatible = actual == expected or (expected == TypeName.UNSIGNED and inferred == TypeName.INTEGER and isinstance(term.value, int) and term.value >= 0) or (expected == TypeName.TIMESTAMP and inferred == TypeName.INTEGER and isinstance(term.value, int) and term.value >= 0) or (expected == TypeName.DIGEST and inferred == TypeName.SYMBOL)
    if actual is None or not declared_ok or not compatible:
        issues.append(ValidationIssue("type-mismatch", f"expected {expected.value}, got {getattr(actual, 'value', actual)}", path))


def _validate_atom(atom, relations, issues, path, env, fact_only=False):
    if not isinstance(atom, Atom): issues.append(ValidationIssue("atom-type", "expected atom", path)); return
    if not isinstance(atom.relation, str):
        issues.append(ValidationIssue(
            "atom-type", "atom relation must be a string", path)); return
    if not isinstance(atom.terms, tuple):
        issues.append(ValidationIssue(
            "atom-type", "atom terms must be a tuple", path)); return
    relation = relations.get(atom.relation)
    if relation is None: issues.append(ValidationIssue("unknown-relation", atom.relation, path)); return
    if fact_only and not relation.primitive:
        issues.append(ValidationIssue("nonprimitive-fact", "facts and evidence may target only primitive relations", path))
    if fact_only and relation.modality.value == "claim":
        issues.append(ValidationIssue(
            "producer-authored-claim",
            "facts and evidence report premises; claim relations require a reviewed rule or mapping",
            path))
    if len(atom.terms) != relation.arity: issues.append(ValidationIssue("arity", f"expected {relation.arity}, got {len(atom.terms)}", path)); return
    for i, (term, column) in enumerate(zip(atom.terms, relation.columns)):
        _validate_term(term, column.type, env, issues, f"{path}.terms[{i}]", fact_only)


def _validate_rule(rule, relations, issues, path):
    env = {}
    if not isinstance(rule.body, tuple):
        issues.append(ValidationIssue(
            "rule-body", "rule body must be a tuple", path))
        body = ()
    else:
        body = rule.body
    if not any(isinstance(item, Atom) and not item.negated for item in body):
        issues.append(ValidationIssue(
            "evidence-free-rule",
            "rules require at least one positive relational premise",
            path))
    for j, atom in enumerate(body):
        if isinstance(atom, Comparison):
            if atom.operator not in {"=", "!=", "<", "<=", ">", ">="}: issues.append(ValidationIssue("operator", atom.operator, f"{path}.body[{j}]"))
            left_type = env.get(atom.left.name) if isinstance(atom.left, Variable) else (atom.left.type or _python_type(atom.left.value)) if isinstance(atom.left, Constant) else None
            right_type = env.get(atom.right.name) if isinstance(atom.right, Variable) else (atom.right.type or _python_type(atom.right.value)) if isinstance(atom.right, Constant) else None
            if left_type is None or right_type is None: issues.append(ValidationIssue("unsafe-comparison", "comparison variables must be positively bound", f"{path}.body[{j}]"))
            elif left_type != right_type: issues.append(ValidationIssue("type-mismatch", "comparison operands must have equal types", f"{path}.body[{j}]"))
            continue
        if not isinstance(atom, Atom):
            _validate_atom(atom, relations, issues, f"{path}.body[{j}]", env)
            continue
        before = set(env)
        target_env = dict(env) if atom.negated else env
        _validate_atom(atom, relations, issues, f"{path}.body[{j}]", target_env)
        if atom.negated and not _atom_vars(atom).issubset(before):
            issues.append(ValidationIssue("unsafe-negation", "negation variables must be positively bound", f"{path}.body[{j}]"))
    bound = set(env)
    _validate_atom(rule.head, relations, issues, f"{path}.head", dict(env))
    if (isinstance(rule.head, Atom) and isinstance(rule.head.terms, tuple)
            and not _atom_vars(rule.head).issubset(bound)):
        issues.append(ValidationIssue("unsafe-variable", "head variables must be positively bound", path))
    _validate_context_joins(rule, relations, issues, path)
    for neg in (a for a in body if isinstance(a, Atom) and a.negated):
        _validate_completeness(neg, rule, relations, issues, path)
    if rule.aggregation:
        if isinstance(rule.aggregation, Aggregation): _validate_aggregation(rule.aggregation, rule, relations, issues, path)
        else: issues.append(ValidationIssue("aggregation-type", "expected Aggregation", path))


def _validate_completeness(negated, rule, relations, issues, path):
    target = negated.relation
    matches = [a for a in rule.body if isinstance(a, Atom) and not a.negated and relations.get(a.relation) and relations[a.relation].modality.value == "completeness" and relations[a.relation].completes == target]
    target_decl = relations.get(target)
    for witness in matches:
        witness_decl = relations[witness.relation]
        if target_decl and witness_decl.context_indices != target_decl.context_indices:
            continue
        ok = True
        target_names = [c.name for c in target_decl.columns] if target_decl else []
        witness_names = [c.name for c in witness_decl.columns]
        # Every shared scope column (not only context columns) must be bound to
        # the same term.  A path witness for one document cannot close another.
        for name in set(target_names) & set(witness_names):
            ti = target_names.index(name); wi = witness_names.index(name)
            if repr(negated.terms[ti]) != repr(witness.terms[wi]): ok = False
        if ok: return
    issues.append(ValidationIssue("missing-completeness", f"negation of {target!r} needs an exact scoped completeness witness", path))


def _validate_mapping_context_joins(mappings, relations, issues):
    """Require an index/run witness inside each mixed support conjunction.

    Support mappings for one claim are AND premises in both kernels.  Checking
    each projection independently would let static and runtime observations
    manufacture a claim without proving that the index describes that run.
    Observation-only mappings deliberately do not authorize the conjunction.
    """
    grouped = {}
    for index, mapping in enumerate(mappings):
        if (not isinstance(mapping, EvidenceMapping)
                or not isinstance(mapping.effect, EvidenceEffect)
                or mapping.effect.value != "support"):
            continue
        declaration = relations.get(mapping.evidence_relation)
        if declaration is not None:
            grouped.setdefault(mapping.claim_id, []).append((index, mapping, declaration))

    for claim_id, entries in grouped.items():
        ordinary = [entry for entry in entries
                    if entry[2].modality.value != "compatibility"]
        static_entries = [entry for entry in ordinary
                          if entry[2].binding.value == "static"]
        runtime_entries = [entry for entry in ordinary
                           if entry[2].binding.value == "runtime"]
        witnesses = [entry for entry in entries
                     if entry[2].modality.value == "compatibility"]
        for static_index, static_mapping, static_decl in static_entries:
            static_columns = {column.name: column for column in static_decl.columns}
            static_claim_index = next(
                (left for left, right in static_mapping.bindings if right == "index"), None)
            for runtime_index, runtime_mapping, runtime_decl in runtime_entries:
                runtime_columns = {column.name: column for column in runtime_decl.columns}
                runtime_claim_run = next(
                    (left for left, right in runtime_mapping.bindings if right == "run"), None)
                path = f"mappings[{static_index}],mappings[{runtime_index}]"
                if ("index" not in static_columns
                        or static_columns["index"].type != TypeName.DIGEST
                        or static_claim_index is None):
                    issues.append(ValidationIssue(
                        "mixed-binding-join",
                        "mixed support mappings require a static digest index bound through the claim",
                        path))
                    continue
                if "run" not in runtime_columns or runtime_claim_run is None:
                    issues.append(ValidationIssue(
                        "mixed-binding-join",
                        "mixed support mappings require a runtime run bound through the claim",
                        path))
                    continue

                found = False
                for _, witness_mapping, witness_decl in witnesses:
                    if not {static_decl.name, runtime_decl.name}.issubset(
                            set(witness_decl.compatibility_targets)):
                        continue
                    witness_columns = {column.name: column
                                       for column in witness_decl.columns}
                    if (not {"index", "run"}.issubset(witness_columns)
                            or not {"index", "run"}.issubset(
                                set(witness_decl.compatibility_context_indices))):
                        continue
                    if (witness_columns["index"].type
                            != static_columns["index"].type
                            or witness_columns["run"].type
                            != runtime_columns["run"].type):
                        continue
                    witness_bindings = {right: left
                                        for left, right in witness_mapping.bindings}
                    if (witness_bindings.get("index") == static_claim_index
                            and witness_bindings.get("run") == runtime_claim_run):
                        found = True
                        break
                if not found:
                    issues.append(ValidationIssue(
                        "mixed-binding-join",
                        f"claim {claim_id!r} mixed support mappings require an exact index/run compatibility support premise",
                        path))


def _validate_context_joins(rule, relations, issues, path):
    # Negation reads a relation just as surely as a positive atom does.  Keep
    # compatibility witnesses positive, but include negated ordinary inputs in
    # the mixed-binding trust boundary.  A completeness atom inherits the
    # binding and compatibility identity of the relation it closes; a producer
    # cannot relabel static closure as runtime to evade the check.
    body = rule.body if isinstance(rule.body, tuple) else ()
    atoms = [a for a in body
             if isinstance(a, Atom) and isinstance(a.relation, str)
             and isinstance(a.terms, tuple) and a.relation in relations
             and len(a.terms) == relations[a.relation].arity]
    compatibility = [(a, relations[a.relation]) for a in atoms
                     if not a.negated
                     and relations[a.relation].modality.value == "compatibility"]
    ordinary = []
    for atom in atoms:
        declaration = relations[atom.relation]
        if declaration.modality.value == "compatibility":
            continue
        effective = (relations.get(declaration.completes)
                     if declaration.modality.value == "completeness"
                     else declaration)
        effective = effective or declaration
        ordinary.append((atom, declaration, effective))
    static_atoms = [entry for entry in ordinary
                    if entry[2].binding.value == "static"]
    runtime_atoms = [entry for entry in ordinary
                     if entry[2].binding.value == "runtime"]
    for static_atom, static_decl, static_effective in static_atoms:
        static_names = [c.name for c in static_decl.columns]
        if "index" not in static_names:
            # A context-free static relation (the frozen source_tree_observed)
            # may join indexed static relations freely; only a body that also
            # reads runtime evidence needs the index/run witness, and then the
            # context-free atom cannot supply the index side of it.
            if not runtime_atoms:
                continue
            issues.append(ValidationIssue(
                "mixed-binding-join",
                "context-free static relations cannot join runtime evidence without an index/run witness",
                path))
            return
        index_term = static_atom.terms[static_names.index("index")]
        static_effective_columns = {
            column.name: column for column in static_effective.columns}
        for runtime_atom, runtime_decl, runtime_effective in runtime_atoms:
            runtime_names = [c.name for c in runtime_decl.columns]
            if "run" not in runtime_names:
                issues.append(ValidationIssue("mixed-binding-join", "indexed static/runtime joins require a runtime run column", path))
                return
            run_term = runtime_atom.terms[runtime_names.index("run")]
            runtime_effective_columns = {
                column.name: column for column in runtime_effective.columns}
            target_index = static_effective_columns.get("index")
            target_run = runtime_effective_columns.get("run")
            found = False
            for witness, witness_decl in compatibility:
                if not {static_effective.name, runtime_effective.name}.issubset(
                        witness_decl.compatibility_targets):
                    continue
                witness_names = [c.name for c in witness_decl.columns]
                witness_columns = {column.name: column
                                   for column in witness_decl.columns}
                if (target_index is None or target_run is None
                        or "index" not in witness_names or "run" not in witness_names
                        or not {"index", "run"}.issubset(
                            set(witness_decl.compatibility_context_indices))):
                    continue
                witness_index = witness.terms[witness_names.index("index")]
                witness_run = witness.terms[witness_names.index("run")]
                if (witness_columns["index"].type == target_index.type
                        and witness_columns["run"].type == target_run.type
                        and _typed_compatibility_binding(
                            index_term, witness_index, target_index.type)
                        and _typed_compatibility_binding(
                            run_term, witness_run, target_run.type)):
                    found = True; break
            if not found:
                issues.append(ValidationIssue("mixed-binding-join", "static/runtime joins require an exact typed index/run compatibility witness", path))
                return
    context_bindings = []
    for atom in atoms:
        decl = relations[atom.relation]; names = [c.name for c in decl.columns]
        context_bindings.append(
            (atom, decl,
             {name: atom.terms[names.index(name)]
              for name in decl.context_indices},
             {column.name: column for column in decl.columns}))
    for i, (left_atom, _, left, left_columns) in enumerate(context_bindings):
        for right_atom, _, right, right_columns in context_bindings[i + 1:]:
            differing = {key for key in left.keys() & right.keys()
                         if repr(left[key]) != repr(right[key])}

            def witness_matches(atom, decl):
                if not {left_atom.relation, right_atom.relation}.issubset(
                        set(decl.compatibility_targets)):
                    return False

                # Schema v1 has no declaration that associates arbitrary
                # witness payload positions with named target dimensions.
                # Preserve the unambiguous one-dimension form only: the two
                # declared positions are, in order, the left and right values
                # for the sole differing context key.  Scanning all payload
                # columns for every key let one (a,b) pair authorize both
                # tenant and run when their types/values happened to coincide.
                if len(differing) != 1:
                    return False
                positions = decl.compatibility_context_indices
                if len(positions) != 2:
                    return False
                names = [column.name for column in decl.columns]
                if any(position not in names for position in positions):
                    return False
                key = next(iter(differing))
                left_position, right_position = (
                    names.index(position) for position in positions)
                left_witness = atom.terms[left_position]
                right_witness = atom.terms[right_position]
                left_type = left_columns[key].type
                right_type = right_columns[key].type
                return (
                    decl.columns[left_position].type == left_type
                    and decl.columns[right_position].type == right_type
                    and _typed_compatibility_binding(
                        left[key], left_witness, left_type)
                    and _typed_compatibility_binding(
                        right[key], right_witness, right_type))

            witnessed = any(witness_matches(atom, decl)
                              for atom, decl in compatibility)
            if differing and not witnessed:
                issues.append(ValidationIssue("missing-compatibility", "cross-context joins require an explicit typed compatibility witness", path)); return


def _validate_aggregation(a, rule, relations, issues, path):
    source = relations.get(a.relation); domain = relations.get(a.domain) if a.domain else None; closure = relations.get(a.closure_witness) if a.closure_witness else None
    if a.operator not in {"count", "sum", "min", "max", "any", "all"}: issues.append(ValidationIssue("aggregation", f"unsupported operator {a.operator!r}", path))
    if source is None: issues.append(ValidationIssue("aggregation-source", "aggregation source relation is unknown", path))
    else:
        names = {c.name: c.type for c in source.columns}
        for g in a.group_by:
            if g not in names: issues.append(ValidationIssue("aggregation-group", f"unknown group column {g!r}", path))
        if a.value_variable not in names: issues.append(ValidationIssue("aggregation-value", f"unknown value column {a.value_variable!r}", path))
        if a.operator in {"sum", "min", "max"} and a.value_variable in names and names[a.value_variable] not in {TypeName.INTEGER, TypeName.UNSIGNED}:
            issues.append(ValidationIssue("aggregation-value", "numeric aggregation requires an integer or decimal value", path))
        source_atoms = [x for x in rule.body if isinstance(x, Atom) and not x.negated and x.relation == a.relation]
        if not source_atoms: issues.append(ValidationIssue("aggregation-source", "aggregation source must be a positive body atom", path))
        else:
            source_atom = source_atoms[0]; source_names = [c.name for c in source.columns]
            domain_atoms = [x for x in rule.body if isinstance(x, Atom) and not x.negated and x.relation == a.domain]
            closure_atoms = [x for x in rule.body if isinstance(x, Atom) and not x.negated and x.relation == a.closure_witness]
            if not domain_atoms: issues.append(ValidationIssue("aggregation-domain", "finite aggregation domain must be a positive body atom", path))
            if not closure_atoms: issues.append(ValidationIssue("aggregation-closure", "aggregation closure witness must be a positive body atom", path))
            if domain_atoms and domain:
                domain_atom = domain_atoms[0]; domain_names = [c.name for c in domain.columns]
                scope_names = set((*source.context_indices, *a.group_by))
                for scope_name in scope_names:
                    if (scope_name not in domain_names
                            or repr(source_atom.terms[source_names.index(scope_name)])
                            != repr(domain_atom.terms[domain_names.index(scope_name)])):
                        issues.append(ValidationIssue("aggregation-domain", f"domain binding does not match source group/context {scope_name!r}", path))
                if a.operator == "all":
                    source_types = {column.name: column.type for column in source.columns}
                    # Domain member identity must be present in the source;
                    # otherwise projection can collapse distinct members to the
                    # group/context and manufacture universal success. The one
                    # reviewed ``outcome`` label is explicit non-key payload.
                    member_names = [name for name in domain_names
                                    if name not in scope_names
                                    and name != a.value_variable
                                    and (domain.name, name)
                                    not in _AGGREGATE_DOMAIN_PAYLOAD_COLUMNS]
                    for member_name in member_names:
                        if member_name not in source_types:
                            issues.append(ValidationIssue(
                                "aggregation-domain",
                                f"all source omits domain member identity {member_name!r}", path))
                            continue
                        domain_column = domain.columns[domain_names.index(member_name)]
                        if source_types[member_name] != domain_column.type:
                            issues.append(ValidationIssue(
                                "aggregation-domain",
                                f"all source/domain member {member_name!r} has incompatible types", path))
                        elif repr(source_atom.terms[source_names.index(member_name)]) != repr(
                                domain_atom.terms[domain_names.index(member_name)]):
                            issues.append(ValidationIssue(
                                "aggregation-domain",
                                f"all source/domain member {member_name!r} is not identically bound", path))
            if closure_atoms and domain:
                closure_atom = closure_atoms[0]; closure_names = [c.name for c in closure.columns]
                for context_name in domain.context_indices:
                    if context_name not in closure_names or repr(domain_atoms[0].terms[[c.name for c in domain.columns].index(context_name)]) != repr(closure_atom.terms[closure_names.index(context_name)]):
                        issues.append(ValidationIssue("aggregation-closure", f"closure binding does not match domain context {context_name!r}", path))
            head_decl = relations.get(rule.head.relation)
            required_head_columns = set(a.group_by) | set(source.context_indices)
            head_names = [c.name for c in head_decl.columns] if head_decl else []
            for group in required_head_columns:
                if group not in source_names or group not in head_names:
                    issues.append(ValidationIssue("aggregation-head", f"head must preserve group/context column {group!r}", path)); continue
                si = source_names.index(group); hi = head_names.index(group)
                if repr(source_atom.terms[si]) != repr(rule.head.terms[hi]):
                    issues.append(ValidationIssue("aggregation-head", f"head does not preserve group/context column {group!r}", path))
    if domain is None: issues.append(ValidationIssue("aggregation-domain", "aggregation requires a named domain", path))
    elif not domain.finite: issues.append(ValidationIssue("aggregation-domain", "aggregation domain must be finite", path))
    elif a.operator == "all" and not domain.nonempty: issues.append(ValidationIssue("aggregation-domain", "all aggregation requires a non-empty domain", path))
    if closure is None: issues.append(ValidationIssue("aggregation-closure", "aggregation requires a closure witness", path))
    elif closure.modality.value != "completeness" or closure.completes != a.domain: issues.append(ValidationIssue("aggregation-closure", "closure witness must complete the named domain", path))
    elif domain and closure.context_indices != domain.context_indices: issues.append(ValidationIssue("aggregation-closure", "closure witness context positions must match its finite domain", path))
    if source and rule.head.relation == source.name: issues.append(ValidationIssue("recursive-aggregation", "aggregation source cannot be the rule head relation", path))


def _validate_claim(claim, relations, issues, path):
    relation = relations.get(claim.relation)
    if relation is None: issues.append(ValidationIssue("unknown-relation", claim.relation, path)); return
    if len(claim.terms) != relation.arity: issues.append(ValidationIssue("arity", f"expected {relation.arity}, got {len(claim.terms)}", path)); return
    env = {}
    for i, (term, col) in enumerate(zip(claim.terms, relation.columns)): _validate_term(term, col.type, env, issues, f"{path}.terms[{i}]")
    known_context = set(relation.context_indices)
    context = _context_values(claim.context)
    if context is None:
        issues.append(ValidationIssue(
            "claim-context", "claim context must be a well-formed Context", path))
        context = {}
    if set(context) != known_context: issues.append(ValidationIssue("claim-context", "claim context must exactly match relation context indices", path))
    relation_names = [column.name for column in relation.columns]
    for name in known_context:
        if name in context:
            position = relation_names.index(name)
            column = relation.columns[position]
            _validate_term(Constant(context[name]), column.type, {}, issues, f"{path}.context.{name}")
            term = claim.terms[position]
            if (claim.quantifier.value == "forall"
                    and isinstance(term, Variable)):
                issues.append(ValidationIssue(
                    "claim-context",
                    f"FORALL context column {name!r} must be a ground constant", path))
            elif isinstance(term, Constant) and term.value != context[name]:
                issues.append(ValidationIssue(
                    "claim-context",
                    f"context value does not equal claim term for column {name!r}", path))
    if claim.quantifier.value == "forall":
        domain = relations.get(claim.domain) if claim.domain else None
        if domain is None or not domain.finite:
            issues.append(ValidationIssue("forall-domain", "FORALL requires a named finite domain", path))
        else:
            if not domain.nonempty:
                issues.append(ValidationIssue("forall-domain", "FORALL domain must be explicitly non-empty", path))
            claim_values = {column.name for column, term in zip(relation.columns, claim.terms)
                            if isinstance(term, Constant)} | set(context)
            unbound_domain_context = set(domain.context_indices) - claim_values
            if unbound_domain_context:
                issues.append(ValidationIssue(
                    "forall-context",
                    "FORALL claim must ground every domain context position: "
                    + ", ".join(sorted(unbound_domain_context)), path))
            closure_decls = [decl for decl in relations.values()
                             if decl.modality.value == "completeness"
                             and decl.completes == domain.name
                             and decl.context_indices == domain.context_indices
                             and tuple(column.name for column in decl.columns)
                             == domain.context_indices]
            if not closure_decls:
                issues.append(ValidationIssue(
                    "forall-closure",
                    "FORALL requires a whole-domain completeness relation over exactly the domain context",
                    path))
            domain_types = {column.name: column.type for column in domain.columns}
            for position, term in enumerate(claim.terms):
                if not isinstance(term, Variable):
                    continue
                if term.name not in domain_types:
                    issues.append(ValidationIssue("forall-binding", f"variable {term.name!r} is absent from the domain", path))
                elif domain_types[term.name] != relation.columns[position].type:
                    issues.append(ValidationIssue("forall-binding", f"variable {term.name!r} has an incompatible domain type", path))


def _validate_recursion(bundle, relations, issues):
    # ``relations`` contains only structurally checked declarations.  Building
    # this graph from raw Bundle members used to re-dereference ``r.name`` and
    # defeat the earlier relation-type diagnostic with AttributeError.
    edges = {name: [] for name in relations}
    for rule in bundle.rules:
        if (not isinstance(rule, Rule) or not isinstance(rule.head, Atom)
                or not isinstance(rule.head.relation, str)
                or rule.head.relation not in edges):
            continue
        body = rule.body if isinstance(rule.body, tuple) else ()
        for atom in body:
            if (isinstance(atom, Atom) and isinstance(atom.relation, str)
                    and atom.relation in edges):
                edges[rule.head.relation].append((atom.relation, atom.negated))
        if isinstance(rule.aggregation, Aggregation) and rule.aggregation.relation in edges: edges[rule.head.relation].append((rule.aggregation.relation, False))
    components = _scc(edges)
    for start in edges:
        for dst, negative in edges[start]:
            if negative and start in _reachable(edges, dst): issues.append(ValidationIssue("recursive-negation", f"negative cycle involving {start} and {dst}", "rules"))
    for rule in bundle.rules:
        if isinstance(rule, Rule) and isinstance(rule.head, Atom) and isinstance(rule.aggregation, Aggregation) and rule.head.relation in components and rule.aggregation.relation in components and components[rule.head.relation] == components[rule.aggregation.relation]:
            issues.append(ValidationIssue("recursive-aggregation", "aggregation dependency is recursively cyclic", "rules"))


def _reachable(edges, source):
    seen = set(); stack = [source]
    while stack:
        node = stack.pop()
        if node in seen: continue
        seen.add(node); stack.extend(dst for dst, _ in edges[node])
    return seen


def _scc(edges):
    """Return strongly connected components with bounded iterative DFS."""
    index = 0
    stack = []
    on_stack = set()
    indices = {}
    low = {}
    result = {}

    def enter(node):
        nonlocal index
        indices[node] = low[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)

    for root in edges:
        if root in indices:
            continue
        enter(root)
        # Frames are (node, next-child position, parent).  This is Tarjan's
        # recursive call stack made explicit so producer input cannot consume
        # the Python stack before the declared depth bound is checked.
        frames = [(root, 0, None)]
        while frames:
            if len(frames) > MAX_RULE_DEPENDENCY_DEPTH:
                raise ValidationResourceError(
                    f"rule dependency depth exceeds {MAX_RULE_DEPENDENCY_DEPTH}")
            node, position, parent = frames[-1]
            children = edges[node]
            if position < len(children):
                child, _ = children[position]
                frames[-1] = (node, position + 1, parent)
                if child not in indices:
                    enter(child)
                    frames.append((child, 0, node))
                elif child in on_stack:
                    low[node] = min(low[node], indices[child])
                continue

            frames.pop()
            if parent is not None:
                low[parent] = min(low[parent], low[node])
            if low[node] == indices[node]:
                component = len(result)
                while True:
                    item = stack.pop()
                    on_stack.remove(item)
                    result[item] = component
                    if item == node:
                        break
    return result
