#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
FIXTURE="$ROOT/examples/go-save-read"
WORK=$(mktemp -d "${TMPDIR:-/tmp}/capcov-real-flow.XXXXXX")
trap 'rm -rf "$WORK"' EXIT INT TERM

run_pipeline() {
  target=$1
  out=$2
  uv run --frozen capcov discover --target "$target" --out "$out/capabilities.json" --quiet
  CAPCOV_FIXTURE_ROOT="$target" uv run --frozen capcov observe --target "$target" --probe browser --out "$out/observed.json" -- go run "$target/runner/main.go"
  uv run --frozen capcov reconcile "$out/capabilities.json" "$out/observed.json" --out "$out/coverage.json" --quiet
  uv run --frozen capcov gate "$out/coverage.json" --exemptions "$target/exemptions.toml"
}

cp -R "$FIXTURE" "$WORK/baseline"
mkdir "$WORK/baseline-out"
run_pipeline "$WORK/baseline" "$WORK/baseline-out"

cp -R "$FIXTURE" "$WORK/mutant"
mkdir "$WORK/mutant-out"
sed 's/notes = append(notes, r.FormValue("title"))/_ = r.FormValue("title")/' "$WORK/mutant/main.go" > "$WORK/mutant/main.go.new"
mv "$WORK/mutant/main.go.new" "$WORK/mutant/main.go"
if run_pipeline "$WORK/mutant" "$WORK/mutant-out" >"$WORK/mutant.log" 2>&1; then
  echo "mutant unexpectedly passed" >&2
  exit 1
fi
grep -q 'required-flow-failed: read-saved-note' "$WORK/mutant.log"
echo "capcov real flow: baseline passed; acknowledged-but-lost write mutant rejected"
