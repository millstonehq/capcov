"""Adapters know about a language and a stack. Nothing below this package does.

An adapter answers four questions and nothing else: where are the entities,
where are the surfaces, where are the entry points, and what calls what. The
core does the fixpoint, the reconciliation and the gate against the artifact an
adapter emits, which is what makes a second stack a day's work rather than a
fork.

Two of the registered adapters read *routes and contracts* rather than a stack:
`treesitter-routes` and `structured-spec` wrap the shared discovery engine
(`flows.discovery`) and project its obligation inventory onto the CORE adapter
contract. The bridge and the multi-adapter merge live here so every adapter
speaks the one shape the fixpoint, reconcile and gate already read.

The two registered adapters are two GENERIC readers, not the only two allowed.
A clean-room rebuild OUT OF a legacy/low-code platform (Zoho Deluge, Salesforce
Apex, COBOL, a Retool/Airtable export) starts from a source with no tree-sitter
grammar and no structured form, so it needs a bespoke reader. Those are brought
in as PLUGINS at the edge: an `[[adapters]]` entry may carry
`plugin = "dotted.module:callable"`, and `load` imports that callable instead of
a registered module (see `capcov/PLUGINS.md` for the callable contract). The core
owns the contract, the two generic readers and this loader; a bespoke reader
never gets special-cased into it.
"""

from __future__ import annotations

from types import ModuleType

from ..artifacts import language_pattern

# Registry entries are dotted-path strings, imported lazily by `load`. Nothing
# here imports cli, a probe, or the browser seam at module load -- the whole
# point of the strings is that adding a route reader does not drag the runtime
# side into every `import capcov.adapters`.
REGISTRY = {
    "python-fastapi-sqlalchemy": "capcov.adapters.python_fastapi_sqlalchemy",
    "treesitter-routes": "capcov.adapters.treesitter_routes",
    "structured-spec": "capcov.adapters.structured_spec",
}

# The source a spec-less adapter reads, when its `[[adapters]]` entry declares no
# `globs`. The stack adapter reads its language's files; the config-driven
# route/contract adapters read exactly what their entry names, so they contribute
# nothing of their own. An adapter absent from this table falls back to the Python
# default rather than to nothing -- an unrecognised adapter must not silently
# shrink the hashed tree to zero files.
DEFAULT_SOURCE_PATTERNS = ("**/*.py",)
ADAPTER_SOURCE_PATTERNS: dict[str, tuple[str, ...]] = {
    "python-fastapi-sqlalchemy": DEFAULT_SOURCE_PATTERNS,
    "treesitter-routes": (),
    "structured-spec": (),
}


def source_patterns(specs: list[tuple[str, dict | None]]) -> tuple[str, ...]:
    """The glob set that backs an artifact's provenance hash, for these specs.

    Resolved PER SPEC and unioned. Taking the union of only the declared `globs`
    is what made a mixed config lie: `python-fastapi-sqlalchemy` never declares
    `globs`, so one Go route adapter beside it replaced `**/*.py` outright and a
    Python source edit stopped invalidating capabilities.json. Each spec now
    contributes either what it declared or its own default, so every adapter's
    source is in the hash.

    Discover and the browser probe MUST agree here: `reconcile` refuses a static
    and a runtime artifact whose `artifact_sha256` disagree, so a second copy of
    this rule is a broken pipeline waiting to happen. There is one.
    """
    patterns: list[str] = []
    for name, config in specs:
        config = config or {}
        declared = list(config.get("globs") or [])
        if not declared and name == "treesitter-routes":
            declared = list(config.get("files") or [])
            if not declared:
                declared = [
                    language_pattern(
                        config.get("language") or config.get("scip_language")
                    )
                ]
        if not declared and name == "structured-spec" and config.get("document"):
            declared = [config["document"]]
        patterns.extend(
            declared or ADAPTER_SOURCE_PATTERNS.get(name, DEFAULT_SOURCE_PATTERNS)
        )
    return tuple(dict.fromkeys(patterns)) or DEFAULT_SOURCE_PATTERNS


