"""Fail-closed differential comparison for the independent claim kernels.

Cross-kernel report contract
----------------------------
An operationally complete report is comparable only when every declared
relation has exactly the same normalized closure and each claim has exactly
the same ``semantic``, ``operational``, ``basis``, and canonical
``missing_premises`` fields.  Those are the admitted fields below.

Invalid or exhausted input has a deliberately narrower contract: only the
exact pair of named top-level ``operational_failure`` values is stable across
backends.  Relations and per-claim payloads may differ (the Python evaluator
retains invalid claim results while Souffle stops at validation), and prose
messages, resources, diagnostics, and provenance are outside this contract.
Any top-level failure remains non-admissible even when both names are
identical: ``compare`` blocks and persists a replay.  The shrinker's
``_difference_shape`` preserves that exact ordered failure pair; it does not
claim message- or resource-stable minimization.

Evaluator selection
-------------------
Which kernels run is the caller's explicit choice (``--evaluator``), never an
availability probe: ``run_evaluators`` runs exactly the evaluators it is handed,
in ``EVALUATORS`` order, and the differential runs only when two or more of them
were asked for.  With one evaluator there is no differential and the result says
so in words (``DIFFERENTIAL_NOT_RUN``) rather than reporting a vacuous match; a
reader of ``judge.json`` can therefore never mistake "one kernel" for "the
kernels agreed".  With two or more, disagreement is fail-closed exactly as
before -- the pairwise python/Souffle ask still goes through ``compare`` and its
shrinker, and the three-kernel ask through ``compare_three``.

``python`` needs nothing but the standard library.  ``souffle`` and
``souffle-compiled`` need the Souffle 2.5 executable (``$SOUFFLE`` or ``souffle``
on PATH); ``require_evaluators`` names it and how to install it before any judging
is done, the way ``capcov discover --resolver scip`` names a missing indexer.
"""
from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .evaluator import ResourceLimits, evaluate
from .ir import Bundle, canonical_dict, canonical_json, digest
from .souffle import SouffleUnavailable, program_for_pack, run_bundle
from .souffle.compile import (CompiledChecker, CompileError, CompiledProgramMismatch,
                              compile_program, run_compiled)
from .validation import ValidationError


# This differential intentionally admits closure plus verdict/status fields.
# Engine-specific provenance, diagnostics, resources, and prose messages are
# outside this checkpoint and are tested against reviewed expectations where
# available rather than pretending Souffle produced them independently.
COMPARABLE_CLAIM_FIELDS = ("semantic", "operational", "basis", "missing_premises")


@dataclass(frozen=True)
class KernelClaim:
    key: str
    index: int
    semantic: str
    operational: str
    basis: str
    missing_premises: tuple[str, ...] = ()


@dataclass(frozen=True)
class KernelReport:
    backend: str
    relations: tuple[tuple[str, tuple[tuple[Any, ...], ...]], ...]
    claims: tuple[KernelClaim, ...]
    operational_failure: str | None = None
    message: str = ""
    # The Souffle kernels' ``SouffleResult.output_digest`` (sha256 over the
    # sorted normalized closure); ``None`` for the Python kernel and for a
    # failed run.  Recorded provenance, outside ``semantic_payload``.
    closure_digest: str | None = None

    def semantic_payload(self) -> tuple[Any, ...]:
        return self.relations, self.claims, self.operational_failure

    @property
    def canonical_digest(self) -> str:
        return digest({"relations": self.relations,
                       "claims": tuple((c.key, c.index, c.semantic, c.operational, c.basis,
                                        c.missing_premises) for c in self.claims),
                       "operational_failure": self.operational_failure})


@dataclass(frozen=True)
class DifferentialResult:
    python: KernelReport
    souffle: KernelReport
    matched: bool
    replay_path: str | None = None
    shrink_steps: int = 0
    shrink_truncated: bool = False
    replay_reproduced: bool = False


