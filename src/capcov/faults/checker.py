"""Turn one run into a claims bundle and judge it with the existing claim kernels.

:func:`build_bundle` emits a ``capcov.claims.ir.Bundle``: the relations and
rules of the shipped pack ``rules-faults-v1.json``, facts from four producers
(the history, the service's change log, the service's ground truth, the final
reads) and the rows the consumer reports about its own bookkeeping.

Absence only means something when the input it is absent from was complete.  A
completeness fact is therefore emitted only when its input says so:
``History.complete_flag``, ``ChangeRead.complete``,
``ServiceFacts.applied_complete``, ``FinalRead.complete`` or ``Rows.complete``.
None of these flags defaults to true.  An invariant whose completeness facts are
missing comes back ``unresolved`` with the missing relation named, and an
unresolved invariant fails the run exactly as a violated one does.

The fake service's applied writes are complete only when it holds no accepted
write it has still to apply.  While one is pending, ``applied_closed`` and
``changes_closed`` are withheld and the service's own final values are an
incomplete final read.

:func:`check` evaluates the bundle with ``capcov.claims.evaluator`` and, with
``engine="both"``, also with Souffle through ``capcov.claims.differential``.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any, Iterable, Mapping, Sequence

from ..claims.ir import (Atom, Bundle, Claim, Constant, Context, Evidence, EvidenceMapping,
                         OutputTemplate, RelationDecl, Rule, TemplateValue, TypeName,
                         bundle_from_json)
from .history import EFFECT_KIND, Digest, History, value_digest
from .service import ChangeEntry, ChangeRead, FakeService

PACK_ID = "rules-faults-v1"
PACK_NAME = "rules-faults-v1.json"
FINALS_SCHEMA_VERSION = 1
DEFAULT_RUN = "run-1"
ENGINES = ("python", "both")

DIAGNOSTIC_POLICY = {"missing_premises": "unresolved", "inconsistent_premises": "inconsistent-premises",
                     "out_of_scope": "out-of-scope", "forbidden_evidence": "invalid-input",
                     "revocation": "refutation", "completeness": "required"}
RELATION_FIELDS = {"name", "columns", "modality", "polarity", "binding", "primitive", "producer_classes",
                   "context_indices", "completes", "finite", "nonempty", "compatibility_targets",
                   "compatibility_context_indices"}


class FaultCheckError(ValueError):
    """The history, finals, pack or bundle handed to the checker is invalid."""


class SouffleRequired(RuntimeError):
    """``engine="both"`` was requested and no ``souffle`` executable is on PATH."""


# --- inputs ---------------------------------------------------------------

@dataclass(frozen=True)
class FinalRead:
    """The final contents of one store: ``rows`` maps key to value.

    ``complete=True`` says the read covered the whole store.  It is false
    unless the caller says so; the checker then refuses to conclude anything
    from a key being absent.
    """
    store: str
    rows: Mapping[str, Any]
    complete: bool = False


@dataclass(frozen=True)
class Rows:
    """Rows of one consumer relation.  ``complete=True`` says they are all of them."""
    rows: tuple[tuple[Any, ...], ...] = ()
    complete: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "rows", tuple(tuple(row) for row in self.rows))


@dataclass(frozen=True)
class ExtraFacts:
    """Rows of a relation the consumer declares in ``extra``.

    ``relation`` names a primitive ``RelationDecl`` passed in the same
    ``extra``, whose first column is the context column ``run``.  Each row
    leaves ``run`` out; the checker adds it.  A completeness relation is closed
    by one empty row: ``ExtraFacts("mine_closed", [()])``.
    """
    relation: str
    rows: tuple[tuple[Any, ...], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "rows", tuple(tuple(row) for row in self.rows))


@dataclass(frozen=True)
class ServiceFacts:
    """What one external service contributes: its log read and its ground truth.

    ``applied`` rows are ``(seq, request, key, value digest, origin)`` with
    origin ``request`` or ``outside``.  ``applied_complete`` is true only when
    the service held no accepted write still to be applied; it has no default.
    """
    store: str
    changes: ChangeRead
    applied: tuple[tuple[int, int, str, str, str], ...]
    overwrote_unread: tuple[tuple[int, str], ...]
    final: Mapping[str, str]
    applied_complete: bool

    @classmethod
    def from_service(cls, service: FakeService) -> "ServiceFacts":
        truth = service.ground_truth()
        return cls(service.name, service.changes(),
                   tuple((a.seq, a.request, a.key, a.value, a.origin) for a in truth.applied),
                   tuple((a.seq, a.key) for a in truth.overwrote_unread),
                   {key: value_digest(value) for key, value in truth.final.items()},
                   not truth.pending)


@dataclass(frozen=True)
class Finals:
    """Everything read after the run ended.

    ``reads`` are final reads of the stores.  The four consumer relations come
    from the consumer's own primary store:

    * ``intent`` rows ``(intent, op, store, key, value)``: an effect the program
      recorded it was going to make.
    * ``recorded`` rows ``(intent, outcome)`` with outcome ``applied`` or
      ``absent``: what the program believes happened.
    * ``settled`` rows ``(intent,)``: intents no recovery will act on again.
    * ``overwrite_reported`` rows ``(store, seq)``: writes the program reported
      as having replaced a value it had not read.

    A relation left ``None`` is treated as not read: no rows, not complete.
    """
    reads: tuple[FinalRead, ...] = ()
    intent: Rows | None = None
    recorded: Rows | None = None
    settled: Rows | None = None
    overwrite_reported: Rows | None = None
    run: str = DEFAULT_RUN

    def __post_init__(self) -> None:
        object.__setattr__(self, "reads", tuple(self.reads))


CONSUMER_RELATIONS = ("intent", "recorded", "settled", "overwrite_reported")
_CONSUMER_CLOSED = {"intent": "intents_closed", "recorded": "recorded_closed",
                    "settled": "settled_closed", "overwrite_reported": "reports_closed"}
#: Column positions of consumer rows (after ``run``) that hold a value to digest.
_CONSUMER_VALUE_COLUMNS = {"intent": (4,)}
_CONSUMER_ARITY = {"intent": 5, "recorded": 2, "settled": 1, "overwrite_reported": 2}
ORIGINS = ("request", "outside")


# --- the pack -------------------------------------------------------------

def read_pack_text() -> str:
    """The shipped pack's text, as ``importlib.resources`` hands it over."""
    return resources.files(__package__).joinpath(PACK_NAME).read_text(encoding="utf-8")


