import tempfile
import unittest
from pathlib import Path

from capcov.core.gate import gate
from capcov.core.reconcile import reconcile


def capabilities(
    entities: list[str], caps: list[dict], surfaces: list[str] | None = None
) -> dict:
    return {
        "entities": [{"name": e} for e in entities],
        "surfaces": [{"id": s} for s in (surfaces or [c["surface"] for c in caps])],
        "capabilities": caps,
    }


def observed(bindings: list[dict]) -> dict:
    return {"bindings": bindings}


def cap(entity: str, surface: str, ops: tuple[str, ...] = ("read",)) -> dict:
    return {"entity": entity, "surface": surface, "operations": list(ops)}


def obs(
    entity: str,
    surface: str,
    ops: tuple[str, ...] = ("read",),
    tests: tuple[str, ...] = (),
) -> dict:
    return {
        "entity": entity,
        "surface": surface,
        "operations": list(ops),
        "tests": list(tests),
    }


class FourCellsTest(unittest.TestCase):
    def cells(
        self,
        entities: list[str],
        caps: list[dict],
        bindings: list[dict],
        surfaces: list[str] | None = None,
    ) -> tuple[dict, dict]:
        result = reconcile(capabilities(entities, caps, surfaces), observed(bindings))
        return {row["entity"]: row["cell"] for row in result["rows"]}, result

    def test_found_by_both_is_a_capability(self) -> None:
        cells, _ = self.cells(["a"], [cap("a", "GET /a")], [obs("a", "GET /a")])
        self.assertEqual(cells["a"], "both")

    def test_found_by_static_only_is_a_coverage_gap(self) -> None:
        cells, _ = self.cells(["a"], [cap("a", "GET /a")], [])
        self.assertEqual(cells["a"], "static_only")

    def test_found_by_runtime_only_is_an_extractor_gap(self) -> None:
        cells, _ = self.cells(["a"], [], [obs("a", "GET /a")], surfaces=["GET /a"])
        self.assertEqual(cells["a"], "runtime_only")

    def test_found_by_neither_is_dead(self) -> None:
        cells, _ = self.cells(["a"], [], [])
        self.assertEqual(cells["a"], "neither")

    def test_a_surface_runtime_saw_and_static_never_found_is_reported(self) -> None:
        _, result = self.cells(
            ["a"], [cap("a", "GET /a")], [obs("a", "GET /a"), obs("a", "POST /surprise")]
        )
        self.assertEqual(
            [u["surface"] for u in result["unknown_surfaces"]], ["POST /surprise"]
        )

    def test_a_test_reaching_past_every_surface_is_not_an_unknown_surface(self) -> None:
        """Otherwise every unit test in the repository fails the build."""
        _, result = self.cells(
            ["a"], [cap("a", "GET /a")], [obs("a", "test:tests/test_x.py::test_y")]
        )
        self.assertEqual(result["unknown_surfaces"], [])
        self.assertEqual(result["reached_only_by_tests"], ["a"])

    def test_an_operation_static_claims_and_runtime_never_saw(self) -> None:
        _, result = self.cells(
            ["a"], [cap("a", "GET /a", ("read", "delete"))], [obs("a", "GET /a", ("read",))]
        )
        self.assertEqual(result["rows"][0]["untested_operations"], ["delete"])

    def test_a_test_for_an_undeclared_entity_is_reported(self) -> None:
        _, result = self.cells(
            [], [], [obs("gone", "GET /gone", tests=["tests/test_gone.py::test_it"])],
            surfaces=["GET /gone"],
        )
        self.assertEqual(
            result["orphan_tests"],
            [{"test": "tests/test_gone.py::test_it", "entity": "gone"}],
        )

    def test_the_new_carriers_default_explicitly_on_the_python_path(self) -> None:
        """A capabilities/observed pair that declares none of the honest-denominator
        carriers (the python-fastapi-sqlalchemy path) still gets them, empty but
        present -- absence would read as none, and the four cells are undisturbed."""
        _, result = self.cells(["a"], [cap("a", "GET /a")], [obs("a", "GET /a")])
        self.assertEqual(result["excluded_surfaces"], {"count": 0, "surfaces": []})
        self.assertEqual(result["unresolved"], [])
        self.assertEqual(result["residue"], [])
        self.assertEqual(
            result["residue_summary"],
            {
                "resolved_by_import": 0,
                "resolved_by_name": 0,
                "ambiguous": 0,
                "external": 0,
                "chained": 0,
                "builtin_shadowed": 0,
            },
        )
        self.assertEqual(result["summary"], {"both": 1, "static_only": 0,
                                             "runtime_only": 0, "neither": 0})

    def test_residue_is_forwarded_from_the_inventory_not_dropped(self) -> None:
        """The leak repaired: residue + residue_summary the inventory built must
        survive the reconcile boundary rather than dying before the gate."""
        caps = capabilities(["a"], [cap("a", "GET /a")])
        caps["residue"] = [{"file": "app.py", "line": 3, "callee": "x.y"}]
        caps["residue_summary"] = {
            "resolved_by_import": 4, "resolved_by_name": 1, "ambiguous": 1,
            "external": 2, "chained": 0, "builtin_shadowed": 0,
        }
        result = reconcile(caps, observed([obs("a", "GET /a")]))
        self.assertEqual(result["residue"], caps["residue"])
        self.assertEqual(result["residue_summary"], caps["residue_summary"])


class GateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def exemptions(self, text: str) -> Path:
        path = Path(self.dir.name) / "exemptions.toml"
        path.write_text(text)
        return path

    def coverage(
        self,
        rows: list[dict],
        unknown: tuple[str, ...] = (),
        orphans: tuple[dict, ...] = (),
    ) -> dict:
        return {
            "rows": rows,
            "unknown_surfaces": [{"surface": s, "entities": ["x"]} for s in unknown],
            "orphan_tests": list(orphans),
        }

    def row(self, entity: str, cell: str) -> dict:
        return {
            "entity": entity,
            "cell": cell,
            "static_surfaces": ["GET /a"],
            "runtime_surfaces": ["GET /a"],
        }

    def rules(self, failures: list) -> list[str]:
        return sorted(f.rule for f in failures)

    def test_an_unexplained_gap_fails(self) -> None:
        failures = gate(self.coverage([self.row("a", "static_only")]), None)
        self.assertEqual(self.rules(failures), ["unexplained-static_only"])

    def test_an_explained_gap_passes(self) -> None:
        path = self.exemptions(
            '[[exempt]]\nentity="a"\ncell="static_only"\ndate="2026-09-06"\nreason="r"\n'
        )
        self.assertEqual(gate(self.coverage([self.row("a", "static_only")]), path), [])

    def test_an_exemption_that_is_no_longer_needed_fails_and_names_itself(self) -> None:
        """The rule usually missing. Without it the list rots into fiction."""
        path = self.exemptions(
            '[[exempt]]\nentity="a"\ncell="static_only"\ndate="2026-09-06"\nreason="r"\n'
        )
        failures = gate(self.coverage([self.row("a", "both")]), path)
        self.assertEqual(self.rules(failures), ["obsolete-exemption"])
        self.assertIn("Delete the exemption", failures[0].detail)

    def test_an_exemption_for_the_wrong_cell_fails(self) -> None:
        path = self.exemptions(
            '[[exempt]]\nentity="a"\ncell="neither"\ndate="2026-09-06"\nreason="r"\n'
        )
        failures = gate(self.coverage([self.row("a", "static_only")]), path)
        self.assertEqual(self.rules(failures), ["stale-exemption"])

    def test_an_exemption_with_no_reason_is_a_silenced_check_and_fails(self) -> None:
        path = self.exemptions('[[exempt]]\nentity="a"\ncell="static_only"\n')
        failures = gate(self.coverage([self.row("a", "static_only")]), path)
        self.assertIn("incomplete-exemption", self.rules(failures))

    def test_an_exemption_naming_something_gone_fails(self) -> None:
        path = self.exemptions(
            '[[exempt]]\nentity="ghost"\ncell="neither"\ndate="2026-01-01"\nreason="r"\n'
        )
        failures = gate(self.coverage([self.row("a", "both")]), path)
        self.assertEqual(self.rules(failures), ["unused-exemption"])

    def test_an_undiscovered_surface_fails(self) -> None:
        failures = gate(
            self.coverage([self.row("a", "both")], unknown=["POST /surprise"]), None
        )
        self.assertEqual(self.rules(failures), ["undiscovered-surface"])

    def test_an_orphan_test_fails(self) -> None:
        failures = gate(
            self.coverage([], orphans=[{"test": "t::x", "entity": "gone"}]), None
        )
        self.assertEqual(self.rules(failures), ["orphan-test"])

    def migration_observation(self) -> dict:
        return reconcile(
            capabilities([], []),
            observed([obs("schema_version", "test:t::migrate", ("create", "read"), ("t::migrate",))]),
        )

    def test_explained_runtime_only_test_entity_remains_visible_without_duplicate_failure(self) -> None:
        report = self.migration_observation()
        path = self.exemptions(
            '[[exempt]]\nentity="schema_version"\ncell="runtime_only"\n'
            'date="2026-09-07"\nreason="Migration framework maintains its version table."\n'
        )
        self.assertEqual(gate(report, path), [])
        self.assertEqual(report["summary"]["runtime_only"], 1)
        self.assertEqual(report["orphan_tests"], [{"test": "t::migrate", "entity": "schema_version"}])
        self.assertFalse(report["rows"][0]["declared"])

    def test_undeclared_test_entity_does_not_imply_historical_deletion(self) -> None:
        failures = gate(self.migration_observation(), None)
        self.assertEqual(self.rules(failures), ["orphan-test", "unexplained-runtime_only"])
        detail = next(f.detail for f in failures if f.rule == "orphan-test")
        self.assertIn("not declared in this inventory", detail)
        self.assertNotIn("no longer exists", detail)

    def test_wrong_cell_exemption_does_not_waive_test_entity(self) -> None:
        path = self.exemptions(
            '[[exempt]]\nentity="schema_version"\ncell="neither"\n'
            'date="2026-09-07"\nreason="Incorrect classification."\n'
        )
        self.assertEqual(
            self.rules(gate(self.migration_observation(), path)),
            ["orphan-test", "stale-exemption"],
        )

    def test_entity_exemption_does_not_waive_undiscovered_surface(self) -> None:
        report = self.migration_observation()
        report["unknown_surfaces"] = [{"surface": "worker:migrate", "entities": ["schema_version"]}]
        path = self.exemptions(
            '[[exempt]]\nentity="schema_version"\ncell="runtime_only"\n'
            'date="2026-09-07"\nreason="Migration version table."\n'
        )
        self.assertEqual(self.rules(gate(report, path)), ["undiscovered-surface"])

    def test_exemption_without_matching_runtime_row_does_not_waive_test_entity(self) -> None:
        path = self.exemptions(
            '[[exempt]]\nentity="schema_version"\ncell="runtime_only"\n'
            'date="2026-09-07"\nreason="Migration version table."\n'
        )
        report = self.coverage([], orphans=[{"test": "t::migrate", "entity": "schema_version"}])
        self.assertEqual(self.rules(gate(report, path)), ["orphan-test", "unused-exemption"])


if __name__ == "__main__":
    unittest.main()
