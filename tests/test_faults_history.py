"""The operation history: two records per operation, and ``info`` unless proven otherwise."""
from __future__ import annotations

import hashlib
import json
import unittest

from capcov.faults.crash import Crash, CrashPoint
from capcov.faults.history import (Digest, History, HistoryError, LogicalClock, Result,
                                   value_digest)
from capcov.faults.service import Refused, ResponseLost, classify


class HistoryRecordTest(unittest.TestCase):
    def test_an_operation_writes_an_invocation_and_a_completion(self) -> None:
        history = History()
        token = history.invoke("save:note-1", "actor-1", "save", "note-1")
        history.complete(token, Result.OK, {"saved": True})
        invoke, complete = history.records
        self.assertEqual((invoke.type, invoke.op, invoke.actor, invoke.kind, invoke.target, invoke.attempt),
                         ("invoke", "save:note-1", "actor-1", "save", "note-1", 1))
        self.assertEqual((complete.type, complete.token, complete.result), ("complete", token, "ok"))
        self.assertLess(invoke.at, complete.at)

    def test_values_are_stored_as_sha256_of_canonical_json(self) -> None:
        history = History()
        history.complete(history.invoke("op", "a", "k", "t"), Result.OK, {"b": 1, "a": [2, 3]})
        expected = hashlib.sha256(b'{"a":[2,3],"b":1}').hexdigest()
        self.assertEqual(history.records[1].value, expected)
        self.assertEqual(value_digest({"a": [2, 3], "b": 1}), expected)
        self.assertEqual(value_digest(Digest(expected)), expected)

    def test_attempts_number_from_one_per_operation_name(self) -> None:
        history = History()
        tokens = [history.invoke("save:note-1", "a", "save", "note-1") for _ in range(2)]
        other = history.invoke("save:note-2", "a", "save", "note-2")
        explicit = history.invoke("save:note-1", "a", "save", "note-1", attempt=7)
        attempts = {record.token: record.attempt for record in history.records}
        self.assertEqual([attempts[token] for token in (*tokens, other, explicit)], [1, 2, 1, 7])

    def test_timestamps_are_logical(self) -> None:
        history = History(LogicalClock(10))
        history.complete(history.invoke("op", "a", "k", "t"), Result.OK)
        self.assertEqual([record.at for record in history.records], [11, 12])

    def test_completing_twice_is_refused(self) -> None:
        history = History()
        token = history.invoke("op", "a", "k", "t")
        history.complete(token, Result.OK)
        with self.assertRaises(HistoryError):
            history.complete(token, Result.OK)


class InfoDefaultTest(unittest.TestCase):
    def test_return_completes_ok(self) -> None:
        history = History()
        with history.operation("op", "a", "k", "t"):
            pass
        self.assertEqual(history.operations()[0]["result"], "ok")

    def test_an_exception_is_info_by_default(self) -> None:
        for error in (TimeoutError("timed out"), ResponseLost("no response"), ValueError("bad"),
                      Refused(409)):
            history = History()
            with self.assertRaises(type(error)):
                with history.operation("op", "a", "k", "t"):
                    raise error
            operation = history.operations()[0]
            self.assertEqual(operation["result"], "info", type(error).__name__)
            self.assertEqual(operation["error"], type(error).__name__)

    def test_a_timeout_is_never_fail_even_with_a_classifier(self) -> None:
        history = History(classify=classify)
        with self.assertRaises(TimeoutError):
            with history.operation("op", "a", "k", "t"):
                raise TimeoutError("timed out")
        self.assertEqual(history.operations()[0]["result"], "info")

    def test_a_classifier_marks_a_known_refusal_fail(self) -> None:
        history = History(classify=classify)
        with self.assertRaises(Refused):
            with history.operation("op", "a", "k", "t"):
                raise Refused(409)
        self.assertEqual(history.operations()[0]["result"], "fail")

    def test_a_crash_is_recorded_info_and_still_propagates(self) -> None:
        history = History(classify=classify)
        with self.assertRaises(Crash):
            with history.operation("op", "a", "k", "t"):
                raise Crash(CrashPoint(1, "write", "index", "after"))
        self.assertEqual(history.operations()[0]["result"], "info")

    def test_close_completes_open_invocations_as_info(self) -> None:
        history = History()
        history.invoke("left-open", "a", "k", "t")
        done = history.invoke("done", "a", "k", "t")
        history.complete(done, Result.OK)
        self.assertFalse(history.complete_flag)
        history.close()
        self.assertTrue(history.complete_flag)
        self.assertEqual({op["op"]: op["result"] for op in history.operations()},
                         {"left-open": "info", "done": "ok"})
        with self.assertRaises(HistoryError):
            history.invoke("late", "a", "k", "t")


class HistoryJsonTest(unittest.TestCase):
    def _history(self) -> History:
        history = History()
        with history.operation("save:note-1", "actor-1", "save", "note-1"):
            pass
        history.invoke("save:note-2", "actor-2", "save", "note-2")
        return history

    def test_round_trip_keeps_every_record_and_the_complete_flag(self) -> None:
        for close in (False, True):
            history = self._history()
            if close:
                history.close()
            again = History.from_json(history.to_json())
            self.assertEqual(again.records, history.records)
            self.assertEqual(again.complete_flag, close)
            self.assertEqual(again.to_json(), history.to_json())

    def test_document_is_a_versioned_header_and_one_record_per_line(self) -> None:
        history = self._history()
        lines = history.to_json().splitlines()
        self.assertEqual(json.loads(lines[0]), {"schema_version": 1, "complete": False})
        self.assertEqual(len(lines), 1 + len(history.records))
        self.assertEqual([json.loads(line)["type"] for line in lines[1:]],
                         ["invoke", "complete", "invoke"])

    def test_an_open_history_can_be_closed_after_loading(self) -> None:
        again = History.from_json(self._history().to_json())
        again.close()
        self.assertEqual(again.operations()[1]["result"], "info")

    def test_malformed_documents_are_refused(self) -> None:
        good = self._history().to_json().splitlines()
        cases = {
            "empty": "",
            "wrong version": '{"schema_version": 2, "complete": false}\n',
            "not json": good[0] + "\n{nope\n",
            "unknown field": good[0] + '\n{"type":"invoke","token":1,"at":1,"op":"o","actor":"a",'
                                       '"kind":"k","target":"t","attempt":1,"extra":1}\n',
            "completion without invocation": good[0] + '\n{"type":"complete","token":9,"at":1,'
                                                       '"result":"ok","value":"","error":""}\n',
            "unknown result": good[0] + "\n" + good[1] + '\n{"type":"complete","token":1,"at":2,'
                                                         '"result":"maybe","value":"","error":""}\n',
            "complete with open invocation": '{"schema_version": 1, "complete": true}\n' + good[3] + "\n",
        }
        for name, text in cases.items():
            with self.subTest(name), self.assertRaises(HistoryError):
                History.from_json(text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
