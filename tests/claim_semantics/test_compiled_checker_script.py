"""``scripts/compiled_checker.py`` end to end, as a subprocess (live binary).

The script is the cross-repo contract: a producer repo runs it on an exported
receipt directory and gates its build on the exit code, so the tests drive the
real CLI rather than its functions.  The synthetic receipt
(``fixtures/replay_receipt_min``) is judged, the option-only invocation (no
subcommand) that a producer's gate uses is asserted to be the same as
``judge``, and every refusal path is pinned to its documented exit code.

souffle is a precondition, not a skip.  One compiled binary is shared by every
case through ``CAPCOV_SOUFFLE_CACHE_DIR``; without it a temp cache compiles
once for the whole class.
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

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = PACKAGE_ROOT / "scripts" / "compiled_checker.py"
SYNTHETIC_RECEIPT_DIR = Path(__file__).resolve().parent / "fixtures" / "replay_receipt_min"
HEX64 = r"^[0-9a-f]{64}$"
CACHE_ENV = "CAPCOV_SOUFFLE_CACHE_DIR"
JUDGE_KEYS = {"schema", "receipt", "pack", "compiled", "kernels", "ops", "required_ops",
              "contract_findings", "verdict", "exit_code", "learn"}
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
        self.assertIsNotNone(shutil.which("souffle"),
                             "souffle must be on PATH: run inside the nix devShell")
        self.assertTrue(SCRIPT.is_file(), SCRIPT)

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
                                    *extra)
        return completed, out

    def test_the_synthetic_receipt_is_supported_and_exits_zero(self) -> None:
        """The positive exit-0 path, on the corpus fixture whose every fact is made up.

        ``replay_receipt_min`` is the only receipt carrying a ``model_well_formed``
        certificate, because it is synthetic throughout and labelled so; it is what
        keeps the supported/complete path of the CLI exercised while Stage D does
        not exist.  It is never evidence about the port.
        """
        completed, out = self.judge(SYNTHETIC_RECEIPT_DIR, "synthetic",
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

    def test_judge_artifacts_carry_no_local_paths(self) -> None:
        completed, out = self.judge(SYNTHETIC_RECEIPT_DIR, "no-paths",
                                    "--require-supported", "delete-issue")
        self.assertEqual(completed.returncode, 0, completed.stderr[-2000:])
        for path in sorted(out.glob("*.json")):
            text = path.read_text()
            with self.subTest(artifact=path.name):
                self.assertNotIn(str(out), text)
                self.assertNotIn(str(self.cache), text)
                self.assertNotIn(str(SYNTHETIC_RECEIPT_DIR), text)
                self.assertNotIn(str(Path.home()), text)

    def test_the_option_only_invocation_is_the_judge_subcommand(self) -> None:
        """The producer repo's gate passes options with no subcommand word."""
        bare, bare_out = self.judge(SYNTHETIC_RECEIPT_DIR, "bare",
                                    "--require-supported", "delete-issue")
        named_out = self.out("named")
        named = self.run_script("judge", "--receipt", str(SYNTHETIC_RECEIPT_DIR),
                                "--out", str(named_out), "--cache-dir", str(self.cache),
                                "--souffle", "souffle", "--require-supported", "delete-issue")
        self.assertEqual((bare.returncode, named.returncode), (0, 0), named.stderr[-2000:])
        stable = ("schema", "receipt", "pack", "compiled", "ops", "required_ops", "verdict",
                  "exit_code", "contract_findings")
        left = json.loads((bare_out / "judge.json").read_text())
        right = json.loads((named_out / "judge.json").read_text())
        self.assertEqual({k: left[k] for k in stable}, {k: right[k] for k in stable})

    def test_the_synthetic_receipt_needs_no_required_op(self) -> None:
        """A verdict over zero requirements would be vacuous; every replayed op decides it."""
        completed, out = self.judge(SYNTHETIC_RECEIPT_DIR, "no-requirement-synthetic")
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
        self.assertIsNone(document["kernels"])
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

    def test_an_absent_souffle_is_unavailable_and_exits_four(self) -> None:
        out = self.out("unavailable")
        completed = self.run_script(
            "--receipt", str(SYNTHETIC_RECEIPT_DIR), "--out", str(out),
            "--cache-dir", str(self.cache), "--souffle", "souffle-that-is-not-installed",
            "--require-supported", "delete-issue")
        self.assertEqual(completed.returncode, 4, completed.stderr[-2000:])
        self.assertIn("toolchain unavailable", completed.stderr)
        document = json.loads((out / "judge.json").read_text())
        self.assertEqual(document["verdict"], "unavailable")
        self.assertEqual(document["exit_code"], 4)
        self.assertIsNone(document["compiled"])

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
        self.assertEqual(document["relation_count"], 115)
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
