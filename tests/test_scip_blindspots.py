"""The per-language blind-spot enumerator -- the honest denominator for Go/PHP.

``blindspots`` used to be 100% Python (``ast`` + a getattr/eval inventory). Deep
extraction runs over Go and PHP too, and there the danger is not a wrong answer
but a SILENT one: a census that returned ``[]`` for a whole language would make
that language look byte-identical to a fully resolved run -- ``scip_residue=[]``,
``ast_call_sites=0``, no error. So these tests prove the Go and PHP census and the
Go/PHP dynamic-dispatch inventory actually POPULATE on real parses, that the
``blind_spot_residue`` subtraction stays language-agnostic, and that the Python
path is untouched (default == explicit "python").

The Go/PHP cases need the optional ``treesitter`` extra and skip cleanly without
it, exactly as ``test_treesitter_routes`` does; the residue / Python / contract
tests are pure and always run.
"""

from __future__ import annotations

import importlib.util
import tempfile
import textwrap
import unittest
from pathlib import Path

from capcov.scip.blindspots import (
    GO_DYNAMIC_METHODS,
    PHP_MAGIC_FACADES,
    blind_spot_residue,
    enumerate_blind_spots,
    enumerate_call_sites,
)

from .support import Project

_HAVE_TS = (
    importlib.util.find_spec("tree_sitter") is not None
    and importlib.util.find_spec("tree_sitter_language_pack") is not None
)


GO_SRC = textwrap.dedent(
    """\
    package jobs

    import "reflect"

    type Fetcher interface {
        Fetch(id int) *Job
    }

    func Handle(f Fetcher, id int) *Job {
        j := f.Fetch(id)                                   // interface dispatch
        reflect.ValueOf(j).MethodByName("Save").Call(nil)  // reflection
        name := fieldOf(j)
        _ = reflect.ValueOf(j).FieldByName(name)           // dynamic field
        plain(j)                                           // ordinary call
        repo.Get(id)                                       // selector call
        return j
    }
    """
)

GO_MODELS_SRC = textwrap.dedent(
    """\
    package jobs

    type Job struct {
        ID int
    }

    func fieldOf(j *Job) string {
        return "ID"
    }
    """
)

PHP_SRC = textwrap.dedent(
    """\
    <?php
    namespace App\\Http;

    class JobController
    {
        public function __call($name, $args)
        {
            return null;
        }

        public function show(JobRepo $repo, $id)
        {
            $repo->find($id);                                  // typed member call
            $repo?->maybe($id);                                // nullsafe member call
            DB::table("jobs")->where("id", $id)->first();      // facade + builder chain
            $m = "handle";
            $this->$m();                                       // dynamic method name
            $$x();                                             // variable variable
            Job::create($data);                                // model static (NOT a facade)
        }
    }
    """
)


def _write(tmp: Path, **files: str) -> Path:
    for name, body in files.items():
        (tmp / name).write_text(body)
    return tmp


