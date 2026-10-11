"""The schedule enumerator and the optional Hypothesis state machine."""
from __future__ import annotations

import importlib.util
import sys
import unittest
from unittest.mock import patch

from capcov.faults.crash import Boundaries, Step
from capcov.faults.history import History
from capcov.faults.machine import (RULES, FailingSchedule, FaultMachine, HypothesisUnavailable,
                                   UnexpectedStepError, enumerate_schedules, hypothesis_machine)
from capcov.faults.service import ResponseLost

_HAVE_HYPOTHESIS = importlib.util.find_spec("hypothesis") is not None


class Counter:
    """A world: a ledger of intents and a count kept by an outside store."""

    def __init__(self) -> None:
        self.intents: list[str] = []
        self.done: set[str] = set()
        self.sent: list[str] = []


class CounterMachine(FaultMachine):
    """Each operation records an intent, sends it, then marks it done.

    ``careful`` recovery skips an intent that was already sent; the other
    recovery sends every intent not marked done.
    """
    careful = True
    lossy_stores = ("outside",)

    def setup(self, history: History) -> Counter:
        return Counter()

    def operation(self, actor: str, number: int) -> list[Step]:
        intent = f"intent-{number}"

        def record(world: Counter, actor: str, boundaries: Boundaries) -> None:
            world.intents.append(intent)

        def send(world: Counter, actor: str, boundaries: Boundaries) -> None:
            try:
                boundaries.effect("send", lambda: world.sent.append(intent), "outside", actor, intent)
            except ResponseLost:
                return
            world.done.add(intent)

        return [Step("record", record), Step("send", send)]

    def recover(self, actor: str, boundaries: Boundaries) -> None:
        for intent in self.state.intents:
            if intent in self.state.done or (self.careful and intent in self.state.sent):
                continue
            boundaries.effect("send", lambda intent=intent: self.state.sent.append(intent),
                              "outside", actor, intent)
            self.state.done.add(intent)

    def invariants(self) -> list[str]:
        failures = []
        if len(self.state.sent) != len(set(self.state.sent)):
            failures.append("sent_twice")
        if set(self.state.sent) != set(self.state.intents):
            failures.append("intent_not_sent")
        return failures


class CarelessMachine(CounterMachine):
    careful = False


class RuleTest(unittest.TestCase):
    def test_rules_report_whether_they_applied(self) -> None:
        machine = CounterMachine()
        self.assertFalse(machine.run_next_step())
        self.assertFalse(machine.crash())
        self.assertFalse(machine.drop_response())
        self.assertFalse(machine.start_second_actor())
        self.assertTrue(machine.start_operation())
        self.assertFalse(machine.start_operation())
        self.assertFalse(machine.run_recovery())
        self.assertTrue(machine.start_second_actor())
        self.assertFalse(machine.start_second_actor())
        self.assertTrue(machine.run_next_step())
        self.assertEqual(machine.state.intents, ["intent-2"])
        self.assertEqual([rule for rule in RULES if machine.applicable(rule)],
                         ["run_next_step", "crash", "drop_response"])
        with self.assertRaises(ValueError):
            machine.applicable("teleport")

    def test_the_second_actor_runs_between_two_steps_of_the_first(self) -> None:
        machine = CounterMachine()
        machine.start_operation()
        machine.run_next_step()
        machine.start_second_actor()
        while machine.run_next_step():
            pass
        self.assertEqual(machine.state.intents, ["intent-1", "intent-2"])
        self.assertEqual(machine.state.sent, ["intent-2", "intent-1"])
        self.assertEqual(machine.finish(), ())

    def test_finish_abandons_in_flight_work_recovers_and_closes_the_history(self) -> None:
        machine = CounterMachine()
        machine.start_operation()
        machine.run_next_step()
        self.assertEqual(machine.finish(), ())
        self.assertEqual(machine.state.sent, ["intent-1"])
        self.assertTrue(machine.history.complete_flag)
        self.assertEqual(machine.finish(), ())


