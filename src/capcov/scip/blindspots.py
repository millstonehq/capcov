"""Enumerate the blind spots no static resolver can see through, and diff the
AST's call sites against what SCIP managed to resolve.

This is the half SCIP cannot do. A `getattr(obj, name)`, an `eval`, a computed
`__import__` -- these have no static target for ANY resolver, SCIP included,
because the target is a value built at runtime, not a source token. capcov's
contract is that such a site is NAMED, not silently dropped: a resolver that
loses an edge reports fewer capabilities and looks identical to one that found
them all. So the hybrid keeps two things SCIP does not give you.

* ``enumerate_blind_spots`` lists every dynamic-access site (getattr / setattr /
  eval / exec / dynamic import / namespace reflection) with a file and a line.
  These are unresolvable by construction -- the enumerable-unresolved floor that
  is true no matter how good the resolver is.

* ``blind_spot_residue`` takes the call sites the AST pass SAW and the sites SCIP
  reported resolving, and returns the difference. SCIP never tells you what it
  failed to resolve; this recovers that set from the black box by subtraction --
  everything enumerated, minus everything resolved -- and marks each remainder
  unresolved rather than dropping it.

The enumerator is per language. ``enumerate_call_sites``/``enumerate_blind_spots``
take a ``language`` (defaulting to ``"python"`` so the resolver integrates with no
ordering dependency): Python is a pure ``ast`` census + the dynamic-access builtin
inventory; Go and PHP are a tree-sitter census + that language's
reflection/dynamic-dispatch inventory (Go ``reflect`` and interface dispatch; PHP
variable-variables, dynamic method names, and facade ``__call``/``__callStatic``
magic -- the known no-larastan scip-php blind spot). A resolver that returned an
empty census for Go/PHP would look byte-identical to a clean run, so a language
with no strategy raises rather than returning ``[]``; that is the soundiness floor
the hybrid exists to hold.

The Python path is pure stdlib and always importable. The tree-sitter half is
imported lazily, only when a Go/PHP enumeration is actually requested (exactly as
``flows/discovery.py`` does), so ``blindspots`` stays dependency-free for the
Python resolver and for ``capcov gate``, and ``blind_spot_residue`` -- the
subtraction -- is language-agnostic and testable with a resolver's output fed in
by hand, no indexer installed.
"""

from __future__ import annotations

import ast
from pathlib import Path

# The dynamic-access builtins. Each names an access whose target is a value, not
# a source token: `getattr(o, name)` reaches an attribute chosen at runtime and
# no static resolver can say which. The kind groups them by WHY they are blind,
# which is what a reader triaging the list acts on. This mapping is the single
# source of truth -- the FastAPI/SQLAlchemy adapter imports it back rather than
# keeping its own copy, so the two never disagree about what counts.
DYNAMIC_BLIND_CALLS = {
    "getattr": "attribute_by_name",
    "setattr": "attribute_by_name",
    "delattr": "attribute_by_name",
    "vars": "namespace_lookup",
    "globals": "namespace_lookup",
    "locals": "namespace_lookup",
    "eval": "dynamic_eval",
    "exec": "dynamic_eval",
    "__import__": "dynamic_import",
    "import_module": "dynamic_import",
}

_REASONS = {
    "attribute_by_name": (
        "attribute accessed by a computed name; the target is not statically "
        "knowable"
    ),
    "namespace_lookup": (
        "namespace read by reflection; its members are not statically enumerable"
    ),
    "dynamic_eval": (
        "code assembled and evaluated at runtime; there is no static call target"
    ),
    "dynamic_import": (
        "module imported by a computed name; the import target is not statically "
        "knowable"
    ),
}

_RESIDUE_REASON = "seen by the ast pass; scip returned no resolution for this site"


def constant_name_arg(node: ast.Call) -> str | None:
    """The 2nd positional argument when it is a string constant, else None.

    ``getattr(o, "field")`` is NOT blind: the name is a literal token any
    analyser can read. Only a computed name hides the access. Returning the
    literal lets a caller record the resolved form instead of inflating the
    blind-spot count with sites nobody needs to review.
    """
    if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
        value = node.args[1].value
        return value if isinstance(value, str) else None
    return None


