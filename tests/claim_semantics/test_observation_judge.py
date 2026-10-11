"""Strict CLI behavior: one Python kernel, external bindings, no fixture pass."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from capcov.claims.differential import KernelReport
from capcov.claims.observation import join as observation_join
from capcov.claims.observation import observation_facts as facts
from capcov.claims.observation import judge

HERE = Path(__file__).resolve().parent
AGREE = HERE / "fixtures" / "observation_receipt_agree"
GAP = HERE / "fixtures" / "observation_receipt_gap"
DISAGREE = HERE / "fixtures" / "observation_receipt_disagree"


def _fixture_identity() -> tuple[str, str]:
    receipt = json.loads((AGREE / facts.RECEIPT_FILE).read_text(encoding="utf-8"))
    return receipt["run"]["nonce"], receipt["fixture"]["digest"]


def _git_candidate(root: Path) -> tuple[str, str]:
    root.mkdir()
    (root / "main.py").write_text("answer = 42\n", encoding="utf-8")
    env = {**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
           "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}
    for args in (("init", "-q", "-b", "main"), ("add", "."),
                 ("commit", "-q", "-m", "candidate")):
        subprocess.run(("git", "-C", str(root), *args), check=True, capture_output=True, env=env)
    commit = subprocess.run(("git", "-C", str(root), "rev-parse", "HEAD"), check=True,
                            capture_output=True, text=True).stdout.strip()
    tree = subprocess.run(("git", "-C", str(root), "rev-parse", "HEAD^{tree}"), check=True,
                          capture_output=True, text=True).stdout.strip()
    return commit, tree


def _valid_invocation_record(root: Path, manifest: Path, *, state: str = "verified",
                             source_digest: str = "a" * 64) -> dict:
    commit, tree = _git_candidate(root)
    manifest.write_text('{"prepared":[]}\n', encoding="utf-8")
    manifest_sha = hashlib.sha256(manifest.read_bytes()).hexdigest()
    nonce, fixture = _fixture_identity()
    return {
        "format": "lane-a-invocation/v1",
        "run_nonce": nonce,
        "expected_fixture_digest": fixture,
        "expected_source_digest": source_digest,
        "candidate": {"commit": commit, "tree": tree},
        "incumbent": {
            "commit": "1" * 40,
            "runtime_commit": "2" * 40,
            "source_manifest_sha256": manifest_sha,
            "runtime_contract_sha256": "3" * 64,
            "baseline_contract_sha256": "4" * 64,
        },
        "fixture_setup": {
            "state": state,
            "services": {"mysql": state == "verified", "redis": state == "verified",
                         "mongo": state == "verified"},
            "schema_sha256": "5" * 64,
            "seed_recipe_sha256": "6" * 64,
            "identity_count": 2,
            "attestation_scope": "test producer setup record only; no independent verifier",
        },
    }


def _write_record(path: Path, document: dict) -> str:
    raw = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path.write_bytes(raw)
    return judge.invocation_identity_digest(document)


class ObservationJudgeCliTest(unittest.TestCase):
    def _args(self, tmp: Path, *, invocation: Path | None = None,
              invocation_digest: str | None = None, nonce: str | None = None,
              fixture: str | None = None) -> list[str]:
        receipt = tmp / "receipt"
        shutil.copytree(AGREE, receipt)
        receipt_nonce, receipt_fixture = _fixture_identity()
        args = ["--receipt", str(receipt), "--candidate-root", str(tmp / "candidate"),
                "--incumbent-manifest", str(tmp / "manifest.json"),
                "--expected-nonce", nonce or receipt_nonce,
                "--expected-fixture-digest", fixture or receipt_fixture,
                "--out-dir", str(tmp / "out")]
        if invocation is not None:
            args.extend(("--invocation-record", str(invocation),
                         "--expected-invocation-digest", invocation_digest or "0" * 64))
        return args

    def _run(self, args: list[str]) -> tuple[int, dict, str]:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = judge.main(args)
        text = stdout.getvalue()
        return code, json.loads(text), text

    def test_golden_receipt_cannot_default_to_fixture_agreement(self) -> None:
        with tempfile.TemporaryDirectory(prefix="capcov-observation-cli-") as tmp:
            root = Path(tmp)
            code, report, text = self._run(self._args(root))
            self.assertEqual(code, 5)
            self.assertEqual(report["judge_status"], "pending")
            self.assertEqual(report["decision_basis"], "external-invocation-record-absent")
            self.assertIsNone(report["observations_agree"])
            self.assertEqual(report["kernels"], ["python"])
            self.assertEqual(report["kernel_execution"], "not-run")
            self.assertEqual(report["external_bindings"]["admission_record_status"], "absent")
            self.assertNotIn(str(root), text)

    def test_stale_external_source_binding_stays_pending_and_single_kernel(self) -> None:
        with tempfile.TemporaryDirectory(prefix="capcov-observation-cli-") as tmp:
            root = Path(tmp)
            record_path = root / "invocation.json"
            record = _valid_invocation_record(root / "candidate", root / "manifest.json")
            digest = _write_record(record_path, record)
            code, report, _text = self._run(self._args(root, invocation=record_path,
                                                       invocation_digest=digest))
            self.assertEqual(code, 5)
            self.assertEqual(report["judge_status"], "pending")
            self.assertFalse(report["source_observed_matches"])
            self.assertEqual(report["kernels"], ["python"])
            self.assertNotEqual(report["status"], "kernel-mismatch")

    def test_wrong_external_nonce_binding_stays_pending(self) -> None:
        with tempfile.TemporaryDirectory(prefix="capcov-observation-cli-") as tmp:
            root = Path(tmp)
            record_path = root / "invocation.json"
            record = _valid_invocation_record(root / "candidate", root / "manifest.json")
            digest = _write_record(record_path, record)
            code, report, _text = self._run(self._args(root, invocation=record_path,
                                                       invocation_digest=digest, nonce="f" * 64))
            self.assertEqual(code, 5)
            self.assertEqual(report["decision_basis"], "external-nonce-or-fixture-binding-mismatch")
            self.assertIsNone(report["observations_agree"])

    def test_external_admission_bytes_are_independently_bound(self) -> None:
        with tempfile.TemporaryDirectory(prefix="capcov-observation-cli-") as tmp:
            root = Path(tmp)
            args = self._args(root)
            admission = root / "reviewed-admissions.json"
            shutil.copyfile(AGREE / facts.ADMISSIONS_FILE, admission)
            digest = hashlib.sha256(admission.read_bytes()).hexdigest()
            args.extend(("--admission-record", str(admission),
                         "--expected-admission-digest", digest))
            code, report, _text = self._run(args)
            self.assertEqual(code, 5)  # Invocation binding is deliberately absent.
            self.assertEqual(report["external_bindings"]["admission_record_status"], "verified")
            self.assertEqual(report["external_bindings"]["admission_record_sha256"], digest)

    def test_wrong_external_admission_digest_is_invalid_input_without_path_leak(self) -> None:
        with tempfile.TemporaryDirectory(prefix="capcov-observation-cli-") as tmp:
            root = Path(tmp)
            args = self._args(root)
            admission = root / "reviewed-admissions.json"
            shutil.copyfile(AGREE / facts.ADMISSIONS_FILE, admission)
            args.extend(("--admission-record", str(admission),
                         "--expected-admission-digest", "0" * 64))
            code, report, text = self._run(args)
            self.assertEqual(code, 3)
            self.assertEqual(report["judge_status"], "invalid-input")
            self.assertNotIn(str(admission), text)

    def test_admission_snapshot_is_consumed_if_path_changes_after_digest_check(self) -> None:
        with tempfile.TemporaryDirectory(prefix="capcov-observation-cli-") as tmp:
            root = Path(tmp)
            args = self._args(root)
            admission = root / "reviewed-admissions.json"
            signed = json.loads((AGREE / facts.ADMISSIONS_FILE).read_text(encoding="utf-8"))
            unsigned = {**signed, "reviewer": "unassigned"}
            unsigned_bytes = (json.dumps(unsigned, indent=1, sort_keys=True) + "\n").encode("utf-8")
            signed_bytes = (AGREE / facts.ADMISSIONS_FILE).read_bytes()
            admission.write_bytes(unsigned_bytes)
            digest = hashlib.sha256(unsigned_bytes).hexdigest()
            args.extend(("--admission-record", str(admission),
                         "--expected-admission-digest", digest))
            real_build = observation_join.build
            built = []

            def replace_before_export(directory, **kwargs):
                admission.write_bytes(signed_bytes)
                join = real_build(directory, **kwargs)
                built.append(join)
                return join

            with patch.object(observation_join, "build", side_effect=replace_before_export):
                code, report, _text = self._run(args)

            self.assertEqual(code, 5)  # Invocation binding is deliberately absent.
            self.assertNotEqual(hashlib.sha256(admission.read_bytes()).hexdigest(), digest)
            self.assertEqual(report["external_bindings"]["admission_record_sha256"], digest)
            self.assertEqual(built[0].exported.counts.get("policy_admitted", 0), 0)
            self.assertEqual(built[0].exported.counts.get("scenario_set_admitted", 0), 0)

    def test_unverified_fixture_setup_stays_pending(self) -> None:
        with tempfile.TemporaryDirectory(prefix="capcov-observation-cli-") as tmp:
            root = Path(tmp)
            record_path = root / "invocation.json"
            record = _valid_invocation_record(root / "candidate", root / "manifest.json", state="pending")
            digest = _write_record(record_path, record)
            code, report, _text = self._run(self._args(root, invocation=record_path,
                                                       invocation_digest=digest))
            self.assertEqual(code, 5)
            self.assertEqual(report["decision_basis"], "fixture-provisioning-not-verified")

    def test_pre_run_identity_digest_excludes_only_mutable_fixture_setup(self) -> None:
        record = {
            "format": "lane-a-invocation/v1", "run_nonce": "0" * 64,
            "expected_fixture_digest": "1" * 64, "expected_source_digest": "2" * 64,
            "candidate": {"commit": "3" * 40, "tree": "4" * 40},
            "incumbent": {"commit": "5" * 40, "runtime_commit": "6" * 40,
                          "source_manifest_sha256": "7" * 64,
                          "runtime_contract_sha256": "8" * 64,
                          "baseline_contract_sha256": "9" * 64},
            "fixture_setup": {"state": "pending"},
        }
        digest = judge.invocation_identity_digest(record)
        record["fixture_setup"] = {"state": "verified"}
        self.assertEqual(judge.invocation_identity_digest(record), digest)
        self.assertEqual(digest, "11da8521932f789da3eec4cce750f3322d8eb059b1d206368ad9235ee7536818")

    def test_mutating_an_immutable_invocation_field_breaks_pre_run_binding(self) -> None:
        with tempfile.TemporaryDirectory(prefix="capcov-observation-cli-") as tmp:
            root = Path(tmp)
            record_path = root / "invocation.json"
            record = _valid_invocation_record(root / "candidate", root / "manifest.json")
            digest = judge.invocation_identity_digest(record)
            record["expected_source_digest"] = "b" * 64
            _write_record(record_path, record)
            code, report, _text = self._run(self._args(root, invocation=record_path,
                                                       invocation_digest=digest))
            self.assertEqual(code, 5)
            self.assertEqual(report["decision_basis"],
                             "pre-run-invocation-identity-digest-mismatch")

    def test_python_setup_failure_is_pending_and_never_agreement(self) -> None:
        join = observation_join.build(GAP, fixture=True)
        observation_join.evaluate_python(join)
        report = judge._report(join)
        self.assertEqual(report["judge_status"], "pending")
        self.assertEqual(report["kernels"], ["python"])
        self.assertEqual(report["observations_agree"], "unresolved")
        self.assertIn("scenario_timed_out", {row["relation"] for row in report["diagnostics"]})

    def test_unsigned_admission_stays_unresolved_pending_five(self) -> None:
        join = observation_join.build(AGREE, fixture=True, allow_receipt_admissions=False)
        observation_join.evaluate_python(join)
        report = judge._report(join)
        self.assertEqual(report["judge_status"], "pending")
        self.assertEqual(report["exit_code"], 5)
        self.assertEqual(report["blocking_premise"]["relation"], "policy_bound")
        self.assertEqual(report["observations_agree"], "unresolved")

    def test_real_disagreement_is_unsupported_even_when_admission_is_missing(self) -> None:
        join = observation_join.build(DISAGREE, fixture=True, allow_receipt_admissions=False)
        observation_join.evaluate_python(join)
        report = judge._report(join)
        self.assertEqual(report["judge_status"], "unsupported")
        self.assertEqual(report["exit_code"], 1)
        self.assertIn("scenario_disagree", {row["relation"] for row in report["diagnostics"]})

    def test_receipt_and_output_directory_collision_is_refused(self) -> None:
        with tempfile.TemporaryDirectory(prefix="capcov-observation-cli-") as tmp:
            root = Path(tmp)
            args = self._args(root)
            receipt = str(root / "receipt")
            out_index = args.index("--out-dir") + 1
            args[out_index] = receipt
            code, report, _text = self._run(args)
            self.assertEqual(code, 3)
            self.assertEqual(report["judge_status"], "invalid-input")
            self.assertEqual(report["operational_failure"], "invalid-input")
            self.assertTrue((root / "receipt" / facts.RECEIPT_FILE).is_file())

    def test_python_operational_failure_dominates_semantic_verdict(self) -> None:
        join = observation_join.build(AGREE, fixture=True)
        with patch("capcov.claims.observation.join.run_python",
                   return_value=KernelReport("python", (), (), "resource-exhausted")):
            observation_join.evaluate_python(join)
        report = judge._report(join)
        self.assertEqual(report["judge_status"], "operational-failure")
        self.assertEqual(report["exit_code"], 3)
        self.assertEqual(report["operational_failure"], "resource-exhausted")

    def test_fixture_support_has_one_python_kernel_and_rechecked_certificates(self) -> None:
        join = observation_join.build(AGREE, fixture=True)
        observation_join.evaluate_python(join)
        report = judge._report(join)
        self.assertEqual(report["judge_status"], "supported")
        self.assertEqual(report["kernels"], ["python"])
        self.assertIn("certificate-observations-agree.json", report["certificates"])
        self.assertEqual(report["source_digest"], join.source_digest)
        self.assertEqual(len(report["python_result_digest"]), 64)


if __name__ == "__main__":
    unittest.main()
