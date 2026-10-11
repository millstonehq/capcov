import unittest

from capcov.adapters import python_fastapi_sqlalchemy as adapter

from .support import Project


class DiscoverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.project = Project()
        self.addCleanup(self.project.close)
        self.raw = adapter.discover(self.project.source, self.project.root)

    def names(self, key: str) -> list[str]:
        return [item["name"] if "name" in item else item["id"] for item in self.raw[key]]

    def test_entities_are_tables_not_classes(self) -> None:
        self.assertEqual(sorted(self.names("entities")), ["crates", "ghosts", "widgets"])
        widget = next(e for e in self.raw["entities"] if e["name"] == "widgets")
        self.assertEqual(widget["symbol"], "Widget")
        self.assertEqual(widget["file"], "app/models.py")

    def test_route_path_composes_mount_and_router_prefix(self) -> None:
        self.assertIn("GET /v1/things/widgets", self.names("surfaces"))
        self.assertIn("POST /v1/things/widgets", self.names("surfaces"))
        self.assertIn("GET /v1/things/crates/{crate_id}", self.names("surfaces"))

    def test_a_handler_nested_in_a_factory_keeps_its_scope(self) -> None:
        healthz = next(s for s in self.raw["surfaces"] if s["path"] == "/healthz")
        self.assertEqual(healthz["handler"], "app.main:create_app.healthz")

    def test_a_router_that_is_never_included_is_marked_unmounted(self) -> None:
        ghosts = next(s for s in self.raw["surfaces"] if "ghosts" in s["path"])
        self.assertFalse(ghosts["mounted"])

    def test_getattr_with_a_literal_name_is_not_a_blind_spot(self) -> None:
        blind = {(b["file"], b["expr"]) for b in self.raw["blind_spots"] if b["blind"]}
        seen = {(b["file"], b["expr"]) for b in self.raw["blind_spots"] if not b["blind"]}
        self.assertIn(("app/dynamics.py", "getattr"), blind)  # the computed one
        self.assertIn(("app/dynamics.py", "getattr"), seen)   # the literal one
        literals = [b for b in self.raw["blind_spots"] if b["resolved_name"]]
        self.assertEqual([b["resolved_name"] for b in literals], ["known_field"])

    def test_vars_is_a_namespace_blind_spot(self) -> None:
        kinds = {b["kind"] for b in self.raw["blind_spots"] if b["blind"]}
        self.assertIn("namespace_lookup", kinds)


class BindingTest(unittest.TestCase):
    """The point of following calls: a handler that touches no table itself."""

    def setUp(self) -> None:
        self.project = Project()
        self.addCleanup(self.project.close)
        self.raw = adapter.discover(self.project.source, self.project.root)

    def test_direct_reference_alone_misses_the_route(self) -> None:
        from capcov.core import fixpoint

        handler = "app.api:get_widgets"
        self.assertEqual(self.raw["_direct"].get(handler, set()), set())
        bound, history = fixpoint.bind([handler], self.raw["_calls"], self.raw["_direct"])
        self.assertEqual(history[0], 0, "the handler names no table itself")
        self.assertIn("widgets", bound[handler], "two hops away through service -> repo")
        self.assertEqual(bound[handler]["widgets"], 2)

    def test_the_evidence_chain_can_be_followed_by_hand(self) -> None:
        from capcov.core import fixpoint

        chain = fixpoint.chain(
            "app.api:get_widgets", "widgets", self.raw["_calls"], self.raw["_direct"]
        )
        self.assertEqual(
            chain, ["app.api:get_widgets", "app.service:list_widgets", "app.repo:fetch_widgets"]
        )


class ResidueTest(unittest.TestCase):
    def test_interface_dispatch_is_named_not_dropped(self) -> None:
        from .support import APP

        files = dict(APP)
        files["storage.py"] = (
            "class Local:\n    def put(self, k): pass\n\n"
            "class Remote:\n    def put(self, k): pass\n"
        )
        files["uses.py"] = "def save(blobs, k):\n    return blobs.put(k)\n"
        with Project(files) as project:
            raw = adapter.discover(project.source, project.root)
        callees = {r["callee"] for r in raw["residue"]}
        self.assertIn("blobs.put", callees)
        entry = next(r for r in raw["residue"] if r["callee"] == "blobs.put")
        self.assertEqual(len(entry["candidates"]), 2)
        self.assertEqual(entry["why"], "ambiguous name")

    def test_an_unambiguous_name_becomes_an_edge_not_residue(self) -> None:
        from .support import APP

        files = dict(APP)
        files["only.py"] = "def uniquely_named(x): pass\n"
        files["uses.py"] = "def go(thing):\n    return thing.uniquely_named(1)\n"
        with Project(files) as project:
            raw = adapter.discover(project.source, project.root)
        self.assertNotIn(
            "thing.uniquely_named", {r["callee"] for r in raw["residue"]}
        )
        self.assertIn("app.only:uniquely_named", raw["_calls"]["app.uses:go"])

    def test_name_match_can_be_turned_off_and_then_it_is_residue(self) -> None:
        from .support import APP

        files = dict(APP)
        files["only.py"] = "def uniquely_named(x): pass\n"
        files["uses.py"] = "def go(thing):\n    return thing.uniquely_named(1)\n"
        with Project(files) as project:
            raw = adapter.discover(project.source, project.root, name_match=False)
        entry = next(r for r in raw["residue"] if r["callee"] == "thing.uniquely_named")
        self.assertEqual(entry["why"], "name-match off")

    def test_a_call_on_something_imported_from_outside_is_not_residue(self) -> None:
        from .support import APP

        files = dict(APP)
        files["uses.py"] = "import os\n\ndef go():\n    return os.environ.get('X')\n"
        with Project(files) as project:
            raw = adapter.discover(project.source, project.root)
        self.assertNotIn("os.environ.get", {r["callee"] for r in raw["residue"]})


if __name__ == "__main__":
    unittest.main()
