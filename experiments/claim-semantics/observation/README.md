# Strict observation judge

The live Lane A command is a single-kernel observation report:

```sh
python -m capcov.claims.observation.judge \
  --receipt "$RECEIPT_DIR" \
  --candidate-root "$CANDIDATE_ROOT" \
  --incumbent-manifest "$PREPARED_RUNTIME_MANIFEST" \
  --expected-nonce "$CAPTURED_NONCE" \
  --expected-fixture-digest "$CAPTURED_FIXTURE_SHA256" \
  --invocation-record "$INVOCATION_RECORD" \
  --expected-invocation-digest "$PRE_RUN_IDENTITY_SHA256" \
  --out-dir "$JUDGMENT_DIR"
```

The invocation record and its expected digest can be omitted together. Without
them, the command reports `pending` with exit 5 and withholds a claim. A supplied
record must be a JSON `lane-a-invocation/v1` object with these fields:

* `run_nonce`, `expected_fixture_digest`, and `expected_source_digest` are
  lowercase SHA-256 strings.
* `candidate` has the observed Git `commit` and `tree` (40 lowercase hex chars).
* `incumbent` has `commit`, `runtime_commit`, `source_manifest_sha256`,
  `runtime_contract_sha256`, and `baseline_contract_sha256`.
* `fixture_setup` has `state`, `services` (`mysql`, `redis`, `mongo` booleans),
  `schema_sha256`, `seed_recipe_sha256`, positive `identity_count`, and a
  non-empty `attestation_scope`.

The judge compares the record's nonce and fixture digest with the explicit
command arguments, checks the candidate Git tree and incumbent manifest bytes,
and recomputes source identity using the record's incumbent commits. It requires
`fixture_setup.state` to be `verified` and all three service checks to be true.
That setup remains producer-reported evidence, not independent verification;
the report says so explicitly and carries only a digest of `attestation_scope`.
The record does not cryptographically authenticate its producer. The CLI does
not independently traverse or verify the incumbent runtime's complete source
closure: the external caller owns that preflight and retains its expected
identity. The report identifies the candidate checkout as observed, the
incumbent identity as externally pinned, and the manifest bytes as hash matched.

`--expected-invocation-digest` is captured and retained before provisioning.
It is SHA-256 over the compact, sorted-key UTF-8 JSON encoding (no trailing
newline, `ensure_ascii=False`) of exactly these immutable fields from the record:
`format`, `run_nonce`, `expected_fixture_digest`, `expected_source_digest`,
`candidate`, and `incumbent`. The judge recomputes that subset after the run and
compares it with the retained digest. The final full-record byte digest is
reported separately as `external_bindings.invocation_record_sha256`; it is not
a replacement for the pre-run binding.

Reviewer admissions are optional inputs, never read from inside the receipt.
To supply them, pass `--admission-record FILE --expected-admission-digest
SHA256` together. The record uses the same strict JSON structure as
`observation_admissions.json`; the external digest binds its bytes, but does not
authenticate the reviewer's identity. Its fields are `producer` (optional,
first token `reviewer`), `reviewer`, `reviewed_at`, `reviewed_against` with
`policy`, `scenario_set`, and `check` digests/identifier, and `rows`. Rows use
`policy_admitted` with a `version`, `scenario_set_admitted` with a `version`, or
`masked_difference_admitted` with `normalization` and `field_path`; an optional
non-empty `note` is discarded. With no external record, the policy or
scenario-set admission premise remains unresolved.

The receipt directory must contain `receipt.json` and its exact `log.txt`.
Missing or unverifiable log bytes refuse the export. Output is bounded to 4 MiB
for `report.json` and 8 MiB total with any rechecked certificates. The output
directory must be empty and cannot overlap the receipt or candidate worktree.
Local paths and low-level exception details are redacted from the report.

`judge_status` and the process status keep semantic and operational outcomes
separate:

| Status | Exit | Meaning |
|---|---:|---|
| `supported` | 0 | The admitted scenarios agree under the admitted comparison policy. |
| `unsupported` | 1 | The report contains a concrete refuted difference. |
| `pending` | 5 | A required observation, source binding, fixture setup, or reviewer admission is unresolved. |
| `invalid-input` or `operational-failure` | 3 | Receipt, identity, resource, or Python execution could not be judged. |
| usage error | 2 | Required command arguments are malformed or incomplete. |

The JSON always names `kernels: ["python"]`; it never claims a differential or
kernel mismatch. `kernel_execution` says whether Python ran. `observations_agree` is bounded by its scenario and policy
digests. It does not establish correctness or capability-wide parity. The
fixture bypass used by golden tests is not exposed by this command.
