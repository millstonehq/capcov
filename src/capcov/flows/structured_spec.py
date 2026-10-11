"""Generic reader for declarative contract/data specs: one engine, config per shape.

A structured spec (JSON/YAML) declares HTTP operations *somewhere* in its tree.
This engine is told WHERE with a small path query and emits one `http:<METHOD>
<path>` surface obligation per declared operation, plus the honest-limit boundary
obligations a declaration can never confirm on its own. It is the sole reader for
declarative specs; OpenAPI is the `OPENAPI_SPEC` config below, not a bespoke path.

The engine confirms nothing a contract cannot: it does not reach the network,
resolve `$ref`s, validate schemas, select servers, or copy descriptions, examples
or security values into obligations. Those unknowns are recorded as boundaries so a
declared surface is never mistaken for a confirmed, reachable one.

Config / query shape
--------------------
A spec profile is a plain dict:

    {
      "declaration": str,           # stamped on surfaces; namespaces boundary ids
      "parser": {"format": "json"|"yaml", "unique_keys": bool,
                 "duplicate_message": str},          # for derive(text, ...)
      "method_transform": "upper"|"none",            # applied to the method in the id
      "methods": [str, ...],        # the recognised verb set (for key_filter="methods")
      "document_rule": {"message": str,              # raised if the doc is the wrong shape
                        "version_field": str, "version_pattern": str},   # both optional
      "prefix_rule": {"message": str, "forbidden_chars": str},           # optional
      "root": {"field": str, "default": obj,
               "require": "map"|"list", "message": str},   # where the top collection lives
      "document_boundaries": [{"category": str, "reason": str}, ...],     # always emitted
      "empty_document_boundaries": [{"category": str, "reason": str}, ...], # if 0 surfaces
      "levels": [ <level>, ... ],   # the path query: how to descend to each operation
    }

Each level names one descent from a collection to its entries. The last level is
the leaf and must bind both `path` and `method`:

    {
      "iterate": "map"|"list",      # map entries are keyed; list entries are positional
      "skip_key_prefix": str,       # map: drop keys starting with this (e.g. extensions)
      "key_filter": "methods",      # map: keep only keys in the method set
      "sort": bool,                 # map: iterate surviving keys in sorted order
      "bind": {"path"|"method": "@key" | <field-name>},   # @key = the map key/list index
      "value_must_be_object": str,  # raised if an entry value is not an object
      "key_validation": {"must_start_with": str, "forbidden_chars": str, "message": str},
      "shape_dedupe": {"template_pattern": str, "replacement": str, "message": str},
      "field_allowlist": {"extra": [str, ...], "allow_prefix": str, "message": str},
      "gaps": [ {"when": "field_present", "field": str, "category": str,
                 "digest_role": "path"|"method", "reason": str},
                {"when": "no_children", "category": str,
                 "digest_role": ..., "reason": str} ],   # emitted before descending
    }

The JSON-pointer in each obligation's source is accumulated from the descent
(root field, then each map key or list index), so it points at the exact node.
Boundary ids are `boundary:{declaration}:{namespace}:{category}` where the
namespace is `digest([relative, prefix])[:16]`; a `digest_role` appends the sha of
the bound path/method, keeping one boundary per distinct entry.
"""

from __future__ import annotations

import json
import re

from .model import digest


def _parse(text: str, parser: dict) -> object:
    fmt = parser.get("format", "json")
    if fmt == "json":
        if parser.get("unique_keys"):
            message = parser.get("duplicate_message", "duplicate object member")

            def hook(pairs: list[tuple[str, object]]) -> dict:
                result: dict = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError(message)
                    result[key] = value
                return result

            return json.loads(text, object_pairs_hook=hook)
        return json.loads(text)
    if fmt == "yaml":
        import yaml  # optional dependency; only structured YAML specs pull it in

        return yaml.safe_load(text)
    raise ValueError(f"unsupported structured-spec parser format: {fmt}")


