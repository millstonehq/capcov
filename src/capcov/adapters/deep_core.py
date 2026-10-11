"""The DEEP core-dict builder: a route handler traced to the DATA it touches.

The shallow builder (`adapters.build_core_dict`) self-binds every route at hop 0:
`entity == surface == id`, `_calls == {}`. A capability then asserts only that a
route EXISTS. The deep builder stops self-binding and emits the **node-keyed
shape the native stack adapter emits** (`python_fastapi_sqlalchemy`), so the
existing `scip.resolve` + `core.fixpoint` walk handler -> entity untouched: the
handler becomes a call-graph NODE (the fixpoint root), every function tree-sitter
found becomes a `_calls` node (the function universe), and the data-access sites
become node-keyed `_direct`/`_ops`. A capability then asserts *"GET /jobs/{id}
READS Job, WRITES AuditLog"* with a replayable chain.

Node identity is **provisional** here and canonicalized later by the resolver's
(file,line)-join (`scip.resolve._hybrid_deep`): the deep tree-sitter adapter
cannot always reproduce an indexer's namespace convention byte-for-byte (a Go
import path is build-derived; a PHP PSR-4 namespace can be remapped away from the
directory), so each node is keyed provisionally and tagged with its DEFINITION's
`(file, 1-based line)` in `_node_locations`. SCIP owns the spelling at each
definition; the join rewrites every provisional key to the SCIP node at the same
location and names any node with no SCIP definition as a `deep-unresolved` entry
-- never a silent dropped edge, never a fabricated one.

This is a SIBLING of `adapters.build_core_dict`; `adapters/__init__.py` is not
edited, so the shallow self-bound path stays byte-stable and is the fallback when
no deep config block is present.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

# The six-cell residue summary `cli.cmd_discover` reads unconditionally. The deep
# tree-sitter path has no AST import/name-match pass of its own -- resolution is
# SCIP's, reported in `scip_residue_summary` -- so this stays all-zero-but-present
# exactly as the shallow promoted adapters emit it. Kept local rather than
# imported from `adapters/__init__` so that module stays uncontended.
_ZERO_RESIDUE_SUMMARY = {
    "resolved_by_import": 0,
    "resolved_by_name": 0,
    "ambiguous": 0,
    "external": 0,
    "chained": 0,
    "builtin_shadowed": 0,
}


# --------------------------------------------------------------------------
# The recognizer contract (design §3.1), implemented per framework by T4.
#
# A capture stream is what the engine's per-framework tree-sitter query produced:
# a list of matches, each match a `{capture_name: [occurrence, ...]}` map where an
# occurrence is `{"text", "file", "line", "start_line", "end_line", "type"}` (all
# lines 1-based, `text` the node's identifier/type text -- never a stored source
# expression). These are transient (consumed here, never written to an artifact).

Capture = dict[str, list[dict]]
EntityDecl = dict          # {name, symbol, module, file, line}
SymbolIndex = dict         # {reference_key -> canonical_entity_name}
OpSite = dict              # {file, line, entity, crud}


@runtime_checkable
class Recognizer(Protocol):
    """Per-framework entity/operation recognition (design §3.1).

    A recognizer is PURELY SEMANTIC. It answers the two framework questions the
    native python oracle answers (`python_fastapi_sqlalchemy.discover_entities`,
    generalized): *is this declaration a persistence entity, and what is its
    canonical identity?* and *what read/write operation does this data-access site
    perform, on which entity?* It never mints call-graph node ids and never
    resolves the call graph -- node identity + enclosing-function attribution are
    the deep builder's job (below); the call graph is SCIP's. This division is why
    a second stack's recognizer is a small file, not a fork of the engine.

    Implementations live in `capcov.adapters.recognizers.<name>` (task T4) and
    expose a module-level ``RECOGNIZER`` instance satisfying this protocol.
    ``load_recognizer(name)`` resolves one.
    """

    #: The SCIP indexer language this recognizer's framework compiles to
    #: ("go" / "php" / "python"). Advisory; the caller already knows the language.
    language: str

    def recognize_entities(
        self, entity_matches: list[Capture]
    ) -> tuple[list[EntityDecl], SymbolIndex]:
        """`(entities, symbol_index)` from the `entity_query` capture stream.

        ``entities``: ``[{name, symbol, module, file, line}]`` where ``name`` is
        the CANONICAL identity the four-cell keys on (a table / collection --
        NOT necessarily the class or struct name, e.g. the gorm snake_case plural
        or an Eloquent ``$table``); ``symbol`` retains the source type name.
        ``symbol_index``: ``{reference_key -> canonical_name}`` mapping every
        code-level reference the recognizer can bind (a typed property/param/return,
        a ``use`` import, a ``::class`` const, a construction) to the entity
        identity, so a data-access site's target reference resolves to the entity.
        """
        ...

    def recognize_ops(
        self, op_matches: list[Capture], symbol_index: SymbolIndex
    ) -> list[OpSite]:
        """`[{file, line, entity, crud}]` from the `op_query` stream + the index.

        Each ``OpSite`` names a data-access site (``file``, 1-based ``line``), the
        canonical ``entity`` it touches (resolved through ``symbol_index``), and
        the ``crud`` verb recognized AT THE SITE (``read``/``create``/``update``/
        ``delete`` -- from the gorm/Eloquent method, never the HTTP verb). A site
        whose target does not resolve to a known entity is OMITTED, not guessed:
        an untyped/dynamic access that ought to be flagged is the blind-spot
        enumerator's job (soundiness), never a fabricated binding.

        (This adapts §3.1's literal ``-> {node -> {entity: {crud}}}``: node
        identity + which enclosing function a site belongs to are the builder's
        concern, so the recognizer returns site-keyed ops and the builder
        attributes each to its enclosing call-graph node.)
        """
        ...


#: scip_language -> the default recognizer module name in
#: `capcov.adapters.recognizers` (task T4). An explicit `deep.recognizer` in
#: capcov.toml overrides it.
DEFAULT_RECOGNIZER = {
    "go": "go_gorm",
    "php": "php_eloquent",
    "python": "python_sqlalchemy",
}


def load_recognizer(name: str) -> Recognizer:
    """The `Recognizer` named `name`, from `capcov.adapters.recognizers.<name>`.

    Raises ``ValueError`` -- naming the missing recognizer and where it ships --
    rather than degrading, so a deep run against an unimplemented framework fails
    loudly instead of silently emitting zero deep capabilities.
    """
    import importlib

    try:
        module = importlib.import_module(f"capcov.adapters.recognizers.{name}")
    except ImportError as exc:
        raise ValueError(
            f"no entity/op recognizer {name!r}; recognizers ship in "
            f"capcov.adapters.recognizers (task T4). Underlying import error: {exc}"
        ) from exc
    recognizer = getattr(module, "RECOGNIZER", None)
    if recognizer is None:
        raise ValueError(
            f"recognizer module {name!r} defines no module-level RECOGNIZER instance"
        )
    return recognizer


# --------------------------------------------------------------------------
# Node identity (provisional; canonicalized by the (file,line)-join).


def node_key(qualname: str, file: str, line: int) -> str:
    """The provisional call-graph node id for a function DEFINITION.

    A provisional-namespace string (`ts:` prefix) distinct from the SCIP canonical
    `package:qualname` it is rewritten to by the join. Keyed by `(file, line)` --
    unique, because a definition has exactly one location -- with the qualname
    carried for legibility only. The join reads `_node_locations[node] = [file,
    line]`, so the string itself is internal: it never reaches an artifact.
    """
    return f"ts:{qualname}@{file}:{line}"


def _match_handler(record: dict, by_name: dict[str, list[tuple[str, str]]]) -> str | None:
    """The call-graph node of a route's handler, matched from the census by name.

    A route captures the handler NAME (`GetJob`, a top-level func, or a decorated
    `def` the function census also saw); the census owns the DEFINITION location
    the node is keyed on. Prefer a same-file definition (a route sits near its
    handler); accept a unique cross-file match; a name with several candidates is
    left unbound (the caller names it) rather than cross-binding the wrong body.
    """
    name = record.get("handler")
    if not name:
        return None
    leaf = name.rsplit(".", 1)[-1]
    candidates = by_name.get(leaf, [])
    same_file = [node for file, node in candidates if file == record.get("file")]
    if len(same_file) == 1:
        return same_file[0]
    if len(candidates) == 1:
        return candidates[0][1]
    return None


def _enclosing_node(
    file: str, line: int, spans_by_file: dict[str, list[tuple[int, int, str]]]
) -> str | None:
    """The innermost census function whose span contains `(file, line)`, or None.

    A data-access site is attributed to the function it sits in -- the tightest
    enclosing definition (latest start wins), mirroring `map._innermost_caller`.
    A site outside every function body (module scope) has no call-graph node to
    root a chain and is reported unattributed rather than dropped.
    """
    best: tuple[int, str] | None = None
    for start, end, node in spans_by_file.get(file, []):
        if start <= line <= end and (best is None or start > best[0]):
            best = (start, node)
    return best[1] if best is not None else None


# --------------------------------------------------------------------------
# The deep builder.


def build_deep_dict(
    *,
    surface_records: list[dict],
    functions: list[dict],
    entities: list[EntityDecl],
    op_sites: list[OpSite],
    excluded_surfaces: dict,
    unresolved: list[dict],
    blind_spots: list[dict] | None = None,
    scip_language: str | None = None,
) -> dict:
    """Assemble the node-keyed DEEP core dict (design §1.2).

    Inputs are the four capture streams the engine's deep queries produced plus
    the recognizer's semantic output:

    * ``surface_records`` -- ``{id, method, path, handler, file, line, module}``
      per route, ``handler`` the source-level handler NAME;
    * ``functions`` -- the function-universe census, ``{qualname, file, line,
      start_line, end_line}`` per func/method DEFINITION (``line`` the name's line,
      to match SCIP's definition occurrence for the join);
    * ``entities`` / ``op_sites`` -- from ``recognizer.recognize_entities`` /
      ``recognize_ops``.

    The dict it returns is provisional: `surfaces[].handler`, `_direct`, `_ops`
    and `_calls` are keyed by `node_key(...)` and every such node carries its
    definition `(file, line)` in `_node_locations`, so `scip.resolve.hybrid_raw(...,
    deep=True)` canonicalizes them against SCIP's own definitions. Two node-universe
    invariants (both silent-failure traps): (1) `surfaces[].handler` is the
    handler's call-graph node, not the surface id, because the fixpoint roots on
    it; (2) EVERY function is a `_calls` key, so `hybrid_raw`'s `node_keys` does not
    drop a mid-chain edge (R8).
    """
    unresolved = list(unresolved)
    node_locations: dict[str, list] = {}
    calls: dict[str, set[str]] = {}
    direct: dict[str, set[str]] = {}
    ops: dict[str, dict[str, set[str]]] = {}
    by_name: dict[str, list[tuple[str, str]]] = {}
    spans_by_file: dict[str, list[tuple[int, int, str]]] = {}

    # The function universe: a node (and a _calls key -- requirement 2) for every
    # function/method the census found. _calls values are placeholders; the SCIP
    # graph replaces them in hybrid_raw, so only the KEYS are load-bearing here.
    for fn in functions:
        node = node_key(fn["qualname"], fn["file"], fn["line"])
        node_locations[node] = [fn["file"], fn["line"]]
        calls.setdefault(node, set())
        direct.setdefault(node, set())
        by_name.setdefault(fn["qualname"].rsplit(".", 1)[-1], []).append(
            (fn["file"], node)
        )
        spans_by_file.setdefault(fn["file"], []).append(
            (fn.get("start_line", fn["line"]), fn.get("end_line", fn["line"]), node)
        )

    # Handlers: the surface's handler is the handler's call-graph NODE (fixpoint
    # root), never the surface id. Reported as `surface["id"]`; rooted on
    # `surface["handler"]` -- the core already separates the two.
    surfaces: list[dict] = []
    for record in surface_records:
        handler_node = _match_handler(record, by_name)
        if handler_node is None:
            # No census function matched the handler name. Mint a node at the route
            # location so the surface still has a root, and name why it will not
            # bind -- never a silently handler-less surface.
            handler_node = node_key(
                record.get("handler") or record["id"], record["file"], record["line"]
            )
            node_locations.setdefault(handler_node, [record["file"], record["line"]])
            calls.setdefault(handler_node, set())
            direct.setdefault(handler_node, set())
            unresolved.append(
                {
                    "kind": "deep-handler-unresolved",
                    "reason": (
                        "route handler matched no function in the census; the "
                        "surface's call chain could not be rooted at a definition"
                    ),
                    "id": record["id"],
                    "handler": record.get("handler"),
                    "source": {"file": record["file"], "line": record["line"]},
                }
            )
        surface = {
            "id": record["id"],
            "kind": "http",
            "method": record.get("method"),
            "path": record.get("path"),
            "handler": handler_node,
            "handler_symbol": record.get("handler"),
            "file": record.get("file"),
            "line": record.get("line"),
            "mounted": True,
        }
        # A plugin reader that carries the document's grouping is not stripped of
        # it on the deep path.
        for key in ("tags", "summary"):
            if record.get(key):
                surface[key] = record[key]
        surfaces.append(surface)

    # Data-access sites: attributed to the enclosing function node. `_direct` binds
    # the entity to that node (hop-0 touch); `_ops` records the CRUD verb the
    # recognizer read at the site.
    for site in op_sites:
        node = _enclosing_node(site["file"], site["line"], spans_by_file)
        if node is None:
            unresolved.append(
                {
                    "kind": "deep-op-unattributed",
                    "reason": (
                        "data-access site is outside every function body (module "
                        "scope); no call-graph node roots it"
                    ),
                    "entity": site.get("entity"),
                    "source": {"file": site["file"], "line": site["line"]},
                }
            )
            continue
        entity = site["entity"]
        direct.setdefault(node, set()).add(entity)
        crud = site.get("crud")
        if crud:
            ops.setdefault(node, {}).setdefault(entity, set()).add(crud)

    result: dict = {
        "entities": sorted(entities, key=lambda e: e["name"]),
        "surfaces": sorted(
            surfaces, key=lambda s: (s["path"] or "", s["method"] or "", s["id"])
        ),
        "_direct": direct,
        "_calls": calls,
        "_ops": ops,
        "_evidence": {},
        # The (file,line)-join's provisional-node -> [file, 1-based line] map. The
        # resolver reads it and never writes it to an artifact.
        "_node_locations": node_locations,
        "residue": [],
        "residue_summary": dict(_ZERO_RESIDUE_SUMMARY),
        # The AST/tree-sitter blind-spot inventory STAYS the enumerator even when
        # SCIP resolves (soundiness; ADR-0001): SCIP does not self-report its
        # blind spots, so they are enumerated here and survive to the artifact.
        "blind_spots": list(blind_spots or []),
        "excluded_surfaces": excluded_surfaces,
        "unresolved": unresolved,
    }
    if scip_language is not None:
        # Read by cli.cmd_discover to pick the SCIP indexer when the adapter was
        # invoked via --adapter (no [[adapters]] config in the CLI's hand). Never
        # copied into the artifact.
        result["scip_language"] = scip_language
    return result
