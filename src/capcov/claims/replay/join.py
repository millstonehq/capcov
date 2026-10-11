"""Judge a replay receipt directory with the replay pack.

``build`` exports the receipt directory (``replay_facts.export_bundle``; a
refusal is a *contract finding* the caller reports, never something to work
around), combines it with ``rules-replay-v1`` (``capcov.claims.replay.pack``)
and the claim-time rows a judge adds, and states four claims per replayed op:
``op_qualified(index, run, op)``, ``corpus_constrains(run, op)`` and the
companion ``undeclared_write`` / ``exclusion_applied`` rows.  Until a PHP SCIP
census exists, the ``op_declared`` rows and the ``index_describes_replay``
witness are labelled *assumptions* (``Evidence.kind == "assumption"``, ids
``<prefix>:assumed:...``) under a synthetic index digest; their sources name
the class the schema requires and say they are reviewer assumptions.

``evaluate_join`` runs the fail-closed differential -- two kernels (python and
interpreted Souffle) or, with ``kernels="three"``, the compiled Souffle
checker as well -- and certifies every claim row from every closure; the
certificates must be identical across kernels.  ``summary`` is the per-op report a
consumer records; ``write_artifacts`` writes digests,
counts, verdicts and certificates only (no source text, no local paths).

This module is the single entry point for building the combined bundle: the
compiled checker's program identity depends on the relation declaration order
the combiner yields (exported schema relations first, then pack relations), so
tests and scripts must both come through ``build``.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..ir import (Atom, Bundle, Claim, Constant, Context, DiagnosticRule, Evidence, OutputTemplate,
                  TemplateValue, Variable, canonical_json)
from ..differential import CompiledKernelMismatch, DifferentialMismatch, compare, compare_three
from ..souffle import program_for_pack
from ..souffle.compile import CompiledChecker, compile_program
from ..static.combine import combine
from .. import assumptions
from ..static.ground import shared_assumptions, why, why_not
from . import replay_facts
from .pack import pack_bundle

SYNTHETIC_INDEX = hashlib.sha256(b"target-go replay pilot: synthetic PHP census index pending a SCIP census").hexdigest()
REVIEWER_SOURCE = "reviewer claim-time observation"
CENSUS_ASSUMPTION_SOURCE = "php-census assumed by the reviewer pending the PHP SCIP census"
INDEX_ASSUMPTION_SOURCE = "reviewer assumed: synthetic census index pending the PHP SCIP census"
MODEL_WITNESSES = ("model_describes_run", "model_observed", "model_admissible_closed",
                   "model_scope_exclusions_closed", "model_well_formed", "model_checker_admitted")
REASONS = {
    "model_describes_run": "no model runner vouched that a model describes the run",
    "model_observed": "the receipt names no model, so the reviewer has no model digest to observe",
    "model_admissible_closed": "the receipt names no model, so no admissible-state set is closed",
    "model_scope_exclusions_closed": "the reviewer did not close the model-scope exclusion set for this model",
    "model_well_formed": "no typed checker certified that the model describing this run is well formed",
    "model_checker_admitted": "the reviewer admits no checker at the version that certified the model",
}
LEARN_UNMODELED_REASON = ("blocked by the learn campaign: its closed unmodelled list names this op, "
                          "so the model the judge is qualifying against does not model it")
UNDECLARED_REASON = "blocked by undeclared writes: PHP or Go wrote a table the model's closed write set does not declare for this op"
# Premises whose absence is "not built yet" rather than a finding against the
# port.  The Stage D typed checker does not exist, so no real receipt can carry
# a ``model_well_formed`` certificate and none of the committed real fixtures
# does: a run blocked *only* here is **pending**, which ``summary``,
# ``judge.json`` and the CLI's exit code keep distinct from unsupported.  This
# is why the pair sits last in ``_BLOCKING_ORDER``: any premise that is a real
# finding about the systems, the model or the review must win over it, so a
# receipt reported as ``pending model_well_formed`` is one where every other
# premise of ``op_qualified`` was checked and held.
PENDING_PREMISES = ("model_well_formed", "model_checker_admitted")
QUALIFICATION_QUALIFIED = "qualified"
QUALIFICATION_UNSUPPORTED = "unsupported"
# The premises op_qualified_rt needs, in the order a reviewer checks them; the
# first one that fails (or the first "any" that holds) is the blocking premise.
_BLOCKING_ORDER = (
    ("replay_run_current", False), ("model_describes_run", False), ("replayed", False), ("op_exercised", False),
    ("corpus_constrains", False),
    ("php_disagreement_closed", False), ("php_disagree_any", True),
    ("go_disagreement_closed", False), ("go_disagree_any", True), ("undeclared_writes_closed", False),
    ("model_scope_exclusions_closed", False), ("undeclared_any", True),
    ("post_state_gap_closed", False), ("post_state_any", True),
    # ordering, cross-request and cross-run gates (after the write-set and post-state gates:
    # a receipt without the sequence/response/stability relations is blocked earlier when it
    # has a write-set gap, and here otherwise)
    ("effect_order_closed", False), ("effect_order_any", True), ("effect_order_exercised", False),
    ("oracle_stable", False),
    # the cross-request gate: a second DELETE of a committed target that answered anything
    # but 404 on either side, or wrote a row outside the reviewer's scope exclusions, blocks
    # the op it belongs to.  The summary reports the rows themselves under "repeat_delete".
    ("repeat_delete_closed", False), ("repeat_delete_any", True),
    # the learn campaign's downgrade: an op its closed unmodelled list names does not
    # qualify.  A run with no learn receipt derives no learn_unmodeled_any and passes.
    ("learn_unmodeled_gate_closed", False), ("learn_unmodeled_any", True),
    ("kill_gap_closed", False), ("kill_closure_gap_any", True), ("index_describes_replay", False),
    ("op_declared", False),
    # the Stage D premise, checked last (PENDING_PREMISES): the checker is not built, so an
    # op that reaches it has passed every premise that says something about this port
    ("model_well_formed", False), ("model_checker_admitted", False),
)
# per-run relations of _BLOCKING_ORDER whose only column is the run
_RUN_ONLY = ("replay_run_current", "kill_gap_closed", "oracle_stable")


def _row_id(prefix: str, segment: str, relation: str, row: list[Any]) -> str:
    return f"{prefix}:{segment}:{relation}:{replay_facts.row_digest(relation, row)[:12]}"


def _values(decls, relation: str, row) -> dict[str, Any]:
    return dict(zip((column.name for column in decls[relation].columns), row))


def exclusions(relations, run: str) -> list[dict[str, str]]:
    """The reviewer's exclusion rows for the model(s) describing ``run``: table and reason."""
    rows = dict(relations) if not isinstance(relations, dict) else relations
    models = {r[0] for r in rows.get("model_describes_run", ()) if r[1] == run}
    return sorted(({"table": r[1], "reason": r[2]} for r in rows.get("model_scope_exclusion", ()) if r[0] in models),
                  key=lambda item: item["table"])


