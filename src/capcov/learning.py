"""Bounded, evidence-preserving behavioral rule mining.

The learner consumes observations already produced by an incumbent or test
runner.  It never executes source, invents expected probe results, or turns a
missing observation into a pass.  The input is intentionally simple so a
separate project can batch its own capability inventory into this interface.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class Observation:
    operand: str
    output: str
    source: str | None = None


def _observations(value: Any, source: str | None = None) -> list[Observation]:
    """Read one operation's observation list without guessing missing fields."""
    if not isinstance(value, list):
        raise ValueError("observations must be a list")
    result: list[Observation] = []
    for index, row in enumerate(value):
        if isinstance(row, list) and len(row) == 2:
            operand, output = row
            row_source = source
        elif isinstance(row, dict):
            operand, output = row.get("input"), row.get("output")
            row_source = row.get("source", source)
        else:
            raise ValueError(f"observation {index} must be [input, output] or an object")
        if not isinstance(operand, str) or not isinstance(output, str):
            raise ValueError(f"observation {index} input and output must be strings")
        result.append(Observation(operand, output, row_source))
    return result


def mine(observations: Iterable[Observation], *, max_prefix: int = 64) -> list[dict[str, Any]]:
    """Mine constant-prefix/variable-suffix rules from one operation.

    A rule requires two distinct supporting suffixes, records contradictory
    observations as exceptions, and proposes the unobserved prefix as a probe.
    Duplicate observations do not increase support.  Rules are scoped to this
    operation by the caller.
    """
    rows = list(observations)
    by_input: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        by_input[row.operand].add(row.output)
    groups: dict[str, set[str]] = defaultdict(set)
    for operand in by_input:
        if len(operand) > 1:
            groups[operand[:-1]].add(operand[-1])
    rules: list[dict[str, Any]] = []
    for prefix in sorted(groups):
        if len(prefix) > max_prefix:
            continue
        support: list[str] = []
        exceptions: list[str] = []
        for suffix in sorted(groups[prefix]):
            operand = prefix + suffix
            if by_input[operand] == {suffix}:
                support.append(operand)
            else:
                exceptions.append(operand)
        if len(support) < 2:
            continue
        rules.append({
            "rule": "prefix + character -> character",
            "prefix": prefix,
            "support": support,
            "exceptions": exceptions,
            "probes": [] if prefix in by_input else [prefix],
            "evidence": {"observations": len(rows), "distinct_inputs": len(by_input)},
        })
    return rules


def learn_document(document: Any, *, max_prefix: int = 64) -> dict[str, Any]:
    """Mine every inventory operation, retaining empty and invalid evidence."""
    if isinstance(document, list):
        operations = {"default": document}
    elif isinstance(document, dict):
        operations = document.get("operations", document)
    else:
        raise ValueError("input must be an operation map or list")
    if not isinstance(operations, dict):
        raise ValueError("operations must be an object")
    learned: list[dict[str, Any]] = []
    family_cells: dict[str, dict[str, dict[str, list[str]]]] = defaultdict(dict)
    for operation in sorted(operations):
        value = operations[operation]
        source = None
        if isinstance(value, dict):
            source = value.get("source")
            family = value.get("family")
            raw_value = value.get("observations")
            if isinstance(family, str) and isinstance(raw_value, list):
                cells: dict[str, list[str]] = defaultdict(list)
                for row in raw_value:
                    if isinstance(row, dict) and isinstance(row.get("cell"), str):
                        cells[row["cell"]].append(str(row.get("output", "")))
                family_cells[family][operation] = dict(cells)
            value = raw_value
        try:
            rows = _observations(value, source)
            rules = mine(rows, max_prefix=max_prefix)
            learned.append({"operation": operation, "status": "learned", "rules": rules,
                            "observation_count": len(rows)})
        except ValueError as error:
            learned.append({"operation": operation, "status": "unresolved",
                            "reason": str(error), "rules": []})
    relations: list[dict[str, Any]] = []
    for family in sorted(family_cells):
        members = family_cells[family]
        names = sorted(members)
        for left_index, left in enumerate(names):
            for right in names[left_index + 1 :]:
                common = sorted(set(members[left]) & set(members[right]))
                equal = [cell for cell in common if members[left][cell] == members[right][cell]]
                if len(equal) >= 2:
                    relations.append({
                        "family": family,
                        "rule": "sibling operations preserve named cell output sequences",
                        "operations": [left, right],
                        "cells": equal,
                        "observed": {left: {cell: members[left][cell] for cell in equal},
                                     right: {cell: members[right][cell] for cell in equal}},
                        "validation": "incumbent-only; candidate comparison required",
                    })
    return {"schema": "capcov/behavioral-learning/v1", "operations": learned,
            "relations": relations}


def run(input_path: Path, output_path: Path, *, max_prefix: int = 64) -> None:
    document = json.loads(input_path.read_text())
    output_path.write_text(json.dumps(learn_document(document, max_prefix=max_prefix),
                                      indent=2, sort_keys=True) + "\n")
