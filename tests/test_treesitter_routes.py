"""The treesitter-routes adapter: one engine, the route opinion in a config query.

Two layers are proven separately. The predicate parser/evaluator is pure Python
and runs even without the optional extra installed -- so #eq?/#match? filtering
is our code, not merely the binding's. The end-to-end tests parse real Go,
JavaScript and Python files through a single adapter, and skip cleanly when the
extra is absent.
"""

from __future__ import annotations

import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from capcov.flows.discovery import _ts_predicates, _ts_satisfied, discover

_HAVE_TS = (
    importlib.util.find_spec("tree_sitter") is not None
    and importlib.util.find_spec("tree_sitter_language_pack") is not None
)

# The route opinion is the query, supplied per language by the config -- the
# engine code is identical for all three.
GO_QUERY = (
    "((call_expression function: (identifier) @fn (#eq? @fn \"handle\") "
    "arguments: (argument_list (interpreted_string_literal) @path)))"
)
GO_QUERY_WITH_HANDLER = (
    "((call_expression function: (identifier) @fn (#eq? @fn \"handle\") "
    "arguments: (argument_list (interpreted_string_literal) @path . (identifier) @handler)))"
)
JS_QUERY = (
    "((call_expression function: (member_expression property: (property_identifier) @method) "
    "arguments: (arguments (string) @path)) "
    "(#match? @method \"^(get|post|put|patch|delete)$\"))"
)
PY_QUERY = (
    "((call function: (attribute attribute: (identifier) @fn (#eq? @fn \"add_url_rule\")) "
    "arguments: (argument_list . (string) @path)))"
)
# Captures the decorated handler function as @handler so its body is walked for
# branch and exception candidates.
PY_HANDLER_QUERY = (
    "(decorated_definition "
    "(decorator (call function: (attribute attribute: (identifier) @method) "
    "arguments: (argument_list (string) @path))) "
    "definition: (function_definition) @handler)"
)

GO_SRC = """package main

func register() {
\thandle("POST /ask", askHandler)
\thandle("GET /api/state", stateHandler)
\t// handle("POST /ghost", ghostHandler)
\thandle(pathVar, dynamicHandler)
\tother("POST /nope", nopeHandler)
}
"""

JS_SRC = """const app = express();
app.get('/health', function (req, res) { res.send('ok'); });
const router = express.Router();
router.post('/users', createUser);
app.listen(3000);
"""

PY_SRC = """from flask import Flask

app = Flask(__name__)
app.add_url_rule("/legacy", "legacy", legacy_view)
"""

# One if/else and one try/except in the handler body -- the branch and
# exception candidates the handler-subtree walk must find.
PY_HANDLER_SRC = """@router.post("/edit")
def edit():
    if authenticated:
        return save()
    else:
        return other()
    try:
        reject()
    except Error:
        return False
"""


class PredicateEvalTests(unittest.TestCase):
    """Proves the predicate layer is ours -- it runs with no extra installed."""

    def _captures(self, **named: str) -> dict[str, list[object]]:
        return {name: [SimpleNamespace(text=value.encode())] for name, value in named.items()}

    def test_eq_literal_keeps_only_the_matching_text(self) -> None:
        preds = _ts_predicates('(#eq? @fn "handle")')
        self.assertTrue(_ts_satisfied(preds, self._captures(fn="handle")))
        self.assertFalse(_ts_satisfied(preds, self._captures(fn="other")))

    def test_match_regex_filters(self) -> None:
        preds = _ts_predicates('(#match? @m "^(get|post)$")')
        self.assertTrue(_ts_satisfied(preds, self._captures(m="get")))
        self.assertFalse(_ts_satisfied(preds, self._captures(m="delete")))

    def test_a_predicate_whose_capture_is_absent_does_not_apply(self) -> None:
        # A predicate scoped to a capture this match never bound is skipped,
        # never treated as a failure -- keeps multi-pattern queries honest.
        preds = _ts_predicates('(#eq? @fn "handle")')
        self.assertTrue(_ts_satisfied(preds, self._captures(path="/x")))

    def test_two_capture_eq(self) -> None:
        preds = _ts_predicates("(#eq? @a @b)")
        self.assertTrue(_ts_satisfied(preds, self._captures(a="x", b="x")))
        self.assertFalse(_ts_satisfied(preds, self._captures(a="x", b="y")))

    def test_negated_predicates_invert(self) -> None:
        self.assertFalse(
            _ts_satisfied(_ts_predicates('(#not-eq? @fn "handle")'), self._captures(fn="handle"))
        )
        self.assertTrue(
            _ts_satisfied(_ts_predicates('(#not-match? @m "^get$")'), self._captures(m="post"))
        )


