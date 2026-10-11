"""SCIP hybrid: rent SCIP as the resolver, keep the AST pass as the enumerator.

SCIP is type-aware, cross-file and multi-language -- everything the intra-module
AST resolver is not. But it is a black box about its own gaps: it reports the
edges it resolved and says nothing about the call sites it could not. The AST
half supplies exactly that missing half -- see `blindspots`.

Running a SCIP indexer needs external node/go CLIs, so it is an OPTIONAL
capability. Nothing in `blindspots` needs them: the enumerator is pure AST and
the differ takes a resolver's OUTPUT as an argument, so that module is always
importable and always CI-testable. Code that actually SHELLS OUT to an indexer
must guard on `scip_tools_available`, and its tests must skip cleanly when the
CLI is absent.
"""

from __future__ import annotations

import shutil

# SCIP indexers ship as separate CLIs (node: scip-python / scip-typescript;
# go: scip / scip-go). "Available" means the indexer this run needs is on PATH;
# the caller names which, and the plain `scip` binary is the default probe.
DEFAULT_SCIP_CLIS = ("scip",)


def scip_tools_available(*cli_names: str) -> bool:
    """True when every named SCIP CLI is on PATH (default: the ``scip`` indexer).

    Use as ``@unittest.skipUnless(scip_tools_available(), ...)`` on any test that
    invokes a real indexer, so it skips cleanly where the node/go tooling is not
    installed.
    """
    names = cli_names or DEFAULT_SCIP_CLIS
    return all(shutil.which(name) is not None for name in names)


def scip_tool_path(name: str) -> str | None:
    """The resolved PATH to a SCIP CLI, or None when it is not installed."""
    return shutil.which(name)
