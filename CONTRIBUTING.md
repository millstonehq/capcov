# Contributing to capcov

## Where changes land

The maintainers develop capcov in a private monorepo and export it here with
[Copybara](https://github.com/google/copybara). This repository is the public
home: issues, pull requests and releases all happen here.

A pull request merged here is imported into the monorepo by a maintainer, and the
next export carries it back with a `GitOrigin-RevId` trailer. Between the merge
and that import the commit exists only in this repository, and CI prints the
import command for the maintainers. You do not need access to anything else.

## Setup

The toolchain is pinned in `flake.nix`: Python, uv, Go, Souffle, scip, scip-go,
scip-python, PHP with scip-php, and Shen with its bifrost launcher.

```sh
nix develop          # or `direnv allow`
PYTHONPATH=src uv run --frozen --extra probe-python --extra treesitter --extra faults-property --group packaging \
  python scripts/check-skips.py
```

`scripts/check-skips.py` runs the whole suite and fails when a test skips that
`tests/skips-allowed.json` does not name, or when a test named there did not skip.
unittest alone exits 0 on a skip, so a missing tool would read as a pass. A test
that needs a new tool gets the tool added to `flake.nix`. The allowlist is for
tests whose fixture cannot be in this repository, and every entry states why.

The suite takes about six minutes in one process. `--shard I/N --result FILE` runs
one of N parts and `--merge FILE...` judges the parts as one run: it fails unless
they ran every discovered test exactly once, then checks the union of their skips
against the allowlist. Test classes are assigned to shards by the seconds recorded
in `tests/shard-timings.json`; `--merge ... --write-timings tests/shard-timings.json`
rewrites that file from a run. A class above a tenth of the run is recorded and
assigned test by test. A new test module needs no entry: its classes are assigned
whole, at the median time.

`tests/tool-families.json` lists the test classes that need Souffle, the Shen
runtime or a SCIP indexer. `--family NAME --shard I/N` runs the classes of one
family, so a job or a container needs only that family's tools; every class the
file does not list is in `base` and needs none. A test that needs a tool gets its
class added to the file. Left out, it runs in `base`, skips there, and the gate
fails the run. `earth +test-pinned` builds one container image per family and
runs each family's tests in its image, and `earth +ci` runs every check.

Three test classes compile Souffle programs to native checkers, about 40 seconds
each on a laptop. Set `CAPCOV_SOUFFLE_CACHE_DIR` to a directory to keep the
binaries between runs. An entry is reused only when the program, the souffle
executable, its compiler configuration and the flags hash to its name and the
binary hashes to its recorded digest.

Without Nix, uv and Python 3.12 or newer run the engine suite. Tests whose tool is
absent skip there; `tests/skips-allowed-plain-python.json` lists them.

```sh
PYTHONPATH=src uv run --frozen --extra probe-python --extra treesitter --extra faults-property \
  python -m unittest $(ls tests/test_*.py | sed 's|/|.|; s|\.py$||')
```

## Before opening a pull request

```sh
nix flake check     # the package build and the Shen, Souffle and scip-go smokes
uv run --frozen --group lint ruff check .
uv run --frozen --with pytest sh scripts/check-backpressure.sh
uv run --frozen --extra treesitter sh scripts/check-real-go-flow.sh
uv run --frozen --extra faults-property sh scripts/check-fault-harness.sh
uv build && sh scripts/check-wheel.sh dist
```

CI runs the same commands.

## What belongs here

Engine changes and their regression tests. Application models, source exports,
identities, startup recipes and execution evidence belong in the repository that
consumes capcov. Tests use synthetic fixtures: do not add private source, real
hostnames or real credentials.

The core (`discover`, `reconcile`, `gate`, `map`) uses the Python standard library
only. A new dependency goes behind an optional extra and is imported lazily.

## Commit messages

Conventional commits: `<type>(<scope>): <subject>`, with `feat`, `fix`, `docs`,
`refactor`, `test` or `chore` as the type.

## Releases

A maintainer updates `version` in `pyproject.toml` and `src/capcov/__init__.py`,
refreshes `uv.lock`, and pushes the tag `v<version>` on the exported commit. CI
checks that the tag equals the package version and attaches the wheel and the
source distribution to a GitHub release.

## License

By contributing, you agree that your contributions are licensed under the MIT
License in `LICENSE`.
