"""A bounded, non-neural task-conditioned grammar prior for Boolean search."""

from collections import Counter
from itertools import product
from random import Random

from .synthesis import eval_program


def features(rows):
    """Rename- and order-invariant statistics of the supplied partial truth table."""
    observations = {
        tuple(value for _, value in sorted(r["input"].items())): r["output"]
        for r in rows
    }
    width = len(next(iter(observations)))
    ones = sum(observations.values()) / len(observations)
    # Separate positive, negative and mixed dependence, estimated on observed edges.
    up = down = edges = 0
    for point, output in observations.items():
        for i, bit in enumerate(point):
            other = point[:i] + (True,) + point[i + 1 :]
            if not bit and other in observations:
                edges += 1
                up += output < observations[other]
                down += output > observations[other]
    return [
        ones,
        up / max(edges, 1),
        down / max(edges, 1),
        float(bool(edges)),
        min(width, 8) / 8,
    ]


def root(program):
    return program.get("name", program.get("op"))


def sleep(tasks, library=None, *, dreams=96, seed=0):
    """Sample bounded DSL programs, execute synthetic tasks, and retain labeled prototypes."""
    if type(dreams) is not int or not 0 <= dreams <= 512:
        raise ValueError("sleep_samples must be in 0..512")
    library = library or {}
    rng = Random(seed)
    productions = {"not": 1, "or": 2, "and": 2, "eq": 2}
    productions.update(
        {name: len(definition["params"]) for name, definition in library.items()}
    )
    prototypes = []

    def record(program, rows):
        label = root(program)
        if label:
            prototypes.append({"features": features(rows), "production": label})

    for task in tasks:
        record(task["program"], task["examples"])
    synthetic_examples = 0
    for i in range(dreams):
        width = 2 + i % 3
        names = [f"x{j}" for j in range(width)]

        def sample(label, depth):
            args = []
            for j in range(productions[label]):
                if depth and rng.random() < 0.25:
                    args.append(sample(rng.choice(list(productions)), depth - 1))
                else:
                    args.append({"var": names[j % width]})
            return (
                {"op": "call", "name": label, "args": args}
                if label in library
                else {"op": label, "args": args}
            )

        label = list(productions)[i % len(productions)]
        program = sample(label, 1)
        rows = []
        for bits in product((False, True), repeat=width):
            inputs = dict(zip(names, bits))
            rows.append(
                {"input": inputs, "output": eval_program(program, inputs, library)}
            )
        record(program, rows)
        synthetic_examples += len(rows)
    return {
        "schema": "capcov/recognition-policy/v1",
        "solved_tasks": len(tasks),
        "seed": seed,
        "synthetic_tasks": dreams,
        "synthetic_examples": synthetic_examples,
        "prototypes": prototypes,
    }


def for_examples(policy, examples):
    if not policy:
        return None
    target = features(examples)
    votes = Counter()
    for row in policy["prototypes"]:
        distance = sum((a - b) ** 2 for a, b in zip(target, row["features"]))
        votes[row["production"]] += 1 / (0.01 + distance) ** 2
    return votes


def priority(ranking, name):
    return ranking.get(name, 0) if ranking else 0
