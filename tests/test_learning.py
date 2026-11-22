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
    assert result["candidate_status_counts"]["mismatch"] > 0
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
    assert result["candidate_status_counts"]["mismatch"] > 0
    assert result["rule_status_counts"].get("exception", 0) == 0


def test_statuses_are_orthogonal_and_missing_candidate_is_unknown():
    operations = {}
    for index in range(10):
        operations[f"op-{index}"] = {
            "family": "docs",
            "observations": [{"cell": "ok", "input": "x", "output": "different"}],
            "candidate_observations": [{"cell": "ok", "input": "x", "output": "wrong"}],
        }
    full = evaluate_document({"operations": operations}, holdout_mod=2)
    rows = [r for r in full["results"] if "rule_status" in r]
    assert any(r["rule_status"] == "supported" and r["candidate_status"] == "mismatch" for r in rows)
    prefix_ops = {f"op-{i}": {"observations": [["!a", "a"], ["!b", "different" if i == 0 else "b"]],
                              "candidate_observations": [["!a", "a"], ["!b", "wrong"]]} for i in range(10)}
    prefix = evaluate_document({"operations": prefix_ops}, holdout_mod=2)
    assert any(r.get("rule_status") == "exception" and r.get("candidate_status") == "mismatch"
               for r in prefix["results"])
    for value in operations.values():
        value.pop("candidate_observations")
    missing = evaluate_document({"operations": operations}, holdout_mod=2)
    assert missing["candidate_status_counts"]["unknown"] > 0


def test_partial_candidate_report_preserves_missing_cell():
    from capcov.learning import _split, evaluate_document

    def sample(cell):
        return {"cell": cell, "input": cell, "output": "observed"}

    ops = {str(i): {"family": "f", "observations": [sample("a"), sample("b")]} for i in range(20)}
    for name, op in ops.items():
        if _split(name, 2, 0) == "holdout":
            op["candidate_observations"] = [sample("a")]
    result = evaluate_document({"operations": ops}, holdout_mod=2)
    for row in result["results"]:
        assert row["candidate_status"] == ("match" if row["cell"] == "a" else "unknown")
