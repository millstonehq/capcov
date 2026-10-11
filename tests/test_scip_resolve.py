"""Tests for the resolver seam that folds SCIP into the entity pipeline.

The pure tests run against the checked-in normalized fixture and hand-built
inputs -- no node/go tooling, the same way the mapper and differ are tested. They
cover the whole seam: SCIP symbol -> AST node translation, the fixpoint ``calls``
graph, the residue subtraction, and the hybrid assembly. The one live test
indexes a real tree with scip-python + the scip CLI and skips cleanly when either
is absent.
"""

from __future__ import annotations

import importlib.util
import json
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from capcov.adapters import python_fastapi_sqlalchemy as adapter
from capcov.core import fixpoint
from capcov.scip import resolve
from capcov.scip import runner

from .support import APP, Project

FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "scip_normalized_router_service.json"
)
_FIXTURES = Path(__file__).resolve().parent / "fixtures"
# Real `scip print --json` dumps: scip-go 0.2.7 over a NESTED-package module and
# scip-php (davidrjenni, dev-main) over a Laravel-shaped controller->repo->model
# slice. These are the crux: the symbol->node half is proven on real go/php
# symbols, not synthesized ones, because a whole-language translation failure
# reads as a green run and the python differential oracle cannot catch it.
GO_FIXTURE = _FIXTURES / "scip_go_nested_symbols.json"
PHP_FIXTURE = _FIXTURES / "scip_php_symbols.json"

# The deep go/php hybrid reads the source tree for its call-site census through
# the per-language blind-spot enumerator, whose go/php inventory is a tree-sitter
# parse. That path is the tree-sitter adapter's own -- a deep go/php ast_raw
# cannot be produced without the extra in the first place -- so the deep-join
# tests below require it and skip cleanly when it is absent. The python path
# (ast, stdlib) is unaffected.
_HAVE_TS = (
    importlib.util.find_spec("tree_sitter") is not None
    and importlib.util.find_spec("tree_sitter_language_pack") is not None
)

_PY = "scip-python python spike 0.0.1 "


class SymbolTranslationTest(unittest.TestCase):
    def test_a_plain_function_symbol_becomes_module_qualname(self) -> None:
        self.assertEqual(
            resolve.scip_symbol_to_node(_PY + "`shop.router`/make_job()."),
            "shop.router:make_job",
        )

    def test_a_method_keeps_its_enclosing_class_in_the_qualname(self) -> None:
        # The AST walker builds `module:Class.method`; the `#` (type) and `().`
        # (method) descriptors must dot-join to exactly that.
        self.assertEqual(
            resolve.scip_symbol_to_node(_PY + "`app.repo`/Repo#fetch()."),
            "app.repo:Repo.fetch",
        )

    def test_a_nested_function_keeps_its_factory_scope(self) -> None:
        # Matches the adapter's `app.main:create_app.healthz` for a handler
        # defined inside an app factory.
        self.assertEqual(
            resolve.scip_symbol_to_node(_PY + "`app.main`/create_app().healthz()."),
            "app.main:create_app.healthz",
        )

    def test_a_type_symbol_translates_too(self) -> None:
        self.assertEqual(
            resolve.scip_symbol_to_node(_PY + "`shop.models`/Job#"),
            "shop.models:Job",
        )

    def test_a_parameter_or_local_or_meta_is_not_a_node(self) -> None:
        # A parameter descriptor, a scip local, and a module meta symbol are not
        # fixpoint nodes -- each yields None so its edge is dropped, never a
        # fabricated node.
        self.assertIsNone(
            resolve.scip_symbol_to_node(_PY + "`shop.router`/make_job().(session)")
        )
        self.assertIsNone(resolve.scip_symbol_to_node("local 0"))
        self.assertIsNone(resolve.scip_symbol_to_node(_PY + "shop/__init__:"))
        self.assertIsNone(resolve.scip_symbol_to_node(None))
        self.assertIsNone(resolve.scip_symbol_to_node(""))


def _definition_nodes(normalized: dict, language: str) -> tuple[dict, list]:
    """(node -> the one symbol that produced it, list of collisions) over every
    DEFINITION symbol in a normalized index, for ``language``."""
    to_node = resolve.normalizer(language).symbol_to_node
    by_node: dict[str, str] = {}
    collisions: list[tuple] = []
    for doc in normalized["documents"]:
        for occ in doc["occurrences"]:
            if not occ["is_definition"]:
                continue
            node = to_node(occ["symbol"])
            if node is None:
                continue
            if node in by_node and by_node[node] != occ["symbol"]:
                collisions.append((node, occ["symbol"], by_node[node]))
            by_node.setdefault(node, occ["symbol"])
    return by_node, collisions


