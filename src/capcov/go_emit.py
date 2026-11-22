from __future__ import annotations
import json, re
from pathlib import Path

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
RESERVED = {"break", "default", "func", "interface", "select", "case", "defer", "go", "map", "struct", "chan", "else", "goto", "package", "switch", "const", "fallthrough", "if", "range", "type", "continue", "for", "import", "return", "var"}

def _name(value: str) -> str:
    if not isinstance(value, str) or not IDENT.fullmatch(value) or value in RESERVED:
        raise ValueError(f"invalid Go identifier: {value!r}")
    return value

def _expr(node, variables: dict[str, str], functions: dict[str, dict]) -> str:
    if isinstance(node, bool): return "true" if node else "false"
    if isinstance(node, dict) and "lit" in node:
        if not isinstance(node["lit"], bool): raise ValueError("only boolean literals can be emitted")
        return "true" if node["lit"] else "false"
    if isinstance(node, dict) and "var" in node:
        name = node["var"]
        if name not in variables: raise ValueError(f"unknown variable: {name}")
        return variables[name]
    if not isinstance(node, dict): raise ValueError("invalid AST node")
    op = node.get("op")
    args = node.get("args", [])
    if op == "if":
        cond, yes, no = (_expr(node[key], variables, functions) for key in ("cond", "then", "else"))
        return f"(({cond} && {yes}) || (!{cond} && {no}))"
    if op in {"and", "or"}:
        if len(args) != 2: raise ValueError(f"{op} expects two arguments")
        symbol = "&&" if op == "and" else "||"
        return f"({_expr(args[0], variables, functions)} {symbol} {_expr(args[1], variables, functions)})"
    if op == "not":
        if len(args) != 1: raise ValueError("not expects one argument")
        return f"(!{_expr(args[0], variables, functions)})"
    if op == "eq":
        if len(args) != 2: raise ValueError("eq expects two arguments")
        return f"({_expr(args[0], variables, functions)} == {_expr(args[1], variables, functions)})"
    if op == "call":
        name = _name(node.get("name"))
        if name not in functions: raise ValueError(f"unknown function: {name}")
        params = functions[name].get("params", [])
        if len(args) != len(params): raise ValueError(f"{name} expects {len(params)} arguments")
        return f"{name}({', '.join(_expr(arg, variables, functions) for arg in args)})"
    raise ValueError(f"unsupported operator: {op!r}")

def emit(report: dict, output: Path) -> None:
    library = report.get("library", {}).get("library", report.get("library", {}))
    evaluation = report.get("evaluation", {})
    candidate = evaluation.get("selected_candidate")
    if candidate is None: raise ValueError("report has no selected candidate")
    if not isinstance(library, dict): raise ValueError("invalid library")
    functions = {_name(name): definition for name, definition in library.items()}
    variables = {_name(part["var"]) for part in _walk(candidate) if isinstance(part, dict) and "var" in part}
    if functions.keys() & variables or "Synthesized" in functions or {"true", "false", "_", "bool"} & variables:
        raise ValueError("identifier collides with a function, type or Boolean literal")
    lines = ["package synthesized", ""]
    for name, definition in sorted(functions.items()):
        params = definition.get("params", [])
        if len(set(params)) != len(params):
            raise ValueError("duplicate formal parameter")
        local = {param: f"arg{i}" for i, param in enumerate(params)}
        lines.append(f"func {name}({', '.join(arg + ' bool' for arg in local.values())}) bool {{")
        lines.append(f"\treturn {_expr(definition['body'], local, functions)}")
        lines.extend(["}", ""])
    signature = ", ".join(name + " bool" for name in sorted(variables))
    lines.extend([f"func Synthesized({signature}) bool {{", f"\treturn {_expr(candidate, {v: v for v in variables}, functions)}", "}", ""])
    output.write_text("\n".join(lines))

def _walk(node):
    yield node
    if isinstance(node, dict):
        for value in node.values(): yield from _walk(value)
    elif isinstance(node, list):
        for value in node: yield from _walk(value)
