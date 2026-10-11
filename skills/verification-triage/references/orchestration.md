# Breadth-first orchestration with workers

Use this workflow for multi-capability rebuilds. The aim is to turn an incumbent
system into a usable replacement, with executable evidence and an honest account
of what remains. Use native subagents when available and authorized; no separate
orchestrator service or permanent reviewer agent is required.

```text
User: objective, priorities, acceptance and scope decisions
  |
Parent orchestrator: global coverage, assignments, integration, progress decisions
  |-- Worker: capability A + its executable verification
  |-- Worker: capability B + its executable verification
  `-- Worker: shared prerequisite or capability C + its verification
```

## Responsibilities

The parent owns selection, continuation, integration and reporting. Keep its
working context centered on the full objective, capability status, dependencies,
active assignments, cumulative effort and evidence links. Inspect relevant diffs
and raw results before accepting claims; worker summaries are pointers, not proof.
Do not become another implementation worker while others run. Integration edits
and resolving cross-worker interfaces are appropriate; sustained implementation
belongs in a bounded worker assignment.

Each worker owns implementation and quasi-formal verification for its assignment:
reference behavior, runnable outcome, persisted and forbidden effects, differential
comparison, and the relevant specific seeded failure. The worker reports evidence
and recommends next work. It cannot authorize its own scope expansion, deploy to
a shared target without ownership, or spawn additional workers unless delegated.
There is no standing reviewer role: checking evidence is part of the parent's
acceptance decision, and producing falsifiable evidence is the worker's job.

## Select models explicitly by role

Use a capable model for orchestration and a cost-efficient implementation model
for ordinary bounded workers. Do not silently inherit the parent's most expensive
model for every worker. User-selected models take precedence.

| Host/provider model family | Default worker | Orchestration or justified escalation |
| --- | --- | --- |
| Codex/OpenAI with these models available | Sol (for example `gpt-5.6-sol`) | The selected capable parent model; a stronger available model for a demonstrated blocker |
| Claude-family models, including through Fable or Claude Code | Sonnet | Opus |
| Other hosts/providers | Available general-purpose coding/workhorse tier | Available stronger reasoning tier |

These are role preferences, not assumptions about a host's supported models or
fixed version requirements. Inspect the actual available model catalog and native
spawn parameters, resolve the preferred family to a supported identifier, and set
it explicitly. Do not invent model IDs or switch providers/accounts merely to
match this table. If selection is unavailable, disclose inherited/default behavior
and continue with the available model unless the user requires a specific model.

Give each worker a concise assignment, necessary source/evidence pointers, and
relevant constraints instead of automatically forking the full conversation. In
hosts where full-history forks prevent a model override, use a fresh or limited
context fork with an explicit model. Preserve the original goal, breadth-first
priority, ownership, acceptance requirements and known hazards in that handoff.

Choose reasoning effort appropriate to the assignment; do not automatically use
the maximum. Model savings never reduce the required verification evidence.
Escalate for a concrete reasoning/implementation blocker after inspecting the
failed approach, not for routine test failures, slow CI or an authentication wait.
The parent records the reason and bounds the stronger model's assignment. Keep
healthy workers running when this policy changes; use the new default on subsequent
assignments rather than restarting work to change models. Distinguish lower cost
per token from fewer tokens; compare actual cost, latency and accepted outcomes
when the host exposes those measurements.

## Select work across the whole system

Before selecting an implementation wave, establish or refresh the feature/outcome
view required by SKILL.md using [capability and evidence accounting](capability-evidence.md).
Reuse the existing consumer and native evidence-derived reports. Show capabilities,
acceptance gaps and unknown areas across the product; timebox the coarse first pass.

Link assignments to feature/outcome IDs, source references and observable acceptance
conditions. Multiple workers or steps serving one outcome do not create additional
completions. At integration, update scoped evidence and compare missing capabilities
across families. Keep counts and estimates in consistent units; retain uncertainty
and record splits/merges without claiming new throughput.

Track separately for each family: implementation state, local behavioral evidence,
deployed evidence, remaining requirements, dependencies, assigned owner and effort
spent. Use existing obligation IDs. A list of route stubs, interfaces, models or
adapter scaffolds is not a runnable capability. Do not infer system percentages
from route counts, test counts or the selected pilot's denominator.

Choose independent, high-value runnable outcomes across families. Prefer expanding
working coverage over polishing additional variants of a mature pilot. Breadth
first is not strict round-robin: a shared prerequisite, integration bottleneck or
required correctness fix can justify concentrated work. Name the capabilities it
unlocks and compare that benefit with the best available breadth assignment.
Three workers on three corners of one pilot do not constitute breadth.

Separate implementation scheduling from release qualification. A worker can return
useful implementation with explicit unproven cases and disabled exposure. That
assignment is partial, not a completed capability. Do not weaken write/auth/delivery
checks to ship sooner; equally, do not require exhaustive qualification of one
family before implementing unrelated families. Existing acceptance requirements
and explicit user priorities still govern release.

## Dispatch bounded assignments

Use available slots; do not assume a fixed agent limit. Give workers only the
relevant context plus the original objective and scheduling priority. Prefer
separate worktrees. If workers share a checkout, assign disjoint files explicitly
and prohibit resets, broad staging, or modifying another worker's inputs. Assign
shared interfaces and fixtures to one owner before parallel dependent work.

Use this compact assignment format in the native task message; a separate file is
not required:

```text
Outcome: one caller-visible behavior and its existing obligation IDs.
Why now: contribution to the full goal; dependencies unlocked.
Scope: owned files/worktree, interfaces, excluded work, authorized side effects.
Reference: incumbent source/contracts and reusable implementation/fixtures.
Proof: cheapest meaningful command; required differential/effect/fault assertions.
Model: explicit supported worker model and effort; reason for any escalation.
Budget: checkpoint time and cumulative effort already spent on this capability.
Return: diff/commit, commands and results, evidence paths/build scope, remaining
        gaps, live process handles, cleanup state, proposed next step.