class NormalizerStrategyTest(unittest.TestCase):
    """The per-indexer normalizer: one parse, a per-language namespace separator.
    The single Python-shaped translator silently mistranslated the others; this
    pins each language's node spelling (the co-design contract T3 reproduces)."""

    def test_python_dotted_module_unchanged(self) -> None:
        n = resolve.normalizer("python")
        self.assertEqual(
            n.symbol_to_node(_PY + "`shop.router`/make_job()."), "shop.router:make_job"
        )
        # scip-python's other observed form -- bare slash namespaces -- yields the
        # same dotted module, so both forms co-design to module.dotted:qualname.
        self.assertEqual(
            n.symbol_to_node(_PY + "app/models/Job#"), "app.models:Job"
        )

    def test_go_import_path_is_the_package(self) -> None:
        # scip-go single-backticks the FULL import path; the go normalizer keeps
        # it verbatim as the package (slash-joined) -> import/path:Recv.Method.
        go = "scip-go gomod github.com/example/gonest . "
        self.assertEqual(
            resolve.normalizer("go").symbol_to_node(
                go + "`github.com/example/gonest/internal/jobs`/Repo#Get()."
            ),
            "github.com/example/gonest/internal/jobs:Repo.Get",
        )

    def test_php_bare_namespaces_are_the_package(self) -> None:
        # scip-php emits BARE namespace descriptors and no backticks: the crux the
        # single Python translator dropped to None. The php normalizer consumes
        # all leading namespaces (backslash-joined) -> Namespace\Class.method.
        php = "scip-php composer example/php-slice 1.0.0.0 "
        self.assertEqual(
            resolve.normalizer("php").symbol_to_node(
                php + "App/Http/Controllers/JobController#getJob()."
            ),
            "App\\Http\\Controllers:JobController.getJob",
        )

    def test_one_translator_cannot_serve_two_indexers_is_the_crux(self) -> None:
        # The crux, made concrete: the SAME php symbol translated with the wrong
        # language's normalizer yields a DIFFERENT node spelling (python dot-joins
        # the namespace, php backslash-joins it). The dotted spelling never
        # matches the php handler adapter's backslash co-design key, so every edge
        # is silently mis-keyed -- a green run. Per-indexer selection is what makes
        # the two sides agree; the php normalizer is the co-design spelling.
        php = "scip-php composer example/php-slice 1.0.0.0 App/Repositories/JobRepository#get()."
        wrong = resolve.normalizer("python").symbol_to_node(php)
        right = resolve.normalizer("php").symbol_to_node(php)
        self.assertEqual(right, "App\\Repositories:JobRepository.get")
        self.assertNotEqual(wrong, right, "the wrong normalizer must not silently agree")
        self.assertEqual(wrong, "App.Repositories:JobRepository.get")

    def test_an_unknown_language_is_rejected_not_silently_python(self) -> None:
        with self.assertRaises(ValueError):
            resolve.normalizer("ruby")


class GoNestedSymbolResolveTest(unittest.TestCase):
    """symbol->node over the REAL scip-go nested-package dump: non-None,
    collision-free, byte-equal to the co-design node for every definition; and
    the resolved call graph drops no in-project edge across packages+files."""

    def setUp(self) -> None:
        from capcov.scip import map as scip_map

        self.scip_map = scip_map
        self.normalized = runner.normalize_scip_json(json.loads(GO_FIXTURE.read_text()))

    def test_every_node_shaped_definition_resolves_and_is_collision_free(self) -> None:
        by_node, collisions = _definition_nodes(self.normalized, "go")
        self.assertEqual(collisions, [], "two definitions must not share a node id")
        to_node = resolve.normalizer("go").symbol_to_node
        for doc in self.normalized["documents"]:
            for occ in doc["occurrences"]:
                if not occ["is_definition"]:
                    continue
                sym = occ["symbol"]
                node = to_node(sym)
                # None is allowed ONLY for a non-node symbol: a local, a bare
                # package (trailing `/`), or a parameter (trailing `)`).
                if node is None:
                    self.assertTrue(
                        sym.startswith("local ")
                        or sym.rstrip().endswith("/")
                        or sym.rstrip().endswith(")"),
                        f"a node-shaped definition translated to None: {sym!r}",
                    )

    def test_the_co_design_node_spellings_are_pinned(self) -> None:
        by_node, _ = _definition_nodes(self.normalized, "go")
        pkg = "github.com/example/gonest"
        for expected in (
            f"{pkg}/api:Handler.GetJob",
            f"{pkg}/internal/jobs:Service.Fetch",
            f"{pkg}/internal/jobs:Repo.Get",
            f"{pkg}/internal/jobs:Repo.Write",
            f"{pkg}/internal/jobs:Job",
            f"{pkg}/internal/audit:AuditLog",
            f"{pkg}/internal/audit:Record",
        ):
            self.assertIn(expected, by_node)

    def test_calls_graph_drops_no_in_project_edge(self) -> None:
        edges = self.scip_map.call_edges(self.normalized)
        to_node = resolve.normalizer("go").symbol_to_node
        in_project = [
            e for e in edges if to_node(e["caller"]) and to_node(e["callee"])
        ]
        calls = resolve.calls_graph(edges, language="go")
        rooted = sum(len(v) for v in calls.values())
        self.assertEqual(
            rooted, len(in_project), "every in-project edge must survive translation"
        )
        pkg = "github.com/example/gonest"
        # the cross-file, cross-package chain GetJob -> Service.Fetch -> Repo.Get
        self.assertIn(
            f"{pkg}/internal/jobs:Service.Fetch",
            calls[f"{pkg}/api:Handler.GetJob"],
        )
        self.assertIn(
            f"{pkg}/internal/jobs:Repo.Get",
            calls[f"{pkg}/internal/jobs:Service.Fetch"],
        )


