# Scoped outcome coverage with pytest

## Test capcov's own advancement gate

From the repository root, run:

```sh
uv run --frozen --with pytest sh scripts/check-backpressure.sh
```

The checked-in `capcov.toml` and `capcov.outcomes.json` bind a small set of capcov's
own acceptance behaviors to exact tests. The script discovers the actual source
and runs `outcomes check` in disposable copies. Its host creates an `advanced`
marker only on exit zero. It verifies three executions: a passing baseline, an
omitted required check, and a source mutant that suppresses required browser-flow
failures. The negative cases must return 1 and name the intended missing/failed
outcome; an unrelated error is not a successful fault control. The script verifies
cleanup and fails if any expectation is unmet. The existing package CI job runs
this same command before building release artifacts.

This establishes local host enforcement for these capcov obligations. It does not
qualify every capcov feature, prove resistance to an agent rewriting its protected
tests/host, or measure autonomous repair/convergence. Ordinary runs retain no JSON
receipts in the repository; CI retains the terminal command output.

## Bind consumer outcomes

Use `capcov outcomes` to keep business outcomes separate from entity reachability.
The consumer owns a JSON map of stable capability/outcome IDs to **exact pytest
node IDs**, including parameter IDs. Every mapped case must pass setup, call, and
teardown; unrelated passing cases cannot satisfy an outcome. Skips and expected
failures are inconclusive. An unresolved product rule remains unresolved even
when its characterization test passes.

This initial integration supports pytest directly. It does not define a general
harness API or require pytest in the core engine's environment. The target Python
interpreter must have pytest and the application's test dependencies installed.

```json
{
  "version": 1,
  "scope": "orders/cancellation/local-api",
  "environment": {"database": "temporary SQLite", "client": "API test client"},
  "limitations": ["No production database concurrency or external delivery evidence"],
  "inputs": ["src", "tests", "pyproject.toml", "uv.lock"],
  "outcomes": [{
    "id": "cancel.ownership",
    "capability": "orders.cancel",
    "description": "Another account cannot cancel or mutate this order",
    "source_refs": ["POST /orders/{order_id}/cancel"],
    "policy": "required",
    "tests": ["tests/test_cancel.py::test_other_account"]
  }]
}
```

`source_refs` must exist in the supplied discovery inventory. This is an authored
semantic mapping: the engine checks identities and execution results, not whether
the assertion correctly expresses the business requirement. Review test meaning
and fixture limitations with the map. An outcome can have an empty `tests` list;
it will remain missing. Use `policy: "unresolved"` with a `reason` for pending
product decisions. There are no exemptions which turn such outcomes into passes.

Run from the consumer project:

For an advancement gate, use the single fresh execution-and-check command:

```sh
capcov outcomes check capcov.outcomes.json --inventory .capcov/inventory.json \
  --python .venv/bin/python --timeout 180 --out .capcov/outcomes-run.json
```

First generate the inventory with `capcov discover` as below. `check` always runs
the union of mapped test IDs, accepts no selection arguments, and returns zero
only when **every authored outcome is demonstrated** with a clean session. Missing
bindings, skips, unresolved outcomes, failures, and timeouts prevent acceptance.
Even an inherited pytest filter that runs a passing subset cannot discharge the
missing cases. The output is the usual run receipt, suitable for `coverage`.
Invalid/stale inputs return 2; valid but unqualified execution returns 1.

Have the existing CI/agent host advance only on exit zero. A host that ignores the
exit code is not enforcing this gate. Keep the accepted map, checker, dependencies,
and invocation under the host's existing trusted revision/review controls; a
candidate that can rewrite those controls can bypass them. This command prevents
accidental old-receipt reuse by executing afresh; it does not sandbox hostile tests
or authenticate their external observations. It has one bounded execution, not
an autonomous retry loop. The caller must bound any retries across invocations.

For diagnostic selection or separate report generation, the existing commands remain:

