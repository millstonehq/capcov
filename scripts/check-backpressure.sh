#!/bin/sh
# Exercise capcov's own acceptance command behind an actual host advancement
# boundary. All faults live in disposable copies, never in the working source.
set -eu

package_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
python_cmd=${CAPCOV_PYTHON:-python}
"$python_cmd" -m pytest --version >/dev/null
scratch=$(mktemp -d "${TMPDIR:-/tmp}/capcov-backpressure.XXXXXX")
cleanup() { rm -rf "$scratch"; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

for mode in baseline missing-check broken-gate; do
    candidate="$scratch/$mode"
    mkdir -p "$candidate"
    cp -R "$package_root/src" "$package_root/tests" "$candidate/"
    cp "$package_root/capcov.toml" "$package_root/capcov.outcomes.json" \
       "$package_root/pyproject.toml" "$package_root/uv.lock" "$candidate/"
    if [ "$mode" = broken-gate ]; then
        patch -s -F 0 -p1 -d "$candidate" < \
            "$package_root/tests/fixtures/backpressure/ignore-required-flows.patch"
    fi
    (
        cd "$candidate"
        export PYTHONPATH="$candidate/src"
        # The host selects the interpreter and test set. Clear inherited options
        # except for the deliberate missing-check experiment below.
        unset PYTEST_ADDOPTS PYTEST_PLUGINS CAPCOV_OBSERVE CAPCOV_ONLY CAPCOV_FLOW_ONLY
        "$python_cmd" -m capcov discover --target . --out inventory.json >/dev/null
        if [ "$mode" = missing-check ]; then
            export PYTEST_ADDOPTS='-k test_pass_on_same_route_cannot_hide_required_failure'
        fi
        status=0
        if "$python_cmd" -m capcov outcomes check capcov.outcomes.json \
            --inventory inventory.json --target . --python "$python_cmd" \
            --timeout 120 --out run.json > check.log 2>&1; then
            touch advanced
        else
            status=$?
        fi
        case "$mode" in
            baseline)
                if [ "$status" -ne 0 ] || [ ! -f advanced ]; then
                    cat check.log
                    echo 'FAIL baseline did not advance' >&2
                    exit 1
                fi
                echo 'PASS baseline: required checks passed; host advanced'
                ;;
            missing-check|broken-gate)
                expected='^missing +capcov.outcomes.required-set:'
                if [ "$mode" = broken-gate ]; then
                    expected='^failed +capcov.browser.required-scenarios:'
                fi
                if [ "$status" -ne 1 ] || [ -f advanced ] || ! grep -Eq "$expected" check.log; then
                    cat check.log
                    echo "FAIL $mode did not block for the intended reason" >&2
                    exit 1
                fi
                grep -E "$expected" check.log
                echo "PASS $mode: specific required check blocked host advancement"
                ;;
        esac
    )
done
cleanup
trap - EXIT
test ! -e "$scratch"
echo 'PASS cleanup: all owned candidate copies and evidence removed'