class PhpSymbolResolveTest(unittest.TestCase):
    """symbol->node over the REAL scip-php dump. scip-php's bare namespaces are
    the documented crux (every edge -> None before the split); scip-php also
    emits NO enclosing range, so the runner synthesizes one or map attributes no
    caller. Both are proven here on the controller->repo call chain."""

    def setUp(self) -> None:
        from capcov.scip import map as scip_map

        self.scip_map = scip_map
        self.normalized = runner.normalize_scip_json(
            json.loads(PHP_FIXTURE.read_text())
        )

    def test_every_node_shaped_definition_resolves_and_is_collision_free(self) -> None:
        by_node, collisions = _definition_nodes(self.normalized, "php")
        self.assertEqual(collisions, [], "two definitions must not share a node id")
        to_node = resolve.normalizer("php").symbol_to_node
        for doc in self.normalized["documents"]:
            for occ in doc["occurrences"]:
                if not occ["is_definition"]:
                    continue
                sym = occ["symbol"]
                if to_node(sym) is None:
                    # None allowed only for a parameter (a `().($p)` symbol).
                    self.assertTrue(
                        sym.rstrip().endswith(")"),
                        f"a node-shaped definition translated to None: {sym!r}",
                    )

    def test_the_co_design_node_spellings_are_pinned(self) -> None:
        by_node, _ = _definition_nodes(self.normalized, "php")
        for expected in (
            "App\\Http\\Controllers:JobController.getJob",
            "App\\Repositories:JobRepository.get",
            "App\\Repositories:JobRepository.writeAudit",
            "App\\Models:Job",
            "App\\Models:AuditLog",
        ):
            self.assertIn(expected, by_node)

    def test_calls_graph_drops_no_in_project_edge(self) -> None:
        # scip-php gives no enclosing range; the runner synthesized callable spans,
        # so map attributes each call to getJob and NO in-project edge is dropped.
        edges = self.scip_map.call_edges(self.normalized)
        to_node = resolve.normalizer("php").symbol_to_node
        in_project = [
            e for e in edges if to_node(e["caller"]) and to_node(e["callee"])
        ]
        self.assertTrue(in_project, "the controller->repo calls must be edges at all")
        calls = resolve.calls_graph(edges, language="php")
        rooted = sum(len(v) for v in calls.values())
        self.assertEqual(rooted, len(in_project))
        handler = "App\\Http\\Controllers:JobController.getJob"
        self.assertEqual(
            calls[handler],
            {
                "App\\Repositories:JobRepository.get",
                "App\\Repositories:JobRepository.writeAudit",
            },
        )


class ScipDefsByLocationTest(unittest.TestCase):
    """The (file,line)-join binding of record: SCIP owns the node spelling at
    each definition site, keyed 1-based to match the adapter's node locations."""

    def test_defs_are_keyed_by_one_based_location_to_the_scip_node(self) -> None:
        normalized = runner.normalize_scip_json(json.loads(PHP_FIXTURE.read_text()))
        defs = resolve.scip_defs_by_location(normalized, language="php")
        # getJob is defined at SCIP 0-based line 15 -> AST 1-based 16.
        self.assertEqual(
            defs[("app/Http/Controllers/JobController.php", 16)],
            "App\\Http\\Controllers:JobController.getJob",
        )
        # get() at 0-based 8 -> 1-based 9.
        self.assertEqual(
            defs[("app/Repositories/JobRepository.php", 9)],
            "App\\Repositories:JobRepository.get",
        )


