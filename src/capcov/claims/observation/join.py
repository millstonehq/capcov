"""Judge one observation receipt directory with the observation pack (Lane A).

``build`` exports the receipt directory (``observation_facts.export_bundle``; a
refusal is a *contract finding* the caller reports, never something to work
around), combines it with ``rules-observation-v1`` (``observation.pack``) and
the claim-time rows a judge adds, and states TWO claims per run::

    claim-observations-agree-<run> : observations_agree(run, check, set, policy)
    claim-incumbent-stable-<run>   : incumbent_stable(run)

``evaluate_join`` runs the fail-closed differential -- two kernels (python and
interpreted Soufflé) or, with ``kernels="three"``, the compiled Soufflé checker
as well -- and certifies every claim row from every closure; the certificates
must be identical across kernels.

WHAT THE VERDICT WORD MEANS.  ``qualification`` returns ``"agreeing"`` or
``"unsupported"``, never ``"qualified"``: that word belongs to ``op_qualified``
in the replay pack and means something this claim does not say.
``observations_agree`` says that on the scenarios the admitted set declares,
the incumbent and the candidate were observed to answer alike under the
admitted comparison policy.  It is not a correctness claim -- a defect present
in both is exactly what agreement looks like -- and it is not a claim about
capability parity: nothing here relates the scenario set to the capability's
behaviour space.  The primary failure mode of the whole design is a reader who
abbreviates the verdict to "parity", which is why ``summary`` always prints the
scenario count, the kind histogram and every ``unassessed`` row verbatim.

WHAT ``summary`` ALWAYS CARRIES.  Every ``unassessed`` row and every message the
exporter produced -- the open completeness boxes with their reasons, the absent
admissions ledger, the absent log -- are in the report whatever the verdict, and
in the REFUSED report too, where the closure has no rows to read and the rows
are taken from the receipt and labelled ``unassessed_source: "receipt"``.  A
disclosure that exists only in an input nobody prints is not a disclosure.

``PENDING_PREMISES`` is deliberately EMPTY.  Lane B splits out premises no
receipt can satisfy yet because the artefact that would satisfy them is not
built; Lane A has no such premise, and borrowing that framing would let a real
finding read as "not built yet".

THE THREE ROWS THE JUDGE ADDS, AND WHY THEY ARE NOT COPIED.
``observation_source_observed`` is recomputed from the clean candidate Git
checkout and the exact prepared-runtime manifest bytes, never lifted from the
receipt.  The strict CLI additionally requires a pre-run external invocation
record to pin the candidate and incumbent identities.  The CLI does not
independently traverse or verify the incumbent runtime's complete source
closure; the caller owns that preflight, while this check confirms the pinned
identity and manifest bytes.  A mismatch is a contract finding with status
``stale``, and the row is withheld.  ``observation_nonce_observed`` takes its
nonce from the reviewer (``nonce=``), not from the receipt, and a mismatch is
likewise ``stale``.  ``observation_fixture_observed`` is the reviewer's
observation of the fixture digest.  ``fixture=True`` is the fixture-judging
mode, for committed receipts with no tree to observe: the three rows are then
taken from the receipt and the caveat is recorded in the summary, loudly,
because in that mode freshness and source binding are assumed rather than
observed (accepted risk R9).

The three ASSUMPTION rows (``policy_admitted``, ``scenario_set_admitted``,
``masked_difference_admitted``) are not added here at all: they come from the
committed ``observation_admissions.json`` through the exporter, which is where
their staleness against the receipt's digests is judged.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..ir import (Atom, Bundle, Claim, Constant, Context, Evidence, canonical_json)
from ..differential import CompiledKernelMismatch, DifferentialMismatch, compare, compare_three
from ..differential import KernelReport, run_python
from ..souffle import program_for_pack
from ..souffle.compile import CompiledChecker, compile_program
from ..static.combine import combine
from .. import assumptions
from ..static.ground import shared_assumptions, why, why_not
from ..static.certificate import certify, claim_conclusions, recheck
from . import observation_facts
from .pack import pack_bundle

REVIEWER_SOURCE = "reviewer claim-time observation"
FIXTURE_SOURCE = "reviewer fixture-mode observation (no tree was inspected)"

QUALIFICATION_AGREEING = "agreeing"
QUALIFICATION_UNSUPPORTED = "unsupported"
#: Empty on purpose: Lane A has no not-yet-built artefact, so no premise may be
#: reported as "pending" rather than as a finding.
PENDING_PREMISES: tuple[str, ...] = ()

#: The premises of observations_agree in the order a reviewer checks them; the
#: first that fails (or the first "any" that holds) is the blocking premise.
#: Every entry is run-keyed on its first column, which is what ``_RUN_ONLY``
#: records -- there is no op here and no index.
_BLOCKING_ORDER = (
    ("observation_run_current", False),
    ("policy_bound", False),
    ("set_bound", False),
    ("check_completed", False),
    ("model_conformance_unassessed", False),
    ("observation_failure_gate_closed", False), ("observation_failure_any", True),
    ("observation_gap_closed", False), ("observation_gap_any", True),
    ("set_exercised", False),
    ("observation_degenerate_closed", False), ("observation_degenerate_any", True),
    ("observation_undeclared_closed", False), ("observation_undeclared_any", True),
    ("scenario_undeclared_closed", False), ("scenario_undeclared_any", True),
    ("observation_masked_gate_closed", False), ("observation_masked_unadmitted", True),
    ("observation_disagreement_closed", False), ("observation_disagree_any", True),
    # C4: last, after disagreement -- an open bodies box is reported as "cannot
    # rule out a disagreement" first, which is the more direct consequence.
    ("observation_mask_disclosure_closed", False), ("observation_mask_undisclosed", True),
)
_RUN_ONLY = tuple(name for name, _ in _BLOCKING_ORDER)

REASONS = {
    "observation_run_current": (
        "the receipt's source digest, fixture digest or nonce was not observed in the tree "
        "being judged -- the receipt is not bound to this tree"),
    "policy_bound": (
        "no reviewer admitted this comparison policy digest; a mask that nobody signed is "
        "not a comparison"),
    "set_bound": (
        "no reviewer admitted this scenario set for this check, so the set could have been "
        "narrowed after the result was seen"),
    "check_completed": "the harness did not reach a compared terminal state",
    "model_conformance_unassessed": (
        "this run declares no conformance status; a receipt that carries a model belongs to "
        "op_qualified, not here"),
    "observation_failure_any": "the run recorded a setup, timeout or internal failure",
    "observation_gap_any": (
        "declared scenarios were not compared on both sides; an observation that did not run "
        "is not agreement"),
    "set_exercised": (
        "no declared scenario was observed on both sides, so the admitted set was not "
        "exercised at all"),
    "observation_degenerate_any": (
        "a scenario's incumbent answer was not of its pre-registered class, so the two sides "
        "may agree only because both failed"),
    "observation_undeclared_any": (
        "a normalization changed a compared value without being declared in the admitted "
        "policy"),
    "scenario_undeclared_any": (
        "a scenario was observed that the admitted set does not declare, so the set digest "
        "does not describe what ran"),
    "observation_masked_unadmitted": (
        "a normalization masked a real difference (the two sides differed before it fired) "
        "and no reviewer admitted that masking"),
    "observation_mask_undisclosed": (
        "a scenario's raw bodies differed while its normalized bodies matched -- a "
        "normalization erased a real difference, on one side or both -- and no "
        "masked_differences row discloses it, so there is nothing a reviewer could have "
        "admitted"),
    "observation_disagree_any": (
        "the incumbent and the candidate differed on an observed scenario"),
    "observation_failure_gate_closed": (
        "the failure list is not closed, so absence of a failure row proves nothing"),
    "observation_gap_closed": (
        "the scenario, observation or unobserved tables are not closed, so absence of a gap "
        "proves nothing"),
    "observation_degenerate_closed": (
        "the incumbent's class table is not closed, so absence of a degenerate scenario "
        "proves nothing"),
    "observation_undeclared_closed": (
        "the normalization or policy tables are not closed, so absence of an undeclared "
        "normalization proves nothing"),
    "scenario_undeclared_closed": (
        "the scenario and observation tables are not closed, so absence of an undeclared "
        "scenario proves nothing"),
    "observation_masked_gate_closed": (
        "the masked-difference table or the reviewer's admission list is not closed, so "
        "absence of an unadmitted mask proves nothing"),
    "observation_disagreement_closed": (
        "the status, body or normalization tables are not closed on both sides, so absence "
        "of a disagreement proves nothing"),
    "observation_mask_disclosure_closed": (
        "the masked-difference table or the body tables are not closed, so absence of an "
        "undisclosed erased difference proves nothing"),
}

#: What the judge REPORTS each diagnostic row as.  Every value is from
#: ``validation.py``'s allowed set (unresolved | refuted | invalid-input |
#: out-of-scope | required | stale | inconsistent-premises | refutation).
#: ``scenario_setup_failed`` gets its own predicate precisely so that a harness
#: that could not start is never reported as a finding against the candidate.
DIAGNOSTIC_STATUS = {
    "scenario_disagree": "refuted",
    "scenario_degenerate": "refuted",
    "observation_masked_unadmitted": "refuted",
    "observation_mask_undisclosed": "refuted",
    "body_masked": "out-of-scope",
    "normalization_undeclared": "refuted",
    "scenario_uncompared": "unresolved",
    "scenario_timed_out": "unresolved",
    "scenario_errored": "unresolved",
    "scenario_setup_failed": "unresolved",
    "observation_unassessed": "out-of-scope",
    "observation_masked_difference": "out-of-scope",
}
#: Closure relations whose falsity makes a negation unusable: "required", not a finding.
CLOSURE_STATUS = "required"

RECOMPUTED_NOTICE = ("Agreement was recomputed from per-side digests; the receipt's own "
                     "`results` block was not trusted.")
SUPPORTED_STATEMENT = (
    "On run {run}, with fixture {fixture} and source {source}, every one of the {count} "
    "scenarios admitted set {scenario_set} declares was observed on both the incumbent and "
    "the candidate -- none set up short, timed out, errored or was skipped -- and under "
    "admitted comparison policy {policy} no compared facet differed.")
#: What the claim does NOT say, restated beside every verdict.
NOT_A_CLAIM = ("This is not a correctness claim and not a capability-parity claim: it is "
               "bounded by the admitted scenario set, and two implementations agreeing is "
               "exactly what a shared defect looks like.")


def _row_id(prefix: str, segment: str, relation: str, row: list[Any]) -> str:
    return f"{prefix}:{segment}:{relation}:{observation_facts.row_digest(relation, row)[:12]}"


def _fact(decls, relation: str, values: dict[str, Any], evidence_id: str, source: str, *,
          kind: str = "fact", depends_on=()) -> tuple[Atom, Evidence]:
    decl = decls[relation]
    atom = Atom(relation, tuple(Constant(values[column.name], column.type)
                                for column in decl.columns))
    context = {name: values[name] for name in decl.context_indices}
    return atom, Evidence(evidence_id, atom, Context.from_mapping(context), source,
                          tuple(depends_on), kind)


# ---------------------------------------------------------------------------
# Source observation: recomputed from the trees, never lifted from the receipt.


def _git(root: Path, *args: str) -> str:
    return subprocess.run(("git", "-C", str(root), *args), check=True, capture_output=True,
                          text=True, timeout=60).stdout.strip()


@dataclass(frozen=True)
class SourceObservation:
    """What the judge saw in the trees, and whether it matches the receipt."""
    method: str                     # "tree" | "fixture" | "absent"
    observed: str | None            # the recomputed source digest, when one was computed
    matches: bool
    detail: str
    status: str = "unobserved"      # "verified" | "stale" | "unobserved" | "operational-failure"


def observe_source(receipt: dict[str, Any], candidate_root: Path | None,
                   incumbent_manifest: Path | None, *,
                   expected_incumbent_commit: str | None = None,
                   expected_incumbent_runtime_commit: str | None = None,
                   expected_manifest_sha256: str | None = None,
                   expected_source_digest: str | None = None,
                   expected_candidate_commit: str | None = None,
                   expected_candidate_tree: str | None = None) -> SourceObservation:
    """Recompute ``sources.source_digest`` from the candidate tree and the manifest.

    The candidate half is observed: ``HEAD`` and ``HEAD^{tree}`` of the worktree
    the caller names, plus a clean-worktree check, because a digest over a dirty
    tree names something nobody can check out again. The incumbent half is the
    exact prepared-runtime manifest bytes and commit identities supplied by the
    caller's pre-run record. This function does not independently verify the
    incumbent runtime's complete source closure; the caller owns that preflight.
    """
    declared = receipt["sources"]["source_digest"]
    if candidate_root is None or incumbent_manifest is None:
        return SourceObservation(
            "absent", None, False,
            "no candidate worktree and prepared-runtime manifest were given, so the receipt's "
            "source digest was not observed in any tree", "unobserved")
    root, manifest = Path(candidate_root), Path(incumbent_manifest)
    try:
        dirty = bool(_git(root, "status", "--porcelain"))
        commit = _git(root, "rev-parse", "HEAD")
        tree = _git(root, "rev-parse", "HEAD^{tree}")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        return SourceObservation("tree", None, False,
                                 f"the candidate worktree {str(root)!r} could not be read: {exc}",
                                 "operational-failure")
    if dirty:
        return SourceObservation("tree", None, False,
                                 f"the candidate worktree {str(root)!r} is dirty; a source "
                                 f"digest over a tree nobody can check out again binds nothing",
                                 "stale")
    if not manifest.is_file():
        return SourceObservation("tree", None, False,
                                 f"the prepared-runtime manifest {str(manifest)!r} is absent",
                                 "operational-failure")
    manifest_sha = hashlib.sha256(manifest.read_bytes()).hexdigest()
    incumbent = receipt["sources"]["incumbent"]
    # In strict live judging, the prepared invocation record supplies the
    # incumbent identity. Receipt-local values remain only the compatibility
    # default for old fixture callers and are never the CLI's authority.
    incumbent_commit = expected_incumbent_commit or incumbent["commit"]
    incumbent_runtime_commit = expected_incumbent_runtime_commit or incumbent["runtime_commit"]
    observed = hashlib.sha256(canonical_json({
        "candidate": {"repo": receipt["sources"]["candidate"]["repo"], "commit": commit,
                      "tree": tree},
        "incumbent": {"commit": incumbent_commit,
                      "runtime_commit": incumbent_runtime_commit,
                      "manifest_sha256": manifest_sha},
    }).encode("utf-8")).hexdigest()
    if ((expected_manifest_sha256 is not None and manifest_sha != expected_manifest_sha256)
            or observed != declared
            or (expected_source_digest is not None and observed != expected_source_digest)
            or (expected_incumbent_commit is not None
                and incumbent["commit"] != expected_incumbent_commit)
            or (expected_incumbent_runtime_commit is not None
                and incumbent["runtime_commit"] != expected_incumbent_runtime_commit)
            or (expected_candidate_commit is not None and commit != expected_candidate_commit)
            or (expected_candidate_tree is not None and tree != expected_candidate_tree)
            or receipt["sources"]["candidate"]["commit"] != commit
            or receipt["sources"]["candidate"]["tree"] != tree):
        return SourceObservation(
            "tree", observed, False,
            f"the receipt names source {declared[:12]} but the candidate checkout and "
            f"prepared-runtime manifest recompute to "
            f"{observed[:12]} (candidate commit {commit[:12]}, tree {tree[:12]}, manifest "
            f"{manifest_sha[:12]}); the receipt is not a receipt for this tree", "stale")
    return SourceObservation("tree", observed, True,
                             f"the candidate checkout and prepared-runtime manifest bytes "
                             f"recompute the bound source digest {observed[:12]} "
                             f"(candidate commit {commit[:12]}); incumbent runtime closure "
                             "was not independently verified", "verified")


# ---------------------------------------------------------------------------


@dataclass
class ObservationJoin:
    receipt_dir: Path
    receipt: dict[str, Any]
    run: str
    exported: Any
    contract_findings: list[str] = field(default_factory=list)
    bundle: Bundle | None = None
    check: str = ""
    scenario_set: str = ""
    policy: str = ""
    fixture: str = ""
    source_digest: str = ""
    source_observation: SourceObservation | None = None
    assumption_ids: tuple[str, ...] = ()
    result: Any = None
    """``DifferentialResult`` (two kernels) or ``ThreeWayResult`` (three) when the kernels agree."""
    mismatch: Any = None
    """The disagreeing result when they do not."""
    kernels: str = "two"
    checker: CompiledChecker | None = None
    certificates: dict[str, dict[str, Any]] = field(default_factory=dict)
    row_certificates: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    invalidations: dict[str, assumptions.Invalidation] = field(default_factory=dict)
    python_report: KernelReport | None = None

    def claim_id(self, kind: str) -> str:
        return f"claim-{kind}-{self.run}"

    @property
    def agree_claim(self) -> str:
        return self.claim_id("observations-agree")

    @property
    def stable_claim(self) -> str:
        return self.claim_id("incumbent-stable")

    def report(self):
        if self.python_report is not None:
            return self.python_report
        return self.result.python if self.result is not None else (
            self.mismatch.python if self.mismatch else None)

    def closures(self) -> list[Any]:
        if self.python_report is not None:
            return [self.python_report]
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


def build(directory: Path | str, *, candidate_root: Path | str | None = None,
          incumbent_manifest: Path | str | None = None, nonce: str | None = None,
          fixture_digest: str | None = None, fixture: bool = False,
          allow_receipt_admissions: bool | None = None,
          external_admissions_path: Path | str | None = None,
          external_admissions_bytes: bytes | None = None,
          expected_incumbent_commit: str | None = None,
          expected_incumbent_runtime_commit: str | None = None,
          expected_manifest_sha256: str | None = None,
          expected_source_digest: str | None = None,
          expected_candidate_commit: str | None = None,
          expected_candidate_tree: str | None = None) -> ObservationJoin:
    """Export the receipt, add the reviewer's three observations, state the two claims.

    ``candidate_root`` / ``incumbent_manifest`` are the trees under judgment.
    Without them ``observation_source_observed`` is NOT emitted and the claim
    fails at ``observation_run_current``, which is the fail-closed answer: an
    unobserved receipt is not a bound receipt.  ``nonce`` is the reviewer's own
    copy of the run nonce; ``fixture_digest`` the reviewer's observation of the
    fixture.  ``fixture=True`` judges a committed fixture receipt with no tree
    to inspect and takes all three from the receipt, recording that it did.
    """
    directory = Path(directory)
    if allow_receipt_admissions and not fixture:
        raise ValueError("receipt-local admissions are available only in explicit fixture mode")
    if external_admissions_path is not None and external_admissions_bytes is not None:
        raise ValueError("external admissions must use a path or a byte snapshot, not both")
    receipt_admissions = fixture if allow_receipt_admissions is None else allow_receipt_admissions
    receipt = json.loads((directory / observation_facts.RECEIPT_FILE).read_text(encoding="utf-8"))
    run = receipt.get("run", {}).get("id", "")
    exported = observation_facts.export_bundle(
        directory, run=run or None, admissions_path=external_admissions_path,
        admissions_bytes=external_admissions_bytes,
        allow_receipt_admissions=(receipt_admissions and external_admissions_path is None
                                  and external_admissions_bytes is None))
    join = ObservationJoin(directory, receipt, run, exported)
    if exported.status != observation_facts.STATUS_COMPLETE:
        join.contract_findings = [f"exporter refused the receipt ({exported.status}): {m}"
                                  for m in exported.messages]
        return join
    metadata = dict(exported.bundle.metadata)
    join.check = metadata["check"]
    join.scenario_set = metadata["scenario_set"]
    join.policy = metadata["policy"]
    join.fixture = metadata["fixture"]
    join.source_digest = metadata["source_digest"]

    pack = pack_bundle()
    decls = {decl.name: decl for decl in (*exported.bundle.relations, *pack.relations)}
    additions: list[tuple[Atom, Evidence]] = []

    # --- the nonce: the reviewer's copy, not the receipt's -------------------
    receipt_nonce = metadata["nonce"]
    if fixture:
        nonce_value, nonce_ok = receipt_nonce, True
    elif nonce is None:
        nonce_value, nonce_ok = None, False
        join.contract_findings.append(
            "no --nonce was given, so the run's freshness was not observed outside the "
            "receipt; observation_nonce_observed is withheld")
    elif nonce != receipt_nonce:
        nonce_value, nonce_ok = None, False
        join.contract_findings.append(
            f"stale: the reviewer's nonce {nonce[:12]} is not the receipt's "
            f"{receipt_nonce[:12]}; this receipt is not the run the reviewer watched")
    else:
        nonce_value, nonce_ok = receipt_nonce, True
    if nonce_ok:
        source = FIXTURE_SOURCE if fixture else REVIEWER_SOURCE
        additions.append(_fact(decls, "observation_nonce_observed",
                               {"run": run, "nonce": nonce_value},
                               _row_id("reviewer", "claim-time", "observation_nonce_observed",
                                       [run, nonce_value]), source))

    # --- the source: recomputed from the trees under judgment ----------------
    if fixture:
        observation = SourceObservation(
            "fixture", join.source_digest, True,
            "fixture mode: the receipt's source digest was taken from the receipt, not "
            "observed in any tree, so source binding is ASSUMED here and not checked", "assumed")
    else:
        observation = observe_source(
            receipt, candidate_root, incumbent_manifest,
            expected_incumbent_commit=expected_incumbent_commit,
            expected_incumbent_runtime_commit=expected_incumbent_runtime_commit,
            expected_manifest_sha256=expected_manifest_sha256,
            expected_source_digest=expected_source_digest,
            expected_candidate_commit=expected_candidate_commit,
            expected_candidate_tree=expected_candidate_tree)
    join.source_observation = observation
    if observation.matches:
        additions.append(_fact(decls, "observation_source_observed",
                               {"source_digest": join.source_digest},
                               _row_id("reviewer", "claim-time", "observation_source_observed",
                                       [join.source_digest]),
                               FIXTURE_SOURCE if fixture else REVIEWER_SOURCE,
                               depends_on=[f"external:source:{join.source_digest}"]))
    else:
        # A mismatch is a contract finding with status ``stale``; an absent
        # observation is simply an absent premise.  Neither invents the row.
        status = observation.status
        join.contract_findings.append(f"{status}: {observation.detail}")

    # --- the fixture: the reviewer's observation -----------------------------
    if fixture or fixture_digest == join.fixture:
        additions.append(_fact(decls, "observation_fixture_observed",
                               {"fixture": join.fixture},
                               _row_id("reviewer", "claim-time", "observation_fixture_observed",
                                       [join.fixture]),
                               FIXTURE_SOURCE if fixture else REVIEWER_SOURCE,
                               depends_on=[f"external:fixture:{join.fixture}"]))
    elif fixture_digest is None:
        join.contract_findings.append(
            "no fixture digest was observed by the reviewer; observation_fixture_observed "
            "is withheld")
    else:
        join.contract_findings.append(
            f"stale: the reviewer observed fixture {fixture_digest[:12]} but the receipt "
            f"names {join.fixture[:12]}")

    join.assumption_ids = tuple(sorted(record.id for record in exported.bundle.evidence
                                       if record.kind == "assumption"))
    claims = [
        Claim("observations_agree",
              (Constant(run, "symbol"), Constant(join.check, "symbol"),
               Constant(join.scenario_set, "digest"), Constant(join.policy, "digest")),
              Context.from_mapping({"run": run}), id=join.agree_claim),
        # Stated ALWAYS, beside the verdict.  With no repeat block it comes back
        # unresolved, which is exactly what the mandatory oracle_stability
        # unassessed row says in prose.
        Claim("incumbent_stable", (Constant(run, "symbol"),),
              Context.from_mapping({"run": run}), id=join.stable_claim),
    ]
    join.bundle = combine(
        exported.bundle, pack,
        facts=[atom for atom, _ in additions],
        evidence=[record for _, record in additions],
        claims=claims,
        metadata={"experiment": "observation receipt join (Lane A, judge side)",
                  "check": join.check, "scenario_set": join.scenario_set,
                  "policy": join.policy, "fixture": join.fixture,
                  "source_digest": join.source_digest,
                  "source_observation": observation.method,
                  "source_observed": observation.matches,
                  "nonce_observed": nonce_ok,
                  "assumptions": list(join.assumption_ids)})
    return join


def evaluate_join(join: ObservationJoin, replay_root: str, *, kernels: str = "two",
                  checker: CompiledChecker | None = None,
                  cache_dir: str | Path = ".capcov/compiled",
                  executable: str = "souffle") -> ObservationJoin:
    """Run the kernels and certify every claim row from every closure."""
    if join.bundle is None:
        return join
    if kernels not in ("two", "three"):
        raise ValueError("kernels must be 'two' or 'three'")
    join.kernels = kernels
    if kernels == "three" and checker is None:
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


def evaluate_python(join: ObservationJoin) -> ObservationJoin:
    """Evaluate only the pure-Python kernel and recheck available certificates.

    This is the live bridge's deliberately single-kernel report. It never
    invents a second backend or calls a missing backend a disagreement.
    """
    if join.bundle is None:
        return join
    report = run_python(join.bundle)
    join.python_report = report
    if report.operational_failure is not None:
        return join
    try:
        for claim in join.bundle.claims:
            rows = claim_conclusions(join.bundle, report.relations, claim)
            for row in rows:
                certificate = certify(join.bundle, report.relations, claim.relation, row)
                if certificate.get("truncated"):
                    continue
                checked = recheck(join.bundle, certificate, report.relations)
                if not checked.ok:
                    raise ValueError("certificate failed recheck")
                join.certificates.setdefault(claim.id, certificate)
                join.row_certificates.setdefault(claim.id, []).append(certificate)
    except (AssertionError, RecursionError, TypeError, ValueError) as exc:
        # A failed certificate check is an operational integrity error. Preserve
        # the kernel's closure for diagnosis, but make it dominate any semantic
        # answer in downstream reporting.
        from dataclasses import replace
        join.python_report = replace(report, operational_failure="certificate-invalid",
                                     message=type(exc).__name__)
    return join


# ---------------------------------------------------------------------------
# Reading the closure


def blocking_premise(relations, run: str) -> dict[str, Any] | None:
    """The first premise of observations_agree that blocks ``run``, or ``None``.

    Mirrors the replay judge: a positive premise that is absent blocks, and so
    does an "any" relation that holds.  Everything here is keyed on the run in
    its first column -- there is no op and no index in this pack.
    """
    rows = dict(relations) if not isinstance(relations, dict) else relations
    if any(r[0] == run for r in rows.get("observations_agree", ())):
        return None

    def holds(name: str) -> bool:
        return any(r and r[0] == run for r in rows.get(name, ()))

    for name, is_blocker in _BLOCKING_ORDER:
        present = holds(name)
        if is_blocker and present:
            return {"relation": name, "holds": True, "reason": REASONS.get(name, "")}
        if not is_blocker and not present:
            return {"relation": name, "holds": False, "reason": REASONS.get(name, "")}
    return {"relation": "observations_agree", "holds": False, "reason": ""}


def qualification(semantic: str | None, operational: str | None,
                  blocking: dict[str, Any] | None = None) -> str:
    """One word for what happened to ``observations_agree``.

    ``"agreeing"`` when the claim is supported and complete, ``"unsupported"``
    otherwise.  There is no third word: ``PENDING_PREMISES`` is empty, because
    Lane A has no premise that is merely not-built-yet, and ``"qualified"`` is
    reserved for ``op_qualified``, which claims more than this one does.
    """
    if semantic == "supported" and operational == "complete":
        return QUALIFICATION_AGREEING
    return QUALIFICATION_UNSUPPORTED


def scenario_histogram(receipt: dict[str, Any]) -> dict[str, int]:
    """How many scenarios of each kind the admitted set declares.  Always printed:
    a one-scenario suite derives the claim as loudly as a twelve-scenario one."""
    out: dict[str, int] = {}
    for scenario in receipt.get("scenario_set", {}).get("scenarios", []):
        out[scenario["kind"]] = out.get(scenario["kind"], 0) + 1
    return dict(sorted(out.items()))


def diagnostics(relations, run: str) -> list[dict[str, Any]]:
    """Every diagnostic row the closure carries, with the status the judge reports it as.

    Statuses come from ``DIAGNOSTIC_STATUS`` and are all from the validator's
    allowed set.  ``scenario_setup_failed`` is ``unresolved`` under its own
    predicate so a harness that could not start never reads as a finding
    against the candidate.
    """
    rows = dict(relations) if not isinstance(relations, dict) else relations
    out = []
    for relation, status in sorted(DIAGNOSTIC_STATUS.items()):
        found = sorted((list(r[1:]) for r in rows.get(relation, ()) if r and r[0] == run),
                       key=canonical_json)
        if found:
            out.append({"relation": relation, "status": status, "rows": found})
    return out


def normalization_note(relations, run: str) -> str | None:
    """"N of these agreements depended on a normalization: ..." -- or nothing fired."""
    rows = dict(relations) if not isinstance(relations, dict) else relations
    firings = sorted({(r[1], r[3], r[4]) for r in rows.get("observation_normalization", ())
                      if r and r[0] == run})
    if not firings:
        return None
    named = ", ".join(f"{scenario}/{facet}/{name}" for scenario, facet, name in firings)
    return (f"{len(firings)} of these agreements depended on a normalization: {named}.")


def unassessed_rows(relations, run: str) -> list[dict[str, str]]:
    """Every ``unassessed`` row, verbatim, reprinted with the verdict."""
    rows = dict(relations) if not isinstance(relations, dict) else relations
    return sorted(({"dimension": r[1], "reason": r[2]}
                   for r in rows.get("observation_unassessed", ()) if r and r[0] == run),
                  key=lambda entry: entry["dimension"])


def receipt_unassessed(receipt: dict[str, Any]) -> list[dict[str, str]]:
    """The receipt's OWN ``unassessed`` block, read defensively.

    Used only where there is no closure to read the rows off -- a refused export
    or a join that was never evaluated.  These rows are the receipt's unchecked
    word about itself, which is why ``summary`` labels them
    ``unassessed_source: "receipt"`` rather than passing them off as closure
    rows.  They are printed anyway: a refusal is exactly the moment a reader
    most needs to know what the run never assessed, and a disclosure that is
    dropped whenever something goes wrong is not a disclosure.
    """
    block = receipt.get("unassessed")
    if not isinstance(block, list):
        return []
    rows = []
    for entry in block:
        if not isinstance(entry, dict):
            continue
        dimension, reason = entry.get("dimension"), entry.get("reason")
        if isinstance(dimension, str) and isinstance(reason, str):
            rows.append({"dimension": dimension, "reason": reason})
    return sorted(rows, key=lambda entry: entry["dimension"])


def exporter_messages(join: ObservationJoin) -> list[str]:
    """Every message the exporter produced, with the local receipt path redacted.

    NOTHING is dropped: each open completeness box and its reason, the absent
    admissions ledger, the absent log, the missing repeat block and the identity
    notice all travel into the judge's output.  The only edit is the receipt
    directory's local path, replaced by ``<receipt-dir>`` -- a redaction, not an
    omission, so no artifact carries a path and no disclosure is lost to keep it
    from doing so.
    """
    directory = str(join.receipt_dir)
    return [message.replace(directory, "<receipt-dir>") for message in join.exported.messages]


def masked_differences(relations, run: str) -> list[dict[str, str]]:
    """Exported, never deleted: every difference a normalization hid."""
    rows = dict(relations) if not isinstance(relations, dict) else relations
    return sorted(({"scenario": r[1], "facet": r[2], "normalization": r[3], "field_path": r[4]}
                   for r in rows.get("observation_masked_difference", ()) if r and r[0] == run),
                  key=canonical_json)


def _explanation_summary(explanation: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in explanation.items() if key != "certificate"}


def summary(join: ObservationJoin) -> dict[str, Any]:
    """The judge's report.  It prints the same disclosures supported or not."""
    receipt_set = join.receipt.get("scenario_set", {})
    if join.bundle is None:
        # A refused receipt still discloses.  The exporter's messages and the
        # receipt's own unassessed rows are the only record of what was never
        # assessed, and the refusal is where a reader needs them most.
        return {"status": "blocked", "run": join.run, "receipt": join.receipt_dir.name,
                "contract_findings": list(join.contract_findings),
                "exporter_status": join.exported.status,
                "exporter_messages": exporter_messages(join),
                "unassessed": receipt_unassessed(join.receipt),
                "unassessed_source": "receipt",
                "not_a_claim": NOT_A_CLAIM}
    relations = dict(join.report().relations) if join.report() is not None else {}
    # 5.6 / C6: the unassessed rows print from the closure where there is one and
    # from the receipt where there is not, with which of the two said so named.
    closure_unassessed = unassessed_rows(relations, join.run) if relations else []
    unassessed = closure_unassessed or receipt_unassessed(join.receipt)
    agree = join.verdict(join.agree_claim) or {}
    stable = join.verdict(join.stable_claim) or {}
    blocking = blocking_premise(relations, join.run) if relations else None
    if join.python_report is not None:
        status = ("operational-failure" if join.python_report.operational_failure
                  else "complete")
    elif join.result is None and join.mismatch is None:
        status = "not-evaluated"
    elif join.result is not None and join.result.matched:
        status = "complete"
    else:
        status = "kernel-mismatch"
    out: dict[str, Any] = {
        "status": status,
        "run": join.run,
        "check": join.check,
        "scenario_set": join.scenario_set,
        "policy": join.policy,
        "fixture": join.fixture,
        "source_digest": join.source_digest,
        "source_observation": (join.source_observation.method
                               if join.source_observation else "absent"),
        "source_observation_detail": (join.source_observation.detail
                                      if join.source_observation else ""),
        "observation_identity": dict(join.exported.bundle.metadata)["observation_digest"],
        "observation_bundle_digest": observation_facts.bundle_digest(join.exported.bundle),
        "combined_bundle_digest": observation_facts.bundle_digest(join.bundle),
        "kernels": (["python"] if join.python_report is not None else
                    ["python", "souffle"] + (["souffle-compiled"] if join.kernels == "three" else [])),
        "contract_findings": list(join.contract_findings),
        "assumption_ids": list(join.assumption_ids),
        # 5.6: always printed, supported or not.
        "scenario_count": receipt_set.get("count", 0),
        "scenario_kinds": scenario_histogram(join.receipt),
        "recomputed_notice": RECOMPUTED_NOTICE,
        "not_a_claim": NOT_A_CLAIM,
        "exporter_status": join.exported.status,
        # Always carried: an open completeness box, an absent ledger or an absent
        # log is a disclosure, and one that lives only in an input nobody prints
        # is no disclosure at all.
        "exporter_messages": exporter_messages(join),
        "unassessed": unassessed,
        "unassessed_source": "closure" if closure_unassessed else "receipt",
        "masked_differences": masked_differences(relations, join.run) if relations else [],
        "diagnostics": diagnostics(relations, join.run) if relations else [],
        "observations_agree": agree.get("semantic"),
        "operational": agree.get("operational"),
        "qualification": qualification(agree.get("semantic"), agree.get("operational"), blocking),
        "blocking_premise": blocking,
        "incumbent_stable": stable.get("semantic") or "unresolved",
        "stability_rows": sorted([list(r[1:]) for r in relations.get("observation_stability", ())
                                  if r and r[0] == join.run], key=canonical_json),
    }
    if join.python_report is not None and join.python_report.operational_failure:
        out["operational_failure"] = join.python_report.operational_failure
    note = normalization_note(relations, join.run) if relations else None
    if note:
        out["normalization_note"] = note
    if "repeat" not in join.receipt or join.receipt.get("repeat") is None:
        out["stability_notice"] = (
            "the receipt carries no repeat block, so incumbent_stable is unresolved and "
            "oracle_stability is a mandatory unassessed row")
    target = (join.run, join.check, join.scenario_set, join.policy)
    if agree.get("semantic") == "supported":
        out["statement"] = SUPPORTED_STATEMENT.format(
            run=join.run, fixture=join.fixture[:12], source=join.source_digest[:12],
            count=receipt_set.get("count", 0), scenario_set=join.scenario_set[:12],
            policy=join.policy[:12])
        explanation = why(join.bundle, relations, "observations_agree", target)
        explanation["shared_assumptions"] = list(
            shared_assumptions(join.bundle, explanation["certificate"]))
    elif relations:
        explanation = why_not(join.bundle, relations, "observations_agree", target)
    else:
        explanation = {}
    if explanation:
        out["explanation"] = _explanation_summary(explanation)
    return out


