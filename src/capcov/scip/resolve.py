"""The resolver seam: source the entity pipeline's call graph from SCIP.

This is where the hybrid is wired into the entity pipeline. ``capcov discover``
normally takes its call graph from the AST adapter's own import-plus-name-match
resolution; with ``--resolver scip`` it takes the graph from a SCIP indexer
instead, which is type-aware, cross-file and multi-language where the AST pass is
none of those. The AST adapter still runs -- it remains the source of entities,
surfaces, operations and, load-bearing, the enumerated blind spots -- so the two
halves are exactly the division ADR-0001 settles: SCIP resolves, the AST pass
says where resolution is blind.

Three properties are kept and none is free:

* **fixpoint sees the same shape.** ``core.fixpoint`` consumes ``calls`` as
  ``{node: {node, ...}}`` and ``direct`` as ``{node: {entity, ...}}``. This
  module translates every SCIP call edge from a global SCIP symbol back into the
  AST adapter's own node identity (``module.dotted:qualname``) and hands fixpoint
  a ``calls`` map of exactly that shape -- so surfaces (roots), ``direct`` and
  ``ops`` all still line up and fixpoint is untouched.

* **a name is never dropped.** Every edge SCIP resolved is carried whole in the
  artifact (``scip_resolved_edges``), including the module-scope edges the
  fixpoint cannot root at. And the sites SCIP stayed *silent* about are recovered
  by subtraction: every call site the AST pass saw, minus every site SCIP
  resolved, is the enumerated residue -- named, with a file and a line, not lost.

* **it is optional.** The module imports with no node/go tooling present (it
  pulls in ``runner``/``map``/``blindspots``, all stdlib). Only ``resolve`` --
  which actually shells out to an indexer -- needs the tools, and it fails with a
  named, actionable error when they are absent rather than silently degrading.

The translation is **per-indexer**: each SCIP indexer emits its own
namespace-descriptor convention, and one Python-shaped translator silently
mistranslates the others (scip-php's bare ``App/Http/Controllers/`` descriptors
map to ``None`` and every PHP edge is dropped; a whole-language failure reads as
a green run). So the symbol->node translation is a per-language *normalizer*
strategy (``normalizer(language)``) behind a stable interface, injected into the
uniform orchestration. A normalizer consumes ALL leading namespace descriptors
(bare *or* backtick-quoted) as the package and the type/term/method descriptors
as the qualname; only the namespace separator differs per language (``.`` python,
``/`` go import path, ``\\`` php namespace). The node string it produces is the
fixpoint node key, co-designed to equal the per-language handler adapter's key;
where that byte-equality is not mechanically reproducible, the (file,line)-join
(``scip_defs_by_location`` + the deep ``hybrid_raw``) is the binding of record so
a residual mismatch becomes a NAMED unresolved, never a silent drop. A symbol a
normalizer cannot parse (a parameter, a local, a package with no member, a scheme
it does not know) yields ``None`` and its edge is simply not rooted -- never a
crash, and never a wrong node.
"""

from __future__ import annotations

import inspect
import shutil
from collections.abc import Callable
from pathlib import Path

from . import blindspots
from . import map as scip_map
from . import runner


def _blindspots_accept_language(func: Callable) -> bool:
    """Whether a blindspots enumerator takes a ``language`` keyword.

    The per-language enumerator (§5) is a sibling task's deliverable, delivered
    ``language``-defaulted so this module integrates without an ordering
    constraint. Until it lands the python-only signature is called unchanged, so
    the shallow python path is byte-identical either way; a deep go/php index
    passes its language through the moment the enumerator accepts it.
    """
    try:
        return "language" in inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False


def _enumerate_call_sites(source_root: str | Path, language: str) -> list[dict]:
    if _blindspots_accept_language(blindspots.enumerate_call_sites):
        return blindspots.enumerate_call_sites(source_root, language=language)
    return blindspots.enumerate_call_sites(source_root)


def _enumerate_blind_spots(source_root: str | Path, language: str) -> list[dict]:
    if _blindspots_accept_language(blindspots.enumerate_blind_spots):
        return blindspots.enumerate_blind_spots(source_root, language=language)
    return blindspots.enumerate_blind_spots(source_root)

