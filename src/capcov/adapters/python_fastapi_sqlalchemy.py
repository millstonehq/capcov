"""Python / FastAPI / SQLAlchemy adapter: discover and bind.

Entities are declarative-mapper classes, surfaces are routed handlers, entry
points are surfaces plus whatever the target declares in capcov.toml, and call
edges are resolved through each module's own import bindings.

Two properties are load-bearing and neither is free:

* **Unresolved calls are named, not dropped.** A call this cannot resolve is
  recorded with a file and a line. A static analyser that silently loses an edge
  reports fewer capabilities and looks the same as one that found them all.
* **Blind spots are enumerated.** Every `getattr`, `vars`, `eval` and dynamic
  import is listed with a file and a line. This is a diagnostic, not a gate --
  it explains why runtime observation found something static did not, and it is
  the reason the static half can be trusted about its own limits.

This is a soundy analyser: a resolved core that documents its unsound corners
rather than hiding them (Livshits et al. 2015, the soundiness manifesto; see
ADR-0001). Those corners are unavoidable -- higher-order and virtual dispatch are
not decidable from syntax (Shivers 1991, k-CFA) and ref-derived call graphs are
unsound in measured practice (Samhi 2024) -- so an ambiguous multi-candidate call
is bound to NONE of its candidates and recorded, never guessed. Resolution here
is syntactic, blind spots are enumerated, and accuracy is unmeasured.
"""

from __future__ import annotations

import ast
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from . import sqlite_literals

# The dynamic-access blind-spot vocabulary lives in the scip package -- it is the
# half a static resolver cannot see through, and it is the same set no matter
# which adapter reads it. Importing it back (rather than keeping a copy) is what
# guarantees the enumerator and this adapter never disagree about what is blind.
from ..scip.blindspots import DYNAMIC_BLIND_CALLS as BLIND_SPOT_CALLS
from ..scip.blindspots import constant_name_arg

NAME = "python-fastapi-sqlalchemy"

# The SCIP indexer language for `--resolver scip`. This adapter reads Python, so
# the optional SCIP path indexes with scip-python; the resolver seam reads this
# to pick the indexer.
LANGUAGE = "python"

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options", "trace")

# Operation inference. Only what is structurally decidable is claimed here; the
# probe reads the SQL verb and knows exactly, so static guessing at operations
# would be inventing a disagreement rather than finding one.
READ_CALLS = {"select", "exists", "get", "scalar", "scalars", "execute"}
DELETE_CALLS = {"delete"}
UPDATE_CALLS = {"update"}


@dataclass
class Module:
    dotted: str
    path: Path
    rel: str
    tree: ast.Module
    # local name -> "pkg.mod" (a module) or "pkg.mod:Symbol" (a symbol)
    bindings: dict[str, str] = field(default_factory=dict)
    routers: dict[str, str] = field(default_factory=dict)  # local name -> prefix
    functions: set[str] = field(default_factory=set)


def _dotted(root: Path, path: Path) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _load_modules(root: Path) -> dict[str, Module]:
    modules: dict[str, Module] = {}
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        dotted = _dotted(root, path)
        modules[dotted] = Module(
            dotted=dotted, path=path, rel=path.relative_to(root).as_posix(), tree=tree
        )
    for module in modules.values():
        _bind_names(module, modules)
    return modules


def _absolute(module: Module, node: ast.ImportFrom) -> str:
    """Resolve a relative import to a dotted module path."""
    if not node.level:
        return node.module or ""
    parts = module.dotted.split(".")
    # A package's __init__ is its own package; a module's level-1 parent is the
    # package containing it.
    is_package = module.path.name == "__init__.py"
    base = parts if is_package else parts[:-1]
    up = node.level - 1
    base = base[: len(base) - up] if up else base
    return ".".join([*base, node.module]) if node.module else ".".join(base)


def _bind_names(module: Module, modules: dict[str, Module]) -> None:
    for node in ast.walk(module.tree):
        if isinstance(node, ast.ImportFrom):
            target = _absolute(module, node)
            for alias in node.names:
                local = alias.asname or alias.name
                candidate = f"{target}.{alias.name}" if target else alias.name
                # `from . import jobs` binds a MODULE; `from .models import Job`
                # binds a symbol. Which one decides whether `jobs.router` is a
                # module attribute or a method call.
                if candidate in modules:
                    module.bindings[local] = candidate
                else:
                    module.bindings[local] = f"{target}:{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                module.bindings[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.Assign):
            call = node.value
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id in ("APIRouter", "FastAPI")
            ):
                prefix = ""
                for kw in call.keywords:
                    if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                        prefix = kw.value.value
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        module.routers[target.id] = prefix
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            module.functions.add(node.name)