def blind_spot_for_call(node: ast.Call) -> tuple[str, str | None] | None:
    """``(kind, statically_resolved_name)`` if the call is a dynamic-access builtin.

    ``resolved_name`` is the literal attribute/module name when the call spells
    it out (so it is NOT blind); None means the access is computed and blind.
    Returns None when the call is not a dynamic-access builtin at all.
    """
    fn = node.func
    name = (
        fn.id
        if isinstance(fn, ast.Name)
        else fn.attr
        if isinstance(fn, ast.Attribute)
        else None
    )
    if name not in DYNAMIC_BLIND_CALLS:
        return None
    return DYNAMIC_BLIND_CALLS[name], constant_name_arg(node)


def _iter_py_files(root: Path):
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def _render_callee(node: ast.Call) -> str:
    """A readable name for a call's target: ``f``, ``a.b.c``, or the node type
    for a call on an expression (``f()()``). Best-effort; never raises."""
    fn = node.func
    parts: list[str] = []
    while isinstance(fn, ast.Attribute):
        parts.append(fn.attr)
        fn = fn.value
    if isinstance(fn, ast.Name):
        parts.append(fn.id)
    else:
        parts.append(type(fn).__name__)
    return ".".join(reversed(parts))


def enumerate_call_sites(
    source_tree: str | Path, language: str = "python"
) -> list[dict]:
    """Every call site under ``source_tree``, as ``{file, line, callee}``.

    This is the left-hand side of the SCIP residue subtraction: the complete set
    of call sites the enumerator SAW, so that ``blind_spot_residue`` can name the
    ones SCIP stayed silent about. It deliberately includes ordinary calls, not
    just dynamic-access builtins -- because SCIP's most important silent gap is
    an ordinary method call on a receiver it could not type (``session.add(job)``
    on an untyped ``session`` emits no occurrence at all), and only a full call
    census surfaces it. ``file`` is POSIX and tree-relative; ``line`` is the
    1-based line, matching ``enumerate_blind_spots``. Sorted by
    (file, line, callee) for a stable diff.

    ``language`` selects the strategy: ``"python"`` (the default) is the pure
    ``ast`` census below; ``"go"``/``"php"`` are the tree-sitter census. An
    unknown language raises rather than returning ``[]`` -- an empty census would
    make an unsupported language look identical to a clean one.
    """
    if language != "python":
        if language in _TS_EXTENSIONS:
            return _ts_call_sites(source_tree, language)
        raise ValueError(f"no call-site census strategy for language {language!r}")
    root = Path(source_tree)
    out: list[dict] = []
    for path in _iter_py_files(root):
        rel = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            out.append(
                {"file": rel, "line": node.lineno, "callee": _render_callee(node)}
            )
    out.sort(key=lambda c: (c["file"], c["line"], c["callee"]))
    return out


def enumerate_blind_spots(
    source_tree: str | Path, language: str = "python"
) -> list[dict]:
    """Every dynamic-dispatch blind spot under ``source_tree``.

    Returns ``{file, line, kind, reason}`` per site, ``file`` POSIX and relative
    to the tree root, sorted so the list is stable to diff. Only genuinely blind
    sites are returned: a ``getattr(o, "literal")`` is statically resolvable and
    omitted, matching the adapter's own blind / not-blind split.

    ``language`` selects the inventory: ``"python"`` (the default) is the
    dynamic-access builtin census below; ``"go"`` is ``reflect`` +
    interface dispatch; ``"php"`` is variable-variables, dynamic method names,
    and facade ``__call``/``__callStatic`` magic. Each returned site is keyed to
    a call-site line so ``_residue`` keeps it unconditionally. An unknown
    language raises rather than returning ``[]``.
    """
    if language != "python":
        if language in _TS_BLIND:
            return _TS_BLIND[language](source_tree)
        raise ValueError(f"no blind-spot inventory for language {language!r}")
    root = Path(source_tree)
    out: list[dict] = []
    for path in _iter_py_files(root):
        rel = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            classified = blind_spot_for_call(node)
            if classified is None:
                continue
            kind, resolved = classified
            if resolved is not None:
                # the literal form -- statically readable, so not a blind spot
                continue
            out.append(
                {
                    "file": rel,
                    "line": node.lineno,
                    "kind": kind,
                    "reason": _REASONS[kind],
                }
            )
    out.sort(key=lambda b: (b["file"], b["line"]))
    return out


def _site_key(site: dict) -> tuple[str, int]:
    return (site["file"], site["line"])


