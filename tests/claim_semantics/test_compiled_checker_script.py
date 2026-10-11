"""``scripts/compiled_checker.py`` end to end, as a subprocess (live binary).

The script is the cross-repo contract: a producer repo runs it on an exported
receipt directory and gates its build on the exit code, so the tests drive the
real CLI rather than its functions.  The synthetic receipt
(``fixtures/replay_receipt_min``) is judged with and without an external
reviewer admission, the option-only invocation (no subcommand) that a
producer's gate uses is asserted to be the same as ``judge``, and every
refusal path is pinned to its documented exit code -- including exit 5, the
"pending a premise" state a receipt is in until a reviewer admits the exact
checker result, which a consumer gate must be able to tell apart from exit 1.

Every judge invocation here passes ``--evaluator all`` -- every evaluator whose
tool is present, which is all three kernels in the pinned devShell and the
stdlib Python kernel alone in a plain checkout.  The script's default is python
alone, so a producer repo that gates on three kernels names them, and
``judge.json`` records which ones ran.  Souffle is therefore NOT a precondition
of this class: the verdict, the exit code and every contract finding are the
same answer with one kernel as with three, and only the assertions that are
*about* the interpreter and the compiled binary are skipped by name when it is
absent (``SOUFFLE_ONLY``).  What is never allowed is a smaller run passing for a
bigger one, so every case asserts which kernels ran and whether a differential
ran at all.  One compiled binary is shared by every case through
``CAPCOV_SOUFFLE_CACHE_DIR``; without it a temp cache compiles once for the
whole class.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from capcov.claims.differential import available_evaluators

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = PACKAGE_ROOT / "scripts" / "compiled_checker.py"
SYNTHETIC_RECEIPT_DIR = Path(__file__).resolve().parent / "fixtures" / "replay_receipt_min"
HEX64 = r"^[0-9a-f]{64}$"
CACHE_ENV = "CAPCOV_SOUFFLE_CACHE_DIR"
JUDGE_KEYS = {"schema", "receipt", "pack", "compiled", "kernels", "differential",
              "differential_report", "ops", "required_ops",
              "contract_findings", "verdict", "exit_code", "learn"}
THREE = ["python", "souffle", "souffle-compiled"]
HAVE_SOUFFLE = shutil.which("souffle") is not None
SOUFFLE_ONLY = "this assertion is about the souffle kernels; souffle is not on PATH here"
#: judge.json writes a digest only for a kernel that ran, under these names
DIGEST_KEY = {"python": "python_digest", "souffle": "souffle_digest",
              "souffle-compiled": "compiled_digest"}
# what a verdict that is not plain "supported" adds
PENDING_KEYS = JUDGE_KEYS | {"unmet_ops", "pending_ops", "message"}


class CompiledCheckerScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        configured = os.environ.get(CACHE_ENV)
        cls.owns_cache = not configured
        cls.cache = Path(configured) if configured else Path(
            tempfile.mkdtemp(prefix="capcov-script-cache-"))
        cls.cache.mkdir(parents=True, exist_ok=True)
        cls.workspace = Path(tempfile.mkdtemp(prefix="capcov-script-out-"))

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.owns_cache:
            shutil.rmtree(cls.cache, ignore_errors=True)
        shutil.rmtree(cls.workspace, ignore_errors=True)

    def setUp(self) -> None:
        self.assertTrue(SCRIPT.is_file(), SCRIPT)

    def expected_kernels(self) -> list[str]:
        """What ``--evaluator all`` resolves to here: every evaluator present."""
        return list(available_evaluators())

    def assert_kernels_recorded(self, document: dict) -> None:
        """Every kernel that ran agreed, and judge.json says which ran and whether.

        Evaluator-agnostic on purpose: with one kernel ``matched`` is vacuous, so
        it is never asserted alone -- ``differential`` must say ``not-run (single
        evaluator)`` in that case and ``ran`` only when two or more kernels were
        there.  A digest for a kernel that did not run would be the smaller run
        passing for the bigger one, so the digest keys are asserted exactly.
        """
        expected = self.expected_kernels()
        self.assertEqual(document["kernels"], expected,
                         "--evaluator all runs every kernel present here")
        self.assertEqual(document["differential"],
                         "ran" if len(expected) > 1 else "not-run (single evaluator)")
        report = document["differential_report"]
        self.assertTrue(report["matched"])
        self.assertEqual(report["failures"], {})
        present = [DIGEST_KEY[name] for name in expected]
        self.assertEqual(sorted(key for key in DIGEST_KEY.values() if key in report),
                         sorted(present), "no digest is written for a kernel that did not run")
        self.assertEqual(len({report[key] for key in present}), 1,
                         "every kernel that ran closed to the same digest")
        for key in present:
            self.assertRegex(report[key], HEX64, key)
        self.assertGreater(report["python_seconds"], 0.0)
        # the closure digest compares the two Souffle kernels with each other: it
        # is unknown, not unequal, when fewer than two of them ran
        self.assertEqual(report["closure_digest_equal"],
                         True if len(expected) == 3 else None)

    def run_script(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=PACKAGE_ROOT,
                              capture_output=True, text=True, check=False)

    def out(self, name: str) -> Path:
        path = self.workspace / name
        shutil.rmtree(path, ignore_errors=True)
        return path

    def judge(self, receipt: Path, name: str, *extra: str) -> tuple[subprocess.CompletedProcess, Path]:
        out = self.out(name)
        completed = self.run_script("--receipt", str(receipt), "--out", str(out),
                                    "--cache-dir", str(self.cache), "--souffle", "souffle",
                                    "--evaluator", "all", *extra)
        return completed, out

    def synthetic_admissions(self) -> Path:
        rows = json.loads((SYNTHETIC_RECEIPT_DIR / "model_operation_checked.json").read_text())["rows"]
        path = self.workspace / "synthetic-reviewer-admissions.json"
        path.write_text(json.dumps([{"producer": "reviewer synthetic-test-policy", **row}
                                    for row in rows]), encoding="utf-8")
        return path

    def test_the_synthetic_receipt_is_supported_and_exits_zero(self) -> None:
        """The positive exit-0 path, on the corpus fixture whose every fact is made up.

        ``replay_receipt_min`` is the only receipt carrying a ``model_well_formed``
        certificate, because it is synthetic throughout and labelled so; it is what
        keeps the supported/complete path of the CLI exercised while Stage D does
        not exist.  It is never evidence about the port.
        """
        admissions = self.synthetic_admissions()
        completed, out = self.judge(
            SYNTHETIC_RECEIPT_DIR, "synthetic",
            "--reviewer-admissions", str(admissions),
            "--require-supported", "delete-issue")
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        document = json.loads((out / "judge.json").read_text())
        self.assertEqual(set(document), JUDGE_KEYS)
        self.assertEqual(document["verdict"], "supported")
        self.assertEqual(document["exit_code"], 0)
        entry = document["ops"]["delete-issue"]
        self.assertEqual(entry["verdict"], "supported")
        self.assertEqual(entry["qualification"], "qualified")
        self.assertEqual(entry["op_qualified"],
                         {"semantic": "supported", "operational": "complete", "missing_premises": []})
        self.assertIsNone(entry["blocking_premise"])
        self.assertRegex(entry["certificate_sha256"], HEX64)
        self.assertTrue((out / "certificate-claim-qualified-delete-issue.json").is_file())
        self.assert_kernels_recorded(document)

    def test_without_a_reviewer_admission_the_synthetic_receipt_is_pending(self) -> None:
        """The checked-operation certificate is in the receipt; nobody admitted it.

        A receipt-local certificate is never reviewer authority, so the op passes
        every premise it can show and stays pending on the admission: exit 5,
        never exit 0 and never exit 1.
        """
        completed, out = self.judge(SYNTHETIC_RECEIPT_DIR, "synthetic-unadmitted",
                                    "--require-supported", "delete-issue")
        self.assertEqual(completed.returncode, 5, completed.stderr[-2000:])
        document = json.loads((out / "judge.json").read_text())
        self.assertEqual(set(document), PENDING_KEYS)
        self.assertEqual(document["verdict"], "pending-premise")
        self.assertEqual(document["pending_ops"], ["delete-issue"])
        self.assertEqual(document["unmet_ops"], ["delete-issue"])
        self.assertIn("pending, not unsupported", document["message"])
        self.assertEqual(document["contract_findings"], [])
        self.assert_kernels_recorded(document)
        entry = document["ops"]["delete-issue"]
        self.assertEqual(entry["qualification"], "pending model_checker_admitted")
        self.assertEqual(entry["op_qualified"],
                         {"semantic": "unresolved", "operational": "complete",
                          "missing_premises": ["model_checker_admitted"]})
        self.assertIsNone(entry["certificate_sha256"], "an unresolved claim certifies no row")
        self.assertFalse((out / "certificate-claim-qualified-delete-issue.json").exists())

    @unittest.skipUnless(HAVE_SOUFFLE, SOUFFLE_ONLY)
    def test_the_souffle_kernels_run_and_the_compiled_binary_is_provenanced(self) -> None:
        """What ``--evaluator all`` adds when the interpreter is here: three kernels.

        The verdict is not re-asserted -- the tests above already pin it, with
        whichever kernels this machine has.  What is asserted here is only what a
        python-only run cannot say: that all three kernels ran, that a
        differential really ran, that the two Souffle kernels closed to the same
        digest, and that the compiled binary is the pack's own program.
        """
        completed, out = self.judge(SYNTHETIC_RECEIPT_DIR, "three-kernels",
                                    "--reviewer-admissions", str(self.synthetic_admissions()),
                                    "--require-supported", "delete-issue")
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        document = json.loads((out / "judge.json").read_text())
        self.assertEqual(document["kernels"], THREE)
        self.assertEqual(document["differential"], "ran")
        kernels = document["differential_report"]
        self.assertTrue(kernels["matched"])
        self.assertTrue(kernels["closure_digest_equal"])
        self.assertEqual(kernels["failures"], {})
        self.assertEqual(len({kernels["python_digest"], kernels["souffle_digest"],
                              kernels["compiled_digest"]}), 1)
        for field in ("interpreter_seconds", "compiled_seconds", "python_seconds"):
            self.assertGreater(kernels[field], 0.0, field)
        self.assertRegex(document["compiled"]["binary_sha256"], HEX64)
        self.assertEqual(document["compiled"]["schema"], "capcov-souffle-compiled-v1")
        self.assertEqual(document["compiled"]["program_digest"], document["pack"]["program_digest"])

    def test_judge_artifacts_carry_no_local_paths(self) -> None:
        admissions = self.synthetic_admissions()
        completed, out = self.judge(SYNTHETIC_RECEIPT_DIR, "no-paths",
                                    "--reviewer-admissions", str(admissions),
                                    "--require-supported", "delete-issue")
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        for path in sorted(out.glob("*.json")):
            text = path.read_text()
            with self.subTest(artifact=path.name):
                self.assertNotIn(str(out), text)
                self.assertNotIn(str(self.cache), text)
                self.assertNotIn(str(SYNTHETIC_RECEIPT_DIR), text)
                self.assertNotIn(str(admissions), text)
                self.assertNotIn(str(Path.home()), text)

    def test_the_option_only_invocation_is_the_judge_subcommand(self) -> None:
        """The producer repo's gate passes options with no subcommand word."""
        admissions = str(self.synthetic_admissions())
        bare, bare_out = self.judge(SYNTHETIC_RECEIPT_DIR, "bare",
                                    "--reviewer-admissions", admissions,
                                    "--require-supported", "delete-issue")
        named_out = self.out("named")
        named = self.run_script("judge", "--receipt", str(SYNTHETIC_RECEIPT_DIR),
                                "--out", str(named_out), "--cache-dir", str(self.cache),
                                "--souffle", "souffle", "--evaluator", "all",
                                "--reviewer-admissions", admissions,
                                "--require-supported", "delete-issue")
        self.assertEqual((bare.returncode, named.returncode), (0, 0), named.stderr[-2000:])
        stable = ("schema", "receipt", "pack", "compiled", "kernels", "differential", "ops",
                  "required_ops", "verdict", "exit_code", "contract_findings")
        left = json.loads((bare_out / "judge.json").read_text())
        right = json.loads((named_out / "judge.json").read_text())
        self.assertEqual({k: left[k] for k in stable}, {k: right[k] for k in stable})

    def test_no_required_op_derives_the_verdict_from_every_replayed_op(self) -> None:
        """A verdict over zero requirements would be vacuous; every replayed op decides it."""
        # the synthetic receipt needs no requirement to be judged supported
        completed, out = self.judge(
            SYNTHETIC_RECEIPT_DIR, "no-requirement-synthetic",
            "--reviewer-admissions", str(self.synthetic_admissions()))
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        document = json.loads((out / "judge.json").read_text())
        self.assertEqual(document["required_ops"], [])
        self.assertEqual(document["verdict"], "supported")
        self.assertEqual(set(document), JUDGE_KEYS)

    def test_an_extra_receipt_key_is_a_contract_finding_and_exits_three(self) -> None:
        copy = self.workspace / "receipt-with-extra-key"
        shutil.rmtree(copy, ignore_errors=True)
        shutil.copytree(SYNTHETIC_RECEIPT_DIR, copy)
        receipt = json.loads((copy / "receipt.json").read_text())
        receipt["extra_key"] = 1
        (copy / "receipt.json").write_text(json.dumps(receipt, indent=1, sort_keys=True) + "\n")
        completed, out = self.judge(copy, "contract-finding", "--require-supported", "delete-issue")
        self.assertEqual(completed.returncode, 3, completed.stderr[-2000:])
        document = json.loads((out / "judge.json").read_text())
        self.assertEqual(document["verdict"], "contract-finding")
        self.assertEqual(document["exit_code"], 3)
        self.assertEqual(document["kernels"], [], "no kernel ran: the receipt was refused")
        self.assertIsNone(document["differential_report"])
        self.assertIsNone(document["compiled"])
        self.assertEqual(document["ops"], {})
        self.assertEqual(len(document["contract_findings"]), 1)
        self.assertIn("extra_key", document["contract_findings"][0])

    def test_an_absent_receipt_is_a_contract_finding_and_exits_three(self) -> None:
        out = self.out("no-receipt")
        completed = self.run_script(
            "--receipt", str(self.workspace / "there-is-no-receipt-here"), "--out", str(out),
            "--cache-dir", str(self.cache), "--require-supported", "delete-issue")
        self.assertEqual(completed.returncode, 3, completed.stderr[-2000:])
        self.assertIn("contract finding", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    def test_an_unwritable_output_directory_blames_the_judge_not_the_receipt(self) -> None:
        """The judge's own I/O is an unavailable environment (4), not a contract finding (3)."""
        blocker = self.workspace / "out-is-a-file"
        blocker.write_text("", encoding="utf-8")
        completed = self.run_script(
            "--receipt", str(SYNTHETIC_RECEIPT_DIR), "--out", str(blocker),
            "--cache-dir", str(self.cache), "--souffle", "souffle",
            "--require-supported", "delete-issue")
        self.assertEqual(completed.returncode, 4, completed.stderr[-2000:])
        self.assertIn("judge environment unavailable", completed.stderr)
        self.assertNotIn("contract finding", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)

    def test_an_asked_for_souffle_that_is_absent_is_unavailable_and_exits_four(self) -> None:
        """Named explicitly, so this is "the tool I asked for is missing", not "use less"."""
        out = self.out("unavailable")
        completed = self.run_script(
            "--receipt", str(SYNTHETIC_RECEIPT_DIR), "--out", str(out),
            "--cache-dir", str(self.cache), "--souffle", "souffle-that-is-not-installed",
            "--evaluator", "python,souffle,souffle-compiled",
            "--require-supported", "delete-issue")
        self.assertEqual(completed.returncode, 4, completed.stderr[-2000:])
        self.assertIn("toolchain unavailable", completed.stderr)
        self.assertIn("souffle-that-is-not-installed", completed.stderr)
        self.assertIn("Install it with:", completed.stderr)
        document = json.loads((out / "judge.json").read_text())
        self.assertEqual(document["verdict"], "unavailable")
        self.assertEqual(document["exit_code"], 4)
        self.assertEqual(document["kernels"], [])
        self.assertIsNone(document["compiled"])

    def test_the_default_evaluator_judges_with_the_python_kernel_alone(self) -> None:
        """No --evaluator: the script judges with the standard library and says so.

        The verdict is the same one all three kernels reach (exit 5 on this
        receipt, which carries no reviewer admission); what changes is that judge.json names one kernel and records
        that no differential ran, so a producer gate cannot mistake this run for
        the three-kernel one it asks for with --evaluator all.
        """
        out = self.out("default-evaluator")
        completed = self.run_script(
            "--receipt", str(SYNTHETIC_RECEIPT_DIR), "--out", str(out),
            "--cache-dir", str(self.cache), "--require-supported", "delete-issue")
        self.assertEqual(completed.returncode, 5, completed.stderr[-2000:])
        document = json.loads((out / "judge.json").read_text())
        self.assertEqual(document["kernels"], ["python"])
        self.assertEqual(document["differential"], "not-run (single evaluator)")
        self.assertIsNone(document["compiled"], "no binary is compiled for a python-only judge")
        self.assertEqual(document["verdict"], "pending-premise")
        self.assertEqual(document["ops"]["delete-issue"]["qualification"],
                         "pending model_checker_admitted")

    def test_an_unknown_evaluator_is_a_usage_refusal_and_judges_nothing(self) -> None:
        out = self.out("unknown-evaluator")
        completed = self.run_script(
            "--receipt", str(SYNTHETIC_RECEIPT_DIR), "--out", str(out),
            "--cache-dir", str(self.cache), "--evaluator", "z3")
        self.assertEqual(completed.returncode, 2, completed.stderr[-2000:])
        self.assertIn("unknown evaluator 'z3'", completed.stderr)
        self.assertFalse(out.exists())

    @unittest.skipUnless(HAVE_SOUFFLE, SOUFFLE_ONLY)
    def test_bench_scales_the_receipt_and_records_both_medians(self) -> None:
        out = self.out("bench")
        completed = self.run_script(
            "bench", "--receipt", str(SYNTHETIC_RECEIPT_DIR), "--out", str(out),
            "--scale", "2", "--repeat", "2", "--cache-dir", str(self.cache))
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        document = json.loads((out / "bench.json").read_text())
        self.assertEqual(document["schema"], "capcov-compiled-bench-v1")
        self.assertEqual(document["fixture"], SYNTHETIC_RECEIPT_DIR.name)
        self.assertEqual(document["scale"], 2)
        # 115 before checker authority became per operation; model_operation_checked is the 116th
        self.assertEqual(document["relation_count"], 116)
        self.assertTrue(document["closures_identical"])
        self.assertRegex(document["binary_sha256"], HEX64)
        self.assertRegex(document["souffle"]["sha256"], HEX64)
        for kernel in ("interpreter", "compiled"):
            with self.subTest(kernel=kernel):
                self.assertEqual(len(document[kernel]["runs"]), 2)
                self.assertGreater(document[kernel]["median_s"], 0.0)
        # scale 2 doubles the req-keyed rows and leaves the model rows alone
        one = self.out("bench-1x")
        self.assertEqual(self.run_script(
            "bench", "--receipt", str(SYNTHETIC_RECEIPT_DIR), "--out", str(one),
            "--scale", "1", "--repeat", "1", "--cache-dir", str(self.cache)).returncode, 0)
        single = json.loads((one / "bench.json").read_text())
        scaled_rows = document["rows_in"] - single["rows_in"]
        self.assertGreater(scaled_rows, 0)
        self.assertEqual(document["rows_in"], single["rows_in"] + scaled_rows)
        self.assertTrue(single["closures_identical"])

    @unittest.skipUnless(HAVE_SOUFFLE, SOUFFLE_ONLY)
    def test_compile_prints_the_provenance_of_each_pack(self) -> None:
        digests = {}
        for pack in ("replay", "static"):
            with self.subTest(pack=pack):
                completed = self.run_script("compile", "--pack", pack,
                                            "--cache-dir", str(self.cache))
                self.assertEqual(completed.returncode, 0, completed.stderr[-4000:])
                provenance = json.loads(completed.stdout)
                self.assertEqual(provenance["pack"], pack)
                self.assertEqual(provenance["schema"], "capcov-souffle-compiled-v1")
                for field in ("compile_key", "program_digest", "binary_sha256", "souffle_sha256"):
                    self.assertRegex(provenance[field], HEX64, field)
                digests[pack] = provenance
                # a second invocation is a cache hit on the same key and binary
                again = json.loads(self.run_script(
                    "compile", "--pack", pack, "--cache-dir", str(self.cache)).stdout)
                self.assertTrue(again["cache_hit"])
                self.assertEqual(again["compile_key"], provenance["compile_key"])
                self.assertEqual(again["binary_sha256"], provenance["binary_sha256"])
        self.assertNotEqual(digests["replay"]["program_digest"], digests["static"]["program_digest"])
        self.assertNotEqual(digests["replay"]["compile_key"], digests["static"]["compile_key"])


if __name__ == "__main__":
    unittest.main()