def _escape(token: object) -> str:
    """RFC 6901 JSON-pointer escaping, matching openapi.py's ~/ handling exactly."""
    return str(token).replace("~", "~0").replace("/", "~1")


def _validate_document(document: object, rule: dict | None) -> None:
    if rule is None:
        return
    ok = isinstance(document, dict)
    if ok and rule.get("version_field"):
        ok = bool(
            re.fullmatch(rule["version_pattern"], str(document.get(rule["version_field"], "")))
        )
    if not ok:
        raise ValueError(rule["message"])


def _validate_prefix(prefix: object, rule: dict | None) -> None:
    if rule is None:
        return
    if not isinstance(prefix, str) or (
        prefix
        and (
            not prefix.startswith("/")
            or prefix.endswith("/")
            or any(character in prefix for character in rule.get("forbidden_chars", ""))
        )
    ):
        raise ValueError(rule["message"])


def _validate_key(key: str, rule: dict | None) -> None:
    if rule is None:
        return
    start = rule.get("must_start_with")
    if (start and not key.startswith(start)) or any(
        character in key for character in rule.get("forbidden_chars", "")
    ):
        raise ValueError(rule["message"])


def _shape_dedupe(key: str, level: dict, level_index: int, bindings: dict,
                  pointer: str, state: dict) -> None:
    """A second path with the same template shape is recorded, not refused.

    "/{id}" and "/{optionId}" are one shape to a router and two to a reader; the
    document is ambiguous about which handler serves a concrete path. That is a
    limit of reading the document -- named as a boundary carrying both paths --
    not a reason to produce nothing. Both surfaces are still emitted.
    """
    rule = level.get("shape_dedupe")
    if rule is None:
        return
    shape = re.sub(rule["template_pattern"], rule["replacement"], key)
    seen = state["shapes"].setdefault(level_index, {})
    if shape in seen:
        _emit_boundary(
            {
                "category": "path-shape-collision:",
                "digest_role": "path",
                "reason": (
                    f"{rule['message']}: {key!r} has the same template shape as "
                    f"{seen[shape]!r}; the document does not say which serves a concrete path"
                ),
            },
            # The digest role is "path"; overriding it with this level's key
            # guarantees the digest input exists even when the level does not bind
            # path itself, and keys the boundary on the colliding entry.
            {**bindings, "path": key},
            pointer,
            state,
        )
        return
    seen[shape] = key


def _field_allowlist(value: object, rule: dict | None, state: dict) -> None:
    if rule is None:
        return
    allowed = set(state["methods"]) | set(rule.get("extra", []))
    prefix = rule.get("allow_prefix")
    for key in set(value if isinstance(value, dict) else {}) - allowed:
        if not (prefix and key.startswith(prefix)):
            raise ValueError(rule["message"])


def _read_field(value: object, field: str) -> object:
    if not isinstance(value, dict) or field not in value:
        raise ValueError(f"structured-spec: expected field {field!r} on operation record")
    return value[field]


def _entries(node: object, level: dict, state: dict) -> list[tuple[object, object, str]]:
    mode = level["iterate"]
    if mode == "map":
        if not isinstance(node, dict):
            raise ValueError(level.get("iterate_error", "expected a map"))
        keys = list(node.keys())
        skip = level.get("skip_key_prefix")
        if skip:
            keys = [key for key in keys if not str(key).startswith(skip)]
        if level.get("key_filter") == "methods":
            keys = [key for key in keys if key in state["methods"]]
        if level.get("sort"):
            keys = sorted(keys)
        return [(key, node[key], _escape(key)) for key in keys]
    if mode == "list":
        if not isinstance(node, list):
            raise ValueError(level.get("iterate_error", "expected a list"))
        return [(index, node[index], str(index)) for index in range(len(node))]
    raise ValueError(f"unsupported iterate mode: {mode}")


