"""Assumption registry and invalidation across runs (engine-independent).

Assumption-kind evidence (``Evidence.kind == "assumption"``) carries ids that
embed the run they were exported under -- a reviewer's ``model_scope_exclusion``
row is ``reviewer:<replay12>:model_scope_exclusion:<row12>`` -- so a registry
keyed by evidence id cannot say that two runs rest on *the same* reviewed
assumption.  This module gives every assumption record a run-independent
``assumption_id`` (contract A1) and answers two questions over one evaluated
bundle:

* ``registry`` -- for each assumption, which claim rows it *carries* (its
  evidence id is a leaf of that row's certificate), which other assumptions it
  shares a heuristic with, and which claims a static ``ground.impact`` predicts
  would fall without it;
* ``invalidate`` -- what actually happens when one assumption is withdrawn:
  the closed revocation of its evidence is dropped from the *combined* bundle,
  both kernels re-evaluate the result, every claim row is re-certified and the
  per-claim verdicts are diffed against the baseline.

Nothing here reads a receipt, changes an exporter contract, or edits a
reviewer's file.  A withdrawal is a question asked of an existing bundle.

WHOLE ASSUMPTIONS.  A withdrawal is asked of an *assumption*, not of a
record.  One assumption may be attested by several records -- two reviewers
signing the identical row land on the identical ``assumption_id`` -- so
``invalidate`` expands whatever it was given to every record attesting the
target before it withdraws.  Dropping one attestation and reporting the result
under an assumption id would say "nothing rests on this assumption" while the
fact it attests is still in force.

SOUNDINESS.  A dropped assumption is a *missing premise*, never a refutation:
no claim may *become* ``refuted`` or ``conflicting`` because of a withdrawal,
and one that does raises ``InvalidationError`` as a finding instead of being
written to a document.  A claim already outside ``supported`` / ``unresolved``
at the baseline and left exactly as it was is not a transition and is reported
unchanged: the operation judges what the withdrawal did, not what it found.
Both directions between ``supported`` and ``unresolved`` are legitimate and
observed: withdrawing a scope exclusion moves ``op_qualified`` from supported
to unresolved *and* moves the open ``undeclared_write`` claim from unresolved
to supported, because the table the exclusion used to cover is now an
undeclared write.  The document reports the two directions separately --
``flipped`` is what the withdrawal cost, ``gained`` is what it revealed --
because a headline claim appearing in ``gained`` is a finding to read, not a
success.

PREDICTION.  ``predicted_fallen`` is a ``ground.impact`` query -- which
certified claim rows rest on the withdrawn leaf -- and it is *reported beside*
the re-evaluation, never substituted for it, because it is wrong in both
directions:

* it over-reports, because ``impact`` records one canonical support path per
  row and a claim with several rows survives on the rows that did not fall
  (withdrawing one scope exclusion drops one ``exclusion_applied`` row of
  four, so the claim is predicted fallen and stays supported);
* it under-reports, because a certificate's leaves are its *positive* support.
  An assumption that guards a claim through a negated atom is not a leaf of
  it: the reviewer's scope exclusions keep a table out of ``undeclared_write``,
  and ``op_qualified`` rests on ``undeclared_any`` being *absent*, so no leaf
  of ``op_qualified`` names an exclusion even though withdrawing one
  unresolves the claim.

Under-reporting is not a fixable bound.  Whether withdrawing evidence makes an
absent atom present is not answerable from a monotone support path; it is
answerable only by re-evaluating, which is what this operation does.
``prediction_agrees`` therefore records whether the static prediction happened
to match the re-evaluation, and a ``False`` is a fact about the shape of the
support, not a failure.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Mapping, Sequence

from .ir import Bundle, Evidence, canonical_json
from .differential import compare
from .replay.replay_facts import bundle_digest, row_digest
from .static.certificate import (DEFAULT_MAX_DEPTH, DEFAULT_MAX_NODES, certify,
                                 claim_conclusions, recheck)
from .static.ground import closed_revocation, impact, shared_assumptions
from .validation import assert_valid

REGISTRY_VERSION = "capcov-assumption-registry-v1"
INVALIDATION_VERSION = "capcov-assumption-invalidation-v1"
ASSUMPTION_PREFIX = "asm:"
ASSUMPTION_KIND = "assumption"

#: The only semantic verdicts a claim may *move into* when an assumption is
#: dropped.  Both directions inside this set are admitted -- including
#: ``unresolved -> supported``, which the first reviewed case exercises (the
#: open ``undeclared_write`` claim picks up the table the withdrawn exclusion
#: covered).  That is wider than "unchanged, or supported -> unresolved", and
#: deliberately so: a negation-revealed row is the honest consequence of the
#: drop.  The widening is not free, so a claim that gains support is listed
#: separately in ``Invalidation.gained`` rather than being absorbed silently.
ALLOWED_AFTER = frozenset({"supported", "unresolved"})

_REVIEW_MODEL = re.compile(r"\bmodel:([0-9a-f]{6,64})\b")
_REVIEW_RUN = re.compile(r"\brun:(\S+)")
_REVIEW_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


class InvalidationError(RuntimeError):
    """A registry or invalidation request that must not be answered with a document."""


# ---------------------------------------------------------------------------
# A1: the stable id


def producer_class(source: str) -> str:
    """The first whitespace token of ``Evidence.source`` (``reviewer``, ``php-census``, ...)."""
    token = source.split(None, 1)[0] if source and source.split() else ""
    if not token:
        raise InvalidationError("an assumption record needs a source naming its producer class")
    return token


def evidence_row(record: Evidence) -> list[Any]:
    """The atom's term values in declared column order."""
    return [term.value for term in record.atom.terms]


