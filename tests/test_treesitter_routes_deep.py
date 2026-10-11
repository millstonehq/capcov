"""The DEEP path: config-gated deep mode on the treesitter-routes adapter.

Deep mode stops self-binding and emits the node-keyed call-graph dict the native
stack adapter emits, so `scip.resolve` + `core.fixpoint` trace a route handler to
the DATA entities it touches. Four things are proven, and the first is the
invariant everything else must not break:

1. NO deep block -> `build_core_dict` byte-identical (the 384-test baseline).
2. deep + SCIP tooling absent -> a NAMED degrade to the shallow dict, never a
   crash and never a silent claim of deep.
3. deep + tooling present -> the node-keyed deep dict: `surfaces[].handler` is the
   handler's call-graph node, EVERY function is a `_calls` key (the function
   universe -- R8), `_direct`/`_ops` are node-keyed at the enclosing function, and
   `_node_locations` tags each node for the (file,line)-join.
4. that deep dict, fed through `resolve.hybrid_raw(..., deep=True)` over a real
   scip-php index, canonicalizes to SCIP nodes and binds the entities through the
   call chain -- the whole point.

Plus the CLI LANGUAGE seam: cmd_discover reads the SCIP language per-config, folds
deep when the adapter emitted a deep dict, and honors the degrade.
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from capcov import cli
from capcov.adapters import deep_core, treesitter_routes
from capcov.adapters.deep_core import build_deep_dict, node_key
from capcov.core import fixpoint
from capcov.scip import resolve as scip_resolve
from capcov.scip import runner

_HAVE_TS = (
    importlib.util.find_spec("tree_sitter") is not None
    and importlib.util.find_spec("tree_sitter_language_pack") is not None
)

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
PHP_FIXTURE = _FIXTURES / "scip_php_symbols.json"

# --- Go fixture source + the four deep queries -----------------------------

GO_ROUTE_QUERY = (
    "((call_expression function: (identifier) @fn (#eq? @fn \"handle\") "
    "arguments: (argument_list (interpreted_string_literal) @path . (identifier) @handler)))"
)
GO_FUNCTION_QUERY = (
    "[(function_declaration name: (identifier) @name) @function "
    " (method_declaration name: (field_identifier) @name) @function]"
)
GO_ENTITY_QUERY = "(type_spec name: (type_identifier) @type) @entity"
GO_OP_QUERY = (
    "(call_expression function: (selector_expression field: (field_identifier) @verb)) @op"
)

GO_SRC = """package main

func register() {
\thandle("GET /jobs/{id}", GetJob)
}

func GetJob(w int, r int) {
\trepo.Get()
\trepo.WriteAudit()
}

func (rp *Repo) Get() {
\tdb.First(&Job{})
}

func (rp *Repo) WriteAudit() {
\tdb.Create(&AuditLog{})
}
"""

_DEEP_GO_CONFIG = {
    "language": "go",
    "scip_language": "go",
    "files": ["svc.go"],
    "query": GO_ROUTE_QUERY,
    "deep": {
        "recognizer": "go_gorm",
        "function_query": GO_FUNCTION_QUERY,
        "entity_query": GO_ENTITY_QUERY,
        "op_query": GO_OP_QUERY,
    },
}


class _FakeRecognizer:
    """A stand-in for a T4 recognizer: fixed entities, verb->crud on the op sites.

    It reads the real serialized capture streams (proving the engine emitted them)
    but keeps the semantics trivial so the wiring, not a framework's rules, is what
    the state-3 tests exercise.
    """

    language = "go"
    _VERBS = {"First": ("jobs", "read"), "Create": ("audit_logs", "create")}

    def recognize_entities(self, entity_matches):
        entities = [
            {"name": "jobs", "symbol": "Job", "module": "", "file": "svc.go", "line": 1},
            {"name": "audit_logs", "symbol": "AuditLog", "module": "", "file": "svc.go", "line": 1},
        ]
        return entities, {"Job": "jobs", "AuditLog": "audit_logs"}

    def recognize_ops(self, op_matches, symbol_index):
        out = []
        for match in op_matches:
            verb = match["verb"][0]["text"]
            site = match["op"][0]
            if verb in self._VERBS:
                entity, crud = self._VERBS[verb]
                out.append({"file": site["file"], "line": site["line"], "entity": entity, "crud": crud})
        return out


def _capabilities(raw: dict) -> list[dict]:
    """cli.cmd_discover's capability assembly, reproduced for a resolved raw dict."""
    direct, calls, ops = raw["_direct"], raw["_calls"], raw["_ops"]
    caps = []
    for surface in raw["surfaces"]:
        per_root, _ = fixpoint.bind([surface["handler"]], calls, direct)
        bound = per_root.get(surface["handler"], {})
        reach = fixpoint.distances(surface["handler"], calls)
        for entity, hops in sorted(bound.items()):
            observed: set[str] = set()
            for node in reach:
                observed |= ops.get(node, {}).get(entity, set())
            caps.append(
                {"entity": entity, "surface": surface["id"], "operations": sorted(observed), "hops": hops}
            )
    return caps


