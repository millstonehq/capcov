#!/bin/sh
# Install the built wheel into a clean environment and check what a consumer gets:
# both console scripts start, the fault harness loads its rule pack from the
# wheel, and the installed version matches the source and, on a tag build, the tag.
#
# Usage: scripts/check-wheel.sh [dist-directory]
# RELEASE_REF, when it names a tag (refs/tags/v1.2.3), must equal v<version>.
set -eu

dist=${1:-dist}
scratch=$(mktemp -d "${TMPDIR:-/tmp}/capcov-wheel.XXXXXX")
trap 'rm -rf "$scratch"' EXIT INT TERM

uv venv --quiet "$scratch/venv"
uv pip install --quiet --python "$scratch/venv/bin/python" "$dist"/*.whl
"$scratch/venv/bin/capcov" --help >/dev/null
"$scratch/venv/bin/capcov" flows --help >/dev/null
"$scratch/venv/bin/synapse-capabilities" --help >/dev/null
# Run from the scratch directory so that `import capcov` finds the installed
# wheel and not a source tree in the working directory.
cd "$scratch"
RELEASE_REF=${RELEASE_REF:-} "$scratch/venv/bin/python" - <<'PY'
import importlib.metadata as metadata
import os

import capcov
from capcov.faults import checker

assert checker.load_pack()["id"] == checker.PACK_ID
version = metadata.version("capcov")
assert capcov.__version__ == version, (capcov.__version__, version)
ref = os.environ.get("RELEASE_REF", "")
assert not ref.startswith("refs/tags/") or ref == "refs/tags/v" + version, (ref, version)
print(f"capcov {version}: wheel installs, capcov and synapse-capabilities start")
PY
