"""structured-spec as a CORE adapter: declarative contracts (OpenAPI is a profile).

A thin wrapper, like `treesitter_routes`. The generic declarative-contract engine
lives in `flows.structured_spec` and is driven through `flows.discovery`; OpenAPI
is one profile of it (`profile = "openapi"`, the default), not a bespoke path.
The engine's obligation inventory is projected onto the CORE adapter contract by
`adapters.build_core_dict`.

Each declared operation becomes a `http:METHOD path` surface -- already the id the
engine emits -- so it is a core entity and a self-bound surface at hop 0. The
honest limits a contract cannot confirm on its own (mounted routes, omitted
routes, schemas, servers, references) arrive as `boundary` entries under
`unresolved`; a document that declares no operations produces no surface and is
named there too, so an empty spec has no green denominator.
"""

from __future__ import annotations

from pathlib import Path

from . import build_core_dict, project_flows_unresolved
from ..flows import discovery as flows_discovery

NAME = "structured-spec"

# A contract is language-agnostic; there is no SCIP indexer language.
LANGUAGE = None

# Profile -> the shared engine's discovery-adapter kind. OpenAPI is the one wired
# profile today; a new declarative shape is a new profile, not a new reader.
_PROFILE_KIND = {"openapi": "openapi-json"}


def _adapter_config(target: Path, config: dict | None) -> dict:
    if config is not None:
        return config
    import tomllib

    path = target / "capcov.toml"
    data = tomllib.loads(path.read_text()) if path.exists() else {}
    for entry in data.get("adapters", []):
        if entry.get("name") == NAME:
            return entry
    return data.get("capcov", {})


def discover(
    source_root: Path, target: Path, name_match: bool = True, *, config: dict | None = None
) -> dict:
    config = _adapter_config(Path(target), config)
    profile = config.get("profile", "openapi")
    if profile not in _PROFILE_KIND:
        raise ValueError(
            f"structured-spec: unknown profile {profile!r}; "
            f"have: {', '.join(sorted(_PROFILE_KIND))}"
        )
    if "document" not in config:
        raise ValueError("structured-spec needs a 'document' path in its [[adapters]] entry")
    entry = {
        "kind": _PROFILE_KIND[profile],
        "document": config["document"],
        "prefix": config.get("prefix", ""),
    }
    flows_cfg = {"scope": config.get("scope", NAME), "root": ".", "adapters": [entry]}
    inventory = flows_discovery._discover_from_config(
        flows_cfg, Path(source_root), "capcov.toml"
    )

    records: list[dict] = []
    for obligation in inventory["obligations"]:
        if obligation["kind"] != "surface":
            continue
        core_id = obligation["id"]  # already "http:METHOD path"
        method, _, path = core_id[len("http:"):].partition(" ")
        records.append(
            {
                "id": core_id,
                "method": method,
                "path": path,
                "handler": None,
                "file": obligation["source"]["file"],
                "line": obligation["source"].get("line", 1),
                "module": obligation.get("declaration", profile),
                "tags": obligation.get("tags"),
                "summary": obligation.get("summary"),
            }
        )
    return build_core_dict(
        records,
        inventory["excluded_surfaces"],
        project_flows_unresolved(inventory, NAME),
    )
