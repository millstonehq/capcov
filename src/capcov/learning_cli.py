from __future__ import annotations

import argparse
from pathlib import Path

from .learning import run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="capcov learn")
    parser.add_argument("input", type=Path, help="JSON operation map with incumbent observations")
    parser.add_argument("--out", type=Path, default=Path("behavioral-learning.json"))
    parser.add_argument("--max-prefix", type=int, default=64)
    args = parser.parse_args(argv)
    if args.max_prefix < 0:
        parser.error("--max-prefix must be non-negative")
    run(args.input, args.out, max_prefix=args.max_prefix)
    return 0