# --------------------------------------------------------------------------
# entities


def discover_entities(modules: dict[str, Module]) -> tuple[list[dict], dict[str, str]]:
    entities: list[dict] = []
    symbol_to_table: dict[str, str] = {}
    for module in modules.values():
        for node in ast.walk(module.tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for stmt in node.body:
                targets = (
                    stmt.targets
                    if isinstance(stmt, ast.Assign)
                    else [stmt.target] if isinstance(stmt, ast.AnnAssign) else []
                )
                names = [t.id for t in targets if isinstance(t, ast.Name)]
                if "__tablename__" in names and isinstance(
                    getattr(stmt, "value", None), ast.Constant
                ):
                    table = stmt.value.value
                    entities.append(
                        {
                            "name": table,
                            "symbol": node.name,
                            "module": module.dotted,
                            "file": module.rel,
                            "line": node.lineno,
                        }
                    )
                    symbol_to_table[f"{module.dotted}:{node.name}"] = table
    entities.sort(key=lambda e: e["name"])
    return entities, symbol_to_table


# --------------------------------------------------------------------------
# surfaces


def _decorator_route(dec: ast.expr) -> tuple[str, str, str] | None:
    """(router_local_name, METHOD, path) for @router.get("/x") and friends."""
    if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
        return None
    if dec.func.attr not in HTTP_METHODS:
        return None
    if not isinstance(dec.func.value, ast.Name):
        return None
    if not dec.args or not isinstance(dec.args[0], ast.Constant):
        return None
    return dec.func.value.id, dec.func.attr.upper(), dec.args[0].value


def _mount_prefixes(modules: dict[str, Module]) -> dict[str, str]:
    """module.dotted:router_name -> mount prefix, from include_router calls."""
    mounts: dict[str, str] = {}
    for module in modules.values():
        for node in ast.walk(module.tree):
            if (
                not isinstance(node, ast.Call)
                or not isinstance(node.func, ast.Attribute)
                or node.func.attr != "include_router"
                or not node.args
            ):
                continue
            prefix = ""
            for kw in node.keywords:
                if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                    prefix = kw.value.value
            arg = node.args[0]
            if isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Name):
                base = module.bindings.get(arg.value.id)
                if base and ":" not in base:
                    mounts[f"{base}:{arg.attr}"] = prefix
    return mounts


