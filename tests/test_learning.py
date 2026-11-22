import json

from capcov.learning import learn_document, mine


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
    assert result["relations"][0]["cells"] == ["missing", "ok"]
    assert result["operations"][-1]["status"] == "unresolved"
    json.dumps(result)
