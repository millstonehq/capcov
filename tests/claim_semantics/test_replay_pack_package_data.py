"""rules-replay-v1.json ships as package data, byte-identical to the reviewed original.

The judge's rule pack has two copies on disk and exactly one of them is the
source of truth:

* ``experiments/claim-semantics/replay/rules-replay-v1.json`` -- the reviewed
  original, edited by a reviewer, read by the corpus adapter next to its cases,
  and present only in a source checkout.
* ``src/capcov/claims/replay/rules-replay-v1.json`` -- a mirror that travels in
  the wheel as package data, read through ``importlib.resources`` by
  ``capcov.claims.replay.pack``, which is how ``join``, the compiled checker
  and ``capcov experiment claims assumptions`` get the pack with no source tree
  on disk.

A mirror that may drift is a second opinion nobody reviewed, so drift is a
failing test rather than a convention: ``test_the_shipped_pack_is_the_reviewed_pack``
compares the bytes.  It skips -- with the reason named -- where the experiments
directory is absent, because there the reviewed original is simply not present
to compare against; every other case here runs from the installed package
alone.
"""
from __future__ import annotations

import json
import tomllib
import unittest
from importlib import resources
from pathlib import Path

from capcov.claims.replay import pack as replay_pack

#: Relative to THESE TESTS, which live in the repository, never relative to
#: ``capcov`` -- which may be an installed wheel in site-packages.
PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = PACKAGE_ROOT / "pyproject.toml"
MANIFEST = PACKAGE_ROOT / "MANIFEST.in"
REVIEWED_PACK_PATH = (PACKAGE_ROOT / "experiments" / "claim-semantics" / "replay"
                      / replay_pack.PACK_NAME)
_HAVE_SOURCE_TREE = PYPROJECT.is_file() and MANIFEST.is_file()


class ReplayPackPackageDataTest(unittest.TestCase):
    def test_the_pack_loads_through_importlib_resources(self) -> None:
        resource = resources.files("capcov.claims.replay").joinpath(replay_pack.PACK_NAME)
        document = json.loads(resource.read_text(encoding="utf-8"))
        self.assertEqual(document, replay_pack.load_pack())
        self.assertEqual(document["id"], replay_pack.PACK_ID)
        self.assertEqual(document["schema_version"], 1)

    def test_the_shipped_path_is_package_data_beside_the_module(self) -> None:
        """PACK_PATH names the shipped copy, not a path relative to the repository."""
        self.assertEqual(replay_pack.PACK_PATH.name, replay_pack.PACK_NAME)
        self.assertEqual(replay_pack.PACK_PATH.parent,
                         Path(replay_pack.__file__).resolve().parent)
        self.assertEqual(json.loads(replay_pack.PACK_PATH.read_text(encoding="utf-8")),
                         replay_pack.load_pack())

    def test_the_bundle_is_the_same_whichever_copy_it_is_built_from(self) -> None:
        self.assertEqual(replay_pack.pack_bundle(),
                         replay_pack.pack_bundle(replay_pack.load_pack(replay_pack.PACK_PATH)))

    @unittest.skipUnless(_HAVE_SOURCE_TREE,
                         "no source checkout beside these tests (pyproject.toml/MANIFEST.in absent): "
                         "the reviewed original is not present to compare the shipped mirror against")
    def test_the_shipped_pack_is_the_reviewed_pack(self) -> None:
        self.assertTrue(REVIEWED_PACK_PATH.is_file(), f"the reviewed pack is missing: {REVIEWED_PACK_PATH}")
        self.assertEqual(REVIEWED_PACK_PATH.read_bytes(), replay_pack.PACK_PATH.read_bytes(),
                         "the shipped pack has drifted from the reviewed original; copy "
                         f"{REVIEWED_PACK_PATH} over {replay_pack.PACK_PATH} "
                         "(the experiment file is canonical)")

    def test_the_loader_names_no_repository_path(self) -> None:
        """An installed module that names a repository directory is the defect removed here."""
        self.assertEqual([name for name in replay_pack.__all__ if "ROOT" in name or "REVIEWED" in name], [])
        for name in ("PACKAGE_ROOT", "REPLAY_ROOT", "REVIEWED_PACK_PATH"):
            self.assertFalse(hasattr(replay_pack, name), name)

    @unittest.skipUnless(_HAVE_SOURCE_TREE, "no source checkout beside these tests")
    def test_pyproject_and_manifest_declare_the_json_as_package_data(self) -> None:
        pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
        package_data = pyproject["tool"]["setuptools"]["package-data"]
        self.assertIn("*.json", package_data["capcov.claims.replay"])
        self.assertIn("recursive-include src/capcov/claims/replay *.json",
                      MANIFEST.read_text(encoding="utf-8"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
