"""Deterministic micro-benchmarks for the Python claims evaluator.

Run from the repository root::

    PYTHONPATH=src python benchmarks/claims_evaluator_bench.py [--sizes small|large]

Each case builds a synthetic bundle, evaluates it once, and prints wall time,
peak traced memory, operational status, and derived row count.  The cases are
the shapes SCIP-derived static claims produce: a one-hop projection, an
equijoin, and a transitive closure over a chain (the worst case for proof-tree
depth).  Results are evidence for EXPERIMENT-PLAN.md section 31, not gates.
"""
from __future__ import annotations

import argparse
import sys
import time
import tracemalloc

from capcov.claims.evaluator import ResourceLimits, evaluate
from capcov.claims.ir import Atom, Bundle, Claim, Column, Constant, Evidence, RelationDecl, Rule, Variable


def _with_evidence(relations, facts, rules, claims) -> Bundle:
    """Attach one evidence record per fact.

    Validation attributes every fact to evidence unconditionally (section 27),
    so a benchmark bundle carries synthetic producer records; their cost is part
    of what is measured, as it is in real bundles.
    """
    evidence = tuple(Evidence(f"bench:{fact.relation}:{i}", fact, source="bench") for i, fact in enumerate(facts))
    return Bundle(relations, tuple(facts), tuple(rules), tuple(claims), evidence=evidence)


def _sym(name: str) -> Column:
    return Column(name, "symbol")


def bundle_one_hop(n: int) -> Bundle:
    relations = (RelationDecl("e", (_sym("s"), _sym("d"))),
                 RelationDecl("r", (_sym("s"), _sym("d")), modality="derived", primitive=False))
    facts = tuple(Atom("e", (Constant(f"n{i}"), Constant(f"n{i + 1}"))) for i in range(n))
    rule = Rule(Atom("r", (Variable("S"), Variable("D"))), (Atom("e", (Variable("S"), Variable("D"))),), name="hop")
    return _with_evidence(relations, facts, (rule,), (Claim("r", (Constant("n0"), Constant("n1")), id="c"),))


def bundle_equijoin(n: int) -> Bundle:
    relations = (RelationDecl("a", (_sym("x"), _sym("y"))), RelationDecl("b", (_sym("y"), _sym("z"))),
                 RelationDecl("j", (_sym("x"), _sym("z")), modality="derived", primitive=False))
    facts = []
    for i in range(n):
        facts.append(Atom("a", (Constant(f"x{i}"), Constant(f"y{i}"))))
        facts.append(Atom("b", (Constant(f"y{i}"), Constant(f"z{i}"))))
    rule = Rule(Atom("j", (Variable("X"), Variable("Z"))),
                (Atom("a", (Variable("X"), Variable("Y"))), Atom("b", (Variable("Y"), Variable("Z")))), name="join")
    return _with_evidence(relations, facts, (rule,), (Claim("j", (Constant("x0"), Constant("z0")), id="c"),))


def bundle_chain_closure(n: int) -> Bundle:
    relations = (RelationDecl("e", (_sym("s"), _sym("d"))),
                 RelationDecl("r", (_sym("s"), _sym("d")), modality="derived", primitive=False))
    facts = tuple(Atom("e", (Constant(f"n{i}"), Constant(f"n{i + 1}"))) for i in range(n))
    rules = (Rule(Atom("r", (Variable("S"), Variable("D"))), (Atom("e", (Variable("S"), Variable("D"))),), name="base"),
             Rule(Atom("r", (Variable("S"), Variable("D"))),
                  (Atom("r", (Variable("S"), Variable("M"))), Atom("e", (Variable("M"), Variable("D")))), name="step"))
    return _with_evidence(relations, facts, rules, (Claim("r", (Constant("n0"), Constant(f"n{n}")), id="c"),))


CASES = {
    "small": [("one-hop", bundle_one_hop, [1000]), ("equijoin", bundle_equijoin, [100, 300]), ("closure-chain", bundle_chain_closure, [50])],
    "large": [("one-hop", bundle_one_hop, [1000, 5000]), ("equijoin", bundle_equijoin, [100, 300, 1000]),
              ("closure-chain", bundle_chain_closure, [50, 100, 200])],
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sizes", choices=sorted(CASES), default="small")
    args = parser.parse_args()
    limits = ResourceLimits(max_seconds=600.0, max_derived_rows=10_000_000, max_provenance=10_000_000)
    print(f"python {sys.version.split()[0]}")
    for label, maker, sizes in CASES[args.sizes]:
        for n in sizes:
            bundle = maker(n)
            tracemalloc.start()
            started = time.perf_counter()
            report = evaluate(bundle, limits)
            elapsed = time.perf_counter() - started
            peak = tracemalloc.get_traced_memory()[1] / 1e6
            tracemalloc.stop()
            rows = dict(report.resources).get("derived_rows")
            print(f"{label:14s} n={n:5d} {elapsed:8.3f}s peak={peak:7.1f}MB status={report.status.value} "
                  f"verdict={report.claims[0].result.semantic.value} rows={rows}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
