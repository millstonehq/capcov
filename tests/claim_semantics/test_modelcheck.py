"""Stage D: the typed model checker (`claims/modelcheck.py`, `shen/modelcheck/`).

The synthetic fixture `fixtures/model_min` is well formed by construction.
Each mutant below breaks exactly one thing the type rules are there to refuse,
and the test asserts that exactly the expected judgement fails and that no
`model_well_formed` fact is written.  The runtime is the pinned shen-go
through bifrost; without it every runtime test skips unless
`CAPCOV_SHEN_REQUIRED` is set, in which case the skip is an error.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from capcov.claims import modelcheck

HERE = Path(__file__).resolve().parent
MODEL_MIN = HERE / "fixtures" / "model_min"
RUNTIME_REASON = None
try:
    modelcheck.runtime()
except modelcheck.ModelcheckUnavailable as exc:
    RUNTIME_REASON = str(exc)
if RUNTIME_REASON and os.environ.get("CAPCOV_SHEN_REQUIRED"):
    raise RuntimeError("CAPCOV_SHEN_REQUIRED is set but " + RUNTIME_REASON)

# (mutant, file, old text, new text, judgement expected to fail)
MUTANTS = (
    ("duplicate-declared-table", "shen/model.shen",
     '["entity_statistics" "issue" "mongo:issue"]', '["entity_statistics" "issue" "issue" "mongo:issue"]',
     "writes:delete-issue"),
    ("undeclared-effect-table", "shen/model.shen",
     '[[sql-update "issue" I [[state -1]]]]', '[[sql-update "issue" I [[state -1]]] [sql-update "sessions" I [[touched 1]]]]',
     "writes:delete-issue"),
    ("declared-never-written", "shen/model.shen",
     '["entity_statistics" "issue" "mongo:issue"]', '["audit_log" "entity_statistics" "issue" "mongo:issue"]',
     "writes:delete-issue"),
    ("committed-on-nonlive-target-admitted", "shen/model.shen",
     "St [delete-issue A I] committed -> [(norn.successor-as-is St [delete-issue A I])]",
     "St [delete-issue A I] committed -> [St]\n  St [delete-issue _ _] committed -> [St]",
     None),  # see test: refusal on non-live committed becomes an admission
    ("unknown-admits-three", "shen/model.shen",
     "St [delete-issue A I] unknown -> [St (norn.successor-as-is St [delete-issue A I])]",
     "St [delete-issue A I] unknown -> [St St (norn.successor-as-is St [delete-issue A I])]",
     "matrix:delete-issue"),
    ("atlas-missing-required-fact", "shen/base.shen",
     '      [txn delete-issue required non-atomic [src "model.php" 2]]\n', "",
     "atlas:delete-issue"),
    ("registry-names-unknown-rule", "shen/model.shen",
     "[kd d-01 delete-issue no-orphan-child", "[kd d-01 delete-issue no-such-rule",
     "registry:1:d-01"),
    ("registry-duplicate-id", "shen/model.shen",
     '"the cascade does not run" [src "model.php" 2]]])',
     '"the cascade does not run" [src "model.php" 2]]\n      [kd d-01 delete-issue no-orphan-child target-with-children children-of-target "again" [src "model.php" 3]]])',
     "registry-ids"),
)


def _mutate(name: str) -> Path:
    for mutant, relative, old, new, _ in MUTANTS:
        if mutant == name:
            break
    else:
        raise KeyError(name)
    target = Path(tempfile.mkdtemp(prefix=f"capcov-model-min-{name}-")) / "model"
    shutil.copytree(MODEL_MIN, target)
    path = target / relative
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1, (name, text.count(old))
    path.write_text(text.replace(old, new), encoding="utf-8")
    return target


class RecipeTest(unittest.TestCase):
    """No runtime needed: the model's identity is a function of its sources."""

    def test_model_files_follow_load_order(self) -> None:
        self.assertEqual(modelcheck.model_files(MODEL_MIN), ["shen/load.shen", "shen/base.shen", "shen/model.shen"])

    def test_digest_is_sha256_over_the_concatenation_in_load_order(self) -> None:
        expected = hashlib.sha256(b"".join((MODEL_MIN / f).read_bytes() for f in modelcheck.model_files(MODEL_MIN))).hexdigest()
        self.assertEqual(modelcheck.model_digest(MODEL_MIN), expected)
        # a byte anywhere in a loaded file moves the digest
        mutated = _mutate("registry-names-unknown-rule")
        self.assertNotEqual(modelcheck.model_digest(mutated), expected)

    def test_a_directory_without_a_loader_is_refused(self) -> None:
        with self.assertRaises(modelcheck.ModelcheckFailure):
            modelcheck.model_files(Path(tempfile.mkdtemp(prefix="capcov-not-a-model-")))

    def test_only_a_well_formed_verdict_yields_a_fact(self) -> None:
        with self.assertRaises(modelcheck.ModelcheckFailure):
            modelcheck.well_formed_file({"verdict": "ill-formed", "model": "0" * 64, "certificate_sha256": "1" * 64})
        fact = modelcheck.well_formed_file({"verdict": "well-formed", "model": "a" * 64, "certificate_sha256": "b" * 64})
        self.assertEqual(fact["producer"], f"modelcheck {modelcheck.CHECKER} {modelcheck.CHECKER_VERSION} model:{'a' * 12}")
        self.assertEqual(fact["rows"][0]["certificate"], "b" * 64)


