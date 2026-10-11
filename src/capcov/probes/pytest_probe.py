"""pytest plugin. Dormant unless CAPCOV_OBSERVE is set.

This is the whole runtime half from the target's point of view: install capcov,
run the test suite you already run. Nothing in the application changes, no
sidecar, no agent, no network. The plugin is loaded through the pytest11 entry
point, so there is not even a conftest line.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from . import python_probe
from .probe_registry import source_provenance_from_env

if TYPE_CHECKING:  # pytest is present whenever this plugin loads; the guard
    # keeps the module importable by capcov's own stdlib-only test suite.
    from pytest import Config, Item, Session

_ENABLED = False
_EXERCISES = 0
_STARTED_NS = 0


def pytest_configure(config: Config) -> None:
    global _ENABLED, _STARTED_NS
    if os.environ.get("CAPCOV_OBSERVE") != "1":
        return
    hooks = python_probe.install()
    _ENABLED = True
    _STARTED_NS = time.perf_counter_ns()
    if not any(hooks.values()):
        raise RuntimeError(
            "CAPCOV_OBSERVE=1 but neither sqlalchemy nor fastapi could be "
            "imported. A probe that cannot observe must not report an empty "
            "observation as a clean run."
        )
    # Before any test module is imported, so `from x import process_one` in a
    # test file picks up the wrapper rather than the original.
    python_probe.install_entry_points(Path(os.environ.get("CAPCOV_TARGET", ".")))


def pytest_runtest_protocol(item: Item, nextitem: Item | None) -> None:
    """Outermost per-test hook: fixture setup counts as part of the exercise."""
    if _ENABLED:
        global _EXERCISES
        _EXERCISES += 1
        python_probe.set_exercise(item.nodeid)
    return None


def pytest_sessionfinish(session: Session, exitstatus: int) -> None:
    if not _ENABLED:
        return
    out = Path(os.environ.get("CAPCOV_OUT", "observed.json"))
    source = Path(os.environ.get("CAPCOV_SOURCE_ROOT", "src"))
    snapshot, carried = source_provenance_from_env(source)
    python_probe.dump(
        out,
        source,
        _EXERCISES,
        source_snapshot=snapshot,
        source_provenance=carried,
    )
    if carried is not None:
        from .. import artifacts

        # Driven by `capcov observe`: the artifact just written is PROVISIONAL.
        # The driver owns the run's one exact source verification, after this
        # process exits, and stamps or discards the artifact.  Walking the tree
        # here too would charge every observation for the source twice -- and
        # a walk performed inside the exercised process is exactly the one the
        # driver could not take on trust.
        document = artifacts.read(out, "observed")
        document["timing"] = {
            "observe_ms": min(
                max(0, (time.perf_counter_ns() - _STARTED_NS) // 1_000_000),
                86_400_000,
            ),
        }
        artifacts.write_document(out, document)