def well_formed_certificate(relations, run: str) -> dict[str, str] | list[dict[str, str]] | str:
    """The typed checker's certificate for the model(s) describing ``run``.

    ``{"checker", "version", "certificate"}`` for the one certificate, ``"missing"``
    when no checker certified the model op_qualified_rt joins (the premise is
    positive and has no closure, so "missing" is exactly what the judge knows), and
    the list when a model carries more than one.
    """
    rows = dict(relations) if not isinstance(relations, dict) else relations
    models = {r[0] for r in rows.get("model_describes_run", ()) if r[1] == run}
    certificates = sorted(({"checker": r[1], "version": r[2], "certificate": r[3]}
                           for r in rows.get("model_well_formed", ()) if r[0] in models),
                          key=canonical_json)
    if not certificates:
        return "missing"
    return certificates[0] if len(certificates) == 1 else certificates


def learn_summary(relations, run: str) -> dict[str, Any]:
    """What the learn campaign bound to ``run`` said, digest-free and reportable.

    ``{"present": False}`` when no campaign is bound -- which is not a finding: the
    downgrade can only take qualification away, never grant it, so a receipt with
    no learn files is judged exactly as it was before the campaign existed.
    """
    rows = dict(relations) if not isinstance(relations, dict) else relations
    runs = [r for r in rows.get("learn_run", ()) if r[0] == run]
    if not runs:
        return {"present": False}
    campaign, model, learn = runs[0][1], runs[0][2], runs[0][3]
    learns = {r[3] for r in runs}
    unmodeled = sorted({r[1] for r in rows.get("learn_unmodeled_any", ()) if r[0] == run})
    return {
        "present": True,
        "campaign": campaign,
        "learn": learn,
        "closed": bool([r for r in rows.get("learn_unmodeled_closed", ()) if r[0] == model and r[1] in learns]),
        "predictions": len([r for r in rows.get("learn_prediction", ()) if r[0] == run]),
        "observations": len([r for r in rows.get("learn_observation", ()) if r[0] == run]),
        # (tape, req, op, predicted, observed) per disagreement: the step is named
        "counterexamples": sorted([list(r[1:]) for r in rows.get("learn_counterexample", ()) if r[0] == run],
                                  key=canonical_json),
        "unmodeled_ops": unmodeled,
        "consistent_ops": sorted({r[1] for r in rows.get("learn_consistent", ()) if r[0] == run}),
    }


def exclusions_applied(relations, run: str, op: str) -> list[str]:
    rows = dict(relations) if not isinstance(relations, dict) else relations
    return sorted({r[2] for r in rows.get("exclusion_applied", ()) if r[0] == run and r[1] == op})


