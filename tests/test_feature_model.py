"""FODA feature-model schema, validator, configuration check, coverage rollup.

The canonical worked example (Kang et al., FODA, SEI CMU/SEI-90-TR-21) is
Authentication; every structural rule is exercised against a deliberately
malformed variant of it and asserted to fail with its own message.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path

from capcov.features.cli import main
from capcov.features.model import (
    coverage_rollup,
    example,
    is_valid_configuration,
    validate,
)

# A minimal, always-valid configuration of the canonical example: the root, its
# mandatory Password child, and one alternative second factor.
BASE = {"auth", "password", "secondFactor", "passkey"}


class ValidateTests(unittest.TestCase):
    def test_canonical_example_validates(self) -> None:
        self.assertIsNone(validate(example()))

    def test_version_must_be_one(self) -> None:
        model = example()
        model["version"] = 2
        with self.assertRaisesRegex(ValueError, "version must be 1"):
            validate(model)

    def test_root_and_features_are_required(self) -> None:
        for missing in ("root", "features"):
            with self.subTest(missing=missing):
                model = example()
                model[missing] = None
                with self.assertRaisesRegex(ValueError, "root feature and nonempty features"):
                    validate(model)

    def test_root_must_be_a_declared_feature(self) -> None:
        model = example()
        model["root"] = "ghost"
        with self.assertRaisesRegex(ValueError, "root ghost is not a declared feature"):
            validate(model)

    def test_root_must_not_declare_a_parent(self) -> None:
        model = example()
        model["features"][0]["parent"] = "password"
        with self.assertRaisesRegex(ValueError, "root auth must not declare a parent"):
            validate(model)

    def test_non_root_feature_needs_a_parent(self) -> None:
        model = example()
        del model["features"][1]["parent"]  # Password loses its parent
        with self.assertRaisesRegex(ValueError, "has no parent but is not the root"):
            validate(model)

    def test_unknown_parent_is_rejected(self) -> None:
        model = example()
        model["features"][1]["parent"] = "ghost"
        with self.assertRaisesRegex(ValueError, "references unknown parent ghost"):
            validate(model)

    def test_duplicate_feature_id_is_rejected(self) -> None:
        model = example()
        model["features"].append(copy.deepcopy(model["features"][1]))
        with self.assertRaisesRegex(ValueError, "duplicate feature id: password"):
            validate(model)

    def test_every_feature_needs_id_and_name(self) -> None:
        model = example()
        model["features"][2]["id"] = ""
        with self.assertRaisesRegex(ValueError, "non-empty string id"):
            validate(model)
        model = example()
        del model["features"][2]["name"]
        with self.assertRaisesRegex(ValueError, "requires a name"):
            validate(model)

    def test_cycle_in_the_tree_is_rejected(self) -> None:
        model = example()
        # A loop among non-root features that never reaches the root: password ->
        # sms -> secondFactor -> password. The root/second-root guards still pass.
        model["features"][1]["parent"] = "sms"  # Password
        with self.assertRaisesRegex(ValueError, "cycle in feature tree"):
            validate(model)

    def test_self_parent_is_rejected(self) -> None:
        model = example()
        model["features"][1]["parent"] = "password"
        with self.assertRaisesRegex(ValueError, "is its own parent"):
            validate(model)

    def test_group_member_must_not_carry_a_decomposition(self) -> None:
        model = example()
        # sms is a member of secondFactor's alternative group.
        model["features"][4]["decomposition"] = "mandatory"
        with self.assertRaisesRegex(ValueError, "must not declare a decomposition"):
            validate(model)

    def test_solitary_child_requires_a_decomposition(self) -> None:
        model = example()
        del model["features"][2]["decomposition"]  # MFA, a solitary child of auth
        with self.assertRaisesRegex(ValueError, "requires a decomposition"):
            validate(model)

    def test_invalid_group_and_decomposition_values(self) -> None:
        model = example()
        model["features"][3]["group"] = "any"
        with self.assertRaisesRegex(ValueError, "invalid group"):
            validate(model)
        model = example()
        model["features"][1]["decomposition"] = "required"
        with self.assertRaisesRegex(ValueError, "invalid decomposition"):
            validate(model)

    def test_root_must_not_declare_a_decomposition(self) -> None:
        model = example()
        model["features"][0]["decomposition"] = "mandatory"
        with self.assertRaisesRegex(ValueError, "root auth must not declare a decomposition"):
            validate(model)

    def test_empty_group_is_rejected(self) -> None:
        model = example()
        # MFA is childless; declaring a group over no members is meaningless.
        model["features"][2]["group"] = "or"
        with self.assertRaisesRegex(ValueError, "or group with no members"):
            validate(model)

    def test_dangling_constraint_reference_is_rejected(self) -> None:
        model = example()
        model["constraints"].append({"type": "requires", "a": "passkey", "b": "ghost"})
        with self.assertRaisesRegex(ValueError, "constraint references unknown feature 'ghost'"):
            validate(model)

    def test_constraint_may_not_relate_a_feature_to_itself(self) -> None:
        model = example()
        model["constraints"].append({"type": "excludes", "a": "sms", "b": "sms"})
        with self.assertRaisesRegex(ValueError, "relates feature sms to itself"):
            validate(model)

    def test_invalid_constraint_type_is_rejected(self) -> None:
        model = example()
        model["constraints"].append({"type": "implies", "a": "sms", "b": "passkey"})
        with self.assertRaisesRegex(ValueError, "invalid type"):
            validate(model)

    def test_requires_and_excludes_contradiction_is_rejected(self) -> None:
        model = example()
        model["constraints"].append({"type": "requires", "a": "mfa", "b": "passkey"})
        model["constraints"].append({"type": "excludes", "a": "mfa", "b": "passkey"})
        with self.assertRaisesRegex(ValueError, "both requires and excludes"):
            validate(model)
        # excludes is symmetric: the reversed pair is the same contradiction.
        model["constraints"][-1] = {"type": "excludes", "a": "passkey", "b": "mfa"}
        with self.assertRaisesRegex(ValueError, "both requires and excludes"):
            validate(model)


class ConfigurationTests(unittest.TestCase):
    def test_a_valid_configuration_is_accepted(self) -> None:
        ok, reasons = is_valid_configuration(example(), BASE)
        self.assertTrue(ok, reasons)
        self.assertEqual(reasons, [])
        # Adding the optional MFA keeps it valid.
        ok, reasons = is_valid_configuration(example(), BASE | {"mfa"})
        self.assertTrue(ok, reasons)

    def test_unknown_selected_feature_is_reported(self) -> None:
        ok, reasons = is_valid_configuration(example(), BASE | {"ghost"})
        self.assertFalse(ok)
        self.assertIn("selected unknown feature: ghost", reasons)

    def test_root_must_be_selected(self) -> None:
        ok, reasons = is_valid_configuration(example(), set())
        self.assertFalse(ok)
        self.assertIn("root feature auth is not selected", reasons)

    def test_a_selected_feature_needs_its_parent(self) -> None:
        ok, reasons = is_valid_configuration(example(), {"auth", "password", "sms"})
        self.assertFalse(ok)
        self.assertIn("feature sms selected without its parent secondFactor", reasons)

    def test_mandatory_child_is_required_when_the_parent_is_selected(self) -> None:
        ok, reasons = is_valid_configuration(example(), {"auth"})
        self.assertFalse(ok)
        self.assertIn("mandatory feature password is required when auth is selected", reasons)

    def test_alternative_group_requires_exactly_one(self) -> None:
        # Parent selected, zero children -> invalid.
        ok, reasons = is_valid_configuration(example(), {"auth", "password", "secondFactor"})
        self.assertFalse(ok)
        self.assertTrue(any("exactly one selected child" in r for r in reasons), reasons)
        # Exactly one -> valid.
        ok, _ = is_valid_configuration(example(), {"auth", "password", "secondFactor", "sms"})
        self.assertTrue(ok)

    def test_selecting_two_of_an_alternative_group_is_rejected(self) -> None:
        selected = {"auth", "password", "secondFactor", "sms", "authenticatorApp"}
        ok, reasons = is_valid_configuration(example(), selected)
        self.assertFalse(ok)
        self.assertTrue(any("exactly one selected child" in r for r in reasons), reasons)

    def test_selecting_passkey_and_sms_together_is_rejected_by_excludes(self) -> None:
        selected = {"auth", "password", "secondFactor", "passkey", "sms"}
        ok, reasons = is_valid_configuration(example(), selected)
        self.assertFalse(ok)
        self.assertIn("feature passkey excludes sms", reasons)

    def test_optional_group_parent_when_unselected_imposes_nothing(self) -> None:
        # secondFactor (optional) omitted: Password alone is a complete product.
        ok, reasons = is_valid_configuration(example(), {"auth", "password"})
        self.assertTrue(ok, reasons)

    def test_or_group_requires_at_least_one(self) -> None:
        model = {
            "version": 1,
            "root": "editor",
            "features": [
                {"id": "editor", "name": "Editor"},
                {"id": "export", "name": "Export", "parent": "editor",
                 "decomposition": "mandatory", "group": "or"},
                {"id": "pdf", "name": "PDF", "parent": "export"},
                {"id": "html", "name": "HTML", "parent": "export"},
            ],
            "constraints": [],
        }
        self.assertIsNone(validate(model))
        ok, reasons = is_valid_configuration(model, {"editor", "export"})
        self.assertFalse(ok)
        self.assertTrue(any("at least one selected child" in r for r in reasons), reasons)
        ok, _ = is_valid_configuration(model, {"editor", "export", "pdf", "html"})
        self.assertTrue(ok)

    def test_requires_constraint_is_enforced(self) -> None:
        model = example()
        model["constraints"].append({"type": "requires", "a": "mfa", "b": "passkey"})
        # Selecting MFA without Passkey violates the requires edge; SMS keeps the
        # alternative group at exactly one so only the requires edge fails.
        ok, reasons = is_valid_configuration(
            model, {"auth", "password", "secondFactor", "sms", "mfa"}
        )
        self.assertFalse(ok)
        self.assertIn("feature mfa requires passkey", reasons)
        # With Passkey present the requires edge holds.
        ok, reasons = is_valid_configuration(model, BASE | {"mfa"})
        self.assertTrue(ok, reasons)


class CoverageRollupTests(unittest.TestCase):
    def test_coverage_rolls_up_and_optional_absence_does_not_drag(self) -> None:
        model = example()
        # Every selected feature carries proven coverage -> complete, and the
        # unselected optional MFA is not part of the denominator.
        result = coverage_rollup(model, BASE, BASE)
        self.assertTrue(result["complete"])
        self.assertEqual(result["uncovered"], [])
        self.assertNotIn("mfa", result["required"])
        self.assertTrue(all(row["rolled_covered"] for row in result["rows"]))

    def test_a_missing_leaf_rolls_up_to_the_root(self) -> None:
        model = example()
        covered = BASE - {"passkey"}
        result = coverage_rollup(model, BASE, covered)
        self.assertFalse(result["complete"])
        self.assertEqual(result["uncovered"], ["passkey"])
        rolled = {row["id"]: row["rolled_covered"] for row in result["rows"]}
        # passkey's gap propagates up through secondFactor -> password -> auth.
        self.assertFalse(rolled["auth"])
        self.assertFalse(rolled["password"])
        self.assertFalse(rolled["secondFactor"])
        self.assertFalse(rolled["passkey"])

    def test_rollup_refuses_an_invalid_configuration(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid configuration"):
            coverage_rollup(example(), {"auth"}, {"auth"})


class CliTests(unittest.TestCase):
    def _write(self, directory: str, model: dict) -> str:
        path = Path(directory) / "model.json"
        path.write_text(json.dumps(model))
        return str(path)

    def test_validate_verb_reports_success_and_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            good = self._write(directory, example())
            self.assertEqual(main(["validate", good]), 0)
            broken = example()
            broken["version"] = 9
            path = Path(directory) / "broken.json"
            path.write_text(json.dumps(broken))
            self.assertEqual(main(["validate", str(path)]), 1)

    def test_check_verb_exit_code_tracks_validity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model = self._write(directory, example())
            self.assertEqual(main(["check", model, "--select", ",".join(sorted(BASE))]), 0)
            bad = ",".join(sorted(BASE | {"sms"}))  # two alternative children
            self.assertEqual(main(["check", model, "--select", bad]), 1)

    def test_rollup_verb_exit_code_tracks_completeness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model = self._write(directory, example())
            sel = ",".join(sorted(BASE))
            self.assertEqual(main(["rollup", model, "--select", sel, "--covered", sel]), 0)
            partial = ",".join(sorted(BASE - {"passkey"}))
            self.assertEqual(main(["rollup", model, "--select", sel, "--covered", partial]), 1)

    def test_validate_verb_prints_the_annotated_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            good = self._write(directory, example())
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = main(["validate", good])
            out = buf.getvalue()
            self.assertEqual(rc, 0)
            # root, a mandatory node, the optional group parent, and a member, each
            # annotated with its facet.
            self.assertIn("Authentication [auth] (root)", out)
            self.assertIn("Password [password] (mandatory)", out)
            self.assertIn("Second Factor [secondFactor] (optional, alternative group)", out)
            self.assertIn("Passkey [passkey] (group member)", out)

    def _write_obligations(self, directory: str, obligations: dict) -> str:
        path = Path(directory) / "obligations.json"
        path.write_text(json.dumps(obligations))
        return str(path)

    def test_coverage_verb_prints_the_completeness_vector(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model = self._write(directory, example())
            obligations = self._write_obligations(
                directory,
                {
                    "auth": {"covered": 1, "total": 1},
                    "password": {"covered": 2, "total": 2},
                    "secondFactor": {"covered": 1, "total": 1},
                    "passkey": {"covered": 3, "total": 3},
                    "mfa": {"covered": 1, "total": 4},
                },
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = main(
                    ["coverage", model, obligations,
                     "--selected", "auth,password,secondFactor,passkey,mfa"]
                )
            out = buf.getvalue()
            self.assertEqual(rc, 0)
            # chosen skeleton auth+password+passkey = 6/6; the two selected optionals
            # (secondFactor, mfa) land in the optional bucket, not the mandatory one.
            self.assertIn("mandatory 6/6", out)
            self.assertIn("optional 2/5", out)

    def test_coverage_verb_without_selection_assesses_the_skeleton(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model = self._write(directory, example())
            obligations = self._write_obligations(
                directory, {"auth": {"covered": 1, "total": 1}}
            )
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = main(["coverage", model, obligations])
            out = buf.getvalue()
            self.assertEqual(rc, 0)
            # no configuration decided: every optional and group choice is unassessed.
            self.assertIn("5 unassessed", out)

    def test_coverage_verb_rejects_obligations_for_unknown_features(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            model = self._write(directory, example())
            obligations = self._write_obligations(
                directory, {"ghost": {"covered": 1, "total": 1}}
            )
            self.assertEqual(main(["coverage", model, obligations]), 1)


if __name__ == "__main__":
    unittest.main()
