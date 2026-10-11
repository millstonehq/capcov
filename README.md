# capcov

capcov finds the parts of your application that no test exercises.

It reads your source to list what the application can do (its HTTP routes and the
database tables behind them), runs your existing test suite with a probe to record
what the tests actually touched, and fails the build on the difference. Every gap
comes with the reason it was flagged and what to do about it.

Here is a three-endpoint FastAPI app whose tests never call the archive endpoint:

```console
$ capcov gate coverage.json
capcov gate: FAIL -- 1 unexplained

  unexplained-static_only: archived_todos
    reachable from POST /todos/{todo_id}/archive and no exercise touched it. This is a coverage gap: write the test.

Each of these is a question with an answer: write the test, fix the adapter, delete the dead entity, or record why in an exemptions file (--exemptions).
```

Add a test for that endpoint and the gate passes:

```console
$ capcov gate coverage.json
capcov gate: PASS -- 2 capabilities covered, 0 exempted with reasons
```

Line coverage reports the same app and tests at 90%: the archive handler leaves only
4 of 42 statements unexecuted. capcov counts the things a user can do and the data each one writes,
so a whole untested endpoint is one finding, named.

## Highlights

- **Static discovery without running your code.** Routes, tables and the call paths
  between them come from parsing source. The default resolver uses only the Python
  standard library.
- **Runtime evidence from the tests you already have.** `capcov observe` wraps your
  pytest command; there is no separate test harness to write.
- **Explained results.** Each entity lands in one of four cells and the gate says
  which, why, and what fixes it.
- **Blind spots are listed, not dropped.** `getattr`, `eval`, dynamic imports and
  computed SQL are reported with file and line, so a clean result cannot hide what
  the analysis could not see.
- **Exemptions need a written reason.** An exemption without one is rejected, and
  one that no longer matches anything fails the build.
