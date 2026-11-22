"""Fit every known Boolean obligation and search for a reachable disagreement."""
from .boolean_search import search
from .synthesis import eval_program


def evaluate(contract, library, *, max_terms, max_cost, max_probes, policy=None):
    known = contract["examples"]
    if not known or max_probes < 1:
        raise ValueError("contract requires known answers and a positive probe bound")
    labels = {}
    for example in known:
        key = tuple(sorted(example["input"].items()))
        if key in labels and labels[key] != example["output"]:
            raise ValueError("known answers conflict on the supplied feature representation")
        labels[key] = example["output"]
    limits = {"max_terms": max_terms, "max_cost": max_cost}
    def fit(examples, definitions, recognition=policy):
        run = search(examples, definitions, policy=recognition, **limits)
        program = run["program"]
        if program is not None and any(eval_program(program, e["input"], definitions) != e["output"] for e in examples):
            raise RuntimeError("program failed independent acceptance interpreter")
        return run

    primary = fit(known, library)
    baseline = fit(known, {}, None)
    report = {"schema": "capcov/full-contract/v1", "target_id": contract["id"],
              "known_examples": len(known), "distinct_known": len(labels), "limits": limits,
              "assisted": primary, "baseline": baseline, "selected_candidate": primary["program"], "probes": []}
    if primary["program"] is None:
        report["outcome"] = "no_assisted_fit_within_budget"
        return report
    pool = contract.get("reachable_inputs", [])
    report["offered_probes"] = len(pool)
    report["unexamined_probes"] = max(0, len(pool) - max_probes)
    for probe in pool[:max_probes]:
        inputs = probe["input"]
        if set(inputs) != set(known[0]["input"]) or any(type(v) is not bool for v in inputs.values()):
            raise ValueError("reachable probe must use the contract's Boolean input schema")
        key = tuple(sorted(inputs.items()))
        row = {"id": probe["id"]}
        if key in labels:
            row["outcome"] = "covered_feature_vector"
        else:
            predicted = eval_program(primary["program"], inputs, library)
            alt = fit(known + [{"input": inputs, "output": not predicted}], library)
            row["search_stop"] = alt["stop"]
            row["attempts"] = alt["attempts"]
            if alt["program"] is None:
                row["outcome"] = "no_witness_within_budget"
            else:
                row.update(outcome="disagreement", assisted_prediction=predicted,
                           alternative_prediction=not predicted, alternative=alt["program"])
        report["probes"].append(row)
    report["outcome"] = "contract_fit"
    return report
