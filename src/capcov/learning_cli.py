from __future__ import annotations

import argparse
from pathlib import Path

from .learning import evaluate_document, run
import json


def main(argv: list[str] | None = None) -> int:
    if argv and argv[0] == "evaluate":
        parser = argparse.ArgumentParser(prog="capcov learn evaluate")
        parser.add_argument("input", type=Path)
        parser.add_argument("--out", type=Path, default=Path("behavioral-learning-evaluation.json"))
        parser.add_argument("--holdout-mod", type=int, default=5)
        parser.add_argument("--holdout-value", type=int, default=0)
        parser.add_argument("--max-prefix", type=int, default=64)
        parser.add_argument("--max-results", type=int, default=10000)
        args = parser.parse_args(argv[1:])
        result = evaluate_document(json.loads(args.input.read_text()), holdout_mod=args.holdout_mod,
                                   holdout_value=args.holdout_value, max_prefix=args.max_prefix,
                                   max_results=args.max_results)
        args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        return 0
    parser = argparse.ArgumentParser(prog="capcov learn")
    parser.add_argument("input", type=Path, help="JSON operation map with incumbent observations")
    parser.add_argument("--out", type=Path, default=Path("behavioral-learning.json"))
    parser.add_argument("--max-prefix", type=int, default=64)
    args = parser.parse_args(argv)
    if args.max_prefix < 0:
        parser.error("--max-prefix must be non-negative")
    run(args.input, args.out, max_prefix=args.max_prefix)
    return 0
