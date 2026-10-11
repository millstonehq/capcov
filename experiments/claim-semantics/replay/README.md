# Replay rule pack and adversarial replay cases (Phase 4, judge side)

This directory holds the reviewed program that turns one replay receipt --
the PHP system, the Go system and the Shen model replaying the same recorded
requests under one nonce and one snapshot, plus the mutation tool's verdicts
-- into the claim `op_qualified(index, run, op)`: *the op declared by the PHP
census is qualified by this replay*.  The replay harness is a producer of
observations, never an oracle; the judge is these rules.

* `rules-replay-v1.json` - the rule pack in raw IR JSON wire form. **This
  copy is the reviewed one**: edit it here. It is mirrored byte-for-byte to
  `src/capcov/claims/replay/rules-replay-v1.json`, which ships in the wheel as
  package data so the judge (`capcov.claims.replay.pack`, `join`, the compiled
  checker, `capcov experiment claims assumptions`) runs with no source tree.
  After changing this file, copy it over the mirror --
  `tests/claim_semantics/test_replay_pack_package_data.py` fails while they differ.
* `cases/NN-*.json` - one positive control (`00`) and twenty-six adversarial
  shapes (the numbering is not contiguous: the gaps are the rejected cases);
  `rejected/NN-*.json` are the cases the ingestion boundary must refuse.
* `expected.json` / `rejected.json` - the per-claim review tables duplicated
  from every case, and what the rejected case would have yielded.

The adapter `tests/claim_semantics/replay_rules/adapter.py` mirrors the static
one (`load_pack, pack_relations, bundle_payload, load_case, pack_bundle`); the
case format is the static format, so `expected.json` tooling is shared.  The
case *facts* are not hand-written: `tests/claim_semantics/replay_rules/cases.py`
builds every case by running `replay_facts.export_bundle` over a small edit of
`tests/claim_semantics/fixtures/replay_receipt_min` and adding the claim-time
reviewer/census rows; `test_replay_corpus_schema` regenerates each file and
requires it to be identical up to `expected`, so the cases cannot drift from
the exporter.  Claims, diagnostics, output templates and the reviewer's verdict
table (`cases.REVIEW`) are hand-authored; `python -m replay_rules.cases` refuses
to write a case whose evaluator verdict, status, missing premises or
discrepancies differ from that table.

Tests: `test_replay_corpus_schema.py` (evaluator independent),
`test_replay_corpus_evaluation.py` (Python reference evaluator and the
Python/Soufflé differential), `test_replay_adversarial_both_engines.py`
(never skips; certificates from both closures) and
`test_replay_evidence_policy.py` (why-not: the absent leaf is named, adding it
flips the claim).

## The rule pack

`primitives` is a byte-identical copy of
`src/capcov/claims/replay/schema_replay_v1.json` (`relations`).
`supplementary_primitives` is empty: the frozen file declares everything the
judge reads, including the reviewer's claim-time witnesses
(`run_nonce_observed`, `snapshot_observed`, `model_observed`), the reviewer's
admitted model checkers (`model_checker_admitted`), the typed checker's
certificate (`model_well_formed`, producer class `modelcheck`), the PHP census
(`op_declared`, static binding) and the two compatibility relations
(`model_describes_run`, `index_describes_replay`).  `derived` opens with the
two rule-less stubs the exporter carries (`mutant_killed_in`,
`op_qualified_rt`), byte-identical to `replay_facts._DERIVED_TARGET_DECLS` --
`combine` refuses a non-identical duplicate -- and then declares the reviewed
program.  Every derived relation is `binding=runtime` with context `run`,
except `op_qualified_rt` / `op_qualified` (context `index, run`).  The only
claim-modality relation is `op_qualified`.  Derived completeness relations
(precedent: the static `static_route_authorized_closed`): `requested_closed`,
`op_surviving_closed`, `php_disagreement_closed`, `go_disagreement_closed`,
`undeclared_writes_closed`.

Rules, in Datalog shorthand (`M` the model, `IX` the census index):