def load_pack(path: str | Path | None = None) -> dict[str, Any]:
    """The rule pack document.  ``path=None`` reads the shipped package data."""
    try:
        text = Path(path).read_text(encoding="utf-8") if path is not None else read_pack_text()
        pack = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        raise FaultCheckError(f"cannot read rule pack: {exc}") from exc
    if not isinstance(pack, dict) or pack.get("schema_version") != 1:
        raise FaultCheckError("unsupported fault rule pack")
    for section in ("primitives", "supplementary_primitives", "derived", "rules", "invariants"):
        if not isinstance(pack.get(section), list):
            raise FaultCheckError(f"rule pack section {section!r} must be a list")
    for invariant in pack["invariants"]:
        if (not isinstance(invariant, dict) or not isinstance(invariant.get("name"), str)
                or not isinstance(invariant.get("requires"), list)):
            raise FaultCheckError("each invariant needs a name and a requires list")
    return pack


def pack_relations(pack: Mapping[str, Any]) -> list[dict[str, Any]]:
    relations = [*pack["primitives"], *pack["supplementary_primitives"], *pack["derived"]]
    names = [relation.get("name") for relation in relations]
    if len(names) != len(set(names)):
        raise FaultCheckError("rule pack declares a relation twice")
    for relation in relations:
        if set(relation) != RELATION_FIELDS:
            raise FaultCheckError(f"relation {relation.get('name')!r} has unknown or missing fields")
    return relations


