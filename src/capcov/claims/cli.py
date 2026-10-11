"""Experimental CLI for the claim-semantics workbench (EXPERIMENT-PLAN section 18).

Reached only through the ``experiment`` namespace of ``capcov``::

    capcov experiment claims shen authority (--bundle B.json | --rules PACK.json)
    capcov experiment claims shen evaluate --bundle B.json --relation R --row '[...]'
    capcov experiment claims shen why-not  --bundle B.json --relation R --row '[...]'

Every command prints one JSON document on stdout.  Exit status: 0 when the
authority verdict is ok / the row is derivable / a why-not report was
produced; 1 when the authority verdict is not ok or the row is not
derivable (a semantic answer, not an error); 3 for a named operational
failure of the Shen runtime (``ShenUnavailable`` / ``ShenFailure``), whose
JSON carries ``operational_failure``; 2 for usage errors.

Stage D's model checker (``claims/modelcheck.py``) is reached the same way::

    capcov experiment claims modelcheck --model DIR [--out DIR] [--timeout S] [--keep]

It typechecks the Shen domain model under ``DIR`` (``DIR/shen/load.shen``) and
prints the certificate; with ``--out`` it writes ``modelcheck-certificate.json``,
the transcript, ``model_well_formed.json`` for valid global structure, and
``model_operation_checked.json`` for successful operation-local checks. Exit 0
when global structure is well formed (operation judgements may still fail), 1
when global structure is ill formed, and 3 for a checker or runtime failure.

The assumption registry (``claims/assumptions.py``) is reached the same way::

    capcov experiment claims assumptions registry   --receipt DIR [--out DIR]
    capcov experiment claims assumptions invalidate --receipt DIR --drop ID [--drop ID]

``--evaluator`` chooses the kernels both commands judge with: ``python``
(the default, standard library only), ``souffle``, ``souffle-compiled``, a
comma list of them, or ``all`` for every one whose tool is present.  Naming one
that is not here is a refusal (exit 2) that names the tool and how to install
it; the default needs nothing, so a bare invocation judges in any checkout.

``registry`` judges a replay receipt with the replay join and prints the A2
registry document; ``invalidate`` additionally withdraws each ``--drop``
assumption (an ``asm:`` id or the evidence id of an assumption row -- either
way every record attesting that assumption is withdrawn), prints one A3
document per drop and, with ``--out``, writes ``assumptions.json`` and
``invalidation-<id12>.json`` beside the join's ``receipt.json``.  Exit 0 when
the documents were produced, 2 for a refusal (the exporter refused the receipt,
an unknown id, or a withdrawal that would refute a claim), 3 when the two
kernels disagree on the withdrawn bundle (the replay path is printed).

Two shapes a consumer has to know, stated here because they are the contract:

* stdout is a **wrapper**, not a bare document: ``{"registry": <A2>}`` for
  ``registry`` and ``{"registry": <A2>, "invalidations": [<A3>, ...]}`` for
  ``invalidate``.  ``--out DIR`` writes the bare documents as files.
* the receipt is ``--receipt DIR``, else the directory named by
  ``CAPCOV_REPLAY_RECEIPT_DIR``.  With neither, or with a path that is not a
  receipt directory, the command refuses with exit 2 -- never a traceback.

``--out`` is a **directory** for these two commands (the join's artifacts),
unlike the ``shen`` commands where it is a file, so a refusal or a kernel
mismatch is printed on stdout only and never written into it.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from typing import Any

from pathlib import Path

from .ir import BundleIngestionError, bundle_from_json
from .validation import ValidationError

#: The producer profiles this namespace can run, and the module each one lives
#: in.  Every profile is imported inside the branch that names it and nowhere
#: else, so registering one costs nothing: a checkout without the module, the
#: key or the runtime it needs still parses and runs every other command, and a
#: caller who never names the profile never loads it.  ``shen`` is the semantic
#: workbench and ``modelcheck`` Stage D's typed checker.
PROFILE_MODULES = {
    "shen": "capcov.claims.shen",
    "modelcheck": "capcov.claims.modelcheck",
}


def _emit(document: Any, out: str | None) -> None:
    text = json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False)
    if out:
        with open(out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    print(text)


def _load_bundle(path: str):
    with open(path, encoding="utf-8") as handle:
        return bundle_from_json(handle.read())


def _load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _row(text: str) -> list[Any]:
    row = json.loads(text)
    if not isinstance(row, list):
        raise argparse.ArgumentTypeError("--row must be a JSON array")
    return row


def _common(parser: argparse.ArgumentParser, *, need_row: bool) -> None:
    parser.add_argument("--bundle", required=need_row, help="schema-v1 bundle JSON")
    parser.add_argument("--rules", default=None,
                        help="rule pack JSON (authority: the pack to check; evaluate/why-not: must be contained in the bundle)")
    parser.add_argument("--frozen", default=None, help="frozen elaborated-pack checksum the Shen side must reproduce")
    parser.add_argument("--timeout", type=float, default=None, help="hard per-call timeout in seconds (default 60)")
    parser.add_argument("--keep", action="store_true", help="keep the generated driver directory")
    parser.add_argument("--out", default=None, help="also write the JSON document to this file")
    if need_row:
        parser.add_argument("--relation", required=True)
        parser.add_argument("--row", required=True, type=_row, help="JSON array of the conclusion row")
        # the defaults live in ``static.certificate`` and are read once the shen
        # profile is imported, so building the parser imports nothing
        parser.add_argument("--max-depth", type=int, default=None)
        parser.add_argument("--max-nodes", type=int, default=None)


#: Names the receipt directory when ``--receipt`` is not given.
RECEIPT_DIR_ENV = "CAPCOV_REPLAY_RECEIPT_DIR"


def _is_receipt_dir(path: Path) -> bool:
    """A receipt is a directory carrying ``receipt.json`` -- not a file, not an empty directory."""
    return path.is_dir() and (path / "receipt.json").is_file()


def _assumptions(args: argparse.Namespace) -> int:
    """``claims assumptions registry|invalidate`` over a replay receipt directory."""
    import shutil
    import tempfile

    from .assumptions import InvalidationError
    from . import differential as differential_mod
    from .differential import DifferentialMismatch, EvaluatorMismatch
    from .replay import join as replay_join

    # --out names a DIRECTORY here (the join's artifacts), so every document
    # below is printed and never written to it -- writing a refusal to --out
    # raised IsADirectoryError on top of the refusal it was trying to report.
    def emit(document: dict[str, Any]) -> None:
        _emit(document, None)

    named = args.receipt or os.environ.get(RECEIPT_DIR_ENV)
    receipt = Path(named) if named else None
    if receipt is None or not _is_receipt_dir(receipt):
        emit({"refusal": "no receipt directory (pass --receipt DIR)"})
        return 2
    # the evaluators are resolved and checked before the join is reached, so an
    # absent souffle is named as an absent souffle wherever the command runs
    try:
        evaluators = differential_mod.resolve_evaluators(args.evaluator)
        differential_mod.require_evaluators(evaluators)
    except (differential_mod.UnknownEvaluator, differential_mod.EvaluatorUnavailable) as exc:
        emit({"refusal": str(exc)})
        return 2
    owned = args.replay_root is None
    replay_root = args.replay_root or tempfile.mkdtemp(prefix="capcov-assumptions-")
    # a kernel disagreement is the one outcome whose evidence lives in the
    # replay root, so that is the one case an owned temporary root survives
    keep = False
    try:
        admissions = ()
        if args.reviewer_admissions:
            try:
                admissions = _load_json(args.reviewer_admissions)
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                emit({"refusal": "reviewer admissions are not readable JSON"})
                return 2
        try:
            join = replay_join.build(receipt, reviewer_admissions=admissions)
            if join.bundle is None:
                emit({"refusal": "the exporter refused the receipt",
                      "contract_findings": list(join.contract_findings)})
                return 2
            replay_join.evaluate_join(join, replay_root, evaluators=evaluators)
            if join.mismatch is not None:
                keep = True
                emit({"kernel_mismatch": "the kernels disagree on the join",
                      "replay": str(join.mismatch.replay_path)})
                return 3
            document: dict[str, Any] = {"registry": replay_join.assumption_registry(join)}
            if args.command == "invalidate":
                document["invalidations"] = [replay_join.invalidate(join, drop, replay_root).as_dict()
                                             for drop in args.drop]
        except (DifferentialMismatch, EvaluatorMismatch) as exc:
            keep = True
            emit({"kernel_mismatch": "the kernels disagree on the withdrawn bundle",
                  "replay": str(exc.result.replay_path)})
            return 3
        except AssertionError as exc:
            # certify_claims: the kernels agree on the rows but not on the claim
            # rows or the certificates of the withdrawn bundle -- a judge with
            # two answers has none, so this is exit 3 like any other mismatch
            keep = True
            emit({"kernel_mismatch": "the kernels disagree on the claims of the withdrawn bundle",
                  "error": str(exc), "replay": replay_root})
            return 3
        except (InvalidationError, ValidationError) as exc:
            emit({"refusal": str(exc)})
            return 2
        if args.out:
            replay_join.write_artifacts(join, Path(args.out))
        emit(document)
        return 0
    finally:
        if owned and not keep:
            shutil.rmtree(replay_root, ignore_errors=True)


class _ProfileUnavailable(RuntimeError):
    """A producer profile's module could not be imported."""


