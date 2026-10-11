#!/bin/sh
# Run the fault-notes example in disposable copies: the baseline must pass under
# every crash point, every fault, bounded schedules and Hypothesis; each mutant,
# made by patching a copy, must be rejected by name. A mutant never exists in
# the working source. Hypothesis is required: run with `--extra faults-property`.
#
# resend-after-unknown: recovery sends without reading the change log. Rejected
#   as effect_applied_twice.
# judge-before-settle: the scenario judges a run while the fake service still
#   holds a delayed write. Left unresolved for want of applied_closed.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
EXAMPLE="$ROOT/examples/fault-notes"
PATCH="$ROOT/tests/fixtures/faults/resend-after-unknown.patch"
PENDING_PATCH="$ROOT/tests/fixtures/faults/judge-before-settle.patch"
PYTHON=${CAPCOV_PYTHON:-python}
ENGINE=${CAPCOV_FAULTS_ENGINE:-python}
WORK=$(mktemp -d "${TMPDIR:-/tmp}/capcov-fault-harness.XXXXXX")
trap 'rm -rf "$WORK"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1

if ! "$PYTHON" -c 'import hypothesis' 2>/dev/null; then
  echo 'FAIL hypothesis is not installed; run with: uv run --frozen --extra faults-property' >&2
  exit 1
fi

cp -R "$EXAMPLE" "$WORK/baseline"
if ! (cd "$WORK/baseline" && "$PYTHON" scenario.py --engine "$ENGINE" --dump "$WORK/baseline-out") \
    > "$WORK/baseline.log" 2>&1; then
  cat "$WORK/baseline.log"
  echo 'FAIL baseline did not pass' >&2
  exit 1
fi
grep -q '^all invariants supported$' "$WORK/baseline.log"
"$PYTHON" -m capcov faults check --engine "$ENGINE" \
  --history "$WORK/baseline-out/history.json" --finals "$WORK/baseline-out/finals.json" \
  > "$WORK/baseline-check.log"
echo 'PASS baseline: every invariant supported under every crash point, fault and schedule'

cp -R "$EXAMPLE" "$WORK/mutant"
patch -s -F 0 -p1 -d "$WORK/mutant" < "$PATCH"
status=0
(cd "$WORK/mutant" && "$PYTHON" scenario.py --engine "$ENGINE" --dump "$WORK/mutant-out") \
  > "$WORK/mutant.log" 2>&1 || status=$?
if [ "$status" -ne 1 ] || ! grep -q '^FAIL effect_applied_twice violated ' "$WORK/mutant.log"; then
  cat "$WORK/mutant.log"
  echo "FAIL mutant was not rejected for effect_applied_twice (exit $status)" >&2
  exit 1
fi
for finder in 'schedule=' 'property'; do
  if ! grep -q "^FAIL .*effect_applied_twice.*\[$finder" "$WORK/mutant.log"; then
    cat "$WORK/mutant.log"
    echo "FAIL the $finder search did not find the mutant" >&2
    exit 1
  fi
done
status=0
"$PYTHON" -m capcov faults check --engine "$ENGINE" \
  --history "$WORK/mutant-out/history.json" --finals "$WORK/mutant-out/finals.json" \
  > "$WORK/mutant-check.log" 2>&1 || status=$?
if [ "$status" -ne 1 ] || ! grep -Eq '^violated +effect_applied_twice ' "$WORK/mutant-check.log"; then
  cat "$WORK/mutant-check.log"
  echo "FAIL capcov faults check did not name effect_applied_twice (exit $status)" >&2
  exit 1
fi
grep -E '^violated +effect_applied_twice ' "$WORK/mutant-check.log"
echo 'PASS mutant: resend after an unknown outcome rejected as effect_applied_twice'

cp -R "$EXAMPLE" "$WORK/pending"
patch -s -F 0 -p1 -d "$WORK/pending" < "$PENDING_PATCH"
status=0
(cd "$WORK/pending" && "$PYTHON" scenario.py --engine "$ENGINE" --depth 1 --examples 1) \
  > "$WORK/pending.log" 2>&1 || status=$?
if [ "$status" -ne 1 ] || ! grep -Eq \
    "^FAIL silent_overwrite unresolved \[fault=delay-apply crash=none\] .*applied_closed" \
    "$WORK/pending.log"; then
  cat "$WORK/pending.log"
  echo "FAIL a run judged with a write pending was not left unresolved (exit $status)" >&2
  exit 1
fi
grep -E '^FAIL silent_overwrite unresolved \[fault=delay-apply crash=none\] ' "$WORK/pending.log"
echo 'PASS mutant: a run judged with a write pending is unresolved, naming applied_closed'
