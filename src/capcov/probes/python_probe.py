"""The Python probe: SQLAlchemy + ASGI + a contextvar.

Attaches at the class level -- `sqlalchemy.engine.Engine` and
`fastapi.FastAPI.__init__` -- rather than to an app instance, because the thing
under observation constructs its own app and the probe must not require it to be
modified. One line in a test setup, and nothing in the application.
"""

from __future__ import annotations

import contextlib
import contextvars
import itertools
import os
import re
from collections import defaultdict
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Behind the guard so the probe still imports where neither library is
    # installed -- capcov's own test suite runs the statement reader with
    # neither sqlalchemy nor fastapi present.
    from fastapi import FastAPI
    from sqlalchemy.engine import Connection
    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.requests import Request
    from starlette.responses import Response

# The SQL verb and the table it acts on. Deliberately a regex over the statement
# text and not a parse: the probe must not depend on a dialect, and it must
# never touch the parameters, which is where the data is.
_STATEMENT = re.compile(
    r"^\s*(?P<verb>SELECT|INSERT|UPDATE|DELETE)\b", re.IGNORECASE | re.DOTALL
)
# Optionally schema-qualified: `FROM pg_catalog.pg_class` must yield the schema
# so it can be dropped, not "pg_catalog" as though it were a table.
_TABLES = re.compile(
    r'\b(?:FROM|INTO|UPDATE|JOIN)\s+"?(?:(?P<schema>[A-Za-z_][A-Za-z0-9_]*)"?\.")?'
    r'?"?(?P<table>[A-Za-z_][A-Za-z0-9_]*)"?',
    re.IGNORECASE,
)

# Catalogs. A test that introspects the schema is exercising the database, not a
# capability of the system, and counting pg_class as an entity would put a row
# in the report that no amount of test-writing can ever move.
SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "sqlite_master", "sqlite_temp_master")
SYSTEM_PREFIXES = ("pg_", "sqlite_", "sql_")
_VERB_TO_OP = {"select": "read", "insert": "create", "update": "update", "delete": "delete"}

SURFACE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "capcov_surface", default=None
)
# The exercise is NOT a context variable. A test runner is serial, and a
# contextvar here loses the label the moment the framework runs the app in
# another thread -- a test client's portal thread copies the context once and
# then reports every later test under the first test's name, or under none.
_EXERCISE: str | None = None


def set_exercise(name: str | None) -> None:
    global _EXERCISE
    _EXERCISE = name

# (surface, entity, operation) -> set of exercises that produced it
_BINDINGS: dict[tuple[str, str, str], set[str]] = defaultdict(set)
_COUNTER = itertools.count()
_INSTALLED = False


@contextlib.contextmanager
def surface(name: str) -> Iterator[None]:
    """Attribute everything in this block to a named surface.

    For entry points that are not HTTP requests -- a worker tick, a queue
    consumer, a CLI command. Without this a worker's writes are attributed to
    the exercise that drove them, which lands the entity in the runtime-only
    cell and makes the gate ask why. That question has a right answer: declare
    the entry point.
    """
    token = SURFACE.set(name)
    try:
        yield
    finally:
        SURFACE.reset(token)


def _record(statement: str) -> None:
    match = _STATEMENT.match(statement)
    if not match:
        return
    op = _VERB_TO_OP[match.group("verb").lower()]
    where = SURFACE.get()
    if where is None:
        exercise = _EXERCISE
        # Not reached through any surface. Recorded under the exercise that did
        # it, so the entity still counts as observed -- an entity that only ever
        # sees SQL from test code reaching past the surfaces is a finding, not
        # an absence.
        where = f"test:{exercise}" if exercise else "<unattributed>"
    for match in _TABLES.finditer(statement):
        schema = (match.group("schema") or "").lower()
        table = match.group("table").lower()
        if schema in SYSTEM_SCHEMAS or table in SYSTEM_SCHEMAS:
            continue
        if table.startswith(SYSTEM_PREFIXES) or table == "dual":
            continue
        _BINDINGS[(where, table, op)].add(_EXERCISE or "")