class DeepHybridJoinTest(unittest.TestCase):
    """The deep hybrid: provisional adapter node keys are canonicalized against
    SCIP's own definition set, node_keys is seeded from SCIP defs (so a mid-chain
    node the adapter never registered still survives is_node), and a location
    with no SCIP definition becomes a NAMED unresolved -- never a silent drop."""

    def _php(self) -> dict:
        return runner.normalize_scip_json(json.loads(PHP_FIXTURE.read_text()))

    def _ast_raw(self) -> dict:
        # A provisional deep dict as the tree-sitter adapter would emit it: nodes
        # keyed provisionally and tagged with each definition's (file, 1-based
        # line). It deliberately does NOT register the Repo.get node -- the SCIP
        # def seed must supply it or the getJob->get edge would fail is_node.
        return {
            "_direct": {"prov:getJob": set(), "prov:writeAudit": {"audit_logs"}},
            "_ops": {"prov:writeAudit": {"audit_logs": {"create"}}},
            "_calls": {"prov:getJob": {"prov:writeAudit"}},
            "surfaces": [
                {"id": "http:GET /jobs/{id}", "handler": "prov:getJob"}
            ],
            "_node_locations": {
                "prov:getJob": ["app/Http/Controllers/JobController.php", 16],
                "prov:writeAudit": ["app/Repositories/JobRepository.php", 15],
                "prov:ghost": ["app/Nowhere.php", 999],
            },
            "unresolved": [],
        }

    @unittest.skipUnless(_HAVE_TS, "deep go/php hybrid census needs the treesitter extra")
    def test_provisional_keys_are_canonicalized_to_scip_nodes(self) -> None:
        hybrid = resolve.hybrid_raw(
            self._ast_raw(), self._php(), "/tmp/phpslice", language="php", deep=True
        )
        handler = "App\\Http\\Controllers:JobController.getJob"
        self.assertEqual(hybrid["surfaces"][0]["handler"], handler)
        self.assertIn(handler, hybrid["_direct"])
        self.assertIn("App\\Repositories:JobRepository.writeAudit", hybrid["_ops"])

    @unittest.skipUnless(_HAVE_TS, "deep go/php hybrid census needs the treesitter extra")
    def test_node_keys_seeded_from_scip_defs_keep_the_mid_chain_edge(self) -> None:
        hybrid = resolve.hybrid_raw(
            self._ast_raw(), self._php(), "/tmp/phpslice", language="php", deep=True
        )
        handler = "App\\Http\\Controllers:JobController.getJob"
        # Repo.get was never a provisional adapter node; it is a known node only
        # because node_keys was seeded from SCIP's definitions (R8).
        self.assertEqual(
            hybrid["_calls"][handler],
            {
                "App\\Repositories:JobRepository.get",
                "App\\Repositories:JobRepository.writeAudit",
            },
        )

    @unittest.skipUnless(_HAVE_TS, "deep go/php hybrid census needs the treesitter extra")
    def test_a_location_with_no_scip_def_is_named_not_dropped(self) -> None:
        hybrid = resolve.hybrid_raw(
            self._ast_raw(), self._php(), "/tmp/phpslice", language="php", deep=True
        )
        ghosts = [u for u in hybrid["unresolved"] if u.get("node") == "prov:ghost"]
        self.assertEqual(len(ghosts), 1)
        self.assertEqual(ghosts[0]["kind"], "deep-unresolved")
        self.assertTrue(ghosts[0]["reason"], "an unresolved node is named, not dropped")

    def test_shallow_path_is_byte_identical_without_a_deep_flag(self) -> None:
        # The shallow python path must not shift: same inputs, deep defaulting off.
        with Project(HybridAssemblyTest.FILES) as project:
            ast_raw = adapter.discover(project.source, project.root)
            shallow = resolve.hybrid_raw(
                ast_raw, HybridAssemblyTest()._normalized(), project.source
            )
        self.assertNotIn("deep", shallow)
        self.assertEqual(
            shallow["_calls"], {"app.router:make_job": {"app.service:create_job"}}
        )


class CallsGraphFromFixtureTest(unittest.TestCase):
    """The seam over the real scip-python fixture: the cross-file edge the
    mapper recovered must become a fixpoint `calls` entry in AST node space."""

    def setUp(self) -> None:
        self.normalized = json.loads(FIXTURE.read_text())
        from capcov.scip import map as scip_map

        self.edges = scip_map.call_edges(self.normalized)

    def test_calls_graph_is_the_cross_file_edge_in_ast_node_space(self) -> None:
        calls = resolve.calls_graph(self.edges)
        self.assertEqual(
            calls, {"shop.router:make_job": {"shop.service:create_job"}}
        )

    def test_resolved_sites_lift_scip_zero_based_lines_to_one_based(self) -> None:
        # The one call reference sits at SCIP 0-based line 4; the AST pass and the
        # residue speak 1-based, so it must surface as line 5.
        self.assertEqual(
            resolve.resolved_sites(self.edges),
            [{"file": "shop/router.py", "line": 5}],
        )

    def test_is_node_filter_drops_edges_outside_the_ast_namespace(self) -> None:
        # An edge whose endpoints are not known AST nodes (an external library)
        # is dropped, exactly as the AST pass drops external calls.
        calls = resolve.calls_graph(self.edges, is_node={"nothing"}.__contains__)
        self.assertEqual(calls, {})


