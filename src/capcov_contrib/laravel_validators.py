"""Laravel validator rules -> one valid and one invalid request body, deterministically.

A Laravel route that takes a ``FormRequest`` declares, in ``rules()``, exactly
what a body must look like. That declaration is the incumbent's own contract
for the ``authorized`` and ``invalid-input`` cells of a write, so the bodies
are derived from it rather than written by hand: ``reflect_requests.php``
(beside this module, under ``laravel/``) boots the application and dumps every
mounted route's rules as the framework parses them; ``bodies_from_rules`` turns
one route's rules into the two bodies plus the bookkeeping the recorder needs.

The value mapping is FIXED so a rerun never changes a recorded vector:
``string`` -> ``'v'``, ``integer`` / ``numeric`` -> ``1``, ``boolean`` ->
``true``, ``in:a,b`` -> ``a`` (typed by the field's other rules), ``date`` ->
``2026-01-01``, ``date_format:F`` -> that date rendered in ``F``, ``email`` ->
``a@example.invalid``, ``uuid`` -> a fixed UUID, ``url`` -> ``https://example.invalid/``,
``array`` -> ``[]`` (or one element when child rules exist). ``min`` / ``max``
/ ``size`` / ``between`` / ``digits`` shape the value. Two rules become
PLACEHOLDERS the recorder resolves against the manifest, because the value is
not the validator's to choose: ``exists:table,col`` -> ``{{id:table}}`` and
``file`` / ``mimes:ext`` -> ``{{file:ext}}`` (which also makes the request
multipart).

The invalid body violates exactly ONE field: the first required field is
omitted; when nothing is required, the first typed field is corrupted with a
value of the wrong type; when neither exists no invalid body can be derived
from the rules alone, and ``invalid`` is ``None`` with a note saying so.

What it refuses to guess, and names instead:

* a rule that is a closure or a custom rule object has no string form; the
  field is ``unresolvable``. Omitted when optional; included with a best-effort
  value and an ``uncertain`` note when required (a wrong-type value still fails
  the framework rule before the custom rule runs, so the invalid body stays
  derivable);
* a rule name outside the vocabulary here is reported in ``unknown_rules`` so
  a new validator idiom is a visible finding, not a silently wrong body;
* a ``regex`` constraint is not solved; the value is ``'v'`` and a note says
  it may not satisfy the pattern;
* an optional wildcard child (``items.*.note``) never conjures an element whose
  required siblings are absent; it is ``skipped`` with the reason.

Pure functions, stdlib only: nothing here boots PHP or reads a census. The
join from reflected routes to a consumer's inventory is the consumer's.
"""

from __future__ import annotations

import copy
import csv
import io
import re

FIXED_UUID = "00000000-0000-4000-8000-000000000001"
FIXED_DATE = "2026-01-01"
FIXED_EMAIL = "a@example.invalid"
FIXED_URL = "https://example.invalid/"

# Rules the mapper understands. Anything else is reported as unknown_rules so a
# new validator vocabulary is a visible finding, not a silently wrong body.
TYPE_RULES = {"string", "integer", "int", "numeric", "boolean", "bool", "array", "email", "uuid", "date",
              "date_format", "url", "in", "not_in", "json", "ip", "ipv4", "ipv6", "alpha", "alpha_num",
              "alpha_dash", "timezone", "file", "image", "mimes", "mimetypes", "exists", "unique", "digits",
              "digits_between", "active_url", "regex", "not_regex"}
MODIFIER_RULES = {"required", "nullable", "sometimes", "present", "bail", "filled", "min", "max", "size", "between",
                  "gt", "gte", "lt", "lte", "distinct", "confirmed", "same", "different", "before", "after",
                  "before_or_equal", "after_or_equal", "required_if", "required_unless", "required_with",
                  "required_with_all", "required_without", "required_without_all", "prohibited", "prohibited_if",
                  "prohibited_unless", "exclude_if", "exclude_unless", "exclude_without", "exclude", "accepted",
                  "declined", "in_array", "starts_with", "ends_with", "lowercase", "uppercase", "missing"}
