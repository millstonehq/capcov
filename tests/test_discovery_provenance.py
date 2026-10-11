"""Discovery must expose what its query narrowed, not just what it kept.

A `covered/N` over a silently shrunk N reads as done when it is not. Two
provenance fields make the narrowing legible: `excluded_surfaces` (candidates the
query could see but filtered out) and `unresolved` (declared adapters that
produced no surface obligations). These are first-class inventory fields, never
implied by absence, and they never change which obligations are produced.

The recognised-verb whitelist that populated `excluded_surfaces` under the retired
Python route reader is now the treesitter-routes `methods` allowlist -- one config
key, the same provenance, expressed generically. `unresolved` is adapter-agnostic
and is exercised here through openapi-json, which needs no optional extra.
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from capcov.flows.discovery import discover

_HAVE_TS = (
    importlib.util.find_spec("tree_sitter") is not None
    and importlib.util.find_spec("tree_sitter_language_pack") is not None
)

# A python decorator route query capturing the verb as @method and the path as
# @path; the enclosing function is @handler so branch/exception candidates are
# walked. The `methods` allowlist decides which verbs are surfaces.
PY_HANDLER_QUERY = (
    "(decorated_definition "
    "(decorator (call function: (attribute attribute: (identifier) @method) "
    "arguments: (argument_list (string) @path))) "
    "definition: (function_definition) @handler)"
)


class ExcludedSurfacesTests(unittest.TestCase):
    @unittest.skipUnless(_HAVE_TS, "treesitter extra not installed")
    def test_route_shaped_candidates_dropped_by_the_verb_allowlist_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "routes.py").write_text(
                '@router.get("/items")\ndef items():\n    return []\n\n'
                '@router.head("/items")\ndef head_items():\n    return None\n\n'
                '@router.websocket("/ws")\ndef ws():\n    return None\n'
            )
            config = root / "discovery.json"
            config.write_text(
                json.dumps(
                    {
                        "scope": "fixture",
                        "root": ".",
                        "adapters": [
                            {
                                "kind": "treesitter-routes",
                                "language": "python",
                                "files": ["routes.py"],
                                "query": PY_HANDLER_QUERY,
                                "methods": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                            }
                        ],
                    }
                )
            )
            inventory = discover(config)

            # The scope could see three route-shaped decorators; the verb allowlist
            # kept only GET. The other two are filtered, not absent.
            self.assertGreater(inventory["excluded_surfaces"]["count"], 0)
            self.assertEqual(inventory["excluded_surfaces"]["count"], 2)
            methods = {s["method"] for s in inventory["excluded_surfaces"]["surfaces"]}
            self.assertEqual(methods, {"HEAD", "WEBSOCKET"})
            paths = {s["path"] for s in inventory["excluded_surfaces"]["surfaces"]}
            self.assertEqual(paths, {"/items", "/ws"})

            # The obligations themselves are unchanged: only the recognised GET is a
            # surface. Filtering is now visible without inflating the denominator.
            surfaces = {o["id"] for o in inventory["obligations"] if o["kind"] == "surface"}
            self.assertEqual(surfaces, {"http:/items"})

    def test_clean_config_reports_zero_excluded_surfaces_explicitly(self) -> None:
        # No verb filtering is configured, so nothing is excluded; the field is
        # still present and explicit, never implied by absence. Uses openapi-json
        # so the guarantee holds even without the treesitter extra installed.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "api.json").write_text(
                json.dumps({"openapi": "3.1.0", "paths": {"/items": {"get": {}}}})
            )
            config = root / "discovery.json"
            config.write_text(
                json.dumps(
                    {
                        "scope": "fixture",
                        "root": ".",
                        "adapters": [{"kind": "openapi-json", "document": "api.json"}],
                    }
                )
            )
            inventory = discover(config)
            self.assertEqual(inventory["excluded_surfaces"]["count"], 0)
            self.assertEqual(inventory["excluded_surfaces"]["surfaces"], [])


class UnresolvedAdapterTests(unittest.TestCase):
    def test_adapter_that_yields_no_surfaces_is_named_under_unresolved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # A document that declares an inline operation (contributes a surface).
            (root / "full.json").write_text(
                json.dumps({"openapi": "3.1.0", "paths": {"/items": {"get": {}}}})
            )
            # A valid OpenAPI 3.0.x document that declares no inline operations:
            # it contributes only boundary markers and zero surfaces.
            (root / "empty.json").write_text(json.dumps({"openapi": "3.0.0", "paths": {}}))
            config = root / "discovery.json"
            config.write_text(
                json.dumps(
                    {
                        "scope": "fixture",
                        "root": ".",
                        "adapters": [
                            {"kind": "openapi-json", "document": "full.json"},
                            {"kind": "openapi-json", "document": "empty.json"},
                        ],
                    }
                )
            )
            inventory = discover(config)

            # The empty document's adapter produced no surface, so it is named.
            unresolved_indices = {u["adapter"] for u in inventory["unresolved"]}
            self.assertIn(1, unresolved_indices)
            # The first adapter did produce a surface, so it is NOT unresolved.
            self.assertNotIn(0, unresolved_indices)
            empty = next(u for u in inventory["unresolved"] if u["adapter"] == 1)
            self.assertEqual(empty["kind"], "openapi-json")

            # Obligations are untouched: the recognised GET is still the only surface.
            surfaces = {o["id"] for o in inventory["obligations"] if o["kind"] == "surface"}
            self.assertEqual(surfaces, {"http:GET /items"})

    def test_every_adapter_producing_surfaces_leaves_unresolved_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "api.json").write_text(
                json.dumps({"openapi": "3.1.0", "paths": {"/items": {"get": {}}}})
            )
            config = root / "discovery.json"
            config.write_text(
                json.dumps(
                    {
                        "scope": "fixture",
                        "root": ".",
                        "adapters": [{"kind": "openapi-json", "document": "api.json"}],
                    }
                )
            )
            inventory = discover(config)
            self.assertEqual(inventory["unresolved"], [])


if __name__ == "__main__":
    unittest.main()
