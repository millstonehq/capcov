"""`features report`: the completeness vector as a table people can paste.

A Harvey ball is a rolled (covered, total) pair drawn as a glyph. The glyph is
computed from the ratio and NEVER from a bare percentage across the tree: a
feature with total 0 renders as unassessed ('?'), not as full.
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from capcov.features.cli import main
from capcov.features.coverage import rollup
from capcov.features.model import example
from capcov.features.report import glyph, render


OBL = {"auth": {"covered": 1, "total": 1}, "password": {"covered": 2, "total": 4},
       "mfa": {"covered": 0, "total": 3}}


class GlyphTests(unittest.TestCase):
    def test_glyphs_follow_quarters(self) -> None:
        self.assertEqual(glyph(0, 0), "?")
        self.assertEqual(glyph(0, 4), "○")
        self.assertEqual(glyph(1, 4), "◔")
        self.assertEqual(glyph(2, 4), "◑")
        self.assertEqual(glyph(3, 4), "◕")
        self.assertEqual(glyph(4, 4), "●")
        self.assertEqual(glyph(1, 3), "◔")
        self.assertEqual(glyph(2, 3), "◕")

    def test_glyphs_break_ties_consistently(self) -> None:
        self.assertEqual(glyph(5, 8), "◕")
        self.assertEqual(glyph(3, 8), "◑")


class RenderTests(unittest.TestCase):
    def test_markdown_has_one_row_per_feature_with_glyph_and_counts(self) -> None:
        text = render(rollup(example(), OBL, selected={"mfa"}), fmt="md")
        self.assertIn("| Authentication |", text)
        self.assertIn("| ◑ | 2/4 |", text)
        self.assertIn("| ○ | 0/3 |", text)
        self.assertIn("unassessed", text)

    def test_indentation_uses_non_breaking_spaces_that_survive_markdown_paste(self) -> None:
        # Second Factor is a grandchild of the root (Authentication -> Password ->
        # Second Factor), depth 2. A Markdown renderer collapses ordinary leading
        # spaces inside a table cell, which flattens the tree on paste; U+00A0
        # does not collapse.
        text = render(rollup(example(), OBL, selected={"mfa"}), fmt="md")
        self.assertIn("|     Second Factor |", text)

    def test_names_are_escaped_for_the_markdown_table(self) -> None:
        model = example()
        for feature in model["features"]:
            if feature["id"] == "password":
                feature["name"] = "Pass|word\nLine2"
        text = render(rollup(model, OBL), fmt="md")
        self.assertIn("Pass\\|word Line2", text)
        self.assertNotIn("Pass|word\nLine2", text)

    def test_unassessed_rows_show_question_glyph_not_the_numeric_ratio(self) -> None:
        text = render(rollup(example(), OBL), fmt="md")
        self.assertIn("| unassessed | ? | 0/3 |", text)

    def test_deselected_rows_show_question_glyph_regardless_of_self_coverage(self) -> None:
        obligations = {**OBL, "sms": {"covered": 1, "total": 2}}
        text = render(rollup(example(), obligations, selected={"mfa"}), fmt="md")
        self.assertIn("| deselected | ? | 1/2 |", text)

    def test_csv_rows_are_parseable(self) -> None:
        text = render(rollup(example(), OBL), fmt="csv")
        rows = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual(rows[0]["feature"], "auth")
        self.assertEqual({"feature", "name", "parent", "kind", "status", "self_covered",
                          "self_total", "covered", "total", "glyph"}, set(rows[0]))

    def test_csv_root_row_parent_is_empty_string_not_none(self) -> None:
        text = render(rollup(example(), OBL), fmt="csv")
        rows = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual(rows[0]["feature"], "auth")
        self.assertEqual(rows[0]["parent"], "")

    def test_unknown_format_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            render(rollup(example(), OBL), fmt="xlsx")


class ReportVerbTests(unittest.TestCase):
    def test_report_verb_writes_csv_and_reports_the_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory)
            (d / "model.json").write_text(json.dumps(example()))
            (d / "obligations.json").write_text(json.dumps(OBL))
            out = d / "report.csv"
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = main([
                    "report", str(d / "model.json"), str(d / "obligations.json"),
                    "--format", "csv", "--out", str(out),
                ])
            self.assertEqual(code, 0, buffer.getvalue())
            self.assertIn(f"capcov features: wrote {out}", buffer.getvalue())
            self.assertTrue(out.exists())
            with out.open() as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(rows[0]["feature"], "auth")
