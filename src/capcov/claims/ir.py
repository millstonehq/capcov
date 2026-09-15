"""The small, serialisable intermediate representation for claim rules.

This module intentionally contains no evaluation code.  Values are frozen so a
bundle can safely be hashed and handed to independent evaluators.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
import hashlib
import json
from typing import Any, Mapping, Sequence
import math


SCHEMA_VERSION = 1


class _TextEnum(str, Enum):
    def __str__(self) -> str: return self.value


class TypeName(_TextEnum):
    SYMBOL = "symbol"
    INTEGER = "integer"
    UNSIGNED = "unsigned"
    BOOLEAN = "boolean"
    TIMESTAMP = "timestamp"  # epoch microseconds
    DIGEST = "digest"
    JSON_METADATA_ONLY = "json-metadata-only"


class Modality(_TextEnum):
    OBSERVATION = "observation"
    ASSUMPTION = "assumption"
    COMPATIBILITY = "compatibility"
    COMPLETENESS = "completeness"
    CLAIM = "claim"
    DERIVED = "derived"


class Polarity(_TextEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"


class BindingTime(_TextEnum):
    STATIC = "static"
    RUNTIME = "runtime"


class Quantifier(_TextEnum):
    EXISTS = "exists"
    FORALL = "forall"


class EvidenceEffect(_TextEnum):
    """How a relation contributes to a claim's semantic polarity."""
    SUPPORT = "support"
    REFUTATION = "refutation"
    OBSERVATION = "observation"
    FORBIDDEN = "forbidden"


class OutputKind(_TextEnum):
    OBSERVED = "observed"
    FORBIDDEN = "forbidden"
    DISCREPANCY = "discrepancy"
    MISSING_PREMISE = "missing_premise"


@dataclass(frozen=True)
class TemplateValue:
    """Typed constant or column reference used by a diagnostic output."""
    source: str = "constant"  # constant, claim, evidence
    column: str = ""
    type: TypeName | str = TypeName.SYMBOL
    value: Any = None
    evidence_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "type", TypeName(self.type))
        object.__setattr__(self, "value", _freeze_value(self.value))


@dataclass(frozen=True)
class OutputTemplate:
    kind: OutputKind | str
    claim_id: str
    evidence_id: str | None = None
    relation: str | None = None
    fields: tuple[tuple[str, TemplateValue], ...] = ()
    requires_all_evidence: tuple[str, ...] = ()
    requires_any_evidence: tuple[str, ...] = ()
    excludes_evidence: tuple[str, ...] = ()
    when_claim: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", OutputKind(self.kind))
        object.__setattr__(self, "fields", tuple(sorted(self.fields)))
        object.__setattr__(self, "requires_all_evidence", tuple(sorted(self.requires_all_evidence)))
        object.__setattr__(self, "requires_any_evidence", tuple(sorted(self.requires_any_evidence)))
        object.__setattr__(self, "excludes_evidence", tuple(sorted(self.excludes_evidence)))


@dataclass(frozen=True)
class Column:
    name: str
    type: TypeName | str
    context: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "type", TypeName(self.type))


@dataclass(frozen=True)
class RelationDecl:
    name: str
    columns: tuple[Column, ...]
    modality: Modality | str = Modality.OBSERVATION
    polarity: Polarity | str = Polarity.POSITIVE
    binding: BindingTime | str = BindingTime.RUNTIME
    primitive: bool = True
    producer_classes: tuple[str, ...] = ()
    context_indices: tuple[str, ...] = ()
    completes: str | None = None
    finite: bool = False
    nonempty: bool = False
    compatibility_targets: tuple[str, ...] = ()
    compatibility_context_indices: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "columns", tuple(self.columns))
        object.__setattr__(self, "modality", Modality(self.modality))
        object.__setattr__(self, "polarity", Polarity(self.polarity))
        object.__setattr__(self, "binding", BindingTime(self.binding))
        object.__setattr__(self, "producer_classes", tuple(sorted(self.producer_classes)))
        object.__setattr__(self, "context_indices", tuple(self.context_indices))
        object.__setattr__(self, "compatibility_targets", tuple(sorted(self.compatibility_targets)))
        object.__setattr__(self, "compatibility_context_indices", tuple(self.compatibility_context_indices))

    @property
    def arity(self) -> int: return len(self.columns)


@dataclass(frozen=True)
class Context:
    values: tuple[tuple[str, Any], ...] = ()

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "Context":
        if not isinstance(values, Mapping):
            raise TypeError("context must be an object")
        return cls(tuple(sorted(values.items())))

    def __post_init__(self) -> None:
        if any(not isinstance(k, str) for k, _ in self.values): raise TypeError("context keys must be strings")
        keys = [k for k, _ in self.values]
        if len(keys) != len(set(keys)): raise ValueError("duplicate context keys")
        object.__setattr__(self, "values", tuple((k, _freeze_value(v)) for k, v in sorted(self.values)))

    def as_dict(self) -> dict[str, Any]: return dict(self.values)


