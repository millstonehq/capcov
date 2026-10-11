"""PHP / Laravel Eloquent entity + operation recognizer (design §3.3).

Entities are Eloquent model classes (``extends Model``); a data-access operation
is recognized at the Eloquent CALL SITE (``Job::find($id)`` reads,
``AuditLog::create($d)`` creates). Canonical identity is the model's ``$table``
property when it sets one, else the framework snake_case plural of the class name.

The binding rule is the crux of the PHP path (design §3.3, R4). This recognizer
binds ONLY the references scip-php can resolve -- a model type spelled as a name:
its class name, ``Model::method(...)`` static access, a ``use``-imported name, a
``::class`` const, a typed property/param/return. **Untyped facade and builder
chains** (``DB::table('jobs')``, ``Model::where(...)`` on an untyped ``$var``,
``__call`` magic, variable-variables) are the KNOWN scip-php blind spot: SCIP is
silent, not wrong, so such a site is NEVER bound and NEVER guessed here. It is
enumerated instead by the tree-sitter blind-spot pass (`scip.blindspots`,
task T2). ``PHP_MAGIC_FACADES`` is imported back from that single source of truth
-- the same discipline the FastAPI adapter uses for ``DYNAMIC_BLIND_CALLS`` -- so
the recognizer and the enumerator never disagree about what counts as a facade.

Capture-stream contract (bound by the ``entity_query`` / ``op_query`` in
`capcov.toml`, authored by task T5):

``entity_query`` -- one match per class:
  * ``@entity``    the ``class_declaration`` node (its ``(file, line)`` and, via the
    occurrence span, ``start_line``/``end_line`` covering the class body);
  * ``@class``     the class ``name`` (the source symbol);
  * ``@base``      OPTIONAL the base class name in ``extends`` -- when present the
    recognizer requires it to be an Eloquent base; when absent the class is
    accepted (the query already gated it via a ``#match?``/``#eq?`` predicate);
  * ``@namespace`` OPTIONAL the file's ``namespace`` name, for FQCN index keys;
  * ``@table_prop`` + ``@table_name`` -- OPTIONAL, the ``$table`` property's name and
    its string-literal value, so identity is that literal, not the plural.

``op_query`` -- one match per Eloquent data-access call:
  * ``@op``     the call node (the site ``(file, line)``);
  * ``@verb``   the method ``name`` (``find``/``get``/``create``/...);
  * ``@target`` the model reference -- a static call's ``scope`` name
    (``Job::find`` -> ``Job``). A facade scope or an untyped object does not
    resolve and the site is OMITTED (the blind-spot enumerator owns it).
"""

from __future__ import annotations

# The facade set is defined ONCE, in the blind-spot enumerator, and imported back
# here so a facade this recognizer refuses to bind is exactly a facade that pass
# enumerates -- never a gap between the two (design §3.3).
from ...scip.blindspots import PHP_MAGIC_FACADES
from . import default_table_name, first_occurrence, first_text, short_name

language = "php"

# The Eloquent base classes a model may extend. ``extends Model`` is the design's
# marker; ``Authenticatable`` and ``Pivot`` are the other Eloquent bases a real
# model extends, so a User model or a pivot is not silently missed.
ELOQUENT_BASES = frozenset({"Model", "Authenticatable", "Pivot"})

# Eloquent method -> CRUD, recognized AT THE CALL SITE (design §3.3: find/get/
# first/all -> read, create/save -> create, update -> update, delete -> delete),
# plus the unambiguous siblings the query builder ships.
ELOQUENT_VERBS: dict[str, str] = {
    "find": "read",
    "findOrFail": "read",
    "findMany": "read",
    "first": "read",
    "firstOrFail": "read",
    "get": "read",
    "all": "read",
    "value": "read",
    "pluck": "read",
    "count": "read",
    "exists": "read",
    "paginate": "read",
    "sole": "read",
    "cursor": "read",
    "create": "create",
    "createMany": "create",
    "save": "create",
    "insert": "create",
    "firstOrCreate": "create",
    "update": "update",
    "updateOrCreate": "update",
    "increment": "update",
    "decrement": "update",
    "delete": "delete",
    "destroy": "delete",
    "forceDelete": "delete",
}


def _strip_php_string(text: str | None) -> str | None:
    """The value of a PHP string literal: drop a surrounding pair of quotes.

    ``'jobs'`` / ``"jobs"`` -> ``jobs``. A query that captured the ``string_content``
    node already has the bare value and it is returned unchanged.
    """
    if text is None:
        return None
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    return text