class DifferentialMismatch(AssertionError):
    def __init__(self, result: DifferentialResult):
        self.result = result
        location = f"; replay: {result.replay_path}" if result.replay_path else ""
        super().__init__(f"claim kernels disagree{location}")


@dataclass(frozen=True)
class ThreeWayResult:
    """``compare`` (python vs interpreted Souffle) extended by the compiled kernel.

    ``matched`` is ``reports_match(souffle, compiled)``; ``closure_digest_equal``
    is equality of the two Souffle kernels' ``output_digest``.  Both must hold
    for ``compare_three`` to return.  ``timings`` records wall seconds per
    backend (``python``, ``souffle``, ``souffle-compiled``).
    """
    python: KernelReport
    souffle: KernelReport
    compiled: KernelReport
    matched: bool
    closure_digest_equal: bool
    replay_path: str | None = None
    timings: tuple[tuple[str, float], ...] = ()


class CompiledKernelMismatch(AssertionError):
    def __init__(self, result: ThreeWayResult):
        self.result = result
        location = f"; replay: {result.replay_path}" if result.replay_path else ""
        super().__init__(f"compiled Souffle kernel disagrees with the interpreter{location}")


def _rows(rows: Any) -> tuple[tuple[Any, ...], ...]:
    normalized = (tuple(canonical_dict(tuple(row))) for row in rows)
    unique = {canonical_json(row): row for row in normalized}
    return tuple(unique[key] for key in sorted(unique))


def _missing(values: Any) -> tuple[str, ...]:
    # Canonicalize scalars too: the literal string '{"relation":"r"}' must
    # not compare equal to the structured object {"relation": "r"}.
    return tuple(sorted({canonical_json(value) for value in values}))


def run_python(bundle: Bundle, *, limits: ResourceLimits | None = None) -> KernelReport:
    try:
        report = evaluate(bundle, limits)
        relations = tuple(
            (decl.name, _rows(report.relation_rows(decl.name)))
            for decl in bundle.relations)
        claims = tuple(
            KernelClaim(
                entry.claim.id or str(entry.index), entry.index,
                entry.result.semantic.value, entry.result.operational.value,
                entry.result.basis.value,
                _missing(entry.result.missing_premises))
            for entry in report.claims)
        failure = None if report.status.value == "complete" else report.status.value
        return KernelReport("python", relations, claims, failure, report.message)
    except RecursionError as exc:
        return KernelReport(
            "python", (), (), "resource-exhausted",
            str(exc) or "Python kernel exceeded the recursion limit")
    except (OverflowError, TimeoutError) as exc:
        return KernelReport("python", (), (), "resource-exhausted", str(exc))
    except (ValueError, TypeError, UnicodeError) as exc:
        return KernelReport("python", (), (), "python-evaluation-invalid", str(exc))
    except OSError as exc:
        return KernelReport("python", (), (), "python-io-failed", str(exc))


def run_souffle(bundle: Bundle, **kwargs: Any) -> KernelReport:
    try:
        report = run_bundle(
            bundle, outputs=(decl.name for decl in bundle.relations), **kwargs)
        relations = tuple(
            (decl.name, _rows(report.relations.get(decl.name, ())))
            for decl in bundle.relations)
        claims = tuple(
            KernelClaim(
                claim.id or str(index), index, result.semantic.value,
                result.operational.value, result.basis.value,
                _missing(result.missing_premises))
            for index, (claim, result) in enumerate(
                zip(bundle.claims, report.claims)))
        return KernelReport("souffle", relations, claims, closure_digest=report.output_digest)
    except SouffleUnavailable as exc:
        return KernelReport("souffle", (), (), "souffle-unavailable", str(exc))
    except ValidationError as exc:
        return KernelReport("souffle", (), (), "invalid-input", str(exc))
    except RecursionError as exc:
        return KernelReport(
            "souffle", (), (), "resource-exhausted",
            str(exc) or "Souffle kernel boundary exceeded the recursion limit")
    except (OverflowError, TimeoutError) as exc:
        return KernelReport("souffle", (), (), "resource-exhausted", str(exc))
    except NotImplementedError as exc:
        return KernelReport("souffle", (), (), "unsupported-construct", str(exc))
    except RuntimeError as exc:
        return KernelReport("souffle", (), (), "souffle-execution-failed", str(exc))
    except (ValueError, TypeError, UnicodeError) as exc:
        return KernelReport("souffle", (), (), "souffle-translation-or-output-invalid", str(exc))
    except OSError as exc:
        return KernelReport("souffle", (), (), "souffle-io-failed", str(exc))


