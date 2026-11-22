import json, subprocess
from pathlib import Path
from capcov.go_emit import emit

def test_emit_go_compiles_and_runs(tmp_path):
    report = {"library": {"library": {"invented_000": {"params": ["$p0"], "body": {"op": "not", "args": [{"var": "$p0"}]}}}},
              "evaluation": {"selected_candidate": {"op": "call", "name": "invented_000", "args": [{"var": "flag"}]}}}
    out = tmp_path / "synth.go"
    emit(report, out)
    (tmp_path / "go.mod").write_text("module example\n\ngo 1.20\n")
    (tmp_path / "synth_test.go").write_text('package synthesized\nimport "testing"\nfunc TestGenerated(t *testing.T){if Synthesized(true)||!Synthesized(false){t.Fatal()}}\n')
    subprocess.run(["gofmt", "-w", str(out), str(tmp_path / "synth_test.go")], check=True)
    subprocess.run(["go", "test", "."], cwd=tmp_path, check=True, capture_output=True, text=True)

def test_emit_rejects_unknown_variable(tmp_path):
    report = {"library": {"library": {"f": {"params": [], "body": {"var": "missing"}}}}, "evaluation": {"selected_candidate": {"op": "call", "name": "f", "args": []}}}
    try:
        emit(report, tmp_path / "x.go")
    except ValueError as error:
        assert "unknown variable" in str(error)
    else:
        raise AssertionError("expected invalid variable")
