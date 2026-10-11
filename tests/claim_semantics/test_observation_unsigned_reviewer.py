"""A placeholder reviewer admits nothing.

The producer's real ledger ships signed ``reviewer: "unassigned"`` with a note
that says an ingesting judge should treat it as unadmitted.  Before this test
the judge did not: any non-empty reviewer string was an admission, so a claim
could derive on a name nobody signed.  Admission is a substantive control, not
a formality -- writing the row takes seconds, deciding whether to trust the
policy is the work -- so an unsigned ledger is handled exactly like an absent
one: no rows, a disclosure message, and the claim left unresolved at
``policy_bound``.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from capcov.claims.observation import observation_facts as facts


def _export_fixture(*args, **kwargs):
    return facts.export_bundle(*args, allow_receipt_admissions=True, **kwargs)

HERE = Path(__file__).resolve().parent
AGREE = HERE / "fixtures" / "observation_receipt_agree"


def _copy_with_reviewer(reviewer: str) -> tempfile.TemporaryDirectory:
    tmp = tempfile.TemporaryDirectory(prefix="capcov-observation-unsigned-")
    root = Path(tmp.name) / "receipt"
    shutil.copytree(AGREE, root)
    path = root / facts.ADMISSIONS_FILE
    document = json.loads(path.read_text(encoding="utf-8"))
    document["reviewer"] = reviewer
    document["producer"] = f"reviewer {reviewer}"
    path.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return tmp


class UnsignedReviewerTest(unittest.TestCase):
    def test_a_signed_ledger_admits(self) -> None:
        result = _export_fixture(AGREE)
        self.assertEqual(result.status, facts.STATUS_COMPLETE, "; ".join(result.messages))
        self.assertEqual(result.counts["policy_admitted"], 1)
        self.assertEqual(result.counts["scenario_set_admitted"], 1)

    def test_every_placeholder_reviewer_admits_nothing(self) -> None:
        for reviewer in sorted(facts.UNSIGNED_REVIEWERS - {""}) + ["Unassigned", "  TBD  "]:
            with self.subTest(reviewer=reviewer):
                with _copy_with_reviewer(reviewer) as tmp:
                    result = _export_fixture(Path(tmp) / "receipt")
                self.assertEqual(result.counts.get("policy_admitted", 0), 0, reviewer)
                self.assertEqual(result.counts.get("scenario_set_admitted", 0), 0, reviewer)
                self.assertEqual(result.counts.get("masked_difference_admitted", 0), 0, reviewer)
                self.assertTrue(any("placeholder" in m and "admits nothing" in m
                                    for m in result.messages),
                                f"no disclosure for {reviewer!r}: {result.messages}")

    def test_an_unsigned_ledger_is_handled_like_an_absent_one(self) -> None:
        """Same rows, same missing premise, only the disclosure sentence differs."""
        with _copy_with_reviewer("unassigned") as tmp:
            unsigned = _export_fixture(Path(tmp) / "receipt")
        tmp2 = tempfile.TemporaryDirectory(prefix="capcov-observation-absent-")
        with tmp2:
            root = Path(tmp2.name) / "receipt"
            shutil.copytree(AGREE, root)
            (root / facts.ADMISSIONS_FILE).unlink()
            absent = _export_fixture(root)
        admitted = ("policy_admitted", "scenario_set_admitted", "masked_difference_admitted")
        self.assertEqual({k: unsigned.counts.get(k, 0) for k in admitted},
                         {k: absent.counts.get(k, 0) for k in admitted})
        self.assertEqual(unsigned.status, absent.status)


if __name__ == "__main__":
    unittest.main()