def _souffle_failure(backend: str, exc: BaseException) -> KernelReport:
    """The interpreter's exception → failure-name mapping, shared by both Souffle kernels."""
    if isinstance(exc, SouffleUnavailable):
        return KernelReport(backend, (), (), "souffle-unavailable", str(exc))
    if isinstance(exc, ValidationError):
        return KernelReport(backend, (), (), "invalid-input", str(exc))
    if isinstance(exc, RecursionError):
        return KernelReport(backend, (), (), "resource-exhausted",
                            str(exc) or f"{backend} kernel boundary exceeded the recursion limit")
    if isinstance(exc, (OverflowError, TimeoutError)):
        return KernelReport(backend, (), (), "resource-exhausted", str(exc))
    if isinstance(exc, NotImplementedError):
        return KernelReport(backend, (), (), "unsupported-construct", str(exc))
    if isinstance(exc, RuntimeError):
        return KernelReport(backend, (), (), "souffle-execution-failed", str(exc))
    if isinstance(exc, (ValueError, TypeError, UnicodeError)):
        return KernelReport(backend, (), (), "souffle-translation-or-output-invalid", str(exc))
    if isinstance(exc, OSError):
        return KernelReport(backend, (), (), "souffle-io-failed", str(exc))
    raise exc


def run_souffle_compiled(bundle: Bundle, *, checker: CompiledChecker | None = None,
                         cache_dir: str | Path = ".capcov/compiled",
                         executable: str = "souffle", **kwargs: Any) -> KernelReport:
    """The compiled Souffle kernel as a ``KernelReport`` (backend ``souffle-compiled``).

    Without ``checker`` the bundle's fact-independent program is compiled (or
    reused from ``cache_dir``) first.  Failures are named: an absent souffle is
    ``souffle-unavailable``, a failed ``souffle -o`` is ``souffle-compile-failed``,
    a checker built from another program is ``compiled-program-mismatch``; the
    rest map exactly as ``run_souffle`` maps them.  A failure is never a
    fallback to the interpreter.
    """
    backend = "souffle-compiled"
    try:
        if checker is None:
            checker = compile_program(program_for_pack(bundle), executable=executable,
                                      cache_dir=cache_dir)
        report = run_compiled(bundle, checker, **kwargs)
        relations = tuple(
            (decl.name, _rows(report.relations.get(decl.name, ())))
            for decl in bundle.relations)
        claims = tuple(
            KernelClaim(
                claim.id or str(index), index, result.semantic.value,
                result.operational.value, result.basis.value,
                _missing(result.missing_premises))
            for index, (claim, result) in enumerate(
                zip(bundle.claims, report.claims)))
        return KernelReport(backend, relations, claims, closure_digest=report.output_digest)
    except CompileError as exc:
        return KernelReport(backend, (), (), "souffle-compile-failed", str(exc))
    except CompiledProgramMismatch as exc:
        return KernelReport(backend, (), (), "compiled-program-mismatch", str(exc))
    except Exception as exc:  # noqa: BLE001 - narrowed by _souffle_failure, which re-raises the rest
        return _souffle_failure(backend, exc)