# SCIP occurrence lines are 0-based and delivered unmodified by the runner; the
# AST pass (and every human-facing capcov line) is 1-based. The residue subtracts
# SCIP-resolved sites from AST-seen sites by (file, line), so SCIP's line is
# lifted into the AST's 1-based frame here -- a mismatch would make every site
# look unresolved. The (file,line)-join keys are in this same 1-based frame.
_SCIP_LINE_IS_ZERO_BASED = 1

# Characters that terminate a descriptor name in the SCIP symbol grammar.
_NAME_STOP = set("#.(:[!/`")


def _strip_scip_prefix(symbol: str | None) -> str | None:
    """The descriptor tail of a SCIP symbol, or None for a non-node symbol.

    A SCIP symbol is ``<scheme> <manager> <package-name> <version> <descriptor>+``
    (four space-separated prefix fields, none of which contains an unescaped
    space for any indexer capcov drives) or ``local <id>``. This returns
    everything after the version -- the descriptor sequence -- and None for a
    local or a string with fewer than four spaces (a scheme this does not know).
    """
    if not symbol or symbol.startswith("local "):
        return None
    parts = symbol.split(" ", 4)
    if len(parts) < 5:
        return None
    return parts[4]


def _parse_descriptor(descriptor: str) -> tuple[list[str], list[str]] | None:
    """Split a descriptor tail into (namespace names, type/term/method names).

    Walks the SCIP descriptor grammar once. A namespace (``name/``, bare or
    backtick-quoted) contributes to the package; a type (``Name#``), a term/field
    (``name.``) and a method (``name().``) contribute to the qualname. A parameter
    (``(name)``), a type parameter (``[name]``), a meta (``name:``), a macro
    (``name!``), or a namespace that follows a type/term/method mean the symbol is
    not a callable/type node -- the whole parse returns None rather than a
    partial, wrong node. A symbol with no member after its package (a bare
    package) also returns None.
    """
    namespaces: list[str] = []
    tail: list[str] = []
    seen_member = False
    i, n = 0, len(descriptor)
    while i < n:
        if descriptor[i] in "([":  # leading parameter / type parameter
            return None
        if descriptor[i] == "`":
            end = descriptor.find("`", i + 1)
            if end == -1:
                return None
            name = descriptor[i + 1 : end]
            i = end + 1
        else:
            start = i
            while i < n and descriptor[i] not in _NAME_STOP:
                i += 1
            name = descriptor[start:i]
        if i >= n:
            return None  # a bare trailing name with no terminator is malformed
        term = descriptor[i]
        if term == "/":  # namespace / package segment
            if seen_member:
                return None  # a namespace after a member is not a node path
            namespaces.append(name)
            i += 1
        elif term == "#":  # type
            seen_member = True
            tail.append(name)
            i += 1
        elif term == ".":  # term / field
            seen_member = True
            tail.append(name)
            i += 1
        elif term == "(":  # method: skip the balanced disambiguator, expect '.'
            depth = 0
            while i < n:
                if descriptor[i] == "(":
                    depth += 1
                elif descriptor[i] == ")":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                i += 1
            if i < n and descriptor[i] == ".":
                seen_member = True
                tail.append(name)
                i += 1
            else:
                return None
        else:  # ':' meta, '!' macro, stray backtick
            return None
    if not tail:
        return None
    return namespaces, tail


class _Normalizer:
    """A per-language SCIP symbol -> fixpoint node translator.

    The parse (``_strip_scip_prefix`` + ``_parse_descriptor``) is shared; only
    the namespace separator differs, because that is the sole per-language
    variation in how a package path is spelled. The qualname is always
    dot-joined, matching how the engine splits ``package:qualname`` and how the
    native python adapter spells ``module.dotted:Class.method``.
    """

    def __init__(self, language: str, namespace_sep: str) -> None:
        self.language = language
        self.namespace_sep = namespace_sep

    def symbol_to_node(self, symbol: str | None) -> str | None:
        return self.explain(symbol)[0]

    def explain(self, symbol: str | None) -> tuple[str | None, str | None]:
        """``(node, None)`` for a rootable symbol, else ``(None, reason)``.

        The same parse as ``symbol_to_node`` but it says *why* a symbol is not a
        node instead of a silent ``None`` -- the static fact exporter records the
        reason as ``scip_symbol_unrooted(index, symbol, reason)`` (section 29).
        Reasons: ``local`` (a ``local N`` symbol), ``unknown-scheme`` (fewer than
        the four prefix fields of the SCIP grammar, or an empty symbol),
        ``non-node-descriptor`` (a parameter, type parameter, meta, macro or
        malformed descriptor tail), ``no-package`` (a member with no leading
        namespace, so nothing to root a node in).
        """
        if not symbol:
            return None, UNROOTED_UNKNOWN_SCHEME
        if symbol.startswith("local "):
            return None, UNROOTED_LOCAL
        descriptor = _strip_scip_prefix(symbol)
        if descriptor is None:
            return None, UNROOTED_UNKNOWN_SCHEME
        parsed = _parse_descriptor(descriptor)
        if parsed is None:
            return None, UNROOTED_NON_NODE
        namespaces, tail = parsed
        if not namespaces:
            return None, UNROOTED_NO_PACKAGE  # not a rootable in-project node
        package = self.namespace_sep.join(namespaces)
        return f"{package}:{'.'.join(tail)}", None


