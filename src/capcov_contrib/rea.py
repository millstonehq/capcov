"""Read saved REA Evidence through capcov's optional plugin seam.

No target execution, MCP setup, or raw payload copying. Record identifiers are
producer claims; the input file hash binds the exact imported bytes. Use REA's
own evidence-import command for semantic authentication before importing.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from capcov.adapters import build_core_dict

NAME = "rea"
_NODE_KINDS = {
    "javascript-module",
    "source-module",
    "context-bridge-api",
    "ipc-channel",
    "ipc-handler",
    "endpoint",
    "native-export",
    "managed-method",
}
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_EVIDENCE_ID = re.compile(r"ev_[a-f0-9]{64}\Z")


class ReaImportError(ValueError):
    """Invalid saved evidence or importer configuration."""


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReaImportError(f"rea: duplicate JSON key {key!r}")
        result[key] = value
    return result


def _read(config, root):
    document = config.get("document")
    if not isinstance(document, str) or not document:
        raise ReaImportError("rea: document must name a saved Evidence JSON file")
    path = (root / document).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ReaImportError("rea: document must be inside the source root")
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    if config.get("sha256") is not None and config["sha256"] != sha:
        raise ReaImportError("rea: document SHA-256 mismatch")
    data = json.loads(
        raw,
        object_pairs_hook=_unique,
        parse_constant=lambda value: (_ for _ in ()).throw(
            ValueError(f"rea: invalid JSON number {value}")
        ),
    )
    if not isinstance(data, dict) or "error" in data or data.get("isError"):
        raise ReaImportError(
            "rea: expected successful Evidence or a bundle, not an error"
        )
    envelope = "/structuredContent" if "structuredContent" in data else ""
    if envelope:
        data = data["structuredContent"]
    if not isinstance(data, dict):
        raise ReaImportError("rea: invalid Evidence envelope")
    if "records" in data:
        records = data["records"]
        unknowns = data.get("unknowns", [])
    else:
        records, unknowns = [data], []
    if not isinstance(records, list) or not isinstance(unknowns, list):
        raise ReaImportError("rea: bundle records and unknowns must be arrays")
    ids = set()
    for record in records:
        if not isinstance(record, dict) or not _EVIDENCE_ID.fullmatch(
            str(record.get("evidence_id", ""))
        ):
            raise ReaImportError("rea: each record requires an ev_<sha256> evidence_id")
        eid = record["evidence_id"]
        if eid in ids:
            raise ReaImportError("rea: duplicate evidence_id")
        ids.add(eid)
        required = {
            "subject",
            "provider",
            "analysis_profile",
            "operation",
            "predicate_type",
            "normalized_result",
            "confidence",
            "authority",
            "locations",
            "limitations",
            "evidence_links",
        }
        if not required <= record.keys():
            raise ReaImportError("rea: incomplete Evidence record")
        if not isinstance(record["operation"], str) or not record["operation"]:
            raise ReaImportError("rea: missing operation")
        if record["confidence"] not in ("observed", "derived", "inferred"):
            raise ReaImportError("rea: invalid confidence")
        subject = record["subject"]
        if subject is not None and (
            not isinstance(subject, dict)
            or not _DIGEST.fullmatch(
                str(
                    (
                        subject.get("digest")
                        if isinstance(subject.get("digest"), dict)
                        else {}
                    ).get("sha256", "")
                )
            )
        ):
            raise ReaImportError("rea: subject requires a SHA-256 digest")
        for key in ("locations", "limitations", "evidence_links"):
            if not isinstance(record[key], list):
                raise ReaImportError(f"rea: {key} must be an array")
        if any(not isinstance(item, str) for item in record["limitations"]):
            raise ReaImportError("rea: limitations must be strings")
    return (
        records,
        unknowns,
        path.relative_to(root.resolve()).as_posix(),
        sha,
        envelope,
        "records" in data,
    )


def discover_flows(adapter: dict, root: Path) -> dict:
    """Import candidates and named gaps into the existing flow inventory."""
    root = Path(root).resolve()
    records, unknowns, relative, sha, envelope, bundled = _read(adapter, root)
    obligations, unresolved = [], []
    seen = set()

    def gap(key, reason, provenance=None):
        unresolved.append(
            {
                "id": f"rea:{relative}:{key}",
                "kind": "boundary",
                "reason": reason,
                "source": {"file": relative, "line": 1},
                "rea": provenance or {"document_sha256": sha},
            }
        )

    def candidate(sid, pointer, provenance, method=None, path=None):
        if sid in seen:
            raise ReaImportError(f"rea: duplicate candidate identity {sid}")
        seen.add(sid)
        obligations.append(
            {
                "id": sid,
                "kind": "surface",
                "method": method,
                "path": path,
                "source": {"file": relative, "line": 1, "pointer": pointer},
                "declaration": "rea-candidate",
                "rea": provenance,
            }
        )

    gap(
        "acceptance",
        "REA findings are discovery candidates; reviewed behavioral bindings "
        "and fresh capcov execution are required. Static analysis and recorded captures "
        "do not establish runtime reachability, authorization, persistence or parity.",
    )
    for index, record in enumerate(records):
        eid = record["evidence_id"]
        pointer = envelope + (f"/records/{index}" if bundled else "")
        provenance = {
            key: record[key]
            for key in (
                "evidence_id",
                "subject",
                "provider",
                "analysis_profile",
                "operation",
                "predicate_type",
                "confidence",
                "authority",
                "locations",
                "evidence_links",
            )
        }
        provenance["document_sha256"] = sha
        for offset, reason in enumerate(record["limitations"]):
            gap(f"{eid}:limitation:{offset}", reason, provenance)
        payload = record["normalized_result"]
        if not isinstance(payload, dict):
            gap(
                f"{eid}:unsupported",
                f"No candidate mapping for {record['operation']}",
                provenance,
            )
            continue
        if record["operation"] == "analyze_javascript_application":
            graph = payload.get("graph")
            if (
                not isinstance(graph, dict)
                or graph.get("schema") != "JavaScriptApplicationGraph"
                or not isinstance(graph.get("nodes"), list)
            ):
                raise ReaImportError(
                    "rea: JavaScript analysis requires an application graph"
                )
            gap(
                f"{eid}:graph",
                "Application graph relationships and non-surface nodes are retained "
                "in the source document; this importer does not resolve a behavioral call graph.",
                provenance,
            )
            for n, node in enumerate(graph["nodes"]):
                if not isinstance(node, dict) or not isinstance(
                    node.get("node_id"), str
                ):
                    raise ReaImportError("rea: invalid application graph node")
                if node.get("kind") in _NODE_KINDS:
                    details = {
                        **provenance,
                        "node_id": node["node_id"],
                        "node_kind": node["kind"],
                        "identity": node.get("identity"),
                        "observations": [
                            o.get("evidence") for o in node.get("observations", [])
                        ],
                    }
                    candidate(
                        f"rea:{node['node_id']}",
                        f"{pointer}/normalized_result/graph/nodes/{n}",
                        details,
                    )
            for n, reason in enumerate(
                payload.get("limitations", []) + graph.get("limitations", [])
            ):
                gap(f"{eid}:graph-limit:{n}", reason, provenance)
            if graph.get("coverage", {}).get("status") != "complete":
                gap(
                    f"{eid}:graph-coverage",
                    "Application graph coverage is partial or unknown",
                    provenance,
                )
        elif (
            record["operation"] == "inspect_analysis_view"
            and payload.get("kind") == "api-surface"
        ):
            groups = payload.get("groups")
            if not isinstance(groups, list):
                raise ReaImportError("rea: API surface view requires groups")
            for n, group in enumerate(groups):
                if not isinstance(group, dict):
                    raise ReaImportError("rea: invalid API surface group")
                method, path, origin = (
                    group.get(key) for key in ("method", "path_template", "origin")
                )
                if not all(
                    isinstance(v, str) and v for v in (method, path, origin)
                ) or not path.startswith("/"):
                    raise ReaImportError(
                        "rea: API surface requires method, origin and absolute path"
                    )
                identity = hashlib.sha256(
                    json.dumps([origin, method, path]).encode()
                ).hexdigest()
                candidate(
                    f"rea:api:{identity}",
                    f"{pointer}/normalized_result/groups/{n}",
                    {
                        **provenance,
                        "origin": origin,
                        "templated_segments": group.get("templated_segments", []),
                        "parent_evidence_id": payload.get("parent_evidence_id"),
                    },
                    method,
                    path,
                )
            coverage = payload.get("coverage", {})
            if (
                coverage.get("next_offset") is not None
                or coverage.get("exhausted") is not True
                or coverage.get("examined") != coverage.get("total")
            ):
                gap(
                    f"{eid}:pagination",
                    "API surface view is paginated; unimported groups remain unknown",
                    provenance,
                )
            counts = payload.get("records", {})
            if (
                counts.get("examined") != counts.get("total")
                or counts.get("excluded", 0)
                or payload.get("excluded_records")
            ):
                gap(
                    f"{eid}:capture-coverage",
                    "Capture records are omitted or excluded",
                    provenance,
                )
        else:
            gap(
                f"{eid}:unsupported",
                f"No candidate mapping for {record['operation']}; inspect the retained record",
                provenance,
            )
    for n, unknown in enumerate(unknowns):
        if not isinstance(unknown, dict):
            raise ReaImportError("rea: invalid residual unknown")
        gap(
            f"unknown:{unknown.get('unknown_id', n)}:{unknown.get('revision', 1)}",
            "REA residual unknown requires review",
            {"document_sha256": sha, "unknown": unknown},
        )
    if not obligations:
        gap("no-surfaces", "REA input produced no supported surface candidates")
    return {
        "obligations": obligations,
        "unresolved": unresolved,
        "excluded_surfaces": [],
        "sources": {relative: sha},
    }


def discover(source_root: Path, target: Path, config: dict | None = None) -> dict:
    """CORE plugin; declare globs so the engine hashes the imported document."""
    config = config or {}
    document = config.get("document")
    root = Path(source_root).resolve()
    if not isinstance(document, str) or not any(
        (root / document) in root.glob(pattern) for pattern in config.get("globs", [])
    ):
        raise ReaImportError(
            "rea: globs must include document for core discovery provenance"
        )
    inventory = discover_flows(config, root)
    records = [
        {
            "id": o["id"],
            "method": o["method"],
            "path": o["path"],
            "file": o["source"]["file"],
            "line": 1,
        }
        for o in inventory["obligations"]
    ]
    result = build_core_dict(
        records, {"count": 0, "surfaces": []}, inventory["unresolved"]
    )
    by_id = {o["id"]: o for o in inventory["obligations"]}
    for surface in result["surfaces"]:
        obligation = by_id[surface["id"]]
        surface.update(
            kind="rea-candidate", rea=obligation["rea"], source=obligation["source"]
        )
    # A capture's HTTP verb is an observation, not a CRUD contract.
    result["_ops"] = {}
    return result
