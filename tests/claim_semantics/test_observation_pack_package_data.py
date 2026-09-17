"""The observation pack and schema ship inside the wheel, or the judge does not run.

``capcov.claims.observation`` carries two JSON documents and reads BOTH through
``importlib.resources``: ``schema_observation_v1.json`` (the primitive relation
declarations, ``load_observation_schema``) and ``rules-observation-v1.json`` (the
judge's rule pack, ``observation.pack``).  Neither has an
``experiments/claim-semantics`` mirror -- unlike the replay pack, the shipped
file IS the reviewed file -- so there is no second copy to fall back on: if the
JSON does not travel as package data, ``pack_bundle()`` raises the moment the
judge is used anywhere but a source checkout, and the failure looks like a
missing feature rather than a missing file.

``[tool.setuptools.package-data]`` listed only ``capcov.claims.static`` and
``capcov.claims.replay``; these tests pin the declaration that was missing, in
both pyproject.toml (wheel) and MANIFEST.in (sdist), and pin the loaders' own
behaviour so a future move of the files is caught here rather than at a user's
install.
"""
from __future__ import annotations

import json
import tomllib
import unittest
from importlib import resources
from pathlib import Path

from capcov.claims import validate_bundle
from capcov.claims.observation import (SCHEMA_NAME, SCHEMA_PATH, load_observation_schema)
from capcov.claims.observation import pack as observation_pack

#: Relative to THESE TESTS, which live in the repository -- never relative to
#: ``capcov``, which may be an installed wheel in site-packages.
PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = PACKAGE_ROOT / "pyproject.toml"
MANIFEST = PACKAGE_ROOT / "MANIFEST.in"
_HAVE_SOURCE_TREE = PYPROJECT.is_file() and MANIFEST.is_file()

PACKAGE = "capcov.claims.observation"


class ObservationPackageDataTest(unittest.TestCase):
    def test_both_documents_load_through_importlib_resources(self) -> None:
        for name in (SCHEMA_NAME, observation_pack.PACK_NAME):
            with self.subTest(name):
                resource = resources.files(PACKAGE).joinpath(name)
                document = json.loads(resource.read_text(encoding="utf-8"))
                self.assertEqual(document["schema_version"], 1)
        self.assertEqual(json.loads(resources.files(PACKAGE).joinpath(SCHEMA_NAME)
                                    .read_text(encoding="utf-8")),
                         load_observation_schema())
        self.assertEqual(json.loads(resources.files(PACKAGE).joinpath(observation_pack.PACK_NAME)
                                    .read_text(encoding="utf-8")),
                         observation_pack.load_pack())

    def test_the_shipped_paths_are_package_data_beside_the_modules(self) -> None:
        module_dir = Path(observation_pack.__file__).resolve().parent
        self.assertEqual(observation_pack.PACK_PATH.parent, module_dir)
        self.assertEqual(observation_pack.PACK_PATH.name, observation_pack.PACK_NAME)
        self.assertEqual(SCHEMA_PATH.parent, module_dir)
        self.assertEqual(SCHEMA_PATH.name, SCHEMA_NAME)

    def test_the_bundle_is_the_same_whichever_copy_it_is_built_from(self) -> None:
        self.assertEqual(observation_pack.pack_bundle(),
                         observation_pack.pack_bundle(
                             observation_pack.load_pack(observation_pack.PACK_PATH)))
        self.assertEqual(validate_bundle(observation_pack.pack_bundle()), ())

    def test_the_pack_is_read_through_resources_not_off_the_path(self) -> None:
        """``read_pack_text`` is the wheel-safe reader; the path is documentation."""
        self.assertEqual(json.loads(observation_pack.read_pack_text()),
                         observation_pack.load_pack())

    def test_the_loader_names_no_repository_path(self) -> None:
        for name in ("PACKAGE_ROOT", "REVIEWED_PACK_PATH", "EXPERIMENTS_ROOT"):
            self.assertFalse(hasattr(observation_pack, name), name)

    @unittest.skipUnless(_HAVE_SOURCE_TREE,
                         "no source checkout beside these tests (pyproject.toml/MANIFEST.in "
                         "absent): the packaging declarations are not present to read")
    def test_pyproject_declares_the_observation_json_as_package_data(self) -> None:
        pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
        package_data = pyproject["tool"]["setuptools"]["package-data"]
        self.assertIn(PACKAGE, package_data,
                      "the observation pack does not ship in the wheel: the judge breaks "
                      "outside a source checkout")
        self.assertIn("*.json", package_data[PACKAGE])

    @unittest.skipUnless(_HAVE_SOURCE_TREE, "no source checkout beside these tests")
    def test_the_manifest_declares_it_for_the_sdist_too(self) -> None:
        self.assertIn("recursive-include src/capcov/claims/observation *.json",
                      MANIFEST.read_text(encoding="utf-8"))

    @unittest.skipUnless(_HAVE_SOURCE_TREE, "no source checkout beside these tests")
    def test_every_json_beside_the_modules_is_covered_by_the_declaration(self) -> None:
        """A third document added later must ship too; ``*.json`` is what makes that so."""
        module_dir = Path(observation_pack.__file__).resolve().parent
        shipped = sorted(path.name for path in module_dir.glob("*.json"))
        self.assertEqual(shipped, sorted([SCHEMA_NAME, observation_pack.PACK_NAME]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
