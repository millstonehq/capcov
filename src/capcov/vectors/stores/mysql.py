"""MySQL / MariaDB store adapter: snapshot as a dump, restore by piping it back.

The adapter drives the database's own command-line clients through a
``CommandRunner`` -- ``mariadb-dump`` (or ``mysqldump``) for the snapshot,
``mariadb`` (or ``mysql``) for the restore and for inspection -- so it works
against a server on the host or inside a container without a database driver
and without leaving the stdlib. The client argv is configuration: a consumer
that authenticates differently passes its own.

Inspection reads every table (or a named subset) through the client's
``--batch`` output and a plain-text reader. That reader sees STRINGS: an
integer column comes back as ``"42"`` and ``NULL`` as ``None``. A consumer that
needs typed rows supplies its own inspector through the fixture seam; the
adapter does not guess a type from the text.

Refusal: a client that exits non-zero is ``CommandFailed`` (from the runner); a
batch output that cannot be parsed into a header-shaped table is
``TableReadError`` naming the table, never an empty list.
"""

from __future__ import annotations

from .binlog import BinlogObserver, Position
from .runner import CommandRunner

DEFAULT_CLIENT_ARGV = ("mariadb", "-uroot")
DEFAULT_DUMP_ARGV = ("mariadb-dump", "-uroot", "--skip-comments")


class TableReadError(RuntimeError):
    """The client's batch output for a table did not parse as header + rows."""


def _unescape(field: str) -> str | None:
    r"""Undo the ``--batch`` escaping: ``\t``, ``\n``, ``\\``, ``\0``; ``NULL`` is None."""
    if field == "NULL":
        return None
    if "\\" not in field:
        return field
    out: list[str] = []
    index = 0
    while index < len(field):
        char = field[index]
        if char == "\\" and index + 1 < len(field):
            nxt = field[index + 1]
            out.append({"t": "\t", "n": "\n", "\\": "\\", "0": "\0"}.get(nxt, "\\" + nxt))
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def parse_batch(text: str, table: str = "<query>") -> list[dict]:
    """Parse ``--batch`` output (tab-separated, header row first) into row dicts.

    An empty result prints nothing at all, so no output is an empty table -- not
    an error. A row whose field count differs from the header's is an error
    naming the table: a silently truncated row would hide a delta.
    """
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines:
        return []
    header = lines[0].split("\t")
    rows = []
    for number, line in enumerate(lines[1:], start=2):
        fields = line.split("\t")
        if len(fields) != len(header):
            raise TableReadError(
                f"{table}: line {number} has {len(fields)} fields, header has {len(header)}"
            )
        rows.append({name: _unescape(field) for name, field in zip(header, fields)})
    return rows


class MySQLStore:
    """Snapshot/restore/inspect one database through the command-line clients."""

    def __init__(
        self,
        runner: CommandRunner,
        database: str,
        client_argv: tuple[str, ...] | list[str] = DEFAULT_CLIENT_ARGV,
        dump_argv: tuple[str, ...] | list[str] = DEFAULT_DUMP_ARGV,
    ) -> None:
        if not database:
            raise ValueError("MySQLStore: database name is empty")
        self.runner = runner
        self.database = database
        self.client_argv = list(client_argv)
        self.dump_argv = list(dump_argv)

    # -- snapshot / restore ---------------------------------------------------

    def snapshot(self) -> bytes:
        """The dump of the whole database, as the bytes the client printed."""
        return self.runner.run([*self.dump_argv, self.database])

    def restore(self, token: bytes) -> None:
        """Pipe the dump back through the client. The dump's own DROP/CREATE statements do the reset."""
        if not isinstance(token, (bytes, bytearray)) or not token:
            raise ValueError("MySQLStore.restore: token is not a non-empty dump")
        self.runner.run([*self.client_argv, self.database], input=bytes(token))

    # -- inspect ------------------------------------------------------------

    def execute(self, sql: str, input: bytes | None = None) -> str:
        """Run one statement (or a script on stdin) against the database; return batch text."""
        argv = [*self.client_argv, "--batch", self.database]
        if input is None:
            argv += ["-e", sql]
        return self.runner.run(argv, input=input).decode(errors="replace")

    def tables(self) -> list[str]:
        text = self.runner.run(
            [*self.client_argv, "--batch", "--skip-column-names", self.database, "-e", "SHOW TABLES"]
        ).decode(errors="replace")
        return [line for line in text.split("\n") if line]

    def read_table(self, table: str) -> list[dict]:
        if "`" in table:
            raise ValueError(f"MySQLStore.read_table: table name {table!r} contains a backtick")
        text = self.execute(f"SELECT * FROM `{table}`")
        return parse_batch(text, table)

    def inspect(self, tables: str | list[str] = "all", exclude: tuple[str, ...] | list[str] = ()) -> dict:
        """``{"rows": {table: [row, ...]}}`` for every table (or the named ones) not excluded.

        Empty tables are omitted, matching the recorder's diff which treats an
        absent table and an empty one alike. A named table that does not exist
        is an error from the client, not a silent omission.
        """
        names = self.tables() if tables == "all" else list(tables)
        rows = {}
        for name in names:
            if name in exclude:
                continue
            data = self.read_table(name)
            if data:
                rows[name] = data
        return {"rows": rows}


class ObservingMySQLStore(MySQLStore):
    """A MySQLStore that observes row changes through the binary log.

    Needs the server started with ``--log-bin --binlog-format=ROW
    --binlog-row-image=FULL``; ``mark()`` refuses when ``log_bin`` is off
    rather than returning an empty delta. ``inspect`` and ``restore`` are
    inherited: a consumer keeps full-diff comparison available for the case
    the observer reports an unbound table.
    """

    def __init__(self, runner: CommandRunner, database: str, log_dir: str, **kwargs) -> None:
        super().__init__(runner, database, **kwargs)
        self.observer = BinlogObserver(
            runner, database, log_dir, client_argv=tuple(self.client_argv),
            decode_argv=tuple(kwargs.pop("decode_argv", ()) or ("mariadb-binlog", "--base64-output=decode-rows", "-vv")),
        )

    def queue_depths(self) -> dict[str, int]:
        return {}

    def mark(self) -> Position:
        return self.observer.mark()

    def changes_since(self, mark: Position) -> dict:
        changes = self.observer.changes_since(mark)
        return {"rows": dict(changes.tables), "collections": {}, "unbound": list(changes.unbound_tables)}
