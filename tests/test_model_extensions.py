"""Extensions to the flow model: mutation declarations, unreachable/unmutated
reporting, and scenario-scoped reconciliation.

Mutations declare wrong-behaviours a rebuild MUST fail (turn a binding RED). The
engine only VALIDATES the field and CARRIES it into the plan so a consumer runner
(mill prove.py) can apply them; the engine never executes a mutation.
"""

from __future__ import annotations

import copy
import unittest

from capcov.flows.model import digest, plan, reconcile, validate


def base_model() -> tuple[dict, dict]:
    """login -> edit -> history over two facts; obligations map read/edit."""
    inventory = {"obligations": [{"id": "read"}, {"id": "edit"}]}
    model = {
        "version": 1,
        "scope": "ext-fixture",
        "facts": ["signed-in", "edited"],
        "initial": [],
        "transitions": [],
    }
    for name, requires, adds in (
        ("login", [], ["signed-in"]),
        ("edit", ["signed-in"], ["edited"]),
        ("history", ["edited"], []),
    ):
        model["transitions"].append(
            {
                "id": name,
                "requires": requires,
                "adds": adds,
                "actor": "operator",
                "outcome": name,
                "evidence": [{"file": "fixture.py", "line": 1}],
                "obligations": ["edit" if name == "edit" else "read"],
                "bindings": {
                    "browser": {
                        "commands": [
                            {"op": "assert", "id": "outcome", "selector": "body", "text": name},
                        ]
                    }
                },
            }
        )
    return inventory, model


def independent_model() -> tuple[dict, dict]:
    """Three independent, single-step transitions, obligations mapped 1:1 -- so
    folding one scenario proves exactly one obligation."""
    inventory = {"obligations": [{"id": "o1"}, {"id": "o2"}, {"id": "o3"}]}
    model = {
        "version": 1,
        "scope": "independent",
        "facts": [],
        "initial": [],
        "transitions": [],
    }
    for name, obligation in (("t1", "o1"), ("t2", "o2"), ("t3", "o3")):
        model["transitions"].append(
            {
                "id": name,
                "requires": [],
                "adds": [],
                "actor": "operator",
                "outcome": name,
                "evidence": [{"file": "fixture.py", "line": 1}],
                "obligations": [obligation],
                "bindings": {
                    "browser": {
                        "commands": [
                            {"op": "assert", "id": "outcome", "selector": "body", "text": name},
                        ]
                    }
                },
            }
        )
    return inventory, model


def evidence(inventory: dict, execution_plan: dict) -> dict:
    return {
        "status": "passed",
        "assurance": "test-fixture",
        "plan_sha256": digest(execution_plan),
        "inventory_sha256": digest(inventory),
        "scenarios": [
            {
                "id": s["id"],
                "status": "passed",
                "assertions": [
                    f"{i}:{step['transition']}:{c['id']}"
                    for i, step in enumerate(s["steps"])
                    for c in step["commands"]
                    if c["op"] == "assert"
                ],
            }
            for s in execution_plan["scenarios"]
        ],
    }