class HybridAssemblyTest(unittest.TestCase):
    """hybrid_raw over a controlled tree + a matching hand-built SCIP index:
    _calls is re-sourced from SCIP, the AST halves are preserved, and the residue
    names the site SCIP stayed silent about."""

    FILES = {
        "service.py": "def create_job(session, name):\n    return name\n",
        "router.py": textwrap.dedent(
            """\
            from app import service


            def make_job(session, name):
                handler = getattr(service, name)
                return service.create_job(session, name)
            """
        ),
    }

    def _normalized(self) -> dict:
        svc = _PY + "`app.service`/"
        rtr = _PY + "`app.router`/"
        return {
            "documents": [
                {
                    "path": "app/service.py",
                    "symbols": [
                        {"symbol": svc + "create_job().", "kind": "method",
                         "display_name": None}
                    ],
                    "occurrences": [
                        {"symbol": svc + "create_job().", "is_definition": True,
                         "start_line": 0, "start_col": 4,
                         "enclosing_start_line": 0, "enclosing_end_line": 1},
                    ],
                },
                {
                    "path": "app/router.py",
                    "symbols": [
                        {"symbol": rtr + "make_job().", "kind": "method",
                         "display_name": None}
                    ],
                    "occurrences": [
                        {"symbol": rtr + "make_job().", "is_definition": True,
                         "start_line": 3, "start_col": 4,
                         "enclosing_start_line": 3, "enclosing_end_line": 5},
                        # the resolved call: service.create_job(...) at 1-based line 6
                        {"symbol": svc + "create_job().", "is_definition": False,
                         "start_line": 5, "start_col": 11,
                         "enclosing_start_line": None, "enclosing_end_line": None},
                    ],
                },
            ]
        }

    def test_hybrid_resources_the_call_graph_and_keeps_the_ast_halves(self) -> None:
        with Project(self.FILES) as project:
            ast_raw = adapter.discover(project.source, project.root)
            hybrid = resolve.hybrid_raw(ast_raw, self._normalized(), project.source)

        # _calls is now the SCIP-resolved graph, in AST node space.
        self.assertEqual(
            hybrid["_calls"], {"app.router:make_job": {"app.service:create_job"}}
        )
        # the AST halves are preserved unchanged.
        self.assertEqual(hybrid["_direct"], ast_raw["_direct"])
        self.assertEqual(hybrid["surfaces"], ast_raw["surfaces"])
        self.assertEqual(hybrid["blind_spots"], ast_raw["blind_spots"])
        self.assertEqual(hybrid["resolver"], "scip")

    def test_the_residue_names_the_site_scip_left_unresolved(self) -> None:
        with Project(self.FILES) as project:
            ast_raw = adapter.discover(project.source, project.root)
            hybrid = resolve.hybrid_raw(ast_raw, self._normalized(), project.source)

        # SCIP resolved service.create_job (line 6) and stayed silent about the
        # getattr (line 5). The residue recovers that silent gap, named.
        residue = {(r["file"], r["line"]) for r in hybrid["scip_residue"]}
        self.assertIn(("app/router.py", 5), residue)
        self.assertNotIn(("app/router.py", 6), residue)
        for site in hybrid["scip_residue"]:
            self.assertFalse(site["resolved"])
            self.assertTrue(site["reason"], "an unresolved site is named, not dropped")

    def test_the_summary_reports_both_halves_never_a_bare_number(self) -> None:
        with Project(self.FILES) as project:
            ast_raw = adapter.discover(project.source, project.root)
            hybrid = resolve.hybrid_raw(ast_raw, self._normalized(), project.source)

        s = hybrid["scip_residue_summary"]
        self.assertEqual(s["scip_resolved_edges"], 1)
        self.assertEqual(s["scip_rooted_edges"], 1)
        self.assertGreaterEqual(s["unresolved_enumerated"], 1)
        self.assertGreaterEqual(s["ast_call_sites"], s["unresolved_enumerated"])
        # the full resolved edge list survives whole in the artifact.
        self.assertEqual(len(hybrid["scip_resolved_edges"]), 1)


