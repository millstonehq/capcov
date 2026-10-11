"""Exact pytest node IDs with setup/call/teardown and expected-failure handling."""

import json
import os
import sys
from pathlib import Path

_reports = {}
_errors = []


def pytest_configure(config):
    _reports.clear()
    _errors.clear()


def pytest_runtest_logreport(report):
    phases = _reports.setdefault(report.nodeid, {})
    if report.when in phases:
        _errors.append(f"duplicate test phase: {report.nodeid}/{report.when}")
    phases[report.when] = (
        "inconclusive" if hasattr(report, "wasxfail") else report.outcome
    )


def pytest_collectreport(report):
    if report.failed:
        _errors.append(f"collection failed: {report.nodeid}")


def pytest_sessionfinish(session, exitstatus):
    path = os.environ.get("CAPCOV_OUTCOME_OUT")
    if not path:
        return
    tests = {}
    for nodeid, phases in _reports.items():
        tests[nodeid] = (
            "failed"
            if "failed" in phases.values()
            else "passed"
            if phases == {"setup": "passed", "call": "passed", "teardown": "passed"}
            else "inconclusive"
        )
    Path(path).write_text(
        json.dumps(
            {
                "nonce": os.environ["CAPCOV_OUTCOME_NONCE"],
                "python_version": sys.version,
                "finished": True,
                "tests": tests,
                "errors": _errors,
            }
        )
    )
