"""Go / gorm entity + operation recognizer (design §3.2).

Entities are gorm model structs; a data-access operation is recognized at the
gorm CALL SITE (``db.First(&Job{})`` reads, ``db.Create(&AuditLog{})`` creates),
never inferred from the route's HTTP verb. Canonical identity is the struct's
``TableName()`` return value when it defines one, else gorm's snake_case plural of
the struct name.

Capture-stream contract (the names the ``entity_query`` / ``op_query`` in
`capcov.toml` must bind -- authored by task T5, consumed here):

``entity_query`` -- one match per struct, plus optional TableName-override matches:
  * ``@entity``  the struct declaration node (its ``(file, line)`` and, via the
    occurrence's ``start_line``/``end_line``, its span);
  * ``@type``    the struct's ``type_identifier`` (the source symbol name);
  * ``@gorm``    OPTIONAL marker that the struct is a gorm model -- an embedded
    ``gorm.Model`` or a ``gorm:"..."`` struct tag. When ANY struct in the stream
    carries this marker, only marked structs (and any with a TableName override)
    are treated as entities; when NO struct carries it, every captured struct is
    an entity (the query author gates precision by choosing to bind ``@gorm``).
  * ``@recv_type`` + ``@table_name`` -- OPTIONAL, from a second pattern matching a
    ``func (T) TableName() string { return "<lit>" }``: the receiver type name and
    the returned string literal, so ``T``'s identity is the literal, not the plural.

``op_query`` -- one match per gorm data-access call:
  * ``@op``     the ``call_expression`` node (the site ``(file, line)``);
  * ``@verb``   the gorm method ``field_identifier`` (``First``/``Create``/...);
  * ``@target`` the model type reference at the site -- the ``type_identifier`` of a
    ``&Job{}`` composite literal. A target that is an untyped local (``&job``) is
    not a type token; it does not resolve and the site is OMITTED, not guessed
    (binding it back to the struct is SCIP's job, design §3.2 / R9).

Node identity and enclosing-function attribution are the deep builder's concern;
this module returns entities, a reference->identity ``symbol_index``, and
site-keyed ops, exactly the §3.1 contract.
"""

from __future__ import annotations

from . import default_table_name, first_occurrence, first_text, short_name

language = "go"

# gorm data-access method -> CRUD verb, recognized AT THE CALL SITE. The design's
# core set (First/Find/Take -> read, Create -> create, Save/Update(s) -> update,
# Delete -> delete) plus the unambiguous siblings gorm ships, so a real repository
# method is not silently missed. Save is an upsert gorm documents as an update of
# the whole record, so it maps to update (not create).
GORM_VERBS: dict[str, str] = {
    "First": "read",
    "Find": "read",
    "Take": "read",
    "Last": "read",
    "Scan": "read",
    "Pluck": "read",
    "Count": "read",
    "FindInBatches": "read",
    "Create": "create",
    "CreateInBatches": "create",
    "Save": "update",
    "Update": "update",
    "Updates": "update",
    "UpdateColumn": "update",
    "UpdateColumns": "update",
    "Delete": "delete",
}


def _strip_go_string(text: str | None) -> str | None:
    """The value of a Go string literal occurrence: drop the surrounding quotes.

    ``"custom_jobs"`` -> ``custom_jobs``; a backtick raw string the same. A query
    that captured the ``*_content`` node instead already has the bare value, so a
    string with no surrounding quote is returned unchanged.
    """
    if text is None:
        return None
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"`":
        return text[1:-1]
    return text


def _collect(entity_matches: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """Split the entity stream into struct declarations and TableName overrides."""
    structs: list[dict] = []
    overrides: dict[str, str] = {}
    for match in entity_matches:
        recv = first_text(match, "recv_type")
        table = _strip_go_string(first_text(match, "table_name"))
        if recv is not None and table is not None:
            overrides[short_name(recv)] = table
            continue
        name = first_text(match, "type")
        if name is None:
            continue
        anchor = first_occurrence(match, "entity") or first_occurrence(match, "type")
        structs.append(
            {
                "name": name,
                "file": anchor["file"] if anchor else None,
                "line": anchor["line"] if anchor else None,
                "gorm": "gorm" in match and bool(match["gorm"]),
            }
        )
    return structs, overrides


class GoGormRecognizer:
    """The §3.1 ``Recognizer`` for Go / gorm."""

    language = "go"

    def recognize_entities(
        self, entity_matches: list[dict]
    ) -> tuple[list[dict], dict[str, str]]:
        structs, overrides = _collect(entity_matches)
        # A ``@gorm`` marker in the stream is the query author's signal to gate to
        # marked structs. A TableName override is an identity refinement, not a
        # gating signal, so it does NOT flip the stream into precision mode.
        markers_present = any(s["gorm"] for s in structs)

        entities: list[dict] = []
        symbol_index: dict[str, str] = {}
        for struct in structs:
            name = struct["name"]
            has_override = name in overrides
            # Precision when the query bound a marker; permissive otherwise. A
            # struct with a TableName override is always a model regardless.
            if markers_present and not struct["gorm"] and not has_override:
                continue
            identity = overrides[name] if has_override else default_table_name(name)
            entities.append(
                {
                    "name": identity,
                    "symbol": name,
                    "module": "",
                    "file": struct["file"],
                    "line": struct["line"],
                    "gorm": struct["gorm"],
                }
            )
            # Every reference form a data-access site can spell: the struct name
            # (a ``&Job{}`` construction) and the definition location (the SCIP
            # (file,line)-join's key). Later structs never overwrite the name key
            # for a different identity silently -- a duplicate struct name across
            # packages is a known ambiguity the join disambiguates by location.
            symbol_index.setdefault(name, identity)
            if struct["file"] is not None and struct["line"] is not None:
                symbol_index[f"{struct['file']}:{struct['line']}"] = identity
        entities.sort(key=lambda e: e["name"])
        return entities, symbol_index

    def recognize_ops(
        self, op_matches: list[dict], symbol_index: dict[str, str]
    ) -> list[dict]:
        out: list[dict] = []
        for match in op_matches:
            verb = first_text(match, "verb")
            crud = GORM_VERBS.get(verb) if verb else None
            if crud is None:
                continue
            site = first_occurrence(match, "op")
            if site is None:
                continue
            target = first_text(match, "target")
            if target is None:
                continue
            entity = symbol_index.get(target) or symbol_index.get(short_name(target))
            if entity is None:
                # An untyped receiver gorm resolves at runtime; the recognizer
                # never guesses it (soundiness -- the blind-spot enumerator owns
                # what static resolution cannot bind).
                continue
            out.append(
                {"file": site["file"], "line": site["line"], "entity": entity, "crud": crud}
            )
        return out


RECOGNIZER = GoGormRecognizer()