def undeclared_tables(relations, decls, run: str, op: str) -> dict[str, list[str]]:
    """Per side, the tables ``undeclared_write(run, op, _)`` names that that side wrote for a request of ``op``."""
    rows = dict(relations) if not isinstance(relations, dict) else relations
    undeclared = {r[2] for r in rows.get("undeclared_write", ()) if r[0] == run and r[1] == op}
    requests = {r[1] for r in rows.get("replay_request", ()) if r[0] == run and r[4] == op}
    out = {}
    for side in ("php", "go"):
        written = {r[2] for r in rows.get(f"{side}_effect", ()) if r[0] == run and r[1] in requests}
        out[side] = sorted(undeclared & written)
    return out


def blocking_premise(relations, run: str, op: str, index: str = SYNTHETIC_INDEX) -> dict[str, Any] | None:
    """The first premise of op_qualified that blocks ``op`` in ``run``, or ``None`` when it is qualified."""
    rows = dict(relations) if not isinstance(relations, dict) else relations
    if any(r[0] == index and r[1] == run and r[2] == op for r in rows.get("op_qualified", ())):
        return None
    models = {r[0] for r in rows.get("model_describes_run", ()) if r[1] == run}

    def holds(name: str) -> bool:
        for r in rows.get(name, ()):
            if name == "op_declared" and r == (index, op):
                return True
            if name == "index_describes_replay" and r == (index, run):
                return True
            if name in _RUN_ONLY and r == (run,):
                return True
            if name == "model_describes_run" and r[1] == run:
                return True
            if name == "model_scope_exclusions_closed" and r[0] in models:
                return True
            if name == "model_well_formed" and r[0] in models:
                return True
            if name == "model_checker_admitted" and any(
                    w[0] in models and w[1:3] == r for w in rows.get("model_well_formed", ())):
                return True
            if r[:2] == (run, op):
                return True
        return False

    for name, is_blocker in _BLOCKING_ORDER:
        present = holds(name)
        if is_blocker and present:
            return {"relation": name, "holds": True}
        if not is_blocker and not present:
            return {"relation": name, "holds": False}
    return {"relation": "op_qualified_rt", "holds": False}


def qualification(semantic: str | None, operational: str | None,
                  blocking: dict[str, Any] | None) -> str:
    """One word for what happened to ``op_qualified``, with *pending* split out.

    ``"qualified"`` when the claim is supported and complete; ``"pending
    <relation>"`` when the only premise that blocked it is one of
    ``PENDING_PREMISES`` -- a premise no receipt can satisfy yet because the
    artefact that would satisfy it (the Stage D typed checker) does not exist;
    ``"unsupported"`` otherwise.  The split matters to a consumer gate: a
    pending op is one whose every checkable premise held, and reporting it as
    unsupported would read as a finding against the port that nobody made.
    """
    if semantic == "supported" and operational == "complete":
        return QUALIFICATION_QUALIFIED
    if blocking and not blocking.get("holds") and blocking.get("relation") in PENDING_PREMISES:
        return f"pending {blocking['relation']}"
    return QUALIFICATION_UNSUPPORTED


def _explanation_summary(explanation: dict[str, Any]) -> dict[str, Any]:
    """Persist the reviewable proof walk, not a duplicate embedded certificate."""
    return {key: value for key, value in explanation.items() if key != "certificate"}


def _fact(decls, relation: str, values: dict[str, Any], evidence_id: str, source: str, *,
          kind: str = "fact", depends_on=()) -> tuple[Atom, Evidence]:
    decl = decls[relation]
    atom = Atom(relation, tuple(Constant(values[column.name], column.type) for column in decl.columns))
    context = {name: values[name] for name in decl.context_indices}
    return atom, Evidence(evidence_id, atom, Context.from_mapping(context), source, tuple(depends_on), kind)