@unittest.skipUnless(_HAVE_TS, "treesitter extra not installed")
class TreesitterRoutesTests(unittest.TestCase):
    def _discover(self, files: dict[str, str], adapters: list[dict]) -> dict:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for name, body in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        config = root / "discovery.json"
        config.write_text(json.dumps({"scope": "fixture", "root": ".", "adapters": adapters}))
        return discover(config)

    def test_one_adapter_three_languages(self) -> None:
        inventory = self._discover(
            {"svc.go": GO_SRC, "server.js": JS_SRC, "legacy.py": PY_SRC},
            [
                {"kind": "treesitter-routes", "language": "go", "files": ["svc.go"], "query": GO_QUERY},
                {
                    "kind": "treesitter-routes",
                    "language": "javascript",
                    "files": ["server.js"],
                    "query": JS_QUERY,
                },
                {
                    "kind": "treesitter-routes",
                    "language": "python",
                    "files": ["legacy.py"],
                    "query": PY_QUERY,
                },
            ],
        )
        by_id = {o["id"]: o for o in inventory["obligations"]}
        # Every route the three queries agree on -- and nothing else.
        self.assertEqual(
            {o["id"] for o in inventory["obligations"] if o["kind"] == "surface"},
            {"http:/ask", "http:/api/state", "http:/health", "http:/users", "http:/legacy"},
        )
        # Go: the commented-out call, the variable arg and the sibling other()
        # call never appear -- syntax-aware, not textual.
        self.assertNotIn("http:/ghost", by_id)
        self.assertNotIn("http:/nope", by_id)
        # Method recovered from the path literal's prefix AND from a @method capture.
        self.assertEqual(by_id["http:/ask"]["method"], "POST")
        self.assertEqual(by_id["http:/api/state"]["method"], "GET")
        self.assertEqual(by_id["http:/health"]["method"], "GET")
        self.assertNotIn("method", by_id["http:/legacy"])
        # Attribution stays with the file the route was parsed from.
        self.assertEqual(by_id["http:/ask"]["source"]["file"], "svc.go")
        self.assertEqual(by_id["http:/health"]["source"]["file"], "server.js")
        self.assertEqual(by_id["http:/legacy"]["source"]["file"], "legacy.py")

    def test_bare_call_route_form_is_discovered(self) -> None:
        # add_url_rule is a bare call, not a decorator: a decorator-only reader
        # would miss it, while a config query capturing the call catches it.
        inventory = self._discover(
            {"legacy.py": PY_SRC},
            [{"kind": "treesitter-routes", "language": "python", "files": ["legacy.py"], "query": PY_QUERY}],
        )
        # Only the route surface, never the endpoint-name string that follows it.
        surfaces = {o["id"] for o in inventory["obligations"] if o["kind"] == "surface"}
        self.assertEqual(surfaces, {"http:/legacy"})
        ids = {o["id"] for o in inventory["obligations"]}
        self.assertNotIn("http:legacy", ids)

    def test_predicate_excludes_a_sibling_call(self) -> None:
        src = 'package main\nfunc f() {\n\thandle("POST /ask", a)\n\troute("POST /skip", b)\n}\n'
        inventory = self._discover(
            {"svc.go": src},
            [{"kind": "treesitter-routes", "language": "go", "files": ["svc.go"], "query": GO_QUERY}],
        )
        ids = {o["id"] for o in inventory["obligations"]}
        self.assertIn("http:/ask", ids)
        self.assertNotIn("http:/skip", ids)

    def test_id_prefix_override(self) -> None:
        inventory = self._discover(
            {"svc.go": 'package main\nfunc f() {\n\thandle("POST /ask", h)\n}\n'},
            [
                {
                    "kind": "treesitter-routes",
                    "language": "go",
                    "files": ["svc.go"],
                    "query": GO_QUERY,
                    "id_prefix": "route:",
                }
            ],
        )
        ids = {o["id"] for o in inventory["obligations"]}
        self.assertIn("route:/ask", ids)
        self.assertNotIn("http:/ask", ids)

    def test_trailing_suffix_is_stripped(self) -> None:
        inventory = self._discover(
            {"svc.go": 'package main\nfunc f() {\n\thandle("GET /exact/{$}", h)\n}\n'},
            [{"kind": "treesitter-routes", "language": "go", "files": ["svc.go"], "query": GO_QUERY}],
        )
        self.assertIn("http:/exact/", {o["id"] for o in inventory["obligations"]})

    def test_handler_capture_is_recorded(self) -> None:
        inventory = self._discover(
            {"svc.go": 'package main\nfunc f() {\n\thandle("POST /ask", myHandler)\n}\n'},
            [
                {
                    "kind": "treesitter-routes",
                    "language": "go",
                    "files": ["svc.go"],
                    "query": GO_QUERY_WITH_HANDLER,
                }
            ],
        )
        by_id = {o["id"]: o for o in inventory["obligations"]}
        self.assertEqual(by_id["http:/ask"]["handler"], "myHandler")

    def _candidate_shapes(self, inventory: dict, surface: str) -> set[tuple[str, str]]:
        """Branch/exception obligations reduced to (kind, structure) -- the sig
        digest and every line number normalised away so only the id SHAPE remains."""
        shapes = set()
        for obligation in inventory["obligations"]:
            if obligation["kind"] not in {"branch-candidate", "exception-candidate"}:
                continue
            self.assertEqual(obligation["surface"], surface)
            self.assertTrue(obligation["id"].startswith(surface + ":"))
            suffix = obligation["id"][len(surface) :]
            suffix = re.sub(r"[0-9a-f]{12}", "H", suffix)
            suffix = re.sub(r":\d+", ":L", suffix)
            shapes.add((obligation["kind"], suffix))
        return shapes

    def test_handler_branches_and_exceptions_are_emitted(self) -> None:
        treesitter = self._discover(
            {"app.py": PY_HANDLER_SRC},
            [
                {
                    "kind": "treesitter-routes",
                    "language": "python",
                    "files": ["app.py"],
                    "query": PY_HANDLER_QUERY,
                }
            ],
        )
        kinds = [o["kind"] for o in treesitter["obligations"]]
        # No obligation-KIND regression: from one Python decorator fixture with an
        # if/else and a try/except, the generic adapter emits every kind the retired
        # per-language Python route adapter did and no other -- surface,
        # branch-candidate, exception-candidate, and the boundary:python:* boundaries
        # (kind unresolved).
        self.assertEqual(
            set(kinds),
            {"surface", "branch-candidate", "exception-candidate", "unresolved"},
        )
        by_kind = {o["id"]: o["kind"] for o in treesitter["obligations"]}
        self.assertEqual(by_kind["http:/edit"], "surface")
        self.assertEqual(kinds.count("branch-candidate"), 2)
        self.assertEqual(kinds.count("exception-candidate"), 1)
        # Both outcomes of the one branch, and the one exception handler, each
        # reduced to (kind, id shape).
        ts_shapes = self._candidate_shapes(treesitter, "http:/edit")
        self.assertEqual(
            ts_shapes,
            {
                ("branch-candidate", ":branch:H:L:true"),
                ("branch-candidate", ":branch:H:L:false"),
                ("exception-candidate", ":except:L"),
            },
        )
        # The route-reading boundaries are present, once each, not language-keyed
        # per file. They retain the boundary:python: token so model.reconcile's
        # mount-census credit keeps matching.
        ids = {o["id"] for o in treesitter["obligations"]}
        for category in (
            "called-function-branches",
            "mounted-route-confirmation",
            "roles-and-configurations",
            "external-effects",
        ):
            self.assertIn(f"boundary:python:{category}", ids)

    def test_dynamic_path_capture_is_unresolved(self) -> None:
        # The query captures the path arg broadly; a non-literal path yields an
        # unresolved dynamic-route obligation rather than a surface.
        src = 'package main\nfunc f() {\n\thandle("POST /ask", a)\n\thandle(pathVar, b)\n}\n'
        query = (
            "((call_expression function: (identifier) @fn (#eq? @fn \"handle\") "
            "arguments: (argument_list . (_) @path)))"
        )
        inventory = self._discover(
            {"svc.go": src},
            [{"kind": "treesitter-routes", "language": "go", "files": ["svc.go"], "query": query}],
        )
        kinds = {o["id"]: o["kind"] for o in inventory["obligations"]}
        self.assertEqual(kinds.get("http:/ask"), "surface")
        dynamic = [o for o in inventory["obligations"] if o["id"].endswith(":dynamic-route")]
        self.assertEqual(len(dynamic), 1)
        self.assertEqual(dynamic[0]["kind"], "unresolved")

    def test_method_allowlist_moves_off_verb_routes_to_excluded_surfaces(self) -> None:
        # The generic analogue of the retired Python reader's HTTP-verb whitelist:
        # a decorator whose method is outside `methods` is filtered into
        # excluded_surfaces, not emitted as a surface, and does not inflate N.
        src = (
            '@router.get("/items")\ndef items():\n    return []\n\n'
            '@router.head("/items")\ndef head_items():\n    return None\n\n'
            '@router.websocket("/ws")\ndef ws():\n    return None\n'
        )
        inventory = self._discover(
            {"routes.py": src},
            [
                {
                    "kind": "treesitter-routes",
                    "language": "python",
                    "files": ["routes.py"],
                    "query": PY_HANDLER_QUERY,
                    "methods": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                }
            ],
        )
        surfaces = {o["id"] for o in inventory["obligations"] if o["kind"] == "surface"}
        self.assertEqual(surfaces, {"http:/items"})
        self.assertEqual(inventory["excluded_surfaces"]["count"], 2)
        excluded = inventory["excluded_surfaces"]["surfaces"]
        self.assertEqual({s["method"] for s in excluded}, {"HEAD", "WEBSOCKET"})
        self.assertEqual({s["path"] for s in excluded}, {"/items", "/ws"})

    def test_dedup_keeps_the_first_location(self) -> None:
        src = 'package main\nfunc f() {\n\thandle("POST /ask", a)\n\thandle("POST /ask", b)\n}\n'
        inventory = self._discover(
            {"svc.go": src},
            [{"kind": "treesitter-routes", "language": "go", "files": ["svc.go"], "query": GO_QUERY}],
        )
        rows = [o for o in inventory["obligations"] if o["id"] == "http:/ask"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"]["line"], 3)

    def test_one_path_under_two_verbs_is_two_surfaces(self) -> None:
        src = (
            'package main\nfunc f() {\n\thandle("GET /api/threads", list)\n'
            '\thandle("POST /api/threads", create)\n\thandle("POST /api/threads", again)\n}\n'
        )
        inventory = self._discover(
            {"svc.go": src},
            [{"kind": "treesitter-routes", "language": "go", "files": ["svc.go"], "query": GO_QUERY}],
        )
        rows = {o["id"]: o for o in inventory["obligations"] if o["kind"] == "surface"}
        self.assertEqual(sorted(rows), ["http:/api/threads", "http:/api/threads#POST"])
        self.assertEqual(rows["http:/api/threads"]["method"], "GET")
        self.assertEqual(rows["http:/api/threads#POST"]["method"], "POST")
        self.assertEqual(rows["http:/api/threads#POST"]["source"]["line"], 4)

    def test_empty_glob_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "empty"):
            self._discover(
                {"svc.go": GO_SRC},
                [{"kind": "treesitter-routes", "language": "go", "globs": ["nope/*.go"], "query": GO_QUERY}],
            )

    def test_glob_discovers_files(self) -> None:
        inventory = self._discover(
            {"routes/svc.go": 'package main\nfunc f() {\n\thandle("POST /ask", h)\n}\n'},
            [{"kind": "treesitter-routes", "language": "go", "globs": ["routes/*.go"], "query": GO_QUERY}],
        )
        self.assertIn("http:/ask", {o["id"] for o in inventory["obligations"]})


if __name__ == "__main__":
    unittest.main()