def content_digest(record: Evidence) -> str:
    """``replay_facts.row_digest(relation, row)`` -- the row's content, without its producer."""
    return row_digest(record.atom.relation, evidence_row(record))


def assumption_id_of(producer: str, relation: str, row: Iterable[Any]) -> str:
    """``"asm:" + sha256(canonical_json([producer_class, relation, row]))`` as lowercase 64-hex.

    Deliberately excludes the run/replay digest that scopes an evidence id and
    every detail of the source beyond its producer class (reviewer name, review
    date, run suffix), so one reviewed row against one model digest keeps one
    id across runs.  It is not an evidence id and never replaces one.
    """
    payload = canonical_json([producer, relation, list(row)])
    return ASSUMPTION_PREFIX + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def assumption_id(record: Evidence) -> str:
    """Contract A1 over an assumption-kind evidence record."""
    return assumption_id_of(producer_class(record.source), record.atom.relation, evidence_row(record))


def assumption_records(bundle: Bundle) -> list[Evidence]:
    """Every assumption-kind evidence record, in bundle order."""
    return [record for record in bundle.evidence if record.kind == ASSUMPTION_KIND]


def resolve(bundle: Bundle, identifier: str) -> list[Evidence]:
    """The assumption records named by an ``asm:`` id or by an evidence id.

    An evidence id that is not an assumption, and an id naming nothing, are
    refusals: a registry may only be asked about what it registers.
    """
    records = assumption_records(bundle)
    if identifier.startswith(ASSUMPTION_PREFIX):
        matched = [record for record in records if assumption_id(record) == identifier]
        if not matched:
            raise InvalidationError(f"{identifier} is not an assumption of this bundle")
        return matched
    matched = [record for record in records if record.id == identifier]
    if not matched:
        known = any(record.id == identifier for record in bundle.evidence)
        raise InvalidationError(f"{identifier} is "
                                + ("not an assumption of this bundle" if known
                                   else "not an evidence id of this bundle"))
    return matched


