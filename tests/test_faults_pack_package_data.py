"""rules-faults-v1.json ships as package data and loads without a source tree."""
from __future__ import annotations

import json
import tomllib
import unittest
from importlib import resources
from pathlib import Path

from capcov.claims.ir import Atom
from capcov.faults import checker

#: Relative to these tests, which live in the repository, never relative to
#: ``capcov``, which may be an installed wheel in site-packages.
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = PACKAGE_ROOT / "pyproject.toml"
MANIFEST = PACKAGE_ROOT / "MANIFEST.in"
_HAVE_SOURCE_TREE = PYPROJECT.is_file() and MANIFEST.is_file()


class FaultPackPackageDataTest(unittest.TestCase):
    def test_the_pack_loads_through_importlib_resources(self) -> None:
        resource = resources.files("capcov.faults").joinpath(checker.PACK_NAME)
        document = json.loads(resource.read_text(encoding="utf-8"))
        self.assertEqual(document, checker.load_pack())
        self.assertEqual((document["id"], document["schema_version"]), (checker.PACK_ID, 1))

    def test_the_pack_is_in_the_replay_packs_wire_form(self) -> None:
        from capcov.claims.replay import pack as replay_pack

        pack = checker.load_pack()
        for section in ("primitives", "supplementary_primitives", "derived", "rules"):
            self.assertIsInstance(pack[section], list)
        for relation in checker.pack_relations(pack):
            self.assertEqual(set(relation), replay_pack.RELATION_FIELDS)
        self.assertEqual(checker.DIAGNOSTIC_POLICY, replay_pack.DIAGNOSTIC_POLICY)

    def test_the_pack_alone_is_a_valid_bundle(self) -> None:
        bundle = checker.pack_bundle()
        self.assertEqual((bundle.facts, bundle.claims), ((), ()))
        names = {relation.name for relation in bundle.relations}
        for invariant in checker.load_pack()["invariants"]:
            self.assertIn(invariant["name"], names)
            for relation in invariant["requires"]:
                self.assertIn(relation, names)

    def test_each_invariant_closure_rule_reads_exactly_its_required_relations(self) -> None:
        bundle = checker.pack_bundle()
        for invariant in checker.load_pack()["invariants"]:
            closures = [rule for rule in bundle.rules if rule.head.relation == "violated_closed"
                        and rule.head.terms[1].value == invariant["name"]]
            self.assertEqual(len(closures), 1, invariant["name"])
            self.assertEqual(sorted(atom.relation for atom in closures[0].body if isinstance(atom, Atom)),
                             sorted(invariant["requires"]))

    def test_a_pack_that_drops_a_completeness_atom_is_refused(self) -> None:
        pack = checker.load_pack()
        for rule in pack["rules"]:
            if rule["name"] == "unsettled_effect":
                rule["body"] = [atom for atom in rule["body"] if atom.get("relation") != "settled_closed"]
        with self.assertRaises(checker.FaultCheckError) as raised:
            checker.pack_bundle(pack)
        self.assertIn("missing-completeness", str(raised.exception))

    @unittest.skipUnless(_HAVE_SOURCE_TREE, "no source checkout beside these tests")
    def test_pyproject_and_manifest_declare_the_json_as_package_data(self) -> None:
        pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
        self.assertIn("*.json", pyproject["tool"]["setuptools"]["package-data"]["capcov.faults"])
        self.assertIn("recursive-include src/capcov/faults *.json", MANIFEST.read_text(encoding="utf-8"))

    @unittest.skipUnless(_HAVE_SOURCE_TREE, "no source checkout beside these tests")
    def test_hypothesis_is_an_optional_extra_and_never_a_dependency(self) -> None:
        project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
        self.assertEqual(project["dependencies"], [])
        extra = project["optional-dependencies"]["faults-property"]
        self.assertEqual(len(extra), 1)
        self.assertTrue(extra[0].startswith("hypothesis"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
