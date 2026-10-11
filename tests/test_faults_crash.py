"""Crash boundaries: numbering, the plan, and exploration of every crash point."""
from __future__ import annotations

import sqlite3
import unittest

from capcov.faults.crash import (Boundaries, Crash, CrashPoint, RaiseAfter, Scenario, Step,
                                 boundary_context, crash_points, explore, instrument, run_steps)
from capcov.faults.history import History
from capcov.faults.service import FakeService, Refused, ResponseLost, classify


class Shelf:
    """A store with one mutating method and one read."""

    def __init__(self) -> None:
        self.items: list[str] = []

    def put(self, item: str) -> int:
        self.items.append(item)
        return len(self.items)

    def count(self) -> int:
        return len(self.items)


def effects(history: History) -> list[tuple[str, str, str]]:
    return [(op["op"], op["target"], op["result"]) for op in history.operations()]


class BoundariesTest(unittest.TestCase):
    def test_effects_are_numbered_and_recorded(self) -> None:
        history = History()
        boundaries = Boundaries(history)
        shelf = Shelf()
        self.assertEqual(boundaries.effect("put", lambda: shelf.put("a"), "shelf", "actor-1", "stock"), 1)
        boundaries.effect("put", lambda: shelf.put("b"), "shelf", "actor-1", "stock")
        self.assertEqual([(e.index, e.label, e.store) for e in boundaries.effects],
                         [(1, "put", "shelf"), (2, "put", "shelf")])
        self.assertEqual(effects(history), [("stock", "shelf/put", "ok")] * 2)
        self.assertEqual({op["kind"] for op in history.operations()}, {"effect"})

    def test_crash_before_skips_the_call_and_records_nothing(self) -> None:
        history, shelf = History(), Shelf()
        boundaries = Boundaries(history, CrashPoint(1, "put", "shelf", "before"))
        with self.assertRaises(Crash):
            boundaries.effect("put", lambda: shelf.put("a"), "shelf", "actor-1", "stock")
        self.assertEqual(shelf.items, [])
        self.assertEqual(history.records, ())

    def test_crash_after_makes_the_call_and_records_info(self) -> None:
        history, shelf = History(), Shelf()
        boundaries = Boundaries(history, CrashPoint(1, "put", "shelf", "after"))
        with self.assertRaises(Crash):
            boundaries.effect("put", lambda: shelf.put("a"), "shelf", "actor-1", "stock")
        self.assertEqual(shelf.items, ["a"])
        self.assertEqual(effects(history), [("stock", "shelf/put", "info")])

    def test_crash_passes_through_except_exception(self) -> None:
        boundaries = Boundaries(History(), CrashPoint(1, "put", "shelf", "after"))

        def program() -> str:
            try:
                boundaries.effect("put", lambda: None, "shelf", "actor-1", "stock")
            except Exception:
                return "handled"
            return "returned"

        with self.assertRaises(Crash):
            program()
        self.assertFalse(issubclass(Crash, Exception))

    def test_raise_after_makes_the_call_then_raises_the_given_exception(self) -> None:
        history, shelf = History(), Shelf()
        boundaries = Boundaries(history, RaiseAfter(1, ResponseLost("no response")))
        with self.assertRaises(ResponseLost):
            boundaries.effect("put", lambda: shelf.put("a"), "shelf", "actor-1", "stock")
        self.assertEqual(shelf.items, ["a"])
        self.assertEqual(effects(history), [("stock", "shelf/put", "info")])

    def test_an_exception_from_the_call_uses_the_history_classifier(self) -> None:
        history = History(classify=classify)
        boundaries = Boundaries(history)

        def refuse() -> None:
            raise Refused(409)

        with self.assertRaises(Refused):
            boundaries.effect("write", refuse, "index", "actor-1", "publish")
        self.assertEqual(effects(history), [("publish", "index/write", "fail")])

    def test_a_plan_entry_fires_once_at_its_own_boundary(self) -> None:
        shelf = Shelf()
        boundaries = Boundaries(History(), [CrashPoint(2, "put", "shelf", "before")])
        boundaries.effect("put", lambda: shelf.put("a"), "shelf", "actor-1", "stock")
        with self.assertRaises(Crash) as raised:
            boundaries.effect("put", lambda: shelf.put("b"), "shelf", "actor-1", "stock")
        self.assertEqual(raised.exception.point.index, 2)
        boundaries.effect("put", lambda: shelf.put("c"), "shelf", "actor-1", "stock")
        self.assertEqual(shelf.items, ["a", "c"])

    def test_lose_next_loses_one_response_on_the_named_store(self) -> None:
        history, shelf = History(), Shelf()
        boundaries = Boundaries(history)
        boundaries.lose_next(lambda: ResponseLost("no response"), ["index"])
        boundaries.effect("put", lambda: shelf.put("a"), "shelf", "actor-1", "stock")
        with self.assertRaises(ResponseLost):
            boundaries.effect("write", lambda: shelf.put("b"), "index", "actor-1", "stock")
        boundaries.effect("write", lambda: shelf.put("c"), "index", "actor-1", "stock")
        self.assertEqual(shelf.items, ["a", "b", "c"])