def reviewed_against(record: Evidence) -> dict[str, Any] | None:
    """``{model, run, reviewed_at, reviewer}`` parsed from a reviewed source suffix.

    ``None`` for a claim-time assumption, whose source names no review.

    EXPORTER-BOUND.  Only ``reviewer`` and ``reviewed_at`` come from what the
    reviewer wrote.  The ``model:``/``run:`` suffix is stamped into the source
    by the exporter at export time out of the receipt being judged, and the
    receipt reader accepts the whole producer string verbatim once it ends in
    that shape, so ``run`` is *this run's* provenance rather than something the
    reviewer signed.  Read it as "which run bound this record", never as
    "which run the reviewer reviewed".
    """
    source = record.source or ""
    model = _REVIEW_MODEL.search(source)
    run = _REVIEW_RUN.search(source)
    if not model or not run:
        return None
    date = _REVIEW_DATE.search(source)
    head = source[: date.start()] if date else source
    reviewer = " ".join(head.split()[1:]) or None
    return {"model": model.group(1), "run": run.group(1),
            "reviewed_at": date.group(1) if date else None, "reviewer": reviewer}


# ---------------------------------------------------------------------------
# certificates


def certificate_sha256(certificate: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(certificate).encode("utf-8")).hexdigest()


def conclusion_of(certificate: Mapping[str, Any]) -> tuple[str, tuple[Any, ...]]:
    """The ``(relation, row)`` a certificate concludes."""
    conclusion = certificate["conclusion"]
    return conclusion["relation"], tuple(conclusion["row"])


def certify_claims(bundle: Bundle, result: Any, *,
                   max_depth: int = DEFAULT_MAX_DEPTH,
                   max_nodes: int = DEFAULT_MAX_NODES) -> tuple[dict[str, dict[str, Any]],
                                                                dict[str, list[dict[str, Any]]]]:
    """Certify every claim row of ``bundle`` from every closure ``result`` carries.

    Returns ``(certificates, row_certificates)``: the first row's certificate
    per claim id, and every row's certificate per claim id (an open claim such
    as ``exclusion_applied`` has one per table).  The closures must agree on
    the claim rows and on each certificate, and each certificate must recheck
    against each closure; otherwise the disagreement is raised rather than
    reported, because a judge with two answers has none.

    ``result`` may be one selected ``KernelReport``, carry two differential
    closures, or carry three (plus compiled Souffle). Every closure present is
    checked; a single-kernel certificate records exactly that selected engine.
    """
    closures = _closures(result)
    certificates: dict[str, dict[str, Any]] = {}
    row_certificates: dict[str, list[dict[str, Any]]] = {}
    for claim in bundle.claims:
        first, first_backend = closures[0]
        rows = claim_conclusions(bundle, first, claim)
        for other, backend in closures[1:]:
            if rows != claim_conclusions(bundle, other, claim):
                raise AssertionError(f"{claim.id}: the kernels disagree on the claim rows ({backend})")
        for row in rows:
            certificate = certify(bundle, first, claim.relation, row,
                                  max_depth=max_depth, max_nodes=max_nodes)
            for other, backend in closures[1:]:
                if certify(bundle, other, claim.relation, row,
                           max_depth=max_depth, max_nodes=max_nodes) != certificate:
                    raise AssertionError(f"{claim.id}: certificates differ between closures ({backend})")
            for closure, backend in closures:
                checked = recheck(bundle, certificate, closure)
                if not checked.ok:
                    raise AssertionError(f"{claim.id}: recheck failed against {backend}: {checked}")
            certificates.setdefault(claim.id, certificate)
            row_certificates.setdefault(claim.id, []).append(certificate)
    return certificates, row_certificates


def _closures(result: Any) -> list[tuple[Any, str]]:
    """Every selected or agreeing kernel's relations and backend name.

    A three-way result carries ``compiled``; a two-way one does not, and a
    hand-built stand-in need not name its backends.
    """
    if hasattr(result, "relations") and hasattr(result, "backend"):
        return [(result.relations, result.backend)]
    reports = [result.python, result.souffle]
    compiled = getattr(result, "compiled", None)
    if compiled is not None:
        reports.append(compiled)
    names = ("python", "souffle", "souffle-compiled")
    return [(report.relations, getattr(report, "backend", None) or names[position])
            for position, report in enumerate(reports)]