@unittest.skipUnless(_HAVE_TS, "treesitter extra not installed")
class GoEnumeratorTest(unittest.TestCase):
    def _run(self):
        with tempfile.TemporaryDirectory() as d:
            root = _write(Path(d), **{"jobs.go": GO_SRC, "models.go": GO_MODELS_SRC})
            return (
                enumerate_call_sites(root, "go"),
                enumerate_blind_spots(root, "go"),
            )

    def test_census_populates_across_files_and_call_shapes(self) -> None:
        census, _ = self._run()
        self.assertTrue(census, "the Go census must not silently return []")
        callees = {c["callee"] for c in census}
        # ordinary, selector, interface, and reflection calls are ALL sites.
        self.assertIn("plain", callees)
        self.assertIn("repo.Get", callees)
        self.assertIn("f.Fetch", callees)
        self.assertIn("reflect.ValueOf", callees)
        # the census spans both files (fieldOf declared in models.go is called).
        files = {c["file"] for c in census}
        self.assertIn("jobs.go", files)
        self.assertIn("fieldOf", callees)
        for c in census:
            self.assertEqual(set(c), {"file", "line", "callee"})
            self.assertIsInstance(c["line"], int)

    def test_magic_inventory_populates_reflection_and_interface(self) -> None:
        _, blind = self._run()
        self.assertTrue(blind, "the Go magic inventory must not silently return []")
        kinds = {b["kind"] for b in blind}
        self.assertIn("reflection", kinds)
        self.assertIn("dynamic_field", kinds)
        self.assertIn("interface_dispatch", kinds)
        for b in blind:
            self.assertEqual(set(b), {"file", "line", "kind", "reason"})
            self.assertTrue(b["reason"], "every blind spot carries a reason")

    def test_every_blind_spot_is_a_census_line(self) -> None:
        # _residue matches blind spots against the census by (file, line) and
        # keeps them unconditionally. A blind spot on a non-census line would
        # never force its site to stay in the residue, so this must hold.
        census, blind = self._run()
        census_keys = {(c["file"], c["line"]) for c in census}
        for b in blind:
            self.assertIn((b["file"], b["line"]), census_keys)

    def test_a_plain_concrete_call_is_not_flagged_blind(self) -> None:
        # Soundiness is a floor, not noise: an ordinary concrete call must stay
        # OUT of the inventory or the denominator inflates with false blinds.
        _, blind = self._run()
        blind_lines = {(b["file"], b["line"]) for b in blind}
        # `plain(j)` and `repo.Get(id)` are concrete; neither is a blind spot.
        self.assertNotIn(("jobs.go", 14), blind_lines)  # plain(j)
        self.assertNotIn(("jobs.go", 15), blind_lines)  # repo.Get(id)


@unittest.skipUnless(_HAVE_TS, "treesitter extra not installed")
class PhpEnumeratorTest(unittest.TestCase):
    def _run(self):
        with tempfile.TemporaryDirectory() as d:
            root = _write(Path(d), **{"JobController.php": PHP_SRC})
            return (
                enumerate_call_sites(root, "php"),
                enumerate_blind_spots(root, "php"),
            )

    def test_census_counts_function_member_nullsafe_scoped_and_chains(self) -> None:
        census, _ = self._run()
        self.assertTrue(census, "the PHP census must not silently return []")
        callees = {c["callee"] for c in census}
        self.assertIn("$repo->find", callees)
        self.assertIn("$repo?->maybe", callees)  # nullsafe member call counted
        self.assertIn("DB::table", callees)
        self.assertIn("Job::create", callees)
        # every link of the fluent chain is its own site, like Python's f()().
        chain = [c for c in census if c["callee"].endswith("->first")]
        self.assertTrue(chain, "the tail of the builder chain is a call site")
        for c in census:
            self.assertEqual(set(c), {"file", "line", "callee"})

    def test_magic_inventory_populates_facade_dynamic_and_varvar(self) -> None:
        _, blind = self._run()
        self.assertTrue(blind, "the PHP magic inventory must not silently return []")
        kinds = {b["kind"] for b in blind}
        self.assertIn("facade_magic", kinds)       # DB::table(...)
        self.assertIn("dynamic_dispatch", kinds)   # $this->$m()
        self.assertIn("variable_variable", kinds)  # $$x()
        for b in blind:
            self.assertEqual(set(b), {"file", "line", "kind", "reason"})
            self.assertTrue(b["reason"])

    def test_a_typed_model_static_is_not_a_facade_blind_spot(self) -> None:
        # Job::create is an Eloquent model call, resolvable through the typed
        # class reference -- binding it is task T4's recognizer, NOT a generic
        # blind spot. Flagging it here would double-count and inflate the floor.
        census, blind = self._run()
        (job_line,) = {c["line"] for c in census if c["callee"] == "Job::create"}
        blind_at = {(b["file"], b["line"]) for b in blind}
        self.assertNotIn(("JobController.php", job_line), blind_at)

    def test_every_blind_spot_is_a_census_line(self) -> None:
        census, blind = self._run()
        census_keys = {(c["file"], c["line"]) for c in census}
        for b in blind:
            self.assertIn((b["file"], b["line"]), census_keys)


