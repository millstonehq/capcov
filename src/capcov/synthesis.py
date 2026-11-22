"""Small, deterministic DreamCoder-style pure-program experiment.

This is deliberately a research lane. Programs are JSON ASTs over variables,
literal values, boolean predicates, conditionals, and learned calls. Effects
and source execution are outside this DSL.
"""
from __future__ import annotations
from collections import Counter
import hashlib, json
from pathlib import Path
from typing import Any


def size(node: Any) -> int:
    return 1 + sum(size(v) for v in node.values()) if isinstance(node, dict) else (sum(size(v) for v in node) if isinstance(node, list) else 1)


def eval_program(node: Any, env: dict[str, Any], library: dict[str, dict[str, Any]] | None = None) -> Any:
    if isinstance(node, (str, int, float, bool)) or node is None:
        return node
    if "var" in node:
        return env[node["var"]]
    if "lit" in node:
        return node["lit"]
    op = node.get("op")
    args = node.get("args", [])
    if op == "and": return bool(eval_program(args[0], env, library)) and bool(eval_program(args[1], env, library))
    if op == "or": return bool(eval_program(args[0], env, library)) or bool(eval_program(args[1], env, library))
    if op == "not": return not bool(eval_program(args[0], env, library))
    if op == "eq": return eval_program(args[0], env, library) == eval_program(args[1], env, library)
    if op == "if": return eval_program(node["then"], env, library) if eval_program(node["cond"], env, library) else eval_program(node["else"], env, library)
    if op == "call":
        definition = (library or {})[node["name"]]
        local = {name: eval_program(arg, env, library) for name, arg in zip(definition["params"], args)}
        return eval_program(definition["body"], local, library)
    raise ValueError(f"unsupported AST node: {node}")


def _key(node: Any) -> str:
    return json.dumps(node, sort_keys=True, separators=(",", ":"))


def _canonical(node: Any, names: dict[str, str] | None = None) -> Any:
    names = {} if names is None else names
    if isinstance(node, dict) and isinstance(node.get("var"), str):
        name = node["var"]
        names.setdefault(name, f"$p{len(names)}")
        return {"var": names[name]}
    if isinstance(node, dict): return {key: _canonical(value, names) for key, value in node.items()}
    if isinstance(node, list): return [_canonical(value, names) for value in node]
    return node


def _instantiate(node: Any, mapping: dict[str, Any]) -> Any:
    if isinstance(node, dict) and isinstance(node.get("var"), str) and node["var"].startswith("$p"):
        return mapping[node["var"]]
    if isinstance(node, dict): return {key: _instantiate(value, mapping) for key, value in node.items()}
    if isinstance(node, list): return [_instantiate(value, mapping) for value in node]
    return node


def _match(node: Any, pattern: Any, mapping: dict[str, Any]) -> bool:
    if isinstance(pattern, dict) and isinstance(pattern.get("var"), str) and pattern["var"].startswith("$p"):
        prior = mapping.setdefault(pattern["var"], node)
        return _key(prior) == _key(node)
    if isinstance(node, dict) and isinstance(pattern, dict):
        return node.keys() == pattern.keys() and all(_match(node[key], pattern[key], mapping) for key in node)
    if isinstance(node, list) and isinstance(pattern, list):
        return len(node) == len(pattern) and all(_match(left, right, mapping) for left, right in zip(node, pattern))
    return node == pattern


def _walk(node: Any):
    yield node
    if isinstance(node, dict):
        for value in node.values(): yield from _walk(value)
    elif isinstance(node, list):
        for value in node: yield from _walk(value)


def _variables(node: Any) -> list[str]:
    return sorted({part["var"] for part in _walk(node) if isinstance(part, dict) and isinstance(part.get("var"), str)})