class MutationValidationTests(unittest.TestCase):
    def test_valid_mutations_pass_validation_and_are_carried_into_steps(self) -> None:
        _, model = base_model()
        mutations = [
            {"kind": "idempotency", "note": "re-running edit must not double-append"},
            {"kind": "scoping", "note": "edit touches only this row", "target": "history"},
            {"kind": "ordering", "note": "edit must follow login", "with": "login"},
        ]
        edit = next(t for t in model["transitions"] if t["id"] == "edit")
        edit["mutations"] = mutations
        validate(model)  # must not raise
        execution_plan = plan(model, "browser")
        scenario = next(s for s in execution_plan["scenarios"] if s["id"] == "edit")
        edit_step = next(s for s in scenario["steps"] if s["transition"] == "edit")
        login_step = next(s for s in scenario["steps"] if s["transition"] == "login")
        self.assertEqual(edit_step["mutations"], mutations)
        # A transition without mutations carries no mutations key.
        self.assertNotIn("mutations", login_step)

    def test_optional_mutations_absent_is_still_valid(self) -> None:
        _, model = base_model()
        validate(model)  # no transition declares mutations; must not raise
        self.assertNotIn("mutations", plan(model, "browser")["scenarios"][0]["steps"][0])

    def test_unknown_mutation_kind_is_rejected(self) -> None:
        _, model = base_model()
        model["transitions"][0]["mutations"] = [{"kind": "parallelism", "note": "nope"}]
        with self.assertRaisesRegex(ValueError, "mutation kind"):
            validate(model)

    def test_ordering_mutation_without_with_is_rejected(self) -> None:
        _, model = base_model()
        model["transitions"][1]["mutations"] = [{"kind": "ordering", "note": "needs a sibling"}]
        with self.assertRaisesRegex(ValueError, "ordering"):
            validate(model)

    def test_ordering_with_naming_a_nonexistent_sibling_is_rejected(self) -> None:
        _, model = base_model()
        model["transitions"][1]["mutations"] = [
            {"kind": "ordering", "note": "dangling", "with": "ghost"}
        ]
        with self.assertRaisesRegex(ValueError, "ordering"):
            validate(model)

    def test_ordering_with_naming_itself_is_not_a_sibling(self) -> None:
        _, model = base_model()
        model["transitions"][1]["mutations"] = [
            {"kind": "ordering", "note": "self", "with": "edit"}
        ]
        with self.assertRaisesRegex(ValueError, "ordering"):
            validate(model)

    def test_mutation_without_note_is_rejected(self) -> None:
        _, model = base_model()
        model["transitions"][0]["mutations"] = [{"kind": "scoping"}]
        with self.assertRaisesRegex(ValueError, "note"):
            validate(model)


class UnreachableReportingTests(unittest.TestCase):
    def test_plan_reports_unreachable_precondition_transition(self) -> None:
        _, model = base_model()
        model["facts"].append("admin")
        # No transition ever adds "admin", so this transition can never fire.
        model["transitions"].append(
            {
                "id": "purge",
                "requires": ["admin"],
                "adds": [],
                "actor": "operator",
                "outcome": "purge",
                "evidence": [{"file": "fixture.py", "line": 2}],
                "obligations": ["read"],
                "bindings": {
                    "browser": {
                        "commands": [{"op": "assert", "id": "x", "selector": "body", "text": "purge"}]
                    }
                },
            }
        )
        result = plan(model, "browser")
        unreachable = {u["transition"]: u["reason"] for u in result["unreachable"]}
        self.assertEqual(unreachable, {"purge": "unreachable preconditions"})

    def test_plan_reports_missing_target_binding_as_unreachable(self) -> None:
        _, model = base_model()
        # history binds no "browser" target -> missing target binding.
        model["transitions"][2]["bindings"] = {}
        result = plan(model, "browser")
        unreachable = {u["transition"]: u["reason"] for u in result["unreachable"]}
        self.assertEqual(unreachable, {"history": "missing target binding"})

    def test_fully_reachable_model_has_no_unreachable(self) -> None:
        _, model = base_model()
        self.assertEqual(plan(model, "browser")["unreachable"], [])


class UnmutatedStateTransitionTests(unittest.TestCase):
    def test_state_mutating_transition_without_mutations_is_warned(self) -> None:
        _, model = base_model()
        result = plan(model, "browser")
        # login adds signed-in, edit adds edited; history has no adds/removes.
        self.assertEqual(result["unmutated_state_transitions"], ["edit", "login"])

    def test_declaring_mutations_clears_the_warning(self) -> None:
        _, model = base_model()
        for t in model["transitions"]:
            if t.get("adds") or t.get("removes"):
                t["mutations"] = [{"kind": "idempotency", "note": "safe to retry"}]
        self.assertEqual(plan(model, "browser")["unmutated_state_transitions"], [])

    def test_non_state_transition_is_not_warned(self) -> None:
        _, model = base_model()
        # history neither adds nor removes: never a member of the warning list.
        self.assertNotIn("history", plan(model, "browser")["unmutated_state_transitions"])