def shared_across(bundle: Bundle, certificates: Sequence[Mapping[str, Any]]) -> list[str]:
    """The assumption ids every one of ``certificates`` rests on, sorted.

    ``ground.shared_assumptions`` per certificate, intersected, then mapped
    from evidence ids to run-independent ids.
    """
    if not certificates:
        return []
    ids = {record.id: assumption_id(record) for record in assumption_records(bundle)}
    common: set[str] | None = None
    for certificate in certificates:
        leaves = set(shared_assumptions(bundle, certificate))
        common = leaves if common is None else (common & leaves)
    return sorted({ids[leaf] for leaf in (common or set()) if leaf in ids})


# ---------------------------------------------------------------------------
# A2: the registry


def _conclusions(row_certificates: Mapping[str, Sequence[Mapping[str, Any]]]
                 ) -> tuple[list[tuple[str, tuple[Any, ...]]], dict[tuple[str, str], list[str]]]:
    """The certified ``(relation, row)`` conclusions and their claim ids."""
    conclusions: list[tuple[str, tuple[Any, ...]]] = []
    owners: dict[tuple[str, str], list[str]] = {}
    for claim_id, certificates in row_certificates.items():
        for certificate in certificates:
            relation, row = conclusion_of(certificate)
            conclusions.append((relation, row))
            owners.setdefault((relation, canonical_json(list(row))), []).append(claim_id)
    return conclusions, owners


def registry(bundle: Bundle, row_certificates: Mapping[str, Sequence[Mapping[str, Any]]], *,
             run: str | None = None, relations: Any = None,
             strict_impact: bool = True,
             max_depth: int = DEFAULT_MAX_DEPTH,
             max_nodes: int = DEFAULT_MAX_NODES) -> dict[str, Any]:
    """Contract A2 over an evaluated bundle and the certificates of its claim rows.

    ``row_certificates`` is what ``certify_claims`` returns, so ``carried_by``
    extends to every per-row certificate the caller holds, not just the
    headline claim.  ``relations`` is a kernel closure; with it each entry's
    ``impact`` is a ``ground.impact`` prediction over the certified claim rows,
    without it ``impact`` is ``None``.

    A truncated impact query is a refusal under ``strict_impact`` (the default,
    for a caller that asked for this document): a partial ``fallen`` list read
    as a whole one under-reports.  ``strict_impact=False`` degrades that one
    entry to ``{"truncated": true, "fallen": null, "survived": null}`` instead,
    for a caller -- the join's ``summary`` -- that embeds the registry in a
    larger document and must not fail because a bundle grew.
    """
    records = assumption_records(bundle)
    ids = {record.id: assumption_id(record) for record in records}
    conclusions, owners = _conclusions(row_certificates)

    carried: dict[str, list[dict[str, Any]]] = {record.id: [] for record in records}
    for claim_id, certificates in row_certificates.items():
        for certificate in certificates:
            digest = certificate_sha256(certificate)
            relation, row = conclusion_of(certificate)
            for leaf in certificate.get("leaves") or ():
                if leaf in carried:
                    carried[leaf].append({"claim_id": claim_id, "relation": relation,
                                          "row": list(row), "certificate_sha256": digest})

    entries: list[dict[str, Any]] = []
    for record in records:
        dependants = sorted(closed_revocation(bundle, [record.id]) - {record.id})
        prediction: dict[str, Any] | None = None
        if relations is not None:
            predicted = impact(bundle, relations, [record.id], conclusions,
                               max_depth=max_depth, max_nodes=max_nodes)
            if predicted["truncated"]:
                if strict_impact:
                    raise InvalidationError(
                        f"{record.id}: the impact query was truncated; raise max_depth/max_nodes")
                prediction = {"truncated": True, "fallen": None, "survived": None}
            else:
                fallen = _claims_of(predicted["fallen"], owners)
                # a claim with several rows can have one row fall and another survive;
                # it counts as fallen, so the two lists stay a partition of the claims
                prediction = {"truncated": False, "fallen": sorted(fallen),
                              "survived": sorted(_claims_of(predicted["survived"], owners) - fallen)}
        entries.append({
            "assumption_id": ids[record.id], "evidence_id": record.id,
            "producer_class": producer_class(record.source), "source": record.source,
            "relation": record.atom.relation, "row": evidence_row(record),
            "content_digest": content_digest(record), "kind": record.kind,
            "depends_on": list(record.depends_on), "reviewed_against": reviewed_against(record),
            "carried_by": sorted(carried[record.id], key=canonical_json),
            "dependants": dependants, "impact": prediction,
        })
    entries.sort(key=lambda entry: (entry["assumption_id"], entry["evidence_id"]))
    return {"registry_version": REGISTRY_VERSION, "run": run,
            "combined_bundle_digest": bundle_digest(bundle),
            "assumptions": entries,
            "shared_assumptions": _shared_groups(entries),
            "unreferenced": sorted({entry["assumption_id"] for entry in entries if not entry["carried_by"]})}