@dataclass
class ReplayJoin:
    receipt_dir: Path
    receipt: dict[str, Any]
    run: str
    exported: Any
    contract_findings: list[str] = field(default_factory=list)
    bundle: Bundle | None = None
    ops: tuple[str, ...] = ()
    index: str = SYNTHETIC_INDEX
    assumption_ids: tuple[str, ...] = ()
    result: Any = None
    """``DifferentialResult`` (two kernels) or ``ThreeWayResult`` (three) when the kernels agree."""
    mismatch: Any = None
    """The disagreeing result when they do not."""
    kernels: str = "two"
    """``"two"`` (python, souffle) or ``"three"`` (plus souffle-compiled)."""
    checker: CompiledChecker | None = None
    """The compiled checker the three-kernel evaluation ran, for provenance."""
    certificates: dict[str, dict[str, Any]] = field(default_factory=dict)
    """The first claim row's certificate per claim id."""
    row_certificates: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    """Every claim row's certificate per claim id (an open claim such as exclusion_applied has one per table)."""
    invalidations: dict[str, assumptions.Invalidation] = field(default_factory=dict)
    """``invalidate`` results by assumption id, in call order."""

    @property
    def model_absent(self) -> bool:
        return not self.receipt.get("model")

    def claim_id(self, kind: str, op: str) -> str:
        return f"claim-{kind}-{op}"

    def report(self):
        return self.result.python if self.result is not None else (self.mismatch.python if self.mismatch else None)

    def closures(self) -> list[Any]:
        """Every agreeing kernel's report, python first."""
        if self.result is None:
            return []
        reports = [self.result.python, self.result.souffle]
        if getattr(self.result, "compiled", None) is not None:
            reports.append(self.result.compiled)
        return reports

    def verdict(self, claim_id: str) -> dict[str, Any] | None:
        report = self.report()
        if report is None:
            return None
        claim = next((c for c in report.claims if c.key == claim_id), None)
        if claim is None:
            return None
        return {"semantic": claim.semantic, "operational": claim.operational,
                "missing_premises": list(claim.missing_premises)}


