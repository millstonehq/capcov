import json
from pathlib import Path

from capcov.search_action import run_search


EXAMPLES = Path(__file__).parents[1] / "examples" / "search-projects"


def operation(name):
    document = json.loads((EXAMPLES / name).read_text())
    return document["operations"][0]


def test_invention_example_learns_and_reuses_a_definition():
    result = run_search(operation("invention.json"))
    assert result["inventions"]
    assert result["evaluation"]["assisted"]["program"]["op"] == "call"
    assert result["evaluation"]["baseline"]["program"]["op"] != "call"


def test_examples_route_existing_intent(tmp_path):
    from capcov.operations.search_workflow import workflow
    for name, expected in [("building.json", "resolve_product_intent"), ("seat.json", "replay_candidate")]:
        op = operation(name)
        result = workflow(EXAMPLES / name, op["id"], False, tmp_path)
        assert result["next_action"]["kind"] == expected
        assert result["intent"]["binding"] == (name == "seat.json")
