---
name: verification-triage
description: Use when clean-room reimplementing a system with a coverage tool (capcov) and deciding how to map its capabilities and prove the rebuild actually works — under time pressure, when a green gate tempts you to declare "done", when a state-mutating or auth path needs proving, or when you are about to wait on CI instead of iterating locally. Symptoms — "is the gate green enough to ship", "did I verify it or only map it", "how do I keep iteration fast", clean-room rebuild, differential/mutation testing, false green, capcov.toml, deep entity extraction, four-cell diff.
---

# Verification Triage

Bare-minimum, brutally pragmatic methodology to map a system's capabilities and prove a clean-room rebuild works. Grounded in capcov's real machinery, not a parallel theory. It is deliberately NOT formal verification.

## Quasi-formality and breadth-first work selection

Quasi-formality means fast, executable, falsifiable claims that catch meaningful
classes of bugs, with explicit limits. Optimize confidence gained per unit of work
across the system; do not pursue exhaustive proof of one pilot while other
capabilities remain untouched. "80/20" is a prioritization principle, not a measured
coverage percentage. Runtime usage complements deliberate checks; it does not
replace required authorization, data-integrity or release acceptance evidence.

For multi-capability rebuilds, use **one parent orchestrator plus implementation
workers**, with each worker owning its own verification. The parent owns global
coverage, task selection, scope extensions and integration; no permanent reviewer
agent is required. Read [the orchestration workflow](references/orchestration.md)
before assigning or resuming this mode. It provides assignment/checkpoint formats,
parallel ownership, explicit worker-model selection and continuation decisions.
Default to the provider's workhorse model for workers (Sol / Sonnet when available),
with the selected capable parent orchestrating; resolve supported IDs in the host. For a single bounded capability,
work directly without manufacturing orchestration overhead.

## Development target and stopping rule

Drive usable end-to-end operations across the full product. “Fully qualified”
across an entire capability family is not the default development target, a
prerequisite for advancing, or a useful headline progress metric. A family can
contain demonstrated operations alongside missing behavior and untested variants.
Credit the demonstrated operations with their evidence and limits; preserve the
remaining requirements without treating all partial families as zero progress.
Do not invent percentages from these mixed states or quietly shrink product scope.

For each bounded assignment:
1. Identify a source-grounded user operation and its observable result.
2. Make the shortest usable path work in the actual running application, including
   persisted or downstream effects. A local instance is sufficient for local claims;
   service-only tests cannot establish that a browser journey works.
3. Run focused checks for meaningful failure modes, especially authorization,
   wrong or lost writes, duplicate effects and delivery. Choose reference comparisons
   and targeted fault checks for the actual risk, using maintained fixtures.
4. Stop when this scoped operation has sufficient evidence. Integrate it and compare
   the next missing required operation with further depth. Default to missing
   functionality over more variants, repeated verification or polish.

Known incorrect behavior is still a defect. Missing required functionality,
unresolved business semantics and additional verification are different remaining
work; report them separately. Preserve explicit user acceptance requirements, but
do not manufacture a requirement for exhaustive mutations, all supplier variants,
hosted execution or customer sign-off at every local checkpoint. Strict audit or
release qualification applies when requested or concretely required by the claim.
A session commit policy does not authorize deepening a family solely to unlock a
commit; preserve local work and keep the development priority intact.

## Demonstrated behavior, not a green mapping gate

A demonstrated operation has executed against a real running instance and produced
the expected observable result, with checks that can distinguish relevant wrong
behavior. State exactly which operation and environment were exercised. The capcov
gate establishes mapping and reconciliation within its scope, not that the whole
replacement works. Keep unmapped and untested behavior visible, and do not claim
product completion from a passing subset.

## What capcov is now — the unified engine (LOCKED)

One `capcov.toml` per target drives everything: the discovery adapter(s) and the runtime probe. The pipeline is `discover` (static) + `observe` (runtime) → `reconcile` (the **four-cell diff**: `both` / `static_only` / `runtime_only` / `neither`) → `gate`; three of the four cells fail until a human names a reason. Discovery is generic — tree-sitter reads configured surfaces in supported languages, structured-spec reads contracts — and **deep** extraction (`discover --resolver scip`) traces a handler through the SCIP-resolved call graph down to the data **entities** it touches, verified for Go, PHP, and Python. The old `flows` surface is folded in: a browser flow is now the **browser probe**, which runs the EFSM plan internally and emits four-cell observed evidence; `outcomes` and `features` are peer surfaces.

