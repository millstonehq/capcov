# Development

## Toolchain and checks

At the repository root, `direnv allow` enters the pinned `flake.nix` shell. It
provides Python, uv, Go, jq, git, Souffle, scip, scip-go and Shen; uv still
resolves capcov itself from the checked-in lockfile. The tools are defined once,
by family, under `nix/tools`: `base` (Python, uv, git), `souffle`, `shen` and
`indexers` (Go, PHP and the SCIP indexers). The default shell has every family;
`nix develop .#souffle` has `base` and that one. To exercise capcov against a
real non-Python process, run:

```sh
nix develop --command sh -c \
  'uv run --frozen --extra treesitter sh scripts/check-real-go-flow.sh'
```

That check uses tree-sitter to discover two routes in a Go HTTP server, plans a
save followed by its prerequisite-bound read, executes each scenario against a
fresh server process, reconciles the observations, and gates the result. It then
changes the server to acknowledge without retaining the write and requires the
gate to name `required-flow-failed: read-saved-note`. The example exemptions
record the static-analysis boundaries outside this bounded claim.

CI runs the flake checks, the unit suite once in parallel shards by tool family, the three
check scripts, builds the wheel and source distribution, and verifies installation
from the wheel. `earth +ci` runs the same checks in one build, with the dev shell
built inside it as one container image per tool family: `earth +test-pinned` is
the suite, each family's tests in that family's image, with the skip gate, and
`earth +checks` is lint, the wheel, the three check scripts and the flake checks.

A CI shard is `scripts/check-skips.py --family <family> --shard <i>/<n> --result <file>`
in that family's dev shell; `--merge <file> ...` judges the shards as one run. A shard
runs its test classes in worker processes, one per CPU unless `--jobs N` or
`CAPCOV_TEST_JOBS` says otherwise, heaviest class first by `tests/shard-timings.json`.
A class runs whole in one worker, each worker has its own `TMPDIR`, and a worker that
dies fails the shard. `--jobs 1` is plain serial unittest, for a test that relies on
running alone. Run a whole family locally with `--shard 1/1`:

```sh
nix develop --no-update-lock-file .#souffle --command bash -lc \
  'PYTHONPATH=src uv run --frozen --extra probe-python --extra treesitter --extra faults-property --group packaging python scripts/check-skips.py --family souffle --shard 1/1 --result souffle.json'
```

To release, update `version` in `pyproject.toml` and `src/capcov/__init__.py`,
refresh `uv.lock`, and merge the reviewed change. Push a tag `v<version>` at that
commit. CI checks the tag against the package version and publishes the tested
wheel and source distribution as GitHub release assets. Tags are immutable; use a
new version for a correction. PyPI publishing is not configured.


## Running many checkouts on one machine

Every artifact binds to a source snapshot: sha256 over a sorted manifest of per-file
hashes. Two caches make that cheap without making it weaker.

- **Per-root manifest** (`~/.cache/capcov/tree-manifests-v1`): a file's digest is reused
  while its size, times, inode and mode are unchanged. Cold for every new checkout.
- **Shared blob index** (`~/.cache/capcov/blob-sha256-v1`): keyed by git blob id. For a
  tracked file that git itself judges byte-identical to its index entry (`git diff-files`,
  the judgement `git commit` relies on), the digest is a fact about the blob, so a fresh
  `git worktree add` of a known commit hashes nothing it has seen in any other checkout.
  Only read-only git plumbing is used. Files git would convert on checkout (filters,
  CRLF, `ident`, working-tree encodings), symlinks, submodules, unmerged, skip-worktree
  and assume-unchanged entries are never keyed by blob; untracked files are hashed.

Both caches are off under `CAPCOV_NO_CACHE=1` and for every verification walk, and any
reuse makes the snapshot inexact (`source_snapshot.verification: cached`). When the two
caches disagree about a file, the bytes are read. The digest formula is unchanged, so no
recorded identity moves. `snapshot_tree(...).verification` reports `files_reused_by_blob`,
`blobs_learned` and a `blob_index` status (`hit`, `learned`, `ready`, `not-a-repo`,
`disabled: core.autocrlf`, ...).

When several agents run indexers, `nix develop` builds, full-tree snapshots or full test
suites on one disk at the same time, none of them finishes. Serialize the heavy steps:

```sh
scripts/with-heavy-lock.py --label "regression" -- \
  nix develop --command bash -lc 'PYTHONPATH="$PWD/src" python -m unittest discover -s tests -t .'
```

One advisory lock per machine (`$CAPCOV_HEAVY_LOCK`, else `~/.cache/capcov/heavy.lock`);
the waiter says who holds it; the child's exit status passes through; `--timeout 0`
fails fast with status 75 instead of waiting. It changes when a step runs, never what it
does.