# The reasons ``_Normalizer.explain`` gives for a symbol it cannot root. Frozen
# vocabulary of ``scip_symbol_unrooted.reason`` in schema_static_v1 / section 29.
UNROOTED_LOCAL = "local"
UNROOTED_UNKNOWN_SCHEME = "unknown-scheme"
UNROOTED_NON_NODE = "non-node-descriptor"
UNROOTED_NO_PACKAGE = "no-package"
UNROOTED_REASONS = frozenset(
    {UNROOTED_LOCAL, UNROOTED_UNKNOWN_SCHEME, UNROOTED_NON_NODE, UNROOTED_NO_PACKAGE}
)


# The node-id spelling per language (see the co-design contract): python joins a
# dotted module (``shop.router:make_job``); go joins the import path with ``/``
# (``github.com/org/app/internal/jobs:Repo.Get``); php joins the namespace with
# ``\`` (``App\Http\Controllers:JobController.getJob``). The tree-sitter handler
# adapter must mint the identical string, or rely on the (file,line)-join.
_NORMALIZERS = {
    "python": _Normalizer("python", "."),
    "go": _Normalizer("go", "/"),
    "php": _Normalizer("php", "\\"),
}


def normalizer(language: str) -> _Normalizer:
    """The symbol->node normalizer for ``language``.

    Raises ``ValueError`` for a language with no normalizer, the same way the
    runner rejects an unknown indexer, so a caller cannot silently translate with
    the wrong scheme.
    """
    try:
        return _NORMALIZERS[language]
    except KeyError:
        raise ValueError(
            f"no SCIP normalizer for language {language!r}; "
            f"expected one of {sorted(_NORMALIZERS)}"
        ) from None


def scip_symbol_to_node(symbol: str | None) -> str | None:
    """A SCIP callable/type symbol -> ``package:qualname``, python spelling.

    Back-compatible shim over ``normalizer("python").symbol_to_node`` -- the
    python normalizer is the one co-designed with the native AST adapter
    (``\\`shop.router\\`/make_job().`` -> ``shop.router:make_job``). Prefer
    ``normalizer(language)`` for a non-python index.
    """
    return normalizer("python").symbol_to_node(symbol)


def calls_graph(
    edges: list[dict],
    is_node: Callable[[str], bool] | None = None,
    *,
    language: str = "python",
) -> dict[str, set[str]]:
    """A fixpoint ``calls`` map (``{node: {node, ...}}``) from SCIP call edges.

    Each edge's caller and callee SCIP symbols are translated to node identities
    by the ``language`` normalizer. An edge with no enclosing caller (a
    module-scope reference, ``caller is None``) or whose endpoints do not
    translate is not rooted -- it is still carried whole in the artifact's
    ``scip_resolved_edges``. When ``is_node`` is given, both endpoints must
    satisfy it, which keeps the graph inside the adapter's node namespace and
    drops edges into external libraries the same way the AST pass does.
    """
    to_node = normalizer(language).symbol_to_node
    calls: dict[str, set[str]] = {}
    for edge in edges:
        caller = to_node(edge.get("caller"))
        callee = to_node(edge.get("callee"))
        if caller is None or callee is None:
            continue
        if is_node is not None and not (is_node(caller) and is_node(callee)):
            continue
        calls.setdefault(caller, set()).add(callee)
    return calls