Default to the existing engine. An explicit user request to extend it authorizes that scope; otherwise reopen only for a concrete gap blocking qualification of a required capability. To observe a spine effect on a non-Python backend, assert it **out-of-band** (do the write, then read the row back from the real DB or a read endpoint — a later request is a legitimate observation); a per-language runtime probe is deferred until out-of-band genuinely cannot establish an effect.

## Establish implementation breadth before choosing a pilot

For a multi-capability replacement, produce or refresh a coarse **feature model
linked to native outcome obligations and source discovery**. Read
[capability and evidence accounting](references/capability-evidence.md) before the
first pass or a denominator change. Features identify product capabilities;
outcomes identify their acceptance requirements. Do not introduce an Operation
or WorkUnit object to count deliverables. CRUD operations and individual flow
transitions are not automatically business capabilities.

Reuse the maintained consumer, native IDs and engine evidence-derived reports.
Keep unmapped source findings and unknown areas across the product visible.
A pilot-only gate or family sketch is insufficient for breadth-first scheduling.
If this view is missing, establish it before another implementation wave.

Bound the coarse first pass to one normal planning iteration. Retain a provisional
feature decomposition and unresolved acceptance requirements, then implement the
next independent capabilities. Refine contracts progressively rather than blocking
on perfect discovery. Report implementation declarations and demonstrated outcomes
separately, with context and scope. Forecast from comparable newly demonstrated
capabilities and measured runtime; keep integration/release work explicit. Do not
count both parents and their descendants, or treat regrouping as new throughput.

## Map the capability — the flow model, four fields per step

**Discovery is automatic; the model is authored, and authoring is progressive — never a prerequisite for starting.** `discover` derives the surface/entity obligations from the code on its own. The flow model (a `<capability>.model.json` file kept next to the consumer's `capcov.toml`) is the *behavior* layer you author on top to make outcomes checkable: you begin from auto-discovery and fill the model in as you go. A human clarifies ambiguous semantics; a human does not hand-author the map before discovery can run.

A transition is **ready to prove** (not "allowed to exist") when all four are filled:

- `outcome` — the caller-distinguishable result, plain words. Include the relevant actor, scope, prerequisites and later effects; identical wording does not establish equivalent behavior.
- `obligations` — the surface(s) it binds (`http:/ask`).
- `evidence` — `file:line` into the **original**. The clean-room anchor: rebuild from here, do not guess.
- `bindings` — the concrete check.

Within an assigned capability, scenario order falls out of the fact machine: `plan` walks `requires`/`adds` from `initial` and generates the scenarios. Take the shortest path to the headline outcome first, then branch **refusal and security paths ahead of happy-path variants** (a wrong refusal is costlier than a missing convenience).

Deep behavioral modeling proceeds one capability at a time after the coarse whole-product inventory. But **filtering a route out of THIS capability's run does not make it "outside the product."** A scoped-out surface must be **accounted globally** — assigned to some other capability's gate, or listed as explicitly unresolved in a global denominator. "Correctly absent from this gate" is a per-run fact, never a product-completeness claim; the excluded set is a ledger to reconcile, not a set to forget.

## The check must DETECT WRONG BEHAVIOR, not reproduce right behavior

For a **state-mutating or auth** operation, a happy response alone is insufficient.
Check the relevant persisted effect and ownership/refusal behavior. Choose a small
set of high-value experiments that can expose the likely costly failures:

- **idempotency** — retry a write or delivery that could duplicate an effect;
- **ordering** — exercise a meaningful stale or reordered operation;
- **scoping** — attempt the operation on another principal's subject;
- **targeted fault injection** — remove a relevant guard or corrupt an effect and
  confirm the specific check detects it.

Do not require every fault type or a mutant for every transition merely to finish
a checklist. Select checks by concrete risk and reuse existing evidence when its
inputs and scope still apply. If a critical claim lacks a discriminating check,
add the cheapest sufficient check before crediting that claim.

Green means "catches the wrong behaviors", not "matched one path". Capcov can validate a
`mutations` list and carry it into a transition plan, but that is not fault execution. Before
relying on mutation evidence, inspect the installed capcov version and locate the maintained
application runner that applies each fault, runs the binding, and restores the source. If no such
executor is available, record an explicit measurement dependency rather than treating declaration
or plan preservation as evidence. Keep the engine locked unless this missing executor concretely
blocks a required qualification.

**Mutations and differential testing answer different questions.** A mutation tests whether a check catches a chosen fault; a reference differential tests whether the rebuild agrees with the incumbent for the selected cases. Use both where their distinct evidence changes a correctness decision, especially for consequential writes or access boundaries. Neither establishes every variant. An unavailable oracle remains an explicit evidence limit; it does not automatically prevent demonstrating a source-grounded local operation or require building new tooling.

**A mutation's evidence is a SPECIFIC failing assertion, not a red aggregate gate.** The gate may already be red for unrelated reasons (incomplete coverage — `prove.py`'s gate stays red even on a good run). "Still red when I inject the fault" proves nothing. Assert that *this transition's binding* reddens *for this fault*, and is green without it. Aggregate red is not caught-the-fault.

