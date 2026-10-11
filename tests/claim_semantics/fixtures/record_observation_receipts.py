#!/usr/bin/env python3
"""Write the four synthetic observation receipts the observation tests read.

    python tests/claim_semantics/fixtures/record_observation_receipts.py

Each ``observation_receipt_<variant>/`` directory holds ``receipt.json``,
``log.txt`` and ``observation_admissions.json`` for one made-up read-only
check, ``role_list``: twelve pre-registered requests put to an incumbent and a
candidate.  Every value is synthetic.  Opaque digests are SHA-256 of a label
(``_hex``); every digest the exporter recomputes (source, fixture, policy,
scenario set, run id, log, receipt) is computed here from the blocks it
covers, so the receipts conform to ``observation-receipt/v1``.

The variants differ in one place each:

* ``agree``    -- both sides answer all twelve alike.
* ``disagree`` -- the candidate's ``tenant-guest`` body differs.
* ``gap``      -- the candidate did not answer ``project-malformed``.
* ``masked``   -- the policy declares ``sort-roles`` over ``body:/roles`` and it
  fired on both sides of ``tenant-own`` over differing raw bodies.

Rerunning the script rewrites the same bytes.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PACKAGE_ROOT = HERE.parents[2]
if str(PACKAGE_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT / "src"))

from capcov.claims.ir import canonical_json  # noqa: E402

CHECK_ID = "role_list"
CREDENTIAL_MARKERS = ["Bearer ", "X-AUTH-", "password", "secret", "token="]

#: (id, kind, actor, target, expected class, intent, status)
SCENARIOS = (
    ("anonymous-global", "denial", "", "GET /roles", "4xx",
     "an unauthenticated request is refused before any role is read", 401),
    ("support-global", "success", "support-staff", "GET /roles", "2xx",
     "a support actor sees the global role list", 200),
    ("member-global", "denial", "tenant-member", "GET /roles", "4xx",
     "a member without global access is refused the global role list", 403),
    ("tenant-own", "success", "tenant-member", "GET /tenants/{tenant}/roles", "2xx",
     "a member sees the roles of the tenant it belongs to", 200),
    ("tenant-guest", "success", "tenant-guest", "GET /tenants/{tenant}/roles", "2xx",
     "a guest sees the reduced role list of its host tenant", 200),
    ("tenant-foreign", "denial", "other-tenant-member", "GET /tenants/{tenant}/roles", "4xx",
     "a member of another tenant is refused", 403),
    ("tenant-pending", "denial", "pending-member", "GET /tenants/{tenant}/roles", "4xx",
     "a membership that was never completed does not grant the list", 403),
    ("tenant-malformed", "malformed", "tenant-member", "GET /tenants/{tenant}/roles", "4xx",
     "a tenant identifier that is not an identifier is rejected, not coerced", 400),
    ("project-own", "success", "project-member", "GET /projects/{project}/roles", "2xx",
     "a project member sees the project role list", 200),
    ("project-foreign", "denial", "other-tenant-member", "GET /projects/{project}/roles", "4xx",
     "a member of another tenant is refused the project role list", 403),
    ("project-archived", "denial", "project-member", "GET /projects/{project}/roles", "4xx",
     "an archived project's role list is not readable through this route", 403),
    ("project-malformed", "malformed", "project-member", "GET /projects/{project}/roles", "4xx",
     "a project identifier that is not an identifier is rejected, not coerced", 400),
)
SEED_IDENTITIES = sorted({actor for _, _, actor, *_ in SCENARIOS if actor})

UNASSESSED = (
    ("model_conformance",
     "Not assessed. No executable model of intended behaviour described this run. Nothing here "
     "says either implementation is correct; it says only that, on the scenarios listed, they "
     "were observed to answer alike. A shared defect present in both is exactly what agreement "
     "looks like."),
    ("fault_sensitivity",
     "Not assessed. No mutation or fault injection was run against this scenario set, so its "
     "power to detect a defect is unmeasured."),
    ("scenario_coverage",
     "Not assessed. The claim is bounded by the admitted scenario set whose digest it names. "
     "Behaviour outside that set is unobserved, not passing. Nothing argues the set is "
     "representative of the capability."),
    ("write_set_declaration",
     "Not assessed. Effects, where compared, were compared incumbent-to-candidate. No declared "
     "write set was checked, so a table both implementations touch and neither should is "
     "invisible here."),
    ("producer_independence",
     "Not assessed. The receipt was produced by the same program that performed both "
     "observations. The controls here make silence, staleness and after-the-fact edits "
     "detectable; they do not make a dishonest or defective producer detectable."),
    ("deployed_parity",
     "Not assessed. A disposable local fixture with a synthetic tenant, one process, no "
     "gateway, no proxy, no real credentials. Nothing here is a claim about a deployed "
     "environment."),
    ("concurrency",
     "Not assessed. Scenarios ran sequentially against a freshly seeded fixture."),
    ("performance",
     "Not assessed. duration_ms is recorded as evidence and is not compared."),
    ("effect_ordering",
     "Not assessed. Effects are compared as an unordered set folded to a digest. Statement "
     "order, interleaving, and transient rows written and deleted inside one scenario are not "
     "observed."),
    ("persisted_effects",
     "Not assessed. Only the stores whose effect closure is true were compared; every store "
     "left out is unobserved, with its reason recorded. Where no store was compared, persisted "
     "state is entirely unassessed and only wire responses are ranked."),
)

SORT_ROLES = {
    "facet": "body", "id": "sort-roles", "kind": "sort", "preserves": "identity",
    "reason": "array order is not part of the contract",
    "target": "body:/roles",
}


def _hex(label: str, length: int = 64) -> str:
    return hashlib.sha256(f"capcov-observation-fixture:{label}".encode("utf-8")).hexdigest()[:length]


def _digest_of(payload) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _closed(closed: bool, reason: str = "") -> dict:
    return {"closed": closed, "reason": reason}


def _observation(scenario: str, side: str, status: int, duration: int) -> dict:
    body = _hex(f"body:{scenario}")
    return {
        "body_digest": body, "class": f"{status // 100}xx", "duration_ms": duration,
        "effects": [], "effects_observed": False, "failure": None,
        "normalizations_applied": [], "outcome": "observed", "raw_digest": body,
        "scenario": scenario, "side": side, "status": status,
    }


def base_receipt() -> dict:
    """The ``agree`` receipt, before its derived digests are filled in."""
    no_writes = "this capability performs no writes; no store was inspected"
    by_side = {side: {"bodies": _closed(True), "classes": _closed(True),
                      "effect_rows": _closed(False, no_writes), "effects": _closed(False, no_writes),
                      "observed": _closed(True), "statuses": _closed(True)}
               for side in ("candidate", "incumbent")}
    completeness = {"by_side": by_side, **{name: _closed(True) for name in (
        "failures", "masked_differences", "normalizations", "policy", "scenarios", "stability",
        "unassessed", "unobserved")}}
    observations = []
    for index, (scenario, *_rest, status) in enumerate(SCENARIOS):
        observations.append(_observation(scenario, "incumbent", status, 11 + index % 7))
        observations.append(_observation(scenario, "candidate", status, 14 + index % 7))
    log_path = f".work/observation/{CHECK_ID}/log.txt"
    return {
        "check": {
            "id": CHECK_ID,
            "limitations": [
                "read-only: no store is inspected, so no persisted effect is compared",
                "no model: neither side is checked against a description of intended behaviour",
                "the 12 requests are the whole claim; nothing argues they are representative",
            ],
            "scope": "one read-only capability, twelve pre-registered requests, one disposable "
                     "fixture tenant",
            "script_path": f"checks/{CHECK_ID}.py",
            "script_sha256": _hex("check-script"),
            "title": "role list: incumbent and candidate answer the same 12 requests",
        },
        "completeness": completeness,
        "configuration": {
            "command_candidate": ["./bin/candidate", "--listen", "$CANDIDATE_LISTEN"],
            "command_incumbent": ["php", "-S", "$INCUMBENT_LISTEN", "app/router.php"],
            "config_digest": _hex("configuration"),
            "env_names": ["INCUMBENT_LISTEN", "CANDIDATE_LISTEN", "FIXTURE_TENANT",
                          "OBSERVATION_DEADLINE_SECONDS"],
            "side_identity": {
                "candidate": {"binary_sha256": _hex("candidate-binary"), "kind": "go-binary",
                              "listen": "loopback:18082"},
                "incumbent": {"kind": "php-builtin-server", "listen": "loopback:18081",
                              "runtime_sha256": _hex("incumbent-runtime")},
            },
            "sides_distinct": True,
        },
        "contract": "observation-receipt/v1",
        "failures": [],
        "fixture": {
            "name": "standard",
            "recipe_sha256": _hex("fixture-recipe"),
            "schema_sha256": _hex("fixture-schema"),
            "seed_identities": SEED_IDENTITIES,
            "seed_sha256": _hex("fixture-seed"),
            "stores": [{"kind": kind, "reset": True} for kind in ("mysql", "redis", "mongo", "smtp")],
            "tenant": "oracle",
        },
        "log": {"path": log_path, "redaction": {"hits": 0, "markers_checked": CREDENTIAL_MARKERS}},
        "masked_differences": [],
        "normalization_firings": [],
        "observations": observations,
        "policy": {"comparator": "exact deep-equal after the listed normalizations",
                   "exclusions": [], "facets": ["status", "body"], "normalizations": [],
                   "status_normalized": False},
        "repeat": {"compared_scenarios": [scenario for scenario, *_ in SCENARIOS],
                   "run_a": _hex("repeat-run-a", 16), "run_b": _hex("repeat-run-b", 16),
                   "side": "incumbent", "stable": True},
        "reproduce": {
            "argv": ["python3", f"checks/{CHECK_ID}.py", "--receipt", ".work/observation"],
            "container_digests": ["sha256:" + _hex("store-container")],
            "cwd": ".", "deterministic": True,
            "env_names": ["INCUMBENT_LISTEN", "CANDIDATE_LISTEN", "FIXTURE_TENANT"],
        },
        "results": [{"difference": None, "outcome": "agree", "scenario": scenario}
                    for scenario, *_ in SCENARIOS],
        "run": {"deadline_seconds": 120, "elapsed_seconds": 38.6,
                "nonce": _hex("run-nonce"), "recorded_at": "2026-09-16T09:14:41Z",
                "started_at": "2026-09-16T09:14:02Z"},
        "scenario_set": {
            "count": len(SCENARIOS), "declared_at": "pre-observation",
            "required_kinds": ["success", "denial", "malformed"],
            "scenarios": [{"actor": actor, "expected_class": expected, "id": scenario,
                           "intent": intent, "kind": kind, "required": True, "target": target}
                          for scenario, kind, actor, target, expected, intent, _ in SCENARIOS],
        },
        "sources": {
            "candidate": {"commit": _hex("candidate-commit", 40), "dirty": False,
                          "inputs": [{"path": "src/roles.go", "sha256": _hex("src/roles.go")},
                                     {"path": "src/roles_rows.go",
                                      "sha256": _hex("src/roles_rows.go")}],
                          "repo": "candidate", "tree": _hex("candidate-tree", 40)},
            "incumbent": {
                "baseline_contract": {"path": "contracts/baselines.json",
                                      "sha256": _hex("contracts/baselines.json")},
                "commit": _hex("incumbent-commit", 40),
                "inputs": [{"path": "app/router.php", "sha256": _hex("app/router.php")},
                           {"path": "app/roles.php", "sha256": _hex("app/roles.php")}],
                "manifest_sha256": _hex("incumbent-manifest"),
                "runtime_commit": _hex("incumbent-runtime-commit", 40),
                "runtime_contract": {"path": "contracts/runtime.json",
                                     "sha256": _hex("contracts/runtime.json")},
                "source_behavior_differences": [],
            },
            "toolchain": {"go": "go1.24.4", "php": "8.2.20", "python": "3.13.1"},
        },
        "terminal": {"blocking_stage": None, "exit_code": 0, "outcome": "compared",
                     "reason": "every declared scenario was put to both sides"},
        "unassessed": [{"dimension": dimension, "reason": reason} for dimension, reason in UNASSESSED],
        "unobserved": [],
    }


def _entry(receipt: dict, scenario: str, side: str) -> dict:
    return next(entry for entry in receipt["observations"]
                if entry["scenario"] == scenario and entry["side"] == side)


def _result(receipt: dict, scenario: str) -> dict:
    return next(row for row in receipt["results"] if row["scenario"] == scenario)


def disagree(receipt: dict) -> list[str]:
    candidate = _entry(receipt, "tenant-guest", "candidate")
    candidate["body_digest"] = candidate["raw_digest"] = _hex("body:tenant-guest:candidate")
    _result(receipt, "tenant-guest").update(outcome="differ", difference={
        "candidate_digest": candidate["body_digest"], "facet": "body", "field_path": "body:/roles",
        "incumbent_digest": _entry(receipt, "tenant-guest", "incumbent")["body_digest"]})
    return ["tenant-guest: bodies differ"]


def gap(receipt: dict) -> list[str]:
    receipt["observations"].remove(_entry(receipt, "project-malformed", "candidate"))
    receipt["unobserved"] = [{"reason": "timeout", "scenario": "project-malformed",
                              "side": "candidate"}]
    _result(receipt, "project-malformed")["outcome"] = "not_observed"
    receipt["terminal"]["reason"] = ("the candidate did not answer project-malformed inside its "
                                     "deadline; the harness itself did not fail")
    return ["project-malformed: candidate did not answer inside the deadline"]


def masked(receipt: dict) -> list[str]:
    receipt["policy"]["normalizations"] = [{**SORT_ROLES, "source_sha256": _hex("sort-roles")}]
    firings = []
    for side in ("incumbent", "candidate"):
        entry = _entry(receipt, "tenant-own", side)
        entry["raw_digest"] = _hex(f"body:tenant-own:{side}:unsorted")
        entry["normalizations_applied"] = ["sort-roles"]
        firings.append({"after_digest": entry["body_digest"], "before_digest": entry["raw_digest"],
                        "facet": "body", "normalization": "sort-roles", "scenario": "tenant-own",
                        "side": side})
    receipt["normalization_firings"] = firings
    receipt["masked_differences"] = [{
        "candidate_before_digest": firings[1]["before_digest"], "facet": "body",
        "field_path": "body:/roles",
        "incumbent_before_digest": firings[0]["before_digest"],
        "normalization": "sort-roles", "scenario": "tenant-own"}]
    return ["tenant-own: sort-roles fired on both sides over differing values"]


VARIANTS = {"agree": None, "disagree": disagree, "gap": gap, "masked": masked}


def seal(receipt: dict, extra_log: list[str]) -> str:
    """Fill in every derived field; returns the log text."""
    results = receipt["results"]
    agree = sum(row["outcome"] == "agree" for row in results)
    differ = sum(row["outcome"] == "differ" for row in results)
    not_observed = sum(row["outcome"] == "not_observed" for row in results)
    receipt["agreement"] = {"agree": agree, "compared": agree + differ,
                            "complete": agree == len(results), "degenerate": 0,
                            "differ": differ, "not_observed": not_observed}
    sources = receipt["sources"]
    sources["source_digest"] = _digest_of({
        "candidate": {key: sources["candidate"][key] for key in ("repo", "commit", "tree")},
        "incumbent": {key: sources["incumbent"][key]
                      for key in ("commit", "runtime_commit", "manifest_sha256")}})
    fixture = receipt["fixture"]
    fixture["digest"] = _digest_of({key: fixture[key] for key in (
        "name", "tenant", "schema_sha256", "seed_sha256", "seed_identities", "recipe_sha256",
        "stores")})
    policy = receipt["policy"]
    policy["digest"] = _digest_of({key: policy[key]
                                   for key in ("facets", "normalizations", "exclusions")})
    scenario_set = receipt["scenario_set"]
    scenario_set["digest"] = _digest_of({
        "check": CHECK_ID, "required_kinds": scenario_set["required_kinds"],
        "scenarios": [{key: entry[key] for key in (
            "id", "kind", "required", "actor", "target", "expected_class", "intent")}
            for entry in scenario_set["scenarios"]]})
    run = receipt["run"]
    run["id"] = _digest_of([CHECK_ID, sources["source_digest"], fixture["digest"],
                            scenario_set["digest"], policy["digest"], run["nonce"],
                            run["started_at"]])[:16]
    answered = len(SCENARIOS) - receipt["agreement"]["not_observed"]
    put = ("12 put to both sides" if answered == len(SCENARIOS)
           else f"{answered} answered by both sides")
    lines = [f"observation check {CHECK_ID}",
             "preflight: worktree clean, check source digest pinned",
             f"fixture: tenant {fixture['tenant']}, {len(fixture['stores'])} stores reset",
             "incumbent: started, listening on the incumbent socket",
             "candidate: started, listening on the candidate socket",
             f"scenarios: {len(SCENARIOS)} declared, {put}",
             f"redaction: scanned for {len(CREDENTIAL_MARKERS)} credential markers, 0 hits",
             *extra_log, "terminal: compared"]
    log = "\n".join(lines) + "\n"
    log_bytes = len(log.encode("utf-8"))
    log_sha = hashlib.sha256(log.encode("utf-8")).hexdigest()
    receipt["log"].update(bytes=log_bytes, sha256=log_sha)
    receipt["reproduce"]["artifacts"] = [{"bytes": log_bytes, "path": receipt["log"]["path"],
                                         "sha256": log_sha}]
    receipt["receipt_digest"] = _digest_of(
        {key: value for key, value in receipt.items() if key != "receipt_digest"})
    return log


def admissions(receipt: dict) -> dict:
    return {
        "producer": "reviewer a-reviewer",
        "reviewed_against": {"check": CHECK_ID, "policy": receipt["policy"]["digest"],
                             "scenario_set": receipt["scenario_set"]["digest"]},
        "reviewed_at": "2026-09-16",
        "reviewer": "a-reviewer",
        "rows": [{"relation": "policy_admitted", "version": "v1"},
                 {"relation": "scenario_set_admitted", "version": "v1"}],
    }


def _dump(document: dict) -> str:
    return json.dumps(document, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    for name, mutate in VARIANTS.items():
        receipt = base_receipt()
        extra = mutate(receipt) if mutate else []
        log = seal(receipt, extra)
        directory = HERE / f"observation_receipt_{name}"
        directory.mkdir(exist_ok=True)
        (directory / "receipt.json").write_text(_dump(receipt), encoding="utf-8")
        (directory / "log.txt").write_text(log, encoding="utf-8")
        (directory / "observation_admissions.json").write_text(_dump(admissions(receipt)),
                                                              encoding="utf-8")
        print(f"wrote {directory.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
