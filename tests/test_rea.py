"""REA import contract and discovery -> reconcile -> gate regressions."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from capcov import cli
from capcov.core.gate import gate
from capcov.core.reconcile import reconcile
from capcov.flows.discovery import discover as flow_discover
from capcov_contrib.rea import discover, discover_flows


def evidence(result, operation="inspect_analysis_view", eid="a"):
    return {
        "evidence_id": "ev_" + eid * 64,
        "subject": {
            "name": "fixture",
            "digest": {"sha256": "b" * 64},
            "local_path": "/fixture",
            "format": "file",
            "architecture": None,
        },
        "provider": {"id": "rea", "name": "REA", "version": "1"},
        "analysis_profile": None,
        "predicate_type": "rea.analysis-view",
        "operation": operation,
        "parameters": {},
        "raw_result": None,
        "normalized_result": result,
        "confidence": "inferred",
        "authority": "shipped-artifact",
        "environment": None,
        "limitations": ["Grouping does not establish handler identity."],
        "relationship_coverage": None,
        "locations": [{"kind": "artifact-path", "path": "fixture.har"}],
        "evidence_links": [],
    }


def api_view():
    return {
        "kind": "api-surface",
        "parent_evidence_id": "ev_" + "c" * 64,
        "coverage": {
            "status": "page",
            "examined": 1,
            "total": 1,
            "next_offset": None,
            "exhausted": True,
        },
        "records": {"examined": 1, "total": 1, "excluded": 0},
        "excluded_records": [],
        "groups": [
            {
                "method": "GET",
                "origin": "https://fixture.invalid",
                "path_template": "/items/{numeric}",
                "templated_segments": [
                    {
                        "segment_index": 1,
                        "rule": "numeric",
                        "basis": "inferred",
                        "observed_values": ["42"],
                    }
                ],
            }
        ],
    }


class ReaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {"document": "rea.json", "globs": ["rea.json"]}
        self.write(evidence(api_view()))

    def write(self, data):
        (self.root / "rea.json").write_text(json.dumps(data))

    def test_published_rea_generated_view(self):
        fixture = Path(__file__).parent / "fixtures" / "rea-api-view.json"
        self.write(json.loads(fixture.read_text()))
        imported = discover_flows(self.config, self.root)
        candidate = imported["obligations"][0]
        self.assertEqual(candidate["path"], "/api/items/{numeric}")
        self.assertEqual(
            candidate["rea"]["evidence_id"],
            "ev_6f705da74de4c9d0b70fcc9879845c3bbae044dd1a04774269f52be4efac7c45",
        )
        self.assertFalse(
            any(u["id"].endswith(":pagination") for u in imported["unresolved"])
        )

    def test_provenance_and_inference_survive_both_interfaces(self):
        imported = discover_flows(self.config, self.root)
        candidate = imported["obligations"][0]
        self.assertEqual(candidate["rea"]["confidence"], "inferred")
        self.assertEqual(candidate["rea"]["subject"]["digest"]["sha256"], "b" * 64)
        self.assertEqual(candidate["rea"]["locations"][0]["path"], "fixture.har")
        self.assertEqual(candidate["source"]["pointer"], "/normalized_result/groups/0")
        sha = hashlib.sha256((self.root / "rea.json").read_bytes()).hexdigest()
        self.assertEqual(imported["sources"], {"rea.json": sha})
        core = discover(self.root, self.root, self.config)
        self.assertEqual(core["surfaces"][0]["rea"], candidate["rea"])
        self.assertEqual(core["_ops"], {})
        self.assertTrue(
            any("handler identity" in u["reason"] for u in core["unresolved"])
        )

    def test_real_cli_pipeline_never_counts_import_as_runtime_evidence(self):
        (self.root / "capcov.toml").write_text("""[capcov]