For composed writes, bind the original effect time and exact child inputs before
any effect. A retry must not recompute a business date or revision and conflict
with an already completed child. Select a lost-acknowledgment check at the final
new write boundary; a failure before that write does not exercise its recovery.
Report GETs must inspect retained child evidence, never invoke the write operation
to reconstruct missing evidence.

**"Observable" includes later and persisted effects, not just the immediate response.** Two calls that both return `500` can leave different persisted state, different permissions, or different retry behavior. On a state-mutating or auth transition, the check must assert the *downstream* effect a later request reveals — matching the immediate response is insufficient exactly where the risk is highest.

## Read the ratio beside its denominator's edge

`covered/N` is meaningless alone. Always render it next to: what `discover` **scoped out** (routes the query filtered, languages with no adapter), and which transitions are **unreachable-from-initial** and **where they are proven instead**. The model's `note.moved_out` block and the baseline reasons ARE that ledger — hand-authored, the tool cannot generate it, and it is the **stopping rule**: the map is not done until every excluded surface and every unreachable transition has a one-line "proven elsewhere / not built / out of tier" reason. This is the soundiness discipline (name what you did not resolve); cutting it deletes the science and keeps a blind counter.

## Keep the loop ripping — never block on the slow signal

The inner loop is local and sub-10s. A signal you block a turn on had better be the cheapest one that can catch what you just changed; a 10-minute CI wait for a one-line edit is a loop bug, not caution.

| tier | signal | latency | run |
|---|---|---|---|
| 0 | compile the one binary | ~1s | every edit |
| 1 | static gate (`scripts/capcov.sh`) | seconds | every edit — proves wiring; says `unproven`, not `covered`, and is honest for it |
| 2 | targeted behavioral check and selected fault checks | seconds | relevant behavior or guard changes |
| 3 | full behavioral run (`capcov observe` + `reconcile`, or the browser probe) | minutes | scoped operation boundary |
| 4 | CI / e2e against a real instance | minutes+ | when publication or an environment-dependent claim requires it |

Rules, dependency-aware (not absolute): **run the cheapest relevant local checks first; block on a slow or live signal only when the next decision actually depends on it.** Don't `git push` to answer a question a local check already answers. But some facts only the live environment teaches — IAM, certificates, managed-DB and Fargate behavior — and waiting on those is informing, not stalling, when your next step depends on them. Scope regression by **blast radius, not line count**: a one-line change to a shared composition can justify broad checks because it moves a fleet. When a signal is slower than your next edit and nothing downstream needs it yet, fire it async and keep working. The fast proxy must be a **strict subset** of the authoritative check (a proxy failure is always a real failure); drift between proxy and authoritative is itself a bug to fix, not tolerate.

## The single worst failure

Run `capcov.sh`, see the gate exit 0 with its baselined lines, declare the rebuild verified — having never run the behavioral step, never seeded a wrong behavior, over a denominator of one route in one tier while dozens of routes and other languages sit outside it by construction. Every honest marker (`unproven`, `moved_out`, "discovery unresolved", the deliberately-red gate) was present and read as a formality. Green meant "the three things I chose to look at are shaped the way I said"; it was reported as "works".

For database migration witnesses, seed the historical schema with explicit legacy
columns, not today's ORM object: ORM inserts can include newly added nullable JSON
columns and fail before the migration runs. Upgrade to the current schema before
invoking current application behavior; test a historical downgrade guard at its
own revision. Preserve the old-row and retained-effect assertions. A fixture/schema
mismatch is a measurement defect, not permission to weaken the runtime or skip
the migration witness.

## Know when to stop extending the tool