class BuildDeepDictShapeTest(unittest.TestCase):
    """The node-keyed dict shape, pure -- no tree-sitter, no SCIP."""

    def _functions(self) -> list[dict]:
        return [
            {"qualname": "GetJob", "file": "a.go", "line": 5, "start_line": 5, "end_line": 8},
            {"qualname": "Repo.Get", "file": "b.go", "line": 3, "start_line": 3, "end_line": 6},
            {"qualname": "Repo.WriteAudit", "file": "b.go", "line": 9, "start_line": 9, "end_line": 12},
        ]

    def _build(self, **over) -> dict:
        kwargs = dict(
            surface_records=[
                {"id": "http:GET /jobs/{id}", "method": "GET", "path": "/jobs/{id}",
                 "handler": "GetJob", "file": "a.go", "line": 2, "module": "a.go"}
            ],
            functions=self._functions(),
            entities=[
                {"name": "jobs", "symbol": "Job", "module": "", "file": "m.go", "line": 1},
                {"name": "audit_logs", "symbol": "AuditLog", "module": "", "file": "m.go", "line": 1},
            ],
            op_sites=[
                {"file": "b.go", "line": 4, "entity": "jobs", "crud": "read"},
                {"file": "b.go", "line": 10, "entity": "audit_logs", "crud": "create"},
            ],
            excluded_surfaces={"count": 0, "surfaces": []},
            unresolved=[],
        )
        kwargs.update(over)
        return build_deep_dict(**kwargs)

    def test_handler_is_the_call_graph_node_not_the_surface_id(self) -> None:
        raw = self._build()
        surface = raw["surfaces"][0]
        expected = node_key("GetJob", "a.go", 5)
        self.assertEqual(surface["handler"], expected)
        self.assertNotEqual(surface["handler"], surface["id"])
        self.assertEqual(surface["handler_symbol"], "GetJob")
        self.assertIn(expected, raw["_direct"])

    def test_every_function_is_a_calls_key_the_function_universe(self) -> None:
        raw = self._build()
        self.assertEqual(
            set(raw["_calls"]),
            {node_key("GetJob", "a.go", 5), node_key("Repo.Get", "b.go", 3),
             node_key("Repo.WriteAudit", "b.go", 9)},
        )
        # Placeholders: SCIP replaces the edges; only the keys are load-bearing.
        self.assertTrue(all(v == set() for v in raw["_calls"].values()))

    def test_node_locations_tag_every_node_for_the_join(self) -> None:
        raw = self._build()
        self.assertEqual(
            raw["_node_locations"][node_key("Repo.Get", "b.go", 3)], ["b.go", 3]
        )
        # Every _calls / _direct key has a location (the join needs it).
        for node in set(raw["_calls"]) | set(raw["_direct"]):
            self.assertIn(node, raw["_node_locations"])

    def test_ops_and_direct_are_node_keyed_at_the_enclosing_function(self) -> None:
        raw = self._build()
        get = node_key("Repo.Get", "b.go", 3)
        write = node_key("Repo.WriteAudit", "b.go", 9)
        self.assertEqual(raw["_direct"][get], {"jobs"})
        self.assertEqual(raw["_ops"][get], {"jobs": {"read"}})
        self.assertEqual(raw["_direct"][write], {"audit_logs"})
        self.assertEqual(raw["_ops"][write], {"audit_logs": {"create"}})
        # The handler touches nothing directly, but is registered (empty set).
        self.assertEqual(raw["_direct"][node_key("GetJob", "a.go", 5)], set())

    def test_entities_are_the_data_entities_sorted(self) -> None:
        raw = self._build()
        self.assertEqual([e["name"] for e in raw["entities"]], ["audit_logs", "jobs"])

    def test_required_carriers_present_and_zero(self) -> None:
        raw = self._build()
        self.assertEqual(raw["residue"], [])
        self.assertEqual(raw["residue_summary"]["resolved_by_import"], 0)
        self.assertEqual(set(raw["residue_summary"]), {
            "resolved_by_import", "resolved_by_name", "ambiguous",
            "external", "chained", "builtin_shadowed"})
        self.assertEqual(raw["excluded_surfaces"], {"count": 0, "surfaces": []})

    def test_unmatched_handler_is_named_not_silent(self) -> None:
        raw = self._build(
            surface_records=[
                {"id": "http:GET /ghost", "method": "GET", "path": "/ghost",
                 "handler": "NoSuchFunc", "file": "a.go", "line": 2, "module": "a.go"}
            ]
        )
        ghosts = [u for u in raw["unresolved"] if u.get("kind") == "deep-handler-unresolved"]
        self.assertEqual(len(ghosts), 1)
        # The surface still has a rooted (if unbindable) handler node -- never None.
        self.assertTrue(raw["surfaces"][0]["handler"])

    def test_op_site_outside_any_function_is_named_not_dropped(self) -> None:
        raw = self._build(
            op_sites=[{"file": "b.go", "line": 999, "entity": "jobs", "crud": "read"}]
        )
        unattributed = [u for u in raw["unresolved"] if u.get("kind") == "deep-op-unattributed"]
        self.assertEqual(len(unattributed), 1)