def pack_bundle(pack: Mapping[str, Any] | None = None) -> Bundle:
    """The rule pack alone (declarations and rules, no facts) as a validated Bundle."""
    pack = pack if pack is not None else load_pack()
    try:
        return bundle_from_json({"schema_version": 1, "relations": pack_relations(pack),
                                 "rules": [dict(rule) for rule in pack["rules"]],
                                 "diagnostic_policy": dict(DIAGNOSTIC_POLICY)}, validate=True)
    except ValueError as exc:
        raise FaultCheckError(f"invalid rule pack: {exc}") from exc


@lru_cache(maxsize=1)
def _shipped() -> tuple[Bundle, tuple[tuple[str, tuple[str, ...]], ...]]:
    pack = load_pack()
    return pack_bundle(pack), tuple(
        (invariant["name"], tuple(invariant["requires"])) for invariant in pack["invariants"])


def invariant_names(pack: Mapping[str, Any] | None = None) -> tuple[str, ...]:
    """Names of the invariants the pack judges, in pack order."""
    if pack is None:
        return tuple(name for name, _ in _shipped()[1])
    return tuple(invariant["name"] for invariant in pack["invariants"])


# --- bundle construction ----------------------------------------------------

class _Facts:
    def __init__(self, run: str, relations: Mapping[str, RelationDecl]) -> None:
        self.run, self.relations = run, relations
        self.atoms: dict[Atom, Evidence] = {}
        self.context = Context.from_mapping({"run": run})

    def add(self, relation: str, row: Sequence[Any], producer: str, detail: str) -> None:
        declaration = self.relations.get(relation)
        if declaration is None:
            raise FaultCheckError(f"the rule pack does not declare {relation!r}")
        values = (self.run, *row)
        if len(values) != declaration.arity:
            raise FaultCheckError(f"{relation} takes {declaration.arity - 1} columns, got {len(row)}")
        for value, column in zip(values, declaration.columns):
            if not _fits(value, column.type):
                raise FaultCheckError(
                    f"{relation}.{column.name} takes {column.type.value}, got {value!r}")
        atom = Atom(relation, tuple(Constant(value, column.type)
                                    for value, column in zip(values, declaration.columns)))
        if atom not in self.atoms:
            self.atoms[atom] = Evidence(f"{producer}:{relation}:{len(self.atoms) + 1}", atom,
                                        self.context, f"{producer} {detail}".strip())

    def has(self, relation: str) -> bool:
        return any(atom.relation == relation for atom in self.atoms)


def _fits(value: Any, type_name: TypeName) -> bool:
    """Whether ``value`` is a constant of column type ``type_name``."""
    if type_name in {TypeName.SYMBOL, TypeName.DIGEST}:
        return isinstance(value, str)
    if type_name is TypeName.BOOLEAN:
        return isinstance(value, bool)
    if type_name in {TypeName.INTEGER, TypeName.UNSIGNED, TypeName.TIMESTAMP}:
        return (isinstance(value, int) and not isinstance(value, bool)
                and (type_name is TypeName.INTEGER or value >= 0))
    return True


def _history_facts(facts: _Facts, history: History) -> None:
    for operation in history.operations():
        if operation["kind"] == EFFECT_KIND:
            store, _, label = operation["target"].partition("/")
            facts.add("effect_sent", (operation["op"], operation["token"], store, label,
                                      operation["actor"], operation["result"] or "info"),
                      "history", "effect")
            continue
        key = (operation["op"], operation["attempt"])
        facts.add("op_invoked", (*key, operation["actor"], operation["kind"], operation["target"]),
                  "history", "invoke")
        if operation["result"] is not None:
            facts.add("op_completed", (*key, operation["result"]), "history", "complete")
    if history.complete_flag:
        facts.add("ops_closed", (), "history", "close")


