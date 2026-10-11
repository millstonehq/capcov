from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest

from capcov.claims import Atom, Bundle, Column, RelationDecl, Rule, Variable, validate_bundle


SCHEMA = Path(__file__).parents[2] / "src/capcov/claims/static/schema_static_v1.json"

COLUMNS = {
    "scip_index": ("index", "indexer", "indexer_version", "language", "project_root", "digest_kind"),
    "scip_index_tree": ("index", "tree_digest", "file_count", "pattern"),
    "scip_index_commit": ("index", "commit"),
    "static_scope": ("index", "scope_kind", "scope_value"),
    "static_language_covered": ("index", "language"),
    "scip_document": ("index", "path", "language", "occurrence_count", "symbol_count", "enclosing_synthesized"),
    "static_source_file": ("index", "path", "language"),
    "scip_symbol": ("index", "symbol", "kind", "category", "display_name"),
    "scip_symbol_node": ("index", "symbol", "node"),
    "scip_symbol_unrooted": ("index", "symbol", "reason"),
    "scip_relationship": ("index", "symbol", "related", "kind"),
    "scip_definition_site": ("index", "path", "line", "symbol"),
    "scip_generated_site": ("index", "path", "line", "symbol"),
    "scip_test_site": ("index", "path", "line", "symbol"),
    "scip_import_site": ("index", "path", "line", "symbol"),
    "scip_write_site": ("index", "path", "line", "symbol"),
    "scip_read_site": ("index", "path", "line", "symbol"),
    "scip_enclosing": ("index", "symbol", "path", "start_line", "end_line", "synthesized"),
    "static_site_owner": ("index", "path", "line", "symbol"),
    "scip_may_reference": ("index", "caller", "callee", "path", "line", "occurrence", "caller_synthesized"),
    "scip_module_scope_reference": ("index", "callee", "path", "line", "occurrence"),
    "scip_type_reference": ("index", "referrer", "type_symbol", "path", "line", "occurrence"),
    "route_site": ("index", "surface", "path", "line", "handler_name"),
    "route_handler_location": ("index", "surface", "path", "line"),
    "route_site_excluded": ("index", "surface", "reason"),
    "static_op_site": ("index", "path", "line", "verb", "entity"),
    "static_entity": ("index", "entity", "path", "line", "table"),
    "static_blind_spot": ("index", "path", "line", "kind", "reason"),
    "scip_unresolved_site": ("index", "path", "line", "callee_text", "kind"),
    "static_unresolved": ("index", "kind", "node", "path", "line"),
    "changed_symbol": ("index", "symbol", "change_kind"),
    "static_scope_leak": ("index", "symbol", "defined_in"),
    "authz_symbol__accepted": ("index", "symbol"),
    "source_tree_observed": ("tree_digest",),
    "run_built_from_commit": ("run", "commit"),
    "scip_documents_closed": ("index",),
    "scip_definitions_closed": ("index", "path"),
    "scip_references_closed": ("index", "path"),
    "static_route_inventory_closed": ("index",),
    "static_scope_closed": ("index",),
    "static_reachability_closed": ("index",),
    "scip_index_comparable": ("index_a", "index_b", "basis"),
    "index_describes_run": ("index", "run"),
}


def relation(raw):
    return RelationDecl(raw["name"], tuple(Column(c["name"], c["type"], c["context"]) for c in raw["columns"]),
                        raw["modality"], raw["polarity"], raw["binding"], raw["primitive"],
                        tuple(raw["producer_classes"]), tuple(raw["context_indices"]), raw["completes"],
                        raw["finite"], raw["nonempty"], tuple(raw["compatibility_targets"]),
                        tuple(raw["compatibility_context_indices"]))


