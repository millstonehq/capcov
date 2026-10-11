"""§4.1 -- Go end-to-end, the GENERIC deep path over a real scip-go index.

This is one of the three-indexer proofs the design requires (Go alone under-tests
the SCIP translation seam, but it is the seam where the scip-go nested-package
trap lives, R1). Nothing here is Go-specific except the fixture and the four
tree-sitter queries in its ``capcov.toml``: the run goes through the *generic*
machinery -- ``treesitter-routes`` (deep block) discovers routes + the function
universe + gorm model structs + gorm data-access sites; ``go_gorm`` recognizes the
entities and ops; scip-go resolves the cross-package call graph; the (file,line)
join canonicalizes the provisional nodes; ``core.fixpoint`` traces the handler to
the data. The whole thing is driven through ``cli.main`` so the CLI LANGUAGE seam
(``scip_language`` read per-config) is exercised too.

The assertion is the point of the whole design: ``GET /jobs/{id}`` must be shown
to READ ``jobs`` and CREATE ``audit_logs`` -- data entities named on the entity
axis, NOT the route echoing itself -- each with a replayable call chain
``GetJob -> Service.Fetch -> Repo.Get/Repo.Write`` that crosses three packages and
three files. That is the difference between "the route exists" (shallow) and "the
route touches this data" (deep).

Runs only under the ``treesitter`` extra and with scip-go + the ``scip`` CLI + the
``go`` toolchain present; it does not skip-to-green when they ARE present. When the
tree-sitter extra is absent (the plain suite) the deep census cannot run, so the
test skips -- that is genuine tool absence, the same guard every live SCIP test in
this package uses.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from capcov import cli
from capcov.scip import resolve as scip_resolve

_HAVE_TS = (
    importlib.util.find_spec("tree_sitter") is not None
    and importlib.util.find_spec("tree_sitter_language_pack") is not None
)
# scip-go needs a buildable module, so the `go` toolchain is a hard requirement
# on top of the indexer + the scip CLI that `tools_available` probes.
_GO_TOOLS = (
    _HAVE_TS
    and shutil.which("go") is not None
    and scip_resolve.tools_available("go")
)

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "go_app"


def _run_discover(fixture: Path) -> dict:
    """Copy the fixture, run ``capcov discover --resolver scip``, return the doc.

    The fixture is copied to a throwaway directory because the indexer writes a
    transient ``index.scip`` into the tree it indexes (``resolve`` removes it, but
    the committed fixture is kept pristine and parallel runs never race).
    """
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name) / "go_app"
    shutil.copytree(fixture, root)
    out = Path(tmp.name) / "capabilities.json"
    rc = cli.main(
        ["discover", "--target", str(root), "--out", str(out), "--quiet",
         "--resolver", "scip"]
    )
    doc = json.loads(out.read_text()) if out.exists() else {}
    tmp.cleanup()
    if rc != 0:
        raise AssertionError(f"capcov discover exited {rc}; deep Go run failed")
    return doc


@unittest.skipUnless(
    _GO_TOOLS,
    "deep Go e2e needs the treesitter extra + scip-go + the scip CLI + go on PATH",
)
class DeepGoEndToEndTest(unittest.TestCase):
    """The generic deep path over the Go fixture, driven through the CLI."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = _run_discover(_FIXTURE)
        cls.caps = {
            (c["entity"], c["surface"]): c for c in cls.doc["capabilities"]
        }

    def test_reads_jobs_through_a_two_hop_cross_file_chain(self) -> None:
        cap = self.caps.get(("jobs", "http:GET /jobs/{id}"))
        self.assertIsNotNone(
            cap, f"no jobs capability; got {sorted(self.caps)}"
        )
        self.assertEqual(cap["operations"], ["read"])
        self.assertEqual(cap["evidence"]["kind"], "call-chain:2")
        chain = cap["evidence"]["chain"]
        # The chain names the resolved SCIP identities GetJob -> Service.Fetch ->
        # Repo.Get, in call order, rooted at the handler and ending at the touch.
        self.assertTrue(chain[0].endswith(":GetJob"), chain)
        self.assertTrue(chain[-1].endswith(":Repo.Get"), chain)
        self.assertTrue(any("Service.Fetch" in n for n in chain), chain)

    def test_creates_audit_logs_through_the_write_chain(self) -> None:
        cap = self.caps.get(("audit_logs", "http:GET /jobs/{id}"))
        self.assertIsNotNone(cap, f"no audit_logs capability; got {sorted(self.caps)}")
        self.assertEqual(cap["operations"], ["create"])
        self.assertEqual(cap["evidence"]["kind"], "call-chain:2")
        chain = cap["evidence"]["chain"]
        self.assertTrue(chain[0].endswith(":GetJob"), chain)
        self.assertTrue(chain[-1].endswith(":Repo.Write"), chain)
        self.assertTrue(any("Service.Fetch" in n for n in chain), chain)

    def test_the_chain_crosses_three_packages_and_three_files(self) -> None:
        # Deep's whole claim is cross-file, type-resolved reachability. Each chain
        # node is a `import/path:Recv.Method` id; the packages (the part before
        # `:`) must be three distinct ones -- api, internal/service, internal/jobs
        # -- proving SCIP crossed package + file boundaries the AST pass cannot.
        chain = self.caps[("jobs", "http:GET /jobs/{id}")]["evidence"]["chain"]
        packages = {node.rsplit(":", 1)[0] for node in chain}
        self.assertEqual(len(packages), 3, f"chain did not cross 3 packages: {chain}")
        self.assertTrue(any(p.endswith("/api") for p in packages), packages)
        self.assertTrue(any(p.endswith("/internal/service") for p in packages), packages)
        self.assertTrue(any(p.endswith("/internal/jobs") for p in packages), packages)

    def test_entity_axis_names_data_entities_not_the_route(self) -> None:
        # The deep dividend: entities are DATA (jobs, audit_logs), never the route
        # echoing itself as its own entity (the shallow self-bound shape).
        entities = {entity for entity, _surface in self.caps}
        self.assertEqual(entities, {"jobs", "audit_logs"})
        self.assertNotIn("http:GET /jobs/{id}", entities)

    def test_no_edge_was_silently_dropped(self) -> None:
        # The R1 trap surfaces as zero deep capabilities on a green run; the
        # (file,line)-join turns any residual node mismatch into a NAMED
        # deep-unresolved rather than a silent drop. Neither happened: real
        # capabilities AND no deep-unresolved node.
        deep_unresolved = [
            u for u in self.doc.get("unresolved", [])
            if u.get("kind") == "deep-unresolved"
        ]
        self.assertEqual(deep_unresolved, [], "a node failed the (file,line)-join")
        self.assertTrue(self.doc["capabilities"], "the run produced no capabilities")
        # scip-go resolved real edges -- not an empty graph masquerading as clean.
        self.assertGreater(self.doc["scip_residue_summary"]["scip_resolved_edges"], 0)

    def test_the_provisional_node_locations_never_leak_into_the_artifact(self) -> None:
        self.assertNotIn("_node_locations", self.doc)


if __name__ == "__main__":
    unittest.main()