- **Optional type-aware call graphs.** `--resolver scip` follows calls across
  modules and interfaces using a [SCIP](https://github.com/sourcegraph/scip) index.

## Installation

capcov needs Python 3.12 or newer and is installed from GitHub; it is not on PyPI.
Install it into the same environment your tests run in: `capcov observe` loads a
probe inside the test process, and the probe has to be importable there.

```sh
# With uv, as a development dependency of your project
uv add --dev "capcov @ git+https://github.com/millstonehq/capcov"

# With pip, in your test virtualenv
pip install "capcov @ git+https://github.com/millstonehq/capcov"
```

To pin a version, add `@<commit>` to the URL. The static commands (`discover`,
`reconcile`, `gate`, `report`) also run from a standalone install, for example
`uv tool install` or `nix run github:millstonehq/capcov -- --help`.

The core has no dependencies. Extras:

| Extra | Adds | Needed for |
| --- | --- | --- |
| `treesitter` | tree-sitter and its language pack | route discovery in Go, JavaScript and other non-Python sources (`capcov flows`) |
| `faults-property` | Hypothesis | property-based schedules in the fault-injection harness |

## Quick start

The app above is in [`examples/quickstart`](https://github.com/millstonehq/capcov/tree/main/examples/quickstart).

```sh
git clone https://github.com/millstonehq/capcov
cd capcov/examples/quickstart
uv sync    # installs the app, pytest and capcov from this checkout
```

Tell capcov which adapter reads your code and where the source is, in `capcov.toml`:

```toml
[capcov]
adapter = "python-fastapi-sqlalchemy"
source = "src"
```

Then run the four steps. Each writes a JSON artifact the next one reads.

```console
$ uv run capcov discover --target . --out capabilities.json
capcov discover: 2 entities, 3 surfaces, 2 entities bound, history 2 -> 2 -> 2, 0 blind spots
capcov discover: call edges 12 by import, 0 by name; residue 0 ambiguous (5 external, 1 chained, 0 builtin-shadowed; none of these are residue)

$ uv run capcov observe --target . --out observed.json -- uv run pytest -q
.                                                                        [100%]
1 passed in 0.83s
capcov observe: uv run pytest -q

$ uv run capcov reconcile capabilities.json observed.json --out coverage.json
capcov reconcile: both 1, static_only 1, runtime_only 0, neither 0

$ uv run capcov report coverage.json
entity          cell          tests  surfaces  operations
archived_todos  static_only       0         1  create
todos           both              1         2  create,read

both: 1, static_only: 1, runtime_only: 0, neither: 0

surfaces no exercise reached (1):
  POST /todos/{todo_id}/archive
```

`uv run capcov gate coverage.json` prints the failure shown at the top and exits 1.
Add a test that calls `POST /todos/{id}/archive`, rerun the steps, and it exits 0.

## How it works

```
 your source ──► discover ──► what the code can reach   (surfaces → entities)
                                        │
 your tests ───► observe ───► what the tests touched    (surfaces → entities)
                                        │
                                        ▼
                               reconcile: one cell per entity
                                        │
                                        ▼
                               gate: fail on any unexplained cell
```

A *surface* is an entry point, such as `POST /todos`. An *entity* is something the
application stores, such as the `todos` table. Reconciliation compares the two maps
and puts each entity in one cell:

| Cell | Static analysis | Tests | What it usually means |
| --- | --- | --- | --- |
| `both` | reaches it | touched it | covered |
| `static_only` | reaches it | never touched it | a missing test |
| `runtime_only` | finds no path | touched it | a route the adapter missed, or dynamic access; check the blind-spot list |
| `neither` | finds no path | never touched it | dead code, or a gap in discovery |

Everything except `both` fails the gate unless an exemptions file records why. The
design follows the software reflexion model (Murphy, Notkin and Sullivan, 1995); the
other sources are in [foundations](https://github.com/millstonehq/capcov/blob/main/docs/foundations.md).

## Beyond entity coverage

The same discover, observe, reconcile, gate shape applies to other questions:

| Command | Question it answers | Guide |
| --- | --- | --- |
| `capcov flows` | Did each route, branch and planned user journey actually run and assert its outcome? Reads any tree-sitter language or an OpenAPI document. | [flows](https://github.com/millstonehq/capcov/blob/main/docs/flows.md) |
| `capcov capabilities` | Do the specified, implemented and exercised capabilities agree? Reads OpenSpec specs and logs. | [capabilities](https://github.com/millstonehq/capcov/blob/main/docs/capabilities.md) |
| `capcov outcomes` | Did the exact pytest cases that prove each business outcome pass? | [outcomes](https://github.com/millstonehq/capcov/blob/main/docs/outcomes.md) |
| `capcov features` | Does coverage roll up correctly across a feature model? | [features](https://github.com/millstonehq/capcov/blob/main/src/capcov/features/README.md) |
| `capcov faults` | Did a run under injected faults and crashes keep its invariants? | [faults](https://github.com/millstonehq/capcov/blob/main/docs/faults.md) |
| `capcov.vectors` | Does a rewrite behave like the system it replaces on the same inputs? | [vectors](https://github.com/millstonehq/capcov/blob/main/skills/capcov-vectors/SKILL.md) |

Adapters for sources capcov cannot parse can be added without changing capcov; see
[plugins](https://github.com/millstonehq/capcov/blob/main/src/capcov/PLUGINS.md).

## Continuous integration

With capcov as a development dependency, CI runs the same commands as the quick
start and fails on the gate's exit status:

```sh
uv run capcov discover --target . --out capabilities.json
uv run capcov observe --target . --out observed.json -- uv run pytest -q
uv run capcov reconcile capabilities.json observed.json --out coverage.json
uv run capcov gate coverage.json --exemptions capcov.exemptions.toml
```

For jobs that only need the static commands, the repository is also a GitHub
Action that puts `capcov` on `PATH`: `uses: millstonehq/capcov@<commit>`, with an
optional `extras` input such as `treesitter`.

## Limitations

- Entity discovery reads Python with FastAPI and SQLAlchemy. Route discovery for
  `capcov flows` covers any tree-sitter language and OpenAPI, but table-level
  coverage for other stacks is not implemented.
- The runtime probe observes SQLAlchemy. Direct `sqlite3` calls can be declared
  ([details](https://github.com/millstonehq/capcov/blob/main/docs/entity-coverage.md#direct-sqlite-declarations))
  but are not observed.
- Routes built from variables or string concatenation are not resolved. They are
  reported as unresolved obligations rather than counted.
- A passing gate means the code, the tests and your exemptions agree on what exists
  and what was exercised. It does not mean the behaviour is correct; assertions in
  your tests still decide that.

## Documentation

- [Entity coverage](https://github.com/millstonehq/capcov/blob/main/docs/entity-coverage.md): exemptions, the SCIP resolver, SQLite declarations
- [Flows](https://github.com/millstonehq/capcov/blob/main/docs/flows.md), [capabilities](https://github.com/millstonehq/capcov/blob/main/docs/capabilities.md), [outcomes](https://github.com/millstonehq/capcov/blob/main/docs/outcomes.md), [faults](https://github.com/millstonehq/capcov/blob/main/docs/faults.md)
- [Development](https://github.com/millstonehq/capcov/blob/main/docs/development.md): the pinned toolchain, running the suite, releases
- [Design decisions](https://github.com/millstonehq/capcov/tree/main/docs/adr) and [foundations](https://github.com/millstonehq/capcov/blob/main/docs/foundations.md)

## Contributing

See [CONTRIBUTING.md](https://github.com/millstonehq/capcov/blob/main/CONTRIBUTING.md).
Tests use synthetic fixtures only: no private source, real data or credentials.

## License and history

MIT; see [LICENSE](https://github.com/millstonehq/capcov/blob/main/LICENSE).

capcov began as `packages/capabilities` in
[millstonehq/synapse](https://github.com/millstonehq/synapse), distributed as
`synapse-capabilities`, and moved here at synapse commit `649be7f`. The
`synapse-capabilities` command remains an alias for `capcov` until 0.3.0.
Boundary vectors, vector claims, the observation judge and reviewer admissions for
the replay checker were written by [@pyrex41](https://github.com/pyrex41)
([synapse#53](https://github.com/millstonehq/synapse/pull/53),
[synapse#54](https://github.com/millstonehq/synapse/pull/54) and
[pyrex41/synapse-capcov](https://github.com/pyrex41/synapse-capcov)).