def import_plugin_callable(dotted: str):
    """Import the `module:callable` a plugin adapter entry declares.

    The single resolver both dispatch paths (`load` here, and
    `flows.discovery._discover_from_config`) use to turn a
    `plugin = "dotted.module:callable"` string into the callable that reads a
    source no built-in adapter can. Raises ValueError with a specific reason on a
    malformed string, a missing module attribute, or a non-callable target.
    """
    import importlib

    module_path, sep, attr = dotted.partition(":")
    if not sep or not module_path or not attr:
        raise ValueError(
            f"plugin {dotted!r} must be 'dotted.module:callable' "
            "(a single colon separates the import path from the callable name)"
        )
    module = importlib.import_module(module_path)
    try:
        target = getattr(module, attr)
    except AttributeError as exc:
        raise ValueError(
            f"plugin {dotted!r}: module {module_path!r} has no attribute {attr!r}"
        ) from exc
    if not callable(target):
        raise ValueError(f"plugin {dotted!r}: {attr!r} is not callable")
    return target


def _plugin_adapter(name: str, dotted: str) -> ModuleType:
    """A module-shaped shim so a plugin reads through the same call + merge path.

    `cli._run_adapter` reads `.discover` (and forwards the `[[adapters]]` entry as
    `config`); `cli.cmd_discover` reads `.LANGUAGE`. The shim exposes both so a
    plugin's core dict is produced and merged EXACTLY as a built-in adapter's is.
    The plugin callable's contract is `(source_root, target, config) -> core dict`
    (see `capcov/PLUGINS.md`); `config` is the entry carrying its `plugin` string.
    """
    fn = import_plugin_callable(dotted)
    shim = ModuleType(f"capcov.adapters._plugins.{name}")

    def discover(source_root, target, name_match: bool = True, *, config=None):
        result = fn(source_root, target, config)
        if not isinstance(result, dict):
            raise SystemExit(
                f"plugin adapter {dotted!r} must return a core adapter dict "
                "(as capcov.adapters.build_core_dict produces)"
            )
        return result

    shim.discover = discover  # type: ignore[attr-defined]
    shim.NAME = name  # type: ignore[attr-defined]
    shim.LANGUAGE = None  # type: ignore[attr-defined]
    shim.PLUGIN = dotted  # type: ignore[attr-defined]
    return shim


def load(name: str, *, plugin: str | None = None) -> ModuleType:
    import importlib

    if plugin is not None:
        # The seam: `name` is a free label; the callable is the reader. Merged
        # by `merge` below exactly like a built-in, so its excluded_surfaces and
        # unresolved carriers reach the honest denominator unchanged.
        try:
            return _plugin_adapter(name, plugin)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    if name not in REGISTRY:
        raise SystemExit(
            f"unknown adapter {name!r}; built-ins are: {', '.join(sorted(REGISTRY))}; "
            'for a source with no built-in reader, declare plugin = "module:callable" '
            "in the [[adapters]] entry to bring your own reader"
        )
    return importlib.import_module(REGISTRY[name])


# --------------------------------------------------------------------------
# The obligation -> entity/surface bridge (route/contract adapters).
#
# The route world has surfaces, never DB tables; the core reconcile is
# entity-centric. The core adapter interface note settles it: "entity is any
# obligation identity." So each surface obligation becomes a core ENTITY and a
# core SURFACE bound to itself at hop 0 -- the fixpoint then emits exactly one
# capability (entity=id, surface=id, operations=[verb->CRUD], evidence
# hops:0/kind:"direct") per route, and the four cells read for a route target as
# both / static_only (coverage gap) / runtime_only (undiscovered surface).

# HTTP verb -> CRUD. Weak but declared (R2): the browser has no faithful source
# for per-operation coverage, so this is the honest static claim, no more. Verbs
# outside the set (HEAD, OPTIONS, a route with no method) claim no operation
# rather than guess one.
_VERB_TO_CRUD = {
    "GET": "read",
    "POST": "create",
    "PUT": "update",
    "PATCH": "update",
    "DELETE": "delete",
}