Escalate: report new dependencies or scope changes; do not self-assign them.
```

Honor project timeboxes. Where none exists, begin with a roughly 15-minute
checkpoint and adjust explicitly to observed work. A checkpoint is a decision
boundary, not a reason to kill a live operation or leave a broken fixture behind.
Renaming an assignment, resuming a turn or changing workers does not reset the
capability's cumulative effort. Workers should report a scope-expanding discovery
as soon as it becomes apparent, not only at the deadline.

## Reassess instead of automatically continuing

On a result, scope expansion or checkpoint, the parent makes one explicit choice:
accept the scoped result and assign new work; extend for a named outcome; redirect;
or resolve a real dependency. Use the following questions:

1. What user-visible behavior or acceptance decision changed, supported by which
   artifact or observation? If none, what specific live process is still running?
2. How much effort has this family consumed relative to untouched families?
3. What is the best alternative breadth assignment right now?
4. Does the proposed continuation unlock more value than that alternative?

A new bug, passing tests, a commit, or "almost done" is not by itself a reason to
extend. Record the chosen continuation and reason briefly in the existing progress
artifact. After repeated extensions on the same family, explicitly reconsider the
assignment and dependency decomposition. Surface genuine scope/priority tradeoffs
to the user; do not invent automatic approval requests for ordinary coordination.
The parent must not merely copy the worker's proposed next step into the next task.

Keep user updates centered on breadth gained, evidence gained, effort concentration
and the next allocation. Report implementation, local qualification, deployment
and runtime acceptance separately. "Full scope remains open" does not substitute
for showing which families remain untouched.

## Integrate and publish

The parent owns integration order and assigns one owner for shared deployments,
migrations, global fixtures and publication. Parallel isolated local checks are
useful; competing writes to a shared preview are not. Reconcile source-bound
receipts after integration and rerun checks affected by changed inputs. Publish
coherent validated checkpoints using the user's existing branch/mainline policy;
this workflow does not require stacked PRs or introduce new approval gates.

Namespaced Crossplane claims can still own the same external service and fight
over its revision. Before retiring a duplicate, compare managed-resource external
IDs and owner references. To preserve the shared service, pause the stale
controller, set its owned resources to orphan on deletion and observe-only, and
verify those policies before deleting its claim. Confirm the stale XR is deleting,
then remove its pause so deletion and finalizer handling can finish without a
normal reconciliation restoring destructive policies; verify the stale children
are gone and the external service remains intact.

Use native spawn/message/follow-up/interrupt facilities to coordinate workers.
Interrupt for an actual redirect, cancellation or unsafe overlap, not because an
observation timed out. Preserve live process handles and perform required cleanup.

For remote qualification (for example ECS Exec), a zero CLI exit is not proof that
the remote command finished. Require a terminal application success marker and
verify cleanup. If the session reports EOF during a longer command, provide a
live PTY or supported noninteractive transport; do not accept a partial transcript
or repeatedly interpret transport closure as an application/authentication failure.

If subagents are unavailable, use the same explicit assignment/reassessment loop
serially and disclose the weaker separation; do not build an agent framework as a
prerequisite to implementing the system.

## Quasi-formality: sufficient evidence with explicit limits

For each claimed outcome, ask: what supports this claim, and what is the cheapest
experiment that could show it is wrong? Prefer checks that expose classes of bugs:
reference differentials, ownership/isolation invariants, state transitions,
idempotency, ordering and targeted failure injection. Match the check to the
actual risk and effect, including later persisted behavior. Stop adding variants
when the agreed assignment evidence is sufficient; return residual uncertainty to
the parent for prioritization.

"80/20" describes the search for high-value experiments, not a measured confidence
score or permission to omit required security/data-integrity checks. Controlled
real usage expands evidence and reveals model gaps; it does not retroactively
justify unsafe exposure. Never call quasi-formal evidence a proof of the entire
distributed system or equate entity reconciliation with a demonstrated operation.

Claims need outcome/operation identity, context, provenance and assumptions. Reuse
existing capcov representations and receipts. If an assumption or build changes,
reassess dependent claims instead of carrying green forward. Datalog, truth
maintenance, specialization, learned state machines and lineage-driven faults are
possible techniques, not mandatory architecture. Add one only when an actual
qualification exposes a concrete blocking gap and the engine-extension rule permits
it. Research interest is not a worker assignment in a product rebuild by default.

An oracle manifest can name one revision while its executed source was copied
from another runtime checkout. Before starting fixtures, resolve the immutable
revision and compare its application-source bytes with the copied and executed
bytes. Record vendor and generated ORM inputs separately as runtime prerequisites;
their presence does not establish application-source identity.

This workflow is a separation of responsibilities, not deterministic enforcement.
Subagents share model limitations; a parent can still accept bad reasoning. Native
hooks or an external watchdog can strengthen checkpoint enforcement if observed
drift warrants them, but no hook or new service is required for this workflow.
