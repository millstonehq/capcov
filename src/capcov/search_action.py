"""Agent-facing DreamCoder-style synthesis action.

The action deliberately keeps the sibling corpus and target contract separate.
Sibling observations are solved and compressed into a library.  The target is
then searched using that frozen library, with all of its known examples fit and
optional reachable inputs used only for disagreement search.
"""

from __future__ import annotations

from typing import Any
import hashlib
import json

from .boolean_search import search
from .contract_search import evaluate
from .synthesis import learn_library
from .recognition import sleep


def _limits(operation: dict[str, Any]) -> dict[str, int]:
    bounds = operation.get("bounds", {})
    defaults = {
        "max_terms": 100_000,
        "max_cost": 12,
        "max_defs": 16,
        "max_probes": 64,
        "sleep_samples": 96,
    }
    limits = {name: bounds.get(name, default) for name, default in defaults.items()}
    if any(type(value) is not int for value in limits.values()):
        raise ValueError("bounds must be integers")
    if (
        limits["max_terms"] < 1
        or not 1 <= limits["max_cost"] <= 32
        or limits["max_defs"] < 0
        or limits["max_probes"] < 1
        or not 0 <= limits["sleep_samples"] <= 512
    ):
        raise ValueError("invalid search bounds")
    return limits


def run_search(
    operation: dict[str, Any], objective: str = "find_fit"
) -> dict[str, Any]:
    """Run one resumable synthesis action and return an evidence receipt.

    ``operation`` has ``id``, optional observation-only ``siblings``, and a
    ``target`` containing complete ``examples`` and optional
    ``reachable_inputs``.  A supplied ``library`` is treated as frozen.  The
    target never contributes to library invention.
    """
    if not isinstance(operation, dict) or not operation.get("id"):
        raise ValueError("operation requires an id")
    target = operation.get("target")
    if (
        not isinstance(target, dict)
        or not target.get("id")
        or not target.get("examples")
    ):
        raise ValueError("operation target requires id and known examples")
    siblings = operation.get("siblings", [])
    if not isinstance(siblings, list):
        raise ValueError("siblings must be a list")
    sibling_ids = [row.get("id") for row in siblings if isinstance(row, dict)]
    if len(sibling_ids) != len(set(sibling_ids)) or target["id"] in set(sibling_ids):
        raise ValueError("sibling and target IDs must be unique")
    if objective not in {"find_fit", "find_disagreement"}:
        raise ValueError("objective must be find_fit or find_disagreement")
    if {"program", "train_examples", "heldout_examples"}.intersection(target):
        raise ValueError("target must provide one complete examples set")
    probes = target.get("reachable_inputs", [])
    probe_ids = [row["id"] for row in probes]
    if len(probe_ids) != len(set(probe_ids)) or any("output" in row for row in probes):
        raise ValueError(
            "reachable inputs require unique IDs and must be unlabeled; put known answers in examples"
        )
    limits = _limits(operation)

    supplied = operation.get("library")
    solved = []
    if supplied is not None:
        if not isinstance(supplied, dict) or not isinstance(
            supplied.get("library"), dict
        ):
            raise ValueError("library must be a synthesis library document")
        training_ids = set(supplied.get("training_ids", []))
        if supplied["library"] and "training_ids" not in supplied:
            raise ValueError(
                "frozen definitions require training_ids to check target isolation"
            )
        if target["id"] in training_ids:
            raise ValueError("frozen library provenance overlaps this operation")
        library = supplied
        wake = []
        inventions = []
    else:
        wake = []
        for sibling in operation.get("siblings", []):
            if (
                not isinstance(sibling, dict)
                or not sibling.get("id")
                or not sibling.get("examples")
            ):
                raise ValueError("siblings require id and observation examples")
            result = search(
                sibling["examples"],
                {},
                max_terms=limits["max_terms"],
                max_cost=limits["max_cost"],
            )
            wake.append({"id": sibling["id"], **result})
            if result["program"] is None:
                continue
            solved.append(
                {
                    "id": sibling["id"],
                    "examples": sibling["examples"],
                    "program": result["program"],
                }
            )
        library = learn_library(solved, max_defs=limits["max_defs"])
        library["training_ids"] = [row["id"] for row in solved]
        inventions = library.get("inventions", [])
    policy = sleep(
        solved,
        library["library"],
        dreams=limits["sleep_samples"],
    )

    # evaluate() fits every known target example.  Reachable inputs are only
    # examined for disagreement, never used to invent the library.
    search_target = dict(target)
    if objective == "find_fit":
        search_target.pop("reachable_inputs", None)
    receipt = evaluate(
        search_target,
        library.get("library", {}),
        max_terms=limits["max_terms"],
        max_cost=limits["max_cost"],
        max_probes=limits["max_probes"],
        policy=policy,
    )
    library_digest = hashlib.sha256(
        json.dumps(library.get("library", {}), sort_keys=True).encode()
    ).hexdigest()
    return {
        "schema": "capcov/search-action/v1",
        "operation_id": operation["id"],
        "objective": objective,
        "target_id": target["id"],
        "bounds": limits,
        "library": library,
        "wake": wake,
        "inventions": inventions,
        "library_digest": library_digest,
        "recognition_policy": policy,
        "evaluation": receipt,
    }
