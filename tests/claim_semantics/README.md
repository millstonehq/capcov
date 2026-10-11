# Stage A claim-semantics corpus

The numbered JSON files are evaluator-independent semantic controls.  A
reviewer can inspect `claims`, `facts`, `assumptions`, and `expected` without
running Python, Soufflé, Shen, or the capcov claim package.

Each fixture references `corpus/schema-v1.json`, which fixes ordered typed
relation arguments, context indices, polarity, and modality. Every claim,
fact, and assumption carries its ordered `args`, complete context-key map, and
explicit provenance dependencies.

Each per-claim expected result keeps five things separate:

* `semantic_verdict`: `supported`, `refuted`, `unresolved`, or `conflicting`;
* `operational_status`: one of the canonical claims statuses (`complete`,
  `invalid-input`, `inconsistent-premises`, `resource-exhausted`,
  `unsupported-construct`, `stale`, or `out-of-scope`);
* `support_leaves` and `refutation_leaves`: polarity-specific derivation IDs;
* `observed_leaves` and `forbidden_leaves`: all observed inputs versus IDs a
  derivation must not use;
* `discrepancies` and `missing_premises`: why a tempting Boolean answer is not
  sufficient.

`expected.json` is a review table duplicated from each fixture.  The tests
only validate that the table and fixtures agree; they do not evaluate rules.
The fixtures are synthetic controls until a real retained execution is added.
`adapter.py` converts each fixture to strict bundle JSON and invokes the real
`bundle_from_json(..., validate=True)` parser; it does not evaluate rules.

## Model well-formedness: no certificate until the checker exists

`op_qualified_rt` carries the positive premise `model_well_formed(M, Checker,
Version, Cert)` under the reviewer's `model_checker_admitted(Checker,
Version)`.  The Stage D typed checker that would emit such a certificate **has
not been built**, so no *real* receipt carries one:

* receipts produced by replaying a real system have **no**
  `model_well_formed.json`, and their `model_checkers.json` admits **no**
  checker (an empty `rows` list: the reviewer has been asked and admits nothing
  yet).  A placeholder certificate there would be a fabricated observation
  satisfying the very gate the premise exists to impose, and the judge would
  report a qualification that nothing certified.  Such receipts stay with the
  system that produced them; none is committed here.
* `replay_receipt_min` is the **synthetic** fixture the adversarial corpus is
  generated from (`replay_rules/cases.py`; run `run-fixture-1`, commits
  `php0000…`/`go0000…`).  Every fact in it is made up, including its
  certificate (`sha256("pending: checker not yet built")` under checker
  `stage-d-typecheck` version `0.1-pending`) and the reviewer list that admits
  that pair.  That is honest because the corpus is labelled synthetic
  throughout, and it is what keeps the *positive* control of the premise
  (corpus case `00`) exercised: a typed checker, named
  by class `modelcheck`, certifying the same model `model_describes_run` binds
  to the run, at a version the reviewer admits.

The consequence for the real receipts is that `op_qualified` is **unresolved
with `model_well_formed` as its missing premise**, and the join reports
`model_well_formed: "missing"`.  That state is not the same as a finding
against the port, so it is reported apart from one:

* `replay.join.PENDING_PREMISES` names the premises nothing can satisfy yet,
  and they sit **last** in `_BLOCKING_ORDER` — an op reported as pending is one
  where every other premise was checked and held.
* the per-op summary (and `judge.json`) carries
  `qualification: "pending model_well_formed"`, distinct from `"qualified"` and
  from `"unsupported"`;
* `scripts/compiled_checker.py` exits **5** (`EXIT_PENDING_PREMISE`) rather than
  1 when every op the verdict turns on is pending, so a consumer gate can tell
  "the checker does not exist yet" from "this port is not qualified".  Exit 1
  still wins whenever any op has a real blocker, such as `undeclared_any`.

When Stage D emits a real certificate, drop it into `model_well_formed.json`,
add its `(checker, checker_version)` to `model_checkers.json`, and flip these
expectations back; nothing in the pack or the exporter has to change.

## Assumption registry and invalidation

`capcov.claims.assumptions` gives each reviewed assumption a run-independent
`asm:` id (producer class + relation + row, so one reviewed row keeps one id
across runs), registers what each assumption carries, and withdraws one —
dropping an assumption and re-evaluating both kernels to see which claims lose
support. `test_claims_assumptions_cli.py` drives it over `replay_receipt_min`
from the command line:

```
capcov experiment claims assumptions registry   --receipt DIR [--out DIR]
capcov experiment claims assumptions invalidate --receipt DIR --drop ID
```

`--receipt` falls back to `CAPCOV_REPLAY_RECEIPT_DIR`; with neither the command
refuses. Exit 0 when the documents were produced, 2 for a refusal (an unknown id, or a
withdrawal that would refute a claim rather than leave a premise missing), 3
when the kernels disagree. Needs Soufflé, no replay environment.
