"""Export a retained SCIP index as claim facts + evidence (section 29).

SCIP is a *producer of static observations*, never an oracle. This module turns
one retained index -- ``runner.read_scip_index(path, retain=True)`` or
``runner.normalize_scip_json(raw, retain=True)`` -- plus the tree-sitter side's
raw discover dict into a validated claims ``Bundle`` whose every fact carries
the same ``index`` digest context, whose every fact has an ``Evidence`` record
with a content-derived id and an explicit ``depends_on`` chain, and whose
completeness witnesses are emitted only when their audited emission conditions
hold. The bundle's ``digest()`` is a pure function of the inputs: permuting the
documents, occurrences or symbols of the index changes nothing.

IDENTITY
--------
``index`` is a content digest of the normalized relations the exporter emits,
not of the index file: scip-go writes per-document ``symbols`` in Go map order,
so two indexings of one tree produce different ``index.scip`` bytes, and a
digest of those bytes is not reproducible (section 28). The recipe
(``INDEX_IDENTITY = "static-relations-v1"``) is

    index = sha256("static-relations-v1:" + canonical_json({
        "relations": sorted(<every relation name the bundle declares>),
        "rows": {relation: sorted(canonical rows with the ``index`` column
                                  removed) for every exported primitive
                 relation with at least one row, except the compatibility
                 relations (``index_describes_run``, ``scip_index_comparable``)}}))

Compatibility relations bind the index to *other* contexts (a run, another
index) and are supplied by the consumer at claim time, so they are keyed by the
identity rather than part of it: declaring that an index describes ``run-1``
does not make it a different index.

Every fact is first built under a placeholder index, the identity is computed
from the finished rows, then every ``index`` column is set to it and the
evidence ids (which embed ``index[:12]`` and the row digest) are computed --
content digest, then index, then ids. Two exports of the same normalized
content therefore carry the same ``index``, the same evidence ids and the same
``bundle_digest`` whatever the indexer's emission order and whichever copy of
the tree was indexed. The digest of the index file the caller read
(``sha256(index.scip bytes)``, kind ``binary``, or ``sha256("scip-json:" +
canonical_json(raw))`` for a checked-in fixture, kind ``json``) is a *run
receipt*: it is reported in ``ExportResult.messages`` and carried in bundle
metadata as ``index_file_digest`` / ``index_file_digest_kind``, and
``bundle_digest`` excludes those keys (``RECEIPT_METADATA_KEYS``, with ``out_of_tree_documents``) so the
receipt never becomes identity. ``scip_index.digest_kind`` carries the literal
``"static-relations-v1"``. The tree-sitter side is bound to the same ``index``
through ``scip_index_tree``: the exporter hashes the language-scoped source
tree (``artifacts.patterns_for(language)``) and, when ``ast_raw`` carries the
``tree_digest`` it was computed over, refuses to export (``status = "stale"``)
unless the two agree.

LINE FRAME
----------
Every ``line`` / ``start_line`` / ``end_line`` column in the bundle is 1-based
-- the frame the tree-sitter side, ``resolve.scip_defs_by_location`` and every
human-facing capcov line use. SCIP delivers 0-based lines and the runner passes
them through unmodified; the exporter lifts them here, once, so
``route_handler_location(IX,S,F,L)`` joins ``scip_definition_site(IX,F,L,Sym)``
and ``static_op_site`` joins ``static_site_owner`` without a per-rule shift.
Recorded in bundle metadata as ``line_frame``.

EVIDENCE
--------
Ids are ``scip:<index12>:<relation>:<row12>`` for SCIP-derived relations
(``scip_*``) and ``static:<index12>:<relation>:<row12>`` for the tree-sitter and
identity side, ``row12 = sha256(canonical_json([relation, row]))[:12]``.
``Evidence.source`` is the producer string: the indexer (``scip-go 0.2.7``) for
what the indexer said, ``treesitter-routes <lang>`` for the tree-sitter side,
``capcov.claims.static.scip_facts v1`` for exporter-computed rows, and
``capcov.claims.static.scip_facts <predicate>-v1`` for each completeness
witness, naming the predicate whose conditions were checked. Raw occurrences
are never exported in the slice profile; a row derived from one depends on
``external:scip-occurrence:<index12>:<occ12>`` where ``occ12`` is the prefix of
the occurrence's content digest (path, symbol, range, roles) -- the same digest
carried in full in the ``occurrence`` column of reference relations.

AST_RAW CONTRACT
----------------
``ast_raw`` is the deep tree-sitter adapter's dict (``deep_core.build_deep_dict``
optionally folded by ``resolve.hybrid_raw``). The keys read, all optional:
``surfaces`` (``{id, handler, handler_symbol, file, line}``), ``_node_locations``
(``{node: [file, line]}``), ``excluded_surfaces`` (``{"surfaces": [...]}`` or a
list of ``{method, path, reason}``), ``entities`` (``{name, file, line, table?}``),
``op_sites`` (``{file, line, entity, crud}`` -- the recognizer's output; the deep
dict itself keeps only the node-keyed ``_ops`` projection, so a caller wanting
``static_op_site`` rows passes the sites through), ``blind_spots``
(``{file, line, kind, reason}``), ``scip_residue`` (``resolve._residue``'s
``{file, line, callee, kind?}``), ``call_sites`` (``{file, line, callee}``, used
for ``static_site_owner`` only in the full profile), ``unresolved``
(``{kind, node?/handler?/id?, source{file,line}?}``) and ``tree_digest``. The
call-site census counts as *available* only when both ``scip_residue`` and
``blind_spots`` are present; without it no ``scip_references_closed`` witness
is emitted, because "no residue" cannot be asserted about a census nobody took.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from ... import artifacts
from ...scip import map as scip_map
from ...scip import resolve, runner
from ..ir import (Atom, Bundle, Column, Constant, Context, Evidence,
                  RelationDecl, TypeName, canonical_json, digest as ir_digest)
from ..validation import ValidationError, assert_valid
from . import load_static_schema
from .runtime_relations import (RUNTIME_ROUTE_OBSERVED, RUNTIME_ROUTE_REACHES_SQL,
                               TRACE_PRIMITIVE_DECLS)

EXPORT_VERSION = "v1"
EXPORTER = "capcov.claims.static.scip_facts"
PRODUCER = f"{EXPORTER} {EXPORT_VERSION}"

STATUS_COMPLETE = "complete"
STATUS_RESOURCE_EXHAUSTED = "resource-exhausted"
STATUS_INVALID_INPUT = "invalid-input"
STATUS_STALE = "stale"

PROFILE_SLICE = "slice"
PROFILE_FULL = "full"
FULL_PROFILE_MAX_DOCUMENTS = 50

SCOPE_ALL = "all"
SCOPE_PACKAGE_PREFIX = "package_prefix"
SCOPE_DOCUMENT_SET = "document_set"
_SCOPE_KINDS = (SCOPE_ALL, SCOPE_PACKAGE_PREFIX, SCOPE_DOCUMENT_SET)

LINE_FRAME = "1-based"
_SCIP_LINE_IS_ZERO_BASED = 1

# The index identity scheme (module docstring, IDENTITY): the value of
# ``scip_index.digest_kind`` and of metadata ``index_digest_kind``.
INDEX_IDENTITY = "static-relations-v1"
_IDENTITY_PREFIX = INDEX_IDENTITY + ":"
# Facts are assembled under this index and re-keyed once the identity is
# known; it never appears in an exported bundle.
_PLACEHOLDER_INDEX = "0" * 64
# Run receipts carried in bundle metadata but excluded from ``bundle_digest``.
RECEIPT_METADATA_KEYS = ("index_file_digest", "index_file_digest_kind", "out_of_tree_documents")

# The witnesses' predicate versions. Each Evidence.source names one of these so
# a reviewer knows which emission contract was checked.
WITNESS_DOCUMENTS = "documents-closed-v1"
WITNESS_DEFINITIONS = "definitions-closed-v1"
WITNESS_REFERENCES = "references-closed-v1"
WITNESS_ROUTE_INVENTORY = "route-inventory-closed-v1"
WITNESS_SCOPE = "scope-closed-v1"
WITNESS_REACHABILITY = "reachability-closed-v1"

# Occurrence role -> the section-29 role relation it populates.
_ROLE_RELATIONS = {
    "generated": "scip_generated_site",
    "test": "scip_test_site",
    "import": "scip_import_site",
    "write": "scip_write_site",
    "read": "scip_read_site",
}

# Relationship flag -> scip_relationship.kind.
_RELATIONSHIP_KINDS = (
    ("is_reference", "reference"),
    ("is_implementation", "implementation"),
    ("is_type_definition", "type_definition"),
    ("is_definition", "definition"),
)

# Relations the frozen primitive schema points at but does not declare: the
# ``completes`` targets of the completeness witnesses (derived projections and
# ``static_reaches``), the ``forall`` domain ``static_route_declared_surface``
# and the runtime primitive ``runtime_route_observed`` named by
# ``index_describes_run``.  They are declared here as rule-less stubs only so an
# exported bundle validates on its own; the rule pack
# (``experiments/claim-semantics/static/rules-static-v1.json``) owns their rules
# and declares them again, byte-identically.  ``combine.combine`` merges the two
# declaration sets by name and refuses a non-identical duplicate, so the stubs
# never widen or narrow what the pack says (section 29, reconciliation item 2).
# ``scip_definition_site_at`` is the (index, path, line) projection the
# per-document witness ``scip_definitions_closed`` closes.
_DERIVED_TARGET_DECLS = (
    RelationDecl("scip_document_path",
                 (Column("index", "digest", True), Column("path", "symbol")),
                 modality="derived", binding="static", primitive=False,
                 context_indices=("index",)),
    RelationDecl("scip_definition_site_at",
                 (Column("index", "digest", True), Column("path", "symbol"),
                  Column("line", "unsigned")),
                 modality="derived", binding="static", primitive=False,
                 context_indices=("index",)),
    RelationDecl("static_route_declared_surface",
                 (Column("index", "digest", True), Column("surface", "symbol")),
                 modality="derived", binding="static", primitive=False, finite=True,
                 nonempty=True, context_indices=("index",)),
    RelationDecl("static_reaches",
                 (Column("index", "digest", True), Column("src", "symbol"),
                  Column("dst", "symbol")),
                 modality="derived", binding="static", primitive=False,
                 context_indices=("index",)),
    RUNTIME_ROUTE_OBSERVED,
    *TRACE_PRIMITIVE_DECLS,
    RUNTIME_ROUTE_REACHES_SQL,
)
STUB_RELATIONS = frozenset(decl.name for decl in _DERIVED_TARGET_DECLS)


# ---------------------------------------------------------------------------
# Public parameter / result types


@dataclass(frozen=True)
class Scope:
    """What part of the index is exported at occurrence granularity.

    ``all`` exports every document; ``package_prefix`` the documents whose path
    or whose defining package (via the language normalizer) starts with
    ``values[0]``; ``document_set`` exactly the listed paths. Document-level rows
    (``scip_document``) are always exported for every document of the indexed
    tree, whatever the scope, so ``scip_documents_closed`` stays truthful (a
    document whose path escapes the tree is a receipt, not a fact -- see
    ``export_bundle``); symbols, occurrence-derived rows and per-document
    witnesses follow the scope.
    """
    kind: str = SCOPE_ALL
    values: tuple[str, ...] = ()

    @classmethod
    def all(cls) -> "Scope":
        return cls(SCOPE_ALL, ())

    @classmethod
    def package_prefix(cls, prefix: str) -> "Scope":
        return cls(SCOPE_PACKAGE_PREFIX, (prefix,))

    @classmethod
    def document_set(cls, paths: Iterable[str]) -> "Scope":
        return cls(SCOPE_DOCUMENT_SET, tuple(sorted(set(paths))))

    def rows(self) -> list[str]:
        if self.kind == SCOPE_ALL:
            return ["*"]
        return list(self.values)


@dataclass(frozen=True)
class ExportLimits:
    """Finite execution bars. Tripping one is ``resource-exhausted``, never an
    exception and never silently raised. ``rows`` mirrors the Soufflé kernel's
    100k-row cap over every relation, inputs included."""
    documents: int = 5_000
    occurrences: int = 200_000
    rows: int = 100_000


@dataclass(frozen=True)
class ExportResult:
    status: str
    bundle: Bundle | None
    counts: dict[str, int] = field(default_factory=dict)
    messages: tuple[str, ...] = ()


class ExportInputError(ValueError):
    """An exporter-internal invariant was broken while assembling rows."""


# ---------------------------------------------------------------------------
# Identity helpers


def evidence_id(index_digest: str, relation: str, row: list[Any]) -> str:
    """``scip:`` / ``static:`` ``<index12>:<relation>:<row12>``."""
    prefix = "scip" if relation.startswith("scip_") else "static"
    return f"{prefix}:{index_digest[:12]}:{relation}:{row_digest(relation, row)[:12]}"


def row_digest(relation: str, row: list[Any]) -> str:
    return hashlib.sha256(canonical_json([relation, list(row)]).encode("utf-8")).hexdigest()


def occurrence_digest(path: str, occ: dict) -> str:
    """Content identity of one occurrence: path, symbol, full range, roles."""
    payload = {
        "path": path,
        "symbol": occ.get("symbol"),
        "range": [occ.get("start_line"), occ.get("start_col"),
                  occ.get("end_line"), occ.get("end_col")],
        "symbol_roles": _occurrence_roles_int(occ),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def occurrence_external_id(index_digest: str, occ_digest: str) -> str:
    return f"external:scip-occurrence:{index_digest[:12]}:{occ_digest[:12]}"


_OCCURRENCE_PREFIX = "external:scip-occurrence:"


def static_relations_index(rows_by_relation: Mapping[str, Iterable[list[Any]]],
                           relation_names: Iterable[str]) -> str:
    """The ``static-relations-v1`` identity of a set of exported rows.

    ``rows_by_relation`` maps a relation to its rows *without* the ``index``
    column; only relations with at least one row take part. Row order and
    relation order do not matter; the declared relation names do.
    """
    payload = {
        "relations": sorted(set(relation_names)),
        "rows": {
            relation: sorted((list(row) for row in rows), key=canonical_json)
            for relation, rows in rows_by_relation.items() if rows
        },
    }
    return hashlib.sha256((_IDENTITY_PREFIX + canonical_json(payload)).encode("utf-8")).hexdigest()


def _occurrence_roles_int(occ: dict) -> int:
    roles = occ.get("symbol_roles")
    if isinstance(roles, int) and not isinstance(roles, bool):
        return roles
    return runner._DEFINITION_ROLE if occ.get("is_definition") else 0


def _occurrence_roles(occ: dict) -> list[str]:
    roles = occ.get("roles")
    if isinstance(roles, list):
        return roles
    return runner.decode_roles(_occurrence_roles_int(occ))


def static_relations() -> tuple[RelationDecl, ...]:
    """The frozen primitive declarations plus the derived-target stubs."""
    frozen = tuple(_relation_from_json(item) for item in load_static_schema()["relations"])
    return frozen + _DERIVED_TARGET_DECLS


def primitive_relations() -> tuple[RelationDecl, ...]:
    """The frozen primitive declarations alone (what the exporter produces rows for)."""
    return tuple(_relation_from_json(item) for item in load_static_schema()["relations"])


def canonical_project_root(reported: Any) -> str:
    """The host-independent ``scip_index.project_root`` value.

    Indexers report the project root as an absolute ``file://`` URI of the
    directory they ran in, which differs per machine and per copy and would make
    the bundle digest of one and the same index unreproducible.  The fact keeps
    only ``file:///<basename>`` of that directory (or ``""`` when the index
    carries no root, as the canonicalized JSON golden does); the reported value
    is surfaced in ``ExportResult.messages`` instead (section 29, item 3).
    """
    if not isinstance(reported, str) or not reported:
        return ""
    path = reported
    if "://" in path:
        path = path.split("://", 1)[1]
    basename = path.rstrip("/").rsplit("/", 1)[-1]
    return f"file:///{basename}" if basename else ""


def _relation_from_json(raw: dict) -> RelationDecl:
    return RelationDecl(
        raw["name"],
        tuple(Column(c["name"], c["type"], c["context"]) for c in raw["columns"]),
        raw["modality"], raw["polarity"], raw["binding"], raw["primitive"],
        tuple(raw["producer_classes"]), tuple(raw["context_indices"]), raw["completes"],
        raw["finite"], raw["nonempty"], tuple(raw["compatibility_targets"]),
        tuple(raw["compatibility_context_indices"]),
    )


# ---------------------------------------------------------------------------
# Row accumulator


class _Facts:
    """Deduplicating fact/evidence accumulator.

    A row is identified by ``(relation, values)``; adding it twice merges the
    dependency sets (the union is sorted by ``Evidence``), so two raw occurrences
    that yield one row yield one fact with one content-derived evidence id.
    """

    def __init__(self, index_digest: str, relations: dict[str, RelationDecl]) -> None:
        self.index_digest = index_digest
        self.relations = relations
        self.rows: dict[tuple[str, tuple], dict] = {}

    def add(self, relation: str, values: dict[str, Any], *, source: str,
            depends_on: Iterable[str] = (), kind: str = "fact") -> str:
        decl = self.relations[relation]
        row: list[Any] = []
        for column in decl.columns:
            if column.name not in values:
                raise ExportInputError(f"{relation}: missing column {column.name!r}")
            row.append(_checked(relation, column, values[column.name]))
        key = (relation, tuple(row))
        eid = evidence_id(self.index_digest, relation, row)
        entry = self.rows.get(key)
        if entry is None:
            entry = {"id": eid, "row": row, "source": source, "kind": kind,
                     "depends_on": set()}
            self.rows[key] = entry
        entry["depends_on"].update(d for d in depends_on if d and d != eid)
        return eid

    def count(self, relation: str) -> int:
        return sum(1 for rel, _ in self.rows if rel == relation)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for rel, _ in self.rows:
            out[rel] = out.get(rel, 0) + 1
        return dict(sorted(out.items()))

    def __len__(self) -> int:
        return len(self.rows)

    def _index_positions(self, relation: str) -> list[int]:
        return [i for i, column in enumerate(self.relations[relation].columns)
                if column.name == "index" and column.type == TypeName.DIGEST]

    def identity(self) -> str:
        """``static_relations_index`` over every accumulated row of a
        non-compatibility relation (module docstring, IDENTITY)."""
        by_relation: dict[str, list[list[Any]]] = {}
        for (relation, _), entry in self.rows.items():
            if self.relations[relation].modality.value == "compatibility":
                continue
            skip = set(self._index_positions(relation))
            by_relation.setdefault(relation, []).append(
                [v for i, v in enumerate(entry["row"]) if i not in skip])
        return static_relations_index(by_relation, self.relations)

    def rebase(self, index: str) -> None:
        """Set every ``index`` column to ``index`` and recompute evidence ids.

        ``depends_on`` entries are rewritten through the old->new id map;
        ``external:scip-occurrence:<old12>:<occ12>`` references are re-prefixed
        and every other external id is left alone.
        """
        old12, new12 = self.index_digest[:12], index[:12]
        rename: dict[str, str] = {}
        rebased: dict[tuple[str, tuple], dict] = {}
        for (relation, _), entry in self.rows.items():
            row = list(entry["row"])
            for i in self._index_positions(relation):
                row[i] = index
            eid = evidence_id(index, relation, row)
            rename[entry["id"]] = eid
            rebased[(relation, tuple(row))] = {**entry, "id": eid, "row": row}
        occ_old = f"{_OCCURRENCE_PREFIX}{old12}:"
        occ_new = f"{_OCCURRENCE_PREFIX}{new12}:"

        def remap(dep: str) -> str:
            if dep in rename:
                return rename[dep]
            if dep.startswith(occ_old):
                return occ_new + dep[len(occ_old):]
            return dep

        for entry in rebased.values():
            entry["depends_on"] = {remap(d) for d in entry["depends_on"]} - {entry["id"]}
        self.rows = rebased
        self.index_digest = index

    def materialize(self) -> tuple[tuple[Atom, ...], tuple[Evidence, ...]]:
        facts: list[Atom] = []
        evidence: list[Evidence] = []
        for (relation, _), entry in self.rows.items():
            decl = self.relations[relation]
            atom = Atom(relation, tuple(
                Constant(value, column.type)
                for value, column in zip(entry["row"], decl.columns)))
            context = {
                name: entry["row"][[c.name for c in decl.columns].index(name)]
                for name in decl.context_indices
            }
            facts.append(atom)
            evidence.append(Evidence(
                entry["id"], atom, Context.from_mapping(context), entry["source"],
                tuple(sorted(entry["depends_on"])), entry["kind"]))
        return tuple(facts), tuple(evidence)


def _checked(relation: str, column: Column, value: Any) -> Any:
    t = column.type
    if t in (TypeName.SYMBOL, TypeName.DIGEST):
        if not isinstance(value, str):
            raise ExportInputError(f"{relation}.{column.name}: expected str, got {value!r}")
        return value
    if t == TypeName.UNSIGNED:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ExportInputError(f"{relation}.{column.name}: expected unsigned, got {value!r}")
        return value
    if t == TypeName.BOOLEAN:
        if not isinstance(value, bool):
            raise ExportInputError(f"{relation}.{column.name}: expected bool, got {value!r}")
        return value
    raise ExportInputError(f"{relation}.{column.name}: unsupported column type {t}")


# ---------------------------------------------------------------------------
# The exporter


def export_bundle(
    normalized_retained: dict,
    *,
    ast_raw: dict | None,
    source_root: str | Path,
    language: str,
    scope: Scope | None = None,
    profile: str = PROFILE_SLICE,
    limits: ExportLimits | None = None,
    index_digest: str,
    index_digest_kind: str,
    commit: str | None = None,
    describes_runs: Iterable[str] = (),
) -> ExportResult:
    """Export one retained index as a validated static-facts ``Bundle``.

    Pure over its inputs: no subprocesses, no index file; the only filesystem
    read is the language-scoped tree walk that produces ``scip_index_tree`` and
    ``static_source_file``. Returns ``ExportResult`` with status ``complete``
    (bundle present), ``resource-exhausted`` (a limit tripped; no bundle),
    ``stale`` (``ast_raw.tree_digest`` disagrees with the tree; no bundle) or
    ``invalid-input`` (an input precondition or bundle validation failed; no
    bundle). Never raises for a limit.
    """
    scope = scope or Scope.all()
    limits = limits or ExportLimits()
    ast_raw = ast_raw or {}
    messages: list[str] = []

    problem = _input_problem(normalized_retained, scope, profile, index_digest,
                             index_digest_kind, language)
    if problem:
        return ExportResult(STATUS_INVALID_INPUT, None, {}, (problem,))

    documents = normalized_retained.get("documents", []) or []
    # A document whose path escapes the indexed tree is not a document of that
    # tree.  scip-go emits Go's generated ``_testmain.go`` for every ``pkg.test``
    # package it loads, read out of GOCACHE and spelled relative to the project
    # root (``../../<cache>/<hash>-d``): none of the target's source, and a path
    # that names the build cache's per-run location.  Such documents contribute
    # to no fact -- the identity rows in particular -- and are recorded as a
    # receipt (``out_of_tree_documents``, excluded from ``bundle_digest``).
    out_of_tree = [d for d in documents if _escapes_tree(d.get("path"))]
    if out_of_tree:
        documents = [d for d in documents if not _escapes_tree(d.get("path"))]
        messages.append(f"{len(out_of_tree)} document(s) outside the indexed tree are not exported "
                        "(paths escaping the project root; recorded as metadata out_of_tree_documents)")
    occurrence_total = sum(len(d.get("occurrences", []) or []) for d in documents)
    if len(documents) > limits.documents:
        return ExportResult(STATUS_RESOURCE_EXHAUSTED, None,
                            {"documents": len(documents), "occurrences": occurrence_total},
                            (f"documents {len(documents)} exceed limit {limits.documents}",))
    if occurrence_total > limits.occurrences:
        return ExportResult(STATUS_RESOURCE_EXHAUSTED, None,
                            {"documents": len(documents), "occurrences": occurrence_total},
                            (f"occurrences {occurrence_total} exceed limit {limits.occurrences}",))

    # --- tree identity ------------------------------------------------------
    patterns = artifacts.patterns_for(language)
    manifest = artifacts.tree_manifest(Path(source_root), patterns)
    tree_digest = artifacts.manifest_sha256(manifest)
    expected_tree = ast_raw.get("tree_digest")
    if expected_tree is not None and expected_tree != tree_digest:
        return ExportResult(STATUS_STALE, None, {},
                            (f"ast_raw was computed over tree {expected_tree[:12]} but the "
                             f"source root hashes to {tree_digest[:12]} over {patterns}",))

    relations = {decl.name: decl for decl in static_relations()}
    # Facts are keyed by a placeholder until the content identity is known
    # (module docstring, IDENTITY); ``index_digest`` is the file receipt.
    facts = _Facts(_PLACEHOLDER_INDEX, relations)
    ix = {"index": _PLACEHOLDER_INDEX}
    meta = normalized_retained.get("metadata") or {}
    indexer = meta.get("tool_name") or "unknown"
    indexer_version = meta.get("tool_version") or "unknown"
    indexer_source = f"{indexer} {indexer_version}"
    treesitter_source = f"treesitter-routes {language}"
    to_node = resolve.normalizer(language)

    # --- identity rows -------------------------------------------------------
    project_root = canonical_project_root(meta.get("project_root"))
    if meta.get("project_root") and project_root != meta.get("project_root"):
        messages.append(f"scip_index.project_root recorded as {project_root!r}; the indexer "
                        f"reported {meta.get('project_root')!r} (host path, not a fact)")
    index_eid = facts.add("scip_index", {
        **ix, "indexer": indexer, "indexer_version": indexer_version,
        "language": language, "project_root": project_root,
        "digest_kind": INDEX_IDENTITY,
    }, source=indexer_source)
    tree_eid = facts.add("scip_index_tree", {
        **ix, "tree_digest": tree_digest, "file_count": len(manifest),
        "pattern": " ".join(patterns),
    }, source=PRODUCER, depends_on=[index_eid])
    if commit:
        facts.add("scip_index_commit", {**ix, "commit": commit}, source=PRODUCER,
                  depends_on=[index_eid, f"external:git-commit:{commit}"])
    for value in scope.rows():
        facts.add("static_scope", {**ix, "scope_kind": scope.kind, "scope_value": value},
                  source=PRODUCER, depends_on=[index_eid])
    facts.add("static_language_covered", {**ix, "language": language},
              source=indexer_source, depends_on=[index_eid])
    for rel_path, _sha in manifest:
        facts.add("static_source_file", {**ix, "path": rel_path, "language": language},
                  source=PRODUCER, depends_on=[tree_eid])
    for run in sorted(set(describes_runs)):
        facts.add("index_describes_run", {**ix, "run": run}, source=PRODUCER,
                  depends_on=[index_eid, f"external:run:{run}"])

    # --- whole-index tables --------------------------------------------------
    table = scip_map._symbol_table(normalized_retained)
    def_sites: dict[str, set[tuple[str, int]]] = {}
    def_paths: dict[str, set[str]] = {}
    def_occurrence_ids: dict[str, set[str]] = {}
    for document in documents:
        path = document.get("path")
        for occ in document.get("occurrences", []) or []:
            if not occ.get("is_definition") or occ.get("start_line") is None:
                continue
            symbol = occ.get("symbol")
            if not symbol or path is None:
                continue
            def_sites.setdefault(symbol, set()).add((path, occ["start_line"]))
            def_paths.setdefault(symbol, set()).add(path)
            def_occurrence_ids.setdefault(symbol, set()).add(
                occurrence_external_id(_PLACEHOLDER_INDEX, occurrence_digest(path, occ)))
    # A symbol defined at two distinct sites is ambiguous (case 09). Category
    # "other" is excluded: scip-go defines the namespace (package) symbol in
    # every file of the package and ``local N`` symbols are file-scoped, which is
    # the language, not an ambiguity. The rule pack's scip_duplicate_definition
    # applies the same predicate (scip_symbol category != "other"), so the
    # witness policy here and the derived relation agree.
    duplicate_symbols = {
        symbol for symbol, sites in def_sites.items()
        if len(sites) > 1 and _symbol_category(symbol, table) != "other"
    }

    in_scope = {
        d.get("path") for d in documents
        if d.get("path") is not None and _in_scope(d, scope, to_node)
    }

    # --- documents (all) -----------------------------------------------------
    doc_eids: dict[str, str] = {}
    doc_language: dict[str, str] = {}
    doc_synthesized: dict[str, bool] = {}
    doc_has_duplicate: dict[str, bool] = {}
    doc_occurrences: dict[str, int] = {}
    for document in documents:
        path = document.get("path")
        if path is None:
            messages.append("a document without a path was skipped")
            continue
        occurrences = document.get("occurrences", []) or []
        symbols = document.get("symbols", []) or []
        doc_lang = document.get("language") or language
        synthesized = bool(document.get("enclosing_synthesized", False))
        doc_language[path] = doc_lang
        doc_synthesized[path] = synthesized
        doc_occurrences[path] = len(occurrences)
        doc_has_duplicate[path] = any(
            o.get("is_definition") and o.get("symbol") in duplicate_symbols
            for o in occurrences)
        doc_eids[path] = facts.add("scip_document", {
            **ix, "path": path, "language": doc_lang, "occurrence_count": len(occurrences),
            "symbol_count": len(symbols), "enclosing_synthesized": synthesized,
        }, source=indexer_source, depends_on=[index_eid])

    # --- symbols and occurrences (in scope) ----------------------------------
    # symbol -> every evidence id an exported row mentioned it under. The full
    # set (not the first writer) keeps depends_on -- and so the bundle digest --
    # independent of document order.
    mentioned: dict[str, set[str]] = {}

    def mention(symbol: str, under: str) -> None:
        mentioned.setdefault(symbol, set()).add(under)

    for document in documents:
        path = document.get("path")
        if path not in in_scope:
            continue
        doc_eid = doc_eids[path]
        for sym in document.get("symbols", []) or []:
            name = sym.get("symbol")
            if not name:
                continue
            _add_symbol(facts, ix, name, sym, table, indexer_source, [doc_eid])
            mention(name, doc_eid)
            for rel in sym.get("relationships") or []:
                related = rel.get("symbol")
                if not related:
                    continue
                for flag, kind in _RELATIONSHIP_KINDS:
                    if rel.get(flag):
                        facts.add("scip_relationship", {
                            **ix, "symbol": name, "related": related, "kind": kind,
                        }, source=indexer_source, depends_on=[doc_eid])
                        mention(related, doc_eid)
        for occ in document.get("occurrences", []) or []:
            symbol = occ.get("symbol")
            line0 = occ.get("start_line")
            if not symbol or line0 is None:
                continue
            line = line0 + _SCIP_LINE_IS_ZERO_BASED
            occ_ext = occurrence_external_id(_PLACEHOLDER_INDEX, occurrence_digest(path, occ))
            roles = _occurrence_roles(occ)
            deps = [doc_eid, occ_ext]
            if occ.get("is_definition") or "definition" in roles:
                facts.add("scip_definition_site",
                          {**ix, "path": path, "line": line, "symbol": symbol},
                          source=indexer_source, depends_on=deps)
                mention(symbol, doc_eid)
                if occ.get("enclosing_start_line") is not None and occ.get("enclosing_end_line") is not None:
                    facts.add("scip_enclosing", {
                        **ix, "symbol": symbol, "path": path,
                        "start_line": occ["enclosing_start_line"] + _SCIP_LINE_IS_ZERO_BASED,
                        "end_line": occ["enclosing_end_line"] + _SCIP_LINE_IS_ZERO_BASED,
                        "synthesized": bool(occ.get("enclosing_synthesized", False)),
                    }, source=indexer_source, depends_on=deps)
            for role in roles:
                relation = _ROLE_RELATIONS.get(role)
                if relation:
                    facts.add(relation, {**ix, "path": path, "line": line, "symbol": symbol},
                              source=indexer_source, depends_on=deps)

    # --- reference graph (in scope) ------------------------------------------
    leaks: dict[tuple[str, str], None] = {}
    for edge in scip_map.reference_edges(normalized_retained, "callable"):
        path = edge["file"]
        if path not in in_scope or edge["line"] is None:
            continue
        occ = edge["occurrence"]
        occ_digest = occurrence_digest(path, occ)
        occ_ext = occurrence_external_id(_PLACEHOLDER_INDEX, occ_digest)
        line = edge["line"] + _SCIP_LINE_IS_ZERO_BASED
        callee = edge["symbol"]
        doc_eid = doc_eids[path]
        mention(callee, doc_eid)
        if edge["owner"] is None:
            facts.add("scip_module_scope_reference", {
                **ix, "callee": callee, "path": path, "line": line, "occurrence": occ_digest,
            }, source=indexer_source, depends_on=[doc_eid, occ_ext])
        else:
            caller = edge["owner"]
            mention(caller, doc_eid)
            facts.add("scip_may_reference", {
                **ix, "caller": caller, "callee": callee, "path": path, "line": line,
                "occurrence": occ_digest, "caller_synthesized": bool(edge["owner_synthesized"]),
            }, source=indexer_source,
                depends_on=[doc_eid, occ_ext, *sorted(def_occurrence_ids.get(caller, ()))])
        # first-party code outside the slice: the callee is defined in a
        # document of this index that the scope excludes.
        for defined_in in sorted(def_paths.get(callee, ())):
            if defined_in not in in_scope:
                leaks[(callee, defined_in)] = None

    type_refs_at_module_scope = 0
    for edge in scip_map.reference_edges(normalized_retained, "type"):
        path = edge["file"]
        if path not in in_scope or edge["line"] is None:
            continue
        if edge["owner"] is None:
            type_refs_at_module_scope += 1
            continue
        occ_digest = occurrence_digest(path, edge["occurrence"])
        doc_eid = doc_eids[path]
        referrer, type_symbol = edge["owner"], edge["symbol"]
        mention(referrer, doc_eid)
        mention(type_symbol, doc_eid)
        facts.add("scip_type_reference", {
            **ix, "referrer": referrer, "type_symbol": type_symbol, "path": path,
            "line": edge["line"] + _SCIP_LINE_IS_ZERO_BASED, "occurrence": occ_digest,
        }, source=indexer_source,
            depends_on=[doc_eid, occurrence_external_id(_PLACEHOLDER_INDEX, occ_digest),
                        *sorted(def_occurrence_ids.get(referrer, ()))])
    if type_refs_at_module_scope:
        messages.append(
            f"{type_refs_at_module_scope} type reference(s) at module scope have no "
            "referrer symbol and no section-29 relation; not exported")

    for symbol, defined_in in sorted(leaks):
        facts.add("static_scope_leak", {**ix, "symbol": symbol, "defined_in": defined_in},
                  source=PRODUCER, depends_on=[index_eid, doc_eids[defined_in]])

    # --- tree-sitter side ----------------------------------------------------
    sites: list[dict] = []
    site_deps: dict[tuple[str, int], set[str]] = {}

    def site(file: Any, line: Any, dep: str) -> None:
        if isinstance(file, str) and isinstance(line, int) and not isinstance(line, bool):
            sites.append({"file": file, "line": line})
            site_deps.setdefault((file, line), set()).add(dep)

    node_locations = ast_raw.get("_node_locations") or {}
    handler_location_missing: list[str] = []
    for surface in ast_raw.get("surfaces") or []:
        sid = surface.get("id")
        file, line = surface.get("file"), surface.get("line")
        handler = surface.get("handler")
        handler_name = surface.get("handler_symbol") or handler
        if not isinstance(sid, str) or not isinstance(file, str) or not isinstance(line, int):
            messages.append(f"surface {sid!r} without a file/line was skipped")
            continue
        eid = facts.add("route_site", {
            **ix, "surface": sid, "path": file, "line": line,
            "handler_name": handler_name if isinstance(handler_name, str) else "",
        }, source=treesitter_source, depends_on=[tree_eid])
        site(file, line, eid)
        loc = node_locations.get(handler) if isinstance(handler, str) else None
        if isinstance(loc, (list, tuple)) and len(loc) == 2 and isinstance(loc[0], str) \
                and isinstance(loc[1], int):
            facts.add("route_handler_location",
                      {**ix, "surface": sid, "path": loc[0], "line": loc[1]},
                      source=treesitter_source, depends_on=[tree_eid, eid])
        else:
            handler_location_missing.append(sid)
            facts.add("static_unresolved", {
                **ix, "kind": "route-handler-location-missing",
                "node": handler if isinstance(handler, str) else "", "path": file, "line": line,
            }, source=treesitter_source, depends_on=[tree_eid, eid])

    excluded = ast_raw.get("excluded_surfaces")
    excluded_list = excluded.get("surfaces", []) if isinstance(excluded, dict) else (excluded or [])
    for entry in excluded_list:
        method, route = entry.get("method"), entry.get("path")
        if not isinstance(route, str):
            continue
        sid = f"http:{method} {route}" if method else f"http:{route}"
        facts.add("route_site_excluded", {
            **ix, "surface": sid, "reason": str(entry.get("reason") or "excluded"),
        }, source=treesitter_source, depends_on=[tree_eid])

    for op in ast_raw.get("op_sites") or []:
        file, line, entity = op.get("file"), op.get("line"), op.get("entity")
        if not (isinstance(file, str) and isinstance(line, int) and isinstance(entity, str)):
            continue
        eid = facts.add("static_op_site", {
            **ix, "path": file, "line": line, "verb": str(op.get("crud") or op.get("verb") or "unknown"),
            "entity": entity,
        }, source=treesitter_source, depends_on=[tree_eid])
        site(file, line, eid)

    for entity in ast_raw.get("entities") or []:
        name, file, line = entity.get("name"), entity.get("file"), entity.get("line")
        if not (isinstance(name, str) and isinstance(file, str) and isinstance(line, int)):
            continue
        facts.add("static_entity", {
            **ix, "entity": name, "path": file, "line": line,
            "table": str(entity.get("table") or name),
        }, source=treesitter_source, depends_on=[tree_eid])

    census_available = (isinstance(ast_raw.get("scip_residue"), list)
                        and isinstance(ast_raw.get("blind_spots"), list))
    blind_by_doc: dict[str, int] = {}
    for spot in ast_raw.get("blind_spots") or []:
        file, line = spot.get("file"), spot.get("line")
        if not (isinstance(file, str) and isinstance(line, int)):
            continue
        blind_by_doc[file] = blind_by_doc.get(file, 0) + 1
        facts.add("static_blind_spot", {
            **ix, "path": file, "line": line, "kind": str(spot.get("kind") or "unknown"),
            "reason": str(spot.get("reason") or ""),
        }, source=treesitter_source, depends_on=[tree_eid])

    residue_by_doc: dict[str, int] = {}
    for entry in ast_raw.get("scip_residue") or []:
        file, line = entry.get("file"), entry.get("line")
        if not (isinstance(file, str) and isinstance(line, int)):
            continue
        residue_by_doc[file] = residue_by_doc.get(file, 0) + 1
        eid = facts.add("scip_unresolved_site", {
            **ix, "path": file, "line": line, "callee_text": str(entry.get("callee") or ""),
            "kind": str(entry.get("kind") or "unresolved"),
        }, source=PRODUCER, depends_on=[tree_eid, index_eid])
        site(file, line, eid)

    if profile == PROFILE_FULL:
        for entry in ast_raw.get("call_sites") or []:
            site(entry.get("file"), entry.get("line"), tree_eid)

    adapter_unresolved = False
    for entry in ast_raw.get("unresolved") or []:
        kind = str(entry.get("kind") or "unresolved")
        if kind == "deep-unavailable" or kind.startswith("adapter"):
            adapter_unresolved = True
        loc = entry.get("source") if isinstance(entry.get("source"), dict) else {}
        path = loc.get("file") or entry.get("file") or ""
        line = loc.get("line") if loc.get("line") is not None else entry.get("line")
        node = entry.get("node") or entry.get("handler") or entry.get("id") or entry.get("adapter") or ""
        facts.add("static_unresolved", {
            **ix, "kind": kind, "node": str(node), "path": str(path),
            "line": line if isinstance(line, int) and not isinstance(line, bool) and line >= 0 else 0,
        }, source=treesitter_source, depends_on=[tree_eid])

    for owned in scip_map.site_owners(normalized_retained, sites, line_base=1):
        owner = owned["owner"]
        if owner is None:
            continue
        file, line = owned["file"], owned["line"]
        mention(owner, doc_eids.get(file, index_eid))
        facts.add("static_site_owner", {**ix, "path": file, "line": line, "symbol": owner},
                  source=PRODUCER,
                  depends_on=[tree_eid, doc_eids.get(file, index_eid),
                              *sorted(site_deps.get((file, line), ())),
                              *sorted(def_occurrence_ids.get(owner, ()))])

    # --- symbol identity for every symbol any exported row mentions ----------
    for symbol, under in sorted(mentioned.items()):
        deps = sorted(under)
        info = table.get(symbol)
        if info is None:
            _add_symbol(facts, ix, symbol, {"symbol": symbol}, table, indexer_source, deps)
        node, reason = to_node.explain(symbol)
        if node is not None:
            facts.add("scip_symbol_node", {**ix, "symbol": symbol, "node": node},
                      source=PRODUCER, depends_on=deps)
        else:
            facts.add("scip_symbol_unrooted", {**ix, "symbol": symbol, "reason": reason},
                      source=PRODUCER, depends_on=deps)

    # --- completeness witnesses ----------------------------------------------
    witness = f"{EXPORTER} "
    facts.add("scip_documents_closed", ix, source=witness + WITNESS_DOCUMENTS,
              depends_on=[index_eid, tree_eid])
    references_closed_docs: set[str] = set()
    for path in sorted(in_scope):
        if doc_occurrences.get(path, 0) == 0:
            continue
        doc_eid = doc_eids[path]
        if not doc_has_duplicate[path]:
            facts.add("scip_definitions_closed", {**ix, "path": path},
                      source=witness + WITNESS_DEFINITIONS, depends_on=[doc_eid, tree_eid])
        if (census_available and not doc_synthesized[path] and not doc_has_duplicate[path]
                and residue_by_doc.get(path, 0) == 0 and blind_by_doc.get(path, 0) == 0):
            facts.add("scip_references_closed", {**ix, "path": path},
                      source=witness + WITNESS_REFERENCES, depends_on=[doc_eid, tree_eid])
            references_closed_docs.add(path)

    route_inventory_closed = ("surfaces" in ast_raw and "excluded_surfaces" in ast_raw
                              and not adapter_unresolved)
    if route_inventory_closed:
        facts.add("static_route_inventory_closed", ix,
                  source=witness + WITNESS_ROUTE_INVENTORY, depends_on=[tree_eid, index_eid])
    scope_closed = not leaks
    if scope_closed:
        facts.add("static_scope_closed", ix, source=witness + WITNESS_SCOPE,
                  depends_on=[index_eid, tree_eid])
    handlers_in_scope = all(
        isinstance(loc, (list, tuple)) and len(loc) == 2 and loc[0] in in_scope
        for s in (ast_raw.get("surfaces") or [])
        for loc in [node_locations.get(s.get("handler"))]
    )
    if (scope_closed and route_inventory_closed and census_available and in_scope
            and not handler_location_missing and handlers_in_scope
            and facts.count("static_unresolved") == 0
            and all(doc_occurrences.get(p, 0) > 0 and p in references_closed_docs for p in in_scope)):
        facts.add("static_reachability_closed", ix, source=witness + WITNESS_REACHABILITY,
                  depends_on=[index_eid, tree_eid])

    # --- bounds, assembly, validation ----------------------------------------
    if len(facts) > limits.rows:
        return ExportResult(STATUS_RESOURCE_EXHAUSTED, None,
                            {**facts.counts(), "documents": len(documents),
                             "occurrences": occurrence_total},
                            (f"rows {len(facts)} exceed limit {limits.rows}",))
    # content digest -> index -> evidence ids (module docstring, IDENTITY)
    index = facts.identity()
    facts.rebase(index)
    messages.append(f"index identity {index} ({INDEX_IDENTITY}); the index file digest "
                    f"{index_digest} ({index_digest_kind}) is a run receipt, not identity")
    fact_atoms, evidence = facts.materialize()
    metadata = {
        "export_version": EXPORT_VERSION,
        "exporter": EXPORTER,
        "index_digest": index,
        "index_digest_kind": INDEX_IDENTITY,
        "index_file_digest": index_digest,
        "index_file_digest_kind": index_digest_kind,
        "language": language,
        "profile": profile,
        "scope": {"kind": scope.kind, "values": scope.rows()},
        "producers": {
            "indexer": indexer, "indexer_version": indexer_version,
            "indexer_arguments": list(meta.get("arguments") or []),
            "treesitter": treesitter_source, "exporter": PRODUCER,
        },
        "tree": {"digest": tree_digest, "file_count": len(manifest), "pattern": " ".join(patterns)},
        "line_frame": LINE_FRAME,
        "census_available": census_available,
        "in_scope_documents": sorted(in_scope),
        "out_of_tree_documents": sorted(
            ({"basename": PurePosixPath(d["path"]).name,
              "occurrence_count": len(d.get("occurrences", []) or [])} for d in out_of_tree),
            key=lambda entry: entry["basename"]),
        "row_counts": facts.counts(),
    }
    if commit:
        metadata["commit"] = commit
    bundle = Bundle(tuple(relations.values()), fact_atoms, (), (), tuple(metadata.items()),
                    evidence=evidence)
    try:
        assert_valid(bundle)
    except ValidationError as exc:
        return ExportResult(STATUS_INVALID_INPUT, None, facts.counts(),
                            tuple(f"{i.code}: {i.message} at {i.path}" for i in exc.issues)
                            if hasattr(exc, "issues") else (str(exc),))
    return ExportResult(STATUS_COMPLETE, bundle, facts.counts(), tuple(messages))


def _input_problem(normalized: dict, scope: Scope, profile: str, index_digest: str,
                   index_digest_kind: str, language: str) -> str | None:
    if not isinstance(normalized, dict) or "metadata" not in normalized:
        return ("normalized index lacks 'metadata'; pass the output of "
                "normalize_scip_json(..., retain=True) / read_scip_index(..., retain=True)")
    documents = normalized.get("documents", []) or []
    if any("enclosing_synthesized" not in d for d in documents):
        return "normalized documents lack retained keys; re-normalize with retain=True"
    if not isinstance(index_digest, str) or not index_digest:
        return "index_digest must be a non-empty digest string"
    if index_digest_kind not in (runner.INDEX_DIGEST_BINARY, runner.INDEX_DIGEST_JSON):
        return f"index_digest_kind must be 'binary' or 'json', got {index_digest_kind!r}"
    if scope.kind not in _SCOPE_KINDS:
        return f"unknown scope kind {scope.kind!r}"
    if scope.kind != SCOPE_ALL and not scope.values:
        return f"scope {scope.kind!r} needs at least one value"
    if profile not in (PROFILE_SLICE, PROFILE_FULL):
        return f"unknown profile {profile!r}"
    if profile == PROFILE_FULL and (scope.kind != SCOPE_DOCUMENT_SET
                                    or len(scope.values) > FULL_PROFILE_MAX_DOCUMENTS):
        return (f"profile 'full' is permitted only for a document_set scope of at most "
                f"{FULL_PROFILE_MAX_DOCUMENTS} documents")
    try:
        artifacts.patterns_for(language)
        resolve.normalizer(language)
    except (KeyError, ValueError):
        return f"no tree pattern set / SCIP normalizer for language {language!r}"
    return None


def _symbol_category(symbol: str, table: dict[str, dict]) -> str:
    return scip_map._category(symbol, (table.get(symbol) or {}).get("kind"))


def _add_symbol(facts: _Facts, ix: dict, symbol: str, info: dict, table: dict,
                source: str, deps: Iterable[str]) -> None:
    kind = info.get("kind")
    facts.add("scip_symbol", {
        **ix, "symbol": symbol, "kind": kind if isinstance(kind, str) and kind else "unspecified",
        "category": _symbol_category(symbol, table),
        "display_name": info.get("display_name") or scip_map._leaf_name(symbol),
    }, source=source, depends_on=list(deps))


def _document_package(document: dict, to_node: resolve._Normalizer) -> str | None:
    for occ in document.get("occurrences", []) or []:
        if not occ.get("is_definition"):
            continue
        node, _ = to_node.explain(occ.get("symbol"))
        if node is not None:
            return node.split(":", 1)[0]
    return None


def _escapes_tree(path: Any) -> bool:
    """True for a document path that is absolute or climbs out of the project root."""
    if not isinstance(path, str) or not path:
        return False
    parsed = PurePosixPath(path)
    return parsed.is_absolute() or ".." in parsed.parts


def _in_scope(document: dict, scope: Scope, to_node: resolve._Normalizer) -> bool:
    path = document.get("path") or ""
    if scope.kind == SCOPE_ALL:
        return True
    if scope.kind == SCOPE_DOCUMENT_SET:
        return path in scope.values
    prefix = scope.values[0]
    if path.startswith(prefix):
        return True
    package = _document_package(document, to_node)
    return package is not None and package.startswith(prefix)


# ---------------------------------------------------------------------------
# The impure entry


def export_from_tree(
    source_root: str | Path,
    language: str,
    *,
    ast_raw: dict | None = None,
    scope: Scope | None = None,
    profile: str = PROFILE_SLICE,
    limits: ExportLimits | None = None,
    commit: str | None = None,
    describes_runs: Iterable[str] = (),
    timeout: int = 600,
) -> ExportResult:
    """Index ``source_root`` with the SCIP indexer for ``language``, hash the
    ``index.scip`` it wrote, remove it, and export the bundle.

    The only impure entry: it shells out through ``scip.runner`` and raises
    ``resolve.ScipToolsUnavailable`` (naming the missing tool) when the indexer
    or the ``scip`` CLI is absent -- never a degraded bundle wearing the SCIP
    label. ``ast_raw`` is the tree-sitter side; without it the tree-sitter
    relations are empty and the call-site census counts as unavailable.
    """
    if not resolve.tools_available(language):
        executable = runner._INDEXERS.get(language, ("<unknown>",))[0]
        raise resolve.ScipToolsUnavailable(
            f"exporting {language!r} static facts needs the indexer {executable!r} and the "
            "scip CLI on PATH")
    root = Path(source_root)
    index_path = runner.run_scip_index(root, language, timeout=timeout)
    try:
        normalized = runner.read_scip_index(index_path, retain=True)
    finally:
        index_path.unlink(missing_ok=True)
    return export_bundle(
        normalized, ast_raw=ast_raw, source_root=root, language=language, scope=scope,
        profile=profile, limits=limits, index_digest=normalized["index_digest"],
        index_digest_kind=normalized["index_digest_kind"], commit=commit,
        describes_runs=describes_runs,
    )


def _without_receipts(items: Any) -> Any:
    """``items`` (a mapping or a frozen sequence of pairs) minus the receipt keys."""
    pairs = list(items.items()) if isinstance(items, Mapping) else list(items)
    return tuple((k, v) for k, v in pairs if k not in RECEIPT_METADATA_KEYS)


def bundle_digest(bundle: Bundle) -> str:
    """The canonical digest of an exported bundle (``claims.ir.digest``) with
    the run receipts ``RECEIPT_METADATA_KEYS`` removed from the metadata, so
    the digest is a function of the exported content and not of which
    ``index.scip`` bytes happened to be read. ``combine.combine`` nests each
    input's metadata under ``source_<i>``; the receipts are removed there too,
    so a combined bundle's digest is receipt-free as well."""
    metadata = []
    for key, value in _without_receipts(bundle.metadata):
        if key.startswith("source_") and isinstance(value, (Mapping, tuple, list)) \
                and all(isinstance(item, (tuple, list)) and len(item) == 2 for item in
                        (value.items() if isinstance(value, Mapping) else value)):
            value = _without_receipts(value)
        metadata.append((key, value))
    return ir_digest(replace(bundle, metadata=tuple(metadata)))


__all__ = [
    "EXPORT_VERSION", "EXPORTER", "PRODUCER", "LINE_FRAME", "INDEX_IDENTITY",
    "RECEIPT_METADATA_KEYS", "static_relations_index",
    "STATUS_COMPLETE", "STATUS_RESOURCE_EXHAUSTED", "STATUS_INVALID_INPUT", "STATUS_STALE",
    "PROFILE_SLICE", "PROFILE_FULL", "Scope", "ExportLimits", "ExportResult",
    "ExportInputError", "evidence_id", "row_digest", "occurrence_digest",
    "occurrence_external_id", "static_relations", "primitive_relations", "STUB_RELATIONS",
    "canonical_project_root", "export_bundle", "export_from_tree", "bundle_digest",
]
