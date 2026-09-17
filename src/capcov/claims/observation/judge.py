"""Strict single-Python CLI for judging an externally observed Lane A receipt.

Example::

    python -m capcov.claims.observation.judge \
      --receipt /private/run \
      --candidate-root /checkout/candidate \
      --incumbent-manifest /private/prepared-runtime.json \
      --expected-nonce "$CAPTURED_NONCE" \
      --expected-fixture-digest "$CAPTURED_FIXTURE_SHA256" \
      --invocation-record /private/invocation.json \
      --expected-invocation-digest "$PRE_RUN_IDENTITY_SHA256" \
      --out-dir /private/judgment

The expected nonce and fixture digest are supplied by the observer. This
command never reads either value from the receipt to fill a missing argument
and has no fixture-mode switch. It runs only the pure-Python kernel.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from . import observation_facts
from . import join as observation_join
from ..differential import KernelReport
from ..ir import canonical_json

MAX_REPORT_BYTES = 4 * 1024 * 1024
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _sha256_arg(name: str):
    def parse(value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise argparse.ArgumentTypeError(f"{name} must be 64 lowercase hexadecimal characters")
        return value
    return parse


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", required=True, type=Path,
                        help="directory containing receipt.json and its observation files")
    parser.add_argument("--candidate-root", required=True, type=Path,
                        help="clean Git worktree for the candidate source tree")
    parser.add_argument("--incumbent-manifest", required=True, type=Path,
                        help="prepared-runtime manifest whose exact bytes are digested")
    parser.add_argument("--expected-nonce", required=True,
                        help="nonce captured outside the receipt")
    parser.add_argument("--expected-fixture-digest", required=True,
                        type=_sha256_arg("--expected-fixture-digest"),
                        help="fixture SHA-256 captured outside the receipt")
    parser.add_argument("--invocation-record", type=Path,
                        help="external lane-a-invocation/v1 source and provisioning record")
    parser.add_argument("--expected-invocation-digest", type=_sha256_arg("--expected-invocation-digest"),
                        help="pre-run SHA-256 of the canonical immutable invocation identity subset")
    parser.add_argument("--admission-record", type=Path,
                        help="external observation_admissions.json reviewer record")
    parser.add_argument("--expected-admission-digest", type=_sha256_arg("--expected-admission-digest"),
                        help="independently captured raw SHA-256 of --admission-record")
    parser.add_argument("--out-dir", required=True, type=Path,
                        help="new or empty directory for the bounded JSON report and certificates")
    return parser


def _safe_text(text: str, roots: tuple[tuple[str, str], ...]) -> str:
    for value, replacement in roots:
        text = text.replace(value, replacement)
    return text


def _redact(value: Any, roots: tuple[tuple[str, str], ...]) -> Any:
    if isinstance(value, str):
        return _safe_text(value, roots)
    if isinstance(value, list):
        return [_redact(item, roots) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item, roots) for key, item in value.items()}
    return value


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def _check_paths(receipt: Path, candidate: Path, manifest: Path, out_dir: Path,
                 invocation: Path | None, admissions: Path | None) -> None:
    receipt = receipt.resolve()
    candidate = candidate.resolve()
    manifest = manifest.resolve()
    out_dir = out_dir.resolve()
    if _paths_overlap(receipt, out_dir):
        raise ValueError("output directory overlaps the receipt directory")
    if manifest == receipt or manifest.is_relative_to(receipt):
        raise ValueError("incumbent manifest must be outside the receipt directory")
    try:
        out_dir.relative_to(candidate)
    except ValueError:
        pass
    else:
        raise ValueError("output directory is inside the candidate worktree")
    if out_dir == manifest or manifest.is_relative_to(out_dir):
        raise ValueError("output directory would contain the incumbent manifest")
    if manifest in {receipt / observation_facts.RECEIPT_FILE,
                    receipt / observation_facts.LOG_FILE,
                    receipt / observation_facts.ADMISSIONS_FILE}:
        raise ValueError("incumbent manifest overlaps a receipt input")
    for label, source in (("invocation record", invocation), ("admission record", admissions)):
        if source is None:
            continue
        source = source.resolve()
        if _paths_overlap(receipt, source):
            raise ValueError(f"external {label} must be outside the receipt directory")
        if out_dir == source or source.is_relative_to(out_dir):
            raise ValueError(f"output directory would contain the external {label}")
def _strict_json(path: Path, expected_digest: str | None,
                 label: str) -> tuple[dict[str, Any], str, bytes]:
    raw = path.read_bytes()
    if len(raw) > 1024 * 1024:
        raise ValueError(f"{label} exceeds the 1 MiB input limit")
    actual = hashlib.sha256(raw).hexdigest()
    if expected_digest is not None and actual != expected_digest:
        raise ValueError(f"{label} digest does not match the independent expected digest")

    def unique_object(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"{label} contains a duplicate JSON key")
            result[key] = value
        return result

    value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value, actual, raw


def _valid_hex(value: Any, length: int = 64) -> bool:
    return isinstance(value, str) and re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is not None


def invocation_identity_digest(record: dict[str, Any]) -> str:
    """Digest only the pre-run identity fields; fixture_setup is later mutable."""
    immutable = {key: record[key] for key in (
        "format", "run_nonce", "expected_fixture_digest", "expected_source_digest",
        "candidate", "incumbent")}
    raw = json.dumps(immutable, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _validate_invocation(record: dict[str, Any], expected_nonce: str,
                         expected_fixture: str) -> tuple[bool, str]:
    top = {"format", "run_nonce", "expected_fixture_digest", "expected_source_digest",
           "candidate", "incumbent", "fixture_setup"}
    if set(record) != top or record.get("format") != "lane-a-invocation/v1":
        raise ValueError("invocation record has an unsupported shape or format")
    if not _valid_hex(record.get("run_nonce")) or not _valid_hex(record.get("expected_fixture_digest")):
        raise ValueError("invocation record nonce or fixture digest is malformed")
    if not _valid_hex(record.get("expected_source_digest")):
        raise ValueError("invocation record source digest is malformed")
    candidate = record["candidate"]
    incumbent = record["incumbent"]
    setup = record["fixture_setup"]
    if not isinstance(candidate, dict) or set(candidate) != {"commit", "tree"}:
        raise ValueError("invocation candidate identity is malformed")
    if any(not _valid_hex(candidate.get(key), 40) for key in ("commit", "tree")):
        raise ValueError("invocation candidate commit or tree is malformed")
    incumbent_fields = {"commit", "runtime_commit", "source_manifest_sha256",
                        "runtime_contract_sha256", "baseline_contract_sha256"}
    if not isinstance(incumbent, dict) or set(incumbent) != incumbent_fields:
        raise ValueError("invocation incumbent identity is malformed")
    if (not _valid_hex(incumbent.get("commit"), 40)
            or not _valid_hex(incumbent.get("runtime_commit"), 40)
            or any(not _valid_hex(incumbent.get(key)) for key in (
                "source_manifest_sha256", "runtime_contract_sha256", "baseline_contract_sha256"))):
        raise ValueError("invocation incumbent identity digest is malformed")
    setup_fields = {"state", "services", "schema_sha256", "seed_recipe_sha256",
                    "identity_count", "attestation_scope"}
    if not isinstance(setup, dict) or set(setup) != setup_fields:
        raise ValueError("invocation fixture setup record is malformed")
    if not _valid_hex(setup.get("schema_sha256")) or not _valid_hex(setup.get("seed_recipe_sha256")):
        raise ValueError("invocation fixture setup digest is malformed")
    if type(setup.get("identity_count")) is not int or setup["identity_count"] < 1:
        raise ValueError("invocation fixture identity count is malformed")
    if not isinstance(setup.get("attestation_scope"), str) or not setup["attestation_scope"].strip():
        raise ValueError("invocation fixture attestation scope is malformed")
    services = setup.get("services")
    if not isinstance(services, dict) or set(services) != {"mysql", "redis", "mongo"}:
        raise ValueError("invocation fixture service readiness is malformed")
    if any(type(ready) is not bool for ready in services.values()):
        raise ValueError("invocation fixture service readiness must use booleans")
    if record["run_nonce"] != expected_nonce or record["expected_fixture_digest"] != expected_fixture:
        return False, "external-nonce-or-fixture-binding-mismatch"
    if setup.get("state") != "verified" or not all(services.values()):
        return False, "fixture-provisioning-not-verified"
    return True, "verified"


def _classify(join: observation_join.ObservationJoin, summary: dict[str, Any],
              gate_reason: str | None = None
              ) -> tuple[str, int, str]:
    if join.exported.status != observation_facts.STATUS_COMPLETE:
        if join.exported.status == observation_facts.STATUS_RESOURCE_EXHAUSTED:
            return "operational-failure", 3, "resource-exhausted"
        if join.exported.status == observation_facts.STATUS_STALE:
            return "invalid-input", 3, "stale-input"
        return "invalid-input", 3, "invalid-input"
    metadata = dict(join.exported.bundle.metadata) if join.exported.bundle else {}
    log = metadata.get("log", {})
    if not log.get("verified", False):
        return "invalid-input", 3, "required-log-unverified"
    if gate_reason:
        return "pending", 5, gate_reason
    report = join.report()
    if report is None:
        return "operational-failure", 3, "python-kernel-did-not-run"
    if report.operational_failure:
        return "operational-failure", 3, report.operational_failure
    if (join.source_observation is not None
            and join.source_observation.status == "operational-failure"):
        return "operational-failure", 3, "source-observation-failed"
    if join.source_observation is not None and not join.source_observation.matches:
        return "pending", 5, "source-binding-unobserved-or-stale"
    if any(finding.startswith("stale:") for finding in join.contract_findings):
        return "pending", 5, "claim-time-binding-stale"
    diagnostics = summary.get("diagnostics", [])
    refuted = [row["relation"] for row in diagnostics if row.get("status") == "refuted"]
    if refuted or summary.get("observations_agree") == "refuted":
        return "unsupported", 1, "observed-disagreement-or-unadmitted-difference"
    if (summary.get("observations_agree") == "supported"
            and summary.get("operational") == "complete"):
        return "supported", 0, "observations-agree-under-admitted-policy"
    blocking = summary.get("blocking_premise") or {}
    relation = blocking.get("relation") or "unresolved-premise"
    # Missing signed policy or scenario-set admission is a review decision, not
    # evidence of a kernel disagreement. Other unresolved runs remain pending
    # too, with their exact blocking premise carried in the report.
    return "pending", 5, str(relation)


def _certificate_documents(join: observation_join.ObservationJoin) -> dict[str, dict[str, Any]]:
    names = {
        join.agree_claim: "certificate-observations-agree.json",
        join.stable_claim: "certificate-incumbent-stable.json",
    }
    return {names[claim_id]: certificate
            for claim_id, certificate in join.certificates.items() if claim_id in names}


def _report(join: observation_join.ObservationJoin,
            gate_reason: str | None = None,
            external_bindings: dict[str, Any] | None = None) -> dict[str, Any]:
    document = observation_join.summary(join)
    report: KernelReport | None = join.python_report
    document["judge"] = "observation-judge-single-python-v1"
    document["judge_status"], document["exit_code"], document["decision_basis"] = _classify(
        join, document, gate_reason)
    # Do not let the caller-supplied private directory name leak into the
    # shareable report. The receipt's run id and exact digests are reported.
    document["receipt"] = "<receipt-dir>"
    document["kernels"] = ["python"]
    document["kernel_execution"] = (
        "not-run" if report is None else
        "operational-failure" if report.operational_failure else "complete")
    # KernelReport.canonical_digest binds normalized relations and claim
    # verdicts too; it is a full result identity, not a closure-only digest.
    document["python_result_digest"] = report.canonical_digest if report else None
    document["source_observed_digest"] = (
        join.source_observation.observed if join.source_observation else None)
    document["source_observed_matches"] = (
        join.source_observation.matches if join.source_observation else False)
    document["source_observation_status"] = (
        join.source_observation.status if join.source_observation else "unobserved")
    document["source_binding_scope"] = {
        "candidate_git": "clean checkout commit and tree are read by the judge",
        "incumbent_identity": "pinned by the pre-run external invocation record",
        "incumbent_manifest": "exact manifest bytes are checked against the pinned SHA-256",
        "incumbent_runtime_closure": "not independently verified by this judge",
    }
    document["certificates"] = {
        filename: {
            "sha256": hashlib.sha256(canonical_json(certificate).encode("utf-8")).hexdigest(),
            "nodes": certificate["nodes"],
            "leaves": len(certificate["leaves"]),
            "truncated": certificate["truncated"],
        }
        for filename, certificate in _certificate_documents(join).items()
    }
    document["external_bindings"] = external_bindings or {}
    if document["judge_status"] in {"invalid-input", "operational-failure"}:
        document["operational_failure"] = document["decision_basis"]
    return document


def _emit(document: dict[str, Any], out_dir: Path | None,
          roots: tuple[tuple[str, str], ...]) -> int:
    certificate_documents = document.pop("_certificate_documents", {})
    document = _redact(document, roots)
    certificate_documents = _redact(certificate_documents, roots)
    rendered = json.dumps(document, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    encoded = rendered.encode("utf-8")
    exit_code = int(document.get("exit_code", 3))
    if len(encoded) > MAX_REPORT_BYTES:
        fallback = {"judge": "observation-judge-single-python-v1",
                    "judge_status": "operational-failure", "exit_code": 3,
                    "operational_failure": "report-too-large"}
        rendered = json.dumps(fallback, indent=1, sort_keys=True) + "\n"
        encoded = rendered.encode("utf-8")
        exit_code = 3
        out_dir = None
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        if any(out_dir.iterdir()):
            raise FileExistsError("output directory must be empty")
        artifacts = {"report.json": encoded}
        artifacts.update({name: (json.dumps(cert, indent=1, sort_keys=True,
                                           ensure_ascii=False) + "\n").encode("utf-8")
                          for name, cert in certificate_documents.items()})
        if sum(map(len, artifacts.values())) > MAX_ARTIFACT_BYTES:
            raise ValueError("report and certificates exceed the artifact size limit")
        for name, content in artifacts.items():
            target = out_dir / name
            if target.exists():
                raise FileExistsError("output file already exists")
            target.write_bytes(content)
    sys.stdout.write(rendered)
    return exit_code


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not _SHA256.fullmatch(args.expected_nonce):
        parser.error("--expected-nonce must be 64 lowercase hexadecimal characters")
    if (args.invocation_record is None) != (args.expected_invocation_digest is None):
        parser.error("--invocation-record and --expected-invocation-digest must be supplied together")
    if (args.admission_record is None) != (args.expected_admission_digest is None):
        parser.error("--admission-record and --expected-admission-digest must be supplied together")
    try:
        _check_paths(args.receipt, args.candidate_root, args.incumbent_manifest, args.out_dir,
                     args.invocation_record, args.admission_record)
        invocation = None
        gate_reason = "external-invocation-record-absent"
        if args.invocation_record is not None:
            invocation, invocation_file_digest, _invocation_bytes = _strict_json(
                args.invocation_record, None, "invocation record")
            invocation_ok, invocation_reason = _validate_invocation(
                invocation, args.expected_nonce, args.expected_fixture_digest)
            invocation_identity = invocation_identity_digest(invocation)
            if invocation_identity != args.expected_invocation_digest:
                invocation_ok, invocation_reason = False, "pre-run-invocation-identity-digest-mismatch"
            gate_reason = None if invocation_ok else invocation_reason
        admission_digest = None
        admission_bytes = None
        if args.admission_record is not None:
            _admissions, admission_digest, admission_bytes = _strict_json(
                args.admission_record, args.expected_admission_digest, "admission record")

        expected_incumbent = (invocation or {}).get("incumbent", {})
        expected_candidate = (invocation or {}).get("candidate", {})
        fixture_setup = (invocation or {}).get("fixture_setup", {})
        can_observe_source = invocation is not None and fixture_setup.get("state") == "verified"
        join = observation_join.build(
            args.receipt,
            candidate_root=args.candidate_root if can_observe_source else None,
            incumbent_manifest=args.incumbent_manifest if can_observe_source else None,
            nonce=args.expected_nonce,
            fixture_digest=args.expected_fixture_digest,
            fixture=False,
            external_admissions_bytes=admission_bytes,
            expected_incumbent_commit=expected_incumbent.get("commit") if can_observe_source else None,
            expected_incumbent_runtime_commit=(expected_incumbent.get("runtime_commit")
                                               if can_observe_source else None),
            expected_manifest_sha256=(expected_incumbent.get("source_manifest_sha256")
                                      if can_observe_source else None),
            expected_source_digest=(invocation.get("expected_source_digest")
                                    if can_observe_source else None),
            expected_candidate_commit=expected_candidate.get("commit") if can_observe_source else None,
            expected_candidate_tree=expected_candidate.get("tree") if can_observe_source else None,
        )
        log_verified = (dict(join.exported.bundle.metadata).get("log", {}).get("verified", False)
                        if join.exported.bundle else False)
        if join.bundle is not None and log_verified and gate_reason is None:
            observation_join.evaluate_python(join)
        bindings = {
            "invocation_record_sha256": invocation_file_digest if invocation is not None else None,
            "pre_run_identity_sha256": (invocation_identity if invocation is not None else None),
            "invocation_record_status": ("verified" if gate_reason is None else
                                          "absent" if invocation is None else "pending"),
            "admission_record_sha256": admission_digest,
            "admission_record_status": "verified" if admission_digest else "absent",
            "admission_hash_scope": ("external bytes only; reviewer identity is not cryptographically authenticated"
                                     if admission_digest else None),
            "fixture_setup_state": fixture_setup.get("state") if invocation is not None else None,
            "fixture_provisioning_basis": ("producer-reported; no independent state verifier"
                                            if invocation is not None else None),
            "fixture_attestation_scope_sha256": (
                hashlib.sha256(fixture_setup["attestation_scope"].encode("utf-8")).hexdigest()
                if invocation is not None else None),
            "invocation_identity": ({
                "run_nonce_sha256": hashlib.sha256(invocation["run_nonce"].encode()).hexdigest(),
                "expected_fixture_digest": invocation["expected_fixture_digest"],
                "expected_source_digest": invocation["expected_source_digest"],
                "candidate": dict(invocation["candidate"]),
                "incumbent": dict(invocation["incumbent"]),
                "fixture_setup": {key: value for key, value in fixture_setup.items()
                                  if key != "attestation_scope"},
            } if invocation is not None else None),
        }
        document = _report(join, gate_reason, bindings)
        document["_certificate_documents"] = _certificate_documents(join)
        roots = ((str(args.receipt.resolve()), "<receipt-dir>"),
                 (str(args.candidate_root.resolve()), "<candidate-root>"),
                 (str(args.incumbent_manifest.resolve()), "<incumbent-manifest>"),
                 (str(args.out_dir.resolve()), "<out-dir>"),
                 *((str(path.resolve()), label) for path, label in (
                     (args.invocation_record, "<invocation-record>"),
                     (args.admission_record, "<admission-record>")) if path is not None))
        return _emit(document, args.out_dir, roots)
    except (OSError, ValueError, TypeError) as exc:
        # Do not echo filesystem paths or low-level parser text into a receipt.
        error = "output-failure" if isinstance(exc, OSError) else "invalid-input"
        document = {"judge": "observation-judge-single-python-v1",
                    "judge_status": "operational-failure" if error == "output-failure" else "invalid-input",
                    "exit_code": 3,
                    "operational_failure": error,
                    "message": "input or output could not be processed"}
        try:
            sys.stdout.write(json.dumps(document, indent=1, sort_keys=True) + "\n")
        except OSError:
            pass
        return 3


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
