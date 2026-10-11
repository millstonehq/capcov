"""A bespoke reader used only by tests/test_plugin_seam.py.

It stands in for a clean-room source with NO tree-sitter grammar and NO
structured form -- a Zoho Deluge script, say -- the kind of source that has to be
brought to capcov as a plugin. It reads nothing from disk; it emits a fixed,
honest per-adapter result in each path's shape, so the test proves the SEAM
(dispatch + merge + carrier survival), not a parser.

* ``discover_flows`` -- the FLOWS-path contract
  (``flows.discovery._discover_from_config`` / ``discover(config)``).
* ``discover_core``  -- the CORE-path contract
  (``adapters.load(name, plugin=...)`` -> ``.discover(source_root, target, config)``).

The full callable contract for both paths is ``src/capcov/PLUGINS.md``.
"""

from __future__ import annotations

from pathlib import Path

# One route-shaped candidate the reader saw and deliberately dropped, and one
# limit it cannot resolve. Both are honest-denominator carriers: they must reach
# the inventory / core dict UNCHANGED so "capcov cannot read this" stays visible.
_EXCLUDED = [
    {
        "file": "forms/Lead.ds",
        "line": 12,
        "method": "",
        "path": "Lead.onValidate",
        "handler": "onValidate",
        "reason": "Deluge validation trigger is not a runtime-reachable surface",
    }
]

_UNRESOLVED_REASON = (
    "Deluge invokeurl connections to external services are not statically resolvable"
)


def discover_flows(adapter: dict, root: Path) -> dict:
    """FLOWS-path plugin: obligations + the honest-denominator carriers."""
    id_prefix = adapter.get("id_prefix", "deluge:")
    obligations = [
        {
            "id": f"{id_prefix}Lead.onCreate",
            "kind": "surface",
            "source": {"file": "forms/Lead.ds", "line": 3},
            "method": "CREATE",
            "handler": "Lead.onCreate",
        },
        {
            "id": f"{id_prefix}Contact.onEdit",
            "kind": "surface",
            "source": {"file": "forms/Contact.ds", "line": 7},
            "method": "UPDATE",
            "handler": "Contact.onEdit",
        },
    ]
    return {
        "obligations": obligations,
        "excluded_surfaces": [dict(s) for s in _EXCLUDED],
        "unresolved": [{"kind": "boundary", "reason": _UNRESOLVED_REASON}],
    }


def discover_core(source_root: Path, target: Path, config: dict) -> dict:
    """CORE-path plugin: a CORE adapter dict, built like a built-in adapter."""
    from capcov.adapters import build_core_dict

    records = [
        {
            "id": "http:POST /leads",
            "method": "POST",
            "path": "/leads",
            "handler": "Lead.onCreate",
            "file": "forms/Lead.ds",
            "line": 3,
            "module": "forms/Lead.ds",
        },
        {
            "id": "http:PUT /contacts",
            "method": "PUT",
            "path": "/contacts",
            "handler": "Contact.onEdit",
            "file": "forms/Contact.ds",
            "line": 7,
            "module": "forms/Contact.ds",
        },
    ]
    excluded = {"count": len(_EXCLUDED), "surfaces": [dict(s) for s in _EXCLUDED]}
    label = (config or {}).get("name", "deluge")
    unresolved = [{"adapter": label, "kind": "boundary", "reason": _UNRESOLVED_REASON}]
    return build_core_dict(records, excluded, unresolved)