KNOWN_RULES = TYPE_RULES | MODIFIER_RULES
OMIT_RULES = {"prohibited", "prohibited_if", "prohibited_unless", "exclude", "exclude_if", "exclude_unless",
              "exclude_without", "missing"}
# Wrong-typed value per typed rule; None where no single value is wrong for
# every instance of the rule (not_in, unique, regex) so corruption moves on.
CORRUPT = {"integer": "not-an-integer", "int": "not-an-integer", "numeric": "not-a-number",
           "boolean": "not-a-boolean", "bool": "not-a-boolean", "array": "not-an-array", "email": "not-an-email",
           "uuid": "not-a-uuid", "date": "not-a-date", "date_format": "not-a-date", "url": "not a url",
           "in": "not-in-the-set", "not_in": None, "json": "{not json", "ip": "not-an-ip", "ipv4": "not-an-ip",
           "ipv6": "not-an-ip", "alpha": "1", "alpha_num": "-", "alpha_dash": " ", "timezone": "Nowhere/Nowhere",
           "file": "not-a-file", "image": "not-a-file", "mimes": "not-a-file", "mimetypes": "not-a-file",
           "exists": "{{missing:id}}", "unique": None, "digits": "x", "digits_between": "x", "active_url": "x",
           "regex": None, "not_regex": None, "string": 1}

_NUMBER = re.compile(r"-?\d+(\.\d+)?")


# --------------------------------------------------------------------------- rules
def parse_rule(text: str) -> tuple[str, list[str]]:
    """Split ``name:params`` the way ``Illuminate\\Validation\\ValidationRuleParser`` does."""
    if ":" not in text:
        return text.strip(), []
    name, params = text.split(":", 1)
    name = name.strip()
    if name in ("regex", "not_regex"):
        return name, [params]
    if name in ("in", "not_in"):
        return name, list(next(csv.reader(io.StringIO(params))))
    return name, [p.strip() for p in params.split(",")]


def field_rules(items: list[dict]) -> tuple[list[tuple[str, list[str]]], list[str]]:
    """(parsed string rules, unresolvable class names) for one reflected field.

    A reflected item is ``{"rule": "max:8"}`` for a string rule, optionally
    with ``"class"`` for a framework rule object that stringified, or
    ``{"class": ..., "unresolvable": true}`` for one that did not.
    """
    parsed, unresolvable = [], []
    for item in items:
        if item.get("unresolvable") or "rule" not in item:
            unresolvable.append(item.get("class") or item.get("rule") or "unknown")
            continue
        parsed.append(parse_rule(item["rule"]))
    return parsed, unresolvable


def rule_names(parsed) -> set[str]:
    return {name for name, _ in parsed}


def param(parsed, name: str):
    for rule_name, params in parsed:
        if rule_name == name:
            return params
    return None


def numeric_bounds(parsed) -> tuple[float | None, float | None]:
    low = high = None
    for name, params in parsed:
        try:
            if name == "min":
                low = float(params[0])
            elif name == "max":
                high = float(params[0])
            elif name == "size":
                low = high = float(params[0])
            elif name == "between":
                low, high = float(params[0]), float(params[1])
            elif name in ("gt", "gte") and params and _NUMBER.fullmatch(params[0]):
                low = float(params[0]) + (1 if name == "gt" else 0)
            elif name in ("lt", "lte") and params and _NUMBER.fullmatch(params[0]):
                high = float(params[0]) - (1 if name == "lt" else 0)
        except (ValueError, IndexError):
            continue
    return low, high