```text
mutant_killed_in(Run,Mu)        :- mutant_killed(Run,Mu,_).
replay_run_current(Run)         :- replay_run(Run,Nonce,_,_,Snap,Model), run_nonce_observed(Run,Nonce),
                                   snapshot_observed(Snap), model_observed(Model).
replay_run_stale(Run,Nonce,Obs) :- replay_run(Run,Nonce,_,_,_,_), run_nonce_observed(Run,Obs), Nonce != Obs.
requested(Run,Req)              :- replay_request(Run,Req,_,_,_).
requested_closed(Run)           :- replay_requests_closed(Run).
replayed(Run,Op)                :- replay_request(Run,_,_,_,Op).
php_model_agree(Run,Req)        :- php_post_state(Run,Req,S), model_describes_run(M,Run), model_admissible(Run,M,Req,S).
php_model_disagree(Run,Req,S)   :- php_post_state(Run,Req,S), model_describes_run(M,Run),
                                   model_admissible_closed(Run,M), !model_admissible(Run,M,Req,S).
go_model_agree / go_model_disagree: the same over go_post_state.
op_exercised(Run,Op)            :- replay_request(Run,Req,_,_,Op), php_model_agree(Run,Req), go_model_agree(Run,Req).
php_observed(Run,Req)           :- php_post_state(Run,Req,_).         go_observed: the same over go_post_state.
php_observed_closed(Run)        :- php_post_states_closed(Run).        go_observed_closed: from go_post_states_closed.
post_state_gap(Run,Req,"php")   :- requested(Run,Req), replay_requests_closed(Run), php_observed_closed(Run),
                                   !php_observed(Run,Req).             post_state_gap(Run,Req,"go"): the same.
post_state_any(Run,Op)          :- replay_request(Run,Req,_,_,Op), post_state_gap(Run,Req,_).
post_state_gap_closed(Run,Op)   :- replayed(Run,Op), replay_requests_closed(Run), php_post_states_closed(Run),
                                   go_post_states_closed(Run).
model_scope_excluded(M,Tb)      :- model_scope_exclusion(M,Tb,_).
model_scope_excluded_closed(M)  :- model_scope_exclusions_closed(M).
exclusion_applied(Run,Op,Tb)    :- replay_request(Run,Req,_,_,Op), php_effect(Run,Req,Tb,_,_,_), model_describes_run(M,Run),
                                   model_scope_excluded(M,Tb).          (and the same over go_effect)
undeclared_write(Run,Op,Tb)     :- replay_request(Run,Req,_,_,Op), php_effect(Run,Req,Tb,_,_,_), model_describes_run(M,Run),
                                   model_writes_closed(M,Op), !model_writes(M,Op,Tb),
                                   model_scope_exclusions_closed(M), model_scope_excluded_closed(M),
                                   !model_scope_excluded(M,Tb).
undeclared_write(Run,Op,Tb)     :- ... the same over go_effect.
surviving_mutant(Run,Op,Mu)     :- model_describes_run(M,Run), mutant(M,Mu,Op), mutants_closed(M,Op),
                                   mutant_kills_closed(Run), !mutant_killed_in(Run,Mu).
op_has_surviving_mutant(Run,Op) :- surviving_mutant(Run,Op,_).
op_surviving_closed(Run,Op)     :- model_describes_run(M,Run), mutants_closed(M,Op), mutant_kills_closed(Run).
corpus_constrains(Run,Op)       :- model_describes_run(M,Run), mutant(M,_,Op), op_surviving_closed(Run,Op),
                                   !op_has_surviving_mutant(Run,Op).
kill_closure_gap(Run,Mu,Req)    :- mutant_killed(Run,Mu,Req), mutant_kills_closed(Run), requested_closed(Run),
                                   !requested(Run,Req).
kill_closure_gap_any(Run,Op)    :- replayed(Run,Op), kill_closure_gap(Run,_,_).
kill_gap_closed(Run)            :- mutant_kills_closed(Run), replay_requests_closed(Run).
php_disagree_any(Run,Op)        :- replay_request(Run,Req,_,_,Op), php_model_disagree(Run,Req,_).
go_disagree_any(Run,Op)         :- replay_request(Run,Req,_,_,Op), go_model_disagree(Run,Req,_).
undeclared_any(Run,Op)          :- undeclared_write(Run,Op,_).
php_disagreement_closed(Run,Op) :- replayed(Run,Op), replay_run_current(Run), model_describes_run(M,Run),
                                   replay_requests_closed(Run), php_post_states_closed(Run),
                                   model_admissible_closed(Run,M).
go_disagreement_closed(Run,Op)  :- the same body with go_post_states_closed(Run).
undeclared_writes_closed(Run,Op):- replayed(Run,Op), replay_run_current(Run), model_describes_run(M,Run),
                                   replay_requests_closed(Run), php_effects_closed(Run), go_effects_closed(Run),
                                   model_writes_closed(M,Op), model_scope_exclusions_closed(M).
learn_predicted(Run,Op)         :- learn_prediction(Run,_,_,_,_,Op,P,_), P != "unknown".
learn_counterexample(Run,Tape,Req,Op,P,O)
                                :- learn_prediction(Run,_,_,Tape,Req,Op,P,_), learn_observation(Run,Tape,Req,Op,O,_),
                                   P != O, P != "unknown".
learn_counterexample_any(Run,Op):- learn_counterexample(Run,_,_,Op,_,_).
learn_counterexample_closed(Run,Op)
                                :- learn_predicted(Run,Op), learn_run(Run,_,M,L,_,_), model_describes_run(M,Run),
                                   learn_describes_model(L,M), learn_predictions_closed(Run,M),
                                   learn_observations_closed(Run).
learn_consistent(Run,Op)        :- learn_counterexample_closed(Run,Op), !learn_counterexample_any(Run,Op).
learn_unmodeled_any(Run,Op)     :- learn_run(Run,_,M,L,_,_), model_describes_run(M,Run), learn_describes_model(L,M),
                                   learn_unmodeled_closed(M,L), learn_unmodeled(M,L,Op).
learn_unmodeled_gate_closed(Run,Op)
                                :- replayed(Run,Op), replay_run_current(Run).
op_qualified_rt(IX,Run,Op)      :- index_describes_replay(IX,Run), replayed(Run,Op), op_exercised(Run,Op),
                                   model_describes_run(M,Run),
                                   corpus_constrains(Run,Op),
                                   php_disagreement_closed(Run,Op), !php_disagree_any(Run,Op),
                                   go_disagreement_closed(Run,Op),  !go_disagree_any(Run,Op),
                                   undeclared_writes_closed(Run,Op), !undeclared_any(Run,Op),
                                   post_state_gap_closed(Run,Op), !post_state_any(Run,Op),
                                   kill_gap_closed(Run), !kill_closure_gap_any(Run,Op),
                                   effect_order_closed(Run,Op), !effect_order_any(Run,Op),
                                   effect_order_exercised(Run,Op), oracle_stable(Run),
                                   repeat_delete_closed(Run,Op), !repeat_delete_any(Run,Op),
                                   learn_unmodeled_gate_closed(Run,Op), !learn_unmodeled_any(Run,Op),
                                   model_well_formed(M,Checker,Version,Cert),
                                   model_checker_admitted(Checker,Version).
op_qualified(IX,Run,Op)         :- op_declared(IX,Op), index_describes_replay(IX,Run), op_qualified_rt(IX,Run,Op).
```