@dataclass(frozen=True)
class Variable:
    name: str


@dataclass(frozen=True)
class Constant:
    value: Any
    type: TypeName | str | None = None

    def __post_init__(self) -> None:
        if self.type is not None: object.__setattr__(self, "type", TypeName(self.type))
        object.__setattr__(self, "value", _freeze_value(self.value))


Term = Variable | Constant


@dataclass(frozen=True)
class Atom:
    relation: str
    terms: tuple[Term, ...]
    negated: bool = False

    def __post_init__(self) -> None: object.__setattr__(self, "terms", tuple(self.terms))


@dataclass(frozen=True)
class Evidence:
    """Authoritative fact identity and provenance, separate from its atom."""
    id: str
    atom: Atom
    context: Context = field(default_factory=Context)
    source: str = ""
    depends_on: tuple[str, ...] = ()
    kind: str = "fact"

    def __post_init__(self) -> None:
        if not self.id: raise ValueError("evidence id must not be empty")
        object.__setattr__(self, "depends_on", tuple(sorted(set(self.depends_on))))


@dataclass(frozen=True)
class EvidenceMapping:
    """Explicit relation-to-relation polarity mapping used by evaluators."""
    claim_relation: str
    evidence_relation: str
    effect: EvidenceEffect | str
    context_indices: tuple[str, ...] = ()
    bindings: tuple[tuple[str, str], ...] = ()
    required: bool = False
    allow_out_of_scope: bool = False
    claim_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.required, bool):
            raise TypeError("mapping required must be boolean")
        if not isinstance(self.allow_out_of_scope, bool):
            raise TypeError("mapping allow_out_of_scope must be boolean")
        try: effect = EvidenceEffect(self.effect)
        except (TypeError, ValueError): effect = self.effect
        object.__setattr__(self, "effect", effect)
        object.__setattr__(self, "context_indices", tuple(sorted(set(self.context_indices))))
        object.__setattr__(self, "bindings", tuple(sorted(self.bindings)))


@dataclass(frozen=True)
class DiagnosticRule:
    """Typed trigger for an operational/semantic diagnostic."""
    trigger_relation: str
    effect: EvidenceEffect | str
    operational_status: str = "complete"
    context_indices: tuple[str, ...] = ()
    when_missing: bool = False
    required: bool = False
    message: str = ""
    claim_id: str = ""
    predicate: tuple[tuple[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.when_missing, bool):
            raise TypeError("diagnostic when_missing must be boolean")
        if not isinstance(self.required, bool):
            raise TypeError("diagnostic required must be boolean")
        if not isinstance(self.message, str):
            raise TypeError("diagnostic message must be a string")
        predicate = tuple(self.predicate)
        if any(not isinstance(pair, (tuple, list)) or len(pair) != 2
               or not isinstance(pair[0], str) for pair in predicate):
            raise TypeError("diagnostic predicate must contain string-keyed pairs")
        keys = [pair[0] for pair in predicate]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate diagnostic predicate key")
        try: effect = EvidenceEffect(self.effect)
        except (TypeError, ValueError): effect = self.effect
        object.__setattr__(self, "effect", effect)
        object.__setattr__(self, "context_indices", tuple(sorted(set(self.context_indices))))
        object.__setattr__(self, "predicate", tuple(sorted(
            (key, _freeze_value(value)) for key, value in predicate)))


@dataclass(frozen=True)
class DiagnosticPolicy:
    """Declared handling policy for evidence diagnostics and revocation."""
    missing_premises: str = "unresolved"
    inconsistent_premises: str = "inconsistent-premises"
    out_of_scope: str = "out-of-scope"
    forbidden_evidence: str = "invalid-input"
    revocation: str = "refutation"
    completeness: str = "required"


@dataclass(frozen=True)
class Comparison:
    left: Term
    operator: str
    right: Term


@dataclass(frozen=True)
class Aggregation:
    name: str
    relation: str
    group_by: tuple[str, ...]
    value_variable: str
    operator: str = "count"
    domain: str | None = None
    closure_witness: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "group_by", tuple(self.group_by))


@dataclass(frozen=True)
class Rule:
    head: Atom
    body: tuple[Atom | Comparison, ...]
    name: str = ""
    aggregation: Aggregation | None = None

    def __post_init__(self) -> None: object.__setattr__(self, "body", tuple(self.body))


@dataclass(frozen=True)
class Claim:
    relation: str
    terms: tuple[Term, ...]
    context: Context = field(default_factory=Context)
    quantifier: Quantifier | str = Quantifier.EXISTS
    domain: str | None = None
    id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "terms", tuple(self.terms))
        object.__setattr__(self, "quantifier", Quantifier(self.quantifier))