def date_for(fmt: str | None) -> str:
    """``FIXED_DATE`` rendered in a PHP ``date()`` format string."""
    if not fmt:
        return FIXED_DATE
    table = {"Y": "2026", "y": "26", "m": "01", "n": "1", "d": "01", "j": "1", "H": "00", "G": "0", "i": "00",
             "s": "00", "T": "UTC", "P": "+00:00", "O": "+0000", "e": "UTC", "u": "000000", "v": "000",
             "D": "Thu", "l": "Thursday", "M": "Jan", "F": "January", "A": "AM", "a": "am", "g": "12", "h": "12",
             "U": "1767225600", "c": "2026-01-01T00:00:00+00:00", "Z": "0"}
    out, escape = [], False
    for ch in fmt:
        if escape:
            out.append(ch)
            escape = False
        elif ch == "\\":
            escape = True
        else:
            out.append(table.get(ch, ch))
    return "".join(out)


def value_for(field: str, parsed, unresolvable: list[str], placeholders: dict, notes: list[str]):
    """Deterministic valid value for one leaf field from its parsed string rules."""
    names = rule_names(parsed)
    low, high = numeric_bounds(parsed)

    if names & {"file", "image", "mimes", "mimetypes"}:
        mimes = param(parsed, "mimes") or (["png"] if "image" in names else ["txt"])
        placeholders[field] = {"kind": "file", "extension": mimes[0], "carrier": "multipart"}
        return "{{file:" + mimes[0] + "}}"
    if "exists" in names:
        params = param(parsed, "exists")
        table = params[0].split(".")[-1] if params else "unknown"
        column = params[1] if len(params) > 1 and params[1] and params[1] != "NULL" else "id"
        placeholders[field] = {"kind": "id", "table": table, "column": column, "extra": params[2:] if params else []}
        return "{{id:" + table + "}}"
    if "in" in names:
        choices = param(parsed, "in") or []
        if not choices:
            notes.append(f"{field}: in: with no choices")
            return "v"
        pick = choices[0]
        if names & {"integer", "int", "numeric"} or (all(re.fullmatch(r"-?\d+", c) for c in choices) and "string" not in names):
            try:
                return int(pick)
            except ValueError:
                pass
        if names & {"boolean", "bool"}:
            return pick in ("1", "true", "True")
        return pick
    if names & {"boolean", "bool"}:
        return True
    if names & {"integer", "int"}:
        digits = param(parsed, "digits")
        if digits:
            return 10 ** (int(digits[0]) - 1)
        value = 1
        if low is not None:
            value = max(value, int(low))
        if high is not None:
            value = min(value, int(high))
        return value
    if "numeric" in names:
        value = 1
        if low is not None:
            value = max(value, int(low))
        if high is not None:
            value = min(value, int(high))
        return value
    if "digits" in names:
        return 10 ** (int(param(parsed, "digits")[0]) - 1)
    if "array" in names:
        return []
    if "email" in names:
        return FIXED_EMAIL
    if "uuid" in names:
        return FIXED_UUID
    if "date_format" in names:
        return date_for((param(parsed, "date_format") or [None])[0])
    if names & {"date", "before", "after", "before_or_equal", "after_or_equal"}:
        return FIXED_DATE
    if names & {"url", "active_url"}:
        return FIXED_URL
    if "ip" in names or "ipv4" in names:
        return "127.0.0.1"
    if "ipv6" in names:
        return "::1"
    if "json" in names:
        return "{}"
    if "timezone" in names:
        return "UTC"
    if "regex" in names:
        notes.append(f"{field}: regex constraint; value 'v' may not satisfy it")
    base = "v1" if ("alpha_num" in names or "alpha_dash" in names) else "v"
    if "string" in names or "alpha" in names or names & {"min", "max", "size"} or not names - MODIFIER_RULES or unresolvable:
        length = len(base)
        if low is not None:
            length = max(length, int(low))
        if high is not None:
            length = min(length, int(high)) if high >= 1 else 1
        # 'unique' needs no special value: the seed holds no 'v...' and the
        # value is unique by construction.
        return (base * ((length // len(base)) + 1))[:length]
    return "v"


# --------------------------------------------------------------------------- bodies
def split_path(field: str) -> list[str]:
    return field.split(".")


def set_path(body, path: list[str], value) -> None:
    """Insert value at a dotted path; ``*`` selects element 0 of a list, creating it."""
    node = body
    for index, part in enumerate(path):
        last = index == len(path) - 1
        if part == "*":
            if not isinstance(node, list):
                raise TypeError("wildcard under a non-list")
            if not node:
                node.append(None)
            if last:
                node[0] = value
                return
            if node[0] is None or not isinstance(node[0], (dict, list)):
                node[0] = [] if path[index + 1] == "*" else {}
            node = node[0]
        else:
            if isinstance(node, list):
                raise TypeError("named key under a list")
            if last:
                node[part] = value
                return
            if part not in node or not isinstance(node[part], (dict, list)):
                node[part] = [] if path[index + 1] == "*" else {}
            node = node[part]


def get_path(body, path: list[str]):
    node = body
    for part in path:
        if part == "*":
            if not isinstance(node, list) or not node:
                return None
            node = node[0]
        else:
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
    return node


def del_path(body, path: list[str]) -> bool:
    parent = get_path(body, path[:-1]) if len(path) > 1 else body
    last = path[-1]
    if last == "*":
        if isinstance(parent, list) and parent:
            parent.pop(0)
            return True
        return False
    if isinstance(parent, dict) and last in parent:
        del parent[last]
        return True
    return False


def corrupt_value(parsed):
    """(rule, value, how) that violates the first typed rule of a field, or None."""
    names = rule_names(parsed)
    for name, _params in parsed:
        if name in CORRUPT and CORRUPT[name] is not None:
            return name, CORRUPT[name], "wrong type"
    low, _high = numeric_bounds(parsed)
    if low is not None and low >= 1 and not names & {"array", "integer", "int", "numeric", "digits"}:
        return "min", "", "below minimum length"
    if low is not None and names & {"integer", "int", "numeric"}:
        return "min", int(low) - 1, "below minimum"
    return None


def is_required(parsed) -> bool:
    return bool(rule_names(parsed) & {"required", "present"})


def drives_element(parsed) -> bool:
    """Conditionally required rules make a wildcard element worth creating."""
    return any(name.startswith("required_") for name in rule_names(parsed))


def is_omitted(parsed) -> bool:
    return bool(rule_names(parsed) & OMIT_RULES)


def has_child_rules(field: str, rules: dict) -> bool:
    prefix = field + "."
    return any(other.startswith(prefix) for other in rules)


def bodies_from_rules(rules: dict) -> dict:
    """Valid body, invalid body and bookkeeping for one route's reflected rules.

    ``rules`` is ``{field: [reflected rule item, ...]}`` in the validator's
    declaration order (order matters: "first required field" is the first one
    the validator declares). Returns::

        {"valid": body,
         "invalid": {"body": body, "violates": {"field", "rule", "how"}} | None,
         "placeholders": {field: {"kind": "id" | "file", ...}},
         "unresolvable": {field: [class, ...]},
         "uncertain": [note, ...],
         "skipped": {field: reason},
         "unknown_rules": [name, ...],
         "fields": n, "fields_included": n}
    """
    placeholders, notes, unresolvable_fields, unknown = {}, [], {}, set()
    parsed_by_field = {}
    for field, items in rules.items():
        parsed, unresolvable = field_rules(items)
        parsed_by_field[field] = (parsed, unresolvable)
        if unresolvable:
            unresolvable_fields[field] = unresolvable
        unknown |= rule_names(parsed) - KNOWN_RULES
    omitted = {field for field, (parsed, _) in parsed_by_field.items() if is_omitted(parsed)}

    def suppressed(field: str) -> bool:
        parts = split_path(field)
        return any(".".join(parts[:i]) in omitted for i in range(1, len(parts) + 1))

    # A rule set keyed '*.x' validates a top-level list body.
    body = [] if any(split_path(field)[0] == "*" for field in rules) else {}
    included, skipped = [], {}

    def include(field: str, parsed, unresolvable) -> None:
        if has_child_rules(field, rules):
            # Laravel's array rule accepts both lists and maps; the child keys decide.
            value = [] if any(o.startswith(field + ".*") for o in rules) else {}
        else:
            value = value_for(field, parsed, unresolvable, placeholders, notes)
            if unresolvable:
                notes.append(f"{field}: required but constrained by {unresolvable}; value is best effort")
        try:
            set_path(body, split_path(field), value)
        except TypeError as error:
            skipped[field] = f"path conflict: {error}"
            return
        included.append(field)

    # Pass 1: every non-wildcard field, plus wildcard fields that force an element
    # to exist (required, present, required_*). Pass 2: optional wildcard fields,
    # only where pass 1 already created the element, so an optional child never
    # conjures an element whose required siblings are missing.
    deferred = []
    for field, (parsed, unresolvable) in parsed_by_field.items():
        if suppressed(field):
            skipped[field] = "prohibited/excluded by its rules"
            continue
        required = is_required(parsed)
        if unresolvable and not required:
            skipped[field] = "unresolvable rule and not required: omitted"
            continue
        if "*" in split_path(field) and not (required or drives_element(parsed)):
            deferred.append((field, parsed, unresolvable))
            continue
        include(field, parsed, unresolvable)
    for field, parsed, unresolvable in deferred:
        parts = split_path(field)
        parent = parts[:max(i for i, part in enumerate(parts) if part == "*")]
        parent_parsed = parsed_by_field.get(".".join(parent), ([], []))[0]
        parent_low, _ = numeric_bounds(parent_parsed)
        if get_path(body, parent + ["*"]) is None and not (parent_low and parent_low >= 1):
            skipped[field] = "optional element field; no element exists"
            continue
        include(field, parsed, unresolvable)

    # array parents with min:n get n elements: copies of the first element when
    # child rules built one, otherwise 'v' because the element type is undeclared
    for field, (parsed, _) in parsed_by_field.items():
        if "array" in rule_names(parsed) and field in included:
            low, _high = numeric_bounds(parsed)
            node = get_path(body, split_path(field))
            if isinstance(node, list) and low and int(low) >= 1 and len(node) < int(low):
                if not node:
                    notes.append(f"{field}: array needs min:{int(low)} elements but declares no element rules; 'v' used")
                    node.append("v")
                while len(node) < int(low):
                    node.append(copy.deepcopy(node[0]))

    invalid = None
    for field in included:
        parsed, _ = parsed_by_field[field]
        if is_required(parsed):
            corrupted = copy.deepcopy(body)
            if del_path(corrupted, split_path(field)):
                invalid = {"body": corrupted, "violates": {"field": field, "rule": "required", "how": "omitted"}}
                break
    if invalid is None:
        # Corrupt the first typed field: one in the body, else an omitted
        # unresolvable one whose string rules still type it (a wrong type fails
        # the framework rule before the custom rule runs).
        candidates = included + [f for f in parsed_by_field if f not in included and not suppressed(f)
                                 and "*" not in split_path(f)]
        for field in candidates:
            parsed, _ = parsed_by_field[field]
            wrong = corrupt_value(parsed)
            if wrong is not None:
                corrupted = copy.deepcopy(body)
                try:
                    set_path(corrupted, split_path(field), wrong[1])
                except TypeError:
                    continue
                invalid = {"body": corrupted, "violates": {"field": field, "rule": wrong[0], "how": wrong[2]}}
                break
    if invalid is None and rules:
        notes.append("no required or typed field: no invalid body can be derived from rules alone")
    return {"valid": body, "invalid": invalid, "placeholders": placeholders, "unresolvable": unresolvable_fields,
            "uncertain": notes, "skipped": skipped, "unknown_rules": sorted(unknown),
            "fields": len(rules), "fields_included": len(included)}


def carrier_for(methods: list[str], placeholders: dict) -> str:
    """The http driver carrier a derived body needs: multipart for a file, query for GET, else json."""
    if any(p.get("carrier") == "multipart" for p in placeholders.values()):
        return "multipart"
    return "query" if methods and methods[0].upper() == "GET" else "json"
