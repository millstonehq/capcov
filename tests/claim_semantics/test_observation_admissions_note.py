"""The admissions ledger's ONE tolerance: an optional per-row ``note``.

The ledger's key validation is exact on purpose -- a reviewer's file that
carries a key the exporter does not understand is refused rather than partly
read.  ``note`` is the single documented exception, and these tests pin what it
costs, which is nothing:

* it is validated (a non-empty string) and then DROPPED, so no relation carries
  it, no ``Evidence.source`` names it and no bundle metadata holds it;
* two ledgers that differ only in their notes export the same rows, the same
  ``observation_digest`` and the same ``bundle_digest``, so a note cannot move a
  verdict by any path, including the identity a receipt is bound by;
* every OTHER unknown key is still R-2, and a note that is not a string is R-2.

The point of the key is a reviewer writing down why they admitted something for
the next human to read.  The point of these tests is that the next human is the
only reader it has.
"""
from __future__ import annotations

import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from capcov.claims import canonical_json, validate_bundle
from capcov.claims.observation import observation_facts as facts


def _export_fixture(*args, **kwargs):
    return facts.export_bundle(*args, allow_receipt_admissions=True, **kwargs)

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
AGREE = FIXTURES / "observation_receipt_agree"
MASKED = FIXTURES / "observation_receipt_masked"

NOTE = ("admitted after reading the normalization's source: it sorts a list the "
        "wire order of which nothing observes")


class _Ledger:
    """A committed receipt copied to a tempdir with its admissions ledger edited."""

    def __init__(self, source: Path, edit=None) -> None:
        self.source, self.edit = source, edit

    def __enter__(self) -> Path:
        self._tmp = tempfile.TemporaryDirectory(prefix="capcov-observation-note-")
        root = Path(self._tmp.name) / "receipt"
        shutil.copytree(self.source, root)
        if self.edit is not None:
            path = root / facts.ADMISSIONS_FILE
            document = json.loads(path.read_text(encoding="utf-8"))
            self.edit(document)
            path.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n",
                            encoding="utf-8")
        return root

    def __exit__(self, *exc) -> None:
        self._tmp.cleanup()


def add_masked_admission(document: dict) -> None:
    document["rows"].append({"relation": "masked_difference_admitted",
                             "normalization": "sort-roles", "field_path": "body:/roles"})


def note_every_row(document: dict) -> None:
    for index, row in enumerate(document["rows"]):
        row["note"] = f"{NOTE} ({index})"


def exported(root: Path):
    result = _export_fixture(root)
    return result


