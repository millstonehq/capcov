"""Infer prefix + character -> character rules from one operation's JSONL samples.

Each input row is [input string, observed output string], from one pinned
environment. Emit hypotheses and unobserved empty-operand probes as JSONL.
"""
import json
import sys
from collections import defaultdict


def hypotheses(samples):
    groups = defaultdict(set)
    observations = defaultdict(set)
    for operand, observed in samples:
        if not isinstance(operand, str) or not isinstance(observed, str):
            raise ValueError("expected string operands and observations")
        observations[operand].add(observed)
        if len(operand) > 1:
            groups[operand[:-1]].add(operand[-1])
    for prefix, suffixes in sorted(groups.items()):
        support = []
        exceptions = []
        for suffix in sorted(suffixes):
            operand = prefix + suffix
            if observations[operand] == {suffix}:
                support.append(operand)
            else:
                exceptions.append(operand)
        if len(support) >= 2:
            yield dict(rule="prefix + character -> character", prefix=prefix,
                       support=support, exceptions=exceptions,
                       probes=[] if prefix in observations else [prefix])


if __name__ == "__main__":
    for rule in hypotheses(json.loads(line) for line in sys.stdin):
        print(json.dumps(rule, sort_keys=True))
