"""Run a SCIP indexer over a target tree and read the resulting index as JSON.

Two tools do two jobs and this module is the seam between them.

  * an INDEXER walks a source tree and emits a binary ``index.scip``:
    ``scip-python`` (Pyright-based) for Python, ``scip-go`` (go/packages) for Go,
    ``scip-php`` (davidrjenni/scip-php, native SCIP) for PHP;
  * the ``scip`` CLI's ``print --json`` turns that binary back into JSON, so
    capcov never links a protobuf runtime.

``read_scip_index`` normalizes the CLI's JSON into the one shape capcov consumes:
a list of documents, each carrying its symbols and its occurrences. Every
occurrence records whether it is a definition (from the SCIP symbol-role bitset)
and, for a definition, the line span (``enclosing_range``) that a caller of that
definition falls inside -- which is how a reference is attributed to the function
it sits in.

Indexers disagree on the JSON dialect and normalization reconciles them so the
rest of capcov sees one shape:

  * ``kind`` arrives as a *name string* from the hand-authored fixture but as a
    *numeric* ``SymbolKind`` from the real ``scip print --json`` (scip-go). A
    number is mapped back to its enum name so ``map._category`` classifies it;
    an unknown number falls back to the descriptor suffix, which is authoritative.
  * ``enclosing_range`` is populated by scip-python and scip-go but **absent from
    every scip-php occurrence**. Without it a caller cannot be attributed and the
    whole PHP call graph is silently empty, so for a document that carries
    definitions yet no enclosing range at all, a callable definition's enclosing
    span is synthesized (its own line through the document's last line); the
    innermost-caller rule then attributes each in-body call to the tightest
    enclosing method. Any dialect that supplies enclosing ranges is left untouched.

Tool requirements:

  * ``scip-python``: ``npm install -g @sourcegraph/scip-python``. Does NOT need
    the target's dependencies installed or a clean type-check; it degrades
    gracefully.
  * ``scip-go``: ``go install github.com/scip-code/scip-go/cmd/scip-go@latest``
    (the module MOVED off ``sourcegraph/``; the old import path no longer
    builds). Needs a buildable Go module in the target directory.
  * ``scip-php``: does NOT fit the ``which(exe) + cwd + --output`` model. It runs
    as ``php <scip-php script> --memory-limit=2G`` from the project root and
    writes ``index.scip`` into the cwd; the script lives in a standalone
    composer install (``davidrjenni/scip-php:dev-main``) outside the target,
    located via ``SCIP_PHP_BIN`` (default ``/tmp/scip-tool/vendor/bin/scip-php``).
    ``php`` must be on PATH (e.g. ``nix shell nixpkgs#php83``) and the target
    needs its ``vendor/autoload.php`` + ``composer.lock`` present.
  * the ``scip`` CLI, located via the ``SCIP_CLI`` environment variable, a
    ``scip`` binary placed next to this package (see ``_BUNDLED_SCIP``), or
    ``scip`` on ``PATH``. Build it with::

        git clone --depth 1 https://github.com/sourcegraph/scip.git
        cd scip && go build -o scip ./cmd/scip
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

# name of the index the indexer writes and the reader consumes, in the target
# directory. Every indexer below is told to write, or is known to write, this.
_INDEX_FILENAME = "index.scip"

# The scip-php script, located outside the target (its php-parser pin conflicts
# with a target's own). Overridable so a differently-placed standalone install
# is found without editing code.
_SCIP_PHP_BIN_ENV = "SCIP_PHP_BIN"
_DEFAULT_SCIP_PHP_BIN = "/tmp/scip-tool/vendor/bin/scip-php"

_SCIP_PHP_HINT = (
    "install a standalone davidrjenni/scip-php (dev-main) via composer in /tmp "
    "(mkdir -p /tmp/scip-tool && cd /tmp/scip-tool; composer require "
    "davidrjenni/scip-php:dev-main) and put php83 on PATH "
    "(nix shell nixpkgs#php83 nixpkgs#php83Packages.composer); "
    f"override the script path with {_SCIP_PHP_BIN_ENV}"
)

# language -> (executable, install hint). ``executable`` is the program that must
# be on PATH; the hint rides in the exception text because a missing indexer is
# an operator problem whose fix is one line they should not have to look up.
_INDEXERS = {
    "python": ("scip-python", "npm install -g @sourcegraph/scip-python"),
    "go": (
        "scip-go",
        "go install github.com/scip-code/scip-go/cmd/scip-go@latest",
    ),
    "php": ("php", _SCIP_PHP_HINT),
}

# SCIP SymbolKind enum (scip.proto): number -> name. The real ``scip print
# --json`` emits kinds as these numbers; ``map._category`` expects the name
# (or falls back to the descriptor suffix). Kept complete so a populated kind is
# never silently discarded; an unlisted number degrades to the suffix.
_SYMBOL_KIND_BY_NUMBER = {
    1: "Array", 2: "Assertion", 3: "AssociatedType", 4: "Attribute", 5: "Axiom",
    6: "Boolean", 7: "Class", 8: "Constant", 9: "Constructor", 10: "DataFamily",
    11: "Enum", 12: "EnumMember", 13: "Event", 14: "Fact", 15: "Field",
    16: "File", 17: "Function", 18: "Getter", 19: "Grammar", 20: "Instance",
    21: "Interface", 22: "Key", 23: "Lang", 24: "Lemma", 25: "Macro",
    26: "Method", 27: "MethodReceiver", 28: "Message", 29: "Module",
    30: "Namespace", 31: "Null", 32: "Number", 33: "Object", 34: "Operator",
    35: "Package", 36: "PackageObject", 37: "Parameter", 38: "ParameterLabel",
    39: "Pattern", 40: "Predicate", 41: "Property", 42: "Protocol",
    43: "Quasiquoter", 44: "SelfParameter", 45: "Setter", 46: "Signature",
    47: "Subscript", 48: "String", 49: "Struct", 50: "Tactic", 51: "Theorem",
    52: "ThisParameter", 53: "Trait", 54: "Type", 55: "TypeAlias",
    56: "TypeClass", 57: "TypeFamily", 58: "TypeParameter", 59: "Union",
    60: "Value", 61: "Variable", 62: "Contract", 63: "Error", 64: "Library",
    65: "Modifier", 66: "AbstractMethod", 67: "MethodSpecification",
    68: "ProtocolMethod", 69: "PureVirtualMethod", 70: "TraitMethod",
    71: "TypeClassMethod", 72: "Accessor", 73: "Delegate", 74: "MethodAlias",
    75: "SingletonClass", 76: "SingletonMethod", 77: "StaticDataMember",
    78: "StaticEvent", 79: "StaticField", 80: "StaticMethod",
    81: "StaticProperty", 82: "StaticVariable", 83: "MethodReceiver",
    84: "Extension", 85: "Mixin", 86: "Concept",
}

# A built ``scip`` binary dropped next to this package is picked up with nothing
# configured. Kept out of the wheel by default; SCIP_CLI overrides it.
_BUNDLED_SCIP = Path(__file__).resolve().parent / "vendor" / "scip"

# SCIP SymbolRole bitset: bit 0x1 is Definition; every other bit (import,
# write/read access, generated, test, ...) is orthogonal to it.
_DEFINITION_ROLE = 0x1

# The full SymbolRole bitset (scip.proto), in bit order. ``decode_roles`` turns
# the raw integer into the names the retained output and the static fact
# exporter (section 29 role relations) consume; the default output keeps only
# the ``is_definition`` projection it always had.
_ROLE_BITS = (
    (0x1, "definition"),
    (0x2, "import"),
    (0x4, "write"),
    (0x8, "read"),
    (0x10, "generated"),
    (0x20, "test"),
    (0x40, "forward_definition"),
)

# Digest kinds for an index identity (section 29): a real ``index.scip`` is
# hashed as its bytes; a checked-in ``scip print --json`` fixture has no bytes
# to hash, so it is hashed as its canonical JSON behind a fixed prefix.
INDEX_DIGEST_BINARY = "binary"
INDEX_DIGEST_JSON = "json"
_JSON_DIGEST_PREFIX = "scip-json:"

# A symbol whose kind the indexer left unset. scip-python emits none of these;
# scip-go populates a string like "Struct"/"Field"/"Function". For an unset
# kind we fall back to parsing the descriptor suffix.
_UNSPECIFIED_KIND = frozenset({None, "", 0, "UnspecifiedKind", "UnspecifiedSymbolKind"})


class IndexerNotFound(RuntimeError):
    """A required SCIP indexer (scip-python / scip-go) is not on PATH."""


class ScipCliNotFound(RuntimeError):
    """The ``scip`` CLI needed to read an index as JSON could not be located."""


def _scip_php_bin() -> str:
    """Absolute path to the standalone scip-php script (env-overridable)."""
    return os.environ.get(_SCIP_PHP_BIN_ENV, _DEFAULT_SCIP_PHP_BIN)


def _index_command(language: str, output: str) -> list[str]:
    """The argv for indexing the current working directory, per language.

    Split out so the exact command can be asserted without a tool installed.
    scip-python and scip-go are pointed at ``output`` and run with the target
    directory as their cwd (see ``run_scip_index``), so a relative ``index.scip``
    lands there. scip-php takes no output flag -- it writes ``index.scip`` into
    the cwd -- and is invoked as ``php <script> --memory-limit=2G`` because the
    ``which(exe) + --output`` model does not fit a ``php vendor/bin`` setup.
    """
    if language == "python":
        return [
            "scip-python", "index",
            "--project-name", "spike",
            "--project-version", "0.0.1",
            "--output", output,
            ".",
        ]
    if language == "go":
        return ["scip-go", "--output", output]
    if language == "php":
        return ["php", _scip_php_bin(), "--memory-limit=2G"]
    raise ValueError(
        f"unsupported language {language!r}; expected one of {sorted(_INDEXERS)}"
    )


def _index_env(language: str) -> dict[str, str] | None:
    """Environment overrides for the indexer, or None to inherit unchanged.

    No indexer needs an environment override today (php is resolved off PATH and
    scip-php loads its own vendored parser), but the hook is part of the
    per-language invocation strategy: the argv and the env together are what a
    caller must reproduce, and both are asserted in the tests.
    """
    return None


def run_scip_index(
    target_dir: str | os.PathLike[str], language: str, *, timeout: int = 600
) -> Path:
    """Index ``target_dir`` and return the path to the written ``index.scip``.

    ``language`` is ``"python"``, ``"go"`` or ``"php"``. Raises ``IndexerNotFound``
    naming the missing tool and how to install it when the indexer is absent, and
    ``ValueError`` for an unsupported language. For php the executable ``php`` on
    PATH is necessary but not sufficient -- the standalone scip-php script must
    also be present -- so both are checked.
    """
    if language not in _INDEXERS:
        raise ValueError(
            f"unsupported language {language!r}; expected one of {sorted(_INDEXERS)}"
        )
    executable, install_hint = _INDEXERS[language]
    if shutil.which(executable) is None:
        raise IndexerNotFound(
            f"{executable!r} is required to index a {language} project but was "
            f"not found on PATH. Install it with: {install_hint}"
        )
    if language == "php" and not Path(_scip_php_bin()).is_file():
        raise IndexerNotFound(
            f"the scip-php script was not found at {_scip_php_bin()!r} (set "
            f"{_SCIP_PHP_BIN_ENV} to its path). Install it with: {install_hint}"
        )

    target = Path(target_dir)
    command = _index_command(language, _INDEX_FILENAME)
    env = _index_env(language)
    result = subprocess.run(
        command,
        cwd=str(target),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env={**os.environ, **env} if env else None,
    )
    index_path = target / _INDEX_FILENAME
    # scip-python degrades gracefully and can exit non-zero while still writing a
    # usable index, so the index's existence is the real success signal -- but a
    # non-zero exit with no index is a hard failure worth surfacing loudly.
    if not index_path.exists():
        raise RuntimeError(
            f"{executable} exited {result.returncode} and wrote no {_INDEX_FILENAME} "
            f"in {target}.\nstderr:\n{result.stderr}"
        )
    return index_path


def _locate_scip_cli() -> str:
    """Find the ``scip`` CLI: SCIP_CLI, then the bundled path, then PATH."""
    override = os.environ.get("SCIP_CLI")
    if override:
        candidate = Path(override)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        raise ScipCliNotFound(
            f"SCIP_CLI={override!r} is not an executable file"
        )
    if _BUNDLED_SCIP.is_file() and os.access(_BUNDLED_SCIP, os.X_OK):
        return str(_BUNDLED_SCIP)
    found = shutil.which("scip")
    if found:
        return found
    raise ScipCliNotFound(
        "the `scip` CLI is required to read an index as JSON, but none was "
        f"found. Set SCIP_CLI to a built binary, drop one at {_BUNDLED_SCIP}, "
        "or put `scip` on PATH. Build it with: git clone --depth 1 "
        "https://github.com/sourcegraph/scip.git && cd scip && "
        "go build -o scip ./cmd/scip"
    )


def _run_scip_print(
    index_path: str | os.PathLike[str], scip_cli: str, *, timeout: int = 120
) -> str:
    result = subprocess.run(
        [scip_cli, "print", "--json", str(index_path)],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"`scip print --json {index_path}` exited {result.returncode}: "
            f"{result.stderr}"
        )
    return result.stdout


def read_scip_index(
    index_path: str | os.PathLike[str], *, retain: bool = False
) -> dict:
    """Read ``index_path`` via ``scip print --json`` and return normalized dict.

    Shape::

        {"documents": [
            {"path": str,
             "symbols": [{"symbol", "kind", "display_name"}],
             "occurrences": [{"symbol", "is_definition", "start_line",
                              "start_col", "enclosing_start_line",
                              "enclosing_end_line"}]}
        ]}

    With ``retain=True`` the dict additionally carries everything
    ``normalize_scip_json(..., retain=True)`` retains plus the index identity:
    ``index_digest`` (sha256 of the ``index.scip`` bytes) and
    ``index_digest_kind`` (``"binary"``). The default output is byte-identical
    to before. Raises ``ScipCliNotFound`` when the ``scip`` CLI cannot be located.
    """
    scip_cli = _locate_scip_cli()
    raw = _run_scip_print(index_path, scip_cli)
    if not retain:
        return normalize_scip_json(json.loads(raw))
    # Hash the bytes the CLI actually read, before any caller unlinks them.
    index_digest = hashlib.sha256(Path(index_path).read_bytes()).hexdigest()
    out = normalize_scip_json(json.loads(raw), retain=True)
    out["index_digest"] = index_digest
    out["index_digest_kind"] = INDEX_DIGEST_BINARY
    return out


def index_digest_of_bytes(data: bytes) -> str:
    """The section-29 index identity of a real ``index.scip``: sha256(bytes)."""
    return hashlib.sha256(data).hexdigest()


def json_index_digest(raw: dict) -> str:
    """The section-29 index identity of a checked-in ``scip print --json`` fixture.

    ``sha256("scip-json:" + canonical_json(raw))`` where canonical JSON is
    sorted keys, compact separators, UTF-8 -- the same canonical form the claims
    IR uses for plain JSON data. A fixture has no ``index.scip`` bytes, so this
    is the only stable identity it can carry (``index_digest_kind = "json"``).
    """
    canonical = json.dumps(
        raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256((_JSON_DIGEST_PREFIX + canonical).encode("utf-8")).hexdigest()


def decode_roles(roles: int) -> list[str]:
    """The SymbolRole names set in ``roles``, in bit order."""
    return [name for bit, name in _ROLE_BITS if roles & bit]


def _first(mapping: dict, *keys: str, default: object = None) -> object:
    """Return the first present key's value -- tolerates snake_case (what the
    scip CLI emits) and camelCase (protojson's default) for the same field."""
    for key in keys:
        if key in mapping:
            return mapping[key]
    return default


def _kind_from_suffix(symbol: str) -> str | None:
    """Derive a symbol's kind from its SCIP descriptor suffix.

    The suffix encodes the kind: ``#`` a type/class, ``().`` a method/function,
    a trailing ``.`` a term/field. Used for scip-python, which leaves kind unset.
    """
    stripped = symbol.rstrip()
    if stripped.endswith("#"):
        return "type"
    if stripped.endswith("()."):
        return "method"
    if stripped.endswith("."):
        return "term"
    return None


def _coerce_kind(raw_kind: object, symbol: str) -> str | None:
    """A symbol's kind as a name string map._category can classify, or None.

    The real ``scip print --json`` emits a numeric ``SymbolKind`` (scip-go), the
    hand-authored fixture a name string (``"Struct"``), and scip-python nothing.
    A number is mapped to its enum name; an unset kind (and an unknown number)
    yields the descriptor-suffix kind, which is authoritative for the grammar.
    """
    if raw_kind in _UNSPECIFIED_KIND:
        return _kind_from_suffix(symbol)
    if isinstance(raw_kind, bool):  # guard: bool is an int subclass
        return _kind_from_suffix(symbol)
    if isinstance(raw_kind, int):
        return _SYMBOL_KIND_BY_NUMBER.get(raw_kind) or _kind_from_suffix(symbol)
    return raw_kind


def _normalize_symbol(symbol: dict, *, retain: bool = False) -> dict:
    name = symbol["symbol"]
    raw_kind = _first(symbol, "kind")
    out = {
        "symbol": name,
        "kind": _coerce_kind(raw_kind, name),
        "display_name": _first(symbol, "display_name", "displayName"),
    }
    if retain:
        out["kind_number"] = (
            raw_kind
            if isinstance(raw_kind, int) and not isinstance(raw_kind, bool)
            else None
        )
        out["relationships"] = [
            _normalize_relationship(r)
            for r in (symbol.get("relationships") or [])
        ]
    return out


def _normalize_relationship(rel: dict) -> dict:
    """One SCIP ``Relationship`` with its four flags as plain booleans."""
    return {
        "symbol": rel.get("symbol"),
        "is_reference": bool(_first(rel, "is_reference", "isReference", default=False)),
        "is_implementation": bool(
            _first(rel, "is_implementation", "isImplementation", default=False)
        ),
        "is_type_definition": bool(
            _first(rel, "is_type_definition", "isTypeDefinition", default=False)
        ),
        "is_definition": bool(_first(rel, "is_definition", "isDefinition", default=False)),
    }


def _range_start(rng: list | None) -> tuple[int | None, int | None]:
    """Start (line, col) of a SCIP range. A range is either
    ``[startLine, startCol, endCol]`` (one line) or
    ``[startLine, startCol, endLine, endCol]``; both start the same way."""
    if not rng:
        return None, None
    return rng[0], rng[1]


def _enclosing_span(rng: list | None) -> tuple[int | None, int | None]:
    """(start_line, end_line) of an enclosing range, tolerating the one-line
    three-element form."""
    if not rng:
        return None, None
    end_line = rng[2] if len(rng) >= 4 else rng[0]
    return rng[0], end_line


def _range_end(rng: list | None) -> tuple[int | None, int | None]:
    """End (line, col) of a SCIP range: ``[l, sc, ec]`` ends on ``l``,
    ``[sl, sc, el, ec]`` on ``el``."""
    if not rng:
        return None, None
    if len(rng) >= 4:
        return rng[2], rng[3]
    return rng[0], rng[2]


def _normalize_occurrence(occ: dict, *, retain: bool = False) -> dict:
    roles = _first(occ, "symbol_roles", "symbolRoles", default=0) or 0
    rng = _first(occ, "range")
    start_line, start_col = _range_start(rng)
    enc_start, enc_end = _enclosing_span(
        _first(occ, "enclosing_range", "enclosingRange")
    )
    out = {
        "symbol": occ.get("symbol"),
        "is_definition": bool(roles & _DEFINITION_ROLE),
        "start_line": start_line,
        "start_col": start_col,
        "enclosing_start_line": enc_start,
        "enclosing_end_line": enc_end,
    }
    if retain:
        end_line, end_col = _range_end(rng)
        out["symbol_roles"] = int(roles)
        out["roles"] = decode_roles(int(roles))
        out["end_line"] = end_line
        out["end_col"] = end_col
        # flipped by _synthesize_enclosing when it invents the span
        out["enclosing_synthesized"] = False
    return out


def _is_callable_suffix(symbol: str | None) -> bool:
    """True for a method/function symbol (descriptor tail ``().``)."""
    return bool(symbol) and symbol.rstrip().endswith("().")


def _synthesize_enclosing(occurrences: list[dict]) -> None:
    """Fill in enclosing spans for a dialect (scip-php) that omits them.

    Applies only when the document carries definitions yet not a single
    enclosing range -- so scip-python / scip-go, which supply their own, are
    never touched. Each callable definition is given the span from its own line
    through the document's last line; ``map._innermost_caller`` (latest start
    wins) then attributes an in-body call to the tightest enclosing method. A
    non-callable definition (class, field, parameter) is left without a span so
    it can never swallow a call. Mutates ``occurrences`` in place and returns
    whether any span was synthesized; a retained occurrence (one carrying the
    ``enclosing_synthesized`` key) has that flag flipped so a consumer can tell
    an indexer-supplied span from an invented one.
    """
    definitions = [o for o in occurrences if o["is_definition"]]
    if not definitions:
        return False
    if any(o["enclosing_start_line"] is not None for o in occurrences):
        return False
    lines = [o["start_line"] for o in occurrences if o["start_line"] is not None]
    if not lines:
        return False
    doc_end = max(lines)
    synthesized = False
    for occ in definitions:
        if occ["start_line"] is None or not _is_callable_suffix(occ["symbol"]):
            continue
        occ["enclosing_start_line"] = occ["start_line"]
        occ["enclosing_end_line"] = doc_end
        if "enclosing_synthesized" in occ:
            occ["enclosing_synthesized"] = True
        synthesized = True
    return synthesized


def _normalize_metadata(meta: dict | None) -> dict:
    """The indexer identity a retained index carries (section 29 ``scip_index``)."""
    meta = meta or {}
    tool = _first(meta, "tool_info", "toolInfo", default={}) or {}
    return {
        "tool_name": tool.get("name"),
        "tool_version": tool.get("version"),
        "arguments": list(tool.get("arguments") or []),
        "project_root": _first(meta, "project_root", "projectRoot"),
        "text_document_encoding": _first(
            meta, "text_document_encoding", "textDocumentEncoding"
        ),
    }


def normalize_scip_json(doc: dict, *, retain: bool = False) -> dict:
    """Normalize a ``scip print --json`` document into capcov's shape.

    Pure over its input: no filesystem, no tools -- which is what lets the test
    suite exercise it against a checked-in sample. Reconciles the per-indexer
    dialects: numeric kinds are named (see ``_coerce_kind``) and a missing
    enclosing range (scip-php) is synthesized (see ``_synthesize_enclosing``).

    ``retain=False`` (the default) is byte-identical to the historical output.
    ``retain=True`` keeps what the static fact exporter needs and the default
    shape discards: top-level ``metadata`` (tool name/version/arguments, project
    root, text-document encoding) and ``external_symbols``; per document
    ``language`` and ``enclosing_synthesized``; per occurrence the raw
    ``symbol_roles``, the decoded ``roles`` list, ``end_line``/``end_col`` and
    ``enclosing_synthesized``; per symbol ``relationships`` and ``kind_number``.
    Every default key keeps its default value, so the retained dict minus the
    new keys equals the default dict.
    """
    documents = []
    for document in doc.get("documents", []):
        occurrences = [
            _normalize_occurrence(o, retain=retain)
            for o in document.get("occurrences", [])
        ]
        synthesized = _synthesize_enclosing(occurrences)
        entry = {
            "path": _first(document, "relative_path", "relativePath", "path"),
            "symbols": [
                _normalize_symbol(s, retain=retain)
                for s in document.get("symbols", [])
            ],
            "occurrences": occurrences,
        }
        if retain:
            entry["language"] = document.get("language")
            entry["enclosing_synthesized"] = synthesized
        documents.append(entry)
    out = {"documents": documents}
    if retain:
        out["metadata"] = _normalize_metadata(doc.get("metadata"))
        out["external_symbols"] = [
            _normalize_symbol(s, retain=True)
            for s in (_first(doc, "external_symbols", "externalSymbols", default=[]) or [])
        ]
    return out
