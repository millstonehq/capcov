"""The AST blind-spot enumerator + differ -- the half SCIP cannot do.

These run with the node/go SCIP CLIs ABSENT, on purpose: the enumerator is pure
AST, and the differ takes a resolver's OUTPUT as an argument rather than running
one. That is what makes the hybrid's recover-the-unresolved step CI-testable
without the optional tooling installed.
"""

from __future__ import annotations

import textwrap
import unittest

from capcov.scip import scip_tools_available
from capcov.scip.blindspots import (
    blind_spot_residue,
    enumerate_blind_spots,
    enumerate_call_sites,
)

from .support import Project


class EnumerateBlindSpotsTest(unittest.TestCase):
    def test_getattr_dispatch_and_eval_are_both_enumerated(self) -> None:
        files = {
            "dispatch.py": textwrap.dedent(
                '''
                def call_by_name(obj, action):
                    handler = getattr(obj, action)
                    return handler()

                def run(src):
                    return eval(src)

                def known(obj):
                    return getattr(obj, "fixed_attr")
                '''
            ),
        }
        with Project(files) as project:
            spots = enumerate_blind_spots(project.source)

        seen = {(s["file"], s["kind"]) for s in spots}
        self.assertIn(("app/dispatch.py", "attribute_by_name"), seen)
        self.assertIn(("app/dispatch.py", "dynamic_eval"), seen)
        # getattr(obj, "fixed_attr") spells its target out -- statically readable,
        # so it is NOT enumerated. Only the computed getattr is a blind spot.
        attr_spots = [s for s in spots if s["kind"] == "attribute_by_name"]
        self.assertEqual(len(attr_spots), 1)
        for s in spots:
            self.assertEqual(set(s), {"file", "line", "kind", "reason"})
            self.assertTrue(s["reason"], "every blind spot carries a reason")
            self.assertIsInstance(s["line"], int)

    def test_agrees_with_the_adapter_on_the_shared_fixture(self) -> None:
        # Refactor-and-reuse parity guard: the adapter now sources its
        # dynamic-blind mapping from this module, so enumerating the shared
        # fixture independently MUST land on the same (file, line, kind) sites
        # the adapter flags as blind. If the two ever drift, this fails.
        from capcov.adapters import python_fastapi_sqlalchemy as adapter

        with Project() as project:  # the default APP tree
            spots = enumerate_blind_spots(project.source)
            raw = adapter.discover(project.source, project.root)

        mine = {(s["file"], s["line"], s["kind"]) for s in spots}
        theirs = {
            (b["file"], b["line"], b["kind"])
            for b in raw["blind_spots"]
            if b["blind"]
        }
        self.assertEqual(mine, theirs)
        self.assertTrue(mine, "the fixture has getattr/vars blind spots to find")


class EnumerateCallSitesTest(unittest.TestCase):
    def test_every_call_site_is_listed_including_ordinary_and_dynamic(self) -> None:
        # The residue's left-hand side must be the FULL call census: an ordinary
        # method call SCIP could not type is its most important silent gap, so a
        # plain `service.do()` has to appear alongside the getattr.
        files = {
            "m.py": textwrap.dedent(
                """\
                def go(service, obj, name):
                    service.do(obj)
                    return getattr(obj, name)
                """
            ),
        }
        with Project(files) as project:
            sites = enumerate_call_sites(project.source)

        by_line = {s["line"]: s["callee"] for s in sites}
        self.assertEqual(by_line[2], "service.do")
        self.assertEqual(by_line[3], "getattr")
        for s in sites:
            self.assertEqual(set(s), {"file", "line", "callee"})
            self.assertEqual(s["file"], "app/m.py")

    def test_a_call_on_an_expression_is_still_a_site(self) -> None:
        files = {"m.py": "def go(f):\n    return f()()\n"}
        with Project(files) as project:
            sites = enumerate_call_sites(project.source)
        # two nested calls on line 2; neither is dropped.
        self.assertEqual(len([s for s in sites if s["line"] == 2]), 2)


class BlindSpotResidueTest(unittest.TestCase):
    def test_residue_is_exactly_the_site_scip_left_unresolved(self) -> None:
        # SCIP is a black box: it reports the plain call it resolved and says
        # NOTHING about the getattr it could not. The residue recovers that gap
        # by subtracting what SCIP resolved from what the AST saw.
        ast_call_sites = [
            {"file": "app/m.py", "line": 3, "callee": "helper", "kind": "call"},
            {
                "file": "app/m.py",
                "line": 7,
                "callee": "getattr",
                "kind": "attribute_by_name",
            },
        ]
        scip_resolved_sites = [
            {"file": "app/m.py", "line": 3, "symbol": "app/m/helper()."},
        ]

        residue = blind_spot_residue(ast_call_sites, scip_resolved_sites)

        self.assertEqual(len(residue), 1)
        (only,) = residue
        self.assertEqual((only["file"], only["line"]), ("app/m.py", 7))
        self.assertEqual(only["callee"], "getattr")
        self.assertFalse(only["resolved"])
        self.assertTrue(only["reason"], "an unresolved site is named, not dropped")

    def test_a_fully_resolved_tree_leaves_no_residue(self) -> None:
        sites = [{"file": "a.py", "line": 1}, {"file": "a.py", "line": 2}]
        resolved = [{"file": "a.py", "line": 1}, {"file": "a.py", "line": 2}]
        self.assertEqual(blind_spot_residue(sites, resolved), [])

    def test_the_differ_needs_no_scip_cli_installed(self) -> None:
        # The whole reason the differ takes SCIP's output as an argument: the
        # diff is testable with the optional node/go tooling absent. A resolver's
        # OWN tests guard on scip_tools_available and skip; this half never does.
        self.assertIsInstance(scip_tools_available(), bool)
        residue = blind_spot_residue([{"file": "a.py", "line": 1}], [])
        self.assertEqual(residue[0]["resolved"], False)


if __name__ == "__main__":
    unittest.main()
