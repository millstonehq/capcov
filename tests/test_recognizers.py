"""Per-framework entity/operation recognizers (design §3.1-§3.3, task T4).

A recognizer answers the two framework questions the native python oracle answers,
generalized: *is this declaration a persistence entity and what is its canonical
identity?* and *what read/write operation does this data-access site perform, on
which entity?* Two crux properties are proven here:

* **Identity is the framework's, not the class name.** gorm's ``TableName()`` /
  snake_case plural and Eloquent's ``$table`` / plural, so ``Job`` -> ``jobs`` and
  ``AuditLog`` -> ``audit_logs`` -- the entity axis names DATA, not the type.
* **Untyped is NEVER guessed.** A gorm call on an untyped receiver and a PHP facade
  or builder chain (``DB::table``) do not resolve, so the recognizer OMITS the
  site rather than fabricating a binding; the blind-spot enumerator (task T2) is
  what names them (the honest-denominator contract, R4). This file proves both the
  omission and, under the treesitter extra, that the omitted PHP facade chain is
  the one ``scip.blindspots`` enumerates.

The pure tests feed the recognizer the serialized capture streams directly (the
``{capture_name: [occurrence, ...]}`` shape the engine's ``_deep_matches``
produces), so they RUN with no extra. The ``*QueryContract`` tests, gated on the
treesitter extra, drive a real tree-sitter query through the engine's own
serializer into the recognizer, proving the documented capture-name contract is
authorable against the live grammars (what task T5's ``capcov.toml`` must bind).
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

from capcov.adapters import deep_core
from capcov.adapters.recognizers import (
    default_table_name,
    go_gorm,
    php_eloquent,
    pluralize,
    snake_case,
)
from capcov.scip.blindspots import PHP_MAGIC_FACADES

_HAVE_TS = (
    importlib.util.find_spec("tree_sitter") is not None
    and importlib.util.find_spec("tree_sitter_language_pack") is not None
)


def _occ(text: str, file: str, line: int, start: int | None = None, end: int | None = None,
         type_: str = "x") -> dict:
    """One capture occurrence, the shape the engine's `_deep_matches` emits."""
    return {
        "text": text,
        "file": file,
        "line": line,
        "start_line": line if start is None else start,
        "end_line": line if end is None else end,
        "type": type_,
    }


# ==========================================================================
# Inflection (the shared canonical-identity default).


class InflectionTest(unittest.TestCase):
    def test_snake_case_camel_and_pascal(self) -> None:
        self.assertEqual(snake_case("Job"), "job")
        self.assertEqual(snake_case("AuditLog"), "audit_log")
        self.assertEqual(snake_case("UserID"), "user_id")
        self.assertEqual(snake_case("HTTPServer"), "http_server")

    def test_pluralize_regular_and_special(self) -> None:
        cases = {
            "job": "jobs", "audit_log": "audit_logs", "category": "categories",
            "address": "addresses", "box": "boxes", "company": "companies",
            "status": "statuses", "quiz": "quizzes", "wolf": "wolves",
            "knife": "knives",
        }
        for singular, plural in cases.items():
            self.assertEqual(pluralize(singular), plural, singular)

    def test_pluralize_irregular_and_uncountable(self) -> None:
        self.assertEqual(pluralize("person"), "people")
        self.assertEqual(pluralize("child"), "children")
        self.assertEqual(pluralize("audit_person"), "audit_people")  # suffix rule
        self.assertEqual(pluralize("equipment"), "equipment")
        self.assertEqual(pluralize("series"), "series")

    def test_default_table_name_is_snake_plural(self) -> None:
        self.assertEqual(default_table_name("Job"), "jobs")
        self.assertEqual(default_table_name("AuditLog"), "audit_logs")
        self.assertEqual(default_table_name("UserProfile"), "user_profiles")


# ==========================================================================
# Go / gorm.


