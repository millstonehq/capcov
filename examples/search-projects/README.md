# Agent-facing DreamCoder search examples

These small projects exercise the search-project interface without requiring a
repository, HTTP server, or private fixtures. They are intentionally synthetic.

Run them from the Capcov checkout with the package environment active:

```sh
capcov reason run --operation building-capacity --project examples/search-projects/building.json --json
capcov reason next --operation building-capacity --project examples/search-projects/building.json --json
capcov reason run --operation seat-allocation --project examples/search-projects/seat.json --json
capcov reason run --operation invention-demo --project examples/search-projects/invention.json --json
```

For an operation with a search contract and no pending intent decision,
`run` first solves the sibling tasks, proposes a shared library abstraction,
then fits the target's complete known contract and searches reachable inputs
for a second fitting program with an opposite prediction. `next` is the
orchestrator-facing read operation. It reports a witness, an unresolved intent
decision, or the next replay action. The result includes the library revision,
search bounds, candidate programs, and the stopping reason.

The building case deliberately has no accepted intent decision. Its two
implementations disagree on the reachable `nbsp` input, so the next action is
an intent decision. The seat case has a confirmed owner decision and no Go
observation. It therefore emits a binding contract and asks the orchestrator
to replay Go. The invention example has no intent packet and demonstrates the
library-learning path alone. Its sibling truth tables all implement
`guard OR (left AND right)`. The run invents a reusable three-argument
definition and solves the target with a `call` to that definition. The
baseline remains the expanded expression, so the receipt makes the learned
production visible.

Known examples are never withheld from the target search. Reachable inputs are
used only to look for disagreements and are not silently promoted to expected
answers.
