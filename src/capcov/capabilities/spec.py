"""The specified set: requirements and scenarios of an OpenSpec tree.

Reads `<root>/**/spec.md`. The directory holding a `spec.md` is the area (a
nested directory joins its parts with "-"). In each file:

    ### Requirement: <title>      a requirement
    #### Scenario: <title>        a scenario of the requirement above it

Each scenario is one capability, `<area>/<requirement>/<scenario>`. A
requirement with no scenarios is itself the capability, `<area>/<requirement>`.
Headings inside fenced code blocks are ignored.

What the reader could not turn into a capability is named in `unresolved`
(a scenario before any requirement, a title with no letters, an ID declared
twice); the first declaration is kept.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import ids

_REQUIREMENT = re.compile(r"^###\s+Requirement:\s*(.*?)\s*$")
_SCENARIO = re.compile(r"^####\s+Scenario:\s*(.*?)\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


def read_openspec(root: Path, *, display_root: str | None = None) -> dict:
    root = Path(root)
    if not root.is_dir():
        raise ValueError(f"spec root {root} is not a directory")
    files = sorted(root.rglob("spec.md"))
    capabilities: dict[str, dict] = {}
    unresolved: list[dict] = []

    def add(capability: str, record: dict) -> None:
        if capability in capabilities:
            first = capabilities[capability]
            unresolved.append(
                {
                    "id": f"duplicate:{capability}",
                    "kind": "duplicate-id",
                    "reason": f"{record['file']}:{record['line']} declares {capability}, "
                    f"already declared at {first['file']}:{first['line']}",
                }
            )
            return
        capabilities[capability] = {"id": capability, **record}

    for path in files:
        relative = path.relative_to(root)
        if len(relative.parts) < 2:
            unresolved.append(
                {
                    "id": f"no-area:{relative}",
                    "kind": "no-area",
                    "reason": f"{relative} is directly under the spec root, so it has no area directory",
                }
            )
            continue
        try:
            area = ids.slug("-".join(relative.parts[:-1]))
        except ids.IdError as error:
            unresolved.append({"id": f"bad-area:{relative}", "kind": "bad-title", "reason": str(error)})
            continue
        shown = f"{display_root or root}/{relative.as_posix()}"
        _read_file(path, shown, area, add, unresolved)

    if not capabilities:
        unresolved.append(
            {
                "id": "no-capabilities",
                "kind": "no-capabilities",
                "reason": f"no requirement or scenario found under {root}",
            }
        )
    return {
        "kind": "capabilities-specified",
        "adapter": "openspec",
        "root": display_root or str(root),
        "capabilities": [capabilities[key] for key in sorted(capabilities)],
        "unresolved": unresolved,
    }


def _read_file(path: Path, shown: str, area: str, add, unresolved: list[dict]) -> None:
    requirement: dict | None = None  # {"slug", "title", "line", "scenarios"}
    in_fence = False

    def close() -> None:
        if requirement and not requirement["scenarios"]:
            add(
                f"{area}/{requirement['slug']}",
                {
                    "area": area,
                    "requirement": requirement["title"],
                    "scenario": None,
                    "file": shown,
                    "line": requirement["line"],
                },
            )

    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        found = _REQUIREMENT.match(line)
        if found:
            close()
            requirement = None
            try:
                requirement = {
                    "slug": ids.slug(found.group(1)),
                    "title": found.group(1),
                    "line": number,
                    "scenarios": 0,
                }
            except ids.IdError as error:
                unresolved.append(
                    {"id": f"bad-title:{shown}:{number}", "kind": "bad-title", "reason": str(error)}
                )
            continue
        found = _SCENARIO.match(line)
        if found:
            if requirement is None:
                unresolved.append(
                    {
                        "id": f"orphan-scenario:{shown}:{number}",
                        "kind": "orphan-scenario",
                        "reason": f"scenario {found.group(1)!r} has no requirement above it",
                    }
                )
                continue
            try:
                scenario = ids.slug(found.group(1))
            except ids.IdError as error:
                unresolved.append(
                    {"id": f"bad-title:{shown}:{number}", "kind": "bad-title", "reason": str(error)}
                )
                continue
            requirement["scenarios"] += 1
            add(
                f"{area}/{requirement['slug']}/{scenario}",
                {
                    "area": area,
                    "requirement": requirement["title"],
                    "scenario": found.group(1),
                    "file": shown,
                    "line": number,
                },
            )
    close()