class EnumeratorTest(unittest.TestCase):
    def test_a_correct_program_has_no_failing_schedule(self) -> None:
        self.assertIsNone(enumerate_schedules(CounterMachine, 5))

    def test_the_shortest_failing_schedule_is_returned(self) -> None:
        failing = enumerate_schedules(CarelessMachine, 6)
        self.assertEqual(failing, FailingSchedule(
            ("start_operation", "run_next_step", "drop_response", "run_next_step"), ("sent_twice",)))

    def test_a_depth_below_the_shortest_failure_finds_nothing(self) -> None:
        self.assertIsNone(enumerate_schedules(CarelessMachine, 3))

    def test_rules_can_be_restricted(self) -> None:
        self.assertIsNone(enumerate_schedules(CarelessMachine, 6,
                                              rules=("start_operation", "run_next_step", "run_recovery")))
        with self.assertRaises(ValueError):
            enumerate_schedules(CarelessMachine, 2, rules=("teleport",))

    def test_the_rule_set_is_the_documented_one(self) -> None:
        self.assertEqual(RULES, ("start_operation", "run_next_step", "crash", "drop_response",
                                 "run_recovery", "start_second_actor"))
        for rule in RULES:
            self.assertTrue(callable(getattr(FaultMachine, rule)))


class HypothesisMachineTest(unittest.TestCase):
    def test_a_missing_hypothesis_is_a_named_error(self) -> None:
        with patch.dict(sys.modules, {"hypothesis": None}):
            with self.assertRaises(HypothesisUnavailable):
                hypothesis_machine(CounterMachine)

    def test_the_module_does_not_import_hypothesis_itself(self) -> None:
        import capcov.faults.machine as machine

        self.assertFalse(hasattr(machine, "hypothesis"))
        self.assertFalse(hasattr(machine, "stateful"))

    @unittest.skipUnless(_HAVE_HYPOTHESIS, "hypothesis (the faults-property extra) is not installed")
    def test_hypothesis_passes_the_correct_program_and_fails_the_careless_one(self) -> None:
        from hypothesis import settings
        from hypothesis.stateful import run_state_machine_as_test

        options = settings(max_examples=100, stateful_step_count=10, deadline=None, database=None,
                           derandomize=True)
        run_state_machine_as_test(hypothesis_machine(CounterMachine), settings=options)
        with self.assertRaises(AssertionError) as raised:
            run_state_machine_as_test(hypothesis_machine(CarelessMachine), settings=options)
        self.assertIn("sent_twice", str(raised.exception))


class BrokenStepMachine(CounterMachine):
    """An operation whose only step fails on a defect before it records anything."""
    error: type[Exception] = AttributeError

    def operation(self, actor: str, number: int) -> list[Step]:
        def broken(world: Counter, actor: str, boundaries: Boundaries) -> None:
            raise self.error("defect")
        return [Step("broken", broken)]


class RefusedStepMachine(BrokenStepMachine):
    error = ResponseLost


class DeclaredStepMachine(BrokenStepMachine):
    expected_errors = (AttributeError,)


class StepErrorTest(unittest.TestCase):
    def test_an_unexpected_exception_from_a_step_is_not_a_passing_schedule(self) -> None:
        with self.assertRaises(UnexpectedStepError) as raised:
            enumerate_schedules(BrokenStepMachine, 4)
        self.assertIsInstance(raised.exception.__cause__, AttributeError)
        self.assertIn("broken", str(raised.exception))
        self.assertEqual(raised.exception.schedule, ("start_operation", "run_next_step"))

    def test_a_service_error_ends_the_operation(self) -> None:
        machine = RefusedStepMachine()
        machine.start_operation()
        self.assertTrue(machine.run_next_step())
        self.assertFalse(machine.applicable("run_next_step"))
        self.assertIsNone(enumerate_schedules(RefusedStepMachine, 3))

    def test_a_subclass_declares_the_errors_it_expects(self) -> None:
        self.assertIsNone(enumerate_schedules(DeclaredStepMachine, 3))

    @unittest.skipUnless(_HAVE_HYPOTHESIS, "hypothesis (the faults-property extra) is not installed")
    def test_hypothesis_fails_on_an_unexpected_exception(self) -> None:
        from hypothesis import settings
        from hypothesis.stateful import run_state_machine_as_test

        options = settings(max_examples=20, stateful_step_count=6, deadline=None, database=None,
                           derandomize=True)
        with self.assertRaises(UnexpectedStepError):
            run_state_machine_as_test(hypothesis_machine(BrokenStepMachine), settings=options)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