def reports_match(left: KernelReport, right: KernelReport) -> bool:
    # Every top-level operational failure blocks admission, including an
    # identical named invalid/exhausted result from both implementations.
    if left.operational_failure or right.operational_failure:
        return False
    return left.semantic_payload() == right.semantic_payload()


def _invoke_runner(runner: Callable[[Bundle], KernelReport], bundle: Bundle,
                   backend: str) -> KernelReport:
    """Convert recursion at an injected or normalization boundary to a report."""
    try:
        return runner(bundle)
    except RecursionError as exc:
        return KernelReport(
            backend, (), (), "resource-exhausted",
            str(exc) or f"{backend} kernel boundary exceeded the recursion limit")


def compare(bundle: Bundle, *, python_runner: Callable[[Bundle], KernelReport] = run_python,
            souffle_runner: Callable[[Bundle], KernelReport] = run_souffle,
            replay_root: str | Path = ".capcov/differential", max_steps: int = 200,
            shrink: bool = True) -> DifferentialResult:
    from .shrinker import MAX_SHRINK_STEPS
    if max_steps < 1 or max_steps > MAX_SHRINK_STEPS:
        raise ValueError(f"max_steps must be between 1 and {MAX_SHRINK_STEPS}")

    def guarded_python(candidate: Bundle) -> KernelReport:
        return _invoke_runner(python_runner, candidate, "python")

    def guarded_souffle(candidate: Bundle) -> KernelReport:
        return _invoke_runner(souffle_runner, candidate, "souffle")

    left = guarded_python(bundle)
    right = guarded_souffle(bundle)
    if reports_match(left, right):
        return DifferentialResult(left, right, True)
    replay_path = None; steps = 0; truncated = False
    from .shrinker import shrink_mismatch
    # Semantic and operational disagreements always use the bounded reducer.
    # ``shrink`` remains an input-compatible no-op for callers that used it to
    # avoid work on expected matches; it cannot bypass replay minimization on
    # an actual disagreement.  A bound hit is surfaced as shrink_truncated.
    del shrink
    shrunk = shrink_mismatch(bundle, python_runner=guarded_python,
                             souffle_runner=guarded_souffle,
                             replay_root=replay_root, max_steps=max_steps,
                             baseline_left=left, baseline_right=right)
    replay_path = str(shrunk.path); steps = shrunk.steps; truncated = shrunk.truncated
    result = DifferentialResult(left, right, False, replay_path, steps, truncated,
                                shrunk.reproduced)
    raise DifferentialMismatch(result)