```sh
capcov discover --target . --out .capcov/inventory.json
capcov outcomes run capcov.outcomes.json --inventory .capcov/inventory.json \
  --python .venv/bin/python --out .capcov/outcomes-run.json
capcov outcomes coverage capcov.outcomes.json --inventory .capcov/inventory.json \
  --run .capcov/outcomes-run.json --out .capcov/outcomes-coverage.json
capcov outcomes gate capcov.outcomes.json --inventory .capcov/inventory.json \
  --run .capcov/outcomes-run.json
```

A failed pytest run still writes evidence for reporting; arrange CI to run coverage
and gate after that failure. `run` returns 1 for unsuccessful/incomplete execution;
`coverage` returns 0 when a valid report is produced, even with gaps; `gate` returns
1 unless all scoped outcomes are demonstrated and the session is clean. Invalid
or stale evidence returns 2. Commands after `--` on `run` are pytest selections or
options; otherwise it runs the union of mapped node IDs.

`run` success is execution success, not acceptance: a passing selection can still
leave required outcomes missing. Use `check`, or explicitly invoke `gate` after
`run`, before advancing. Printed success messages from tests do not override the
pytest setup/call/teardown results collected by the probe.

The browser path now also preserves required scenario failures through
`observe --probe browser` → `reconcile` → `gate`. A passing scenario on a shared
route cannot hide a failed/missing scenario. Structural exemptions cannot waive
these failures. Duplicate scenario IDs and nonzero runner exits are rejected;
empty, blocked, assertionless, diagnostic, and `--only` executions cannot qualify
the full plan. Author a separate bounded model to qualify a smaller capability.
Older browser observation files without required-flow results must be regenerated.
Browser runners remain trusted assertion observers; this is not proof that an
arbitrary runner truthfully reports SQL, SMTP, or browser effects.

The report distinguishes `demonstrated`, `failed`, `missing`, `unresolved`, and
`inconclusive`. It is complete only **within the authored scope and environment**;
it does not close unmapped source obligations, discovery blind spots, or the
separate entity/flow gates. A missing test file can cause pytest collection to
abort; all outcomes without executed evidence then remain missing.

Runs use a private result path and fresh nonce. Source inventory, map, declared
input files (including mapped tests), and engine code are hashed; coverage/gate
recompute them against the current checkout. Include fixtures, test configuration,
dependency locks, and other assertion inputs in `inputs`. These checks prevent
accidental stale evidence reuse; the runner/test code is trusted, and this is not
cryptographic attestation of an external service or proof of the deployed build.
Execution/reset/cleanup remain the consumer fixture's responsibility. A timeout
fails the run; consumers must own cleanup for subprocesses their fixtures start.


A download can itself be an observable assertion against a reviewed expected file:

```json
{"op": "assert", "mode": "download", "id": "export-bytes", "selector": "a.export", "file": "fixtures/expected.csv"}
```

The expected file uses a canonical repository-relative POSIX path, with the same
confinement and enrolled-byte requirements as upload fixtures. The consumer must
perform the selected download, require successful completion and compare its
complete bytes with the checked expected bytes before emitting the assertion ID.
A link, filename, prefix match, or successful HTTP response alone is insufficient.
`text` and `refresh_timeout_ms` are invalid for this mode; download actions are
never automatically retried. The consumer owns origin restrictions and bounded
transfer/size handling, and must reject the mode if it cannot honor that contract.
Planning neither reads the expected file nor downloads anything.


A journey can retain a runtime-created resource path and revisit it after an
identity change:

```json
{"op": "remember-path", "name": "invoice"}
{"op": "visit-path", "name": "invoice", "expect_status": 404}
```

Names match `[a-z][a-z0-9_-]{0,63}`. `visit-path` requires an integer expected HTTP
status from 200 through 599. Both are actions; a binding still needs a separate
observable assertion. They accept no alternate path, selector or value.

