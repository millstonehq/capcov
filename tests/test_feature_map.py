"""`capcov features map`: surfaces -> feature obligations, with the remainder named.

The FODA layer consumes `{feature_id: {covered, total}}`. Nothing produced that map
until now; it was hand-written. `map` derives it from a static inventory
(capabilities.json) and, optionally, a reconciliation (coverage.json), and keeps
the honest remainder visible: surfaces no feature claims, surfaces several
features claim, and discovery's own excluded/unresolved counts. Static-only runs
say so: `covered` is zero everywhere and `assurance` is "static-only".
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from capcov.features.cli import main
from capcov.features.mapping import project, validate_mapping


def _surface(sid: str, **extra: object) -> dict:
    method, path = sid.split(" ", 1)
    return {"id": sid, "kind": "http", "method": method[len("http:"):], "path": path,
            "file": "routes.php", "line": 1, **extra}


CAPS = {
    "surfaces": [
        _surface("http:GET /contacts"),
        _surface("http:POST /contacts"),
        _surface("http:GET /contacts/{id}"),
        _surface("http:GET /invoices", tags=["Billing"]),
        _surface("http:GET /health"),
    ],
    "excluded_surfaces": {"count": 2, "surfaces": []},
    "unresolved": [{"adapter": "x", "kind": "boundary", "reason": "r"}],
}

MODEL = {
    "version": 1,
    "root": "crm",
    "features": [
        {"id": "crm", "name": "CRM", "parent": None, "decomposition": None, "group": None},
        {"id": "contacts", "name": "Contacts", "parent": "crm",
         "decomposition": "mandatory", "group": None},
        {"id": "billing", "name": "Billing", "parent": "crm",
         "decomposition": "optional", "group": None},
    ],
    "constraints": [],
}

MAPPING = {
    "version": 1,
    "features": {
        "contacts": {"surfaces": ["http:* /contacts", "http:* /contacts/*"]},
        "billing": {"tags": ["Billing"]},
    },
}

COVERAGE = {
    "rows": [
        {"entity": "http:GET /contacts", "cell": "both",
         "runtime_surfaces": ["http:GET /contacts"]},
        {"entity": "http:GET /invoices", "cell": "both",
         "runtime_surfaces": ["http:GET /invoices"]},
    ]
}


class ProjectTests(unittest.TestCase):
    def test_glob_and_tag_rules_assign_surfaces(self) -> None:
        result = project(MODEL, MAPPING, CAPS)
        self.assertEqual(result["obligations"]["contacts"], {"covered": 0, "total": 3})
        self.assertEqual(result["obligations"]["billing"], {"covered": 0, "total": 1})

    def test_unassigned_surfaces_are_named_not_dropped(self) -> None:
        result = project(MODEL, MAPPING, CAPS)
        self.assertEqual(result["unassigned"], ["http:GET /health"])
        self.assertEqual(result["surfaces_total"], 5)
        self.assertEqual(result["assigned"], 4)

    def test_static_only_run_is_labelled_and_covers_nothing(self) -> None:
        result = project(MODEL, MAPPING, CAPS)
        self.assertEqual(result["assurance"], "static-only")
        self.assertTrue(all(v["covered"] == 0 for v in result["obligations"].values()))

    def test_coverage_rows_mark_exercised_surfaces_covered(self) -> None:
        result = project(MODEL, MAPPING, CAPS, COVERAGE)
        self.assertEqual(result["assurance"], "static+runtime")
        self.assertEqual(result["obligations"]["contacts"], {"covered": 1, "total": 3})
        self.assertEqual(result["obligations"]["billing"], {"covered": 1, "total": 1})

    def test_contested_surfaces_are_counted_for_each_claimant_and_reported(self) -> None:
        mapping = {"version": 1, "features": {
            "contacts": {"surfaces": ["http:GET /*"]},
            "billing": {"surfaces": ["http:GET /invoices"]},
        }}
        result = project(MODEL, mapping, CAPS)
        self.assertEqual(result["contested"], {"http:GET /invoices": ["billing", "contacts"]})
        self.assertEqual(result["obligations"]["billing"]["total"], 1)

    def test_discovery_carriers_are_forwarded(self) -> None:
        result = project(MODEL, MAPPING, CAPS)
        self.assertEqual(result["excluded_surfaces"], 2)
        self.assertEqual(result["unresolved"], 1)

    def test_empty_inventory_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            project(MODEL, MAPPING, {"surfaces": []})

    def test_unknown_feature_in_mapping_is_refused(self) -> None:
        bad = {"version": 1, "features": {"ghost": {"surfaces": ["*"]}}}
        with self.assertRaises(ValueError):
            validate_mapping(bad, MODEL)

    def test_rule_that_claims_nothing_is_refused(self) -> None:
        bad = {"version": 1, "features": {"contacts": {}}}
        with self.assertRaises(ValueError):
            validate_mapping(bad, MODEL)

    def test_wrong_shape_coverage_is_refused(self) -> None:
        # CAPS is a capabilities.json shape (discovery), not a reconciliation: it
        # has no 'rows'. Passing it as coverage must refuse, not read as zero rows.
        with self.assertRaises(ValueError):
            project(MODEL, MAPPING, CAPS, CAPS)

    def test_exercised_is_reported_in_the_result(self) -> None:
        result = project(MODEL, MAPPING, CAPS)
        self.assertEqual(result["exercised"], 0)
        result = project(MODEL, MAPPING, CAPS, COVERAGE)
        self.assertEqual(result["exercised"], 2)

    def test_runtime_surface_absent_from_inventory_is_not_covered(self) -> None:
        coverage = {"rows": [{"entity": "http:GET /ghost", "cell": "both",
                               "runtime_surfaces": ["http:GET /ghost"]}]}
        result = project(MODEL, MAPPING, CAPS, coverage)
        self.assertEqual(result["exercised"], 0)
        self.assertTrue(all(v["covered"] == 0 for v in result["obligations"].values()))

    def test_unmapped_features_are_named(self) -> None:
        result = project(MODEL, MAPPING, CAPS)
        self.assertEqual(result["unmapped_features"], ["crm"])

    def test_empty_rules_are_named_not_silently_zero(self) -> None:
        mapping = {"version": 1, "features": {
            "contacts": {"surfaces": ["http:* /contacts"]},
            "billing": {"surfaces": ["http:GET /nonexistent"]},
        }}
        result = project(MODEL, mapping, CAPS)
        self.assertEqual(result["empty_rules"], ["billing"])

    def test_files_rule_claims_surfaces_by_declaring_file(self) -> None:
        mapping = {"version": 1, "features": {"contacts": {"files": ["routes.php"]}}}
        result = project(MODEL, mapping, CAPS)
        # every CAPS surface declares file "routes.php"
        self.assertEqual(result["obligations"]["contacts"]["total"], 5)

    def test_unknown_rule_key_is_refused(self) -> None:
        bad = {"version": 1, "features": {"contacts": {"bogus": ["x"]}}}
        with self.assertRaises(ValueError):
            validate_mapping(bad, MODEL)

    def test_excluded_surfaces_wrong_type_is_refused(self) -> None:
        caps = dict(CAPS, excluded_surfaces=["not", "a", "dict"])
        with self.assertRaises(ValueError):
            project(MODEL, MAPPING, caps)

    def test_duplicate_surface_ids_are_refused(self) -> None:
        caps = {"surfaces": CAPS["surfaces"] + [CAPS["surfaces"][0]]}
        with self.assertRaises(ValueError):
            project(MODEL, MAPPING, caps)


class MapVerbTests(unittest.TestCase):
    def _run(self, argv: list[str]) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(argv)
        return code, buffer.getvalue()

    def test_map_writes_obligations_that_coverage_consumes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory)
            (d / "model.json").write_text(json.dumps(MODEL))
            (d / "mapping.json").write_text(json.dumps(MAPPING))
            (d / "caps.json").write_text(json.dumps(CAPS))
            (d / "cov.json").write_text(json.dumps(COVERAGE))
            code, out = self._run([
                "map", str(d / "model.json"), str(d / "mapping.json"), str(d / "caps.json"),
                "--coverage", str(d / "cov.json"),
                "--out", str(d / "obligations.json"), "--report", str(d / "report.json"),
            ])
            self.assertEqual(code, 0, out)
            self.assertIn("5 surfaces", out)
            self.assertIn("1 unassigned", out)
            self.assertIn("static+runtime, 2 exercised", out)
            self.assertIn("1 unmapped features, 0 empty rules", out)
            obligations = json.loads((d / "obligations.json").read_text())
            self.assertEqual(obligations, {"contacts": {"covered": 1, "total": 3},
                                           "billing": {"covered": 1, "total": 1}})
            report = json.loads((d / "report.json").read_text())
            self.assertEqual(report["unassigned"], ["http:GET /health"])
            self.assertEqual(report["exercised"], 2)
            self.assertEqual(report["unmapped_features"], ["crm"])
            self.assertEqual(report["empty_rules"], [])
            code, out = self._run(["coverage", str(d / "model.json"), str(d / "obligations.json")])
            self.assertEqual(code, 0, out)
            self.assertIn("mandatory 1/3", out)


if __name__ == "__main__":
    unittest.main()