_ZERO_RESIDUE_SUMMARY = {
    "resolved_by_import": 0,
    "resolved_by_name": 0,
    "ambiguous": 0,
    "external": 0,
    "chained": 0,
    "builtin_shadowed": 0,
}


def build_core_dict(
    surface_records: list[dict],
    excluded_surfaces: dict,
    unresolved: list[dict],
) -> dict:
    """Assemble a CORE adapter dict from normalized surface records.

    Each record is ``{id, method, path, handler, file, line, module}`` plus the
    optional ``tags`` and ``summary`` a declarative reader may carry, where
    ``id`` is the ``"http:METHOD path"`` surface identity (the string a runtime
    probe must reproduce to land in `both`). The record's ``id`` is used as the
    fixpoint root key -- surface ids are unique, so a handler function serving
    two routes never cross-binds one route's entity onto the other's surface.

    `blind_spots`, `residue` and `residue_summary` are emitted REQUIRED-but-empty
    because `cli.cmd_discover` reads them unconditionally; a route reader has no
    call graph and therefore no residue. `excluded_surfaces` and `unresolved` are
    first-class top-level keys carrying the honest denominator (Property 3):
    verb-allowlist drops and the named limits of static reading, never dropped
    silently.
    """
    entities: list[dict] = []
    surfaces: list[dict] = []
    direct: dict[str, set[str]] = {}
    ops: dict[str, dict[str, set[str]]] = {}
    for record in surface_records:
        sid = record["id"]
        root = sid  # unique node id: one route, one entity, no cross-binding.
        entities.append(
            {
                "name": sid,
                "symbol": None,
                "module": record.get("module"),
                "file": record.get("file"),
                "line": record.get("line"),
            }
        )
        surface = {
            "id": sid,
            "kind": "http",
            "method": record.get("method"),
            "path": record.get("path"),
            "handler": root,
            # The source-level handler name when the query captured one; the
            # node id above is what the fixpoint keys on.
            "handler_symbol": record.get("handler"),
            "file": record.get("file"),
            "line": record.get("line"),
            "mounted": True,
        }
        for key in ("tags", "summary"):
            if record.get(key):
                surface[key] = record[key]
        surfaces.append(surface)
        direct[root] = {sid}
        crud = _VERB_TO_CRUD.get((record.get("method") or "").upper())
        if crud:
            ops[root] = {sid: {crud}}
    entities.sort(key=lambda e: e["name"])
    surfaces.sort(key=lambda s: (s["path"] or "", s["method"] or "", s["id"]))
    return {
        "entities": entities,
        "surfaces": surfaces,
        "_direct": direct,
        "_calls": {},
        "_ops": ops,
        "_evidence": {},
        "residue": [],
        "residue_summary": dict(_ZERO_RESIDUE_SUMMARY),
        "blind_spots": [],
        "excluded_surfaces": excluded_surfaces,
        "unresolved": unresolved,
    }


def project_flows_unresolved(inventory: dict, adapter_name: str) -> list[dict]:
    """The flows inventory's honest limits, projected to `{adapter, kind, reason}`.

    Two kinds of "could not resolve" reach the surface denominator (Property 3),
    each named, never dropped:

    * adapters that ran and produced no surface at all (`inventory["unresolved"]`);
    * `kind == "unresolved"` obligations -- a non-literal (dynamic) route path,
      and the boundary obligations that spell out what static route/spec reading
      cannot confirm (`boundary:*`).

    `kind` classifies each for the gate (T4): ``no-surfaces``, ``dynamic-route``,
    ``boundary``. Branch- and exception-candidate obligations are NOT included --
    they are behavioural obligations at the `outcomes` lane's granularity, not
    surfaces, and folding them into the surface denominator would fail the gate
    for the wrong reason.
    """
    out: list[dict] = []
    for entry in inventory.get("unresolved", []):
        out.append(
            {
                "adapter": adapter_name,
                "kind": "no-surfaces",
                "reason": entry.get("reason"),
                "flows_kind": entry.get("kind"),
            }
        )
    for obligation in inventory.get("obligations", []):
        if obligation.get("kind") != "unresolved":
            continue
        oid = obligation["id"]
        if oid.endswith(":dynamic-route"):
            kind = "dynamic-route"
        elif oid.startswith("boundary:"):
            kind = "boundary"
        else:
            kind = "unresolved"
        out.append(
            {
                "adapter": adapter_name,
                "kind": kind,
                "reason": obligation.get("reason") or oid,
                "id": oid,
                "source": obligation.get("source"),
            }
        )
    return out


