# Deterministic gating: rationale and first experiment

Research and integration contract, 2026-09-14. Capcov base: `d3ac0ba`.
This branch extends existing capcov checks; it adds no scheduler, Shen integration,
or deployment controller. Read it when wiring the skill to the host's advancement boundary.

## Implemented boundary in this branch

- `outcomes check` executes the existing exact-node pytest integration and gates the
  entire map in one command. It does not consume a caller-supplied old run. The
  existing source/map/input/engine checks and nonce guard remain in use.
- Browser scenario failures now survive the existing core reconciliation and gate,
  independently of the entity coverage cells. Another passing scenario cannot hide
  a failed or missing case on the same route. Structural exemptions do not waive it.
- Browser execution rejects duplicate scenario identities and nonzero process exits.
  Diagnostic/partial runs and incomplete plans cannot qualify full-plan acceptance.

The host must still enforce the exit code and own the accepted checker/contract
revision. These changes do not prevent hostile runner code from fabricating its
own observation, or prove deployment, rollback, and business-contract adequacy.

Capcov itself now supplies a bounded local host check. From the capcov source tree,
run `uv run --frozen --with pytest sh scripts/check-backpressure.sh`. Its authored
`capcov.outcomes.json` binds capcov's own acceptance behavior. The script uses the
actual CLI in disposable copies and creates an advancement marker only on exit zero.
The baseline must advance; an omitted required test and a source mutant suppressing
browser-flow failures must block for their specifically named outcomes. Cleanup is
checked. These three cases passed locally; the same command is wired into package
CI, whose remote execution has not been observed here. This tests local host
enforcement, not an autonomous agent's behavior or an external product migration.

## The useful distinction

There are three independent questions:

1. **Decision determinism:** do the same contract, checker, and evidence produce the
   same verdict? A judge's sampled output can be an input, but that does not make the
   judge or application deterministic.
2. **Enforcement:** can the candidate advance without satisfying that verdict? A
   prompt and an editable `status.json` cannot establish this boundary.
3. **Adequacy:** does the chosen contract detect the failures that matter? A perfectly
   enforced, deterministic checker can enforce a wrong or incomplete requirement.

Quasi-formality benefits from all three without claiming universal verification.
Failure diagnostics improve the next attempt; an acceptance decision controls which
attempt is eligible to advance. Preserve that distinction even when the same command
provides both outputs.

The smallest useful architecture is the existing product runner producing evidence,
the existing capcov binding accounting for obligations, and an existing host/CI
boundary enforcing the selected contract. These responsibilities need not become
three new services. The implementation agent cannot be the sole authority on whether
its own change has satisfied acceptance.

## What the sources contribute

### Huntley and Moss: spend feedback on the right decisions

