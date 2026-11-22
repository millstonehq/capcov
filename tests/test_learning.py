import json

from capcov.learning import evaluate_document, learn_document, mine


def test_mine_retains_exception_and_missing_probe():
    rules = mine([("!a", "a"), ("!b", "b"), ("!c", "different")])
    assert rules[0]["support"] == ["!a", "!b"]
    assert rules[0]["exceptions"] == ["!c"]
    assert rules[0]["probes"] == ["!"]


def test_inventory_emits_family_relation_and_unresolved():
    doc = {
        "operations": {
            "a": {"family": "docs", "observations": [
                {"cell": "ok", "input": "x", "output": "200"},
                {"cell": "missing", "input": "y", "output": "404"},
            ]},
            "b": {"family": "docs", "observations": [
                {"cell": "ok", "input": "x", "output": "200"},
                {"cell": "missing", "input": "y", "output": "404"},
            ]},
            "bad": {"family": "docs", "observations": "unknown"},
        }
    }
    result = learn_document(doc)
    assert {r["cell"] for r in result["relations"]} == {"missing", "ok"}
    assert all(len(r["support"]) == 1 for r in result["relations"])
    assert result["operations"][-1]["status"] == "unresolved"
    json.dumps(result)


def test_evaluator_does_not_leak_holdout_and_classifies_mismatch():
    operations = {}
    for index in range(10):
        operation = f"op-{index}"
        operations[operation] = {"observations": [["!a", "a"], ["!b", "b"]],
                                 "candidate_observations": [["!a", "a"], ["!b", "WRONG"]]}
    result = evaluate_document({"operations": operations}, holdout_mod=2, holdout_value=0)
    assert set(result["split"]["training_operations"]).isdisjoint(result["split"]["holdout_operations"])
    assert result["counts"]["candidate_mismatch"] > 0
    assert all(r["operation"] in result["split"]["holdout_operations"] for r in result["results"])


def test_evaluator_checks_named_cells_and_preserves_incumbent_exception():
    operations = {}
    for index in range(10):
        operations[f"op-{index}"] = {
            "family": "docs",
            "observations": [{"cell": "ok", "input": "x", "output": "200"}],
            "candidate_observations": [{"cell": "ok", "input": "x", "output": "500"}],
        }
    result = evaluate_document({"operations": operations}, holdout_mod=2)
    assert result["counts"]["candidate_mismatch"] > 0
    assert result["counts"].get("incumbent_exception", 0) == 0