class HybridBeatsAstAmbiguityTest(unittest.TestCase):
    """The whole point of the hybrid, on ONE fixture.

    Two modules define a ``persist`` method; a caller invokes it on an untyped
    parameter (``store.persist(job)``). The AST name-match cannot type ``store``
    and finds TWO project candidates, so it declares the cross-file call
    *ambiguous* and roots no edge -- the honest AST failure this hybrid exists to
    fix. SCIP, type-aware, resolves that same call to the one correct target. And
    a ``getattr(store, name)`` on the very next line has no static target for ANY
    resolver: SCIP emits nothing for it, so it must still surface, named, in the
    residue. Both properties hold together or the hybrid is pointless -- a
    resolver that hides its gaps is only safe paired with the AST enumerator.
    """

    FILES = {
        "alpha.py": "class Alpha:\n    def persist(self, job):\n        return job\n",
        "beta.py": "class Beta:\n    def persist(self, job):\n        return job\n",
        # line 1 def; line 2 getattr (blind); line 3 the ambiguous cross-file call
        "router.py": textwrap.dedent(
            """\
            def make_job(store, name, job):
                handler = getattr(store, name)
                return store.persist(job)
            """
        ),
    }

    def _normalized(self) -> dict:
        al = _PY + "`app.alpha`/"
        bt = _PY + "`app.beta`/"
        rt = _PY + "`app.router`/"
        return {
            "documents": [
                {
                    "path": "app/alpha.py",
                    "symbols": [
                        {"symbol": al + "Alpha#", "kind": "type", "display_name": None},
                        {"symbol": al + "Alpha#persist().", "kind": "method",
                         "display_name": None},
                    ],
                    "occurrences": [
                        {"symbol": al + "Alpha#", "is_definition": True,
                         "start_line": 0, "start_col": 6,
                         "enclosing_start_line": 0, "enclosing_end_line": 2},
                        {"symbol": al + "Alpha#persist().", "is_definition": True,
                         "start_line": 1, "start_col": 8,
                         "enclosing_start_line": 1, "enclosing_end_line": 2},
                    ],
                },
                {
                    "path": "app/beta.py",
                    "symbols": [
                        {"symbol": bt + "Beta#", "kind": "type", "display_name": None},
                        {"symbol": bt + "Beta#persist().", "kind": "method",
                         "display_name": None},
                    ],
                    "occurrences": [
                        {"symbol": bt + "Beta#", "is_definition": True,
                         "start_line": 0, "start_col": 6,
                         "enclosing_start_line": 0, "enclosing_end_line": 2},
                        {"symbol": bt + "Beta#persist().", "is_definition": True,
                         "start_line": 1, "start_col": 8,
                         "enclosing_start_line": 1, "enclosing_end_line": 2},
                    ],
                },
                {
                    "path": "app/router.py",
                    "symbols": [
                        {"symbol": rt + "make_job().", "kind": "method",
                         "display_name": None}
                    ],
                    "occurrences": [
                        {"symbol": rt + "make_job().", "is_definition": True,
                         "start_line": 0, "start_col": 4,
                         "enclosing_start_line": 0, "enclosing_end_line": 2},
                        # SCIP typed `store` as Alpha and bound store.persist ->
                        # Alpha.persist at 0-based line 2 (== AST 1-based line 3).
                        {"symbol": al + "Alpha#persist().", "is_definition": False,
                         "start_line": 2, "start_col": 17,
                         "enclosing_start_line": None, "enclosing_end_line": None},
                    ],
                },
            ]
        }

    def test_the_ast_name_match_calls_this_cross_file_call_ambiguous(self) -> None:
        # No SCIP involved: the AST adapter alone. `store` is untyped, two project
        # methods are named persist, so the edge is NOT rooted and the call lands
        # in the residue marked ambiguous with both candidates named.
        with Project(self.FILES) as project:
            ast_raw = adapter.discover(project.source, project.root)

        self.assertNotIn(
            "app.alpha:Alpha.persist",
            ast_raw["_calls"].get("app.router:make_job", set()),
            "the AST must NOT have rooted the ambiguous cross-file edge",
        )
        ambiguous = [r for r in ast_raw["residue"] if r["callee"] == "store.persist"]
        self.assertEqual(len(ambiguous), 1, "the call is in the AST residue")
        self.assertEqual(ambiguous[0]["why"], "ambiguous name")
        self.assertEqual(
            sorted(ambiguous[0]["candidates"]),
            ["app.alpha:Alpha.persist", "app.beta:Beta.persist"],
        )

    def test_scip_resolves_the_call_the_ast_left_ambiguous(self) -> None:
        with Project(self.FILES) as project:
            ast_raw = adapter.discover(project.source, project.root)
            hybrid = resolve.hybrid_raw(ast_raw, self._normalized(), project.source)

        # SCIP disambiguated store.persist to exactly Alpha.persist, across files,
        # and the fixpoint-shaped graph now carries the edge the AST could not.
        self.assertEqual(
            hybrid["_calls"]["app.router:make_job"], {"app.alpha:Alpha.persist"}
        )
        self.assertNotIn("app.beta:Beta.persist", hybrid["_calls"]["app.router:make_job"])

    def test_the_getattr_scip_is_silent_about_is_still_enumerated(self) -> None:
        with Project(self.FILES) as project:
            ast_raw = adapter.discover(project.source, project.root)
            hybrid = resolve.hybrid_raw(ast_raw, self._normalized(), project.source)

        by_key = {(r["file"], r["line"]): r for r in hybrid["scip_residue"]}
        # the getattr (line 2) SCIP said nothing about is named as unresolved ...
        self.assertIn(("app/router.py", 2), by_key)
        spot = by_key[("app/router.py", 2)]
        self.assertFalse(spot["resolved"])
        self.assertEqual(spot["kind"], "attribute_by_name")
        self.assertTrue(spot["reason"], "the blind spot is named, not dropped")
        # ... while the call SCIP DID resolve (line 3) is absent from the residue.
        self.assertNotIn(("app/router.py", 3), by_key)

    def test_both_halves_are_reported_together(self) -> None:
        with Project(self.FILES) as project:
            ast_raw = adapter.discover(project.source, project.root)
            hybrid = resolve.hybrid_raw(ast_raw, self._normalized(), project.source)

        s = hybrid["scip_residue_summary"]
        self.assertEqual(s["scip_rooted_edges"], 1, "SCIP resolved one edge")
        self.assertGreaterEqual(
            s["unresolved_enumerated"], 1, "and one blind spot stayed enumerated"
        )


