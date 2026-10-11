# Entity coverage

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

## Optional SCIP resolver (`--resolver scip`)

`capcov discover` resolves its call graph with a stdlib-only AST pass by default.
`--resolver scip` sources that graph from a SCIP indexer instead. That graph is type-aware,
cross-file and multi-language, which recovers call chains (and so entity
bindings) the AST pass cannot follow across module and interface boundaries:

```sh
capcov discover --target . --resolver scip --out capabilities.json
```

This is a hybrid, not a replacement. The AST pass still runs and stays the
enumerator of blind spots (`getattr`/`eval`/dynamic import) and the fallback,
because SCIP is a black box about its own gaps: it reports the edges it resolved
and says nothing about the sites it could not. capcov recovers that silence by
subtraction, so the artifact carries **both** halves: `scip_resolved_edges` and
the `scip_residue` of call sites SCIP left unresolved, each named with a file and
a line (an untyped `session.add(job)`, a computed `getattr`). Coverage therefore
reports resolved-by-SCIP plus unresolved-enumerated, never a bare number, and the
"name unresolved, do not drop" property holds through the SCIP path. These fields
flow through `reconcile` into `coverage.json` alongside the existing cells.

The resolver depends on **external command-line tools**, not Python packages, so
it is opt-in and there is no pip extra for it. `capcov` and its non-SCIP tests
run with none of them installed; `--resolver scip` fails with a named, actionable
error when a tool is absent. Install, per target language:

- a SCIP indexer. Python: `npm install -g @sourcegraph/scip-python`; Go:
  `go install github.com/scip-code/scip-go/cmd/scip-go@latest`;
- the `scip` CLI, which reads an index as JSON:
  `git clone --depth 1 https://github.com/sourcegraph/scip.git && cd scip && go build -o scip ./cmd/scip`.

capcov finds the indexer on `PATH` and locates the `scip` CLI via `$SCIP_CLI`, a
binary dropped at `src/capcov/scip/vendor/scip`, or `PATH`.


## Direct SQLite declarations

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