def build(directory: Path) -> ReplayJoin:
    receipt = json.loads((directory / replay_facts.RECEIPT_FILE).read_text(encoding="utf-8"))
    run = receipt["run"]
    exported = replay_facts.export_bundle(directory, run=run)
    join = ReplayJoin(directory, receipt, run, exported)
    if exported.status != replay_facts.STATUS_COMPLETE:
        join.contract_findings = [f"exporter refused the receipt ({exported.status}): {m}" for m in exported.messages]
        return join
    pack = pack_bundle()
    decls = {decl.name: decl for decl in (*exported.bundle.relations, *pack.relations)}
    ops = tuple(sorted({fact.terms[4].value for fact in exported.bundle.facts if fact.relation == "replay_request"}))
    join.ops = ops
    additions = [
        _fact(decls, "run_nonce_observed", {"run": run, "nonce": receipt["nonce"]},
              _row_id("reviewer", "claim-time", "run_nonce_observed", [run, receipt["nonce"]]), REVIEWER_SOURCE),
        _fact(decls, "snapshot_observed", {"snapshot": receipt["snapshot"]},
              _row_id("reviewer", "claim-time", "snapshot_observed", [receipt["snapshot"]]), REVIEWER_SOURCE),
    ]
    if not join.model_absent:  # the strict exporter never exports an empty model; kept for the summary
        additions.append(_fact(decls, "model_observed", {"model": receipt["model"]},
                               _row_id("reviewer", "claim-time", "model_observed", [receipt["model"]]), REVIEWER_SOURCE))
    assumptions = [_fact(decls, "index_describes_replay", {"index": SYNTHETIC_INDEX, "run": run},
                         _row_id("reviewer", "assumed", "index_describes_replay", [SYNTHETIC_INDEX, run]),
                         INDEX_ASSUMPTION_SOURCE, kind="assumption", depends_on=[f"external:index:{SYNTHETIC_INDEX}"])]
    for op in ops:
        assumptions.append(_fact(decls, "op_declared", {"index": SYNTHETIC_INDEX, "op": op},
                                 _row_id("php", "assumed", "op_declared", [SYNTHETIC_INDEX, op]),
                                 CENSUS_ASSUMPTION_SOURCE, kind="assumption",
                                 depends_on=[f"external:index:{SYNTHETIC_INDEX}"]))
    join.assumption_ids = tuple(record.id for _, record in assumptions)
    present = {record.atom.relation: record.id for record in exported.bundle.evidence
               if record.atom.relation in MODEL_WITNESSES}
    present.update({record.atom.relation: record.id for _, record in additions if record.atom.relation in MODEL_WITNESSES})
    # the effect rows the model's closed write set does not cover, per op (what
    # the undeclared_write rules will derive from), so the why-not can name them
    exported_rows = {name: [] for name in ("replay_request", "php_effect", "go_effect", "model_writes", "model_writes_closed",
                                           "model_scope_exclusion", "model_scope_exclusions_closed",
                                           "learn_run", "learn_unmodeled", "learn_unmodeled_closed")}
    evidence_of: dict[tuple[str, tuple], str] = {}
    for record in exported.bundle.evidence:
        if record.atom.relation in exported_rows:
            row = tuple(term.value for term in record.atom.terms)
            exported_rows[record.atom.relation].append(row)
            evidence_of[(record.atom.relation, row)] = record.id
    declared = {(r[1], r[2]) for r in exported_rows["model_writes"]}
    closed_ops = {r[1] for r in exported_rows["model_writes_closed"]}
    exclusions_closed = bool(exported_rows["model_scope_exclusions_closed"])
    excluded = {r[1] for r in exported_rows["model_scope_exclusion"]} if exclusions_closed else set()
    learn_rows = {name: exported_rows[name] for name in
                  ("learn_run", "learn_unmodeled", "learn_unmodeled_closed")}
    claims, diagnostics, outputs = [], [], []
    for op in ops:
        qualified = Claim("op_qualified", (Constant(SYNTHETIC_INDEX, "digest"), Constant(run, "symbol"), Constant(op, "symbol")),
                          Context.from_mapping({"index": SYNTHETIC_INDEX, "run": run}), id=join.claim_id("qualified", op))
        constrains = Claim("corpus_constrains", (Constant(run, "symbol"), Constant(op, "symbol")),
                           Context.from_mapping({"run": run}), id=join.claim_id("corpus-constrains", op))
        undeclared = Claim("undeclared_write", (Constant(run, "symbol"), Constant(op, "symbol"), Variable("table")),
                           Context.from_mapping({"run": run}), id=join.claim_id("undeclared-write", op))
        applied = Claim("exclusion_applied", (Constant(run, "symbol"), Constant(op, "symbol"), Variable("table")),
                        Context.from_mapping({"run": run}), id=join.claim_id("exclusions-applied", op))
        claims.extend([qualified, constrains, undeclared, applied])
        if learn_rows["learn_run"]:
            # only when a campaign is bound to this run: a claim about an absent
            # campaign would be unresolved for want of the campaign, not for want
            # of consistency, and would read as a finding about the model
            claims.append(Claim("learn_consistent", (Constant(run, "symbol"), Constant(op, "symbol")),
                                Context.from_mapping({"run": run}), id=join.claim_id("learn-consistent", op)))
            downgrading = [evidence_of[("learn_unmodeled", row)] for row in learn_rows["learn_unmodeled"]
                           if row[2] == op and learn_rows["learn_unmodeled_closed"]]
            if downgrading:
                diagnostics.append(DiagnosticRule("learn_unmodeled", "observation", "complete", (),
                                                  claim_id=qualified.id))
                outputs.append(OutputTemplate(
                    "missing_premise", qualified.id, relation="learn_unmodeled",
                    fields=(("reason", TemplateValue("constant", "", "symbol", LEARN_UNMODELED_REASON)),),
                    requires_any_evidence=tuple(sorted(downgrading)), when_claim="unresolved"))
        requests = {r[1] for r in exported_rows["replay_request"] if r[4] == op}
        offending = [evidence_of[(side, row)] for side in ("php_effect", "go_effect") for row in exported_rows[side]
                     if row[1] in requests and op in closed_ops and exclusions_closed
                     and (op, row[2]) not in declared and row[2] not in excluded]
        if offending:
            for side in ("php_effect", "go_effect"):
                diagnostics.append(DiagnosticRule(side, "observation", "complete", ("run",), claim_id=qualified.id))
            outputs.append(OutputTemplate(
                "missing_premise", qualified.id, relation="model_writes",
                fields=(("reason", TemplateValue("constant", "", "symbol", UNDECLARED_REASON)),),
                requires_any_evidence=tuple(sorted(offending)), when_claim="unresolved"))
        for relation in MODEL_WITNESSES:
            context = ("run",) if relation in ("model_describes_run", "model_admissible_closed") else ()
            diagnostics.append(DiagnosticRule(relation, "observation", "complete", context, claim_id=qualified.id))
            if relation == "model_checker_admitted" and "model_well_formed" not in present:
                # "the reviewer admits no checker at the version that certified the model" is
                # not a premise a receipt with no certificate at all is missing: the missing
                # premise is the certificate, and naming both would report one gap twice.
                continue
            excludes = (present[relation],) if relation in present else ()
            outputs.append(OutputTemplate(
                "missing_premise", qualified.id, relation=relation,
                fields=(("reason", TemplateValue("constant", "", "symbol", REASONS[relation])),),
                excludes_evidence=excludes, when_claim="unresolved"))
    join.bundle = combine(
        exported.bundle, pack,
        facts=[atom for atom, _ in (*additions, *assumptions)],
        evidence=[record for _, record in (*additions, *assumptions)],
        claims=claims, diagnostics=diagnostics, outputs=outputs,
        metadata={"experiment": "target-go replay receipt join (Phase 4, judge side)",
                  "synthetic_index": SYNTHETIC_INDEX, "model_absent": join.model_absent,
                  "assumptions": list(join.assumption_ids)})
    return join


