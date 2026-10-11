"""The flows deprecation shims (design §5 deletion plan).

``discover``, ``catalog``, and ``plan`` run on the shared discovery/planning
engine the promoted core adapters and the browser probe wrap -- they are KEPT and
must NOT carry a deprecation notice. ``run``, ``coverage``, ``gate`` and
``model.reconcile`` are RETIRED to deprecation shims: they still resolve and
return (so the suite and pinned consumers stay green) but emit a notice pointing
at the unified ``observe(--probe browser) -> reconcile -> gate`` path.

This module pins both halves: the retired verbs still dispatch AND emit the
notice; the kept verbs dispatch WITHOUT one. The three broader guard suites
(test_flows, test_model_extensions, test_cli_only) pin that the shims' return
values are byte-for-byte unchanged.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

from capcov.flows.cli import main
from capcov.flows.model import plan, reconcile
from tests.test_flows import evidence, fixture


def _run(argv: list[str]) -> tuple[int, str]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = main(argv)
    return code, buffer.getvalue()


def _write_case(directory: str) -> dict[str, str]:
    """A valid inventory/model/plan/run quartet on disk, plus an --out path."""
    inventory, model = fixture()
    execution_plan = plan(model, "browser")
    run = evidence(inventory, execution_plan)
    root = Path(directory)
    paths = {
        "inventory": root / "inventory.json",
        "model": root / "model.json",
        "plan": root / "plan.json",
        "run": root / "run.json",
        "out": root / "coverage.json",
    }
    paths["inventory"].write_text(json.dumps(inventory))
    paths["model"].write_text(json.dumps(model))
    paths["plan"].write_text(json.dumps(execution_plan))
    paths["run"].write_text(json.dumps(run))
    return {name: str(path) for name, path in paths.items()}


class ModelReconcileShimTests(unittest.TestCase):
    def test_reconcile_emits_a_deprecation_warning_but_still_returns(self) -> None:
        inventory, model = fixture()
        execution_plan = plan(model, "browser")
        run = evidence(inventory, execution_plan)
        with self.assertWarns(DeprecationWarning) as caught:
            result = reconcile(inventory, model, execution_plan, run)
        # The notice names the unified successor, not merely "deprecated".
        message = str(caught.warning)
        self.assertIn("capcov.core.reconcile", message)
        self.assertIn("--probe browser", message)
        # Still returns the genuine four-obligation verdict -- the shim does its work.
        self.assertTrue(result["complete"], result["failures"])
        self.assertEqual(result["summary"]["covered"], 2)

    def test_reconcile_return_value_is_untouched_by_the_shim(self) -> None:
        """Suppress the notice and confirm the shim's result equals the value the
        suite has always pinned (a shim is a notice, not a behaviour change)."""
        inventory, model = fixture()
        execution_plan = plan(model, "browser")
        run = evidence(inventory, execution_plan)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            result = reconcile(inventory, model, execution_plan, run)
        self.assertEqual({r["id"] for r in result["rows"]}, {"read", "edit"})
        self.assertEqual(result["assurance"], "test-fixture")


class RetiredVerbShimTests(unittest.TestCase):
    def test_coverage_verb_still_dispatches_and_prints_the_notice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = _write_case(directory)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                code, out = _run(
                    [
                        "coverage",
                        paths["inventory"],
                        paths["model"],
                        paths["plan"],
                        "--run",
                        paths["run"],
                        "--out",
                        paths["out"],
                    ]
                )
            report = json.loads(Path(paths["out"]).read_text())
        # Dispatched and did real work: exit 0, a complete artifact on disk.
        self.assertEqual(code, 0, out)
        self.assertTrue(report["complete"], report["failures"])
        # And emitted the notice pointing at the unified path.
        self.assertIn("DEPRECATED", out)
        self.assertIn("capcov observe --probe browser", out)
        self.assertIn("capcov reconcile", out)

    def test_gate_verb_still_dispatches_and_prints_the_notice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            coverage, baseline = root / "coverage.json", root / "baseline.json"
            coverage.write_text(json.dumps({"complete": False, "failures": ["unmapped: old"]}))
            baseline.write_text(json.dumps({"unmapped: old": "Existing screen; awaiting a flow"}))
            args = ["gate", str(coverage), "--baseline", str(baseline)]
            # A fully baselined coverage still gates 0 -- the shim gates as before.
            code, out = _run(args)
            self.assertEqual(code, 0, out)
            self.assertIn("DEPRECATED", out)
            self.assertIn("exemptions.toml", out)
            # A new, un-baselined failure still fails -- behaviour unchanged.
            coverage.write_text(
                json.dumps({"complete": False, "failures": ["unmapped: old", "unmapped: new"]})
            )
            code, out = _run(args)
            self.assertEqual(code, 1, out)
            self.assertIn("DEPRECATED", out)

    def test_run_verb_still_dispatches_and_prints_the_notice(self) -> None:
        inventory, model = fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {name: root / f"{name}.json" for name in ("inventory", "plan", "out")}
            paths["inventory"].write_text(json.dumps(inventory))
            paths["plan"].write_text(json.dumps(plan(model, "browser")))
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
            buffer = io.StringIO()
            with (
                patch("capcov.flows.cli.discover", return_value=inventory),
                patch("capcov.flows.cli.subprocess.run") as command,
                contextlib.redirect_stdout(buffer),
            ):
                command.return_value.returncode = 0
                # Runner writes no fresh evidence -> the shim's freshness guard
                # still returns 1. It was reached (dispatched) and the runner ran.
                code = main(args)
            out = buffer.getvalue()
        self.assertEqual(code, 1)
        self.assertTrue(command.called)
        self.assertIn("DEPRECATED", out)
        self.assertIn("capcov observe --probe browser", out)


class KeptVerbTests(unittest.TestCase):
    """The engine verbs are NOT deprecated: they emit no notice."""

    def test_plan_verb_is_not_deprecated(self) -> None:
        _, model = fixture()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "model.json"
            output = Path(directory) / "plan.json"
            source.write_text(json.dumps(model))
            code, out = _run(
                ["plan", str(source), "--target", "browser", "--out", str(output)]
            )
            generated = json.loads(output.read_text())
        self.assertEqual(code, 0, out)
        self.assertEqual({s["id"] for s in generated["scenarios"]}, {"login", "edit", "history"})
        self.assertNotIn("DEPRECATED", out)

    def test_catalog_verb_is_not_deprecated(self) -> None:
        inventory = {"scope": "shim-fixture", "obligations": [{"id": "p", "kind": "predicate"}]}
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "inventory.json"
            output = Path(directory) / "catalog.json"
            source.write_text(json.dumps(inventory))
            code, out = _run(["catalog", str(source), "--out", str(output), "--quiet"])
            catalog = json.loads(output.read_text())
        self.assertEqual(code, 0, out)
        self.assertEqual(catalog["scope"], "shim-fixture")
        self.assertNotIn("DEPRECATED", out)


if __name__ == "__main__":
    unittest.main()
