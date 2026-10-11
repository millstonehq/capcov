"""CLI surface added for the FODA integration: tree-printing ``validate`` and the
``coverage`` verb that wires the numeric completeness vector (coverage.py::rollup).

The Build's CLI exposed ``example``/``validate``/``check``/``rollup`` (the last being
the BINARY model.py::coverage_rollup). coverage.py::rollup -- the numeric vector that
consumes a per-feature obligations map ``{id: {covered, total}}`` -- had no CLI entry
point; the ``coverage`` verb is that entry point. These tests pin both additions.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from capcov.features.cli import main
from capcov.features.model import example


def _run(argv: list[str]) -> tuple[int, str]:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = main(argv)
    return code, buffer.getvalue()


class ValidatePrintsTreeTests(unittest.TestCase):
    def test_validate_prints_the_annotated_feature_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.json"
            path.write_text(json.dumps(example()))
            code, out = _run(["validate", str(path)])
        self.assertEqual(code, 0)
        # The tree, not just the one-line verdict: every feature id, indented, with
        # its facet annotation.
        for fid in ("auth", "password", "mfa", "secondFactor", "sms", "passkey"):
            self.assertIn(f"[{fid}]", out)
        self.assertIn("(root)", out)
        self.assertIn("(mandatory)", out)
        self.assertIn("alternative group", out)
        self.assertIn("group member", out)


class CoverageVerbTests(unittest.TestCase):
    def _write(self, directory: str, model: dict, obligations: dict) -> tuple[str, str]:
        m = Path(directory) / "model.json"
        o = Path(directory) / "obligations.json"
        m.write_text(json.dumps(model))
        o.write_text(json.dumps(obligations))
        return str(m), str(o)

    def test_coverage_prints_the_completeness_vector(self) -> None:
        obligations = {
            "auth": {"covered": 1, "total": 1},
            "password": {"covered": 2, "total": 2},
            "mfa": {"covered": 3, "total": 3},
        }
        with tempfile.TemporaryDirectory() as directory:
            m, o = self._write(directory, example(), obligations)
            code, out = _run(["coverage", m, o])
        self.assertEqual(code, 0)
        # Vector, not a single number: the four denominators and the per-feature rows.
        self.assertIn("mandatory", out)
        self.assertIn("optional", out)
        self.assertIn("unassessed", out)
        # selected=None -> mandatory skeleton assessed, every optional/group unassessed.
        self.assertIn("required", out)
        self.assertIn("unassessed", out)

    def test_coverage_honours_a_selection(self) -> None:
        obligations = {
            "auth": {"covered": 1, "total": 1},
            "password": {"covered": 1, "total": 1},
            "secondFactor": {"covered": 1, "total": 1},
            "passkey": {"covered": 1, "total": 1},
            "mfa": {"covered": 1, "total": 2},
        }
        with tempfile.TemporaryDirectory() as directory:
            m, o = self._write(directory, example(), obligations)
            code, out = _run(
                ["coverage", m, o, "--selected", "auth,password,secondFactor,passkey,mfa"]
            )
        self.assertEqual(code, 0)
        # The selected optional MFA lands in the optional bucket, deselected factors out.
        self.assertIn("selected", out)
        self.assertIn("deselected", out)

    def test_coverage_rejects_bad_obligation_counts(self) -> None:
        obligations = {"auth": {"covered": 5, "total": 2}}  # covered > total
        with tempfile.TemporaryDirectory() as directory:
            m, o = self._write(directory, example(), obligations)
            code, out = _run(["coverage", m, o])
        self.assertEqual(code, 1)
        self.assertIn("0 <= covered <= total", out)

    def test_coverage_rejects_obligations_for_unknown_features(self) -> None:
        obligations = {"ghost": {"covered": 1, "total": 1}}
        with tempfile.TemporaryDirectory() as directory:
            m, o = self._write(directory, example(), obligations)
            code, out = _run(["coverage", m, o])
        self.assertEqual(code, 1)
        self.assertIn("unknown features", out)


class ShippedExampleTests(unittest.TestCase):
    """The shipped example JSON + obligations demo compose end-to-end through the CLI."""

    ROOT = Path(__file__).resolve().parents[1]

    def test_shipped_model_validates(self) -> None:
        model = self.ROOT / "examples" / "feature-model-authentication.json"
        code, out = _run(["validate", str(model)])
        self.assertEqual(code, 0, out)

    def test_shipped_obligations_roll_up(self) -> None:
        model = self.ROOT / "examples" / "feature-model-authentication.json"
        obligations = self.ROOT / "examples" / "feature-obligations-authentication.json"
        code, out = _run(["coverage", str(model), str(obligations)])
        self.assertEqual(code, 0, out)
        self.assertIn("mandatory", out)


if __name__ == "__main__":
    unittest.main()
