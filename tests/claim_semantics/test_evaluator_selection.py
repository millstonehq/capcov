"""The verdict is the receipt's, not the toolchain's: every evaluator set agrees.

``--evaluator`` chooses which kernels judge.  The whole point of making the
Soufflé kernels opt-in is that choosing fewer of them must change *what is
recorded about the run* -- which kernels ran, whether a differential ran -- and
nothing about the answer.  This module pins that from both sides:

* **Host, no optional tool.**  The python-only judge is run over the committed
  synthetic receipt fixture (with its synthetic reviewer admission) and compared, field by field, against
  ``fixtures/expected_judge_verdicts.json`` -- the verdicts, per-op
  qualifications, missing premises and certificate digests **recorded from a
  three-kernel devShell run** (``record.py`` beside the table, which is how the
  file is regenerated).  A python-only judge that quietly answered something
  else would fail here rather than in a reviewer's head.
* **devShell.**  ``--evaluator all`` judges the same fixtures with three kernels
  and must produce that same table, plus a differential that actually ran.  If
  it ever stops matching, the table is the thing that was recorded, so the
  failure names which side moved.

Nothing here skips for an absent Soufflé except the class that is about Soufflé.
"""
from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from capcov.claims import differential
from capcov.claims.replay import judge as replay_judge

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXPECTED_PATH = FIXTURES / "expected_judge_verdicts.json"
#: The committed receipts judged here, by the name the table keys them under.
#: Named relative to the package root, where the suite and the recorder run: a
#: certificate signs the join's bundle, whose metadata carries the receipt
#: directory as it was given, so an absolute path would make the recorded
#: certificate digests depend on where the checkout lives.
RECEIPTS = {
    "synthetic": Path("tests") / "claim_semantics" / "fixtures" / "replay_receipt_min",
}
SOUFFLE_ONLY = "this assertion is about the souffle interpreter; it is not on PATH here"


def verdicts(document: dict) -> dict:
    """The part of a judge document that must not depend on which kernels ran.

    Deliberately *not* the whole document: ``kernels``, ``differential``,
    ``differential_report`` and ``compiled`` are exactly the fields that record
    which evaluators ran, and timings are wall clock.  Everything else -- the
    verdict, the exit code, every op's qualification, its missing premises, its
    blocking premise and the sha256 of its certificate -- is the judgement, and
    the judgement is the receipt's.
    """
    return {key: document[key] for key in
            ("verdict", "exit_code", "ops", "required_ops", "contract_findings", "learn")
            if key in document} | {
        key: document[key] for key in ("unmet_ops", "pending_ops", "message")
        if key in document}


def admissions(receipt: Path) -> list[dict]:
    """The reviewer admission of every checked operation in ``receipt``.

    Reviewer authority is external to a receipt, so the positive control needs
    an admission supplied the way a producer's gate supplies one: one row per
    ``model_operation_checked`` row, under a synthetic reviewer name.
    """
    checked = receipt / "model_operation_checked.json"
    if not checked.is_file():
        return []
    rows = json.loads(checked.read_text(encoding="utf-8"))["rows"]
    return [{"producer": "reviewer synthetic-test-policy", **row} for row in rows]


def judge(receipt: Path, out: Path, evaluator: str | None) -> dict:
    document, _ = replay_judge.judge_receipt(receipt, out, [], evaluators=evaluator,
                                             reviewer_admissions=admissions(receipt))
    return document


def expected() -> dict:
    return json.loads(EXPECTED_PATH.read_text(encoding="utf-8"))


