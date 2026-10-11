"""``capcov faults``: judge one recorded run.

    capcov faults check --history h.json --finals f.json [--rules pack.json]
                        [--engine python|both] [--json]

``--history`` is the document ``History.to_json`` writes and ``--finals`` the
one ``checker.finals_to_json`` writes.  One line is printed per invariant.

Exit status: 0 when every invariant is supported; 1 when any is violated or
unresolved; 2 for invalid input, including ``--engine both`` with no
``souffle`` on PATH; 3 when the Python and Souffle kernels disagree.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


def _check(args: argparse.Namespace) -> int:
    from . import checker
    from .history import History, HistoryError
    from ..claims.differential import DifferentialMismatch

    try:
        history = History.from_json(Path(args.history).read_text(encoding="utf-8"))
        finals, service = checker.finals_from_json(Path(args.finals).read_text(encoding="utf-8"))
        pack = checker.load_pack(args.rules) if args.rules else None
        bundle = checker.build_bundle(history, service, finals, pack=pack)
        verdicts = checker.check(bundle, args.engine)
    except (OSError, UnicodeError, HistoryError, checker.FaultCheckError,
            checker.SouffleRequired) as exc:
        print(f"capcov faults: {exc}", file=sys.stderr)
        return 2
    except DifferentialMismatch as exc:
        print(f"capcov faults: {exc}", file=sys.stderr)
        return 3
    if args.json:
        print(json.dumps(verdicts.as_dict(), indent=2, sort_keys=True))
    else:
        for verdict in verdicts.verdicts:
            detail = verdict.violations or verdict.missing_premises
            suffix = f"  {json.dumps(list(detail), sort_keys=True)}" if detail else ""
            print(f"{verdict.status:<10} {verdict.name}{suffix}")
    return 0 if verdicts.ok else 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="capcov faults", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="judge a recorded history and its final reads")
    check.add_argument("--history", required=True, help="history document (History.to_json)")
    check.add_argument("--finals", required=True, help="finals document (checker.finals_to_json)")
    check.add_argument("--rules", default=None, help="rule pack to use instead of the shipped one")
    check.add_argument("--engine", choices=("python", "both"), default="python")
    check.add_argument("--json", action="store_true", help="print the verdicts as one JSON document")
    args = parser.parse_args(argv)
    return _check(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
