"""Crash injection at side-effect boundaries.

A boundary is a call that changes something outside the process: a commit to a
primary store, a put to an object store, a request to an external service.
:class:`Boundaries` numbers each one as it happens, records it in the history,
and can end the process immediately before it or immediately after it.

:func:`crash_points` lists the boundaries of a run from one dry run, and
:func:`explore` replays the run once per crash point, runs recovery as a second
actor, and hands the result to a check.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Iterator, Sequence

from .history import EFFECT_KIND, History, Result


class Crash(BaseException):
    """A killed process.

    It derives from ``BaseException`` so ``except Exception`` in the program
    under test does not catch it, as no handler runs when a process is killed.
    """

    def __init__(self, point: "CrashPoint") -> None:
        super().__init__(f"crash {point.when} boundary {point.index} ({point.store}/{point.label})")
        self.point = point


@dataclass(frozen=True)
class CrashPoint:
    """Die ``before`` or ``after`` the boundary numbered ``index`` (from 1)."""
    index: int
    label: str
    store: str
    when: str

    def __post_init__(self) -> None:
        if self.when not in {"before", "after"}:
            raise ValueError("when must be 'before' or 'after'")


@dataclass(frozen=True)
class RaiseAfter:
    """Perform boundary ``index``, then raise ``error``: the response was lost."""
    index: int
    error: BaseException


@dataclass(frozen=True)
class Effect:
    """One boundary a run crossed, as :attr:`Boundaries.effects` lists it."""
    index: int
    label: str
    store: str
    actor: str
    op: str


class Boundaries:
    """Numbers side effects, records them, and injects what ``plan`` asks for.

    ``plan`` is a :class:`CrashPoint`, a :class:`RaiseAfter`, or several of
    them.  Each entry fires once, at the boundary whose number it names.
    Numbering is per ``Boundaries`` value, so recovery gets its own, with no
    plan, and is never crashed by the plan of the run it recovers.
    """

    def __init__(self, history: History,
                 plan: CrashPoint | RaiseAfter | Iterable[CrashPoint | RaiseAfter] | None = None) -> None:
        self.history = history
        if plan is None:
            entries: tuple[Any, ...] = ()
        elif isinstance(plan, (CrashPoint, RaiseAfter)):
            entries = (plan,)
        else:
            entries = tuple(plan)
        self._plan = {entry.index: entry for entry in entries}
        self._lose_next: tuple[frozenset[str] | None, Callable[[], BaseException]] | None = None
        self.effects: list[Effect] = []
        self.actor = ""
        self.op = ""

    @contextmanager
    def scope(self, actor: str, op: str) -> Iterator["Boundaries"]:
        """Name the actor and operation that instrumented calls are made for."""
        previous = (self.actor, self.op)
        self.actor, self.op = actor, op
        try:
            yield self
        finally:
            self.actor, self.op = previous

    def lose_next(self, error: Callable[[], BaseException],
                  stores: Iterable[str] | None = None) -> None:
        """Lose the response of the next boundary (on one of ``stores``, if given)."""
        self._lose_next = (frozenset(stores) if stores is not None else None, error)

    def effect(self, label: str, call: Callable[[], Any], store: str,
               actor: str | None = None, op: str | None = None) -> Any:
        """Run ``call`` as one numbered side effect."""
        actor = self.actor if actor is None else actor
        op = self.op if op is None else op
        index = len(self.effects) + 1
        self.effects.append(Effect(index, label, store, actor, op))
        planned = self._plan.pop(index, None)
        if isinstance(planned, CrashPoint) and planned.when == "before":
            raise Crash(planned)
        token = self.history.invoke(op, actor, EFFECT_KIND, f"{store}/{label}", attempt=index)
        try:
            value = call()
        except BaseException as exc:
            self.history.complete(token, self.history.classify(exc), error=type(exc).__name__)
            raise
        if isinstance(planned, CrashPoint):
            self.history.complete(token, Result.INFO, error="Crash")
            raise Crash(planned)
        error = planned.error if isinstance(planned, RaiseAfter) else None
        if error is None and self._lose_next is not None:
            stores, make = self._lose_next
            if stores is None or store in stores:
                self._lose_next, error = None, make()
        if error is not None:
            self.history.complete(token, Result.INFO, error=type(error).__name__)
            raise error
        self.history.complete(token, Result.OK)
        return value


class _Instrumented:
    def __init__(self, target: Any, methods: frozenset[str], boundaries: Boundaries,
                 store: str) -> None:
        self._target, self._methods = target, methods
        self._boundaries, self._store = boundaries, store

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._target, name)
        if name not in self._methods:
            return attribute

        def boundary(*args: Any, **kwargs: Any) -> Any:
            return self._boundaries.effect(name, lambda: attribute(*args, **kwargs), self._store)
        return boundary


def instrument(obj: Any, methods: Iterable[str], boundaries: Boundaries, store: str) -> Any:
    """A proxy of ``obj`` whose named methods are boundaries on ``store``.

    The actor and operation recorded are the ones set by :meth:`Boundaries.scope`.
    """
    names = frozenset(methods)
    missing = sorted(name for name in names if not callable(getattr(obj, name, None)))
    if missing:
        raise AttributeError(f"{type(obj).__name__} has no callable {missing}")
    return _Instrumented(obj, names, boundaries, store)


@contextmanager
def boundary_context(label: str, cm: Any, boundaries: Boundaries, store: str) -> Iterator[Any]:
    """Enter ``cm``; make its exit (a transaction commit) one boundary.

    When the block raises, ``cm`` exits with that exception as usual and no
    boundary is counted, because a rolled-back transaction changed nothing.  A
    crash before the commit also exits ``cm`` with the crash, so the open
    transaction is rolled back as the death of the process would roll it back.
    """
    value = cm.__enter__()
    try:
        yield value
    except BaseException as exc:
        if not cm.__exit__(type(exc), exc, exc.__traceback__):
            raise
        return
    exited = False

    def commit() -> Any:
        nonlocal exited
        exited = True
        return cm.__exit__(None, None, None)
    try:
        boundaries.effect(label, commit, store)
    except BaseException as exc:
        if not exited:
            cm.__exit__(type(exc), exc, exc.__traceback__)
        raise


def crash_points(run: Callable[[Boundaries], Any]) -> list[CrashPoint]:
    """Run once with no plan and list ``before`` and ``after`` for every boundary crossed."""
    return _points(run, History)


def _points(run: Callable[[Boundaries], Any], new_history: Callable[[], History]) -> list[CrashPoint]:
    boundaries = Boundaries(new_history())
    run(boundaries)
    points = []
    for effect in boundaries.effects:
        points.append(CrashPoint(effect.index, effect.label, effect.store, "before"))
        points.append(CrashPoint(effect.index, effect.label, effect.store, "after"))
    return points


@dataclass(frozen=True)
class Step:
    """One named step of an operation.  ``run(state, actor, boundaries)``."""
    name: str
    run: Callable[[Any, str, Boundaries], Any]


def run_steps(steps: Sequence[Step], state: Any, actor: str, boundaries: Boundaries) -> None:
    """Run ``steps`` in order for ``actor``.  A :class:`Crash` propagates."""
    for step in steps:
        step.run(state, actor, boundaries)


@dataclass(frozen=True)
class Scenario:
    """A run to explore.

    ``setup(history)`` builds a fresh world.  ``steps`` are run against it by
    ``actor``.  ``settle(state)``, when given, runs between the crash and
    recovery: the place to let delayed work in a fake service land.
    ``new_history`` builds the history of each run; pass one that carries a
    classifier to record known refusals as ``fail``.
    """
    setup: Callable[[History], Any]
    steps: Sequence[Step]
    actor: str = "actor-1"
    recovery_actor: str = "actor-2"
    settle: Callable[[Any], None] | None = None
    new_history: Callable[[], History] = History


@dataclass(frozen=True)
class Exploration:
    """The outcome of one explored run.  ``point`` is ``None`` for the run with no crash."""
    point: CrashPoint | None
    history: History
    result: Any


def explore(scenario: Scenario, recover: Callable[[Any, str, Boundaries], Any],
            check: Callable[[Any, History], Any]) -> list[Exploration]:
    """Crash a fresh run at every crash point, recover with a second actor, then check.

    The first entry is the run with no crash.  ``recover`` runs in every run,
    that one included, so a recovery that damages a healthy run is found.
    ``check(state, history)`` receives a closed history; its return value is
    kept in :attr:`Exploration.result`.
    """
    def dry(boundaries: Boundaries) -> None:
        run_steps(scenario.steps, scenario.setup(boundaries.history), scenario.actor, boundaries)

    explorations = []
    for point in (None, *_points(dry, scenario.new_history)):
        history = scenario.new_history()
        state = scenario.setup(history)
        try:
            run_steps(scenario.steps, state, scenario.actor, Boundaries(history, point))
        except Crash:
            pass
        if scenario.settle is not None:
            scenario.settle(state)
        recover(state, scenario.recovery_actor, Boundaries(history))
        history.close()
        explorations.append(Exploration(point, history, check(state, history)))
    return explorations


__all__ = ["Crash", "CrashPoint", "RaiseAfter", "Effect", "Boundaries", "instrument",
           "boundary_context", "crash_points", "Step", "run_steps", "Scenario",
           "Exploration", "explore"]