def _emit_boundary(spec: dict, bindings: dict, pointer: str, state: dict) -> None:
    category = spec["category"]
    role = spec.get("digest_role")
    if role:
        category = category + digest(bindings[role])[:16]
    state["obligations"].append(
        {
            "id": f"boundary:{state['declaration']}:{state['namespace']}:{category}",
            "kind": "unresolved",
            "source": {"file": state["relative"], "line": 1, "pointer": pointer},
            "reason": spec["reason"],
        }
    )


def _emit_surface(bindings: dict, pointer: str, state: dict, value: object = None) -> None:
    method = bindings["method"]
    if state["method_transform"] == "upper":
        method = method.upper()
    path = state["prefix"] + bindings["path"]
    obligation = {
        "id": f"http:{method} {path}",
        "kind": "surface",
        "source": {"file": state["relative"], "line": 1, "pointer": pointer},
        "declaration": state["declaration"],
    }
    if isinstance(value, dict):
        # The document's own grouping and one-line intent, carried verbatim so a
        # feature tree can be seeded from it and `features map` can claim by tag.
        tags = value.get("tags")
        if isinstance(tags, list) and tags and all(isinstance(t, str) for t in tags):
            obligation["tags"] = list(tags)
        summary = value.get("summary")
        if isinstance(summary, str) and summary:
            obligation["summary"] = summary
    state["obligations"].append(obligation)
    state["surface_count"] += 1


def _emit_gaps(level: dict, value: object, next_level: dict, bindings: dict,
               pointer: str, state: dict) -> None:
    for gap in level.get("gaps", []):
        when = gap["when"]
        if when == "field_present":
            fire = isinstance(value, dict) and gap["field"] in value
        elif when == "no_children":
            fire = len(_entries(value, next_level, state)) == 0
        else:
            raise ValueError(f"unsupported gap condition: {when}")
        if fire:
            _emit_boundary(gap, bindings, pointer, state)


def _descend(node: object, levels: list[dict], level_index: int, bindings: dict,
             pointer: str, state: dict) -> None:
    level = levels[level_index]
    leaf = level_index == len(levels) - 1
    for raw_key, value, segment in _entries(node, level, state):
        child_pointer = pointer + "/" + segment
        _validate_key(str(raw_key), level.get("key_validation"))
        if level.get("value_must_be_object") and not isinstance(value, dict):
            raise ValueError(level["value_must_be_object"])
        _field_allowlist(value, level.get("field_allowlist"), state)
        bound = dict(bindings)
        for role, source in level.get("bind", {}).items():
            bound[role] = raw_key if source == "@key" else _read_field(value, source)
        _shape_dedupe(str(raw_key), level, level_index, bound, child_pointer, state)
        if leaf:
            _emit_surface(bound, child_pointer, state, value)
        else:
            _emit_gaps(level, value, levels[level_index + 1], bound, child_pointer, state)
            _descend(value, levels, level_index + 1, bound, child_pointer, state)


def derive_document(document: object, config: dict, relative: str, prefix: str = "") -> list[dict]:
    """Emit obligations from an already-parsed structured document via `config`."""
    _validate_document(document, config.get("document_rule"))
    _validate_prefix(prefix, config.get("prefix_rule"))
    state = {
        "obligations": [],
        "relative": relative,
        "prefix": prefix,
        "declaration": config["declaration"],
        "namespace": digest([relative, prefix])[:16],
        "methods": set(config.get("methods", [])),
        "method_transform": config.get("method_transform", "none"),
        "surface_count": 0,
        "shapes": {},
    }
    for spec in config.get("document_boundaries", []):
        _emit_boundary(spec, {}, "", state)

    root = config["root"]
    root_node = document.get(root["field"], root.get("default")) if isinstance(document, dict) else None
    require = root.get("require")
    if (require == "map" and not isinstance(root_node, dict)) or (
        require == "list" and not isinstance(root_node, list)
    ):
        raise ValueError(root["message"])
    _descend(root_node, config["levels"], 0, {}, "/" + _escape(root["field"]), state)

    if state["surface_count"] == 0:
        for spec in config.get("empty_document_boundaries", []):
            _emit_boundary(spec, {}, "", state)
    return state["obligations"]


