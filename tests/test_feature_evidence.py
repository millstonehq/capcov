from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from capcov.artifacts import tree_sha256
from capcov.features.evidence import reconcile
from capcov.flows.model import digest, plan
from capcov.outcomes import provenance


class FeatureEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "src").mkdir()
        (self.root / "src/app.py").write_text("x = 1\n")
        (self.root / "tests").mkdir()
        (self.root / "tests/test_app.py").write_text("def test_save(): pass\n")
        self.inventory = {
            "schema_version": 1, "kind": "capabilities",
            "derived_from": {"artifact": "src", "artifact_sha256": tree_sha256(self.root / "src")[0]},
            "surfaces": [{"id": "POST /save"}, {"id": "GET /unknown"}],
        }
        self.model = {
            "version": 1, "root": "mail", "constraints": [],
            "features": [
                {"id": "mail", "name": "Mail", "parent": None, "decomposition": None, "group": None},
                {"id": "save", "name": "Save", "parent": "mail", "decomposition": "mandatory", "group": None},
            ],
        }
        self.mapping = {
            "version": 1, "scope": "mail", "environment": "fixture",
            "limitations": ["local"], "inputs": ["src", "tests"],
            "outcomes": [{
                "id": "save-ok", "capability": "save", "description": "mail saves",
                "source_refs": ["POST /save"], "policy": "required",
                "tests": ["tests/test_app.py::test_save"],
            }],
        }
        p = provenance(self.root, self.mapping, self.inventory)
        self.run = {"version": 1, "provenance": p, "finished": True, "exit_code": 0,
                    "errors": [], "tests": {"tests/test_app.py::test_save": "passed"}}

    def test_exact_native_evidence_emits_id_sets_and_unknown_discovery(self):
        report = reconcile(self.model, self.mapping, self.inventory, self.run, self.root, {"mail", "save"})
        self.assertTrue(report["behavioral_complete"])
        self.assertFalse(report["complete"])
        save = next(r for r in report["tree_rows"] if r["feature"] == "save")
        self.assertEqual(save["own_outcome_ids"], ["save-ok"])
        self.assertEqual(save["demonstrated_outcome_ids"], ["save-ok"])
        self.assertEqual(report["unknown_discovery"], ["GET /unknown"])
        self.assertIn("inputs_sha256", report["context"])

    def test_missing_evidence_is_not_vacuously_complete(self):
        self.run["tests"] = {}
        report = reconcile(self.model, self.mapping, self.inventory, self.run, self.root, {"mail", "save"})
        self.assertFalse(report["complete"])
        self.assertEqual(report["mandatory_covered"], 0)
        self.assertEqual(report["mandatory_total"], 1)

    def test_selected_leaf_without_outcome_is_an_explicit_gap(self):
        self.mapping["outcomes"][0]["capability"] = "mail"
        self.run["provenance"] = provenance(self.root, self.mapping, self.inventory)
        report = reconcile(self.model, self.mapping, self.inventory, self.run, self.root, {"mail", "save"})
        save = next(r for r in report["tree_rows"] if r["feature"] == "save")
        self.assertFalse(report["complete"])
        self.assertEqual(save["missing_required_outcomes"], ["<no-outcome-defined>"])

    def test_unknown_feature_and_invalid_configuration_are_rejected(self):
        self.mapping["outcomes"][0]["capability"] = "invented"
        with self.assertRaisesRegex(ValueError, "unknown feature"):
            reconcile(self.model, self.mapping, self.inventory, self.run, self.root, {"mail", "save"})
        self.mapping["outcomes"][0]["capability"] = "save"
        with self.assertRaisesRegex(ValueError, "invalid configuration"):
            reconcile(self.model, self.mapping, self.inventory, self.run, self.root, {"mail"})

    def test_changed_inputs_reject_old_pass(self):
        (self.root / "tests/test_app.py").write_text("def test_save(): assert False\n")
        with self.assertRaisesRegex(ValueError, "current map"):
            reconcile(self.model, self.mapping, self.inventory, self.run, self.root, {"mail", "save"})

    def test_raw_native_flow_can_demonstrate_an_outcome(self):
        self.run["tests"] = {}
        flow_inventory = {"obligations": [{"id": "POST /save"}]}
        flow_model = {
            "version": 1, "scope": "mail", "facts": [], "initial": [],
            "evidence_context": {"environment": "fixture", "limitations": ["local"]},
            "transitions": [{
                "id": "save", "requires": [], "adds": [], "actor": "operator",
                "outcome": "saved", "evidence": [{"file": "src/app.py", "line": 1}],
                "obligations": ["POST /save"],
                "bindings": {"browser": {"commands": [
                    {"op": "assert", "id": "saved", "selector": "body", "text": "saved"}
                ]}},
            }],
        }
        execution_plan = plan(flow_model, "browser")
        flow_run = {
            "status": "passed", "assurance": "browser",
            "plan_sha256": digest(execution_plan),
            "inventory_sha256": digest(flow_inventory),
            "scenarios": [{
                "id": scenario["id"], "status": "passed",
                "assertions": ["0:save:saved"], "observed_requests": [],
            } for scenario in execution_plan["scenarios"]],
        }
        self.mapping["outcomes"][0]["tests"] = []
        self.mapping["outcomes"][0]["flow_bindings"] = [
            {"transition": "save", "assertion": "saved"}
        ]
        flow_run["outcome_input_provenance"] = provenance(
            self.root, self.mapping, self.inventory
        )
        report = reconcile(
            self.model, self.mapping, self.inventory, None, self.root,
            {"mail", "save"},
            flow_inputs=(flow_model, execution_plan, flow_inventory, flow_run),
        )
        self.assertTrue(report["behavioral_complete"])
        self.assertEqual(report["evidence_rows"][0]["evidence_sources"], ["browser-flow"])

        flow_run["scenarios"][0]["assertions"] = []
        report = reconcile(
            self.model, self.mapping, self.inventory, None, self.root,
            {"mail", "save"},
            flow_inputs=(flow_model, execution_plan, flow_inventory, flow_run),
        )
        self.assertFalse(report["behavioral_complete"])
        self.assertEqual(report["evidence_rows"][0]["status"], "missing")
        flow_run["scenarios"][0]["assertions"] = ["0:save:saved"]

        self.mapping["outcomes"][0]["flow_bindings"] = [
            {"transition": "save", "assertion": "wrong"}
        ]
        with self.assertRaisesRegex(ValueError, "does not attest current"):
            reconcile(
                self.model, self.mapping, self.inventory, None, self.root,
                {"mail", "save"},
                flow_inputs=(flow_model, execution_plan, flow_inventory, flow_run),
            )
        flow_run["outcome_input_provenance"] = provenance(
            self.root, self.mapping, self.inventory
        )
        with self.assertRaisesRegex(ValueError, "unknown flow binding"):
            reconcile(
                self.model, self.mapping, self.inventory, None, self.root,
                {"mail", "save"},
                flow_inputs=(flow_model, execution_plan, flow_inventory, flow_run),
            )
        flow_model["evidence_context"]["environment"] = "production"
        with self.assertRaisesRegex(ValueError, "context does not match"):
            reconcile(
                self.model, self.mapping, self.inventory, None, self.root,
                {"mail", "save"},
                flow_inputs=(flow_model, execution_plan, flow_inventory, flow_run),
            )

    def test_unresolved_flow_only_outcome_stays_unresolved(self):
        self.mapping["outcomes"][0].update(
            tests=[], policy="unresolved", reason="decision required"
        )
        report = reconcile(
            self.model, self.mapping, self.inventory, None, self.root,
            {"mail", "save"},
        )
        self.assertEqual(report["evidence_rows"][0]["status"], "unresolved")
        self.assertFalse(report["behavioral_complete"])

    def test_frontier_rejects_overlap_and_reports_missing_selected_branch(self):
        with self.assertRaisesRegex(ValueError, "overlap ancestor"):
            reconcile(
                self.model, self.mapping, self.inventory, self.run, self.root,
                {"mail", "save"}, report_features={"mail", "save"},
            )
        report = reconcile(
            self.model, self.mapping, self.inventory, self.run, self.root,
            {"mail", "save"}, report_features={"mail"},
        )
        self.assertEqual(report["report_frontier"], ["mail"])
        self.assertEqual(report["capability_summary"]["demonstrated"], 1)

    def test_cli_reconciles_in_a_real_subprocess(self):
        self.inventory["surfaces"] = [{"id": "POST /save"}]
        self.run["provenance"] = provenance(self.root, self.mapping, self.inventory)
        values = {"model": self.model, "map": self.mapping,
                  "inventory": self.inventory, "run": self.run}
        for name, value in values.items():
            (self.root / f"{name}.json").write_text(json.dumps(value))
        output = self.root / "report.json"
        result = subprocess.run([
            sys.executable, "-m", "capcov", "features", "reconcile",
            str(self.root / "model.json"), "--outcomes-map", str(self.root / "map.json"),
            "--inventory", str(self.root / "inventory.json"), "--run", str(self.root / "run.json"),
            "--target", str(self.root), "--selected", "mail,save", "--out", str(output),
        ], cwd=self.root, text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(json.loads(output.read_text())["complete"])
