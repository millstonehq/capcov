import json
from capcov.synthesis import learn_library, synthesize, synthesize_target


def _task(i, heldout):
    body = {"op": "eq", "args": [{"var": "x"}, {"lit": True}]}
    return {"id": f"t{i}", "program": {"op": "and", "args": [body, body]},
            "examples": [{"input": {"x": True}, "output": True}],
            "heldout_examples": [{"input": {"x": heldout}, "output": heldout}]}


def test_invention_is_used_and_compresses():
    tasks = [_task(i, bool(i % 2)) for i in range(4)]
    library = learn_library(tasks)
    assert library["inventions"]
    result = synthesize(tasks, library)
    assert all(row["used_invented"] for row in result["results"])
    assert result["counts"]["candidate_pass"] == 4


def test_negative_heldout_control_does_not_pass():
    tasks = [_task(i, True) for i in range(4)]
    tasks[0]["heldout_examples"] = [{"input": {"x": False}, "output": True}]
    library = learn_library(tasks)
    result = synthesize(tasks, library)
    assert not result["results"][0]["candidate_heldout_pass"]
    json.dumps(result)


def test_target_has_no_program_and_is_sealed_after_selection():
    train = [_task(i, True) for i in range(4)]
    library = learn_library(train)
    target = {"id": "held-out", "train_examples": [
        {"input": {"x": True, "y": True}, "output": True},
        {"input": {"x": False, "y": True}, "output": False}],
        "heldout_examples": [{"input": {"x": True, "y": True}, "output": True}]}
    result = synthesize_target(target, library, max_terms=10000, max_cost=7)
    assert result["selected_candidate"] is not None
    assert result["selected_candidate"] == {"var": "x"}
    assert result["baseline_candidate"] == {"var": "x"}
    changed = dict(target, heldout_examples=[{"input": {"x": True, "y": True}, "output": False}])
    changed_result = synthesize_target(changed, library, max_terms=10000, max_cost=7)
    assert changed_result["selected_candidate"] == result["selected_candidate"]


def _walk(node):
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)