def evaluate_join(join: ReplayJoin, replay_root: str, *, kernels: str = "two",
                  checker: CompiledChecker | None = None, cache_dir: str | Path = ".capcov/compiled",
                  executable: str = "souffle") -> ReplayJoin:
    """Run the kernels and certify every claim row from every closure.

    ``kernels="two"`` is the pairwise differential (python vs interpreted
    Souffle, with its shrinker); ``kernels="three"`` adds the compiled Souffle
    checker (``checker``, or compiled into ``cache_dir`` from the bundle's
    fact-independent program) through ``compare_three``.  In either mode the
    claim rows, the certificates and the rechecks must agree across every
    closure; a kernel disagreement is recorded on ``join.mismatch`` and
    nothing is certified.
    """
    if join.bundle is None:
        return join
    if kernels not in ("two", "three"):
        raise ValueError("kernels must be 'two' or 'three'")
    join.kernels = kernels
    if kernels == "three" and checker is None:
        # compile up front so the provenance of the binary that judged the
        # receipt is recorded even when the caller did not supply a checker
        checker = compile_program(program_for_pack(join.bundle), executable=executable,
                                  cache_dir=cache_dir)
    join.checker = checker
    try:
        if kernels == "two":
            join.result = compare(join.bundle, replay_root=replay_root)
        else:
            join.result = compare_three(join.bundle, checker=checker, replay_root=replay_root,
                                        cache_dir=cache_dir, executable=executable)
    except (DifferentialMismatch, CompiledKernelMismatch) as exc:
        join.mismatch = exc.result
        return join
    join.certificates, join.row_certificates = assumptions.certify_claims(join.bundle, join.result)
    return join


def _explain(join: ReplayJoin):
    """Pack-specific detail for an invalidated claim: what blocks it, and the tables to blame."""
    qualified = {join.claim_id("qualified", op): op for op in join.ops}

    def explain(claim_id: str, relations) -> dict[str, Any]:
        op = qualified.get(claim_id)
        if op is None:
            return {}
        rows = dict(relations)
        out: dict[str, Any] = {"blocking_premise": blocking_premise(rows, join.run, op)}
        if out["blocking_premise"] and out["blocking_premise"]["relation"] == "undeclared_any":
            out["undeclared_tables"] = undeclared_tables(rows, None, join.run, op)
        return out

    return explain


def assumption_registry(join: ReplayJoin, *, strict_impact: bool = True) -> dict[str, Any]:
    """The A2 registry of the join's combined bundle (contract: ``assumptions.json``).

    ``strict_impact=False`` degrades a truncated ``ground.impact`` prediction to
    a ``truncated`` marker instead of refusing, for the embedded copy ``summary``
    carries: a bigger bundle must not make the pilot summary unanswerable.
    """
    if join.bundle is None:
        return {"registry_version": assumptions.REGISTRY_VERSION, "run": join.run,
                "combined_bundle_digest": None, "assumptions": [], "shared_assumptions": [],
                "unreferenced": []}
    report = join.report()
    return assumptions.registry(join.bundle, join.row_certificates, run=join.run,
                                relations=report.relations if report is not None else None)


def invalidate(join: ReplayJoin, identifier: str, replay_root: str) -> assumptions.Invalidation:
    """Withdraw one registered assumption and record what every claim did.

    ``identifier`` is an ``asm:`` id or the evidence id of an assumption row.
    The join must have been evaluated: the baseline verdicts and certificates
    are read from it rather than recomputed.
    """
    if join.bundle is None or join.result is None:
        raise assumptions.InvalidationError("the join must be evaluated before an assumption is withdrawn")
    result = assumptions.invalidate(
        join.bundle, identifier, replay_root=replay_root,
        baseline_result=join.result, baseline_row_certificates=join.row_certificates,
        explain=_explain(join))
    join.invalidations[result.assumption_id] = result
    return result


