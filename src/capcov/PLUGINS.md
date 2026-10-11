# capcov adapter plugins — bringing your own reader

capcov ships two GENERIC adapters: `treesitter-routes` (any source with a
tree-sitter grammar) and `structured-spec` (any declarative contract, OpenAPI
being one profile). They are two generic mechanisms, not a ban on bespoke ones.

A clean-room rebuild *out of* a legacy or low-code platform — Zoho Deluge,
Salesforce Apex, COBOL, a Retool/Airtable export — starts from a source with **no
tree-sitter grammar and no structured form**. That source needs a bespoke reader.
capcov takes one as a **plugin at the edge**: it lives in a consumer repo or a
contrib package, and an adapter entry names it. The core owns the adapter
contract, the two generic readers and the loader; a bespoke reader is never
special-cased into the core engine.

## Declaring a plugin

Any adapter entry may carry a `plugin` key:

```
plugin = "dotted.module:callable"
```

When `plugin` is given, capcov imports `callable` from `dotted.module` and invokes
it **instead of** dispatching on a built-in `kind`. `kind` (flows path) / the
adapter `name` (core path) becomes a **free label** — used only for provenance and
error text, no longer required to match a built-in.

When an entry's `kind`/`name` is neither a built-in nor accompanied by a `plugin`,
capcov raises a clear error that names this seam rather than dropping the source
silently. "capcov cannot read this source" stays visible.

The dotted string is `module_path:attribute`. A malformed string, a missing
attribute, or a non-callable target raises a specific error.

## The two callable contracts

A plugin produces the **same per-adapter result the built-in of its path
produces**, and capcov merges it **identically** — so a plugin's honest-denominator
carriers (`excluded_surfaces`, `unresolved`) reach the coverage denominator
unchanged, exactly as a built-in adapter's do.

### FLOWS path — `flows.discovery._discover_from_config` / `flows.discovery.discover(config)`

This is the path a consumer's own product-flow tooling drives via `discover(config)`.

```python
def reader(adapter: dict, root: pathlib.Path) -> dict:
    ...
```

* `adapter` — the adapter entry from the discovery config (its `plugin` string
  and any bespoke keys the reader defines). `kind`, if present, is a free label.
* `root` — the resolved discovery root (`(base / config["root"]).resolve()`), the
  same directory the built-in readers read their sources under.

Return a dict:

| key                 | required | shape                                                                                   |
| ------------------- | -------- | --------------------------------------------------------------------------------------- |
| `obligations`       | yes      | list of `{"id": str, "kind": str, "source": {"file": str, "line": int}, **extra}`. A discovered surface is `kind == "surface"` (with optional `"method"`, `"handler"`); a limit the reader cannot resolve is `kind == "unresolved"`. |
| `excluded_surfaces` | no       | list of `{"file", "line", "method", "path", "reason", ...}` — candidates the reader saw and deliberately dropped. Flows through verbatim (carries the denominator honestly). |
| `unresolved`        | no       | list of `{"kind", "reason", ...}` — the reader's named limits. The seam stamps this entry's index as `adapter` and the free label as `kind` when the plugin omits them; any value the plugin supplies is preserved. |
| `sources`           | no       | `{relative_path: sha256}` for files the reader consumed; recorded into the inventory's `sources` and marked analysed. |

The returned `obligations` are appended, then the same generic honesty check every
built-in gets applies: a plugin that yields no `surface` obligation is itself named
under `unresolved` ("adapter declared but produced no surface obligations"). Ids
must be unique across the whole run (the existing duplicate-id invariant).

### CORE path — `adapters.load(name, plugin=...)` (for `capcov.toml [[adapters]]` parity)

```python
def reader(source_root: pathlib.Path, target: pathlib.Path, config: dict) -> dict:
    ...
```

* `source_root` — the resolved source root (`target / [capcov].source`).
* `target` — the project root (the directory holding `capcov.toml`).
* `config` — the `[[adapters]]` entry (its `plugin` string and any bespoke keys),
  or `None` for a bare invocation.

Return a **CORE adapter dict** — the same shape a built-in core adapter emits.
Build it with `capcov.adapters.build_core_dict(surface_records, excluded_surfaces,
unresolved)`, which fills the required-but-empty call-graph keys and carries
`excluded_surfaces` (`{"count", "surfaces"}`) and `unresolved` (a list) as
first-class top-level keys. `adapters.merge` folds several adapters (built-in or
plugin) into one and preserves those carriers unchanged. A duplicate surface id or
entity name across adapters is refused loudly, never silently clobbered.

`capcov.toml` wires it by naming the plugin on the entry:

```toml
[[adapters]]
name   = "deluge"                       # free label
plugin = "mycompany.capcov_deluge:read" # the bespoke reader
# ...any keys your reader reads...
```

## Shipped contrib readers

`capcov_contrib` ships bespoke readers that use this seam and are NOT part of the
core. Declare them exactly like a consumer-local plugin:

    [[adapters]]
    name = "laravel-routes"
    plugin = "capcov_contrib.laravel_routes:discover"
    globs = ["routes/**/*.php", "app/**/Routes/**/*.php"]   # default
    mounts = [
      { glob = "routes/api.php", prefix = "/api" },
    ]