def _profile(name: str):
    """Import the module a producer profile lives in, or say which one is missing.

    A profile that is not part of this checkout is a named refusal, never an
    ImportError traceback: the namespace is a workbench, and a missing optional
    profile says nothing about the profiles that are here.
    """
    try:
        return importlib.import_module(PROFILE_MODULES[name])
    except ImportError as exc:
        raise _ProfileUnavailable(
            f"the {name} profile is not available in this checkout "
            f"({PROFILE_MODULES[name]}: {exc})") from exc


def _modelcheck(args) -> int:
    try:
        modelcheck = _profile("modelcheck")
    except _ProfileUnavailable as exc:
        _emit({"operational_failure": "profile-unavailable", "error": str(exc)}, None)
        return 3
    if args.keep:
        # Preserve the explicit scratch-directory debugging mode.  The normal
        # producer-profile path uses preflight below, which has a stable
        # never-raises contract for callers that are gating a command.
        try:
            result = modelcheck.check(args.model, out_dir=args.out,
                                      timeout=args.timeout, keep=True)
        except modelcheck.ModelcheckUnavailable as exc:
            _emit({"operational_failure": "modelcheck-unavailable", "error": str(exc)}, None)
            return 3
        except modelcheck.ModelcheckFailure as exc:
            _emit({"operational_failure": "modelcheck-failure", "error": str(exc)}, None)
            return 3
        report = {"status": result.status, "model": result.model_digest,
                  "fact": result.fact["rows"][0] if result.fact else None,
                  "certificate_sha256": result.certificate["certificate_sha256"],
                  "failures": [{"id": j.id, "message": j.message} for j in result.failures],
                  "error": None, "workdir_retained": result.workdir is not None}
    else:
        report = modelcheck.preflight(args.model, out_dir=args.out, timeout=args.timeout)
    if report["status"] in {"unavailable", "failed"}:
        failure = "modelcheck-unavailable" if report["status"] == "unavailable" else "modelcheck-failure"
        _emit({"operational_failure": failure, "error": report["error"]}, None)
        return 3
    document = {"verdict": report["status"], "model": report["model"],
                "failures": list(report["failures"]),
                "certificate_sha256": report["certificate_sha256"],
                "fact": report["fact"],
                "workdir_retained": bool(report.get("workdir_retained", False))}
    _emit(document, None)
    return 0 if report["status"] == "well-formed" else 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="capcov experiment")
    sub = parser.add_subparsers(dest="area", required=True)
    claims = sub.add_parser("claims", help="claim-semantics experiment commands")
    claims_sub = claims.add_subparsers(dest="tool", required=True)
    shen_parser = claims_sub.add_parser("shen", help="executable Shen semantic workbench (section 18)")
    shen_sub = shen_parser.add_subparsers(dest="command", required=True)
    _common(shen_sub.add_parser("authority", help="structural authority checks over a rule pack"), need_row=False)
    _common(shen_sub.add_parser("evaluate", help="search for a derivation and emit its certificate"), need_row=True)
    _common(shen_sub.add_parser("why-not", help="bounded missing-premise alternatives for a row"), need_row=True)
    asm = claims_sub.add_parser("assumptions", help="assumption registry and invalidation over a replay receipt")
    asm_sub = asm.add_subparsers(dest="command", required=True)
    for name, help_text in (("registry", "list every assumption and the claims it carries"),
                            ("invalidate", "withdraw an assumption and report what every claim did")):
        command = asm_sub.add_parser(name, help=help_text)
        command.add_argument("--receipt", default=None, help="replay receipt directory (default: $CAPCOV_REPLAY_RECEIPT_DIR)")
        command.add_argument("--out", default=None, help="write the join artifacts to this directory")
        command.add_argument("--replay-root", default=None, help="differential replay directory for a kernel mismatch")
        command.add_argument(
            "--reviewer-admissions", default=None,
            help="JSON array of external exact-certificate reviewer admissions")
        command.add_argument("--evaluator", default=None, metavar="NAME[,NAME...]",
                             help="kernels to judge with: python (default, stdlib only), souffle, "
                                  "souffle-compiled, a comma list, or 'all' for every one present")
        if name == "invalidate":
            command.add_argument("--drop", action="append", default=[], required=True, metavar="ID",
                                 help="an asm: id or the evidence id of an assumption row (repeatable)")
    mc = claims_sub.add_parser("modelcheck", help="Stage D: typed well-formedness of a Shen domain model")
    mc.add_argument("--model", required=True, help="model directory holding shen/load.shen")
    mc.add_argument("--out", default=None,
                    help="write the certificate, transcript, model_well_formed.json and model_operation_checked.json here")
    mc.add_argument("--timeout", type=float, default=None, help="seconds before the runtime is killed")
    mc.add_argument("--keep", action="store_true", help="keep the generated driver and units")
    args = parser.parse_args(argv)
    if args.tool == "assumptions":
        return _assumptions(args)
    if args.tool == "modelcheck":
        return _modelcheck(args)

    try:
        shen = _profile("shen")
    except _ProfileUnavailable as exc:
        _emit({"operational_failure": "profile-unavailable", "error": str(exc)}, None)
        return 3

    try:
        bundle = _load_bundle(args.bundle) if args.bundle else None
        rules = _load_json(args.rules) if args.rules else None
        if args.command == "authority":
            if bundle is None and rules is None:
                parser.error("authority needs --bundle or --rules")
            report = shen.authority(bundle, rules, frozen=args.frozen, timeout=args.timeout, keep=args.keep)
            _emit(report.as_dict(), args.out)
            return 0 if report.ok else 1
        max_depth = shen.DEFAULT_MAX_DEPTH if getattr(args, "max_depth", None) is None else args.max_depth
        max_nodes = shen.DEFAULT_MAX_NODES if getattr(args, "max_nodes", None) is None else args.max_nodes
        if args.command == "evaluate":
            result = shen.evaluate(bundle, rules, args.relation, args.row, frozen=args.frozen,
                                   max_depth=max_depth, max_nodes=max_nodes,
                                   timeout=args.timeout, keep=args.keep)
            _emit(result.as_dict(), args.out)
            return 0 if result.outcome == "positive" else 1
        report = shen.why_not(bundle, rules, args.relation, args.row, frozen=args.frozen,
                              max_depth=max_depth, max_nodes=max_nodes,
                              timeout=args.timeout, keep=args.keep)
        _emit(report, args.out)
        return 0
    except (BundleIngestionError, ValidationError, OSError, ValueError) as exc:
        _emit({"operational_failure": "invalid-input", "error": str(exc)}, args.out)
        return 3
    except shen.ShenUnavailable as exc:
        _emit({"operational_failure": exc.operational_failure, "error": str(exc)}, args.out)
        return 3
    except shen.ShenFailure as exc:
        _emit({"operational_failure": exc.kind, "error": exc.message,
               "returncode": exc.returncode, "stderr": exc.stderr[-4000:],
               "provenance": exc.provenance.as_dict() if exc.provenance else None}, args.out)
        return 3


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