def summary(join: ReplayJoin) -> dict[str, Any]:
    """The per-op report of a judged receipt: verdicts, qualification, blocking premise."""
    if join.bundle is None:
        return {"status": "blocked", "run": join.run, "receipt": join.receipt_dir.name,
                "contract_findings": list(join.contract_findings)}
    out: dict[str, Any] = {
        "status": "complete" if join.result is not None and join.result.matched else "kernel-mismatch",
        "run": join.run,
        "replay_identity": dict(join.exported.bundle.metadata)["replay_digest"],
        "replay_bundle_digest": replay_facts.bundle_digest(join.exported.bundle),
        "combined_bundle_digest": replay_facts.bundle_digest(join.bundle),
        "model_absent": join.model_absent,
        "kernels": ["python", "souffle"] + (["souffle-compiled"] if join.kernels == "three" else []),
        "synthetic_index": SYNTHETIC_INDEX,
        "assumption_ids": list(join.assumption_ids),
        "ops": list(join.ops),
    }
    registry = assumption_registry(join, strict_impact=False)
    # the run-independent registry entries; "assumption_ids" above stays the evidence-id list
    out["assumptions"] = registry["assumptions"]
    out["shared_assumptions"] = registry["shared_assumptions"]
    relations = dict(join.report().relations) if join.report() is not None else {}
    for op in join.ops:
        constrains = join.verdict(join.claim_id("corpus-constrains", op)) or {}
        qualified = join.verdict(join.claim_id("qualified", op)) or {}
        undeclared = join.verdict(join.claim_id("undeclared-write", op)) or {}
        blocking = blocking_premise(relations, join.run, op) if relations else None
        tables = undeclared_tables(relations, None, join.run, op) if relations else {}
        applied = exclusions_applied(relations, join.run, op) if relations else []
        kinds = {record.id: record.kind for record in join.bundle.evidence}
        leaves = set()
        for claim_id in (join.claim_id("qualified", op), join.claim_id("exclusions-applied", op)):
            for cert in join.row_certificates.get(claim_id, ()):
                leaves.update(cert.get("leaves", ()))
        assumption_leaves = sorted(leaf for leaf in leaves if kinds.get(leaf) == "assumption"
                                   and leaf.split(":")[2] == "model_scope_exclusion")
        entry = {"corpus_constrains": constrains.get("semantic") == "supported",
                 "op_qualified": qualified.get("semantic"),
                 "operational": qualified.get("operational"),
                 # "qualified" / "pending <relation>" / "unsupported": a pending op passed
                 # every premise that is checkable today (see PENDING_PREMISES)
                 "qualification": qualification(qualified.get("semantic"), qualified.get("operational"), blocking),
                 # template-rendered absent leaves; the evaluator's claim-id fallback is never reported here
                 "missing_premise": [json.loads(item)["relation"] for item in qualified.get("missing_premises", [])
                                     if item.startswith("{")],
                 "undeclared_write": undeclared.get("semantic"),
                 "blocking_premise": blocking,
                 "exclusions_applied": applied,
                 "assumption_leaves": len(assumption_leaves),
                 "assumption_leaf_ids": assumption_leaves}
        target = (SYNTHETIC_INDEX, join.run, op)
        if qualified.get("semantic") == "supported":
            explanation = why(join.bundle, relations, "op_qualified", target)
            explanation["shared_assumptions"] = list(
                shared_assumptions(join.bundle, explanation["certificate"]))
        else:
            # Explain the actual gate body.  Asking why-not of the thin
            # op_qualified wrapper would only say op_qualified_rt is absent
            # and hide the useful missing or blocking premise beneath it.
            explanation = why_not(join.bundle, relations, "op_qualified_rt", target)
        entry["explanation"] = _explanation_summary(explanation)
        requests = {r[1] for r in relations.get("replay_request", ()) if r[0] == join.run and r[4] == op}
        # the ordering, cross-request and cross-run rows of this op's requests (digest-free, reportable)
        entry["effect_order"] = {
            "violations": sorted([list(r[1:]) for r in relations.get("effect_order_violation", ())
                                  if r[0] == join.run and r[2] in requests], key=canonical_json),
            "respected": sorted([list(r[1:]) for r in relations.get("effect_order_respected", ())
                                 if r[0] == join.run and r[2] in requests], key=canonical_json),
            "exercised": (join.run, op) in set(relations.get("effect_order_exercised", ())),
        }
        entry["learn_consistent"] = (join.verdict(join.claim_id("learn-consistent", op)) or {}).get("semantic")
        entry["learn_unmodeled"] = (join.run, op) in set(relations.get("learn_unmodeled_any", ()))
        entry["repeat_delete"] = {
            "repeats": sorted([list(r[1:]) for r in relations.get("repeat_delete", ())
                               if r[0] == join.run and r[1] in requests], key=canonical_json),
            "violations": sorted([list(r[1:]) for r in relations.get("repeat_delete_violation", ())
                                  if r[0] == join.run and r[1] in requests], key=canonical_json),
            "not_found": sorted([r[1] for r in relations.get("repeat_delete_not_found", ()) if r[0] == join.run]),
        }
        if blocking and blocking["relation"] == "undeclared_any":
            entry["blocked_by"] = "blocked by undeclared writes: " + json.dumps(tables, sort_keys=True)
            entry["undeclared_tables"] = tables
        if qualified.get("semantic") == "supported" and applied:
            entry["qualified_under_exclusions"] = (f"qualified under {len(applied)} reviewer exclusions: "
                                                   + ", ".join(applied))
        out[op] = entry
    out["exclusions"] = exclusions(relations, join.run) if relations else []
    # the Stage D premise: what typechecked the model this run is judged against
    out["model_well_formed"] = well_formed_certificate(relations, join.run) if relations else "missing"
    out["learn"] = learn_summary(relations, join.run) if relations else {"present": False}
    out["stability"] = {
        "rows": sorted([list(r[1:]) for r in relations.get("replay_stability", ()) if r[0] == join.run], key=canonical_json),
        "oracle_stable": (join.run,) in set(relations.get("oracle_stable", ())),
        "oracle_unstable": (join.run,) in set(relations.get("oracle_unstable", ())),
    } if relations else {}
    return out