class ResidueStaysLanguageAgnosticTest(unittest.TestCase):
    """``blind_spot_residue`` is the subtraction, and it must not care about the
    language: it keys on (file, line) only. These run with no extra installed."""

    def test_a_go_style_census_minus_resolved_names_the_remainder(self) -> None:
        census = [
            {"file": "jobs.go", "line": 8, "callee": "f.Fetch"},
            {"file": "jobs.go", "line": 9, "callee": "reflect.ValueOf"},
        ]
        resolved = [{"file": "jobs.go", "line": 8, "symbol": "jobs:Fetcher.Fetch"}]

        residue = blind_spot_residue(census, resolved)

        self.assertEqual(len(residue), 1)
        (only,) = residue
        self.assertEqual((only["file"], only["line"]), ("jobs.go", 9))
        self.assertEqual(only["callee"], "reflect.ValueOf")
        self.assertFalse(only["resolved"])
        self.assertTrue(only["reason"])

    def test_a_php_blind_kind_reason_is_preserved_and_appended(self) -> None:
        # When a census site already carries a blind-spot reason (as _residue
        # merges in), the residue keeps it and appends its own clause.
        census = [
            {
                "file": "JobController.php",
                "line": 7,
                "callee": "DB::table",
                "kind": "facade_magic",
                "reason": "facade routes through __callStatic",
            }
        ]
        residue = blind_spot_residue(census, [])
        (only,) = residue
        self.assertIn("facade routes through __callStatic", only["reason"])
        self.assertIn("scip returned no resolution", only["reason"])
        self.assertEqual(only["kind"], "facade_magic")


class PythonPathUnchangedTest(unittest.TestCase):
    """The Python enumerator must be byte-identical: the default and an explicit
    ``language="python"`` produce the same output, and it still finds the AST
    blind spots. Pure ``ast``; runs with no extra installed."""

    def test_default_equals_explicit_python_for_census_and_blind(self) -> None:
        with Project() as project:  # the shared APP fixture
            default_sites = enumerate_call_sites(project.source)
            explicit_sites = enumerate_call_sites(project.source, "python")
            default_blind = enumerate_blind_spots(project.source)
            explicit_blind = enumerate_blind_spots(project.source, language="python")

        self.assertEqual(default_sites, explicit_sites)
        self.assertEqual(default_blind, explicit_blind)
        # and the Python path is genuinely doing its AST work, not a stub.
        self.assertTrue(default_sites, "the APP fixture has call sites")
        self.assertIn("attribute_by_name", {b["kind"] for b in default_blind})

    def test_python_census_is_still_pure_ast(self) -> None:
        files = {
            "m.py": textwrap.dedent(
                """\
                def go(service, obj, name):
                    service.do(obj)
                    return getattr(obj, name)
                """
            )
        }
        with Project(files) as project:
            sites = enumerate_call_sites(project.source)
        by_line = {s["line"]: s["callee"] for s in sites}
        self.assertEqual(by_line[2], "service.do")
        self.assertEqual(by_line[3], "getattr")


class UnsupportedLanguageRaisesTest(unittest.TestCase):
    """The soundiness floor: an unsupported language must RAISE, never return []
    (an empty census would look identical to a clean, fully resolved run)."""

    def test_census_raises_for_an_unknown_language(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                enumerate_call_sites(d, "rust")

    def test_blind_spots_raise_for_an_unknown_language(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                enumerate_blind_spots(d, "rust")


class SingleSourceOfTruthConstantsTest(unittest.TestCase):
    """T4's recognizers import these back rather than keeping their own copy, the
    same contract as the FastAPI adapter importing ``DYNAMIC_BLIND_CALLS``."""

    def test_go_dynamic_methods_and_php_facades_are_exported(self) -> None:
        self.assertIn("MethodByName", GO_DYNAMIC_METHODS)
        self.assertIn("FieldByName", GO_DYNAMIC_METHODS)
        self.assertIn("DB", PHP_MAGIC_FACADES)
        self.assertIn("Schema", PHP_MAGIC_FACADES)


if __name__ == "__main__":
    unittest.main()