def _validate_service(service: ServiceFacts) -> None:
    if not isinstance(service.store, str) or not service.store:
        raise FaultCheckError("service store must be a non-empty string")
    if not isinstance(service.applied_complete, bool) or not isinstance(service.changes.complete, bool):
        raise FaultCheckError("service applied_complete and changes.complete must be booleans")
    for row in service.applied:
        if len(row) != 5:
            raise FaultCheckError(f"service applied rows take 5 columns, got {len(row)}")
        if row[4] not in ORIGINS:
            raise FaultCheckError(f"service applied origin must be one of {ORIGINS}, got {row[4]!r}")
    for row in service.overwrote_unread:
        if len(row) != 2:
            raise FaultCheckError(f"service overwrote_unread rows take 2 columns, got {len(row)}")
    if not isinstance(service.final, Mapping):
        raise FaultCheckError("service final must map each key to a digest")
    for key, digest in service.final.items():
        if not isinstance(key, str) or not isinstance(digest, str):
            raise FaultCheckError(f"service final must map a string key to a digest, got {key!r}: {digest!r}")


def _service_facts(facts: _Facts, service: ServiceFacts) -> None:
    _validate_service(service)
    store = service.store
    for entry in service.changes.entries:
        entry = ChangeEntry(*entry)
        facts.add("change", (store, entry.seq, entry.actor, entry.key, entry.value_digest),
                  "service-log", store)
    if service.changes.complete:
        facts.add("changes_closed", (), "service-log", store)
    for seq, request, key, value, origin in service.applied:
        facts.add("applied", (store, seq, request, key, value, origin), "ground-truth", store)
    for seq, key in service.overwrote_unread:
        facts.add("overwrote_unread", (store, seq, key), "ground-truth", store)
    if service.applied_complete:
        facts.add("applied_closed", (), "ground-truth", store)


def _extra_facts(facts: _Facts, items: Sequence[ExtraFacts], declared: Mapping[str, RelationDecl]) -> None:
    for item in items:
        declaration = declared.get(item.relation)
        if declaration is None:
            raise FaultCheckError(
                f"extra facts name {item.relation!r}, which extra does not declare; "
                "facts of the pack's relations come from the run, not from extra")
        if not declaration.primitive:
            raise FaultCheckError(f"{item.relation!r} is derived and takes no facts")
        if (declaration.context_indices != ("run",) or not declaration.columns
                or declaration.columns[0].name != "run"):
            raise FaultCheckError(f"{item.relation!r} needs the context column 'run' first")
        if declaration.producer_classes and "consumer" not in declaration.producer_classes:
            raise FaultCheckError(f"{item.relation!r} does not admit the producer class 'consumer'")
        for row in item.rows:
            facts.add(item.relation, row, "consumer", item.relation)


def _consumer_rows(name: str, rows: Rows) -> list[tuple[Any, ...]]:
    converted = []
    for row in rows.rows:
        if len(row) != _CONSUMER_ARITY[name]:
            raise FaultCheckError(f"{name} rows take {_CONSUMER_ARITY[name]} columns, got {len(row)}")
        converted.append(tuple(value_digest(value) if index in _CONSUMER_VALUE_COLUMNS.get(name, ())
                               else value for index, value in enumerate(row)))
    return converted


