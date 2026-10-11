# capcov

Capability coverage: derive what a system can do, map each capability to a test,
and fail the build when the two drift apart in either direction.

The canonical implementation is this repository,
[millstonehq/capcov](https://github.com/millstonehq/capcov). Consumers pin a
release tag or a commit and retain only their models, recipes, adapters specific
to their own application, and evidence. Do not vendor another independently
edited engine.

Standalone capability discovery and model-based testing engine. The distribution,
the Python import package and the command are all named `capcov`. Before 0.2.0
the distribution was named `synapse-capabilities`; see [History](#history).

The engine owns discovery, finite fact-state planning, evidence reconciliation,
coverage gates, and reports. Consumer repositories own reviewed behavior models,
source scope, identities, startup/reset recipes, runtime fixtures, and target
bindings. It needs no hosted service.

## Foundations

Each mechanism has a lineage in testing and program-analysis literature.
`ADR-0001-capcov-literature-foundations` (in the
[Synapse](https://github.com/millstonehq/synapse) repository under
`content/90_Architecture/ADRs/`) records the full map and the resolver decision;
the anchors in brief:

- **Flow model = model-based testing.** Facts + guarded transitions form an
  EFSM/STRIPS model; `plan()` emits one shortest-prerequisite scenario per
  transition ("all-transitions" generation). — Chow 1978; Utting & Legeard 2007;
  Lee & Yannakakis 1996.
- **Obligations = test requirements.** The obligation set is the denominator, and
  each branch outcome is a separate obligation, never collapsed. — Ammann & Offutt
  (2008/2016).
- **Reachability + soundy resolution.** The backward-reachability fixpoint closes
  over *resolved* edges; the adapter resolves what it can syntactically and
  *enumerates* its blind spots rather than guessing. — Livshits et al. 2015
  (soundiness); Shivers 1991 (k-CFA); Samhi et al. 2024; Néron et al. 2015;
  Creager & van Antwerpen 2023 (stack graphs: scale, no precision/recall figure).
- **Static-vs-runtime reconciliation = a Software Reflexion Model.** Declared vs
  observed maps convergence/divergence/absence onto `covered` / `runtime-only` /
  `dead`. — Murphy, Notkin & Sullivan 1995.
- **Capability nouns descend from FODA.** Capabilities as a facet tree; adopting
  its variability structure is a future option. — Kang et al. 1990.

## Install and test

Requires Python 3.12 or newer. From the repository root:

```sh
uv tool install .
capcov --help
capcov flows --help

PYTHONPATH=src uv run python -m unittest discover -s tests -t .
```

The `synapse-capabilities` command is a compatibility alias for `capcov` and is
removed in 0.3.0. Static
discovery, planning, reconciliation, and gates use the Python standard library.
The optional `probe-python` extra supplies SQLAlchemy/pytest integration and the
optional `faults-property` extra supplies Hypothesis for the fault harness; browser
execution uses the consumer's runner and browser installation.

## Two evidence pipelines

Entity coverage compares static discovery with runtime observations:

```sh
capcov discover --target . --out capabilities.json
capcov observe --target . --out observed.json -- pytest -q
capcov reconcile capabilities.json observed.json --out coverage.json
capcov gate coverage.json --exemptions capcov.exemptions.toml
capcov report coverage.json
```

Runtime-only tables remain visible even when the consumer records an exact,
dated exemption explaining their origin (for example a migration framework's
version table). A valid exemption accounts for the corresponding test observation
once; it does not mark the table statically discovered or waive an unknown entry
point. Missing reasons, changed cells and obsolete exemptions still fail. The
legacy `orphan_tests` report key means a test observed an undeclared entity; one
inventory cannot prove that the entity was deleted. Do not infer historical
removal from that label or treat an accounted gap as a demonstrated business outcome.

### Optional SCIP resolver (`--resolver scip`)

`capcov discover` resolves its call graph with a stdlib-only AST pass by default.
`--resolver scip` sources that graph from a SCIP indexer instead — type-aware,
cross-file and multi-language, which recovers call chains (and so entity
bindings) the AST pass cannot follow across module and interface boundaries:

```sh
capcov discover --target . --resolver scip --out capabilities.json
```

This is a hybrid, not a replacement. The AST pass still runs and stays the
enumerator of blind spots (`getattr`/`eval`/dynamic import) and the fallback,
because SCIP is a black box about its own gaps: it reports the edges it resolved
and says nothing about the sites it could not. capcov recovers that silence by
subtraction, so the artifact carries **both** halves — `scip_resolved_edges` and
the `scip_residue` of call sites SCIP left unresolved, each named with a file and
a line (an untyped `session.add(job)`, a computed `getattr`). Coverage therefore
reports resolved-by-SCIP plus unresolved-enumerated, never a bare number, and the
"name unresolved, do not drop" property holds through the SCIP path. These fields
flow through `reconcile` into `coverage.json` alongside the existing cells.

The resolver depends on **external command-line tools**, not Python packages, so
it is opt-in and there is no pip extra for it. `capcov` and its non-SCIP tests
run with none of them installed; `--resolver scip` fails with a named, actionable
error when a tool is absent. Install, per target language:

- a SCIP indexer — Python: `npm install -g @sourcegraph/scip-python`; Go:
  `go install github.com/scip-code/scip-go/cmd/scip-go@latest`;
- the `scip` CLI (reads an index as JSON) —
  `git clone --depth 1 https://github.com/sourcegraph/scip.git && cd scip && go build -o scip ./cmd/scip`.

capcov finds the indexer on `PATH` and locates the `scip` CLI via `$SCIP_CLI`, a
binary dropped at `src/capcov/scip/vendor/scip`, or `PATH`.

Flow coverage retains the source obligation denominator and checks observed
outcomes against a reviewed behavior model:

```sh
capcov flows discover discovery.json --out inventory.json
capcov flows catalog inventory.json --out catalog.json --report catalog.md
capcov flows plan model.json --target local --out plan.json
capcov flows run plan.json --inventory inventory.json --config discovery.json --out run.json -- node runner.mjs
capcov flows coverage inventory.json model.json plan.json --run run.json --out coverage.json
capcov flows report coverage.json plan.json --out report.md
capcov flows gate coverage.json
```

Assertions use `mode: "text"` by default and require visible matching text. A
negative assertion can instead require that its selector match nothing:

```json
{"op": "assert", "mode": "absent", "id": "no-privileged-control", "selector": "form[data-privileged]"}
```

An absence assertion carries no `text` field. Unknown or contradictory modes
fail planning. Its unique assertion ID is still mandatory execution evidence in
every applicable step; declaring absence in the model is not a passing result.
Consumer runners must implement zero matches (including hidden elements for a
browser), not merely invisibility, and record the ID only after the check passes.
Existing text assertions retain their current contract.

A consumer may support bounded refresh while awaiting an asynchronous outcome:

```json
{"op": "assert", "id": "review-ready", "selector": "main", "text": "Ready for review", "refresh_timeout_ms": 20000}
```

`refresh_timeout_ms` is an integer from 1 through 60000, valid only on assertions.
It requests repeated reads of the current view until the assertion succeeds,
under one total deadline covering refreshes, checks and delays. It must never
repeat earlier action commands or resubmit a mutation. Browser consumers must
restrict refresh to a reviewed, same-application GET view and fail if navigation
leaves that view or origin. Consumers that cannot honor this contract must reject
the option. A timeout or unsuccessful refresh supplies no assertion evidence;
planning itself performs no waiting, browser work or network requests.

Browser consumers can select a native option by its exact value and attach one
or more fixture files:

```json
{"op": "select", "selector": "select[name=vendor]", "value": "fixture-vendor"}
{"op": "upload", "selector": "input[type=file]", "files": ["fixtures/invoice.png"]}
```

`select.value` must be a string (including an empty option value). `upload.files`
is a nonempty list of repository-relative POSIX paths, without absolute paths,
drive prefixes, backslashes, empty/dot/parent components or NULs. Both commands
require a selector and retain their exact inputs in the plan. Neither is an
assertion: a binding still needs a separate observable outcome assertion.

Planning does not read or upload files. A consumer must resolve each file within
its declared repository root, reject escaping symlinks, and require its content
hash in the run's source inventory before transfer. Uploads belong to the intended
application origin; redirecting to authentication must not change the destination
of fixture data. Unsupported consumer operations fail execution rather than being
silently ignored. The model does not invent upload contents or vendor/store values.

Missing adapters, unconfirmed meaning, absent bindings, unreachable states,
unmapped obligations, and insufficient evidence remain gaps. A baseline can
permit reviewed gaps without calling them covered. A passing browser navigation
does not prove persistence, authorization, external delivery, or product parity.

Route branch and exception candidates retain their parent HTTP surface. A mapped
candidate requires that parent request in the same execution step, together with
the declared outcome assertions; observing the route in another step is not
enough. Missing or invalid parent references fail reconciliation. The candidate
ID is a source obligation, never a URL the runner should fabricate. HTTP presence
alone does not distinguish which branch ran: consumer-reviewed assertions still
have to establish the intended outcome.

Source code in any tree-sitter-supported language is read by the single
`treesitter-routes` adapter (install the `treesitter` extra). The opinion of
which calls are routes is a config-supplied tree-sitter query, so one adapter
serves Go, JavaScript, Python and the rest -- there is no per-language reader. It
sees literal string arguments in the nodes the query matches: a path built by
concatenation or held in a variable is invisible and becomes an unresolved
dynamic-route obligation. Unlike a regex it is comment- and syntax-aware, so a
commented-out call never matches. When the query captures the enclosing handler
as `@handler`, its body is walked for branch and exception candidates; the node
types that count are config-driven (`branch_nodes`, `exception_nodes`) with
per-language defaults. Four route-reading limits are emitted once for the
combined inventory.

The same source read at more than one mount point is namespaced with distinct
`id_prefix` values, which keeps each mount's surface and branch IDs separate;
overlapping declarations of the same ID remain an error. A `mount` composes a
router prefix into every path of an entry (one entry per mounted router), so the
surface id is the path the runtime serves rather than the literal in the file. An optional `methods`
allowlist records a matched, route-shaped candidate whose verb is outside the set
under `excluded_surfaces` -- the query saw it and the verb filter dropped it, so
the narrowed denominator stays legible instead of implied by absence. This is a
conservative source rule, not proof of runtime mounting or framework object
identity. Runtime reconciliation remains required.

## OpenAPI operation inventory

An HTTP service in any language can supply a local OpenAPI JSON document:

```json
{
  "scope": "Declared API operations; runtime and business behavior unconfirmed",
  "root": ".",
  "adapters": [
    {"kind": "openapi-json", "document": "openapi.json", "prefix": "/v1"}
  ]
}
```

The adapter supports OpenAPI 3.0.x and 3.1.x JSON and inventories all eight inline
HTTP operation methods. Each surface retains the document hash and JSON pointer;
line 1 identifies the document, not an inferred operation line. The optional
prefix defaults to empty and must be reviewed against the deployment. Server URLs
are never fetched or selected. Export the contract locally using the application's
own recipe; this adapter performs no network requests and adds no dependencies.

This is an operation inventory, not a full OpenAPI validator. It does not resolve
Path Item references or invent methods for empty/filtered paths: each stays an
explicit gap. It also retains boundaries for runtime/omitted routes, business
outcomes, authorization/configurations, schema/reference behavior, server bindings,
and callbacks/webhooks/extensions. Descriptions, examples, operation IDs and server
values are not copied into reports. Duplicate JSON members, unsupported versions,
and invalid path/operation shapes fail discovery. Equivalent templated paths
(`/{id}` beside `/{optionId}`) do not: every operation is still inventoried and
the collision is recorded as a `path-shape-collision` boundary carrying both paths,
since the document does not say which serves a concrete path.
The supported method and Path Item rules come from the
[OpenAPI 3.1 specification](https://spec.openapis.org/oas/v3.1.0.html#path-item-object).

Run the existing `flows discover` and `flows catalog` commands on this config.
Operations become candidate flow families with missing outcomes, fixtures and
bindings. Whole-document declaration accounting, reference resolution and behavioral
completeness remain false. A fresh exported contract hashes the snapshot; it does not prove the
snapshot belongs to the running build. The consumer must bind export, build and
execution provenance in its local recipe.

Keep contract and source inventories separate when comparing them: overlapping
HTTP surfaces still fail if configured in one inventory. Agreement does not prove
completeness, and differences need investigation. A contract may expose inherited
administration and authentication operations outside the selected source-flow
scope. Those additional operations receive no coverage merely from discovery.

## Current support

- Python/FastAPI/SQLAlchemy entity discovery and runtime probes.
- Opt-in literal and shared-constant SQLite declarations, with unbound query diagnostics.
- Tree-sitter route source obligations in any supported language, and OpenAPI JSON operations (one config of the generic structured-spec reader).
- Required/forbidden fact-state planning with explicit blocked transitions.
- Consumer execution commands with fresh run nonces and source/model/plan hashes.
- Exact scenario/assertion reconciliation, per-step HTTP evidence, and gap gates.

Go/React discovery, a general-purpose browser exploration agent, and automatic
semantic inference are not implemented.

### Direct SQLite declarations

For a Python target using direct SQLite calls, add to `capcov.toml`:

```toml
[capcov]
adapter = "python-fastapi-sqlalchemy"
source = "src"
sqlite_ddl = true
```

This adds literal `CREATE [TEMP] TABLE [IF NOT EXISTS] name (...)` declarations
inside `execute`, `executemany`, and `executescript` calls to the entity inventory.
It reads Python/SQL tokens without importing or running the application. Comments
and string contents cannot create table declarations. Declaration records carry
file/line evidence and `declaration_kind = "sqlite_literal_ddl"`; duplicate table
names retain their first declaration (an existing ORM declaration takes priority).

A direct module-level string assignment or annotated string assignment can also
supply the SQL argument. These declarations carry `sqlite_constant_ddl` and their
calls retain `constant_sql_unbound` diagnostics. Resolution requires one binding
with no reassignment, deletion, import or lexical shadow anywhere in that module.
Even shadowing in an unrelated function leaves the name unresolved. Conditional
initialization, aliases, imported constants, string computation and SQL wrappers
remain outside this conservative subset. This is static lexical resolution, not
proof that runtime monkeypatching or dynamic code cannot replace a value.

Literal module mappings of tuples are also supported when a `for` loop unpacks
`SCHEMAS.values()` and its first statement passes a string tuple element directly
to a SQLite execution call. Other reads of the mapping, aliases, mutations,
shadowing, computed entries, duplicate keys and ambiguous unpacking remain
unresolved. This pattern also retains `constant_sql_unbound`; no query or route
binding is inferred from the declaration.

Opting in asserts these SQL-shaped method calls are relevant to the target; this
pass does not infer the receiver's runtime type. It deliberately creates no CRUD
or route binding. Every candidate call stays in `blind_spots`: literal statements
as `literal_sql_unbound`, computed SQL or ORM expressions as
`computed_sql_or_expression`. Parameter values and SQL text are not emitted.
Unsupported declarations (virtual tables, `AS SELECT`, single-quoted names),
external SQL files, and connections outside the source scope remain outside this
declaration subset. Schema qualifications are retained, but entity keys do not
distinguish separate databases with identical table names.

The runtime probe still observes SQLAlchemy, not direct `sqlite3` operations.
New declarations with neither route bindings nor runtime evidence correctly stay
in the `neither` cell. This feature expands the denominator; it does not establish
SQLite coverage or solve query/receiver analysis.

## Specified, implemented and exercised capabilities

`capcov capabilities` compares three sets of capability IDs and gates the
differences. An ID is `<area>/<requirement>/<scenario>`, each segment lowercase
with runs of other characters turned into `-`. A shorter ID applies to every ID
beneath it, so a log line naming `area/requirement` covers each scenario of that
requirement. `capcov.capabilities.ids` has the helpers (`slug`, `capability_id`).

| Set | Adapter | Reads |
| --- | --- | --- |
| specified | `openspec` | `<root>/**/spec.md`: each `### Requirement:` and each `#### Scenario:` under it. The directory holding `spec.md` is the area. A requirement with no scenario is itself an ID. |
| implemented | `[[capabilities.implemented]]` entries | the surfaces `capcov discover` wrote to `capabilities.json`, mapped to IDs by glob (`surface`), regex (`pattern`, named groups fill `{name}` in `capability`) or `plugin` |
| exercised | `[[capabilities.exercised]]` entries | log files, one record per line, each classified `pass` or `fail` |

```toml
[capabilities.specified]
kind = "openspec"                 # or plugin = "package.module:callable"
root = "openspec/specs"           # default

[[capabilities.implemented]]
surface = "POST /v1/sessions"
capability = "sessions/open-a-session"

[[capabilities.exercised]]
name = "audit"                    # label in reports, exclusions and --input
glob = "logs/*.log"               # relative to the project root
format = "logfmt"                 # logfmt | json | regex (with pattern = '(?P<x>...)')
match = { contains = "audit" }    # lines of interest; also { regex = "..." }
where = []                        # conditions every line must meet
capability = "sessions/{action}/{target}"   # fields are slugged into the ID
pass_when = [{ field = "result", op = "eq", value = "ok" }]
fail_when = [{ field = "result", op = "eq", value = "error" }]

[[capabilities.exclude]]
capability = "sessions/close-a-session/*"   # fnmatch over the finding's ID
kind = "specified-unexercised"              # optional; absent waives every kind
reason = "close ships next"                 # required
```

A condition is `{ field, op, value }` with `op` one of `eq`, `ne`, `gt`, `gte`,
`lt`, `lte`, `in`, `contains`, `exists`. A condition on a field the record lacks
is false for every operator. `fail_when` wins; with no `pass_when` every other
attributed line passes. A JSON access log works the same way, for example
`format = "json"` with `fail_when` on `status` `gte` 500 and on `response_flags`
`ne` `"-"`.

A log format capcov does not parse belongs in a plugin, not in capcov. An
`exercised` entry with `plugin = "package.module:callable"` is called per line as
`callable(line, source)` and returns `None`, one record or a list of records
`{"capability": id, "result": "pass" | "fail", "detail": optional text}`.
An `implemented` plugin is called `callable(surfaces, config)` and returns
`{"capability", "surface"}` records. A `specified` plugin is called
`callable(target, config)` and returns `{"capabilities": [{"id": ...}], "unresolved": []}`.

```console
capcov capabilities check --target . [--discovered capabilities.json] [--input audit=run.log] [--json]
```

`specified`, `implemented`, `exercised` and `reconcile` run one step each;
`check` runs all of them. Files go to `capabilities-out/`: `specified.json`,
`implemented.json`, `exercised.json` and `reconciliation.json`, which holds the
findings, a row per specified ID and the gate verdict under `gate`.

| Finding | Meaning |
| --- | --- |
| `specified-unimplemented` | a specified ID no implemented ID covers |
| `implemented-unspecified` | an implemented ID outside the specification, or a discovered surface no entry maps (`unmapped:<surface>`) |
| `specified-unexercised` | an implemented, specified ID no log record shows running |
| `exercised-failing` | an exercised ID with at least one failing record, with counts and the first failing lines |

`exercised-unspecified` is listed but does not gate. Adapter problems are named
in `unresolved` and gate under their own ID: a duplicate scenario, a spec tree
with nothing in it, a source with no log file (`no-input:<name>`), selected lines
that gave no capability or result (`unattributed:<name>`), an entry matching no
surface. An exclusion with no reason does not apply, and one that waives nothing
fails. Exit status is 0 on pass, 1 on failures, 2 on a configuration error.
A passing gate says the three sets agree. It does not say the system is correct.

## Fault-injection harness

`capcov.faults` is test support for a program that writes to a primary store, an
object store and an external service without conditional writes or idempotency
keys. It records an operation history with `ok`, `fail` and `info` results (the
convention of Jepsen histories), injects faults through an in-memory fake
service that keeps ground truth, crashes the program before and after every side
effect, enumerates bounded schedules, and judges each run with a rule pack
evaluated by the `capcov.claims` kernels.

```sh
capcov faults check --history h.json --finals f.json [--engine python|both]
uv run --frozen --extra faults-property sh scripts/check-fault-harness.sh
```

`check` exits 0 when every invariant is supported, 1 when any is violated or
unresolved, and 2 on invalid input. The script runs the worked example in
`examples/fault-notes`, then applies two patches to temporary copies. It requires
the harness to reject a resend after an unknown outcome as `effect_applied_twice`
and to leave a run judged with a write still pending `unresolved`.

The harness uses the standard library only and is never imported by `discover`,
`reconcile` or `gate`. Hypothesis is the optional `faults-property` extra. The
API, the relations, the six invariants and the limits are in
`src/capcov/faults/README.md`.

## Development and releases

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

To release, update `version` in `pyproject.toml` and `src/capcov/__init__.py`,
refresh `uv.lock`, and merge the reviewed change. Push a tag `v<version>` at that
commit. CI checks the tag against the package version and publishes the tested
wheel and source distribution as GitHub release assets. Tags are immutable; use a
new version for a correction. PyPI publishing is not configured.

## Using capcov from another repository

Pin a release tag. Three ways, by toolchain:

With uv, as a project dependency:

```toml
[project]
dependencies = ["capcov"]

[tool.uv.sources]
capcov = { git = "https://github.com/millstonehq/capcov.git", tag = "v0.2.0" }
```

With Nix, as a flake input, so the command is in the dev shell and in CI without
an install step:

```nix
{
  inputs.capcov.url = "github:millstonehq/capcov/v0.2.0";

  outputs = { nixpkgs, capcov, ... }: {
    # in a dev shell:
    #   packages = [ capcov.packages.${system}.default ];
    # with the tree-sitter adapters:
    #   packages = [ capcov.packages.${system}.capcov-treesitter ];
  };
}
```

`nix run github:millstonehq/capcov/v0.2.0 -- --help` runs it without installing.

In GitHub Actions, the action at the same tag installs the tool from that tag's
source and puts `capcov` on `PATH`:

```yaml
- uses: millstonehq/capcov@v0.2.0
  with:
    extras: treesitter   # optional
- run: capcov discover --target . --out capabilities.json
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, the checks to run and how a
change made here reaches a release.

Keep engine changes and regression tests here. Application models, source exports,
identities, startup recipes, and execution evidence belong in the consuming
repository. Conformance tests use synthetic fixtures; do not add private source
or real credentials. Run the unittest suite and build/install checks before a PR.
The engine is distributed under the repository's MIT license, included in both
Python distribution formats.

## Many worktrees, many agents, one machine

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

## Scoped outcome coverage with pytest

### Test capcov's own advancement gate

From the repository root, run:

```sh
uv run --frozen --with pytest sh scripts/check-backpressure.sh
```

The checked-in `capcov.toml` and `capcov.outcomes.json` bind a small set of capcov's
own acceptance behaviors to exact tests. The script discovers the actual source
and runs `outcomes check` in disposable copies. Its host creates an `advanced`
marker only on exit zero. It verifies three executions: a passing baseline, an
omitted required check, and a source mutant that suppresses required browser-flow
failures. The negative cases must return 1 and name the intended missing/failed
outcome; an unrelated error is not a successful fault control. The script verifies
cleanup and fails if any expectation is unmet. The existing package CI job runs
this same command before building release artifacts.

This establishes local host enforcement for these capcov obligations. It does not
qualify every capcov feature, prove resistance to an agent rewriting its protected
tests/host, or measure autonomous repair/convergence. Ordinary runs retain no JSON
receipts in the repository; CI retains the terminal command output.

### Bind consumer outcomes

Use `capcov outcomes` to keep business outcomes separate from entity reachability.
The consumer owns a JSON map of stable capability/outcome IDs to **exact pytest
node IDs**, including parameter IDs. Every mapped case must pass setup, call, and
teardown; unrelated passing cases cannot satisfy an outcome. Skips and expected
failures are inconclusive. An unresolved product rule remains unresolved even
when its characterization test passes.

This initial integration supports pytest directly. It does not define a general
harness API or require pytest in the core engine's environment. The target Python
interpreter must have pytest and the application's test dependencies installed.

```json
{
  "version": 1,
  "scope": "orders/cancellation/local-api",
  "environment": {"database": "temporary SQLite", "client": "API test client"},
  "limitations": ["No production database concurrency or external delivery evidence"],
  "inputs": ["src", "tests", "pyproject.toml", "uv.lock"],
  "outcomes": [{
    "id": "cancel.ownership",
    "capability": "orders.cancel",
    "description": "Another account cannot cancel or mutate this order",
    "source_refs": ["POST /orders/{order_id}/cancel"],
    "policy": "required",
    "tests": ["tests/test_cancel.py::test_other_account"]
  }]
}
```

`source_refs` must exist in the supplied discovery inventory. This is an authored
semantic mapping: the engine checks identities and execution results, not whether
the assertion correctly expresses the business requirement. Review test meaning
and fixture limitations with the map. An outcome can have an empty `tests` list;
it will remain missing. Use `policy: "unresolved"` with a `reason` for pending
product decisions. There are no exemptions which turn such outcomes into passes.

Run from the consumer project:

For an advancement gate, use the single fresh execution-and-check command:

```sh
capcov outcomes check capcov.outcomes.json --inventory .capcov/inventory.json \
  --python .venv/bin/python --timeout 180 --out .capcov/outcomes-run.json
```

First generate the inventory with `capcov discover` as below. `check` always runs
the union of mapped test IDs, accepts no selection arguments, and returns zero
only when **every authored outcome is demonstrated** with a clean session. Missing
bindings, skips, unresolved outcomes, failures, and timeouts prevent acceptance.
Even an inherited pytest filter that runs a passing subset cannot discharge the
missing cases. The output is the usual run receipt, suitable for `coverage`.
Invalid/stale inputs return 2; valid but unqualified execution returns 1.

Have the existing CI/agent host advance only on exit zero. A host that ignores the
exit code is not enforcing this gate. Keep the accepted map, checker, dependencies,
and invocation under the host's existing trusted revision/review controls; a
candidate that can rewrite those controls can bypass them. This command prevents
accidental old-receipt reuse by executing afresh; it does not sandbox hostile tests
or authenticate their external observations. It has one bounded execution, not
an autonomous retry loop. The caller must bound any retries across invocations.

For diagnostic selection or separate report generation, the existing commands remain:

```sh
capcov discover --target . --out .capcov/inventory.json
capcov outcomes run capcov.outcomes.json --inventory .capcov/inventory.json \
  --python .venv/bin/python --out .capcov/outcomes-run.json
capcov outcomes coverage capcov.outcomes.json --inventory .capcov/inventory.json \
  --run .capcov/outcomes-run.json --out .capcov/outcomes-coverage.json
capcov outcomes gate capcov.outcomes.json --inventory .capcov/inventory.json \
  --run .capcov/outcomes-run.json
```

A failed pytest run still writes evidence for reporting; arrange CI to run coverage
and gate after that failure. `run` returns 1 for unsuccessful/incomplete execution;
`coverage` returns 0 when a valid report is produced, even with gaps; `gate` returns
1 unless all scoped outcomes are demonstrated and the session is clean. Invalid
or stale evidence returns 2. Commands after `--` on `run` are pytest selections or
options; otherwise it runs the union of mapped node IDs.

`run` success is execution success, not acceptance: a passing selection can still
leave required outcomes missing. Use `check`, or explicitly invoke `gate` after
`run`, before advancing. Printed success messages from tests do not override the
pytest setup/call/teardown results collected by the probe.

The browser path now also preserves required scenario failures through
`observe --probe browser` → `reconcile` → `gate`. A passing scenario on a shared
route cannot hide a failed/missing scenario. Structural exemptions cannot waive
these failures. Duplicate scenario IDs and nonzero runner exits are rejected;
empty, blocked, assertionless, diagnostic, and `--only` executions cannot qualify
the full plan. Author a separate bounded model to qualify a smaller capability.
Older browser observation files without required-flow results must be regenerated.
Browser runners remain trusted assertion observers; this is not proof that an
arbitrary runner truthfully reports SQL, SMTP, or browser effects.

The report distinguishes `demonstrated`, `failed`, `missing`, `unresolved`, and
`inconclusive`. It is complete only **within the authored scope and environment**;
it does not close unmapped source obligations, discovery blind spots, or the
separate entity/flow gates. A missing test file can cause pytest collection to
abort; all outcomes without executed evidence then remain missing.

Runs use a private result path and fresh nonce. Source inventory, map, declared
input files (including mapped tests), and engine code are hashed; coverage/gate
recompute them against the current checkout. Include fixtures, test configuration,
dependency locks, and other assertion inputs in `inputs`. These checks prevent
accidental stale evidence reuse; the runner/test code is trusted, and this is not
cryptographic attestation of an external service or proof of the deployed build.
Execution/reset/cleanup remain the consumer fixture's responsibility. A timeout
fails the run; consumers must own cleanup for subprocesses their fixtures start.


A download can itself be an observable assertion against a reviewed expected file:

```json
{"op": "assert", "mode": "download", "id": "export-bytes", "selector": "a.export", "file": "fixtures/expected.csv"}
```

The expected file uses a canonical repository-relative POSIX path, with the same
confinement and enrolled-byte requirements as upload fixtures. The consumer must
perform the selected download, require successful completion and compare its
complete bytes with the checked expected bytes before emitting the assertion ID.
A link, filename, prefix match, or successful HTTP response alone is insufficient.
`text` and `refresh_timeout_ms` are invalid for this mode; download actions are
never automatically retried. The consumer owns origin restrictions and bounded
transfer/size handling, and must reject the mode if it cannot honor that contract.
Planning neither reads the expected file nor downloads anything.


A journey can retain a runtime-created resource path and revisit it after an
identity change:

```json
{"op": "remember-path", "name": "invoice"}
{"op": "visit-path", "name": "invoice", "expect_status": 404}
```

Names match `[a-z][a-z0-9_-]{0,63}`. `visit-path` requires an integer expected HTTP
status from 200 through 599. Both are actions; a binding still needs a separate
observable assertion. They accept no alternate path, selector or value.

Consumers must scope saved paths to one scenario, reject missing names and
redefinitions, retain only a reviewed same-application path (no credentials, query
or fragment), and revisit it with a bounded GET. The final URL must remain the
saved resource and the final response must match `expect_status`; a login redirect
is not a resource-access refusal. Invalid captures and unresolved names fail,
without a guessed/default path. Models must describe the capture prerequisites
and identity changes. Planning does not invent a resource URL or perform requests.


A reviewed direct HTTP response can be an assertion, including a denied mutation:

```json
{"op":"assert","mode":"http","id":"price-refused","method":"POST","path":"/items/1/base","form":{"price":"4.99"},"expect_status":403,"text":"not permitted"}
```

This initial contract supports GET and form-encoded POST on literal application
paths composed of letters, digits, underscores, hyphens and single slashes. It
excludes queries, fragments, encoded paths, alternate origins, arbitrary headers
and raw bodies. GET has no form. Form keys/values are strings with bounded sizes.
Expected statuses are 200–299 or 400–599; response text must be nonempty.

Consumers must use the scenario's current authenticated session, confine the
request to a reviewed application endpoint, send it once, reject redirects, and
bound both transfer time and complete response bytes. They must verify exact
response URL/status and the declared body text before recording assertion
evidence, and record the actual request for route reconciliation. A DOM assertion
elsewhere is not response evidence. No retries or refresh are permitted, including
for writes. The planner performs no requests. An expected denial alone does not
prove absence of side effects; model follow-up state checks where required.


Planning defaults to 10,000 reachable fact states. For larger reviewed models,
use `capcov flows plan model.json --target local --max-states 20000 --out plan.json`.
The positive integer limit bounds exploration; exhaustion still fails without
emitting a complete plan. Non-default limits are recorded in the plan and used
when coverage independently re-derives its scenarios. Raising this budget adds
no coverage and does not waive unreachable targets or missing evidence. Default
plans retain their existing artifact shape.


A click may declare a native browser confirmation explicitly:

```json
{"op":"click","selector":"button.delete","confirmation":{"message":"Delete this sample?","action":"dismiss"}}
```

The contract requires an exact nonempty message of at most 1024 characters and
an `accept` or `dismiss` action. It is valid only on `click`; extra confirmation
keys are rejected. The declaration is preserved in the plan and bound to run
evidence. It does not itself grant coverage or replace subsequent outcome
assertions. A browser consumer must wait for a native `confirm` dialog caused by
that click, match the message exactly, apply the declared action, and fail on a
missing or mismatched dialog within a bounded deadline. It must not silently
accept prompts, alerts or arbitrary confirmation messages. Planning does not
open or answer a browser dialog.


Run evidence may declare `execution_scope: "diagnostic"` when a consumer selects
only one journey for debugging. Such a run can have `status: "passed"`, meaning
the selected execution succeeded, but reconciliation awards **zero coverage**,
does not resolve runtime-mount boundaries, and remains incomplete. This applies
even if the diagnostic happens to execute every planned scenario. Missing scope
defaults to `"full"` for existing runners; an explicit scope must be `"full"` or
`"diagnostic"`, otherwise reconciliation rejects it. Consumers must preserve the
full plan and its digest when selecting diagnostic scenarios and retain all steps
of a selected journey. This flag neither selects scenarios nor supplies their
prerequisites; selection remains a consumer runner responsibility. Plan, source
identity, assertion and request-evidence checks still apply.

An element-relative mouse drag is explicit as well:

```json
{"op":"drag","selector":"#drawing","from":[0.1,0.2],"to":[0.8,0.7]}
```

Coordinates are numeric x/y fractions of the rendered element bounds, from zero
to one. Both pairs are required and must differ; booleans, non-finite values and
out-of-bounds coordinates are rejected. Consumers must resolve the element,
bring it into view, verify that both endpoints can be hit within it, and perform
an actual mouse press/move/release. They must release the button on failure and
must not replace the gesture with direct application-state mutation. Subsequent
assertions still establish the outcome; declaring a drag grants no coverage.

## History

capcov was developed as `packages/capabilities` in
[millstonehq/synapse](https://github.com/millstonehq/synapse), under the
distribution name `synapse-capabilities`. This repository starts from that
directory at synapse commit
[`649be7f84f5cb191531e3f65688615f16f55bb65`](https://github.com/millstonehq/synapse/tree/649be7f84f5cb191531e3f65688615f16f55bb65/packages/capabilities); <!-- a commit id, not a credential. gitleaks:allow -->
the earlier history is in that repository. The experiment plan that source comments
cite as `EXPERIMENT-PLAN.md` remains there; `ADR-0001` is in `docs/adr`.

Boundary vectors and vector claims (`capcov.vectors`) were written by
[@pyrex41](https://github.com/pyrex41) in
[millstonehq/synapse#53](https://github.com/millstonehq/synapse/pull/53) and
[millstonehq/synapse#54](https://github.com/millstonehq/synapse/pull/54).

The observation judge (`capcov.claims.observation`), the replay judge module
behind `scripts/compiled_checker.py` with its `--evaluator` and
`--reviewer-admissions` options, and the operation-scoped checker authority
they rest on were written by [@pyrex41](https://github.com/pyrex41) on the
`integration/luna-wave-20260917` branch of
[pyrex41/synapse-capcov](https://github.com/pyrex41/synapse-capcov) at
`38d39b12760a9dd5edb81a5753d5eed5faf61e4d`. <!-- a commit id, not a credential. gitleaks:allow -->
