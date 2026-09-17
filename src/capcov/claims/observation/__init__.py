"""Observation (Lane A) claim inputs: the primitive observation schema and its exporter.

``schema_observation_v1.json`` is the declaration set for the observation
judge: every primitive relation one *check harness* reports after putting one
pre-registered scenario set to an incumbent implementation and a candidate
implementation, the ``run`` / ``policy`` / ``scenario_set`` context columns,
the completeness witnesses, and the three reviewer admissions.  Its
per-relation field set is exactly that of ``capcov.claims.replay.schema_replay_v1``.

WHAT THIS PACK IS NOT.  It declares **no model relation and no model producer
class**, so a model row cannot be ingested at all: the claim it supports,
``observations_agree(run, check, scenario_set, policy)``, is model-free *by
construction* rather than by omission.  It says that on the scenarios the
admitted set declares, two implementations were observed to answer alike under
an admitted comparison policy.  It does not say either is correct -- a shared
defect present in both is exactly what agreement looks like -- and it is not a
capability-parity claim.  The verdict word is ``agreeing``; ``qualified``
belongs to ``op_qualified`` in the replay pack and is never used here.

``rules-observation-v1.json`` is the judge's rule pack, shipped here as package
data and loaded by ``pack.py`` through ``importlib.resources``.  Unlike the
replay pack it has **no** ``experiments/claim-semantics`` mirror in v1: there
is no corpus adapter for observation receipts yet, so this file is the only
copy and there is no second opinion to drift from.

``observation_facts`` turns one receipt directory into a validated claims
``Bundle`` of facts and evidence; ``join`` judges one against the pack.
"""

from __future__ import annotations

from importlib import resources
import json
from pathlib import Path

SCHEMA_NAME = "schema_observation_v1.json"
SCHEMA_PATH = Path(__file__).resolve().parent / SCHEMA_NAME


def load_observation_schema() -> dict:
    """The primitive observation schema document (``{"schema_version", "relations", ...}``).

    Read through ``importlib.resources`` so the same code works from a source
    checkout and from an installed wheel, where the JSON travels as package
    data.
    """
    return json.loads(resources.files(__name__).joinpath(SCHEMA_NAME).read_text(encoding="utf-8"))


__all__ = ["SCHEMA_NAME", "SCHEMA_PATH", "load_observation_schema"]