class PythonOnlyJudgeMatchesTheRecordedVerdicts(unittest.TestCase):
    """No optional tool, no flag: the same verdicts the three-kernel run recorded."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="capcov-evaluator-selection-"))
        cls.table = expected()
        cls.documents = {
            name: judge(receipt, cls.tmp / name, "python")
            for name, receipt in sorted(RECEIPTS.items())}

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_table_covers_every_committed_receipt(self) -> None:
        self.assertEqual(set(self.table["receipts"]), set(RECEIPTS))
        self.assertEqual(self.table["recorded_with"],
                         ["python", "souffle", "souffle-compiled"],
                         "the table is only worth comparing against if three kernels produced it")

    def test_every_receipt_yields_its_recorded_verdict(self) -> None:
        for name, document in sorted(self.documents.items()):
            with self.subTest(receipt=name):
                self.assertEqual(verdicts(document), self.table["receipts"][name])

    def test_the_synthetic_receipt_is_the_positive_control(self) -> None:
        """Spelled out, so the table cannot drift into agreeing with a wrong answer."""
        self.assertEqual(self.documents["synthetic"]["verdict"], "supported")
        self.assertEqual(self.documents["synthetic"]["exit_code"], 0)

    def test_each_document_records_one_kernel_and_no_differential(self) -> None:
        for name, document in sorted(self.documents.items()):
            with self.subTest(receipt=name):
                self.assertEqual(document["kernels"], ["python"])
                self.assertEqual(document["differential"], "not-run (single evaluator)")
                self.assertIsNone(document["compiled"])

    def test_certificate_digests_are_the_ones_the_three_kernel_run_certified(self) -> None:
        """The strongest field in the table: a certificate is bytes, not a verdict."""
        signed = 0
        for name, document in sorted(self.documents.items()):
            for op, entry in sorted(document["ops"].items()):
                recorded = self.table["receipts"][name]["ops"][op]["certificate_sha256"]
                with self.subTest(receipt=name, op=op):
                    self.assertEqual(entry["certificate_sha256"], recorded)
                signed += entry["certificate_sha256"] is not None
        self.assertGreater(signed, 0, "no receipt certified an op_qualified row")


class EvaluatorSetsDoNotChangeTheAnswer(unittest.TestCase):
    """Two evaluators, three evaluators: the same table, and a differential that ran."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="capcov-evaluator-agreement-"))
        cls.table = expected()

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @unittest.skipUnless(shutil.which("souffle"), SOUFFLE_ONLY)
    def test_all_runs_three_kernels_and_reaches_the_recorded_verdicts(self) -> None:
        for name, receipt in sorted(RECEIPTS.items()):
            document = judge(receipt, self.tmp / f"all-{name}", "all")
            with self.subTest(receipt=name):
                self.assertEqual(document["kernels"],
                                 ["python", "souffle", "souffle-compiled"])
                self.assertEqual(document["differential"], "ran")
                self.assertTrue(document["differential_report"]["matched"])
                self.assertTrue(document["differential_report"]["closure_digest_equal"])
                self.assertEqual(verdicts(document), self.table["receipts"][name])

    @unittest.skipUnless(shutil.which("souffle"), SOUFFLE_ONLY)
    def test_the_pairwise_set_runs_two_kernels_and_reaches_them_too(self) -> None:
        document = judge(RECEIPTS["synthetic"], self.tmp / "pair", "python,souffle")
        self.assertEqual(document["kernels"], ["python", "souffle"])
        self.assertEqual(document["differential"], "ran")
        self.assertIsNone(document["compiled"], "no binary is compiled for the pairwise ask")
        self.assertEqual(verdicts(document), self.table["receipts"]["synthetic"])


