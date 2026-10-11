"""The promoted route/contract adapters wrap the shared engine to the CORE shape.

treesitter-routes and structured-spec run `flows.discovery`'s engine and project
its obligation inventory onto the CORE adapter contract: each surface obligation
becomes an entity AND a self-bound surface (hop-0 `_direct`), verb->CRUD `_ops`,
and REQUIRED-but-empty `blind_spots`/`residue`/`residue_summary` (which
`cli.cmd_discover` reads unconditionally). The honest denominator survives as
first-class top-level keys: `excluded_surfaces` (verb-allowlist drops) and
`unresolved` (dynamic paths, boundary limits, adapters that found nothing).

The treesitter cases need the optional `treesitter` extra and RUN (not skip)
under `--extra treesitter`; the OpenAPI cases need no extra.
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from capcov import cli
from capcov.adapters import build_core_dict, merge, structured_spec, treesitter_routes
from capcov.core import fixpoint

_HAVE_TS = (
    importlib.util.find_spec("tree_sitter") is not None
    and importlib.util.find_spec("tree_sitter_language_pack") is not None
)

_ZERO_RESIDUE = {
    "resolved_by_import": 0,
    "resolved_by_name": 0,
    "ambiguous": 0,
    "external": 0,
    "chained": 0,
    "builtin_shadowed": 0,
}

# A python decorator route query: @method is the verb, @path the literal, and the
# decorated function is @handler so its body is walked for candidates.
PY_HANDLER_QUERY = (
    "(decorated_definition "
    "(decorator (call function: (attribute attribute: (identifier) @method) "
    "arguments: (argument_list (string) @path))) "
    "definition: (function_definition) @handler)"
)
# A Go bare-call route query capturing the handler identifier as @handler.
GO_QUERY = (
    "((call_expression function: (identifier) @fn (#eq? @fn \"handle\") "
    "arguments: (argument_list (interpreted_string_literal) @path)))"
)
GO_QUERY_HANDLER = (
    "((call_expression function: (identifier) @fn (#eq? @fn \"handle\") "
    "arguments: (argument_list (interpreted_string_literal) @path . (identifier) @handler)))"
)


def _fixpoint_capabilities(raw: dict) -> list[dict]:
    """Reproduce cli.cmd_discover's capability assembly for the given core dict."""
    direct, calls, ops = raw["_direct"], raw["_calls"], raw["_ops"]
    roots = [s["handler"] for s in raw["surfaces"]]
    per_root, _history = fixpoint.bind(roots, calls, direct)
    caps = []
    for surface in raw["surfaces"]:
        bound = per_root.get(surface["handler"], {})
        reach = fixpoint.distances(surface["handler"], calls)
        for entity, hops in sorted(bound.items()):
            observed_ops: set[str] = set()
            for node in reach:
                observed_ops |= ops.get(node, {}).get(entity, set())
            caps.append(
                {
                    "entity": entity,
                    "surface": surface["id"],
                    "operations": sorted(observed_ops),
                    "hops": hops,
                    "chain": fixpoint.chain(surface["handler"], entity, calls, direct),
                }
            )
    return caps