def _assert_bundle_ir_shape(value: Any, path: str) -> None:
    """Reject malformed nested Python graphs before Bundle canonical sorting.

    Semantic-invalid schema-v1 values remain constructible so the differential
    can persist and replay them.  This boundary only rejects objects that are
    not an instance of the declared recursive IR shape and therefore cannot
    have authoritative canonical bytes.
    """
    def require(condition: bool, message: str) -> None:
        if not condition:
            raise TypeError(f"{path}: {message}")

    def term(item: Any, item_path: str) -> None:
        if isinstance(item, Variable):
            require(isinstance(item.name, str),
                    f"{item_path} variable name must be a string")
            return
        if isinstance(item, Constant):
            require(item.type is None or isinstance(item.type, TypeName),
                    f"{item_path} constant type must be a TypeName")
            # Require the frozen representation itself, not merely that the
            # value could be frozen: a mutable container smuggled in through
            # object.__setattr__ would otherwise sit inside a hashed Bundle.
            try:
                _assert_frozen_value(item.value, f"{item_path}.value")
            except (TypeError, ValueError) as exc:
                require(False, str(exc))
            return
        require(False, f"{item_path} must be Variable or Constant")

    def atom(item: Any, item_path: str) -> None:
        require(isinstance(item, Atom), f"{item_path} must be an Atom")
        require(isinstance(item.relation, str),
                f"{item_path}.relation must be a string")
        require(isinstance(item.terms, tuple),
                f"{item_path}.terms must be a tuple")
        require(isinstance(item.negated, bool),
                f"{item_path}.negated must be boolean")
        for index, item_term in enumerate(item.terms):
            term(item_term, f"{item_path}.terms[{index}]")

    def context(item: Any, item_path: str) -> None:
        require(isinstance(item, Context),
                f"{item_path} must be a Context")
        require(isinstance(item.values, tuple),
                f"{item_path}.values must be a tuple")
        keys = []
        for index, pair in enumerate(item.values):
            require(isinstance(pair, tuple) and len(pair) == 2
                    and isinstance(pair[0], str),
                    f"{item_path}.values[{index}] must be a string-keyed pair")
            keys.append(pair[0])
            _assert_frozen_value(
                pair[1], f"{item_path}.values[{index}][1]")
        require(keys == sorted(keys),
                f"{item_path}.values must be sorted by key")
        require(len(keys) == len(set(keys)),
                f"{item_path}.values keys must be unique")

    if isinstance(value, RelationDecl):
        require(isinstance(value.name, str), "relation name must be a string")
        require(isinstance(value.columns, tuple), "relation columns must be a tuple")
        for index, column in enumerate(value.columns):
            require(isinstance(column, Column),
                    f"columns[{index}] must be a Column")
            require(isinstance(column.name, str),
                    f"columns[{index}].name must be a string")
            require(isinstance(column.type, TypeName),
                    f"columns[{index}].type must be a TypeName")
            require(isinstance(column.context, bool),
                    f"columns[{index}].context must be boolean")
        require(isinstance(value.modality, Modality),
                "relation modality must be a Modality")
        require(isinstance(value.polarity, Polarity),
                "relation polarity must be a Polarity")
        require(isinstance(value.binding, BindingTime),
                "relation binding must be a BindingTime")
        require(all(isinstance(flag, bool) for flag in
                    (value.primitive, value.finite, value.nonempty)),
                "relation primitive/finite/nonempty fields must be boolean")
        require(value.completes is None or isinstance(value.completes, str),
                "relation completes must be a string or null")
        for name in ("producer_classes", "context_indices",
                     "compatibility_targets", "compatibility_context_indices"):
            items = getattr(value, name)
            require(isinstance(items, tuple)
                    and all(isinstance(item, str) for item in items),
                    f"relation {name} must be a tuple of strings")
        return
    if isinstance(value, Atom):
        atom(value, path)
        return
    if isinstance(value, Evidence):
        require(all(isinstance(item, str) for item in
                    (value.id, value.source, value.kind)),
                "evidence id/source/kind must be strings")
        context(value.context, f"{path}.context")
        require(isinstance(value.depends_on, tuple)
                and all(isinstance(item, str) for item in value.depends_on),
                "evidence dependencies must be a tuple of strings")
        atom(value.atom, f"{path}.atom")
        return
    if isinstance(value, Rule):
        atom(value.head, f"{path}.head")
        require(isinstance(value.body, tuple), "rule body must be a tuple")
        for index, body_item in enumerate(value.body):
            item_path = f"{path}.body[{index}]"
            if isinstance(body_item, Atom):
                atom(body_item, item_path)
            elif isinstance(body_item, Comparison):
                term(body_item.left, f"{item_path}.left")
                term(body_item.right, f"{item_path}.right")
                require(isinstance(body_item.operator, str),
                        f"{item_path}.operator must be a string")
            else:
                require(False, f"{item_path} must be an Atom or Comparison")
        require(isinstance(value.name, str), "rule name must be a string")
        if value.aggregation is not None:
            aggregation = value.aggregation
            require(isinstance(aggregation, Aggregation),
                    "rule aggregation must be an Aggregation or null")
            require(all(isinstance(item, str) for item in
                        (aggregation.name, aggregation.relation,
                         aggregation.value_variable, aggregation.operator)),
                    "aggregation scalar fields must be strings")
            require(isinstance(aggregation.group_by, tuple)
                    and all(isinstance(item, str)
                            for item in aggregation.group_by),
                    "aggregation group_by must be a tuple of strings")
            require(all(item is None or isinstance(item, str) for item in
                        (aggregation.domain, aggregation.closure_witness)),
                    "aggregation domain/closure must be strings or null")
        return
    if isinstance(value, Claim):
        require(isinstance(value.relation, str),
                "claim relation must be a string")
        require(isinstance(value.terms, tuple), "claim terms must be a tuple")
        for index, item_term in enumerate(value.terms):
            term(item_term, f"{path}.terms[{index}]")
        context(value.context, f"{path}.context")
        require(isinstance(value.quantifier, Quantifier),
                "claim quantifier must be a Quantifier")
        require(value.domain is None or isinstance(value.domain, str),
                "claim domain must be a string or null")
        require(isinstance(value.id, str), "claim id must be a string")
        return
    if isinstance(value, EvidenceMapping):
        require(all(isinstance(item, str) for item in
                    (value.claim_relation, value.evidence_relation,
                     value.claim_id)),
                "mapping relation/id fields must be strings")
        require(isinstance(value.effect, (EvidenceEffect, str)),
                "mapping effect must be a string")
        require(isinstance(value.context_indices, tuple)
                and all(isinstance(item, str)
                        for item in value.context_indices),
                "mapping context indices must be a tuple of strings")
        require(isinstance(value.bindings, tuple)
                and all(isinstance(pair, tuple) and len(pair) == 2
                        and all(isinstance(item, str) for item in pair)
                        for pair in value.bindings),
                "mapping bindings must be string pairs")
        require(isinstance(value.required, bool)
                and isinstance(value.allow_out_of_scope, bool),
                "mapping options must be boolean")
        return
    if isinstance(value, DiagnosticRule):
        require(all(isinstance(item, str) for item in
                    (value.trigger_relation, value.operational_status,
                     value.message, value.claim_id)),
                "diagnostic scalar fields must be strings")
        require(isinstance(value.effect, (EvidenceEffect, str)),
                "diagnostic effect must be a string")
        require(isinstance(value.when_missing, bool)
                and isinstance(value.required, bool),
                "diagnostic options must be boolean")
        require(isinstance(value.context_indices, tuple)
                and all(isinstance(item, str)
                        for item in value.context_indices),
                "diagnostic context indices must be a tuple of strings")
        require(isinstance(value.predicate, tuple),
                "diagnostic predicate must be a tuple")
        for index, pair in enumerate(value.predicate):
            require(isinstance(pair, tuple) and len(pair) == 2
                    and isinstance(pair[0], str),
                    f"diagnostic predicate[{index}] must be a string-keyed pair")
            try:
                _assert_frozen_value(pair[1], f"diagnostic predicate[{index}].value")
            except (TypeError, ValueError) as exc:
                require(False, str(exc))
        return
    if isinstance(value, OutputTemplate):
        require(isinstance(value.kind, OutputKind),
                "output kind must be an OutputKind")
        require(isinstance(value.claim_id, str),
                "output claim id must be a string")
        require(all(item is None or isinstance(item, str) for item in
                    (value.evidence_id, value.relation, value.when_claim)),
                "output scalar fields must be strings or null")
        require(isinstance(value.fields, tuple), "output fields must be a tuple")
        for index, pair in enumerate(value.fields):
            require(isinstance(pair, tuple) and len(pair) == 2
                    and isinstance(pair[0], str)
                    and isinstance(pair[1], TemplateValue),
                    f"output field[{index}] must be a name/TemplateValue pair")
            template = pair[1]
            require(isinstance(template.source, str)
                    and isinstance(template.column, str)
                    and isinstance(template.type, TypeName)
                    and (template.evidence_id is None
                         or isinstance(template.evidence_id, str)),
                    f"output field[{index}] is malformed")
            _assert_frozen_value(
                template.value, f"{path}.fields[{index}].value")
        for name in ("requires_all_evidence", "requires_any_evidence",
                     "excludes_evidence"):
            items = getattr(value, name)
            require(isinstance(items, tuple)
                    and all(isinstance(item, str) for item in items),
                    f"output {name} must be a tuple of strings")
        return
    if isinstance(value, DiagnosticPolicy):
        for policy_field in fields(value):
            require(isinstance(getattr(value, policy_field.name), str),
                    f"diagnostic policy {policy_field.name} must be a string")
        return
    require(False, "unsupported IR member")


