# capcov.faults

Test support for a program that writes to more than one store: a primary store,
an object store, and an external service without conditional writes or
idempotency keys. The harness injects crashes and service faults, records what
happened, and judges the run with the `capcov.claims` kernels.

Standard library only. `capcov discover`, `reconcile` and `gate` never import it.

## Modules

| Module | Contents |
|---|---|
| `history` | `History`, `Result` (`ok`, `fail`, `info`), `LogicalClock` |
| `service` | `FakeService`, its fault menu, `ChangeRead`, `GroundTruth` |
| `crash` | `Crash`, `Boundaries`, `instrument`, `boundary_context`, `crash_points`, `explore` |
| `machine` | `FaultMachine`, `enumerate_schedules`, `hypothesis_machine` |
| `checker` | `build_bundle`, `check`, `Finals`, `FinalRead`, `Rows`, the rule pack loader |

## History

`History.invoke(op, actor, kind, target, attempt=None)` returns a token and
`complete(token, result, value=None, error=None)` closes it, so each operation
is two records. `with history.operation(...)` completes `ok` on return and asks
`classify(exc)` on an exception. The default classifier answers `info` for every
exception: a timeout does not say whether the effect landed. Pass
`History(classify=service.classify)` to record the fake service's definite
refusals as `fail`. `close()` completes every open invocation as `info` and sets
`complete_flag`.

`to_json()` writes a header line `{"schema_version": 1, "complete": ...}` and
then one record per line. Timestamps are logical. Values are stored as the
sha256 of their canonical JSON.

## Fake service

`FakeService(clock, history=None, name="service")` offers `read`, `write`
(unconditional replace), `add` and `remove` for set-valued keys, `create` (no
deduplication), `search`, and `changes(key=None)`, which returns
`ChangeRead(entries, complete)` with entries `(seq, actor, at, key, value_digest)`.

Faults are queued with `inject(fault, match=None)` and consumed by the next
matching call.

| Fault | Effect |
|---|---|
| `Refuse(status)` | raises `Refused`; nothing is applied |
| `LoseResponse()` | applies, then raises `ResponseLost` |
| `DelayApply(ticks)` | raises `ResponseLost`; applies on a later `advance()` |
| `ApplyTwice()` | applies one request twice |
| `EditBeforeNextWrite(key, value, actor)` | an outside editor writes after the caller's read and before its next write |
| `DelayChangeEntry(ticks)` | applies, but withholds the change-log entry |
| `AmbiguousError(status, applied)` | raises `Ambiguous`; `applied` says whether it landed |
| `DuplicateOnConcurrentCreate(actor)` | a second actor's identical create lands too; both records are kept |

`advance(ticks)` and `heal()` release delayed writes and withheld entries.
`ground_truth()` returns the final values, every applied write with the request
that caused it, every write that replaced a value its sender had not read, and
`pending`: the requests the service accepted and has not applied.

A write replaced an unread value when the key changed after the last read or
write its sender had made of that key when it sent the request. `Request.seen`
records that sequence number at the moment of sending, so a `DelayApply` write
that lands after its sender read or wrote a newer value is reported. The second
apply of one `ApplyTwice` request is not reported.

`ChangeRead.complete` is false while an entry is withheld or a write is pending.
A real service does not report that; the checker uses it to decide whether a
missing entry means anything.

## Crash boundaries

`Crash` derives from `BaseException`, so `except Exception` in the program under
test does not catch it. `Boundaries(history, plan=None).effect(label, call,
store, actor, op)` numbers each side effect and records it. A `CrashPoint` in
the plan raises `Crash` before the call (nothing recorded) or after it (recorded
`info`); a `RaiseAfter` raises a given exception after the call, which is a lost
response.

`instrument(obj, methods, boundaries, store)` returns a proxy whose named
methods are boundaries. `boundary_context(label, cm, boundaries, store)` makes a
context manager's exit one boundary, for a transaction commit; a crash before
the commit rolls the transaction back.

`crash_points(run)` lists `before` and `after` for each boundary from one dry
run. `explore(Scenario(setup, steps), recover, check)` runs a fresh scenario to
each crash point, runs `recover` as a second actor, closes the history, and
calls `check(state, history)`. The first run it returns has no crash.

## Schedules

`FaultMachine` has six rules: `start_operation`, `run_next_step`, `crash`,
`drop_response`, `run_recovery` and `start_second_actor`. A subclass supplies
`setup`, `operation`, `recover` and `invariants`. `finish()` abandons work in
flight, runs recovery, closes the history and returns the failed invariants.

A step that lets one of `expected_errors` escape (by default `ServiceError`)
ends its operation, and recovery and the invariants judge what it left. Any
other exception from a step raises `UnexpectedStepError`, with the original as
`__cause__` and, under the enumerator, the rules that led to it in `schedule`.
A schedule in which the program fails on a defect therefore has no verdict.

`enumerate_schedules(factory, depth)` tries every schedule up to `depth` rules
and returns the shortest one that fails, or `None`. `hypothesis_machine(factory)`
returns a Hypothesis `RuleBasedStateMachine` subclass; it needs the
`faults-property` extra and raises `HypothesisUnavailable` without it.