class GoGormEntityTest(unittest.TestCase):
    def _struct(self, name: str, file: str, line: int, gorm: bool = False) -> dict:
        match = {
            "entity": [_occ(f"type {name} struct {{}}", file, line, line, line + 3)],
            "type": [_occ(name, file, line)],
        }
        if gorm:
            match["gorm"] = [_occ("gorm.Model", file, line + 1)]
        return match

    def _tablename(self, recv: str, table: str, file: str, line: int) -> dict:
        return {
            "tablename": [_occ("func ...", file, line, line, line)],
            "recv_type": [_occ(recv, file, line)],
            "table_name": [_occ(f'"{table}"', file, line)],
        }

    def test_plain_struct_gets_snake_plural_identity(self) -> None:
        entities, index = go_gorm.RECOGNIZER.recognize_entities(
            [self._struct("Job", "models/job.go", 3),
             self._struct("AuditLog", "models/audit.go", 5)]
        )
        self.assertEqual([e["name"] for e in entities], ["audit_logs", "jobs"])
        job = next(e for e in entities if e["symbol"] == "Job")
        self.assertEqual(job["name"], "jobs")
        self.assertEqual(job["file"], "models/job.go")
        self.assertEqual(job["line"], 3)
        # symbol_index binds the construction name AND the definition location.
        self.assertEqual(index["Job"], "jobs")
        self.assertEqual(index["models/job.go:3"], "jobs")

    def test_tablename_override_beats_the_plural(self) -> None:
        entities, index = go_gorm.RECOGNIZER.recognize_entities(
            [self._struct("Job", "m.go", 3),
             self._tablename("Job", "custom_jobs", "m.go", 9)]
        )
        self.assertEqual([e["name"] for e in entities], ["custom_jobs"])
        self.assertEqual(entities[0]["symbol"], "Job")
        self.assertEqual(index["Job"], "custom_jobs")

    def test_tablename_override_does_not_gate_other_structs(self) -> None:
        # A regression guard: an override is an identity refinement, NOT a marker
        # that flips the stream into precision mode and drops unmarked structs.
        entities, _ = go_gorm.RECOGNIZER.recognize_entities(
            [self._struct("Job", "m.go", 3),
             self._tablename("Job", "custom_jobs", "m.go", 9),
             self._struct("AuditLog", "m.go", 15)]
        )
        self.assertEqual({e["name"] for e in entities}, {"custom_jobs", "audit_logs"})

    def test_gorm_marker_gates_to_marked_structs(self) -> None:
        # When ANY struct carries @gorm the query author has opted into precision:
        # a plain (unmarked, no-override) struct is then NOT an entity.
        entities, _ = go_gorm.RECOGNIZER.recognize_entities(
            [self._struct("Job", "m.go", 3, gorm=True),
             self._struct("Repo", "m.go", 20)]  # a plain struct, no gorm marker
        )
        self.assertEqual([e["symbol"] for e in entities], ["Job"])

    def test_no_marker_is_permissive(self) -> None:
        # With no @gorm anywhere, every captured struct is an entity (the query
        # scoped precision by its globs, not by a marker).
        entities, _ = go_gorm.RECOGNIZER.recognize_entities(
            [self._struct("Job", "m.go", 3), self._struct("Widget", "m.go", 8)]
        )
        self.assertEqual({e["symbol"] for e in entities}, {"Job", "Widget"})


class GoGormOpTest(unittest.TestCase):
    def _op(self, verb: str, target: str | None, file: str = "r.go", line: int = 4) -> dict:
        match = {"op": [_occ("db.X(&Y{})", file, line)], "verb": [_occ(verb, file, line)]}
        if target is not None:
            match["target"] = [_occ(target, file, line)]
        return match

    _INDEX = {"Job": "jobs", "AuditLog": "audit_logs"}

    def test_verbs_map_to_crud_at_the_call_site(self) -> None:
        matches = [
            self._op("First", "Job", line=4),
            self._op("Create", "AuditLog", line=6),
            self._op("Updates", "Job", line=8),
            self._op("Delete", "Job", line=10),
        ]
        ops = go_gorm.RECOGNIZER.recognize_ops(matches, self._INDEX)
        self.assertEqual(
            {(o["entity"], o["crud"], o["line"]) for o in ops},
            {("jobs", "read", 4), ("audit_logs", "create", 6),
             ("jobs", "update", 8), ("jobs", "delete", 10)},
        )

    def test_unresolved_target_is_omitted_not_guessed(self) -> None:
        # An untyped receiver (`db.First(j)`) is not a type token; SCIP would bind
        # it, the recognizer never does.
        ops = go_gorm.RECOGNIZER.recognize_ops([self._op("First", "j")], self._INDEX)
        self.assertEqual(ops, [])

    def test_unknown_verb_and_missing_captures_are_omitted(self) -> None:
        self.assertEqual(go_gorm.RECOGNIZER.recognize_ops(
            [self._op("Preload", "Job")], self._INDEX), [])
        self.assertEqual(go_gorm.RECOGNIZER.recognize_ops(
            [self._op("First", None)], self._INDEX), [])
        self.assertEqual(go_gorm.RECOGNIZER.recognize_ops(
            [{"op": [_occ("x", "r.go", 4)]}], self._INDEX), [])


