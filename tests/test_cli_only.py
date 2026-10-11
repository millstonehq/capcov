"""The ``--only`` inner-loop selector on the flows CLI.

``coverage --only <id>`` scopes the reconciliation to a single transition or
scenario id so a change can be checked in seconds instead of folding the whole
plan. The CLI's job -- and all this module pins -- is that the id is threaded to
the ``reconcile`` call as ``only=<id>`` and nothing is threaded when the flag is
absent, so the unscoped path is byte-for-byte the pre-existing behaviour.

The scoping itself lives in ``model.py::reconcile`` (its keyword ``only`` param).
The positive path patches ``capcov.flows.cli.reconcile`` with a stub that honours
``only`` -- so these tests assert the CLI wiring in isolation, not model.py's
restriction logic (that is pinned in test_model_extensions). The negative path
exercises the real ``reconcile`` end to end, which proves the unscoped invocation
is genuinely unchanged (it never receives an ``only`` kwarg).
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from capcov.flows.cli import main
from capcov.flows.model import plan
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


def _scoped_stub(*seen: dict):
    """A reconcile double that records its call and honours ``only``.

    Returns a report whose rows are restricted to ``only`` when the kwarg is
    present -- standing in for the real model.py restriction so the emitted
    artifact demonstrably narrows to the selected id.
    """

    full_rows = [
        {"id": "read", "status": "covered", "transitions": ["login", "history"]},
        {"id": "edit", "status": "covered", "transitions": ["edit"]},
    ]

    def fake(inventory, model, execution_plan, run, **kwargs):
        seen[0]["args"] = (inventory, model, execution_plan, run)
        seen[0]["kwargs"] = kwargs
        only = kwargs.get("only")
        rows = [r for r in full_rows if r["id"] == only] if only is not None else full_rows
        return {
            "version": 1,
            "scope": "fixture",
            "target": "browser",
            "assurance": "test-fixture",
            "rows": rows,
            "blocked": [],
            "failures": [],
            "summary": {"covered": len(rows), "unproven": 0, "unmapped": 0},
            "complete": True,
        }

    return fake


class CoverageOnlyTests(unittest.TestCase):
    def test_only_threads_the_id_to_reconcile_and_scopes_the_report(self) -> None:
        seen = [{}]
        with tempfile.TemporaryDirectory() as directory:
            paths = _write_case(directory)
            with patch("capcov.flows.cli.reconcile", _scoped_stub(seen[0])):
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
                        "--only",
                        "edit",
                    ]
                )
            report = json.loads(Path(paths["out"]).read_text())
        self.assertEqual(code, 0)
        # The id reached reconcile as a keyword, exactly as Integrate expects.
        self.assertEqual(seen[0]["kwargs"], {"only": "edit"})
        # The emitted artifact is narrowed to the selected id -- not the whole plan.
        self.assertEqual([row["id"] for row in report["rows"]], ["edit"])
        self.assertIn("assurance=test-fixture", out)

    def test_omitting_only_passes_no_scope_and_reports_the_whole_plan(self) -> None:
        seen = [{}]
        with tempfile.TemporaryDirectory() as directory:
            paths = _write_case(directory)
            with patch("capcov.flows.cli.reconcile", _scoped_stub(seen[0])):
                code, _ = _run(
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
        self.assertEqual(code, 0)
        # No --only means no scope keyword at all: the call is the bare four-arg
        # form, folding the whole plan via reconcile's own ``only`` default.
        self.assertEqual(seen[0]["kwargs"], {})
        self.assertEqual([row["id"] for row in report["rows"]], ["read", "edit"])

    def test_unscoped_coverage_runs_the_real_reconcile_unchanged(self) -> None:
        """No patch: the real reconcile must still accept the unscoped invocation
        and emit a genuine, complete coverage artifact."""
        with tempfile.TemporaryDirectory() as directory:
            paths = _write_case(directory)
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
        self.assertEqual(code, 0, out)
        self.assertTrue(report["complete"], report["failures"])
        self.assertEqual({row["id"] for row in report["rows"]}, {"read", "edit"})


class RunOnlyTests(unittest.TestCase):
    """The run half of the inner loop: --only narrows what the runner executes,
    threaded to it as the CAPCOV_FLOW_ONLY env var and absent when unset."""

    def _invoke(self, extra: list[str]) -> dict:
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
                *extra,
                "--",
                "runner",
            ]
            with (
                patch("capcov.flows.cli.discover", return_value=inventory),
                patch("capcov.flows.cli.subprocess.run") as command,
                patch("builtins.print"),
            ):
                command.return_value.returncode = 0
                # The runner writes no fresh evidence, so main returns 1 -- but it is
                # invoked first, which is all we inspect: the env it was handed.
                self.assertEqual(main(args), 1)
            return command.call_args.kwargs["env"]

    def test_only_is_threaded_to_the_runner_environment(self) -> None:
        env = self._invoke(["--only", "edit"])
        self.assertEqual(env["CAPCOV_FLOW_ONLY"], "edit")

    def test_no_only_leaves_the_runner_environment_unscoped(self) -> None:
        env = self._invoke([])
        self.assertNotIn("CAPCOV_FLOW_ONLY", env)


if __name__ == "__main__":
    unittest.main()