def install_sqlalchemy() -> bool:
    try:
        from sqlalchemy import event
        from sqlalchemy.engine import Engine
    except ImportError:
        return False

    @event.listens_for(Engine, "before_cursor_execute")
    def _before(
        conn: Connection,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        _record(statement)

    return True


def install_asgi() -> bool:
    """Patch FastAPI so every app built from here on carries the request hook."""
    try:
        from fastapi import FastAPI
    except ImportError:
        return False

    if getattr(FastAPI, "_capcov_patched", False):
        return True
    original = FastAPI.__init__

    def patched(self: FastAPI, *args: object, **kwargs: object) -> None:
        original(self, *args, **kwargs)

        @self.middleware("http")
        async def _capcov(
            request: Request, call_next: RequestResponseEndpoint
        ) -> Response:
            # A unique label per request, not a shared "<in-request>" sentinel.
            # Two concurrent requests both relabelling the sentinel would steal
            # each other's rows, and the result would look plausible.
            pending = f"<in-request:{next(_COUNTER)}>"
            token = SURFACE.set(pending)
            try:
                response = await call_next(request)
                # THE ORDERING WRINKLE. The matched route is not in the scope on
                # the way in, only on the way out, so the template has to be read
                # after the handler. Reading request.url.path instead records one
                # surface per record id: 26 real bindings became 40 rows of noise
                # the first time this was written.
                route = request.scope.get("route")
                template = getattr(route, "path", None) or request.url.path
                _relabel(pending, f"{request.method} {template}")
                return response
            finally:
                SURFACE.reset(token)

    FastAPI.__init__ = patched
    FastAPI._capcov_patched = True
    return True


def _relabel(pending: str, surface_id: str) -> None:
    """Move rows recorded during this request onto the resolved template."""
    for key in [k for k in _BINDINGS if k[0] == pending]:
        exercises = _BINDINGS.pop(key)
        _BINDINGS[(surface_id, key[1], key[2])] |= exercises


def install_entry_points(target: Path) -> list[str]:
    """Wrap each declared non-HTTP entry point so its work is attributed to it.

    A worker is an entry point but not a request, so nothing sets the surface
    when a test drives it directly -- and the table it writes then lands in the
    runtime-only cell for a reason that is about instrumentation rather than
    about the system.

    The alternative was asking the application to import capcov and wrap its own
    worker loop. Refused: a framework that requires production code to know
    about it is one people evaluate and do not adopt. capcov.toml already names
    these symbols for the static half, so the same declaration does both jobs.

    Ordering matters. This runs at plugin configure time, before pytest imports
    any test module, so `from x import process_one` in a test file picks up the
    wrapper rather than the original.
    """
    import importlib
    import tomllib

    config_path = target / "capcov.toml"
    if not config_path.exists():
        return []
    wrapped = []
    for item in tomllib.loads(config_path.read_text()).get("entry_points", []):
        module_name, _, symbol = item["symbol"].partition(":")
        try:
            module = importlib.import_module(module_name)
            original = getattr(module, symbol)
        except (ImportError, AttributeError) as exc:
            # Loud. A declared entry point that cannot be imported is a stale
            # config, and silently skipping it would show up later as an
            # unexplained runtime-only cell with no hint of the cause.
            raise RuntimeError(
                f"capcov.toml declares entry point {item['id']!r} as "
                f"{item['symbol']!r}, which does not resolve: {exc}"
            ) from exc
        wrapped.append(item["id"])
        setattr(module, symbol, _attributed(original, item["id"]))
    return wrapped


def _attributed[T](func: Callable[..., T], surface_id: str) -> Callable[..., T]:
    import functools
    import inspect

    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args: object, **kwargs: object) -> T:
            with surface(surface_id):
                return await func(*args, **kwargs)

        return async_wrapper

    @functools.wraps(func)
    def wrapper(*args: object, **kwargs: object) -> T:
        with surface(surface_id):
            return func(*args, **kwargs)

    return wrapper


def install() -> dict[str, bool]:
    global _INSTALLED
    if _INSTALLED:
        return {"already": True}
    hooks = {"sqlalchemy": install_sqlalchemy(), "asgi": install_asgi()}
    _INSTALLED = any(hooks.values())
    return hooks


def dump(
    out: Path,
    source_root: Path,
    exercises: int,
    *,
    source_snapshot=None,
    source_provenance: dict | None = None,
) -> None:
    from .. import artifacts

    # ``cmd observe`` already captured the source identity and owns the one
    # exact verification; reusing it here means the pytest process walks the
    # oracle zero times.  A direct caller IS the publication boundary, so it
    # keeps the legacy behaviour: one exact walk, from bytes.
    snapshot = source_snapshot
    if snapshot is None and source_provenance is None:
        snapshot = artifacts.snapshot_tree(source_root, trust_cache=False)
    merged: dict[tuple[str, str], dict] = {}
    for (surface_id, entity, op), exercises_ in sorted(_BINDINGS.items()):
        row = merged.setdefault(
            (surface_id, entity), {"operations": set(), "tests": set()}
        )
        row["operations"].add(op)
        row["tests"].update(t for t in exercises_ if t)
    bindings = [
        {
            "surface": surface_id,
            "entity": entity,
            "operations": sorted(row["operations"]),
            "tests": sorted(row["tests"]),
        }
        for (surface_id, entity), row in sorted(merged.items())
    ]
    if source_provenance is not None:
        derived_from = {**source_provenance, "extractor": "capcov python-probe"}
    else:
        if snapshot is None:
            raise ValueError("python probe needs a source snapshot or provenance")
        derived_from = snapshot.provenance(
            os.path.basename(str(source_root)), "capcov python-probe"
        )
    artifacts.write(out, "observed", derived_from, {"bindings": bindings, "exercises": exercises})
