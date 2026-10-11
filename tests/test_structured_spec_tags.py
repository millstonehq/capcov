"""Two OpenAPI reader changes for real-world documents.

A real committed openapi.json (several hundred paths) has four templated-path
collisions ("/{id}" beside "/{optionId}"). The reader raised and produced
nothing -- a hard failure on a spec quirk, against the package's own rule that a
limit becomes a named entry. It now records a boundary and keeps every surface.
Operations also carry `tags` and `summary` onto the surface so `features map`
can claim by tag and a tree can be seeded from the document's own grouping.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from capcov.adapters import load
from capcov.flows.openapi import derive


DOC = {
    "openapi": "3.0.0",
    "paths": {
        "/{id}": {"get": {"tags": ["Options"], "summary": "Get one"}},
        "/{optionId}": {"delete": {"tags": ["Options"]}},
        "/jobs": {"get": {"tags": ["Jobs", "Core"], "summary": "List jobs"}},
    },
}


class CollisionBecomesBoundaryTests(unittest.TestCase):
    def test_equivalent_templated_paths_record_a_boundary_and_keep_surfaces(self) -> None:
        obligations = derive(json.dumps(DOC), "openapi.json")
        surfaces = {o["id"] for o in obligations if o["kind"] == "surface"}
        self.assertEqual(surfaces, {"http:GET /{id}", "http:DELETE /{optionId}", "http:GET /jobs"})
        boundaries = [o for o in obligations if o["kind"] == "unresolved"
                      and "path-shape-collision" in o["id"]]
        self.assertEqual(len(boundaries), 1)
        self.assertIn("/{optionId}", boundaries[0]["reason"])
        self.assertIn("/{id}", boundaries[0]["reason"])

    def test_three_way_collision_records_one_boundary_per_colliding_entry(self) -> None:
        doc = {
            "openapi": "3.0.0",
            "paths": {
                "/{id}": {"get": {}},
                "/{optionId}": {"delete": {}},
                "/{upchargeId}": {"patch": {}},
            },
        }
        obligations = derive(json.dumps(doc), "openapi.json")
        surfaces = {o["id"] for o in obligations if o["kind"] == "surface"}
        self.assertEqual(
            surfaces, {"http:GET /{id}", "http:DELETE /{optionId}", "http:PATCH /{upchargeId}"}
        )
        boundaries = [o for o in obligations if o["kind"] == "unresolved"
                      and "path-shape-collision" in o["id"]]
        self.assertEqual(len(boundaries), 2)
        self.assertEqual(len({b["id"] for b in boundaries}), 2)
        self.assertEqual(
            [b["source"]["pointer"] for b in boundaries],
            ["/paths/~1{optionId}", "/paths/~1{upchargeId}"],
        )
        for boundary in boundaries:
            self.assertIn("'/{id}'", boundary["reason"])


class TagsAndSummaryTests(unittest.TestCase):
    def test_surface_obligations_carry_tags_and_summary(self) -> None:
        by_id = {o["id"]: o for o in derive(json.dumps(DOC), "openapi.json")}
        self.assertEqual(by_id["http:GET /jobs"]["tags"], ["Jobs", "Core"])
        self.assertEqual(by_id["http:GET /jobs"]["summary"], "List jobs")
        self.assertEqual(by_id["http:DELETE /{optionId}"]["tags"], ["Options"])
        self.assertNotIn("summary", by_id["http:DELETE /{optionId}"])

    def test_empty_tags_are_not_carried(self) -> None:
        doc = {"openapi": "3.0.0", "paths": {"/jobs": {"get": {"tags": [], "summary": ""}}}}
        by_id = {o["id"]: o for o in derive(json.dumps(doc), "openapi.json")}
        self.assertNotIn("tags", by_id["http:GET /jobs"])
        self.assertNotIn("summary", by_id["http:GET /jobs"])

    def test_core_bridge_forwards_tags_and_summary_onto_surfaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "openapi.json").write_text(json.dumps(DOC))
            module = load("structured-spec")
            core = module.discover(root, root, config={"document": "openapi.json"})
        by_id = {s["id"]: s for s in core["surfaces"]}
        self.assertEqual(by_id["http:GET /jobs"]["tags"], ["Jobs", "Core"])
        self.assertEqual(by_id["http:GET /jobs"]["summary"], "List jobs")
        self.assertEqual(by_id["http:DELETE /{optionId}"].get("tags"), ["Options"])