class StructuredSpecAdapterTests(unittest.TestCase):
    def _discover(self, document: dict, **cfg) -> dict:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "openapi.json").write_text(json.dumps(document))
        return structured_spec.discover(root, root, config={"document": "openapi.json", **cfg})

    def test_operation_becomes_entity_and_self_bound_surface(self) -> None:
        raw = self._discover({"openapi": "3.1.0", "paths": {"/items": {"get": {}, "post": {}}}})
        ids = {"http:GET /items", "http:POST /items"}
        self.assertEqual({s["id"] for s in raw["surfaces"]}, ids)
        # entity name == surface id, the faithful obligation-as-entity mapping.
        self.assertEqual({e["name"] for e in raw["entities"]}, ids)
        # Every surface's handler is a valid node id present in _direct, bound to
        # exactly its own entity at hop 0.
        for surface in raw["surfaces"]:
            self.assertIn(surface["handler"], raw["_direct"])
            self.assertEqual(raw["_direct"][surface["handler"]], {surface["id"]})
        # verb -> CRUD.
        self.assertEqual(raw["_ops"]["http:GET /items"], {"http:GET /items": {"read"}})
        self.assertEqual(raw["_ops"]["http:POST /items"], {"http:POST /items": {"create"}})

    def test_required_but_empty_carriers_present(self) -> None:
        raw = self._discover({"openapi": "3.1.0", "paths": {"/items": {"get": {}}}})
        self.assertEqual(raw["blind_spots"], [])
        self.assertEqual(raw["residue"], [])
        self.assertEqual(raw["residue_summary"], _ZERO_RESIDUE)
        self.assertEqual(raw["excluded_surfaces"], {"count": 0, "surfaces": []})

    def test_fixpoint_emits_one_hop0_direct_capability_per_operation(self) -> None:
        raw = self._discover({"openapi": "3.1.0", "paths": {"/items": {"get": {}}}})
        caps = _fixpoint_capabilities(raw)
        self.assertEqual(len(caps), 1)
        cap = caps[0]
        self.assertEqual(cap["entity"], "http:GET /items")
        self.assertEqual(cap["surface"], "http:GET /items")
        self.assertEqual(cap["operations"], ["read"])
        self.assertEqual(cap["hops"], 0)
        self.assertEqual(cap["chain"], ["http:GET /items"])

    def test_boundaries_reach_unresolved_not_dropped(self) -> None:
        raw = self._discover({"openapi": "3.1.0", "paths": {"/items": {"get": {}}}})
        boundaries = [u for u in raw["unresolved"] if u["kind"] == "boundary"]
        self.assertTrue(boundaries)
        # The document boundaries name the limits of a declaration, never dropped.
        self.assertTrue(all(u["id"].startswith("boundary:openapi:") for u in boundaries))

    def test_empty_doc_yields_only_unresolved(self) -> None:
        raw = self._discover({"openapi": "3.1.0", "paths": {}})
        self.assertEqual(raw["entities"], [])
        self.assertEqual(raw["surfaces"], [])
        self.assertEqual(raw["excluded_surfaces"]["count"], 0)
        self.assertTrue(raw["unresolved"])
        kinds = {u["kind"] for u in raw["unresolved"]}
        # The adapter that produced no surface is named, and so are the boundaries:
        # an empty spec has no green denominator.
        self.assertIn("no-surfaces", kinds)
        self.assertIn("boundary", kinds)

    def test_prefix_is_applied(self) -> None:
        raw = self._discover({"openapi": "3.1.0", "paths": {"/items": {"get": {}}}}, prefix="/api")
        self.assertEqual({s["id"] for s in raw["surfaces"]}, {"http:GET /api/items"})

    def test_unknown_profile_raises(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "spec.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "unknown profile"):
            structured_spec.discover(root, root, config={"document": "spec.json", "profile": "raml"})


@unittest.skipUnless(_HAVE_TS, "treesitter extra not installed")
class TreesitterRoutesAdapterTests(unittest.TestCase):
    def _discover(self, files: dict[str, str], config: dict) -> dict:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for name, body in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        return treesitter_routes.discover(root, root, config=config)

    def test_route_becomes_self_bound_surface_with_verb_in_id(self) -> None:
        raw = self._discover(
            {"svc.go": 'package main\nfunc f() {\n\thandle("POST /ask", askHandler)\n}\n'},
            {"language": "go", "files": ["svc.go"], "query": GO_QUERY_HANDLER},
        )
        self.assertEqual({s["id"] for s in raw["surfaces"]}, {"http:POST /ask"})
        surface = raw["surfaces"][0]
        # The runtime-matchable id carries the verb; the source handler name is
        # kept as provenance, and the node id is the id (unique per route).
        self.assertEqual(surface["method"], "POST")
        self.assertEqual(surface["path"], "/ask")
        self.assertEqual(surface["handler_symbol"], "askHandler")
        self.assertEqual(surface["handler"], "http:POST /ask")
        self.assertEqual(raw["_direct"]["http:POST /ask"], {"http:POST /ask"})
        self.assertEqual(raw["_ops"]["http:POST /ask"], {"http:POST /ask": {"create"}})
        # entity name coincides with the surface id.
        self.assertEqual({e["name"] for e in raw["entities"]}, {"http:POST /ask"})

    def test_every_surface_has_a_valid_handler_node_id(self) -> None:
        raw = self._discover(
            {"svc.go": (
                'package main\nfunc f() {\n'
                '\thandle("POST /ask", a)\n'
                '\thandle("GET /state", b)\n}\n'
            )},
            {"language": "go", "files": ["svc.go"], "query": GO_QUERY},
        )
        self.assertEqual(len(raw["surfaces"]), 2)
        for surface in raw["surfaces"]:
            self.assertIn(surface["handler"], raw["_direct"])
            self.assertTrue(surface["handler"])

    def test_verb_allowlist_drop_lands_in_excluded_not_surfaces(self) -> None:
        raw = self._discover(
            {"routes.py": (
                '@router.get("/items")\ndef items():\n    return []\n\n'
                '@router.head("/items")\ndef head_items():\n    return None\n\n'
                '@router.websocket("/ws")\ndef ws():\n    return None\n'
            )},
            {
                "language": "python",
                "files": ["routes.py"],
                "query": PY_HANDLER_QUERY,
                "methods": ["GET", "POST", "PUT", "PATCH", "DELETE"],
            },
        )
        self.assertEqual({s["id"] for s in raw["surfaces"]}, {"http:GET /items"})
        self.assertEqual(raw["excluded_surfaces"]["count"], 2)
        self.assertEqual(
            {s["method"] for s in raw["excluded_surfaces"]["surfaces"]}, {"HEAD", "WEBSOCKET"}
        )

    def test_dynamic_path_is_unresolved(self) -> None:
        query = (
            "((call_expression function: (identifier) @fn (#eq? @fn \"handle\") "
            "arguments: (argument_list . (_) @path)))"
        )
        raw = self._discover(
            {"svc.go": (
                'package main\nfunc f() {\n'
                '\thandle("POST /ask", a)\n'
                '\thandle(pathVar, b)\n}\n'
            )},
            {"language": "go", "files": ["svc.go"], "query": query},
        )
        self.assertEqual({s["id"] for s in raw["surfaces"]}, {"http:POST /ask"})
        dynamic = [u for u in raw["unresolved"] if u["kind"] == "dynamic-route"]
        self.assertEqual(len(dynamic), 1)

    def test_one_path_under_two_verbs_keeps_both_surfaces_with_plain_paths(self) -> None:
        raw = self._discover(
            {"svc.go": 'package main\nfunc f() {\n\thandle("GET /api/threads", a)\n\thandle("POST /api/threads", b)\n}\n'},
            {"language": "go", "files": ["svc.go"], "query": GO_QUERY_HANDLER},
        )
        self.assertEqual(
            sorted((s["id"], s["path"]) for s in raw["surfaces"]),
            [("http:GET /api/threads", "/api/threads"), ("http:POST /api/threads", "/api/threads")],
        )

    def test_missing_language_or_query_raises(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        with self.assertRaisesRegex(ValueError, "language.*query"):
            treesitter_routes.discover(root, root, config={"language": "go"})


class MergeTests(unittest.TestCase):
    def _route(self, sid: str, method: str, path: str) -> dict:
        return build_core_dict(
            [{"id": sid, "method": method, "path": path, "handler": None,
              "file": "f", "line": 1, "module": "m"}],
            {"count": 0, "surfaces": []},
            [{"adapter": "x", "kind": "boundary", "reason": "r", "id": "boundary:x"}],
        )

    def test_single_adapter_returned_unchanged(self) -> None:
        one = self._route("http:GET /a", "GET", "/a")
        self.assertIs(merge([one]), one)

    def test_two_adapters_combine(self) -> None:
        merged = merge([
            self._route("http:GET /a", "GET", "/a"),
            self._route("http:GET /b", "GET", "/b"),
        ])
        self.assertEqual({s["id"] for s in merged["surfaces"]}, {"http:GET /a", "http:GET /b"})
        self.assertEqual({e["name"] for e in merged["entities"]}, {"http:GET /a", "http:GET /b"})
        # Both adapters' unresolved carriers survive the merge -- N is not shrunk.
        self.assertEqual(len(merged["unresolved"]), 2)
        self.assertEqual(merged["_direct"]["http:GET /a"], {"http:GET /a"})
        self.assertEqual(merged["residue_summary"], _ZERO_RESIDUE)

    def test_duplicate_surface_id_across_adapters_raises(self) -> None:
        dup = self._route("http:GET /a", "GET", "/a")
        with self.assertRaisesRegex(ValueError, "duplicate obligation IDs"):
            merge([dup, self._route("http:GET /a", "GET", "/a")])


class CliIntegrationTests(unittest.TestCase):
    """The whole discover path (adapter -> fixpoint -> capabilities.json) via cli."""

    def _project(self, capcov_toml: str, files: dict[str, str]) -> dict:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "capcov.toml").write_text(capcov_toml)
        (root / "src").mkdir()
        for name, body in files.items():
            (root / "src" / name).write_text(body)
        out = root / "capabilities.json"
        rc = cli.main(["discover", "--target", str(root), "--out", str(out), "--quiet"])
        self.assertEqual(rc, 0)
        return json.loads(out.read_text())

    def test_structured_spec_end_to_end(self) -> None:
        doc = json.dumps({"openapi": "3.1.0", "paths": {"/items": {"get": {}}}})
        capabilities = self._project(
            '[capcov]\nadapter = "structured-spec"\nsource = "src"\n'
            'document = "openapi.json"\nprofile = "openapi"\n',
            {"openapi.json": doc},
        )
        caps = capabilities["capabilities"]
        self.assertEqual(len(caps), 1)
        self.assertEqual(caps[0]["entity"], "http:GET /items")
        self.assertEqual(caps[0]["surface"], "http:GET /items")
        self.assertEqual(caps[0]["operations"], ["read"])
        self.assertEqual(caps[0]["evidence"]["hops"], 0)
        self.assertEqual(caps[0]["evidence"]["kind"], "direct")
        # The required-but-empty carriers are written; discover does not choke.
        self.assertEqual(capabilities["blind_spots"], [])
        self.assertEqual(capabilities["residue"], [])

    @unittest.skipUnless(_HAVE_TS, "treesitter extra not installed")
    def test_treesitter_end_to_end(self) -> None:
        capcov_toml = (
            '[capcov]\n'
            'adapter = "treesitter-routes"\n'
            'source = "src"\n'
            'language = "go"\n'
            'globs = ["*.go"]\n'
            "query = '" + GO_QUERY + "'\n"
        )
        capabilities = self._project(
            capcov_toml,
            {"svc.go": 'package main\nfunc f() {\n\thandle("POST /ask", askHandler)\n}\n'},
        )
        caps = capabilities["capabilities"]
        self.assertEqual(len(caps), 1)
        self.assertEqual(caps[0]["entity"], "http:POST /ask")
        self.assertEqual(caps[0]["surface"], "http:POST /ask")
        self.assertEqual(caps[0]["operations"], ["create"])
        self.assertEqual(caps[0]["evidence"]["hops"], 0)


if __name__ == "__main__":
    unittest.main()