def _walk_scoped(node: ast.AST, stack: list[str]) -> Iterator[tuple[ast.AST, str]]:
    """Yield (node, qualname) for every function, carrying its enclosing scopes.

    ast.walk flattens, and a route handler defined inside an app factory --
    which is how FastAPI's own documentation writes it -- then gets the same key
    as a module-level function of that name. The traversal that binds entities
    uses qualnames, so the two would not meet and the surface would look like an
    entry point with no body.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            qual = [*stack, child.name]
            if not isinstance(child, ast.ClassDef):
                yield child, ".".join(qual)
            yield from _walk_scoped(child, qual)
        else:
            yield from _walk_scoped(child, stack)


def discover_surfaces(modules: dict[str, Module]) -> list[dict]:
    mounts = _mount_prefixes(modules)
    surfaces: list[dict] = []
    for module in modules.values():
        for node, qualname in _walk_scoped(module.tree, []):
            for dec in node.decorator_list:
                parsed = _decorator_route(dec)
                if parsed is None:
                    continue
                local, method, path = parsed
                if local not in module.routers:
                    continue
                key = f"{module.dotted}:{local}"
                # An app object is its own mount; a router is mounted or it is
                # dead, and "defined and never included" is a finding.
                is_app = module.routers.get(local) == "" and key not in mounts
                mounted = key in mounts or _is_app_object(module, local)
                full = (mounts.get(key, "") + module.routers[local] + path) or "/"
                surfaces.append(
                    {
                        "id": f"{method} {full}",
                        "kind": "http",
                        "method": method,
                        "path": full,
                        "handler": f"{module.dotted}:{qualname}",
                        "file": module.rel,
                        "line": node.lineno,
                        "mounted": bool(mounted) or is_app,
                    }
                )
    surfaces.sort(key=lambda s: (s["path"], s["method"]))
    return surfaces


def _is_app_object(module: Module, local: str) -> bool:
    for node in ast.walk(module.tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "FastAPI"
            and any(isinstance(t, ast.Name) and t.id == local for t in node.targets)
        ):
            return True
    return False


# --------------------------------------------------------------------------
# functions, references, calls


class _FunctionWalker(ast.NodeVisitor):
    """One pass per module: direct entity refs, call edges, blind spots."""

    def __init__(
        self, module: Module, entity_symbols: dict[str, str], sqlite_ddl: bool = False
    ) -> None:
        self.module = module
        self.entity_symbols = entity_symbols  # local name -> table
        self.stack: list[str] = []
        self.direct: dict[str, set[str]] = {}
        self.ops: dict[str, dict[str, set[str]]] = {}
        self.calls: dict[str, set[str]] = {}
        self.unresolved: list[dict] = []
        self.blind: list[dict] = []
        self.evidence: dict[str, list[dict]] = {}
        self.sqlite_ddl = sqlite_ddl
        self.sql_constants = sqlite_literals.constant_strings(module.tree) if sqlite_ddl else {}
        self.sql_loops = sqlite_literals.mapping_loop_sql(module.tree) if sqlite_ddl else {}
        self.sql_entities: list[dict] = []

    # -- scope

    def _enter(self, node: ast.AST) -> str:
        self.stack.append(node.name)
        key = f"{self.module.dotted}:{'.'.join(self.stack)}"
        self.direct.setdefault(key, set())
        self.calls.setdefault(key, set())
        return key

    def visit_FunctionDef(self, node: ast.AST) -> None:
        self._enter(node)
        for child in node.body:
            self.visit(child)
        for arg_default in node.args.defaults + node.args.kw_defaults:
            if arg_default is not None:
                self.visit(arg_default)
        self.stack.pop()
        # Decorators are evaluated in the ENCLOSING scope, and a route decorator
        # is registration rather than a call edge -- discover_surfaces has
        # already read it. Visiting them here made @router.get the single
        # largest entry in the residue, which is bookkeeping noise wearing the
        # costume of a finding.
        for dec in node.decorator_list:
            if _decorator_route(dec) is None:
                self.visit(dec)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    @property
    def _current(self) -> str:
        """The scope a statement belongs to.

        Class bodies and module level are real scopes -- a `select()` in a class
        body runs at import -- so they get a node too. They are simply never
        entry points, so nothing roots a traversal at them.
        """
        if not self.stack:
            return f"{self.module.dotted}:<module>"
        return f"{self.module.dotted}:{'.'.join(self.stack)}"

    # -- references

    def visit_Name(self, node: ast.Name) -> None:
        table = self.entity_symbols.get(node.id)
        here = self._current
        if table:
            self.direct.setdefault(here, set()).add(table)
            self.evidence.setdefault(here, []).append(
                {"entity": table, "kind": "direct", "file": self.module.rel,
                 "line": node.lineno}
            )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        here = self._current
        fn = node.func
        sql = sqlite_literals.inspect_call(node, self.sql_constants) if self.sqlite_ddl else None
        if id(node) in self.sql_loops:
            sql = ([table for value in self.sql_loops[id(node)]
                    for table in sqlite_literals.tables(value)], "constant_sql_unbound")
        if sql is not None:
            tables, kind = sql
            for table in tables:
                self.sql_entities.append({
                    "name": table, "symbol": None, "module": self.module.dotted,
                    "file": self.module.rel, "line": node.lineno,
                    "declaration_kind": ("sqlite_constant_ddl" if kind == "constant_sql_unbound"
                                         else "sqlite_literal_ddl"),
                })
            self.blind.append({
                "kind": kind, "expr": _render(fn), "file": self.module.rel,
                "line": node.lineno, "in": here, "resolved_name": None,
                "blind": True,
            })
        # blind spots -- recorded whether or not we are inside a function, since
        # a module-level getattr is just as invisible.
        callee_name = (
            fn.id if isinstance(fn, ast.Name)
            else fn.attr if isinstance(fn, ast.Attribute) else None
        )
        if callee_name in BLIND_SPOT_CALLS:
            # getattr(x, "literal") is not blind -- the name is right there and
            # an analyser can read it. Only a computed name hides an access, so
            # the literal form is recorded as resolved rather than inflating the
            # blind-spot count with sites nobody needs to review.
            literal = constant_name_arg(node)
            self.blind.append(
                {
                    "kind": BLIND_SPOT_CALLS[callee_name],
                    "expr": callee_name,
                    "file": self.module.rel,
                    "line": node.lineno,
                    "in": here,
                    "resolved_name": literal,
                    "blind": literal is None,
                }
            )

        self._classify_op(node, here)
        target = self._resolve_call(fn)
        if target:
            self.calls.setdefault(here, set()).add(target)
        elif isinstance(fn, ast.Name | ast.Attribute):
            base = fn.value if isinstance(fn, ast.Attribute) else None
            self.unresolved.append(
                {
                    "callee": _render(fn),
                    "file": self.module.rel,
                    "line": node.lineno,
                    "in": here,
                    "base": base.id if isinstance(base, ast.Name) else None,
                    "chained": base is not None and not isinstance(base, ast.Name),
                    "base_binding": (
                        self.module.bindings.get(base.id)
                        if isinstance(base, ast.Name) else None
                    ),
                }
            )
        self.generic_visit(node)

    def _classify_op(self, node: ast.Call, here: str) -> None:
        fn = node.func
        name = (
            fn.id if isinstance(fn, ast.Name)
            else fn.attr if isinstance(fn, ast.Attribute) else None
        )
        if name is None:
            return
        # Entity(...) -- a construction is a create, and it is the one operation
        # that is unambiguous in the AST.
        if isinstance(fn, ast.Name) and fn.id in self.entity_symbols:
            self._op(here, self.entity_symbols[fn.id], "create", node.lineno)
            return
        bucket = (
            "read" if name in READ_CALLS
            else "delete" if name in DELETE_CALLS
            else "update" if name in UPDATE_CALLS
            else None
        )
        if bucket is None:
            return
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            for sub in ast.walk(arg):
                if isinstance(sub, ast.Name) and sub.id in self.entity_symbols:
                    self._op(here, self.entity_symbols[sub.id], bucket, node.lineno)

    def _op(self, here: str, table: str, op: str, line: int) -> None:
        self.ops.setdefault(here, {}).setdefault(table, set()).add(op)

    def _resolve_call(self, fn: ast.expr) -> str | None:
        if isinstance(fn, ast.Name):
            bound = self.module.bindings.get(fn.id)
            if bound and ":" in bound:
                return bound
            if fn.id in self.module.functions:
                return f"{self.module.dotted}:{fn.id}"
            return None
        if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
            base = self.module.bindings.get(fn.value.id)
            if base and ":" not in base:  # a module
                return f"{base}:{fn.attr}"
        return None


def _render(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_render(node.value)}.{node.attr}"
    return type(node).__name__


# --------------------------------------------------------------------------
# entry points declared by the target


def read_config(target: Path) -> dict:
    path = target / "capcov.toml"
    if not path.exists():
        return {}
    return tomllib.loads(path.read_text())


def declared_entry_points(config: dict) -> list[dict]:
    out = []
    for item in config.get("entry_points", []):
        out.append(
            {
                "id": item["id"],
                "kind": item.get("kind", "declared"),
                "handler": item["symbol"],
                "path": None,
                "method": None,
                "file": None,
                "line": None,
                "mounted": True,
            }
        )
    out.sort(key=lambda s: s["id"])
    return out


# --------------------------------------------------------------------------
# the adapter entry point


def discover(source_root: Path, target: Path, name_match: bool = True) -> dict:
    modules = _load_modules(source_root)
    entities, symbol_to_table = discover_entities(modules)
    surfaces = discover_surfaces(modules)
    config = read_config(target)
    sqlite_ddl = config.get("capcov", {}).get("sqlite_ddl", False)
    if not isinstance(sqlite_ddl, bool):
        raise ValueError("[capcov] sqlite_ddl must be a boolean")
    surfaces = surfaces + declared_entry_points(config)

    direct: dict[str, set[str]] = {}
    calls: dict[str, set[str]] = {}
    ops: dict[str, dict[str, set[str]]] = {}
    unresolved: list[dict] = []
    blind: list[dict] = []
    evidence: dict[str, list[dict]] = {}

    for module in modules.values():
        # Which entity classes does THIS module have a name for? Requiring a
        # binding is what stops a local variable called `Export` from being read
        # as the exports table.
        local: dict[str, str] = {}
        for name, bound in module.bindings.items():
            if bound in symbol_to_table:
                local[name] = symbol_to_table[bound]
        for qualified, table in symbol_to_table.items():
            mod, _, sym = qualified.partition(":")
            if mod == module.dotted:
                local[sym] = table
        walker = _FunctionWalker(module, local, sqlite_ddl=sqlite_ddl)
        walker.visit(module.tree)
        # Preserve ORM declarations when both styles name the same entity. DDL
        # creates a denominator obligation, never a route or CRUD binding.
        known_entities = {entity["name"] for entity in entities}
        for entity in walker.sql_entities:
            if entity["name"] not in known_entities:
                entities.append(entity)
                known_entities.add(entity["name"])
        direct.update(walker.direct)
        for key, callees in walker.calls.items():
            calls.setdefault(key, set()).update(callees)
        for key, table_ops in walker.ops.items():
            for table, verbs in table_ops.items():
                ops.setdefault(key, {}).setdefault(table, set()).update(verbs)
        unresolved.extend(walker.unresolved)
        blind.extend(walker.blind)
        for key, items in walker.evidence.items():
            evidence.setdefault(key, []).extend(items)

    # ------------------------------------------------------------------
    # Second-tier resolution, and the residue.
    #
    # `service.create_job(...)` is unresolvable through imports -- the type of
    # `service` is not in the AST. But if exactly one function in the whole tree
    # is named create_job, the edge is almost certainly that one. Almost is why
    # the tier is recorded on the edge rather than merged silently.
    #
    # What is left after that is the residue, and it is split rather than
    # totalled: a call to a name no project function has is an external library
    # and no analyser was ever going to follow it, while a call to a name TWO
    # project functions share is a genuinely missed edge. Reporting 971
    # "unresolved calls" as one number hides the eight that matter.
    by_name: dict[str, list[str]] = {}
    for key in set(direct) | set(calls):
        by_name.setdefault(key.rsplit(":", 1)[1].rsplit(".", 1)[-1], []).append(key)

    # Names that are also methods on a builtin container. `run.get(...)` is far
    # more likely to be a dict than the project's BlobStorage.get, and nothing
    # in the AST settles it. Counted as ambiguous-with-a-builtin and not listed:
    # a residue list nobody can act on is a residue list nobody reads.
    builtin_names = set()
    for builtin in (dict, list, set, str, tuple, bytes):
        builtin_names.update(n for n in dir(builtin) if not n.startswith("_"))

    project = {
        entry.name.removesuffix(".py")
        for entry in source_root.iterdir()
        if entry.is_dir() or entry.suffix == ".py"
    }

    name_matched = 0
    ambiguous: list[dict] = []
    external = 0
    chained = 0
    builtin_shadowed = 0
    for call in unresolved:
        binding = call.get("base_binding")
        if binding and binding.split(".")[0].split(":")[0] not in project:
            # os.environ.get, boto3.client(...). A method on something imported
            # from outside the tree is not a missed project edge, and counting
            # it as residue buries the ones that are.
            external += 1
            continue
        if call.get("chained"):
            # f(x).get(...) -- the receiver is an expression, so there is no
            # name to match on. Counted, not listed: nothing to review.
            chained += 1
            continue
        leaf = call["callee"].rsplit(".", 1)[-1]
        candidates = sorted(by_name.get(leaf, []))
        if candidates and call.get("base") and leaf in builtin_names:
            builtin_shadowed += 1
            continue
        if not candidates:
            external += 1
        elif len(candidates) == 1 and name_match:
            calls.setdefault(call["in"], set()).add(candidates[0])
            name_matched += 1
        elif len(candidates) == 1:
            ambiguous.append({**call, "candidates": candidates, "why": "name-match off"})
        else:
            # Several project functions share this name. This is where static
            # analysis actually loses interface dispatch: blobs.get() could be
            # any of three implementations and the AST does not say which. Bound
            # to none of them ON PURPOSE -- binding all three would invent
            # capabilities and fail the build for tests that were never missing.
            ambiguous.append({**call, "candidates": candidates, "why": "ambiguous name"})

    blind.sort(key=lambda b: (b["file"], b["line"]))
    entities.sort(key=lambda e: e["name"])
    ambiguous.sort(key=lambda u: (u["file"], u["line"]))

    return {
        "entities": entities,
        "surfaces": surfaces,
        "modules": len(modules),
        "_direct": direct,
        "_calls": calls,
        "_ops": ops,
        "_evidence": evidence,
        "residue": ambiguous,
        "residue_summary": {
            "resolved_by_import": sum(len(v) for v in calls.values()) - name_matched,
            "resolved_by_name": name_matched,
            "ambiguous": len(ambiguous),
            "external": external,
            "chained": chained,
            "builtin_shadowed": builtin_shadowed,
        },
        "blind_spots": blind,
    }
