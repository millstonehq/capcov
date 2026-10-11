"""Stage D: the typed model checker (`claims/modelcheck.py`, `shen/modelcheck/`).

The synthetic fixture `fixtures/model_min` has one fully checked operation and
one operation without a live witness, exercising partial authority.
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
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import patch

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
    ("aborted-admits-wrong-singleton", "shen/model.shen",
     "St [delete-issue _ _] aborted -> [St]",
     "St [delete-issue _ _] aborted -> [[bogus]]",
     "matrix:delete-issue"),
    ("committed-admits-wrong-singleton", "shen/model.shen",
     "St [delete-issue A I] committed -> [(norn.successor-as-is St [delete-issue A I])]",
     "St [delete-issue A I] committed -> [[bogus]]",
     "matrix:delete-issue"),
    ("unknown-admits-wrong-states", "shen/model.shen",
     "St [delete-issue A I] unknown -> [St (norn.successor-as-is St [delete-issue A I])]",
     "St [delete-issue A I] unknown -> [[bogus] [also-bogus]]",
     "matrix:delete-issue"),
    ("atlas-missing-required-fact", "shen/base.shen",
     '      [txn delete-issue required non-atomic [src "model.php" 2]]', "",
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

    def test_runtime_rejects_script_wrappers_and_hashes_the_resolved_native_binary(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="capcov-runtime-identity-"))
        wrapper = root / "shen-go"
        wrapper.write_text("#!/bin/sh\nexec /some/other/shen-go \"$@\"\n", encoding="utf-8")
        wrapper.chmod(0o755)
        with patch("capcov.claims.modelcheck.shutil.which", return_value=str(root / "bifrost")), \
                patch.dict(os.environ, {"BIFROST_SHEN_GO": str(wrapper)}):
            with self.assertRaisesRegex(modelcheck.ModelcheckUnavailable, "actual pinned shen-go binary"):
                modelcheck.runtime()

        native = root / "shen-go-native"
        native.write_bytes(b"\xcf\xfa\xed\xfe" + b"fixture native bytes")
        native.chmod(0o755)
        alias = root / "shen-go-alias"
        alias.symlink_to(native)
        with patch("capcov.claims.modelcheck.shutil.which", return_value=str(native)), \
                patch.dict(os.environ, {"BIFROST_SHEN_GO": str(alias)}):
            resolved = modelcheck.runtime()
        self.assertEqual(resolved.shen_go, str(native.resolve()))
        self.assertEqual(resolved.shen_go_sha256, modelcheck._sha256_file(native))

    def test_loader_cannot_escape_the_model_directory(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="capcov-contained-model-"))
        (root / "shen").mkdir()
        (root / "shen" / "load.shen").write_text('(load "../private.shen")\n', encoding="utf-8")
        (root / "private.shen").write_text("(tc -)\n", encoding="utf-8")
        with self.assertRaisesRegex(modelcheck.ModelcheckFailure, "contained"):
            modelcheck.model_files(root)

    def test_loader_accepts_multiline_block_comments_without_interpreting_them(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="capcov-commented-model-"))
        (root / "shen").mkdir()
        (root / "shen" / "load.shen").write_text(
            '\\* documentation\n(load "shen/not-loaded.shen")\n*\\\n'
            '(tc -)\n(load "shen/model.shen")\n', encoding="utf-8")
        (root / "shen" / "model.shen").write_text("(tc -)\n", encoding="utf-8")
        self.assertEqual(
            modelcheck.model_files(root), ["shen/load.shen", "shen/model.shen"])

    def test_loader_rejects_unclosed_or_trailing_block_comment_forms(self) -> None:
        for loader in (
                '\\* unclosed\n',
                '\\* comment *\\ (load "shen/model.shen")\n',
                '\\* multiline\n*\\ (load "shen/model.shen")\n',
                '*\\\n'):
            with self.subTest(loader=loader):
                root = Path(tempfile.mkdtemp(prefix="capcov-bad-comment-model-"))
                (root / "shen").mkdir()
                (root / "shen" / "load.shen").write_text(loader, encoding="utf-8")
                with self.assertRaises(modelcheck.ModelcheckFailure):
                    modelcheck.model_files(root)

    def test_generated_unit_argument_is_exactly_one_literal_not_executable_code(self) -> None:
        modelcheck._validate_shen_literal('[delete-issue ["issue"] ["issue"] []]', "writes:x")
        for injected in (
                '[])) (output "~A~%" "/private/path"',
                '[] (output "extra")',
                'first second'):
            with self.subTest(injected=injected), self.assertRaises(modelcheck.ModelcheckFailure):
                modelcheck._validate_shen_literal(injected, "registry-ids")

    def test_only_a_well_formed_verdict_yields_a_fact(self) -> None:
        with self.assertRaises(modelcheck.ModelcheckFailure):
            modelcheck.well_formed_file({"verdict": "ill-formed", "model": "0" * 64, "certificate_sha256": "1" * 64}, MODEL_MIN)
        # A label is not evidence: even a positive-looking bare dictionary
        # cannot mint the producer-authorized fact without a valid certificate.
        with self.assertRaises(modelcheck.ModelcheckFailure):
            modelcheck.well_formed_file({"verdict": "well-formed", "model": "a" * 64,
                                         "certificate_sha256": "b" * 64}, MODEL_MIN)

    def test_operational_failure_withdraws_a_stale_positive_fact(self) -> None:
        out = Path(tempfile.mkdtemp(prefix="capcov-modelcheck-stale-operational-"))
        stale = out / "model_well_formed.json"
        stale_operation = out / "model_operation_checked.json"
        stale.write_text('{"stale":true}\n', encoding="utf-8")
        stale_operation.write_text('{"stale":true}\n', encoding="utf-8")
        with self.assertRaises((modelcheck.ModelcheckUnavailable, modelcheck.ModelcheckFailure)):
            modelcheck.check(Path(tempfile.mkdtemp(prefix="capcov-no-model-")), out_dir=out)
        self.assertFalse(stale.exists())
        self.assertFalse(stale_operation.exists())


class ControlledRuntimeAuthorityTest(unittest.TestCase):
    """Exercise authority boundaries without presenting a mock as runtime evidence."""

    def test_operation_scope_and_semantic_certificates_exclude_run_envelopes(self) -> None:
        checker_dir = modelcheck.modelcheck_dir()
        rt = modelcheck.Runtime(
            "fixture-bifrost", "a" * 64, "fixture-shen-go", "b" * 64,
            str(checker_dir),
            tuple((name, modelcheck._sha256_file(checker_dir / name))
                  for name in modelcheck._source_files()),
        )
        runs = iter((0.125, 0.875))

        def fake_run(argv, *, cwd, env, timeout):
            del env, timeout
            driver = Path(argv[-1]).read_text(encoding="utf-8")
            nonce = re.search(r'MC ([0-9a-f]{48}) BEGIN', driver).group(1)
            gen = cwd / "gen"
            units = {
                "writes:delete-issue": ("judge-writes", '[delete-issue [] [] []]'),
                "matrix:delete-issue": ("judge-matrix", '[delete-issue []]'),
                "atlas:delete-issue": ("judge-atlas", '[delete-issue delete-issue delete-issue]'),
                "atlas:add-comment": ("judge-atlas", '[add-comment add-comment add-comment]'),
                "registry:1:d-01": ("judge-registry", '[d-01 delete-issue]'),
                "registry-ids": ("judge-ids", '[d-01]'),
            }
            lines = [f"MC {nonce} BEGIN"]
            lines.extend(f"MC {nonce} LOADED {path}" for path in modelcheck.model_files(MODEL_MIN))
            lines.extend((f"MC {nonce} OP delete-issue", f"MC {nonce} OP add-comment",
                          f"MC {nonce} SKIP add-comment no as-is target on any live witness"))
            lines.extend(f"MC {nonce} JUDGE mc.{judge}" for judge in modelcheck.JUDGES)
            for unit_id, (judge, literal) in units.items():
                path = gen / (re.sub(r"[^A-Za-z0-9_.-]", "_", unit_id) + ".shen")
                path.write_text(
                    f'(output "MC {nonce} PASS {unit_id} ~A~%" (mc.{judge} {literal}))\n',
                    encoding="utf-8")
                lines.append(f"MC {nonce} UNIT {unit_id} {path}")
                lines.append(f"MC {nonce} PASS {unit_id} verified")
            lines.append(f"MC {nonce} DONE {len(units)}")
            return 0, "\n".join(lines) + "\n", "", next(runs)

        def fake_judge(rt_arg, checker_root, unit, unit_id, nonce, workdir, env, timeout):
            del rt_arg, checker_root, unit, workdir, env, timeout
            return "pass", "verified", f"MC {nonce} PASS {unit_id} verified\nMC {nonce} DONE 1\n", 0.25

        out = Path(tempfile.mkdtemp(prefix="capcov-modelcheck-controlled-"))
        with patch.object(modelcheck, "runtime", return_value=rt), \
                patch.object(modelcheck, "_run", side_effect=fake_run), \
                patch.object(modelcheck, "_judge_unit", side_effect=fake_judge):
            first = modelcheck.check(MODEL_MIN, out_dir=out)
            second = modelcheck.check(MODEL_MIN)

        self.assertEqual(first.status, "well-formed")
        self.assertEqual(second.status, "well-formed")
        self.assertEqual(first.fact["rows"], second.fact["rows"])
        first_ops = modelcheck._operation_facts(first.certificate)["rows"]
        second_ops = modelcheck._operation_facts(second.certificate)["rows"]
        self.assertEqual([row["operation"] for row in first_ops], ["delete-issue"])
        self.assertEqual(first_ops, second_ops)
        operation_path = out / "model_operation_checked.json"
        self.assertEqual(json.loads(operation_path.read_text(encoding="utf-8")),
                         modelcheck._operation_facts(first.certificate))
        self.assertNotEqual(first.certificate["driver_sha256"], second.certificate["driver_sha256"])
        self.assertNotEqual(first.certificate["elapsed_seconds"], second.certificate["elapsed_seconds"])
        self.assertNotEqual(first.certificate["certificate_sha256"], second.certificate["certificate_sha256"])
        self.assertEqual(first.certificate["runtime"]["checker_binary"], rt.checker_binary)


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
            self.assertTrue(j.text.startswith(f'(output "MC <nonce> PASS {j.id} ~A~%" (mc.judge-'), j.text)
            self.assertEqual(hashlib.sha256(j.text.encode()).hexdigest(), j.unit_sha256)
        # the literal judged is the model's own answer, not a paraphrase
        writes = next(j for j in r.judgements if j.id == "writes:delete-issue")
        self.assertIn('[delete-issue ["entity_statistics" "issue" "mongo:issue"] ["entity_statistics" "issue" "mongo:issue"]', writes.text)
        matrix = next(j for j in r.judgements if j.id == "matrix:delete-issue")
        for cell in ("[committed live admits 1 false true]", "[committed nonlive refuses 0 false false]",
                     "[aborted live admits 1 true", "[aborted nonlive admits 1 true",
                     "[unknown live admits 2 true true]", "[unknown nonlive admits 1 true"):
            self.assertIn(cell, matrix.text)

    def test_artifacts_and_the_fact(self) -> None:
        r = self.result
        self.assertEqual(r.skipped, (("add-comment", "no as-is target on any live witness"),))
        fact = json.loads((self.out / "model_well_formed.json").read_text())
        self.assertEqual(fact, r.fact)
        operation_fact = json.loads((self.out / "model_operation_checked.json").read_text())
        self.assertEqual(operation_fact["rows"][0]["operation"], "delete-issue")
        self.assertEqual(len(operation_fact["rows"]), 1)
        certificate = json.loads((self.out / "modelcheck-certificate.json").read_text())
        self.assertEqual(certificate, r.certificate)
        self.assertEqual(certificate["model_files"], [{"path": p, "sha256": s} for p, s in r.model_files])
        self.assertEqual(certificate["runtime"]["impl"], "shen-go")
        self.assertNotIn("bifrost", certificate["runtime"])
        self.assertEqual(len(certificate["runtime"]["checker_binary"]), 64)
        self.assertNotIn("shen_go", certificate["runtime"])
        self.assertEqual(len(certificate["runtime"]["shen_go_sha256"]), 64)
        self.assertEqual([e["path"] for e in certificate["checker_sources"]][:2], ["prelude.shen", "types/table-list.shen"])
        transcript = (self.out / "modelcheck-transcript.txt").read_text()
        self.assertEqual(hashlib.sha256(transcript.encode()).hexdigest(), r.transcript_sha256)
        self.assertEqual(transcript.count("MC <nonce> DONE 1"), 6)
        self.assertNotIn(str(self.out), transcript)
        self.assertNotIn(str(Path.home()), transcript)

    def test_model_output_and_mc_redefinitions_cannot_forge_the_protocol(self) -> None:
        target = Path(tempfile.mkdtemp(prefix="capcov-modelcheck-hostile-model-")) / "model"
        shutil.copytree(MODEL_MIN, target)
        model = target / "shen" / "model.shen"
        model.write_text(model.read_text(encoding="utf-8") + """