# ==========================================================================
# PHP / Eloquent.


class PhpEloquentEntityTest(unittest.TestCase):
    def _class(self, name: str, file: str, line: int, base: str | None = "Model",
               end: int | None = None, namespace: str | None = None) -> dict:
        match = {
            "entity": [_occ(f"class {name} ...", file, line, line, end or line)],
            "class": [_occ(name, file, line)],
        }
        if base is not None:
            match["base"] = [_occ(base, file, line)]
        if namespace is not None:
            match["namespace"] = [_occ(namespace, file, line)]
        return match

    def _table(self, value: str, file: str, line: int) -> dict:
        return {
            "table_prop": [_occ("table", file, line)],
            "table_name": [_occ(value, file, line)],
        }

    def test_table_property_is_the_identity(self) -> None:
        # $table on line 6 falls inside the class span [4, 8] -> bound by containment.
        entities, index = php_eloquent.RECOGNIZER.recognize_entities(
            [self._class("Job", "Job.php", 4, end=8), self._table("jobs", "Job.php", 6)]
        )
        self.assertEqual([e["name"] for e in entities], ["jobs"])
        self.assertEqual(entities[0]["symbol"], "Job")
        self.assertEqual(index["Job"], "jobs")

    def test_no_table_falls_back_to_snake_plural(self) -> None:
        entities, _ = php_eloquent.RECOGNIZER.recognize_entities(
            [self._class("AuditLog", "AuditLog.php", 4, end=6)]
        )
        self.assertEqual(entities[0]["name"], "audit_logs")

    def test_table_in_the_same_match_binds(self) -> None:
        # The alternative query shape: $table captured in the class match itself.
        match = self._class("Job", "Job.php", 4, end=8)
        match["table_prop"] = [_occ("table", "Job.php", 6)]
        match["table_name"] = [_occ("legacy_jobs", "Job.php", 6)]
        entities, _ = php_eloquent.RECOGNIZER.recognize_entities([match])
        self.assertEqual(entities[0]["name"], "legacy_jobs")

    def test_non_eloquent_base_is_not_an_entity(self) -> None:
        entities, _ = php_eloquent.RECOGNIZER.recognize_entities(
            [self._class("Helper", "Helper.php", 4, base="Controller")]
        )
        self.assertEqual(entities, [])

    def test_absent_base_is_trusted_the_query_gated_it(self) -> None:
        entities, _ = php_eloquent.RECOGNIZER.recognize_entities(
            [self._class("Job", "Job.php", 4, base=None)]
        )
        self.assertEqual(entities[0]["name"], "jobs")

    def test_namespace_yields_fqcn_index_key(self) -> None:
        _, index = php_eloquent.RECOGNIZER.recognize_entities(
            [self._class("Job", "Job.php", 4, namespace="App\\Models")]
        )
        self.assertEqual(index["Job"], "jobs")
        self.assertEqual(index["App\\Models\\Job"], "jobs")


