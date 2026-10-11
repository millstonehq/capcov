"""``capcov experiment claims assumptions registry|invalidate`` over the synthetic receipt.

The receipt comes from ``--receipt`` or ``CAPCOV_REPLAY_RECEIPT_DIR``; with
neither the command refuses (exit 2) rather than judging a default.  souffle is
a precondition: the join evaluates in both kernels.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from capcov.claims import cli
from capcov.claims.replay import join as replay_join

RECEIPT = Path(__file__).resolve().parent / "fixtures" / "replay_receipt_min"


def _run(*argv: str, env: dict[str, str] | None = None) -> tuple[int, str]:
    buffer = io.StringIO()
    environ = {k: v for k, v in os.environ.items() if k != cli.RECEIPT_DIR_ENV}
    environ.update(env or {})
    with patch.dict(os.environ, environ, clear=True), contextlib.redirect_stdout(buffer):
        code = cli.main(list(argv))
    return code, buffer.getvalue()


def _owned_replay_roots() -> set[Path]:
    return set(Path(tempfile.gettempdir()).glob("capcov-assumptions-*"))


class AssumptionsCommandLineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(shutil.which("souffle"), "souffle must be on PATH: run inside the nix devShell")

    def test_registry_prints_the_wrapper_and_writes_the_artifacts(self) -> None:
        out = Path(tempfile.mkdtemp(prefix="capcov-assumption-cli-"))
        before = _owned_replay_roots()
        try:
            code, text = _run("claims", "assumptions", "registry", "--receipt", str(RECEIPT), "--out", str(out))
            self.assertEqual(code, 0, text[:2000])
            document = json.loads(text)
            self.assertEqual(set(document), {"registry"})
            self.assertTrue(document["registry"]["assumptions"])
            self.assertTrue((out / "assumptions.json").is_file())
            self.assertTrue((out / "receipt.json").is_file())
            self.assertEqual(_owned_replay_roots() - before, set(),
                             "a replay root the CLI made itself is removed when nothing needs it")
        finally:
            shutil.rmtree(out, ignore_errors=True)

    def test_the_environment_names_the_receipt_when_the_flag_does_not(self) -> None:
        flag, flag_text = _run("claims", "assumptions", "registry", "--receipt", str(RECEIPT))
        env, env_text = _run("claims", "assumptions", "registry", env={cli.RECEIPT_DIR_ENV: str(RECEIPT)})
        self.assertEqual((flag, env), (0, 0), env_text[:2000])
        self.assertEqual(json.loads(flag_text), json.loads(env_text))

    def test_no_receipt_is_a_refusal(self) -> None:
        code, text = _run("claims", "assumptions", "registry")
        self.assertEqual(code, 2)
        self.assertIn("no receipt directory", json.loads(text)["refusal"])

    def test_missing_receipt_is_a_refusal(self) -> None:
        code, text = _run("claims", "assumptions", "registry", "--receipt", "/nonexistent-receipt-dir")
        self.assertEqual(code, 2)
        self.assertIn("no receipt directory", json.loads(text)["refusal"])

    def test_unknown_drop_is_a_refusal(self) -> None:
        code, text = _run("claims", "assumptions", "invalidate",
                          "--receipt", str(RECEIPT), "--drop", "asm:" + "0" * 64)
        self.assertEqual(code, 2)
        self.assertIn("not an assumption", json.loads(text)["refusal"])

    def test_a_kernel_disagreement_on_the_claims_is_exit_3(self) -> None:
        """``certify_claims`` raises ``AssertionError``; the contract calls that 3, not a traceback."""
        code, text = _run("claims", "assumptions", "registry", "--receipt", str(RECEIPT))
        self.assertEqual(code, 0, text[:2000])
        drop = json.loads(text)["registry"]["assumptions"][0]["assumption_id"]

        def raising(join, identifier, replay_root):
            raise AssertionError(f"{identifier}: certificates differ between closures")

        before = _owned_replay_roots()
        with patch.object(replay_join, "invalidate", raising):
            code, text = _run("claims", "assumptions", "invalidate", "--receipt", str(RECEIPT), "--drop", drop)
        self.assertEqual(code, 3, text[:2000])
        document = json.loads(text)
        self.assertIn("disagree", document["kernel_mismatch"])
        self.assertIn("certificates differ", document["error"])
        kept = _owned_replay_roots() - before
        self.assertEqual({str(path) for path in kept}, {document["replay"]},
                         "a mismatch keeps its replay root: the evidence is in it")
        for path in kept:
            shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
