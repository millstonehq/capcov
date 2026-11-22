import json, subprocess, sys

def test_batch_cli_rejects_contradictions_and_bounds(tmp_path):
    doc = {"operations": {"a": {"family": "docs", "examples": [{"input": {"x": True}, "output": True}, {"input": {"x": False}, "output": False}]}, "bad": {"family": "docs", "examples": [{"input": {"x": True}, "output": True}, {"input": {"x": True}, "output": False}]}}}
    source = tmp_path / "in.json"; out = tmp_path / "out.json"; source.write_text(json.dumps(doc))
    subprocess.run([sys.executable, "-m", "capcov", "synthesize-batch", str(source), "--out", str(out)], check=True)
    report = json.loads(out.read_text()); assert report["included_operations"] == 1; assert report["excluded_operations"] == 1
    assert report["families"][0]["invention_success"] is False


def test_batch_uses_the_entire_target_contract_and_disjoint_siblings():
    from capcov.synthesis_batch import build
    examples = [{"input": {"x": x, "y": y}, "output": x != y}
                for x in (False, True) for y in (False, True)]
    result = build({"operations": {
        name: {"family": "f", "examples": examples} for name in ("sibling", "target")}})
    family, = result["families"]
    assert family["training_operations"] == ["sibling"]
    assert family["target_operation"] == "target"
    assert family["target_contract_examples"] == 4
    assert family["candidate_contract_fit"] and family["baseline_contract_fit"]