class AdmissionNoteTest(unittest.TestCase):
    def test_the_note_is_the_only_optional_row_key(self) -> None:
        self.assertEqual(facts.ADMISSION_ROW_NOTE, "note")
        self.assertEqual(facts.ADMISSION_ROW_KEYS, {
            "policy_admitted": ("relation", "version"),
            "scenario_set_admitted": ("relation", "version"),
            "masked_difference_admitted": ("relation", "normalization", "field_path")})

    def test_a_note_on_every_admission_row_exports_clean(self) -> None:
        def edit(document: dict) -> None:
            add_masked_admission(document)
            note_every_row(document)
        with _Ledger(MASKED, edit) as root:
            result = exported(root)
        self.assertEqual(result.status, facts.STATUS_COMPLETE, "; ".join(result.messages))
        self.assertEqual(validate_bundle(result.bundle), ())
        self.assertEqual(result.counts["policy_admitted"], 1)
        self.assertEqual(result.counts["scenario_set_admitted"], 1)
        self.assertEqual(result.counts["masked_difference_admitted"], 1)

    def test_two_ledgers_differing_only_in_notes_export_the_same_bundle(self) -> None:
        """The note is dropped, so it cannot reach identity -- or anything else."""
        with _Ledger(MASKED, add_masked_admission) as plain_root:
            plain = exported(plain_root)
        with _Ledger(MASKED, lambda d: (add_masked_admission(d), note_every_row(d))) as noted_root:
            noted = exported(noted_root)
        for result in (plain, noted):
            self.assertEqual(result.status, facts.STATUS_COMPLETE, "; ".join(result.messages))
        self.assertEqual(dict(plain.counts), dict(noted.counts))
        self.assertEqual(dict(plain.bundle.metadata)["observation_digest"],
                         dict(noted.bundle.metadata)["observation_digest"])
        self.assertEqual(facts.bundle_digest(plain.bundle), facts.bundle_digest(noted.bundle))
        # Row by row, evidence record by evidence record: identical.
        self.assertEqual(canonical_json(sorted(canonical_json(atom) for atom in plain.bundle.facts)),
                         canonical_json(sorted(canonical_json(atom) for atom in noted.bundle.facts)))
        self.assertEqual(sorted(record.source for record in plain.bundle.evidence),
                         sorted(record.source for record in noted.bundle.evidence))

    def test_the_note_text_appears_nowhere_in_the_exported_bundle(self) -> None:
        with _Ledger(MASKED, lambda d: (add_masked_admission(d), note_every_row(d))) as root:
            result = exported(root)
        self.assertEqual(result.status, facts.STATUS_COMPLETE, "; ".join(result.messages))
        rendered = canonical_json({
            "facts": [canonical_json(atom) for atom in result.bundle.facts],
            "evidence": [canonical_json(record) for record in result.bundle.evidence],
            "metadata": dict(result.bundle.metadata),
        })
        self.assertNotIn("admitted after reading", rendered)
        self.assertNotIn("note", [column.name for relation in result.bundle.relations
                                  for column in relation.columns])

    def test_the_note_changes_no_message_the_exporter_produces(self) -> None:
        with _Ledger(MASKED, add_masked_admission) as plain_root:
            plain = exported(plain_root)
            plain_messages = [m.replace(str(plain_root), "") for m in plain.messages]
        with _Ledger(MASKED, lambda d: (add_masked_admission(d), note_every_row(d))) as noted_root:
            noted = exported(noted_root)
            noted_messages = [m.replace(str(noted_root), "") for m in noted.messages]
        self.assertEqual(plain_messages, noted_messages)

    def test_a_second_unknown_key_is_still_refused(self) -> None:
        def edit(document: dict) -> None:
            document["rows"][0]["note"] = NOTE
            document["rows"][0]["waive"] = "please"
        with _Ledger(AGREE, edit) as root:
            result = exported(root)
        self.assertEqual(result.status, facts.STATUS_INVALID_INPUT)
        self.assertEqual(result.messages, (
            "R-2: observation_admissions.json.rows[0] has unknown keys ['waive']",))

    def test_a_note_that_is_not_a_string_is_refused(self) -> None:
        def edit(document: dict) -> None:
            document["rows"][0]["note"] = {"seen": True}
        with _Ledger(AGREE, edit) as root:
            result = exported(root)
        self.assertEqual(result.status, facts.STATUS_INVALID_INPUT)
        self.assertEqual(result.messages, (
            "R-2: observation_admissions.json.rows[0].note must be a non-empty string",))

    def test_an_empty_note_is_refused_rather_than_carried(self) -> None:
        def edit(document: dict) -> None:
            document["rows"][0]["note"] = ""
        with _Ledger(AGREE, edit) as root:
            result = exported(root)
        self.assertEqual(result.status, facts.STATUS_INVALID_INPUT)
        self.assertIn("rows[0].note must be a non-empty string", result.messages[0])

    def test_a_note_on_a_ledger_reviewing_another_receipt_is_still_stale(self) -> None:
        """The tolerance is one key, not a softer ledger."""
        def edit(document: dict) -> None:
            note_every_row(document)
            document["reviewed_against"]["policy"] = "0" * 64
        with _Ledger(AGREE, edit) as root:
            result = exported(root)
        self.assertEqual(result.status, facts.STATUS_STALE)

    def test_the_documented_ledger_shape_names_the_note_and_its_limits(self) -> None:
        """The key is only safe because it is documented as read by nothing."""
        doc = facts.__doc__ or ""
        self.assertIn("``note``", doc)
        self.assertIn("no relation carries it", doc)
        self.assertIn("every other unknown key is still refused (R-2)", doc)


class AdmissionNoteVerdictTest(unittest.TestCase):
    """A note cannot admit anything: the masked receipt still needs a real row."""

    def _blocking(self, root: Path) -> str | None:
        from capcov.claims.evaluator import evaluate
        from capcov.claims.observation import join as observation_join
        join = observation_join.build(root, fixture=True)
        self.assertIsNotNone(join.bundle, join.contract_findings)
        relations = dict(evaluate(join.bundle).relations)
        blocking = observation_join.blocking_premise(relations, join.run)
        return None if blocking is None else blocking["relation"]

    def test_a_note_describing_a_mask_does_not_admit_it(self) -> None:
        def edit(document: dict) -> None:
            for row in document["rows"]:
                row["note"] = "the sort-roles mask over body:/roles is fine by me"
        with _Ledger(MASKED, edit) as root:
            self.assertEqual(self._blocking(root), "observation_masked_unadmitted")

    def test_the_real_admission_row_is_what_admits_it(self) -> None:
        def edit(document: dict) -> None:
            add_masked_admission(document)
            note_every_row(document)
        with _Ledger(MASKED, edit) as root:
            self.assertIsNone(self._blocking(root))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
