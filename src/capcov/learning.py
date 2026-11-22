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
import hashlib


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
    rows = [row if isinstance(row, Observation) else Observation(*row) for row in observations]
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
    family_cells: dict[str, dict[str, dict[str, tuple[str, ...]]]] = defaultdict(dict)
    for operation in sorted(operations):
        value = operations[operation]
        source = None
        family = None
        if isinstance(value, dict):
            source = value.get("source")
            family = value.get("family")
            raw_value = value.get("observations")
            value = raw_value
        try:
            rows = _observations(value, source)
            if not rows:
                raise ValueError("observations must not be empty")
            if isinstance(family, str):
                cells: dict[str, list[str]] = defaultdict(list)
                for row in value:
                    if isinstance(row, dict) and isinstance(row.get("cell"), str):
                        cells[row["cell"]].append(str(row.get("output", "")))
                family_cells[family][operation] = {cell: tuple(outputs) for cell, outputs in cells.items()}
            rules = mine(rows, max_prefix=max_prefix)
            learned.append({"operation": operation, "family": family, "status": "learned", "rules": rules,
                            "observation_count": len(rows)})
        except ValueError as error:
            learned.append({"operation": operation, "family": family, "status": "unresolved",
                            "reason": str(error), "rules": []})
    relations: list[dict[str, Any]] = []
    for family in sorted(family_cells):
        by_cell: dict[str, dict[tuple[str, ...], list[str]]] = defaultdict(lambda: defaultdict(list))
        for operation, cells in family_cells[family].items():
            for cell, sequence in cells.items():
                by_cell[cell][sequence].append(operation)
        for cell in sorted(by_cell):
            supports = by_cell[cell]
            ordered = sorted(supports.items(), key=lambda item: (item[0], item[1]))
            if max((len(ops) for _, ops in ordered), default=0) < 2:
                continue
            relations.append({
                "family": family,
                "rule": "sibling operations preserve named cell output sequences",
                "cell": cell,
                "consensus": ordered[0][0] if len(ordered) == 1 else None,
                "ambiguous": len(ordered) > 1,
                "support": [{"sequence": sequence, "operations": sorted(ops)} for sequence, ops in ordered],
                "validation": "incumbent-only; candidate comparison required",
            })
    return {"schema": "capcov/behavioral-learning/v1", "operations": learned,
            "relations": relations}


def run(input_path: Path, output_path: Path, *, max_prefix: int = 64) -> None:
    document = json.loads(input_path.read_text())
    output_path.write_text(json.dumps(learn_document(document, max_prefix=max_prefix),
                                      indent=2, sort_keys=True) + "\n")


def _split(operation: str, holdout_mod: int, holdout_value: int) -> str:
    digest = hashlib.sha256(operation.encode()).digest()
    return "holdout" if int.from_bytes(digest[:8], "big") % holdout_mod == holdout_value else "train"


