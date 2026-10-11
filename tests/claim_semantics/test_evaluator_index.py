from __future__ import annotations

import unittest

from capcov.claims import (
    Atom, Claim, Column, Comparison, Constant, RelationDecl, Rule,
    TypeName, Variable,
)
from tests.claim_fixtures import Bundle  # attributed fixture bundles
from capcov.claims.evaluator import (
    EvaluationReport, ResourceLimits, _Engine,
)
from capcov.claims.ir import canonical_json
from capcov.claims.validation import assert_valid


def relation(name, *columns, **kwargs):
    return RelationDecl(
        name,
        tuple(Column(column, type_name) for column, type_name in columns),
        **kwargs,
    )


class _FullScanEngine(_Engine):
    """Pre-index candidate selection retained as a behavioral oracle."""

    def _candidate_rows(self, atom, env, rows_override):
        del env
        self._full_scan_atom_matches += 1
        return self.rows[atom.relation] if rows_override is None else rows_override


def run_engine(engine_type, bundle):
    assert_valid(bundle)
    engine = engine_type(bundle, ResourceLimits(max_seconds=None))
    engine.run()
    claims = tuple(
        engine.evaluate_claim(index, claim)
        for index, claim in enumerate(bundle.claims)
    )
    relations = tuple(
        (name, tuple(sorted(rows, key=canonical_json)))
        for name, rows in sorted(engine.rows.items())
    )
    provenance = tuple(
        (
            name,
            tuple(
                (row, tuple(proofs))
                for row, proofs in sorted(
                    data.items(), key=lambda item: canonical_json(item[0])
                )
            ),
        )
        for name, data in sorted(engine.proofs.items())
    )
    resources = (
        ("derived_rows", engine.derived_rows),
        ("provenance_nodes", engine.provenance_count),
        ("unattributed_facts", engine.unattributed_facts),
        ("discarded_alternatives", engine.discarded_alternatives),
    )
    return engine, EvaluationReport(
        relations, provenance, claims, resources=resources
    )


