"""Engine-independent ground certificates for static claim rows (section 29).

Soufflé computes relations only, and the Python kernel's proof trees are its
own.  This module re-derives a *ground certificate* for one row from nothing
but the bundle (declarations, rules, evidence) and an engine's relation
closure, so the same extractor over either engine's rows yields the same
artifact and the Stage C checker can replay it without search.

Derivation strategy
-------------------
* A primitive row maps to the evidence ids that attest it (a leaf).
* A non-recursive derived row is unified against the rules for its relation
  in canonical order (``canonical_json`` of the rule, the order the Python
  kernel uses); each body is instantiated over the relation rows in canonical
  row order, negated atoms are checked absent, comparisons are re-evaluated,
  and the first instantiation whose premises all certify is kept.
* A row of a recursive relation is certified by a breadth-first search
  backwards over the linear recursive rules of its SCC: predecessor rows are
  taken from the closure, the search stops at the first row a non-recursive
  ("base") rule derives, and the result is a chain of exactly ``k`` step
  applications on top of one base application, ``k`` being the shortest such
  chain (``fixpoint.distances`` semantics).  Ties are broken by canonical row
  and rule order, so the choice is deterministic for a given closure.

Bounds are hard: ``max_depth`` (default 64, the same bound as
``fixpoint.distances(max_hops)``) limits recursive steps and nesting depth,
``max_nodes`` (default 10_000) limits certificate nodes plus rows visited by
the search.  Exceeding either yields ``truncated: True`` with the reason, never
a partial certificate presented as complete.  A row that is *in* the closure
but that no rule derives from the closure is a kernel inconsistency and
raises ``CertificateError``; it is a finding, not something to paper over.

``recheck`` walks a certificate without search: every leaf must be attested by
the named evidence for exactly that row, every rule application must unify
head and premises under one environment with comparisons holding, and negated
atoms are verified absent when a closure is supplied (otherwise reported as
unchecked).  A substituted leaf, row or rule is therefore detected.
"""
from __future__ import annotations

from dataclasses import dataclass
import operator
from typing import Any, Iterable, Mapping

from ..ir import (Atom, Bundle, Claim, Comparison, Constant, Rule, Variable,
                  canonical_dict, canonical_json, digest)

CERTIFICATE_VERSION = "capcov-static-certificate-v1"
DEFAULT_MAX_DEPTH = 64
DEFAULT_MAX_NODES = 10_000

_OPERATORS = {"=": operator.eq, "!=": operator.ne, "<": operator.lt,
              "<=": operator.le, ">": operator.gt, ">=": operator.ge}


class CertificateError(ValueError):
    """The closure and the rules disagree, or the input cannot be certified."""


