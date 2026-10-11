"""Per-framework entity/operation recognizers (design §3.1-§3.3).

A recognizer answers the two framework questions the native python oracle answers
(`python_fastapi_sqlalchemy.discover_entities`, generalized): *is this declaration
a persistence entity, and what is its canonical identity?* and *what read/write
operation does this data-access site perform, on which entity?* It is PURELY
SEMANTIC: it never mints call-graph node ids and never resolves the call graph --
node identity and enclosing-function attribution are the deep builder's job
(`adapters.deep_core`), the call graph is SCIP's. That division is why a second
stack's recognizer is a small file, not a fork of the engine.

The `Recognizer` Protocol and `load_recognizer` live in `adapters.deep_core`
(task T3). Each recognizer module here exposes a module-level ``RECOGNIZER``
instance satisfying that protocol; `deep_core.DEFAULT_RECOGNIZER` maps a SCIP
language to the module name (``go`` -> ``go_gorm``, ``php`` -> ``php_eloquent``).

This package module carries the ONE thing both recognizers share: the canonical
identity a framework derives from a type name when it declares no explicit table.
gorm and Eloquent both default to *the snake_case plural of the type name*
(`NamingStrategy.TableName` = `toDBName(inflection.Plural(name))`; Eloquent =
`Str::snake(Str::pluralStudly(class_basename))`). `default_table_name` reproduces
that so `Job` -> ``jobs`` and `AuditLog` -> ``audit_logs`` on both sides. The
pluralizer is a faithful port of `jinzhu/inflection` (the library gorm links), so
the Go path is byte-equal to gorm for every case that library covers; Eloquent's
Doctrine inflector agrees on the common vocabulary and diverges only on edge
irregulars, which the recognizer overrides anyway when the model sets ``$table``.
"""

from __future__ import annotations

import re

# --------------------------------------------------------------------------
# Inflection: snake_case + English pluralization (jinzhu/inflection port).

# Words with no distinct plural form (jinzhu's uncountable set). Compared against
# the whole snake string; a match returns it unchanged.
_UNCOUNTABLE = frozenset({
    "equipment", "information", "rice", "money", "species", "series",
    "fish", "sheep", "jeans", "police",
})

# Irregular singular -> plural, applied as a suffix rule exactly as jinzhu does:
# regex ``(first-char)(rest)$`` -> ``first-char + plural[1:]``, so ``person`` ->
# ``people`` and ``salesperson`` -> ``salespeople`` (and, faithfully, jinzhu's
# quirk that ``human`` -> ``humen`` -- gorm produces the same, so a co-designed
# node key stays byte-equal).
_IRREGULAR = {
    "person": "people",
    "man": "men",
    "child": "children",
    "sex": "sexes",
    "move": "moves",
    "zombie": "zombies",
}

# ($-anchored regex, replacement), highest priority first -- the reverse of
# jinzhu's registration order, since it resolves the most recently added rule
# first. The first pattern that matches wins.
_PLURAL_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(quiz)$"), r"\1zes"),
    (re.compile(r"^(oxen)$"), r"\1"),
    (re.compile(r"^(ox)$"), r"\1en"),
    (re.compile(r"^(m|l)ice$"), r"\1ice"),
    (re.compile(r"^(m|l)ouse$"), r"\1ice"),
    (re.compile(r"(matr|vert|ind)(?:ix|ex)$"), r"\1ices"),
    (re.compile(r"(x|ch|ss|sh)$"), r"\1es"),
    (re.compile(r"([^aeiouy]|qu)y$"), r"\1ies"),
    (re.compile(r"(hive)$"), r"\1s"),
    (re.compile(r"(?:([^f])fe|([lr])f)$"), r"\1\2ves"),
    (re.compile(r"sis$"), "ses"),
    (re.compile(r"([ti])a$"), r"\1a"),
    (re.compile(r"([ti])um$"), r"\1a"),
    (re.compile(r"(buffal|tomat)o$"), r"\1oes"),
    (re.compile(r"(bu)s$"), r"\1ses"),
    (re.compile(r"(alias|status)$"), r"\1es"),
    (re.compile(r"(octop|vir)i$"), r"\1i"),
    (re.compile(r"(octop|vir)us$"), r"\1i"),
    (re.compile(r"^(ax|test)is$"), r"\1es"),
    (re.compile(r"s$"), "s"),
    (re.compile(r"$"), "s"),
]