Design points a reviewer should check:

* **Every model/run join carries `model_describes_run(M, Run)`.**  The
  `model` and `run` context columns have different names, so the validator's
  cross-context check does not force this; the pack lint in
  `test_replay_corpus_schema` does, for every rule that reads a model-context
  relation and a run-context relation.  Without the witness no `php_model_*`,
  `go_model_*`, `undeclared_write`, `surviving_mutant` or closure row exists
  (case 04).
* **Negation only through named completeness.**  `!model_admissible` needs
  `model_admissible_closed(Run, M)`, `!model_writes` needs
  `model_writes_closed(M, Op)`, `!mutant_killed_in` needs
  `mutant_kills_closed(Run)`, `!requested` needs `requested_closed(Run)`, and
  the three per-op "any" relations are negated only under their derived
  closures, whose bodies list the closure of *every* positive input --
  `replay_run_current`, `model_describes_run`, `replay_requests_closed`,
  `model_admissible_closed` and, for the write check, `php_effects_closed`,
  `go_effects_closed`, `model_writes_closed(M, Op)`.  A run that is not current
  (missing witness, case 05; nonce mismatch, case 06) therefore has no closure
  and cannot qualify anything.
* **`corpus_constrains` needs at least one declared mutant** for the op
  (positive `mutant(M, _, Op)`) and the closed absence of survivors:
  `op_surviving_closed` is the derived completeness of
  `op_has_surviving_mutant`.  An op with no mutants is never constrained.
* **The static join is isolated in the final rule.**  `op_qualified_rt` is
  runtime-only (it reads `index_describes_replay` only to carry the index);
  `op_qualified` is the one rule that reads a static relation
  (`op_declared`) and it carries `index_describes_replay(IX, Run)`, whose
  `compatibility_targets` name both `op_declared` and `op_qualified_rt`.
  `test_replay_evidence_policy` shows that the same join without the witness
  is rejected with `mixed-binding-join`.
* **`op_exercised`** is not in the section's original list.  Every PHP/Go/Shen
  observation enters `op_qualified_rt` only through negated atoms, which
  contribute no support leaf; requiring at least one replayed request of the
  op whose PHP and Go post-states the model admits closes that vacuity (an op
  with zero agreeing requests is not qualified by "no disagreement") and is
  what makes the control's support leaves span the `php`, `go` and `shen`
  producer classes.  `replayed` is kept as the spec writes it; `op_exercised`
  implies it.
* **Post-state completeness** (schema addition, pre-consumer):
  `php_post_states_closed(run)` / `go_post_states_closed(run)` complete the
  post-state relations (`receipt.json` `closed.php_post_states` /
  `closed.go_post_states`).  Without them a replayed request whose post-state
  the runner omitted was invisible to `php_disagree_any` while the
  disagreement closure still held, so one agreeing request could satisfy
  `op_exercised` and every other request's disagreement could be hidden by
  omission.  Both disagreement closures now require the witness, and
  `op_qualified_rt` is gated on `!post_state_any(Run, Op)` under
  `post_state_gap_closed`, where `post_state_gap(Run, Req, side)` is a
  replayed request with no post-state on that side (case 09; the mutation
  tests in `test_replay_corpus_evaluation` show that dropping the gate flips
  the case).