def blind_spot_residue(
    ast_call_sites: list[dict],
    scip_resolved_sites: list[dict],
    reason: str = _RESIDUE_REASON,
) -> list[dict]:
    """The call sites the AST pass SAW but SCIP did NOT resolve, matched by file+line.

    SCIP reports what it resolved and stays silent about the rest, so its own
    unresolved list is recovered here by subtraction: every site in
    ``ast_call_sites`` whose (file, line) is absent from ``scip_resolved_sites``.
    Each remainder is returned with the original site's fields preserved, plus
    ``resolved: False`` and a ``reason`` -- NAMED, not dropped, which is the
    capcov property the hybrid must not lose to a black-box resolver.

    Matching is exact on (file, line); both sides must use the same file
    convention (tree-relative POSIX, as ``enumerate_blind_spots`` emits). When a
    site already carries its own ``reason`` (e.g. a blind-spot kind), it is kept
    and the residue clause is appended, so no information is lost.
    """
    resolved_keys = {_site_key(s) for s in scip_resolved_sites}
    residue: list[dict] = []
    for site in ast_call_sites:
        if _site_key(site) in resolved_keys:
            continue
        prior = site.get("reason")
        residue.append(
            {
                **site,
                "resolved": False,
                "reason": f"{prior}; {reason}" if prior else reason,
            }
        )
    residue.sort(key=_site_key)
    return residue


# ---------------------------------------------------------------------------
# Per-language tree-sitter enumerators (Go, PHP).
#
# Everything above this line is the Python strategy: pure ``ast``, no optional
# dependency. Every other language needs a real parse tree, so this half lazily
# imports the ``treesitter`` extra (tree-sitter + tree-sitter-language-pack) --
# the same lazy import ``flows/discovery.py`` uses -- and stays out of the
# default import path so ``blindspots`` remains stdlib-only for the Python
# resolver and ``capcov gate``. The soundiness contract governs this half: a
# census that silently returned [] for Go/PHP would be indistinguishable from a
# fully resolved run, so a missing extra RAISES and a language with no strategy
# RAISES; neither ever no-ops into a false-clean.
# ---------------------------------------------------------------------------

# Go reflection / dynamic dispatch. A ``reflect.*`` call builds its target from a
# runtime value, and reflect.Value's ``MethodByName``/``FieldByName`` select a
# method or field by a string no static resolver can follow. These two names are
# distinctive enough to flag with a near-zero false-positive rate; the broader
# reflect chain (``.Call``/``.Method`` on a Value) is caught on the same line via
# the ``reflect.`` package call or the ``MethodByName`` that produced the Value.
GO_REFLECT_PACKAGE = "reflect"
GO_DYNAMIC_METHODS = {
    "MethodByName": "dynamic_method",
    "FieldByName": "dynamic_field",
}

# Laravel facades whose static calls route through ``__callStatic`` and whose
# fluent builders route through ``__call``. Without larastan, scip-php cannot
# type these chains, so a call rooted at one is the known, enumerable PHP blind
# spot (design R4). This is the single source of truth -- the php_eloquent
# recognizer (task T4) imports it back rather than keeping its own copy, the same
# way the FastAPI adapter imports ``DYNAMIC_BLIND_CALLS``.
PHP_MAGIC_FACADES = frozenset({
    "DB", "Schema", "Cache", "Redis", "Storage", "Queue", "Route",
    "Config", "Log", "Auth", "Session", "Gate", "Event", "Mail",
    "Http", "Validator", "Cookie", "Crypt", "File", "Hash", "View",
    "Notification", "Password", "Response", "URL", "Artisan", "Blade",
})

_GO_REASONS = {
    "reflection": (
        "call target built from a runtime value via reflect; no static target"
    ),
    "dynamic_method": (
        "method selected by a runtime string via reflect; no static target"
    ),
    "dynamic_field": (
        "field selected by a runtime string via reflect; no static target"
    ),
    "interface_dispatch": (
        "call through an interface value; the concrete implementation that "
        "actually touches data is not statically known"
    ),
}

_PHP_REASONS = {
    "variable_variable": (
        "callee named by a variable variable ($$name); the target is not "
        "statically knowable"
    ),
    "dynamic_dispatch": (
        "method named by a runtime value ($obj->$m / Cls::$m); routes through "
        "__call/__callStatic, invisible to scip-php"
    ),
    "facade_magic": (
        "static call on a Laravel facade; routes through __callStatic and is "
        "untyped without larastan, invisible to scip-php"
    ),
}

_TS_EXTENSIONS = {"go": (".go",), "php": (".php", ".phtml", ".php3", ".php4", ".php5")}

_TS_CALL_TYPES = {
    "go": {"call_expression"},
    "php": {
        "function_call_expression",
        "member_call_expression",
        "nullsafe_member_call_expression",
        "scoped_call_expression",
    },
}


