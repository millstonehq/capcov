"""Capability IDs: `<area>/<requirement>/<scenario>`.

Each segment is a slug: lowercase, every run of characters outside [a-z0-9]
becomes one "-", leading and trailing "-" removed. An ID with fewer segments
is a prefix and applies to every ID beneath it, so a log line that names only
`area/requirement` covers each scenario of that requirement.
"""

from __future__ import annotations

import re

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_TEMPLATE_FIELD = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class IdError(ValueError):
    pass


def slug(text: object) -> str:
    out = _NON_ALNUM.sub("-", str(text).lower()).strip("-")
    if not out:
        raise IdError(f"{text!r} has no letters or digits to form an ID segment from")
    return out


def capability_id(area: str, requirement: str, scenario: str | None = None) -> str:
    parts = [slug(area), slug(requirement)]
    if scenario is not None:
        parts.append(slug(scenario))
    return "/".join(parts)


def validate(capability: object) -> str:
    """Return `capability` if it is one to three well-formed segments."""
    parts = capability.split("/") if isinstance(capability, str) else []
    if not 1 <= len(parts) <= 3 or any(not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", p) for p in parts):
        raise IdError(
            f"{capability!r} is not a capability ID: expected "
            "<area>[/<requirement>[/<scenario>]] with each segment [a-z0-9-]"
        )
    return capability


def covers(prefix: str, capability: str) -> bool:
    """True when `prefix` equals `capability` or is a segment prefix of it."""
    return capability == prefix or capability.startswith(prefix + "/")


def fill(template: str, fields: dict) -> str:
    """Substitute `{name}` in `template` with the slug of each field's value.

    Literal text is kept as written. A missing field raises `KeyError` naming it;
    a value that slugs to nothing, or a result that is not an ID, raises `IdError`.
    """

    def one(match: re.Match) -> str:
        name = match.group(1)
        if name not in fields or fields[name] is None:
            raise KeyError(name)
        return slug(fields[name])

    return validate(_TEMPLATE_FIELD.sub(one, template))
