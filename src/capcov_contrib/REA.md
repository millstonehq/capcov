# Optional REA discovery importer

REA can inspect packaged incumbents and recorded interactions. This plugin reads
saved REA Evidence; it does not install REA, launch targets, connect to MCP, or
execute payloads. Importing is discovery, never an observation of a capcov test.

## Core pipeline

Save a successful REA record, MCP `structuredContent`, or exported Evidence bundle
inside the source root, then configure:

```toml
[capcov]
source = "."

[[adapters]]
name = "rea"
plugin = "capcov_contrib.rea:discover"
document = "evidence/rea.json"
globs = ["evidence/rea.json"]
# Optional exact input-file pin:
# sha256 = "<64 lowercase hexadecimal characters>"
```

Run `capcov discover --target . --out capabilities.json`. `globs` must include
`document`: the existing engine and probes must hash the imported file for
provenance. No new dependency or core adapter registration is required.

For flow inventory, use the same keys in a discovery JSON adapter, replacing
`name` with `kind` and `plugin` with `capcov_contrib.rea:discover_flows`.
The flow importer records the consumed document in the inventory's `sources`.

## Mappings

* `analyze_javascript_application`: JavaScript/source modules, context bridge APIs,
  IPC channels/handlers, endpoints, native exports and managed methods from the
  application graph become `rea:<node_id>` candidates. Exact node identity and
  observation evidence retain their source locations, confidence and coverage.
  Relationships and other nodes remain in the hashed input; a named boundary
  states that this plugin does not resolve them into a behavioral call graph.
* `inspect_analysis_view` with `normalized_result.kind = "api-surface"`: HAR
  method/origin/path-template groups become `rea:api:<digest>` candidates. Origin
  participates in identity, so equal paths on different hosts cannot collide.
  Template inference and parent Evidence identity stay attached. Missing pages,
  omitted/excluded records and producer limitations create unresolved entries.
* Other operations, including standalone native and .NET analysis records, remain
  named unresolved findings. This version does not invent route or behavioral
  semantics from pseudocode, assembly or metadata.

These are candidate identities, not ordinary `http:METHOD path` bindings. Review
and reference the candidate IDs in consumer models and assertions. Runtime runners
must emit fresh evidence through capcov's existing execution contract. No CRUD
operations are inferred from capture verbs. Producer `observed` confidence means
REA observed a fact; it does not establish capcov behavioral acceptance.

The importer always emits an acceptance boundary; empty or unsupported input emits
an additional no-surfaces boundary. Reconciliation preserves these, and the normal
gate fails until the consumer accounts for them through its existing reviewed,
dated exemption format (`cell = "unresolved"`). Exemptions explain discovery limits;
they do not demonstrate permissions, downstream effects or replacement parity.

## Integrity and scope

The importer checks the envelope it uses, duplicate JSON keys/record and candidate
identities, required provenance, document scope, and an optional file SHA-256 pin.
It preserves REA semantic IDs as producer claims. It does **not** reproduce REA's
canonical semantic authentication or bundle-manifest/unknown-history validation.
Use REA's `rea evidence-import <file>` to authenticate saved Evidence before
importing; an input-file hash binds bytes, not the truth of the producer's claims.

Complete records are required; REA bundle summary projections are not accepted.
Failed tool replies and their partial observations are rejected. Original raw
results, source text and network bodies are not copied into capcov artifacts.
Keep the saved input as retained evidence: emitted pointers refer to that exact
hashed JSON document, with line 1 as a document anchor rather than a code location.

Contract fixtures are based on REA 6.5.0. Availability of REA's own analysis tools
still depends on its host prerequisites; macOS JavaScript and HAR workers need
`swiftc` for the owned-process helper.