* **Reviewer scope exclusions** (schema addition, pre-consumer).  The
  infrastructure tables the systems write around an op (a session touch,
  job bookkeeping, a cache, an outbox) leave the `undeclared_write` judgement
  only through explicit reviewer-owned facts: `model_scope_exclusion(model,
  table, reason)` (modality *assumption*, producer `reviewer`, exported as
  assumption-kind evidence from `model_scope_exclusions.json`, whose source
  names the reviewer, the date and the model/run reviewed) under
  `model_scope_exclusions_closed(model)` (`closed.model_scope_exclusions`).
  The negation in `undeclared_write` is gated by that closure through the
  projection `model_scope_excluded` and its derived completeness, and the
  closure is an input of `undeclared_writes_closed`, so an empty exclusion set
  with no closure qualifies nothing (case 15) and rows without the closure
  license nothing (case 14).  A negated atom carries no leaf, so the
  `op_qualified` certificate cites the closure witness; the exclusion rows
  themselves are the assumption leaves of the companion `exclusion_applied`
  certificate (case 13), which is what the join summary counts as
  `assumption_leaves` and reports as "qualified under N reviewer exclusions".
* **`kill_closure_gap`** (case 08) is the replay analogue of the static
  `02-lying-witness` gap: a kill that names a request outside the closed
  request set while `mutant_kills_closed` is asserted.  It is derived *from*
  the lying witness, so its support intersects `forbidden_leaves`.  Unlike
  the static precedent the lie does **not** qualify anything: `op_qualified_rt`
  is gated on `!kill_closure_gap_any(Run, Op)` under `kill_gap_closed(Run)`
  (kills and requests both closed), and a gap anywhere in the run poisons
  every replayed op of that run, because the contradicted witness is per run
  -- an op whose own mutants were honestly killed still rests on it.
* **Witness producer authority.**  Every completeness and compatibility
  relation of the schema names the class that vouches for it: the harness
  (`replay`) owns the request, effect, post-state and kill closures; the
  model runner (`shen`) owns `model_admissible_closed`, `model_writes_closed`
  and `model_describes_run`; the mutation tool (`mut`) owns
  `mutants_closed`; the reviewer owns `index_describes_replay`.  The
  exporter tags each witness with its class (`replay_facts.witness_source`)
  and the validator refuses a closure emitted by another producer
  (`rejected/12-closure-producer-violation`: `model_admissible_closed`
  sourced `replay`).

* **Statement order** (v1 ordering addendum).  `php_effect_seq` /
  `go_effect_seq(run, req, seq, table, kind, pk)` and `model_effect_seq(run,
  model, req, seq, table, kind, pk)` carry the order the effects were
  *observed* in, which the folded `php_effect` / `go_effect` tables discard.
  `effect_order_violation(run, side, req, table_a, table_b)` fires when the
  model declares `table_a` before `table_b` for a request and that side wrote
  them the other way round; the join is on `(table, kind)` and never on `pk`,
  because the model's pk for a derived table is a domain id while the
  system's is a row id.  `op_qualified_rt` is gated on
  `!effect_order_any(Run, Op)` under `effect_order_closed(Run, Op)` (all three
  sequences closed) *and* on `effect_order_exercised(Run, Op)`, which requires
  a request whose order both sides actually demonstrated -- without it an op
  whose requests each write a single table would pass the order gate
  vacuously (cases 17, 21).  Only SQL tables captured with a timeline appear:
  store effects (`redis`, `mongo:*`) come from before/after snapshot diffs and
  have no order, so the audit leg of a declared order is vacuous by
  construction and the receipt says so in `receipts.effect_seq_scope`.