class ExplainTest(unittest.TestCase):
    """``_Normalizer.explain`` gives the reason a symbol is not a node instead of
    a silent None; the vocabulary is the frozen ``scip_symbol_unrooted.reason``."""

    GO = "scip-go gomod github.com/example/gonest . "

    def test_a_rooted_symbol_has_a_node_and_no_reason(self) -> None:
        node, reason = resolve.normalizer("go").explain(
            self.GO + "`github.com/example/gonest/api`/Handler#GetJob()."
        )
        self.assertEqual(node, "github.com/example/gonest/api:Handler.GetJob")
        self.assertIsNone(reason)

    def test_every_reason_is_in_the_frozen_vocabulary(self) -> None:
        cases = {
            "local 3": "local",
            "": "unknown-scheme",
            None: "unknown-scheme",
            "scip-go gomod x": "unknown-scheme",
            self.GO + "`github.com/example/gonest/api`/Handler#GetJob().(id)": "non-node-descriptor",
            self.GO + "`github.com/example/gonest/api`/": "non-node-descriptor",
            "scip-python python spike 0.0.1 Job#": "no-package",
        }
        n = resolve.normalizer("go")
        for symbol, expected in cases.items():
            with self.subTest(symbol=symbol):
                node, reason = n.explain(symbol)
                self.assertIsNone(node)
                self.assertEqual(reason, expected)
                self.assertIn(reason, resolve.UNROOTED_REASONS)
                # symbol_to_node is exactly the node half of explain
                self.assertEqual(n.symbol_to_node(symbol), node)

    def test_symbol_to_node_is_unchanged_on_the_real_go_dump(self) -> None:
        normalized = runner.normalize_scip_json(json.loads(GO_FIXTURE.read_text()))
        n = resolve.normalizer("go")
        for doc in normalized["documents"]:
            for sym in doc["symbols"]:
                node, reason = n.explain(sym["symbol"])
                self.assertEqual(n.symbol_to_node(sym["symbol"]), node)
                self.assertTrue((node is None) != (reason is None))


class ResolveDigestTest(unittest.TestCase):
    def test_resolve_hashes_the_index_before_unlinking_it(self) -> None:
        import hashlib
        import tempfile

        raw_text = FIXTURE.read_text()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            index = root / runner._INDEX_FILENAME

            def fake_index(target_dir, language, *, timeout=600):
                index.write_bytes(b"real-index-bytes")
                return index

            observed = {}

            def fake_read(path, *, retain=False):
                observed["retain"] = retain
                observed["existed"] = Path(path).exists()
                out = runner.normalize_scip_json(
                    {"documents": json.loads(raw_text)["documents"]}, retain=retain
                )
                if retain:
                    out["index_digest"] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
                    out["index_digest_kind"] = "binary"
                return out

            with (
                patch("capcov.scip.resolve.shutil.which", return_value="/usr/bin/scip-python"),
                patch("capcov.scip.runner._locate_scip_cli", return_value="/usr/bin/scip"),
                patch("capcov.scip.runner.run_scip_index", fake_index),
                patch("capcov.scip.runner.read_scip_index", fake_read),
                patch("capcov.scip.resolve._enumerate_call_sites", return_value=[]),
                patch("capcov.scip.resolve._enumerate_blind_spots", return_value=[]),
            ):
                hybrid = resolve.resolve(
                    root, {"_direct": {}, "_calls": {}, "surfaces": []}, language="python"
                )

            self.assertTrue(observed["retain"])
            self.assertTrue(observed["existed"], "read (and hash) before unlink")
            self.assertFalse(index.exists(), "the transient index is still removed")
        self.assertEqual(
            hybrid["scip_index_digest"], hashlib.sha256(b"real-index-bytes").hexdigest()
        )
        self.assertEqual(hybrid["scip_index_digest_kind"], "binary")
        # the hybrid keeps its shape: the resolver keys are all still there
        for key in ("_calls", "resolver", "scip_resolved_edges", "scip_entities",
                    "scip_residue", "scip_residue_summary"):
            self.assertIn(key, hybrid)