def evaluate_document(document: Any, *, holdout_mod: int = 5,
                      holdout_value: int = 0, max_prefix: int = 64,
                      max_results: int = 10000) -> dict[str, Any]:
    """Train on one deterministic operation partition and compare held-out cells.

    Each operation may provide ``candidate_observations`` alongside incumbent
    ``observations``.  The evaluator never learns from holdout rows. A result
    is ``unknown`` when either side is absent, ``incumbent_exception`` when the
    incumbent disagrees with an applicable learned rule, ``candidate_mismatch``
    when incumbent and candidate differ, and ``pass`` when they agree.
    """
    if not isinstance(document, dict) or not isinstance(document.get("operations"), dict):
        raise ValueError("input must contain an operations object")
    if holdout_mod < 2 or not 0 <= holdout_value < holdout_mod:
        raise ValueError("holdout requires mod >= 2 and 0 <= value < mod")
    if max_results < 1:
        raise ValueError("max_results must be positive")
    operations = document["operations"]
    training: dict[str, Any] = {}
    heldout: dict[str, Any] = {}
    for operation, value in operations.items():
        (heldout if _split(operation, holdout_mod, holdout_value) == "holdout" else training)[operation] = value
    learned = learn_document({"operations": training}, max_prefix=max_prefix)
    rules_by_prefix: dict[tuple[str | None, str], list[dict[str, Any]]] = defaultdict(list)
    for item in learned["operations"]:
        for rule in item["rules"]:
            rules_by_prefix[(item.get("family"), rule["prefix"])].append(rule)
    relations_by_family: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for relation in learned["relations"]:
        relations_by_family[relation["family"]][relation["cell"]] = relation
    results: list[dict[str, Any]] = []
    for operation in sorted(heldout):
        value = heldout[operation]
        if not isinstance(value, dict):
            results.append({"operation": operation, "status": "unknown", "reason": "invalid operation"})
            continue
        incumbent = value.get("observations")
        candidate = value.get("candidate_observations")
        if not isinstance(incumbent, list):
            results.append({"operation": operation, "status": "unknown", "reason": "missing incumbent"})
            continue
        inc_rows = _observations(incumbent)
        cand_rows = _observations(candidate) if isinstance(candidate, list) else []
        cand_by_input: dict[str, list[str]] = defaultdict(list)
        for row in cand_rows:
            cand_by_input[row.operand].append(row.output)
        applicable = 0
        family = value.get("family")
        if isinstance(family, str):
            inc_cells: dict[str, list[str]] = defaultdict(list)
            cand_cells: dict[str, list[str]] = defaultdict(list)
            for row in incumbent:
                if isinstance(row, dict) and isinstance(row.get("cell"), str):
                    inc_cells[row["cell"]].append(str(row.get("output", "")))
            if isinstance(candidate, list):
                for row in candidate:
                    if isinstance(row, dict) and isinstance(row.get("cell"), str):
                        cand_cells[row["cell"]].append(str(row.get("output", "")))
            for cell in sorted(set(inc_cells) & set(relations_by_family.get(family, {}))):
                relation = relations_by_family[family][cell]
                incumbent_sequence = tuple(inc_cells[cell])
                supported = any(item["sequence"] == incumbent_sequence for item in relation["support"])
                candidate_sequence = tuple(cand_cells[cell]) if cell in cand_cells else None
                applicable += 1
                rule_status = "ambiguous" if relation["ambiguous"] else ("supported" if supported else "exception")
                candidate_status = "unknown" if not isinstance(candidate, list) else ("match" if candidate_sequence == incumbent_sequence else "mismatch")
                results.append({"operation": operation, "family": family, "cell": cell,
                                "incumbent": list(incumbent_sequence),
                                "candidate": None if candidate_sequence is None else list(candidate_sequence),
                                "rule_status": rule_status, "candidate_status": candidate_status})
        for row in inc_rows:
            rules = rules_by_prefix.get((family, row.operand[:-1]), []) if len(row.operand) > 1 else []
            if not any(row.operand in rule["support"] for rule in rules):
                continue
            applicable += 1
            candidate_outputs = cand_by_input.get(row.operand, [])
            rule_status = "supported" if row.output == row.operand[-1] else "exception"
            candidate_status = "unknown" if not isinstance(candidate, list) or not candidate_outputs else ("match" if candidate_outputs == [row.output] else "mismatch")
            results.append({"operation": operation, "input": row.operand,
                            "incumbent": row.output, "candidate": candidate_outputs,
                            "rule_status": rule_status, "candidate_status": candidate_status})
        if not applicable:
            results.append({"operation": operation, "status": "unknown",
                            "reason": "no learned rule applies"})
    counts: dict[str, int] = defaultdict(int)
    rule_counts: dict[str, int] = defaultdict(int)
    candidate_counts: dict[str, int] = defaultdict(int)
    for result in results:
        if "rule_status" in result:
            rule_counts[result["rule_status"]] += 1
            candidate_counts[result["candidate_status"]] += 1
        else:
            counts[result["status"]] += 1
    return {"schema": "capcov/behavioral-learning-evaluation/v1",
            "split": {"holdout_mod": holdout_mod, "holdout_value": holdout_value,
                      "training_operations": sorted(training), "holdout_operations": sorted(heldout)},
            "counts": dict(sorted(counts.items())), "rule_status_counts": dict(sorted(rule_counts.items())),
            "candidate_status_counts": dict(sorted(candidate_counts.items())), "results": results[:max_results],
            "results_truncated": len(results) > max_results,
            "training": learned}