* **The repeat delete** (v1 ordering addendum).  `replay_request_seq(run, req,
  seq, target)` gives the tape its order and its targets (`target` is the HTTP
  method and raw path, so two requests aimed at one resource share it), and
  `php_response` / `go_response(run, req, status)` the status each system
  returned.  `delete_target(run, req, seq, tenant, target)` carries the tenant
  the request was served for, and `repeat_delete(run, req, target)` -- a later
  request for a target an earlier request already addressed -- joins the two
  positions on `(tenant, target)`, so the same path under two tenants is two
  resources, not a repeat.  `first_delete_committed(run, req, target)` names
  the *first* delete of that `(tenant, target)`: the negated
  `earlier_delete(run, tenant, target, seq)` under `earlier_delete_closed(run)`
  refuses to read a later committing delete as the commit (case 24), and the
  request must have answered 200 on both sides *and* written an `issue` update
  on both (case 25).  When it did,
  the claim `repeat_delete_not_found(run, target)` says the repeat answered
  404 on both sides and, under both closed effect tables, wrote nothing
  *outside the reviewer's scope exclusions* (case 00).
  `repeat_delete_violation(run, req, side)` is the negative shape -- a status
  that is not 404 on either side, or any effect at all (`side = "effects"`,
  case 18).  **The reviewer's scope exclusions apply here exactly as they do
  to `undeclared_write`**: `repeat_delete_has_effect` joins
  `model_describes_run(M, Run)`, `model_scope_excluded_closed(M)` and
  `!model_scope_excluded(M, Tb)`, and its completeness
  `repeat_delete_effects_closed(Run)` lists `model_scope_exclusions_closed(M)`
  among its inputs so an unclosed exclusion set withholds the claim instead of
  vacuously supporting it (cases 04, 14, 15).  The excluded tables are the
  bookkeeping writes a system makes on *every* authenticated request -- a
  session-token touch, a cache key, a job-status row -- and a repeat DELETE
  that 404s still authenticates, so without the guard a correct port would
  fail the claim on rows the reviewer already accepted (case 23).
  **`op_qualified_rt` is gated on the repeat**: `repeat_delete_closed(Run, Op),
  !repeat_delete_any(Run, Op)`, where `repeat_delete_any(run, op)` joins a
  `repeat_delete_violation` to the *op of the violating request*, so one op's
  repeat does not poison the run.  The earlier design left the repeat as a claim
  of its own on the theory that a second delete which answers 200 or writes a
  business table is caught through the model's `Admissible`.  Case 18 disproves
  it: it plants an `issue` update -- a **declared** table -- on the
  second delete, and every per-request premise still holds, so without this gate
  the op qualified and the receipt's only defect lived in a claim nothing read.
  With it, case 18 leaves `delete-issue` unresolved (missing premise
  `repeat_delete`) while `issues.create` / `issues.close` stay supported, and
  `test_replay_corpus_evaluation` shows that dropping either half of the gate
  flips it back.
  `repeat_delete_closed(Run, Op)` lists the closure of **every** positive input
  of the chain it negates: `replay_run_current`, `replay_requests_closed`,
  `replay_request_seqs_closed` (the tape order `repeat_delete` reads),
  `php_responses_closed` / `go_responses_closed` (the status half of
  `repeat_delete_violation` and of `first_delete_committed`),
  `php_effects_closed` / `go_effects_closed` (its effect half and
  `repeat_delete_has_effect`) and -- because `repeat_delete_has_effect` itself
  negates `model_scope_excluded` -- `model_describes_run(M, Run)` with
  `model_scope_exclusions_closed(M)`.  Case 31 opens the PHP response table and
  every op goes unresolved on `php_responses_closed`, while the per-target claim
  `repeat_delete_not_found`, which reads the response rows *positively*, stays
  supported: a positive claim may rest on the rows it was given, a negation may
  not.
* **Checker authority is operation scoped.** `model_well_formed(M, Checker,
  Version, Binary, StructureCert)` certifies global structure. For each op,
  `model_operation_checked(M, Op, Checker, Version, Binary, OperationCert)`
  exists only when that operation's write, matrix, atlas and registry checks
  pass. `model_checker_admitted` joins the same six values as the operation
  row; a checked operation cannot borrow another operation's certificate.
  `Binary` is the SHA-256 of the resolved native Shen executable. The stable
  semantic certificate also binds the Bifrost launcher hash, checker sources,
  model, and exact judgements while excluding nonce, elapsed time and run
  envelope hashes.
* **Reviewer authority is external.** The exporter ignores receipt-local
  `model_checkers.json`, rejects placeholder reviewer names as non-authority,
  and admits only an exact operation tuple supplied by the caller. The receipt
  exporter still validates the model digest, checker identity/version and
  stable certificate before it projects any authority into IR.
* **Pending remains distinct from unsupported.** Global structure, per-op
  checker coverage and reviewer admission are positive premises with no
  closure relation, and remain last in `op_qualified_rt`. Missing authority
  therefore cannot mask an earlier system/model finding. A checked and
  admitted operation can qualify while sibling operations remain pending at
  `model_operation_checked`; the synthetic corpus case
  `32-partial-operation-check` demonstrates that split. A receipt from a
  real system with no admitted checker evidence remains pending until a
  reviewer supplies an exact admission.
