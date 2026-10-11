"""Inventory literal SQLite table declarations without executing target code.

This is deliberately declaration discovery, not SQL data-flow analysis. Calls
remain diagnostics until a query/receiver adapter can bind them honestly.
"""

from __future__ import annotations

import ast
import re

# Keep strings/comments as indivisible tokens: CREATE TABLE inside either is
# data, not a declaration. Quoted identifiers retain their token type.
_TOKEN = re.compile(
    r"(?P<space>\s+)|(?P<comment>--[^\n]*|/\*[\s\S]*?\*/)"
    r"|(?P<string>'(?:''|[^'])*')"
    r'|(?P<quoted>"(?:""|[^"])*"|`(?:``|[^`])*`|\[[^\]]*\])'
    r"|(?P<word>[A-Za-z_][A-Za-z_0-9]*)|(?P<other>.)",
    re.DOTALL,
)


def tables(sql: str) -> list[str]:
    """Read CREATE [TEMP] TABLE [IF NOT EXISTS] name (...) statements only.

    Attached schemas are retained: collapsing main.items and audit.items would
    silently merge two entities. SQLite's accepted single-quoted table names
    are intentionally unsupported rather than confusing data with identifiers.
    """
    tokens = [
        (m.lastgroup, m.group()) for m in _TOKEN.finditer(sql)
        if m.lastgroup not in {"space", "comment"}
    ]
    out = []
    statements: list[list[tuple[str, str]]] = [[]]
    for token in tokens:
        if token == ("other", ";"):
            statements.append([])
        else:
            statements[-1].append(token)
    for statement in statements:
        index = 0

        def keyword(value: str) -> bool:
            nonlocal index
            if (index < len(statement) and statement[index][0] == "word"
                    and statement[index][1].upper() == value):
                index += 1
                return True
            return False

        def identifier() -> str | None:
            nonlocal index
            if index >= len(statement):
                return None
            kind, value = statement[index]
            if kind not in {"word", "quoted"}:
                return None
            index += 1
            if kind == "quoted":
                quote = value[0]
                value = value[1:-1]
                if quote in {'"', '`'}:
                    value = value.replace(quote * 2, quote)
            return value

        if not keyword("CREATE"):
            continue
        if not keyword("TEMP"):
            keyword("TEMPORARY")
        if not keyword("TABLE"):
            continue
        if keyword("IF") and not (keyword("NOT") and keyword("EXISTS")):
            continue
        name = identifier()
        if name is None:
            continue
        if index < len(statement) and statement[index] == ("other", "."):
            index += 1
            table = identifier()
            if table is None:
                continue
            name += "." + table
        if index < len(statement) and statement[index] == ("other", "("):
            out.append(name)
    return out


def constant_strings(tree: ast.Module) -> dict[str, str]:
    """Resolve direct module string assignments with no competing lexical binding.

    This intentionally rejects a name shadowed anywhere in the module, including
    an unrelated function: absence is preferable to binding SQL to the wrong
    value. Imports, aliases, conditional initialization and computed expressions
    remain explicit diagnostics. No target code is imported or evaluated.
    """
    candidates: dict[str, str] = {}
    for statement in tree.body:
        target = (
            statement.targets[0]
            if isinstance(statement, ast.Assign) and len(statement.targets) == 1
            else statement.target if isinstance(statement, ast.AnnAssign) else None
        )
        value = getattr(statement, "value", None)
        if (isinstance(target, ast.Name) and isinstance(value, ast.Constant)
                and isinstance(value.value, str)):
            candidates[target.id] = value.value
    safe = _unshadowed_names(tree)
    return {name: value for name, value in candidates.items() if name in safe}


def _unshadowed_names(tree: ast.Module) -> set[str]:
    writes: dict[str, int] = {}
    forbidden = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            writes[node.id] = writes.get(node.id, 0) + 1
        elif isinstance(node, ast.arg):
            forbidden.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            forbidden.add(node.name)
        elif isinstance(node, ast.Import):
            forbidden.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if any(alias.name == "*" for alias in node.names):
                return set()
            forbidden.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
            forbidden.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            forbidden.add(node.rest)
    return {name for name, count in writes.items() if count == 1 and name not in forbidden}


def mapping_loop_sql(tree: ast.Module) -> dict[int, list[str]]:
    """Find literal tuple SQL passed directly from a mapping values loop.

    Only the loop's first statement can execute the selected tuple element.
    Other uses of the mapping (including aliases and mutations), shadowing,
    computed values and ambiguous unpacking stay unresolved. This inventories
    declarations, never execution, receiver identity or query bindings.
    """
    safe = _unshadowed_names(tree)
    definitions = {}
    for statement in tree.body:
        target = (statement.targets[0]
                  if isinstance(statement, ast.Assign) and len(statement.targets) == 1
                  else statement.target if isinstance(statement, ast.AnnAssign) else None)
        value = getattr(statement, "value", None)
        if isinstance(target, ast.Name) and target.id in safe and isinstance(value, ast.Dict):
            try:
                literal = ast.literal_eval(value)
            except (ValueError, TypeError, SyntaxError):
                continue
            if (literal and all(isinstance(k, str) for k in literal)
                    and len(literal) == len(value.keys)
                    and all(isinstance(v, tuple) for v in literal.values())):
                definitions[target.id] = literal
    loops = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.For) or not node.body:
            continue
        iterator = node.iter
        if not (isinstance(iterator, ast.Call) and not iterator.args and not iterator.keywords
                and isinstance(iterator.func, ast.Attribute) and iterator.func.attr == "values"
                and isinstance(iterator.func.value, ast.Name)
                and iterator.func.value.id in definitions):
            continue
        if not (isinstance(node.target, ast.Tuple)
                and all(isinstance(t, ast.Name) for t in node.target.elts)):
            continue
        names = [t.id for t in node.target.elts]
        if len(set(names)) != len(names):
            continue
        first = node.body[0]
        call = first.value if isinstance(first, ast.Expr) else None
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and call.func.attr in {"execute", "executescript", "executemany"}):
            continue
        sql = call.args[0] if call.args else next(
            (kw.value for kw in call.keywords if kw.arg in {"sql", "sql_script"}), None)
        if not isinstance(sql, ast.Name) or sql.id not in names:
            continue
        index = names.index(sql.id)
        values = definitions[iterator.func.value.id].values()
        if not all(len(v) == len(names) and isinstance(v[index], str) for v in values):
            continue
        loops.append((iterator.func.value, call, [v[index] for v in values]))
    allowed_reads = {id(name) for name, _, _ in loops}
    escaped = {node.id for node in ast.walk(tree)
               if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
               and node.id in definitions and id(node) not in allowed_reads}
    return {id(call): sql for name, call, sql in loops if name.id not in escaped}


def inspect_call(
    node: ast.Call, constants: dict[str, str] | None = None,
) -> tuple[list[str], str] | None:
    if not isinstance(node.func, ast.Attribute) or node.func.attr not in {
        "execute", "executemany", "executescript",
    }:
        return None
    sql = node.args[0] if node.args else next(
        (kw.value for kw in node.keywords if kw.arg in {"sql", "sql_script"}), None
    )
    if isinstance(sql, ast.Constant) and isinstance(sql.value, str):
        return tables(sql.value), "literal_sql_unbound"
    if isinstance(sql, ast.Name) and constants is not None and sql.id in constants:
        return tables(constants[sql.id]), "constant_sql_unbound"
    return [], "computed_sql_or_expression"
