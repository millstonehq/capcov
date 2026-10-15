# capcov

capcov is a capability coverage tool. It reads a system's source or API contracts
to list what the system can do (HTTP routes, commands, pipeline stages and the data
they reach), collects what the system actually did from test runs, browser
recordings or traffic, and fails the build when something on the first list never
appears in the second.

Line coverage does not catch this. In
[`examples/python-pytest`](https://github.com/millstonehq/capcov/tree/main/examples/python-pytest)
the tests pass with 90% line coverage and the archive endpoint is never called;
capcov reports the endpoint by name.

It was built for rewrites: checking that a new system does everything the old one
did, including by replaying recorded inputs against both
([vectors](https://github.com/millstonehq/capcov/blob/main/skills/capcov-vectors/SKILL.md)).
It is also useful as a CI gate against new routes that land without tests.

The core is the same for every language. Reading a particular language, contract
format or kind of runtime evidence is the job of an adapter (static) or a probe
(runtime), and you choose the pair that fits the system.

Here is a Go service with three routes. Tree-sitter reads the routes from source;
a browser run recorded as a HAR file shows which ones were called:

```console
$ capcov report coverage.json
entity                         cell          tests  surfaces  operations
http:GET /notes                both              1         1  read
http:POST /notes               both              1         1  create
http:POST /notes/{id}/archive  static_only       0         1  create

both: 2, static_only: 1, runtime_only: 0, neither: 0

surfaces no exercise reached (1):
  http:POST /notes/{id}/archive

$ capcov gate coverage.json --exemptions capcov.exemptions.toml
capcov gate: FAIL -- 1 unexplained

  unexplained-static_only: http:POST /notes/{id}/archive
    reachable from http:POST /notes/{id}/archive and no exercise touched it. This is a coverage gap: write the test.
```

Record a browser run that also archives a note and the gate passes:

```console
$ capcov gate coverage.json --exemptions capcov.exemptions.toml
capcov gate: PASS -- 3 capabilities covered, 0 exempted with reasons
```

capcov knows nothing about Go beyond the tree-sitter query in `capcov.toml`, and
nothing about the browser beyond the HAR file.

## How it works

```
 source, contracts ──► adapter ──► what the system can do     (static)
                                             │
 tests, browser runs,                        │
 traffic, logs ─────► probe ────► what it was seen doing      (runtime)
                                             │
                                             ▼
                              reconcile: one cell per capability
                                             │
                                             ▼
                              gate: fail on anything unexplained
```

A *surface* is a way into the system: an HTTP route, a command, a pipeline stage.
An *entity* is what a surface reads or changes: a table, a resource, a sink, or
the surface itself when the adapter sees no further. Adapters emit surfaces,
entities and the paths between them; probes emit which surfaces and entities a
run touched, as route templates and names only, never parameters or values.
Reconciliation puts each entity in one cell:

| Cell | Static analysis | Runtime evidence | What it usually means |
| --- | --- | --- | --- |
| `both` | reaches it | touched it | covered |
| `static_only` | reaches it | never touched it | a missing test or scenario |
| `runtime_only` | finds no path | touched it | an entry point the adapter missed, or dynamic access |
| `neither` | finds no path | never touched it | dead code, or a gap in discovery |

Everything except `both` fails the gate unless an exemptions file records a
reason. Anything an adapter cannot resolve, such as a route built from a variable
or a call through `getattr`, is reported with its file and line and counts
against the gate too, so a clean result cannot hide what the analysis could not
see. The design follows the software reflexion model (Murphy, Notkin and Sullivan,
1995); the other sources are in
[foundations](https://github.com/millstonehq/capcov/blob/main/docs/foundations.md).

## Adapters and probes

Static adapters:

| Adapter | Reads | Scope |
| --- | --- | --- |
| `treesitter-routes` | route declarations, matched by a tree-sitter query you supply, plus the branches and exception handlers inside each handler | any language with a tree-sitter grammar |
| `structured-spec` | operations in an OpenAPI 3.0 or 3.1 JSON document | any HTTP service with a contract |
| `python-fastapi-sqlalchemy` | FastAPI routes, SQLAlchemy models and the call graph between them, optionally through a [SCIP](https://github.com/sourcegraph/scip) index | Python |
| plugin | anything else, through a callable you write | any source; see [plugins](https://github.com/millstonehq/capcov/blob/main/src/capcov/PLUGINS.md) |

Runtime probes:

| Probe | Observes | Scope |
| --- | --- | --- |
| `har` | requests in HAR files from an existing browser or HTTP run, such as Playwright's `recordHar` | any HTTP service |
| `browser` | scenarios capcov plans from a behaviour model, executed by your browser runner | any web application |
| `pytest` | requests and ORM operations during your pytest suite, correlated per request | Python, run inside the test environment |
| `load` | stages, tables and sinks a data pipeline moved; the contract ships, the driver does not yet | data pipelines |

Several commands take runtime evidence from other sources: application logs
(`capcov capabilities`), and recorded database effects in MySQL, MongoDB, Redis
and S3 (`capcov.vectors`).

## Capability lenses

The same static-against-runtime reconciliation answers other questions:

| Command | Question it answers | Guide |
| --- | --- | --- |
| `capcov capabilities` | Do the specified, implemented and exercised capabilities agree? Reads OpenSpec specifications, discovered surfaces and logs. | [capabilities](https://github.com/millstonehq/capcov/blob/main/docs/capabilities.md) |
| `capcov flows` | Did each planned user journey run, and assert its outcome, against every route and branch it should reach? | [flows](https://github.com/millstonehq/capcov/blob/main/docs/flows.md) |
| `capcov features` | Does coverage roll up correctly across a feature model? | [features](https://github.com/millstonehq/capcov/blob/main/src/capcov/features/README.md) |
| `capcov.vectors` | Does a rewrite behave like the system it replaces, on the same inputs and starting state? | [vectors](https://github.com/millstonehq/capcov/blob/main/skills/capcov-vectors/SKILL.md) |
| `capcov outcomes` | Did the exact test cases that prove each business outcome pass? | [outcomes](https://github.com/millstonehq/capcov/blob/main/docs/outcomes.md) |
| `capcov faults` | Did a run under injected faults and crashes keep its invariants? | [faults](https://github.com/millstonehq/capcov/blob/main/docs/faults.md) |

## Installation

capcov needs Python 3.12 or newer to run, whatever language the target is
written in. It is installed from GitHub; it is not on PyPI.

```sh
uv tool install "capcov[treesitter] @ git+https://github.com/millstonehq/capcov"
```

`pip install` takes the same argument. To pin a version, add `@<commit>` to the
URL. Nix users can run it without installing:
`nix run github:millstonehq/capcov -- --help`.

The core has no dependencies. Extras:

| Extra | Adds | Needed for |
| --- | --- | --- |
| `treesitter` | tree-sitter and its language pack | the `treesitter-routes` adapter |
| `faults-property` | Hypothesis | property-based schedules in the fault-injection harness |

A probe that runs inside the target's own process needs capcov installed there
as well. For the `pytest` probe, add capcov as a development dependency of the
Python project under test. The `har` probe reads files and needs nothing in the
target.

## Quick start

The Go service above is in
[`examples/go-har`](https://github.com/millstonehq/capcov/tree/main/examples/go-har),
with the HAR file its browser run recorded.

```sh
git clone https://github.com/millstonehq/capcov
cd capcov/examples/go-har
```

`capcov.toml` names the adapter and the tree-sitter query that finds route
declarations, here calls to the service's `route` helper:

```toml
[[adapters]]
name = "treesitter-routes"
language = "go"
globs = ["*.go"]
query = '''
(call_expression
  function: (identifier) @fn (#eq? @fn "route")
  arguments: (argument_list (interpreted_string_literal) @path . (identifier) @handler))
'''
```

Run the four steps. Each writes a JSON file the next one reads.

```console
$ capcov discover --target . --out capcov.capabilities.json
capcov discover: 3 entities, 3 surfaces, 3 entities bound, history 3, 0 blind spots

$ capcov observe --target . --probe har --out observed.json -- e2e/run.har

$ capcov reconcile capcov.capabilities.json observed.json --out coverage.json
capcov reconcile: both 2, static_only 1, runtime_only 0, neither 0

$ capcov gate coverage.json --exemptions capcov.exemptions.toml
capcov gate: FAIL -- 1 unexplained
```

The exemptions file accepts, with a reason each, the four limits that reading
route declarations cannot settle, such as whether the server mounts every route
it declares. To close the gap, add the archive request to `e2e/run.mjs` and record
again with the service running:

```sh
PORT=8080 go run . &
(cd e2e && npm install)
PW_CHANNEL=chrome BASE_URL=http://127.0.0.1:8080 node e2e/run.mjs
```

`PW_CHANNEL=chrome` uses an installed Google Chrome; without it, Playwright needs
its own browser (`npx playwright install chromium`). Then rerun the steps.

The same steps run a Python service with the `python-fastapi-sqlalchemy` adapter
and the `pytest` probe: see
[`examples/python-pytest`](https://github.com/millstonehq/capcov/tree/main/examples/python-pytest),
where `capcov observe` wraps `uv run pytest -q`.

## Continuous integration

Run the four commands after the job that produces your runtime evidence, and let
the gate's exit status fail the build. In GitHub Actions, the repository is also
an action that puts `capcov` on `PATH`:

```yaml
- uses: millstonehq/capcov@<commit>
  with:
    extras: treesitter
- run: |
    capcov discover --target . --out capcov.capabilities.json
    capcov observe --target . --probe har --out observed.json -- e2e/*.har
    capcov reconcile capcov.capabilities.json observed.json --out coverage.json
    capcov gate coverage.json --exemptions capcov.exemptions.toml
```

## Limitations

- A tree-sitter adapter sees what its query matches. Routes registered through
  configuration or reflection need a query that finds them, a plugin, or an
  exemption; the ones it cannot resolve are reported, not dropped.
- Entities beyond the route itself, such as the tables a request writes, are
  discovered for Python with FastAPI and SQLAlchemy only. Other adapters bind each
  route to itself.
- The `har` probe sees requests, not what the server did with them. A route in
  `both` was called; whether it behaved correctly is for your tests' assertions.
- A passing gate means the code, the runtime evidence and your exemptions agree
  on what exists and what was exercised. It does not mean the behaviour is
  correct.

## Documentation

- [Entity coverage](https://github.com/millstonehq/capcov/blob/main/docs/entity-coverage.md): exemptions, the SCIP resolver, SQLite declarations
- [Capabilities](https://github.com/millstonehq/capcov/blob/main/docs/capabilities.md), [flows](https://github.com/millstonehq/capcov/blob/main/docs/flows.md), [outcomes](https://github.com/millstonehq/capcov/blob/main/docs/outcomes.md), [faults](https://github.com/millstonehq/capcov/blob/main/docs/faults.md)
- [Plugins](https://github.com/millstonehq/capcov/blob/main/src/capcov/PLUGINS.md) for adapters capcov does not ship
- [Development](https://github.com/millstonehq/capcov/blob/main/docs/development.md): the pinned toolchain, running the suite, releases
- [Design decisions](https://github.com/millstonehq/capcov/tree/main/docs/adr) and [foundations](https://github.com/millstonehq/capcov/blob/main/docs/foundations.md)

## Optional REA discovery

[The REA importer](src/capcov_contrib/REA.md) reads saved reverse-engineering
Evidence through the plugin seam. It adds packaged JavaScript application and HAR
API candidates with provenance and unresolved findings. Imported findings never
supply passing runtime evidence or inferred CRUD coverage.

## Contributing

See [CONTRIBUTING.md](https://github.com/millstonehq/capcov/blob/main/CONTRIBUTING.md).
Tests use synthetic fixtures only: no private source, real data or credentials.

## Acknowledgements

Thanks to [Reuben (@pyrex41)](https://github.com/pyrex41) for his contributions to
the claims engine, static evidence, replay verification and incremental caching.

## License

[MIT](LICENSE).