@dataclass(frozen=True)
class Bundle:
    relations: tuple[RelationDecl, ...]
    facts: tuple[Atom, ...] = ()
    rules: tuple[Rule, ...] = ()
    claims: tuple[Claim, ...] = ()
    metadata: tuple[tuple[str, Any], ...] = ()
    schema_version: int = SCHEMA_VERSION
    evidence: tuple[Evidence, ...] = ()
    mappings: tuple[EvidenceMapping, ...] = ()
    diagnostic_policy: DiagnosticPolicy = field(default_factory=DiagnosticPolicy)
    diagnostics: tuple[DiagnosticRule, ...] = ()
    outputs: tuple[OutputTemplate, ...] = ()

    def __post_init__(self) -> None:
        # ``compare`` and canonical replay accept Bundle values, not arbitrary
        # Python object graphs.  Reject non-IR collection members here rather
        # than letting validation partly inspect them and then crash (or making
        # the shrinker pretend an unencodable object has replay bytes).
        collections = (
            ("relations", self.relations, RelationDecl),
            ("facts", self.facts, Atom),
            ("rules", self.rules, Rule),
            ("claims", self.claims, Claim),
            ("evidence", self.evidence, Evidence),
            ("mappings", self.mappings, EvidenceMapping),
            ("diagnostics", self.diagnostics, DiagnosticRule),
            ("outputs", self.outputs, OutputTemplate),
        )
        normalized = {}
        for name, values, expected in collections:
            try:
                values = tuple(values)
            except TypeError as exc:
                raise TypeError(f"Bundle.{name} must be an iterable of {expected.__name__} values") from exc
            if any(not isinstance(value, expected) for value in values):
                raise TypeError(f"Bundle.{name} must contain only {expected.__name__} values")
            for index, value in enumerate(values):
                _assert_bundle_ir_shape(value, f"Bundle.{name}[{index}]")
            normalized[name] = tuple(sorted(values, key=_sort_key))
        if not isinstance(self.diagnostic_policy, DiagnosticPolicy):
            raise TypeError("Bundle.diagnostic_policy must be a DiagnosticPolicy")
        _assert_bundle_ir_shape(
            self.diagnostic_policy, "Bundle.diagnostic_policy")

        # These collections denote sets in the language.  Normalize their
        # order at construction time so independently assembled bundles hash
        # identically even when their producers enumerate inputs differently.
        for name, values in normalized.items():
            object.__setattr__(self, name, values)
        metadata_keys = [k for k, _ in self.metadata]
        if any(not isinstance(k, str) for k in metadata_keys): raise TypeError("metadata keys must be strings")
        if len(metadata_keys) != len(set(metadata_keys)): raise ValueError("duplicate metadata keys")
        object.__setattr__(self, "metadata", tuple((k, _freeze_value(v)) for k, v in sorted(self.metadata)))
        if (type(self.schema_version) is not int
                or self.schema_version != SCHEMA_VERSION):
            raise ValueError(f"unsupported claim schema version: {self.schema_version}")


