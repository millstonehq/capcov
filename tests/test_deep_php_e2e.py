"""§4.2 -- PHP end-to-end, the GENERIC deep path over a real scip-php index.

The third of the three-indexer proofs, and the one the design leans on hardest:
scip-php emits BARE namespace descriptors (``App/Http/Controllers/...``) that the
original Python-shaped translator mapped to ``None``, dropping every PHP edge
silently (R2), and scip-php emits NO enclosing range so the runner has to
synthesize one or attribute no caller. Both are exercised here, live, on the whole
controller -> repository -> Eloquent-model chain -- not on a hand-authored symbol
dump.

Two things are proven, and the second is the honest-denominator half without which
the first would be a half-truth:

1. The typed chain BINDS. ``GET /jobs/{id}`` reads ``jobs`` and creates
   ``audit_logs``, resolved through scip-php's typed references (the ``JobRepository
   $repo`` property, the ``Job``/``AuditLog`` model references) -- data entities on
   the entity axis, each with a replayable call chain.
2. The untyped facade chain is ENUMERATED, never bound and never dropped. The
   fixture's ``DB::table('audit_trail')->insert(...)`` is the known no-larastan
   scip-php blind spot (R4): SCIP is silent about it, so it must appear in
   ``blind_spots`` (the tree-sitter enumerator's job) and must NOT appear as a
   guessed capability. A resolver that silently missed it would look identical to
   one that resolved everything; a resolver that guessed it would invent a
   capability. Neither is allowed.

Driven through ``cli.main`` so the CLI LANGUAGE seam is exercised. Runs only under
the ``treesitter`` extra with php + the standalone scip-php script + the ``scip``
CLI present; php is located (PATH, then the common system dirs, then the nix
store) and prepended to PATH at import so ``tools_available('php')`` can see it. It
does not skip-to-green when the tooling IS present.
"""

from __future__ import annotations

import glob
import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from capcov.scip import resolve as scip_resolve

_HAVE_TS = (
    importlib.util.find_spec("tree_sitter") is not None
    and importlib.util.find_spec("tree_sitter_language_pack") is not None
)


def _locate_php_bindir() -> str | None:
    """A directory containing a ``php`` executable, or None if php is already on
    PATH or none can be found.

    scip-php runs as ``php <script>``; the recipe gets php from nix
    (``nix shell nixpkgs#php83``), which leaves it in the store rather than on
    PATH. So the test finds a real php -- PATH first, then the usual system
    locations, then a php 8.x in the nix store -- and returns its bin dir to
    prepend. Returns None when php is already resolvable (nothing to do) or when
    no php exists at all (the test then skips: genuine tool absence)."""
    if shutil.which("php") is not None:
        return None
    for directory in (
        "/run/current-system/sw/bin", "/opt/homebrew/bin",
        "/usr/local/bin", "/usr/bin",
    ):
        if Path(directory, "php").is_file():
            return directory
    # Prefer a php-with-extensions 8.3 (the recipe's php83), then any php 8.x,
    # then any php, taking the lexically-last match (usually the newest build).
    for pattern in (
        "/nix/store/*php-with-extensions-8.3*/bin/php",
        "/nix/store/*php-with-extensions-8*/bin/php",
        "/nix/store/*php-8*/bin/php",
        "/nix/store/*php*/bin/php",
    ):
        hits = sorted(glob.glob(pattern))
        if hits:
            return str(Path(hits[-1]).parent)
    return None


_PHP_BINDIR = _locate_php_bindir()
if _PHP_BINDIR:
    os.environ["PATH"] = _PHP_BINDIR + os.pathsep + os.environ.get("PATH", "")

_PHP_TOOLS = _HAVE_TS and scip_resolve.tools_available("php")

_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "php_app"

# The fixture's untyped facade chain: DB::table('audit_trail')->insert(...), the
# known scip-php blind spot. It lives on this line of the repository.
_FACADE_FILE = "app/Repositories/JobRepository.php"


def _run_discover(fixture: Path) -> dict:
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name) / "php_app"
    shutil.copytree(fixture, root)
    out = Path(tmp.name) / "capabilities.json"
    rc = cli_main(
        ["discover", "--target", str(root), "--out", str(out), "--quiet",
         "--resolver", "scip"]
    )
    doc = json.loads(out.read_text()) if out.exists() else {}
    tmp.cleanup()
    if rc != 0:
        raise AssertionError(f"capcov discover exited {rc}; deep PHP run failed")
    return doc