def build_bundle(history: History, service: FakeService | ServiceFacts | None, finals: Finals,
                 extra: Iterable[Any] = (), *, pack: Mapping[str, Any] | None = None) -> Bundle:
    """One run as a claims bundle.

    ``service`` is the fake service the run used, or the :class:`ServiceFacts`
    read from it.  The service's own final values are added as a final read of
    its store unless ``finals`` already reads that store; that read is complete
    only when the service holds no pending write.  ``applied_closed`` is emitted
    only then too.  With ``service=None`` no service fact and no
    ``applied_closed`` is emitted, so every invariant that reads the service
    stays unresolved.

    ``extra`` adds a consumer's own ``RelationDecl``, ``Rule``, ``Claim``,
    ``OutputTemplate`` and :class:`ExtraFacts` values.  A consumer invariant
    ``mine`` is two rules, ``violated(Run, "mine")`` and
    ``violated_closed(Run, "mine")``, plus ``invariant_claim(run, "mine")``.
    ``ExtraFacts`` supplies rows for a relation declared in the same ``extra``
    and never for a relation of the pack.
    """
    if pack is None:
        base, invariants = _shipped()
    else:
        base = pack_bundle(pack)
        invariants = tuple((item["name"], tuple(item["requires"])) for item in pack["invariants"])
    extras = tuple(extra)
    unknown = [item for item in extras
               if not isinstance(item, (RelationDecl, Rule, Claim, OutputTemplate, ExtraFacts))]
    if unknown:
        raise FaultCheckError("extra accepts RelationDecl, Rule, Claim, OutputTemplate and ExtraFacts, "
                              f"got {type(unknown[0]).__name__}")
    own = [item for item in extras if isinstance(item, RelationDecl)]
    pack_names = {relation.name for relation in base.relations}
    clashes = sorted({relation.name for relation in own} & pack_names)
    if clashes or len({relation.name for relation in own}) != len(own):
        raise FaultCheckError(f"extra declares a relation twice or redeclares the pack's: {clashes}")
    relations = (*base.relations, *own)
    by_name = {relation.name: relation for relation in relations}
    run = finals.run
    facts = _Facts(run, by_name)
    try:
        _history_facts(facts, history)
        service_facts = (ServiceFacts.from_service(service) if isinstance(service, FakeService)
                         else service)
        reads = list(finals.reads)
        if service_facts is not None:
            _service_facts(facts, service_facts)
            if all(read.store != service_facts.store for read in reads):
                reads.append(FinalRead(service_facts.store,
                                       {key: Digest(value) for key, value in service_facts.final.items()},
                                       complete=service_facts.applied_complete))
        stores = [read.store for read in reads]
        if len(stores) != len(set(stores)):
            raise FaultCheckError("a store has more than one final read")
        for read in reads:
            for key, value in read.rows.items():
                facts.add("final_value", (read.store, key, value_digest(value)), "final-read", read.store)
            if read.complete:
                facts.add("final_closed", (read.store,), "final-read", read.store)
        intent_stores = set()
        for name in CONSUMER_RELATIONS:
            rows = getattr(finals, name)
            if rows is None:
                continue
            for row in _consumer_rows(name, rows):
                facts.add(name, row, "consumer", name)
                if name == "intent":
                    intent_stores.add(row[2])
            if rows.complete:
                facts.add(_CONSUMER_CLOSED[name], (), "consumer", name)
        complete_stores = {read.store for read in reads if read.complete}
        open_stores = sorted(({read.store for read in reads} | intent_stores) - complete_stores)
        if not open_stores:
            facts.add("finals_closed", (), "final-read", "all-stores")
        _extra_facts(facts, [item for item in extras if isinstance(item, ExtraFacts)],
                     {relation.name: relation for relation in own})
    except (TypeError, ValueError) as exc:
        if isinstance(exc, FaultCheckError):
            raise
        raise FaultCheckError(f"cannot build facts: {exc}") from exc

    claims = [invariant_claim(run, name) for name, _ in invariants]
    claims.extend(item for item in extras if isinstance(item, Claim))
    outputs = [item for item in extras if isinstance(item, OutputTemplate)]
    for name, requires in invariants:
        for relation in requires:
            if facts.has(relation):
                continue
            if relation == "finals_closed":
                outputs.extend(_missing_output(name, "final_closed", store) for store in open_stores)
            else:
                outputs.append(_missing_output(name, relation))
    mappings = [EvidenceMapping("holds", "violated", "refutation", ("run",),
                                (("name", "name"), ("run", "run")), claim_id=claim.id)
                for claim in claims if claim.relation == "holds"]
    try:
        return Bundle(relations=relations, facts=tuple(facts.atoms),
                      rules=(*base.rules, *(item for item in extras if isinstance(item, Rule))),
                      claims=tuple(claims), metadata=(("pack", PACK_ID), ("run", run)),
                      evidence=tuple(facts.atoms.values()), mappings=tuple(mappings),
                      diagnostic_policy=base.diagnostic_policy, outputs=tuple(outputs))
    except (TypeError, ValueError) as exc:
        raise FaultCheckError(f"cannot build bundle: {exc}") from exc


