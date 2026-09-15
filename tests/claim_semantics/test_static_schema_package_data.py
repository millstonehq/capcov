"""schema_static_v1.json ships as package data and loads from an installed distribution.

Three layers: the module reads the schema through ``importlib.resources``; the
build configuration declares the JSON as package data for
``capcov.claims.static`` (pyproject) and in the sdist manifest; and, where the
interpreter has ``pip`` and ``setuptools``, an install into a temporary target
directory carries the schema and loads it from there.  The install test is
skip-guarded honestly: the nix devShell's python has neither pip nor
setuptools, so that layer is evidence only where it actually ran.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import tomllib
import unittest
from importlib import resources
from pathlib import Path

from capcov.claims import static as static_pkg

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = PACKAGE_ROOT / "pyproject.toml"
MANIFEST = PACKAGE_ROOT / "MANIFEST.in"
_HAVE_BUILD_TOOLS = all(importlib.util.find_spec(name) is not None for name in ("pip", "setuptools"))


class StaticSchemaPackageDataTest(unittest.TestCase):
    def test_schema_loads_through_importlib_resources(self) -> None:
        resource = resources.files("capcov.claims.static").joinpath(static_pkg.SCHEMA_NAME)
        document = json.loads(resource.read_text(encoding="utf-8"))
        self.assertEqual(document, static_pkg.load_static_schema())
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(static_pkg.SCHEMA_PATH.name, static_pkg.SCHEMA_NAME)
        self.assertEqual(json.loads(static_pkg.SCHEMA_PATH.read_text(encoding="utf-8")), document)

    def test_pyproject_and_manifest_declare_the_json_as_package_data(self) -> None:
        pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
        package_data = pyproject["tool"]["setuptools"]["package-data"]
        self.assertIn("*.json", package_data["capcov.claims.static"])
        self.assertIn("recursive-include src/capcov/claims/static *.json", MANIFEST.read_text(encoding="utf-8"))

    @unittest.skipUnless(_HAVE_BUILD_TOOLS,
                         "pip and setuptools are not importable in this interpreter (the nix devShell "
                         "python ships neither); the installed-distribution layer is unverified here")
    def test_installed_distribution_carries_the_schema(self) -> None:
        with tempfile.TemporaryDirectory(prefix="capcov-wheel-") as target:
            install = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--quiet", "--no-deps", "--no-build-isolation",
                 "--target", target, str(PACKAGE_ROOT)],
                capture_output=True, text=True, timeout=600)
            self.assertEqual(install.returncode, 0, install.stderr[-2000:])
            probe = subprocess.run(
                [sys.executable, "-c",
                 "import json, os, sys; from importlib import resources; import capcov.claims.static as s; "
                 "assert os.path.realpath(s.__file__).startswith(os.path.realpath(sys.argv[1])), s.__file__; "
                 "print(json.dumps(json.loads(resources.files('capcov.claims.static')"
                 ".joinpath('schema_static_v1.json').read_text(encoding='utf-8')), sort_keys=True))",
                 target],
                capture_output=True, text=True, timeout=120,
                env={"PYTHONPATH": target, "PATH": "/usr/bin:/bin"}, cwd=target)
            self.assertEqual(probe.returncode, 0, probe.stderr[-2000:])
            self.assertEqual(json.loads(probe.stdout), static_pkg.load_static_schema())


if __name__ == "__main__":
    unittest.main()
