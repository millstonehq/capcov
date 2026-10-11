"""The binlog observer turns decoded row images into the delta shape the differ consumes."""

from __future__ import annotations

import unittest
from pathlib import Path

from capcov.vectors.stores.binlog import (
    BinlogObserver,
    BinlogParseError,
    Position,
    RowChanges,
    parse_decoded,
)

FIXTURE = Path(__file__).parent / "fixtures" / "binlog_decoded_sample.txt"
COLUMNS = {
    "app.orders": ["id", "uuid", "code", "state", "modified_by", "modified_at"],
    "app.notes": ["id", "order_id", "body", "author_id", "weight"],
}


class ParseDecodedTests(unittest.TestCase):
    def setUp(self) -> None:
        self.changes = parse_decoded(FIXTURE.read_text(), COLUMNS)

    def test_update_yields_only_the_changed_columns_keyed_by_id(self) -> None:
        orders = self.changes.tables["orders"]
        self.assertEqual(orders["inserted"], [])
        self.assertEqual(orders["deleted"], [])
        self.assertEqual(orders["updated"], [
            {"id": 4122, "changed": {"state": [1, -1], "modified_by": [None, 6]}},
        ])

    def test_insert_and_delete_carry_full_rows_with_decoded_values(self) -> None:
        notes = self.changes.tables["notes"]
        row = {"id": 9001, "order_id": 4122, "body": "it's a probe", "author_id": 6, "weight": 12.5}
        self.assertEqual(notes["inserted"], [row])
        self.assertEqual(notes["deleted"], [row])
        self.assertEqual(notes["updated"], [])

    def test_a_table_without_known_columns_is_reported_not_guessed(self) -> None:
        self.assertEqual(self.changes.unbound_tables, ["shadow"])
        self.assertNotIn("shadow", self.changes.tables)

    def test_statement_count_and_delta_split(self) -> None:
        self.assertEqual(self.changes.statements, 4)
        compared, info = self.changes.delta(informational=("notes",))
        self.assertEqual(sorted(compared), ["mysql.orders"])
        self.assertEqual(sorted(info), ["mysql.notes"])

    def test_bare_table_name_binds_when_schema_qualified_key_is_absent(self) -> None:
        changes = parse_decoded(FIXTURE.read_text(), {"orders": COLUMNS["app.orders"]})
        self.assertIn("orders", changes.tables)
        self.assertEqual(sorted(changes.unbound_tables), ["notes", "shadow"])

    def test_extra_ordinals_beyond_the_column_list_keep_their_ordinal(self) -> None:
        changes = parse_decoded(FIXTURE.read_text(), {"app.notes": ["id", "order_id"]})
        inserted = changes.tables["notes"]["inserted"][0]
        self.assertEqual(inserted["@3"], "it's a probe")

    def test_unbalanced_update_images_are_refused(self) -> None:
        broken = "\n".join([
            "# at 10",
            "### UPDATE `app`.`orders`",
            "### WHERE",
            "###   @1=1 /* INT meta=0 nullable=0 is_null=0 */",
            "# at 20",
        ])
        with self.assertRaisesRegex(BinlogParseError, "1 before and 0 after"):
            parse_decoded(broken, COLUMNS)

    def test_signed_unsigned_ambiguity_takes_the_signed_reading(self) -> None:
        text = "\n".join(["# at 1", "### UPDATE `app`.`orders`", "### WHERE",
                          "###   @1=1 /* INT meta=0 nullable=0 is_null=0 */", "###   @4=1 /* TINYINT meta=0 nullable=0 is_null=0 */",
                          "### SET", "###   @1=1 /* INT meta=0 nullable=0 is_null=0 */", "###   @4=-1 (255) /* TINYINT meta=0 nullable=0 is_null=0 */", "# at 2"])
        changes = parse_decoded(text, COLUMNS)
        self.assertEqual(changes.tables["orders"]["updated"], [{"id": 1, "changed": {"state": [1, -1]}}])

    def test_empty_text_is_no_changes(self) -> None:
        changes = parse_decoded("", COLUMNS)
        self.assertEqual(changes.tables, {})
        self.assertEqual(changes.statements, 0)


class FakeRunner:
    def __init__(self, responses: dict[str, bytes]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []

    def run(self, argv: list[str], input: bytes | None = None) -> bytes:
        self.calls.append(list(argv))
        for needle, payload in self.responses.items():
            if any(needle in part for part in argv):
                return payload
        raise AssertionError(f"unexpected argv {argv}")


class ObserverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = FakeRunner({
            "SHOW MASTER STATUS": b"vectors.000001\t326\t\t\n",
            "information_schema": (
                b"app\torders\tid,uuid,code,state,modified_by,modified_at\n"
                b"app\tnotes\tid,order_id,body,author_id,weight\n"
            ),
            "--start-position": FIXTURE.read_bytes(),
        })
        self.observer = BinlogObserver(self.runner, "app", "/var/lib/mysql/")

    def test_mark_reads_the_current_position(self) -> None:
        self.assertEqual(self.observer.mark(), Position("vectors.000001", 326))

    def test_changes_since_decodes_the_range_and_binds_columns_once(self) -> None:
        changes = self.observer.changes_since(Position("vectors.000001", 326), Position("vectors.000001", 3059))
        self.assertEqual(changes.tables["orders"]["updated"][0]["changed"]["state"], [1, -1])
        decode = [c for c in self.runner.calls if "--start-position=326" in c][0]
        self.assertIn("--stop-position=3059", decode)
        self.assertIn("/var/lib/mysql/vectors.000001", decode)
        self.observer.changes_since(Position("vectors.000001", 326), Position("vectors.000001", 3059))
        schema_reads = [c for c in self.runner.calls if any("information_schema" in p for p in c)]
        self.assertEqual(len(schema_reads), 1)

    def test_same_position_is_no_changes_without_decoding(self) -> None:
        before = len(self.runner.calls)
        changes = self.observer.changes_since(Position("vectors.000001", 326), Position("vectors.000001", 326))
        self.assertIsInstance(changes, RowChanges)
        self.assertEqual(changes.tables, {})
        self.assertEqual(len(self.runner.calls), before)

    def test_rotation_is_refused_loudly(self) -> None:
        with self.assertRaisesRegex(BinlogParseError, "rotated"):
            self.observer.changes_since(Position("vectors.000001", 326), Position("vectors.000002", 4))

    def test_missing_master_status_is_refused(self) -> None:
        runner = FakeRunner({"SHOW MASTER STATUS": b""})
        with self.assertRaisesRegex(BinlogParseError, "log_bin"):
            BinlogObserver(runner, "app", "/x").mark()


if __name__ == "__main__":
    unittest.main()