def invariant_claim(run: str, name: str) -> Claim:
    """The claim ``holds(run, name)``."""
    return Claim("holds", (Constant(run, "symbol"), Constant(name, "symbol")),
                 Context.from_mapping({"run": run}), id=f"holds:{name}")


def _missing_output(name: str, relation: str, store: str | None = None) -> OutputTemplate:
    fields = (("store", TemplateValue("constant", type="symbol", value=store)),) if store else ()
    return OutputTemplate("missing_premise", f"holds:{name}", relation=relation, fields=fields,
                          when_claim="unresolved")


# --- verdicts ---------------------------------------------------------------

@dataclass(frozen=True)
class Verdict:
    """One invariant's outcome.

    ``status`` is ``supported`` (the claim is supported and the evaluation
    completed), ``violated`` (a violation row was derived) or ``unresolved``.
    ``violations`` are the rows of the relation named after the invariant, and
    ``missing_premises`` names what an unresolved claim lacked.
    """
    name: str
    status: str
    semantic: str
    operational: str
    violations: tuple[tuple[Any, ...], ...] = ()
    missing_premises: tuple[Any, ...] = ()

    @property
    def passed(self) -> bool:
        return self.status == "supported"

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "semantic": self.semantic,
                "operational": self.operational,
                "violations": [list(row) for row in self.violations],
                "missing_premises": list(self.missing_premises)}


@dataclass(frozen=True)
class Verdicts:
    run: str
    engine: str
    verdicts: tuple[Verdict, ...]

    @property
    def ok(self) -> bool:
        return bool(self.verdicts) and all(verdict.passed for verdict in self.verdicts)

    @property
    def failed(self) -> tuple[Verdict, ...]:
        return tuple(verdict for verdict in self.verdicts if not verdict.passed)

    def __getitem__(self, name: str) -> Verdict:
        for verdict in self.verdicts:
            if verdict.name == name:
                return verdict
        raise KeyError(name)

    def as_dict(self) -> dict[str, Any]:
        return {"run": self.run, "engine": self.engine, "ok": self.ok,
                "invariants": [verdict.as_dict() for verdict in self.verdicts]}


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def check(bundle: Bundle, engine: str = "python", *, replay_root: str | Path | None = None) -> Verdicts:
    """Judge every claim in ``bundle``.

    ``engine="python"`` uses the Python evaluator.  ``engine="both"`` also runs
    Souffle and requires the two kernels to agree; it raises
    :class:`SouffleRequired` when ``souffle`` is not installed and
    ``capcov.claims.differential.DifferentialMismatch`` when they disagree.
    """
    if engine not in ENGINES:
        raise FaultCheckError(f"engine must be one of {ENGINES}")
    from ..claims.evaluator import evaluate

    report = evaluate(bundle)
    if report.status.value != "complete":
        raise FaultCheckError(f"bundle evaluation is {report.status.value}: {report.message}")
    if engine == "both":
        if shutil.which("souffle") is None:
            raise SouffleRequired("engine 'both' needs the souffle executable on PATH")
        from ..claims.differential import compare

        if replay_root is None:
            with tempfile.TemporaryDirectory(prefix="capcov-faults-") as scratch:
                compare(bundle, replay_root=scratch)
        else:
            compare(bundle, replay_root=replay_root)
    relation_names = {relation.name for relation in bundle.relations}
    verdicts = []
    for entry in report.claims:
        claim, result = entry.claim, entry.result
        run_value = claim.terms[0].value if claim.terms and isinstance(claim.terms[0], Constant) else None
        name = claim.id
        if claim.relation == "holds" and len(claim.terms) == 2 and isinstance(claim.terms[1], Constant):
            name = claim.terms[1].value
        violations = tuple(row for row in report.relation_rows(name)
                           if row and row[0] == run_value) if name in relation_names else ()
        semantic, operational = result.semantic.value, result.operational.value
        if semantic == "supported" and operational == "complete":
            status = "supported"
        elif semantic in {"refuted", "conflicting"}:
            status = "violated"
        else:
            status = "unresolved"
        missing = tuple(_plain(item) for item in result.missing_premises) if status == "unresolved" else ()
        verdicts.append(Verdict(name, status, semantic, operational, violations, missing))
    return Verdicts(dict(bundle.metadata).get("run", ""), engine, tuple(verdicts))