def _plain(value: Any) -> Any:
    if isinstance(value, Enum): return value.value
    if isinstance(value, (str, int, float, bool)) or value is None: return value
    if isinstance(value, _FrozenMap): return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, Context):
        # Context has one public representation: the same flat object accepted
        # on the wire.  A dataclass wrapper named ``values`` would collide with
        # the legal context key ``values`` and make schema-v1 decoding lossy.
        return {key: _plain(item) for key, item in value.values}
    if isinstance(value, Mapping): raise TypeError("mapping must be recursively frozen before canonicalisation")
    if isinstance(value, (tuple, list, set, frozenset)): return [_plain(v) for v in value]
    if is_dataclass(value):
        return {f.name: _plain(getattr(value, f.name)) for f in fields(value)}
    raise TypeError(f"not canonicalisable: {type(value).__name__}")


def canonical_dict(value: Any) -> dict[str, Any] | Any:
    """Return JSON-compatible data with deterministic field and set ordering."""
    return _plain(value if is_dataclass(value) else _freeze_value(value))


def canonical_json(value: Any) -> str:
    return json.dumps(canonical_dict(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def schema_digest(bundle: Bundle) -> str: return digest(bundle)


def to_json(value: Any) -> str: return canonical_json(value)


def canonical_digest(value: Any) -> str: return digest(value)


def _sort_key(value: Any) -> str:
    """Canonical structural key; malformed IR gets a deterministic type key."""
    try:
        return canonical_json(value)
    except (TypeError, ValueError):
        if is_dataclass(value):
            return value.__class__.__module__ + "." + value.__class__.__qualname__ + "{" + ",".join(f.name + ":" + _sort_key(getattr(value, f.name)) for f in fields(value)) + "}"
        return type(value).__module__ + "." + type(value).__qualname__


class _FrozenMap(Mapping[str, Any]):
    __slots__ = ("_items",)
    def __init__(self, items): self._items = tuple(items)
    def __getitem__(self, key): return dict(self._items)[key]
    def __iter__(self): return (k for k, _ in self._items)
    def __len__(self): return len(self._items)
    def items(self): return self._items
    def __hash__(self): return hash(self._items)


def _freeze_value(value: Any) -> Any:
    """Copy supported JSON values into immutable values before they enter IR."""
    if value is None or isinstance(value, (str, bool, int)): return value
    if isinstance(value, float):
        if not math.isfinite(value): raise ValueError("non-finite floats are not canonical JSON values")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(k, str) for k in value):
            raise TypeError("canonical JSON object keys must be strings")
        return _FrozenMap((k, _freeze_value(v)) for k, v in sorted(value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(v) for v in value)
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def _assert_frozen_value(value: Any, path: str) -> None:
    """Require the exact immutable representation produced by _freeze_value."""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite float")
        return
    if isinstance(value, tuple):
        for index, item in enumerate(value):
            _assert_frozen_value(item, f"{path}[{index}]")
        return
    if isinstance(value, _FrozenMap):
        items = value.items()
        if (not isinstance(items, tuple)
                or any(not isinstance(pair, tuple) or len(pair) != 2
                       or not isinstance(pair[0], str) for pair in items)):
            raise TypeError(f"{path} contains a malformed frozen object")
        keys = [key for key, _ in items]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise TypeError(f"{path} contains a noncanonical frozen object")
        for key, item in items:
            _assert_frozen_value(item, f"{path}.{key}")
        return
    raise TypeError(f"{path} is not recursively frozen")


def _strict_object(raw, allowed, path):
    if not isinstance(raw, Mapping): raise TypeError(f"{path} must be an object")
    unknown = set(raw) - set(allowed)
    if unknown: raise ValueError(f"{path} has unknown fields: {sorted(unknown)}")
    return raw


def _strict_sequence(raw, path):
    if not isinstance(raw, (list, tuple)):
        raise TypeError(f"{path} must be an array")
    return raw


def _sequence_field(raw, name, path):
    if name not in raw:
        return ()
    return _strict_sequence(raw[name], f"{path}.{name}")


def _strict_pairs(raw, path):
    pairs = []
    for pair in _strict_sequence(raw, path):
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ValueError(f"{path} must contain key/value pairs")
        key, value = pair
        if not isinstance(key, str):
            raise TypeError(f"{path} keys must be strings")
        pairs.append((key, value))
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError(f"duplicate {path} key")
    return tuple(pairs)


def _object_or_pairs(raw, path):
    if isinstance(raw, Mapping):
        return raw
    return dict(_strict_pairs(raw, path))


# Schema v1 originally canonicalised a Context through the generic dataclass
# path as {"values": [[key, value], ...]}.  The flat encoding now used is the
# only accepted form, and the legacy wrapper is a reserved shape: a context
# whose sole key is "values" holding a list of string-keyed pairs is rejected
# rather than reinterpreted, so old canonical bytes cannot silently change
# meaning and a genuine "values" context key (any other value shape) still works.
def _is_legacy_context_wrapper(raw) -> bool:
    if not isinstance(raw, Mapping) or set(raw) != {"values"}:
        return False
    inner = raw["values"]
    return (isinstance(inner, (list, tuple))
            and all(isinstance(pair, (list, tuple)) and len(pair) == 2 and isinstance(pair[0], str) for pair in inner))


def _context(raw, path):
    if _is_legacy_context_wrapper(raw):
        raise ValueError(f"{path} uses the retired schema-v1 context wrapper {{\"values\": [[key, value], ...]}}; "
                         "re-canonicalise the bundle (contexts are flat objects)")
    raw = _strict_object(raw, set(raw) if isinstance(raw, Mapping) else (), path)
    return Context.from_mapping(raw)


class BundleIngestionError(ValueError):
    """Raw input could not become a canonical Bundle."""

    operational_failure = "invalid-input"


def _bundle_from_json(source: str | bytes | Mapping[str, Any], *, validate: bool = True) -> Bundle:
    """Strictly ingest a schema-v1 JSON object; unknown fields are rejected."""
    def reject_constant(value): raise ValueError(f"non-standard JSON constant: {value}")
    def reject_duplicate(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result
    raw = json.loads(source, parse_constant=reject_constant, object_pairs_hook=reject_duplicate) if isinstance(source, (str, bytes)) else source
    _strict_object(raw, {"schema_version", "relations", "facts", "evidence", "mappings", "diagnostic_policy", "diagnostics", "outputs", "rules", "claims", "metadata"}, "bundle")
    if (type(raw.get("schema_version")) is not int
            or raw["schema_version"] != SCHEMA_VERSION):
        raise ValueError("unsupported claim schema version")
    def column(x):
        x = _strict_object(x, {"name", "type", "context"}, "column")
        return Column(x["name"], x["type"], x.get("context", False))
    def relation(x):
        x = _strict_object(x, {"name", "columns", "modality", "polarity", "binding", "primitive", "producer_classes", "context_indices", "completes", "finite", "nonempty", "compatibility_targets", "compatibility_context_indices"}, "relation")
        return RelationDecl(x["name"], tuple(column(c) for c in _sequence_field(x, "columns", "relation")), x.get("modality", "observation"), x.get("polarity", "positive"), x.get("binding", "runtime"), x.get("primitive", True), tuple(_sequence_field(x, "producer_classes", "relation")), tuple(_sequence_field(x, "context_indices", "relation")), x.get("completes"), x.get("finite", False), x.get("nonempty", False), tuple(_sequence_field(x, "compatibility_targets", "relation")), tuple(_sequence_field(x, "compatibility_context_indices", "relation")))
    def term(x):
        # Accept both wire form (variable) and canonical dataclass form
        # (name), so canonical bundles are strict-ingestible again.
        if isinstance(x, Mapping) and "name" in x and "variable" not in x:
            if set(x) != {"name"}:
                raise ValueError("canonical variable term has unknown fields")
            x = {"variable": x["name"]}
        x = _strict_object(x, {"variable", "value", "type"}, "term")
        if "variable" in x:
            if set(x) != {"variable"}: raise ValueError("variable term cannot have value/type")
            return Variable(x["variable"])
        if "value" not in x: raise ValueError("term needs variable or value")
        return Constant(x["value"], x.get("type"))
    def atom(x):
        x = _strict_object(x, {"relation", "terms", "negated"}, "atom")
        return Atom(x["relation"], tuple(term(t) for t in _sequence_field(x, "terms", "atom")), x.get("negated", False))
    def comparison(x):
        x = _strict_object(x, {"left", "operator", "right"}, "comparison")
        return Comparison(term(x["left"]), x["operator"], term(x["right"]))
    def rule(x):
        x = _strict_object(x, {"head", "body", "name", "aggregation"}, "rule")
        agg = x.get("aggregation")
        if agg is not None:
            _strict_object(agg, {"name", "relation", "group_by", "value_variable", "operator", "domain", "closure_witness"}, "aggregation")
            agg = Aggregation(agg["name"], agg["relation"], tuple(_sequence_field(agg, "group_by", "aggregation")), agg["value_variable"], agg.get("operator", "count"), agg.get("domain"), agg.get("closure_witness"))
        def body_item(item):
            if isinstance(item, Mapping) and "comparison" in item:
                item = _strict_object(item, {"comparison"}, "rule body item")
                return comparison(item["comparison"])
            if isinstance(item, Mapping) and set(item) == {"left", "operator", "right"}:
                return comparison(item)
            return atom(item)
        body = tuple(body_item(a) for a in _sequence_field(x, "body", "rule"))
        return Rule(atom(x["head"]), body, x.get("name", ""), agg)
    def claim(x):
        x = _strict_object(x, {"id", "relation", "terms", "context", "quantifier", "domain"}, "claim")
        context = _context(x["context"], "claim.context") if "context" in x else Context()
        return Claim(x["relation"], tuple(term(t) for t in _sequence_field(x, "terms", "claim")), context, x.get("quantifier", "exists"), x.get("domain"), x.get("id", ""))
    def evidence(x):
        x = _strict_object(x, {"id", "atom", "relation", "terms", "context", "source", "depends_on", "kind"}, "evidence")
        has_atom = "atom" in x
        has_wire_atom = "relation" in x or "terms" in x
        if has_atom and has_wire_atom:
            raise ValueError(
                "evidence must use either atom or relation/terms, not both")
        if has_atom:
            raw_atom = x["atom"]
        else:
            if "relation" not in x:
                raise ValueError("wire evidence requires relation")
            raw_atom = {"relation": x["relation"], "terms": _sequence_field(x, "terms", "evidence")}
        context = _context(x["context"], "evidence.context") if "context" in x else Context()
        return Evidence(x["id"], atom(raw_atom), context, x.get("source", ""), tuple(_sequence_field(x, "depends_on", "evidence")), x.get("kind", "fact"))
    def mapping(x):
        x = _strict_object(x, {"claim_id", "claim_relation", "evidence_relation", "effect", "context_indices", "bindings", "required", "allow_out_of_scope"}, "mapping")
        bindings = _strict_pairs(_sequence_field(x, "bindings", "mapping"), "mapping.bindings")
        if any(not isinstance(value, str) for _, value in bindings):
            raise TypeError("mapping.bindings values must be strings")
        return EvidenceMapping(x["claim_relation"], x["evidence_relation"], x["effect"], tuple(_sequence_field(x, "context_indices", "mapping")), bindings, x.get("required", False), x.get("allow_out_of_scope", False), x.get("claim_id", ""))
    def diagnostic(x):
        x = _strict_object(x, {"claim_id", "trigger_relation", "effect", "operational_status", "context_indices", "when_missing", "required", "message", "predicate"}, "diagnostic")
        predicate = _object_or_pairs(x["predicate"], "diagnostic.predicate") if "predicate" in x else {}
        if predicate: _strict_object(predicate, {"column", "operator", "value"}, "diagnostic.predicate")
        if predicate and (not isinstance(predicate.get("column"), str) or not isinstance(predicate.get("operator"), str)):
            raise ValueError("diagnostic predicate column/operator must be strings")
        return DiagnosticRule(x["trigger_relation"], x["effect"], x.get("operational_status", "complete"), tuple(_sequence_field(x, "context_indices", "diagnostic")), x.get("when_missing", False), x.get("required", False), x.get("message", ""), x.get("claim_id", ""), tuple(sorted(predicate.items())))
    def output(x):
        x = _strict_object(x, {"kind", "claim_id", "evidence_id", "relation", "fields", "requires_all_evidence", "requires_any_evidence", "excludes_evidence", "when_claim"}, "output")
        fields_raw = _object_or_pairs(x["fields"], "output.fields") if "fields" in x else {}
        values = []
        for name, value in fields_raw.items():
            if not isinstance(name, str):
                raise TypeError("output field names must be strings")
            value = _strict_object(value, {"source", "column", "type", "value", "evidence_id"}, "output field")
            values.append((name, TemplateValue(value.get("source", "constant"), value.get("column", ""), value.get("type", "symbol"), value.get("value"), value.get("evidence_id"))))
        kind = x["kind"]
        default_when = "unresolved" if kind in {"discrepancy", "missing_premise"} else None
        return OutputTemplate(kind, x["claim_id"], x.get("evidence_id"), x.get("relation"), tuple(values), tuple(_sequence_field(x, "requires_all_evidence", "output")), tuple(_sequence_field(x, "requires_any_evidence", "output")), tuple(_sequence_field(x, "excludes_evidence", "output")), x.get("when_claim", default_when))
    policy_raw = raw["diagnostic_policy"] if "diagnostic_policy" in raw else {}
    _strict_object(policy_raw, {"missing_premises", "inconsistent_premises", "out_of_scope", "forbidden_evidence", "revocation", "completeness"}, "diagnostic_policy")
    if any(not isinstance(value, str) for value in policy_raw.values()):
        raise TypeError("diagnostic_policy values must be strings")
    policy = DiagnosticPolicy(**policy_raw)
    metadata_raw = raw["metadata"] if "metadata" in raw else {}
    metadata_items = (tuple(metadata_raw.items()) if isinstance(metadata_raw, Mapping)
                      else _strict_pairs(metadata_raw, "metadata"))
    bundle = Bundle(tuple(relation(x) for x in _sequence_field(raw, "relations", "bundle")), tuple(atom(x) for x in _sequence_field(raw, "facts", "bundle")), tuple(rule(x) for x in _sequence_field(raw, "rules", "bundle")), tuple(claim(x) for x in _sequence_field(raw, "claims", "bundle")), tuple(sorted(metadata_items)), SCHEMA_VERSION, tuple(evidence(x) for x in _sequence_field(raw, "evidence", "bundle")), tuple(mapping(x) for x in _sequence_field(raw, "mappings", "bundle")), policy, tuple(diagnostic(x) for x in _sequence_field(raw, "diagnostics", "bundle")), tuple(output(x) for x in _sequence_field(raw, "outputs", "bundle")))
    if validate:
        from .validation import assert_valid
        assert_valid(bundle)
    return bundle


def bundle_from_json(source: str | bytes | Mapping[str, Any], *,
                     validate: bool = True) -> Bundle:
    """Ingest raw JSON or raise one named ``invalid-input`` boundary error.

    Differential comparison starts only after this function has produced a
    canonical Bundle.  Raw malformed bytes therefore do not pretend to have a
    Bundle replay digest; callers can classify this explicit ingestion result
    without broadly treating implementation ``AttributeError`` as bad input.
    """
    try:
        return _bundle_from_json(source, validate=validate)
    except BundleIngestionError:
        raise
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise BundleIngestionError(str(exc)) from exc


def from_json(source): return bundle_from_json(source)