# --------------------------------------------------------------------------
# Multi-adapter merge (`[[adapters]]`).
#
# Defined here; invoked by cli.cmd_discover (T2) after it runs each adapter. A
# single-adapter list returns that adapter's dict unchanged, so an existing
# `adapter = "..."` consumer sees byte-identical output.


def _obligation_ids(discovery: dict) -> set[str]:
    ids = {surface["id"] for surface in discovery.get("surfaces", [])}
    ids |= {entity["name"] for entity in discovery.get("entities", [])}
    return ids


def merge(discoveries: list[dict]) -> dict:
    """Merge several core adapter dicts into one, or raise on a shared id.

    The existing `duplicate obligation IDs` invariant, applied at the adapter
    boundary: a surface id or entity name emitted by two adapters is a collision
    the four-cell cannot represent (it keys on entity name), so the merge raises
    rather than silently letting one clobber the other. Qualify the surfaces (a
    per-adapter prefix) to keep them distinct.
    """
    if not discoveries:
        raise ValueError("merge requires at least one adapter discovery")
    if len(discoveries) == 1:
        return discoveries[0]

    seen: set[str] = set()
    for discovery in discoveries:
        ids = _obligation_ids(discovery)
        clash = ids & seen
        if clash:
            raise ValueError(
                "duplicate obligation IDs across adapters: "
                + ", ".join(sorted(clash))
                + "; qualify separate application surfaces"
            )
        seen |= ids

    entities: list[dict] = []
    surfaces: list[dict] = []
    direct: dict[str, set[str]] = {}
    calls: dict[str, set[str]] = {}
    ops: dict[str, dict[str, set[str]]] = {}
    evidence: dict[str, list[dict]] = {}
    blind: list[dict] = []
    residue: list[dict] = []
    residue_summary = dict(_ZERO_RESIDUE_SUMMARY)
    excluded_surfaces: list[dict] = []
    unresolved: list[dict] = []

    for discovery in discoveries:
        entities.extend(discovery.get("entities", []))
        surfaces.extend(discovery.get("surfaces", []))
        for key, values in discovery.get("_direct", {}).items():
            direct.setdefault(key, set()).update(values)
        for key, values in discovery.get("_calls", {}).items():
            calls.setdefault(key, set()).update(values)
        for key, table_ops in discovery.get("_ops", {}).items():
            for table, verbs in table_ops.items():
                ops.setdefault(key, {}).setdefault(table, set()).update(verbs)
        for key, items in discovery.get("_evidence", {}).items():
            evidence.setdefault(key, []).extend(items)
        blind.extend(discovery.get("blind_spots", []))
        residue.extend(discovery.get("residue", []))
        for cell, count in discovery.get("residue_summary", {}).items():
            residue_summary[cell] = residue_summary.get(cell, 0) + count
        excluded = discovery.get("excluded_surfaces") or {"count": 0, "surfaces": []}
        excluded_surfaces.extend(excluded.get("surfaces", []))
        unresolved.extend(discovery.get("unresolved", []))

    entities.sort(key=lambda e: e["name"])
    surfaces.sort(key=lambda s: (s.get("path") or "", s.get("method") or "", s["id"]))
    return {
        "entities": entities,
        "surfaces": surfaces,
        "_direct": direct,
        "_calls": calls,
        "_ops": ops,
        "_evidence": evidence,
        "residue": residue,
        "residue_summary": residue_summary,
        "blind_spots": blind,
        "excluded_surfaces": {
            "count": len(excluded_surfaces),
            "surfaces": excluded_surfaces,
        },
        "unresolved": unresolved,
    }
