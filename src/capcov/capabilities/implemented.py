"""The implemented set: discovery surfaces mapped to capability IDs.

Discovery (`capcov discover`) names what the code exposes, such as a route or a
command, as a surface with an `id`. Each `[[capabilities.implemented]]` entry
maps surfaces to one capability:

    surface    = "POST /v1/sessions"          fnmatch glob over the surface id
    pattern    = '^(?P<verb>\\w+) /v1/(?P<area>[^/]+)'   regex, named groups
    capability = "sessions/create-a-session"  ID, or a template over the groups
    plugin     = "package.module:callable"    reads the surfaces itself

Exactly one of `surface`, `pattern` or `plugin` selects surfaces. A plugin is
called `callable(surfaces: list[dict], config: dict)` and returns a list of
`{"capability": <id>, "surface": <surface id>}`.

A surface that no entry maps is listed in `unmapped_surfaces`: code the
specification never claimed.
"""

from __future__ import annotations

import fnmatch
import re

from . import ids


def read(surfaces: list[dict], entries: list[dict]) -> dict:
    by_capability: dict[str, list[str]] = {}
    mapped: set[str] = set()
    unresolved: list[dict] = []
    surface_ids = [s["id"] for s in surfaces]

    def record(capability: str, surface: str) -> None:
        by_capability.setdefault(capability, [])
        if surface not in by_capability[capability]:
            by_capability[capability].append(surface)
        mapped.add(surface)

    for index, entry in enumerate(entries):
        label = f"capabilities.implemented[{index}]"
        selectors = [k for k in ("surface", "pattern", "plugin") if k in entry]
        if len(selectors) != 1:
            raise ValueError(f"{label}: set exactly one of surface, pattern, plugin")
        if "plugin" in entry:
            from ..adapters import import_plugin_callable

            for item in import_plugin_callable(entry["plugin"])(list(surfaces), entry):
                try:
                    record(ids.validate(item["capability"]), item["surface"])
                except (KeyError, ids.IdError) as error:
                    raise ValueError(f"{label}: plugin returned a bad record {item!r}: {error}")
            continue
        if "capability" not in entry:
            raise ValueError(f"{label}: capability is required")
        matches: list[tuple[str, dict]] = []
        if "surface" in entry:
            matches = [(s, {}) for s in surface_ids if fnmatch.fnmatchcase(s, entry["surface"])]
        else:
            regex = re.compile(entry["pattern"])
            for s in surface_ids:
                found = regex.search(s)
                if found:
                    matches.append((s, found.groupdict()))
        if not matches:
            unresolved.append(
                {
                    "id": f"unmatched-entry:{index}",
                    "kind": "unmatched-entry",
                    "reason": f"{label} ({entry.get('surface') or entry.get('pattern')}) "
                    "matched no discovered surface",
                }
            )
        for surface, fields in matches:
            try:
                record(ids.fill(entry["capability"], fields), surface)
            except (KeyError, ids.IdError) as error:
                unresolved.append(
                    {
                        "id": f"bad-capability:{index}:{surface}",
                        "kind": "bad-capability",
                        "reason": f"{label} on {surface}: {error}",
                    }
                )
    return {
        "kind": "capabilities-implemented",
        "capabilities": [
            {"id": key, "surfaces": sorted(by_capability[key])} for key in sorted(by_capability)
        ],
        "unmapped_surfaces": sorted(set(surface_ids) - mapped),
        "unresolved": unresolved,
    }