def _classes_and_tables(
    entity_matches: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Split the entity stream into class declarations and ``$table`` literals."""
    classes: list[dict] = []
    tables: list[dict] = []
    for match in entity_matches:
        table_prop = first_text(match, "table_prop")
        table_name = _strip_php_string(first_text(match, "table_name"))
        # A $table literal (possibly in this same match, possibly a standalone
        # pattern). Only count it when it is the ``$table`` property.
        if table_name is not None and (table_prop is None or table_prop == "table"):
            occ = first_occurrence(match, "table_name")
            if occ is not None:
                tables.append({"value": table_name, "file": occ["file"], "line": occ["line"]})

        name = first_text(match, "class")
        if name is None:
            continue
        anchor = first_occurrence(match, "entity") or first_occurrence(match, "class")
        classes.append(
            {
                "name": name,
                "base": first_text(match, "base"),
                "namespace": first_text(match, "namespace"),
                "file": anchor["file"] if anchor else None,
                "line": anchor["line"] if anchor else None,
                "start": anchor.get("start_line") if anchor else None,
                "end": anchor.get("end_line") if anchor else None,
            }
        )
    return classes, tables


def _table_for(cls: dict, tables: list[dict]) -> str | None:
    """The ``$table`` literal declared inside ``cls``'s span, by containment.

    A ``$table`` occurrence in the same file whose line falls within the class's
    ``[start, end]`` belongs to it. This binds ``$table`` to its class whether the
    query captured it in the class match or as a standalone pattern.
    """
    if cls["file"] is None or cls["start"] is None or cls["end"] is None:
        return None
    for table in tables:
        if table["file"] == cls["file"] and cls["start"] <= table["line"] <= cls["end"]:
            return table["value"]
    return None


class PhpEloquentRecognizer:
    """The §3.1 ``Recognizer`` for PHP / Laravel Eloquent."""

    language = "php"

    def recognize_entities(
        self, entity_matches: list[dict]
    ) -> tuple[list[dict], dict[str, str]]:
        classes, tables = _classes_and_tables(entity_matches)

        entities: list[dict] = []
        symbol_index: dict[str, str] = {}
        for cls in classes:
            base = cls["base"]
            # ``extends Model`` is the marker. Honor an explicit base when the
            # query bound one; trust the query when it did not (it gated with a
            # predicate).
            if base is not None and short_name(base) not in ELOQUENT_BASES:
                continue
            identity = _table_for(cls, tables) or default_table_name(cls["name"])
            entities.append(
                {
                    "name": identity,
                    "symbol": cls["name"],
                    "module": cls["namespace"] or "",
                    "file": cls["file"],
                    "line": cls["line"],
                }
            )
            # Bindable reference forms: the class short name (``Job::find``, a
            # ``use``-imported name, a ``::class`` const) and, when the namespace
            # is known, the FQCN (a fully-qualified reference). setdefault keeps a
            # cross-namespace name collision from silently rebinding.
            symbol_index.setdefault(cls["name"], identity)
            if cls["namespace"]:
                symbol_index.setdefault(f"{cls['namespace']}\\{cls['name']}", identity)
        entities.sort(key=lambda e: e["name"])
        return entities, symbol_index

    def recognize_ops(
        self, op_matches: list[dict], symbol_index: dict[str, str]
    ) -> list[dict]:
        out: list[dict] = []
        for match in op_matches:
            verb = first_text(match, "verb")
            crud = ELOQUENT_VERBS.get(verb) if verb else None
            if crud is None:
                continue
            site = first_occurrence(match, "op")
            if site is None:
                continue
            target = first_text(match, "target")
            if target is None:
                continue
            scope = short_name(target)
            # An untyped facade/builder chain is the known scip-php blind spot: it
            # is enumerated by the blind-spot pass, never bound here.
            if scope in PHP_MAGIC_FACADES:
                continue
            entity = symbol_index.get(target) or symbol_index.get(scope)
            if entity is None:
                # Untyped variable, dynamic dispatch, or a name the recognizer
                # cannot resolve -- omitted, never guessed (soundiness).
                continue
            out.append(
                {"file": site["file"], "line": site["line"], "entity": entity, "crud": crud}
            )
        return out


RECOGNIZER = PhpEloquentRecognizer()