def _replace(node: Any, target: Any, replacement: Any) -> tuple[Any, bool]:
    if _key(node) == _key(target): return replacement, True
    if isinstance(node, dict):
        for key, value in node.items():
            new, changed = _replace(value, target, replacement)
            if changed:
                out = dict(node); out[key] = new; return out, True
    if isinstance(node, list):
        for index, value in enumerate(node):
            new, changed = _replace(value, target, replacement)
            if changed:
                out = list(node); out[index] = new; return out, True
    return node, False


def _binding_suffix(names):
    """Shared trailing identifier tokens across the observed argument bindings."""
    tokens = [name.split("_")[::-1] for name in sorted(set(names))]
    parts = []
    for column in zip(*tokens):
        if len(set(column)) != 1:
            break
        parts.append(column[0])
    return "_".join(reversed(parts))


def learn_library(tasks: list[dict[str, Any]], *, max_defs: int = 16) -> dict[str, Any]:
    counts = Counter(_key(_canonical(part)) for task in tasks for part in _walk(task["program"]) if isinstance(part, dict) and ("op" in part))
    candidates = [(count, key) for key, count in counts.items() if count >= 2]
    candidates.sort(key=lambda item: (-item[0], item[1]))
    library: dict[str, dict[str, Any]] = {}
    inventions = []
    current_programs = [task["program"] for task in tasks]
    current_total = sum(size(program) for program in current_programs)
    definition_cost = 0
    for index, (count, key) in enumerate(candidates):
        body = json.loads(key)
        params = _variables(body)
        name = f"invented_{index:03d}"
        before = current_total
        rewritten = []
        bindings = []
        for original in current_programs:
            program, changed = original, False
            for part in list(_walk(original)):
                mapping: dict[str, Any] = {}
                if _match(part, body, mapping):
                    call = {"op": "call", "name": name, "args": [mapping[param] for param in params]}
                    program, changed = _replace(original, part, call)
                    bindings.append(mapping)
                    break
            rewritten.append(program)
        matches = sum(program != original for program, original in zip(rewritten, current_programs))
        if matches < 2:
            continue
        after = sum(size(program) for program in rewritten) + definition_cost + size(body) + 2
        if after >= before: continue
        if len(library) >= max_defs: break
        current_programs = rewritten
        current_total = after
        definition_cost += size(body) + 2
        suffixes = {param: _binding_suffix([m[param]["var"] for m in bindings])
                    for param in params if all("var" in m[param] for m in bindings)}
        library[name] = {"params": params, "body": body, "support": matches,
                         "argument_suffixes": suffixes}
        inventions.append({"name": name, "support": matches, "body": body, "params": params, "compression": before - after})
    return {"schema": "capcov/synthesis-library/v1", "library": library, "inventions": inventions}


def synthesize(tasks: list[dict[str, Any]], library_doc: dict[str, Any], *, max_tasks: int = 100) -> dict[str, Any]:
    library = library_doc.get("library", {})
    results = []
    for task in sorted(tasks, key=lambda item: item["id"])[:max_tasks]:
        baseline = task["program"]
        candidate = baseline
        used = []
        for name, definition in sorted(library.items()):
            changed = False
            for part in list(_walk(candidate)):
                mapping: dict[str, Any] = {}
                if _match(part, definition["body"], mapping):
                    call = {"op": "call", "name": name, "args": [mapping[param] for param in definition["params"]]}
                    candidate, changed = _replace(candidate, part, call)
                    break
            if changed: used.append(name)
        train_ok = all(eval_program(candidate, ex["input"], library) == ex["output"] for ex in task.get("examples", []))
        sealed = task.get("heldout_examples", [])
        candidate_ok = train_ok and bool(used) and all(eval_program(candidate, ex["input"], library) == ex["output"] for ex in sealed)
        baseline_ok = all(eval_program(baseline, ex["input"], {}) == ex["output"] for ex in sealed)
        results.append({"id": task["id"], "used_invented": used, "training_pass": train_ok,
                        "candidate_heldout_pass": candidate_ok, "baseline_heldout_pass": baseline_ok,
                        "heldout_count": len(sealed)})
    return {"schema": "capcov/synthesis-evaluation/v1", "results": results,
            "counts": {"candidate_pass": sum(r["candidate_heldout_pass"] for r in results),
                       "baseline_pass": sum(r["baseline_heldout_pass"] for r in results)}}