class EvaluatorResolutionTests(unittest.TestCase):
    """What ``--evaluator`` accepts, refuses, and what it never does silently."""

    def test_the_default_is_python_alone(self) -> None:
        self.assertEqual(differential.resolve_evaluators(None), ("python",))

    def test_a_comma_list_is_ordered_and_deduplicated(self) -> None:
        self.assertEqual(differential.resolve_evaluators("souffle-compiled,python,souffle"),
                         ("python", "souffle", "souffle-compiled"))
        self.assertEqual(differential.resolve_evaluators("python,python"), ("python",))

    def test_all_is_every_available_evaluator_and_python_is_always_one(self) -> None:
        resolved = differential.resolve_evaluators("all")
        self.assertIn("python", resolved)
        self.assertEqual(resolved, differential.available_evaluators())
        if shutil.which("souffle") is None:
            self.assertEqual(resolved, ("python",))
        else:
            self.assertEqual(resolved, ("python", "souffle", "souffle-compiled"))

    def test_an_unknown_name_is_refused_rather_than_ignored(self) -> None:
        with self.assertRaises(differential.UnknownEvaluator) as caught:
            differential.resolve_evaluators("python,coq")
        self.assertIn("unknown evaluator 'coq'", str(caught.exception))

    def test_requiring_an_absent_souffle_names_the_tool_and_the_way_out(self) -> None:
        with self.assertRaises(differential.EvaluatorUnavailable) as caught:
            differential.require_evaluators(("souffle",), executable="souffle-not-installed")
        message = str(caught.exception)
        self.assertIn("--evaluator souffle needs the Souffle 2.5 executable", message)
        self.assertIn("souffle-not-installed", message)
        self.assertIn("not on PATH or $SOUFFLE", message)
        self.assertIn("Install it with:", message)
        self.assertIn("or use --evaluator python", message)

    def test_requiring_python_never_raises(self) -> None:
        differential.require_evaluators(("python",), executable="souffle-not-installed")

    def test_the_souffle_executable_honours_the_environment(self) -> None:
        import os

        self.assertEqual(differential.souffle_executable("explicit"), "explicit")
        previous = os.environ.get("SOUFFLE")
        os.environ["SOUFFLE"] = "/opt/souffle/bin/souffle"
        try:
            self.assertEqual(differential.souffle_executable(), "/opt/souffle/bin/souffle")
        finally:
            if previous is None:
                del os.environ["SOUFFLE"]
            else:
                os.environ["SOUFFLE"] = previous
        self.assertEqual(differential.souffle_executable(), previous or "souffle")

    def test_a_single_evaluator_that_fails_is_not_an_agreement_with_itself(self) -> None:
        """Fail-closed with one kernel: a named operational failure is never a verdict.

        With two kernels a failure blocks through ``reports_match``.  With one
        there is nothing to compare it against, so the check has to be made
        explicitly -- otherwise a kernel that produced no closure at all would be
        recorded as ``matched`` and its (empty) relations certified.
        """
        from capcov.claims.replay import pack as replay_pack

        planted = differential.KernelReport("python", (), (), "python-evaluation-invalid",
                                            "planted failure")
        root = Path(tempfile.mkdtemp(prefix="capcov-evaluator-failed-"))
        self.addCleanup(shutil.rmtree, root, True)
        with self.assertRaises(differential.EvaluatorMismatch) as caught:
            differential.run_evaluators(replay_pack.pack_bundle(), ("python",),
                                        runners={"python": lambda bundle: planted},
                                        replay_root=root)
        self.assertIn("claim evaluator failed (python: python-evaluation-invalid)",
                      str(caught.exception))
        self.assertFalse(caught.exception.result.matched)
        self.assertEqual(caught.exception.result.differential, "not-run (single evaluator)")
        self.assertTrue(sorted(root.glob("evaluators-*.json")),
                        "the bundle nothing could answer for is persisted for replay")

    def test_a_single_evaluator_result_says_the_differential_did_not_run(self) -> None:
        """The fail-closed property in miniature: ``matched`` alone must not be readable
        as "the kernels agreed" when there was only one of them."""
        from capcov.claims.replay import pack as replay_pack

        result = differential.run_evaluators(replay_pack.pack_bundle(), ("python",))
        self.assertEqual(result.evaluators, ("python",))
        self.assertEqual(result.differential, "not-run (single evaluator)")
        self.assertTrue(result.matched)
        self.assertIsNone(result.souffle)
        self.assertIsNone(result.compiled)
        self.assertEqual([report.backend for report in result.reports], ["python"])


