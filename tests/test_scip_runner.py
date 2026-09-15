"""Tests for the SCIP runner + reader.

The normalization suite runs entirely against a checked-in `scip print --json`
sample (tests/fixtures/scip_print_sample.json). It needs no network and no scip
tools: normalization is a pure function over the CLI's JSON, and the fixture is
the contract. Tests that actually shell out to an indexer or the scip CLI are
isolated in LiveToolTest and skip when the tool is absent.
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from capcov.scip import runner

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "scip_print_sample.json"

_PY = "scip-python python spike 0.0.1 app/models/"
_GO = "scip-go gomod github.com/example/app v0.0.0 `app`/"


class NormalizationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = json.loads(FIXTURE.read_text())
        self.out = runner.normalize_scip_json(self.raw)
        self.docs = {d["path"]: d for d in self.out["documents"]}

    def test_documents_are_keyed_by_relative_path(self) -> None:
        self.assertEqual(set(self.docs), {"app/models/job.py", "app/server.go"})

    def test_python_kind_is_derived_from_the_symbol_suffix(self) -> None:
        """scip-python leaves kind null; the descriptor suffix (`#`, `().`, `.`)
        is the only signal for whether a symbol is a type, a method or a term."""
        kinds = {
            s["display_name"]: s["kind"]
            for s in self.docs["app/models/job.py"]["symbols"]
        }
        self.assertEqual(kinds, {"Job": "type", "save": "method", "status": "term"})

    def test_go_populated_kind_is_preserved(self) -> None:
        """scip-go fills kind in; a populated kind must survive untouched rather
        than be second-guessed from the suffix."""
        kinds = {
            s["display_name"]: s["kind"]
            for s in self.docs["app/server.go"]["symbols"]
        }
        self.assertEqual(
            kinds, {"Server": "Struct", "Addr": "Field", "Start": "Function"}
        )

    def test_python_occurrences_are_fully_normalized(self) -> None:
        self.assertEqual(
            self.docs["app/models/job.py"]["occurrences"],
            [
                {
                    "symbol": _PY + "Job#",
                    "is_definition": True,
                    "start_line": 10,
                    "start_col": 6,
                    "enclosing_start_line": 10,
                    "enclosing_end_line": 40,
                },
                {
                    "symbol": _PY + "Job#",
                    "is_definition": False,
                    "start_line": 55,
                    "start_col": 4,
                    "enclosing_start_line": None,
                    "enclosing_end_line": None,
                },
                {
                    "symbol": _PY + "Job#save().",
                    "is_definition": True,
                    "start_line": 15,
                    "start_col": 8,
                    "enclosing_start_line": 15,
                    "enclosing_end_line": 22,
                },
            ],
        )

    def test_definition_bit_marks_definitions_and_references(self) -> None:
        occs = self.docs["app/models/job.py"]["occurrences"]
        self.assertEqual(
            [o["is_definition"] for o in occs], [True, False, True]
        )

    def test_missing_symbol_roles_is_not_a_definition(self) -> None:
        """The go reference occurrence carries no symbol_roles key at all; a
        missing role must default to 0, i.e. not a definition."""
        ref = self.docs["app/server.go"]["occurrences"][-1]
        self.assertFalse(ref["is_definition"])
        self.assertEqual(ref["symbol"], _GO + "Server#")

    def test_single_line_three_element_range(self) -> None:
        """A [line, startCol, endCol] range (same line) yields the start line/col
        exactly like a four-element range does."""
        start = self.docs["app/server.go"]["occurrences"][1]  # Start(), range [20,5,10]
        self.assertEqual((start["start_line"], start["start_col"]), (20, 5))
        self.assertEqual(
            (start["enclosing_start_line"], start["enclosing_end_line"]), (20, 30)
        )

    def test_definition_bit_is_masked_not_equality_checked(self) -> None:
        """A role value with additional bits set (e.g. 0x8 ReadAccess | 0x1
        Definition) is still a definition."""
        doc = {
            "documents": [
                {
                    "relative_path": "x.py",
                    "symbols": [],
                    "occurrences": [
                        {"symbol": "s", "range": [0, 0, 1], "symbol_roles": 9},
                        {"symbol": "s", "range": [1, 0, 1], "symbol_roles": 8},
                    ],
                }
            ]
        }
        occs = runner.normalize_scip_json(doc)["documents"][0]["occurrences"]
        self.assertEqual([o["is_definition"] for o in occs], [True, False])


class KindFromSuffixTest(unittest.TestCase):
    def test_suffix_grammar(self) -> None:
        self.assertEqual(runner._kind_from_suffix("pkg Thing#"), "type")
        self.assertEqual(runner._kind_from_suffix("pkg Thing#method()."), "method")
        self.assertEqual(runner._kind_from_suffix("pkg Thing#field."), "term")

    def test_unknown_suffix_is_none(self) -> None:
        self.assertIsNone(runner._kind_from_suffix("pkg param(x)"))


class RunIndexTest(unittest.TestCase):
    def test_missing_python_indexer_names_the_tool(self) -> None:
        with patch("capcov.scip.runner.shutil.which", return_value=None):
            with self.assertRaises(runner.IndexerNotFound) as ctx:
                runner.run_scip_index("/some/dir", "python")
        self.assertIn("scip-python", str(ctx.exception))

    def test_missing_go_indexer_names_the_tool_and_the_moved_module(self) -> None:
        with patch("capcov.scip.runner.shutil.which", return_value=None):
            with self.assertRaises(runner.IndexerNotFound) as ctx:
                runner.run_scip_index("/some/dir", "go")
        message = str(ctx.exception)
        self.assertIn("scip-go", message)
        # the module moved off sourcegraph/; the install hint must reflect that.
        self.assertIn("scip-code/scip-go", message)

    def test_unknown_language_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            runner.run_scip_index("/some/dir", "ruby")

    def test_python_command_shape(self) -> None:
        self.assertEqual(
            runner._index_command("python", "index.scip"),
            [
                "scip-python", "index",
                "--project-name", "spike",
                "--project-version", "0.0.1",
                "--output", "index.scip",
                ".",
            ],
        )

    def test_go_command_shape(self) -> None:
        self.assertEqual(
            runner._index_command("go", "index.scip"),
            ["scip-go", "--output", "index.scip"],
        )

    def test_run_index_invokes_indexer_in_target_dir_and_returns_path(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            recorded = {}

            def fake_run(command, **kwargs):
                recorded["command"] = command
                recorded["cwd"] = kwargs.get("cwd")
                (Path(kwargs["cwd"]) / "index.scip").write_bytes(b"\x00")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with (
                patch("capcov.scip.runner.shutil.which", return_value="/usr/bin/scip-python"),
                patch("capcov.scip.runner.subprocess.run", fake_run),
            ):
                out = runner.run_scip_index(d, "python")

            self.assertEqual(out, Path(d) / "index.scip")
            self.assertEqual(recorded["cwd"], str(Path(d)))
            self.assertEqual(recorded["command"][0], "scip-python")

    def test_run_index_raises_when_indexer_writes_no_index(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            def fake_run(command, **kwargs):
                return SimpleNamespace(returncode=1, stdout="", stderr="boom")

            with (
                patch("capcov.scip.runner.shutil.which", return_value="/usr/bin/scip-python"),
                patch("capcov.scip.runner.subprocess.run", fake_run),
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    runner.run_scip_index(d, "python")
            self.assertIn("boom", str(ctx.exception))


class PhpInvocationTest(unittest.TestCase):
    """scip-php does not fit which(exe)+cwd+--output: it runs as
    ``php <script> --memory-limit=2G`` from the project root. The argv and env are
    asserted here WITHOUT the tool installed, and the two-part availability
    (php on PATH AND the standalone script present) is enforced."""

    def test_php_command_shape_is_php_script_memory_limit(self) -> None:
        with patch.dict(os.environ, {"SCIP_PHP_BIN": "/opt/scip-php"}):
            self.assertEqual(
                runner._index_command("php", "index.scip"),
                ["php", "/opt/scip-php", "--memory-limit=2G"],
            )

    def test_php_command_defaults_the_script_path(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                runner._index_command("php", "index.scip"),
                ["php", "/tmp/scip-tool/vendor/bin/scip-php", "--memory-limit=2G"],
            )

    def test_index_env_is_none_for_every_language(self) -> None:
        # The invocation strategy is (argv, env); php needs no env override.
        for language in ("python", "go", "php"):
            self.assertIsNone(runner._index_env(language))

    def test_missing_php_executable_names_the_tool_and_hint(self) -> None:
        with patch("capcov.scip.runner.shutil.which", return_value=None):
            with self.assertRaises(runner.IndexerNotFound) as ctx:
                runner.run_scip_index("/some/dir", "php")
        message = str(ctx.exception)
        self.assertIn("php", message)
        self.assertIn("scip-php", message)

    def test_php_on_path_but_missing_script_names_scip_php(self) -> None:
        with (
            patch("capcov.scip.runner.shutil.which", return_value="/usr/bin/php"),
            patch.dict(os.environ, {"SCIP_PHP_BIN": "/no/such/scip-php"}),
        ):
            with self.assertRaises(runner.IndexerNotFound) as ctx:
                runner.run_scip_index("/some/dir", "php")
        self.assertIn("scip-php", str(ctx.exception))
        self.assertIn("/no/such/scip-php", str(ctx.exception))


class NumericKindTest(unittest.TestCase):
    """The real `scip print --json` emits kinds as numeric SymbolKind values;
    the hand-authored fixture used name strings. A number must be mapped to its
    name (or fall back to the suffix) -- otherwise map._category calls .strip()
    on an int and the whole language crashes."""

    def test_numeric_kinds_map_to_their_enum_names(self) -> None:
        self.assertEqual(runner._coerce_kind(49, "pkg X#"), "Struct")
        self.assertEqual(runner._coerce_kind(26, "pkg X#m()."), "Method")
        self.assertEqual(runner._coerce_kind(17, "pkg f()."), "Function")
        self.assertEqual(runner._coerce_kind(15, "pkg X#f."), "Field")

    def test_unknown_number_falls_back_to_the_descriptor_suffix(self) -> None:
        self.assertEqual(runner._coerce_kind(9999, "pkg Thing#"), "type")
        self.assertIsNone(runner._coerce_kind(9999, "pkg param(x)"))

    def test_string_kind_and_unset_are_unchanged(self) -> None:
        self.assertEqual(runner._coerce_kind("Struct", "pkg X#"), "Struct")
        self.assertEqual(runner._coerce_kind(None, "pkg X#"), "type")
        self.assertEqual(runner._coerce_kind(0, "pkg X#m()."), "method")

    def test_real_go_dump_normalizes_without_crashing(self) -> None:
        raw = json.loads(
            (Path(__file__).resolve().parent / "fixtures"
             / "scip_go_nested_symbols.json").read_text()
        )
        out = runner.normalize_scip_json(raw)
        kinds = {
            s["kind"]
            for doc in out["documents"]
            for s in doc["symbols"]
        }
        # numeric kinds became names map._category understands.
        self.assertIn("Struct", kinds)
        self.assertIn("Method", kinds)
        # and the downstream mapper runs (it .strip()s the kind).
        from capcov.scip import map as scip_map

        self.assertTrue(scip_map.call_edges(out), "go call edges must resolve")


class PhpEnclosingSynthesisTest(unittest.TestCase):
    """scip-php emits no enclosing range on any occurrence, so a caller cannot be
    attributed and the whole PHP call graph is silently empty. The runner
    synthesizes a span for each callable definition; a dialect that supplies
    enclosing ranges is left untouched."""

    def test_callable_defs_get_a_span_non_callables_do_not(self) -> None:
        raw = json.loads(
            (Path(__file__).resolve().parent / "fixtures"
             / "scip_php_symbols.json").read_text()
        )
        out = runner.normalize_scip_json(raw)
        occ_by = {}
        for doc in out["documents"]:
            for o in doc["occurrences"]:
                occ_by.setdefault(o["symbol"], o)
        get_job = occ_by[
            "scip-php composer example/php-slice 1.0.0.0 "
            "App/Http/Controllers/JobController#getJob()."
        ]
        self.assertIsNotNone(get_job["enclosing_start_line"])
        self.assertIsNotNone(get_job["enclosing_end_line"])
        self.assertEqual(get_job["enclosing_start_line"], get_job["start_line"])
        # the class definition is not callable -> it must NOT be given a span, or
        # it would swallow calls that belong to its methods.
        klass = occ_by[
            "scip-php composer example/php-slice 1.0.0.0 "
            "App/Http/Controllers/JobController#"
        ]
        self.assertIsNone(klass["enclosing_start_line"])

    def test_a_dialect_with_enclosing_ranges_is_untouched(self) -> None:
        # The sample fixture (python+go) already carries enclosing ranges; the
        # synthesis must not overwrite them.
        out = runner.normalize_scip_json(json.loads(FIXTURE.read_text()))
        job = out["documents"][0]["occurrences"][0]  # Job# definition
        self.assertEqual(job["enclosing_start_line"], 10)
        self.assertEqual(job["enclosing_end_line"], 40)


# The keys retain=True adds and NOTHING else may differ from the default output.
_RETAINED_TOP = {"metadata", "external_symbols"}
_RETAINED_DOC = {"language", "enclosing_synthesized"}
_RETAINED_OCC = {"symbol_roles", "roles", "end_line", "end_col", "enclosing_synthesized"}
_RETAINED_SYM = {"relationships", "kind_number"}


def _strip_retained(retained: dict) -> dict:
    """The retained dict minus the retain-only keys, for the round-trip test."""
    out = {k: v for k, v in retained.items() if k not in _RETAINED_TOP}
    docs = []
    for doc in out["documents"]:
        d = {k: v for k, v in doc.items() if k not in _RETAINED_DOC}
        d["occurrences"] = [
            {k: v for k, v in o.items() if k not in _RETAINED_OCC} for o in d["occurrences"]
        ]
        d["symbols"] = [
            {k: v for k, v in s.items() if k not in _RETAINED_SYM} for s in d["symbols"]
        ]
        docs.append(d)
    out["documents"] = docs
    return out


class RetainModeTest(unittest.TestCase):
    """``retain=True`` is opt-in and additive: on every checked-in fixture the
    default output is byte-identical to before, and equals the retained output
    with the new keys removed. The retained keys carry the identity the static
    fact exporter (section 29) needs."""

    FIXTURES = (
        "scip_print_sample.json",
        "scip_go_nested_symbols.json",
        "scip_php_symbols.json",
    )

    def _raw(self, name: str) -> dict:
        return json.loads((FIXTURE.parent / name).read_text())

    def test_default_output_equals_retained_minus_new_keys_on_every_fixture(self) -> None:
        for name in self.FIXTURES:
            with self.subTest(fixture=name):
                raw = self._raw(name)
                default = runner.normalize_scip_json(raw)
                retained = runner.normalize_scip_json(raw, retain=True)
                self.assertEqual(default, _strip_retained(retained))
                # and the default is exactly the JSON-serialized shape it always was
                self.assertEqual(
                    json.dumps(default, sort_keys=True),
                    json.dumps(runner.normalize_scip_json(self._raw(name)), sort_keys=True),
                )

    def test_default_output_has_no_retained_keys(self) -> None:
        out = runner.normalize_scip_json(self._raw("scip_go_nested_symbols.json"))
        self.assertEqual(set(out), {"documents"})
        for doc in out["documents"]:
            self.assertEqual(set(doc), {"path", "symbols", "occurrences"})
            for occ in doc["occurrences"]:
                self.assertEqual(
                    set(occ),
                    {"symbol", "is_definition", "start_line", "start_col",
                     "enclosing_start_line", "enclosing_end_line"},
                )
            for sym in doc["symbols"]:
                self.assertEqual(set(sym), {"symbol", "kind", "display_name"})

    def test_metadata_and_document_language_are_retained_for_scip_go(self) -> None:
        out = runner.normalize_scip_json(self._raw("scip_go_nested_symbols.json"), retain=True)
        self.assertEqual(
            out["metadata"],
            {
                "tool_name": "scip-go",
                "tool_version": "0.2.7",
                "arguments": ["--output", "index.scip"],
                "project_root": "file:///tmp/gonest",
                "text_document_encoding": 1,
            },
        )
        self.assertEqual(out["external_symbols"], [])
        for doc in out["documents"]:
            self.assertEqual(doc["language"], "go")
            self.assertFalse(doc["enclosing_synthesized"])

    def test_roles_are_decoded_and_the_range_end_is_kept(self) -> None:
        doc = {
            "documents": [
                {
                    "relative_path": "x.go",
                    "symbols": [{"symbol": "s", "kind": 26, "relationships": [
                        {"symbol": "iface", "is_implementation": True, "is_reference": True},
                    ]}],
                    "occurrences": [
                        {"symbol": "s", "range": [3, 4, 9], "symbol_roles": 1 | 32},
                        {"symbol": "s", "range": [5, 1, 7, 2], "symbol_roles": 8 | 16},
                        {"symbol": "s", "range": [8, 0, 1]},
                    ],
                }
            ]
        }
        out = runner.normalize_scip_json(doc, retain=True)
        occs = out["documents"][0]["occurrences"]
        self.assertEqual(occs[0]["symbol_roles"], 33)
        self.assertEqual(occs[0]["roles"], ["definition", "test"])
        self.assertEqual((occs[0]["end_line"], occs[0]["end_col"]), (3, 9))
        self.assertEqual(occs[1]["roles"], ["read", "generated"])
        self.assertEqual((occs[1]["end_line"], occs[1]["end_col"]), (7, 2))
        self.assertEqual(occs[2]["symbol_roles"], 0)
        self.assertEqual(occs[2]["roles"], [])
        sym = out["documents"][0]["symbols"][0]
        self.assertEqual(sym["kind"], "Method")
        self.assertEqual(sym["kind_number"], 26)
        self.assertEqual(
            sym["relationships"],
            [{"symbol": "iface", "is_reference": True, "is_implementation": True,
              "is_type_definition": False, "is_definition": False}],
        )
        self.assertEqual(
            runner.decode_roles(1 | 2 | 4 | 8 | 16 | 32 | 64),
            ["definition", "import", "write", "read", "generated", "test",
             "forward_definition"],
        )

    def test_synthesized_spans_are_flagged_only_when_retained(self) -> None:
        raw = self._raw("scip_php_symbols.json")
        retained = runner.normalize_scip_json(raw, retain=True)
        flagged = [
            o for d in retained["documents"] for o in d["occurrences"]
            if o["enclosing_synthesized"]
        ]
        self.assertTrue(flagged, "scip-php spans are synthesized, so some must be flagged")
        self.assertTrue(all(o["is_definition"] for o in flagged))
        self.assertTrue(any(d["enclosing_synthesized"] for d in retained["documents"]))
        # the go dump supplies its own enclosing ranges: nothing is synthesized
        go = runner.normalize_scip_json(self._raw("scip_go_nested_symbols.json"), retain=True)
        self.assertFalse(any(
            o["enclosing_synthesized"] for d in go["documents"] for o in d["occurrences"]
        ))

    def test_json_fixture_digest_is_prefixed_canonical_sha256(self) -> None:
        raw = self._raw("scip_go_nested_symbols.json")
        canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        import hashlib

        expected = hashlib.sha256(("scip-json:" + canonical).encode()).hexdigest()
        self.assertEqual(runner.json_index_digest(raw), expected)
        # key order in the raw JSON does not change the identity
        shuffled = json.loads(json.dumps(raw, sort_keys=True))
        self.assertEqual(runner.json_index_digest(shuffled), expected)


class ReadIndexTest(unittest.TestCase):
    def test_read_with_retain_returns_the_binary_index_digest(self) -> None:
        raw_text = FIXTURE.read_text()
        with tempfile.TemporaryDirectory() as d:
            cli = Path(d) / "scip"
            cli.write_text("#!/bin/sh\n")
            cli.chmod(0o755)
            index = Path(d) / "index.scip"
            index.write_bytes(b"\x00scip-bytes")

            def fake_run(command, **kwargs):
                return SimpleNamespace(returncode=0, stdout=raw_text, stderr="")

            with (
                patch.dict(os.environ, {"SCIP_CLI": str(cli)}),
                patch("capcov.scip.runner.subprocess.run", fake_run),
            ):
                default = runner.read_scip_index(index)
                retained = runner.read_scip_index(index, retain=True)

        import hashlib

        self.assertNotIn("index_digest", default)
        self.assertEqual(retained["index_digest"], hashlib.sha256(b"\x00scip-bytes").hexdigest())
        self.assertEqual(retained["index_digest_kind"], "binary")
        self.assertIn("metadata", retained)
        self.assertEqual(
            default,
            _strip_retained({k: v for k, v in retained.items()
                             if k not in {"index_digest", "index_digest_kind"}}),
        )

    def test_scip_cli_not_located_raises_named_error(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("capcov.scip.runner._BUNDLED_SCIP", Path("/no/such/scip")),
            patch("capcov.scip.runner.shutil.which", return_value=None),
        ):
            with self.assertRaises(runner.ScipCliNotFound) as ctx:
                runner.read_scip_index("index.scip")
        self.assertIn("SCIP_CLI", str(ctx.exception))

    def test_scip_cli_env_var_takes_precedence(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            cli = Path(d) / "scip"
            cli.write_text("#!/bin/sh\n")
            cli.chmod(0o755)
            with patch.dict(os.environ, {"SCIP_CLI": str(cli)}):
                self.assertEqual(runner._locate_scip_cli(), str(cli))

    def test_read_uses_the_cli_and_normalizes_its_output(self) -> None:
        raw_text = FIXTURE.read_text()
        with tempfile.TemporaryDirectory() as d:
            cli = Path(d) / "scip"
            cli.write_text("#!/bin/sh\n")
            cli.chmod(0o755)
            captured = {}

            def fake_run(command, **kwargs):
                captured["command"] = command
                return SimpleNamespace(returncode=0, stdout=raw_text, stderr="")

            with (
                patch.dict(os.environ, {"SCIP_CLI": str(cli)}),
                patch("capcov.scip.runner.subprocess.run", fake_run),
            ):
                out = runner.read_scip_index(Path(d) / "index.scip")

            self.assertEqual(captured["command"][0], str(cli))
            self.assertEqual(captured["command"][1:3], ["print", "--json"])
            self.assertEqual(
                {doc["path"] for doc in out["documents"]},
                {"app/models/job.py", "app/server.go"},
            )


# Live tests actually shell out to a real tool. They verify the wiring the
# mocked tests cannot, and skip cleanly wherever the tool is not installed.
_HAVE_SCIP_PYTHON = shutil.which("scip-python") is not None


def _have_scip_cli() -> bool:
    try:
        runner._locate_scip_cli()
        return True
    except runner.ScipCliNotFound:
        return False


class LiveToolTest(unittest.TestCase):
    @unittest.skipUnless(_HAVE_SCIP_PYTHON, "scip-python not installed")
    def test_scip_python_indexes_a_tiny_project(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "m.py").write_text(
                "class C:\n    def f(self):\n        return 1\n"
            )
            out = runner.run_scip_index(d, "python")
            self.assertEqual(out, Path(d) / "index.scip")
            self.assertTrue(out.exists() and out.stat().st_size > 0)

    @unittest.skipUnless(
        _HAVE_SCIP_PYTHON and _have_scip_cli(),
        "needs both scip-python and the scip CLI (set SCIP_CLI)",
    )
    def test_end_to_end_index_then_read(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "m.py").write_text(
                "class C:\n    def f(self):\n        return 1\n"
            )
            index = runner.run_scip_index(d, "python")
            result = runner.read_scip_index(index)
            self.assertIn("documents", result)
            definitions = [
                occ
                for doc in result["documents"]
                for occ in doc["occurrences"]
                if occ["is_definition"]
            ]
            self.assertTrue(definitions, "a class + method must yield definitions")


if __name__ == "__main__":
    unittest.main()