def synthesize_target(target: dict[str, Any], library_doc: dict[str, Any], *, max_terms: int = 100000,
                      max_cost: int = 12) -> dict[str, Any]:
    from .boolean_search import search

    if "program" in target:
        raise ValueError("synthesis target must not include a program")
    fit = target["train_examples"]
    sealed = target["heldout_examples"]
    if not sealed:
        raise ValueError("target needs sealed test examples")
    library = library_doc["library"]
    # Only fit examples cross the search interface. Score both frozen ASTs
    # afterward, including a simpler solution that uses no learned calls.
    candidate = search(fit, library, max_terms=max_terms, max_cost=max_cost)
    baseline = search(fit, {}, max_terms=max_terms, max_cost=max_cost)

    def score(run, definitions):
        ast = run["program"]
        if ast is None:
            return None
        if not all(eval_program(ast, row["input"], definitions) == row["output"] for row in fit):
            raise RuntimeError("search interpreter and reference interpreter disagree on fit data")
        return sum(eval_program(ast, row["input"], definitions) == row["output"] for row in sealed)

    candidate_score = score(candidate, library)
    baseline_score = score(baseline, {})
    return {"schema": "capcov/synthesis-target-evaluation/v1", "target_id": target["id"],
            "selected_candidate": candidate["program"], "baseline_candidate": baseline["program"],
            "search": {"candidate": candidate, "baseline": baseline, "max_terms": max_terms, "max_cost": max_cost},
            "candidate_heldout_pass": candidate_score == len(sealed),
            "baseline_heldout_pass": baseline_score == len(sealed),
            "candidate_correct": candidate_score, "baseline_correct": baseline_score,
            "fit_count": len(fit), "sealed_count": len(sealed)}


def run_synthesis(input_path: Path, output_path: Path, *, max_defs: int = 16, max_tasks: int = 100,
                  max_terms: int = 100000, max_cost: int = 7, induce_training: bool = False) -> None:
    from .boolean_search import search

    document = json.loads(input_path.read_text())
    tasks = document.get("tasks", [])
    if not 0 < len(tasks) <= max_tasks:
        raise ValueError("training corpus must be nonempty and within max_tasks")
    target = document.get("target")
    ids = [task["id"] for task in tasks]
    if len(set(ids)) != len(ids) or (target is not None and target["id"] in ids):
        raise ValueError("training tasks and target must have distinct IDs")
    if isinstance(target, dict) and "program" in target:
        raise ValueError("target must not contain a solved program")
    valid_tasks = []
    invalid = []
    wake = []
    for task in tasks:
        try:
            examples = task["examples"]
            if induce_training:
                if "program" in task:
                    raise ValueError("observation-only wake tasks must omit the program")
                solution = search(examples, {}, max_terms=max_terms, max_cost=max_cost)
                wake.append({"task": task["id"], **solution})
                if solution["program"] is None:
                    raise ValueError("no fit within sibling search budget")
                task = {**task, "program": solution["program"]}
            if not examples or any(eval_program(task["program"], ex["input"], {}) != ex["output"] for ex in examples):
                raise ValueError("solved program does not match all training examples")
            valid_tasks.append(task)
        except (KeyError, TypeError, ValueError) as error:
            invalid.append({"id": task.get("id"), "reason": str(error)})
    library = learn_library(valid_tasks, max_defs=max_defs)
    result = synthesize_target(target, library, max_terms=max_terms, max_cost=max_cost) if isinstance(target, dict) else synthesize(valid_tasks, library, max_tasks=max_tasks)
    output_path.write_text(json.dumps({"library": library, "evaluation": result, "wake": wake,
                                       "invalid_training_tasks": invalid}, indent=2, sort_keys=True) + "\n")