* **The learn campaign** (v1 learn addendum).  A *learn campaign* is a separate
  producer chain -- a tape generator, the PHP oracle and the model host -- that
  replays generated tapes against the oracle and against the model and reports,
  per tape position, what each said.  It is **optional**, and its receipt lives
  in `learn/` beside `receipt.json` (`replay_facts` module docstring, THE LEARN
  RECEIPT).  The judge reads two things from it.

  `learn_consistent(run, op)` is the claim that, under `learn_predictions_closed`
  and `learn_observations_closed`, no tape position of that op has the model
  predicting a class the oracle did not produce.  `learn_counterexample(run,
  tape, req, op, predicted, observed)` is such a position, and it names the step:
  the `req` key is `<tape>/<request id>`, because a request id repeats across
  tapes.  The reserved predicted class `"unknown"` means the model made no
  prediction there and is never a counterexample.

  `learn_unmodeled_any(run, op)` **downgrades** `op_qualified_rt`: an op the
  campaign's *closed* unmodelled list names does not qualify, because the model
  the judge is qualifying against does not model it.  Every input of that
  relation is positive -- the campaign must be bound to the run (`learn_run`), to
  the model (`model_describes_run`, `learn_describes_model`) and must have closed
  its list (`learn_unmodeled_closed`) -- so a receipt with no `learn/` derives
  nothing and is judged exactly as it was.  Its completeness
  `learn_unmodeled_gate_closed(run, op)` therefore reads only `replayed` and
  `replay_run_current`: what it licenses is "the downgrades **this bundle**
  carries are all of them", never "the model covers every op", which no absence
  could evidence.  **Absence of a learn campaign is not evidence of coverage.**
  The gate can only take qualification away; that asymmetry is what makes it safe
  to add to every receipt at once, and it is the one thing a reviewer must hold
  on to here.

  Producer authority is the usual split: `replay` owns `learn_run` and
  `learn_observations_closed` (the harness ran the tapes), `php` owns
  `learn_observation` (the oracle answered), and `shen` owns `learn_prediction`,
  `learn_unmodeled`, their closures and the compatibility row
  `learn_describes_model(learn, model)`.  A campaign whose header names another
  model is `stale`, not `invalid-input`: it was run against another artifact.

* **Cross-run stability** (v1 ordering addendum).  `replay_stability(run,
  run_a, run_b, side, stable)` binds the receipt's run to a *selftest* of the
  same oracle: two further runs of the same tape whose provenance (oracle
  commit, PHP/schema/seed digests) equals this run's.  The harness emits the
  row only when that provenance matches, so the row itself is the binding --
  and the row depends on `external:run:<run_a>` / `external:run:<run_b>`, so a
  certificate that rests on cross-run stability names the two runs it rests on
  (what the harness checked about them is still the harness's word, and a
  reviewer who wants more must read those runs);
  `stable` is `"true"` iff every request agreed on status and net SQL effects
  between the two.  `oracle_unstable(run)` fires on any `"false"` row and
  `oracle_stable(run)` needs the closure, a `"true"` PHP row and the absence
  of an unstable one; `op_qualified_rt` requires `oracle_stable`.  An oracle
  that does not reproduce itself qualifies nothing, however well PHP, Go and
  the model agree within one run (cases 19, 20).  Case 26 is the one that makes
  the negated atom load-bearing: the PHP row says `"true"` and a second row
  says the Go side did not reproduce, so `oracle_stable`'s positive premise
  holds and only `!oracle_unstable` refuses.

## Deviations from the design, and what the rules do not cover

* **`delete_target` still names the op constant `delete-issue`** instead of
  being generic over `replay_request_seq`.  The tape table carries *every*
  request, not only the DELETEs, so an op-generic rule would read two POSTs to
  one collection path as a delete and its repeat.  Genericity needs either a
  method column on `replay_request_seq` or an op-shaped predicate; until then
  the constant is the honest restriction, and a second delete op would need a
  second rule.
* **`first_delete_committed` names `issue` / `update`** as what a committed
  delete writes -- a soft delete, which is what the modelled op does.  A hard
  delete (`issue` / `delete`) would need a second rule; the pack would report
  the repeat claim as unresolved rather than wrongly supported, which is the
  safe direction.
* **A prediction is a set, but the counterexample rule compares one at a time.**
  The model may admit several post-states for one tape position, so
  `learn_prediction` is keyed by `(run, model, learn, tape, req, state_digest)`
  and a position can carry more than one row.  `learn_counterexample` fires when
  *some* predicted class differs from the observed one, which for a position
  whose rows all agree on the class -- the only shape the model produces today --
  is exact, and otherwise over-reports.  Over-reporting withdraws
  `learn_consistent`, which is the safe direction; a model that assigns two
  different classes to one position must refine the rule (compare against the
  *set* under a per-position closure) before the pack can judge it.
* **The order join is on `(table, kind)`, so a model that declared two
  statements with the same `(table, kind)` for one request** -- two `issue`
  updates, say -- would pair them crosswise and could report a violation that
  is only an artefact of the pairing.  The model declares one statement per
  `(table, kind)` per request, so this is not reachable today; a model that
  changes that must refine the join (on `pk`, or on the sequence position
  itself) before the pack can judge it.
* **Case 17 plants the order fault on the Go side only.**  The two
  `effect_order_violation` rules are the same shape over `php_effect_seq` and
  `go_effect_seq` and the pack lint checks both, but the PHP leg is exercised
  by symmetry, not by a case of its own.

## One observation per key

The exporter, not the pack, settles duplicate observations: two `php_effect`
/ `go_effect` / `model_effect` rows sharing `(run, [model,] req, table, kind,
pk)` with different `cols_digest`, or two `php_post_state` / `go_post_state`
rows for one `(run, req)` with different `state_digest`, are contradictory
reports of the same event and make `export_bundle` return `invalid-input`
naming the key (`replay_facts.UNIQUE_KEYS`).  A pack-side uniqueness
completeness was rejected because it would let both rows into the fact set
and leave a rule to choose between them; the judge never carries a
contradiction it could have refused at ingestion.  A row repeated verbatim is
one fact, and `model_admissible` stays a set (several admissible states per
request).

