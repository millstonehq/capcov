---
name: capcov-vectors
description: Prove a rebuild with boundary vectors — record what an incumbent system does at its edge, replay the same inputs against a candidate from the same state, and roll the difference up. Use when measuring how completely a rewrite reproduces an existing system, or when building a consumer of capcov.vectors.
allowed-tools: Read, Write, Edit, Bash, Glob, Grep
---

# capcov.vectors: rebuild coverage by recorded boundary behaviour

A static inventory says what a system *declares*. A runtime probe says what an
exercise *touched*. Neither says whether a candidate rebuilt from the same
requirements *behaves the same*. Vectors do: record the incumbent's boundary
behaviour once, replay the same inputs against the candidate from the same
starting state, and compare. `capcov.vectors` is the
system-agnostic engine; a consumer supplies only its own operations, fixture and
inputs.

## The design, fixed by the engine (do not work around these)

- **The unit is a boundary operation** — the smallest thing a caller can invoke:
  an HTTP route, a webhook, a scheduled event, a queued job, a command. The
  consumer supplies operations through the plugin seam (`schema.Operation`); the
  engine never special-cases a product.
- **The requirement is recorded vectors.** An operation is covered when every
  recorded vector passes against the candidate and at least one exists. A vector
  is one restored snapshot, one or more steps, the responses, and the delta of
  every store on the edge.
- **The stores are on the edge.** Databases, document stores, caches, object
  stores are snapshotted before and inspected after each vector, so a write to
  the wrong table or a read that shouldn't touch one is a difference, not an
  invisible effect. Declared stores are compared; every other touch is carried
  as *informational* — reportable, never dropped.
- **The templates are fixed** (`templates`). The cells per access class are the
  design's, not the operation's (read: permitted / no-permission / other-tenant
  / missing-record / anonymous; write adds invalid-input / retry; scheduled,
  queued, webhook, command). A case not in a template is not required; add it to
  the template, never to one operation.
- **Normalization is at compare time** (`normalize`, versioned). Recorded
  vectors stay raw; the policy masks volatile values (timestamps, tokens,
  generated ids) on both sides when compared, so a policy change never forces a
  re-record. Never widen it for one consumer — bump the version in the engine.
- **Gaps are never faked.** A cell that cannot be driven, a store that cannot be
  inspected, a fault that cannot be injected: each is a named gap carried to the
  rollup. A count that silently excludes what could not run is the lie this
  package refuses.

## The pieces

- `schema` — `Operation`, `Request`, `StepSpec`, the plugin seam.
- `templates` — the fixed cells per access class; `build_vectors(op, manifest, inputs)`.
- `fixture` — `ComposedFixture` over a dict of stores; snapshot/restore/inspect/
  drain, and the *observing* fast path (`mark`, `changes_since`) so a vector
  costs what it changed and a clean read skips the restore.
- `stores/` — `mysql` (full inspect, or observing via the row binlog),
  `mongo`/`mongowire` (observing count-and-tail over a stdlib OP_MSG client),
  `redis`, `s3`; `runner` for docker-exec vs local clients.
- `drivers/` — `http`, `form_post`, `shell`, `webhook`.
- `recorder` — `Session` (the actor-loss guard: halt when an actor accepted
  earlier is refused; the consumer names its credential-loss phrases so a "may
  not" 401 is not mistaken for "who are you"), `run_vector`, `record`, `replay`.
- `normalize` / `diff` — compare-time policy, `first_difference` (a JSON path
  and the two values), `store_delta`.
- `capcov_contrib/laravel_validators` (+ `laravel/reflect_requests.php`) — an
  optional input miner for a Laravel incumbent; a template for other stacks.

## Building a consumer

The consumer owns, and the engine never sees the shape of:

1. **Operations** — enumerate the incumbent's boundary as `schema.Operation`s
   with a driver each (route table, sitemap, console registry, …).
2. **A fixture** — compose the incumbent's stores; a golden snapshot to restore
   between vectors. Prefer observing stores (binlog / count-and-tail / DBSIZE)
   so record and replay are fast.
3. **A manifest + inputs** — the actors (permitted / no-permission / other-tenant
   per operation, resolved from the fixture), the ids for `missing-record`, and
   valid/invalid bodies (mine them from the system, don't hand-write).
4. **A rollup** into the consumer's own feature model.

Then: `record` against the incumbent, `replay` against the candidate, roll up
`vectors_passing / vectors_recorded` and `operations passing / operations in
scope` (the *true denominator*: every in-scope row in exactly one standing —
passing / partial / failing / unrecordable / not-recorded / observed-through /
placeholder).

## Invariants a consumer must keep

- One driver per operation; a driverless boundary row is a named gap, not a skip.
- Credentials resolved at send time, never at plan time (a restore re-mints them).
- The channel a credential rides is part of the request when the system reads it
  from different places by route (bearer / cookie / request-parameter / signed
  internal key) — a live credential on the wrong channel is refused like a dead
  one.
- A recording never carries a credential.
- An incumbent 5xx cell is a gap (the fixture or the synthesized input broke the
  incumbent), not a specification for the candidate.

A reference consumer records a ~1,800-operation PHP incumbent (Laravel HTTP, a
legacy dispatcher, console jobs) and replays it against a Go rewrite, driving
the loop from `tools/vectors/` with a `burn-down` goal document and the vectors
dimension of a product-breadth report.
