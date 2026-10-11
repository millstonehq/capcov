"""The worked example ``examples/fault-notes``: the baseline passes, each mutant is found.

A mutant exists only in a temporary copy of the example, made by applying a
patch from ``tests/fixtures/faults`` to it.
"""
from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from capcov.faults import service as faults
from capcov.faults.crash import explore
from capcov.faults.machine import enumerate_schedules

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = PACKAGE_ROOT / "examples" / "fault-notes"
PATCH = PACKAGE_ROOT / "tests" / "fixtures" / "faults" / "resend-after-unknown.patch"
PENDING_PATCH = PACKAGE_ROOT / "tests" / "fixtures" / "faults" / "judge-before-settle.patch"
MODULES = ("notes_app", "scenario")
_HAVE_PATCH = shutil.which("patch") is not None


def load_example(directory: Path):
    """Import ``scenario`` (and the ``notes_app`` beside it) from ``directory``."""
    for name in MODULES:
        sys.modules.pop(name, None)
    sys.path.insert(0, str(directory))
    try:
        return importlib.import_module("scenario")
    finally:
        sys.path.remove(str(directory))
        for name in MODULES:
            sys.modules.pop(name, None)


def failures(scenario, fault=None) -> list[tuple[str, str, str]]:
    """``(crash point, invariant, status)`` for every failed invariant of every explored run."""
    found = []
    for exploration in explore(scenario.scenario(fault), scenario.notes_app.recover, scenario.judge):
        point = exploration.point
        where = "none" if point is None else f"{point.when} {point.store}/{point.label}"
        found.extend((where, verdict.name, verdict.status) for verdict in exploration.result.failed)
    return found


class BaselineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.scenario = load_example(EXAMPLE)

    def test_every_crash_point_passes(self) -> None:
        explorations = explore(self.scenario.scenario(), self.scenario.notes_app.recover,
                               self.scenario.judge)
        stores = {exploration.point.store for exploration in explorations[1:]}
        self.assertEqual(stores, {"primary", "objects", "index"})
        self.assertGreaterEqual(len(explorations), 13)
        self.assertEqual([e.result.failed for e in explorations], [()] * len(explorations))

    def test_every_fault_passes_under_every_crash_point(self) -> None:
        self.assertEqual(
            {type(fault()).__name__ for fault in self.scenario.FAULTS.values()},
            {"Refuse", "LoseResponse", "DelayApply", "ApplyTwice", "EditBeforeNextWrite",
             "DelayChangeEntry", "AmbiguousError", "DuplicateOnConcurrentCreate"})
        for name, fault in self.scenario.FAULTS.items():
            with self.subTest(name):
                self.assertEqual(failures(self.scenario, fault), [])

    def test_an_unknown_outcome_is_settled_from_the_change_log_without_a_resend(self) -> None:
        captured = []
        explore(self.scenario.scenario(faults.LoseResponse), self.scenario.notes_app.recover,
                lambda world, history: captured.append(world))
        truth = captured[0].index.ground_truth()
        self.assertEqual(len(truth.applied), 1)
        self.assertEqual([request.method for request in truth.requests].count("write"), 1)
        self.assertEqual(captured[0].db.execute(
            "SELECT status, outcome FROM intents WHERE store = 'index'").fetchall(),
            [("settled", "applied")])

    def test_an_overwrite_of_an_unread_value_is_reported(self) -> None:
        captured = []
        fault = self.scenario.FAULTS["edit-before-next-write"]
        explore(self.scenario.scenario(fault), self.scenario.notes_app.recover,
                lambda world, history: captured.append(world))
        self.assertEqual(captured[0].db.execute("SELECT store, seq FROM overwrites").fetchall(),
                         [("index", 2)])

    def test_no_schedule_to_depth_five_fails(self) -> None:
        self.assertIsNone(enumerate_schedules(self.scenario.NotesMachine, 5))


