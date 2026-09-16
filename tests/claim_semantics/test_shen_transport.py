"""Stage D transport: named operational failures of the Shen adapter.

The timeout, non-zero exit and malformed-output paths are exercised by
pointing ``BIFROST_SHEN_GO`` at small fake launcher scripts; bifrost itself is
real.  The translation layer is checked without any runtime.  Nothing here
substitutes a fake for the semantic answer: a fake runtime can only produce a
named failure, never a certificate.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capcov.claims import bundle_from_json
from capcov.claims import shen

try:
    from .static_rules.adapter import pack_bundle
except ImportError:  # unittest discover -s imports this directory as top-level
    from static_rules.adapter import pack_bundle

RUNTIME_PRESENT = bool(shutil.which("bifrost") and os.environ.get("BIFROST_SHEN_GO"))
SKIP_REASON = "bifrost on PATH and BIFROST_SHEN_GO are required; the Shen runtime is never mocked"
if os.environ.get("CAPCOV_SHEN_REQUIRED") and not RUNTIME_PRESENT:
    # fail-closed mode for the manifest runner: a missing runtime is an error, not a skip
    raise RuntimeError("CAPCOV_SHEN_REQUIRED is set but " + SKIP_REASON)


def _fake_launcher(directory: Path, body: str) -> str:
    path = directory / "fake-shen-go"
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


class TranslationTest(unittest.TestCase):
    def test_strings_with_quotes_and_nested_values_are_transportable(self) -> None:
        self.assertEqual(shen._string("plain"), '"plain"')
        self.assertEqual(shen._string('say "hi"'), '(capcov.dq ["say " "hi" ""])')
        self.assertEqual(shen._literal({"b": [1, True, None], "a": "x"}),
                         '(capcov.obj [(@p "a" "x") (@p "b" (capcov.arr [1 true capcov.null]))])')
        with self.assertRaises(shen.ShenFailure) as ctx:
            shen._literal(1.5)
        self.assertEqual(ctx.exception.kind, "invalid-input")
        with self.assertRaises(shen.ShenFailure):
            shen._literal(2 ** 60)

    def test_pack_checksum_matches_the_shen_recurrence_on_a_known_vector(self) -> None:
        # a = (4a + k + 1) mod 2^31-1, b = (3b + k + 1) mod 2^31-19, from (7, 11)
        a, b = 7, 11
        for k in b"ab":
            a = (4 * a + k + 1) % 2147483647
            b = (3 * b + k + 1) % 2147483629
        self.assertEqual(shen.pack_checksum("ab"), f"ck2-{a}-{b}")

    def test_driver_is_deterministic_and_names_every_input(self) -> None:
        bundle = pack_bundle()
        kwargs = dict(frozen=None, max_depth=64, max_nodes=10000, directory=Path("/lib"),
                      bundle_digest="b" * 64, rules_digest_value="r" * 64)
        first = shen.render_driver(bundle, {"kind": "authority"}, **kwargs)
        second = shen.render_driver(bundle, {"kind": "authority"}, **kwargs)
        self.assertEqual(first, second)
        for name in shen.LIBRARY_FILES:
            self.assertIn(f'(load "/lib/{name}")', first)
        self.assertIn("(do (set capcov.*in-request* [capcov.authority]) ok)", first)
        self.assertTrue(first.rstrip().endswith("(capcov.main)"))

    def test_marker_extraction_ignores_echoed_literals(self) -> None:
        stdout = ('"<<<CAPCOV-SHEN-JSON-BEGIN>>>"\n(fn x)\n\n<<<CAPCOV-SHEN-JSON-BEGIN>>>\n'
                  '{"kind":"why-not","report":{}}\n<<<CAPCOV-SHEN-JSON-END>>>\n')
        self.assertEqual(shen._extract(stdout)["kind"], "why-not")
        with self.assertRaises(shen.ShenFailure) as ctx:
            shen._extract("garbage without markers")
        self.assertEqual(ctx.exception.kind, "malformed-output")
        with self.assertRaises(shen.ShenFailure) as ctx:
            shen._extract("<<<CAPCOV-SHEN-JSON-BEGIN>>>\nnot json\n<<<CAPCOV-SHEN-JSON-END>>>\n")
        self.assertEqual(ctx.exception.kind, "malformed-output")

    def test_missing_runtime_is_shen_unavailable(self) -> None:
        with mock.patch.dict(os.environ, {"BIFROST_SHEN_GO": ""}):
            with self.assertRaises(shen.ShenUnavailable):
                shen.runtime()
        with mock.patch.dict(os.environ, {"BIFROST_SHEN_GO": "/nonexistent/shen-go"}):
            with self.assertRaises(shen.ShenUnavailable):
                shen.runtime()
        with mock.patch("capcov.claims.shen.shutil.which", return_value=None):
            with self.assertRaises(shen.ShenUnavailable):
                shen.runtime()


@unittest.skipUnless(RUNTIME_PRESENT, SKIP_REASON)
class FakeLauncherFailureTest(unittest.TestCase):
    """bifrost is real; BIFROST_SHEN_GO points at a script that misbehaves."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="capcov-shen-fake-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bundle = pack_bundle()

    def _with_fake(self, body: str):
        return mock.patch.dict(os.environ, {"BIFROST_SHEN_GO": _fake_launcher(self.tmp, body)})

    def test_timeout_is_a_named_failure_and_the_process_is_killed(self) -> None:
        with self._with_fake("sleep 30"):
            with self.assertRaises(shen.ShenFailure) as ctx:
                shen.authority(self.bundle, timeout=2)
        self.assertEqual(ctx.exception.kind, "timeout")
        self.assertIsNotNone(ctx.exception.provenance)
        self.assertLess(ctx.exception.provenance.elapsed_seconds, 15)

    def test_malformed_output_is_a_named_failure(self) -> None:
        with self._with_fake('echo "not a result block"'):
            with self.assertRaises(shen.ShenFailure) as ctx:
                shen.authority(self.bundle)
        self.assertEqual(ctx.exception.kind, "malformed-output")
        with self._with_fake('echo "<<<CAPCOV-SHEN-JSON-BEGIN>>>"; echo "{oops"; echo "<<<CAPCOV-SHEN-JSON-END>>>"'):
            with self.assertRaises(shen.ShenFailure) as ctx:
                shen.authority(self.bundle)
        self.assertEqual(ctx.exception.kind, "malformed-output")
        with self._with_fake('echo "<<<CAPCOV-SHEN-JSON-BEGIN>>>"; echo "{\\"kind\\":\\"derive\\"}"; '
                             'echo "<<<CAPCOV-SHEN-JSON-END>>>"'):
            with self.assertRaises(shen.ShenFailure) as ctx:
                shen.authority(self.bundle)
        self.assertEqual(ctx.exception.kind, "malformed-output")

    def test_nonzero_exit_is_a_named_failure(self) -> None:
        with self._with_fake('echo "boom" 1>&2; exit 7'):
            with self.assertRaises(shen.ShenFailure) as ctx:
                shen.authority(self.bundle)
        self.assertEqual(ctx.exception.kind, "runtime-exit")
        self.assertEqual(ctx.exception.returncode, 7)
        # bifrost folds the launcher's stderr into its own stdout
        self.assertIn("boom", ctx.exception.stdout + ctx.exception.stderr)

    def test_shen_side_error_payloads_are_classified(self) -> None:
        payload = json.dumps({"kind": "error", "error": "capcov-unsupported-construct: planted"})
        with self._with_fake(f"echo '<<<CAPCOV-SHEN-JSON-BEGIN>>>'; echo '{payload}'; echo '<<<CAPCOV-SHEN-JSON-END>>>'"):
            with self.assertRaises(shen.ShenFailure) as ctx:
                shen.authority(self.bundle)
        self.assertEqual(ctx.exception.kind, "unsupported-construct")

    def test_elaborated_pack_checksum_disagreement_is_a_named_failure(self) -> None:
        payload = json.dumps({"kind": "authority", "ok": True, "rules": [], "pack": [],
                              "elaborated": {"rules": []}, "elaborated_checksum": "ck2-0-0"})
        with self._with_fake(f"echo '<<<CAPCOV-SHEN-JSON-BEGIN>>>'; echo '{payload}'; echo '<<<CAPCOV-SHEN-JSON-END>>>'"):
            with self.assertRaises(shen.ShenFailure) as ctx:
                shen.authority(self.bundle)
        self.assertEqual(ctx.exception.kind, "elaborated-pack-mismatch")


