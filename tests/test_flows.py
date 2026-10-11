from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from capcov.flows.cli import main
from capcov.flows.discovery import discover
from capcov.flows.model import digest, plan, reconcile

_HAVE_TS = (
    importlib.util.find_spec("tree_sitter") is not None
    and importlib.util.find_spec("tree_sitter_language_pack") is not None
)

# A decorated route: the decorator call captures @path and the function it
# decorates is captured as @handler, so its body is walked for branch and
# exception candidates. The route opinion lives in this config query, not in the
# engine, so the single treesitter-routes adapter serves every language.
PY_HANDLER_QUERY = (
    "(decorated_definition "
    "(decorator (call function: (attribute attribute: (identifier) @method) "
    "arguments: (argument_list (string) @path))) "
    "definition: (function_definition) @handler)"
)


def fixture() -> tuple[dict, dict]:
    inventory = {"obligations": [{"id": "read"}, {"id": "edit"}]}
    model = {
        "version": 1,
        "scope": "fixture",
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


class StateBudgetTests(unittest.TestCase):
    def test_explicit_budget_preserves_full_plan_and_reconciliation(self):
        inventory, model = fixture()
        default = plan(model, "browser")
        expanded = plan(model, "browser", 20000)
        self.assertEqual(expanded.pop("max_states"), 20000)
        self.assertEqual(expanded, default)
        expanded["max_states"] = 20000
        self.assertTrue(reconcile(inventory, model, expanded, evidence(inventory, expanded))["complete"])
        expanded["scenarios"].pop()
        with self.assertRaisesRegex(ValueError, "derived scenarios"):
            reconcile(inventory, model, expanded, None)

    def test_invalid_or_exhausted_budget_never_emits_a_plan(self):
        _, model = fixture()
        for budget in [0, -1, True, 1.5, "20000", None]:
            with self.subTest(budget=budget), self.assertRaisesRegex(ValueError, "positive integer"):
                plan(model, "browser", budget)
        with self.assertRaisesRegex(ValueError, "state budget exceeded"):
            plan(model, "browser", 2)
        self.assertEqual(plan(model, "browser", 3)["states_explored"], 3)

    def test_cli_budget_and_reconciliation_reject_understated_limit(self):
        inventory, model = fixture()
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "model.json", Path(directory) / "plan.json"
            source.write_text(json.dumps(model))
            args = ["plan", str(source), "--target", "browser", "--out", str(output)]
            self.assertEqual(main([*args, "--max-states", "2"]), 1)
            self.assertFalse(output.exists())
            self.assertEqual(main([*args, "--max-states", "3"]), 0)
            generated = json.loads(output.read_text())
            self.assertEqual(generated["max_states"], 3)
            generated["max_states"] = 2
            with self.assertRaisesRegex(ValueError, "state budget exceeded"):
                reconcile(inventory, model, generated, None)


class FlowTests(unittest.TestCase):
    def test_run_gives_runner_the_exact_canonical_plan_bytes(self) -> None:
        inventory, model = fixture()
        execution_plan = plan(model, "browser")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan_path, inventory_path, out = root / "plan.json", root / "inventory.json", root / "run.json"
            plan_path.write_text(json.dumps(execution_plan, indent=2))
            inventory_path.write_text(json.dumps(inventory))

            def runner(_command, *, env, **_kwargs):
                raw = Path(env["CAPCOV_FLOW_PLAN"]).read_bytes()
                self.assertEqual(hashlib.sha256(raw).hexdigest(), digest(execution_plan))
                Path(env["CAPCOV_FLOW_OUT"]).write_text(json.dumps({
                    **evidence(inventory, execution_plan), "nonce": env["CAPCOV_FLOW_NONCE"]
                }))
                return type("Result", (), {"returncode": 0})()

            with patch("capcov.flows.cli.discover", return_value=inventory), patch(
                "capcov.flows.cli.subprocess.run", side_effect=runner
            ):
                self.assertEqual(main([
                    "run", str(plan_path), "--inventory", str(inventory_path),
                    "--config", "unused.json", "--out", str(out), "--", "runner",
                ]), 0)

    def test_diagnostic_execution_never_qualifies_even_when_all_scenarios_pass(self) -> None:
        inventory, model = fixture()
        generated = plan(model, "browser")
        run = evidence(inventory, generated)
        self.assertTrue(reconcile(inventory, model, generated, run)["complete"])
        run["execution_scope"] = "diagnostic"
        result = reconcile(inventory, model, generated, run)
        self.assertEqual(result["summary"]["covered"], 0)
        self.assertFalse(result["complete"])
        self.assertIn("diagnostic execution does not qualify coverage", result["failures"])
        run["scenarios"] = run["scenarios"][:1]
        self.assertEqual(reconcile(inventory, model, generated, run)["summary"]["covered"], 0)

    def test_execution_scope_validation_and_mount_credit(self) -> None:
        inventory, model = fixture()
        inventory["obligations"].extend([
            {"id": "http:GET /", "kind": "surface"},
            {"id": "boundary:python:mounted-route-confirmation", "kind": "unresolved"},
        ])
        generated = plan(model, "browser")
        run = evidence(inventory, generated)
        run["mounted_surfaces"] = ["http:GET /"]
        run["execution_scope"] = "full"
        result = reconcile(inventory, model, generated, run)
        self.assertTrue(any(row["resolution"] == "runtime-mount-census" for row in result["rows"]))
        run["execution_scope"] = "diagnostic"
        result = reconcile(inventory, model, generated, run)
        self.assertEqual(result["summary"]["covered"], 0)
        self.assertTrue(all(row["resolution"] is None for row in result["rows"]))
        for scope in (None, "selected", "", False, []):
            with self.subTest(scope=scope), self.assertRaisesRegex(ValueError, "execution scope"):
                run["execution_scope"] = scope
                reconcile(inventory, model, generated, run)

    def test_every_declared_outcome_requires_its_own_evidence(self) -> None:
        inventory, model = fixture()
        inventory["obligations"][0]["outcomes"] = ["allowed", "denied"]
        execution_plan = plan(model, "browser")
        result = reconcile(inventory, model, execution_plan, evidence(inventory, execution_plan))
        self.assertFalse(result["complete"])
        model["transitions"][0]["covers_outcomes"] = {"read": ["allowed"]}
        execution_plan = plan(model, "browser")
        self.assertFalse(
            reconcile(inventory, model, execution_plan, evidence(inventory, execution_plan))[
                "complete"
            ]
        )
        model["transitions"][2]["covers_outcomes"] = {"read": ["denied"]}
        execution_plan = plan(model, "browser")
        self.assertTrue(
            reconcile(inventory, model, execution_plan, evidence(inventory, execution_plan))[
                "complete"
            ]
        )

    def test_mapping_an_unknown_boundary_to_an_assertion_cannot_close_it(self) -> None:
        inventory, model = fixture()
        inventory["obligations"][0]["kind"] = "unresolved"
        execution_plan = plan(model, "browser")
        self.assertFalse(
            reconcile(inventory, model, execution_plan, evidence(inventory, execution_plan))[
                "complete"
            ]
        )

    def test_http_claim_requires_observation_in_the_same_step(self) -> None:
        inventory, model = fixture()
        inventory["obligations"].append({"id": "http:POST /edit", "kind": "surface"})
        model["transitions"][1]["obligations"].append("http:POST /edit")
        execution_plan = plan(model, "browser")
        run = evidence(inventory, execution_plan)
        self.assertFalse(reconcile(inventory, model, execution_plan, run)["complete"])
        for scenario, result in zip(execution_plan["scenarios"], run["scenarios"], strict=True):
            result["observed_requests"] = [
                {"step": f"{i}:{step['transition']}", "surface": "http:POST /edit"}
                for i, step in enumerate(scenario["steps"])
                if step["transition"] == "edit"
            ]
        self.assertTrue(reconcile(inventory, model, execution_plan, run)["complete"])
        run["mounted_surfaces"] = ["http:POST /edit", "http:DELETE /new"]
        self.assertIn(
            "runtime-only surface: http:DELETE /new",
            reconcile(inventory, model, execution_plan, run)["failures"],
        )

    def test_baseline_never_hides_new_or_obsolete_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            coverage, baseline = root / "coverage.json", root / "baseline.json"
            coverage.write_text(json.dumps({"complete": False, "failures": ["unmapped: old"]}))
            baseline.write_text(json.dumps({"unmapped: old": "Existing screen; awaiting a flow"}))
            args = ["gate", str(coverage), "--baseline", str(baseline)]
            with patch("builtins.print"):
                self.assertEqual(main(args), 0)
                coverage.write_text(
                    json.dumps(
                        {
                            "complete": False,
                            "failures": [
                                "unmapped: old",
                                "unmapped: new",
                            ],
                        }
                    )
                )
                self.assertEqual(main(args), 1)
                coverage.write_text(json.dumps({"complete": True, "failures": []}))
                self.assertEqual(main(args), 1)

    def test_branch_and_exception_use_parent_route_http_evidence(self) -> None:
        for kind in ("branch-candidate", "exception-candidate"):
            with self.subTest(kind=kind):
                inventory, model = fixture()
                route = "http:POST /edit"
                candidate = route + ":" + kind + ":7"
                inventory["obligations"].extend([
                    {"id": route, "kind": "surface"},
                    {"id": candidate, "kind": kind, "surface": route},
                ])
                model["transitions"][0]["obligations"].append(route)
                model["transitions"][1]["obligations"].append(candidate)
                execution_plan = plan(model, "browser")
                run = evidence(inventory, execution_plan)
                for scenario, result in zip(execution_plan["scenarios"], run["scenarios"], strict=True):
                    result["observed_requests"] = [
                        {"step": f"{i}:{step['transition']}", "surface": route}
                        for i, step in enumerate(scenario["steps"])
                        if step["transition"] in {"login", "edit"}
                    ]
                result = reconcile(inventory, model, execution_plan, run)
                self.assertTrue(result["complete"], result["failures"])
                # A claimed branch still needs the real parent request in the
                # same step; another scenario/step cannot supply that evidence.
                for result in run["scenarios"]:
                    result["observed_requests"] = [
                        request for request in result["observed_requests"]
                        if not request["step"].endswith(":edit")
                    ]
                result = reconcile(inventory, model, execution_plan, run)
                self.assertFalse(result["complete"])
                self.assertEqual(
                    next(row["status"] for row in result["rows"] if row["id"] == candidate),
                    "unproven",
                )

    def test_http_candidate_without_valid_parent_is_rejected(self) -> None:
        inventory, model = fixture()
        inventory["obligations"].append({
            "id": "http:POST /edit:except:7", "kind": "exception-candidate",
        })
        with self.assertRaisesRegex(ValueError, "parent surface"):
            reconcile(inventory, model, plan(model, "browser"), None)

    def test_run_rejects_success_without_fresh_evidence(self) -> None:
        inventory, model = fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {name: root / f"{name}.json" for name in ("inventory", "plan", "out")}
            paths["inventory"].write_text(json.dumps(inventory))
            paths["plan"].write_text(json.dumps(plan(model, "browser")))
            # Even an apparently successful previous output must not be read.
            paths["out"].write_text(json.dumps({"status": "passed"}))
            args = [
                "run",
                str(paths["plan"]),
                "--inventory",
                str(paths["inventory"]),
                "--config",
                "unused.json",
                "--out",
                str(paths["out"]),
                "--",
                "runner",
            ]
            with (
                patch("capcov.flows.cli.discover", return_value=inventory),
                patch("capcov.flows.cli.subprocess.run") as command,
                patch("builtins.print"),
            ):
                command.return_value.returncode = 0
                self.assertEqual(main(args), 1)
                self.assertFalse(paths["out"].exists())

    def test_empty_inventory_is_not_complete(self) -> None:
        _, model = fixture()
        with self.assertRaisesRegex(ValueError, "empty inventory"):
            reconcile({"obligations": []}, model, plan(model, "browser"), None)

    def test_duplicate_scenarios_are_rejected(self) -> None:
        inventory, model = fixture()
        execution_plan = plan(model, "browser")
        run = evidence(inventory, execution_plan)
        run["scenarios"].append(copy.deepcopy(run["scenarios"][0]))
        self.assertFalse(reconcile(inventory, model, execution_plan, run)["complete"])

    def test_history_requires_login_and_edit_in_order(self) -> None:
        _, model = fixture()
        result = plan(model, "browser")
        history = next(s for s in result["scenarios"] if s["id"] == "history")
        self.assertEqual([s["transition"] for s in history["steps"]], ["login", "edit", "history"])

    def test_missing_dependency_is_blocked_not_skipped(self) -> None:
        _, model = fixture()
        model["transitions"][0]["bindings"] = {}
        result = plan(model, "browser")
        self.assertEqual(len(result["blocked"]), 3)
        self.assertEqual(result["scenarios"], [])

    def test_forbidden_state_forces_another_path(self) -> None:
        _, model = fixture()
        model["transitions"][2]["forbids"] = ["signed-in"]
        self.assertIn("history", [b["transition"] for b in plan(model, "browser")["blocked"]])
        model["transitions"][1]["removes"] = ["signed-in"]
        self.assertEqual(plan(model, "browser")["blocked"], [])

    def test_state_explosion_fails_loudly(self) -> None:
        _, model = fixture()
        with self.assertRaisesRegex(ValueError, "budget"):
            plan(model, "browser", max_states=1)

    def test_action_without_assertion_is_not_a_test(self) -> None:
        _, model = fixture()
        model["transitions"][0]["bindings"]["browser"]["commands"] = [{"op": "goto", "path": "/"}]
        with self.assertRaisesRegex(ValueError, "assertion"):
            plan(model, "browser")

    def test_absence_assertions_are_planned_and_require_execution(self) -> None:
        inventory, model = fixture()
        command = {
            "op": "assert", "mode": "absent", "id": "no-privileged-control",
            "selector": "form[data-privileged]",
        }
        model["transitions"][0]["bindings"]["browser"]["commands"] = [command]
        execution_plan = plan(model, "browser")
        for scenario in execution_plan["scenarios"]:
            self.assertEqual(scenario["steps"][0]["commands"], [command])
        run = evidence(inventory, execution_plan)
        self.assertTrue(reconcile(inventory, model, execution_plan, run)["complete"])
        run["scenarios"][0]["assertions"].remove("0:login:no-privileged-control")
        result = reconcile(inventory, model, execution_plan, run)
        self.assertFalse(result["complete"])
        self.assertTrue(result["failures"])

    def test_unknown_assertion_modes_fail_closed(self) -> None:
        for mode in ("missing", True, None, []):
            with self.subTest(mode=mode):
                _, model = fixture()
                command = model["transitions"][0]["bindings"]["browser"]["commands"][0]
                command["mode"] = mode
                with self.assertRaisesRegex(ValueError, "assertion mode"):
                    plan(model, "browser")

    def test_absence_assertion_cannot_also_claim_text(self) -> None:
        _, model = fixture()
        command = model["transitions"][0]["bindings"]["browser"]["commands"][0]
        command["mode"] = "absent"
        with self.assertRaisesRegex(ValueError, "absence assertion"):
            plan(model, "browser")

    def test_drag_is_preserved_and_bound_to_evidence(self) -> None:
        inventory, model = fixture()
        commands = model["transitions"][0]["bindings"]["browser"]["commands"]
        command = {"op": "drag", "selector": "#drawing", "from": [0.1, 0.2], "to": [0.8, 0.7]}
        commands.insert(0, command)
        execution_plan = plan(model, "browser")
        self.assertEqual(execution_plan["scenarios"][0]["steps"][0]["commands"][0], command)
        run = evidence(inventory, execution_plan)
        self.assertTrue(reconcile(inventory, model, execution_plan, run)["complete"])
        command["to"] = [0.7, 0.7]
        with self.assertRaises(ValueError):
            reconcile(inventory, model, plan(model, "browser"), run)

    def test_drag_requires_bounded_distinct_numeric_pairs(self) -> None:
        for point in (None, [], [0], [0, 1, 2], [True, 0], ["0", 0],
                      [-0.1, 0], [1.1, 0], [float("nan"), 0], [float("inf"), 0]):
            with self.subTest(point=point):
                _, model = fixture()
                model["transitions"][0]["bindings"]["browser"]["commands"].insert(
                    0, {"op": "drag", "selector": "#drawing", "from": point, "to": [1, 1]})
                with self.assertRaisesRegex(ValueError, "drag requires"):
                    plan(model, "browser")
        _, model = fixture()
        model["transitions"][0]["bindings"]["browser"]["commands"].insert(
            0, {"op": "drag", "selector": "#drawing", "from": [0, 0], "to": [0, 0]})
        with self.assertRaisesRegex(ValueError, "drag requires"):
            plan(model, "browser")

    def test_confirmation_is_preserved_and_bound_to_evidence(self) -> None:
        inventory, model = fixture()
        commands = model["transitions"][0]["bindings"]["browser"]["commands"]
        command = {"op": "click", "selector": "button.delete",
                   "confirmation": {"message": "Delete this sample?", "action": "dismiss"}}
        commands.insert(0, command)
        execution_plan = plan(model, "browser")
        self.assertEqual(execution_plan["scenarios"][0]["steps"][0]["commands"][0], command)
        run = evidence(inventory, execution_plan)
        self.assertTrue(reconcile(inventory, model, execution_plan, run)["complete"])
        command["confirmation"]["action"] = "accept"
        with self.assertRaises(ValueError):
            reconcile(inventory, model, plan(model, "browser"), run)

    def test_confirmation_contract_rejects_ambiguous_commands(self) -> None:
        invalid = [None, True, {}, {"message": "x"}, {"message": "", "action": "accept"},
                   {"message": "x" * 1025, "action": "accept"},
                   {"message": 1, "action": "accept"},
                   {"message": "x", "action": True}, {"message": "x", "action": "guess"},
                   {"message": "x", "action": "accept", "prompt": "value"}]
        for confirmation in invalid:
            with self.subTest(confirmation=confirmation):
                _, model = fixture()
                model["transitions"][0]["bindings"]["browser"]["commands"].insert(
                    0, {"op": "click", "selector": "button", "confirmation": confirmation})
                with self.assertRaisesRegex(ValueError, "confirmation"):
                    plan(model, "browser")
        _, model = fixture()
        model["transitions"][0]["bindings"]["browser"]["commands"][0]["confirmation"] = {
            "message": "x", "action": "accept"}
        with self.assertRaisesRegex(ValueError, "confirmation"):
            plan(model, "browser")

    def test_refresh_assertion_preserves_deadline_and_requires_evidence(self) -> None:
        inventory, model = fixture()
        command = model["transitions"][0]["bindings"]["browser"]["commands"][0]
        command["refresh_timeout_ms"] = 20_000
        execution_plan = plan(model, "browser")
        self.assertEqual(execution_plan["scenarios"][0]["steps"][0]["commands"][0], command)
        run = evidence(inventory, execution_plan)
        self.assertTrue(reconcile(inventory, model, execution_plan, run)["complete"])
        run["scenarios"][0]["assertions"] = []
        self.assertFalse(reconcile(inventory, model, execution_plan, run)["complete"])
        command["refresh_timeout_ms"] = 10_000
        with self.assertRaises(ValueError):
            reconcile(inventory, model, plan(model, "browser"), run)

    def test_refresh_requires_a_bounded_integer_deadline_on_an_assertion(self) -> None:
        for timeout in (None, True, False, 0, -1, 60_001, 1.5, "20000", []):
            with self.subTest(timeout=timeout):
                _, model = fixture()
                command = model["transitions"][0]["bindings"]["browser"]["commands"][0]
                command["refresh_timeout_ms"] = timeout
                with self.assertRaisesRegex(ValueError, "refresh_timeout_ms"):
                    plan(model, "browser")
        _, model = fixture()
        model["transitions"][0]["bindings"]["browser"]["commands"].insert(
            0, {"op": "click", "selector": "button", "refresh_timeout_ms": 1000}
        )
        with self.assertRaisesRegex(ValueError, "refresh_timeout_ms"):
            plan(model, "browser")

    def test_download_assertion_requires_exact_planned_evidence(self) -> None:
        inventory, model = fixture()
        command = {"op": "assert", "mode": "download", "id": "export-bytes",
                   "selector": "a.export", "file": "fixtures/expected.csv"}
        model["transitions"][0]["bindings"]["browser"]["commands"] = [command]
        execution_plan = plan(model, "browser")
        self.assertEqual(execution_plan["scenarios"][0]["steps"][0]["commands"], [command])
        run = evidence(inventory, execution_plan)
        self.assertTrue(reconcile(inventory, model, execution_plan, run)["complete"])
        run["scenarios"][0]["assertions"] = []
        self.assertFalse(reconcile(inventory, model, execution_plan, run)["complete"])

    def test_download_assertion_rejects_unsafe_or_contradictory_inputs(self) -> None:
        for fields in ({}, {"file": "../x"}, {"file": "/tmp/x"}, {"file": "C:/x"},
                       {"file": "a/./b"}, {"file": "a//b"}, {"file": []},
                       {"file": "a.csv", "text": "partial"},
                       {"file": "a.csv", "refresh_timeout_ms": 1000}):
            with self.subTest(fields=fields):
                _, model = fixture()
                model["transitions"][0]["bindings"]["browser"]["commands"] = [
                    {"op": "assert", "mode": "download", "id": "download",
                     "selector": "a.export", **fields}]
                with self.assertRaisesRegex(ValueError, "download assertion"):
                    plan(model, "browser")

    def test_http_assertion_preserves_reviewed_request_and_requires_evidence(self) -> None:
        inventory, model = fixture()
        command = {"op": "assert", "mode": "http", "id": "refused",
                   "method": "POST", "path": "/items/1/base", "form": {"price": "4.99"},
                   "expect_status": 403, "text": "not permitted"}
        model["transitions"][0]["bindings"]["browser"]["commands"] = [command]
        execution_plan = plan(model, "browser")
        self.assertEqual(execution_plan["scenarios"][0]["steps"][0]["commands"], [command])
        run = evidence(inventory, execution_plan)
        self.assertTrue(reconcile(inventory, model, execution_plan, run)["complete"])
        run["scenarios"][0]["assertions"] = []
        self.assertFalse(reconcile(inventory, model, execution_plan, run)["complete"])

    def test_http_assertion_rejects_ambiguous_or_unsafe_requests(self) -> None:
        bad = [{"path": p} for p in [None, [], "//host/x", "https://host/x", "/x?token=y", "/x#f", "/a/../b", "/x%2fy", "/a//b"]]
        bad += [{"expect_status": s} for s in [True, "403", 199, 302, 600, 403.5]]
        bad += [{"method": []}, {"method": "DELETE"}, {"method": "get"}, {"text": ""}, {"text": []},
                {"form": []}, {"form": {"x": 1}}, {"form": {"": "x"}},
                {"form": {"x": "a" * 4097}}, {"method": "GET"},
                {"selector": "body"}, {"headers": {}}, {"body": "x"},
                {"refresh_timeout_ms": 10}]
        for fields in bad:
            with self.subTest(fields=fields):
                _, model = fixture()
                command = {"op": "assert", "mode": "http", "id": "refused", "method": "POST",
                           "path": "/items/1", "form": {"x": "y"}, "expect_status": 403,
                           "text": "refused", **fields}
                model["transitions"][0]["bindings"]["browser"]["commands"] = [command]
                with self.assertRaisesRegex(ValueError, "HTTP assertion"):
                    plan(model, "browser")

    def test_saved_paths_are_actions_and_preserve_expected_status(self) -> None:
        inventory, model = fixture()
        commands = model["transitions"][0]["bindings"]["browser"]["commands"]
        commands[:0] = [{"op": "remember-path", "name": "invoice"},
                       {"op": "visit-path", "name": "invoice", "expect_status": 404}]
        execution_plan = plan(model, "browser")
        self.assertEqual(execution_plan["scenarios"][0]["steps"][0]["commands"], commands)
        run = evidence(inventory, execution_plan)
        self.assertTrue(reconcile(inventory, model, execution_plan, run)["complete"])
        commands.pop()
        with self.assertRaisesRegex(ValueError, "assertion"):
            plan(model, "browser")

    def test_saved_paths_reject_invalid_names_statuses_and_ambiguous_sources(self) -> None:
        invalid = [{"op": "remember-path", "name": value}
                   for value in (None, "", "a/b", "a.b", "A", "a" * 65, True)]
        invalid += [{"op": "visit-path", "name": "invoice", "expect_status": value}
                    for value in (None, True, "404", 199, 600, 200.5)]
        invalid += [{"op": "remember-path", "name": "invoice", "path": "/fallback"},
                    {"op": "remember-path", "name": "invoice", "expect_status": 200}]
        for command in invalid:
            with self.subTest(command=command):
                _, model = fixture()
                model["transitions"][0]["bindings"]["browser"]["commands"].insert(0, command)
                with self.assertRaises(ValueError):
                    plan(model, "browser")

    def test_duplicate_transition_is_rejected(self) -> None:
        _, model = fixture()
        model["transitions"].append(copy.deepcopy(model["transitions"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            plan(model, "browser")

    def test_complete_requires_exact_outcome_evidence(self) -> None:
        inventory, model = fixture()
        execution_plan = plan(model, "browser")
        run = evidence(inventory, execution_plan)
        self.assertTrue(reconcile(inventory, model, execution_plan, run)["complete"])
        run["scenarios"][0]["assertions"] = []
        self.assertFalse(reconcile(inventory, model, execution_plan, run)["complete"])

    def test_new_capability_cannot_hide_behind_existing_entity(self) -> None:
        inventory, model = fixture()
        execution_plan = plan(model, "browser")
        inventory["obligations"].append({"id": "delete"})
        result = reconcile(inventory, model, execution_plan, evidence(inventory, execution_plan))
        self.assertIn("unmapped: delete", result["failures"])

    def test_no_execution_means_no_coverage(self) -> None:
        inventory, model = fixture()
        result = reconcile(inventory, model, plan(model, "browser"), None)
        self.assertEqual(result["summary"]["covered"], 0)

    def test_stale_model_inventory_or_plan_cannot_pass(self) -> None:
        inventory, model = fixture()
        execution_plan = plan(model, "browser")
        run = evidence(inventory, execution_plan)
        for altered in ("inventory", "model", "plan"):
            i, m, p = copy.deepcopy((inventory, model, execution_plan))
            if altered == "inventory":
                i["obligations"].append({"id": "new"})
            elif altered == "model":
                m["scope"] = "changed"
            else:
                p["scenarios"].pop()
            with self.subTest(altered=altered), self.assertRaises(ValueError):
                reconcile(i, m, p, run)

    def test_failed_or_skipped_run_cannot_cover_anything(self) -> None:
        inventory, model = fixture()
        execution_plan = plan(model, "browser")
        run = evidence(inventory, execution_plan)
        run["status"] = "failed"
        self.assertEqual(reconcile(inventory, model, execution_plan, run)["summary"]["covered"], 0)
        run["status"] = "passed"
        run["scenarios"].pop()
        self.assertFalse(reconcile(inventory, model, execution_plan, run)["complete"])

    @unittest.skipUnless(_HAVE_TS, "treesitter extra not installed")
    def test_discovery_retains_branches_exceptions_and_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            route = root / "routes.py"
            route.write_text(
                '@router.post("/edit")\ndef edit():\n'
                "    if authenticated:\n        return save()\n"
                "    try:\n        reject()\n    except Error:\n        return False\n"
            )
            config = root / "discovery.json"
            config.write_text(
                json.dumps(
                    {
                        "scope": "fixture",
                        "root": ".",
                        "adapters": [
                            {
                                "kind": "treesitter-routes",
                                "language": "python",
                                "files": ["routes.py"],
                                "query": PY_HANDLER_QUERY,
                            },
                        ],
                    }
                )
            )
            inventory = discover(config)
            kinds = [o["kind"] for o in inventory["obligations"]]
            self.assertEqual(kinds.count("branch-candidate"), 2)
            self.assertEqual(kinds.count("exception-candidate"), 1)
            self.assertIn("unresolved", kinds)
            route.write_text('@router.get("/new")\ndef new():\n    return 1\n')
            self.assertNotEqual(inventory, discover(config))

    @unittest.skipUnless(_HAVE_TS, "treesitter extra not installed")
    def test_multiple_route_mounts_preserve_surfaces_and_shared_limits(self) -> None:
        # The same source read at two mount points. Tree-sitter has no path
        # prefix; distinct mounts are namespaced by id_prefix, and the four route
        # discovery limits are still emitted once for the combined inventory.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "routes.py").write_text(
                '@router.get("/items")\ndef items():\n'
                '    if allowed:\n        return []\n    return None\n'
            )
            config = root / "discovery.json"
            settings = {
                "scope": "two-mounts", "root": ".",
                "adapters": [
                    {
                        "kind": "treesitter-routes", "language": "python",
                        "files": ["routes.py"], "query": PY_HANDLER_QUERY,
                        "id_prefix": prefix,
                    }
                    for prefix in ("portal:", "admin:")
                ],
            }
            config.write_text(json.dumps(settings))
            inventory = discover(config)
            surfaces = {o["id"] for o in inventory["obligations"] if o["kind"] == "surface"}
            self.assertEqual(surfaces, {"portal:/items", "admin:/items"})
            branches = [o for o in inventory["obligations"] if o["kind"] == "branch-candidate"]
            self.assertEqual(len(branches), 4)
            self.assertEqual({o["surface"] for o in branches}, surfaces)
            limits = [o for o in inventory["obligations"] if o["kind"] == "unresolved"]
            self.assertEqual(len(limits), 4)
            self.assertIn("boundary:python:mounted-route-confirmation", {o["id"] for o in limits})
            # Only global discovery limits are shared. Colliding route declarations
            # still fail instead of silently shrinking the coverage denominator.
            settings["adapters"][1]["id_prefix"] = "portal:"
            config.write_text(json.dumps(settings))
            with self.assertRaisesRegex(ValueError, "duplicate obligation IDs"):
                discover(config)

    def test_unknown_adapter_does_not_produce_empty_green(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "discovery.json"
            config.write_text(json.dumps({"root": ".", "adapters": [{"kind": "magic"}]}))
            with self.assertRaisesRegex(ValueError, "unsupported"):
                discover(config)