def selective_bundle(size=100):
    source = relation(
        "selective_source",
        ("key", TypeName.SYMBOL),
        ("value", TypeName.INTEGER),
    )
    selected = relation(
        "selected_value",
        ("value", TypeName.INTEGER),
        modality="claim",
        primitive=False,
    )
    facts = tuple(
        Atom(
            "selective_source",
            (Constant(f"key-{index}"), Constant(index)),
        )
        for index in range(size)
    )
    # A duplicate input verifies that the index retains relation set behavior.
    facts = (*facts, facts[size // 2])
    rule = Rule(
        Atom("selected_value", (Variable("value"),)),
        (
            Atom(
                "selective_source",
                (Constant(f"key-{size // 2}"), Variable("value")),
            ),
        ),
        "select-one",
    )
    claim = Claim("selected_value", (Constant(size // 2),), id="selected")
    return Bundle((source, selected), facts=facts, rules=(rule,), claims=(claim,))


def unconstrained_bundle():
    source = relation(
        "unconstrained_source",
        ("key", TypeName.SYMBOL),
        ("value", TypeName.INTEGER),
    )
    copied = relation(
        "copied",
        ("key", TypeName.SYMBOL),
        ("value", TypeName.INTEGER),
        primitive=False,
    )
    facts = tuple(
        Atom("unconstrained_source", (Constant(key), Constant(value)))
        for key, value in (("c", 3), ("a", 1), ("b", 2))
    )
    rule = Rule(
        Atom("copied", (Variable("key"), Variable("value"))),
        (Atom("unconstrained_source", (Variable("key"), Variable("value"))),),
        "copy-all",
    )
    return Bundle((source, copied), facts=facts, rules=(rule,))


def repeated_variable_bundle():
    pairs = relation(
        "pairs",
        ("left", TypeName.SYMBOL),
        ("right", TypeName.SYMBOL),
    )
    same = relation(
        "same",
        ("value", TypeName.SYMBOL),
        modality="claim",
        primitive=False,
    )
    facts = tuple(
        Atom("pairs", (Constant(left), Constant(right)))
        for left, right in (("a", "a"), ("a", "b"), ("b", "b"))
    )
    rule = Rule(
        Atom("same", (Variable("value"),)),
        (Atom("pairs", (Variable("value"), Variable("value"))),),
        "same-pair",
    )
    claim = Claim("same", (Constant("a"),), id="same-a")
    return Bundle((pairs, same), facts=facts, rules=(rule,), claims=(claim,))


def nested_value_bundle():
    metadata = relation(
        "metadata", ("payload", TypeName.JSON_METADATA_ONLY)
    )
    selected = relation(
        "nested_selected",
        ("value", TypeName.SYMBOL),
        modality="claim",
        primitive=False,
    )
    wanted = {"nested": [1, {"active": True, "labels": ["a", "b"]}]}
    facts = (
        Atom("metadata", (Constant({"nested": [2]}),)),
        Atom("metadata", (Constant(wanted),)),
    )
    rule = Rule(
        Atom("nested_selected", (Constant("yes"),)),
        (Atom("metadata", (Constant(wanted),)),),
        "select-nested",
    )
    claim = Claim("nested_selected", (Constant("yes"),), id="nested")
    return Bundle((metadata, selected), facts=facts, rules=(rule,), claims=(claim,))


def negative_comparison_bundle():
    seed = relation("neg_seed", ("value", TypeName.SYMBOL))
    blocked = relation("neg_blocked", ("value", TypeName.SYMBOL))
    closed = relation(
        "neg_blocked_closed", modality="completeness", completes="neg_blocked"
    )
    allowed = relation(
        "neg_allowed",
        ("value", TypeName.SYMBOL),
        modality="claim",
        primitive=False,
    )
    facts = (
        Atom("neg_seed", (Constant("a"),)),
        Atom("neg_seed", (Constant("b"),)),
        Atom("neg_seed", (Constant("skip"),)),
        Atom("neg_blocked", (Constant("b"),)),
        Atom("neg_blocked_closed", ()),
    )
    rule = Rule(
        Atom("neg_allowed", (Variable("value"),)),
        (
            Atom("neg_seed", (Variable("value"),)),
            Atom("neg_blocked_closed", ()),
            Atom("neg_blocked", (Variable("value"),), negated=True),
            Comparison(Variable("value"), "!=", Constant("skip")),
        ),
        "not-blocked-or-skipped",
    )
    claim = Claim("neg_allowed", (Constant("a"),), id="allowed-a")
    return Bundle(
        (seed, blocked, closed, allowed),
        facts=facts,
        rules=(rule,),
        claims=(claim,),
    )


def equijoin_bundle(size):
    left = relation(
        "join_left",
        ("key", TypeName.INTEGER),
        ("left_value", TypeName.SYMBOL),
    )
    right = relation(
        "join_right",
        ("key", TypeName.INTEGER),
        ("right_value", TypeName.SYMBOL),
    )
    joined = relation(
        "joined",
        ("key", TypeName.INTEGER),
        ("left_value", TypeName.SYMBOL),
        ("right_value", TypeName.SYMBOL),
        primitive=False,
    )
    facts = tuple(
        Atom("join_left", (Constant(index), Constant(f"left-{index}")))
        for index in range(size)
    ) + tuple(
        Atom("join_right", (Constant(index), Constant(f"right-{index}")))
        for index in range(size)
    )
    rule = Rule(
        Atom(
            "joined",
            (Variable("key"), Variable("left"), Variable("right")),
        ),
        (
            Atom("join_left", (Variable("key"), Variable("left"))),
            Atom("join_right", (Variable("key"), Variable("right"))),
        ),
        "equijoin",
    )
    return Bundle((left, right, joined), facts=facts, rules=(rule,))


class EvaluatorIndexTests(unittest.TestCase):
    def test_selective_constant_uses_index_and_reduces_candidates(self):
        bundle = selective_bundle(100)
        indexed, indexed_report = run_engine(_Engine, bundle)
        full_scan, full_scan_report = run_engine(_FullScanEngine, bundle)

        self.assertEqual(indexed_report, full_scan_report)
        # The fixed point is semi-naive over changed rows: a non-recursive rule
        # is matched once in the complete first pass, and the second pass has
        # no pivot because the head relation never appears in the body.
        self.assertEqual(indexed._indexed_atom_matches, 1)
        self.assertEqual(indexed._full_scan_atom_matches, 0)
        self.assertEqual(indexed._candidate_rows_examined, 1)
        self.assertEqual(full_scan._candidate_rows_examined, 100)

    def test_indexed_and_full_scan_semantics_are_identical(self):
        cases = {
            "selective": selective_bundle(20),
            "unconstrained": unconstrained_bundle(),
            "repeated-variable": repeated_variable_bundle(),
            "nested-value": nested_value_bundle(),
            "negative-comparison": negative_comparison_bundle(),
        }
        for name, bundle in cases.items():
            with self.subTest(case=name):
                indexed, indexed_report = run_engine(_Engine, bundle)
                _, full_scan_report = run_engine(_FullScanEngine, bundle)
                self.assertEqual(indexed_report, full_scan_report)
                if name == "unconstrained":
                    self.assertGreater(indexed._full_scan_atom_matches, 0)

    def test_equijoin_candidate_work_scales_linearly(self):
        small_size = 40
        large_size = 160
        small, _ = run_engine(_Engine, equijoin_bundle(small_size))
        large, _ = run_engine(_Engine, equijoin_bundle(large_size))

        # One complete pass examines N unconstrained left rows and one indexed
        # right row per left row: 2N rather than N squared.  The semi-naive
        # second pass matches nothing because the head is not in the body.
        self.assertEqual(small._candidate_rows_examined, 2 * small_size)
        self.assertEqual(large._candidate_rows_examined, 2 * large_size)
        self.assertEqual(large._full_scan_atom_matches, 1)
        self.assertEqual(large._indexed_atom_matches, large_size)
        quadratic_growth = (large_size // small_size) ** 2
        self.assertLess(
            large._candidate_rows_examined,
            small._candidate_rows_examined * quadratic_growth,
        )


if __name__ == "__main__":
    unittest.main()
