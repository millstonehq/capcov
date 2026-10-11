"""Static (SCIP-derived) claim inputs: the frozen primitive schema and its exporter.

``schema_static_v1.json`` is the frozen declaration set for section 29 of the
experiment plan -- every primitive static relation, its typed columns, the one
``index`` digest context column, the completeness witnesses and the two
compatibility relations. It is loaded here, never edited here: a change to it is
a schema version, not a patch.

``scip_facts`` turns a retained SCIP index (``runner.read_scip_index(...,
retain=True)`` or ``normalize_scip_json(..., retain=True)``) plus the
tree-sitter side's raw discover dict into a validated claims ``Bundle`` of facts
and evidence keyed by that index's digest.

``runtime_relations`` declares the runtime causal-trace primitives and the
static/runtime join rules.  ``certificate`` / ``ground`` re-derive and
explain engine-independent certificates (Stage C why / why-not).
"""

from __future__ import annotations

from importlib import resources
import json
from pathlib import Path

SCHEMA_NAME = "schema_static_v1.json"
SCHEMA_PATH = Path(__file__).resolve().parent / SCHEMA_NAME


def load_static_schema() -> dict:
    """The frozen static schema document (``{"schema_version", "relations", ...}``).

    Read through ``importlib.resources`` so the same code works from a source
    checkout and from an installed wheel, where the JSON travels as package
    data (``[tool.setuptools.package-data]`` in ``pyproject.toml``).
    """
    return json.loads(resources.files(__name__).joinpath(SCHEMA_NAME).read_text(encoding="utf-8"))


__all__ = ["SCHEMA_NAME", "SCHEMA_PATH", "load_static_schema"]