## Checker

`build_bundle(history, service, finals, extra=())` returns a
`capcov.claims.ir.Bundle`. Every relation has the context column `run`.

| Producer class | Relations |
|---|---|
| `history` | `op_invoked`, `op_completed`, `effect_sent`, `ops_closed` |
| `service-log` | `change`, `changes_closed` |
| `ground-truth` | `applied`, `overwrote_unread`, `applied_closed` |
| `final-read` | `final_value`, `final_closed(run, store)`, `finals_closed` |
| `consumer` | `intent`, `recorded`, `settled`, `overwrite_reported`, and `intents_closed`, `recorded_closed`, `settled_closed`, `reports_closed` |

A completeness fact is emitted only when its input says it is complete:
`History.complete_flag`, `ChangeRead.complete`, `ServiceFacts.applied_complete`,
`FinalRead.complete`, `Rows.complete`. No flag defaults to true:
`FinalRead(store, rows)` and `Rows(rows)` are incomplete until the caller passes
`complete=True`. `finals_closed(run)` is emitted when every store with a final
read or an intent has a complete final read.

`applied_closed` is emitted only when the fake service holds no pending write.
While one is pending, `changes_closed` is withheld too and the service's own
final values are an incomplete final read, so every invariant that reads the
service is `unresolved`. Call `heal()` or `advance()` before the final reads.

The consumer rows come from the program's own primary store, through `Finals`:

| Relation | Row | Meaning |
|---|---|---|
| `intent` | `(intent, op, store, key, value)` | an effect the program recorded before making it |
| `recorded` | `(intent, outcome)` | `applied` or `absent`: what the program believes happened |
| `settled` | `(intent,)` | no recovery will act on this intent again |
| `overwrite_reported` | `(store, seq)` | a write the program reported as replacing a value it had not read |

The shipped pack `rules-faults-v1.json` defines six invariants. Each has one
claim, `holds(run, name)`.

| Invariant | Violated when |
|---|---|
| `effect_applied_twice` | two requests applied the same intent |
| `orphan_change` | the change log holds a write by one of the program's actors with no intent |
| `lost_acknowledgement` | an operation completed `ok` and an intent of it is in no store |
| `silent_overwrite` | a write replaced an unread value and the program did not report it |
| `unsettled_effect` | an intent was never settled |
| `split_outcome` | the recorded outcome of an intent disagrees with the stores |

`check(bundle, engine="python")` returns `Verdicts`. An invariant passes only
when its claim is `supported` with operational status `complete`. A derived
violation makes it `violated`. Missing completeness makes it `unresolved`, which
fails the run, and `missing_premises` names the absent relation, for example
`{"relation": "changes_closed"}`. `engine="both"` also runs Souffle through
`capcov.claims.differential.compare` and raises `SouffleRequired` when `souffle`
is not on `PATH`.

A consumer invariant `mine` is passed through `extra`: a `RelationDecl` if it
needs one, a rule deriving `violated(Run, "mine")`, a rule deriving
`violated_closed(Run, "mine")` from the completeness relations it depends on,
and `invariant_claim(run, "mine")`.

`ExtraFacts(relation, rows)` in `extra` supplies facts for a relation declared
in the same `extra`. The relation must be primitive, have the context column
`run` first, and admit the producer class `consumer` (or declare none). Rows
leave `run` out and are checked for arity and column type, as the built-in
facts are. A completeness relation is closed with one empty row:
`ExtraFacts("mine_closed", [()])`. `extra` cannot supply facts for a relation
of the pack or declare one again.

## Command line

```sh
capcov faults check --history h.json --finals f.json [--rules pack.json] [--engine python|both] [--json]
```

`h.json` is `History.to_json()` and `f.json` is `checker.finals_to_json(finals,
service)`. Exit 0 when every invariant is supported, 1 when any is violated or
unresolved, 2 on invalid input or when `--engine both` finds no Souffle, 3 when
the two kernels disagree.

The `service` object of `f.json` must carry the boolean `applied_complete`, an
`applied` origin of `request` or `outside`, and string digests in `final`.
Anything else exits 2.

## Limits

- One external service per run, one thread, logical time. The fake service does
  not model a network.
- Two operations interleave by nesting: the second actor runs between two steps
  of the first. Recovery runs only while no operation is in flight.
- Recovery is not crashed and is not given a lost response: `explore` and
  `FaultMachine` hand it a `Boundaries` with no plan. `heal()` drops queued
  service faults, so a scenario that heals before recovery runs recovery
  against a healthy service. A fault still queued when recovery starts does
  fire on recovery's calls. A recovery that must survive its own crash needs a
  scenario that runs it as the operation under test.
- Intents are matched to stores by `(store, key, value digest)`. Two intents of
  one run must differ in at least one of the three.
- `orphan_change` compares the value a key holds after a write with the intent's
  value, so the shipped rule fits `write`. `add`, `remove` and `create` need a
  consumer rule.
- A request the service itself applied twice (`ApplyTwice`) is recorded in the
  ground truth and is not an `effect_applied_twice` violation, which counts
  distinct requests.
- The enumerator's cost grows exponentially with depth.
