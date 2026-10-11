#!/usr/bin/env python3
"""Judge a replay receipt with the claim kernels asked for, up to three of them.

Three subcommands, stdlib and ``capcov`` only:

``judge``    build the receipt's combined bundle, run the evaluators
             ``--evaluator`` names (default ``python``; ``all`` is every one
             whose tool is present, which in the pinned devShell is python /
             interpreted Souffle / compiled Souffle), certify every claim row
             from every closure, and write ``receipt.json``, the per-row
             certificates and ``judge.json``.  ``judge`` is the default, so a
             caller may omit it.

             **A producer repo that gates on three kernels must pass
             ``--evaluator all``** (or the explicit list): the default judges
             with the standard library alone, so a checkout with no Souffle
             still judges rather than failing, and ``judge.json`` records which
             kernels ran (``kernels``) and whether a differential ran at all
             (``differential``) so a smaller run can never be mistaken for a
             passed one.
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
unless every requested kernel agrees.

Asking for a kernel whose tool is absent is exit 4 (``the toolchain is
unavailable``) with the message that names the binary, where it is looked for
and how to install it.  That is this script's documented code for exactly this
condition and a producer repo's build gates on it; the ``capcov`` CLI reports the
same refusal as exit 2, because there it is a command line that does not name a
runnable judge rather than a judgement about a receipt.
"""
from __future__ import annotations

import argparse
import json
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

from capcov.claims import souffle  # noqa: E402
from capcov.claims.replay import judge as replay_judge  # noqa: E402
from capcov.claims.replay import pack as replay_pack  # noqa: E402
from capcov.claims.replay import replay_facts  # noqa: E402
from capcov.claims.souffle import compile as compiled  # noqa: E402

# The judge itself -- the document, the verdict, the exit codes -- moved into
# ``capcov.claims.replay.judge`` so `capcov reconcile --judge claims` and this
# script judge from one implementation.  What stays here is this script's CLI,
# its reader-facing printing, and the two subcommands (bench, compile) that are
# developer tools rather than a judgement.
JUDGE_SCHEMA = replay_judge.JUDGE_SCHEMA
BENCH_SCHEMA = "capcov-compiled-bench-v1"
JUDGE_FILE = replay_judge.JUDGE_FILE
BENCH_FILE = "bench.json"
DEFAULT_CACHE_DIR = replay_judge.DEFAULT_CACHE_DIR
# Receipt relations keyed by ``req``: the rows a synthetic scale-N receipt
# multiplies.  ``model_writes``, ``mutant`` and the reviewer's scope exclusions
# describe the model and the review, not the requests, and are never scaled.
SCALED_RELATIONS = ("replay_request", "php_effect", "go_effect", "php_post_state",
                    "go_post_state", "model_admissible", "model_effect", "mutant_killed")

EXIT_OK = replay_judge.EXIT_OK
EXIT_NOT_SUPPORTED = replay_judge.EXIT_NOT_SUPPORTED
EXIT_KERNEL = replay_judge.EXIT_KERNEL
EXIT_CONTRACT = replay_judge.EXIT_CONTRACT
EXIT_UNAVAILABLE = replay_judge.EXIT_UNAVAILABLE
EXIT_PENDING_PREMISE = replay_judge.EXIT_PENDING_PREMISE

VERDICT_SUPPORTED = replay_judge.VERDICT_SUPPORTED
VERDICT_NOT_SUPPORTED = replay_judge.VERDICT_NOT_SUPPORTED
VERDICT_PENDING_PREMISE = replay_judge.VERDICT_PENDING_PREMISE
VERDICT_KERNEL_MISMATCH = replay_judge.VERDICT_KERNEL_MISMATCH
VERDICT_CONTRACT_FINDING = replay_judge.VERDICT_CONTRACT_FINDING
VERDICT_UNAVAILABLE = replay_judge.VERDICT_UNAVAILABLE

_sha256_json = replay_judge.sha256_json
_write_json = replay_judge.write_json


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


# The document builder, the verdict rules and the exit-code mapping now live in
# ``capcov.claims.replay.judge``; these aliases keep this script's own names.
_op_entry = replay_judge.op_entry
_judge_document = replay_judge.judge_document
_op_is_supported = replay_judge.op_is_supported
_all_pending = replay_judge.all_pending
_unmet_ops = replay_judge.unmet_ops
_read_join = replay_judge.read_join


def _read_reviewer_admissions(path: str | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise replay_facts.ExportInputError(
            "reviewer admissions could not be read as JSON") from exc
    if not isinstance(document, list):
        raise replay_facts.ExportInputError("reviewer admissions must be a JSON array")
    return document


def judge(args: argparse.Namespace) -> int:
    """Judge with the requested kernels and print the script's reader-facing report.

    The judgement is ``replay_judge.judge_receipt``; what is left here is where
    each line goes.  The op lines and the verdict line are printed only once the
    receipt was actually judged -- a contract finding, an unavailable toolchain
    or a kernel disagreement has no ops to report and says so on stderr alone.
    """
    try:
        evaluators = replay_judge.differential.resolve_evaluators(args.evaluator,
                                                                 executable=args.souffle)
    except replay_judge.differential.UnknownEvaluator as exc:
        # argparse's code for a command line that does not name a run; the
        # judge's own codes are about the receipt, and this says nothing about it
        print(f"--evaluator: {exc}", file=sys.stderr)
        return 2
    admissions = _read_reviewer_admissions(args.reviewer_admissions)
    document, diagnostics = replay_judge.judge_receipt(
        Path(args.receipt), Path(args.out), list(dict.fromkeys(args.require_supported)),
        evaluators=evaluators, cache_dir=args.cache_dir, executable=args.souffle,
        reviewer_admissions=admissions)
    if document["verdict"] in replay_judge.JUDGED_VERDICTS:
        for line in replay_judge.summary_lines(document):
            print(line)
    for line in diagnostics:
        print(line, file=sys.stderr)
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
    judge_parser.add_argument("--souffle", default=None,
                              help="the souffle executable (default: $SOUFFLE, then 'souffle')")
    judge_parser.add_argument("--evaluator", default=None, metavar="NAME[,NAME...]",
                              help="which kernels judge: python (default, stdlib only), souffle, "
                                   "souffle-compiled, a comma list, or 'all' for every one "
                                   "present. Two or more run the fail-closed differential")
    judge_parser.add_argument(
        "--reviewer-admissions", default=None,
        help="JSON array of external exact-certificate reviewer admissions")
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
