"""Run the note publisher under every crash point, every fault and bounded schedules.

    python scenario.py [--engine python|both] [--depth N] [--examples N] [--shrink] [--dump DIR]

Exit 0 when every invariant is supported in every run, 1 when any run has a
violated or unresolved invariant (each is printed with the run it failed in).
The explicit enumerator already returns the shortest failing schedule, so
Hypothesis only generates here; ``--shrink`` turns its shrinking on.
``--dump DIR`` writes ``history.json`` and ``finals.json`` of the first failing
run, or of the run with no crash and no fault when nothing fails, in the form
``capcov faults check`` reads.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Callable

from capcov.faults import checker
from capcov.faults.crash import Boundaries, Scenario, explore
from capcov.faults.history import History
from capcov.faults.machine import FaultMachine, enumerate_schedules, hypothesis_machine
from capcov.faults import service as faults

import notes_app
from notes_app import INDEX, OBJECTS, PRIMARY, World

NOTE, BODY = "note-1", "first draft"
VISIBILITY = f"visibility/{NOTE}"

#: Every fault on the menu, by name.  The publisher never calls ``create``, so
#: the last one is queued and never fires.
FAULTS: dict[str, Callable[[], Any]] = {
    "refuse": lambda: faults.Refuse(503),
    "lose-response": faults.LoseResponse,
    "delay-apply": lambda: faults.DelayApply(3),
    "apply-twice": faults.ApplyTwice,
    "edit-before-next-write": lambda: faults.EditBeforeNextWrite(VISIBILITY, "private"),
    "delay-change-entry": lambda: faults.DelayChangeEntry(3),
    "ambiguous-error-applied": lambda: faults.AmbiguousError(500, applied=True),
    "ambiguous-error-not-applied": lambda: faults.AmbiguousError(500, applied=False),
    "duplicate-on-concurrent-create": faults.DuplicateOnConcurrentCreate,
}


def new_history() -> History:
    return History(classify=faults.classify)


def finals(world: World) -> checker.Finals:
    """Final reads of the primary and object stores, and the publisher's own bookkeeping.

    Each read below selects a whole table or the whole object store, which is
    what ``complete=True`` states.
    """
    db = world.db
    notes = {row[0]: {"body": row[1], "state": row[2]}
             for row in db.execute("SELECT id, body, state FROM notes")}
    intents = db.execute("SELECT id, op, store, key, value, status, outcome FROM intents").fetchall()
    return checker.Finals(
        reads=(checker.FinalRead(PRIMARY, notes, complete=True),
               checker.FinalRead(OBJECTS, dict(world.objects.objects), complete=True)),
        intent=checker.Rows(tuple((row[0], row[1], row[2], row[3], json.loads(row[4]))
                                  for row in intents), complete=True),
        recorded=checker.Rows(tuple((row[0], row[6]) for row in intents if row[6] is not None),
                              complete=True),
        settled=checker.Rows(tuple((row[0],) for row in intents if row[5] == "settled"),
                             complete=True),
        overwrite_reported=checker.Rows(tuple(db.execute("SELECT store, seq FROM overwrites")),
                                        complete=True))


def judge(world: World, history: History, engine: str = "python") -> checker.Verdicts:
    return checker.check(checker.build_bundle(history, world.index, finals(world)), engine)


def scenario(fault: Callable[[], Any] | None = None) -> Scenario:
    """Publish one note.  ``fault`` is queued on the index service before the run."""
    def setup(history: History) -> World:
        world = World.new(history)
        if fault is not None:
            world.index.inject(fault())
        return world

    return Scenario(setup, notes_app.publish_steps(NOTE, BODY),
                    settle=lambda world: world.index.heal(), new_history=new_history)


class NotesMachine(FaultMachine):
    """Schedules of publishes, crashes, lost responses and recovery."""
    lossy_stores = (INDEX,)
    engine = "python"

    def new_history(self) -> History:
        return new_history()

    def setup(self, history: History) -> World:
        return World.new(history)

    def operation(self, actor: str, number: int) -> list[Any]:
        return notes_app.publish_steps(f"note-{number}", f"body {number}")

    def recover(self, actor: str, boundaries: Boundaries) -> None:
        notes_app.recover(self.state, actor, boundaries)

    def settle(self) -> None:
        self.state.index.heal()

    def invariants(self) -> list[str]:
        return [verdict.name for verdict in judge(self.state, self.history, self.engine).failed]


class Report:
    def __init__(self, dump: Path | None) -> None:
        self.failures = 0
        self.dump = dump
        self._dumped = False

    def write(self, world: World, history: History) -> None:
        if self.dump is None or self._dumped:
            return
        self._dumped = True
        self.dump.mkdir(parents=True, exist_ok=True)
        (self.dump / "history.json").write_text(history.to_json(), encoding="utf-8")
        (self.dump / "finals.json").write_text(
            checker.finals_to_json(finals(world), world.index), encoding="utf-8")

    def judged(self, where: str, verdicts: checker.Verdicts, world: World, history: History) -> None:
        for verdict in verdicts.failed:
            self.failures += 1
            detail = verdict.violations or verdict.missing_premises
            print(f"FAIL {verdict.name} {verdict.status} [{where}] {list(detail)}")
        if verdicts.failed:
            self.write(world, history)


def run(engine: str, depth: int, examples: int, dump: Path | None, shrink: bool = False) -> int:
    report = Report(dump)
    healthy: tuple[World, History] | None = None
    for name, fault in (("none", None), *FAULTS.items()):
        worlds: list[World] = []

        def check(world: World, history: History) -> checker.Verdicts:
            worlds.append(world)
            return judge(world, history, engine)

        explorations = explore(scenario(fault), notes_app.recover, check)
        for world, exploration in zip(worlds, explorations):
            point = exploration.point
            crash = "none" if point is None else f"{point.when} {point.index} {point.store}/{point.label}"
            report.judged(f"fault={name} crash={crash}", exploration.result, world, exploration.history)
            if healthy is None:
                healthy = (world, exploration.history)
        print(f"ran fault={name}: {len(explorations)} runs")

    NotesMachine.engine = engine
    failing = enumerate_schedules(NotesMachine, depth)
    if failing is not None:
        report.failures += 1
        print(f"FAIL {' '.join(failing.failures)} [schedule={' '.join(failing.rules)}]")
    print(f"ran schedules to depth {depth}")

    from hypothesis import Phase, settings
    from hypothesis.stateful import run_state_machine_as_test
    phases = tuple(Phase) if shrink else (Phase.explicit, Phase.generate)
    try:
        run_state_machine_as_test(
            hypothesis_machine(NotesMachine),
            settings=settings(max_examples=examples, stateful_step_count=12, deadline=None,
                              database=None, derandomize=True, phases=phases))
    except AssertionError as exc:
        report.failures += 1
        print(f"FAIL {exc} [property]")
    print(f"ran {examples} property examples")

    if not report.failures and healthy is not None:
        report.write(*healthy)
    print("all invariants supported" if not report.failures else f"{report.failures} failures")
    return 1 if report.failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--engine", choices=checker.ENGINES, default="python")
    parser.add_argument("--depth", type=int, default=5)
    parser.add_argument("--examples", type=int, default=100)
    parser.add_argument("--shrink", action="store_true",
                        help="let Hypothesis shrink a failing schedule (slower)")
    parser.add_argument("--dump", type=Path, default=None)
    args = parser.parse_args(argv)
    return run(args.engine, args.depth, args.examples, args.dump, args.shrink)


if __name__ == "__main__":
    sys.exit(main())
