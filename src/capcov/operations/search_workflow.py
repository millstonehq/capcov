"""Resumable operation-scoped synthesis and evidence routing."""

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

from ..intent import adjudicate, prompt_packet


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def load_operation(project, operation_id):
    document = json.loads(project.read_text())
    if document.get("schema") != "capcov/search-project/v1":
        raise ValueError("unsupported search project schema")
    rows = [r for r in document.get("operations", []) if r["id"] == operation_id]
    if len(rows) > 1:
        raise ValueError("duplicate operation ID")
    op = rows[0] if rows else {"id": operation_id}
    if "target" not in op and "intent" not in op and "corpus" in document:
        corpus = json.loads((project.parent / document["corpus"]).read_text())
        inventory = corpus["operations"]
        if operation_id not in inventory:
            raise ValueError("operation missing from projected corpus")
        target = inventory[operation_id]
        if not target.get("family"):
            raise ValueError("projected target requires a sibling family")
        members = sorted(
            k
            for k, v in inventory.items()
            if k != operation_id
            and v.get("family") == target["family"]
            and v.get("examples")
        )
        limit = document.get("max_siblings", 32)
        if type(limit) is not int or limit < 0:
            raise ValueError("max_siblings must be a nonnegative integer")
        op = {
            **op,
            "target": {
                "id": operation_id,
                "examples": target["examples"],
                "reachable_inputs": target.get("reachable_inputs", []),
            },
            "siblings": [
                {"id": k, "examples": inventory[k]["examples"]} for k in members[:limit]
            ],
            "corpus_scope": {
                "family": target["family"],
                "eligible_siblings": len(members),
                "used_siblings": len(members[:limit]),
                "source_bank_digest": corpus.get("source_bank_digest"),
            },
        }
    revision = op.get("revision", document.get("revision"))
    if not revision or not (op.get("target") or op.get("intent")):
        raise ValueError(
            "expected one matching operation with a revision and contract or intent"
        )
    return {
        **op,
        "revision": revision,
        "bounds": op.get("bounds", document.get("bounds", {})),
    }


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        try:
            json.dump(value, f, indent=2, sort_keys=True)
            f.write("\n")
            f.close()
            os.replace(f.name, path)
        finally:
            Path(f.name).unlink(missing_ok=True)


