"""Row-change observation from the MySQL / MariaDB binary log.

Diffing two full inspections costs the size of the database on every vector:
a snapshot with a few hundred thousand rows takes a minute to serialise twice
even when the operation touched one row. The server already records every row
change in its binary log with ``binlog_format=ROW`` and
``binlog_row_image=FULL``, so the delta is available directly: mark the log
position before the steps, drain, read the events between the two positions.
Cost is proportional to what changed, not to what exists -- tens of
milliseconds on a multi-gigabyte database.

The reader drives ``mariadb-binlog --base64-output=decode-rows -vv`` (or
``mysqlbinlog``) through the same ``CommandRunner`` the other adapters use and
parses its verbose text. Row images name columns by ordinal (``@1``, ``@2``),
so the store loads column order once per table from ``information_schema`` and
binds names at parse time; a table whose column list is unknown is reported as
an ``unbound_tables`` finding, never guessed.

Value decoding stays deliberately literal: the text form the tool prints is
the value (``'2026-01-01 00:00:00'`` is a string, ``42`` is an int, ``NULL`` is
None, ``12.5`` is a float). A consumer comparing typed rows already normalises
volatile fields by name; type fidelity beyond that is not the reader's job.

Refusal: a decode that exits non-zero is ``CommandFailed``; an event whose
table map was never seen in the range is ``BinlogParseError`` naming the
position, because a row without its table would be a silently dropped change.

The delta shape is the one ``capcov.vectors.diff.store_delta`` produces from
two inspections, so a fixture may observe with the binlog and still feed the
same comparison:

    {"<table>": {"inserted": [row], "updated": [{"id", "changed": {col: [old, new]}}],
                 "deleted": [row]}}
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .runner import CommandRunner

DEFAULT_DECODE_ARGV = ("mariadb-binlog", "--base64-output=decode-rows", "-vv")
DEFAULT_CLIENT_ARGV = ("mariadb", "-uroot")

_TABLE_MAP = re.compile(r"Table_map: `([^`]+)`\.`([^`]+)` mapped to number (\d+)")
_STATEMENT = re.compile(r"^### (INSERT INTO|UPDATE|DELETE FROM) `([^`]+)`\.`([^`]+)`$")
_COLUMN = re.compile(r"^###\s+@(\d+)=(.*?)(?:\s+/\*.*\*/)?$")
_AT = re.compile(r"^# at (\d+)$")


class BinlogParseError(RuntimeError):
    """The decoded log referenced a table whose map was not in the range, or was malformed."""


@dataclass
class Position:
    file: str
    offset: int


@dataclass
class RowChanges:
    """Per-table changes plus the honest remainder."""

    tables: dict[str, dict] = field(default_factory=dict)
    unbound_tables: list[str] = field(default_factory=list)
    statements: int = 0

    def delta(self, informational: tuple[str, ...] = ()) -> tuple[dict, dict]:
        """Split into (compared, informational) by table name, the shape store_delta yields."""
        compared, info = {}, {}
        for table, change in sorted(self.tables.items()):
            (info if table in informational else compared)[f"mysql.{table}"] = change
        return compared, info


_SIGNED_UNSIGNED = re.compile(r"^(-?\d+) \(\d+\)$")


def _decode_value(text: str):
    if text == "NULL":
        return None
    # An integer column the tool cannot tell signed from unsigned prints both:
    # "-1 (255)". Take the signed reading; the column's declared signedness is
    # the consumer's to know and the comparison sees the same text both ways.
    both = _SIGNED_UNSIGNED.match(text)
    if both:
        text = both.group(1)
    if len(text) >= 2 and text[0] == "'" and text[-1] == "'":
        # The tool prints strings SQL-quoted: an embedded quote is doubled.
        return text[1:-1].replace("''", "'")
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    return text


def parse_decoded(text: str, columns: dict[str, list[str]], key_column: str = "id") -> RowChanges:
    """Turn ``--base64-output=decode-rows -vv`` text into RowChanges.

    ``columns`` maps ``schema.table`` (and, as a fallback, bare ``table``) to
    its ordinal column list. Updates are keyed by ``key_column`` when the
    table has it; otherwise by the full before-image.
    """
    out = RowChanges()
    unbound: set[str] = set()
    current: tuple[str, str] | None = None
    images: dict[str, list] = {}
    phase: str | None = None
    position = 0

    def bind(schema: str, table: str, values: dict[int, object]) -> dict | None:
        names = columns.get(f"{schema}.{table}") or columns.get(table)
        if names is None:
            unbound.add(table)
            return None
        row = {}
        for ordinal, value in values.items():
            if ordinal - 1 < len(names):
                row[names[ordinal - 1]] = value
            else:
                row[f"@{ordinal}"] = value
        return row

    def flush() -> None:
        nonlocal current, images, phase
        if current is None:
            return
        verb, schema, table = current[0], *current[1].split(".", 1)
        entry = out.tables.setdefault(table, {"inserted": [], "updated": [], "deleted": []})
        if verb == "INSERT INTO":
            for values in images.get("SET", []):
                row = bind(schema, table, values)
                if row is not None:
                    entry["inserted"].append(row)
        elif verb == "DELETE FROM":
            for values in images.get("WHERE", []):
                row = bind(schema, table, values)
                if row is not None:
                    entry["deleted"].append(row)
        else:
            befores, afters = images.get("WHERE", []), images.get("SET", [])
            if len(befores) != len(afters):
                raise BinlogParseError(f"at {position}: UPDATE on {table} has {len(befores)} before and {len(afters)} after images")
            for before_values, after_values in zip(befores, afters):
                before, after = bind(schema, table, before_values), bind(schema, table, after_values)
                if before is None or after is None:
                    continue
                changed = {name: [before.get(name), after[name]] for name in after if after[name] != before.get(name)}
                if changed:
                    entry["updated"].append({"id": before.get(key_column, before), "changed": changed})
        out.statements += 1
        current, images, phase = None, {}, None

    for line in text.splitlines():
        at = _AT.match(line)
        if at:
            flush()
            position = int(at.group(1))
            continue
        statement = _STATEMENT.match(line)
        if statement:
            flush()
            current = (statement.group(1), f"{statement.group(2)}.{statement.group(3)}")
            images, phase = {}, None
            continue
        if current is None:
            continue
        if line in ("### SET", "### WHERE"):
            phase = line[4:]
            images.setdefault(phase, []).append({})
            continue
        column = _COLUMN.match(line)
        if column and phase:
            images[phase][-1][int(column.group(1))] = _decode_value(column.group(2))
    flush()
    for table, entry in list(out.tables.items()):
        if not any(entry.values()):
            del out.tables[table]
    out.unbound_tables = sorted(unbound)
    return out


class BinlogObserver:
    """Mark a log position, then read the row changes made since it."""

    def __init__(
        self,
        runner: CommandRunner,
        database: str,
        log_dir: str,
        client_argv: tuple[str, ...] = DEFAULT_CLIENT_ARGV,
        decode_argv: tuple[str, ...] = DEFAULT_DECODE_ARGV,
        key_column: str = "id",
    ) -> None:
        self.runner = runner
        self.database = database
        self.log_dir = log_dir.rstrip("/")
        self.client_argv = list(client_argv)
        self.decode_argv = list(decode_argv)
        self.key_column = key_column
        self._columns: dict[str, list[str]] | None = None

    def _sql(self, statement: str) -> str:
        return self.runner.run(self.client_argv + ["-N", "-B", "-e", statement]).decode()

    def columns(self) -> dict[str, list[str]]:
        """Ordinal column names per table, read once from information_schema."""
        if self._columns is None:
            text = self._sql(
                "SELECT table_schema, table_name, GROUP_CONCAT(column_name ORDER BY ordinal_position SEPARATOR ',') "
                f"FROM information_schema.columns WHERE table_schema='{self.database}' GROUP BY table_schema, table_name"
            )
            columns: dict[str, list[str]] = {}
            for line in text.splitlines():
                schema, table, names = line.split("\t", 2)
                columns[f"{schema}.{table}"] = names.split(",")
                columns.setdefault(table, names.split(","))
            self._columns = columns
        return self._columns

    def mark(self) -> Position:
        line = self._sql("SHOW MASTER STATUS").splitlines()
        if not line:
            raise BinlogParseError("SHOW MASTER STATUS returned nothing; is log_bin ON?")
        file, offset = line[0].split("\t")[:2]
        return Position(file, int(offset))

    def changes_since(self, start: Position, end: Position | None = None) -> RowChanges:
        end = end or self.mark()
        if end.file != start.file:
            raise BinlogParseError(f"binlog rotated between {start.file} and {end.file}; rotation is not handled")
        if end.offset == start.offset:
            return RowChanges()
        argv = self.decode_argv + [
            f"--start-position={start.offset}",
            f"--stop-position={end.offset}",
            f"{self.log_dir}/{start.file}",
        ]
        text = self.runner.run(argv).decode(errors="replace")
        return parse_decoded(text, self.columns(), self.key_column)

    def reset_columns(self) -> None:
        self._columns = None