def compare_three(bundle: Bundle, *, checker: CompiledChecker | None = None,
                  replay_root: str | Path = ".capcov/differential", max_steps: int = 200,
                  cache_dir: str | Path = ".capcov/compiled", executable: str = "souffle",
                  python_runner: Callable[[Bundle], KernelReport] = run_python,
                  souffle_runner: Callable[[Bundle], KernelReport] = run_souffle,
                  compiled_runner: Callable[[Bundle], KernelReport] | None = None) -> ThreeWayResult:
    """``compare`` plus the compiled kernel: python, souffle and souffle-compiled must agree.

    The pairwise differential (with its shrinker) runs first and raises
    ``DifferentialMismatch`` as before.  Then the compiled kernel must match
    the interpreter's report *and* its closure digest; on disagreement the
    bundle is persisted to ``<replay_root>/compiled-<bundle digest>.json`` and
    ``CompiledKernelMismatch`` is raised.  There is no three-way shrink: the
    pairwise shrinker stays two-kernel.
    """
    import time
    timings: dict[str, float] = {}

    def timed(name: str, runner: Callable[[Bundle], KernelReport]) -> Callable[[Bundle], KernelReport]:
        def run(candidate: Bundle) -> KernelReport:
            started = time.monotonic()
            try:
                return runner(candidate)
            finally:
                timings[name] = timings.get(name, 0.0) + (time.monotonic() - started)
        return run

    two = compare(bundle, python_runner=timed("python", python_runner),
                  souffle_runner=timed("souffle", souffle_runner),
                  replay_root=replay_root, max_steps=max_steps)
    if compiled_runner is None:
        def compiled_runner(candidate: Bundle) -> KernelReport:
            return run_souffle_compiled(candidate, checker=checker, cache_dir=cache_dir,
                                        executable=executable)
    compiled = _invoke_runner(timed("souffle-compiled", compiled_runner), bundle, "souffle-compiled")
    # Both pairs, not just the Souffle pair: reports_match is transitive over
    # semantic_payload, but stating it keeps the three-way contract explicit
    # if either report ever grows a field canonical_digest does not cover.
    matched = (reports_match(two.souffle, compiled) and reports_match(two.python, compiled)
               and len({two.python.canonical_digest, two.souffle.canonical_digest,
                        compiled.canonical_digest}) == 1)
    closure_equal = (two.souffle.closure_digest is not None
                     and two.souffle.closure_digest == compiled.closure_digest)
    recorded = tuple(sorted(timings.items()))
    if matched and closure_equal:
        return ThreeWayResult(two.python, two.souffle, compiled, True, True, None, recorded)
    root = Path(replay_root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"compiled-{digest(bundle)}.json"
    path.write_text(canonical_json(bundle) + "\n", encoding="utf-8")
    raise CompiledKernelMismatch(ThreeWayResult(two.python, two.souffle, compiled, matched,
                                                closure_equal, str(path), recorded))


# ---------------------------------------------------------------------------
# evaluator selection

#: Every evaluator a caller may ask for, in the order a result reports them.
EVALUATOR_PYTHON = "python"
EVALUATOR_SOUFFLE = "souffle"
EVALUATOR_SOUFFLE_COMPILED = "souffle-compiled"
EVALUATORS = (EVALUATOR_PYTHON, EVALUATOR_SOUFFLE, EVALUATOR_SOUFFLE_COMPILED)
#: The evaluators that need the Souffle executable.
SOUFFLE_EVALUATORS = (EVALUATOR_SOUFFLE, EVALUATOR_SOUFFLE_COMPILED)
#: What runs when nobody asks: the stdlib kernel alone.
DEFAULT_EVALUATORS = (EVALUATOR_PYTHON,)
#: The word that means "every evaluator whose tool is actually here".
EVALUATOR_ALL = "all"

#: ``EvaluationResult.differential`` -- never a bool, because "one kernel" and
#: "the kernels agreed" must not be spellable the same way in an artifact.
DIFFERENTIAL_RAN = "ran"
DIFFERENTIAL_NOT_RUN = "not-run (single evaluator)"

SOUFFLE_INSTALL_HINT = (
    "install Souffle 2.5 (https://souffle-lang.github.io/install) or enter the "
    "pinned devShell with `nix develop`"
)


class UnknownEvaluator(ValueError):
    """A caller named an evaluator that does not exist."""


class EvaluatorUnavailable(RuntimeError):
    """An evaluator was asked for and the executable it needs is not here.

    The sibling of ``capcov.scip.resolve.ScipToolsUnavailable``: raised before
    any work is done, naming the tool, where it is looked for and how to install
    it, so a caller that asked for a kernel gets a reason rather than a silently
    smaller differential.
    """


def souffle_executable(executable: str | None = None) -> str:
    """The Souffle executable to use: the argument, then ``$SOUFFLE``, then ``souffle``."""
    return executable or os.environ.get("SOUFFLE") or "souffle"


def evaluator_available(name: str, *, executable: str | None = None) -> bool:
    """True when ``name`` can run here. Runs nothing; ``python`` is always available."""
    if name == EVALUATOR_PYTHON:
        return True
    if name in SOUFFLE_EVALUATORS:
        return shutil.which(souffle_executable(executable)) is not None
    raise UnknownEvaluator(f"unknown evaluator {name!r}; expected one of {', '.join(EVALUATORS)}")


def available_evaluators(*, executable: str | None = None) -> tuple[str, ...]:
    """Every evaluator whose tool resolves here, in ``EVALUATORS`` order."""
    return tuple(name for name in EVALUATORS if evaluator_available(name, executable=executable))


def resolve_evaluators(value: str | Sequence[str] | None,
                       *, executable: str | None = None) -> tuple[str, ...]:
    """The evaluators ``value`` asks for, deduplicated and in ``EVALUATORS`` order.

    ``None`` is the default (``python`` alone).  A string is a comma list
    (``"python,souffle-compiled"``).  ``"all"`` is every *available* evaluator,
    which is the one spelling that depends on the environment -- and the reason
    it can never fail for an absent tool, while naming ``souffle`` explicitly
    always does.
    """
    if value is None:
        return DEFAULT_EVALUATORS
    tokens = [token.strip() for token in value.split(",")] if isinstance(value, str) else list(value)
    tokens = [token for token in tokens if token]
    if not tokens:
        raise UnknownEvaluator(
            f"no evaluator named; expected {EVALUATOR_ALL} or a comma list of "
            f"{', '.join(EVALUATORS)}")
    if EVALUATOR_ALL in tokens:
        if len(tokens) > 1:
            raise UnknownEvaluator(
                f"{EVALUATOR_ALL!r} already means every available evaluator; it cannot be "
                f"combined with {', '.join(t for t in tokens if t != EVALUATOR_ALL)}")
        return available_evaluators(executable=executable)
    unknown = [token for token in tokens if token not in EVALUATORS]
    if unknown:
        raise UnknownEvaluator(
            f"unknown evaluator {unknown[0]!r}; expected {EVALUATOR_ALL} or a comma list of "
            f"{', '.join(EVALUATORS)}")
    return tuple(name for name in EVALUATORS if name in tokens)


def require_evaluators(names: Sequence[str], *, executable: str | None = None) -> None:
    """Raise ``EvaluatorUnavailable`` naming the first asked-for evaluator that cannot run."""
    for name in names:
        if evaluator_available(name, executable=executable):
            continue
        resolved = souffle_executable(executable)
        raise EvaluatorUnavailable(
            f"--evaluator {name} needs the Souffle 2.5 executable {resolved!r}, which is not "
            f"on PATH or $SOUFFLE. Install it with: {SOUFFLE_INSTALL_HINT}; or use "
            f"--evaluator python")


@dataclass(frozen=True)
class EvaluationResult:
    """What the evaluators a caller asked for reported, and whether they agreed.

    ``reports`` carries one ``KernelReport`` per requested evaluator in
    ``EVALUATORS`` order; ``python``/``souffle``/``compiled`` name them for the
    callers that read a fixed kernel, and are ``None`` for an evaluator that was
    not asked for.  ``differential`` is ``DIFFERENTIAL_RAN`` or
    ``DIFFERENTIAL_NOT_RUN``: with one evaluator ``matched`` is true because
    nothing disagreed, which is *not* the same statement, so the two are
    recorded separately and both are written into ``judge.json``.
    """
    reports: tuple[KernelReport, ...]
    evaluators: tuple[str, ...]
    matched: bool
    differential: str
    closure_digest_equal: bool | None = None
    replay_path: str | None = None
    timings: tuple[tuple[str, float], ...] = ()

    def report_for(self, name: str) -> KernelReport | None:
        return next((report for report in self.reports if report.backend == name), None)

    @property
    def python(self) -> KernelReport | None:
        return self.report_for(EVALUATOR_PYTHON)

    @property
    def souffle(self) -> KernelReport | None:
        return self.report_for(EVALUATOR_SOUFFLE)

    @property
    def compiled(self) -> KernelReport | None:
        return self.report_for(EVALUATOR_SOUFFLE_COMPILED)


class EvaluatorMismatch(AssertionError):
    """The requested evaluators did not produce one admissible answer.

    Two kinds, because a single evaluator can fail too and a failure is not a
    verdict: they disagreed, or one of them reported a named operational failure
    (invalid input, an exhausted bound, a tool that would not run).  Either way
    nothing is certified and the bundle is persisted for replay.
    """

    def __init__(self, result: EvaluationResult):
        self.result = result
        location = f"; replay: {result.replay_path}" if result.replay_path else ""
        failures = [f"{report.backend}: {report.operational_failure}"
                    for report in result.reports if report.operational_failure]
        if failures:
            super().__init__("claim evaluator failed (" + "; ".join(failures) + f"){location}")
        else:
            super().__init__("claim evaluators disagree ("
                             + ", ".join(result.evaluators) + f"){location}")


def _persist(bundle: Bundle, replay_root: str | Path) -> str:
    """Write the bundle a set of evaluators could not answer for, and name the file."""
    root = Path(replay_root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"evaluators-{digest(bundle)}.json"
    path.write_text(canonical_json(bundle) + "\n", encoding="utf-8")
    return str(path)


def _runner_for(name: str, *, checker: CompiledChecker | None, cache_dir: str | Path,
                executable: str) -> Callable[[Bundle], KernelReport]:
    if name == EVALUATOR_PYTHON:
        return run_python
    if name == EVALUATOR_SOUFFLE:
        return lambda candidate: run_souffle(candidate, executable=executable)
    return lambda candidate: run_souffle_compiled(candidate, checker=checker,
                                                  cache_dir=cache_dir, executable=executable)


def run_evaluators(bundle: Bundle, evaluators: Sequence[str] = DEFAULT_EVALUATORS, *,
                   checker: CompiledChecker | None = None,
                   replay_root: str | Path = ".capcov/differential", max_steps: int = 200,
                   cache_dir: str | Path = ".capcov/compiled",
                   executable: str | None = None,
                   runners: Mapping[str, Callable[[Bundle], KernelReport]] | None = None,
                   ) -> EvaluationResult:
    """Run exactly the evaluators asked for; compare them only when there are two or more.

    The two historical asks keep their historical code path, so nothing about
    the differential changes when it is the thing being asked for:
    ``python,souffle`` goes through ``compare`` (and its shrinker, and
    ``DifferentialMismatch``), and ``python,souffle,souffle-compiled`` through
    ``compare_three`` (and ``CompiledKernelMismatch``).  Any other combination --
    including every single-evaluator ask -- is run here and, when there is more
    than one, compared with the same ``reports_match`` plus canonical-digest
    equality the three-way path uses; a disagreement persists the bundle and
    raises ``EvaluatorMismatch``.

    A single evaluator is never silently a passed differential: ``matched`` is
    true (nothing disagreed) but ``differential`` is ``DIFFERENTIAL_NOT_RUN``.
    """
    names = tuple(evaluators)
    if not names:
        raise UnknownEvaluator("at least one evaluator must be requested")
    unknown = [name for name in names if name not in EVALUATORS]
    if unknown:
        raise UnknownEvaluator(f"unknown evaluator {unknown[0]!r}; expected one of "
                               f"{', '.join(EVALUATORS)}")
    names = tuple(name for name in EVALUATORS if name in names)
    resolved_executable = souffle_executable(executable)
    timings: dict[str, float] = {}

    def timed(name: str, runner: Callable[[Bundle], KernelReport]) -> Callable[[Bundle], KernelReport]:
        def run(candidate: Bundle) -> KernelReport:
            started = time.monotonic()
            try:
                return runner(candidate)
            finally:
                timings[name] = timings.get(name, 0.0) + (time.monotonic() - started)
        return run

    def runner(name: str) -> Callable[[Bundle], KernelReport]:
        supplied = (runners or {}).get(name)
        return timed(name, supplied or _runner_for(name, checker=checker, cache_dir=cache_dir,
                                                   executable=resolved_executable))

    if names == (EVALUATOR_PYTHON, EVALUATOR_SOUFFLE):
        two = compare(bundle, python_runner=runner(EVALUATOR_PYTHON),
                      souffle_runner=runner(EVALUATOR_SOUFFLE),
                      replay_root=replay_root, max_steps=max_steps)
        return EvaluationResult((two.python, two.souffle), names, True, DIFFERENTIAL_RAN,
                                None, two.replay_path, tuple(sorted(timings.items())))
    if names == EVALUATORS:
        three = compare_three(bundle, checker=checker, replay_root=replay_root,
                              max_steps=max_steps, cache_dir=cache_dir,
                              executable=resolved_executable,
                              python_runner=runner(EVALUATOR_PYTHON),
                              souffle_runner=runner(EVALUATOR_SOUFFLE),
                              compiled_runner=runner(EVALUATOR_SOUFFLE_COMPILED))
        return EvaluationResult((three.python, three.souffle, three.compiled), names, True,
                                DIFFERENTIAL_RAN, three.closure_digest_equal, three.replay_path,
                                three.timings)
    reports = tuple(_invoke_runner(runner(name), bundle, name) for name in names)
    recorded = tuple(sorted(timings.items()))
    failed = [report for report in reports if report.operational_failure]
    if len(reports) == 1:
        # Nothing disagreed because nothing else ran.  ``matched`` says that and
        # ``differential`` says why, so neither can be read as the other -- and a
        # kernel that failed operationally produced no closure at all, which is
        # not an agreement with itself: it fails closed like any disagreement.
        if not failed:
            return EvaluationResult(reports, names, True, DIFFERENTIAL_NOT_RUN, None, None,
                                    recorded)
        raise EvaluatorMismatch(EvaluationResult(reports, names, False, DIFFERENTIAL_NOT_RUN,
                                                 None, _persist(bundle, replay_root), recorded))
    first = reports[0]
    matched = (not failed
               and all(reports_match(first, other) for other in reports[1:])
               and len({report.canonical_digest for report in reports}) == 1)
    # the closure digest is a Souffle-side artifact: it exists to compare the two
    # Souffle kernels with each other, and is None (unknown, not unequal) when
    # fewer than two of them ran
    souffle_side = [report for report in reports if report.backend in SOUFFLE_EVALUATORS]
    digests = {report.closure_digest for report in souffle_side}
    closure_equal = (None if len(souffle_side) < 2
                     else (len(digests) == 1 and None not in digests))
    if matched and closure_equal is not False:
        return EvaluationResult(reports, names, True, DIFFERENTIAL_RAN, closure_equal, None,
                                recorded)
    raise EvaluatorMismatch(EvaluationResult(reports, names, matched, DIFFERENTIAL_RAN,
                                             closure_equal, _persist(bundle, replay_root),
                                             recorded))


__all__ = ["COMPARABLE_CLAIM_FIELDS", "KernelClaim", "KernelReport", "DifferentialResult",
           "DifferentialMismatch", "ThreeWayResult", "CompiledKernelMismatch",
           "run_python", "run_souffle", "run_souffle_compiled", "reports_match", "compare",
           "compare_three",
           "EVALUATOR_PYTHON", "EVALUATOR_SOUFFLE", "EVALUATOR_SOUFFLE_COMPILED", "EVALUATORS",
           "SOUFFLE_EVALUATORS", "DEFAULT_EVALUATORS", "EVALUATOR_ALL", "DIFFERENTIAL_RAN",
           "DIFFERENTIAL_NOT_RUN", "SOUFFLE_INSTALL_HINT", "UnknownEvaluator",
           "EvaluatorUnavailable", "EvaluatorMismatch", "EvaluationResult", "souffle_executable",
           "evaluator_available", "available_evaluators", "resolve_evaluators",
           "require_evaluators", "run_evaluators"]
