# Behavioral rule mining

`capcov learn` is a bounded observation miner. It consumes a JSON document
whose operations contain incumbent observations. It can identify repeated
constant-prefix/variable-suffix behavior and compare those rules against
candidate observations on a deterministic held-out split.

## Basic run

```sh
capcov learn observations.json --out behavioral-learning.json
capcov learn evaluate observations.json --holdout-mod 5 \
  --holdout-value 0 --max-results 10000 --out evaluation.json
```

The input to the basic command is an operation map:

```json
{
  "operations": {
    "lookup": {
      "observations": [
        ["!a", "a"],
        ["!b", "b"],
        ["!c", "different"]
      ]
    }
  }
}
```

The miner reports the shared prefix `!`, support `!a` and `!b`, exception
`!c`, and proposed probe `!`. Duplicate observations do not increase support.
The prefix bound is controlled by `--max-prefix` and protects the search from
unbounded input strings.

## Candidate comparison

An operation may include `candidate_observations` in the same format. The
`evaluate` command partitions operations by a stable hash into training and
holdout sets. It learns relations from training operations and reports only
holdout cells. The result has separate `rule_status` and `candidate_status`
fields:

- `supported` means the incumbent observations satisfy the mined relation.
- `exception` means a supporting family has an incumbent exception.
- `ambiguous` means the available observations do not select one relation.
- `match`, `mismatch`, or `unknown` describe candidate evidence independently.

Missing or malformed candidate observations remain `unknown`. `--max-results`
limits the serialized result rows and sets `results_truncated` when necessary.
This bounds artifact size while preserving aggregate counts.

## Evidence boundary

The miner does not run the application, infer an intended requirement, or
claim that a passing candidate comparison is correct. A proposed probe needs a
fixture and an independent oracle. A product owner or declarative specification
must resolve cases where incumbent behavior and candidate behavior differ.

The miner is one input to a larger Capcov workflow:

```text
discover capabilities -> observe incumbent -> learn candidate rules
    -> exercise proposed probes -> reconcile candidate observations
    -> accept or reject the resulting requirement
```

Keep the observation source, operation identity, and candidate revision in the
surrounding evidence artifact so a later replay can reproduce the comparison.
