"""Build the replay review cases from receipt variants of ``fixtures/replay_receipt_min``.

Every case's ``facts`` section is what ``replay_facts.export_bundle`` emits
over a small edit of the checked-in receipt (plus the claim-time reviewer /
census rows a judge adds), so the cases can never drift from the exporter:
``test_replay_corpus_schema`` regenerates each case through ``build`` and
requires the checked-in file to be identical up to its ``expected`` table.
Claims, diagnostics, output templates and the reviewer's verdict table
(``REVIEW``) are hand-authored here; ``python -m replay_rules.cases`` (from
``tests/claim_semantics``) rewrites the case files, refusing to write a case
whose Python-evaluator verdict, status, missing premises or discrepancies
differ from ``REVIEW``.  Support leaves are recorded from the evaluator and
frozen in the file; the differential and certificate tests then bind both
kernels to them.
"""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

SRC = Path(__file__).resolve().parents[3] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from capcov.claims import canonical_json  # noqa: E402
from capcov.claims.replay import replay_facts  # noqa: E402

try:
    from .adapter import CASES_DIR, PACK_ID, REJECTED_DIR
except ImportError:  # unittest discover -s imports this directory as top-level
    from replay_rules.adapter import CASES_DIR, PACK_ID, REJECTED_DIR

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "replay_receipt_min"
RUN = "run-fixture-1"
RECEIPT = json.loads((FIXTURE / "receipt.json").read_text(encoding="utf-8"))
MODEL = RECEIPT["model"]
NONCE = RECEIPT["nonce"]
SNAPSHOT = RECEIPT["snapshot"]
# The synthetic PHP census index the op_declared rows and index_describes_replay bind to.
INDEX = hashlib.sha256(b"rules-replay-v1 synthetic php census index").hexdigest()
CREATE, CLOSE, DELETE = "issues.create", "issues.close", "delete-issue"
OPS = (CREATE, CLOSE, DELETE)
# the claim-id suffix of each op (claim-qualified-<suffix>)
SUFFIX = {CREATE: "create", CLOSE: "close", DELETE: "delete"}
# the delete-issue pair of the fixture: req-4 commits the delete, req-5 repeats it (404, no effects)
DELETE_TARGET = "DELETE /api/issues/1"
FIRST_DELETE, REPEAT_DELETE = "req-4", "req-5"
OTHER_NONCE = hashlib.sha256(b"rules-replay-v1 another nonce").hexdigest()
# the model of a *different* Shen domain model: what a checker certificate for the
# previous model names (case 28), which must not join this run's model
OTHER_MODEL = hashlib.sha256(b"rules-replay-v1 another model").hexdigest()
# synthetic runtime identity used by the checker authority controls
CHECKER, CHECKER_VERSION = "stage-d-typecheck", "0.1-pending"
CHECKER_CERTIFICATE = hashlib.sha256(b"pending: checker not yet built").hexdigest()
CHECKER_BINARY = "b" * 64
DISAGREEING_STATE = hashlib.sha256(b"rules-replay-v1 php post-state outside the model").hexdigest()

REVIEWER_SOURCE = "reviewer claim-time observation"
CENSUS_SOURCE = "php-census target-cloud route census v1"
REJECTED_SOURCE = "shen shen-model-host v1"
REJECTED_CLOSURE_SOURCE = "replay capcov.claims.replay.replay_facts model-admissible-closed-v1"


def claim_id_for(op: str) -> str:
    return f"claim-qualified-{SUFFIX[op]}"

Edit = Callable[[Any], Any]


# ---------------------------------------------------------------------------
# receipt variants


@contextmanager
def variant(edits: dict[str, Edit]) -> Iterator[Path]:
    """A copy of the fixture with ``<stem>.json`` documents rewritten by ``edits``
    (``None`` deletes the file; the callable receives ``None`` when absent)."""
    with tempfile.TemporaryDirectory(prefix="replay-case-") as d:
        root = Path(d) / "receipt"
        shutil.copytree(FIXTURE, root)
        for stem, edit in edits.items():
            path = root / f"{stem}.json"
            document = json.loads(path.read_text()) if path.exists() else None
            replacement = edit(copy.deepcopy(document))
            if replacement is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(json.dumps(replacement, indent=1, sort_keys=True))
        yield root


def _edit_row(index: int, **changes: Any) -> Edit:
    def edit(document: Any) -> Any:
        document["rows"][index].update(changes)
        return document
    return edit


def _append_row(**row: Any) -> Edit:
    def edit(document: Any) -> Any:
        document["rows"].append(row)
        return document
    return edit


def _drop_row(index: int) -> Edit:
    def edit(document: Any) -> Any:
        del document["rows"][index]
        return document
    return edit


# ---------------------------------------------------------------------------
# claim-time rows (what a judge adds to an exported receipt)


def claim_time_id(prefix: str, relation: str, row: list[Any]) -> str:
    return f"{prefix}:claim-time:{relation}:{replay_facts.row_digest(relation, row)[:12]}"


def _claim_time_fact(relation: str, arg_order: list[str], args: list[Any], context: dict[str, Any],
                     prefix: str, source: str, depends_on: list[str]) -> dict[str, Any]:
    return {"id": claim_time_id(prefix, relation, args), "kind": "fact", "relation": relation,
            "arg_order": arg_order, "args": args, "context": context, "source": source,
            "provenance": {"depends_on": depends_on}}


def reviewer_facts(*, nonce: str | None = NONCE, snapshot: str | None = SNAPSHOT,
                   model: str | None = MODEL) -> list[dict[str, Any]]:
    out = []
    if nonce is not None:
        out.append(_claim_time_fact("run_nonce_observed", ["run", "nonce"], [RUN, nonce], {},
                                    "reviewer", REVIEWER_SOURCE, []))
    if snapshot is not None:
        out.append(_claim_time_fact("snapshot_observed", ["snapshot"], [snapshot], {},
                                    "reviewer", REVIEWER_SOURCE, []))
    if model is not None:
        out.append(_claim_time_fact("model_observed", ["model"], [model], {},
                                    "reviewer", REVIEWER_SOURCE, []))
    return out


def census_facts(ops: tuple[str, ...] = OPS) -> list[dict[str, Any]]:
    return [_claim_time_fact("op_declared", ["index", "op"], [INDEX, op], {"index": INDEX},
                             "php", CENSUS_SOURCE, [f"external:index:{INDEX}"]) for op in ops]


# ---------------------------------------------------------------------------
# exporter rows -> case facts


def reviewer_admissions(receipt_dir: Path) -> list[dict[str, str]]:
    """Synthetic corpus policy for each checked operation, outside its receipt."""
    path = receipt_dir / "model_operation_checked.json"
    if not path.is_file():
        return []
    rows = json.loads(path.read_text(encoding="utf-8"))["rows"]
    return [{"producer": "reviewer synthetic-corpus-policy", **row} for row in rows]


def exported_facts(receipt_dir: Path, *, drop_relations: tuple[str, ...] = (),
                   reviewer_authority: bool = True) -> tuple[list[dict[str, Any]], str]:
    result = replay_facts.export_bundle(
        receipt_dir, run=RUN, describes_indexes=(INDEX,),
        reviewer_admissions=reviewer_admissions(receipt_dir) if reviewer_authority else ())
    if result.status != replay_facts.STATUS_COMPLETE:
        raise AssertionError(f"export failed: {result.status} {result.messages}")
    bundle = result.bundle
    decls = {decl.name: decl for decl in bundle.relations}
    facts = []
    for record in bundle.evidence:
        if record.atom.relation in drop_relations:
            continue
        decl = decls[record.atom.relation]
        facts.append({"id": record.id, "kind": record.kind, "relation": decl.name,
                      "arg_order": [column.name for column in decl.columns],
                      "args": [term.value for term in record.atom.terms],
                      "context": record.context.as_dict(), "source": record.source,
                      "provenance": {"depends_on": list(record.depends_on)}})
    order = [decl.name for decl in bundle.relations]
    facts.sort(key=lambda entry: (order.index(entry["relation"]), canonical_json(entry["args"])))
    return facts, dict(bundle.metadata)["replay_digest"]


def ir_facts(entries: list[dict[str, Any]], decls: dict[str, Any]) -> list[tuple[Any, Any]]:
    """Case-format fact entries as ``(Atom, Evidence)`` pairs for ``combine``."""
    from capcov.claims import Atom, Constant, Context, Evidence
    out = []
    for entry in entries:
        decl = decls[entry["relation"]]
        atom = Atom(decl.name, tuple(Constant(value, column.type)
                                     for value, column in zip(entry["args"], decl.columns)))
        out.append((atom, Evidence(entry["id"], atom, Context.from_mapping(entry["context"]), entry["source"],
                                   tuple(entry["provenance"]["depends_on"]), entry["kind"])))
    return out


def find_id(facts: list[dict[str, Any]], relation: str, **columns: Any) -> str:
    matches = [entry["id"] for entry in facts if entry["relation"] == relation
               and all(dict(zip(entry["arg_order"], entry["args"])).get(k) == v for k, v in columns.items())]
    if len(matches) != 1:
        raise LookupError(f"{relation} {columns}: {len(matches)} matches")
    return matches[0]


# ---------------------------------------------------------------------------
# claims, diagnostics and templates


def _claim(claim_id: str, relation: str, arg_order: list[str], args: list[Any], context: dict[str, Any],
           reading: str, diagnostics: list[dict[str, Any]] = ()) -> dict[str, Any]:
    return {"id": claim_id, "relation": relation, "arg_order": arg_order, "args": args, "context": context,
            "quantifier": "exists", "domain": None, "mappings": [], "diagnostics": list(diagnostics),
            "reading": reading}


def qualified_claim(op: str, reading: str, diagnostics: list[dict[str, Any]]) -> dict[str, Any]:
    return _claim(claim_id_for(op), "op_qualified", ["index", "run", "op"],
                  [INDEX, RUN, op], {"index": INDEX, "run": RUN}, reading, diagnostics)


def witness_diagnostics(op: str) -> list[dict[str, Any]]:
    """Operation authority rows are relevant only when their operation matches the claim."""
    return [*WITNESS_DIAGNOSTICS,
            observation("model_operation_checked", [], {"column": "operation", "operator": "=", "value": op}),
            observation("model_checker_admitted", [], {"column": "operation", "operator": "=", "value": op})]


def observation(trigger: str, context: list[str], predicate: dict[str, Any] | None = None,
                status: str = "complete") -> dict[str, Any]:
    out = {"trigger_relation": trigger, "effect": "observation", "operational_status": status,
           "context_indices": context}
    if predicate:
        out["predicate"] = predicate
    return out


# The witnesses every op_qualified claim weighs.  Each is made relevant to the
# claim by an observation diagnostic so a missing-premise template can be
# suppressed by the witness's presence (excludes_evidence).
WITNESS_DIAGNOSTICS = [observation("model_describes_run", ["run"]), observation("run_nonce_observed", []),
                       observation("snapshot_observed", []), observation("model_observed", []),
                       observation("php_post_states_closed", ["run"]), observation("go_post_states_closed", ["run"]),
                       observation("php_effects_closed", ["run"]), observation("go_effects_closed", ["run"]),
                       observation("model_admissible_closed", ["run"]),
                       observation("model_scope_exclusions_closed", []),
                       observation("php_effect_seqs_closed", ["run"]), observation("go_effect_seqs_closed", ["run"]),
                       observation("model_effect_seqs_closed", ["run"]), observation("replay_request_seqs_closed", ["run"]),
                       observation("php_responses_closed", ["run"]), observation("go_responses_closed", ["run"]),
                       observation("replay_stability_closed", ["run"]),
                       observation("model_well_formed", [])]
