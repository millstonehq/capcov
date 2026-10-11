"""The pluggable probe interface: registry, shared carriers, freshness guard, and
the load stub. These pin the seam T2 (cmd_observe) and T5 (browser probe) build on.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from capcov import artifacts
from capcov.probes import load_probe, probe_registry


class RegistryTests(unittest.TestCase):
    def test_registry_names_exactly_the_four_probes(self) -> None:
        self.assertEqual(
            set(probe_registry.REGISTRY), {"pytest", "browser", "load", "har"}
        )

    def test_resolve_returns_dotted_paths(self) -> None:
        self.assertEqual(probe_registry.resolve("pytest"), "capcov.probes.pytest_probe")
        self.assertEqual(probe_registry.resolve("browser"), "capcov.probes.browser_probe")
        self.assertEqual(probe_registry.resolve("load"), "capcov.probes.load_probe")

    def test_resolve_does_not_import_the_probe(self) -> None:
        """`browser` is shipped by another task and may not be importable here;
        resolving a name must not import it (design §1.3: dotted path only)."""
        sys.modules.pop("capcov.probes.browser_probe", None)
        self.assertEqual(probe_registry.resolve("browser"), "capcov.probes.browser_probe")
        self.assertNotIn("capcov.probes.browser_probe", sys.modules)

    def test_load_imports_an_existing_probe(self) -> None:
        self.assertIs(probe_registry.load("load"), load_probe)
        self.assertTrue(hasattr(probe_registry.load("pytest"), "pytest_configure"))

    def test_unknown_probe_raises_rather_than_resolving_to_empty_green(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            probe_registry.resolve("selenium")
        self.assertIn("selenium", str(caught.exception))
        with self.assertRaises(SystemExit):
            probe_registry.load("selenium")


class ObservedCarrierTests(unittest.TestCase):
    def test_absent_carriers_default_to_explicit_empty(self) -> None:
        self.assertEqual(
            probe_registry.observed_carriers(),
            {"excluded_surfaces": {"count": 0, "surfaces": []}, "unresolved": []},
        )

    def test_excluded_surfaces_get_a_count_and_a_stable_order(self) -> None:
        surfaces = [
            {"path": "/b", "reason": "diagnostic scope"},
            {"path": "/a", "reason": "diagnostic scope"},
        ]
        carrier = probe_registry.observed_carriers(excluded_surfaces=surfaces)
        self.assertEqual(carrier["excluded_surfaces"]["count"], 2)
        self.assertEqual(
            [s["path"] for s in carrier["excluded_surfaces"]["surfaces"]], ["/a", "/b"]
        )

    def test_unresolved_is_preserved_not_dropped(self) -> None:
        unresolved = [{"adapter": "load", "kind": "sample", "reason": "unattributed"}]
        carrier = probe_registry.observed_carriers(unresolved=unresolved)
        self.assertEqual(carrier["unresolved"], unresolved)


class FreshnessGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        self.source = self.root / "src"
        self.source.mkdir()
        (self.source / "app.py").write_text("x = 1\n")
        self.out = self.root / "observed.json"
        self.evidence = self.root / "run.json"

    def guard(self) -> probe_registry.FreshnessGuard:
        return probe_registry.FreshnessGuard(self.out, self.source)

    def test_fresh_nonce_matched_evidence_passes(self) -> None:
        guard = self.guard()
        nonce = guard.begin()
        self.evidence.write_text(f'{{"nonce": "{nonce}"}}')
        self.assertEqual(guard.verify_output(self.evidence)["nonce"], nonce)

    def test_begin_unlinks_a_stale_output(self) -> None:
        self.out.write_text('{"kind": "observed", "stale": true}')
        self.guard().begin()
        self.assertFalse(self.out.exists())

    def test_stale_out_with_no_fresh_evidence_is_rejected(self) -> None:
        """The exercise ran and wrote nothing fresh: an absent artifact is not a
        clean run, and a leftover one was already unlinked by begin()."""
        guard = self.guard()
        guard.begin()
        with self.assertRaises(ValueError) as caught:
            guard.verify_output(self.evidence)  # never written
        self.assertIn("no fresh evidence", str(caught.exception))

    def test_wrong_nonce_is_rejected_as_stale(self) -> None:
        guard = self.guard()
        guard.begin()
        self.evidence.write_text('{"nonce": "yesterday"}')
        with self.assertRaises(ValueError) as caught:
            guard.verify_output(self.evidence)
        self.assertIn("nonce", str(caught.exception))

    def test_a_mid_run_source_change_is_rejected(self) -> None:
        guard = self.guard()
        nonce = guard.begin()
        (self.source / "app.py").write_text("x = 2  # changed during the run\n")
        self.evidence.write_text(f'{{"nonce": "{nonce}"}}')
        with self.assertRaises(ValueError) as caught:
            guard.verify_output(self.evidence)
        self.assertIn("source changed", str(caught.exception))


class LoadProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        self.source = self.root / "src"
        self.source.mkdir()
        (self.source / "pipeline.py").write_text("STAGES = ['extract', 'load']\n")
        self.out = self.root / "observed.json"

    def test_projection_maps_stage_entity_effect_and_run(self) -> None:
        self.assertEqual(
            load_probe.project_binding(
                stage="load", entity="jobs", effect="upsert", run_id="run-1"
            ),
            {
                "surface": "pipeline:load",
                "entity": "jobs",
                "operations": ["update"],
                "tests": ["run-1"],
            },
        )

    def test_observe_emits_a_valid_observed_envelope_with_carriers(self) -> None:
        load_probe.observe(source_root=self.source, out=self.out)
        doc = artifacts.read(self.out, "observed")
        self.assertEqual(doc["schema_version"], 1)
        self.assertIn("bindings", doc)
        self.assertIn("exercises", doc)
        self.assertEqual(doc["excluded_surfaces"], {"count": 0, "surfaces": []})
        self.assertIn("unresolved", doc)
        self.assertTrue(doc["derived_from"]["artifact_sha256"])

    def test_the_stub_declares_the_missing_driver_rather_than_reporting_empty(self) -> None:
        """A stub that observed nothing must not look like a clean, empty run."""
        load_probe.observe(source_root=self.source, out=self.out)
        doc = artifacts.read(self.out, "observed")
        self.assertEqual(doc["bindings"], [])
        self.assertTrue(doc["unresolved"], "empty observation reported as clean")
        self.assertEqual(doc["unresolved"][0]["adapter"], "load")
        self.assertIn("driver", doc["unresolved"][0]["kind"])

    def test_observe_overwrites_a_stale_output_through_the_guard(self) -> None:
        self.out.write_text('{"kind": "observed", "stale": true}')
        doc = load_probe.observe(source_root=self.source, out=self.out)
        self.assertNotIn("stale", doc)
        self.assertEqual(doc["kind"], "observed")


if __name__ == "__main__":
    unittest.main()
