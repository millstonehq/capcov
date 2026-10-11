"""Consumer-authored outcome obligations backed by fresh, exact pytest cases.

This is a pytest integration, not a general harness protocol. Structural discovery
and this scoped behavioral report have separate denominators.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from .artifacts import (
    SourceSnapshot,
    normalise_patterns,
    snapshot_tree,
    source_patterns_of,
    tree_sha256,
)
from .flows.model import digest

STATUSES = ("demonstrated", "failed", "missing", "unresolved", "inconclusive")


def read(path: str | Path) -> dict:
    document = json.loads(Path(path).read_text())
    if not isinstance(document, dict):
        raise ValueError(f"expected an outcome JSON object: {path}")
    return document


def write(path: str | Path, value: dict) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def local_path(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not relative:
        raise ValueError(f"expected a local relative path: {relative}")
    result = root / path
    if not result.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"path escapes target: {relative}")
    return result


def validate(mapping: dict, inventory: dict) -> None:
    if mapping.get("version") != 1 or not mapping.get("scope"):
        raise ValueError("outcome map requires version 1 and a named scope")
    if inventory.get("kind") != "capabilities" or inventory.get("schema_version") != 1:
        raise ValueError("expected a version 1 capability inventory")
    if not mapping.get("environment") or not mapping.get("limitations"):
        raise ValueError("declare the environment and evidence limitations")
    if not mapping.get("inputs") or not mapping.get("outcomes"):
        raise ValueError("nonempty inputs and outcomes are required")
    surfaces = {s["id"] for s in inventory["surfaces"]}
    seen = set()
    for row in mapping["outcomes"]:
        if not row.get("id") or row["id"] in seen or not row.get("capability"):
            raise ValueError("outcomes require unique IDs and a capability identity")
        seen.add(row["id"])
        if not row.get("description") or not row.get("source_refs"):
            raise ValueError(f"{row['id']}: description and source_refs required")
        if not set(row["source_refs"]) <= surfaces:
            raise ValueError(f"{row['id']}: unknown source surface")
        if row.get("policy") not in ("required", "unresolved"):
            raise ValueError(f"{row['id']}: policy must be required or unresolved")
        if row["policy"] == "unresolved" and not row.get("reason"):
            raise ValueError(f"{row['id']}: unresolved policy requires a reason")
        tests = row.get("tests", [])
        if not isinstance(tests, list) or len(tests) != len(set(tests)):
            raise ValueError(f"{row['id']}: duplicate or malformed test identities")
        for test in tests:
            if not isinstance(test, str) or "::" not in test or test.startswith("-"):
                raise ValueError(f"invalid pytest node ID: {test}")
            local_path(Path.cwd(), test.split("::")[0])


def provenance(
    root: Path,
    mapping: dict,
    inventory: dict,
    source_snapshot: SourceSnapshot | None = None,
) -> dict:
    validate(mapping, inventory)
    source = local_path(root, inventory["derived_from"]["artifact"])
    # Recompute the inventory's hash over the SAME globs discover used. Reading
    # the tree as `**/*.py` against an inventory taken over `*.go` is not a
    # staleness finding, it is two different questions -- and it made every
    # `capcov outcomes` command unusable on the non-Python targets discover now
    # supports.
    # Normalized on both sides: a duplicate or empty glob in a recorded
    # `source_patterns` is not a staleness finding, and reporting it as one
    # sends the reader to a re-discover that cannot fix it.
    patterns = normalise_patterns(source_patterns_of(inventory["derived_from"]))
    source_snapshot = source_snapshot or snapshot_tree(source, patterns)
    if (
        source_snapshot.root != source.resolve()
        or source_snapshot.patterns != patterns
        or source_snapshot.digest != inventory["derived_from"]["artifact_sha256"]
    ):
        raise ValueError("source inventory is stale; re-discover")
    # When the outcome map declares the exact oracle root as an input, its
    # source-bound snapshot already contains the digest for every selected
    # source file. Reuse those bytes' identities here. Files outside the
    # selected source patterns still take the legacy exact-read path, so this
    # optimization cannot silently change ``inputs_sha256`` semantics.
    source_entries = dict(source_snapshot.entries)
    source_root = source.resolve()
    paths = {}
    inputs = [local_path(root, name) for name in mapping["inputs"]]
    for row in mapping["outcomes"]:
        for test in row.get("tests", []):
            test_path = local_path(root, test.split("::")[0])
            if not any(test_path == p or p in test_path.parents for p in inputs):
                raise ValueError(f"mapped test must be covered by inputs: {test}")
    for path in inputs:
        if path.is_symlink():
            raise ValueError(f"symlink evidence input: {path}")
        if not path.exists():
            raise ValueError(f"missing evidence input: {path.relative_to(root)}")
        for file in sorted(path.rglob("*")) if path.is_dir() else [path]:
            if "__pycache__" in file.parts or ".pytest_cache" in file.parts:
                continue
            if file.is_symlink():
                raise ValueError(f"symlink evidence input: {file}")
            if file.is_file():
                relative = file.relative_to(root).as_posix()
                file_digest = None
                if path.resolve() == source_root and source_entries:
                    try:
                        file_digest = source_entries.get(
                            file.resolve().relative_to(source_root).as_posix()
                        )
                    except ValueError:
                        file_digest = None
                paths[relative] = file_digest or hashlib.sha256(
                    file.read_bytes()
                ).hexdigest()
    return {
        "map_sha256": digest(mapping),
        "inventory_sha256": digest(inventory),
        "engine_sha256": tree_sha256(Path(__file__).parent)[0],
        "inputs_sha256": digest(paths),
        "input_files": paths,
    }


def reconcile(mapping: dict, inventory: dict, run: dict, current: dict) -> dict:
    validate(mapping, inventory)
    if not isinstance(run, dict):
        raise ValueError("outcome evidence must be an object")
    if run.get("version") != 1 or run.get("provenance") != current:
        raise ValueError(
            "evidence does not match the current map, inventory, or inputs"
        )
    tests = run.get("tests", {})
    if not isinstance(tests, dict):
        raise ValueError("outcome tests must map exact node IDs to results")
    errors = run.get("errors", [])
    if not isinstance(errors, list) or any(not isinstance(e, str) for e in errors):
        raise ValueError("outcome errors must be a list of diagnostics")
    rows = []
    for row in mapping["outcomes"]:
        expected = row.get("tests", [])
        states = [tests.get(test, "missing") for test in expected]
        status = (
            "unresolved"
            if row["policy"] == "unresolved"
            else "failed"
            if "failed" in states
            else "missing"
            if not expected or "missing" in states
            else "inconclusive"
            if run.get("finished") is not True or any(s != "passed" for s in states)
            else "demonstrated"
        )
        rows.append(
            {
                **row,
                "status": status,
                "test_results": {t: tests.get(t, "missing") for t in expected},
            }
        )
    errors = list(errors)
    if (
        run.get("finished") is not True
        or type(run.get("exit_code")) is not int
        or run["exit_code"] not in (0, 1)
    ):
        errors.append("pytest session did not finish normally")
    if run.get("exit_code") == 1:
        errors.append("pytest session contains failures")
    return {
        "version": 1,
        "kind": "outcome-coverage",
        "scope": mapping["scope"],
        "environment": mapping["environment"],
        "limitations": mapping["limitations"],
        "provenance": current,
        "rows": rows,
        "run_errors": errors,
        "summary": {s: sum(r["status"] == s for r in rows) for s in STATUSES},
        "complete": not errors and all(r["status"] == "demonstrated" for r in rows),
    }


def execute(
    root: Path,
    mapping: dict,
    inventory: dict,
    python: str,
    selection: list[str],
    timeout: int,
) -> dict:
    phase_started = time.perf_counter_ns()
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    source = local_path(root, inventory["derived_from"]["artifact"])
    patterns = normalise_patterns(source_patterns_of(inventory["derived_from"]))
    source_snapshot = snapshot_tree(source, patterns)
    before = provenance(root, mapping, inventory, source_snapshot)
    expected = sorted({t for row in mapping["outcomes"] for t in row.get("tests", [])})
    if not expected and not selection:
        raise ValueError("no mapped tests to execute")
    with tempfile.TemporaryDirectory(prefix="capcov-outcomes-") as directory:
        output = Path(directory) / "results.json"
        nonce = uuid.uuid4().hex
        env = {
            **os.environ,
            "CAPCOV_OUTCOME_OUT": str(output),
            "CAPCOV_OUTCOME_NONCE": nonce,
            "PYTHONPATH": str(Path(__file__).resolve().parent.parent)
            + os.pathsep
            + os.environ.get("PYTHONPATH", ""),
        }
        command = [
            python,
            "-m",
            "pytest",
            "-q",
            "-p",
            "capcov.probes.outcomes_probe",
            *(selection or expected),
        ]
        try:
            result = subprocess.run(
                command, cwd=root, env=env, timeout=timeout, check=False
            )
            exit_code = result.returncode
        except subprocess.TimeoutExpired:
            exit_code = 124
        run = (
            read(output)
            if output.exists()
            else {
                "tests": {},
                "finished": False,
                "errors": ["no fresh pytest evidence"],
            }
        )
        if output.exists() and run.get("nonce") != nonce:
            raise ValueError("pytest evidence nonce mismatch")
        verified_snapshot = source_snapshot.verify()
        if provenance(root, mapping, inventory, verified_snapshot) != before:
            raise ValueError(
                "source, tests, or declared inputs changed during execution"
            )
        return {
            **run,
            "version": 1,
            "provenance": before,
            "exit_code": exit_code,
            "command": command,
            "nonce": nonce,
            "timing": {
                "observe_ms": min(
                    max(0, (time.perf_counter_ns() - phase_started) // 1_000_000),
                    86_400_000,
                ),
                "source_verification": verified_snapshot.verification,
            },
        }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="capcov outcomes")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "check", "coverage", "gate"):
        p = sub.add_parser(name)
        p.add_argument("mapping")
        p.add_argument("--inventory", required=True)
        p.add_argument("--target", default=".")
        if name in ("run", "check"):
            p.add_argument(
                "--python", default=sys.executable, help="target pytest interpreter"
            )
            p.add_argument("--timeout", type=int, default=180)
            p.add_argument("--out", required=True)
        else:
            p.add_argument("--run", required=True)
            if name == "coverage":
                p.add_argument("--out", required=True)
    raw, selection = list(argv), []
    if "--" in raw:
        index = raw.index("--")
        raw, selection = raw[:index], raw[index + 1 :]
    args = parser.parse_args(raw)
    try:
        if selection and args.command != "run":
            raise ValueError("pytest selection is supported only by run")
        root = Path(args.target).resolve()
        mapping, inventory = read(args.mapping), read(args.inventory)
        if hasattr(args, "out"):
            out = Path(args.out).resolve()
            protected = [
                Path(args.mapping).resolve(),
                Path(args.inventory).resolve(),
                *[local_path(root, p).resolve() for p in mapping["inputs"]],
            ]
            if hasattr(args, "run"):
                protected.append(Path(args.run).resolve())
            if any(
                out == p or (p.is_dir() and out.is_relative_to(p)) for p in protected
            ):
                raise ValueError("output must not overwrite evidence inputs")
        if args.command in ("run", "check"):
            out.unlink(missing_ok=True)
            run = execute(
                root, mapping, inventory, args.python, selection, args.timeout
            )
            if read(args.mapping) != mapping or read(args.inventory) != inventory:
                raise ValueError("map or inventory changed during execution")
            write(out, run)
            if args.command == "run":
                return (
                    0
                    if run["finished"] and run["exit_code"] == 0 and not run["errors"]
                    else 1
                )
            # One host-callable acceptance operation: execute the whole map and
            # decide against the required set, never accept a supplied receipt.
            # `current` must be derived independently -- passing run["provenance"]
            # here makes reconcile's staleness check compare a value to itself,
            # and stops covering the window between execute() and the decision.
            report = reconcile(
                mapping, inventory, run, provenance(root, mapping, inventory)
            )
        else:
            current = provenance(root, mapping, inventory)
            report = reconcile(mapping, inventory, read(args.run), current)
        if args.command == "coverage":
            write(args.out, report)
        for row in report["rows"]:
            print(f"{row['status']:13} {row['id']}: {row['description']}")
            if row["status"] != "demonstrated":
                print(f"  expected all mapped tests passed; observed {row['test_results']}")
        for error in report["run_errors"]:
            print(f"  run error: {error}")
        print(f"Scoped outcomes: {report['summary']}; complete={report['complete']}")
        return 0 if args.command == "coverage" or report["complete"] else 1
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(f"capcov outcomes: {error}", file=sys.stderr)
        return 2