_SNAKE_1 = re.compile(r"(.)([A-Z][a-z]+)")
_SNAKE_2 = re.compile(r"([a-z0-9])([A-Z])")


def snake_case(name: str) -> str:
    """A type name in ``snake_case``: ``AuditLog`` -> ``audit_log``.

    The camel/Pascal -> snake conversion gorm's ``toDBName`` and Laravel's
    ``Str::snake`` both perform on a class name. A boundary is inserted before an
    uppercase run that starts a new word (``HTTPServer`` -> ``http_server``,
    ``UserID`` -> ``user_id``); acronyms stay together. This approximates gorm's
    common-initialisms table on the vocabulary that matters for a table name and
    is exact for the fixture types (`Job`, `AuditLog`).
    """
    stepped = _SNAKE_1.sub(r"\1_\2", name)
    stepped = _SNAKE_2.sub(r"\1_\2", stepped)
    return stepped.replace("-", "_").replace(" ", "_").lower()


def pluralize(word: str) -> str:
    """The English plural of a lowercase word/phrase, jinzhu's ruleset.

    Rules are ``$``-anchored, so a snake phrase pluralizes its trailing word
    (``line_item`` -> ``line_items``, ``audit_log`` -> ``audit_logs``). Uncountables
    return unchanged; irregulars are matched as a suffix (``person`` -> ``people``).
    Input is expected already lowercased (as ``snake_case`` returns).
    """
    if not word:
        return word
    if word in _UNCOUNTABLE:
        return word
    for singular, plural in _IRREGULAR.items():
        if word == singular or word.endswith(singular):
            # jinzhu keeps the first char of the match and swaps the rest.
            head = word[: len(word) - len(singular)]
            return f"{head}{singular[0]}{plural[1:]}"
    for pattern, replacement in _PLURAL_RULES:
        if pattern.search(word):
            return pattern.sub(replacement, word)
    return word


def default_table_name(type_name: str) -> str:
    """The canonical identity a framework derives from a type name with no override.

    ``snake_case`` then ``pluralize`` -- the shared default of gorm's
    ``NamingStrategy`` and Eloquent's table guesser. ``Job`` -> ``jobs``,
    ``AuditLog`` -> ``audit_logs``, ``Category`` -> ``categories``.
    """
    return pluralize(snake_case(type_name))


# --------------------------------------------------------------------------
# Capture-stream helpers shared by both recognizers.


def first_text(match: dict, capture: str) -> str | None:
    """The ``text`` of a capture's first occurrence in a match, or None.

    A match is ``{capture_name: [occurrence, ...]}`` (design §3.1 / the engine's
    `_deep_matches`); a capture the query did not bind is absent or empty. Guards
    both so a recognizer reads an optional capture without a KeyError.
    """
    occurrences = match.get(capture)
    if not occurrences:
        return None
    return occurrences[0].get("text")


def first_occurrence(match: dict, capture: str) -> dict | None:
    """The first occurrence dict for a capture (``{text, file, line, ...}``), or None."""
    occurrences = match.get(capture)
    if not occurrences:
        return None
    return occurrences[0]


def short_name(qualified: str, *separators: str) -> str:
    """The last segment of a qualified name, split on any of ``separators``.

    ``App\\Models\\Job`` -> ``Job`` (on ``\\``); ``pkg.Type`` -> ``Type`` (on
    ``.``). Given no separators, splits on ``\\``, ``/`` and ``.`` -- the three a
    Go import path or a PHP FQCN uses. A leading separator (``\\App\\..``) is
    handled because only the final segment is kept.
    """
    seps = separators or ("\\", "/", ".")
    out = qualified
    for sep in seps:
        out = out.rsplit(sep, 1)[-1]
    return out