@unittest.skipUnless(_HAVE_TS, "deep join census needs the treesitter extra")
class DeepDictJoinsThroughScipTest(unittest.TestCase):
    """build_deep_dict's output, fed through the real (file,line)-join over a
    dumped scip-php index, binds the entities through the resolved call chain."""

    def _php_normalized(self) -> dict:
        return runner.normalize_scip_json(json.loads(PHP_FIXTURE.read_text()))

    def _deep_dict(self) -> dict:
        controller = "app/Http/Controllers/JobController.php"
        repo = "app/Repositories/JobRepository.php"
        return build_deep_dict(
            surface_records=[
                {"id": "http:GET /jobs/{id}", "method": "GET", "path": "/jobs/{id}",
                 "handler": "JobController.getJob", "file": controller, "line": 5,
                 "module": controller}
            ],
            # def lines match the scip-php fixture's definition occurrences.
            functions=[
                {"qualname": "JobController.getJob", "file": controller, "line": 16,
                 "start_line": 16, "end_line": 20},
                {"qualname": "JobRepository.get", "file": repo, "line": 9,
                 "start_line": 9, "end_line": 12},
                {"qualname": "JobRepository.writeAudit", "file": repo, "line": 15,
                 "start_line": 15, "end_line": 18},
            ],
            entities=[
                {"name": "jobs", "symbol": "Job", "module": "", "file": "app/Models/Job.php", "line": 5},
                {"name": "audit_logs", "symbol": "AuditLog", "module": "", "file": "app/Models/AuditLog.php", "line": 5},
            ],
            op_sites=[
                {"file": repo, "line": 10, "entity": "jobs", "crud": "read"},
                {"file": repo, "line": 16, "entity": "audit_logs", "crud": "create"},
            ],
            excluded_surfaces={"count": 0, "surfaces": []},
            unresolved=[],
            scip_language="php",
        )

    def test_provisional_nodes_canonicalize_and_bind_the_entities(self) -> None:
        hybrid = scip_resolve.hybrid_raw(
            self._deep_dict(), self._php_normalized(), "/tmp/does-not-exist",
            language="php", deep=True,
        )
        handler = "App\\Http\\Controllers:JobController.getJob"
        self.assertEqual(hybrid["surfaces"][0]["handler"], handler)
        # _calls is the SCIP-resolved graph in canonical node space.
        self.assertEqual(
            hybrid["_calls"][handler],
            {"App\\Repositories:JobRepository.get", "App\\Repositories:JobRepository.writeAudit"},
        )
        caps = _capabilities(hybrid)
        by_entity = {c["entity"]: c for c in caps}
        self.assertEqual(by_entity["jobs"]["surface"], "http:GET /jobs/{id}")
        self.assertEqual(by_entity["jobs"]["operations"], ["read"])
        self.assertEqual(by_entity["audit_logs"]["operations"], ["create"])
        # The entity axis names DATA entities, not the route echoing itself.
        self.assertNotIn("http:GET /jobs/{id}", by_entity)


