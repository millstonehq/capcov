# fault-notes

A note publisher that writes to three stores, run under the `capcov.faults`
harness.

| Store | Implementation | What publishing writes |
|---|---|---|
| primary | `sqlite3` | the note row and one intent row per effect |
| objects | a dict with a create-only `put` | a snapshot of the note body |
| index | `capcov.faults.service.FakeService` | a visibility value, by unconditional replace |

## Program

`notes_app.py` publishes in four steps: record the note and both intents in one
transaction, put the snapshot, send the visibility write, settle. An unknown
outcome of the send leaves the intent pending and the operation unacknowledged.
Recovery looks for the write in the service's change log. It settles the intent
when the write is there and sends only when it is not. It also reports a write
that replaced a value the sender had not read.

Recovery must run after the service's propagation delay. The scenario models
that by calling `heal()` on the fake service before recovery.

## Run

From the repository root:

```sh
uv run --frozen --extra faults-property sh scripts/check-fault-harness.sh
```

The script copies this directory three times. The first copy must pass under
every crash point, each fault on the menu combined with every crash point, every
schedule of at most five rules, and 100 Hypothesis examples. The second copy is
patched with `tests/fixtures/faults/resend-after-unknown.patch`, which makes
recovery send without reading the change log; the script requires exit 1 and
`effect_applied_twice` from the crash-point runs, the enumerator, Hypothesis and
`capcov faults check`. The third copy is patched with
`tests/fixtures/faults/judge-before-settle.patch`, which removes the `heal()`
before recovery, so the `DelayApply` run is judged while its write is pending;
the script requires exit 1 and `unresolved` naming `applied_closed`.

To run the scenario directly:

```sh
cd examples/fault-notes
PYTHONPATH=../../src uv run --frozen --extra faults-property python scenario.py --dump /tmp/fault-notes
uv run --frozen capcov faults check --history /tmp/fault-notes/history.json --finals /tmp/fault-notes/finals.json
```

The publisher never calls `create`, so `DuplicateOnConcurrentCreate` is queued
and never fires in this example.