source = "."
[[adapters]]
name = "rea"
plugin = "capcov_contrib.rea:discover"
document = "rea.json"
globs = ["rea.json"]
""")
        out = self.root / "capabilities.json"
        self.assertEqual(
            cli.main(
                ["discover", "--target", str(self.root), "--out", str(out), "--quiet"]
            ),
            0,
        )
        static = json.loads(out.read_text())
        coverage = reconcile(static, {"bindings": []})
        self.assertEqual(coverage["summary"]["static_only"], 1)
        self.assertTrue(gate(coverage, None))
        sid = static["surfaces"][0]["id"]
        observed = {
            "bindings": [
                {
                    "surface": sid,
                    "entity": sid,
                    "operations": [],
                    "tests": ["reviewed:test"],
                }
            ]
        }
        matched = reconcile(static, observed)
        self.assertEqual(matched["summary"]["both"], 1)
        self.assertTrue(
            gate(matched, None), "Matching identities must not waive REA limits"
        )
        old_hash = static["derived_from"]["artifact_sha256"]
        data = evidence(api_view())
        data["limitations"].append("additional caveat")
        self.write(data)
        cli.main(["discover", "--target", str(self.root), "--out", str(out), "--quiet"])
        self.assertNotEqual(
            old_hash, json.loads(out.read_text())["derived_from"]["artifact_sha256"]
        )

    def test_flows_plugin_dispatch_hashes_source(self):
        cfg = self.root / "discovery.json"
        cfg.write_text(
            json.dumps(
                {
                    "scope": "rea",
                    "root": ".",
                    "adapters": [
                        {
                            **self.config,
                            "kind": "rea",
                            "plugin": "capcov_contrib.rea:discover_flows",
                        }
                    ],
                }
            )
        )
        inventory = flow_discover(cfg)
        self.assertEqual(
            len([o for o in inventory["obligations"] if o["kind"] == "surface"]), 1
        )
        self.assertIn("rea.json", inventory["sources"])

    def test_origin_identity_and_pagination_are_preserved(self):
        view = api_view()
        second = copy.deepcopy(view["groups"][0])
        second["origin"] = "https://another.invalid"
        view["groups"].append(second)
        view["coverage"].update(examined=2, total=3, next_offset=2, exhausted=False)
        view["records"].update(total=5, excluded=1)
        self.write(evidence(view))
        result = discover_flows(self.config, self.root)
        self.assertEqual(len({o["id"] for o in result["obligations"]}), 2)
        reasons = " ".join(u["reason"] for u in result["unresolved"])
        self.assertIn("paginated", reasons)
        self.assertIn("omitted or excluded", reasons)

    def test_bundle_unknowns_and_unsupported_records_remain_visible(self):
        record = evidence({"functions": []}, "analyze_function", "d")
        self.write(
            {
                "records": [evidence(api_view()), record],
                "unknowns": [
                    {
                        "unknown_id": "unk_" + "e" * 64,
                        "status": "open",
                        "question": "permission?",
                    }
                ],
            }
        )
        result = discover_flows(self.config, self.root)
        self.assertEqual(
            result["obligations"][0]["source"]["pointer"],
            "/records/0/normalized_result/groups/0",
        )
        self.assertTrue(
            any("analyze_function" in u["reason"] for u in result["unresolved"])
        )
        self.assertTrue(
            any(
                u["rea"].get("unknown", {}).get("question") == "permission?"
                for u in result["unresolved"]
            )
        )

    def test_javascript_nodes_keep_exact_observation_provenance(self):
        observation = {
            "authority": "analyst-inference",
            "confidence": "inferred",
            "limitations": ["dynamic IPC"],
            "location": {"available": False, "reason": "unavailable"},
            "coverage": {"status": "partial"},
        }
        result = {
            "graph": {
                "schema": "JavaScriptApplicationGraph",
                "nodes": [
                    {
                        "node_id": "jag_node_" + "d" * 64,
                        "kind": "ipc-handler",
                        "identity": {"key": "items:list"},
                        "observations": [{"evidence": observation}],
                    }
                ],
                "coverage": {"status": "partial"},
                "limitations": [],
            },
            "limitations": [],
        }
        self.write(evidence(result, "analyze_javascript_application"))
        imported = discover_flows(self.config, self.root)
        self.assertEqual(
            imported["obligations"][0]["rea"]["observations"], [observation]
        )
        self.assertTrue(
            any("partial or unknown" in u["reason"] for u in imported["unresolved"])
        )

    def test_empty_input_keeps_a_failing_boundary(self):
        self.write({"records": [], "unknowns": []})
        result = discover(self.root, self.root, self.config)
        self.assertEqual(result["surfaces"], [])
        self.assertTrue(
            any(u["id"].endswith(":no-surfaces") for u in result["unresolved"])
        )

    def test_rejects_errors_duplicate_ids_keys_and_invalid_records(self):
        cases = [
            {"error": "failure", "partial_observation": evidence(api_view())},
            {"records": [evidence(api_view()), evidence(api_view())]},
            {"records": [evidence(api_view())], "unknowns": {}},
            {"evidence_id": "ev_" + "a" * 64},
            evidence({"kind": "api-surface", "groups": [{}]}),
        ]
        for data in cases:
            with self.subTest(data=data):
                self.write(data)
                with self.assertRaises(ValueError):
                    discover_flows(self.config, self.root)
        (self.root / "rea.json").write_text('{"records":[],"records":[]}')
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            discover_flows(self.config, self.root)

    def test_hash_pin_and_source_scope_are_enforced(self):
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            discover_flows({**self.config, "sha256": "0" * 64}, self.root)
        with self.assertRaisesRegex(ValueError, "globs must include"):
            discover(self.root, self.root, {"document": "rea.json"})
        with self.assertRaisesRegex(ValueError, "inside the source root"):
            discover_flows({"document": "../outside.json"}, self.root)


if __name__ == "__main__":
    unittest.main()
