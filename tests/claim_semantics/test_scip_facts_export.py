"""The SCIP fact exporter over the real scip-go 0.2.7 dump (section 29).

Every test here is pure: the index is the checked-in ``scip_go_nested_symbols.json``
(module ``gonest``, four documents) normalized with ``retain=True``, the tree the
exporter hashes is ``tests/fixtures/go_app`` (any Go tree serves; the digest only
has to be reproducible), and the tree-sitter side is a small hand-built
``ast_raw`` in the deep adapter's shape. No indexer, no scip CLI, no tree-sitter.
"""
from __future__ import annotations

import copy
import hashlib
import json
import random
import unittest
from pathlib import Path
from unittest.mock import patch

from capcov import artifacts
from capcov.claims import (Constant, bundle_from_json, canonical_json, digest,
                           validate_bundle)
from capcov.claims.static import load_static_schema, scip_facts
from capcov.scip import resolve, runner

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
GO_DUMP = FIXTURES / "scip_go_nested_symbols.json"
GO_TREE = FIXTURES / "go_app"

_GO = "scip-go gomod github.com/example/gonest . "
_API = _GO + "`github.com/example/gonest/api`/"
_JOBS = _GO + "`github.com/example/gonest/internal/jobs`/"
GET_JOB = _API + "Handler#GetJob()."
FETCH = _JOBS + "Service#Fetch()."
REPO_GET = _JOBS + "Repo#Get()."
SURFACE = "http:GET /jobs/{id}"
HANDLER_NODE = "github.com/example/gonest/api:Handler.GetJob"


def _raw() -> dict:
    return json.loads(GO_DUMP.read_text())


def _definition_line(normalized: dict, symbol: str) -> tuple[str, int]:
    """(path, 1-based line) of ``symbol``'s definition occurrence."""
    for doc in normalized["documents"]:
        for occ in doc["occurrences"]:
            if occ["is_definition"] and occ["symbol"] == symbol:
                return doc["path"], occ["start_line"] + 1
    raise AssertionError(f"no definition of {symbol}")


def _ast_raw(normalized: dict, **overrides) -> dict:
    handler_path, handler_line = _definition_line(normalized, GET_JOB)
    fetch_path, fetch_line = _definition_line(normalized, FETCH)
    raw = {
        "surfaces": [
            {"id": SURFACE, "kind": "http", "method": "GET", "path": "/jobs/{id}",
             "handler": HANDLER_NODE, "handler_symbol": "GetJob",
             "file": handler_path, "line": handler_line + 1, "mounted": True},
        ],
        "_node_locations": {HANDLER_NODE: [handler_path, handler_line]},
        "excluded_surfaces": {"count": 1, "surfaces": [
            {"file": handler_path, "line": 3, "method": "TRACE", "path": "/debug",
             "handler": None, "reason": "route method not in the configured recognised verb set"},
        ]},
        "entities": [{"name": "jobs", "symbol": "Job", "module": "",
                      "file": "internal/jobs/repo.go", "line": 8}],
        "op_sites": [{"file": fetch_path, "line": fetch_line + 1, "entity": "jobs", "crud": "read"}],
        "blind_spots": [],
        "scip_residue": [],
        "unresolved": [],
    }
    raw.update(overrides)
    return raw


def _rows(bundle, relation: str) -> list[list]:
    return sorted(
        [t.value for t in fact.terms][1:]  # drop the index column
        for fact in bundle.facts if fact.relation == relation
    )


def _evidence(bundle, relation: str):
    return [e for e in bundle.evidence if e.atom.relation == relation]