def workflow(project, operation_id, execute, state):
    op = load_operation(project, operation_id)
    revision = op["revision"]
    status = {
        "schema": "capcov/operation-next/v1",
        "operation": operation_id,
        "revision": revision,
    }

    if "corpus_scope" in op:
        status["corpus_scope"] = op["corpus_scope"]

    def action(kind, **details):
        return {**status, "next_action": {"kind": kind, **details}}

    def replay(case):
        return action(
            "replay_candidate",
            case=case,
            argv=op.get("replay_argv"),
            requires="candidate observation at the declared revision",
        )

    intent = op.get("intent")
    if intent:
        packet = intent["packet"]
        status["obligation"] = packet["packet_id"]
        recommendation = intent.get("recommendation")
        decision = intent.get("accepted")
        if recommendation is None and decision is None:
            return action("request_expert_review", prompt=prompt_packet(packet))
        # A previously confirmed policy can be consumed directly.
        evidence_review = (
            recommendation
            if recommendation is not None
            else {"source_ids": decision["source_ids"], "origin": "decision record"}
        )
        report = adjudicate(packet, evidence_review, decision)
        status["intent"] = report
        for row in report["classifications"]:
            if "intended" in row["missing"]:
                return action(
                    "resolve_product_intent",
                    case=row["id"],
                    question=packet["question"],
                    owner="domain_owner",
                )
            if "go" in row["missing"]:
                return replay(row["id"])
        if "target" not in op:
            failures = [
                r["id"]
                for r in report["classifications"]
                if r["matches_intent"]["go"] is False
            ]
            return (
                action("repair_candidate", cases=failures)
                if failures
                else action("scoped_cases_match")
            )
        target = dict(op["target"])
        target["examples"] = [*target["examples"], *report["synthesis_contracts"]]
        op = {**op, "target": target}

    objective = op.get("objective", "find_disagreement")
    engine_dir = Path(__file__).resolve().parents[1]
    engine = {
        name: hashlib.sha256((engine_dir / (name + ".py")).read_bytes()).hexdigest()
        for name in (
            "search_action",
            "boolean_search",
            "contract_search",
            "recognition",
            "synthesis",
        )
    }
    key = digest(
        [
            engine,
            {
                k: v
                for k, v in op.items()
                if k not in ("candidate_replay", "replay_argv")
            },
        ]
    )
    path = state / (key + ".json")
    status.update(search_key=key, receipt=str(path))
    if not path.exists() and not execute:
        return action(
            "run_synthesis",
            objective=objective,
            bounds=op.get("bounds", {}),
            argv=[
                "capcov",
                "reason",
                "run",
                "--operation",
                operation_id,
                "--project",
                str(project),
                "--state-dir",
                str(state),
                "--json",
            ],
        )
    if path.exists():
        receipt = json.loads(path.read_text())
        if (
            receipt["key"] != key
            or digest(receipt["result"]) != receipt["result_digest"]
        ):
            raise ValueError("search receipt integrity mismatch")
        result = receipt["result"]
    else:
        from ..search_action import run_search

        result = run_search(op, objective)
        save(path, {"key": key, "result_digest": digest(result), "result": result})
    status["search"] = result
    evaluation = result["evaluation"]
    if evaluation["selected_candidate"] is None:
        return action("review_search_bounds", stop=evaluation["assisted"]["stop"])
    witnesses = [p for p in evaluation["probes"] if p["outcome"] == "disagreement"]
    if witnesses:
        probe = witnesses[0]
        raw = next(
            r for r in op["target"]["reachable_inputs"] if r["id"] == probe["id"]
        )
        return action(
            "observe_disagreement",
            probe=raw,
            predictions=probe,
            intent_packet={
                "schema": "capcov/intent-packet/v1",
                "packet_id": f"{operation_id}/{probe['id']}",
                "question": "Which output should this operation produce on the distinguishing input?",
                "alternatives": [
                    {
                        "id": "selected",
                        "program": evaluation["selected_candidate"],
                        "output": probe["assisted_prediction"],
                    },
                    {
                        "id": "alternative",
                        "program": probe["alternative"],
                        "output": probe["alternative_prediction"],
                    },
                ],
                "cases": [{"id": probe["id"], "input": raw["input"]}],
                "evidence": [
                    {
                        "id": "search-receipt",
                        "kind": "bounded_search",
                        "status": "observed",
                        "revision": revision,
                        "path": str(path),
                    }
                ],
            },
            argv=op.get("replay_argv"),
        )
    observed = op.get("candidate_replay")
    if observed is None or observed.get("revision") != revision:
        return replay(op["target"]["id"])
    if not observed.get("source"):
        raise ValueError("candidate replay requires an evidence source")
    actual = {}
    for row in observed["observations"]:
        key = digest(row["input"])
        if key in actual and digest(actual[key]) != digest(row["output"]):
            raise ValueError("conflicting replay observations for one projected input")
        actual[key] = row["output"]
    missing, failures = [], []
    for row in op["target"]["examples"]:
        key = digest(row["input"])
        if key not in actual:
            missing.append(row["input"])
        elif digest(actual[key]) != digest(row["output"]):
            failures.append({**row, "observed": actual[key]})
    if failures:
        return action("repair_candidate", cases=failures, source=observed["source"])
    if missing:
        return action(
            "replay_candidate", missing_inputs=missing, argv=op.get("replay_argv")
        )
    return action(
        "scoped_cases_match",
        source=observed["source"],
        examples=evaluation["known_examples"],
    )


def main(argv):
    if not argv or argv[0] not in ("next", "run"):
        parser = argparse.ArgumentParser(prog="capcov reason", description="Orchestrate DreamCoder-style search and scoped replay.")
        parser.add_argument("command", choices=["next", "run"], help="next selects the agent action; run executes wake/sleep search")
        parser.parse_args(argv)
    command = argv[0]
    descriptions = {
        "next": "Inspect an operation and return its next action as JSON. Dispatch next_action.argv when synthesis is due.",
        "run": "Run DreamCoder-style Boolean search: solve siblings, invent reusable functions, train a search ranking on synthetic tasks, and fit the full target contract. A reachable disagreement becomes an intent or replay obligation.",
    }
    parser = argparse.ArgumentParser(prog=f"capcov reason {command}", description=descriptions[command],
        epilog=f"Example: capcov reason {command} --operation invention-demo --project examples/search-projects/invention.json --json")
    parser.add_argument("--project", type=Path, default=Path("capcov.search.json"), help="search project JSON (default: capcov.search.json)")
    parser.add_argument("--operation", required=True, help="operation or projected corpus census ID")
    parser.add_argument(
        "--emit-go", type=Path, help="write the selected Boolean candidate as Go"
    )
    parser.add_argument("--state-dir", type=Path, help="directory for reusable source-bound search receipts")
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON (default)"
    )
    args = parser.parse_args(argv[1:])
    try:
        state = args.state_dir or args.project.parent / ".capcov" / "search"
        result = workflow(args.project, args.operation, command == "run", state)
        if args.emit_go:
            from ..go_emit import emit

            emit(result.get("search", {}), args.emit_go)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(json.dumps({"operational_failure": str(error)}), file=sys.stderr)
        return 2
