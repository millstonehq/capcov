#!/usr/bin/env python3
"""Record ``expected_judge_verdicts.json`` from a THREE-KERNEL run of the judge.

The table beside this script is what ``tests/claim_semantics/test_evaluator_selection.py``
holds the python-only judge to: verdicts, exit codes, per-op qualifications,
missing premises and certificate digests, for every committed receipt fixture,
judged with the reviewer admissions the test supplies.
It is only worth comparing against if all three kernels produced it, so this
script refuses to write one unless ``--evaluator all`` really resolved to
``python, souffle, souffle-compiled`` -- i.e. it must be run inside the pinned
devShell::

    nix develop . --command \\
        uv run python tests/claim_semantics/fixtures/record_judge_verdicts.py

Nothing volatile is recorded: no timings, no paths, no compiled-binary
provenance, no kernel digests -- only the judgement, which is a property of the
receipt and the rules and must be identical whichever kernels ran.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACKAGE_ROOT = HERE.parents[2]
for entry in (PACKAGE_ROOT / "src", HERE.parent):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from capcov.claims.replay import judge as replay_judge  # noqa: E402
from test_evaluator_selection import RECEIPTS, admissions, verdicts  # noqa: E402

THREE = ["python", "souffle", "souffle-compiled"]


def main() -> int:
    out = Path(tempfile.mkdtemp(prefix="capcov-record-judge-"))
    try:
        recorded = {}
        for name, receipt in sorted(RECEIPTS.items()):
            document, _ = replay_judge.judge_receipt(receipt, out / name, [], evaluators="all",
                                                     reviewer_admissions=admissions(receipt))
            if document["kernels"] != THREE:
                print(f"{name}: ran {document['kernels']}, not {THREE}: "
                      "run this inside `nix develop`", file=sys.stderr)
                return 1
            if not document["differential_report"]["matched"]:
                print(f"{name}: the kernels disagree; nothing is recorded", file=sys.stderr)
                return 2
            recorded[name] = verdicts(document)
        path = HERE / "expected_judge_verdicts.json"
        path.write_text(json.dumps({"recorded_with": THREE, "receipts": recorded},
                                   indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {path.name}: {', '.join(sorted(recorded))}")
        return 0
    finally:
        shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