@unittest.skipUnless(_HAVE_TS, "treesitter extra not installed")
class SelectionRuleTests(unittest.TestCase):
    """The three-state selection rule inside treesitter_routes.discover."""

    def _discover(self, files: dict[str, str], config: dict) -> dict:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for name, body in files.items():
            (root / name).write_text(body)
        return treesitter_routes.discover(root, root, config=config)

    def test_no_deep_block_is_byte_identical_to_the_shallow_builder(self) -> None:
        # State 1: the shallow self-bound dict. No _node_locations, empty _calls,
        # handler == surface id -- the exact shape build_core_dict emits.
        raw = self._discover(
            {"svc.go": GO_SRC},
            {"language": "go", "files": ["svc.go"], "query": GO_ROUTE_QUERY},
        )
        self.assertNotIn("_node_locations", raw)
        self.assertEqual(raw["_calls"], {})
        surface = raw["surfaces"][0]
        self.assertEqual(surface["handler"], surface["id"])
        self.assertEqual(raw["_direct"][surface["id"]], {surface["id"]})

    def test_deep_without_tooling_degrades_named_never_crashes(self) -> None:
        # State 2: tooling absent -> shallow dict + a NAMED deep-unavailable reason.
        original = scip_resolve.tools_available
        scip_resolve.tools_available = lambda language: False
        try:
            raw = self._discover({"svc.go": GO_SRC}, dict(_DEEP_GO_CONFIG))
        finally:
            scip_resolve.tools_available = original
        self.assertNotIn("_node_locations", raw)
        self.assertEqual(raw["_calls"], {})
        degrade = [u for u in raw["unresolved"] if u.get("kind") == "deep-unavailable"]
        self.assertEqual(len(degrade), 1)
        self.assertIn("go", degrade[0]["reason"])
        # Still a usable shallow capability set -- the fallback holds.
        surface = raw["surfaces"][0]
        self.assertEqual(surface["handler"], surface["id"])

    def test_deep_with_tooling_emits_the_node_keyed_dict(self) -> None:
        # State 3: tooling present + a recognizer -> the node-keyed deep dict.
        orig_tools = scip_resolve.tools_available
        orig_load = deep_core.load_recognizer
        scip_resolve.tools_available = lambda language: True
        deep_core.load_recognizer = lambda name: _FakeRecognizer()
        try:
            raw = self._discover({"svc.go": GO_SRC}, dict(_DEEP_GO_CONFIG))
        finally:
            scip_resolve.tools_available = orig_tools
            deep_core.load_recognizer = orig_load

        self.assertIn("_node_locations", raw)
        # The function universe: a _calls key for register, GetJob, Get, WriteAudit.
        self.assertEqual(len(raw["_calls"]), 4)
        # The handler is a call-graph node (ts: provisional namespace), not the id.
        surface = raw["surfaces"][0]
        self.assertEqual(surface["id"], "http:GET /jobs/{id}")
        self.assertTrue(surface["handler"].startswith("ts:GetJob@"))
        self.assertIn(surface["handler"], raw["_node_locations"])
        # Data-access sites bound at their enclosing function nodes.
        touched = {frozenset(v): k for k, v in raw["_direct"].items() if v}
        self.assertIn(frozenset({"jobs"}), touched)
        self.assertIn(frozenset({"audit_logs"}), touched)
        get_node = touched[frozenset({"jobs"})]
        self.assertEqual(raw["_ops"][get_node], {"jobs": {"read"}})
        self.assertEqual({e["name"] for e in raw["entities"]}, {"jobs", "audit_logs"})