class PhpEloquentOpTest(unittest.TestCase):
    def _op(self, verb: str, target: str | None, file: str = "R.php", line: int = 5) -> dict:
        match = {"op": [_occ("X::y()", file, line)], "verb": [_occ(verb, file, line)]}
        if target is not None:
            match["target"] = [_occ(target, file, line)]
        return match

    _INDEX = {"Job": "jobs", "AuditLog": "audit_logs", "App\\Models\\Job": "jobs"}

    def test_typed_static_references_bind(self) -> None:
        matches = [
            self._op("find", "Job", line=5),
            self._op("all", "Job", line=6),
            self._op("create", "AuditLog", line=7),
            self._op("update", "Job", line=8),
            self._op("delete", "Job", line=9),
        ]
        ops = php_eloquent.RECOGNIZER.recognize_ops(matches, self._INDEX)
        self.assertEqual(
            {(o["entity"], o["crud"]) for o in ops},
            {("jobs", "read"), ("audit_logs", "create"), ("jobs", "update"),
             ("jobs", "delete")},
        )

    def test_fully_qualified_reference_resolves_via_short_name(self) -> None:
        ops = php_eloquent.RECOGNIZER.recognize_ops(
            [self._op("find", "\\App\\Models\\Job")], self._INDEX)
        self.assertEqual(ops[0]["entity"], "jobs")

    def test_facade_chain_is_not_bound(self) -> None:
        # DB::table('jobs')->...->get() -- the known scip-php blind spot. The scope
        # is a magic facade, so it is NEVER bound (owned by the enumerator).
        ops = php_eloquent.RECOGNIZER.recognize_ops(
            [self._op("table", "DB"), self._op("connection", "Schema")], self._INDEX)
        self.assertEqual(ops, [])

    def test_the_omitted_facade_is_exactly_a_known_magic_facade(self) -> None:
        # The recognizer refuses to bind precisely what the enumerator enumerates:
        # one shared source of truth, no gap between them.
        self.assertIn("DB", PHP_MAGIC_FACADES)
        self.assertIs(php_eloquent.PHP_MAGIC_FACADES, PHP_MAGIC_FACADES)

    def test_untyped_variable_target_is_omitted(self) -> None:
        # A method call on an untyped variable ($q->get()) -- target does not
        # resolve to a model and is not a facade; omitted, never guessed.
        ops = php_eloquent.RECOGNIZER.recognize_ops(
            [self._op("get", "$query"), self._op("first", "$this->builder")], self._INDEX)
        self.assertEqual(ops, [])


# ==========================================================================
# The Recognizer Protocol wiring (deep_core resolves and type-checks them).


class RecognizerProtocolTest(unittest.TestCase):
    def test_default_recognizer_maps_go_and_php(self) -> None:
        self.assertEqual(deep_core.DEFAULT_RECOGNIZER["go"], "go_gorm")
        self.assertEqual(deep_core.DEFAULT_RECOGNIZER["php"], "php_eloquent")

    def test_load_recognizer_resolves_and_satisfies_the_protocol(self) -> None:
        for name, lang in (("go_gorm", "go"), ("php_eloquent", "php")):
            rec = deep_core.load_recognizer(name)
            self.assertIsInstance(rec, deep_core.Recognizer)
            self.assertEqual(rec.language, lang)

    def test_missing_recognizer_fails_loud(self) -> None:
        with self.assertRaises(ValueError):
            deep_core.load_recognizer("go_ent")  # not implemented -> named failure


# ==========================================================================
# The capture-name contract against the live grammars (treesitter extra).
#
# These prove the documented @capture names are authorable as a real tree-sitter
# query and flow through the engine's own serializer into the recognizer -- i.e.
# the contract task T5's capcov.toml relies on is real, not invented.

GO_ENTITY_QUERY = (
    "[(type_declaration (type_spec name: (type_identifier) @type "
    "type: (struct_type))) @entity "
    "(method_declaration receiver: (parameter_list (parameter_declaration "
    "type: (type_identifier) @recv_type)) name: (field_identifier) @m "
    "result: (type_identifier) body: (block (statement_list (return_statement "
    "(expression_list (interpreted_string_literal) @table_name)))) "
    '(#eq? @m "TableName")) @tablename]'
)
GO_OP_QUERY = (
    "(call_expression function: (selector_expression field: (field_identifier) @verb) "
    "arguments: (argument_list (unary_expression operand: (composite_literal "
    "type: (type_identifier) @target)))) @op"
)
GO_MODELS = """package models

type Job struct {
\tgorm.Model
\tName string
}

func (Job) TableName() string { return "custom_jobs" }

type AuditLog struct {
\tgorm.Model
}
"""
GO_REPO = """package repo

func (r *Repo) Get() { db.First(&Job{}) }
func (r *Repo) Write() { db.Create(&AuditLog{}) }
"""

