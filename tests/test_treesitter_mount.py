"""`mount` on a treesitter-routes adapter entry composes a prefix into the path.

An Express router mounted at app.use('/api/v1', router) serves '/api/v1/jobs',
not '/jobs'. The literal in the file is the second; the runtime surface is the
first. Without a mount two routers with the same literals collide, and no
runtime probe can match the id. `mount` is per adapter entry, so each mounted
router gets its own entry and its own prefix.
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

JS_QUERY = (
    "((call_expression function: (member_expression property: (property_identifier) @method) "
    "arguments: (arguments (string) @path)) "
    "(#match? @method \"^(get|post|put|patch|delete)$\"))"
)
SRC = "router.get('/jobs', h);\nrouter.post('/jobs/:id', h);\n"


@unittest.skipUnless(_HAVE_TS, "treesitter extra not installed")
class MountTests(unittest.TestCase):
    @staticmethod
    def _entry(mount: str | None, **extra: object) -> dict:
        entry: dict = {"kind": "treesitter-routes", "language": "javascript",
                       "files": ["routes.js"], "query": JS_QUERY, **extra}
        if mount is not None:
            entry["mount"] = mount
        return entry

    @staticmethod
    def _run(entries: list[dict]) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "routes.js").write_text(SRC)
            config = root / "discovery.json"
            config.write_text(json.dumps({"scope": "t", "root": ".", "adapters": entries}))
            return discover(config)

    def _discover(self, mount: str | None) -> set[str]:
        inventory = self._run([self._entry(mount)])
        return {o["id"] for o in inventory["obligations"] if o["kind"] == "surface"}

    def test_mount_prefixes_every_route(self) -> None:
        self.assertEqual(self._discover("/api/v1"), {"http:/api/v1/jobs", "http:/api/v1/jobs/:id"})

    def test_mount_joins_with_a_single_slash(self) -> None:
        self.assertEqual(self._discover("/api/v1/"), {"http:/api/v1/jobs", "http:/api/v1/jobs/:id"})

    def test_no_mount_is_unchanged(self) -> None:
        self.assertEqual(self._discover(None), {"http:/jobs", "http:/jobs/:id"})

    def test_mount_must_be_an_absolute_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "mount.*api/v1"):
            self._run([self._entry("api/v1")])

    def test_two_routers_with_the_same_literals_stay_distinct_under_different_mounts(self) -> None:
        # The stated motivation: without mounts these two entries collide on id
        # (the engine refuses duplicate obligation IDs). With mounts, each router's
        # surfaces are the paths the runtime actually serves.
        inventory = self._run([self._entry("/api/v1"), self._entry("/api/v2")])
        surfaces = {o["id"] for o in inventory["obligations"] if o["kind"] == "surface"}
        self.assertEqual(surfaces, {
            "http:/api/v1/jobs", "http:/api/v1/jobs/:id",
            "http:/api/v2/jobs", "http:/api/v2/jobs/:id",
        })

    def test_excluded_surface_carries_the_mounted_path(self) -> None:
        # A verb outside the allowlist is reported, not emitted; the report names
        # the path the runtime serves, so the narrowed denominator is legible.
        inventory = self._run([self._entry("/api/v1", methods=["GET"])])
        surfaces = {o["id"] for o in inventory["obligations"] if o["kind"] == "surface"}
        self.assertEqual(surfaces, {"http:/api/v1/jobs"})
        excluded = inventory["excluded_surfaces"]["surfaces"]
        self.assertEqual([(s["method"], s["path"]) for s in excluded],
                         [("POST", "/api/v1/jobs/:id")])
