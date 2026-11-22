from capcov.search_action import run_search


def test_target_is_excluded_from_invention_and_receipt_has_next_action():
    sibling = {
        "id": "sibling",
        "examples": [
            {"input": {"a": False, "b": False}, "output": False},
            {"input": {"a": True, "b": False}, "output": True},
            {"input": {"a": False, "b": True}, "output": True},
        ],
    }
    target = {
        "id": "target",
        "examples": [
            {"input": {"a": False, "b": False}, "output": False},
            {"input": {"a": True, "b": False}, "output": True},
        ],
        "reachable_inputs": [{"id": "unseen", "input": {"a": False, "b": True}}],
    }
    result = run_search(
        {"id": "op-1", "siblings": [sibling], "target": target}, "find_disagreement"
    )
    assert result["schema"] == "capcov/search-action/v1"
    assert result["target_id"] == "target"
    assert result["wake"][0]["id"] == "sibling"
    assert result["evaluation"]["known_examples"] == 2
    assert result["evaluation"]["probes"][0]["outcome"] == "disagreement"


def test_frozen_library_prevents_wake():
    operation = {
        "id": "op-2",
        "library": {"schema": "capcov/synthesis-library/v1", "library": {}},
        "siblings": [{"id": "must-not-run", "examples": []}],
        "target": {
            "id": "target",
            "examples": [{"input": {"a": False}, "output": False}],
        },
    }
    result = run_search(operation)
    assert result["wake"] == []
    assert result["inventions"] == []
