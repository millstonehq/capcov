"""Fault injection for programs that write to more than one store.

The harness is test support, split into five modules:

* ``history``: an operation history with ``ok`` / ``fail`` / ``info`` results.
* ``service``: a fake external service without conditional writes or
  idempotency keys, with ground truth and a fault menu.
* ``crash``: crash injection at numbered side-effect boundaries.
* ``machine``: a schedule enumerator and an optional Hypothesis state machine.
* ``checker``: builds a ``capcov.claims`` bundle from one run and judges it
  with the shipped rule pack ``rules-faults-v1.json``.

Standard library only, apart from the optional ``faults-property`` extra that
``machine.hypothesis_machine`` imports.  Nothing here is imported by
``capcov discover``, ``reconcile`` or ``gate``; ``capcov faults`` loads it on
demand, and the names below resolve their module on first use.
"""
from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "history": ("History", "LogicalClock", "Result", "Digest", "value_digest", "HistoryError"),
    "service": ("FakeService", "ChangeRead", "ChangeEntry", "GroundTruth", "ServiceError", "Refused",
                "ResponseLost", "Ambiguous", "Refuse", "LoseResponse", "DelayApply",
                "ApplyTwice", "EditBeforeNextWrite", "DelayChangeEntry", "AmbiguousError",
                "DuplicateOnConcurrentCreate"),
    "crash": ("Crash", "CrashPoint", "RaiseAfter", "Boundaries", "instrument",
              "boundary_context", "crash_points", "Step", "run_steps", "Scenario",
              "Exploration", "explore"),
    "machine": ("FaultMachine", "FailingSchedule", "enumerate_schedules", "hypothesis_machine",
                "HypothesisUnavailable", "UnexpectedStepError"),
    "checker": ("FinalRead", "Rows", "ExtraFacts", "Finals", "ServiceFacts", "build_bundle", "check",
                "invariant_claim", "invariant_names", "Verdict", "Verdicts",
                "FaultCheckError", "SouffleRequired", "finals_to_json", "finals_from_json"),
}
_HOME = {name: module for module, names in _EXPORTS.items() for name in names}

__all__ = sorted(_HOME)


def __getattr__(name: str) -> Any:
    module = _HOME.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(f"{__name__}.{module}"), name)