`laravel_routes` composes nested `Route::group` / `Route::prefix()->group()`
prefixes (array and fluent forms, including a fluent verb such as
`Route::middleware(...)->prefix(...)->get(...)`) into each route's path, reads
`[C::class, 'm']`, `'C@m'`, `['uses' => 'C@m']`, an invokable `C::class`
(`C@__invoke`) and closures, expands `resource`/`apiResource` (honouring
`only`/`except` in array or variadic form and `->parameters([...])`), and names
what it cannot read: `dynamic-route`, `dynamic-prefix`, `dynamic-parameters`,
`unmounted-file`, `no-surfaces` unresolved entries and duplicate declarations
under `excluded_surfaces`. Requires the `treesitter` extra.

Each per-site unresolved entry carries
`id = "laravel-routes:<file>:<line>:<column>:<kind>"` (the file-level
`unmounted-file` entry carries `"laravel-routes:<file>:unmounted-file"`) so the
gate can exempt ONE obligation at a time; the reader emits at most one entry per
node+kind, and the column keeps two declarations on ONE line from sharing an id,
as `flows.discovery` does.

**Duplicates.** A second declaration of the same METHOD+path is reported under
`excluded_surfaces`, and the FIRST declaration is the surface kept. Laravel's
route collection overwrites on method+URI, so the runtime serves the LAST one:
the retained surface generally carries the shadowed handler and line. Treat a
duplicate exclusion as "check which handler actually serves this", not as noise.

**`Route::any`** emits the five recognised verbs and NOT `OPTIONS` or `HEAD`,
which Laravel registers too. Laravel also registers `HEAD` alongside every
`GET`, so emitting the pair for `any` alone would be arbitrary and would inflate
the inventory with surfaces no probe exercises.

**Mounts.** The outermost prefix is usually declared OUTSIDE the route file. Stock
Laravel mounts `routes/api.php` under `/api` (`RouteServiceProvider` up to
Laravel 10; `apiPrefix` in `bootstrap/app.php` from Laravel 11), and a versioned
API typically mounts each directory under `/api/<version>` from a provider. The
reader cannot see those, so declare them in `mounts`: for each file, the FIRST
matching mount's `prefix` (which must start with `/`) seeds the path. Without
mounts, two versions declaring the same relative path collide and the second is
reported as a duplicate.

A glob that resolves to NO file raises `ValueError` naming it. One mis-cased
entry (`app/Rest/v1/...`) would otherwise leave a whole directory unmounted
while the run still reported zero unresolved -- silently reinstating the bug
mounts exist to fix. Once `mounts` is configured, a routed file matching none of
them is an `unmounted-file` unresolved entry rather than a silent `""` seed;
with no `mounts` at all the empty prefix IS the declared contract and stays
silent.

**A `mounts` list is unconditional; an app may register a route type
conditionally.** A target may gate a whole route type on a debug flag, so
`APP_DEBUG`, so its eleven `/api/test` surfaces are declared by the mount but
are NOT served by a production runtime -- they will read `static_only` in the
four-cell diff. That is guidance for whoever reads the report, not a defect in
the reader: the static inventory is of what the source declares, and the gap is
the environment gate. Say so in the report rather than exempting the surfaces.

**Not read** (each is named, never guessed): `Route::view`, `Route::redirect`
and `Route::fallback` declarations; a verb or `group` call on a receiver other
than the `Route` facade (`$router->get(...)`) -- a `dynamic-route` entry naming
the receiver, its body not walked; a group whose body is a file include
(`Route::group([...], base_path('routes/x.php'))`) -- a `dynamic-prefix` entry,
the included file is read without that prefix only if a glob covers it; a
nested resource name (`photos.comments`) -- a `dynamic-route` entry; a
`->parameters(...)` argument that is not a literal map -- a `dynamic-parameters`
entry naming the fallback.

**The resource wildcard.** It follows `ResourceRegistrar::getResourceWildcard`:
an explicit `->parameters(['work-orders' => 'order'])` wins, otherwise a
singularisation heuristic (`ies` -> `y`; `es` stripped only after
`ss`/`us`/`x`/`ch`/`sh`; else one `s`), and either way the registrar's
unconditional `str_replace('-', '_')` applies, so `work-orders` yields
`{work_order}`. Two classes of name still come out wrong without an explicit
`->parameters([...])`:

* `ies` where the stem is not a `y` word: `movies` -> `movy` (also `series`,
  `species`);
* `-uses`/`-ouses`/`-auses` names, which lose the trailing `e` to the rule that
  fixes `statuses`: `houses` -> `hous`, `warehouses` -> `warehous`, `causes` ->
  `caus`.

English gives no structural way to separate those from `statuses`, so the reader
does NOT trade one mis-fire for another: it keeps the rule, documents the miss,
and reads Laravel's own override when the app declares one.