class SouffleIsWaitedForNeverFailedFor(unittest.TestCase):
    """The other half of ``--evaluator``: no suite may *fail* for an absent kernel.

    Choosing the kernels is only half optional if the suites that exercise the
    Soufflé ones report failures on a plain checkout -- a reviewer reading nine
    red lines cannot tell "the tool is not installed" from "the kernels
    disagree", which is the one distinction this whole line is about.  So the
    Soufflé-only suites must declare their dependence as a *skip*, and these
    assertions hold in both environments: with the interpreter present nothing
    is skipped, without it everything Soufflé-only is, by name.

    Deliberately checked through ``__unittest_skip__`` rather than by running
    the suites: running them proves nothing on a host that has no souffle, and
    costs a compile on one that does.
    """

    @staticmethod
    def _suite(name: str):
        try:
            return importlib.import_module(f"tests.claim_semantics.{name}")
        except ImportError:  # unittest discover -s imports this directory as top-level
            return importlib.import_module(name)

    def test_the_three_kernel_receipt_suites_wait_for_the_interpreter(self) -> None:
        """The compiled-kernel module's corpus class waits for the interpreter."""
        absent = shutil.which("souffle") is None
        module = self._suite("test_souffle_compiled_kernel")
        for name in ("SouffleCompiledCorpusTests",):
            with self.subTest(suite=name):
                cls = getattr(module, name)
                self.assertEqual(getattr(cls, "__unittest_skip__", False), absent,
                                 "a suite that is only about the souffle kernels skips "
                                 "when souffle is absent and runs when it is here")
                self.assertIn("souffle", getattr(cls, "__unittest_skip_why__", "souffle"),
                              "the skip names the tool it is waiting for")

    def test_the_kernel_boundary_keeps_its_python_half_without_the_interpreter(self) -> None:
        """The split: python's boundary is checked everywhere, souffle's waits."""
        module = self._suite("test_kernel_closure")
        cls = module.KernelClosureTests
        self.assertFalse(getattr(cls, "__unittest_skip__", False),
                         "the kernel-closure suite is mostly python and never skips wholesale")
        python_half = cls.test_forall_context_alias_is_rejected_by_the_python_kernel_boundary
        souffle_half = cls.test_forall_context_alias_is_rejected_by_the_souffle_kernel_boundary
        self.assertFalse(getattr(python_half, "__unittest_skip__", False))
        self.assertEqual(getattr(souffle_half, "__unittest_skip__", False),
                         shutil.which("souffle") is None)


class AssumptionsCliEvaluatorTests(unittest.TestCase):
    """The experiment CLI takes the same flag, with the same default and refusal.

    ``capcov experiment claims assumptions`` judges a receipt to build its A2
    registry, so it had the same hard dependency on the interpreter.  PATH is
    emptied for every run here so the answers do not depend on whether this
    machine happens to have Souffle.
    """

    def _run(self, *extra: str):
        return subprocess.run(
            [sys.executable, "-m", "capcov", "experiment", "claims", "assumptions",
             "registry", "--receipt", str(RECEIPTS["synthetic"]), *extra],
            text=True, capture_output=True, cwd=str(PACKAGE_ROOT),
            env={**os.environ, "PATH": "", "PYTHONPATH": str(PACKAGE_ROOT / "src")})

    def test_the_default_builds_the_registry_with_no_tool_on_path(self) -> None:
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        document = json.loads(proc.stdout)
        self.assertTrue(document["registry"]["assumptions"])

    def test_asking_for_souffle_without_it_is_a_named_refusal(self) -> None:
        proc = self._run("--evaluator", "souffle")
        self.assertEqual(proc.returncode, 2, proc.stderr[-2000:])
        refusal = json.loads(proc.stdout)["refusal"]
        self.assertIn("--evaluator souffle needs the Souffle 2.5 executable", refusal)
        self.assertIn("or use --evaluator python", refusal)

    def test_an_unknown_evaluator_is_refused_before_the_receipt_is_read(self) -> None:
        proc = self._run("--evaluator", "datalog")
        self.assertEqual(proc.returncode, 2, proc.stderr[-2000:])
        self.assertIn("unknown evaluator 'datalog'", json.loads(proc.stdout)["refusal"])


if __name__ == "__main__":
    unittest.main()
