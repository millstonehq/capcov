"""treesitter-routes as a CORE adapter: routes in any tree-sitter language.

This is a thin wrapper. The discovery engine lives in `flows.discovery`
(`_discover_from_config`) and stays the shared implementation the flows CLI also
uses; here it is fed a config assembled from a `[[adapters]]` entry in
capcov.toml and its obligation inventory is projected onto the CORE adapter
contract by `adapters.build_core_dict`.

The route opinion -- WHICH calls are routes -- is the config-supplied tree-sitter
query, so one adapter serves Go, JavaScript, Python and the rest. A `methods`
allowlist drops off-verb candidates into `excluded_surfaces` (visible, not
silent); a non-literal path becomes a `dynamic-route` under `unresolved`; the
named limits of static route reading become `boundary` entries under
`unresolved`. Branch/exception candidates the engine also finds are behavioural
obligations for the `outcomes` lane and are not projected here.

Needs the optional `treesitter` extra (tree-sitter + tree-sitter-language-pack),
imported lazily by the engine, so `import capcov.adapters.treesitter_routes`
stays stdlib-only.
"""

from __future__ import annotations

from pathlib import Path

from .. import artifacts
from . import build_core_dict, project_flows_unresolved
from ..flows import discovery as flows_discovery

NAME = "treesitter-routes"

# No single SCIP indexer language: the language is per-config (go, javascript,
# python, ...). The `--resolver scip` path is meaningless for a route reader
# (there is no call graph to re-source), so this stays None.
LANGUAGE = None

# Per-adapter keys copied through to the shared engine's treesitter-routes config.
_ENGINE_KEYS = (
    "methods",
    "globs",
    "files",
    "id_prefix",
    "mount",
    "strip_suffixes",
    "branch_nodes",
    "exception_nodes",
)


def _adapter_config(target: Path, config: dict | None) -> dict:
    """The per-adapter config: the entry T2 passes, or the matching capcov.toml one.

    When `config` is given (the multi-adapter merge path) it is used verbatim.
    Otherwise capcov.toml is read and the first `[[adapters]]` entry named
    `treesitter-routes` is used, falling back to the `[capcov]` block -- enough
    for a direct single-adapter invocation and for tests.
    """
    if config is not None:
        return config
    import tomllib

    path = target / "capcov.toml"
    data = tomllib.loads(path.read_text()) if path.exists() else {}
    for entry in data.get("adapters", []):
        if entry.get("name") == NAME:
            return entry
    return data.get("capcov", {})


# Deep-query keys copied through to the engine so it emits the extra capture
# streams. Only present in a `deep` config block; absent -> the engine's inventory
# is byte-identical and the shallow self-bound builder runs.
_DEEP_ENGINE_KEYS = ("function_query", "entity_query", "op_query")