WITNESS_REASONS = {
    "model_describes_run": "no model_describes_run witness binds the receipt's model to the run",
    "run_nonce_observed": "the reviewer did not observe the run's nonce",
    "snapshot_observed": "the reviewer did not observe the run's snapshot digest",
    "model_observed": "the reviewer did not observe the model digest",
    "php_post_states_closed": "the harness did not close the PHP post-state table for the run",
    "go_post_states_closed": "the harness did not close the Go post-state table for the run",
    "php_effects_closed": "the harness did not close the PHP effect table for the run",
    "go_effects_closed": "the harness did not close the Go effect table for the run",
    "model_admissible_closed": "the model runner did not close the admissible-state set for the run and model",
    "model_scope_exclusions_closed": "the reviewer did not close the model-scope exclusion set for the model",
    "php_effect_seqs_closed": "the harness did not close the PHP per-statement effect sequence for the run",
    "go_effect_seqs_closed": "the harness did not close the Go per-statement effect sequence for the run",
    "model_effect_seqs_closed": "the model runner did not close the model's effect order for the run and model",
    "replay_request_seqs_closed": "the harness did not close the tape order for the run",
    "php_responses_closed": "the harness did not close the PHP response table for the run",
    "go_responses_closed": "the harness did not close the Go response table for the run",
    "replay_stability_closed": "the harness did not close the cross-run stability table for the run (no bound selftest)",
    "model_well_formed": "no typed checker certified the model's global structure",
    "model_operation_checked": "no successful operation-scoped checker certificate matches this model operation",
    "model_checker_admitted": "the reviewer admits no exact checker, binary, and certificate tuple for this operation",
}


def missing(claim_id: str, relation: str, reason: str, *, excludes: list[str] = (),
            requires: list[str] = ()) -> dict[str, Any]:
    out = {"kind": "missing_premise", "claim_id": claim_id, "relation": relation,
           "fields": {"reason": {"value": reason}}, "when_claim": "unresolved"}
    if excludes:
        out["excludes_evidence"] = list(excludes)
    if requires:
        out["requires_all_evidence"] = list(requires)
    return out