def assumption_registry(join: ObservationJoin, *, strict_impact: bool = True) -> dict[str, Any]:
    """The A2 registry of the join's combined bundle (contract: ``assumptions.json``)."""
    if join.bundle is None:
        return {"registry_version": assumptions.REGISTRY_VERSION, "run": join.run,
                "combined_bundle_digest": None, "assumptions": [], "shared_assumptions": [],
                "unreferenced": []}
    report = join.report()
    return assumptions.registry(join.bundle, join.row_certificates, run=join.run,
                                relations=report.relations if report is not None else None)


def invalidate(join: ObservationJoin, identifier: str, replay_root: str) -> assumptions.Invalidation:
    """Withdraw one registered assumption and record what every claim did."""
    if join.bundle is None or join.result is None:
        raise assumptions.InvalidationError(
            "the join must be evaluated before an assumption is withdrawn")
    result = assumptions.invalidate(
        join.bundle, identifier, replay_root=replay_root, baseline_result=join.result,
        baseline_row_certificates=join.row_certificates)
    join.invalidations[result.assumption_id] = result
    return result


def _kernel_digests(join: ObservationJoin) -> dict[str, Any]:
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


def write_artifacts(join: ObservationJoin, out_dir: Path) -> dict[str, Any]:
    """Digests, counts and verdicts only; no source text and no local paths."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    document = {
        "pilot": "observation receipt join (Lane A, judge side)",
        "receipt": {"run": join.run, "check": join.check, "contract": join.receipt.get("contract"),
                    "scenario_set": join.scenario_set, "policy": join.policy,
                    "fixture": join.fixture, "source_digest": join.source_digest,
                    "terminal": join.receipt.get("terminal", {}).get("outcome"),
                    "agreement": join.receipt.get("agreement")},
        # The exporter's messages travel whole; only the local receipt path is
        # redacted (``exporter_messages``).  Dropping the messages that named a
        # path, as this once did, dropped disclosures to avoid one.
        "export": {"status": join.exported.status, "row_counts": dict(join.exported.counts),
                   "messages": exporter_messages(join)},
        "contract_findings": list(join.contract_findings),
        "join": summary(join),
        "kernels": None if join.result is None and join.mismatch is None
                   else _kernel_digests(join),
        "compiled": join.checker.provenance() if join.checker is not None else None,
        "certificates": {claim_id: {"sha256": hashlib.sha256(
                                        canonical_json(cert).encode()).hexdigest(),
                                    "leaves": len(cert["leaves"]), "nodes": cert["nodes"],
                                    "truncated": cert["truncated"]}
                         for claim_id, cert in join.certificates.items()},
    }
    registry = assumption_registry(join, strict_impact=False)
    _write(out_dir / "assumptions.json", {
        "registry_version": assumptions.REGISTRY_VERSION, "run": join.run,
        "combined_bundle_digest": (observation_facts.bundle_digest(join.bundle)
                                   if join.bundle is not None else None),
        "assumptions": registry.get("assumptions", []),
        "shared_assumptions": registry.get("shared_assumptions", []),
        "unreferenced": sorted({entry["assumption_id"]
                                for entry in registry.get("assumptions", [])
                                if not entry["carried_by"]})})
    document["assumptions"] = {"registry": "assumptions.json"}
    for claim_id, certs in join.row_certificates.items():
        for position, cert in enumerate(certs):
            name = (f"certificate-{claim_id}.json" if position == 0
                    else f"certificate-{claim_id}-{position}.json")
            _write(out_dir / name, cert)
    _write(out_dir / "receipt.json", document)
    return document


__all__ = ["REVIEWER_SOURCE", "FIXTURE_SOURCE", "REASONS", "DIAGNOSTIC_STATUS",
           "CLOSURE_STATUS", "RECOMPUTED_NOTICE", "SUPPORTED_STATEMENT", "NOT_A_CLAIM",
           "PENDING_PREMISES", "QUALIFICATION_AGREEING", "QUALIFICATION_UNSUPPORTED",
           "SourceObservation", "observe_source", "ObservationJoin", "build", "evaluate_join",
           "qualification", "blocking_premise", "summary", "write_artifacts",
           "scenario_histogram", "diagnostics", "normalization_note", "unassessed_rows",
           "receipt_unassessed", "exporter_messages",
           "masked_differences", "assumption_registry", "invalidate"]
