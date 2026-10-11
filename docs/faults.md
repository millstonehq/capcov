# Fault-injection harness

`capcov.faults` is test support for a program that writes to a primary store, an
object store and an external service without conditional writes or idempotency
keys. It records an operation history with `ok`, `fail` and `info` results (the
convention of Jepsen histories), injects faults through an in-memory fake
service that keeps ground truth, crashes the program before and after every side
effect, enumerates bounded schedules, and judges each run with a rule pack
evaluated by the `capcov.claims` kernels.

```sh
capcov faults check --history h.json --finals f.json [--engine python|both]
uv run --frozen --extra faults-property sh scripts/check-fault-harness.sh
```

`check` exits 0 when every invariant is supported, 1 when any is violated or
unresolved, and 2 on invalid input. The script runs the worked example in
`examples/fault-notes`, then applies two patches to temporary copies. It requires
the harness to reject a resend after an unknown outcome as `effect_applied_twice`
and to leave a run judged with a write still pending `unresolved`.

The harness uses the standard library only and is never imported by `discover`,
`reconcile` or `gate`. Hypothesis is the optional `faults-property` extra. The
API, the relations, the six invariants and the limits are in
`src/capcov/faults/README.md`.