def discover(
    source_root: Path, target: Path, name_match: bool = True, *, config: dict | None = None
) -> dict:
    config = _adapter_config(Path(target), config)
    if "language" not in config or "query" not in config:
        raise ValueError(
            "treesitter-routes needs 'language' and 'query' in its [[adapters]] entry"
        )
    # A language-only route config must analyze its language tree.  Leaving the
    # glob empty made discovery fail closed in the engine while the CLI's
    # provenance path appeared to hash a non-empty tree.  Keep explicit globs or
    # files authoritative; otherwise use the safe per-language default.
    if not config.get("globs") and not config.get("files"):
        config = {
            **config,
            "globs": [artifacts.language_pattern(config.get("language"))],
        }
    deep = config.get("deep")
    # The deep decision is made BEFORE the engine runs: whether tooling is present
    # settles whether the deep queries are even forwarded (state 3) or the run is
    # the plain shallow one plus a named degrade (state 2). scip_resolve pulls in
    # only stdlib (runner/map/blindspots), so importing it here keeps the module's
    # import-time stdlib-only guarantee.
    go_deep = False
    scip_language = None
    if deep:
        from ..scip import resolve as scip_resolve

        scip_language = config.get("scip_language") or config["language"]
        go_deep = scip_resolve.tools_available(scip_language)

    entry: dict = {
        "kind": "treesitter-routes",
        "language": config["language"],
        "query": config["query"],
    }
    for key in _ENGINE_KEYS:
        if key in config:
            entry[key] = config[key]
    if go_deep:
        # Forward the deep tree-sitter queries so the engine also emits the
        # function universe, entity declarations and data-access sites. Skipped on
        # the degrade path -- a shallow run has no use for them.
        for key in _DEEP_ENGINE_KEYS:
            if key in deep:
                entry[key] = deep[key]
    flows_cfg = {"scope": config.get("scope", NAME), "root": ".", "adapters": [entry]}
    inventory = flows_discovery._discover_from_config(
        flows_cfg, Path(source_root), "capcov.toml"
    )

    id_prefix = config.get("id_prefix", "http:")
    records: list[dict] = []
    for obligation in inventory["obligations"]:
        if obligation["kind"] != "surface":
            continue
        oid = obligation["id"]
        method = obligation.get("method")
        path = oid[len(id_prefix):] if oid.startswith(id_prefix) else oid
        if method and path.endswith(f"#{method}"):
            path = path[: -len(method) - 1]
        # The surface identity a runtime probe must reproduce to land in `both`:
        # "http:METHOD path" when the verb is known statically, else the raw id
        # (a verb-less route is a legitimate near-miss, R2, not a match).
        core_id = f"http:{method} {path}" if method else oid
        records.append(
            {
                "id": core_id,
                "method": method,
                "path": path,
                "handler": obligation.get("handler"),
                "file": obligation["source"]["file"],
                "line": obligation["source"]["line"],
                "module": obligation["source"]["file"],
                "tags": obligation.get("tags"),
                "summary": obligation.get("summary"),
            }
        )
    unresolved = project_flows_unresolved(inventory, NAME)

    # Selection rule (design §1.1), three states in this order:
    #
    # 1. No deep block -> the shallow self-bound dict, byte-for-byte as before.
    #    This keeps the 384-test baseline green (plain and --extra treesitter).
    if not deep:
        return build_core_dict(records, inventory["excluded_surfaces"], unresolved)

    # 2. Deep requested but the SCIP tooling for the language is absent -> still
    #    emit the shallow dict, and NAME the degrade in `unresolved`. Never crash,
    #    never silently claim deep; cli.cmd_discover reads this reason and skips the
    #    SCIP fold rather than shelling out to a tool that is not there.
    if not go_deep:
        degrade = {
            "adapter": NAME,
            "kind": "deep-unavailable",
            "reason": (
                f"deep requested but the SCIP tooling for {scip_language!r} is not "
                "available (indexer or the scip CLI not on PATH); fell back to the "
                "shallow self-bound path"
            ),
            "scip_language": scip_language,
        }
        return build_core_dict(
            records, inventory["excluded_surfaces"], [*unresolved, degrade]
        )

    # 3. Deep and tooling present -> the node-keyed deep dict. cli's --resolver
    #    scip hook then folds in the SCIP call graph and the (file,line)-join.
    from . import deep_core

    recognizer = deep_core.load_recognizer(
        deep.get("recognizer")
        or deep_core.DEFAULT_RECOGNIZER.get(scip_language)
        or scip_language
    )
    captures = inventory.get(
        "deep_captures", {"functions": [], "entity_matches": [], "op_matches": []}
    )
    entities, symbol_index = recognizer.recognize_entities(captures["entity_matches"])
    op_sites = recognizer.recognize_ops(captures["op_matches"], symbol_index)
    return deep_core.build_deep_dict(
        surface_records=records,
        functions=captures["functions"],
        entities=entities,
        op_sites=op_sites,
        excluded_surfaces=inventory["excluded_surfaces"],
        unresolved=unresolved,
        blind_spots=_deep_blind_spots(Path(source_root), scip_language),
        scip_language=scip_language,
    )


def _deep_blind_spots(source_root: Path, language: str) -> list[dict]:
    """The per-language blind-spot inventory, shaped for the capabilities artifact.

    The AST/tree-sitter pass STAYS the enumerator even when SCIP resolves (§5,
    ADR-0001): SCIP does not self-report its blind spots. The enumerator's entries
    are `{file, line, kind, reason}`; `cli.cmd_discover` keys on `blind` and
    `expr`, and every entry here is genuinely blind (dynamic dispatch / facade
    magic / untyped access), so `blind` is True and `expr` carries the callee text
    where the enumerator recorded one, else the kind.
    """
    from ..scip import blindspots

    shaped: list[dict] = []
    for spot in blindspots.enumerate_blind_spots(source_root, language):
        shaped.append(
            {
                **spot,
                "expr": spot.get("expr") or spot.get("reason") or spot.get("kind"),
                "blind": True,
            }
        )
    return shaped