@unittest.skipUnless(_HAVE_PATCH, "the patch command is unavailable")
class MutantTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.copy = Path(cls.directory.name) / "fault-notes"
        shutil.copytree(EXAMPLE, cls.copy, ignore=shutil.ignore_patterns("__pycache__"))
        with PATCH.open("rb") as patch:
            subprocess.run(["patch", "-s", "-F", "0", "-p1", "-d", str(cls.copy)],
                           stdin=patch, check=True)
        cls.scenario = load_example(cls.copy)

    def test_the_mutant_exists_only_in_the_copy(self) -> None:
        original = (EXAMPLE / "notes_app.py").read_text(encoding="utf-8")
        mutant = (self.copy / "notes_app.py").read_text(encoding="utf-8")
        lookup = "own = self._own_entry(intent)"
        self.assertEqual((original.count(lookup), mutant.count(lookup)), (2, 1))
        self.assertNotIn("own = None", original)
        self.assertEqual(Path(self.scenario.notes_app.__file__).resolve().parent, self.copy.resolve())

    def test_the_enumerator_finds_the_mutant(self) -> None:
        failing = enumerate_schedules(self.scenario.NotesMachine, 5)
        self.assertIsNotNone(failing)
        self.assertEqual(failing.failures, ("effect_applied_twice",))
        self.assertEqual(failing.rules, ("start_operation", "run_next_step", "run_next_step",
                                         "run_next_step"))

    def test_a_crash_after_the_send_is_applied_twice(self) -> None:
        self.assertIn(("after index/write", "effect_applied_twice", "violated"), failures(self.scenario))

    def test_a_lost_response_is_applied_twice(self) -> None:
        self.assertIn(("none", "effect_applied_twice", "violated"),
                      failures(self.scenario, faults.LoseResponse))

    def test_a_refused_send_is_still_sent_once(self) -> None:
        self.assertEqual(failures(self.scenario, lambda: faults.Refuse(503)), [])


@unittest.skipUnless(_HAVE_PATCH, "the patch command is unavailable")
class JudgedBeforeSettleTest(unittest.TestCase):
    """The scenario no longer lets delayed work land before recovery and the verdict."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        cls.copy = Path(cls.directory.name) / "fault-notes"
        shutil.copytree(EXAMPLE, cls.copy, ignore=shutil.ignore_patterns("__pycache__"))
        with PENDING_PATCH.open("rb") as patch:
            subprocess.run(["patch", "-s", "-F", "0", "-p1", "-d", str(cls.copy)],
                           stdin=patch, check=True)
        cls.scenario = load_example(cls.copy)

    def test_the_patch_removes_only_the_settle_step(self) -> None:
        original = (EXAMPLE / "scenario.py").read_text(encoding="utf-8")
        mutant = (self.copy / "scenario.py").read_text(encoding="utf-8")
        self.assertEqual((original.count("world.index.heal()"), mutant.count("world.index.heal()")),
                         (1, 0))
        self.assertEqual((original.count("self.state.index.heal()"),
                          mutant.count("self.state.index.heal()")), (1, 1))

    def test_a_pending_write_leaves_the_run_unresolved_naming_applied_closed(self) -> None:
        captured = []

        def judge(world, history):
            captured.append(world)
            return self.scenario.judge(world, history)

        explorations = explore(self.scenario.scenario(self.scenario.FAULTS["delay-apply"]),
                               self.scenario.notes_app.recover, judge)
        healthy, world = explorations[0].result, captured[0]
        self.assertEqual(len(world.index.ground_truth().pending), 1)
        self.assertEqual(len(world.index.ground_truth().applied), 1)
        self.assertFalse(healthy.ok)
        for name in ("effect_applied_twice", "silent_overwrite", "split_outcome"):
            self.assertEqual(healthy[name].status, "unresolved", name)
            self.assertIn({"relation": "applied_closed"}, healthy[name].missing_premises, name)
        world.index.heal()
        self.assertEqual(len(world.index.ground_truth().applied), 2)

    def test_a_run_with_no_fault_holds_no_pending_write_and_still_passes(self) -> None:
        self.assertEqual(failures(self.scenario), [])


class LazyImportTest(unittest.TestCase):
    def test_the_gate_path_does_not_load_the_fault_harness(self) -> None:
        code = ("import sys, capcov.cli, capcov.core.gate, capcov.core.reconcile\n"
                "loaded = sorted(name for name in sys.modules if name.startswith('capcov.faults'))\n"
                "assert not loaded, loaded\n"
                "import capcov.faults\n"
                "loaded = sorted(name for name in sys.modules if name.startswith('capcov.faults.'))\n"
                "assert not loaded, loaded\n"
                "assert capcov.faults.History.__module__ == 'capcov.faults.history'\n"
                "assert 'capcov.claims' not in sys.modules\n")
        environment = {**os.environ, "PYTHONPATH": os.pathsep.join(path for path in sys.path if path)}
        subprocess.run([sys.executable, "-c", code], check=True, env=environment)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