def witness_templates(claim_id: str, facts: list[dict[str, Any]], *, absent: tuple[str, ...] = (),
                      override: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """One ``missing_premise`` template per witness: excluded by the witness row
    when the case carries it; absent witnesses get a template with no exclusion
    (nothing exists to name), so exactly the absent ones fire."""
    out = []
    override = override or {}
    op = next((candidate for candidate, suffix in SUFFIX.items()
               if claim_id == f"claim-qualified-{suffix}"), None)
    for relation, reason in WITNESS_REASONS.items():
        if relation in override:
            out.append(override[relation])
            continue
        if relation in absent:
            out.append(missing(claim_id, relation, reason))
        elif relation in ("model_operation_checked", "model_checker_admitted"):
            checked = [entry for entry in facts if entry["relation"] == "model_operation_checked"
                       and dict(zip(entry["arg_order"], entry["args"])).get("operation") == op]
            matching = [entry for entry in facts if entry["relation"] == relation
                        and dict(zip(entry["arg_order"], entry["args"])).get("operation") == op]
            if relation == "model_checker_admitted" and not checked:
                # No operation certificate means admission is not yet meaningful.
                continue
            if matching:
                out.append(missing(claim_id, relation, reason,
                                   excludes=[entry["id"] for entry in matching]))
            else:
                out.append(missing(claim_id, relation, reason))
        elif relation == "model_well_formed":
            matching = [entry for entry in facts if entry["relation"] == relation
                        and dict(zip(entry["arg_order"], entry["args"])).get("model") == MODEL]
            if matching:
                out.append(missing(claim_id, relation, reason,
                                   excludes=[entry["id"] for entry in matching]))
            else:
                out.append(missing(claim_id, relation, reason))
        else:
            out.append(missing(claim_id, relation, reason, excludes=[find_id(facts, relation)]))
    return out


def discrepancy(claim_id: str, kind: str, requires: list[str], **fields: Any) -> dict[str, Any]:
    values = {"kind": {"value": kind}, **{name: {"value": value} for name, value in fields.items()}}
    return {"kind": "discrepancy", "claim_id": claim_id, "when_claim": "supported", "fields": values,
            "requires_all_evidence": list(requires)}


# ---------------------------------------------------------------------------
# the cases


def _case(stem: str, title: str, seeded_fault: str | None, notes: list[str], facts: list[dict[str, Any]],
          claims: list[dict[str, Any]], outputs: list[dict[str, Any]]) -> dict[str, Any]:
    # the exporter's assumption-kind rows (the reviewer's scope exclusions) file under assumptions
    return {"schema_version": 1, "id": stem, "title": title, "rule_pack": PACK_ID,
            "provenance": {"kind": "synthetic",
                           "source": "replay_facts.export_bundle over an edit of tests/claim_semantics/fixtures/replay_receipt_min",
                           "seeded_fault": seeded_fault},
            "context": {"run": RUN, "model": MODEL, "index": INDEX},
            "review_notes": notes, "facts": [f for f in facts if f["kind"] == "fact"],
            "assumptions": [f for f in facts if f["kind"] == "assumption"], "claims": claims, "outputs": outputs}


def _exclude(*tables: str) -> Edit:
    """Rewrite the fixture's model_scope_exclusions.json to exactly ``tables``."""
    def edit(document: Any) -> Any:
        document["rows"] = [{"model": MODEL, "table": table, "reason": f"reviewer-accepted bookkeeping write: {table}"}
                            for table in tables]
        return document
    return edit


def _open(*keys: str) -> Edit:
    def edit(document: Any) -> Any:
        for key in keys:
            document["closed"][key] = False
        return document
    return edit


def _both_qualified(facts, readings, *, absent=(), extra_diagnostics=None, override=None):
    extra_diagnostics = extra_diagnostics or {}
    claims, outputs = [], []
    for op in OPS:
        claim = qualified_claim(op, readings[op], witness_diagnostics(op) + extra_diagnostics.get(op, []))
        claims.append(claim)
        outputs.extend(witness_templates(claim["id"], facts, absent=absent,
                                         override=(override or {}).get(op)))
    return claims, outputs


def build_00() -> dict[str, Any]:
    with variant({}) as root:
        exported, _ = exported_facts(root)
    facts = exported + reviewer_facts() + census_facts()
    claims, outputs = _both_qualified(facts, {
        CREATE: "issues.create is declared by the census, replayed (req-1, req-3), exercised with PHP/Go post-states "
                "the model admits, has no disagreement, no undeclared write and no surviving mutant (m-1 killed by req-1); "
                "req-1 writes issue then entity_statistics on both sides in the model's declared order.",
        CLOSE: "issues.close is declared, replayed by req-2, agreed by PHP/Go/model, writes only the declared tables "
               "in the declared order and its mutant m-2 is killed by req-2.",
        DELETE: "delete-issue is declared, replayed by req-4 (200, issue then entity_statistics) and repeated by req-5 "
                "(404, no effects); m-3 is killed by req-4, the repeat is not-found on both sides and the PHP oracle "
                "is stable across the bound selftest."})
    stability = find_id(facts, "replay_stability")
    claims.append(_claim("claim-repeat-delete-not-found", "repeat_delete_not_found", ["run", "target"],
                         [RUN, DELETE_TARGET], {"run": RUN},
                         "req-5 repeats req-4's target after req-4 committed (200 on both sides with an issue update); "
                         "both sides answered 404 and, under both closed effect tables, recorded no effect for req-5.",
                         [observation("php_response", ["run"], {"column": "req", "operator": "=", "value": REPEAT_DELETE}),
                          observation("go_response", ["run"], {"column": "req", "operator": "=", "value": REPEAT_DELETE})]))
    claims.append(_claim("claim-oracle-stable", "oracle_stable", ["run"], [RUN], {"run": RUN},
                         "the bound selftest (two fresh PHP runs of the tape) agreed on status and net SQL effects for "
                         "every request; the stability table is closed and carries no unstable row.",
                         [observation("replay_stability", ["run"])]))
    notes = [
        "The receipt is the fixture unchanged: one run, three ops, five requests (req-4 deletes issue 1, req-5 repeats "
        "the delete), PHP/Go/model agreeing row for row and statement for statement, three mutants all killed, "
        "the PHP selftest stable, every closure witness present, and the model typechecked by an admitted checker.",
        "The reviewer observed the run's nonce, snapshot and model, and the PHP census declares all three ops for the "
        "synthetic index; index_describes_replay binds that index to the run, which is the only static/runtime join "
        "in the pack.",
        "op_qualified support for issues.create uses req-1 (the canonical proof is the shortest, then lexically least, "
        "so req-1 is chosen over req-3 for replayed/op_exercised and for effect_order_exercised); leaves span the "
        "replay, php, go, shen, mut, reviewer, php-census and modelcheck producer classes and include the php/go/model "
        "effect sequences and the stability row.",
        "This is also the positive control of checker authority: structural validity is a model-level support leaf; "
        "each operation has its own checked certificate, and the reviewer admits that exact model/operation/checker/"
        "version/binary/certificate tuple.  Case 32 keeps only delete-issue checked to prove the other operations "
        "remain pending; cases 27-29 withhold separate authority premises and 30 (rejected) lets the model host "
        "sign its own structural certificate.",
        "The per-target claim repeat_delete_not_found(run, " + DELETE_TARGET + ") is supported from the tape order, "
        "both 200/404 response pairs, req-4's issue update on both sides and both effect closures; oracle_stable "
        "is supported from the stability row and its closure.",
        "Every missing_premise template is excluded by the witness row it names, so none renders.",
    ]
    return _case("00-positive-control", "Positive control: all three ops qualify from a clean receipt", None, notes,
                 facts, claims, outputs)


def build_01() -> dict[str, Any]:
    with variant({"php_post_state": _edit_row(1, state_digest=DISAGREEING_STATE)}) as root:
        exported, _ = exported_facts(root)
    facts = exported + reviewer_facts() + census_facts()
    php_req2 = find_id(facts, "php_post_state", req="req-2")
    claims, outputs = _both_qualified(facts, {
        CREATE: "issues.create is untouched by the planted disagreement and qualifies as in the control.",
        CLOSE: "req-2's PHP post-state is not in the model's closed admissible set, so php_model_disagree derives, "
               "php_disagree_any(issues.close) holds and the negation in op_qualified_rt fails.",
        DELETE: "delete-issue is untouched by the planted disagreement and qualifies as in the control."},
        extra_diagnostics={CLOSE: [observation("php_post_state", ["run"],
                                               {"column": "req", "operator": "=", "value": "req-2"})]})
    outputs.append(missing("claim-qualified-close", "php_model_agree",
                           "the PHP post-state of req-2 lies outside the model's admissible set for issues.close",
                           requires=[php_req2]))
    companion = _claim("claim-php-disagrees-req-2", "php_model_disagree", ["run", "req", "state_digest"],
                       [RUN, "req-2", DISAGREEING_STATE], {"run": RUN},
                       "model_admissible is closed for the run and model and has no row with req-2's PHP digest.")
    claims.append(companion)
    outputs.append(discrepancy(companion["id"], "php-state-outside-model", [php_req2], req="req-2"))
    notes = [
        "php_post_state.json row 1 (req-2, issues.close) carries a digest the model's admissible set does not contain; "
        "everything else is the control.",
        "op_qualified(issues.close) is unresolved: the missing premise names php_model_agree for req-2, triggered by the "
        "disagreeing PHP row, while every witness template is excluded by its present witness.",
        "The companion php_model_disagree claim is supported from the PHP row, the model_describes_run witness and the "
        "model_admissible_closed witness; its discrepancy output renders php-state-outside-model.",
    ]
    return _case("01-planted-disagreement", "Planted PHP/model disagreement on issues.close", "php-state-outside-model",
                 notes, facts, claims, outputs)


def build_02() -> dict[str, Any]:
    audit = {"req": "req-2", "run": RUN, "table": "audit_log", "kind": "insert",
             "pk": hashlib.sha256(b"audit_log pk 1").hexdigest(),
             "cols_digest": hashlib.sha256(b"audit_log cols 1").hexdigest()}
    with variant({"go_effect": _append_row(**audit)}) as root:
        exported, _ = exported_facts(root)
    facts = exported + reviewer_facts() + census_facts()
    go_audit = find_id(facts, "go_effect", table="audit_log")
    claims, outputs = _both_qualified(facts, {
        CREATE: "issues.create is untouched by the undeclared write and qualifies as in the control.",
        CLOSE: "Go wrote audit_log while serving req-2; model_writes is closed for (model, issues.close) and does not "
               "declare audit_log, so undeclared_write derives and undeclared_any(issues.close) blocks qualification.",
        DELETE: "delete-issue is untouched by the undeclared write and qualifies as in the control."},
        extra_diagnostics={CLOSE: [observation("go_effect", ["run"],
                                               {"column": "table", "operator": "=", "value": "audit_log"})]})
    outputs.append(missing("claim-qualified-close", "model_writes",
                           "Go wrote table audit_log for issues.close, which the model's closed write set does not declare",
                           requires=[go_audit]))
    companion = _claim("claim-undeclared-audit-log", "undeclared_write", ["run", "op", "table"],
                       [RUN, CLOSE, "audit_log"], {"run": RUN},
                       "The go_effect row for req-2/audit_log joins the replay_request for issues.close and the closed "
                       "model_writes set that lacks audit_log.")
    claims.append(companion)
    outputs.append(discrepancy(companion["id"], "undeclared-table", [go_audit], table="audit_log"))
    notes = [
        "go_effect.json gains an insert into audit_log for req-2 (issues.close); model_writes still declares only issues "
        "for that op and model_writes_closed covers it.",
        "op_qualified(issues.close) is unresolved with model_writes as the missing premise, triggered by the audit_log "
        "row; op_qualified(issues.create) is supported.",
        "The companion undeclared_write claim is supported through the go rule; PHP wrote no audit_log row so the php "
        "rule contributes nothing.",
    ]
    return _case("02-planted-undeclared-write", "Go writes an undeclared table for issues.close", "undeclared-table",
                 notes, facts, claims, outputs)


def build_03() -> dict[str, Any]:
    with variant({"mutant_killed": _drop_row(1)}) as root:
        exported, _ = exported_facts(root)
    facts = exported + reviewer_facts() + census_facts()
    m2 = find_id(facts, "mutant", mutant="m-2")
    claims, outputs = _both_qualified(facts, {
        CREATE: "issues.create keeps its killed mutant m-1 and qualifies as in the control.",
        CLOSE: "mutant m-2 of issues.close has no mutant_killed row while mutant_kills_closed is asserted, so "
               "surviving_mutant derives, op_has_surviving_mutant holds and corpus_constrains fails.",
        DELETE: "delete-issue keeps its killed mutant m-3 and qualifies as in the control."},
        extra_diagnostics={CLOSE: [observation("mutant", [],
                                               {"column": "mutant", "operator": "=", "value": "m-2"})]})
    outputs.append(missing("claim-qualified-close", "mutant_killed",
                           "mutant m-2 of issues.close was never killed by a replayed request", requires=[m2]))
    companion = _claim("claim-m2-survives", "surviving_mutant", ["run", "op", "mutant"], [RUN, CLOSE, "m-2"],
                       {"run": RUN}, "mutants are closed for (model, issues.close), kills are closed for the run, and "
                                     "no mutant_killed_in(run, m-2) exists.")
    claims.append(companion)
    outputs.append(discrepancy(companion["id"], "mutant-not-killed", [m2], mutant="m-2"))
    notes = [
        "mutant_killed.json loses its m-2 row; mutant.json still declares m-2 for issues.close and the receipt still "
        "asserts mutant_kills closed.",
        "op_qualified(issues.close) is unresolved, missing premise mutant_killed (for m-2); issues.create is supported.",
        "The companion surviving_mutant claim is supported from the mutant row, model_describes_run, mutants_closed and "
        "mutant_kills_closed; the negated mutant_killed_in contributes no leaf.",
    ]
    return _case("03-surviving-mutant", "A mutant of issues.close survives the replay", "surviving-mutant",
                 notes, facts, claims, outputs)


def build_04() -> dict[str, Any]:
    with variant({}) as root:
        exported, _ = exported_facts(root, drop_relations=("model_describes_run",))
    facts = exported + reviewer_facts() + census_facts()
    claims, outputs = _both_qualified(facts, {
        CREATE: "Without model_describes_run no model-scoped relation joins the run: no agreement, no closure, no "
                "corpus, so op_qualified_rt cannot derive.",
        CLOSE: "As for issues.create: the model witness is the missing premise.",
        DELETE: "As for issues.create."},
        absent=("model_describes_run",))
    companion = _claim("claim-php-agrees-req-1", "php_model_agree", ["run", "req"], [RUN, "req-1"], {"run": RUN},
                       "Even plain agreement needs the compatibility witness; without it no php_model_* row exists.")
    claims.append(companion)
    outputs.append(missing(companion["id"], "model_describes_run", WITNESS_REASONS["model_describes_run"]))
    notes = [
        "The exported model_describes_run row is withheld; every other row of the control is present.",
        "Both op_qualified claims and the php_model_agree companion are unresolved with model_describes_run as the only "
        "missing premise; the three reviewer witness templates are excluded by their present rows.",
        "No php_model_agree, php_model_disagree, go_model_agree or go_model_disagree row derives (evaluation test).",
    ]
    return _case("04-missing-model-witness", "No model_describes_run witness", None, notes, facts, claims, outputs)


def build_05() -> dict[str, Any]:
    with variant({}) as root:
        exported, _ = exported_facts(root)
    facts = exported + reviewer_facts(snapshot=None) + census_facts()
    reason = "the run's snapshot digest was not observed, so replay_run_current does not hold"
    override = {op: {"snapshot_observed": missing(claim_id_for(op), "replay_run_current", reason)}
                for op in OPS}
    claims, outputs = _both_qualified(facts, {
        CREATE: "replay_run_current needs run_nonce_observed, snapshot_observed and model_observed; the snapshot witness "
                "is absent, so every *_closed relation of the run is absent and op_qualified_rt cannot derive.",
        CLOSE: "As for issues.create.", DELETE: "As for issues.create."}, override=override)
    notes = [
        "The receipt is the control; the reviewer's snapshot_observed row is not added.",
        "Both op_qualified claims are unresolved with replay_run_current as the missing premise (reason: snapshot not "
        "observed); the model_describes_run, run_nonce_observed and model_observed templates are excluded by their rows.",
    ]
    return _case("05-missing-snapshot-witness", "No snapshot_observed witness", None, notes, facts, claims, outputs)


def build_06() -> dict[str, Any]:
    with variant({}) as root:
        exported, _ = exported_facts(root)
    facts = exported + reviewer_facts(nonce=OTHER_NONCE) + census_facts()
    nonce_id = find_id(facts, "run_nonce_observed")
    reason = "the observed nonce differs from the run's nonce, so the replay is stale rather than current"
    override = {op: {"run_nonce_observed": missing(claim_id_for(op), "replay_run_current", reason,
                                                    requires=[nonce_id])} for op in OPS}
    claims, outputs = _both_qualified(facts, {
        CREATE: "run_nonce_observed names another nonce: replay_run_stale derives (operational status stale) and "
                "replay_run_current does not, so no closure and no qualification.",
        CLOSE: "As for issues.create.", DELETE: "As for issues.create."},
        extra_diagnostics={op: [observation("replay_run_stale", ["run"], status="stale")] for op in OPS},
        override=override)
    companion = _claim("claim-run-stale", "replay_run_stale", ["run", "nonce", "observed"],
                       [RUN, NONCE, OTHER_NONCE], {"run": RUN},
                       "replay_run and run_nonce_observed bind different digests and the comparison holds.")
    claims.append(companion)
    notes = [
        "The reviewer's run_nonce_observed row carries a nonce other than the receipt's.",
        "Both op_qualified claims are unresolved with operational status stale (diagnostic on replay_run_stale) and "
        "replay_run_current as the missing premise, triggered by the mismatching nonce row.",
        "The companion replay_run_stale claim is supported from the replay_run header and the nonce row.",
    ]
    return _case("06-stale-replay", "Observed nonce differs from the run's nonce", "stale-replay", notes, facts,
                 claims, outputs)


def build_08() -> dict[str, Any]:
    with variant({"mutant_killed": _edit_row(0, req="req-9")}) as root:
        exported, _ = exported_facts(root)
    facts = exported + reviewer_facts() + census_facts()
    kill = find_id(facts, "mutant_killed", mutant="m-1")
    closed = find_id(facts, "mutant_kills_closed")
    reason = "mutant m-1 is recorded as killed by req-9, which is not a replayed request: the run's kill closure is contradicted"
    claims, outputs = _both_qualified(facts, {
        CREATE: "m-1 is recorded as killed by req-9, a request the closed replay_request set does not contain; "
                "kill_closure_gap derives, kill_closure_gap_any poisons every op of the run (the per-run "
                "mutant_kills_closed witness is demonstrably wrong) and the gate in op_qualified_rt fails.",
        CLOSE: "issues.close's own mutant m-2 is honestly killed by req-2, but its qualification rests on the same "
               "contradicted per-run kill closure, so it is unresolved too.",
        DELETE: "As for issues.close: m-3 is honestly killed by req-4, the per-run kill closure is still contradicted."},
        extra_diagnostics={op: [observation("mutant_killed", ["run"],
                                            {"column": "mutant", "operator": "=", "value": "m-1"})] for op in OPS})
    for op in OPS:
        outputs.append(missing(claim_id_for(op), "replay_request", reason, requires=[kill]))
    companion = _claim("claim-kill-outside-corpus", "kill_closure_gap", ["run", "mutant", "req"], [RUN, "m-1", "req-9"],
                       {"run": RUN}, "mutant_kills_closed and replay_requests_closed are both asserted, yet the kill "
                                     "names req-9 which requested/replay_request does not contain.")
    claims.append(companion)
    outputs.append(discrepancy(companion["id"], "kill-outside-replayed-requests", [kill, closed],
                               mutant="m-1", req="req-9"))
    notes = [
        "mutant_killed.json row 0 names req-9; replay_request.json is unchanged and the receipt still asserts "
        "replay_requests and mutant_kills closed.",
        "The kill_closure_gap claim is supported and its support includes the mutant_kills_closed witness that the "
        "harness should have withheld; that witness is listed under forbidden_leaves (seeded_fault "
        "lying-completeness-witness).",
        "Both op_qualified claims are unresolved: kill_gap_closed holds (kills and requests both closed) and "
        "kill_closure_gap_any(run, op) holds for every replayed op, so the contradiction gate fails; the missing "
        "premise names replay_request (the req-9 the kill relies on), triggered by the m-1 kill row.",
    ]
    return _case("08-lying-closure", "mutant_kills_closed asserted over a kill outside the replayed requests",
                 "lying-completeness-witness", notes, facts, claims, outputs)


def build_09() -> dict[str, Any]:
    with variant({"php_post_state": _drop_row(2)}) as root:
        exported, _ = exported_facts(root)
    facts = exported + reviewer_facts() + census_facts()
    req3 = find_id(facts, "replay_request", req="req-3")
    claims, outputs = _both_qualified(facts, {
        CREATE: "req-1 still agrees on both sides, so op_exercised(issues.create) holds; req-3's PHP post-state is "
                "omitted while php_post_states_closed is asserted, so post_state_gap(req-3, php) derives, "
                "post_state_any(issues.create) holds and the gate in op_qualified_rt fails.",
        CLOSE: "issues.close (req-2) is fully observed and qualifies as in the control.",
        DELETE: "delete-issue (req-4, req-5) is fully observed and qualifies as in the control."},
        extra_diagnostics={CREATE: [observation("replay_request", ["run"],
                                                {"column": "req", "operator": "=", "value": "req-3"})]})
    outputs.append(missing("claim-qualified-create", "php_post_state",
                           "req-3 of issues.create was replayed but the PHP runner reported no post-state for it",
                           requires=[req3]))
    companion = _claim("claim-req-3-php-post-state-gap", "post_state_gap", ["run", "req", "side"],
                       [RUN, "req-3", "php"], {"run": RUN},
                       "replay_request is closed and contains req-3; php_post_states_closed is asserted and no "
                       "php_post_state(run, req-3, _) exists.",
                       # the gap rule reads the request through the requested projection; the
                       # diagnostic makes the request row itself relevant to the discrepancy
                       [observation("replay_request", ["run"], {"column": "req", "operator": "=", "value": "req-3"})])
    claims.append(companion)
    outputs.append(discrepancy(companion["id"], "post-state-missing", [req3], req="req-3", side="php"))
    notes = [
        "php_post_state.json loses its req-3 row; receipt.json still asserts php_post_states closed and every other "
        "row of the control is present.",
        "Without the post-state gate, issues.create would qualify: req-1 satisfies op_exercised and the omitted req-3 "
        "is invisible to php_disagree_any.  With it, op_qualified(issues.create) is unresolved with php_post_state as "
        "the missing premise (for req-3); issues.close is supported.",
        "The companion post_state_gap claim is supported from the req-3 request row, replay_requests_closed and "
        "php_post_states_closed.",
    ]
    return _case("09-missing-post-state", "A replayed request of issues.create has no PHP post-state",
                 "post-state-omitted", notes, facts, claims, outputs)


def build_10() -> dict[str, Any]:
    with variant({"receipt": _open("php_effects")}) as root:
        exported, _ = exported_facts(root)
    facts = exported + reviewer_facts() + census_facts()
    claims, outputs = _both_qualified(facts, {
        CREATE: "closed.php_effects is false, so no php_effects_closed witness is exported; undeclared_writes_closed "
                "lists it as an input and does not derive, so op_qualified_rt cannot derive although every "
                "observation agrees and every other closure holds.",
        CLOSE: "As for issues.create.", DELETE: "As for issues.create."}, absent=("php_effects_closed",))
    notes = [
        "receipt.json says closed.php_effects = false; php_effect.json is the control's (three rows) and every "
        "other closure, witness and observation is present.  go_effects stays closed so exactly one leaf is absent.",
        "Both op_qualified claims are unresolved with php_effects_closed as the only missing premise: an open effect "
        "table cannot license !undeclared_any.",
    ]
    return _case("10-missing-effects-closure", "The PHP effect table is not closed", None, notes, facts, claims,
                 outputs)


def build_11() -> dict[str, Any]:
    with variant({"receipt": _open("model_admissible")}) as root:
        exported, _ = exported_facts(root)
    facts = exported + reviewer_facts() + census_facts()
    claims, outputs = _both_qualified(facts, {
        CREATE: "model_describes_run is present and every post-state is admitted, but closed.model_admissible is "
                "false: without model_admissible_closed neither php_model_disagree nor php_disagreement_closed "
                "can derive, so agreement is observed yet cannot be closed and op_qualified_rt cannot derive.",
        CLOSE: "As for issues.create.", DELETE: "As for issues.create."}, absent=("model_admissible_closed",))
    companion = _claim("claim-php-agrees-req-1", "php_model_agree", ["run", "req"], [RUN, "req-1"], {"run": RUN},
                       "Agreement needs only the model witness and an admissible row, both present.")
    claims.append(companion)
    notes = [
        "receipt.json says closed.model_admissible = false; model_admissible.json and model_describes_run are the "
        "control's, distinguishing this case from 04 where the compatibility witness itself is absent.",
        "Both op_qualified claims are unresolved with model_admissible_closed as the only missing premise while the "
        "php_model_agree companion is supported: observed agreement is not closed agreement.",
    ]
    return _case("11-missing-admissible-closure", "The model's admissible-state set is not closed", None, notes,
                 facts, claims, outputs)


AUDIT = {"req": "req-2", "run": RUN, "table": "audit_log", "kind": "insert",
         "pk": hashlib.sha256(b"audit_log pk 1").hexdigest(),
         "cols_digest": hashlib.sha256(b"audit_log cols 1").hexdigest()}


def build_13() -> dict[str, Any]:
    with variant({"go_effect": _append_row(**AUDIT), "model_scope_exclusions": _exclude("authentication", "redis", "audit_log")}) as root:
        exported, _ = exported_facts(root)
    facts = exported + reviewer_facts() + census_facts()
    go_audit = find_id(facts, "go_effect", table="audit_log")
    exclusion = find_id(facts, "model_scope_exclusion", table="audit_log")
    claims, outputs = _both_qualified(facts, {
        CREATE: "issues.create is untouched and qualifies as in the control.",
        CLOSE: "Go wrote audit_log for req-2 exactly as in case 02, but the reviewer's closed exclusion set names "
               "audit_log, so !model_scope_excluded fails, undeclared_write does not derive and issues.close "
               "qualifies under that reviewer assumption.",
        DELETE: "delete-issue is untouched and qualifies as in the control."})
    companion = _claim("claim-audit-log-excluded", "exclusion_applied", ["run", "op", "table"], [RUN, CLOSE, "audit_log"],
                       {"run": RUN}, "The go_effect audit_log row for req-2 joins the reviewer's exclusion of audit_log "
                                     "for this model; the assumption row is a leaf of this certificate.",
                       # the rule reads the exclusion through the model_scope_excluded projection; the
                       # diagnostic makes the assumption row itself relevant to the discrepancy
                       [observation("model_scope_exclusion", [], {"column": "table", "operator": "=", "value": "audit_log"})])
    claims.append(companion)
    outputs.append(discrepancy(companion["id"], "write-excluded-by-reviewer", [go_audit, exclusion], table="audit_log"))
    notes = [
        "The receipt is case 02 (an undeclared Go write to audit_log) plus a reviewer exclusion file naming "
        "authentication, redis and audit_log, with closed.model_scope_exclusions true.",
        "Both op_qualified claims are supported: the same write that leaves issues.close unresolved in case 02 is "
        "covered by an explicit, reviewer-owned assumption here.  Contrast 02 (same table, not excluded).",
        "The companion exclusion_applied claim is supported and cites the exclusion assumption as a leaf; the "
        "op_qualified certificate cites the closure witness (a negated atom carries no leaf), which is why the "
        "assumption is surfaced through the companion.",
    ]
    return _case("13-excluded-undeclared-write", "An undeclared Go write covered by a reviewer exclusion", None,
                 notes, facts, claims, outputs)


def build_14() -> dict[str, Any]:
    with variant({"go_effect": _append_row(**AUDIT), "model_scope_exclusions": _exclude("authentication", "redis", "audit_log"),
                  "receipt": _open("model_scope_exclusions")}) as root:
        exported, _ = exported_facts(root)
    facts = exported + reviewer_facts() + census_facts()
    claims, outputs = _both_qualified(facts, {
        CREATE: "The exclusion file is present but closed.model_scope_exclusions is false: no "
                "model_scope_exclusions_closed witness, so undeclared_writes_closed cannot derive for any op.",
        CLOSE: "As for issues.create; the audit_log exclusion row exists but an unclosed set licenses nothing.",
        DELETE: "As for issues.create."},
        absent=("model_scope_exclusions_closed",))
    notes = [
        "Case 13's receipt with closed.model_scope_exclusions = false: the exclusion rows are exported (as assumptions) "
        "but the reviewer did not vouch that the set is complete.",
        "Both op_qualified claims are unresolved with model_scope_exclusions_closed as the only missing premise: an "
        "open exclusion set neither licenses !model_scope_excluded nor closes undeclared_any.",
    ]
    return _case("14-exclusions-not-closed", "Exclusion rows without a closed exclusion set", None, notes, facts,
                 claims, outputs)


def build_15() -> dict[str, Any]:
    with variant({"model_scope_exclusions": lambda _: None, "receipt": _open("model_scope_exclusions")}) as root:
        exported, _ = exported_facts(root)
    facts = exported + reviewer_facts() + census_facts()
    claims, outputs = _both_qualified(facts, {
        CREATE: "No exclusion file and no closure: even a receipt whose every write is declared (the control's) "
                "cannot be judged free of undeclared writes without a closed exclusion set.",
        CLOSE: "As for issues.create.", DELETE: "As for issues.create."},
        absent=("model_scope_exclusions_closed",))
    notes = [
        "The control receipt without model_scope_exclusions.json and with closed.model_scope_exclusions = false.",
        "Both op_qualified claims are unresolved, missing model_scope_exclusions_closed: an empty exclusion set with "
        "no closure qualifies nothing (the reviewer must say the set is empty, not merely say nothing).",
    ]
    return _case("15-no-exclusions-no-closure", "Neither exclusions nor a closed exclusion set", None, notes, facts,
                 claims, outputs)


def _swap_seq(req: str, table_a: str, table_b: str) -> Edit:
    """Swap the ``seq`` of two effect-sequence rows of one request (the side observed them the other way round)."""
    def edit(document: Any) -> Any:
        rows = {row["table"]: row for row in document["rows"] if row["req"] == req}
        rows[table_a]["seq"], rows[table_b]["seq"] = rows[table_b]["seq"], rows[table_a]["seq"]
        return document
    return edit


def build_17() -> dict[str, Any]:
    with variant({"go_effect_seq": _swap_seq("req-1", "issue", "entity_statistics")}) as root:
        exported, _ = exported_facts(root)
    facts = exported + reviewer_facts() + census_facts()
    go_issue = find_id(facts, "go_effect_seq", req="req-1", table="issue")
    go_stats = find_id(facts, "go_effect_seq", req="req-1", table="entity_statistics")
    req1 = observation("go_effect_seq", ["run"], {"column": "req", "operator": "=", "value": "req-1"})
    claims, outputs = _both_qualified(facts, {
        CREATE: "The model declares issue before entity_statistics for req-1 and PHP observed that order, but Go's "
                "per-statement sequence has entity_statistics (seq 1) before issue (seq 2): effect_order_violation "
                "derives for (go, req-1), effect_order_any(issues.create) holds and the negation in op_qualified_rt "
                "fails; req-3 has a single effect and cannot exercise the order either.",
        CLOSE: "issues.close (req-2) is observed in the declared order on both sides and qualifies as in the control.",
        DELETE: "delete-issue (req-4) is observed in the declared order on both sides and qualifies as in the control."},
        extra_diagnostics={CREATE: [req1]})
    outputs.append(missing("claim-qualified-create", "effect_order_respected",
                           "Go wrote entity_statistics before issue for req-1, the reverse of the model's declared order",
                           requires=[go_stats]))
    companion = _claim("claim-go-order-violated-req-1", "effect_order_violation",
                       ["run", "side", "req", "table_a", "table_b"], [RUN, "go", "req-1", "issue", "entity_statistics"],
                       {"run": RUN},
                       "model_effect_seq places issue (seq 1) before entity_statistics (seq 2) for req-1 and go_effect_seq "
                       "places entity_statistics (seq 1) before issue (seq 2): the same table/kind pair, the opposite order.",
                       [req1])
    claims.append(companion)
    outputs.append(discrepancy(companion["id"], "effect-order-violated", [go_issue, go_stats], req="req-1", side="go"))
    notes = [
        "go_effect_seq.json swaps the seq of req-1's issue and entity_statistics rows; go_effect.json (the folded net "
        "effects) is unchanged, so no other rule sees a difference: the order is the only fault.",
        "op_qualified(issues.create) is unresolved with effect_order_respected as the missing premise, triggered by the "
        "swapped entity_statistics row; issues.close and delete-issue are supported.",
        "The companion effect_order_violation claim is supported from the model_describes_run witness, the two model "
        "sequence rows and the two Go sequence rows; its discrepancy output renders effect-order-violated.  The join "
        "is on (table, kind) only: the model's entity_statistics pk is a domain id, the systems' pk a row id.",
    ]
    return _case("17-effect-order-violation", "Go writes the declared tables of issues.create in the wrong order",
                 "effect-order-violated", notes, facts, claims, outputs)


REPEAT_EFFECT = {"req": REPEAT_DELETE, "run": RUN, "table": "issue", "kind": "update",
                 "pk": "286bbc3165c77702a9a2c88c60a87d0cdb9f625f8d8b6863a90f25a9b6c07ceb",
                 "cols_digest": hashlib.sha256(b"rules-replay-v1 issue touched again by the repeat delete").hexdigest()}
REPEAT_EFFECT_SEQ = {"req": REPEAT_DELETE, "run": RUN, "seq": 1, "table": "issue", "kind": "update", "pk": REPEAT_EFFECT["pk"]}


def build_18() -> dict[str, Any]:
    with variant({"php_effect": _append_row(**REPEAT_EFFECT), "php_effect_seq": _append_row(**REPEAT_EFFECT_SEQ)}) as root:
        exported, _ = exported_facts(root)
    facts = exported + reviewer_facts() + census_facts()
    php_row = find_id(facts, "php_effect", req=REPEAT_DELETE)
    req5 = observation("php_effect", ["run"], {"column": "req", "operator": "=", "value": REPEAT_DELETE})
    blocked = missing(claim_id_for(DELETE), "repeat_delete",
                      "the second DELETE of " + DELETE_TARGET + " (req-5) wrote a row outside the reviewer's scope "
                      "exclusions: repeat_delete_violation(req-5, effects) holds, so repeat_delete_any(run, "
                      "delete-issue) holds and the negated premise !repeat_delete_any fails",
                      requires=[php_row])
    claims, outputs = _both_qualified(facts, {
        CREATE: "issues.create is untouched by the repeat's effect and qualifies as in the control: repeat_delete_any "
                "is joined on the request, so a violation by a delete-issue request does not reach another op.",
        CLOSE: "issues.close is untouched by the repeat's effect and qualifies as in the control.",
        DELETE: "req-5 repeats the committed delete of " + DELETE_TARGET + " and still answers 404 on both sides, but PHP "
                "recorded an issue update for it: repeat_delete_has_effect derives (issue is not a reviewer-excluded "
                "table) and repeat_delete_violation(req-5, effects) holds, so repeat_delete_any(run, delete-issue) "
                "holds and op_qualified_rt's !repeat_delete_any fails.  The write is a *declared* table and both "
                "post-states stay inside the model, so nothing else in the op gate objects: the cross-request "
                "premise is the only thing standing between this receipt and a qualification."},
        extra_diagnostics={DELETE: [req5]})
    outputs.append(blocked)
    violation = _claim("claim-repeat-delete-has-effects", "repeat_delete_violation", ["run", "req", "side"],
                       [RUN, REPEAT_DELETE, "effects"], {"run": RUN},
                       "req-5 is a later request for req-4's target, req-4 committed (200 on both sides with an issue "
                       "update on both), and php_effect carries an issue update for req-5.", [req5])
    claims.append(violation)
    outputs.append(discrepancy(violation["id"], "repeat-delete-with-effects", [php_row], req=REPEAT_DELETE, table="issue"))
    not_found = _claim("claim-repeat-delete-not-found", "repeat_delete_not_found", ["run", "target"], [RUN, DELETE_TARGET],
                       {"run": RUN}, "The responses are 404 on both sides and the effect tables are closed, but the "
                                     "negated repeat_delete_has_effect(run, req-5) fails on the PHP row.", [req5])
    claims.append(not_found)
    outputs.append(missing(not_found["id"], "repeat_delete_has_effect",
                           "PHP recorded an issue update for req-5, so the repeat is not effect-free: the negated "
                           "premise !repeat_delete_has_effect(run, req-5) fails", requires=[php_row]))
    notes = [
        "php_effect.json (and php_effect_seq.json, to stay coherent) gain an issue update for req-5, the repeat of "
        "req-4's DELETE; responses stay 200/404 and the write is a declared table, so undeclared_write does not fire.",
        "op_qualified(delete-issue) is unresolved: this is the case that makes the cross-request claim load-bearing.  "
        "Every per-request premise holds -- PHP, Go and the model agree, the write is declared, the order is "
        "respected, the mutants are killed, the oracle is stable -- and the op is refused only because the second "
        "delete of a committed target wrote rows.  issues.create and issues.close are supported: repeat_delete_any "
        "joins the violating request's own op, so one op's repeat does not poison the run.",
        "The companion repeat_delete_violation(run, req-5, effects) is supported (discrepancy "
        "repeat-delete-with-effects); the companion repeat_delete_not_found is unresolved, its missing premise naming "
        "the negated repeat_delete_has_effect that the PHP row defeats.  issue is not one of the reviewer's excluded "
        "tables (authentication, redis), so the exclusion guard on repeat_delete_has_effect does not exempt it -- "
        "case 23 is the shape where the write *is* excluded and both the claim and the op survive.",
    ]
    return _case("18-repeat-delete-with-effects", "The repeat delete answers 404 but PHP writes the issue again",
                 "repeat-delete-with-effects", notes, facts, claims, outputs)


# the repeat's bookkeeping write: the session-token touch every authenticated request
# makes, on a table the reviewer excluded from the write-set judgement
EXCLUDED_REPEAT_EFFECT = {"req": REPEAT_DELETE, "run": RUN, "table": "authentication", "kind": "update",
                          "pk": hashlib.sha256(b"rules-replay-v1 session row of the replay actor").hexdigest(),
                          "cols_digest": hashlib.sha256(b"rules-replay-v1 session token expiry touched").hexdigest()}
EXCLUDED_REPEAT_EFFECT_SEQ = {"req": REPEAT_DELETE, "run": RUN, "seq": 1, "table": "authentication", "kind": "update",
                              "pk": EXCLUDED_REPEAT_EFFECT["pk"]}


def build_23() -> dict[str, Any]:
    with variant({"php_effect": _append_row(**EXCLUDED_REPEAT_EFFECT),
                  "php_effect_seq": _append_row(**EXCLUDED_REPEAT_EFFECT_SEQ),
                  "go_effect": _append_row(**EXCLUDED_REPEAT_EFFECT),
                  "go_effect_seq": _append_row(**EXCLUDED_REPEAT_EFFECT_SEQ)}) as root:
        exported, _ = exported_facts(root)
    facts = exported + reviewer_facts() + census_facts()
    go_row = find_id(facts, "go_effect", req=REPEAT_DELETE)
    exclusion = find_id(facts, "model_scope_exclusion", table="authentication")
    req5 = observation("php_effect", ["run"], {"column": "req", "operator": "=", "value": REPEAT_DELETE})
    claims, outputs = _both_qualified(facts, {
        CREATE: "issues.create is untouched by the repeat's session touch and qualifies as in the control.",
        CLOSE: "issues.close is untouched by the repeat's session touch and qualifies as in the control.",
        DELETE: "delete-issue qualifies as in the control: authentication is a reviewer-excluded table, so neither "
                "undeclared_write nor repeat_delete_has_effect derives from the touch."},
        extra_diagnostics={DELETE: [req5]})
    not_found = _claim("claim-repeat-delete-not-found", "repeat_delete_not_found", ["run", "target"],
                       [RUN, DELETE_TARGET], {"run": RUN},
                       "req-5 repeats req-4's committed delete, answers 404 on both sides and, under both closed "
                       "effect tables and the reviewer's closed exclusion set, wrote nothing outside those "
                       "exclusions: its only rows are the authentication touch both systems make on every "
                       "authenticated request.", [req5])
    claims.append(not_found)
    companion = _claim("claim-session-touch-excluded", "exclusion_applied", ["run", "op", "table"],
                       [RUN, DELETE, "authentication"], {"run": RUN},
                       "the repeat's authentication row joins the reviewer's exclusion of authentication for this "
                       "model; the assumption row is a leaf of this certificate.",
                       [observation("model_scope_exclusion", [], {"column": "table", "operator": "=",
                                                                  "value": "authentication"})])
    claims.append(companion)
    outputs.append(discrepancy(companion["id"], "write-excluded-by-reviewer", [go_row, exclusion],
                               table="authentication"))
    notes = [
        "php_effect / go_effect (and both sequences, to stay coherent) gain an authentication update for req-5, the "
        "repeat of req-4's DELETE: the session-token touch every authenticated request makes, whatever it answers.  "
        "The reviewer's exclusion file already names authentication.",
        "All three op_qualified claims are supported and so is repeat_delete_not_found: repeat_delete_has_effect "
        "joins !model_scope_excluded on the written table, so an excluded bookkeeping row is not a finding.  Without "
        "that guard this receipt -- the shape a correct port produces, since a 404 repeat still authenticates -- "
        "would defeat the per-target claim (contrast case 18, where the repeat writes issue).",
        "The companion exclusion_applied claim is supported and cites the exclusion assumption as a leaf, as in "
        "case 13; the negated atom in repeat_delete_has_effect carries none.",
    ]
    return _case("23-repeat-delete-excluded-write", "The repeat delete touches only a reviewer-excluded table", None,
                 notes, facts, claims, outputs)


def _swap_tape_seq(req_a: str, req_b: str) -> Edit:
    """Swap the tape position of two requests (the repeat was sent before the commit)."""
    def edit(document: Any) -> Any:
        rows = {row["req"]: row for row in document["rows"]}
        rows[req_a]["seq"], rows[req_b]["seq"] = rows[req_b]["seq"], rows[req_a]["seq"]
        return document
    return edit


def build_24() -> dict[str, Any]:
    with variant({"replay_request_seq": _swap_tape_seq(FIRST_DELETE, REPEAT_DELETE)}) as root:
        exported, _ = exported_facts(root)
    facts = exported + reviewer_facts() + census_facts()
    tape = find_id(facts, "replay_request_seq", req=FIRST_DELETE)
    seen = observation("replay_request_seq", ["run"], {"column": "req", "operator": "=", "value": FIRST_DELETE})
    claims, outputs = _both_qualified(facts, {
        CREATE: "issues.create is untouched by the tape order of the two deletes and qualifies as in the control.",
        CLOSE: "issues.close is untouched by the tape order of the two deletes and qualifies as in the control.",
        DELETE: "delete-issue still qualifies: op_qualified_rt reads the requests, not their order relative to one "
                "another, and every per-request premise is the control's."})
    not_found = _claim("claim-repeat-delete-not-found", "repeat_delete_not_found", ["run", "target"],
                       [RUN, DELETE_TARGET], {"run": RUN},
                       "the 404 (req-5) now sits at tape position 4 and the committing 200 (req-4) at position 5: "
                       "the first delete of the target did not commit, and the one that did is not the first, so "
                       "first_delete_committed derives for neither and the claim has nothing to say.", [seen])
    claims.append(not_found)
    outputs.append(missing(not_found["id"], "first_delete_committed",
                           "the first delete of " + DELETE_TARGET + " (req-5, tape position 4) answered 404 and wrote "
                           "nothing; req-4 committed but an earlier delete of the same target precedes it",
                           requires=[tape]))
    notes = [
        "replay_request_seq.json swaps the tape positions of req-4 and req-5; every other observation, including both "
        "response rows and every effect row, is the control's.",
        "repeat_delete_not_found is unresolved: first_delete_committed names the *first* delete of a (tenant, target) "
        "-- the negated earlier_delete under earlier_delete_closed -- so a later committing delete cannot stand in "
        "for it.  Without that condition req-4 would be read as the commit and req-4 itself as the repeat, and the "
        "pack would report status and effect violations against a tape it had misread.",
        "All three op_qualified claims are supported: the repeat is its own claim and no premise of op_qualified_rt "
        "reads the tape order.",
    ]
    return _case("24-repeat-before-the-commit", "The repeat of the delete sits before the commit in the tape", None,
                 notes, facts, claims, outputs)


def _drop_rows(**columns: Any) -> Edit:
    """Drop every row matching ``columns``."""
    def edit(document: Any) -> Any:
        document["rows"] = [row for row in document["rows"]
                            if any(row.get(k) != v for k, v in columns.items())]
        return document
    return edit


def build_25() -> dict[str, Any]:
    with variant({"go_effect": _drop_rows(req=FIRST_DELETE, table="issue"),
                  "go_effect_seq": _drop_rows(req=FIRST_DELETE, table="issue")}) as root:
        exported, _ = exported_facts(root)
    facts = exported + reviewer_facts() + census_facts()
    php_issue = find_id(facts, "php_effect", req=FIRST_DELETE, table="issue")
    seen = observation("php_effect", ["run"], {"column": "req", "operator": "=", "value": FIRST_DELETE})
    claims, outputs = _both_qualified(facts, {
        CREATE: "issues.create is untouched and qualifies as in the control.",
        CLOSE: "issues.close is untouched and qualifies as in the control.",
        DELETE: "req-4 answered 200 on both sides, but Go recorded no issue update for it: the only request of "
                "delete-issue that could demonstrate the model's declared order no longer does so on the Go side, so "
                "effect_order_exercised(delete-issue) fails and the op is unresolved."},
        extra_diagnostics={DELETE: [seen]})
    not_found = _claim("claim-repeat-delete-not-found", "repeat_delete_not_found", ["run", "target"],
                       [RUN, DELETE_TARGET], {"run": RUN},
                       "req-5 repeats req-4 and answers 404 on both sides with no effect, but req-4 is not a "
                       "committed delete: a 200 whose side wrote no issue update did not soft-delete the issue.",
                       [seen])
    claims.append(not_found)
    outputs.append(missing("claim-qualified-delete", "effect_order_respected",
                           "Go recorded no issue update for req-4, the only request of delete-issue whose effects the "
                           "model puts in a declared order, so no side pair demonstrates that order",
                           requires=[php_issue]))
    outputs.append(missing(not_found["id"], "first_delete_committed",
                           "req-4 answered 200 on both sides but Go recorded no issue update for it, so the first "
                           "delete of " + DELETE_TARGET + " is not a committed one", requires=[php_issue]))
    notes = [
        "go_effect.json and go_effect_seq.json lose req-4's issue update; PHP's is untouched, both responses stay "
        "200/404 and no table is added, so neither undeclared_write nor a status violation fires.",
        "repeat_delete_not_found is unresolved: first_delete_committed reads a 200 *and* an issue update on both "
        "sides, which is what 'committed' means -- without the effect premises a 200 that changed nothing would "
        "license the claim.",
        "op_qualified(delete-issue) is unresolved too, but for the order gate: req-4 is the op's only ordered "
        "request and Go no longer demonstrates the declared order for it.",
    ]
    return _case("25-first-delete-not-committed", "The first delete answers 200 but Go writes no issue update", None,
                 notes, facts, claims, outputs)


def build_19() -> dict[str, Any]:
    with variant({"replay_stability": _edit_row(0, stable="false")}) as root:
        exported, _ = exported_facts(root)
    facts = exported + reviewer_facts() + census_facts()
    stability = find_id(facts, "replay_stability")
    reason = "the bound PHP selftest is unstable (a request differed in status or net SQL effects between two fresh runs)"
    seen = observation("replay_stability", ["run"])
    claims, outputs = _both_qualified(facts, {
        CREATE: "The bound selftest reports stable = false: oracle_unstable derives, so oracle_stable (which negates it "
                "under replay_stability_closed) does not, and op_qualified_rt cannot derive for any op of the run.",
        CLOSE: "As for issues.create.", DELETE: "As for issues.create."},
        extra_diagnostics={op: [seen] for op in OPS})
    for op in OPS:
        outputs.append(missing(claim_id_for(op), "oracle_stable", reason, requires=[stability]))
    companion = _claim("claim-oracle-unstable", "oracle_unstable", ["run"], [RUN], {"run": RUN},
                       "replay_stability carries a row with stable = false for the run.", [seen])
    claims.append(companion)
    outputs.append(discrepancy(companion["id"], "oracle-unstable", [stability], side="php"))
    notes = [
        "replay_stability.json row 0 says stable = false (the two selftest runs disagreed); closed.replay_stability "
        "stays true, every observation of the run itself is the control's.",
        "All three op_qualified claims are unresolved with oracle_stable as the missing premise, triggered by the "
        "unstable row: an oracle that does not reproduce itself qualifies nothing, however well PHP, Go and the model "
        "agree in this one run.",
        "The companion oracle_unstable claim is supported from the stability row alone (discrepancy oracle-unstable).",
    ]
    return _case("19-unstable-oracle", "The bound selftest says the PHP oracle is unstable", "oracle-unstable",
                 notes, facts, claims, outputs)


# the selftest's two runs, as the fixture's stability row names them
SELFTEST_A, SELFTEST_B = RECEIPT["receipts"]["selftest"]["run_ids"]


def build_26() -> dict[str, Any]:
    with variant({"replay_stability": _append_row(run=RUN, run_a=SELFTEST_A, run_b=SELFTEST_B,
                                                  side="go", stable="false")}) as root:
        exported, _ = exported_facts(root)
    facts = exported + reviewer_facts() + census_facts()
    unstable = find_id(facts, "replay_stability", side="go")
    reason = ("the bound selftest is unstable on the Go side (a request differed in status or net SQL effects "
              "between the two runs)")
    seen = observation("replay_stability", ["run"], {"column": "side", "operator": "=", "value": "go"})
    claims, outputs = _both_qualified(facts, {
        CREATE: "The PHP leg of the selftest is stable, but its Go leg is not: oracle_unstable derives from the "
                "\"false\" row, and oracle_stable negates it even though its own positive premise (a \"true\" PHP "
                "row) is satisfied.",
        CLOSE: "As for issues.create.", DELETE: "As for issues.create."},
        extra_diagnostics={op: [seen] for op in OPS})
    for op in OPS:
        outputs.append(missing(claim_id_for(op), "oracle_stable", reason, requires=[unstable]))
    companion = _claim("claim-oracle-unstable", "oracle_unstable", ["run"], [RUN], {"run": RUN},
                       "replay_stability carries a row with stable = false for the run (the Go side).", [seen])
    claims.append(companion)
    outputs.append(discrepancy(companion["id"], "oracle-unstable", [unstable], side="go"))
    notes = [
        "replay_stability.json keeps the control's stable = true PHP row and gains a stable = false row for the Go "
        "side of the same pair of selftest runs; nothing else is touched.",
        "All three op_qualified claims are unresolved with oracle_stable as the missing premise.  This is the case "
        "that makes the negation in oracle_stable load-bearing: in case 19 the single PHP row is flipped, so "
        "oracle_stable already fails on its positive premise, and a pack that dropped !oracle_unstable would still "
        "pass.  Here the positive premise holds and only the negation refuses.",
        "The companion oracle_unstable claim is supported from the Go row alone (discrepancy oracle-unstable).",
    ]
    return _case("26-unstable-on-one-side", "The selftest reproduces the oracle on one side only", "oracle-unstable",
                 notes, facts, claims, outputs)


def build_20() -> dict[str, Any]:
    with variant({"receipt": _open("replay_stability")}) as root:
        exported, _ = exported_facts(root)
    facts = exported + reviewer_facts() + census_facts()
    claims, outputs = _both_qualified(facts, {
        CREATE: "closed.replay_stability is false (no selftest bound to the run, or its provenance differs): no "
                "replay_stability_closed witness, so oracle_stable cannot derive and op_qualified_rt cannot derive.",
        CLOSE: "As for issues.create.", DELETE: "As for issues.create."},
        absent=("replay_stability_closed",))
    notes = [
        "receipt.json says closed.replay_stability = false; the stability row is still exported (stable = true) but an "
        "unclosed table licenses neither the negation of oracle_unstable nor oracle_stable itself.",
        "All three op_qualified claims are unresolved with replay_stability_closed as the only missing premise.",
    ]
    return _case("20-missing-stability-closure", "The cross-run stability table is not closed", None, notes, facts,
                 claims, outputs)


def build_21() -> dict[str, Any]:
    with variant({"receipt": _open("php_effect_seqs")}) as root:
        exported, _ = exported_facts(root)
    facts = exported + reviewer_facts() + census_facts()
    claims, outputs = _both_qualified(facts, {
        CREATE: "closed.php_effect_seqs is false: no php_effect_seqs_closed witness, so effect_order_closed lists an "
                "absent input and does not derive; !effect_order_any is unlicensed and op_qualified_rt cannot derive "
                "although every sequence row is present and in order.",
        CLOSE: "As for issues.create.", DELETE: "As for issues.create."},
        absent=("php_effect_seqs_closed",))
    notes = [
        "receipt.json says closed.php_effect_seqs = false; php_effect_seq.json is the control's (seven rows) and "
        "every other closure is present.  go_effect_seqs and model_effect_seqs stay closed so exactly one leaf is absent.",
        "All three op_qualified claims are unresolved with php_effect_seqs_closed as the only missing premise: an "
        "open sequence cannot license !effect_order_any.",
    ]
    return _case("21-missing-effect-seq-closure", "The PHP per-statement effect sequence is not closed", None, notes,
                 facts, claims, outputs)


def build_31() -> dict[str, Any]:
    with variant({"receipt": _open("php_responses")}) as root:
        exported, _ = exported_facts(root)
    facts = exported + reviewer_facts() + census_facts()
    claims, outputs = _both_qualified(facts, {
        CREATE: "closed.php_responses is false: no php_responses_closed witness, so repeat_delete_closed lists an "
                "absent input and does not derive; !repeat_delete_any is unlicensed and op_qualified_rt cannot "
                "derive, although every response row is present and the repeat did answer 404 on both sides.",
        CLOSE: "As for issues.create.", DELETE: "As for issues.create."},
        absent=("php_responses_closed",))
    not_found = _claim("claim-repeat-delete-not-found", "repeat_delete_not_found", ["run", "target"],
                       [RUN, DELETE_TARGET], {"run": RUN},
                       "the per-target claim reads the two 404 rows positively and needs no response closure, so it "
                       "is supported here: what the open table costs is the op gate's negation, not the claim.",
                       [observation("php_response", ["run"], {"column": "req", "operator": "=", "value": REPEAT_DELETE}),
                        observation("go_response", ["run"], {"column": "req", "operator": "=", "value": REPEAT_DELETE})])
    claims.append(not_found)
    notes = [
        "receipt.json says closed.php_responses = false; php_response.json is the control's (five rows) and every "
        "other closure is present, so exactly one leaf is absent.",
        "All three op_qualified claims are unresolved with php_responses_closed as the only missing premise.  This is "
        "the completeness half of the cross-request gate: without a closed response table a repeat whose 200 was "
        "simply not reported would look like a repeat that answered 404, and !repeat_delete_any would be a statement "
        "about what the harness happened to write down rather than about what the systems did.",
        "repeat_delete_not_found is still supported: it reads php_response / go_response positively.  The asymmetry "
        "is the point -- a positive claim may rest on the rows it was given, a negation may not.",
    ]
    return _case("31-missing-response-closure", "The PHP response table is not closed", None, notes,
                 facts, claims, outputs)


def build_27() -> dict[str, Any]:
    with variant({"model_well_formed": lambda _: None}) as root:
        exported, _ = exported_facts(root)
    facts = exported + reviewer_facts() + census_facts()
    claims, outputs = _both_qualified(facts, {
        CREATE: "model_well_formed.json is absent, so no typed checker certified the global structure of the model "
                "bound to this run: op_qualified_rt's positive structural premise has nothing to match and no op "
                "qualifies, however well PHP, Go and the operation-local checks agree.",
        CLOSE: "As for issues.create.", DELETE: "As for issues.create."},
        absent=("model_well_formed",))
    notes = [
        "The control receipt without model_well_formed.json; checker facts for the operation-local checks and every "
        "other observation, witness and closure remain present.",
        "All three op_qualified claims are unresolved with model_well_formed as the only missing premise. The "
        "global structure premise is positive, not a negation under a closure: an uncertified model structure is not "
        "qualified by silence.",
    ]
    return _case("27-model-not-well-formed", "No typed well-formedness certificate for the model", None, notes,
                 facts, claims, outputs)


def _foreign_certificate() -> dict[str, Any]:
    """A checker certificate that reached the judge at claim time and names another model.

    The exporter refuses such a row inside a receipt (``stale``: the certificate is
    for a different artifact), so the only way it reaches a judge is directly from
    the checker -- which is exactly the shape a stale Stage D run produces."""
    return _claim_time_fact("model_well_formed", ["model", "checker", "checker_version",
                                                    "checker_binary", "certificate"],
                            [OTHER_MODEL, CHECKER, CHECKER_VERSION, CHECKER_BINARY, CHECKER_CERTIFICATE],
                            {"model": OTHER_MODEL}, "modelcheck",
                            f"modelcheck {CHECKER} {CHECKER_VERSION} model:{OTHER_MODEL[:12]}",
                            [f"external:model:{OTHER_MODEL}"])


def build_28() -> dict[str, Any]:
    with variant({"model_well_formed": lambda _: None}) as root:
        exported, _ = exported_facts(root)
    facts = exported + reviewer_facts() + census_facts() + [_foreign_certificate()]
    foreign = find_id(facts, "model_well_formed")
    reason = ("the only checker certificate names model " + OTHER_MODEL[:12] + ", not the model "
              "model_describes_run binds to this run, so it does not join")
    override = {op: {"model_well_formed": missing(claim_id_for(op), "model_well_formed", reason,
                                                  requires=[foreign])} for op in OPS}
    claims, outputs = _both_qualified(facts, {
        CREATE: "A certificate exists, but for another model: op_qualified_rt joins model_well_formed(M, ...) on the "
                "same M as model_describes_run(M, Run), and the certified model is not the run's, so the premise "
                "does not bind and no op qualifies.",
        CLOSE: "As for issues.create.", DELETE: "As for issues.create."},
        override=override)
    notes = [
        "The control receipt without model_well_formed.json, plus a claim-time structural certificate for a different "
        "model digest. The exporter refuses such a row inside a receipt (stale: the certificate is for another "
        "artifact), so a certificate naming a foreign model can only arrive at claim time. Operation-local checked "
        "rows and reviewer admissions remain bound to the receipt model.",
        "All three op_qualified claims are unresolved with model_well_formed as the missing premise, triggered by the "
        "foreign structural certificate: the join is on the model, so a certificate for another model is no evidence. "
        "Contrast case 27, where no structural certificate exists.",
        "The foreign structural row is the only fact whose model differs from the case's model.",
    ]
    return _case("28-well-formed-other-model", "The checker certified a different model", "well-formed-other-model",
                 notes, facts, claims, outputs)


def build_29() -> dict[str, Any]:
    with variant({"model_checkers": lambda _: None}) as root:
        exported, _ = exported_facts(root, reviewer_authority=False)
    facts = exported + reviewer_facts() + census_facts()
    claims, outputs = _both_qualified(facts, {
        CREATE: "The model and this operation have successful checker facts, but the reviewer's exact operation "
                "admission is absent, so model_checker_admitted has no tuple to join and op_qualified_rt cannot "
                "derive.",
        CLOSE: "As for issues.create.", DELETE: "As for issues.create."},
        absent=("model_checker_admitted",))
    notes = [
        "The model_checkers.json row in the receipt is ignored, and the caller supplies no reviewer admission. "
        "The structural and operation checker facts remain present.",
        "All three op_qualified claims are unresolved with model_checker_admitted as the only missing premise: "
        "reviewer authority must join the exact model, operation, checker, version, binary and semantic-certificate "
        "tuple, not merely a checker name and version.",
    ]
    return _case("29-checker-not-admitted", "The certifying checker version is not admitted by the reviewer", None,
                 notes, facts, claims, outputs)


def build_32() -> dict[str, Any]:
    def only_delete(document: Any) -> Any:
        return {**document, "rows": [row for row in document["rows"] if row["operation"] == DELETE]}

    with variant({"model_operation_checked": only_delete}) as root:
        exported, _ = exported_facts(root)
    facts = exported + reviewer_facts() + census_facts()
    claims, outputs = _both_qualified(facts, {
        CREATE: "The model is structurally well formed and delete-issue has exact checker and reviewer authority, "
                "but issues.create has no operation-local checked certificate, so it stays pending.",
        CLOSE: "As for issues.create.",
        DELETE: "delete-issue has a successful operation-local certificate admitted for the exact model, operation, "
                "checker version, native checker binary and semantic certificate digest, so it qualifies as in case 00."})
    notes = [
        "The receipt contains only the model_operation_checked row for delete-issue; caller-supplied reviewer "
        "authority also admits only that exact tuple. The structural model_well_formed fact remains present.",
        "delete-issue remains supported while issues.create and issues.close are unresolved with "
        "model_operation_checked as their only missing premise. No operation can inherit another operation's fact.",
    ]
    return _case("32-partial-operation-check", "One checked operation does not grant authority to its siblings",
                 None, notes, facts, claims, outputs)


def build_rejected_30() -> dict[str, Any]:
    case = build_00()
    case["id"] = "30-well-formed-producer-violation"
    case["title"] = "Positive control whose model_well_formed row is emitted by the model host"
    case["provenance"]["seeded_fault"] = "well-formed-producer-violation"
    relabelled = 0
    for entry in case["facts"]:
        if entry["relation"] == "model_well_formed":
            entry["source"] = REJECTED_SOURCE
            relabelled += 1
    assert relabelled == 1
    case["review_notes"] = [
        "The facts are the control's; only the model_well_formed certificate names the shen class, i.e. the model "
        "host certifying the well-formedness of its own model.",
        "load_case refuses the file (evidence-producer: model_well_formed admits modelcheck); evaluated unvalidated "
        "all three op_qualified verdicts would be supported (rejected.json).  This is the producer boundary the "
        "premise exists for: a host that could vouch for itself would add nothing to the judgement.",
    ]
    return case


def build_rejected_22() -> dict[str, Any]:
    case = build_00()
    case["id"] = "22-effect-seq-producer-violation"
    case["title"] = "Positive control whose php_effect_seq rows claim the shen producer class"
    case["provenance"]["seeded_fault"] = "effect-seq-producer-violation"
    relabelled = 0
    for entry in case["facts"]:
        if entry["relation"] == "php_effect_seq":
            entry["source"] = REJECTED_SOURCE
            relabelled += 1
    assert relabelled == 7
    case["review_notes"] = [
        "The facts are the control's; only the seven php_effect_seq evidence sources name the shen class, i.e. the "
        "model host reporting PHP's statement order on PHP's behalf.",
        "load_case refuses the file (evidence-producer: php_effect_seq admits php); evaluated unvalidated every claim "
        "of the control would be supported (rejected.json).",
    ]
    return case


REJECTED_EXCLUSION_SOURCE = "replay capcov.claims.replay.replay_facts 2026-09-16 model:" + MODEL[:12] + " run:" + RUN


def build_rejected_16() -> dict[str, Any]:
    case = build_00()
    case["id"] = "16-exclusion-producer-violation"
    case["title"] = "Positive control whose scope exclusions are emitted by the harness"
    case["provenance"]["seeded_fault"] = "exclusion-producer-violation"
    relabelled = 0
    for entry in case["assumptions"]:
        if entry["relation"] == "model_scope_exclusion":
            entry["source"] = REJECTED_EXCLUSION_SOURCE
            relabelled += 1
    assert relabelled == 2
    case["review_notes"] = [
        "The facts are the control's; only the two model_scope_exclusion assumptions name the replay class, i.e. the "
        "harness excluding tables from the write-set judgement on the reviewer's behalf.",
        "load_case refuses the file (evidence-producer: model_scope_exclusion admits reviewer); evaluated unvalidated "
        "all three op_qualified verdicts would be supported (rejected.json).",
    ]
    return case


def build_rejected_12() -> dict[str, Any]:
    case = build_00()
    case["id"] = "12-closure-producer-violation"
    case["title"] = "Positive control whose model_admissible_closed witness is emitted by the harness"
    case["provenance"]["seeded_fault"] = "closure-producer-violation"
    relabelled = 0
    for entry in case["facts"]:
        if entry["relation"] == "model_admissible_closed":
            entry["source"] = REJECTED_CLOSURE_SOURCE
            relabelled += 1
    assert relabelled == 1
    case["review_notes"] = [
        "The facts are the control's; only the model_admissible_closed witness names the replay class, i.e. the "
        "harness closing the model runner's admissible set on its behalf.",
        "load_case refuses the file (evidence-producer: model_admissible_closed admits shen); evaluated unvalidated "
        "all three op_qualified verdicts would be supported (rejected.json).",
    ]
    return case


def build_rejected_07() -> dict[str, Any]:
    case = build_00()
    case["id"] = "07-producer-class-violation"
    case["title"] = "Positive control whose php_post_state rows claim the shen producer class"
    case["provenance"]["seeded_fault"] = "producer-class-violation"
    relabelled = 0
    for entry in case["facts"]:
        if entry["relation"] == "php_post_state":
            entry["source"] = REJECTED_SOURCE
            relabelled += 1
    assert relabelled == 5
    case["review_notes"] = [
        "The facts are the control's; only the five php_post_state evidence sources name the shen class.",
        "load_case refuses the file (evidence-producer); validate_bundle on the unvalidated bundle reports exactly five "
        "evidence-producer issues; the closure and all three op_qualified verdicts are otherwise those of the control, "
        "which is why validation is the sole barrier (rejected.json).",
    ]
    return case


BUILDERS: dict[str, Callable[[], dict[str, Any]]] = {
    "00-positive-control": build_00,
    "01-planted-disagreement": build_01,
    "02-planted-undeclared-write": build_02,
    "03-surviving-mutant": build_03,
    "04-missing-model-witness": build_04,
    "05-missing-snapshot-witness": build_05,
    "06-stale-replay": build_06,
    "08-lying-closure": build_08,
    "09-missing-post-state": build_09,
    "10-missing-effects-closure": build_10,
    "11-missing-admissible-closure": build_11,
    "13-excluded-undeclared-write": build_13,
    "14-exclusions-not-closed": build_14,
    "15-no-exclusions-no-closure": build_15,
    "17-effect-order-violation": build_17,
    "18-repeat-delete-with-effects": build_18,
    "19-unstable-oracle": build_19,
    "20-missing-stability-closure": build_20,
    "21-missing-effect-seq-closure": build_21,
    "23-repeat-delete-excluded-write": build_23,
    "24-repeat-before-the-commit": build_24,
    "25-first-delete-not-committed": build_25,
    "26-unstable-on-one-side": build_26,
    "27-model-not-well-formed": build_27,
    "28-well-formed-other-model": build_28,
    "29-checker-not-admitted": build_29,
    "31-missing-response-closure": build_31,
    "32-partial-operation-check": build_32,
}
REJECTED_BUILDERS: dict[str, Callable[[], dict[str, Any]]] = {
    "07-producer-class-violation": build_rejected_07,
    "12-closure-producer-violation": build_rejected_12,
    "16-exclusion-producer-violation": build_rejected_16,
    "22-effect-seq-producer-violation": build_rejected_22,
    "30-well-formed-producer-violation": build_rejected_30,
}

# The reviewer's table: claim -> (verdict, status, missing-premise relations, discrepancy kinds).
SUPPORTED = ("supported", "complete", [], [])


def _all_ops(missing_relation: str, status: str = "complete") -> dict[str, tuple[str, str, list[str], list[str]]]:
    """Every op_qualified claim unresolved with the same run-wide missing premise."""
    return {claim_id_for(op): ("unresolved", status, [missing_relation], []) for op in OPS}


REVIEW: dict[str, dict[str, tuple[str, str, list[str], list[str]]]] = {
    "00-positive-control": {"claim-qualified-create": SUPPORTED, "claim-qualified-close": SUPPORTED,
                            "claim-qualified-delete": SUPPORTED,
                            "claim-repeat-delete-not-found": SUPPORTED, "claim-oracle-stable": SUPPORTED},
    "01-planted-disagreement": {"claim-qualified-create": SUPPORTED,
                                "claim-qualified-close": ("unresolved", "complete", ["php_model_agree"], []),
                                "claim-qualified-delete": SUPPORTED,
                                "claim-php-disagrees-req-2": ("supported", "complete", [], ["php-state-outside-model"])},
    "02-planted-undeclared-write": {"claim-qualified-create": SUPPORTED,
                                    "claim-qualified-close": ("unresolved", "complete", ["model_writes"], []),
                                    "claim-qualified-delete": SUPPORTED,
                                    "claim-undeclared-audit-log": ("supported", "complete", [], ["undeclared-table"])},
    "03-surviving-mutant": {"claim-qualified-create": SUPPORTED,
                            "claim-qualified-close": ("unresolved", "complete", ["mutant_killed"], []),
                            "claim-qualified-delete": SUPPORTED,
                            "claim-m2-survives": ("supported", "complete", [], ["mutant-not-killed"])},
    "04-missing-model-witness": {**_all_ops("model_describes_run"),
                                 "claim-php-agrees-req-1": ("unresolved", "complete", ["model_describes_run"], [])},
    "05-missing-snapshot-witness": _all_ops("replay_run_current"),
    "06-stale-replay": {**_all_ops("replay_run_current", "stale"), "claim-run-stale": SUPPORTED},
    "08-lying-closure": {**_all_ops("replay_request"),
                         "claim-kill-outside-corpus": ("supported", "complete", [], ["kill-outside-replayed-requests"])},
    "09-missing-post-state": {"claim-qualified-create": ("unresolved", "complete", ["php_post_state"], []),
                              "claim-qualified-close": SUPPORTED, "claim-qualified-delete": SUPPORTED,
                              "claim-req-3-php-post-state-gap": ("supported", "complete", [], ["post-state-missing"])},
    "10-missing-effects-closure": _all_ops("php_effects_closed"),
    "11-missing-admissible-closure": {**_all_ops("model_admissible_closed"), "claim-php-agrees-req-1": SUPPORTED},
    "13-excluded-undeclared-write": {"claim-qualified-create": SUPPORTED, "claim-qualified-close": SUPPORTED,
                                     "claim-qualified-delete": SUPPORTED,
                                     "claim-audit-log-excluded": ("supported", "complete", [], ["write-excluded-by-reviewer"])},
    "14-exclusions-not-closed": _all_ops("model_scope_exclusions_closed"),
    "15-no-exclusions-no-closure": _all_ops("model_scope_exclusions_closed"),
    "17-effect-order-violation": {"claim-qualified-create": ("unresolved", "complete", ["effect_order_respected"], []),
                                  "claim-qualified-close": SUPPORTED, "claim-qualified-delete": SUPPORTED,
                                  "claim-go-order-violated-req-1": ("supported", "complete", [], ["effect-order-violated"])},
    "18-repeat-delete-with-effects": {"claim-qualified-create": SUPPORTED, "claim-qualified-close": SUPPORTED,
                                      "claim-qualified-delete": ("unresolved", "complete", ["repeat_delete"], []),
                                      "claim-repeat-delete-has-effects": ("supported", "complete", [], ["repeat-delete-with-effects"]),
                                      "claim-repeat-delete-not-found": ("unresolved", "complete", ["repeat_delete_has_effect"], [])},
    "19-unstable-oracle": {**_all_ops("oracle_stable"),
                           "claim-oracle-unstable": ("supported", "complete", [], ["oracle-unstable"])},
    "20-missing-stability-closure": _all_ops("replay_stability_closed"),
    "21-missing-effect-seq-closure": _all_ops("php_effect_seqs_closed"),
    "24-repeat-before-the-commit": {"claim-qualified-create": SUPPORTED, "claim-qualified-close": SUPPORTED,
                                   "claim-qualified-delete": SUPPORTED,
                                   "claim-repeat-delete-not-found": ("unresolved", "complete",
                                                                     ["first_delete_committed"], [])},
    "25-first-delete-not-committed": {"claim-qualified-create": SUPPORTED, "claim-qualified-close": SUPPORTED,
                                      "claim-qualified-delete": ("unresolved", "complete",
                                                                 ["effect_order_respected"], []),
                                      "claim-repeat-delete-not-found": ("unresolved", "complete",
                                                                        ["first_delete_committed"], [])},
    "26-unstable-on-one-side": {**_all_ops("oracle_stable"),
                                "claim-oracle-unstable": ("supported", "complete", [], ["oracle-unstable"])},
    "27-model-not-well-formed": _all_ops("model_well_formed"),
    "28-well-formed-other-model": _all_ops("model_well_formed"),
    "29-checker-not-admitted": _all_ops("model_checker_admitted"),
    "32-partial-operation-check": {
        "claim-qualified-create": ("unresolved", "complete", ["model_operation_checked"], []),
        "claim-qualified-close": ("unresolved", "complete", ["model_operation_checked"], []),
        "claim-qualified-delete": SUPPORTED,
    },
    "31-missing-response-closure": {**_all_ops("php_responses_closed"),
                                    "claim-repeat-delete-not-found": SUPPORTED},
    "23-repeat-delete-excluded-write": {"claim-qualified-create": SUPPORTED, "claim-qualified-close": SUPPORTED,
                                        "claim-qualified-delete": SUPPORTED,
                                        "claim-repeat-delete-not-found": SUPPORTED,
                                        "claim-session-touch-excluded": ("supported", "complete", [],
                                                                         ["write-excluded-by-reviewer"])},
}
# Evidence a derivation must not use, per case: the lying witness of 08.
FORBIDDEN: dict[str, Callable[[list[dict[str, Any]]], list[str]]] = {
    "08-lying-closure": lambda facts: [find_id(facts, "mutant_kills_closed")],
}


def build(stem: str) -> dict[str, Any]:
    """The case document without its ``expected`` table."""
    builders = {**BUILDERS, **REJECTED_BUILDERS}
    return builders[stem]()


def build_all() -> dict[str, dict[str, Any]]:
    return {stem: build(stem) for stem in BUILDERS}


def _dump(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _expected_table(case: dict[str, Any]) -> dict[str, Any]:
    """Evaluate the case with the Python kernel and check it against REVIEW."""
    from capcov.claims import bundle_from_json
    from capcov.claims.evaluator import evaluate
    from capcov.claims.output import VerifiedProofEvidence, relevant_evidence_ids, render_outputs

    try:
        from .adapter import bundle_payload
    except ImportError:
        from replay_rules.adapter import bundle_payload

    bundle = bundle_from_json(bundle_payload(case), validate=True)
    report = evaluate(bundle)
    if report.status.value != "complete":
        raise AssertionError(f"{case['id']}: {report.message}")
    ids = {record.id for record in bundle.evidence}
    forbidden = FORBIDDEN.get(case["id"], lambda facts: [])(case["facts"])
    table = {}
    for entry in report.claims:
        result = entry.result
        leaves = set(result.support) | set(result.refutation)
        proof = VerifiedProofEvidence.from_bundle(bundle, entry.claim.id, leaves) if leaves else None
        rendered = render_outputs(bundle, entry.claim.id, ids, proof, claim_state=result.semantic.value)
        discrepancies = [item["fields"] for item in rendered if item["kind"] == "discrepancy"]
        observed = relevant_evidence_ids(bundle, entry.claim.id, ids, leaves)
        review = REVIEW[case["id"]][entry.claim.id]
        actual = (result.semantic.value, result.operational.value,
                  sorted(item["relation"] for item in result.missing_premises),
                  sorted(item["kind"] for item in discrepancies))
        if actual != (review[0], review[1], sorted(review[2]), sorted(review[3])):
            raise AssertionError(f"{case['id']} {entry.claim.id}: evaluator says {actual}, reviewer says {review}")
        table[entry.claim.id] = {
            "semantic_verdict": result.semantic.value,
            "operational_status": result.operational.value,
            "support_leaves": sorted(result.support),
            "refutation_leaves": sorted(result.refutation),
            "observed_leaves": sorted(observed | leaves),
            "forbidden_leaves": sorted(leaf for leaf in forbidden if leaf in (observed | leaves)),
            "discrepancies": sorted(discrepancies, key=canonical_json),
            "missing_premises": sorted(result.missing_premises, key=canonical_json),
        }
    return {"claims": table}


def write_cases() -> None:
    expected = {"schema_version": 1, "description": "Per-claim reviewed expectations duplicated from each replay case",
                "rule_pack": PACK_ID,
                "evaluation_basis_by_quantifier": {"exists": "derivational", "forall": "bounded-history-model"},
                "cases": {}}
    for stem, case in build_all().items():
        case["expected"] = _expected_table(case)
        expected["cases"][stem] = case["expected"]
        _dump(CASES_DIR / f"{stem}.json", case)
    _dump(CASES_DIR.parent / "expected.json", expected)
    for stem, builder in REJECTED_BUILDERS.items():
        _dump(REJECTED_DIR / f"{stem}.json", builder())
    print(f"wrote {len(BUILDERS)} cases and {len(REJECTED_BUILDERS)} rejected case(s)")


if __name__ == "__main__":
    write_cases()
