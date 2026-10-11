"""A small, deterministic evaluator for the schema-v1 claim language.

This module intentionally has no dependency on a second rule engine.  It is
the executable reference for the finite part of the IR: facts and Horn rules
are closed to a fixed point, and claims are evaluated over that closure.  The
implementation favours inspectable data structures and conservative failure
statuses over cleverness or implicit open-world assumptions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import operator
import time
from typing import Any, Iterable, Mapping

from .ir import (Aggregation, Atom, Bundle, Claim, Comparison, Constant,
                 OutputKind, RelationDecl, Rule, Variable, canonical_dict,
                 canonical_json)
from .output import output_triggered, relevant_evidence_ids
from .validation import ValidationError, ValidationResourceError, assert_valid
from .verdicts import (EvaluationBasis, EvaluationResult, OperationalStatus,
                       SemanticVerdict, verdict)


Row = tuple[Any, ...]
Environment = dict[str, Any]


@dataclass(frozen=True)
class ResourceLimits:
    """Hard bounds used by the evaluator.

    ``max_iterations`` is per stratum, while rows and provenance are bundle
    wide.  ``None`` means no bound; the defaults are deliberately finite.
    """

    max_iterations: int | None = 10_000
    max_derived_rows: int | None = 100_000
    max_provenance: int | None = 100_000
    max_alternatives_per_row: int | None = 64
    max_seconds: float | None = 30.0


@dataclass(frozen=True)
class Derivation:
    """A ground rule application, or a stable fact leaf."""

    relation: str
    row: Row
    rule: str = "fact"
    children: tuple["Derivation", ...] = ()
    leaf_id: str | None = None
    kind: str = "rule"
    alternatives: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "row", tuple(self.row))
        object.__setattr__(self, "children", tuple(self.children))

    # Proof trees are immutable and heavily shared (a closure row's proof
    # embeds its predecessor's).  Structural keys are therefore memoised per
    # node; without this every insert re-walks trees whose depth is the
    # recursion depth, which made transitive closure quadratic-times-depth.
    def _memo(self, name: str, compute):
        cached = self.__dict__.get(name)
        if cached is None:
            cached = compute()
            object.__setattr__(self, name, cached)
        return cached

    @property
    def leaves(self) -> tuple[str, ...]:
        def compute():
            if self.leaf_id is not None:
                return (self.leaf_id,)
            values: set[str] = set()
            for child in self.children:
                values.update(child.leaves)
            return tuple(sorted(values))
        return self._memo("_leaves", compute)

    @property
    def depth(self) -> int:
        return self._memo("_depth", lambda: 0 if not self.children else 1 + max(child.depth for child in self.children))

    def contains(self, relation: str, row: Row) -> bool:
        """Return whether this proof already depends on the same ground tuple."""
        return (self.relation == relation and self.row == tuple(row)) or any(
            child.contains(relation, row) for child in self.children
        )

    def signature(self) -> tuple[Any, ...]:
        """Compact structural identity used instead of serialising proof trees."""
        return self._memo("_signature", lambda: (
            self.relation, self.row, self.rule, self.kind, self.leaf_id,
            self.alternatives, tuple(child.signature() for child in self.children)))

    def _choice_structure(self) -> tuple[Any, ...]:
        """Return proof structure without mutable alternative-path counts."""
        return self._memo("_choice_structure_cache", lambda: (
            self.relation, self.row, self.rule, self.kind, self.leaf_id,
            tuple(child._choice_structure() for child in self.children)))

    def choice_key(self) -> tuple[Any, ...]:
        """Stable canonical ordering: shortest proof, then lexical structure.

        ``alternatives`` is intentionally absent.  It describes discarded OR
        paths, not the quality of this proof, and can grow while closure is
        being computed.  Letting it select a canonical proof would make an
        otherwise unchanged path churn as alternatives are discovered.
        """
        # Rows may contain ``_FrozenMap`` JSON values, whose inherited object
        # repr includes an allocation address.  Canonical JSON is the IR's
        # address-free lexical order and remains stable after strict reload.
        return self._memo("_choice_key", lambda: (self.depth, canonical_json(self._choice_structure())))

    def path_key(self) -> tuple[Any, ...]:
        """Identity of a ground proof path, independent of child snapshots.

        A retained child's canonical proof can improve after this node was first
        derived.  Treating that as the same path lets the fixed point replace
        the stale snapshot instead of retaining both versions as alternatives.
        """
        def compute():
            if self.kind == "fact":
                return (self.relation, self.row, self.kind, self.leaf_id)
            return (self.relation, self.row, self.rule, self.kind,
                    tuple((child.relation, child.row) for child in self.children))
        return self._memo("_path_key", compute)

    def as_dict(self) -> dict[str, Any]:
        return {
            "relation": self.relation,
            "row": list(self.row),
            "rule": self.rule,
            "kind": self.kind,
            "leaf_id": self.leaf_id,
            "alternatives": self.alternatives,
            "children": [child.as_dict() for child in self.children],
            "leaves": list(self.leaves),
        }


@dataclass(frozen=True)
class ClaimResult:
    index: int
    claim: Claim
    result: EvaluationResult

    @property
    def semantic(self) -> SemanticVerdict:
        return self.result.semantic

    @property
    def operational(self) -> OperationalStatus:
        return self.result.operational

    def as_dict(self) -> dict[str, Any]:
        return {"index": self.index, "claim": canonical_json(self.claim), "result": self.result.as_dict()}


@dataclass(frozen=True)
class EvaluationReport:
    relations: tuple[tuple[str, tuple[Row, ...]], ...]
    provenance: tuple[tuple[str, tuple[tuple[Row, tuple[Derivation, ...]], ...]], ...]
    claims: tuple[ClaimResult, ...]
    status: OperationalStatus = OperationalStatus.COMPLETE
    message: str = ""
    resources: tuple[tuple[str, int | float], ...] = ()

    def relation_rows(self, name: str) -> tuple[Row, ...]:
        return dict(self.relations).get(name, ())

    @property
    def results(self) -> tuple[ClaimResult, ...]:
        return self.claims

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "message": self.message,
            "resources": dict(self.resources),
            "relations": {name: [list(row) for row in rows] for name, rows in self.relations},
            "claims": [entry.as_dict() for entry in self.claims],
        }


class _LimitReached(RuntimeError):
    pass


class _UnsupportedConstruct(RuntimeError):
    pass


class _Engine:
    def __init__(self, bundle: Bundle, limits: ResourceLimits) -> None:
        self.bundle = bundle
        self.limits = limits
        self.started = time.monotonic()
        self.relations = {r.name: r for r in bundle.relations}
        self.rows: dict[str, set[Row]] = {name: set() for name in self.relations}
        # IR values are recursively frozen before evaluation, so even nested
        # JSON values are safe dictionary keys.  Indexing the values directly
        # also preserves Python equality semantics (for example, 0 and False)
        # used by unification instead of substituting a merely lexical key.
        self._row_indexes: dict[str, tuple[dict[Any, set[Row]], ...]] = {
            name: tuple({} for _ in declaration.columns)
            for name, declaration in self.relations.items()
        }
        self.proofs: dict[str, dict[Row, list[Derivation]]] = {name: {} for name in self.relations}
        # Candidate identities are bounded by max_alternatives_per_row.  Do not
        # retain an unbounded shadow graph merely to count discarded proofs.
        self.proof_path_candidates: dict[str, dict[Row, set[tuple[Any, ...]]]] = {
            name: {} for name in self.relations
        }
        self.derived_rows = 0
        self.provenance_count = 0
        self.unattributed_facts = 0
        self.discarded_alternatives = 0
        self.evidence_by_leaf = {record.id: record for record in bundle.evidence}
        # Private deterministic instrumentation for algorithmic benchmarks.
        self._candidate_rows_examined = 0
        self._indexed_atom_matches = 0
        self._full_scan_atom_matches = 0
        # Canonical-order caches.  Consumers still iterate rows in canonical
        # JSON order; these only avoid re-serialising every row on every
        # match.  Invalidation is by relation version, which advances only when
        # a new row is inserted.
        self._version: dict[str, int] = {name: 0 for name in self.relations}
        self._row_key: dict[str, dict[Row, str]] = {name: {} for name in self.relations}
        self._sorted_cache: dict[str, tuple[int, list[Row]]] = {}
        self._override_cache: dict[int, tuple[int, list[Row]]] = {}
        # Rows whose retained proofs or candidate set changed in the current
        # iteration.  A derivation is a pure function of its children's
        # retained proofs and candidate counts, so only derivations touching a
        # changed row can produce a different outcome in the next pass; that is
        # what lets the fixed point run semi-naively without losing canonical
        # proof propagation.
        self._changed: dict[str, set[Row]] = {name: set() for name in self.relations}
        self._signatures: dict[str, dict[Row, set[tuple[Any, ...]]]] = {name: {} for name in self.relations}

    def _canonical_key(self, relation: str, row: Row) -> str:
        keys = self._row_key[relation]
        key = keys.get(row)
        if key is None:
            key = canonical_json(row)
            keys[row] = key
        return key

    def _canonical_rows(self, relation: str) -> list[Row]:
        """Rows of ``relation`` in canonical JSON order, cached per version."""
        version = self._version[relation]
        cached = self._sorted_cache.get(relation)
        if cached is not None and cached[0] == version:
            return cached[1]
        ordered = sorted(self.rows[relation], key=lambda row: self._canonical_key(relation, row))
        self._sorted_cache[relation] = (version, ordered)
        return ordered

    def check_limits(self) -> None:
        if self.limits.max_seconds is not None and time.monotonic() - self.started > self.limits.max_seconds:
            raise _LimitReached("evaluation exceeded the time limit")
        if self.limits.max_derived_rows is not None and self.derived_rows > self.limits.max_derived_rows:
            raise _LimitReached("evaluation exceeded the derived-row limit")
        if self.limits.max_provenance is not None and self.provenance_count > self.limits.max_provenance:
            raise _LimitReached("evaluation exceeded the provenance limit")

    def add(self, relation: str, row: Row, proof: Derivation) -> bool:
        """Retain a canonical bounded set and report row *or proof* changes.

        A path is replaced only by a strictly better canonical proof.  In
        particular, a recursive snapshot cannot repeatedly replace its
        shallower base proof merely because the nested signature changed.
        """
        self.check_limits()
        row = tuple(row)
        if row in self.rows[relation]:
            paths = self.proofs[relation].setdefault(row, [])
            signature = proof.signature()
            signatures = self._signatures[relation].setdefault(row, set())
            if signature in signatures:
                return False
            if any(child.contains(relation, row) for child in proof.children):
                return False
            path_key = proof.path_key()
            same_path = next((i for i, existing in enumerate(paths)
                              if existing.path_key() == path_key), None)
            if same_path is not None:
                existing = paths[same_path]
                if proof.choice_key() >= existing.choice_key():
                    return False
                paths[same_path] = proof
                paths.sort(key=Derivation.choice_key)
                signatures.discard(existing.signature()); signatures.add(signature)
                self._changed[relation].add(row)
                return True

            # Bounds may truncate explanations only if semantic evaluation is
            # separately complete.  This kernel uses retained proofs to decide
            # claim eligibility, so crossing the per-row bound must fail closed
            # rather than silently discard the only unrevoked path.
            cap = self.limits.max_alternatives_per_row
            candidate_keys = self.proof_path_candidates[relation].setdefault(row, set())
            if cap is not None and len(candidate_keys) >= max(1, cap):
                self.discarded_alternatives += 1
                raise _LimitReached(
                    f"row {relation}{canonical_json(row)} exceeded the alternative provenance limit"
                )
            if self.limits.max_provenance is not None and self.provenance_count >= self.limits.max_provenance:
                raise _LimitReached("evaluation exceeded the provenance limit")
            candidate_keys.add(path_key)
            paths.append(proof)
            paths.sort(key=Derivation.choice_key)
            signatures.add(signature)
            self._changed[relation].add(row)
            self.provenance_count += 1
            return True
        if self.limits.max_provenance is not None and self.provenance_count >= self.limits.max_provenance:
            raise _LimitReached("evaluation exceeded the provenance limit")
        self.rows[relation].add(row)
        self._version[relation] += 1
        for position, value in enumerate(row):
            self._row_indexes[relation][position].setdefault(value, set()).add(row)
        self.proofs[relation][row] = [proof]
        self._signatures[relation][row] = {proof.signature()}
        self._changed[relation].add(row)
        self.proof_path_candidates[relation][row] = {proof.path_key()}
        self.derived_rows += 1
        self.provenance_count += 1
        self.check_limits()
        return True

    def seed(self) -> None:
        evidence: dict[tuple[str, str], list[str]] = {}
        for record in self.bundle.evidence:
            row = tuple(_ground_term(term, {}) for term in record.atom.terms)
            evidence.setdefault((record.atom.relation, canonical_json(row)), []).append(record.id)
        for ids in evidence.values():
            ids.sort()
        for fact in self.bundle.facts:
            if fact.negated:
                continue
            row = tuple(_ground_term(term, {}) for term in fact.terms)
            ids = evidence.get((fact.relation, canonical_json(row)), ())
            if not ids:
                self.unattributed_facts += 1
                ids = ("fact:" + fact.relation + ":" + canonical_json(row),)
            for leaf in ids:
                self.add(fact.relation, row, Derivation(fact.relation, row, leaf_id=leaf, kind="fact"))

    def strata(self) -> dict[str, int]:
        levels = {name: 0 for name in self.relations}
        # Validation has already rejected negative cycles.  Repeated relaxation
        # also handles dependencies that are not in the same positive SCC.
        for _ in range(max(1, len(levels) * len(levels))):
            changed = False
            for rule in self.bundle.rules:
                head = rule.head.relation
                for atom in rule.body:
                    if isinstance(atom, Atom):
                        proposed = levels[atom.relation] + (1 if atom.negated else 0)
                        if proposed > levels[head]:
                            levels[head] = proposed
                            changed = True
                if rule.aggregation is not None:
                    proposed = levels[rule.aggregation.relation]
                    if proposed > levels[head]:
                        levels[head] = proposed
                        changed = True
            if not changed:
                break
        return levels

    def run(self) -> None:
        self.seed()
        levels = self.strata()
        for level in range(max(levels.values(), default=0) + 1):
            rules = sorted((r for r in self.bundle.rules if levels[r.head.relation] == level),
                           key=lambda r: canonical_json(r))
            iterations = 0
            delta: Mapping[str, set[Row]] | None = None
            while True:
                iterations += 1
                if self.limits.max_iterations is not None and iterations > self.limits.max_iterations:
                    raise _LimitReached(f"stratum {level} exceeded the iteration limit")
                self._override_cache.clear()
                for name in self._changed: self._changed[name] = set()
                # The first pass is complete.  Later passes are semi-naive over
                # the rows whose retained proofs or candidates changed, which
                # includes canonical-proof replacements, so a proof that becomes
                # canonical after its row was discovered still propagates into
                # downstream proof trees exactly as a full pass would.
                for rule in rules:
                    for row, proof in self._derive_rule(rule, delta):
                        self.add(rule.head.relation, row, proof)
                self.check_limits()
                delta = {name: set(rows) for name, rows in self._changed.items() if rows}
                if not delta:
                    break

    def _derive_rule(self, rule: Rule, delta: Mapping[str, set[Row]] | None) -> Iterable[tuple[Row, Derivation]]:
        aggregate = rule.aggregation
        source_index = None
        if aggregate is not None:
            source_index = next((i for i, atom in enumerate(rule.body)
                                 if isinstance(atom, Atom) and not atom.negated and atom.relation == aggregate.relation), None)
        body = tuple(atom for i, atom in enumerate(rule.body) if i != source_index)
        # Aggregates are non-recursive by contract, so a complete pass is
        # sufficient and avoids accidentally treating a partial group as a
        # closed finite aggregate.
        if aggregate is not None:
            for env, children, body_alternatives in self._match_body(body, {}):
                source_atom = rule.body[source_index]  # type: ignore[index]
                source_decl = self.relations[aggregate.relation]
                grouped_names = set(aggregate.group_by) | set(source_decl.context_indices)
                source_names = [c.name for c in source_decl.columns]
                aggregate_env_seed = {
                    term.name: env[term.name]
                    for name, term in zip(source_names, source_atom.terms)
                    if name in grouped_names and isinstance(term, Variable) and term.name in env
                }
                candidates = list(self._match_atom(source_atom, aggregate_env_seed, positive_only=True))
                if not candidates and aggregate.operator in {"sum", "min", "max"}:
                    continue
                try:
                    index = source_names.index(aggregate.value_variable)
                except ValueError:
                    continue
                required_domain_candidates: list[tuple[Environment, list[Derivation]]] = []
                if aggregate.operator == "count":
                    result: Any = len(candidates)
                elif aggregate.operator == "sum":
                    result = sum(_ground_term(source_atom.terms[index], candidate_env) for candidate_env, _ in candidates)
                elif aggregate.operator == "min":
                    result = min(_ground_term(source_atom.terms[index], candidate_env) for candidate_env, _ in candidates)
                elif aggregate.operator == "max":
                    result = max(_ground_term(source_atom.terms[index], candidate_env) for candidate_env, _ in candidates)
                elif aggregate.operator == "any":
                    result = any(bool(_ground_term(source_atom.terms[index], candidate_env))
                                 for candidate_env, _ in candidates)
                elif aggregate.operator == "all":
                    domain_atom = next((item for item in rule.body if isinstance(item, Atom)
                                        and not item.negated and item.relation == aggregate.domain), None)
                    domain_decl = self.relations[aggregate.domain]
                    domain_names = [column.name for column in domain_decl.columns]
                    domain_seed = {
                        term.name: env[term.name]
                        for name, term in zip(domain_names, domain_atom.terms)
                        if name in grouped_names and isinstance(term, Variable) and term.name in env
                    } if domain_atom is not None else {}
                    domain_candidates = (list(self._match_atom(domain_atom, domain_seed, positive_only=True))
                                         if domain_atom is not None else [])
                    required_domain_candidates = domain_candidates
                    shared = [name for name in domain_names if name in source_names
                              and name != aggregate.value_variable]
                    def projected(atom: Atom, names: list[str], candidate_env: Environment) -> tuple[Any, ...]:
                        return tuple(_ground_term(atom.terms[names.index(name)], candidate_env) for name in shared)
                    source_keys = {projected(source_atom, source_names, candidate_env)
                                   for candidate_env, _ in candidates}
                    domain_keys = {projected(domain_atom, domain_names, candidate_env)
                                   for candidate_env, _ in domain_candidates} if domain_atom is not None else set()
                    result = (bool(domain_keys) and source_keys == domain_keys
                              and all(bool(_ground_term(source_atom.terms[index], candidate_env))
                                      for candidate_env, _ in candidates))
                else:
                    raise _UnsupportedConstruct(f"unsupported aggregate: {aggregate.operator}")
                aggregate_env = dict(env)
                aggregate_env[aggregate.name] = result
                # ``value_variable`` is the aggregate result binding for every
                # operator.  The source atom was removed from the ordinary
                # body, so count cannot inherit this value from a source row.
                aggregate_env[aggregate.value_variable] = result
                value_in_head = any(isinstance(term, Variable) and term.name == aggregate.value_variable
                                    for term in rule.head.terms)
                # A Boolean aggregate whose value is absent from the head is a
                # guard, not an instruction to emit the same head for false.
                if aggregate.operator in {"any", "all"} and not value_in_head and not result:
                    continue
                row = tuple(_ground_term(t, aggregate_env) for t in rule.head.terms)
                proof_children = list(children)
                alternatives = body_alternatives
                child_keys = {(child.relation, child.row) for child in proof_children}
                for candidate_env, proofs in required_domain_candidates:
                    if not proofs:
                        continue
                    domain_row = tuple(_ground_term(term, candidate_env)
                                       for term in domain_atom.terms)
                    domain_key = (domain_decl.name, domain_row)
                    if domain_key not in child_keys:
                        proof_children.append(proofs[0])
                        child_keys.add(domain_key)
                        alternatives += max(0, len(self.proof_path_candidates[domain_decl.name]
                                                    .get(domain_row, ())) - 1)
                for candidate_env, proofs in candidates:
                    if proofs:
                        proof_children.append(proofs[0])
                        candidate_row = tuple(_ground_term(term, candidate_env) for term in source_atom.terms)
                        alternatives += max(0, len(self.proof_path_candidates[source_decl.name]
                                                    .get(candidate_row, ())) - 1)
                yield row, Derivation(rule.head.relation, row, rule.name or "rule", tuple(proof_children),
                                      kind="aggregate", alternatives=alternatives)
            return

        if delta is None:
            for env, children, alternatives in self._match_body(rule.body, {}):
                row = tuple(_ground_term(t, env) for t in rule.head.terms)
                yield row, Derivation(rule.head.relation, row, rule.name or "rule", tuple(children),
                                      alternatives=alternatives)
            return

        # Semi-naive delta step: each positive body atom is used once as the
        # pivot against only rows added in the previous iteration.  Unioning
        # pivots prevents old tuples from redoing the full Cartesian product.
        pivots = [i for i, atom in enumerate(rule.body)
                  if isinstance(atom, Atom) and not atom.negated and delta.get(atom.relation)]
        seen: set[Any] = set()
        for pivot in pivots:
            overrides = {pivot: delta[rule.body[pivot].relation]}  # type: ignore[index]
            for env, children, alternatives in self._match_body(rule.body, {}, overrides):
                row = tuple(_ground_term(t, env) for t in rule.head.terms)
                proof = Derivation(rule.head.relation, row, rule.name or "rule", tuple(children),
                                   alternatives=alternatives)
                try:
                    key: Any = (row, proof.signature())
                    hash(key)
                except TypeError:
                    key = repr((row, proof.signature()))
                if key not in seen:
                    seen.add(key)
                    yield row, proof

    def _match_body(self, body: Iterable[Atom | Comparison], env: Environment,
                    overrides: Mapping[int, set[Row]] | None = None,
                    offset: int = 0) -> Iterable[tuple[Environment, list[Derivation], int]]:
        items = tuple(body)
        if not items:
            yield dict(env), [], 0
            return
        first, rest = items[0], items[1:]
        first_index = offset
        if isinstance(first, Comparison):
            if _compare(first, env):
                yield from self._match_body(rest, env, overrides, offset + 1)
            return
        if first.negated:
            matches = list(self._match_atom(first, env, positive_only=True))
            if not matches:
                yield from self._match_body(rest, env, overrides, offset + 1)
            return
        rows_override = overrides.get(first_index) if overrides else None
        for next_env, proofs in self._match_atom(first, env, positive_only=True, rows_override=rows_override):
            # Alternative proofs are OR paths.  One deterministic proof is an
            # AND child; unioning all of them would assert that every producer
            # and every route was required.
            selected = list(proofs[:1])
            matched_row = tuple(_ground_term(term, next_env) for term in first.terms)
            discarded = max(0, len(self.proof_path_candidates[first.relation]
                                    .get(matched_row, ())) - 1)
            for final_env, rest_proofs, rest_alternatives in self._match_body(rest, next_env, overrides, offset + 1):
                yield final_env, selected + rest_proofs, discarded + rest_alternatives

    def _candidate_rows(self, atom: Atom, env: Environment,
                        rows_override: set[Row] | None) -> set[Row]:
        """Return the indexed candidate intersection, or the full scan set."""
        constraints = []
        for position, term in enumerate(atom.terms):
            if isinstance(term, Constant):
                constraints.append((position, term.value))
            elif isinstance(term, Variable) and term.name in env:
                constraints.append((position, env[term.name]))

        if constraints:
            self._indexed_atom_matches += 1
            rows: set[Row] | None = None
            indexes = self._row_indexes[atom.relation]
            for position, value in constraints:
                candidates = indexes[position].get(value, set())
                rows = set(candidates) if rows is None else rows.intersection(candidates)
                if not rows:
                    break
            rows = rows or set()
            if rows_override is not None:
                rows.intersection_update(rows_override)
        else:
            self._full_scan_atom_matches += 1
            rows = self.rows[atom.relation] if rows_override is None else rows_override
        return rows

    def _match_atom(self, atom: Atom, env: Environment, *, positive_only: bool,
                    rows_override: set[Row] | None = None) -> Iterable[tuple[Environment, list[Derivation]]]:
        del positive_only  # reserved for the future explicit negative relation form
        decl = self.relations[atom.relation]
        rows = self._candidate_rows(atom, env, rows_override)
        if rows is self.rows[atom.relation]:
            ordered: Iterable[Row] = self._canonical_rows(atom.relation)
        elif rows_override is not None and rows is rows_override:
            cached = self._override_cache.get(id(rows_override))
            if cached is None or cached[0] != len(rows_override):
                ordered = sorted(rows_override, key=lambda row: self._canonical_key(atom.relation, row))
                self._override_cache[id(rows_override)] = (len(rows_override), list(ordered))
            else:
                ordered = cached[1]
        else:
            ordered = sorted(rows, key=lambda row: self._canonical_key(atom.relation, row))
        for row in ordered:
            self._candidate_rows_examined += 1
            next_env = dict(env)
            ok = True
            for term, value in zip(atom.terms, row):
                if isinstance(term, Variable):
                    if term.name in next_env and next_env[term.name] != value:
                        ok = False
                        break
                    next_env[term.name] = value
                elif _ground_term(term, next_env) != value:
                    ok = False
                    break
            if ok:
                proofs = self.proofs[decl.name].get(row, ())
                yield next_env, list(proofs)

    def _claim_values(self, claim: Claim) -> dict[str, Any]:
        decl = self.relations[claim.relation]
        values = dict(claim.context.as_dict())
        for column, term in zip(decl.columns, claim.terms):
            if isinstance(term, Constant):
                values[column.name] = term.value
        return values

    def _source_matches(
            self, claim: Claim, relation_name: str,
            bindings: Iterable[tuple[str, str]] = (),
            variable_values: Mapping[str, Any] | None = None,
    ) -> list[tuple[Row, list[Derivation], Environment]]:
        """Match one projection while preserving claim-variable bindings.

        The returned environment can be threaded through the next support
        mapping.  This is the relational join: each mapping does not get a
        fresh interpretation of a variable-valued claim.
        """
        decl = self.relations[relation_name]
        claim_decl = self.relations[claim.relation]
        claim_names = [column.name for column in claim_decl.columns]
        claim_values = self._claim_values(claim)
        projection = tuple(bindings)
        result = []
        names = [column.name for column in decl.columns]
        for row in self._canonical_rows(relation_name):
            row_values = dict(zip(names, row))
            next_variables = dict(variable_values or {})
            matches_projection = True
            for left, right in projection:
                if right not in row_values or left not in claim_names:
                    matches_projection = False
                    break
                value = row_values[right]
                term = claim.terms[claim_names.index(left)]
                if isinstance(term, Constant) and value != term.value:
                    matches_projection = False
                    break
                if isinstance(term, Variable):
                    previous = next_variables.setdefault(term.name, value)
                    if previous != value:
                        matches_projection = False
                        break
                if left in claim_values and value != claim_values[left]:
                    matches_projection = False
                    break
            if not matches_projection:
                continue
            # Context names shared by the claim and source are always scoped,
            # even when a mapping omitted a redundant projection.
            if any(row_values[name] != value for name, value in claim.context.as_dict().items()
                   if name in row_values):
                continue
            result.append((row, list(self.proofs[relation_name].get(row, ())),
                           next_variables))
        return result

    def _first_allowed(self, proofs: Iterable[Derivation],
                       blocked: set[str]) -> Derivation | None:
        return next((resolved for proof in proofs
                     if (resolved := self._allowed_proof(proof, blocked)) is not None), None)

    def _mapping_conjunction(
            self, claim: Claim, mappings: tuple[Any, ...], blocked: set[str],
            position: int = 0, variable_values: Mapping[str, Any] | None = None,
    ) -> tuple[Derivation, ...] | None:
        """Return the first canonical, jointly unified support mapping path."""
        if position == len(mappings):
            return ()
        mapping = mappings[position]
        for _, proofs, next_variables in self._source_matches(
                claim, mapping.evidence_relation, mapping.bindings,
                variable_values):
            selected = self._first_allowed(proofs, blocked)
            if selected is None:
                continue
            rest = self._mapping_conjunction(
                claim, mappings, blocked, position + 1, next_variables)
            if rest is not None:
                return (selected, *rest)
        return None

    def _declared_missing_premises(
            self, claim: Claim, fallback: tuple[Any, ...],
            claim_state: SemanticVerdict = SemanticVerdict.UNRESOLVED,
    ) -> tuple[Any, ...]:
        """Materialize only missing-premise templates whose triggers hold.

        Output declarations describe diagnostics; their mere presence cannot
        suppress the evaluator's fallback.  Evidence conditions refer to the
        bundle's reviewed Evidence identities, not producer verdict metadata.
        """
        values = self._claim_values(claim)
        active_evidence = relevant_evidence_ids(
            self.bundle, claim.id, set(self.evidence_by_leaf),
            scoped_claim=claim)
        evidence_values: dict[str, dict[str, Any]] = {}
        for record in self.bundle.evidence:
            declaration = self.relations.get(record.atom.relation)
            row_values = {"id": record.id}
            if declaration is not None:
                row_values.update(
                    (column.name, term.value)
                    for column, term in zip(declaration.columns, record.atom.terms)
                    if isinstance(term, Constant))
            evidence_values[record.id] = row_values
        declared = []
        for output in self.bundle.outputs:
            if (output.claim_id != claim.id
                    or output.kind != OutputKind.MISSING_PREMISE
                    or not output_triggered(
                        output, active_evidence,
                        (claim_state.value, "underived"), bundle=self.bundle,
                        scoped_claim=claim)):
                continue
            item: dict[str, Any] = {"relation": output.relation}
            complete = True
            for name, template in output.fields:
                if template.source == "constant":
                    item[name] = canonical_dict(template.value)
                elif template.source == "claim" and template.column in values:
                    item[name] = canonical_dict(values[template.column])
                elif template.source == "evidence":
                    evidence_id = template.evidence_id or output.evidence_id or ""
                    source_values = evidence_values.get(evidence_id, {})
                    if evidence_id not in active_evidence or template.column not in source_values:
                        complete = False
                        break
                    item[name] = canonical_dict(source_values[template.column])
                else:
                    complete = False
                    break
            if complete:
                declared.append(item)
        if not declared:
            return fallback
        unique = {canonical_json(item): item for item in declared}
        return tuple(unique[key] for key in sorted(unique))

    def _diagnostic_rows(self, claim: Claim, diagnostic: Any) -> list[Row]:
        decl = self.relations[diagnostic.trigger_relation]
        names = [column.name for column in decl.columns]
        claim_values = self._claim_values(claim)
        predicate = dict(diagnostic.predicate)
        scoped_names = set(diagnostic.context_indices) | (set(names) & set(claim_values))
        result = []
        for row in self._canonical_rows(diagnostic.trigger_relation):
            values = dict(zip(names, row))
            if any(name not in claim_values or values[name] != claim_values[name]
                   for name in scoped_names):
                continue
            if predicate:
                actual = values[predicate["column"]]
                expected = predicate.get("value")
                if not {"=": actual == expected, "!=": actual != expected,
                        "in": actual in expected if isinstance(expected, (list, tuple)) else False,
                        "not-in": actual not in expected if isinstance(expected, (list, tuple)) else False,
                        "exists": actual is not None}.get(predicate["operator"], False):
                    continue
            result.append(row)
        return result

    def _diagnostic_active(self, claim: Claim, diagnostic: Any) -> bool:
        matches = self._diagnostic_rows(claim, diagnostic)
        return (not matches) if diagnostic.when_missing else bool(matches)

    def _rule_may_head_claim(self, rule: Rule, claim: Claim) -> bool:
        """Conservatively decide whether a rule can derive this claim instance."""
        if rule.head.relation != claim.relation:
            return False
        decl = self.relations[claim.relation]
        known = self._claim_values(claim)
        variables: dict[str, Any] = {}
        for column, head_term in zip(decl.columns, rule.head.terms):
            if column.name not in known:
                continue
            expected = known[column.name]
            if isinstance(head_term, Constant) and head_term.value != expected:
                return False
            if isinstance(head_term, Variable):
                previous = variables.setdefault(head_term.name, expected)
                if previous != expected:
                    return False
        return True

    def _blocked_evidence(self, claim: Claim) -> set[str]:
        blocked_rows: set[tuple[str, str]] = set()
        for diagnostic in self.bundle.diagnostics:
            if (diagnostic.claim_id != claim.id
                    or diagnostic.effect.value not in {"forbidden", "refutation"}
                    or self.relations[diagnostic.trigger_relation].modality.value != "assumption"
                    or diagnostic.when_missing):
                continue
            blocked_rows.update((diagnostic.trigger_relation, canonical_json(row))
                                for row in self._diagnostic_rows(claim, diagnostic))
        return {record.id for record in self.bundle.evidence
                if (record.atom.relation,
                    canonical_json(tuple(_ground_term(term, {}) for term in record.atom.terms))) in blocked_rows}

    def _allowed_proof(self, proof: Derivation, blocked: set[str],
                       visiting: set[tuple[str, Row]] | None = None) -> Derivation | None:
        """Resolve each AND child through its first claim-eligible OR path."""
        visiting = set() if visiting is None else set(visiting)
        key = (proof.relation, proof.row)
        if key in visiting:
            return None
        visiting.add(key)
        if proof.leaf_id is not None:
            pending = [proof.leaf_id]; seen = set(pending)
            while pending:
                leaf = pending.pop()
                record = self.evidence_by_leaf.get(leaf)
                if record is None:
                    continue
                for dependency in record.depends_on:
                    if dependency not in seen:
                        seen.add(dependency); pending.append(dependency)
            return None if seen & blocked else proof
        children = []
        for child in proof.children:
            resolved = None
            for candidate in self.proofs[child.relation].get(child.row, (child,)):
                resolved = self._allowed_proof(candidate, blocked, visiting)
                if resolved is not None:
                    break
            if resolved is None:
                return None
            children.append(resolved)
        return Derivation(proof.relation, proof.row, proof.rule, tuple(children),
                          proof.leaf_id, proof.kind, proof.alternatives)

    def _evaluate_exists(self, claim: Claim) -> EvaluationResult:
        decl = self.relations[claim.relation]
        blocked = self._blocked_evidence(claim)
        direct_proof = None
        for _, proofs, _ in self._source_matches(
                claim, claim.relation,
                tuple((column.name, column.name) for column in decl.columns)):
            direct_proof = self._first_allowed(proofs, blocked)
            if direct_proof is not None:
                break
        direct_payload = direct_proof.leaves if direct_proof is not None else ()
        support_payload = direct_payload if decl.polarity.value == "positive" else ()
        refutation_payload = direct_payload if decl.polarity.value == "negative" else ()

        support_mappings = tuple(
            mapping for mapping in self.bundle.mappings
            if mapping.claim_id == claim.id and mapping.effect.value == "support")
        mapped_path = (self._mapping_conjunction(claim, support_mappings, blocked)
                       if support_mappings else None)
        # Explicit support mappings are conjunctive.  Datalog derivations are
        # preferred because they state sufficiency and retain AND provenance;
        # mappings supply signed support when no claim tuple was derived.
        has_claim_rules = any(self._rule_may_head_claim(rule, claim)
                              for rule in self.bundle.rules)
        if (not support_payload and mapped_path is not None
                and (not has_claim_rules or bool(direct_payload))):
            support_payload = tuple(sorted({leaf for proof in mapped_path
                                            for leaf in proof.leaves}))

        if not refutation_payload:
            for mapping in (mapping for mapping in self.bundle.mappings
                            if mapping.claim_id == claim.id
                            and mapping.effect.value == "refutation"):
                mapped_proof = None
                for _, proofs, _ in self._source_matches(
                        claim, mapping.evidence_relation, mapping.bindings):
                    mapped_proof = self._first_allowed(proofs, blocked)
                    if mapped_proof is not None:
                        break
                if mapped_proof is not None:
                    refutation_payload = mapped_proof.leaves
                    break
        refutation_payload = tuple(sorted(set(refutation_payload)))
        support_payload = tuple(sorted(set(support_payload)))

        status = OperationalStatus.COMPLETE
        for diagnostic in self.bundle.diagnostics:
            if diagnostic.claim_id != claim.id or diagnostic.operational_status == "complete":
                continue
            if not self._diagnostic_active(claim, diagnostic):
                continue
            trigger = self.relations[diagnostic.trigger_relation]
            # Rejected assumptions make dependent proof paths ineligible. An
            # independent surviving proof is not stale merely because another
            # assumption path was revoked. Producer/runtime scope diagnostics,
            # however, remain operationally relevant even when support exists.
            if trigger.modality.value == "assumption":
                if diagnostic.effect.value == "forbidden":
                    continue
                if support_payload:
                    continue
            status = OperationalStatus(diagnostic.operational_status)
            break
        matched = bool(support_payload or refutation_payload)
        semantic = verdict(bool(support_payload), bool(refutation_payload))
        missing = () if matched else self._declared_missing_premises(
            claim, (f"claim:{claim.relation}:{canonical_json(claim.context.as_dict())}",),
            semantic)
        return EvaluationResult(semantic, status,
                                EvaluationBasis.DERIVATIONAL, support=support_payload,
                                refutation=refutation_payload, missing_premises=missing)

    def evaluate_claim(self, index: int, claim: Claim) -> ClaimResult:
        if claim.quantifier.value != "forall":
            return ClaimResult(index, claim, self._evaluate_exists(claim))
        domain = self.relations[claim.domain]  # validator guarantees this
        domain_names = [c.name for c in domain.columns]
        claim_values = self._claim_values(claim)
        # Scope the finite domain by every named claim constant it can carry,
        # not merely context-marked columns.  Otherwise another capability in
        # the same tenant/run can enlarge this universal's member set.
        shared_values = {name: value for name, value in claim_values.items()
                         if name in domain_names}
        domain_rows = [row for row in self._canonical_rows(domain.name)
                       if all(dict(zip(domain_names, row))[name] == value
                              for name, value in shared_values.items())]
        blocked = self._blocked_evidence(claim)
        closure_decls = [decl for decl in self.relations.values()
                         if decl.modality.value == "completeness"
                         and decl.completes == domain.name
                         and decl.context_indices == domain.context_indices
                         and tuple(column.name for column in decl.columns)
                         == domain.context_indices]
        closure_proof = None
        for closure in closure_decls:
            names = [column.name for column in closure.columns]
            for row in self._canonical_rows(closure.name):
                values = dict(zip(names, row))
                if any(values[name] != shared_values[name]
                       for name in names if name in shared_values):
                    continue
                for proof in self.proofs[closure.name].get(row, ()):
                    closure_proof = self._allowed_proof(proof, blocked)
                    if closure_proof is not None:
                        break
                if closure_proof is not None:
                    break
            if closure_proof is not None:
                break
        closure_available = closure_proof is not None
        if not closure_available:
            result = EvaluationResult(
                SemanticVerdict.UNRESOLVED, OperationalStatus.COMPLETE,
                EvaluationBasis.BOUNDED_HISTORY_MODEL,
                missing_premises=self._declared_missing_premises(
                    claim, (f"closure:{domain.name}",)),
                message="universal domain has no claim-eligible completeness witness")
            return ClaimResult(index, claim, result)
        if not domain_rows:
            result = EvaluationResult(SemanticVerdict.UNRESOLVED, OperationalStatus.INCONSISTENT_PREMISES,
                                      EvaluationBasis.BOUNDED_HISTORY_MODEL,
                                      missing_premises=self._declared_missing_premises(
                                          claim, (f"domain:{domain.name}",)),
                                      message="universal domain is empty in claim context")
            return ClaimResult(index, claim, result)
        domain_proofs = []
        for drow in domain_rows:
            proof = next((resolved for candidate in self.proofs[domain.name].get(drow, ())
                          if (resolved := self._allowed_proof(candidate, blocked)) is not None), None)
            if proof is None:
                result = EvaluationResult(
                    SemanticVerdict.UNRESOLVED, OperationalStatus.COMPLETE,
                    EvaluationBasis.BOUNDED_HISTORY_MODEL,
                    missing_premises=(f"domain-evidence:{domain.name}:{canonical_json(drow)}",),
                    message="universal domain member has no claim-eligible provenance")
                return ClaimResult(index, claim, result)
            domain_proofs.append(proof)
        subresults = []
        claim_decl = self.relations[claim.relation]
        for drow in domain_rows:
            env = dict(zip(domain_names, drow))
            subclaim_terms = tuple(Constant(env[t.name], claim_decl.columns[position].type)
                                   if isinstance(t, Variable) and t.name in env else t
                                   for position, t in enumerate(claim.terms))
            subclaim = Claim(claim.relation, subclaim_terms, claim.context, "exists", None, claim.id)
            subresults.append(self._evaluate_exists(subclaim))
        status = next((r.operational for r in subresults
                       if r.operational != OperationalStatus.COMPLETE), OperationalStatus.COMPLETE)
        all_supported = all(r.semantic in {SemanticVerdict.SUPPORTED, SemanticVerdict.CONFLICTING}
                            for r in subresults)
        any_refuted = any(r.semantic in {SemanticVerdict.REFUTED, SemanticVerdict.CONFLICTING}
                          for r in subresults)
        support = ()
        if all_supported:
            support = tuple(sorted({
                *(leaf for proof in (*domain_proofs, closure_proof) for leaf in proof.leaves),
                *(leaf for subresult in subresults for leaf in subresult.support),
            }))
        refutation_leaves: set[str] = set()
        if any_refuted:
            refutation_leaves.update(closure_proof.leaves)
            for domain_proof, subresult in zip(domain_proofs, subresults):
                if subresult.semantic not in {
                        SemanticVerdict.REFUTED, SemanticVerdict.CONFLICTING}:
                    continue
                refutation_leaves.update(domain_proof.leaves)
                refutation_leaves.update(subresult.refutation)
        subresult_missing = tuple(item for subresult in subresults
                                  for item in subresult.missing_premises)
        semantic = verdict(all_supported, any_refuted)
        # Missing-premise outputs were evaluated against each grounded member
        # above.  Re-evaluating them against the open FORALL claim would let
        # evidence for one member trigger a template for another member.
        if all_supported or any_refuted:
            missing = ()
        else:
            unique_missing = {canonical_json(item): item
                              for item in subresult_missing}
            missing = tuple(unique_missing[key] for key in sorted(unique_missing))
        result = EvaluationResult(semantic, status,
                                  EvaluationBasis.BOUNDED_HISTORY_MODEL,
                                  support=support,
                                  refutation=tuple(sorted(refutation_leaves)),
                                  missing_premises=missing)
        return ClaimResult(index, claim, result)


def _proof_payload(proofs: Iterable[Derivation], include: bool) -> tuple[Any, ...]:
    if not include:
        return ()
    seen: dict[str, Derivation] = {}
    for proof in proofs:
        seen.update({leaf: proof for leaf in proof.leaves})
    return tuple(sorted(seen, key=str))


def _ground_term(term: Any, env: Mapping[str, Any]) -> Any:
    if isinstance(term, Variable):
        return env.get(term.name)
    if isinstance(term, Constant):
        return term.value
    return None


def _compare(comparison: Comparison, env: Mapping[str, Any]) -> bool:
    left = _ground_term(comparison.left, env)
    right = _ground_term(comparison.right, env)
    if left is None or right is None:
        return False
    operations = {"=": operator.eq, "!=": operator.ne, "<": operator.lt,
                  "<=": operator.le, ">": operator.gt, ">=": operator.ge}
    try:
        return bool(operations[comparison.operator](left, right))
    except (KeyError, TypeError):
        return False


def evaluate(bundle: Bundle, limits: ResourceLimits | None = None) -> EvaluationReport:
    """Evaluate a validated bundle and return deterministic closure/results."""
    limits = limits or ResourceLimits()
    try:
        assert_valid(bundle)
        engine = _Engine(bundle, limits)
        engine.run()
        claims = tuple(engine.evaluate_claim(i, claim) for i, claim in enumerate(bundle.claims))
        relations = tuple((name, tuple(engine._canonical_rows(name))) for name in sorted(engine.rows))
        provenance = tuple((name, tuple((row, tuple(proofs)) for row, proofs in sorted(data.items(), key=lambda x: canonical_json(x[0]))))
                           for name, data in sorted(engine.proofs.items()))
        # Runtime is intentionally not part of the canonical result: it would
        # make shuffle/differential comparisons nondeterministic.  Callers can
        # measure wall time around evaluate() when benchmarking.
        resources = (("derived_rows", engine.derived_rows),
                     ("provenance_nodes", engine.provenance_count),
                     ("unattributed_facts", engine.unattributed_facts),
                     ("discarded_alternatives", engine.discarded_alternatives))
        return EvaluationReport(relations, provenance, claims, resources=resources)
    except ValidationError as exc:
        claims = tuple(ClaimResult(i, claim, EvaluationResult(SemanticVerdict.UNRESOLVED,
                                                               OperationalStatus.INVALID_INPUT,
                                                               message=str(exc))) for i, claim in enumerate(bundle.claims))
        return EvaluationReport((), (), claims, OperationalStatus.INVALID_INPUT, str(exc))
    except _LimitReached as exc:
        claims = tuple(ClaimResult(i, claim, EvaluationResult(SemanticVerdict.UNRESOLVED,
                                                               OperationalStatus.RESOURCE_EXHAUSTED,
                                                               message=str(exc))) for i, claim in enumerate(bundle.claims))
        return EvaluationReport((), (), claims, OperationalStatus.RESOURCE_EXHAUSTED, str(exc))
    except _UnsupportedConstruct as exc:
        claims = tuple(ClaimResult(i, claim, EvaluationResult(SemanticVerdict.UNRESOLVED,
                                                               OperationalStatus.UNSUPPORTED_CONSTRUCT,
                                                               message=str(exc))) for i, claim in enumerate(bundle.claims))
        return EvaluationReport((), (), claims, OperationalStatus.UNSUPPORTED_CONSTRUCT, str(exc))
    except (RecursionError, ValidationResourceError) as exc:
        # Deep producer input must cross the evaluator boundary as a named
        # operational failure, never leak a Python implementation exception.
        message = str(exc) or "evaluation exceeded the proof recursion limit"
        claims = tuple(ClaimResult(i, claim, EvaluationResult(
            SemanticVerdict.UNRESOLVED, OperationalStatus.RESOURCE_EXHAUSTED,
            message=message)) for i, claim in enumerate(bundle.claims))
        return EvaluationReport((), (), claims,
                                OperationalStatus.RESOURCE_EXHAUSTED, message)


evaluate_bundle = evaluate


class PythonEvaluator:
    """Object-oriented facade useful to callers that retain configuration."""

    def __init__(self, limits: ResourceLimits | None = None) -> None:
        self.limits = limits or ResourceLimits()

    def evaluate(self, bundle: Bundle) -> EvaluationReport:
        return evaluate(bundle, self.limits)