## Validator findings

None.  The pack validates alone with zero issues (`pack_bundle`), every case
ingests with `validate=True`, and `combine(export_bundle(...), pack_bundle())`
accepts the exporter's stubs.  No rule needed restructuring for
`missing-completeness`, `unsafe-negation`, `mixed-binding-join`,
`missing-compatibility` or `recursive-negation`; the pack has no recursion at
all.

## How a reviewer decides a case

As for the static pack: `facts` are the only inputs (there are no
assumptions), `claims` name the rows in question, `review_notes` say why the
table holds, `expected.claims` is the table.  `supported` means one ground
instantiation exists and `support_leaves` is the evidence of the canonical
proof (shortest, then lexically least: for `issues.create`, replayed by
`req-1` and `req-3`, the proof uses `req-1`).  Negated atoms and the witnesses
that license them contribute the witness row, never the absent row.
`unresolved` means no instantiation exists and `missing_premises` names the
absent leaf.

### Why-not templates

Each `op_qualified` claim carries `missing_premise` templates
(`when_claim: "unresolved"`).  A template whose satisfying witness is present
in the case names that row in `excludes_evidence` and is therefore suppressed;
a template for an absent witness has nothing to exclude and fires.  The
witness rows are made relevant to the claim by per-claim `observation`
diagnostics (`model_describes_run`, `run_nonce_observed`, `snapshot_observed`,
`model_observed`), exactly as the static lying-witness case makes its trigger
rows relevant.  Planted blockers (cases 01-03) name the *agreement* that is
missing (`php_model_agree`, `model_writes`, `mutant_killed`) and are triggered
by the row that blocks (`requires_all_evidence`).  The result is that an
unresolved `op_qualified` renders exactly one missing premise per case.

`operational_status` is `complete` unless a claim declares a diagnostic with
another status whose trigger rows exist: `stale` on `replay_run_stale` (case
06).  `discrepancies` are `discrepancy` templates on the companion claims
whose `requires_all_evidence` rows exist.  `forbidden_leaves` names evidence a
derivation must not use (the lying `mutant_kills_closed` witness of case 08);
`observed_leaves` is every input relevant to the claim.

### Evidence ids and provenance

