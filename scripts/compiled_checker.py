#!/usr/bin/env python3
"""Judge a replay receipt with three claim kernels, one of them a compiled Souffle binary.

Three subcommands, stdlib and ``capcov`` only:

``judge``    build the receipt's combined bundle, compile the rule pack's
             program, run python / interpreted Souffle / compiled Souffle,
             certify every claim row from every closure, and write
             ``receipt.json``, the per-row certificates and ``judge.json``.
             ``judge`` is the default, so a caller may omit it.
``bench``    time the interpreter against the binary on a synthetic receipt
             scaled from a fixture, and write ``bench.json``.
``compile``  compile one rule pack's program and print its ``provenance.json``.

Exit codes (``judge``)::

    0  every required op is op_qualified supported/complete in all three kernels
    1  a required op is not supported, not complete, or not replayed at all
    2  the kernels disagree, or the compiled kernel could not be built
    3  the receipt does not meet the exporter's contract (a contract finding)
    4  the toolchain or the judge's own environment is unavailable (no souffle,
       no souffle-compile.py, an unwritable cache or output directory)
    5  every op the verdict turns on is *pending* a premise nothing can satisfy
       yet -- today only the Stage D typed-checker certificate
       (``model_well_formed``; ``replay.join.PENDING_PREMISES``).  Such an op
       passed every premise that says something about this port: the systems
       agreed with the model, nothing outside the reviewer's exclusions was
       written, no mutant survived, the order and stability gates held.  Exit 5
       is deliberately distinct from 1 so a consumer gate can tell "the checker
       does not exist yet" from "this port is not qualified", and 1 wins over 5
       whenever any op the verdict turns on has a real blocker.

The per-op ``qualification`` field of ``judge.json`` carries the same three
states in words: ``qualified``, ``pending <relation>``, ``unsupported``.

``judge.json`` also carries the run's cross-request and learn-campaign context:
each op entry has ``repeat_delete`` (the repeats found, the violations among them
and the targets judged not-found) and ``learn_consistent`` / ``learn_unmodeled``,
and the document has a run-level ``learn`` block naming the campaign, its
counterexamples and the ops it reports as unmodelled.  ``{"present": false}``
there means no campaign is bound to the run, which is not a finding.

Without ``--require-op`` the verdict is derived from every replayed op rather
than from an empty requirement: a verdict over zero requirements would be
vacuously ``supported``, which no party has asserted.

The compiled binary is a third independent evaluator with recorded provenance,
never a replacement for the interpreter or the Python kernel: a compiled-side
failure is a named failure, never a fallback, and ``judge`` exits non-zero
unless all three kernels agree.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import statistics
import sys
import tempfile
import time
from typing import Any

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
for _entry in (PACKAGE_ROOT / "src",):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from capcov.claims import canonical_json, souffle  # noqa: E402
from capcov.claims.replay import join as replay_join  # noqa: E402
from capcov.claims.replay import pack as replay_pack  # noqa: E402
from capcov.claims.replay import replay_facts  # noqa: E402
from capcov.claims.souffle import compile as compiled  # noqa: E402

JUDGE_SCHEMA = "capcov-compiled-judge-v1"
BENCH_SCHEMA = "capcov-compiled-bench-v1"
JUDGE_FILE = "judge.json"
BENCH_FILE = "bench.json"
DEFAULT_CACHE_DIR = Path(os.environ.get("CAPCOV_SOUFFLE_CACHE_DIR")
                         or Path.home() / ".cache" / "capcov" / "souffle-compiled")
# Receipt relations keyed by ``req``: the rows a synthetic scale-N receipt
# multiplies.  ``model_writes``, ``mutant`` and the reviewer's scope exclusions
# describe the model and the review, not the requests, and are never scaled.
SCALED_RELATIONS = ("replay_request", "php_effect", "go_effect", "php_post_state",
                    "go_post_state", "model_admissible", "model_effect", "mutant_killed")

EXIT_OK = 0
EXIT_NOT_SUPPORTED = 1
EXIT_KERNEL = 2
EXIT_CONTRACT = 3
EXIT_UNAVAILABLE = 4
EXIT_PENDING_PREMISE = 5

VERDICT_SUPPORTED = "supported"
VERDICT_NOT_SUPPORTED = "not-supported"
VERDICT_PENDING_PREMISE = "pending-premise"
VERDICT_KERNEL_MISMATCH = "kernel-mismatch"
VERDICT_CONTRACT_FINDING = "contract-finding"
VERDICT_UNAVAILABLE = "unavailable"


def _sha256_json(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _write_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def _pack_bundle(pack: str):
    """The rule pack alone, whose program a checker is compiled from."""
    if pack == "replay":
        return replay_pack.pack_bundle()
    if pack == "static":
        tests_root = PACKAGE_ROOT / "tests" / "claim_semantics"
        if str(tests_root) not in sys.path:
            sys.path.insert(0, str(tests_root))
        from static_rules import adapter as static_adapter  # noqa: PLC0415

        return static_adapter.pack_bundle()
    raise ValueError(f"unknown pack {pack!r}")


# ---------------------------------------------------------------------------
# judge


def _op_entry(join: replay_join.ReplayJoin, summary: dict[str, Any], op: str) -> dict[str, Any]:
    verdict = join.verdict(join.claim_id("qualified", op)) or {}
    entry = summary.get(op, {})
    certificate = join.certificates.get(join.claim_id("qualified", op))
    qualified = {
        "semantic": verdict.get("semantic"),
        "operational": verdict.get("operational"),
        "missing_premises": entry.get("missing_premise", []),
    }
    return {
        # the per-op answer in one word, so a caller need not re-derive it from
        # the pair; VERDICT_SUPPORTED only when the op is supported *and* complete
        "verdict": (VERDICT_SUPPORTED if qualified["semantic"] == "supported"
                    and qualified["operational"] == "complete" else VERDICT_NOT_SUPPORTED),
        # "qualified" / "pending <relation>" / "unsupported" (replay.join.qualification):
        # a pending op passed every premise that is checkable today
        "qualification": entry.get("qualification", replay_join.QUALIFICATION_UNSUPPORTED),
        "op_qualified": qualified,
        "corpus_constrains": bool(entry.get("corpus_constrains")),
        # the cross-request gate op_qualified_rt is now bound by: the repeats of this op's
        # requests, the violations found among them, and the targets judged not-found
        "repeat_delete": entry.get("repeat_delete", {"repeats": [], "violations": [], "not_found": []}),
        # the learn campaign, when one is bound to the run: whether the model's predictions
        # matched the oracle for this op, and whether the campaign says the op is unmodelled
        "learn_consistent": entry.get("learn_consistent"),
        "learn_unmodeled": bool(entry.get("learn_unmodeled")),
        "exclusions_applied": list(entry.get("exclusions_applied", [])),
        "blocking_premise": entry.get("blocking_premise"),
        "certificate_sha256": _sha256_json(certificate) if certificate is not None else None,
    }


def _judge_document(join: replay_join.ReplayJoin, required: list[str], *,
                    verdict: str, exit_code: int, program_digest: str | None,
                    findings: list[str]) -> dict[str, Any]:
    receipt = join.receipt
    document: dict[str, Any] = {
        "schema": JUDGE_SCHEMA,
        "receipt": {"run": join.run, "model": receipt.get("model"), "nonce": receipt.get("nonce"),
                    "snapshot": receipt.get("snapshot"), "php_commit": receipt.get("php_commit"),
                    "go_commit": receipt.get("go_commit")},
        "pack": {"id": replay_pack.PACK_ID, "program_digest": program_digest,
                 "relation_count": len(join.bundle.relations) if join.bundle is not None else 0,
                 "rule_count": len(join.bundle.rules) if join.bundle is not None else 0},
        "compiled": join.checker.provenance() if join.checker is not None else None,
        "kernels": None,
        "ops": {},
        "required_ops": list(required),
        "learn": {"present": False},
        "contract_findings": list(findings),
        "verdict": verdict,
        "exit_code": exit_code,
    }
    outcome = join.result if join.result is not None else join.mismatch
    if outcome is not None:
        timings = dict(getattr(outcome, "timings", ()))
        document["kernels"] = {
            "matched": join.result is not None and join.result.matched,
            "python_digest": outcome.python.canonical_digest,
            "souffle_digest": outcome.souffle.canonical_digest,
            "compiled_digest": getattr(outcome, "compiled", None) and outcome.compiled.canonical_digest,
            "closure_digest_equal": getattr(outcome, "closure_digest_equal", False),
            "interpreter_seconds": round(timings.get("souffle", 0.0), 3),
            "compiled_seconds": round(timings.get("souffle-compiled", 0.0), 3),
            "python_seconds": round(timings.get("python", 0.0), 3),
            "failures": {report.backend: report.operational_failure
                         for report in (outcome.python, outcome.souffle,
                                        getattr(outcome, "compiled", None))
                         if report is not None and report.operational_failure},
        }
    if join.result is not None:
        summary = replay_join.summary(join)
        document["ops"] = {op: _op_entry(join, summary, op) for op in join.ops}
        document["learn"] = summary.get("learn", {"present": False})
    return document


def _op_is_supported(entry: dict[str, Any]) -> bool:
    return entry["verdict"] == VERDICT_SUPPORTED


def _all_pending(replayed: dict[str, Any], unmet: list[str]) -> bool:
    """Every unmet op is blocked only by a premise nothing can satisfy yet.

    A required op that was never replayed has no entry and is never pending: it
    is an unmet requirement about this receipt, which exit 1 is for.
    """
    return bool(unmet) and all(
        str(replayed.get(op, {}).get("qualification", "")).startswith("pending ") for op in unmet)


def _unmet_ops(replayed: dict[str, Any], required: list[str]) -> list[str]:
    """The ops the verdict turns on that are not op_qualified supported/complete.

    With ``--require-op`` those are exactly the required ops (an op that was
    never replayed is unmet).  With none, the verdict is derived from every
    replayed op instead of from an empty requirement: ``supported`` over zero
    requirements is a vacuous truth no party has asserted, and a caller that
    reads ``.verdict`` would take it for a positive judgement of the receipt.
    """
    if required:
        return [op for op in required if op not in replayed or not _op_is_supported(replayed[op])]
    return [op for op in sorted(replayed) if not _op_is_supported(replayed[op])]


def _read_join(receipt_dir: Path) -> replay_join.ReplayJoin:
    """Build the join; the receipt's own I/O errors are the receipt's contract.

    Only a read of the receipt directory maps OSError to a contract finding.
    The judge's own I/O (its cache, its output directory) stays an environment
    failure, so an unwritable disk is never reported as a finding against the
    receipt.
    """
    try:
        return replay_join.build(receipt_dir)
    except OSError as exc:
        raise replay_facts.ExportInputError(
            f"receipt directory could not be read: {receipt_dir}: {exc}") from exc


def judge(args: argparse.Namespace) -> int:
    receipt_dir = Path(args.receipt)
    out_dir = Path(args.out)
    required = list(dict.fromkeys(args.require_supported))
    join = _read_join(receipt_dir)
    if join.bundle is None:
        document = _judge_document(join, required, verdict=VERDICT_CONTRACT_FINDING,
                                   exit_code=EXIT_CONTRACT, program_digest=None,
                                   findings=join.contract_findings)
        out_dir.mkdir(parents=True, exist_ok=True)
        _write_json(out_dir / JUDGE_FILE, document)
        for finding in join.contract_findings:
            print(f"contract finding: {finding}", file=sys.stderr)
        return EXIT_CONTRACT
    program_digest = souffle.program_for_pack(join.bundle).program_digest
    try:
        join = replay_join.evaluate_join(join, str(out_dir / "differential"), kernels="three",
                                         cache_dir=args.cache_dir, executable=args.souffle)
    except souffle.SouffleUnavailable as exc:
        document = _judge_document(join, required, verdict=VERDICT_UNAVAILABLE,
                                   exit_code=EXIT_UNAVAILABLE, program_digest=program_digest,
                                   findings=join.contract_findings)
        document["message"] = str(exc)
        _write_json(out_dir / JUDGE_FILE, document)
        print(f"toolchain unavailable: {exc}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    except compiled.CompileError as exc:
        document = _judge_document(join, required, verdict=VERDICT_KERNEL_MISMATCH,
                                   exit_code=EXIT_KERNEL, program_digest=program_digest,
                                   findings=join.contract_findings)
        document["message"] = str(exc)[-4000:]
        _write_json(out_dir / JUDGE_FILE, document)
        print(f"compile failed: {str(exc)[-400:]}", file=sys.stderr)
        return EXIT_KERNEL
    except AssertionError as exc:
        # the kernels agreed on the closure but not on a claim row, a
        # certificate or a recheck: still a kernel disagreement, never a verdict
        document = _judge_document(join, required, verdict=VERDICT_KERNEL_MISMATCH,
                                   exit_code=EXIT_KERNEL, program_digest=program_digest,
                                   findings=join.contract_findings)
        document["message"] = str(exc)[-4000:]
        _write_json(out_dir / JUDGE_FILE, document)
        print(f"kernels disagree on a certified claim row: {exc}", file=sys.stderr)
        return EXIT_KERNEL
    if join.mismatch is not None:
        document = _judge_document(join, required, verdict=VERDICT_KERNEL_MISMATCH,
                                   exit_code=EXIT_KERNEL, program_digest=program_digest,
                                   findings=join.contract_findings)
        _write_json(out_dir / JUDGE_FILE, document)
        print("claim kernels disagree; the receipt is not judged", file=sys.stderr)
        return EXIT_KERNEL
    replay_join.write_artifacts(join, out_dir)
    document = _judge_document(join, required, verdict=VERDICT_SUPPORTED, exit_code=EXIT_OK,
                               program_digest=program_digest, findings=join.contract_findings)
    unmet = _unmet_ops(document["ops"], required)
    judged = required or sorted(document["ops"])
    if unmet or not judged:
        document["verdict"] = VERDICT_NOT_SUPPORTED
        document["exit_code"] = EXIT_NOT_SUPPORTED
        document["unmet_ops"] = unmet
        if not judged:
            document["message"] = "no op was replayed and none was required; nothing is supported"
        elif _all_pending(document["ops"], unmet):
            # every unmet op is blocked only by a premise nothing can satisfy yet
            document["verdict"] = VERDICT_PENDING_PREMISE
            document["exit_code"] = EXIT_PENDING_PREMISE
            document["pending_ops"] = list(unmet)
            document["message"] = ("pending, not unsupported: " + ", ".join(
                f"{op} is {document['ops'][op]['qualification']}" for op in unmet))
    _write_json(out_dir / JUDGE_FILE, document)
    for op, entry in sorted(document["ops"].items()):
        print(f"{op}: {entry['qualification']} op_qualified={entry['op_qualified']['semantic']}"
              f"/{entry['op_qualified']['operational']}"
              f" missing={entry['op_qualified']['missing_premises']}"
              f" exclusions={entry['exclusions_applied']}")
    binary = document["compiled"]["binary_sha256"] if document["compiled"] else "-"
    print(f"verdict={document['verdict']} kernels_matched={document['kernels']['matched']}"
          f" binary={binary[:12]}")
    if unmet and document["verdict"] == VERDICT_PENDING_PREMISE:
        print(document["message"], file=sys.stderr)
    elif unmet:
        label = "required" if required else "replayed"
        print(f"{label} ops not supported: {', '.join(unmet)}", file=sys.stderr)
    elif not judged:
        print(document["message"], file=sys.stderr)
    return int(document["exit_code"])


# ---------------------------------------------------------------------------
# bench


def _scaled_receipt(source: Path, destination: Path, scale: int) -> int:
    """Copy ``source`` to ``destination``, multiplying every ``req``-keyed row ``scale`` times."""
    destination.mkdir(parents=True, exist_ok=True)
    rows_in = 0
    for path in sorted(source.iterdir()):
        if path.suffix != ".json" or not path.is_file():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if path.stem in SCALED_RELATIONS and isinstance(payload.get("rows"), list):
            multiplied = []
            for index in range(scale):
                for row in payload["rows"]:
                    copy = dict(row)
                    copy["req"] = f"{row['req']}-{index}"
                    multiplied.append(copy)
            payload = {**payload, "rows": multiplied}
        if isinstance(payload.get("rows"), list):
            rows_in += len(payload["rows"])
        (destination / path.name).write_text(
            json.dumps(payload, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return rows_in


def _timed(runner, bundle, repeat: int) -> list[float]:
    runs = []
    for _ in range(repeat):
        started = time.monotonic()
        report = runner(bundle)
        runs.append(round(time.monotonic() - started, 4))
        if report.operational_failure:
            raise RuntimeError(f"{report.backend}: {report.operational_failure}: {report.message[:400]}")
    return runs


def bench(args: argparse.Namespace) -> int:
    from capcov.claims.differential import run_souffle, run_souffle_compiled

    out_dir = Path(args.out)
    source = Path(args.receipt)
    workspace = Path(tempfile.mkdtemp(prefix="capcov-bench-receipt-"))
    try:
        try:
            rows_in = _scaled_receipt(source, workspace / "receipt", args.scale)
        except OSError as exc:
            raise replay_facts.ExportInputError(
                f"receipt directory could not be read: {source}: {exc}") from exc
        join = _read_join(workspace / "receipt")
        if join.bundle is None:
            print(f"contract finding: {join.contract_findings}", file=sys.stderr)
            return EXIT_CONTRACT
        program = souffle.program_for_pack(join.bundle)
        started = time.monotonic()
        checker = compiled.compile_program(program, executable=args.souffle,
                                           cache_dir=args.cache_dir)
        compile_s = round(time.monotonic() - started, 3)
        cache_hit = checker.cache_hit
        interpreter_runs = _timed(run_souffle, join.bundle, args.repeat)
        compiled_runs = _timed(lambda b: run_souffle_compiled(b, checker=checker),
                               join.bundle, args.repeat)
        interpreted = run_souffle(join.bundle)
        native = run_souffle_compiled(join.bundle, checker=checker)
        document = {
            "schema": BENCH_SCHEMA,
            "fixture": source.name,
            "scale": args.scale,
            "repeat": args.repeat,
            "rows_in": rows_in,
            "relation_count": len(join.bundle.relations),
            "rule_count": len(join.bundle.rules),
            "interpreter": {"runs": interpreter_runs,
                            "median_s": round(statistics.median(interpreter_runs), 4)},
            "compiled": {"runs": compiled_runs,
                         "median_s": round(statistics.median(compiled_runs), 4),
                         "compile_s": compile_s, "cache_hit": cache_hit},
            "souffle": {"path": checker.souffle_path, "sha256": checker.souffle_sha256,
                        "version": checker.souffle_version},
            "binary_sha256": checker.binary_sha256,
            "program_digest": program.program_digest,
            "closures_identical": (interpreted.canonical_digest == native.canonical_digest
                                   and interpreted.closure_digest == native.closure_digest
                                   and interpreted.operational_failure is None),
        }
        _write_json(out_dir / BENCH_FILE, document)
        print(f"scale={args.scale} rows_in={rows_in} "
              f"interpreter_median={document['interpreter']['median_s']}s "
              f"compiled_median={document['compiled']['median_s']}s "
              f"compile={compile_s}s cache_hit={cache_hit} "
              f"closures_identical={document['closures_identical']}")
        return EXIT_OK if document["closures_identical"] else EXIT_KERNEL
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


# ---------------------------------------------------------------------------
# compile


def compile_pack(args: argparse.Namespace) -> int:
    bundle = _pack_bundle(args.pack)
    program = souffle.program_for_pack(bundle)
    checker = compiled.compile_program(program, executable=args.souffle, cache_dir=args.cache_dir)
    document = dict(checker.provenance())
    document["pack"] = args.pack
    document["cache_hit"] = checker.cache_hit
    print(json.dumps(document, indent=1, sort_keys=True))
    return EXIT_OK


# ---------------------------------------------------------------------------
# CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="compiled_checker.py", description=__doc__.splitlines()[0])
    subparsers = parser.add_subparsers(dest="command")

    judge_parser = subparsers.add_parser("judge", help="judge a receipt directory with three kernels")
    judge_parser.add_argument("--receipt", required=True, help="the exported receipt directory")
    judge_parser.add_argument("--out", required=True, help="where judge.json and the certificates land")
    judge_parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR),
                              help="compiled-checker cache directory")
    judge_parser.add_argument("--souffle", default="souffle", help="the souffle executable")
    judge_parser.add_argument("--require-supported", "--require-op", action="append", default=[],
                              metavar="OP", dest="require_supported",
                              help="an op that must be op_qualified supported/complete "
                                   "(repeatable; with none, every replayed op must be)")
    judge_parser.set_defaults(handler=judge)

    bench_parser = subparsers.add_parser("bench", help="time the interpreter against the binary")
    bench_parser.add_argument("--receipt", required=True, help="the receipt to scale")
    bench_parser.add_argument("--out", required=True, help="where bench.json lands")
    bench_parser.add_argument("--scale", type=int, default=1, help="copies of every req-keyed row")
    bench_parser.add_argument("--repeat", type=int, default=3, help="timed runs per kernel")
    bench_parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    bench_parser.add_argument("--souffle", default="souffle")
    bench_parser.set_defaults(handler=bench)

    compile_parser = subparsers.add_parser("compile", help="compile one pack and print its provenance")
    compile_parser.add_argument("--pack", choices=("replay", "static"), required=True)
    compile_parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    compile_parser.add_argument("--souffle", default="souffle")
    compile_parser.set_defaults(handler=compile_pack)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # ``judge`` is the default subcommand: a caller that passes only options
    # (the way the candidate repo's `make judge-compiled` does) judges.
    if not argv or argv[0].startswith("-"):
        argv.insert(0, "judge")
    args = build_parser().parse_args(argv)
    if getattr(args, "scale", 1) < 1 or getattr(args, "repeat", 1) < 1:
        print("--scale and --repeat must be positive", file=sys.stderr)
        return EXIT_KERNEL
    try:
        return int(args.handler(args))
    except souffle.SouffleUnavailable as exc:
        print(f"toolchain unavailable: {exc}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    except compiled.CompileError as exc:
        print(f"compile failed: {str(exc)[-400:]}", file=sys.stderr)
        return EXIT_KERNEL
    except (replay_facts.ExportInputError, json.JSONDecodeError) as exc:
        print(f"contract finding: {exc}", file=sys.stderr)
        return EXIT_CONTRACT
    except OSError as exc:
        # An unreadable receipt is a contract finding, raised as one where the
        # receipt is read.  What is left here is the judge's own I/O -- its
        # cache, its output directory -- which is an unavailable environment,
        # never a finding against the receipt and never a traceback.
        print(f"judge environment unavailable: {exc}", file=sys.stderr)
        return EXIT_UNAVAILABLE


if __name__ == "__main__":
    sys.exit(main())
