import json
import subprocess
import sys

from capcov.operations.search_workflow import workflow


def project(tmp_path, operation):
    path = tmp_path / "project.json"
    path.write_text(
        json.dumps({"schema": "capcov/search-project/v1", "operations": [operation]})
    )
    return path


def operation():
    return {
        "id": "validate",
        "revision": "v1",
        "siblings": [],
        "objective": "find_fit",
        "target": {
            "id": "validate",
            "examples": [
                {"input": {"x": True}, "output": True},
                {"input": {"x": False}, "output": False},
            ],
        },
        "bounds": {"max_cost": 4, "max_terms": 1000, "max_probes": 4},
    }


def test_agent_command_resume_and_replay(tmp_path):
    op = operation()
    path = project(tmp_path, op)
    base = [sys.executable, "-m", "capcov", "reason"]

    def invoke(mode):
        raw = subprocess.check_output(
            [*base, mode, "--operation", op["id"], "--project", str(path), "--json"]
        )
        return json.loads(raw)

    assert invoke("next")["next_action"]["kind"] == "run_synthesis"
    run = invoke("run")
    assert run["next_action"]["kind"] == "replay_candidate"
    assert invoke("next") == run
    op["candidate_replay"] = {
        "revision": "v1",
        "source": "synthetic run",
        "observations": op["target"]["examples"],
    }
    project(tmp_path, op)
    assert invoke("next")["next_action"]["kind"] == "scoped_cases_match"
    op["revision"] = "v2"
    project(tmp_path, op)
    assert invoke("next")["next_action"]["kind"] == "run_synthesis"
    assert invoke("run")["next_action"]["kind"] == "replay_candidate"


def test_open_review_and_owner_decision(tmp_path):
    op = operation()
    del op["target"]
    evidence = {
        "id": "owner",
        "revision": "v1",
        "path": "decisions#1",
        "kind": "owner_decision",
        "status": "confirmed",
    }
    packet = {
        "schema": "capcov/intent-packet/v1",
        "packet_id": "obligation",
        "question": "write?",
        "cases": [{"id": "full", "input": {}, "php_output": True}],
        "evidence": [evidence],
    }
    op["intent"] = {"packet": packet}

    def kind():
        return workflow(project(tmp_path, op), op["id"], False, tmp_path)[
            "next_action"
        ]["kind"]

    assert kind() == "request_expert_review"
    op["intent"]["recommendation"] = {"source_ids": ["owner"]}
    assert kind() == "resolve_product_intent"
    op["intent"]["accepted"] = {
        "accepted": True,
        "packet_schema": packet["schema"],
        "packet_id": "obligation",
        "authority": "owner",
        "source_ids": ["owner"],
        "intended_outputs": {"full": False},
    }
    assert kind() == "replay_candidate"
    packet["cases"][0]["go_output"] = True
    assert kind() == "repair_candidate"
    packet["cases"][0]["go_output"] = False
    assert kind() == "scoped_cases_match"


def test_witness_becomes_an_independent_binding_contract(tmp_path):
    op = operation()
    op["objective"] = "find_disagreement"
    op["target"] = {
        "id": "target",
        "examples": [
            {"input": {"a": False, "b": False}, "output": False},
            {"input": {"a": True, "b": True}, "output": True},
        ],
        "reachable_inputs": [{"id": "mixed", "input": {"a": True, "b": False}}],
    }

    def run():
        return workflow(project(tmp_path, op), op["id"], True, tmp_path)

    action = run()["next_action"]
    assert action["kind"] == "observe_disagreement"
    packet = action["intent_packet"]
    packet["cases"][0].update(php_output=True, go_output=False)
    packet["evidence"].append(
        {
            "id": "policy",
            "revision": "r2",
            "path": "rules:2",
            "kind": "owner_decision",
            "status": "confirmed",
        }
    )
    op["intent"] = {
        "packet": packet,
        "accepted": {
            "accepted": True,
            "authority": "owner",
            "source_ids": ["policy"],
            "intended_outputs": {"mixed": False},
            "packet_id": packet["packet_id"],
            "packet_schema": packet["schema"],
        },
    }
    rerun = run()
    assert rerun["intent"]["classifications"][0]["classification"] == "go-only"
    assert rerun["search"]["evaluation"]["known_examples"] == 3
    assert (
        rerun["search"]["evaluation"]["probes"][0]["outcome"]
        == "covered_feature_vector"
    )


def test_existing_inventory_supplies_target_and_bounded_siblings(tmp_path):
    rows = operation()["target"]["examples"]
    inventory = {
        name: {"family": family, "examples": rows}
        for name, family in [("a", "f"), ("b", "f"), ("c", "f"), ("other", "g")]
    }
    (tmp_path / "bank.json").write_text(json.dumps({"operations": inventory}))
    path = tmp_path / "project.json"
    path.write_text(
        json.dumps(
            {
                "schema": "capcov/search-project/v1",
                "revision": "fixture",
                "corpus": "bank.json",
                "max_siblings": 1,
            }
        )
    )
    report = workflow(path, "c", True, tmp_path / "state")
    assert report["corpus_scope"]["eligible_siblings"] == 2
    assert [row["id"] for row in report["search"]["wake"]] == ["a"]
    assert report["search"]["evaluation"]["known_examples"] == len(rows)