@unittest.skipIf(RUNTIME_REASON, RUNTIME_REASON or "")
class CheckerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.out = Path(tempfile.mkdtemp(prefix="capcov-modelcheck-out-"))
        cls.result = modelcheck.check(MODEL_MIN, out_dir=cls.out)

    def test_the_fixture_is_well_formed_and_every_judgement_is_typed(self) -> None:
        r = self.result
        self.assertEqual(r.status, "well-formed", [(j.id, j.message) for j in r.failures])
        self.assertEqual(sorted(j.id for j in r.judgements),
                         ["atlas:add-comment", "atlas:delete-issue", "matrix:delete-issue", "registry-ids",
                          "registry:1:d-01", "writes:delete-issue"])
        self.assertEqual(r.skipped, (("add-comment", "no as-is target on any live witness"),))
        self.assertEqual(r.model_digest, modelcheck.model_digest(MODEL_MIN))
        for j in r.judgements:
            self.assertEqual(j.verdict, "pass", j)
            self.assertTrue(j.text.startswith(f'(output "MC PASS {j.id} ~A~%" (mc.judge-'), j.text)
            self.assertEqual(hashlib.sha256(j.text.encode()).hexdigest(), j.unit_sha256)
        # the literal judged is the model's own answer, not a paraphrase
        writes = next(j for j in r.judgements if j.id == "writes:delete-issue")
        self.assertIn('[delete-issue ["entity_statistics" "issue" "mongo:issue"] ["entity_statistics" "issue" "mongo:issue"]', writes.text)
        matrix = next(j for j in r.judgements if j.id == "matrix:delete-issue")
        for cell in ("[committed live admits 1]", "[committed nonlive refuses 0]", "[aborted live admits 1]",
                     "[aborted nonlive admits 1]", "[unknown live admits 2]", "[unknown nonlive admits 1]"):
            self.assertIn(cell, matrix.text)

    def test_artifacts_and_the_fact(self) -> None:
        r = self.result
        fact = json.loads((self.out / "model_well_formed.json").read_text())
        self.assertEqual(fact, r.fact)
        self.assertEqual(fact["producer"].split(" ")[0], modelcheck.PRODUCER_CLASS)
        [row] = fact["rows"]
        self.assertEqual(row, {"model": r.model_digest, "checker": modelcheck.CHECKER,
                               "checker_version": modelcheck.CHECKER_VERSION,
                               "certificate": r.certificate["certificate_sha256"]})
        certificate = json.loads((self.out / "modelcheck-certificate.json").read_text())
        self.assertEqual(certificate, r.certificate)
        self.assertEqual(certificate["model_files"], [{"path": p, "sha256": s} for p, s in r.model_files])
        self.assertEqual(certificate["runtime"]["impl"], "shen-go")
        self.assertEqual(len(certificate["runtime"]["shen_go_sha256"]), 64)
        self.assertEqual([e["path"] for e in certificate["checker_sources"]][:2], ["prelude.shen", "types/table-list.shen"])
        transcript = (self.out / "modelcheck-transcript.txt").read_text()
        self.assertEqual(hashlib.sha256(transcript.encode()).hexdigest(), r.transcript_sha256)
        self.assertIn("MC DONE 6", transcript)

    def test_recheck_accepts_the_certificate_and_refuses_tampering(self) -> None:
        ok = modelcheck.recheck(self.result.certificate, MODEL_MIN)
        self.assertTrue(ok.ok, ok.problems)
        self.assertEqual(len(ok.unchecked), 1)  # the judgements themselves need the runtime
        forged = json.loads(json.dumps(self.result.certificate))
        forged["judgements"][0]["text"] += " "
        self.assertIn("does not hash", " ".join(modelcheck.recheck(forged, MODEL_MIN).problems))
        relabelled = json.loads(json.dumps(self.result.certificate))
        relabelled["verdict"] = "well-formed"
        relabelled["judgements"][0]["verdict"] = "fail"
        relabelled["certificate_sha256"] = modelcheck.certificate_digest(relabelled)
        problems = " ".join(modelcheck.recheck(relabelled, MODEL_MIN).problems)
        self.assertIn("well-formed verdict but judgement", problems)
        other_model = _mutate("registry-names-unknown-rule")
        self.assertIn("model digest differs", " ".join(modelcheck.recheck(self.result.certificate, other_model).problems))

    def test_each_mutant_fails_exactly_the_judgement_its_defect_breaks(self) -> None:
        for name, _, _, _, expected in MUTANTS:
            with self.subTest(mutant=name):
                out = Path(tempfile.mkdtemp(prefix=f"capcov-modelcheck-{name}-"))
                r = modelcheck.check(_mutate(name), out_dir=out)
                self.assertEqual(r.status, "ill-formed", name)
                failed = sorted(j.id for j in r.failures)
                if expected is None:
                    # admitting a committed outcome on a non-live target is a cell violation
                    self.assertEqual(failed, ["matrix:delete-issue"])
                else:
                    self.assertEqual(failed, [expected], [(j.id, j.message) for j in r.failures])
                for j in r.failures:
                    self.assertIn("type error", j.message.lower())
                self.assertIsNone(r.fact)
                self.assertFalse((out / "model_well_formed.json").exists())
                self.assertTrue((out / "modelcheck-certificate.json").exists())
                self.assertEqual(json.loads((out / "modelcheck-certificate.json").read_text())["verdict"], "ill-formed")

    def test_a_stale_fact_is_removed_when_the_model_becomes_ill_formed(self) -> None:
        out = Path(tempfile.mkdtemp(prefix="capcov-modelcheck-stale-"))
        modelcheck.check(MODEL_MIN, out_dir=out)
        self.assertTrue((out / "model_well_formed.json").exists())
        modelcheck.check(_mutate("registry-names-unknown-rule"), out_dir=out)
        self.assertFalse((out / "model_well_formed.json").exists())

    def test_cli_exit_codes(self) -> None:
        env = {**os.environ, "PYTHONPATH": str(HERE.parents[1] / "src")}
        good = subprocess.run([sys.executable, "-m", "capcov", "experiment", "claims", "modelcheck", "--model", str(MODEL_MIN)],
                              capture_output=True, text=True, env=env, timeout=600)
        self.assertEqual(good.returncode, 0, good.stderr[-500:])
        self.assertEqual(json.loads(good.stdout)["verdict"], "well-formed")
        bad = subprocess.run([sys.executable, "-m", "capcov", "experiment", "claims", "modelcheck", "--model",
                              str(_mutate("atlas-missing-required-fact"))], capture_output=True, text=True, env=env, timeout=600)
        self.assertEqual(bad.returncode, 1, bad.stderr[-500:])
        document = json.loads(bad.stdout)
        self.assertEqual(document["verdict"], "ill-formed")
        self.assertEqual([j["id"] for j in document["judgements"] if j["verdict"] == "fail"], ["atlas:delete-issue"])
        self.assertIsNone(document["fact"])
        missing = subprocess.run([sys.executable, "-m", "capcov", "experiment", "claims", "modelcheck", "--model",
                                  tempfile.mkdtemp(prefix="capcov-no-model-")], capture_output=True, text=True, env=env, timeout=600)
        self.assertEqual(missing.returncode, 3)
        self.assertEqual(json.loads(missing.stdout)["operational_failure"], "modelcheck-failure")