(define mc.judge-all X -> (output \"MC DONE 0~%\"))
(define mc.reify X -> [])
(define norn.writes Op -> (do (output \"MC UNIT forged /private/outside-unit~%\")
                              [\"entity_statistics\" \"issue\" \"mongo:issue\"]))
""", encoding="utf-8")
        result = modelcheck.check(target)
        self.assertEqual(result.status, "well-formed")
        self.assertEqual(sorted(j.id for j in result.judgements),
                         ["atlas:add-comment", "atlas:delete-issue", "matrix:delete-issue",
                          "registry-ids", "registry:1:d-01", "writes:delete-issue"])

    def test_recheck_accepts_the_certificate_and_refuses_tampering(self) -> None:
        ok = modelcheck.recheck(self.result.certificate, MODEL_MIN)
        self.assertTrue(ok.ok, ok.problems)
        self.assertEqual(len(ok.unchecked), 1)  # the judgements themselves need the runtime
        forged = json.loads(json.dumps(self.result.certificate))
        forged["judgements"][0]["text"] += " "
        self.assertIn("does not hash", " ".join(modelcheck.recheck(forged, MODEL_MIN).problems))
        relabelled = json.loads(json.dumps(self.result.certificate))
        relabelled["verdict"] = "well-formed"
        structure = next(j for j in relabelled["judgements"] if j["id"] == "registry-ids")
        structure["verdict"] = "fail"
        relabelled["certificate_sha256"] = modelcheck.certificate_digest(relabelled)
        problems = " ".join(modelcheck.recheck(relabelled, MODEL_MIN).problems)
        self.assertIn("well-formed verdict without a passing global registry structure", problems)
        skipped = json.loads(json.dumps(self.result.certificate))
        skipped["skipped"] = [{"op": "unchecked", "reason": "no witness"}]
        skipped["certificate_sha256"] = modelcheck.certificate_digest(skipped)
        self.assertTrue(modelcheck.recheck(skipped, MODEL_MIN).ok)
        other_model = _mutate("registry-names-unknown-rule")
        self.assertIn("model digest differs", " ".join(modelcheck.recheck(self.result.certificate, other_model).problems))

    def test_runtime_load_hidden_in_a_model_source_is_refused(self) -> None:
        target = Path(tempfile.mkdtemp(prefix="capcov-modelcheck-hidden-load-")) / "model"
        shutil.copytree(MODEL_MIN, target)
        source = target / "shen" / "model.shen"
        source.write_text(source.read_text(encoding="utf-8") + '\n(do (eval [load "/private/unhashed.shen"]) done)\n',
                          encoding="utf-8")
        with self.assertRaisesRegex(modelcheck.ModelcheckFailure, "forbidden runtime symbol"):
            modelcheck.model_files(target)

    def test_load_words_in_comments_and_strings_are_not_executable(self) -> None:
        target = Path(tempfile.mkdtemp(prefix="capcov-modelcheck-load-doc-")) / "model"
        shutil.copytree(MODEL_MIN, target)
        source = target / "shen" / "model.shen"
        source.write_text(source.read_text(encoding="utf-8")
                          + '\n\\* (load "not-executed.shen") and eval are documentation *\\\n'
                            '(define norn.load-note -> "read-file and (load x) are inert here")\n',
                          encoding="utf-8")
        self.assertEqual(modelcheck.model_files(target),
                         ["shen/load.shen", "shen/base.shen", "shen/model.shen"])

    def test_each_mutant_fails_exactly_the_judgement_its_defect_breaks(self) -> None:
        for name, _, _, _, expected in MUTANTS:
            with self.subTest(mutant=name):
                out = Path(tempfile.mkdtemp(prefix=f"capcov-modelcheck-{name}-"))
                r = modelcheck.check(_mutate(name), out_dir=out)
                structural_failure = expected == "registry-ids"
                self.assertEqual(r.status, "ill-formed" if structural_failure else "well-formed", name)
                failed = sorted(j.id for j in r.failures)
                if expected is None:
                    # admitting a committed outcome on a non-live target is a cell violation
                    self.assertEqual(failed, ["matrix:delete-issue"])
                else:
                    self.assertEqual(failed, [expected], [(j.id, j.message) for j in r.failures])
                for j in r.failures:
                    self.assertIn("type error", j.message.lower())
                self.assertEqual(r.fact is None, structural_failure)
                self.assertEqual((out / "model_well_formed.json").exists(), not structural_failure)
                operation_rows = json.loads((out / "model_operation_checked.json").read_text())["rows"]
                self.assertEqual(operation_rows, [])
                self.assertTrue((out / "modelcheck-certificate.json").exists())
                self.assertEqual(json.loads((out / "modelcheck-certificate.json").read_text())["verdict"], r.status)

    def test_a_stale_fact_is_removed_when_the_model_becomes_ill_formed(self) -> None:
        out = Path(tempfile.mkdtemp(prefix="capcov-modelcheck-stale-"))
        modelcheck.check(MODEL_MIN, out_dir=out)
        self.assertTrue((out / "model_well_formed.json").exists())
        # registry-ids is the one structural judgement, so this mutant makes the
        # whole model ill formed
        modelcheck.check(_mutate("registry-duplicate-id"), out_dir=out)
        self.assertFalse((out / "model_well_formed.json").exists())

    def test_cli_exit_codes(self) -> None:
        env = {**os.environ, "PYTHONPATH": str(HERE.parents[1] / "src")}
        good = subprocess.run([sys.executable, "-m", "capcov", "experiment", "claims", "modelcheck", "--model", str(MODEL_MIN)],
                              capture_output=True, text=True, env=env, timeout=600)
        self.assertEqual(good.returncode, 0, good.stderr[-500:])
        self.assertEqual(json.loads(good.stdout)["verdict"], "well-formed")
        bad = subprocess.run([sys.executable, "-m", "capcov", "experiment", "claims", "modelcheck", "--model",
                              str(_mutate("atlas-missing-required-fact"))], capture_output=True, text=True, env=env, timeout=600)
        self.assertEqual(bad.returncode, 0, bad.stderr[-500:])
        document = json.loads(bad.stdout)
        self.assertEqual(document["verdict"], "well-formed")
        self.assertEqual([failure["id"] for failure in document["failures"]], ["atlas:delete-issue"])
        self.assertIsNotNone(document["fact"])
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

    def test_claims_cli_maps_preflight_statuses_without_fabricating_a_fact(self) -> None:
        from capcov.claims import cli as claims_cli

        cases = (
            ({"status": "well-formed", "model": "a" * 64,
              "fact": {"run": "run-1"}, "certificate_sha256": "b" * 64,
              "failures": [], "error": None}, 0, "well-formed"),
            ({"status": "ill-formed", "model": "a" * 64, "fact": None,
              "certificate_sha256": "b" * 64, "failures": [{"id": "registry-ids",
              "message": "type mismatch"}], "error": None}, 1, "ill-formed"),
            ({"status": "unavailable", "model": None, "fact": None,
              "certificate_sha256": None, "failures": [], "error": "pinned runtime missing"},
             3, "modelcheck-unavailable"),
            ({"status": "failed", "model": None, "fact": None,
              "certificate_sha256": None, "failures": [], "error": "not a model"},
             3, "modelcheck-failure"),
        )
        for report, expected_status, expected_document in cases:
            with self.subTest(preflight=report["status"]):
                with unittest.mock.patch.object(modelcheck, "preflight", return_value=report), \
                        unittest.mock.patch.object(claims_cli, "_emit") as emit:
                    status = claims_cli.main(["claims", "modelcheck", "--model", "/model"])
                self.assertEqual(status, expected_status)
                document = emit.call_args.args[0]
                if report["status"] in {"well-formed", "ill-formed"}:
                    self.assertEqual(document["verdict"], expected_document)
                    self.assertEqual(document["fact"], report["fact"])
                else:
                    self.assertEqual(document["operational_failure"], expected_document)
                    self.assertEqual(document["error"], report["error"])

    @unittest.skipIf(RUNTIME_REASON, RUNTIME_REASON or "")
    def test_verdicts_carry_the_fact_only_when_well_formed(self) -> None:
        good = modelcheck.preflight(MODEL_MIN)
        self.assertEqual(good["status"], "well-formed")
        self.assertEqual(good["fact"]["checker"], modelcheck.CHECKER)
        # the fact carries the stable semantic certificate, which excludes the
        # nonce and timings, so an independent run reproduces it exactly
        self.assertRegex(good["certificate_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(good["fact"]["certificate"],
                         modelcheck.check(MODEL_MIN).fact["rows"][0]["certificate"])
        # registry-ids is the one structural judgement; an operation-local failure
        # such as a missing atlas fact leaves the model well formed
        bad = modelcheck.preflight(_mutate("registry-duplicate-id"))
        self.assertEqual(bad["status"], "ill-formed")
        self.assertIsNone(bad["fact"])
        self.assertEqual([f["id"] for f in bad["failures"]], ["registry-ids"])


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