class _Truncated(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class RecheckResult:
    ok: bool
    problems: tuple[str, ...] = ()
    unchecked: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# value helpers


def _plain(value: Any) -> Any:
    return canonical_dict(value)


def _key(value: Any) -> str:
    return canonical_json(value)


def _same(left: Any, right: Any) -> bool:
    # Canonical JSON equality: distinguishes True from 1 and "1" from 1, which
    # Python equality does not, and survives frozen-vs-plain JSON values.
    return _key(left) == _key(right)


def _ground(term: Any, env: Mapping[str, Any]) -> Any:
    if isinstance(term, Variable):
        return env.get(term.name)
    if isinstance(term, Constant):
        return _plain(term.value)
    return None


def _unify(terms: Iterable[Any], row: Iterable[Any], env: Mapping[str, Any]) -> dict[str, Any] | None:
    terms = tuple(terms)
    row = tuple(row)
    if len(terms) != len(row):
        return None
    out = dict(env)
    for term, value in zip(terms, row):
        if isinstance(term, Variable):
            if term.name in out:
                if not _same(out[term.name], value):
                    return None
            else:
                out[term.name] = value
        elif isinstance(term, Constant):
            if not _same(term.value, value):
                return None
        else:
            return None
    return out


def _compare(comparison: Comparison, env: Mapping[str, Any]) -> bool:
    left = _ground(comparison.left, env)
    right = _ground(comparison.right, env)
    if left is None or right is None:
        return False
    try:
        return bool(_OPERATORS[comparison.operator](left, right))
    except (KeyError, TypeError):
        return False


def rules_digest(bundle: Bundle) -> str:
    """Digest of the bundle's (canonically ordered) rules alone."""
    return digest([canonical_dict(rule) for rule in bundle.rules])


def normalize_relations(relations: Any) -> dict[str, list[tuple[Any, ...]]]:
    """``{relation: rows}`` from a mapping or ``KernelReport.relations`` pairs.

    Rows come back as plain tuples (frozen JSON values unwrapped) in canonical
    order, deduplicated -- the same normalization both kernels apply before the
    differential compares them.
    """
    items = relations.items() if isinstance(relations, Mapping) else relations
    out: dict[str, list[tuple[Any, ...]]] = {}
    for name, rows in items:
        unique = {_key(tuple(_plain(v) for v in row)): tuple(_plain(v) for v in row) for row in rows}
        out[name] = [unique[key] for key in sorted(unique)]
    return out


# ---------------------------------------------------------------------------
# recursion structure


def _positive_dependencies(rules: Iterable[Rule]) -> dict[str, set[str]]:
    edges: dict[str, set[str]] = {}
    for rule in rules:
        targets = edges.setdefault(rule.head.relation, set())
        for item in rule.body:
            if isinstance(item, Atom) and not item.negated:
                targets.add(item.relation)
                edges.setdefault(item.relation, set())
    return edges


def _components(edges: Mapping[str, set[str]]) -> dict[str, int]:
    """Tarjan's SCC, iterative; returns relation -> component id."""
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    low: dict[str, int] = {}
    result: dict[str, int] = {}
    for root in sorted(edges):
        if root in indices:
            continue
        frames = [(root, iter(sorted(edges[root])))]
        indices[root] = low[root] = index; index += 1
        stack.append(root); on_stack.add(root)
        while frames:
            node, children = frames[-1]
            advanced = False
            for child in children:
                if child not in indices:
                    indices[child] = low[child] = index; index += 1
                    stack.append(child); on_stack.add(child)
                    frames.append((child, iter(sorted(edges[child]))))
                    advanced = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], indices[child])
            if advanced:
                continue
            frames.pop()
            if frames:
                parent = frames[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == indices[node]:
                component = len(result)
                while True:
                    item = stack.pop(); on_stack.discard(item)
                    result[item] = component
                    if item == node:
                        break
    return result


# ---------------------------------------------------------------------------
# the certifier


class _Certifier:
    def __init__(self, bundle: Bundle, relations: Any, max_depth: int, max_nodes: int) -> None:
        if max_depth < 1 or max_nodes < 1:
            raise ValueError("max_depth and max_nodes must be positive")
        self.bundle = bundle
        self.max_depth = max_depth
        self.max_nodes = max_nodes
        self.decls = {decl.name: decl for decl in bundle.relations}
        rows = normalize_relations(relations)
        self.rows: dict[str, list[tuple[Any, ...]]] = {name: rows.get(name, []) for name in self.decls}
        self.row_keys: dict[str, set[str]] = {name: {_key(row) for row in rows} for name, rows in self.rows.items()}
        self._index: dict[str, dict[int, dict[str, list[tuple[Any, ...]]]]] = {}
        self.evidence: dict[tuple[str, str], list[str]] = {}
        for record in bundle.evidence:
            row = tuple(_ground(term, {}) for term in record.atom.terms)
            self.evidence.setdefault((record.atom.relation, _key(row)), []).append(record.id)
        for ids in self.evidence.values():
            ids.sort()
        self.rules_by_head: dict[str, list[Rule]] = {}
        for rule in sorted(bundle.rules, key=canonical_json):
            self.rules_by_head.setdefault(rule.head.relation, []).append(rule)
        self.rule_digest = {id(rule): digest(rule) for rule in bundle.rules}
        components = _components(_positive_dependencies(bundle.rules))
        self.recursive: set[str] = set()
        for rule in bundle.rules:
            head = rule.head.relation
            for item in rule.body:
                if (isinstance(item, Atom) and not item.negated
                        and components.get(item.relation) == components.get(head)):
                    self.recursive.add(head)
        self.components = components
        self.nodes = 0
        self.max_steps = 0
        self.max_nesting = 0

    # -- bookkeeping ----------------------------------------------------------

    def _tick(self, what: str) -> None:
        self.nodes += 1
        if self.nodes > self.max_nodes:
            raise _Truncated(f"{what}: certificate exceeds max_nodes={self.max_nodes}")

    def _nest(self, depth: int) -> None:
        self.max_nesting = max(self.max_nesting, depth)
        if depth > self.max_depth:
            raise _Truncated(f"derivation nesting exceeds max_depth={self.max_depth}")

    # -- row access -----------------------------------------------------------

    def _candidates(self, atom: Atom, env: Mapping[str, Any]) -> list[tuple[Any, ...]]:
        """Rows of ``atom.relation`` narrowed by the bound positions, canonical order."""
        rows = self.rows.get(atom.relation, [])
        best: list[tuple[Any, ...]] | None = None
        for position, term in enumerate(atom.terms):
            if isinstance(term, Constant):
                value = _plain(term.value)
            elif isinstance(term, Variable) and term.name in env:
                value = env[term.name]
            else:
                continue
            candidates = self._indexed(atom.relation, position).get(_key(value), [])
            if best is None or len(candidates) < len(best):
                best = candidates
        return rows if best is None else best

    def _indexed(self, relation: str, position: int) -> dict[str, list[tuple[Any, ...]]]:
        by_position = self._index.setdefault(relation, {})
        table = by_position.get(position)
        if table is None:
            table = {}
            for row in self.rows.get(relation, []):
                if position < len(row):
                    table.setdefault(_key(row[position]), []).append(row)
            by_position[position] = table
        return table

    def _present(self, relation: str, row: tuple[Any, ...]) -> bool:
        return _key(row) in self.row_keys.get(relation, set())

    # -- body instantiation -----------------------------------------------------

    def _instantiations(self, body: tuple[Any, ...], env: dict[str, Any], position: int = 0,
                        positives: tuple = (), negatives: tuple = (), comparisons: tuple = ()):
        if position == len(body):
            yield env, positives, negatives, comparisons
            return
        item = body[position]
        if isinstance(item, Comparison):
            if _compare(item, env):
                record = {"left": _ground(item.left, env), "operator": item.operator,
                          "right": _ground(item.right, env)}
                yield from self._instantiations(body, env, position + 1, positives, negatives,
                                                comparisons + (record,))
            return
        if not isinstance(item, Atom):
            raise CertificateError("rule body contains a non-atom, non-comparison item")
        if item.negated:
            row = tuple(_ground(term, env) for term in item.terms)
            if any(value is None for value in row):
                raise CertificateError(f"negated atom {item.relation} is not ground at check time")
            if not self._present(item.relation, row):
                yield from self._instantiations(body, env, position + 1, positives,
                                                negatives + ((item, row),), comparisons)
            return
        for row in self._candidates(item, env):
            self._tick(f"matching {item.relation}")
            next_env = _unify(item.terms, row, env)
            if next_env is not None:
                yield from self._instantiations(body, next_env, position + 1,
                                                positives + ((item, row),), negatives, comparisons)

    # -- nodes ---------------------------------------------------------------

    def _fact_node(self, relation: str, row: tuple[Any, ...]) -> dict[str, Any]:
        ids = self.evidence.get((relation, _key(row)))
        if not ids:
            raise CertificateError(f"primitive row {relation}{_key(row)} has no attesting evidence")
        return {"kind": "fact", "relation": relation, "row": list(row), "evidence": list(ids)}

    def _rule_node(self, rule: Rule, row: tuple[Any, ...], premises: list[dict[str, Any]],
                   negatives: tuple, comparisons: tuple) -> dict[str, Any]:
        return {
            "kind": "rule", "rule": rule.name, "rule_digest": self.rule_digest[id(rule)],
            "relation": rule.head.relation, "row": list(row),
            "premises": premises,
            "absent": [{"relation": atom.relation, "row": list(absent)} for atom, absent in negatives],
            "comparisons": list(comparisons),
        }

    def derive(self, relation: str, row: tuple[Any, ...], depth: int) -> dict[str, Any]:
        self._nest(depth)
        self._tick(f"deriving {relation}")
        decl = self.decls.get(relation)
        if decl is None:
            raise CertificateError(f"unknown relation {relation!r}")
        row = tuple(_plain(value) for value in row)
        if decl.primitive:
            if not self._present(relation, row):
                raise CertificateError(f"primitive row {relation}{_key(row)} is not in the closure")
            return self._fact_node(relation, row)
        if not self._present(relation, row):
            raise CertificateError(f"row {relation}{_key(row)} is not in the closure")
        if relation in self.recursive:
            return self._derive_recursive(relation, row, depth)
        for rule in self.rules_by_head.get(relation, ()):
            node = self._apply(rule, row, depth)
            if node is not None:
                return node
        raise CertificateError(f"no rule derives {relation}{_key(row)} from the closure")

    def _apply(self, rule: Rule, row: tuple[Any, ...], depth: int,
               recursive_premise: tuple[Atom, dict[str, Any]] | None = None) -> dict[str, Any] | None:
        env = _unify(rule.head.terms, row, {})
        if env is None:
            return None
        for final_env, positives, negatives, comparisons in self._instantiations(rule.body, env):
            if recursive_premise is not None:
                atom, node = recursive_premise
                if not any(item is atom and _same(prow, node["row"]) for item, prow in positives):
                    continue
            premises = []
            for atom, prow in positives:
                if recursive_premise is not None and atom is recursive_premise[0]:
                    premises.append(recursive_premise[1])
                else:
                    premises.append(self.derive(atom.relation, prow, depth + 1))
            return self._rule_node(rule, row, premises, negatives, comparisons)
        return None

    # -- recursion ---------------------------------------------------------------

    def _split_rules(self, relation: str) -> tuple[list[Rule], list[tuple[Rule, Atom]]]:
        component = self.components.get(relation)
        base: list[Rule] = []
        step: list[tuple[Rule, Atom]] = []
        for rule in self.rules_by_head.get(relation, ()):
            recursive_atoms = [item for item in rule.body
                               if isinstance(item, Atom) and not item.negated
                               and self.components.get(item.relation) == component]
            if not recursive_atoms:
                base.append(rule)
            elif len(recursive_atoms) == 1:
                step.append((rule, recursive_atoms[0]))
            else:
                raise CertificateError(
                    f"rule {rule.name or canonical_json(rule)} is non-linear recursive; unsupported")
        return base, step

    def _derive_recursive(self, relation: str, row: tuple[Any, ...], depth: int) -> dict[str, Any]:
        target = (relation, row)
        parent: dict[tuple[str, str], Any] = {(relation, _key(row)): None}
        frontier: list[tuple[str, tuple[Any, ...]]] = [target]
        rule_cache: dict[str, tuple[list[Rule], list[tuple[Rule, Atom]]]] = {}
        for steps in range(self.max_depth + 1):
            # 1. is any frontier row derivable by a base rule?  Frontier rows are
            #    visited in canonical (relation, row) order, rules in canonical order.
            for rel, r in frontier:
                base, _ = rule_cache.setdefault(rel, self._split_rules(rel))
                for rule in base:
                    node = self._apply(rule, r, depth + steps)
                    if node is not None:
                        self.max_steps = max(self.max_steps, steps)
                        return self._rebuild_chain(parent, (rel, r), node, depth, steps)
            # 2. expand one step backwards through the linear step rules.
            next_frontier: list[tuple[str, tuple[Any, ...]]] = []
            for rel, r in frontier:
                _, step_rules = rule_cache.setdefault(rel, self._split_rules(rel))
                for rule, recursive_atom in step_rules:
                    env = _unify(rule.head.terms, r, {})
                    if env is None:
                        continue
                    for _env, positives, _negatives, _comparisons in self._instantiations(rule.body, env):
                        predecessor = next(prow for atom, prow in positives if atom is recursive_atom)
                        key = (recursive_atom.relation, _key(predecessor))
                        if key in parent:
                            continue
                        self._tick(f"searching {relation}")
                        parent[key] = ((rel, r), rule, recursive_atom)
                        next_frontier.append((recursive_atom.relation, predecessor))
            if not next_frontier:
                raise CertificateError(
                    f"recursive row {relation}{_key(row)} has no base derivation within the closure")
            next_frontier.sort(key=lambda item: (item[0], _key(item[1])))
            frontier = next_frontier
        raise _Truncated(f"recursive derivation of {relation} exceeds max_depth={self.max_depth} steps")

    def _rebuild_chain(self, parent: Mapping[tuple[str, str], Any], start: tuple[str, tuple[Any, ...]],
                       node: dict[str, Any], depth: int, steps: int) -> dict[str, Any]:
        current = node
        rel, r = start
        remaining = steps
        while True:
            link = parent[(rel, _key(r))]
            if link is None:
                return current
            (parent_rel, parent_row), rule, recursive_atom = link
            remaining -= 1
            rebuilt = self._apply(rule, parent_row, depth + remaining, (recursive_atom, current))
            if rebuilt is None:
                raise CertificateError("recorded step instantiation no longer applies")  # pragma: no cover
            current = rebuilt
            rel, r = parent_rel, parent_row


def _collect(node: Mapping[str, Any], leaves: set[str], witnesses: dict[str, dict[str, Any]],
             absent: dict[str, dict[str, Any]], decls: Mapping[str, Any]) -> None:
    if node["kind"] == "fact":
        leaves.update(node["evidence"])
        decl = decls.get(node["relation"])
        if decl is not None and decl.modality.value in {"completeness", "compatibility"}:
            witnesses[_key((node["relation"], node["row"]))] = {
                "relation": node["relation"], "row": list(node["row"]),
                "modality": decl.modality.value, "evidence": list(node["evidence"])}
        return
    for item in node.get("absent", ()):
        absent[_key((item["relation"], item["row"]))] = {"relation": item["relation"], "row": list(item["row"])}
    for premise in node["premises"]:
        _collect(premise, leaves, witnesses, absent, decls)


def certify(bundle: Bundle, relations: Any, relation: str, row: Iterable[Any], *,
            max_depth: int = DEFAULT_MAX_DEPTH, max_nodes: int = DEFAULT_MAX_NODES) -> dict[str, Any]:
    """Re-derive a bounded ground certificate for ``relation(row)`` from ``relations``.

    ``relations`` is either ``{name: rows}`` or the ``(name, rows)`` pairs a
    ``KernelReport`` carries; rows must be the engine's closure of the bundle.
    The result is JSON-serializable and independent of which engine produced
    the rows.
    """
    certifier = _Certifier(bundle, relations, max_depth, max_nodes)
    conclusion = {"relation": relation, "row": [_plain(value) for value in row]}
    base = {
        "certificate_version": CERTIFICATE_VERSION,
        "bundle_digest": digest(bundle),
        "rules_digest": rules_digest(bundle),
        "bounds": {"max_depth": max_depth, "max_nodes": max_nodes},
        "conclusion": conclusion,
    }
    try:
        node = certifier.derive(relation, tuple(conclusion["row"]), 0)
    except _Truncated as exc:
        return {**base, "truncated": True, "truncation": exc.reason, "steps": None,
                "nodes": certifier.nodes, "derivation": None, "leaves": [], "witnesses": [],
                "absent": []}
    leaves: set[str] = set()
    witnesses: dict[str, dict[str, Any]] = {}
    absent: dict[str, dict[str, Any]] = {}
    _collect(node, leaves, witnesses, absent, certifier.decls)
    return {
        **base, "truncated": False, "truncation": None,
        "steps": certifier.max_steps if relation in certifier.recursive or certifier.max_steps else 0,
        "nodes": certifier.nodes, "nesting": certifier.max_nesting,
        "derivation": node,
        "leaves": sorted(leaves),
        "witnesses": [witnesses[key] for key in sorted(witnesses)],
        "absent": [absent[key] for key in sorted(absent)],
    }


def claim_conclusions(bundle: Bundle, relations: Any, claim: Claim) -> list[tuple[Any, ...]]:
    """The ground rows of ``claim.relation`` in the closure that instantiate ``claim``.

    An ``exists`` claim contributes every closure row unifying with its terms
    and context; a ``forall`` claim contributes one row per domain member in
    its context, when that row is present.  Rows are in canonical order.
    """
    rows = normalize_relations(relations)
    decls = {decl.name: decl for decl in bundle.relations}
    decl = decls[claim.relation]
    names = [column.name for column in decl.columns]
    context = claim.context.as_dict()

    def matches(row: tuple[Any, ...]) -> bool:
        if _unify(claim.terms, row, {}) is None:
            return False
        return all(_same(row[names.index(name)], value) for name, value in context.items() if name in names)

    if claim.quantifier.value != "forall":
        return [row for row in rows.get(claim.relation, []) if matches(row)]
    domain = decls[claim.domain]
    domain_names = [column.name for column in domain.columns]
    known = dict(context)
    known.update((name, _plain(term.value)) for name, term in zip(names, claim.terms) if isinstance(term, Constant))
    out = []
    for member in rows.get(claim.domain, []):
        values = dict(zip(domain_names, member))
        if any(name in known and not _same(values[name], known[name]) for name in domain_names):
            continue
        row = tuple(values[term.name] if isinstance(term, Variable) and term.name in values else _ground(term, {})
                    for term in claim.terms)
        if _key(row) in {_key(r) for r in rows.get(claim.relation, [])}:
            out.append(row)
    return out


def recheck(bundle: Bundle, certificate: Mapping[str, Any], relations: Any = None) -> RecheckResult:
    """Ground re-check of a certificate against the bundle; no search."""
    problems: list[str] = []
    unchecked: list[str] = []
    if certificate.get("certificate_version") != CERTIFICATE_VERSION:
        problems.append("unknown certificate version")
    if certificate.get("bundle_digest") != digest(bundle):
        problems.append("certificate was issued for a different bundle digest")
    if certificate.get("rules_digest") != rules_digest(bundle):
        problems.append("certificate was issued for different rules")
    if certificate.get("truncated"):
        return RecheckResult(False, (*problems, "certificate is truncated; nothing is certified"), ())
    derivation = certificate.get("derivation")
    conclusion = certificate.get("conclusion") or {}
    if not isinstance(derivation, Mapping):
        return RecheckResult(False, (*problems, "certificate has no derivation"), ())
    decls = {decl.name: decl for decl in bundle.relations}
    evidence = {record.id: record for record in bundle.evidence}
    rules = {digest(rule): rule for rule in bundle.rules}
    closure = normalize_relations(relations) if relations is not None else None
    closure_keys = ({name: {_key(row) for row in rows} for name, rows in closure.items()}
                    if closure is not None else None)
    leaves: set[str] = set()

    def walk(node: Mapping[str, Any], relation: str, row: Any, path: str) -> None:
        if node.get("relation") != relation or not _same(node.get("row"), row):
            problems.append(f"{path}: node does not conclude {relation}{_key(row)}")
            return
        decl = decls.get(relation)
        if decl is None:
            problems.append(f"{path}: unknown relation {relation!r}")
            return
        if node.get("kind") == "fact":
            if not decl.primitive:
                problems.append(f"{path}: {relation} is not primitive but appears as a leaf")
            ids = node.get("evidence") or []
            if not ids:
                problems.append(f"{path}: leaf without evidence")
            for evidence_id in ids:
                record = evidence.get(evidence_id)
                if record is None:
                    problems.append(f"{path}: unknown evidence {evidence_id!r}")
                    continue
                attested = tuple(_ground(term, {}) for term in record.atom.terms)
                if record.atom.relation != relation or not _same(attested, row):
                    problems.append(f"{path}: evidence {evidence_id!r} attests "
                                    f"{record.atom.relation}{_key(attested)}, not {relation}{_key(row)}")
                if decl.producer_classes:
                    producer_class = (record.source.split(" ", 1)[0] if record.source else "")
                    if producer_class not in decl.producer_classes:
                        problems.append(
                            f"{path}: evidence {evidence_id!r} producer {producer_class!r} "
                            f"is not admitted by {relation} {decl.producer_classes}")
                leaves.add(evidence_id)
            return
        if node.get("kind") != "rule":
            problems.append(f"{path}: unknown node kind {node.get('kind')!r}")
            return
        rule = rules.get(node.get("rule_digest"))
        if rule is None:
            problems.append(f"{path}: rule {node.get('rule')!r} is not in the bundle")
            return
        if rule.head.relation != relation:
            problems.append(f"{path}: rule {rule.name!r} does not derive {relation}")
            return
        env = _unify(rule.head.terms, tuple(row), {})
        if env is None:
            problems.append(f"{path}: rule head does not unify with the row")
            return
        premises = node.get("premises") or []
        positives = [item for item in rule.body if isinstance(item, Atom) and not item.negated]
        negatives = [item for item in rule.body if isinstance(item, Atom) and item.negated]
        comparisons = [item for item in rule.body if isinstance(item, Comparison)]
        if len(premises) != len(positives):
            problems.append(f"{path}: {len(premises)} premises for {len(positives)} positive atoms")
            return
        for position, (atom, premise) in enumerate(zip(positives, premises)):
            if not isinstance(premise, Mapping):
                problems.append(f"{path}.premises[{position}]: malformed premise"); return
            env_next = _unify(atom.terms, tuple(premise.get("row") or ()), env)
            if env_next is None:
                problems.append(f"{path}.premises[{position}]: premise row does not unify with "
                                f"{atom.relation}")
                return
            env = env_next
        for position, (atom, premise) in enumerate(zip(positives, premises)):
            walk(premise, atom.relation, premise.get("row"), f"{path}.premises[{position}]")
        absent = node.get("absent") or []
        if len(absent) != len(negatives):
            problems.append(f"{path}: {len(absent)} absence checks for {len(negatives)} negated atoms")
        for atom, item in zip(negatives, absent):
            expected = tuple(_ground(term, env) for term in atom.terms)
            if item.get("relation") != atom.relation or not _same(item.get("row"), expected):
                problems.append(f"{path}: absence check does not match {atom.relation}{_key(expected)}")
                continue
            if closure_keys is None:
                unchecked.append(f"{path}: absence of {atom.relation}{_key(expected)} not checked (no closure)")
            elif _key(expected) in closure_keys.get(atom.relation, set()):
                problems.append(f"{path}: negated row {atom.relation}{_key(expected)} is present in the closure")
        for comparison in comparisons:
            if not _compare(comparison, env):
                problems.append(f"{path}: comparison {comparison.operator} does not hold")

    walk(derivation, conclusion.get("relation"), conclusion.get("row"), "derivation")
    if not problems and sorted(leaves) != list(certificate.get("leaves") or []):
        problems.append("certificate leaves do not equal the leaves of its derivation")
    return RecheckResult(not problems, tuple(problems), tuple(unchecked))


__all__ = ["CERTIFICATE_VERSION", "DEFAULT_MAX_DEPTH", "DEFAULT_MAX_NODES", "CertificateError",
           "RecheckResult", "rules_digest", "normalize_relations", "certify", "claim_conclusions",
           "recheck"]