def _claims_of(rows: Iterable[Mapping[str, Any]], owners: Mapping[tuple[str, str], list[str]]) -> set[str]:
    out: set[str] = set()
    for entry in rows:
        out.update(owners.get((entry["relation"], canonical_json(list(entry["row"]))), ()))
    return out


def _shared_groups(entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Assumptions grouped by the heuristic they rest on.

    The heuristic is an ``external:`` dependency the producers share -- a
    census index, a model digest -- so two *different* producer classes that
    lean on one artefact are visible as one group.  An assumption with no
    external dependency is its own heuristic.
    """
    groups: dict[str, dict[str, set[str]]] = {}
    for entry in entries:
        keys = [dep for dep in entry["depends_on"] if dep.startswith("external:")] or [entry["assumption_id"]]
        for key in keys:
            group = groups.setdefault(key, {"assumption_ids": set(), "producer_classes": set(), "claims": set()})
            group["assumption_ids"].add(entry["assumption_id"])
            group["producer_classes"].add(entry["producer_class"])
            group["claims"].update(item["claim_id"] for item in entry["carried_by"])
    return [{"heuristic": key, "assumption_ids": sorted(group["assumption_ids"]),
             "producer_classes": sorted(group["producer_classes"]), "claims": sorted(group["claims"])}
            for key, group in sorted(groups.items())]


# ---------------------------------------------------------------------------
# withdrawal


def withdraw(bundle: Bundle, ids: Iterable[str]) -> Bundle:
    """``bundle`` minus the closed revocation of ``ids`` -- the reduced bundle, re-validated.

    Drops those evidence records, every record that transitively depends on
    them, and the atoms no remaining record attests; a fact another producer
    still attests stays.  Rules, relation declarations, claims, diagnostics and
    metadata are untouched, and the reduction goes through ``replace`` rather
    than a re-``combine`` so the combined bundle's digest stays comparable with
    the baseline's.

    A withdrawn id named by a reviewed ``OutputTemplate`` is refused rather
    than repaired: rewriting a reviewer's template to keep a document
    renderable would silently change what was reviewed.
    """
    requested = list(ids)
    known = {record.id for record in bundle.evidence}
    unknown = sorted(set(requested) - known)
    if unknown:
        raise InvalidationError(f"cannot withdraw {unknown[0]}: not an evidence id of this bundle")
    dropped = closed_revocation(bundle, requested)
    for output in bundle.outputs:
        named = (set(output.requires_all_evidence) | set(output.requires_any_evidence)
                 | set(output.excludes_evidence)) & dropped
        if named:
            raise InvalidationError(
                f"the output template for {output.claim_id} names withdrawn evidence "
                f"{sorted(named)[0]}; a reviewed template is never repaired")
    remaining = tuple(record for record in bundle.evidence if record.id not in dropped)
    attested = {record.atom for record in remaining}
    orphaned = {record.atom for record in bundle.evidence if record.id in dropped} - attested
    reduced = replace(bundle,
                      facts=tuple(fact for fact in bundle.facts if fact not in orphaned),
                      evidence=remaining)
    return assert_valid(reduced)


# ---------------------------------------------------------------------------
# A3: invalidation


@dataclass
class Invalidation:
    """What one withdrawal did to every claim of a bundle."""
    assumption_id: str
    withdrawn: list[str]
    baseline_bundle_digest: str
    withdrawn_bundle_digest: str
    kernels: dict[str, Any]
    claims: dict[str, dict[str, Any]]
    flipped: list[str]
    """Claims that stopped being supported -- what the withdrawal cost."""
    predicted_fallen: list[str]
    """``ground.impact``'s static guess at ``flipped`` (see the module docstring)."""
    prediction_agrees: bool
    gained: list[str] = field(default_factory=list)
    """Claims that *became* supported -- what the withdrawal revealed.

    Sound for an open claim, whose rows a withdrawal can legitimately expose
    (dropping a scope exclusion makes the table it covered an undeclared
    write).  A closed, headline claim here is a finding to read: support that
    arrives when a premise leaves is support that was being suppressed.
    """
    bundle: Bundle | None = None
    relations: Any = None
    """The post-withdrawal Python closure, so a caller can assert on the rows
    that survived the drop (the A3 document carries verdicts, not rows)."""
    certificates: dict[str, dict[str, Any]] = field(default_factory=dict)
    row_certificates: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    @property
    def short_id(self) -> str:
        """The 12 hex characters that name this invalidation's artifacts."""
        return self.assumption_id[len(ASSUMPTION_PREFIX):][:12]

    def as_dict(self) -> dict[str, Any]:
        """The A3 document: verdicts and digests only, no bundle and no local path."""
        return {"invalidation_version": INVALIDATION_VERSION, "assumption_id": self.assumption_id,
                "withdrawn": list(self.withdrawn),
                "baseline_bundle_digest": self.baseline_bundle_digest,
                "withdrawn_bundle_digest": self.withdrawn_bundle_digest,
                "kernels": dict(self.kernels), "claims": {k: dict(v) for k, v in self.claims.items()},
                "flipped": list(self.flipped), "gained": list(self.gained),
                "predicted_fallen": list(self.predicted_fallen),
                "prediction_agrees": self.prediction_agrees}


def _verdicts(report: Any) -> dict[str, dict[str, Any]]:
    return {claim.key: {"semantic": claim.semantic, "operational": claim.operational,
                        "missing_premises": list(claim.missing_premises)}
            for claim in report.claims}


def missing_relations(missing_premises: Iterable[str]) -> list[str]:
    """The relations of the template-rendered absent leaves.

    The evaluator's ``claim:<relation>:<context>`` fallback is not a relation
    name and is deliberately not reported here, matching the join's summary.
    """
    out = []
    for item in missing_premises:
        if isinstance(item, str) and item.startswith("{"):
            out.append(json.loads(item)["relation"])
    return sorted(set(out))


def invalidate(bundle: Bundle, ids: str | Iterable[str], *, replay_root: str,
               baseline_result: Any, baseline_row_certificates: Mapping[str, Sequence[Mapping[str, Any]]],
               explain: Callable[[str, Any], Mapping[str, Any]] | None = None,
               max_depth: int = DEFAULT_MAX_DEPTH,
               max_nodes: int = DEFAULT_MAX_NODES) -> Invalidation:
    """Contract A3: withdraw one assumption and report what every claim did.

    ``ids`` is one ``asm:`` id or evidence id (or several ids of the *same*
    assumption); whichever is given, *every* record attesting that assumption
    is withdrawn, because the document is signed with an assumption id and must
    mean what it says.  ``baseline_result`` / ``baseline_row_certificates`` are
    the evaluated baseline, so the before-verdicts and the ``ground.impact``
    prediction are read, never recomputed with different bounds.

    ``explain(claim_id, relations_after)`` may contribute pack-specific keys
    (``blocking_premise``, ``undeclared_tables``) to a claim entry; this module
    stays independent of any one rule pack.  A ``DifferentialMismatch`` from
    the withdrawn bundle propagates: the caller reports it as a kernel
    disagreement, not as an answer.
    """
    requested = [ids] if isinstance(ids, str) else list(ids)
    if not requested:
        raise InvalidationError("invalidate needs an assumption to withdraw")
    records = [record for identifier in requested for record in resolve(bundle, identifier)]
    targets = {assumption_id(record) for record in records}
    if len(targets) != 1:
        raise InvalidationError(f"one invalidation withdraws one assumption, got {sorted(targets)}")
    target = targets.pop()
    # expand to every attestation of the target: an evidence id names one
    # record, but this document is published under an assumption id, and an
    # assumption a second reviewer also signed is not withdrawn until both
    # records go -- otherwise the fact stands, no claim moves, and the document
    # reads "flipped: []" against an assumption that is still in force.
    records = [record for record in assumption_records(bundle) if assumption_id(record) == target]
    evidence_ids = sorted({record.id for record in records})
    withdrawn = sorted(closed_revocation(bundle, evidence_ids))

    reduced = withdraw(bundle, evidence_ids)
    result = compare(reduced, replay_root=replay_root)
    certificates, row_certificates = certify_claims(reduced, result, max_depth=max_depth, max_nodes=max_nodes)

    before = _verdicts(baseline_result.python)
    after = _verdicts(result.python)
    relations_after = result.python.relations
    claims: dict[str, dict[str, Any]] = {}
    lost: list[str] = []
    gained: list[str] = []
    for claim_id in sorted(set(before) | set(after)):
        was, now = before.get(claim_id), after.get(claim_id)
        # only a *transition* is a finding: a claim the baseline already held
        # outside supported/unresolved, left exactly as it was, was not caused
        # by this withdrawal and must not make the operation unusable.
        if (now is not None and now["semantic"] not in ALLOWED_AFTER
                and (was is None or was["semantic"] != now["semantic"])):
            raise InvalidationError(
                f"withdrawing {target} left {claim_id} {now['semantic']}: a dropped assumption is a "
                "missing premise, never a refutation")
        entry: dict[str, Any] = {
            "before": None if was is None else {"semantic": was["semantic"], "operational": was["operational"]},
            "after": None if now is None else {"semantic": now["semantic"], "operational": now["operational"]},
            "missing_premise": missing_relations(now["missing_premises"]) if now else [],
            "blocking_premise": None,
            "certificate_sha256": (certificate_sha256(certificates[claim_id])
                                   if claim_id in certificates else None),
        }
        entry["changed"] = entry["before"] != entry["after"]
        if explain is not None:
            entry.update(explain(claim_id, relations_after))
        # "flipped" is what the withdrawal *cost*: claims that stopped being
        # supported.  A claim that gains support (an open undeclared_write
        # claim picks up the table the exclusion used to cover) is "changed"
        # but is not a loss, and is not comparable with a fallen-row prediction.
        if was and was["semantic"] == "supported" and (now is None or now["semantic"] != "supported"):
            lost.append(claim_id)
        if now and now["semantic"] == "supported" and (was is None or was["semantic"] != "supported"):
            gained.append(claim_id)
        claims[claim_id] = entry

    conclusions, owners = _conclusions(baseline_row_certificates)
    predicted = impact(bundle, baseline_result.python.relations, evidence_ids, conclusions,
                       max_depth=max_depth, max_nodes=max_nodes)
    predicted_fallen = sorted(_claims_of(predicted["fallen"], owners))
    return Invalidation(
        assumption_id=target, withdrawn=withdrawn,
        baseline_bundle_digest=bundle_digest(bundle), withdrawn_bundle_digest=bundle_digest(reduced),
        kernels={"matched": bool(result.matched), "python_digest": result.python.canonical_digest,
                 "souffle_digest": result.souffle.canonical_digest},
        claims=claims, flipped=lost, gained=gained, predicted_fallen=predicted_fallen,
        prediction_agrees=set(predicted_fallen) == set(lost),
        bundle=reduced, relations=relations_after,
        certificates=certificates, row_certificates=row_certificates)


__all__ = ["REGISTRY_VERSION", "INVALIDATION_VERSION", "ASSUMPTION_PREFIX", "ASSUMPTION_KIND",
           "ALLOWED_AFTER", "InvalidationError", "Invalidation",
           "producer_class", "evidence_row", "content_digest", "assumption_id_of", "assumption_id",
           "assumption_records", "resolve", "reviewed_against", "certificate_sha256", "certify_claims",
           "shared_across", "registry", "withdraw", "invalidate", "missing_relations", "conclusion_of"]