Consumers must scope saved paths to one scenario, reject missing names and
redefinitions, retain only a reviewed same-application path (no credentials, query
or fragment), and revisit it with a bounded GET. The final URL must remain the
saved resource and the final response must match `expect_status`; a login redirect
is not a resource-access refusal. Invalid captures and unresolved names fail,
without a guessed/default path. Models must describe the capture prerequisites
and identity changes. Planning does not invent a resource URL or perform requests.


A reviewed direct HTTP response can be an assertion, including a denied mutation:

```json
{"op":"assert","mode":"http","id":"price-refused","method":"POST","path":"/items/1/base","form":{"price":"4.99"},"expect_status":403,"text":"not permitted"}
```

This initial contract supports GET and form-encoded POST on literal application
paths composed of letters, digits, underscores, hyphens and single slashes. It
excludes queries, fragments, encoded paths, alternate origins, arbitrary headers
and raw bodies. GET has no form. Form keys/values are strings with bounded sizes.
Expected statuses are 200–299 or 400–599; response text must be nonempty.

Consumers must use the scenario's current authenticated session, confine the
request to a reviewed application endpoint, send it once, reject redirects, and
bound both transfer time and complete response bytes. They must verify exact
response URL/status and the declared body text before recording assertion
evidence, and record the actual request for route reconciliation. A DOM assertion
elsewhere is not response evidence. No retries or refresh are permitted, including
for writes. The planner performs no requests. An expected denial alone does not
prove absence of side effects; model follow-up state checks where required.


Planning defaults to 10,000 reachable fact states. For larger reviewed models,
use `capcov flows plan model.json --target local --max-states 20000 --out plan.json`.
The positive integer limit bounds exploration; exhaustion still fails without
emitting a complete plan. Non-default limits are recorded in the plan and used
when coverage independently re-derives its scenarios. Raising this budget adds
no coverage and does not waive unreachable targets or missing evidence. Default
plans retain their existing artifact shape.


A click may declare a native browser confirmation explicitly:

```json
{"op":"click","selector":"button.delete","confirmation":{"message":"Delete this sample?","action":"dismiss"}}
```

The contract requires an exact nonempty message of at most 1024 characters and
an `accept` or `dismiss` action. It is valid only on `click`; extra confirmation
keys are rejected. The declaration is preserved in the plan and bound to run
evidence. It does not itself grant coverage or replace subsequent outcome
assertions. A browser consumer must wait for a native `confirm` dialog caused by
that click, match the message exactly, apply the declared action, and fail on a
missing or mismatched dialog within a bounded deadline. It must not silently
accept prompts, alerts or arbitrary confirmation messages. Planning does not
open or answer a browser dialog.


Run evidence may declare `execution_scope: "diagnostic"` when a consumer selects
only one journey for debugging. Such a run can have `status: "passed"`, meaning
the selected execution succeeded, but reconciliation awards **zero coverage**,
does not resolve runtime-mount boundaries, and remains incomplete. This applies
even if the diagnostic happens to execute every planned scenario. Missing scope
defaults to `"full"` for existing runners; an explicit scope must be `"full"` or
`"diagnostic"`, otherwise reconciliation rejects it. Consumers must preserve the
full plan and its digest when selecting diagnostic scenarios and retain all steps
of a selected journey. This flag neither selects scenarios nor supplies their
prerequisites; selection remains a consumer runner responsibility. Plan, source
identity, assertion and request-evidence checks still apply.

An element-relative mouse drag is explicit as well:

```json
{"op":"drag","selector":"#drawing","from":[0.1,0.2],"to":[0.8,0.7]}
```

Coordinates are numeric x/y fractions of the rendered element bounds, from zero
to one. Both pairs are required and must differ; booleans, non-finite values and
out-of-bounds coordinates are rejected. Consumers must resolve the element,
bring it into view, verify that both endpoints can be hit within it, and perform
an actual mouse press/move/release. They must release the button on failure and
must not replace the gesture with direct application-state mutation. Subsequent
assertions still establish the outcome; declaring a drag grants no coverage.
