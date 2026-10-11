"""The exercised set: what a running system did, read from structured logs.

Each `[[capabilities.exercised]]` entry is one log source:

    name        label used in reports and exclusions
    glob        files under the project root (an input path from the CLI replaces it)
    format      "logfmt" | "json" | "regex"      how a line becomes fields
    pattern     regex with named groups (format = "regex")
    match       {contains = "...", regex = "..."}  which lines are of interest
    where       [condition, ...]                 field conditions every line must meet
    capability  ID or template over the fields, e.g. "attend/{action}/{target}"
    pass_when   [condition, ...]                 any true makes the record a pass
    fail_when   [condition, ...]                 any true makes the record a fail
    plugin      "package.module:callable"        replaces format, capability, pass_when, fail_when

A condition is `{field = "status", op = "gte", value = 500}` with `op` one of
eq, ne, gt, gte, lt, lte, in, contains, exists. A condition on a field the record
does not have is false, whatever the operator.

`fail_when` wins. With no `pass_when`, every other attributed line passes.

A plugin is called `callable(line: str, source: dict)` per line and returns None
(ignore), or one record or a list of records, each
`{"capability": <id>, "result": "pass" | "fail", "detail": <optional text>}`.

Lines the source selected but could not turn into a record (invalid JSON, a
missing field, a result neither pass_when nor fail_when classifies, a bad ID)
are counted as unattributed and named in `unresolved`; a source whose files do
not exist is named too. Neither reads as a quiet system.
"""

from __future__ import annotations

import glob as globlib
import json
import re
from pathlib import Path

from . import ids

_LOGFMT = re.compile(r'([A-Za-z_][\w.\-]*)=("(?:[^"\\]|\\.)*"|\S*)')
_SAMPLES = 3
_DETAIL = 240
_OPS = ("eq", "ne", "gt", "gte", "lt", "lte", "in", "contains", "exists")


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def holds(condition: dict, fields: dict) -> bool:
    op = condition.get("op")
    if op not in _OPS:
        raise ValueError(f"condition {condition!r}: op must be one of {', '.join(_OPS)}")
    name = condition.get("field")
    if not name:
        raise ValueError(f"condition {condition!r}: field is required")
    if op == "exists":
        return (name in fields and fields[name] is not None) == bool(condition.get("value", True))
    if name not in fields or fields[name] is None:
        return False
    actual, wanted = fields[name], condition.get("value")
    if op == "eq":
        return str(actual) == str(wanted)
    if op == "ne":
        return str(actual) != str(wanted)
    if op == "in":
        return str(actual) in [str(w) for w in wanted]
    if op == "contains":
        return str(wanted) in str(actual)
    a, b = _number(actual), _number(wanted)
    if a is None or b is None:
        return False
    return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]


def _unquote(raw: str) -> str:
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        try:
            return json.loads(raw)
        except ValueError:
            return raw[1:-1]
    return raw


def _fields(line: str, source: dict, regex: re.Pattern | None) -> dict | None:
    """The fields of `line`, or None when the line is not in the source's format."""
    fmt = source.get("format")
    if fmt == "regex":
        found = regex.search(line) if regex else None
        return found.groupdict() if found else None
    if fmt == "json":
        start = line.find("{")
        if start < 0:
            return None
        try:
            value = json.loads(line[start:])
        except ValueError:
            return None
        return value if isinstance(value, dict) else None
    pairs = {key: _unquote(raw) for key, raw in _LOGFMT.findall(line)}
    return pairs or None


