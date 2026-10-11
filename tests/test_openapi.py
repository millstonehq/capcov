import json
from pathlib import Path
import tempfile
import unittest

from capcov.flows.catalog import build_catalog
from capcov.flows.discovery import discover
from capcov.flows.openapi import METHODS, derive


class OpenAPIInventoryTests(unittest.TestCase):
    def inventory(self, paths, **extra):
        return derive(json.dumps({"openapi": "3.1.0", "paths": paths, **extra}), "api.json")

    def test_all_methods_keep_exact_paths_and_source_pointers(self):
        rows = self.inventory({"/items/{id}": {method: {} for method in METHODS}})
        surfaces = [row for row in rows if row["kind"] == "surface"]
        self.assertEqual({r["id"] for r in surfaces}, {
            f"http:{m.upper()} /items/{{id}}" for m in METHODS
        })
        self.assertEqual(surfaces[0]["source"]["pointer"], "/paths/~1items~1{id}/delete")
        self.assertEqual(len(rows) - len(surfaces), 6)

    def test_document_is_hashed_and_catalog_requires_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            document = root / "api.json"
            document.write_text(json.dumps({"openapi": "3.0.4", "paths": {"/items": {"get": {}}}}))
            config = root / "discovery.json"
            config.write_text(json.dumps({
                "root": ".", "scope": "contract",
                "source_sets": [{"directory": ".", "extensions": [".json"],
                                 "exclude": {"discovery.json": "configuration"}}],
                "adapters": [{"kind": "openapi-json", "document": "api.json", "prefix": "/v1"}],
            }))
            inventory = discover(config)
            self.assertEqual(inventory["source_census"], [{"file": "api.json", "classified": True}])
            catalog = build_catalog(inventory)
            self.assertEqual(catalog["families"][0]["id"], "http:GET /v1/items")
            self.assertIn("confirmed-business-outcome", catalog["families"][0]["missing"])
            self.assertFalse(catalog["behaviorally_complete"])
            self.assertFalse(catalog["declarations_accounted"])
            self.assertFalse(catalog["references_resolved"])
            document.write_text(document.read_text() + "\n")
            self.assertNotEqual(inventory["sources"], discover(config)["sources"])

    def test_refs_filtered_paths_and_extensions_remain_unknown(self):
        rows = self.inventory({
            "/filtered": {}, "/remote": {"$ref": "https://invalid.example/api.json"},
            "/mixed": {"$ref": "#/components/pathItems/X", "get": {}},
            "x-extension": {"post": {}},
        }, webhooks={"incoming": {"post": {}}})
        self.assertEqual([r["id"] for r in rows if r["kind"] == "surface"], ["http:GET /mixed"])
        reasons = [r.get("reason", "") for r in rows]
        self.assertEqual(sum("unresolved Path Item" in r for r in reasons), 2)
        self.assertEqual(sum("path has no inline" in r for r in reasons), 2)
        self.assertNotIn("invalid.example", json.dumps(rows))
        self.assertTrue(any("webhooks" in r for r in reasons))

    def test_empty_document_has_no_green_denominator(self):
        rows = self.inventory({})
        self.assertTrue(all(r["kind"] == "unresolved" for r in rows))
        self.assertTrue(any("no inline HTTP operations" in r["reason"] for r in rows))

    def test_private_content_and_server_urls_are_not_reported_or_selected(self):
        rows = self.inventory({"/items": {"get": {
            "description": "PRIVATE_TEXT", "operationId": "PRIVATE_TEXT",
            "responses": {"200": {"description": "PRIVATE_TEXT"}},
        }}}, servers=[{"url": "https://PRIVATE_TEXT/v2"}])
        self.assertNotIn("PRIVATE_TEXT", json.dumps(rows))
        self.assertIn("http:GET /items", [r["id"] for r in rows])

    def test_invalid_shapes_and_versions_fail_closed(self):
        for document in [[], {"openapi": "3.2.0"}, {"openapi": "2.0"},
                         {"openapi": "3.1.0", "paths": []}]:
            with self.subTest(document=document), self.assertRaises(ValueError):
                derive(json.dumps(document), "api.json")
        for paths in [{"items": {}}, {"/items?q=1": {}}, {"/items": []},
                      {"/items": {"GET": {}}}, {"/items": {"get": None}}]:
            with self.subTest(paths=paths), self.assertRaises(ValueError):
                self.inventory(paths)
        with self.assertRaisesRegex(ValueError, "duplicate JSON member"):
            derive('{"openapi":"3.1.0","paths":{"/x":{"get":{},"get":{}}}}', "api.json")
        for prefix in [None, False, "v1", "/v1/", "/{tenant}", "/v1?x"]:
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                derive('{"openapi":"3.1.0"}', "api.json", prefix)

    def test_overlapping_contract_surfaces_remain_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.json").write_text('{"openapi":"3.1.0","paths":{"/x":{"get":{}}}}')
            (root / "b.json").write_text('{"openapi":"3.1.0","paths":{"/x":{"get":{}}}}')
            config = root / "discovery.json"
            config.write_text(json.dumps({"root": ".", "scope": "overlap", "adapters": [
                {"kind": "openapi-json", "document": "a.json"},
                {"kind": "openapi-json", "document": "b.json"},
            ]}))
            with self.assertRaisesRegex(ValueError, "duplicate obligation IDs"):
                discover(config)

    def test_document_path_cannot_escape_source_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "api.json").write_text('{"openapi":"3.1.0"}')
            child = root / "child"
            child.mkdir()
            config = child / "discovery.json"
            config.write_text(json.dumps({"root": ".", "scope": "escape", "adapters": [
                {"kind": "openapi-json", "document": "../api.json"},
            ]}))
            with self.assertRaisesRegex(ValueError, "out-of-root"):
                discover(config)

    def test_contract_surface_needs_same_step_runtime_and_outcome_evidence(self):
        from capcov.flows.model import plan, reconcile
        from tests.test_flows import evidence

        route = "http:GET /items"
        inventory = {"obligations": self.inventory({"/items": {"get": {}}})}
        model = {
            "version": 1, "scope": "contract", "facts": [], "initial": [],
            "transitions": [{
                "id": "read", "actor": "operator", "outcome": "items are shown",
                "evidence": [{"file": "review.md", "line": 1}],
                "obligations": [route], "bindings": {"browser": {"commands": [
                    {"op": "assert", "id": "items", "selector": "body", "text": "Items"}
                ]}},
            }],
        }
        execution_plan = plan(model, "browser")
        run = evidence(inventory, execution_plan)
        self.assertEqual(reconcile(inventory, model, execution_plan, run)["summary"]["covered"], 0)
        run["scenarios"][0]["observed_requests"] = [{"step": "0:read", "surface": route}]
        result = reconcile(inventory, model, execution_plan, run)
        self.assertEqual(result["summary"]["covered"], 1)
        self.assertFalse(result["complete"])
        run["scenarios"][0]["assertions"] = []
        self.assertEqual(reconcile(inventory, model, execution_plan, run)["summary"]["covered"], 0)
