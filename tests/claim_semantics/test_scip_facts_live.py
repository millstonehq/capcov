"""Live: index a copy of ``tests/fixtures/go_app`` with scip-go and export it.

This is the impure path the pure exporter tests cannot cover: the real indexer
writes ``index.scip``, the real ``scip`` CLI prints it, the exporter hashes the
bytes, removes the file and exports. It skips wherever ``scip-go``, ``scip`` or
``go`` is absent -- a skipped run is not evidence and the gate treats it as
such. Assertions are limited to what the go_app SOURCE fixes (file and document
counts, definition lines, the resolved call chain, node spellings from the Go
normalizer) so the test is a statement about the module, not about incidental
indexer output.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from capcov.claims import canonical_json, validate_bundle
from capcov.claims.static import scip_facts
from capcov.scip import runner

GO_APP = Path(__file__).resolve().parents[1] / "fixtures" / "go_app"
MODULE = "github.com/example/jobsvc"

# 1-based definition / site lines in go_app, read off the fixture source.
GET_JOB_LINE = 10          # api/jobs.go: func GetJob(w int, r int)
ROUTE_LINE = 17            # api/jobs.go: handle("GET /jobs/{id}", GetJob)
REPO_GET_OP_LINE = 15      # internal/jobs/repo.go: r.db.First(&models.Job{})
REPO_WRITE_OP_LINE = 20    # internal/jobs/repo.go: r.db.Create(&models.AuditLog{})
GO_FILES = 5               # api, gorm, internal/jobs, internal/service, models

_HAVE_TOOLS = bool(shutil.which("scip-go") and shutil.which("scip") and shutil.which("go"))


def _ast_raw() -> dict:
    """The tree-sitter side go_app's capcov.toml would produce, hand-shaped."""
    return {
        "surfaces": [{
            "id": "http:GET /jobs/{id}", "kind": "http", "method": "GET", "path": "/jobs/{id}",
            "handler": f"{MODULE}/api:GetJob", "handler_symbol": "GetJob",
            "file": "api/jobs.go", "line": ROUTE_LINE, "mounted": True,
        }],
        "_node_locations": {f"{MODULE}/api:GetJob": ["api/jobs.go", GET_JOB_LINE]},
        "excluded_surfaces": {"count": 0, "surfaces": []},
        "entities": [
            {"name": "jobs", "symbol": "Job", "module": "", "file": "models/models.go", "line": 7},
            {"name": "audit_logs", "symbol": "AuditLog", "module": "", "file": "models/models.go", "line": 13},
        ],
        "op_sites": [
            {"file": "internal/jobs/repo.go", "line": REPO_GET_OP_LINE, "entity": "jobs", "crud": "read"},
            {"file": "internal/jobs/repo.go", "line": REPO_WRITE_OP_LINE, "entity": "audit_logs", "crud": "create"},
        ],
        "blind_spots": [],
        "scip_residue": [],
        "unresolved": [],
    }


def _rows(bundle, relation):
    return sorted([t.value for t in f.terms][1:] for f in bundle.facts if f.relation == relation)