def cli_main(argv: list[str]) -> int:
    # Imported lazily so a plain-suite import of this module (no treesitter extra)
    # never pulls the CLI in before the skip guard runs.
    from capcov import cli

    return cli.main(argv)


@unittest.skipUnless(
    _PHP_TOOLS,
    "deep PHP e2e needs the treesitter extra + php + the standalone scip-php "
    "script (SCIP_PHP_BIN) + the scip CLI",
)
class DeepPhpEndToEndTest(unittest.TestCase):
    """The generic deep path over the Laravel fixture, driven through the CLI."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.doc = _run_discover(_FIXTURE)
        cls.caps = {
            (c["entity"], c["surface"]): c for c in cls.doc["capabilities"]
        }

    def test_reads_jobs_bound_through_typed_references(self) -> None:
        cap = self.caps.get(("jobs", "http:GET /jobs/{id}"))
        self.assertIsNotNone(cap, f"no jobs capability; got {sorted(self.caps)}")
        self.assertEqual(cap["operations"], ["read"])
        self.assertTrue(cap["evidence"]["kind"].startswith("call-chain:"))
        chain = cap["evidence"]["chain"]
        # Bound through the TYPED $repo property: getJob -> JobRepository.get, in
        # scip-php's canonical `Namespace\Class.method` node spelling.
        self.assertTrue(chain[0].endswith(":JobController.getJob"), chain)
        self.assertTrue(chain[-1].endswith(":JobRepository.get"), chain)

    def test_creates_audit_logs_bound_through_typed_references(self) -> None:
        cap = self.caps.get(("audit_logs", "http:GET /jobs/{id}"))
        self.assertIsNotNone(cap, f"no audit_logs capability; got {sorted(self.caps)}")
        self.assertEqual(cap["operations"], ["create"])
        chain = cap["evidence"]["chain"]
        self.assertTrue(chain[0].endswith(":JobController.getJob"), chain)
        self.assertTrue(chain[-1].endswith(":JobRepository.writeAudit"), chain)

    def test_the_chain_crosses_files(self) -> None:
        chain = self.caps[("jobs", "http:GET /jobs/{id}")]["evidence"]["chain"]
        namespaces = {node.rsplit(":", 1)[0] for node in chain}
        # Controller and repository are different namespaces/files.
        self.assertIn("App\\Http\\Controllers", namespaces)
        self.assertIn("App\\Repositories", namespaces)

    def test_entity_axis_names_data_entities_not_the_route(self) -> None:
        entities = {entity for entity, _surface in self.caps}
        self.assertEqual(entities, {"jobs", "audit_logs"})
        self.assertNotIn("http:GET /jobs/{id}", entities)

    def test_the_untyped_facade_chain_is_enumerated_as_a_blind_spot(self) -> None:
        # The honest-denominator assertion (R4): DB::table(...) is untyped, so
        # scip-php cannot see it. It MUST be named by the tree-sitter enumerator.
        facade_spots = [
            b for b in self.doc["blind_spots"]
            if b["file"] == _FACADE_FILE and b["kind"] == "facade_magic"
        ]
        self.assertTrue(
            facade_spots,
            f"the DB:: facade chain in {_FACADE_FILE} was not enumerated as a "
            f"blind spot; blind_spots={self.doc['blind_spots']}",
        )

    def test_the_untyped_facade_chain_is_never_guessed_into_a_capability(self) -> None:
        # A blind spot must be enumerated, NOT bound: no capability may name the
        # facade's table, and no resolved chain may run through a DB facade node.
        entities = {entity for entity, _surface in self.caps}
        self.assertNotIn("audit_trail", entities, "an untyped facade was guessed bound")
        for cap in self.doc["capabilities"]:
            for node in cap["evidence"]["chain"]:
                self.assertNotIn(":DB", node, f"a facade leaked into a chain: {node}")

    def test_no_edge_was_silently_dropped(self) -> None:
        deep_unresolved = [
            u for u in self.doc.get("unresolved", [])
            if u.get("kind") == "deep-unresolved"
        ]
        self.assertEqual(deep_unresolved, [], "a node failed the (file,line)-join")
        self.assertTrue(self.doc["capabilities"], "the run produced no capabilities")
        self.assertGreater(self.doc["scip_residue_summary"]["scip_resolved_edges"], 0)

    def test_the_provisional_node_locations_never_leak_into_the_artifact(self) -> None:
        self.assertNotIn("_node_locations", self.doc)


if __name__ == "__main__":
    unittest.main()
