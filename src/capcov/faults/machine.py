"""Schedules of operations, crashes, lost responses and recovery.

:class:`FaultMachine` holds one world and six rules that act on it.  A schedule
is a sequence of rule names.  :func:`enumerate_schedules` tries every schedule
up to a depth, shortest first, with the standard library alone.
:func:`hypothesis_machine` hands the same rules to Hypothesis, which samples
longer schedules and shrinks a failing one.

Invariants are judged once per schedule, in :meth:`FaultMachine.finish`, after
delayed work has landed and recovery has run.  Judging them mid-schedule would
report every in-flight operation as unsettled.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

from .crash import Boundaries, Step
from .history import History
from .service import ResponseLost, ServiceError

RULES = ("start_operation", "run_next_step", "crash", "drop_response",
         "run_recovery", "start_second_actor")


class HypothesisUnavailable(RuntimeError):
    """``hypothesis_machine`` needs the ``faults-property`` extra, which is not installed."""


class UnexpectedStepError(RuntimeError):
    """A step raised an exception the machine does not list in ``expected_errors``.

    The program under test failed on a defect, so the schedule has no verdict.
    The original exception is ``__cause__``.  ``schedule`` holds the rules that
    led to it when :func:`enumerate_schedules` was driving.
    """

    def __init__(self, message: str, schedule: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.schedule = schedule


@dataclass
class _Running:
    actor: str
    op: int
    steps: list[Step]


@dataclass(frozen=True)
class FailingSchedule:
    """A schedule after which :meth:`FaultMachine.finish` reported failures."""
    rules: tuple[str, ...]
    failures: tuple[str, ...]


class FaultMachine:
    """One world and the rules that act on it.

    A subclass supplies the world and the program:

    * :meth:`setup` builds the world.
    * :meth:`operation` returns the steps of one operation.
    * :meth:`recover` runs the program's recovery.
    * :meth:`invariants` returns the names of the invariants that failed.

    Each rule returns ``True`` when it applied and ``False`` when it had
    nothing to act on.  Two operations can interleave in one way: the second
    actor's operation runs, in whole or in part, between two steps of the
    first.  Recovery runs only while no operation is in flight; a program whose
    recovery may run beside a live operation needs a subclass that overrides
    :meth:`run_recovery`.
    """

    actors = ("actor-1", "actor-2")
    recovery_actor = "recovery"
    #: Stores whose responses ``drop_response`` may lose; ``None`` means any store.
    lossy_stores: Iterable[str] | None = None
    #: Exceptions a step may let escape.  One of these ends the operation and
    #: leaves what it did to recovery and the invariants.  Any other exception
    #: is raised as :class:`UnexpectedStepError`.
    expected_errors: tuple[type[BaseException], ...] = (ServiceError,)

    def __init__(self) -> None:
        self.history = self.new_history()
        self.boundaries = Boundaries(self.history)
        self.state = self.setup(self.history)
        self._running: list[_Running] = []
        self._operations = 0
        self._second_started = False
        self._drop_armed = False
        self._finished: tuple[str, ...] | None = None

    # --- what a subclass supplies ------------------------------------------

    def new_history(self) -> History:
        return History()

    def setup(self, history: History) -> Any:
        raise NotImplementedError

    def operation(self, actor: str, number: int) -> Sequence[Step]:
        """The steps of operation ``number`` (from 1), run by ``actor``."""
        raise NotImplementedError

    def recover(self, actor: str, boundaries: Boundaries) -> None:
        raise NotImplementedError

    def settle(self) -> None:
        """Let delayed work land before the final recovery.  Does nothing by default."""

    def lost_response(self) -> BaseException:
        """The exception a caller sees when a response is lost.

        A subclass that returns something other than a ``ServiceError`` adds
        its type to ``expected_errors``.
        """
        return ResponseLost("no response")

    def invariants(self) -> Sequence[str]:
        """Names of the invariants that do not hold.  Empty when all hold."""
        raise NotImplementedError

    # --- rules --------------------------------------------------------------

    def _start(self, actor: str) -> None:
        self._operations += 1
        self._running.append(_Running(actor, self._operations,
                                      list(self.operation(actor, self._operations))))

    def applicable(self, rule: str) -> bool:
        """Whether ``rule`` has something to act on now."""
        if rule not in RULES:
            raise ValueError(f"unknown rule: {rule}")
        if rule in {"start_operation", "run_recovery"}:
            return not self._running
        if rule == "start_second_actor":
            return len(self._running) == 1 and not self._second_started
        if rule == "drop_response":
            return bool(self._running) and not self._drop_armed
        return bool(self._running)

    def start_operation(self) -> bool:
        if not self.applicable("start_operation"):
            return False
        self._start(self.actors[0])
        return True

    def start_second_actor(self) -> bool:
        if not self.applicable("start_second_actor"):
            return False
        self._second_started = True
        self._start(self.actors[1])
        return True

    def run_next_step(self) -> bool:
        if not self.applicable("run_next_step"):
            return False
        running = self._running[-1]
        step = running.steps.pop(0)
        try:
            step.run(self.state, running.actor, self.boundaries)
        except tuple(self.expected_errors):
            # The program let a service error escape: the operation is over, and
            # what it left behind is for recovery and the invariants to judge.
            running.steps.clear()
        except Exception as exc:
            raise UnexpectedStepError(
                f"step {step.name!r} of operation {running.op} ({running.actor}) raised "
                f"{type(exc).__name__}: {exc}") from exc
        if not running.steps:
            self._running.pop()
        return True

    def crash(self) -> bool:
        if not self.applicable("crash"):
            return False
        self._running.pop()
        return True

    def drop_response(self) -> bool:
        if not self.applicable("drop_response"):
            return False
        self._drop_armed = True

        def lose() -> BaseException:
            self._drop_armed = False
            return self.lost_response()
        self.boundaries.lose_next(lose, self.lossy_stores)
        return True

    def run_recovery(self) -> bool:
        if not self.applicable("run_recovery"):
            return False
        self.recover(self.recovery_actor, Boundaries(self.history))
        return True

    # --- end of a schedule ----------------------------------------------------

    def finish(self) -> tuple[str, ...]:
        """End the schedule and return the failed invariants.

        Operations still in flight are abandoned as a crash would abandon them.
        Then :meth:`settle` and recovery run, the history is closed, and
        :meth:`invariants` is asked.  The answer is computed once.
        """
        if self._finished is None:
            self._running.clear()
            self.settle()
            self.recover(self.recovery_actor, Boundaries(self.history))
            self.history.close()
            self._finished = tuple(self.invariants())
        return self._finished


def enumerate_schedules(factory: Callable[[], FaultMachine], depth: int,
                        rules: Sequence[str] = RULES) -> FailingSchedule | None:
    """Try every schedule of at most ``depth`` rules; return the shortest that fails.

    Schedules are tried in order of length, and within a length in the order of
    ``rules``, so the answer is deterministic.  A schedule containing a rule
    that had nothing to act on is skipped, because it equals a shorter one.
    Returns ``None`` when no schedule fails.  A step that raises an exception
    outside the machine's ``expected_errors`` stops the search with
    :class:`UnexpectedStepError`.
    """
    if depth < 0:
        raise ValueError("depth must not be negative")
    unknown = [rule for rule in rules if rule not in RULES]
    if unknown:
        raise ValueError(f"unknown rules: {unknown}")
    frontier: list[tuple[str, ...]] = [()]
    for length in range(depth + 1):
        following: list[tuple[str, ...]] = []
        for schedule in frontier:
            machine = factory()
            try:
                if not all(getattr(machine, rule)() for rule in schedule):
                    continue
            except UnexpectedStepError as exc:
                exc.schedule = schedule
                raise
            failures = machine.finish()
            if failures:
                return FailingSchedule(schedule, tuple(failures))
            if length < depth:
                following.extend((*schedule, rule) for rule in rules)
        frontier = following
    return None


def hypothesis_machine(factory: Callable[[], FaultMachine]) -> type:
    """A Hypothesis ``RuleBasedStateMachine`` subclass driving ``factory()``.

    Run it with ``hypothesis.stateful.run_state_machine_as_test`` or use its
    ``TestCase``.  A rule is offered only while it is applicable, and the
    invariants are asserted in ``teardown``.  Hypothesis is
    imported here and nowhere else; install the ``faults-property`` extra.
    """
    try:
        from hypothesis import stateful
    except ImportError as exc:
        raise HypothesisUnavailable(
            "hypothesis is not installed; install capcov[faults-property]") from exc

    def init(self: Any) -> None:
        stateful.RuleBasedStateMachine.__init__(self)
        self.machine = factory()

    def teardown(self: Any) -> None:
        failures = self.machine.finish()
        if failures:
            raise AssertionError(f"invariants failed: {', '.join(failures)}")

    def make(name: str) -> Any:
        def run(self: Any) -> None:
            getattr(self.machine, name)()
        run.__name__ = name
        return stateful.precondition(lambda self: self.machine.applicable(name))(stateful.rule()(run))

    namespace: dict[str, Any] = {"__init__": init, "teardown": teardown}
    namespace.update((name, make(name)) for name in RULES)
    return type("FaultStateMachine", (stateful.RuleBasedStateMachine,), namespace)


__all__ = ["RULES", "HypothesisUnavailable", "UnexpectedStepError", "FailingSchedule",
           "FaultMachine", "enumerate_schedules", "hypothesis_machine"]