class InstrumentTest(unittest.TestCase):
    def test_named_methods_become_boundaries_and_others_pass_through(self) -> None:
        history, shelf = History(), Shelf()
        boundaries = Boundaries(history)
        proxy = instrument(shelf, ["put"], boundaries, "shelf")
        with boundaries.scope("actor-1", "stock"):
            self.assertEqual(proxy.put("a"), 1)
        self.assertEqual(proxy.count(), 1)
        self.assertEqual(proxy.items, ["a"])
        self.assertEqual([(op["op"], op["actor"], op["target"]) for op in history.operations()],
                         [("stock", "actor-1", "shelf/put")])

    def test_an_unknown_method_name_is_refused(self) -> None:
        with self.assertRaises(AttributeError):
            instrument(Shelf(), ["missing"], Boundaries(History()), "shelf")

    def test_a_fake_service_method_can_be_a_boundary(self) -> None:
        history = History()
        service = FakeService(history.clock, name="index")
        boundaries = Boundaries(history, CrashPoint(1, "write", "index", "after"))
        proxy = instrument(service, ["write"], boundaries, "index")
        with self.assertRaises(Crash), boundaries.scope("actor-1", "publish"):
            proxy.write("visibility/note-1", "public", "actor-1")
        self.assertEqual(len(service.ground_truth().applied), 1)


class BoundaryContextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript("CREATE TABLE notes (id TEXT PRIMARY KEY);")

    def rows(self) -> list[str]:
        return [row[0] for row in self.db.execute("SELECT id FROM notes ORDER BY id")]

    def insert(self, boundaries: Boundaries, note: str) -> None:
        with boundary_context("commit", self.db, boundaries, "primary") as db:
            db.execute("INSERT INTO notes VALUES (?)", (note,))

    def test_the_commit_is_one_boundary(self) -> None:
        history = History()
        boundaries = Boundaries(history)
        with boundaries.scope("actor-1", "save"):
            self.insert(boundaries, "note-1")
        self.assertEqual(self.rows(), ["note-1"])
        self.assertEqual(effects(history), [("save", "primary/commit", "ok")])

    def test_a_crash_before_the_commit_rolls_the_transaction_back(self) -> None:
        boundaries = Boundaries(History(), CrashPoint(1, "commit", "primary", "before"))
        with self.assertRaises(Crash):
            self.insert(boundaries, "note-1")
        self.assertEqual(self.rows(), [])
        self.assertFalse(self.db.in_transaction)

    def test_a_crash_after_the_commit_keeps_the_rows(self) -> None:
        boundaries = Boundaries(History(), CrashPoint(1, "commit", "primary", "after"))
        with self.assertRaises(Crash):
            self.insert(boundaries, "note-1")
        self.assertEqual(self.rows(), ["note-1"])

    def test_an_error_in_the_block_rolls_back_and_counts_no_boundary(self) -> None:
        boundaries = Boundaries(History())
        with self.assertRaises(sqlite3.IntegrityError):
            with boundary_context("commit", self.db, boundaries, "primary") as db:
                db.execute("INSERT INTO notes VALUES ('note-1')")
                db.execute("INSERT INTO notes VALUES ('note-1')")
        self.assertEqual(self.rows(), [])
        self.assertEqual(boundaries.effects, [])


def stock_steps() -> list[Step]:
    def put(item: str) -> Step:
        def run(shelf: Shelf, actor: str, boundaries: Boundaries) -> None:
            boundaries.effect("put", lambda: shelf.put(item), "shelf", actor, "stock")
        return Step(f"put-{item}", run)
    return [put("a"), put("b"), put("c")]


class ExplorationTest(unittest.TestCase):
    def test_one_dry_run_lists_before_and_after_for_each_boundary(self) -> None:
        points = crash_points(lambda boundaries: run_steps(stock_steps(), Shelf(), "actor-1", boundaries))
        self.assertEqual([(point.index, point.label, point.store, point.when) for point in points],
                         [(1, "put", "shelf", "before"), (1, "put", "shelf", "after"),
                          (2, "put", "shelf", "before"), (2, "put", "shelf", "after"),
                          (3, "put", "shelf", "before"), (3, "put", "shelf", "after")])

    def test_explore_crashes_a_fresh_run_at_every_point_then_recovers_and_checks(self) -> None:
        recovered_by: list[str] = []

        def recover(shelf: Shelf, actor: str, boundaries: Boundaries) -> None:
            recovered_by.append(actor)
            for item in ("a", "b", "c"):
                if item not in shelf.items:
                    boundaries.effect("put", lambda item=item: shelf.put(item), "shelf", actor, "restock")

        def check(shelf: Shelf, history: History) -> tuple[tuple[str, ...], bool]:
            return tuple(sorted(shelf.items)), history.complete_flag

        explorations = explore(Scenario(lambda history: Shelf(), stock_steps()), recover, check)
        self.assertEqual([e.point for e in explorations][0], None)
        self.assertEqual(len(explorations), 7)
        self.assertEqual({e.result for e in explorations}, {(("a", "b", "c"), True)})
        self.assertEqual(set(recovered_by), {"actor-2"})
        after_first = explorations[2]
        self.assertEqual((after_first.point.index, after_first.point.when), (1, "after"))
        self.assertEqual([(op["actor"], op["op"], op["result"]) for op in after_first.history.operations()],
                         [("actor-1", "stock", "info"), ("actor-2", "restock", "ok"),
                          ("actor-2", "restock", "ok")])

    def test_explore_finds_a_recovery_that_repeats_an_effect(self) -> None:
        def recover(shelf: Shelf, actor: str, boundaries: Boundaries) -> None:
            for item in ("a", "b", "c"):
                boundaries.effect("put", lambda item=item: shelf.put(item), "shelf", actor, "restock")

        explorations = explore(Scenario(lambda history: Shelf(), stock_steps()), recover,
                               lambda shelf, history: len(shelf.items) == 3)
        self.assertEqual([e.result for e in explorations], [False, True, False, False, False, False, False])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