class _Exported(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = _raw()
        cls.normalized = runner.normalize_scip_json(cls.raw, retain=True)
        cls.file_digest = runner.json_index_digest(cls.raw)   # the run receipt
        cls.ast_raw = _ast_raw(cls.normalized)
        cls.result = cls.export()
        assert cls.result.status == scip_facts.STATUS_COMPLETE, cls.result.messages
        cls.bundle = cls.result.bundle
        cls.index = dict(cls.bundle.metadata)["index_digest"]   # static-relations-v1 identity

    @classmethod
    def export(cls, normalized=None, ast_raw=None, **kwargs):
        return scip_facts.export_bundle(
            normalized if normalized is not None else cls.normalized,
            ast_raw=ast_raw if ast_raw is not None else cls.ast_raw,
            source_root=GO_TREE, language="go",
            index_digest=kwargs.pop("index_digest", cls.file_digest),
            index_digest_kind=kwargs.pop("index_digest_kind", "json"), **kwargs,
        )


class BundleShapeTest(_Exported):
    def test_bundle_validates_and_round_trips_through_strict_json(self) -> None:
        self.assertEqual(validate_bundle(self.bundle), ())
        again = bundle_from_json(canonical_json(self.bundle), validate=True)
        self.assertEqual(digest(again), digest(self.bundle))
        self.assertEqual(self.bundle.metadata, again.metadata)

    def test_relations_are_the_frozen_schema_plus_declared_derived_targets(self) -> None:
        frozen = [r["name"] for r in load_static_schema()["relations"]]
        declared = {r.name for r in self.bundle.relations}
        self.assertTrue(set(frozen) <= declared)
        stubs = declared - set(frozen)
        self.assertEqual(stubs, {"scip_document_path", "scip_definition_site_at",
                                 "static_route_declared_surface", "static_reaches",
                                 "runtime_route_observed", "runtime_function_entered",
                                 "runtime_sql_executed", "runtime_tx_committed",
                                 "runtime_route_completed", "runtime_route_reaches_sql_on_index"})
        runtime_primitives = {"runtime_route_observed", "runtime_function_entered",
                              "runtime_sql_executed", "runtime_tx_committed",
                              "runtime_route_completed"}
        for r in self.bundle.relations:
            # the static derived targets are stubs (no rules here: the rule pack
            # owns them); runtime_route_observed is a runtime primitive declared
            # only because index_describes_run names it as a compatibility target
            if r.name in stubs - runtime_primitives:
                self.assertFalse(r.primitive)
                self.assertIn(r.binding.value, {"static", "runtime"})
        self.assertEqual(self.bundle.rules, ())
        self.assertEqual(self.bundle.claims, ())

    def test_every_fact_has_exactly_the_evidence_the_exporter_declares(self) -> None:
        fact_keys = {canonical_json(f) for f in self.bundle.facts}
        evidence_keys = [canonical_json(e.atom) for e in self.bundle.evidence]
        self.assertEqual(fact_keys, set(evidence_keys))
        self.assertEqual(len(evidence_keys), len(set(evidence_keys)))
        self.assertEqual(len(self.bundle.facts), len(self.bundle.evidence))
        self.assertEqual(self.result.counts, dict(self.bundle.metadata)["row_counts"])
        self.assertEqual(sum(self.result.counts.values()), len(self.bundle.facts))
        for record in self.bundle.evidence:
            self.assertEqual(record.kind, "fact")
            self.assertTrue(record.source)
            self.assertEqual(record.context.as_dict(), {"index": self.index})
            self.assertEqual(record.atom.terms[0], Constant(self.index, "digest"))

    def test_evidence_ids_are_unique_and_content_derived(self) -> None:
        ids = [e.id for e in self.bundle.evidence]
        self.assertEqual(len(ids), len(set(ids)))
        index12 = self.index[:12]
        for record in self.bundle.evidence:
            row = [t.value for t in record.atom.terms]
            relation = record.atom.relation
            self.assertEqual(record.id, scip_facts.evidence_id(self.index, relation, row))
            prefix = "scip" if relation.startswith("scip_") else "static"
            self.assertEqual(
                record.id,
                f"{prefix}:{index12}:{relation}:{scip_facts.row_digest(relation, row)[:12]}",
            )
        # the same row under another index has another id
        other = scip_facts.evidence_id("f" * 64, "scip_document", ["f" * 64, "a.go"])
        self.assertTrue(other.startswith("scip:ffffffffffff:scip_document:"))

    def test_depends_on_chains_follow_section_29(self) -> None:
        by_id = {e.id: e for e in self.bundle.evidence}
        [index_eid] = [e.id for e in _evidence(self.bundle, "scip_index")]
        [tree_eid] = [e.id for e in _evidence(self.bundle, "scip_index_tree")]
        doc_eids = {e.atom.terms[1].value: e.id for e in _evidence(self.bundle, "scip_document")}
        self.assertIn(index_eid, by_id[tree_eid].depends_on)
        for record in self.bundle.evidence:
            for dep in record.depends_on:
                self.assertNotEqual(dep, record.id)
                if not dep.startswith("external:"):
                    self.assertIn(dep, by_id, f"{record.id} depends on unknown {dep}")
        # an edge depends on its document, its reference occurrence and the
        # caller's definition occurrence
        edges = _evidence(self.bundle, "scip_may_reference")
        self.assertTrue(edges)
        for edge in edges:
            path = edge.atom.terms[3].value
            self.assertIn(doc_eids[path], edge.depends_on)
            occurrences = [d for d in edge.depends_on
                           if d.startswith(f"external:scip-occurrence:{self.index[:12]}:")]
            self.assertGreaterEqual(len(occurrences), 2, edge.id)
            self.assertIn(
                f"external:scip-occurrence:{self.index[:12]}:{edge.atom.terms[5].value[:12]}",
                occurrences,
            )
        # sites depend on the tree identity
        for relation in ("route_site", "static_op_site", "static_entity", "route_site_excluded",
                         "static_source_file"):
            for record in _evidence(self.bundle, relation):
                self.assertIn(tree_eid, record.depends_on, relation)
        # witnesses depend on their document and the tree identity
        for record in _evidence(self.bundle, "scip_references_closed"):
            self.assertIn(tree_eid, record.depends_on)
            self.assertIn(doc_eids[record.atom.terms[1].value], record.depends_on)
        for relation in ("scip_documents_closed", "static_scope_closed",
                         "static_route_inventory_closed", "static_reachability_closed"):
            [record] = _evidence(self.bundle, relation)
            self.assertIn(tree_eid, record.depends_on)

    def test_commit_depends_on_the_external_git_commit(self) -> None:
        result = self.export(commit="abc123")
        [record] = _evidence(result.bundle, "scip_index_commit")
        self.assertEqual(record.atom.terms[1].value, "abc123")
        self.assertIn("external:git-commit:abc123", record.depends_on)
        self.assertEqual(dict(result.bundle.metadata)["commit"], "abc123")
        self.assertNotEqual(digest(result.bundle), digest(self.bundle))

    def test_index_describes_run_carries_both_contexts(self) -> None:
        result = self.export(describes_runs=("run-9",))
        [record] = _evidence(result.bundle, "index_describes_run")
        self.assertEqual(record.context.as_dict(), {"index": self.index, "run": "run-9"})
        self.assertIn("external:run:run-9", record.depends_on)
        self.assertEqual(validate_bundle(result.bundle), ())


class MetadataTest(_Exported):
    def test_metadata_carries_index_digest_tool_info_and_tree_digest(self) -> None:
        meta = dict(self.bundle.metadata)
        self.assertEqual(meta["index_digest"], self.index)
        self.assertRegex(self.index, r"^[0-9a-f]{64}$")
        self.assertEqual(meta["index_digest_kind"], scip_facts.INDEX_IDENTITY)
        # the index file digest is a run receipt: carried and reported, not identity
        self.assertEqual(meta["index_file_digest"], self.file_digest)
        self.assertEqual(meta["index_file_digest_kind"], "json")
        self.assertNotEqual(self.index, self.file_digest)
        self.assertTrue(any(self.file_digest in m and "receipt" in m for m in self.result.messages))
        self.assertTrue(any(self.index in m for m in self.result.messages))
        self.assertEqual(meta["export_version"], "v1")
        self.assertEqual(meta["exporter"], "capcov.claims.static.scip_facts")
        self.assertEqual(meta["profile"], "slice")
        self.assertEqual(meta["language"], "go")
        self.assertEqual(meta["line_frame"], "1-based")
        self.assertEqual(dict(meta["scope"]), {"kind": "all", "values": ("*",)})
        producers = dict(meta["producers"])
        self.assertEqual(producers["indexer"], "scip-go")
        self.assertEqual(producers["indexer_version"], "0.2.7")
        self.assertEqual(producers["indexer_arguments"], ("--output", "index.scip"))
        self.assertEqual(producers["treesitter"], "treesitter-routes go")
        tree = dict(meta["tree"])
        expected_digest, expected_count = artifacts.tree_sha256(GO_TREE, ("**/*.go",))
        self.assertEqual(tree["digest"], expected_digest)
        self.assertEqual(tree["file_count"], expected_count)
        self.assertGreater(tree["file_count"], 0)
        self.assertEqual(tree["pattern"], "**/*.go")
        self.assertTrue(meta["census_available"])

    def test_identity_rows_match_the_metadata(self) -> None:
        # The dump reports file:///tmp/gonest; the fact keeps only the basename
        # so the bundle digest does not depend on where the index was built.
        self.assertEqual(self.raw["metadata"]["project_root"], "file:///tmp/gonest")
        self.assertEqual(
            _rows(self.bundle, "scip_index"),
            [["scip-go", "0.2.7", "go", "file:///gonest", scip_facts.INDEX_IDENTITY]],
        )
        self.assertTrue(any("file:///tmp/gonest" in m for m in self.result.messages))
        self.assertEqual(scip_facts.canonical_project_root(None), "")
        self.assertEqual(scip_facts.canonical_project_root("file:///Users/dev/y/go_app/"), "file:///go_app")
        self.assertEqual(scip_facts.canonical_project_root("/private/tmp/abc/go_app"), "file:///go_app")
        [[tree_digest, file_count, pattern]] = _rows(self.bundle, "scip_index_tree")
        self.assertEqual((tree_digest, file_count, pattern),
                         (*artifacts.tree_sha256(GO_TREE, ("**/*.go",)), "**/*.go"))
        self.assertEqual(_rows(self.bundle, "static_scope"), [["all", "*"]])
        self.assertEqual(_rows(self.bundle, "static_language_covered"), [["go"]])
        self.assertEqual(
            [row[0] for row in _rows(self.bundle, "static_source_file")],
            [rel for rel, _ in artifacts.tree_manifest(GO_TREE, ("**/*.go",))],
        )
        [index_record] = _evidence(self.bundle, "scip_index")
        self.assertEqual(index_record.source, "scip-go 0.2.7")

    def test_a_go_tree_hashed_with_the_python_default_would_be_empty(self) -> None:
        # the reason patterns_for exists: the default set sees no Go file
        self.assertEqual(artifacts.tree_sha256(GO_TREE)[1], 0)
        self.assertEqual(artifacts.patterns_for("go"), ("**/*.go",))
        self.assertEqual(artifacts.patterns_for("php"), ("**/*.php",))
        self.assertEqual(artifacts.patterns_for("python"), artifacts.DEFAULT_PATTERNS)


class DeterminismTest(_Exported):
    def test_digest_is_identical_under_any_permutation_of_the_index(self) -> None:
        reference = digest(self.bundle)
        for seed in (1, 7, 42):
            rng = random.Random(seed)
            shuffled = copy.deepcopy(self.raw)
            rng.shuffle(shuffled["documents"])
            for doc in shuffled["documents"]:
                rng.shuffle(doc["occurrences"])
                rng.shuffle(doc["symbols"])
            normalized = runner.normalize_scip_json(shuffled, retain=True)
            ast_raw = _ast_raw(normalized)
            for key in ("surfaces", "entities", "op_sites"):
                ast_raw[key] = list(reversed(ast_raw[key]))
            result = self.export(normalized, ast_raw)
            self.assertEqual(result.status, scip_facts.STATUS_COMPLETE, result.messages)
            self.assertEqual(digest(result.bundle), reference, f"seed {seed}")
            self.assertEqual(result.counts, self.result.counts)
            # a permuted dump is a DIFFERENT JSON document, so its file receipt
            # differs; the exported bundle and its identity are permutation-invariant
            self.assertNotEqual(runner.json_index_digest(shuffled), self.file_digest)
            self.assertEqual(dict(result.bundle.metadata)["index_digest"], self.index)

    def test_exporting_twice_is_bit_for_bit_identical(self) -> None:
        again = self.export()
        self.assertEqual(canonical_json(again.bundle), canonical_json(self.bundle))

    def test_identity_is_the_exported_relations_not_the_index_file(self) -> None:
        # Section 29 "Identity and context": the index is the static-relations-v1
        # content digest, computed after the rows are built and before the ids.
        # A permuted copy of the same normalized index read from a DIFFERENT
        # index file (different receipt) has the same index, the same evidence
        # ids and the same bundle digest.
        rng = random.Random(3)
        shuffled = copy.deepcopy(self.raw)
        rng.shuffle(shuffled["documents"])
        for doc in shuffled["documents"]:
            rng.shuffle(doc["occurrences"])
            rng.shuffle(doc["symbols"])
        normalized = runner.normalize_scip_json(shuffled, retain=True)
        other_file = hashlib.sha256(b"another index.scip of the same tree").hexdigest()
        result = self.export(normalized, _ast_raw(normalized), index_digest=other_file, index_digest_kind="binary")
        self.assertEqual(result.status, scip_facts.STATUS_COMPLETE, result.messages)
        meta = dict(result.bundle.metadata)
        self.assertEqual(meta["index_digest"], self.index)
        self.assertEqual((meta["index_file_digest"], meta["index_file_digest_kind"]), (other_file, "binary"))
        self.assertEqual({e.id for e in result.bundle.evidence}, {e.id for e in self.bundle.evidence})
        self.assertEqual(sorted(canonical_json(f) for f in result.bundle.facts),
                         sorted(canonical_json(f) for f in self.bundle.facts))
        self.assertEqual(scip_facts.bundle_digest(result.bundle), scip_facts.bundle_digest(self.bundle))
        # the receipts are the only difference the plain IR digest sees
        self.assertNotEqual(digest(result.bundle), digest(self.bundle))
        stripped = lambda b: {k: v for k, v in b.metadata if k not in scip_facts.RECEIPT_METADATA_KEYS}  # noqa: E731
        self.assertEqual(stripped(result.bundle), stripped(self.bundle))
        # the recipe is public and recomputable from the rows
        rows = {}
        for fact in self.bundle.facts:
            decl = next(r for r in self.bundle.relations if r.name == fact.relation)
            rows.setdefault(fact.relation, []).append(
                [t.value for t, c in zip(fact.terms, decl.columns) if c.name != "index"])
        self.assertEqual(scip_facts.static_relations_index(rows, (r.name for r in self.bundle.relations)), self.index)
        self.assertEqual(self.index, hashlib.sha256((
            "static-relations-v1:" + canonical_json({
                "relations": sorted(r.name for r in self.bundle.relations),
                "rows": {rel: sorted(rs, key=canonical_json) for rel, rs in rows.items()}})
        ).encode()).hexdigest())
        # and a different content is a different identity, under the same receipt
        changed = copy.deepcopy(self.normalized)
        [doc] = [d for d in changed["documents"] if d["path"] == "api/jobs.go"]
        doc["occurrences"].append(dict(doc["occurrences"][0], start_line=doc["occurrences"][0]["start_line"] + 60))
        self.assertNotEqual(dict(self.export(changed).bundle.metadata)["index_digest"], self.index)
        # the assembly placeholder never leaks into an exported bundle
        self.assertNotIn(scip_facts._PLACEHOLDER_INDEX[:12] + ":", canonical_json(self.bundle))
        self.assertNotIn(scip_facts._PLACEHOLDER_INDEX, canonical_json(self.bundle))


class SliceProfileTest(_Exported):
    def test_no_raw_occurrence_rows_are_exported(self) -> None:
        relations = {f.relation for f in self.bundle.facts}
        self.assertNotIn("scip_occurrence", relations)
        self.assertNotIn("scip_occurrence", {r.name for r in self.bundle.relations})
        external = {d for e in self.bundle.evidence for d in e.depends_on
                    if d.startswith("external:scip-occurrence:")}
        self.assertTrue(external, "occurrences are referenced, never exported")
        for dep in external:
            self.assertRegex(dep, rf"^external:scip-occurrence:{self.index[:12]}:[0-9a-f]{{12}}$")

    def test_full_profile_is_permitted_only_for_a_small_document_set(self) -> None:
        refused = self.export(profile="full")
        self.assertEqual(refused.status, scip_facts.STATUS_INVALID_INPUT)
        self.assertIsNone(refused.bundle)
        allowed = self.export(profile="full",
                              scope=scip_facts.Scope.document_set(["api/jobs.go"]))
        self.assertEqual(allowed.status, scip_facts.STATUS_COMPLETE, allowed.messages)
        self.assertEqual(dict(allowed.bundle.metadata)["profile"], "full")
        self.assertEqual(_rows(allowed.bundle, "static_scope"), [["document_set", "api/jobs.go"]])
        too_many = self.export(profile="full",
                               scope=scip_facts.Scope.document_set(f"d{i}.go" for i in range(51)))
        self.assertEqual(too_many.status, scip_facts.STATUS_INVALID_INPUT)


class LimitsTest(_Exported):
    def test_limits_trip_to_resource_exhausted_without_an_exception(self) -> None:
        for limits in (scip_facts.ExportLimits(documents=1),
                       scip_facts.ExportLimits(occurrences=10),
                       scip_facts.ExportLimits(rows=20)):
            with self.subTest(limits=limits):
                result = self.export(limits=limits)
                self.assertEqual(result.status, scip_facts.STATUS_RESOURCE_EXHAUSTED)
                self.assertIsNone(result.bundle)
                self.assertTrue(result.messages)
                self.assertIn("documents", result.counts)
                self.assertIn("occurrences", result.counts)

    def test_defaults_do_not_trip_on_the_fixture(self) -> None:
        self.assertEqual(self.export(limits=scip_facts.ExportLimits()).status, "complete")

    def test_unretained_input_is_invalid_input(self) -> None:
        plain = runner.normalize_scip_json(self.raw)
        result = self.export(plain)
        self.assertEqual(result.status, scip_facts.STATUS_INVALID_INPUT)
        self.assertIsNone(result.bundle)
        self.assertIn("retain=True", result.messages[0])

    def test_a_tree_that_does_not_match_ast_raw_is_stale(self) -> None:
        result = self.export(ast_raw={**self.ast_raw, "tree_digest": "0" * 64})
        self.assertEqual(result.status, scip_facts.STATUS_STALE)
        self.assertIsNone(result.bundle)
        tree_digest, _ = artifacts.tree_sha256(GO_TREE, ("**/*.go",))
        fresh = self.export(ast_raw={**self.ast_raw, "tree_digest": tree_digest})
        self.assertEqual(fresh.status, scip_facts.STATUS_COMPLETE)


class FactContentTest(_Exported):
    def test_lines_are_one_based_so_the_handler_location_joins_its_definition(self) -> None:
        [[surface, path, line]] = _rows(self.bundle, "route_handler_location")
        self.assertEqual(surface, SURFACE)
        definitions = {(p, l): s for p, l, s in _rows(self.bundle, "scip_definition_site")}
        self.assertEqual(definitions[(path, line)], GET_JOB)
        # the scip line was 0-based: the exported line is exactly one more
        _, scip_line = _definition_line(self.normalized, GET_JOB)
        self.assertEqual(line, scip_line)
        # the handler is a callable symbol, as the rule pack's join requires
        symbols = {row[0]: row for row in _rows(self.bundle, "scip_symbol")}
        self.assertEqual(symbols[GET_JOB][1:], ["Method", "callable", "GetJob"])

    def test_the_call_chain_is_exported_as_rooted_edges(self) -> None:
        pairs = {(caller, callee) for caller, callee, *_ in _rows(self.bundle, "scip_may_reference")}
        self.assertIn((GET_JOB, FETCH), pairs)
        self.assertIn((FETCH, REPO_GET), pairs)
        for caller, callee, path, line, occurrence, synthesized in _rows(self.bundle, "scip_may_reference"):
            self.assertTrue(callee.endswith("()."))
            self.assertRegex(occurrence, r"^[0-9a-f]{64}$")
            self.assertFalse(synthesized)
            self.assertGreaterEqual(line, 1)
        self.assertEqual(_rows(self.bundle, "scip_module_scope_reference"), [])

    def test_constructor_calls_are_type_references_not_edges(self) -> None:
        refs = _rows(self.bundle, "scip_type_reference")
        self.assertTrue(refs)
        callees = {callee for _, callee, *_ in _rows(self.bundle, "scip_may_reference")}
        for referrer, type_symbol, path, line, occurrence in refs:
            self.assertTrue(type_symbol.endswith("#"), type_symbol)
            self.assertNotIn(type_symbol, callees)
            self.assertTrue(referrer.endswith("()."), referrer)

    def test_symbol_nodes_and_unrooted_reasons(self) -> None:
        nodes = dict(_rows(self.bundle, "scip_symbol_node"))
        self.assertEqual(nodes[GET_JOB], HANDLER_NODE)
        self.assertEqual(nodes[FETCH], "github.com/example/gonest/internal/jobs:Service.Fetch")
        unrooted = dict(_rows(self.bundle, "scip_symbol_unrooted"))
        self.assertTrue(unrooted)
        self.assertTrue(set(unrooted.values()) <= resolve.UNROOTED_REASONS)
        self.assertFalse(set(nodes) & set(unrooted))
        # every symbol an edge mentions is either rooted or explained
        mentioned = {s for row in _rows(self.bundle, "scip_may_reference") for s in row[:2]}
        self.assertTrue(mentioned <= set(nodes) | set(unrooted))
        # package symbols are namespaces, not nodes: explained, never rooted
        self.assertEqual(unrooted.get(_API), "non-node-descriptor")

    def test_role_relations_are_decoded_from_the_bitset(self) -> None:
        reads = _rows(self.bundle, "scip_read_site")
        self.assertTrue(reads)
        definitions = {(p, l, s) for p, l, s in _rows(self.bundle, "scip_definition_site")}
        self.assertFalse({tuple(r) for r in reads} & definitions)
        self.assertEqual(_rows(self.bundle, "scip_generated_site"), [])
        self.assertEqual(_rows(self.bundle, "scip_test_site"), [])

    def test_enclosing_spans_are_exported_unsynthesized_for_scip_go(self) -> None:
        rows = _rows(self.bundle, "scip_enclosing")
        self.assertTrue(rows)
        for symbol, path, start, end, synthesized in rows:
            self.assertLessEqual(start, end)
            self.assertGreaterEqual(start, 1)
            self.assertFalse(synthesized)

    def test_documents_carry_counts_and_the_synthesized_flag(self) -> None:
        docs = {path: rest for path, *rest in _rows(self.bundle, "scip_document")}
        self.assertEqual(set(docs), {d["path"] for d in self.normalized["documents"]})
        for doc in self.normalized["documents"]:
            self.assertEqual(
                docs[doc["path"]],
                ["go", len(doc["occurrences"]), len(doc["symbols"]), False],
            )

    def test_tree_sitter_side_rows(self) -> None:
        handler_path, handler_line = _definition_line(self.normalized, GET_JOB)
        self.assertEqual(_rows(self.bundle, "route_site"),
                         [[SURFACE, handler_path, handler_line + 1, "GetJob"]])
        self.assertEqual(_rows(self.bundle, "route_site_excluded"),
                         [["http:TRACE /debug", "route method not in the configured recognised verb set"]])
        self.assertEqual(_rows(self.bundle, "static_entity"),
                         [["jobs", "internal/jobs/repo.go", 8, "jobs"]])
        fetch_path, fetch_line = _definition_line(self.normalized, FETCH)
        self.assertEqual(_rows(self.bundle, "static_op_site"),
                         [[fetch_path, fetch_line + 1, "read", "jobs"]])
        # the op site and the route site are owned by the innermost definition
        owners = {(p, l): s for p, l, s in _rows(self.bundle, "static_site_owner")}
        self.assertEqual(owners[(fetch_path, fetch_line + 1)], FETCH)
        self.assertEqual(owners[(handler_path, handler_line + 1)], GET_JOB)
        for record in _evidence(self.bundle, "route_site"):
            self.assertEqual(record.source, "treesitter-routes go")

    def test_package_prefix_scope_restricts_occurrence_rows_and_leaks(self) -> None:
        result = self.export(scope=scip_facts.Scope.package_prefix("github.com/example/gonest/api"))
        self.assertEqual(result.status, scip_facts.STATUS_COMPLETE, result.messages)
        bundle = result.bundle
        self.assertEqual(_rows(bundle, "static_scope"),
                         [["package_prefix", "github.com/example/gonest/api"]])
        self.assertEqual(dict(bundle.metadata)["in_scope_documents"], ("api/jobs.go",))
        # documents are always all exported; occurrence rows only in scope
        self.assertEqual(len(_rows(bundle, "scip_document")), 4)
        self.assertTrue(all(p == "api/jobs.go" for p, *_ in _rows(bundle, "scip_definition_site")))
        # GetJob -> Fetch lands on first-party code outside the slice
        leaks = _rows(bundle, "static_scope_leak")
        self.assertIn([FETCH, "internal/jobs/service.go"], leaks)
        self.assertEqual(_rows(bundle, "static_scope_closed"), [])
        self.assertEqual(_rows(bundle, "static_reachability_closed"), [])
        self.assertEqual(validate_bundle(bundle), ())


class WitnessTest(_Exported):
    WITNESSES = ("scip_documents_closed", "scip_definitions_closed", "scip_references_closed",
                 "static_route_inventory_closed", "static_scope_closed",
                 "static_reachability_closed")

    def test_every_witness_names_its_predicate_version(self) -> None:
        expected = {
            "scip_documents_closed": "documents-closed-v1",
            "scip_definitions_closed": "definitions-closed-v1",
            "scip_references_closed": "references-closed-v1",
            "static_route_inventory_closed": "route-inventory-closed-v1",
            "static_scope_closed": "scope-closed-v1",
            "static_reachability_closed": "reachability-closed-v1",
        }
        for relation, predicate in expected.items():
            records = _evidence(self.bundle, relation)
            self.assertTrue(records, relation)
            for record in records:
                self.assertEqual(record.source, f"capcov.claims.static.scip_facts {predicate}")

    def test_all_witnesses_hold_on_the_clean_fixture(self) -> None:
        paths = sorted(d["path"] for d in self.normalized["documents"])
        self.assertEqual([p for [p] in _rows(self.bundle, "scip_references_closed")], paths)
        self.assertEqual([p for [p] in _rows(self.bundle, "scip_definitions_closed")], paths)
        for relation in ("scip_documents_closed", "static_route_inventory_closed",
                         "static_scope_closed", "static_reachability_closed"):
            self.assertEqual(_rows(self.bundle, relation), [[]], relation)

    def test_a_blind_spot_withholds_references_and_reachability(self) -> None:
        ast_raw = _ast_raw(self.normalized, blind_spots=[
            {"file": "internal/jobs/service.go", "line": 9, "kind": "interface_dispatch",
             "reason": "call through an interface value"},
        ])
        result = self.export(ast_raw=ast_raw)
        bundle = result.bundle
        self.assertEqual(_rows(bundle, "static_blind_spot"),
                         [["internal/jobs/service.go", 9, "interface_dispatch",
                           "call through an interface value"]])
        closed = {p for [p] in _rows(bundle, "scip_references_closed")}
        self.assertNotIn("internal/jobs/service.go", closed)
        self.assertIn("api/jobs.go", closed)
        self.assertEqual(_rows(bundle, "static_reachability_closed"), [])
        self.assertEqual(_rows(bundle, "static_scope_closed"), [[]])
        self.assertEqual(validate_bundle(bundle), ())

    def test_residue_withholds_references_for_its_document(self) -> None:
        ast_raw = _ast_raw(self.normalized, scip_residue=[
            {"file": "api/jobs.go", "line": 12, "callee": "svc.Untyped", "resolved": False,
             "reason": "seen by the ast pass; scip returned no resolution for this site"},
        ])
        bundle = self.export(ast_raw=ast_raw).bundle
        self.assertEqual(_rows(bundle, "scip_unresolved_site"),
                         [["api/jobs.go", 12, "svc.Untyped", "unresolved"]])
        self.assertNotIn("api/jobs.go", {p for [p] in _rows(bundle, "scip_references_closed")})
        self.assertEqual(_rows(bundle, "static_reachability_closed"), [])

    def test_no_census_means_no_reference_witness_at_all(self) -> None:
        ast_raw = _ast_raw(self.normalized)
        del ast_raw["scip_residue"]
        del ast_raw["blind_spots"]
        bundle = self.export(ast_raw=ast_raw).bundle
        self.assertEqual(_rows(bundle, "scip_references_closed"), [])
        self.assertEqual(_rows(bundle, "static_reachability_closed"), [])
        self.assertFalse(dict(bundle.metadata)["census_available"])
        # definitions and documents closure do not need the census
        self.assertTrue(_rows(bundle, "scip_definitions_closed"))
        self.assertEqual(_rows(bundle, "scip_documents_closed"), [[]])

    def test_synthesized_spans_withhold_the_witnesses_and_flag_the_edges(self) -> None:
        normalized = copy.deepcopy(self.normalized)
        [doc] = [d for d in normalized["documents"] if d["path"] == "api/jobs.go"]
        doc["enclosing_synthesized"] = True
        for occ in doc["occurrences"]:
            if occ["is_definition"] and occ["enclosing_start_line"] is not None:
                occ["enclosing_synthesized"] = True
        result = self.export(normalized)
        bundle = result.bundle
        docs = {p: rest for p, *rest in _rows(bundle, "scip_document")}
        self.assertTrue(docs["api/jobs.go"][-1])
        self.assertNotIn("api/jobs.go", {p for [p] in _rows(bundle, "scip_references_closed")})
        self.assertEqual(_rows(bundle, "static_reachability_closed"), [])
        edges = {(c, d): syn for c, d, p, l, o, syn in _rows(bundle, "scip_may_reference")}
        self.assertTrue(edges[(GET_JOB, FETCH)])
        self.assertFalse(edges[(FETCH, REPO_GET)])
        enclosing = {s: syn for s, p, st, en, syn in _rows(bundle, "scip_enclosing")}
        self.assertTrue(enclosing[GET_JOB])
        self.assertEqual(validate_bundle(bundle), ())

    def test_duplicate_definitions_withhold_definitions_and_references(self) -> None:
        normalized = copy.deepcopy(self.normalized)
        [doc] = [d for d in normalized["documents"] if d["path"] == "api/jobs.go"]
        [definition] = [o for o in doc["occurrences"] if o["is_definition"] and o["symbol"] == GET_JOB]
        second = dict(definition, start_line=definition["start_line"] + 40,
                      end_line=definition["end_line"] + 40,
                      enclosing_start_line=None, enclosing_end_line=None)
        doc["occurrences"].append(second)
        bundle = self.export(normalized).bundle
        sites = [l for p, l, s in _rows(bundle, "scip_definition_site") if s == GET_JOB]
        self.assertEqual(len(sites), 2, "both definition sites are exported (case 09)")
        self.assertNotIn("api/jobs.go", {p for [p] in _rows(bundle, "scip_definitions_closed")})
        self.assertNotIn("api/jobs.go", {p for [p] in _rows(bundle, "scip_references_closed")})
        self.assertIn("internal/jobs/repo.go", {p for [p] in _rows(bundle, "scip_definitions_closed")})
        self.assertEqual(_rows(bundle, "static_reachability_closed"), [])
        self.assertEqual(validate_bundle(bundle), ())

    def test_package_symbols_defined_per_file_are_not_duplicates(self) -> None:
        # scip-go defines the package symbol in every file of internal/jobs and
        # `local N` symbols are file-scoped, so both repeat across documents;
        # that is the language, not an ambiguous definition.  The exporter's
        # predicate (category != "other") is the one the rule pack's
        # scip_duplicate_definition applies, so neither withholds a witness.
        repeated = {}
        for path, _, symbol in _rows(self.bundle, "scip_definition_site"):
            repeated.setdefault(symbol, set()).add(path)
        repeated = {s: sorted(p) for s, p in repeated.items() if len(p) > 1}
        self.assertTrue(any(s.endswith("/") for s in repeated), repeated)
        self.assertTrue(any(s.startswith("local ") for s in repeated), repeated)
        categories = {s: c for s, _, c, _ in _rows(self.bundle, "scip_symbol")}
        self.assertEqual({categories[s] for s in repeated}, {"other"})
        documents = {p for p, *_ in _rows(self.bundle, "scip_document")}
        self.assertEqual({p for [p] in _rows(self.bundle, "scip_definitions_closed")}, documents)
        self.assertEqual({p for [p] in _rows(self.bundle, "scip_references_closed")}, documents)
        self.assertEqual(_rows(self.bundle, "static_reachability_closed"), [[]])

    def test_route_inventory_is_withheld_when_the_adapter_degraded(self) -> None:
        ast_raw = _ast_raw(self.normalized, unresolved=[
            {"adapter": "treesitter-routes", "kind": "deep-unavailable",
             "reason": "deep requested but the SCIP tooling is not available"},
        ])
        bundle = self.export(ast_raw=ast_raw).bundle
        self.assertEqual(_rows(bundle, "static_route_inventory_closed"), [])
        self.assertEqual(_rows(bundle, "static_reachability_closed"), [])
        self.assertEqual(_rows(bundle, "static_unresolved"),
                         [["deep-unavailable", "treesitter-routes", "", 0]])
        without_surfaces = {k: v for k, v in _ast_raw(self.normalized).items()
                            if k not in ("surfaces", "excluded_surfaces")}
        bundle = self.export(ast_raw=without_surfaces).bundle
        self.assertEqual(_rows(bundle, "static_route_inventory_closed"), [])
        self.assertEqual(_rows(bundle, "route_site"), [])

    def test_a_handler_without_a_location_is_named_and_withholds_reachability(self) -> None:
        ast_raw = _ast_raw(self.normalized, _node_locations={})
        bundle = self.export(ast_raw=ast_raw).bundle
        self.assertEqual(_rows(bundle, "route_handler_location"), [])
        kinds = {kind for kind, *_ in _rows(bundle, "static_unresolved")}
        self.assertEqual(kinds, {"route-handler-location-missing"})
        self.assertEqual(_rows(bundle, "static_reachability_closed"), [])
        self.assertEqual(_rows(bundle, "static_route_inventory_closed"), [[]])


class ImpureEntryTest(unittest.TestCase):
    def test_export_from_tree_names_the_missing_tools(self) -> None:
        with patch("capcov.scip.resolve.tools_available", return_value=False):
            with self.assertRaises(resolve.ScipToolsUnavailable) as ctx:
                scip_facts.export_from_tree(GO_TREE, "go")
        self.assertIn("scip-go", str(ctx.exception))

    def test_export_from_tree_hashes_then_removes_the_index_and_exports(self) -> None:
        import shutil
        import tempfile

        raw = _raw()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "go_app"
            shutil.copytree(GO_TREE, root)
            index = root / runner._INDEX_FILENAME

            def fake_index(target_dir, language, *, timeout=600):
                index.write_bytes(b"fake-index")
                return index

            def fake_read(path, *, retain=False):
                self.assertTrue(Path(path).exists())
                out = runner.normalize_scip_json(raw, retain=True)
                out["index_digest"] = runner.index_digest_of_bytes(Path(path).read_bytes())
                out["index_digest_kind"] = "binary"
                return out

            with (
                patch("capcov.scip.resolve.tools_available", return_value=True),
                patch("capcov.scip.runner.run_scip_index", fake_index),
                patch("capcov.scip.runner.read_scip_index", fake_read),
            ):
                result = scip_facts.export_from_tree(root, "go", ast_raw=None)
            self.assertFalse(index.exists())
        self.assertEqual(result.status, scip_facts.STATUS_COMPLETE, result.messages)
        meta = dict(result.bundle.metadata)
        self.assertEqual(meta["index_file_digest"], runner.index_digest_of_bytes(b"fake-index"))
        self.assertEqual(meta["index_file_digest_kind"], "binary")
        self.assertEqual(meta["index_digest_kind"], scip_facts.INDEX_IDENTITY)
        self.assertNotEqual(meta["index_digest"], meta["index_file_digest"])
        self.assertEqual(_rows(result.bundle, "scip_index")[0][-1], scip_facts.INDEX_IDENTITY)
        # no tree-sitter side and no census: nothing closes references
        self.assertEqual(_rows(result.bundle, "scip_references_closed"), [])
        self.assertEqual(_rows(result.bundle, "route_site"), [])


if __name__ == "__main__":
    unittest.main()