Exported rows: `<prefix>:<replay12>:<relation>:<row12>` exactly as
`replay_facts` emits them (`prefix` from the relation's producer class:
`replay`, `php`, `go`, `shen`, `mut`, `reviewer`, `modelcheck`; `php-census` shares `php`;
witnesses and compatibility rows use their owning class's prefix; `replay12` is the
`replay-relations-v1` identity of the case's receipt variant; `row12 =
sha256(canonical_json([relation, row]))[:12]`).  Claim-time rows the judge
adds -- the reviewer's three witnesses and the census -- use
`<prefix>:claim-time:<relation>:<row12>`, mirroring the static
`static:claim-time:...` convention.  `Evidence.source` is the producer string
and its first token is the producer class the validator checks
(`evidence-producer`).  All digests in the cases are synthetic; a live join
must take the model digest from the model's own digest script, never from a
constant in a case.

## Case table

| case | seeded edit of the fixture receipt | reviewed expectation |
|---|---|---|
| 00 positive control | none; reviewer observed nonce, snapshot, model; census declares all three ops; the model typechecked under an admitted checker | `op_qualified` supported/complete for `issues.create`, `issues.close` and `delete-issue`; leaves span `replay, php, go, shen, mut, reviewer, php-census, modelcheck`; companions `repeat_delete_not_found(run, DELETE /api/issues/1)` and `oracle_stable(run)` supported |
| 01 planted disagreement | `php_post_state` of req-2 outside the admissible set | close: unresolved, missing `php_model_agree`; create: supported; companion `php_model_disagree` supported, discrepancy `php-state-outside-model` |
| 02 planted undeclared write | `go_effect` insert into `audit_log` for req-2 | close: unresolved, missing `model_writes`; companion `undeclared_write` supported, discrepancy `undeclared-table` |
| 03 surviving mutant | `mutant_killed` row for m-2 removed | close: unresolved, missing `mutant_killed`; companion `surviving_mutant` supported, discrepancy `mutant-not-killed` |
| 04 missing model witness | exported `model_describes_run` withheld | both unresolved, missing `model_describes_run`; no `php_model_*`/`go_model_*` rows; companion `php_model_agree` unresolved |
| 05 missing snapshot witness | no `snapshot_observed` | both unresolved, missing `replay_run_current` |
| 06 stale replay | `run_nonce_observed` carries another nonce | both unresolved, operational `stale`, missing `replay_run_current`; companion `replay_run_stale` supported |
| 09 missing post-state | `php_post_state` row for req-3 (issues.create) removed; `php_post_states` still closed | create: unresolved, missing `php_post_state` (req-1 alone satisfies `op_exercised`, so only the gate catches it); close: supported; companion `post_state_gap(run, req-3, php)` supported, discrepancy `post-state-missing` |
| 10 missing effects closure | `closed.php_effects = false` (go stays closed) | both unresolved, missing `php_effects_closed`: an open effect table cannot license `!undeclared_any` |
| 11 missing admissible closure | `closed.model_admissible = false`; `model_describes_run` present (unlike 04) | both unresolved, missing `model_admissible_closed`; companion `php_model_agree` supported (observed agreement is not closed agreement) |
| 13 excluded undeclared write | case 02's Go `audit_log` write plus a closed reviewer exclusion file naming `authentication`, `redis`, `audit_log` | both ops supported; companion `exclusion_applied(run, issues.close, audit_log)` supported with the exclusion assumption as a leaf, discrepancy `write-excluded-by-reviewer`; contrast 02 (same table, not excluded: unresolved) |
| 14 exclusions not closed | case 13's file with `closed.model_scope_exclusions = false` | both unresolved, missing `model_scope_exclusions_closed` |
| 15 no exclusions, no closure | control without the file and without the closure | both unresolved, missing `model_scope_exclusions_closed`: the reviewer must close an empty set, not say nothing |
| 17 effect order violation | `go_effect_seq` seq of req-1's `issue` and `entity_statistics` swapped (`go_effect` untouched) | create: unresolved, missing `effect_order_respected`; close and delete-issue: supported; companion `effect_order_violation(run, go, req-1, issue, entity_statistics)` supported, discrepancy `effect-order-violated` |
| 18 repeat delete with effects | `php_effect` (and `php_effect_seq`) gain an `issue` update for req-5, the repeat of req-4's DELETE | delete-issue: unresolved, missing `repeat_delete_not_found`; create and close supported; companion `repeat_delete_violation(run, req-5, effects)` supported, discrepancy `repeat-delete-with-effects`; companion `repeat_delete_not_found` unresolved on the negated `repeat_delete_has_effect` |
| 19 unstable oracle | `replay_stability` row says `stable = false` | all three unresolved, missing `oracle_stable`; companion `oracle_unstable` supported, discrepancy `oracle-unstable` |
| 20 missing stability closure | `closed.replay_stability = false` (the row stays, and says `true`) | all three unresolved, missing `replay_stability_closed` |
| 21 missing effect-seq closure | `closed.php_effect_seqs = false` (go and model stay closed) | all three unresolved, missing `php_effect_seqs_closed`: an open sequence cannot license `!effect_order_any` |
| 08 lying closure | `mutant_killed` m-1 names req-9, not a replayed request; closures asserted | both ops **unresolved** (contradiction gate; missing premise `replay_request` for req-9); `kill_closure_gap` supported with support ∩ forbidden = the `mutant_kills_closed` witness (seeded fault), discrepancy `kill-outside-replayed-requests` |
| 27 model not well formed | `model_well_formed.json` removed while operation-local checked rows remain | all three unresolved, missing `model_well_formed`: global structure is a positive premise and an uncertified structure is not qualified by silence |
| 28 well-formed, other model | no `model_well_formed.json`; a *claim-time* structural certificate naming another model digest (the exporter calls such a row inside a receipt `stale`); operation rows and admissions stay bound to the receipt model | all three unresolved, missing `model_well_formed`: the foreign structural certificate does not join to the model bound by `model_describes_run` |
| 29 checker not admitted | no caller-supplied reviewer admission (receipt-local `model_checkers.json` is ignored) | all three unresolved, missing `model_checker_admitted`: the reviewer must admit the exact model, operation, checker, version, binary, and semantic-certificate tuple |
| 32 partial operation check | only `delete-issue` has an operation-local checker row and exact reviewer admission | delete-issue supported; `issues.create` and `issues.close` unresolved, missing `model_operation_checked`; no operation inherits another operation's checker fact |
| rejected 30 | control with the `model_well_formed` row sourced `shen shen-model-host v1` (the model host certifying its own model) | `load_case` raises; `evidence-producer` ×1; evaluated unvalidated every claim would be supported |
| rejected 07 | control with `php_post_state` sourced `shen shen-model-host v1` | `load_case` raises; `validate_bundle` lists `evidence-producer` ×3; evaluated unvalidated both ops would be supported |
| rejected 16 | control with the two `model_scope_exclusion` assumptions sourced `replay …` (the harness excluding on the reviewer's behalf) | `load_case` raises; `evidence-producer` ×2; evaluated unvalidated both ops would be supported |
| rejected 12 | control with `model_admissible_closed` sourced `replay ...` (the harness closing the model runner's table) | `load_case` raises; `evidence-producer` ×1; evaluated unvalidated every claim would be supported |
| rejected 22 | control with the seven `php_effect_seq` rows sourced `shen shen-model-host v1` (the model host reporting PHP's statement order) | `load_case` raises; `evidence-producer` ×7; evaluated unvalidated every claim would be supported |
