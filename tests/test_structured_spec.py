"""The generic structured-spec engine: OpenAPI is one config, and a second,
non-OpenAPI shape proves the path query — not the reader — is what varies."""

import json
import unittest

from capcov.flows import openapi
from capcov.flows import structured_spec
from capcov.flows.structured_spec import OPENAPI_SPEC, derive, derive_document


def _openapi(paths, **extra):
    return json.dumps({"openapi": "3.1.0", "paths": paths, **extra})


class OpenAPIConfigReproducesReader(unittest.TestCase):
    """structured_spec.derive under OPENAPI_SPEC == openapi.derive, byte for byte."""

    CASES = [
        # (json text, prefix)
        (_openapi({"/items/{id}": {method: {} for method in openapi.METHODS}}), ""),
        (json.dumps({"openapi": "3.0.4", "paths": {"/items": {"get": {}}}}), "/v1"),
        (_openapi({
            "/filtered": {},
            "/remote": {"$ref": "https://invalid.example/api.json"},
            "/mixed": {"$ref": "#/components/pathItems/X", "get": {}},
            "x-extension": {"post": {}},
        }, webhooks={"incoming": {"post": {}}}), ""),
        (_openapi({"/items": {"get": {
            "description": "PRIVATE_TEXT", "operationId": "PRIVATE_TEXT",
            "responses": {"200": {"description": "PRIVATE_TEXT"}},
        }}}, servers=[{"url": "https://PRIVATE_TEXT/v2"}]), ""),
        (_openapi({"/a": {"get": {}, "post": {}}, "/b/{x}/c": {"delete": {}}}), "/api"),
        (_openapi({}), ""),                                 # no operations
        (json.dumps({"openapi": "3.1.0"}), ""),             # no paths key at all
    ]

    def test_identical_obligations_for_every_case(self):
        for text, prefix in self.CASES:
            with self.subTest(text=text, prefix=prefix):
                self.assertEqual(
                    derive(text, OPENAPI_SPEC, "api.json", prefix),
                    openapi.derive(text, "api.json", prefix),
                )

    def test_private_content_and_server_urls_never_appear(self):
        text = self.CASES[3][0]
        self.assertNotIn("PRIVATE_TEXT", json.dumps(derive(text, OPENAPI_SPEC, "api.json")))

    def test_duplicate_member_fails_the_same_way(self):
        dup = '{"openapi":"3.1.0","paths":{"/x":{"get":{},"get":{}}}}'
        with self.assertRaisesRegex(ValueError, "duplicate JSON member"):
            derive(dup, OPENAPI_SPEC, "api.json")
        with self.assertRaisesRegex(ValueError, "duplicate JSON member"):
            openapi.derive(dup, "api.json")

    def test_declaration_and_boundaries_are_carried(self):
        rows = derive(_openapi({"/items": {"get": {}}}), OPENAPI_SPEC, "api.json")
        surface = next(r for r in rows if r["kind"] == "surface")
        self.assertEqual(surface["declaration"], "openapi")
        self.assertEqual(surface["source"]["pointer"], "/paths/~1items/get")
        self.assertEqual(sum(r["kind"] == "unresolved" for r in rows), 6)


# A genuinely different declarative shape: a flat LIST of operation records, each
# naming its own path and method as FIELDS (not nested map keys). Same engine,
# different path query. Repeated paths are legal here — this shape has no OpenAPI
# templated-path uniqueness rule, so the config simply does not declare one.
RECORD_SPEC = {
    "declaration": "route-table",
    "method_transform": "none",
    "document_rule": {"message": "route table must be an object"},
    "root": {"field": "operations", "default": [], "require": "list",
             "message": "operations must be a list"},
    "document_boundaries": [
        {"category": "runtime-confirmation",
         "reason": "a listed operation is not a confirmed mounted route"},
    ],
    "empty_document_boundaries": [
        {"category": "no-operations", "reason": "route table declares no operations"},
    ],
    "levels": [
        {
            "iterate": "list",
            "value_must_be_object": "operation record must be an object",
            "bind": {"path": "path", "method": "method"},
        },
    ],
}


class SecondShapeProvesGenericity(unittest.TestCase):
    def test_list_of_records_yields_http_surfaces(self):
        document = {"operations": [
            {"path": "/widgets", "method": "GET"},
            {"path": "/widgets", "method": "POST"},
            {"path": "/widgets/{id}", "method": "GET"},
        ]}
        rows = derive_document(document, RECORD_SPEC, "routes.json")
        surfaces = [r for r in rows if r["kind"] == "surface"]
        self.assertEqual(
            [r["id"] for r in surfaces],
            ["http:GET /widgets", "http:POST /widgets", "http:GET /widgets/{id}"],
        )
        # A repeated path across methods is fine here; the OpenAPI-only
        # templated-equivalence rule lives in config, not the engine.
        self.assertEqual(surfaces[0]["declaration"], "route-table")
        self.assertEqual(surfaces[0]["source"]["pointer"], "/operations/0")
        self.assertEqual(surfaces[2]["source"]["pointer"], "/operations/2")

    def test_prefix_and_boundaries_apply_generically(self):
        rows = derive_document(
            {"operations": [{"path": "/widgets", "method": "GET"}]},
            RECORD_SPEC, "routes.json", prefix="/api",
        )
        self.assertIn("http:GET /api/widgets", [r["id"] for r in rows])
        self.assertTrue(any(r["id"].startswith("boundary:route-table:") for r in rows))

    def test_empty_route_table_has_no_green_denominator(self):
        rows = derive_document({"operations": []}, RECORD_SPEC, "routes.json")
        self.assertTrue(all(r["kind"] == "unresolved" for r in rows))
        self.assertTrue(any("declares no operations" in r["reason"] for r in rows))

    def test_wrong_root_type_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "operations must be a list"):
            derive_document({"operations": {}}, RECORD_SPEC, "routes.json")


if __name__ == "__main__":
    unittest.main()