class ToolGuardTest(unittest.TestCase):
    def test_resolve_names_a_missing_indexer_rather_than_degrading(self) -> None:
        with patch("capcov.scip.resolve.shutil.which", return_value=None):
            with self.assertRaises(resolve.ScipToolsUnavailable) as ctx:
                resolve.resolve("/some/dir", {"_direct": {}, "_calls": {}, "surfaces": []},
                                language="python")
        self.assertIn("scip-python", str(ctx.exception))

    def test_resolve_rejects_an_unsupported_language(self) -> None:
        with self.assertRaises(ValueError):
            resolve.resolve("/some/dir", {}, language="ruby")

    def test_tools_available_is_a_bool_and_never_runs_a_tool(self) -> None:
        self.assertIsInstance(resolve.tools_available("python"), bool)

    def test_tools_available_php_requires_the_script_not_just_php(self) -> None:
        # php on PATH is necessary but not sufficient: without the standalone
        # scip-php script php would falsely report available.
        with (
            patch("capcov.scip.resolve.shutil.which", return_value="/usr/bin/php"),
            patch("capcov.scip.runner._scip_php_bin", return_value="/no/such/scip-php"),
        ):
            self.assertFalse(resolve.tools_available("php"))

    def test_resolve_php_names_the_missing_scip_php_script(self) -> None:
        with (
            patch("capcov.scip.resolve.shutil.which", return_value="/usr/bin/php"),
            patch("capcov.scip.runner._scip_php_bin", return_value="/no/such/scip-php"),
        ):
            with self.assertRaises(resolve.ScipToolsUnavailable) as ctx:
                resolve.resolve(
                    "/some/dir",
                    {"_direct": {}, "_calls": {}, "surfaces": []},
                    language="php",
                )
        self.assertIn("scip-php", str(ctx.exception))


# ---------------------------------------------------------------------------
# Live: index the real APP tree with SCIP, resolve, and bind through the
# fixpoint. Skips when the indexer or the scip CLI is absent.


def _have_tools() -> bool:
    return resolve.tools_available("python")


class LiveResolveTest(unittest.TestCase):
    @unittest.skipUnless(_have_tools(), "needs scip-python + the scip CLI (set SCIP_CLI)")
    def test_scip_resolved_graph_binds_an_entity_through_a_call_chain(self) -> None:
        # get_widgets touches no table itself; it reaches `widgets` two hops away
        # through service -> repo. SCIP must resolve that chain across three files.
        with Project(APP) as project:
            ast_raw = adapter.discover(project.source, project.root)
            hybrid = resolve.resolve(project.source, ast_raw, language="python")

        handler = "app.api:get_widgets"
        bound, _ = fixpoint.bind([handler], hybrid["_calls"], hybrid["_direct"])
        self.assertIn(
            "widgets", bound.get(handler, {}),
            "SCIP must resolve get_widgets -> list_widgets -> fetch_widgets",
        )

    @unittest.skipUnless(_have_tools(), "needs scip-python + the scip CLI (set SCIP_CLI)")
    def test_scip_stays_silent_about_the_dynamic_sites_and_the_residue_names_them(
        self,
    ) -> None:
        with Project(APP) as project:
            ast_raw = adapter.discover(project.source, project.root)
            hybrid = resolve.resolve(project.source, ast_raw, language="python")

        # the computed getattr in dynamics.py is unresolvable for any resolver;
        # SCIP emits no occurrence for it, so it must surface in the residue.
        residue_files = {r["file"] for r in hybrid["scip_residue"]}
        self.assertIn("app/dynamics.py", residue_files)
        self.assertTrue(hybrid["scip_resolved_edges"], "SCIP resolved real edges")

    @unittest.skipUnless(_have_tools(), "needs scip-python + the scip CLI (set SCIP_CLI)")
    def test_the_transient_index_is_cleaned_up(self) -> None:
        with Project(APP) as project:
            ast_raw = adapter.discover(project.source, project.root)
            resolve.resolve(project.source, ast_raw, language="python")
            self.assertFalse(
                (project.source / runner._INDEX_FILENAME).exists(),
                "resolve must remove the index.scip it wrote into the tree",
            )


if __name__ == "__main__":
    unittest.main()