class FrozenStaticSchemaTests(unittest.TestCase):
    def load(self):
        return json.loads(SCHEMA.read_text(encoding="utf-8"))

    def test_exact_primitive_names_columns_and_modal_contract(self):
        document = self.load()
        canonical = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(hashlib.sha256(canonical).hexdigest(),
                         "98cefbf987a78a8954fae960e7a3af526a6fa329f48453e1e91fb509a7c27564")
        declarations = document["relations"]
        self.assertEqual([item["name"] for item in declarations], list(COLUMNS))
        for item in declarations:
            name = item["name"]
            with self.subTest(relation=name):
                self.assertEqual(tuple(c["name"] for c in item["columns"]), COLUMNS[name])
                self.assertTrue(item["primitive"])
                self.assertEqual(item["polarity"], "positive")
                self.assertEqual(set(item), {"name", "columns", "modality", "polarity", "binding", "primitive",
                                             "producer_classes", "context_indices", "completes", "finite", "nonempty",
                                             "compatibility_targets", "compatibility_context_indices"})
                self.assertEqual(set(item["context_indices"]),
                                 {c["name"] for c in item["columns"] if c["context"]})
                if name not in {"source_tree_observed", "run_built_from_commit", "scip_index_comparable",
                                "index_describes_run"}:
                    self.assertEqual(item["binding"], "static")
                    self.assertEqual(item["context_indices"], ["index"])
        by_name = {item["name"]: item for item in declarations}
        self.assertEqual((by_name["scip_index"]["finite"], by_name["scip_index"]["nonempty"]), (True, True))
        self.assertEqual(by_name["authz_symbol__accepted"]["modality"], "assumption")
        self.assertEqual(by_name["source_tree_observed"]["context_indices"], [])
        self.assertEqual(by_name["run_built_from_commit"]["binding"], "runtime")
        self.assertEqual({name: by_name[name]["completes"] for name in (
            "scip_documents_closed", "scip_definitions_closed", "scip_references_closed",
            "static_route_inventory_closed", "static_scope_closed", "static_reachability_closed")}, {
                "scip_documents_closed": "scip_document_path",
                "scip_definitions_closed": "scip_definition_site_at",
                "scip_references_closed": "scip_may_reference",
                "static_route_inventory_closed": "static_route_declared_surface",
                "static_scope_closed": "static_scope",
                "static_reachability_closed": "static_reaches",
            })
        self.assertEqual(by_name["scip_index_comparable"]["compatibility_context_indices"],
                         ["index_a", "index_b"])
        self.assertTrue({"runtime_route_observed", "static_route_declared_surface"}
                        <= set(by_name["index_describes_run"]["compatibility_targets"]))

    def merged_relations(self):
        raw = self.load()["relations"]
        relations = [relation(item) for item in raw]
        relations.extend((
            RelationDecl("scip_document_path", (Column("index", "digest", True), Column("path", "symbol")),
                         binding="static", primitive=False, context_indices=("index",)),
            RelationDecl("scip_definition_site_at", (Column("index", "digest", True), Column("path", "symbol")),
                         binding="static", primitive=False, context_indices=("index",)),
            RelationDecl("static_route_declared_surface", (Column("index", "digest", True), Column("surface", "symbol")),
                         binding="static", primitive=False, finite=True, nonempty=True, context_indices=("index",)),
            RelationDecl("static_reaches", (Column("index", "digest", True), Column("src", "symbol"), Column("dst", "symbol")),
                         binding="static", primitive=False, context_indices=("index",)),
            RelationDecl("runtime_route_observed", (Column("tenant", "symbol", True), Column("surface", "symbol", True),
                         Column("event", "symbol", True), Column("run", "symbol", True)),
                         context_indices=("tenant", "surface", "event", "run")),
            RelationDecl("runtime_function_entered", (Column("run", "symbol", True), Column("request", "symbol", True),
                         Column("symbol", "symbol")), producer_classes=("target-go-runtime-trace-v2",),
                         context_indices=("run", "request")),
            RelationDecl("runtime_sql_executed", (Column("run", "symbol", True), Column("request", "symbol", True),
                         Column("tx", "symbol", True), Column("operation", "symbol"), Column("ordinal", "unsigned")),
                         producer_classes=("target-go-runtime-trace-v2",), context_indices=("run", "request", "tx")),
            RelationDecl("runtime_tx_committed", (Column("run", "symbol", True), Column("request", "symbol", True),
                         Column("tx", "symbol", True)), producer_classes=("target-go-runtime-trace-v2",),
                         context_indices=("run", "request", "tx")),
            RelationDecl("runtime_route_completed", (Column("run", "symbol", True), Column("request", "symbol", True),
                         Column("surface", "symbol", True)), producer_classes=("target-go-runtime-trace-v2",),
                         context_indices=("run", "request", "surface")),
            RelationDecl("runtime_route_reaches_sql_on_index", (Column("index", "digest", True),
                         Column("run", "symbol", True), Column("request", "symbol", True),
                         Column("surface", "symbol", True), Column("symbol", "symbol"),
                         Column("tx", "symbol", True), Column("operation", "symbol")),
                         modality="derived", primitive=False,
                         context_indices=("index", "run", "request", "surface", "tx")),
            RelationDecl("cross_out", (Column("path", "symbol"), Column("symbol", "symbol")), primitive=False),
            RelationDecl("runtime_route_without_static", (Column("tenant", "symbol", True), Column("surface", "symbol", True),
                         Column("run", "symbol", True), Column("index", "digest", True)), modality="claim",
                         primitive=False, context_indices=("tenant", "surface", "run", "index")),
        ))
        return tuple(relations)

    def test_static_schema_requires_declared_derived_targets_before_validation(self):
        primitive_relations = tuple(relation(item)
                                    for item in self.load()["relations"])
        issue_codes = {issue.code
                       for issue in validate_bundle(Bundle(primitive_relations))}
        self.assertIn("completeness-target", issue_codes)
        self.assertIn("compatibility-target", issue_codes)

    def test_merged_schema_validates_cross_index_and_exact_static_runtime_rules(self):
        relations = self.merged_relations()
        cross = Rule(Atom("cross_out", (Variable("path"), Variable("symbol"))), (
            Atom("scip_document", (Variable("left"), Variable("path"), Variable("language"), Variable("oc"),
                                   Variable("sc"), Variable("synth"))),
            Atom("scip_symbol", (Variable("right"), Variable("symbol"), Variable("kind"), Variable("category"),
                                 Variable("display"))),
            Atom("scip_index_comparable", (Variable("left"), Variable("right"), Variable("basis"))),
        ))
        mixed = Rule(Atom("runtime_route_without_static", (Variable("tenant"), Variable("surface"),
                                                             Variable("run"), Variable("index"))), (
            Atom("runtime_route_observed", (Variable("tenant"), Variable("surface"), Variable("event"), Variable("run"))),
            Atom("index_describes_run", (Variable("index"), Variable("run"))),
            Atom("static_route_inventory_closed", (Variable("index"),)),
            Atom("static_route_declared_surface", (Variable("index"), Variable("surface")), negated=True),
        ))
        issues = validate_bundle(Bundle(relations, rules=(cross, mixed)))
        self.assertEqual(issues, ())


if __name__ == "__main__":
    unittest.main()
