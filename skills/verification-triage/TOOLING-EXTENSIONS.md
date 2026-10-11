# capcov tooling extensions the verification-triage method demands

The method (see `SKILL.md`) is grounded in the literature (model-based testing, coverage
obligations, reflexion model-vs-reality, soundiness). Where capcov as built cannot express
what the science requires, the fix is to **extend capcov**, not to weaken the method to fit
the current tool. These came out of a 2026-09-13 expert review (Linus / Fournier / Majors /
Karpathy / red-team-Taleb) grounded in a real consumer's tree.

Ranked by how much a missing one lets a false green through.

These are proposed extensions, not a statement that every behavior below ships.
At capcov base `d3ac0ba`, mutation declarations and unmutated-transition reporting
exist; declarations do not execute faults. Before extending a consumer, inspect its
actual bindings. For the proposed external advancement boundary and source review,
see [DETERMINISTIC-GATING.md](DETERMINISTIC-GATING.md). Prefer extending an existing
runner only where a demonstrated false-success path requires it.

## 1. A `mutations` field + a generalized fault-matrix runner (highest value)

**Gap.** A binding with one happy-path assert cannot distinguish two systems that agree on
the trajectory and diverge on a mutation (idempotency, ordering, scoping). This is the
governing correctness risk for every state-mutating or auth transition.

**The pattern already exists** in one consumer, whose `prove.py` runs an 8-fault
matrix: it injects a wrong behavior and asserts the gate goes RED. It is per-model and
hand-built for the conversation model; the credential model has none.

**Extension.** Each transition may declare `mutations: [{kind: idempotency|ordering|scoping,
...}]`. The runner applies each mutation to the rebuild and asserts the transition's binding
reddens. Proposed acceptance requires its baseline assertion to pass and each required
seeded implementation fault to fail that assertion. Repeating, reordering, or changing
the subject of an input is an adversarial scenario; it is not by itself an implementation
mutation. Fault controls and differential execution establish different facts and are
complementary. Declaration counts alone satisfy neither requirement.

## 2. First-class denominator provenance from `discover`

**Gap.** The discovery query silently filters (credential config matches 3 routes; ~65 on
`web.go` are excluded; some models set `adapters: []` with "discovery unresolved"). A
`covered/N` ratio over a silently-narrowed `N` reads as "done" when it means "done for the
sliver I chose to look at".

**Extension.** `discover` emits, as first-class fields, the **excluded-surface count** (what
the scope query could see but filtered) and the **unresolved-language list** (extensions
missing). `report`/`gate` render `covered` next to `excluded` and `unresolved`, never a bare
percentage.

## 3. `plan` emits the unreachable-from-initial transition set

**Gap.** Transitions excluded from the plan (unreachable from `initial`, or moved out) can
never surface as a gap — they are invisible to the gate. Today the reasoning about them lives
in hand-authored `note.moved_out` prose that the tool cannot generate or check.

**Extension.** `plan` outputs the set of declared transitions that its BFS did not reach from
`initial`. The human's job becomes annotating **where each is proven instead** (a Go suite, an
acceptance row), turning the soundiness ledger from remembered prose into a checked list.

## 4. A `--only <transition>` selector for the sub-10s inner loop

**Gap.** `reconcile()` always folds the whole plan, so the tightest scope the tool can express
is the whole capability — minutes at scale, when the loop wants one transition in seconds.

**Extension.** `--only <transition|scenario>` on `run`/`coverage` scopes to a single unit so
the inner loop is: edit → build the one thing → check the one transition (+ its mutations) →
red/green in seconds. Whole-capability gate stays the outer (CI) loop.

## 5. Wire the behavioral step into the driver, or make its omission loud

**Gap.** That consumer's driver script, `capcov.sh`, omits `capcov flows run` — the only step that produces
behavioral evidence — so the operator's loop reads `unproven` for every surface and baselines
it as accepted. Running it daily trains "unproven = fine".

**Extension.** Either wire `run` into the driver behind the same freshness guards, or have the
driver print a loud, unbaselineable banner that behavioral assurance was NOT collected and the
result is structural-only. A done-check MUST run `run` against a real instance.

## 6. (If a live run is added) stamp the original's identity into `assurance`

**Gap.** `assurance` is a provenance string that currently says *injected boundaries +
fixtures*. Reading it as a liveness flag manufactures a false green.

**Extension.** When a run executes against a real original, record the original's commit/build
id into the `assurance` value, so "live" is a fact the artifact carries, not an adjective the
reader supplies. Print `assurance` verbatim; "injected fixtures" is not-done for a parity claim.

---

## 7. Global denominator accounting

**Gap.** `excluded_surfaces` (extension 2) is per-capability. Filtering a route out of one
capability's run does not establish that it is outside the product. Left per-run, an excluded
surface is silently no one's responsibility.

**Extension.** Roll the per-capability excluded/unresolved sets into a **global denominator**:
every surface `discover` can see across all capability configs must be **assigned to some
capability's gate** or listed **explicitly unresolved**. A route that is excluded everywhere is
a coverage hole, not "correctly absent". `report` gains a global view: assigned / unassigned /
unresolved across the whole product.

## 8. Distinguish undiscovered from unimplemented

**Gap.** `unknown-obligation` is reported as one class, but it conflates "the feature is not
built" with "discovery failed to extract/identify/map it" (a duplicate-route collision, an
unresolved language). Acting on it as "go build" produces a duplicate or chases a phantom.

**Extension.** `unknown-obligation` carries a cause hint where the tool can infer one
(duplicate symbol seen; language unresolved; scope filter hit) so triage can split
mapping-failure from genuinely-not-built before any product work is prescribed.

---

**On mutations vs differential — complementary, not either/or (corrected).** An earlier draft
here said seeded mutations *subsume* a live differential. They do not, and that was wrong.
A mutation proves your *check* can catch a fault you seeded; a differential (run the rebuild
against the reference, assert agreement) catches divergence you did *not* seed. Each exposes
what the other misses. Build **both**: seeded mutations (extension 1) to prove the checks have
power on the spine, and a differential against the running original (multi-input, and asserting
persisted/downstream state, not just the immediate response) to catch unseeded drift. Neither
replaces the other; hand-transcribed literal asserts replace *neither* and should go.

---

## Design note — the generic discovery architecture (informs #38)

The generic design needs more than tree-sitter: **two generic mechanisms, one per
input class, and the remainder in config**: (a) **tree-sitter** for source code (any language via a
query), replacing every language-specific code reader; (b) a **generic structured-spec reader**
for declarative contract/data files (via a path query), of which **OpenAPI is one config, not a
bespoke adapter** — the honest home for the non-code modality. SQLite-constant discovery, being
code-side, folds into a tree-sitter query. Result: exactly two discovery mechanisms, both
generic, no per-language and no per-format special cases.