def resolved_sites(edges: list[dict]) -> list[dict]:
    """The ``{file, line}`` sites SCIP resolved, in the AST pass's 1-based frame.

    This is the right-hand side of the residue subtraction. SCIP never says what
    it failed to resolve; the sites it *did* resolve are its call-edge
    occurrences, and anything the AST saw that is absent here is a silent gap.
    """
    out: list[dict] = []
    for edge in edges:
        file, line = edge.get("file"), edge.get("line")
        if file is None or line is None:
            continue
        out.append({"file": file, "line": line + _SCIP_LINE_IS_ZERO_BASED})
    return out


def scip_defs_by_location(
    normalized: dict, *, language: str = "python"
) -> dict[tuple[str, int], str]:
    """``(file, 1-based line) -> node id`` for every SCIP DEFINITION.

    The **binding of record** for the deep (file,line)-join: SCIP owns the node
    spelling at each definition site, so the deep tree-sitter adapter never has
    to reproduce the indexer's namespace convention byte-for-byte -- it tags each
    handler / function / entity-touch node with the definition's ``(file, line)``
    and this map canonicalizes it. A definition whose symbol does not translate
    (a non-node) is skipped; the line is lifted to the AST's 1-based frame so it
    joins against the adapter's 1-based node locations. Later documents do not
    overwrite an earlier location (a symbol is defined once).
    """
    to_node = normalizer(language).symbol_to_node
    out: dict[tuple[str, int], str] = {}
    for document in normalized.get("documents", []) or []:
        path = document.get("path")
        for occ in document.get("occurrences", []) or []:
            if not occ.get("is_definition"):
                continue
            node = to_node(occ.get("symbol"))
            if node is None:
                continue
            line = occ.get("start_line")
            if line is None:
                continue
            key = (path, line + _SCIP_LINE_IS_ZERO_BASED)
            out.setdefault(key, node)
    return out


def _residue(
    source_root: str | Path, edges: list[dict], language: str = "python"
) -> list[dict]:
    """The enumerated residue: every call site the AST saw that SCIP left blind.

    Two silences are folded together, because both are things SCIP cannot resolve
    and capcov must name rather than drop:

    * **dynamic-access blind spots** (getattr / eval / dynamic import). SCIP does
      emit an occurrence for the *builtin call itself* -- ``getattr`` resolves to
      the builtin symbol -- but resolving the ``getattr`` call is NOT resolving
      the attribute it reaches at runtime. So a SCIP occurrence on a blind-spot
      line is disregarded, and every enumerated blind spot stays in the residue
      unconditionally, carrying its kind and reason.

    * **untyped-receiver calls** SCIP emitted no occurrence for at all
      (``session.add(job)`` on an untyped ``session``). These are recovered by
      subtracting the sites SCIP resolved from the full call census -- the silent
      gap the map module's docstring names as the reason the AST differ exists.
    """
    blind = _enumerate_blind_spots(source_root, language)
    blind_by_key = {(b["file"], b["line"]): b for b in blind}
    census = []
    for site in _enumerate_call_sites(source_root, language):
        spot = blind_by_key.get((site["file"], site["line"]))
        census.append({**site, "kind": spot["kind"], "reason": spot["reason"]} if spot else site)
    resolved = [
        site
        for site in resolved_sites(edges)
        if (site["file"], site["line"]) not in blind_by_key
    ]
    return blindspots.blind_spot_residue(census, resolved)


def _scip_summary(
    edges: list[dict], scip_calls: dict[str, set[str]], census: list, residue: list
) -> dict:
    return {
        "scip_resolved_edges": len(edges),
        "scip_rooted_edges": sum(len(v) for v in scip_calls.values()),
        "ast_call_sites": len(census),
        "unresolved_enumerated": len(residue),
    }