@unittest.skipUnless(_HAVE_TOOLS, "needs scip-go, the scip CLI and go on PATH")
class LiveGoExportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / "go_app"
        shutil.copytree(GO_APP, cls.root)
        cls.result = scip_facts.export_from_tree(cls.root, "go", ast_raw=_ast_raw())

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_export_completes_and_validates(self) -> None:
        self.assertEqual(self.result.status, scip_facts.STATUS_COMPLETE, self.result.messages)
        self.assertEqual(validate_bundle(self.result.bundle), ())

    def test_the_transient_index_is_removed_after_hashing(self) -> None:
        self.assertFalse((self.root / runner._INDEX_FILENAME).exists())
        meta = dict(self.result.bundle.metadata)
        # the hashed bytes are the run receipt; the identity is the exported relations
        self.assertEqual(meta["index_file_digest_kind"], "binary")
        self.assertRegex(meta["index_file_digest"], r"^[0-9a-f]{64}$")
        self.assertEqual(meta["index_digest_kind"], scip_facts.INDEX_IDENTITY)
        self.assertRegex(meta["index_digest"], r"^[0-9a-f]{64}$")
        self.assertNotEqual(meta["index_digest"], meta["index_file_digest"])
        self.assertEqual(_rows(self.result.bundle, "scip_index")[0][-1], scip_facts.INDEX_IDENTITY)

    def test_relation_row_counts_fixed_by_the_source_tree(self) -> None:
        counts = self.result.counts
        self.assertEqual(counts["scip_index"], 1)
        self.assertEqual(counts["scip_index_tree"], 1)
        self.assertEqual(counts["static_source_file"], GO_FILES)
        self.assertEqual(counts["scip_document"], GO_FILES)
        self.assertEqual(counts["route_site"], 1)
        self.assertEqual(counts["route_handler_location"], 1)
        self.assertEqual(counts["static_op_site"], 2)
        self.assertEqual(counts["static_entity"], 2)
        self.assertEqual(counts["static_scope"], 1)
        self.assertEqual(counts["static_language_covered"], 1)
        self.assertEqual(counts["scip_documents_closed"], 1)
        self.assertEqual(counts["scip_definitions_closed"], GO_FILES)
        self.assertNotIn("scip_occurrence", counts)
        self.assertNotIn("scip_module_scope_reference", counts,
                         "every callable reference in go_app sits inside a function body")
        self.assertGreaterEqual(counts["scip_may_reference"], 5)
        self.assertGreaterEqual(counts["scip_type_reference"], 2)
        tree = dict(dict(self.result.bundle.metadata)["tree"])
        self.assertEqual(tree["file_count"], GO_FILES)
        self.assertEqual(tree["pattern"], "**/*.go")
        self.assertEqual(dict(dict(self.result.bundle.metadata)["producers"])["indexer"], "scip-go")

    def test_node_ids_are_the_go_normalizer_spellings(self) -> None:
        nodes = set(dict(_rows(self.result.bundle, "scip_symbol_node")).values())
        for node in (f"{MODULE}/api:GetJob", f"{MODULE}/api:Register", f"{MODULE}/api:handle",
                     f"{MODULE}/internal/service:Service.Fetch",
                     f"{MODULE}/internal/jobs:Repo.Get", f"{MODULE}/internal/jobs:Repo.Write",
                     f"{MODULE}/models:Job", f"{MODULE}/models:AuditLog",
                     f"{MODULE}/gorm:DB.First", f"{MODULE}/gorm:DB.Create"):
            self.assertIn(node, nodes)

    def test_the_resolved_chain_and_the_one_based_join(self) -> None:
        bundle = self.result.bundle
        node_of = dict(_rows(bundle, "scip_symbol_node"))
        edges = {(node_of.get(c), node_of.get(d)) for c, d, *_ in _rows(bundle, "scip_may_reference")}
        self.assertIn((f"{MODULE}/api:GetJob", f"{MODULE}/internal/service:Service.Fetch"), edges)
        self.assertIn((f"{MODULE}/internal/service:Service.Fetch", f"{MODULE}/internal/jobs:Repo.Get"), edges)
        self.assertIn((f"{MODULE}/internal/service:Service.Fetch", f"{MODULE}/internal/jobs:Repo.Write"), edges)
        self.assertIn((f"{MODULE}/internal/jobs:Repo.Get", f"{MODULE}/gorm:DB.First"), edges)
        self.assertIn((f"{MODULE}/api:Register", f"{MODULE}/api:handle"), edges)
        # route_handler_location(F, L) joins scip_definition_site(F, L, GetJob)
        [[_, path, line]] = _rows(bundle, "route_handler_location")
        definitions = {(p, l): node_of.get(s) for p, l, s in _rows(bundle, "scip_definition_site")}
        self.assertEqual(definitions[(path, line)], f"{MODULE}/api:GetJob")
        # constructor calls are type references owned by the repo methods
        type_refs = {(node_of.get(r), node_of.get(t)) for r, t, *_ in _rows(bundle, "scip_type_reference")}
        self.assertIn((f"{MODULE}/internal/jobs:Repo.Get", f"{MODULE}/models:Job"), type_refs)
        self.assertIn((f"{MODULE}/internal/jobs:Repo.Write", f"{MODULE}/models:AuditLog"), type_refs)
        # data-access sites are owned by the method whose body holds them
        owners = {(p, l): node_of.get(s) for p, l, s in _rows(bundle, "static_site_owner")}
        self.assertEqual(owners[("internal/jobs/repo.go", REPO_GET_OP_LINE)], f"{MODULE}/internal/jobs:Repo.Get")
        self.assertEqual(owners[("internal/jobs/repo.go", REPO_WRITE_OP_LINE)], f"{MODULE}/internal/jobs:Repo.Write")
        self.assertEqual(owners[("api/jobs.go", ROUTE_LINE)], f"{MODULE}/api:Register")

    def test_witnesses_hold_for_the_clean_fixture(self) -> None:
        bundle = self.result.bundle
        self.assertEqual(_rows(bundle, "static_scope_closed"), [[]])
        self.assertEqual(_rows(bundle, "static_route_inventory_closed"), [[]])
        self.assertEqual(len(_rows(bundle, "scip_references_closed")), GO_FILES)
        self.assertEqual(_rows(bundle, "static_reachability_closed"), [[]])
        self.assertEqual(_rows(bundle, "static_scope_leak"), [])

    def test_re_indexing_the_same_copy_reproduces_the_identity_ids_and_digest(self) -> None:
        # scip-go's per-document ``symbols`` order is Go map iteration order
        # (EXPERIMENT-PLAN section 28), so two indexings of one and the same
        # copy may write different ``index.scip`` bytes.  The identity is the
        # static-relations-v1 content digest of the exported rows, so the index,
        # every evidence id and the bundle digest must be identical regardless;
        # only the file receipt may differ.
        again = scip_facts.export_from_tree(self.root, "go", ast_raw=_ast_raw())
        self.assertEqual(again.status, scip_facts.STATUS_COMPLETE, again.messages)
        self.assertFalse((self.root / runner._INDEX_FILENAME).exists())
        first, second = dict(self.result.bundle.metadata), dict(again.bundle.metadata)
        self.assertEqual(second["index_digest"], first["index_digest"])
        self.assertEqual({r.id for r in again.bundle.evidence}, {r.id for r in self.result.bundle.evidence})
        self.assertEqual(sorted(canonical_json(f) for f in again.bundle.facts),
                         sorted(canonical_json(f) for f in self.result.bundle.facts))
        self.assertEqual(sorted(canonical_json(e) for e in again.bundle.evidence),
                         sorted(canonical_json(e) for e in self.result.bundle.evidence))
        self.assertEqual(scip_facts.bundle_digest(again.bundle), scip_facts.bundle_digest(self.result.bundle))
        receipts = {k: v for k, v in second.items() if k in scip_facts.RECEIPT_METADATA_KEYS}
        self.assertEqual(set(receipts), set(scip_facts.RECEIPT_METADATA_KEYS))
        self.assertEqual({k: v for k, v in second.items() if k not in receipts},
                         {k: v for k, v in first.items() if k not in receipts})

    def test_project_root_fact_is_host_independent(self) -> None:
        [[_, _, _, project_root, kind]] = [[t.value for t in f.terms][1:] for f in self.result.bundle.facts
                                          if f.relation == "scip_index"]
        self.assertEqual((project_root, kind), ("file:///go_app", scip_facts.INDEX_IDENTITY))
        self.assertTrue(any(str(self.root) in message for message in self.result.messages), self.result.messages)
        self.assertNotIn(str(self.root), canonical_json(self.result.bundle))


if __name__ == "__main__":
    unittest.main()