def derive(text: str, config: dict, relative: str, prefix: str = "") -> list[dict]:
    """Parse `text` with the config's parser, then derive obligations."""
    return derive_document(_parse(text, config.get("parser", {"format": "json"})),
                           config, relative, prefix)


# OpenAPI 3.0.x / 3.1.x expressed as one config of the generic engine: paths is a
# map of path -> path-item; each path-item is a map of method -> operation. This
# reproduces flows/openapi.derive exactly (surfaces, boundaries, pointers, ids).
_OPENAPI_METHODS = ["get", "put", "post", "delete", "options", "head", "patch", "trace"]

OPENAPI_SPEC: dict = {
    "declaration": "openapi",
    "parser": {
        "format": "json",
        "unique_keys": True,
        "duplicate_message": "duplicate JSON member in OpenAPI document",
    },
    "method_transform": "upper",
    "methods": _OPENAPI_METHODS,
    "document_rule": {
        "message": "openapi-json requires an OpenAPI 3.0.x or 3.1.x object",
        "version_field": "openapi",
        "version_pattern": r"3\.[01]\.\d+",
    },
    "prefix_rule": {
        "message": "OpenAPI prefix must be empty or an absolute path without a trailing slash",
        "forbidden_chars": "?#{}",
    },
    "root": {
        "field": "paths",
        "default": {},
        "require": "map",
        "message": "OpenAPI paths must be an object",
    },
    "document_boundaries": [
        {"category": "runtime-and-omitted-routes",
         "reason": "declared operations do not confirm mounted routes or discover omitted routes"},
        {"category": "business-outcomes",
         "reason": "schemas do not establish business outcomes, branches or external effects"},
        {"category": "roles-and-configurations",
         "reason": "authorization and deployment configurations require separate evidence"},
        {"category": "schemas-and-references",
         "reason": "parameters, responses, schemas and references are not resolved or validated"},
        {"category": "servers-and-bindings",
         "reason": "server URLs are not selected; consumer must verify the explicit path prefix and target binding"},
        {"category": "callbacks-webhooks-and-extensions",
         "reason": "callbacks, webhooks and specification extensions are not inventoried as operations"},
    ],
    "empty_document_boundaries": [
        {"category": "no-operations",
         "reason": "document declares no inline HTTP operations; this is not an empty complete system"},
    ],
    "levels": [
        {  # path level: map of path -> path-item
            "iterate": "map",
            "skip_key_prefix": "x-",
            "bind": {"path": "@key"},
            "key_validation": {
                "must_start_with": "/",
                "forbidden_chars": "?#",
                "message": "OpenAPI path must be an absolute path without a query or fragment",
            },
            "shape_dedupe": {
                "template_pattern": r"\{[^{}]+\}",
                "replacement": "{}",
                "message": "equivalent templated OpenAPI paths",
            },
            "value_must_be_object": "OpenAPI Path Item must be an object",
            "field_allowlist": {
                "extra": ["$ref", "summary", "description", "servers", "parameters"],
                "allow_prefix": "x-",
                "message": "unsupported OpenAPI Path Item field",
            },
            "gaps": [
                {"when": "field_present", "field": "$ref", "category": "path-ref:",
                 "digest_role": "path",
                 "reason": "unresolved Path Item reference; inline operations do not account for referenced operations"},
                {"when": "no_children", "category": "empty-path:", "digest_role": "path",
                 "reason": "path has no inline operations; referenced or filtered operations remain unknown"},
            ],
        },
        {  # method level: map of method -> operation (the leaf)
            "iterate": "map",
            "key_filter": "methods",
            "sort": True,
            "bind": {"method": "@key"},
            "value_must_be_object": "OpenAPI Operation must be an object",
        },
    ],
}