def _ts_language(language: str):
    """The tree-sitter module + compiled grammar for ``language``, imported lazily.

    Raises ``ValueError`` (never returns a no-op) when the optional extra is not
    installed, so a Go/PHP enumeration cannot silently degrade to an empty census
    and manufacture a false-clean denominator.
    """
    try:
        import tree_sitter as ts
        from tree_sitter_language_pack import get_language
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise ValueError(
            f"blind-spot enumeration for {language!r} needs the 'treesitter' extra"
        ) from exc
    return ts, get_language(language)


def _ts_files(root: str | Path, language: str):
    """Every source file of ``language`` under ``root``, sorted, tree-relative.

    Mirrors ``_iter_py_files``: a deterministic walk over the language's source
    extensions, skipping the VCS directory. The census must be COMPLETE, so no
    first-party/third-party filtering happens here -- scoping is the caller's job
    (it chooses ``root``)."""
    root = Path(root)
    seen: set[Path] = set()
    for ext in _TS_EXTENSIONS[language]:
        for path in root.rglob(f"*{ext}"):
            if path.is_file() and ".git" not in path.parts:
                seen.add(path)
    yield from sorted(seen)


def _walk(node):
    """Depth-first over every named node in the subtree (order-independent; the
    callers sort their output)."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.named_children))


# --- Go -------------------------------------------------------------------

def _go_expr_name(node) -> str:
    """A readable dotted name for a Go call target. Best-effort, never raises --
    the parallel of the Python ``_render_callee``."""
    if node is None:
        return "?"
    if node.type in ("identifier", "field_identifier", "type_identifier",
                     "package_identifier"):
        return node.text.decode()
    if node.type == "selector_expression":
        operand = _go_expr_name(node.child_by_field_name("operand"))
        field = node.child_by_field_name("field")
        return f"{operand}.{field.text.decode()}" if field is not None else operand
    if node.type == "call_expression":
        return _go_expr_name(node.child_by_field_name("function"))
    return node.type


def _go_callee(node) -> str:
    return _go_expr_name(node.child_by_field_name("function"))


def _go_interface_types(root) -> set[str]:
    """Names of interface types declared IN THIS FILE.

    A within-file heuristic: interface types imported from elsewhere are not
    resolved here, and the residue subtraction (census minus resolved) is the
    backstop that keeps those from being dropped. This inventory is the
    best-effort floor, not an exhaustive interface index."""
    names: set[str] = set()
    for node in _walk(root):
        if node.type != "type_spec":
            continue
        ty = node.child_by_field_name("type")
        name = node.child_by_field_name("name")
        if ty is not None and ty.type == "interface_type" and name is not None:
            names.add(name.text.decode())
    return names


def _go_interface_vars(root, interface_types: set[str]) -> set[str]:
    """Identifiers bound to an in-file interface type (params, typed vars).

    Any node carrying a ``type`` field that names an interface contributes its
    ``identifier`` children -- this covers ``parameter_declaration`` (``f F``)
    and typed ``var_spec`` (``var f F``) uniformly without hardcoding node
    types."""
    bound: set[str] = set()
    if not interface_types:
        return bound
    for node in _walk(root):
        ty = node.child_by_field_name("type")
        if ty is None or ty.type != "type_identifier":
            continue
        if ty.text.decode() not in interface_types:
            continue
        for child in node.named_children:
            if child.type == "identifier":
                bound.add(child.text.decode())
    return bound


def _go_blind_kind(call_node, interface_vars: set[str]) -> str | None:
    fn = call_node.child_by_field_name("function")
    if fn is None or fn.type != "selector_expression":
        return None
    operand = fn.child_by_field_name("operand")
    field = fn.child_by_field_name("field")
    field_name = field.text.decode() if field is not None else ""
    if (
        operand is not None
        and operand.type == "identifier"
        and operand.text.decode() == GO_REFLECT_PACKAGE
    ):
        return "reflection"
    if field_name in GO_DYNAMIC_METHODS:
        return GO_DYNAMIC_METHODS[field_name]
    if (
        operand is not None
        and operand.type == "identifier"
        and operand.text.decode() in interface_vars
    ):
        return "interface_dispatch"
    return None


def _go_blind_spots(source_tree: str | Path) -> list[dict]:
    ts, language = _ts_language("go")
    parser = ts.Parser(language)
    root = Path(source_tree)
    out: list[dict] = []
    for path in _ts_files(root, "go"):
        rel = path.relative_to(root).as_posix()
        tree = parser.parse(path.read_bytes())
        interface_types = _go_interface_types(tree.root_node)
        interface_vars = _go_interface_vars(tree.root_node, interface_types)
        for node in _walk(tree.root_node):
            if node.type != "call_expression":
                continue
            kind = _go_blind_kind(node, interface_vars)
            if kind is None:
                continue
            out.append({
                "file": rel,
                "line": node.start_point[0] + 1,
                "kind": kind,
                "reason": _GO_REASONS[kind],
            })
    out.sort(key=lambda b: (b["file"], b["line"], b["kind"]))
    return out


# --- PHP ------------------------------------------------------------------

def _php_name(node) -> str:
    """A readable name for a PHP callee fragment. Best-effort, never raises."""
    if node is None:
        return "?"
    if node.type in ("name", "variable_name", "dynamic_variable_name"):
        return node.text.decode()
    text = node.text.decode()
    return text if "\n" not in text and len(text) <= 40 else node.type


def _php_callee(node) -> str:
    kind = node.type
    if kind == "function_call_expression":
        return _php_name(node.child_by_field_name("function"))
    if kind in ("member_call_expression", "nullsafe_member_call_expression"):
        obj = _php_name(node.child_by_field_name("object"))
        sep = "?->" if kind.startswith("nullsafe") else "->"
        return f"{obj}{sep}{_php_name(node.child_by_field_name('name'))}"
    if kind == "scoped_call_expression":
        scope = _php_name(node.child_by_field_name("scope"))
        return f"{scope}::{_php_name(node.child_by_field_name('name'))}"
    return kind


def _php_blind_kind(node) -> str | None:
    kind = node.type
    if kind == "function_call_expression":
        fn = node.child_by_field_name("function")
        if fn is not None and fn.type == "dynamic_variable_name":
            return "variable_variable"
        return None
    if kind in ("member_call_expression", "nullsafe_member_call_expression"):
        name = node.child_by_field_name("name")
        # A dynamic method name ($obj->$m() / $obj->{$e}()) is not a plain ``name``.
        if name is not None and name.type != "name":
            return "dynamic_dispatch"
        return None
    if kind == "scoped_call_expression":
        name = node.child_by_field_name("name")
        if name is not None and name.type != "name":
            return "dynamic_dispatch"
        scope = node.child_by_field_name("scope")
        if (
            scope is not None
            and scope.type == "name"
            and scope.text.decode() in PHP_MAGIC_FACADES
        ):
            return "facade_magic"
        return None
    return None


def _php_blind_spots(source_tree: str | Path) -> list[dict]:
    ts, language = _ts_language("php")
    parser = ts.Parser(language)
    root = Path(source_tree)
    out: list[dict] = []
    for path in _ts_files(root, "php"):
        rel = path.relative_to(root).as_posix()
        tree = parser.parse(path.read_bytes())
        for node in _walk(tree.root_node):
            kind = _php_blind_kind(node)
            if kind is None:
                continue
            out.append({
                "file": rel,
                "line": node.start_point[0] + 1,
                "kind": kind,
                "reason": _PHP_REASONS[kind],
            })
    out.sort(key=lambda b: (b["file"], b["line"], b["kind"]))
    return out


# --- Generic tree-sitter census + strategy tables -------------------------

_CALLEE_RENDERERS = {"go": _go_callee, "php": _php_callee}


def _ts_call_sites(source_tree: str | Path, language: str) -> list[dict]:
    """The full call-site census for a tree-sitter language.

    Every node whose type is a call form for ``language`` becomes a
    ``{file, line, callee}`` entry -- the same shape and sort key the Python
    census emits, so ``blind_spot_residue`` diffs the two uniformly. Nested calls
    in a fluent chain (``DB::table(x)->where(y)->first()``) each count as their
    own site, matching the Python census's treatment of ``f()()``."""
    ts, grammar = _ts_language(language)
    parser = ts.Parser(grammar)
    call_types = _TS_CALL_TYPES[language]
    render = _CALLEE_RENDERERS[language]
    root = Path(source_tree)
    out: list[dict] = []
    for path in _ts_files(root, language):
        rel = path.relative_to(root).as_posix()
        tree = parser.parse(path.read_bytes())
        for node in _walk(tree.root_node):
            if node.type in call_types:
                out.append({
                    "file": rel,
                    "line": node.start_point[0] + 1,
                    "callee": render(node),
                })
    out.sort(key=lambda c: (c["file"], c["line"], c["callee"]))
    return out


_TS_BLIND = {"go": _go_blind_spots, "php": _php_blind_spots}
