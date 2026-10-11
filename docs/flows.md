# Flow coverage

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
