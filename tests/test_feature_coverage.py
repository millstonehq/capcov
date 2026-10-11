"""Numeric FODA coverage roll-up: the completeness vector, not one percentage.

Every case is built against the canonical Authentication example (Kang et al.,
FODA, SEI CMU/SEI-90-TR-21): auth(root) -> password(mandatory), mfa(optional);
password -> secondFactor(optional), whose children form an alternative group
{ sms | authenticatorApp | passkey }.
"""

from __future__ import annotations

import unittest

from capcov.features.coverage import rollup
from capcov.features.model import example

VECTOR_FIELDS = (
    "mandatory_covered",
    "mandatory_total",
    "optional_assessed",
    "optional_unassessed",
    "tree_rows",
)


def obligations(**pairs: tuple[int, int]) -> dict:
    """feature -> {covered, total} from feature=(covered, total) kwargs."""
    return {fid: {"covered": c, "total": t} for fid, (c, t) in pairs.items()}


def rows_by_feature(result: dict) -> dict:
    return {row["feature"]: row for row in result["tree_rows"]}


class VectorShapeTests(unittest.TestCase):
    def test_result_is_a_vector_not_one_number(self) -> None:
        result = rollup(example(), obligations(), selected=None)
        for field in VECTOR_FIELDS:
            self.assertIn(field, result)
        # One row per feature, each carrying its own and rolled-up pair.
        self.assertEqual(len(result["tree_rows"]), len(example()["features"]))
        for row in result["tree_rows"]:
            self.assertLessEqual(row["self_covered"], row["self_total"])
            self.assertLessEqual(row["covered"], row["total"])

    def test_rollup_rejects_unknown_features(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown features"):
            rollup(example(), obligations(ghost=(1, 1)), selected=None)
        with self.assertRaisesRegex(ValueError, "unknown"):
            rollup(example(), obligations(), selected={"auth", "ghost"})


class MandatoryGapTests(unittest.TestCase):
    def test_a_mandatory_gap_lowers_the_score(self) -> None:
        full = rollup(
            example(),
            obligations(auth=(1, 1), password=(2, 2)),
            selected=None,
        )
        gapped = rollup(
            example(),
            obligations(auth=(1, 1), password=(1, 2)),  # one Password obligation open
            selected=None,
        )
        # The mandatory skeleton is auth + password; the gap is real and shows.
        self.assertEqual((full["mandatory_covered"], full["mandatory_total"]), (3, 3))
        self.assertEqual((gapped["mandatory_covered"], gapped["mandatory_total"]), (2, 3))
        self.assertLess(
            gapped["mandatory_covered"] / gapped["mandatory_total"],
            full["mandatory_covered"] / full["mandatory_total"],
        )


class UnselectedOptionalTests(unittest.TestCase):
    def test_an_unselected_optional_does_not_drag_the_score(self) -> None:
        selected = {"auth", "password", "secondFactor", "passkey"}  # mfa left out
        result = rollup(
            example(),
            obligations(
                password=(2, 2),
                passkey=(1, 1),
                mfa=(0, 5),  # deeply uncovered, but unselected
            ),
            selected=selected,
        )
        # mfa's five open obligations are excluded from the denominator entirely.
        self.assertEqual((result["mandatory_covered"], result["mandatory_total"]), (3, 3))
        self.assertEqual(rows_by_feature(result)["mfa"]["status"], "deselected")


class AlternativeGroupTests(unittest.TestCase):
    def test_an_alternative_group_counts_only_the_chosen_branch(self) -> None:
        selected = {"auth", "password", "secondFactor", "passkey"}  # passkey chosen
        result = rollup(
            example(),
            obligations(
                passkey=(2, 2),  # the chosen branch
                sms=(0, 3),  # branches not taken -- must not be owed
                authenticatorApp=(0, 3),
            ),
            selected=selected,
        )
        rows = rows_by_feature(result)
        # Only the chosen member is required; the six unchosen obligations vanish.
        self.assertEqual((result["mandatory_covered"], result["mandatory_total"]), (2, 2))
        self.assertEqual(rows["passkey"]["status"], "required")
        self.assertEqual(rows["sms"]["status"], "deselected")
        self.assertEqual(rows["authenticatorApp"]["status"], "deselected")


class OptionalUnassessedTests(unittest.TestCase):
    def test_the_vector_separates_optional_unassessed_from_covered(self) -> None:
        # selected=None: mfa is fully covered, but no one chose it, so its coverage
        # must NOT read as covered -- it is optional, unassessed.
        result = rollup(example(), obligations(mfa=(3, 3)), selected=None)
        rows = rows_by_feature(result)
        # mfa + secondFactor + its three group members are all still open.
        self.assertEqual(result["optional_unassessed"], 5)
        self.assertEqual(result["optional_assessed"], 0)
        self.assertEqual(rows["mfa"]["status"], "unassessed")
        # Its three covered obligations are visible on its own row but never fold
        # into the mandatory numerator -- not silently covered.
        self.assertEqual(rows["mfa"]["self_covered"], 3)
        self.assertEqual(result["mandatory_covered"], 0)

    def test_a_selected_optional_is_assessed_not_mandatory(self) -> None:
        selected = {"auth", "password", "secondFactor", "passkey", "mfa"}
        result = rollup(example(), obligations(mfa=(1, 2)), selected=selected)
        rows = rows_by_feature(result)
        self.assertEqual(rows["mfa"]["status"], "selected")
        self.assertGreaterEqual(result["optional_assessed"], 1)
        self.assertEqual(result["optional_covered"], 1)
        self.assertEqual(result["optional_total"], 2)
        # A selected optional's gap stays out of the mandatory denominator.
        self.assertEqual(result["mandatory_total"], 0)


if __name__ == "__main__":
    unittest.main()