class ScenarioScopedReconciliationTests(unittest.TestCase):
    def test_only_none_preserves_full_reconciliation(self) -> None:
        inventory, model = independent_model()
        execution_plan = plan(model, "browser")
        run = evidence(inventory, execution_plan)
        default = reconcile(inventory, model, execution_plan, run)
        scoped_none = reconcile(inventory, model, execution_plan, run, only=None)
        self.assertEqual(default, scoped_none)
        self.assertTrue(default["complete"])
        self.assertEqual(default["summary"]["covered"], 3)

    def test_only_covers_exactly_the_named_scenario(self) -> None:
        inventory, model = independent_model()
        execution_plan = plan(model, "browser")
        run = evidence(inventory, execution_plan)
        result = reconcile(inventory, model, execution_plan, run, only="t2")
        statuses = {r["id"]: r["status"] for r in result["rows"]}
        self.assertEqual(statuses["o2"], "covered")
        self.assertEqual(statuses["o1"], "unproven")
        self.assertEqual(statuses["o3"], "unproven")
        self.assertEqual(result["summary"]["covered"], 1)
        self.assertFalse(result["complete"])

    def test_only_a_multistep_scenario_covers_its_whole_path(self) -> None:
        inventory, model = base_model()
        execution_plan = plan(model, "browser")
        run = evidence(inventory, execution_plan)
        # The "edit" scenario's path is login -> edit, so folding it proves both.
        result = reconcile(inventory, model, execution_plan, run, only="edit")
        statuses = {r["id"]: r["status"] for r in result["rows"]}
        # "read" is owned by {login, history}; history is not in edit's path, so
        # read stays unproven even though login was folded.
        self.assertEqual(statuses["edit"], "covered")
        self.assertEqual(statuses["read"], "unproven")


class CoverageProvenancePassThroughTests(unittest.TestCase):
    """Discovery's provenance (excluded_surfaces / unresolved) must reach the
    coverage artifact so a narrowed denominator is legible there, not only in the
    raw inventory. It adds no obligation and never changes pass/fail."""

    def test_declared_provenance_is_carried_into_coverage(self) -> None:
        inventory, model = independent_model()
        excluded = {
            "count": 1,
            "surfaces": [
                {
                    "file": "routes.py",
                    "line": 9,
                    "method": "WEBSOCKET",
                    "path": "/ws",
                    "handler": "ws",
                    "reason": "route decorator method not in the recognised HTTP verb set",
                }
            ],
        }
        unresolved = [{"adapter": 1, "kind": "openapi-json", "reason": "no surfaces"}]
        inventory["excluded_surfaces"] = excluded
        inventory["unresolved"] = unresolved
        execution_plan = plan(model, "browser")
        run = evidence(inventory, execution_plan)
        result = reconcile(inventory, model, execution_plan, run)
        self.assertEqual(result["excluded_surfaces"], excluded)
        self.assertEqual(result["unresolved"], unresolved)
        # Provenance is legibility only: it does not disturb completeness.
        self.assertTrue(result["complete"])

    def test_absent_provenance_defaults_explicitly_rather_than_missing(self) -> None:
        inventory, model = independent_model()  # a bare inventory declares neither
        execution_plan = plan(model, "browser")
        run = evidence(inventory, execution_plan)
        result = reconcile(inventory, model, execution_plan, run)
        self.assertEqual(result["excluded_surfaces"], {"count": 0, "surfaces": []})
        self.assertEqual(result["unresolved"], [])


if __name__ == "__main__":
    unittest.main()