@unittest.skipUnless(_HAVE_TS, "treesitter extra not installed")
class CliLanguageSeamTests(unittest.TestCase):
    """The one cmd_discover change: the SCIP language read per-config, deep folded
    when the adapter emitted a deep dict, and the degrade honored."""

    def _project(self, adapters_toml: str, files: dict[str, str], extra_argv: list[str]) -> tuple[int, Path]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "capcov.toml").write_text(adapters_toml)
        (root / "src").mkdir()
        for name, body in files.items():
            (root / "src" / name).write_text(body)
        out = root / "capabilities.json"
        rc = cli.main(["discover", "--target", str(root), "--out", str(out), "--quiet", *extra_argv])
        return rc, out

    _DEEP_TOML = (
        "[[adapters]]\n"
        'name = "treesitter-routes"\n'
        'language = "go"\n'
        'scip_language = "go"\n'
        'files = ["svc.go"]\n'
        f"query = '{GO_ROUTE_QUERY}'\n"
        "[adapters.deep]\n"
        'recognizer = "go_gorm"\n'
        f"function_query = '{GO_FUNCTION_QUERY}'\n"
        f"op_query = '{GO_OP_QUERY}'\n"
        f"entity_query = '{GO_ENTITY_QUERY}'\n"
    )

    def _stub_resolve(self):
        calls = []

        def stub(source, raw, *, language, deep, timeout=600):
            calls.append({"language": language, "deep": deep})
            out = dict(raw)
            out["resolver"] = "scip"
            out["scip_resolved_edges"] = []
            out["scip_entities"] = []
            out["scip_residue"] = []
            out["scip_residue_summary"] = {
                "scip_resolved_edges": 0, "scip_rooted_edges": 0,
                "ast_call_sites": 0, "unresolved_enumerated": 0}
            return out

        stub.calls = calls
        return stub

    def test_resolver_scip_reads_language_from_config_and_folds_deep(self) -> None:
        stub = self._stub_resolve()
        orig_tools, orig_load, orig_resolve = (
            scip_resolve.tools_available, deep_core.load_recognizer, scip_resolve.resolve)
        scip_resolve.tools_available = lambda language: True
        deep_core.load_recognizer = lambda name: _FakeRecognizer()
        scip_resolve.resolve = stub
        try:
            rc, out = self._project(self._DEEP_TOML, {"svc.go": GO_SRC}, ["--resolver", "scip"])
        finally:
            scip_resolve.tools_available = orig_tools
            deep_core.load_recognizer = orig_load
            scip_resolve.resolve = orig_resolve
        self.assertEqual(rc, 0)
        self.assertEqual(len(stub.calls), 1)
        self.assertEqual(stub.calls[0]["language"], "go")
        self.assertTrue(stub.calls[0]["deep"])
        # _node_locations never leaks into the artifact.
        self.assertNotIn("_node_locations", json.loads(out.read_text()))

    def test_language_falls_back_to_adapter_tagged_when_config_omits_it(self) -> None:
        # No scip_language key: the adapter tags the deep dict from `language`, and
        # cmd_discover reads it off the dict.
        toml = self._DEEP_TOML.replace('scip_language = "go"\n', "")
        stub = self._stub_resolve()
        orig_tools, orig_load, orig_resolve = (
            scip_resolve.tools_available, deep_core.load_recognizer, scip_resolve.resolve)
        scip_resolve.tools_available = lambda language: True
        deep_core.load_recognizer = lambda name: _FakeRecognizer()
        scip_resolve.resolve = stub
        try:
            rc, _ = self._project(toml, {"svc.go": GO_SRC}, ["--resolver", "scip"])
        finally:
            scip_resolve.tools_available = orig_tools
            deep_core.load_recognizer = orig_load
            scip_resolve.resolve = orig_resolve
        self.assertEqual(rc, 0)
        self.assertEqual(stub.calls[0]["language"], "go")

    def test_degrade_skips_the_scip_fold_and_still_succeeds(self) -> None:
        stub = self._stub_resolve()
        orig_tools, orig_resolve = scip_resolve.tools_available, scip_resolve.resolve
        scip_resolve.tools_available = lambda language: False
        scip_resolve.resolve = stub
        try:
            rc, out = self._project(self._DEEP_TOML, {"svc.go": GO_SRC}, ["--resolver", "scip"])
        finally:
            scip_resolve.tools_available = orig_tools
            scip_resolve.resolve = orig_resolve
        self.assertEqual(rc, 0)
        # resolve() was NOT called -- the adapter already degraded and named why.
        self.assertEqual(stub.calls, [])
        doc = json.loads(out.read_text())
        degrade = [u for u in doc.get("unresolved", []) if u.get("kind") == "deep-unavailable"]
        self.assertEqual(len(degrade), 1)

    def test_deep_block_without_resolver_scip_is_named_not_silent(self) -> None:
        orig_tools, orig_load = scip_resolve.tools_available, deep_core.load_recognizer
        scip_resolve.tools_available = lambda language: True
        deep_core.load_recognizer = lambda name: _FakeRecognizer()
        try:
            with self.assertRaises(SystemExit) as ctx:
                self._project(self._DEEP_TOML, {"svc.go": GO_SRC}, [])
        finally:
            scip_resolve.tools_available = orig_tools
            deep_core.load_recognizer = orig_load
        self.assertIn("--resolver scip", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