def _kernel_digests(join: ReplayJoin) -> dict[str, Any]:
    outcome = join.result if join.result is not None else join.mismatch
    digests: dict[str, Any] = {
        "matched": join.result is not None and join.result.matched,
        "python_digest": outcome.python.canonical_digest,
        "souffle_digest": outcome.souffle.canonical_digest,
    }
    compiled = getattr(outcome, "compiled", None)
    if compiled is not None:
        digests["compiled_digest"] = compiled.canonical_digest
        digests["closure_digest_equal"] = outcome.closure_digest_equal
    return digests


def _write(path: Path, document: Any) -> None:
    path.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def write_artifacts(join: ReplayJoin, out_dir: Path) -> dict[str, Any]:
    """Digests, counts and verdicts only; no source text and no local paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    receipts = join.receipt.get("receipts", {})
    digests_only = {k: v for k, v in receipts.items()
                    if isinstance(v, (int, bool)) or (isinstance(v, str) and k.endswith("sha256"))
                    or k in {"canonicalizer", "recorded_at"}}
    document = {
        "pilot": "target-go replay receipt join (Phase 4, judge side)",
        "receipt": {"run": join.run, "nonce": join.receipt.get("nonce"), "snapshot": join.receipt.get("snapshot"),
                    "model": join.receipt.get("model"), "php_commit": join.receipt.get("php_commit"),
                    "go_commit": join.receipt.get("go_commit"), "closed": join.receipt.get("closed"),
                    "receipts": digests_only},
        "export": {"status": join.exported.status, "row_counts": dict(join.exported.counts),
                   "messages": [m for m in join.exported.messages if "receipt directory" not in m]},
        "contract_findings": list(join.contract_findings),
        "join": summary(join),
        "kernels": None if join.result is None and join.mismatch is None else _kernel_digests(join),
        # the compiled checker's provenance verbatim: program, souffle bytes,
        # compiler configuration and the sha256 of the binary that actually ran
        "compiled": join.checker.provenance() if join.checker is not None else None,
        "certificates": {claim_id: {"sha256": hashlib.sha256(canonical_json(cert).encode()).hexdigest(),
                                    "leaves": len(cert["leaves"]), "nodes": cert["nodes"], "truncated": cert["truncated"]}
                         for claim_id, cert in join.certificates.items()},
    }
    # the registry entries summary() already computed, restated as the standalone A2 document
    registry = document["join"].get("assumptions")
    registry_document = {"registry_version": assumptions.REGISTRY_VERSION, "run": join.run,
                         "combined_bundle_digest": (replay_facts.bundle_digest(join.bundle)
                                                    if join.bundle is not None else None),
                         "assumptions": registry if registry is not None else [],
                         "shared_assumptions": document["join"].get("shared_assumptions", []),
                         "unreferenced": sorted({entry["assumption_id"] for entry in (registry or [])
                                                 if not entry["carried_by"]})}
    _write(out_dir / "assumptions.json", registry_document)
    invalidations = {}
    for assumption_id, invalidation in join.invalidations.items():
        name = f"invalidation-{invalidation.short_id}.json"
        _write(out_dir / name, invalidation.as_dict())
        invalidations[assumption_id] = name
        for claim_id, certs in invalidation.row_certificates.items():
            for position, cert in enumerate(certs):
                suffix = "" if position == 0 else f"-{position}"
                _write(out_dir / f"invalidation-{invalidation.short_id}-certificate-{claim_id}{suffix}.json", cert)
    document["assumptions"] = {"registry": "assumptions.json", "invalidations": invalidations}
    for claim_id, certs in join.row_certificates.items():
        for position, cert in enumerate(certs):
            name = f"certificate-{claim_id}.json" if position == 0 else f"certificate-{claim_id}-{position}.json"
            _write(out_dir / name, cert)
    _write(out_dir / "receipt.json", document)
    return document


__all__ = ["SYNTHETIC_INDEX", "REVIEWER_SOURCE", "CENSUS_ASSUMPTION_SOURCE", "INDEX_ASSUMPTION_SOURCE",
           "MODEL_WITNESSES", "REASONS", "UNDECLARED_REASON", "PENDING_PREMISES",
           "QUALIFICATION_QUALIFIED", "QUALIFICATION_UNSUPPORTED", "qualification",
           "ReplayJoin", "build", "evaluate_join",
           "summary", "write_artifacts", "blocking_premise", "undeclared_tables", "exclusions",
           "exclusions_applied", "well_formed_certificate", "learn_summary", "LEARN_UNMODELED_REASON",
           "assumption_registry", "invalidate"]
