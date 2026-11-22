import json, subprocess, sys


def packet():
    return {
        "schema": "capcov/intent-packet/v1",
        "packet_id": "p1",
        "question": "which output is intended?",
        "evidence": [
            {
                "id": "php-1",
                "status": "confirmed",
                "kind": "owner_decision",
                "revision": "abc",
                "path": "decisions.md:1",
            },
            {"id": "go-1", "status": "unknown"},
        ],
        "cases": [
            {
                "id": "c1",
                "input": {"x": True},
                "php_output": 1,
                "go_output": 2,
                "reference_output": 1,
            }
        ],
        "alternatives": [{"id": "a", "condition": "x"}],
    }


def test_adjudication_cannot_self_promote(tmp_path):
    p = tmp_path / "p.json"
    r = tmp_path / "r.json"
    o = tmp_path / "o.json"
    p.write_text(json.dumps(packet()))
    r.write_text(
        json.dumps(
            {"source_ids": ["php-1"], "recommendation": {"c1": 1}, "binding": True}
        )
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "capcov",
            "intent",
            "adjudicate",
            str(p),
            str(r),
            "--out",
            str(o),
        ],
        check=True,
    )
    x = json.loads(o.read_text())
    assert (
        x["binding"] is False and x["classifications"][0]["classification"] == "unknown"
    )


def test_prompt_and_explicit_acceptance(tmp_path):
    p = tmp_path / "p.json"
    r = tmp_path / "r.json"
    a = tmp_path / "a.json"
    o = tmp_path / "o.json"
    p.write_text(json.dumps(packet()))
    r.write_text(json.dumps({"source_ids": ["php-1"]}))
    a.write_text(
        json.dumps(
            {
                "accepted": True,
                "packet_schema": "capcov/intent-packet/v1",
                "packet_id": "p1",
                "authority": "review-board",
                "source_ids": ["php-1"],
                "intended_outputs": {"c1": 1},
            }
        )
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "capcov",
            "intent",
            "prompt",
            str(p),
            "--out",
            str(tmp_path / "prompt.json"),
        ],
        check=True,
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "capcov",
            "intent",
            "adjudicate",
            str(p),
            str(r),
            "--accepted",
            str(a),
            "--out",
            str(o),
        ],
        check=True,
    )
    assert json.loads(o.read_text())["binding"] is True


def test_partial_intent_and_neither():
    from capcov.intent import adjudicate
    import pytest

    p = packet()
    p["cases"] = [
        {
            "id": "one",
            "input": {},
            "php_output": {"v": True},
            "go_output": {"v": True},
            "reference_output": {"v": 1},
        },
        {"id": "two", "input": {}, "php_output": None},
        {"id": "three", "input": {}, "php_output": None, "go_output": None},
    ]
    a = dict(
        accepted=True,
        packet_schema=p["schema"],
        packet_id="p1",
        authority="owner",
        source_ids=["php-1"],
        intended_outputs={"one": {"v": 1}, "two": None},
    )
    r = adjudicate(p, {"source_ids": ["php-1"]}, a)
    assert [c["classification"] for c in r["classifications"]] == [
        "neither",
        "unknown",
        "unknown",
    ]
    assert r["classifications"][1]["matches_intent"] == {
        "go": None,
        "reference": None,
        "php": True,
    }
    assert len(r["synthesis_contracts"]) == 2
    with pytest.raises(ValueError):
        adjudicate(p, {"source_ids": ["php-1"]}, dict(a, source_ids=["go-1"]))
