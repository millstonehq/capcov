"""Replay (runtime) claim inputs: the frozen primitive replay schema and its exporter.

``schema_replay_v1.json`` is the frozen declaration set for the replay judge
(Phase 4) -- every primitive replay relation the harness, the two systems under
test, the model runner and the mutation tool report, the ``run`` / ``model``
context columns, the completeness witnesses and the two compatibility
relations that bind a model to a run and a static index to a replay.  It is
loaded here, never edited here: a change to it is a schema version, not a
patch.  The per-relation field set is exactly that of
``capcov.claims.static.schema_static_v1.json``.

``rules-replay-v1.json`` is the judge's rule pack, shipped here as package data
and loaded by ``pack.py`` through ``importlib.resources``.  It is a
byte-identical mirror of the reviewed
``experiments/claim-semantics/replay/rules-replay-v1.json``; that copy is the
one a reviewer edits, and a test fails while the two differ.

``replay_facts`` turns one receipt directory written by the replay harness
into a validated claims ``Bundle`` of facts and evidence keyed by the receipt's
``run``, whose identity is a digest of the exported relations and never of the
files' bytes.
"""

from __future__ import annotations

from importlib import resources
import json
from pathlib import Path

SCHEMA_NAME = "schema_replay_v1.json"
SCHEMA_PATH = Path(__file__).resolve().parent / SCHEMA_NAME


def load_replay_schema() -> dict:
    """The frozen replay schema document (``{"schema_version", "relations", ...}``).

    Read through ``importlib.resources`` so the same code works from a source
    checkout and from an installed wheel, where the JSON travels as package
    data (``[tool.setuptools.package-data]`` in ``pyproject.toml``).
    """
    return json.loads(resources.files(__name__).joinpath(SCHEMA_NAME).read_text(encoding="utf-8"))


__all__ = ["SCHEMA_NAME", "SCHEMA_PATH", "load_replay_schema"]