Extending capcov is a means, not the work. **Stop the moment an engine change no longer *blocks* evaluating a required product outcome, and go back to implementing and demonstrating required product operations.** A tool extension is justified only while its absence prevents you from trusting a real outcome (e.g. no way to prove a write's effect). The failure mode this session's whole thread warns about is the reverse: accumulating infrastructure while most of the product stays unassessed. The loop is: discover across the surface → classify each gap (measurement / implementation / acceptance) → pick a high-value user outcome → run targeted checks and the relevant faults → demonstrate that operation → advance the next missing behavior. Reach a usable end-to-end flow early; do not let engine work outrun usable product behavior.

## Preserve product scope and consequential correctness

A demonstrated operation is progress within the product, not a declaration that
its entire family or the release is complete. Keep remaining required capabilities
assigned or unresolved. Shipping a limited scope follows the user's delivery and
acceptance policy; discovery filters never authorize dropping product requirements.

Authentication, writes and delivery warrant focused checks of ownership and
persisted effects because a happy response can conceal a serious error. Use deeper
verification when a concrete risk, observed failure or explicit acceptance condition
requires it. Do not turn these categories into an automatic exhaustive verification
program. Once the relevant correctness decision has sufficient evidence, advance
other missing functionality and retain the limits of the claim.

## Use capcov's own words

Core verbs: `discover` / `observe` / `reconcile` / `gate` / `report`; the reconcile cells are `both` / `static_only` / `runtime_only` / `neither`. A browser-flow capability also uses the flows states `covered` / `unproven` / `unmapped`. Also `unknown-obligation` / `OBSOLETE`.

**Classify every gap before prescribing work — three kinds, three different actions:**

- **measurement** — discovery failed to extract, identify, or map it (a duplicate-route collision, an unresolved language, a scoping mistake). Fix the tool or the model; do NOT build anything.
- **implementation** — genuinely not built. A build task.
- **acceptance** — built, but not yet qualified against reference behavior. A test/qualification task.

`unknown-obligation` is usually measurement OR implementation, and prescribing "go build it" on a measurement gap builds a duplicate or chases a phantom — your duplicate-route blocker is exactly this. `unproven` is an acceptance gap. Split the cause first; only a confirmed *implementation* gap is a build task. Then fix confirmed `unknown-obligation` before `unproven`; `unmapped` means your model missed a scoped surface.

Do not re-teach the gate/baseline discipline here. Keep one description of it, next to the consumer's baseline file, and point at that.

The locked engine includes mutation declaration and plan preservation, `discover` provenance,
deep SCIP handler→entity extraction across Go/PHP/Python, and the honest denominator threaded
end-to-end; see `TOOLING-EXTENSIONS.md` for the measured boundary. Mutation application and
restoration require a separately maintained runner. If you hit a real wall, name it against a real
qualification rather than assuming metadata is executable or extending the engine speculatively.

## Rollback across SQL-backed notification queues

For a replacement sharing notification stores with its incumbent, an empty Redis
queue is not proof that processing is drained: due recipients and ready emails in
SQL can repopulate it when scanning resumes. Check both durable eligibility and
ready/delayed/reserved queue work before transferring ownership. Inspect the
deployed send job too: explicit-ID or serialized-payload jobs may bypass current
SQL status and resend already-settled messages. In the target system this was verified
against the deployed PHP sender; the ordinary ready-status query alone missed it.
Preserve restored legacy backlog and isolate qualification work rather than
starting an unscoped sender or resetting statuses to make a rehearsal pass.

## Restoring a missing pinned oracle runtime

A Nix output path disappearing is a fixture prerequisite failure, not a product
failure. Rebuild from the declared derivation and keep an output link as a GC root
for the qualification. Compare extension bytes as well as loaded PHP/extension
versions and a BSON round-trip. A rebuilt output can retain its path and version
while changing bytes: record a fresh fingerprint and rerun the required contract;
do not silently reuse earlier receipts or disable hash verification. the target system
cloud-save qualification encountered this with its MongoDB PHP extension.

## Keep fixture premises independent of the candidate

Build SQL fixtures from pinned incumbent DDL or executed migrations, and retain
the source identity. Do not infer column names or types from candidate queries: a
test schema that repeats the implementation can pass while the real database
rejects it. Check required columns against the migrated fixture before running
behavior. Likewise, a missing-object fixture cannot establish present-object
serialization; retain that conditional dependency instead of hardcoding its
observed output as the general contract. A discovered shared assumption invalidates
the affected earlier evidence until the independent comparison is rerun.

Bind the reference to the deployed incumbent revision when qualifying a deployed
replacement, and verify the relevant source hashes rather than assuming the local
checkout matches. Compare a nonempty wire response without projecting away fields;
an empty child collection or a normalizer that discards fields cannot prove the
populated contract. Apply that revision's required migrations to the disposable
fixture. Any excluded runtime-only field must be observed, source-anchored and
reported separately from full response equality.
