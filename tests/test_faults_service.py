"""Each fault on the menu, checked against the fake service's ground truth."""
from __future__ import annotations

import unittest

from capcov.faults.history import History, value_digest
from capcov.faults.service import (Ambiguous, AmbiguousError, ApplyTwice, DelayApply,
                                   DelayChangeEntry, DuplicateOnConcurrentCreate,
                                   EditBeforeNextWrite, FakeService, LoseResponse, Refuse, Refused,
                                   ResponseLost)

KEY = "visibility/note-1"


class CallerApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = FakeService()

    def test_write_replaces_unconditionally_and_takes_no_precondition(self) -> None:
        self.service.write(KEY, "public", "actor-1")
        self.service.write(KEY, "private", "actor-2")
        self.assertEqual(self.service.read(KEY, "actor-1"), "private")
        with self.assertRaises(TypeError):
            self.service.write(KEY, "public", "actor-1", if_match=1)  # type: ignore[call-arg]

    def test_set_valued_keys(self) -> None:
        self.service.add("tags/note-1", "b", "actor-1")
        self.service.add("tags/note-1", "a", "actor-1")
        self.service.add("tags/note-1", "a", "actor-1")
        self.assertEqual(self.service.read("tags/note-1", "actor-1"), ["a", "b"])
        self.service.remove("tags/note-1", "b", "actor-1")
        self.assertEqual(self.service.read("tags/note-1", "actor-1"), ["a"])

    def test_create_does_not_deduplicate(self) -> None:
        first = self.service.create("label", {"name": "urgent"}, "actor-1")
        second = self.service.create("label", {"name": "urgent"}, "actor-1")
        self.assertNotEqual(first, second)
        self.assertEqual([found[0] for found in self.service.search("label", name="urgent")],
                         [first, second])
        self.assertEqual(self.service.search("label", name="other"), [])

    def test_change_entries_carry_seq_actor_time_key_and_value_digest(self) -> None:
        self.service.write(KEY, "public", "actor-1")
        self.service.write("other", 1, "actor-2")
        read = self.service.changes()
        self.assertTrue(read.complete)
        self.assertEqual([(entry.seq, entry.actor, entry.key) for entry in read.entries],
                         [(1, "actor-1", KEY), (2, "actor-2", "other")])
        seq, actor, at, key, digest = read.entries[0]
        self.assertEqual(digest, value_digest("public"))
        self.assertGreater(at, 0)
        self.assertEqual([entry.key for entry in self.service.changes("other").entries], ["other"])

    def test_ground_truth_links_each_applied_write_to_its_request(self) -> None:
        self.service.read(KEY, "actor-1")
        self.service.write(KEY, "public", "actor-1")
        truth = self.service.ground_truth()
        self.assertEqual(truth.final, {KEY: "public"})
        (applied,) = truth.applied
        request = truth.requests[applied.request - 1]
        self.assertEqual((request.method, request.key, request.value, request.actor),
                         ("write", KEY, "public", "actor-1"))
        self.assertEqual(applied.origin, "request")

    def test_calls_are_recorded_when_a_history_is_given(self) -> None:
        history = History()
        service = FakeService(history.clock, history, name="index")
        service.write(KEY, "public", "actor-1")
        service.inject(Refuse(409))
        with self.assertRaises(Refused):
            service.write(KEY, "private", "actor-1")
        self.assertEqual([(op["op"], op["kind"], op["target"], op["result"])
                          for op in history.operations()],
                         [("index:1", "write", KEY, "ok"), ("index:2", "write", KEY, "fail")])


class FaultMenuTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = FakeService()

    def applied(self) -> list[tuple[int, str, str]]:
        return [(a.request, a.key, a.origin) for a in self.service.ground_truth().applied]

    def test_refuse_applies_nothing(self) -> None:
        self.service.inject(Refuse(429))
        with self.assertRaises(Refused) as raised:
            self.service.write(KEY, "public", "actor-1")
        self.assertEqual(raised.exception.status, 429)
        self.assertEqual(self.applied(), [])
        self.assertEqual(self.service.changes().entries, ())

    def test_lose_response_applies_then_raises(self) -> None:
        self.service.inject(LoseResponse())
        with self.assertRaises(ResponseLost):
            self.service.write(KEY, "public", "actor-1")
        self.assertEqual(self.applied(), [(1, KEY, "request")])
        self.assertEqual(self.service.ground_truth().final, {KEY: "public"})

    def test_delay_apply_lands_on_a_later_advance(self) -> None:
        self.service.inject(DelayApply(3))
        with self.assertRaises(ResponseLost):
            self.service.write(KEY, "public", "actor-1")
        self.assertEqual(self.applied(), [])
        self.service.advance(2)
        self.assertEqual(self.applied(), [])
        self.service.advance(1)
        self.assertEqual(self.applied(), [(1, KEY, "request")])

    def test_heal_releases_a_delayed_write(self) -> None:
        self.service.inject(DelayApply(50))
        with self.assertRaises(ResponseLost):
            self.service.write(KEY, "public", "actor-1")
        self.service.heal()
        self.assertEqual(self.applied(), [(1, KEY, "request")])

    def test_apply_twice_applies_one_request_twice(self) -> None:
        self.service.inject(ApplyTwice())
        self.service.add("tags/note-1", "a", "actor-1")
        self.assertEqual(self.applied(), [(1, "tags/note-1", "request")] * 2)
        self.assertEqual(len(self.service.changes().entries), 2)
        self.assertEqual(self.service.ground_truth().overwrote_unread, ())

    def test_apply_twice_on_create_makes_two_records(self) -> None:
        self.service.inject(ApplyTwice())
        self.service.create("label", {"name": "urgent"}, "actor-1")
        self.assertEqual(len(self.service.search("label", name="urgent")), 2)

    def test_edit_before_next_write_is_an_unread_overwrite(self) -> None:
        self.service.write(KEY, "draft", "actor-1")
        self.assertEqual(self.service.read(KEY, "actor-1"), "draft")
        self.service.inject(EditBeforeNextWrite(KEY, "private", "editor"))
        self.service.write(KEY, "public", "actor-1")
        truth = self.service.ground_truth()
        self.assertEqual([(a.actor, a.origin) for a in truth.applied],
                         [("actor-1", "request"), ("editor", "outside"), ("actor-1", "request")])
        self.assertEqual([(a.seq, a.key) for a in truth.overwrote_unread], [(3, KEY)])
        self.assertEqual(truth.final, {KEY: "public"})

    def test_a_write_over_a_value_the_sender_read_is_not_reported(self) -> None:
        self.service.write(KEY, "draft", "actor-1")
        self.service.read(KEY, "actor-2")
        self.service.write(KEY, "public", "actor-2")
        self.assertEqual(self.service.ground_truth().overwrote_unread, ())

    def test_a_write_over_an_unread_value_is_reported_without_any_fault(self) -> None:
        self.service.write(KEY, "draft", "actor-1")
        self.service.write(KEY, "public", "actor-2")
        self.assertEqual([a.seq for a in self.service.ground_truth().overwrote_unread], [2])

    def test_delay_change_entry_makes_the_log_read_incomplete(self) -> None:
        self.service.inject(DelayChangeEntry(2))
        self.service.write(KEY, "public", "actor-1")
        self.assertEqual(self.applied(), [(1, KEY, "request")])
        read = self.service.changes()
        self.assertEqual((read.entries, read.complete), ((), False))
        self.service.advance(2)
        read = self.service.changes()
        self.assertEqual(([entry.seq for entry in read.entries], read.complete), ([1], True))

    def test_heal_releases_a_withheld_entry(self) -> None:
        self.service.inject(DelayChangeEntry(50))
        self.service.write(KEY, "public", "actor-1")
        self.service.heal()
        self.assertTrue(self.service.changes().complete)
        self.assertEqual(len(self.service.changes().entries), 1)

    def test_ambiguous_error_may_or_may_not_have_applied(self) -> None:
        for landed in (True, False):
            service = FakeService()
            service.inject(AmbiguousError(502, applied=landed))
            with self.assertRaises(Ambiguous) as raised:
                service.write(KEY, "public", "actor-1")
            self.assertEqual(raised.exception.status, 502)
            self.assertEqual(len(service.ground_truth().applied), 1 if landed else 0)

    def test_duplicate_on_concurrent_create_keeps_both_records(self) -> None:
        self.service.inject(DuplicateOnConcurrentCreate("actor-2"))
        created = self.service.create("label", {"name": "urgent"}, "actor-1")
        found = self.service.search("label", name="urgent")
        self.assertEqual(len(found), 2)
        self.assertIn(created, [record_id for record_id, _ in found])
        self.assertEqual([(a.actor, a.origin) for a in self.service.ground_truth().applied],
                         [("actor-1", "request"), ("actor-2", "outside")])

    def test_faults_are_consumed_once_in_queue_order_by_the_call_they_match(self) -> None:
        self.service.inject(Refuse(503), match={"key": "other"})
        self.service.inject(LoseResponse())
        self.assertIsNone(self.service.read(KEY, "actor-1"))
        with self.assertRaises(ResponseLost):
            self.service.write(KEY, "public", "actor-1")
        self.service.write(KEY, "public", "actor-1")
        with self.assertRaises(Refused):
            self.service.write("other", 1, "actor-1")
        self.service.write("other", 1, "actor-1")

    def test_a_fault_can_be_matched_to_reads(self) -> None:
        self.service.inject(Refuse(503), match={"method": "read"})
        self.service.write(KEY, "public", "actor-1")
        with self.assertRaises(Refused):
            self.service.read(KEY, "actor-1")