# --- finals on disk -----------------------------------------------------------

def finals_to_json(finals: Finals, service: FakeService | ServiceFacts | None = None) -> str:
    """Finals, and the service's facts, as the document ``capcov faults check`` reads.

    Values are written as sha256 digests of their canonical JSON.
    """
    facts = ServiceFacts.from_service(service) if isinstance(service, FakeService) else service
    document: dict[str, Any] = {
        "schema_version": FINALS_SCHEMA_VERSION, "run": finals.run,
        "final_reads": [{"store": read.store, "complete": read.complete,
                         "rows": sorted([key, value_digest(value)] for key, value in read.rows.items())}
                        for read in finals.reads],
        "service": None if facts is None else {
            "store": facts.store,
            "changes": {"complete": facts.changes.complete,
                        "entries": [list(entry) for entry in facts.changes.entries]},
            "applied": [list(row) for row in facts.applied],
            "applied_complete": facts.applied_complete,
            "overwrote_unread": [list(row) for row in facts.overwrote_unread],
            "final": dict(sorted(facts.final.items()))}}
    for name in CONSUMER_RELATIONS:
        rows = getattr(finals, name)
        document[name] = None if rows is None else {
            "complete": rows.complete, "rows": [list(row) for row in _consumer_rows(name, rows)]}
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def finals_from_json(text: str) -> tuple[Finals, ServiceFacts | None]:
    """Read the document :func:`finals_to_json` writes."""
    try:
        document = json.loads(text)
        if not isinstance(document, dict) or document.get("schema_version") != FINALS_SCHEMA_VERSION:
            raise ValueError("unsupported finals schema version")
        known = {"schema_version", "run", "final_reads", "service", *CONSUMER_RELATIONS}
        if set(document) - known:
            raise ValueError(f"unknown fields: {sorted(set(document) - known)}")
        if not isinstance(document.get("run"), str) or not document["run"]:
            raise ValueError("run must be a non-empty string")
        reads = tuple(FinalRead(read["store"], {key: Digest(value) for key, value in read["rows"]},
                                _boolean(read["complete"]))
                      for read in document.get("final_reads", ()))
        consumer = {}
        for name in CONSUMER_RELATIONS:
            raw = document.get(name)
            if raw is None:
                consumer[name] = None
                continue
            rows = []
            for row in raw["rows"]:
                rows.append(tuple(Digest(value) if index in _CONSUMER_VALUE_COLUMNS.get(name, ())
                                  else value for index, value in enumerate(row)))
            consumer[name] = Rows(tuple(rows), _boolean(raw["complete"]))
        raw_service = document.get("service")
        service = None
        if raw_service is not None:
            service = ServiceFacts(
                raw_service["store"],
                ChangeRead(tuple(ChangeEntry(*entry) for entry in raw_service["changes"]["entries"]),
                           _boolean(raw_service["changes"]["complete"])),
                tuple(tuple(row) for row in raw_service["applied"]),
                tuple(tuple(row) for row in raw_service["overwrote_unread"]),
                dict(raw_service["final"]),
                _boolean(raw_service["applied_complete"]))
            _validate_service(service)
        return Finals(reads=reads, run=document["run"], **consumer), service
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise FaultCheckError(f"invalid finals document: {exc}") from exc


def _boolean(value: Any) -> bool:
    if not isinstance(value, bool):
        raise TypeError("complete must be a boolean")
    return value


__all__ = ["PACK_ID", "PACK_NAME", "DEFAULT_RUN", "ENGINES", "FaultCheckError", "SouffleRequired",
           "FinalRead", "Rows", "ExtraFacts", "ServiceFacts", "Finals", "read_pack_text", "load_pack",
           "pack_relations", "pack_bundle", "invariant_names", "build_bundle", "invariant_claim",
           "Verdict", "Verdicts", "check", "finals_to_json", "finals_from_json"]
