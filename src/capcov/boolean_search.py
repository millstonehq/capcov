"""Cost-ordered Boolean program enumeration using fit-set truth vectors."""
from dataclasses import dataclass
from itertools import product
from .recognition import for_examples, priority


@dataclass
class Term:
    signature: int
    ast: dict


def partitions(total, slots, levels):
    """Yield argument costs lazily, skipping levels without any terms."""
    if slots == 0:
        if total == 0:
            yield ()
        return
    for first in range(1, total - slots + 2):
        if levels[first]:
            for tail in partitions(total - first, slots - 1, levels):
                yield (first, *tail)


def compile_body(node, params, mask, library):
    if "var" in node:
        index = params.index(node["var"])
        return lambda args: args[index]
    if "lit" in node:
        if type(node["lit"]) is not bool:
            raise ValueError("Boolean search needs Boolean library literals")
        bits = mask if node["lit"] else 0
        return lambda args: bits
    op = node["op"]
    if op == "if":
        c, t, f = (compile_body(node[key], params, mask, library) for key in ("cond", "then", "else"))
        return lambda args: (c(args) & t(args)) | ((mask ^ c(args)) & f(args))
    children = [compile_body(child, params, mask, library) for child in node["args"]]
    if op == "call":
        fn = library[node["name"]]
        return lambda args: fn(tuple(child(args) for child in children))
    if op == "not" and len(children) == 1:
        return lambda args: mask ^ children[0](args)
    a, b = children
    if op == "and":
        return lambda args: a(args) & b(args)
    if op == "or":
        return lambda args: a(args) | b(args)
    if op == "eq":
        return lambda args: mask ^ (a(args) ^ b(args))
    raise ValueError(f"unknown Boolean operator {op}")


def search(examples, library, *, max_terms, max_cost, policy=None):
    if not examples or max_terms < 1 or not 1 <= max_cost <= 32:
        raise ValueError("search requires fit examples, positive term budget and cost in 1..32")
    variables = sorted(examples[0]["input"])
    for row in examples:
        if sorted(row["input"]) != variables or type(row["output"]) is not bool or any(
                type(value) is not bool for value in row["input"].values()):
            raise ValueError("fit examples must share Boolean inputs and a Boolean output")
    policy = for_examples(policy, examples)
    mask = (1 << len(examples)) - 1
    goal = sum(int(row["output"]) << i for i, row in enumerate(examples))
    levels = [[] for _ in range(max_cost + 1)]
    seen = set()
    attempts = 0

    def result(ast, cost, stop):
        return {"program": ast, "cost": cost, "stop": stop, "attempts": attempts, "distinct": len(seen)}

    def consider(bits, make_ast, cost):
        nonlocal attempts
        attempts += 1
        if bits not in seen:
            ast = make_ast()
            seen.add(bits)
            if bits == goal:
                return result(ast, cost, "fit")
            levels[cost].append(Term(bits, ast))
        if attempts >= max_terms:
            return result(None, None, "term_budget")
        return None

    variable_bits = {}
    for name in variables:
        bits = sum(int(row["input"][name]) << i for i, row in enumerate(examples))
        variable_bits[name] = bits
        if found := consider(bits, lambda: {"var": name}, 1):
            return found
    for value in (False, True):
        if found := consider(mask if value else 0, lambda: {"lit": value}, 1):
            return found

    operators = []
    compiled = {}
    for name, definition in library.items():
        params = definition["params"]
        compiled[name] = compile_body(definition["body"], params, mask, compiled)
        operators.append((name, len(params), compiled[name], False))
        # A learned binding prior proposes a closed application when each
        # argument role maps uniquely to a target field. It is one grammar
        # production; unconstrained calls are still enumerated at normal cost.
        bound = []
        for param in params:
            suffix = definition.get("argument_suffixes", {}).get(param)
            matches = [v for v in variables if suffix and (v == suffix or v.endswith("_" + suffix))]
            if len(matches) != 1:
                break
            bound.append(matches[0])
        if bound and len(bound) == len(params):
            ast = {"op": "call", "name": name, "args": [{"var": v} for v in bound]}
            signature = compiled[name](tuple(variable_bits[v] for v in bound))
            if found := consider(signature, lambda: ast, 1):
                return found
    primitives = [
        ("not", 1, lambda a: mask ^ a[0], False),
        ("and", 2, lambda a: a[0] & a[1], True),
        ("or", 2, lambda a: a[0] | a[1], True),
        ("eq", 2, lambda a: mask ^ (a[0] ^ a[1]), True),
    ]
    ordered = primitives + operators
    ordered.sort(key=lambda row: priority(policy, row[0]), reverse=True)
    for cost in range(2, max_cost + 1):
        for op, arity, apply, symmetric in ordered:
            for costs in partitions(cost - 1, arity, levels):
                for terms in product(*(levels[c] for c in costs)):
                    bits = tuple(t.signature for t in terms)
                    if symmetric and bits[0] > bits[1]:
                        continue

                    def make_ast():
                        args = [t.ast for t in terms]
                        if op in library:
                            return {"op": "call", "name": op, "args": args}
                        return {"op": op, "args": args}

                    if found := consider(apply(bits), make_ast, cost):
                        return found
    return result(None, None, "cost_budget")
