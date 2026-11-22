import json
from itertools import product
from pathlib import Path
import subprocess
import sys

from capcov.boolean_search import search
from capcov.synthesis import eval_program, learn_library


def test_bound_call_prior_and_unrelated_inputs():
    # No production or role in the learner refers to these features.
    def var(name):
        return {"var": name}

    def binary(op, a, b):
        return {"op": op, "args": [a, b]}

    def body(prefix):
        return binary("or", var(prefix + "_a"), binary("and", var(prefix + "_b"),
                      binary("eq", var(prefix + "_c"), var(prefix + "_d"))))

    programs = [{"id": str(i), "program": binary("or", var("s"), body("f" + str(i)))} for i in range(12)]
    lib = learn_library(programs)["library"]
    assert lib
    examples = []
    for bits in product((False, True), repeat=5):
        env = dict(zip(["q_a", "q_b", "q_c", "q_d", "mode"], bits))
        examples.append({"input": env, "output": env["mode"] and eval_program(body("q"), env)})
    trial = search(examples, lib, max_terms=500000, max_cost=12)
    assert trial["stop"] == "fit"
    assert '"call"' in json.dumps(trial["program"])
    assert all(eval_program(trial["program"], row["input"], lib) == row["output"] for row in examples)
    assert search(examples, {}, max_terms=1, max_cost=12)["program"] is None


def test_wake_cli_reads_program_free_siblings(tmp_path):
    # The CLI must actually solve these tasks. Supplying the target answer to
    # the wake phase would cause the disjoint-ID or leakage check to fail.
    rows = [{"input": {"x": x, "y": y}, "output": x != y} for x, y in product((False, True), repeat=2)]
    path = tmp_path / "in.json"
    out = tmp_path / "result.json"
    target = {"id": "new", "train_examples": rows, "heldout_examples": rows[:1]}
    path.write_text(json.dumps({"tasks": [{"id": "one", "examples": rows}], "target": target}))
    subprocess.run([sys.executable, "-m", "capcov", "synthesize", str(path), "--induce-training",
                    "--out", str(out)], check=True)
    report = json.loads(out.read_text())
    assert len(report["wake"]) == 1 and report["wake"][0]["stop"] == "fit"
    assert report["evaluation"]["candidate_correct"] == 1
    assert report["invalid_training_tasks"] == []