class PreflightTest(unittest.TestCase):
    """The profile entry never raises and names what is missing."""

    def test_unavailable_runtime_is_a_named_refusal(self) -> None:
        with unittest.mock.patch.dict(os.environ, {"BIFROST_SHEN_GO": ""}):
            report = modelcheck.preflight(MODEL_MIN)
        self.assertEqual(report["status"], "unavailable")
        self.assertIsNone(report["fact"])
        # whichever piece is missing first on this host: the launcher or the pinned binary
        self.assertIn("bifrost", report["error"].lower())

    def test_not_a_model_is_a_failure_not_a_verdict(self) -> None:
        report = modelcheck.preflight(tempfile.mkdtemp(prefix="capcov-no-model-"))
        self.assertIn(report["status"], ("failed", "unavailable"))
        self.assertIsNone(report["fact"])

    @unittest.skipIf(RUNTIME_REASON, RUNTIME_REASON or "")
    def test_verdicts_carry_the_fact_only_when_well_formed(self) -> None:
        good = modelcheck.preflight(MODEL_MIN)
        self.assertEqual(good["status"], "well-formed")
        self.assertEqual(good["fact"]["checker"], modelcheck.CHECKER)
        self.assertEqual(good["fact"]["certificate"], good["certificate_sha256"])
        bad = modelcheck.preflight(_mutate("atlas-missing-required-fact"))
        self.assertEqual(bad["status"], "ill-formed")
        self.assertIsNone(bad["fact"])
        self.assertEqual([f["id"] for f in bad["failures"]], ["atlas:delete-issue"])


@unittest.skipIf(RUNTIME_REASON, RUNTIME_REASON or "")
@unittest.skipUnless(os.environ.get("CAPCOV_MODEL_DIR"), "needs CAPCOV_MODEL_DIR (a real model checkout, read only)")
class LiveModelTest(unittest.TestCase):
    """The real model, when a checkout is named.  Nothing under it is written."""

    def test_the_named_model_is_well_formed_at_its_recorded_digest(self) -> None:
        model = os.environ["CAPCOV_MODEL_DIR"]
        r = modelcheck.check(model)
        self.assertEqual(r.status, "well-formed", [(j.id, j.message) for j in r.failures])
        expected = os.environ.get("CAPCOV_MODEL_DIGEST")
        if expected:
            self.assertEqual(r.model_digest, expected)
        self.assertTrue(modelcheck.recheck(r.certificate, model).ok)


if __name__ == "__main__":
    unittest.main()