class LateApplyTest(unittest.TestCase):
    """A delayed write is judged against what its sender had seen when it sent it."""

    def setUp(self) -> None:
        self.service = FakeService()
        self.service.inject(DelayApply(50))
        with self.assertRaises(ResponseLost):
            self.service.write(KEY, "old", "actor-1")

    def overwrote(self) -> list[tuple[int, str]]:
        truth = self.service.ground_truth()
        return [(a.seq, a.actor) for a in truth.overwrote_unread]

    def test_a_late_write_replaces_a_value_its_sender_read_after_sending(self) -> None:
        self.service.write(KEY, "new", "actor-2")
        self.assertEqual(self.service.read(KEY, "actor-1"), "new")
        self.service.heal()
        self.assertEqual(self.service.ground_truth().final, {KEY: "old"})
        self.assertEqual(self.overwrote(), [(2, "actor-1")])

    def test_a_late_write_replaces_its_senders_own_later_write(self) -> None:
        self.service.write(KEY, "new", "actor-1")
        self.service.heal()
        self.assertEqual(self.service.ground_truth().final, {KEY: "old"})
        self.assertEqual(self.overwrote(), [(2, "actor-1")])

    def test_a_late_write_replaces_another_actors_write_its_sender_never_read(self) -> None:
        self.service.write(KEY, "new", "actor-2")
        self.service.heal()
        self.assertEqual(self.overwrote(), [(2, "actor-1")])

    def test_a_late_write_onto_an_unchanged_key_is_not_reported(self) -> None:
        self.service.heal()
        self.assertEqual(self.overwrote(), [])

    def test_a_request_records_what_its_sender_had_seen(self) -> None:
        self.service.write(KEY, "new", "actor-2")
        self.service.read(KEY, "actor-1")
        self.service.write(KEY, "newer", "actor-1")
        self.assertEqual([(r.method, r.actor, r.seen) for r in self.service.ground_truth().requests],
                         [("write", "actor-1", 0), ("write", "actor-2", 0), ("read", "actor-1", 0),
                          ("write", "actor-1", 1)])

    def test_pending_writes_are_listed_and_leave_the_log_read_incomplete(self) -> None:
        truth = self.service.ground_truth()
        self.assertEqual([(r.id, r.key) for r in truth.pending], [(1, KEY)])
        self.assertFalse(self.service.changes().complete)
        self.service.heal()
        self.assertEqual(self.service.ground_truth().pending, ())
        self.assertTrue(self.service.changes().complete)


class ApplyTwiceOverwriteTest(unittest.TestCase):
    def test_the_second_apply_of_one_request_is_not_an_unread_overwrite(self) -> None:
        service = FakeService()
        service.write(KEY, "draft", "actor-2")
        service.inject(ApplyTwice())
        service.write(KEY, "public", "actor-1")
        self.assertEqual([a.seq for a in service.ground_truth().overwrote_unread], [2])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