class _Source:
    def __init__(self, entry: dict, index: int) -> None:
        self.entry = entry
        self.label = f"capabilities.exercised[{index}]"
        self.name = entry.get("name")
        if not self.name:
            raise ValueError(f"{self.label}: name is required")
        self.label = f"{self.label} ({self.name})"
        self.plugin = None
        self.regex = None
        if "plugin" in entry:
            from ..adapters import import_plugin_callable

            self.plugin = import_plugin_callable(entry["plugin"])
        else:
            fmt = entry.get("format")
            if fmt not in ("logfmt", "json", "regex"):
                raise ValueError(f"{self.label}: format must be logfmt, json or regex (or set plugin)")
            if "capability" not in entry:
                raise ValueError(f"{self.label}: capability is required")
            if fmt == "regex":
                if "pattern" not in entry:
                    raise ValueError(f"{self.label}: format regex needs pattern")
                self.regex = re.compile(entry["pattern"])
        match = entry.get("match") or {}
        self.contains = match.get("contains")
        self.match_regex = re.compile(match["regex"]) if "regex" in match else None
        for key in ("where", "pass_when", "fail_when"):
            for condition in entry.get(key) or []:
                holds(condition, {})  # validates op and field

    def selected(self, line: str) -> bool:
        if self.contains is not None and self.contains not in line:
            return False
        return self.match_regex is None or bool(self.match_regex.search(line))

    def records(self, line: str) -> list[dict] | None:
        """Records for a selected line; None when the line is not attributable."""
        entry = self.entry
        if self.plugin is not None:
            out = self.plugin(line, entry)
            if out is None:
                return []
            batch = [out] if isinstance(out, dict) else list(out)
            try:
                return [
                    {
                        "capability": ids.validate(r["capability"]),
                        "result": r["result"] if r["result"] in ("pass", "fail") else _bad(r),
                        "detail": r.get("detail"),
                    }
                    for r in batch
                ]
            except (KeyError, TypeError, ids.IdError, ValueError):
                return None
        fields = _fields(line, entry, self.regex)
        if fields is None:
            return None
        if not all(holds(c, fields) for c in entry.get("where") or []):
            return []
        try:
            capability = ids.fill(entry["capability"], fields)
        except (KeyError, ids.IdError):
            return None
        if any(holds(c, fields) for c in entry.get("fail_when") or []):
            result = "fail"
        elif "pass_when" not in entry or any(holds(c, fields) for c in entry["pass_when"]):
            result = "pass"
        else:
            return None
        return [{"capability": capability, "result": result, "detail": None}]

    def pre_filter_ok(self, line: str) -> bool:
        """Whether a line without an explicit `match` is of interest to the source."""
        if self.plugin is not None or "match" in self.entry:
            return True
        # With no match, the format decides: only lines the format parses are selected.
        return _fields(line, self.entry, self.regex) is not None


def _bad(record: dict):
    raise ValueError(f"result must be pass or fail, got {record.get('result')!r}")


def read(
    entries: list[dict], root: Path, inputs: dict[str, list[str]] | None = None
) -> dict:
    root = Path(root)
    inputs = inputs or {}
    observed: dict[str, dict] = {}
    stats: dict[str, dict] = {}
    unresolved: list[dict] = []
    names = set()
    for index, entry in enumerate(entries):
        source = _Source(entry, index)
        if source.name in names:
            raise ValueError(f"{source.label}: name is used twice")
        names.add(source.name)
        if source.name in inputs:
            paths = [Path(p) for p in inputs[source.name]]
        else:
            if "glob" not in entry:
                raise ValueError(f"{source.label}: glob is required (or pass an input path)")
            paths = sorted(Path(p) for p in globlib.glob(str(root / entry["glob"]), recursive=True))
        paths = [p for p in paths if p.is_file()]
        counts = {"files": len(paths), "lines": 0, "selected": 0, "attributed": 0, "unattributed": 0}
        stats[source.name] = counts
        if not paths:
            unresolved.append(
                {
                    "id": f"no-input:{source.name}",
                    "kind": "no-input",
                    "reason": f"{source.label} matched no log file ({entry.get('glob')})",
                }
            )
        bad_samples: list[str] = []
        for path in paths:
            shown = _shown(path, root)
            with path.open(encoding="utf-8", errors="replace") as stream:
                for number, raw in enumerate(stream, 1):
                    line = raw.rstrip("\n")
                    counts["lines"] += 1
                    if not line.strip() or not source.selected(line) or not source.pre_filter_ok(line):
                        continue
                    counts["selected"] += 1
                    records = source.records(line)
                    if records is None:
                        counts["unattributed"] += 1
                        if len(bad_samples) < _SAMPLES:
                            bad_samples.append(f"{shown}:{number}: {line[:_DETAIL]}")
                        continue
                    for record in records:
                        counts["attributed"] += 1
                        row = observed.setdefault(
                            record["capability"],
                            {"id": record["capability"], "pass": 0, "fail": 0, "sources": [], "fail_samples": []},
                        )
                        row[record["result"]] += 1
                        if source.name not in row["sources"]:
                            row["sources"].append(source.name)
                        if record["result"] == "fail" and len(row["fail_samples"]) < _SAMPLES:
                            row["fail_samples"].append(
                                {
                                    "source": source.name,
                                    "at": f"{shown}:{number}",
                                    "line": (record["detail"] or line)[:_DETAIL],
                                }
                            )
        if counts["unattributed"]:
            unresolved.append(
                {
                    "id": f"unattributed:{source.name}",
                    "kind": "unattributed-log-line",
                    "reason": f"{source.label}: {counts['unattributed']} selected line(s) gave no "
                    "capability and result. First: " + " | ".join(bad_samples),
                }
            )
    for row in observed.values():
        row["sources"].sort()
    return {
        "kind": "capabilities-exercised",
        "capabilities": [observed[key] for key in sorted(observed)],
        "sources": stats,
        "unresolved": unresolved,
    }


def _shown(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)