def hybrid_raw(
    ast_raw: dict,
    normalized: dict,
    source_root: str | Path,
    *,
    language: str = "python",
    deep: bool = False,
) -> dict:
    """Fold a normalized SCIP index into the adapter's raw discover output.

    Returns a copy of ``ast_raw`` with ``_calls`` replaced by the SCIP-resolved
    graph (translated into the adapter's node namespace by the ``language``
    normalizer) and the SCIP evidence added: the full resolved edge list, the
    SCIP-side type definitions, and the enumerated residue -- the call sites the
    AST pass saw that SCIP left unresolved. Everything else the adapter produced
    (entities, surfaces, ``_direct``, ``_ops``, blind spots, AST residue) is
    preserved untouched.

    ``deep`` switches on the (file,line)-join (``_hybrid_deep``) for the deep
    tree-sitter adapter, whose node keys are provisional and canonicalized
    against SCIP's own definition set. The default (``deep=False``,
    ``language="python"``) is the shallow path and is byte-identical to before:
    ``node_keys`` come from the adapter's ``_direct`` / ``_calls`` / handlers.

    Pure over ``(ast_raw, normalized)`` plus one read of the source tree for the
    call-site census, so it is fully exercised by a checked-in fixture with no
    indexer installed.
    """
    edges = scip_map.call_edges(normalized)
    if deep:
        return _hybrid_deep(ast_raw, normalized, edges, source_root, language)

    node_keys = (
        set(ast_raw.get("_direct", {}))
        | set(ast_raw.get("_calls", {}))
        | {s["handler"] for s in ast_raw.get("surfaces", [])}
    )
    scip_calls = calls_graph(edges, is_node=node_keys.__contains__, language=language)
    census = _enumerate_call_sites(source_root, language)
    residue = _residue(source_root, edges, language)

    hybrid = dict(ast_raw)
    hybrid["_calls"] = scip_calls
    hybrid["resolver"] = "scip"
    hybrid["scip_resolved_edges"] = edges
    hybrid["scip_entities"] = scip_map.entities(normalized)
    hybrid["scip_residue"] = residue
    hybrid["scip_residue_summary"] = _scip_summary(edges, scip_calls, census, residue)
    return hybrid


def _rewrite_keys(mapping: dict, rename: dict) -> dict:
    """A copy of ``mapping`` with node keys rewritten through ``rename``."""
    return {rename.get(k, k): v for k, v in (mapping or {}).items()}


def _hybrid_deep(
    ast_raw: dict,
    normalized: dict,
    edges: list[dict],
    source_root: str | Path,
    language: str,
) -> dict:
    """The deep hybrid: canonicalize provisional node keys against SCIP defs.

    The deep tree-sitter adapter (a sibling task) cannot always reproduce an
    indexer's namespace convention byte-for-byte (a go import path is
    build-derived; a php PSR-4 namespace can be remapped away from the
    directory). So it keys its handler / ``_direct`` / ``_ops`` / ``_calls`` nodes
    provisionally and hands over ``_node_locations`` -- ``{provisional node ->
    [file, 1-based line]}`` for each node's DEFINITION site. This joins those
    locations against ``scip_defs_by_location`` (the binding of record) and:

    * rewrites every provisional key to the SCIP node the join names, so the
      adapter's nodes and the SCIP call graph share one namespace;
    * a provisional node whose location has no SCIP definition becomes a NAMED
      ``deep-unresolved`` entry in ``unresolved`` -- never a silent dropped edge
      and never a fabricated one;
    * seeds ``node_keys`` from SCIP's own definition set (union the rewritten
      adapter keys), so a mid-chain function SCIP resolved is a known node and
      its edge survives ``is_node`` (the ``node_keys`` filter would otherwise
      drop it).

    Then ``_calls`` is replaced by the SCIP graph in that shared namespace and
    the SCIP evidence + residue are attached, exactly as the shallow path does.
    """
    defs_by_loc = scip_defs_by_location(normalized, language=language)
    node_locations = ast_raw.get("_node_locations", {}) or {}

    rename: dict[str, str] = {}
    deep_unresolved: list[dict] = []
    for provisional, loc in node_locations.items():
        key = (loc[0], loc[1])
        canonical = defs_by_loc.get(key)
        if canonical is not None:
            rename[provisional] = canonical
        else:
            deep_unresolved.append(
                {
                    "kind": "deep-unresolved",
                    "node": provisional,
                    "file": loc[0],
                    "line": loc[1],
                    "reason": (
                        "no SCIP definition at this location; the node could not "
                        "be joined to the SCIP call graph and its edges are "
                        "reported unresolved rather than dropped"
                    ),
                }
            )

    direct = _rewrite_keys(ast_raw.get("_direct", {}), rename)
    ops = _rewrite_keys(ast_raw.get("_ops", {}), rename)
    surfaces = [
        {**s, "handler": rename.get(s["handler"], s["handler"])}
        for s in ast_raw.get("surfaces", [])
    ]
    node_keys = (
        set(defs_by_loc.values())
        | set(direct)
        | {rename.get(k, k) for k in ast_raw.get("_calls", {})}
        | {s["handler"] for s in surfaces}
    )
    scip_calls = calls_graph(edges, is_node=node_keys.__contains__, language=language)
    census = _enumerate_call_sites(source_root, language)
    residue = _residue(source_root, edges, language)

    hybrid = dict(ast_raw)
    hybrid["_direct"] = direct
    hybrid["_ops"] = ops
    hybrid["surfaces"] = surfaces
    hybrid["_calls"] = scip_calls
    hybrid["resolver"] = "scip"
    hybrid["deep"] = True
    hybrid["scip_resolved_edges"] = edges
    hybrid["scip_entities"] = scip_map.entities(normalized)
    hybrid["scip_residue"] = residue
    hybrid["scip_residue_summary"] = _scip_summary(edges, scip_calls, census, residue)
    hybrid["unresolved"] = list(ast_raw.get("unresolved", []) or []) + deep_unresolved
    return hybrid