@unittest.skipUnless(RUNTIME_PRESENT, SKIP_REASON)
class ExperimentCliTest(unittest.TestCase):
    def test_experiment_namespace_dispatches_and_reports_json(self) -> None:
        import contextlib
        import io
        from capcov import cli
        from capcov.claims import canonical_json
        tmp = Path(tempfile.mkdtemp(prefix="capcov-shen-cli-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        bundle_path = tmp / "pack.json"
        bundle_path.write_text(canonical_json(pack_bundle()), encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["experiment", "claims", "shen", "authority", "--bundle", str(bundle_path),
                           "--out", str(tmp / "authority.json")])
        self.assertEqual(rc, 0)
        document = json.loads((tmp / "authority.json").read_text(encoding="utf-8"))
        self.assertTrue(document["ok"])
        self.assertEqual(document["provenance"]["hashes"]["runtime_binary_sha256"],
                         shen.runtime().shen_go_sha256)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with mock.patch.dict(os.environ, {"BIFROST_SHEN_GO": "/nonexistent"}):
                rc = cli.main(["experiment", "claims", "shen", "authority", "--bundle", str(bundle_path)])
        self.assertEqual(rc, 3)
        self.assertEqual(json.loads(out.getvalue())["operational_failure"], "shen-unavailable")


if __name__ == "__main__":
    unittest.main()