PHP_ENTITY_QUERY = (
    "[(class_declaration name: (name) @class (base_clause (name) @base)) @entity "
    "(property_declaration (property_element name: (variable_name (name) @table_prop) "
    'default_value: (string (string_content) @table_name)) (#eq? @table_prop "table"))]'
)
PHP_OP_QUERY = "(scoped_call_expression scope: (name) @target name: (name) @verb) @op"
PHP_MODELS = """<?php
namespace App\\Models;
use Illuminate\\Database\\Eloquent\\Model;
class Job extends Model { protected $table = 'jobs'; }
class AuditLog extends Model { }
class Helper { }
"""
PHP_REPO = """<?php
namespace App\\Repositories;
use App\\Models\\Job;
class JobRepository {
    public function get($id) { return Job::find($id); }
    public function raw() { return DB::table('jobs')->where('x', 1)->get(); }
    public function w(array $d) { AuditLog::create($d); }
}
"""


@unittest.skipUnless(_HAVE_TS, "capture-name contract needs the treesitter extra")
class QueryContractTest(unittest.TestCase):
    """A real query -> the engine's serializer -> the recognizer."""

    def _matches(self, source: str, language: str, query_text: str, rel: str) -> list[dict]:
        import tree_sitter as ts
        from tree_sitter_language_pack import get_language

        from capcov.flows import discovery

        grammar = get_language(language)
        tree = ts.Parser(grammar).parse(source.encode())
        query = ts.Query(grammar, query_text)
        predicates = discovery._ts_predicates(query_text)
        return discovery._deep_matches(query, predicates, ts, tree, rel)

    def test_go_query_binds_entities_and_ops(self) -> None:
        ents = self._matches(GO_MODELS, "go", GO_ENTITY_QUERY, "models/models.go")
        entities, index = go_gorm.RECOGNIZER.recognize_entities(ents)
        by_symbol = {e["symbol"]: e["name"] for e in entities}
        self.assertEqual(by_symbol["Job"], "custom_jobs")   # TableName() override
        self.assertEqual(by_symbol["AuditLog"], "audit_logs")  # snake_case plural

        ops = self._matches(GO_REPO, "go", GO_OP_QUERY, "repo/repo.go")
        sites = go_gorm.RECOGNIZER.recognize_ops(ops, index)
        self.assertEqual(
            {(s["entity"], s["crud"]) for s in sites},
            {("custom_jobs", "read"), ("audit_logs", "create")},
        )

    def test_php_query_binds_typed_refs_and_omits_the_facade(self) -> None:
        ents = self._matches(PHP_MODELS, "php", PHP_ENTITY_QUERY, "app/Models/m.php")
        entities, index = php_eloquent.RECOGNIZER.recognize_entities(ents)
        by_symbol = {e["symbol"]: e["name"] for e in entities}
        self.assertEqual(by_symbol["Job"], "jobs")           # $table
        self.assertEqual(by_symbol["AuditLog"], "audit_logs")  # plural fallback
        self.assertNotIn("Helper", by_symbol)                # not extends Model

        ops = self._matches(PHP_REPO, "php", PHP_OP_QUERY, "app/Repositories/R.php")
        sites = php_eloquent.RECOGNIZER.recognize_ops(ops, index)
        self.assertEqual(
            {(s["entity"], s["crud"]) for s in sites},
            {("jobs", "read"), ("audit_logs", "create")},
        )
        # The DB::table facade chain never became a binding.
        self.assertNotIn("DB", {s["entity"] for s in sites})

    def test_php_facade_chain_the_recognizer_omits_is_enumerated_as_blind(self) -> None:
        # The honest-denominator half (design §4.2, R4): the untyped facade chain
        # the recognizer refuses to bind is exactly what the blind-spot enumerator
        # names -- never a silent gap.
        from capcov.scip import blindspots

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "R.php").write_text(PHP_REPO)
            spots = blindspots.enumerate_blind_spots(tmp, "php")
        self.assertTrue(any(s["kind"] == "facade_magic" for s in spots))


if __name__ == "__main__":
    unittest.main()