class ScipToolsUnavailable(RuntimeError):
    """A SCIP indexer or the ``scip`` CLI needed by ``resolve`` is not present."""


def tools_available(language: str) -> bool:
    """True when both the indexer for ``language`` and the ``scip`` CLI resolve.

    Use to gate the live path (a test decorator, a caller deciding whether to ask
    for ``--resolver scip``). Never runs a tool. For php the ``php`` executable on
    PATH is necessary but not sufficient -- the standalone scip-php script must
    also be present -- so both are probed, per the per-language invocation
    strategy (a plain ``which(exe)`` would report php available with no indexer).
    """
    executable = runner._INDEXERS.get(language, (None,))[0]
    if executable is None or shutil.which(executable) is None:
        return False
    if language == "php" and not Path(runner._scip_php_bin()).is_file():
        return False
    try:
        runner._locate_scip_cli()
    except runner.ScipCliNotFound:
        return False
    return True


def resolve(
    source_root: str | Path,
    ast_raw: dict,
    *,
    language: str = "python",
    deep: bool = False,
    timeout: int = 600,
) -> dict:
    """Index ``source_root`` with SCIP and return the hybrid raw discover output.

    This is the one impure entry point: it shells out to a SCIP indexer and the
    ``scip`` CLI. It raises ``ScipToolsUnavailable`` -- naming the missing tool
    and how to install it -- rather than degrading, because a caller that asked
    for ``--resolver scip`` wants the SCIP graph or a clear reason it could not be
    built, not a quietly hand-rolled one wearing the SCIP label. The transient
    ``index.scip`` the indexer writes into the tree is removed before returning.
    ``deep`` forwards to the (file,line)-join variant for the deep adapter.
    """
    if language not in runner._INDEXERS:
        raise ValueError(
            f"unsupported SCIP language {language!r}; "
            f"expected one of {sorted(runner._INDEXERS)}"
        )
    executable, install_hint = runner._INDEXERS[language]
    if shutil.which(executable) is None:
        raise ScipToolsUnavailable(
            f"--resolver scip needs the {language} indexer {executable!r}, which "
            f"is not on PATH. Install it with: {install_hint}"
        )
    if language == "php" and not Path(runner._scip_php_bin()).is_file():
        raise ScipToolsUnavailable(
            f"--resolver scip needs the scip-php script at "
            f"{runner._scip_php_bin()!r} (set {runner._SCIP_PHP_BIN_ENV}). "
            f"Install it with: {install_hint}"
        )
    try:
        runner._locate_scip_cli()
    except runner.ScipCliNotFound as exc:
        raise ScipToolsUnavailable(str(exc)) from exc

    source_root = Path(source_root)
    index_path = runner.run_scip_index(source_root, language, timeout=timeout)
    try:
        # retain=True hashes index.scip while it still exists: the digest is the
        # index identity every static fact is keyed by (section 29), and it must
        # be read before the transient index is removed below.
        normalized = runner.read_scip_index(index_path, retain=True)
    finally:
        index_path.unlink(missing_ok=True)
    hybrid = hybrid_raw(ast_raw, normalized, source_root, language=language, deep=deep)
    hybrid["scip_index_digest"] = normalized.get("index_digest")
    hybrid["scip_index_digest_kind"] = normalized.get("index_digest_kind")
    return hybrid