Huntley's [Ralph article](https://ghuntley.com/ralph/) emphasizes focused iterations,
fast compiler/test feedback, and preserving why a test exists. His
[loop article](https://ghuntley.com/loop/) emphasizes observing recurring failure
modes and improving the loop. Neither establishes that repetition guarantees eventual
correctness. For capcov, retain a compact failed assertion and its reason across
iterations, and define an exit budget rather than relying on eventual convergence.

Huntley's [backpressure post](https://ghuntley.com/pressure/) points to
[Moss's original article](https://banay.me/dont-waste-your-backpressure/): automated
feedback frees the engineer from repeatedly correcting mechanical mistakes. The
accessible Huntley page ends at a subscriber boundary; the linked Moss article was
read directly. Our extension is to reserve judgment for contract selection and
unexpected behavior while machines handle reproducible checks. Feedback quality and
latency matter together; an enormous slow suite can be poor inner-loop feedback even
when it remains necessary for release.

### Shen-Backpressure: explicit invariants, a real runner, qualified guarantees

Inspected [Shen-Backpressure](https://github.com/pyrex41/Shen-Backpressure) at
`6b9dde09b3a98ee5d20d0a6556acceacad4657d8`. Its manifest-driven Go runner executes
gate commands and returns failures; its context output provides feedback to agents.
This is a concrete separation of evaluation from prompting worth reusing.

The [trust model](https://github.com/pyrex41/Shen-Backpressure/blob/6b9dde09b3a98ee5d20d0a6556acceacad4657d8/docs/TRUST-MODEL.md)
distinguishes structural, runtime, sampled, and assumed premises. Do not import the
stronger claim that exported Go guard types can only be constructed through their
constructors. A concrete counterexample using the repository's generated code compiled
and ran: `NewJwtIssuer("")` rejected the input, but a zero-valued `JwtIssuer` crossed
a typed function boundary and returned the empty value. No `unsafe` was involved.
This challenges the construction guarantee; it does not demonstrate an exploit of
the complete HTTP service.

For capcov, typed stages could require evidence of tenant binding or source identity
before producing an acceptance value. Prove rejection at the consuming boundary,
including zero/nil values, supported deserialization, and expired context. Constructor
checks alone do not establish lifetime validity or truth of external observations.
Shen remains optional: use it where a named invariant makes bypass mechanically
harder, and test the generator/runtime assumptions explicitly.

### StrongDM and Willison: acceptance independence and behavioral breadth

[StrongDM's account](https://factory.strongdm.ai/) describes externally held scenarios,
empirical satisfaction judgments, and behavioral twins of dependencies. The useful
transfer is acceptance that cannot simply be rewritten alongside implementation,
plus cheap execution of otherwise expensive scenarios. A twin's agreement must be
calibrated against the dependency for the claimed scope. It does not demonstrate
production credentials, deployment, or all provider behavior.

[Simon Willison's report](https://simonwillison.net/2026/feb/7/software-factory/)
highlights the problem of agents authoring both code and its tests. It reports the
team's approach rather than supplying an independent correctness certification.
For capcov, hold out acceptance authority; hiding every test is unnecessary. Public
regressions can teach repairs. Repeated adaptive feedback can overfit a holdout, so
retain independent qualification when making stronger claims. A satisfaction average
must not cancel a failed ownership or data-integrity invariant.

### Attractor: explicit control flow is useful, acceptance needs a stricter profile

Inspected [Attractor's specification](https://github.com/strongdm/attractor/blob/fb57a55ed97372a27ac90102f436947e29f48426/attractor-spec.md)
at `fb57a55ed97372a27ac90102f436947e29f48426`. This repository specifies a pipeline;
it is not the production runner implementation.

Its graph, conditional routing, checkpoints, tool stages, and retry policy make
workflow decisions inspectable. However, section 3.4 checks visited goal nodes and
accepts `PARTIAL_SUCCESS`; `auto_status` can synthesize success when enabled. Section
3.2 also has a no-next-edge success path. These semantics do not by themselves enforce
every required acceptance obligation. This is a specification finding, not a tested
exploit in a particular implementation.

An acceptance integration needs a final required-set check on every exit, fresh
results after repairs, explicit process/assertion failure handling, no automatic or
partial acceptance, and a total budget across back-edges. Let useful exploratory
stages have partial outcomes without allowing them to satisfy hard requirements.
Adopt these semantics in an existing host before considering a full DOT pipeline.

## Proposed acceptance predicate

For a selected release scope, let `R` be the nonempty set of required check IDs in
the accepted contract and `E` the current run's eligible results. Accept only when:

- Every member of `R` has exactly one unambiguous terminal result for this attempt.
- Each result is passing under its declared assertion semantics, not merely present.
- Source/build, contract, checker, fixture scope, and attempt identities match the
  expected roles; original and replacement builds are intentionally different roles.
- Relevant contradictory observations, process failures, incomplete observations,
  and failed cleanup are absent or resolved by an explicit contract rule.

For a valid release scope with no applicable checks, report no qualification claim;
do not obtain acceptance by vacuous truth. Additional unknown behavior remains visible
in the global ledger. A local slice's acceptance does not imply product completeness.

This predicate is our proposed integration rule, not an existing capcov API or schema.
Use existing receipt fields where possible. Add a field only to distinguish a concrete
false-success case. A hash binds bytes to a trusted reference; it does not authenticate
the observer, prevent concurrent mutation, or stop an agent rewriting both sides.

Contract or checker changes may be legitimate, especially for measurement gaps.
Route them through existing ownership/review rules, retain the reproducing case, and
invalidate affected evidence. Do not demand fresh user permission for already
authorized routine repairs; do not let the candidate redefine acceptance unilaterally.

## First bounded experiment

The first code increment targets four reproduced browser false-success cases and
the execution-versus-acceptance ambiguity of `outcomes run`. The remaining host
integration experiment below must be performed in the consuming project.

Use one existing product execution and its checker. Keep product behavior in its
implementation language, capcov's existing Python accounting in capcov, and process
execution in the existing host. No new Python harness, alternate business algorithm,
general scheduler, or engine rewrite is required to investigate this contract.

First find the actual host advancement hook and show whether it can invoke a protected
checker and prevent acceptance on failure. If there is no such hook, label the result
advisory and identify that concrete integration gap before adding more evidence fields.

Freeze one accepted contract, run one baseline, and challenge the boundary:

| Experiment | Required result |
|---|---|
| All required assertions pass on the bound candidate | Selected scope may advance |
| Required check omitted or bypassed by a graph branch | Refuse: missing check |
| Agent writes success while a real assertion fails | Refuse: assertion failure |
| Prior result replayed after a source change | Refuse: stale evidence |
| Candidate weakens the contract or checker | Prior acceptance invalid; require authoritative revision |
| Exit zero with skipped assertions or malformed evidence | Refuse: incomplete/invalid evidence |
| Infrastructure unavailable or retry budget exhausted | Inconclusive/blocked, never accepted |
| Repair loop revisits an earlier passing stage after changing its inputs | Re-evaluate dependent checks |

These are host integration criteria, not claims of deployed enforcement. Package
regressions exercise the missing/failed browser scenarios and the real pytest CLI's
pass, skip, missing binding, filtered selection, changed inputs, and timeout paths.
Implement the smallest adapter to the actual host only after locating that boundary.
Return one failing check with
expected/actual observations to the repair loop. Verify owned-fixture cleanup on both
success and failure. The success measure is prevented false advancement plus a useful
repair cycle, not the number of receipts or pipeline nodes.

## Executed research check

The Go zero-value counterexample above was run with `go run .` in
`/tmp/capcov-guard-zero-repro` using an unchanged copy of
`examples/multi-tenant-api/internal/shenguard/guards_gen.go` from the pinned source.
Minimal external-package program (import path assumes that copy is in `guardrepro/shenguard`):

```go
package main

import (
    "fmt"
    "guardrepro/shenguard"
)

func boundary(x shenguard.JwtIssuer) string { return x.Val() }

func main() {
    _, err := shenguard.NewJwtIssuer("")
    var issuer shenguard.JwtIssuer
    if err == nil || boundary(issuer) != "" {
        panic("counterexample did not reproduce")
    }
    fmt.Printf("constructor rejected empty issuer: %v; zero value crossed typed boundary: %q\n",
        err != nil, boundary(issuer))
}
```

Observed output: `constructor rejected empty issuer: true; zero value crossed typed boundary: ""`.
No production target-system qualification or deployed host enforcement is claimed.
