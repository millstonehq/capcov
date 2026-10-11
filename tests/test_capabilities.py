"""The three capability sets and their reconciliation, one adapter at a time."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from capcov.capabilities import exercised, ids, implemented, reconcile, spec
from capcov.capabilities.cli import main as cli_main

SPEC = """\
# sessions Specification

## Requirements
### Requirement: Open a session
The system SHALL open sessions.

#### Scenario: Operator opens a session
- **WHEN** an operator asks
- **THEN** a session exists

#### Scenario: Open fails when the quota is spent
- **WHEN** the quota is spent
- **THEN** the request is refused

```
#### Scenario: Not a scenario, inside a fence
```

### Requirement: Close a session
The system SHALL close sessions.
"""


def write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text) if not text.startswith("#") else text, encoding="utf-8")
    return path


class IdsTest(unittest.TestCase):
    def test_slug(self) -> None:
        self.assertEqual(ids.slug("Operator opens a session!"), "operator-opens-a-session")
        self.assertEqual(ids.slug("api.example.com:443"), "api-example-com-443")

    def test_slug_refuses_a_value_with_nothing_to_keep(self) -> None:
        with self.assertRaises(ids.IdError):
            ids.slug("???")

    def test_capability_id(self) -> None:
        self.assertEqual(ids.capability_id("sessions", "Open a session", "Quota spent"),
                         "sessions/open-a-session/quota-spent")
        self.assertEqual(ids.capability_id("sessions", "Open a session"), "sessions/open-a-session")

    def test_covers_is_a_segment_prefix(self) -> None:
        self.assertTrue(ids.covers("a/b", "a/b/c"))
        self.assertTrue(ids.covers("a/b/c", "a/b/c"))
        self.assertFalse(ids.covers("a/b", "a/bc/c"))
        self.assertFalse(ids.covers("a/b/c", "a/b"))

    def test_fill_slugs_fields_and_keeps_literals(self) -> None:
        self.assertEqual(ids.fill("attend/{action}/{target}", {"action": "Click", "target": "Save Button"}),
                         "attend/click/save-button")

    def test_fill_names_a_missing_field(self) -> None:
        with self.assertRaises(KeyError):
            ids.fill("a/{x}", {})

    def test_fill_refuses_a_result_that_is_not_an_id(self) -> None:
        with self.assertRaises(ids.IdError):
            ids.fill("A/B/C/D", {})


class SpecAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_reads_requirements_and_scenarios_as_ids(self) -> None:
        write(self.root, "specs/sessions/spec.md", SPEC)
        doc = spec.read_openspec(self.root / "specs")
        self.assertEqual(
            [c["id"] for c in doc["capabilities"]],
            [
                "sessions/close-a-session",
                "sessions/open-a-session/open-fails-when-the-quota-is-spent",
                "sessions/open-a-session/operator-opens-a-session",
            ],
        )
        self.assertEqual(doc["unresolved"], [])

    def test_a_heading_in_a_fence_is_not_a_scenario(self) -> None:
        write(self.root, "specs/sessions/spec.md", SPEC)
        ids_ = {c["id"] for c in spec.read_openspec(self.root / "specs")["capabilities"]}
        self.assertFalse(any("not-a-scenario" in i for i in ids_))

    def test_area_is_the_directory_and_nested_directories_join(self) -> None:
        write(self.root, "specs/billing/invoices/spec.md",
              "### Requirement: Issue\n#### Scenario: Monthly\n")
        doc = spec.read_openspec(self.root / "specs")
        self.assertEqual(doc["capabilities"][0]["id"], "billing-invoices/issue/monthly")

    def test_records_where_each_capability_is_declared(self) -> None:
        write(self.root, "specs/sessions/spec.md", SPEC)
        found = {c["id"]: c for c in spec.read_openspec(self.root / "specs", display_root="specs")["capabilities"]}
        row = found["sessions/open-a-session/operator-opens-a-session"]
        self.assertEqual(row["file"], "specs/sessions/spec.md")
        self.assertEqual(row["line"], 7)

    def test_names_what_it_cannot_read(self) -> None:
        write(self.root, "specs/a/spec.md",
              "#### Scenario: Orphan\n### Requirement: R\n#### Scenario: Same\n#### Scenario: Same\n"
              "### Requirement: !!!\n")
        doc = spec.read_openspec(self.root / "specs")
        kinds = sorted(u["kind"] for u in doc["unresolved"])
        self.assertEqual(kinds, ["bad-title", "duplicate-id", "orphan-scenario"])
        self.assertEqual([c["id"] for c in doc["capabilities"]], ["a/r/same"])

    def test_an_empty_tree_is_unresolved_not_green(self) -> None:
        (self.root / "specs").mkdir()
        doc = spec.read_openspec(self.root / "specs")
        self.assertEqual([u["kind"] for u in doc["unresolved"]], ["no-capabilities"])

    def test_a_missing_root_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            spec.read_openspec(self.root / "absent")


SURFACES = [{"id": "POST /v1/sessions"}, {"id": "DELETE /v1/sessions/{id}"}, {"id": "GET /healthz"}]


class ImplementedAdapterTest(unittest.TestCase):
    def test_glob_maps_a_surface_to_a_capability(self) -> None:
        doc = implemented.read(
            SURFACES, [{"surface": "POST /v1/sessions", "capability": "sessions/open-a-session"}]
        )
        self.assertEqual(doc["capabilities"],
                         [{"id": "sessions/open-a-session", "surfaces": ["POST /v1/sessions"]}])

    def test_pattern_fills_a_template_from_named_groups(self) -> None:
        doc = implemented.read(
            SURFACES, [{"pattern": r"^(?P<verb>[A-Z]+) /v1/(?P<area>[a-z]+)", "capability": "{area}/{verb}"}]
        )
        self.assertEqual([c["id"] for c in doc["capabilities"]], ["sessions/delete", "sessions/post"])

    def test_surfaces_no_entry_maps_are_listed(self) -> None:
        doc = implemented.read(
            SURFACES, [{"surface": "POST /v1/sessions", "capability": "sessions/open-a-session"}]
        )
        self.assertEqual(doc["unmapped_surfaces"], ["DELETE /v1/sessions/{id}", "GET /healthz"])

    def test_an_entry_that_matches_nothing_is_unresolved(self) -> None:
        doc = implemented.read(SURFACES, [{"surface": "PUT /nope", "capability": "a/b"}])
        self.assertEqual([u["kind"] for u in doc["unresolved"]], ["unmatched-entry"])

    def test_a_capability_that_is_not_an_id_is_unresolved(self) -> None:
        doc = implemented.read(SURFACES, [{"surface": "GET /healthz", "capability": "Not An ID"}])
        self.assertEqual([u["kind"] for u in doc["unresolved"]], ["bad-capability"])
        self.assertEqual(doc["capabilities"], [])

    def test_exactly_one_selector(self) -> None:
        with self.assertRaises(ValueError):
            implemented.read(SURFACES, [{"capability": "a/b"}])
        with self.assertRaises(ValueError):
            implemented.read(SURFACES, [{"surface": "x", "pattern": "y", "capability": "a/b"}])

    def test_plugin_reads_the_surfaces_itself(self) -> None:
        doc = implemented.read(
            SURFACES, [{"plugin": "tests.capability_plugins:map_health"}]
        )
        self.assertEqual(doc["capabilities"], [{"id": "ops/health", "surfaces": ["GET /healthz"]}])
        self.assertEqual(len(doc["unmapped_surfaces"]), 2)


AUDIT = (
    "2026-10-04T10:00:00Z axon-audit action=open target=session-1 result=ok\n"
    "2026-10-04T10:00:01Z axon-audit action=open target=session-2 result=error reason=quota\n"
    "2026-10-04T10:00:02Z unrelated line action=open result=error\n"
    "2026-10-04T10:00:03Z axon-audit action=close target=session-1 result=denied\n"
)
AUDIT_SOURCE = {
    "name": "audit",
    "glob": "logs/*.log",
    "format": "logfmt",
    "match": {"contains": "axon-audit"},
    "capability": "attend/{action}",
    "pass_when": [{"field": "result", "op": "eq", "value": "ok"}],
    "fail_when": [{"field": "result", "op": "eq", "value": "error"}],
}
ENVOY = (
    '{"host":"api.example.com","status":200,"response_flags":"-"}\n'
    '{"host":"api.example.com","status":503,"response_flags":"UF"}\n'
    '{"host":"api.example.com","status":200,"response_flags":"DC"}\n'
    '{"host":"other.example.com","status":200,"response_flags":"-"}\n'
    "not json at all\n"
)
ENVOY_SOURCE = {
    "name": "envoy",
    "glob": "logs/*.json",
    "format": "json",
    "where": [{"field": "host", "op": "eq", "value": "api.example.com"}],
    "capability": "edge/{host}",
    "fail_when": [
        {"field": "status", "op": "gte", "value": 500},
        {"field": "response_flags", "op": "ne", "value": "-"},
    ],
}


class ExercisedAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def by_id(self, doc: dict) -> dict:
        return {c["id"]: c for c in doc["capabilities"]}

    def test_logfmt_reports_pass_and_fail_per_capability(self) -> None:
        write(self.root, "logs/a.log", AUDIT)
        doc = exercised.read([AUDIT_SOURCE], self.root)
        found = self.by_id(doc)
        self.assertEqual((found["attend/open"]["pass"], found["attend/open"]["fail"]), (1, 1))
        self.assertIn("quota", found["attend/open"]["fail_samples"][0]["line"])

    def test_lines_the_match_does_not_select_are_ignored(self) -> None:
        write(self.root, "logs/a.log", AUDIT)
        doc = exercised.read([AUDIT_SOURCE], self.root)
        self.assertEqual(doc["sources"]["audit"]["selected"], 3)
        self.assertEqual(doc["sources"]["audit"]["lines"], 4)

    def test_a_selected_line_no_rule_classifies_is_unattributed(self) -> None:
        write(self.root, "logs/a.log", AUDIT)
        doc = exercised.read([AUDIT_SOURCE], self.root)
        self.assertEqual([u["id"] for u in doc["unresolved"]], ["unattributed:audit"])
        self.assertNotIn("attend/close", self.by_id(doc))

    def test_json_with_field_conditions_and_response_flags(self) -> None:
        write(self.root, "logs/e.json", ENVOY)
        doc = exercised.read([ENVOY_SOURCE], self.root)
        row = self.by_id(doc)["edge/api-example-com"]
        self.assertEqual((row["pass"], row["fail"]), (1, 2))
        self.assertNotIn("edge/other-example-com", self.by_id(doc))
        self.assertEqual(doc["unresolved"], [])

    def test_regex_format(self) -> None:
        write(self.root, "logs/r.log", "GET /x 200\nGET /y 502\nnoise\n")
        source = {
            "name": "access", "glob": "logs/r.log", "format": "regex",
            "pattern": r"^GET (?P<path>/\S+) (?P<status>\d+)$",
            "capability": "web/{path}",
            "fail_when": [{"field": "status", "op": "gte", "value": 500}],
        }
        found = self.by_id(exercised.read([source], self.root))
        self.assertEqual((found["web/x"]["pass"], found["web/x"]["fail"]), (1, 0))
        self.assertEqual((found["web/y"]["pass"], found["web/y"]["fail"]), (0, 1))

    def test_a_missing_template_field_is_unattributed(self) -> None:
        write(self.root, "logs/a.log", "axon-audit result=ok\n")
        doc = exercised.read([AUDIT_SOURCE], self.root)
        self.assertEqual(doc["capabilities"], [])
        self.assertEqual([u["kind"] for u in doc["unresolved"]], ["unattributed-log-line"])

    def test_a_source_with_no_files_is_named(self) -> None:
        doc = exercised.read([AUDIT_SOURCE], self.root)
        self.assertEqual([u["id"] for u in doc["unresolved"]], ["no-input:audit"])

    def test_an_input_path_replaces_the_glob(self) -> None:
        path = write(self.root, "elsewhere/x.log", AUDIT)
        doc = exercised.read([AUDIT_SOURCE], self.root, {"audit": [str(path)]})
        self.assertIn("attend/open", self.by_id(doc))

    def test_plugin_parses_lines(self) -> None:
        write(self.root, "logs/p.log", "ok journey-1\nfail journey-2\nskip\n")
        source = {"name": "canary", "glob": "logs/p.log", "plugin": "tests.capability_plugins:parse_journey"}
        found = self.by_id(exercised.read([source], self.root))
        self.assertEqual(found["canary/journey-1"]["pass"], 1)
        self.assertEqual(found["canary/journey-2"]["fail"], 1)

    def test_plugin_record_with_a_bad_result_is_unattributed(self) -> None:
        write(self.root, "logs/p.log", "maybe journey-1\n")
        source = {"name": "canary", "glob": "logs/p.log", "plugin": "tests.capability_plugins:parse_journey"}
        doc = exercised.read([source], self.root)
        self.assertEqual(doc["capabilities"], [])
        self.assertEqual([u["id"] for u in doc["unresolved"]], ["unattributed:canary"])

    def test_condition_on_a_missing_field_is_false_for_every_operator(self) -> None:
        for op in ("eq", "ne", "gt", "in", "contains"):
            self.assertFalse(exercised.holds({"field": "x", "op": op, "value": "1" if op != "in" else ["1"]}, {}))
        self.assertTrue(exercised.holds({"field": "x", "op": "exists", "value": False}, {}))

    def test_numeric_operators_compare_numbers_not_strings(self) -> None:
        self.assertTrue(exercised.holds({"field": "s", "op": "gte", "value": 500}, {"s": "1000"}))
        self.assertFalse(exercised.holds({"field": "s", "op": "lt", "value": 500}, {"s": "1000"}))

    def test_inclusive_operators_hold_at_the_boundary(self) -> None:
        self.assertTrue(exercised.holds({"field": "s", "op": "gte", "value": 500}, {"s": 500}))
        self.assertTrue(exercised.holds({"field": "s", "op": "lte", "value": 500}, {"s": 500}))
        self.assertFalse(exercised.holds({"field": "s", "op": "gt", "value": 500}, {"s": 500}))
        self.assertFalse(exercised.holds({"field": "s", "op": "lt", "value": 500}, {"s": 500}))

    def test_bad_configuration_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            exercised.read([{"name": "x", "glob": "g", "format": "xml", "capability": "a/b"}], self.root)
        with self.assertRaises(ValueError):
            exercised.read([{**AUDIT_SOURCE, "fail_when": [{"field": "r", "op": "approx"}]}], self.root)


def doc(capabilities: list[dict], **extra) -> dict:
    return {"capabilities": capabilities, "unresolved": [], **extra}


def spec_doc(*cap_ids: str) -> dict:
    return doc([{"id": i} for i in cap_ids])


def impl_doc(*cap_ids: str, unmapped: tuple[str, ...] = ()) -> dict:
    return doc([{"id": i, "surfaces": [f"s-{i}"]} for i in cap_ids], unmapped_surfaces=list(unmapped))


def exer_doc(*rows: tuple[str, int, int]) -> dict:
    return doc([{"id": i, "pass": p, "fail": f, "sources": ["s"],
                 "fail_samples": [{"source": "s", "at": "f:1", "line": "boom"}] if f else []}
                for i, p, f in rows])


class ReconcileTest(unittest.TestCase):
    def kinds(self, result: dict) -> dict[str, list[str]]:
        return {k: [f["id"] for f in v] for k, v in result["findings"].items()}

    def test_specified_but_unimplemented(self) -> None:
        result = reconcile.reconcile(spec_doc("a/b/c", "a/b/d"), impl_doc("a/b/c"), exer_doc(("a/b/c", 1, 0)))
        self.assertEqual(self.kinds(result)["specified-unimplemented"], ["a/b/d"])

    def test_unimplemented_is_not_repeated_as_unexercised(self) -> None:
        result = reconcile.reconcile(spec_doc("a/b/c"), impl_doc(), exer_doc())
        self.assertEqual(self.kinds(result)["specified-unexercised"], [])

    def test_implemented_but_unspecified_including_unmapped_surfaces(self) -> None:
        result = reconcile.reconcile(
            spec_doc("a/b/c"), impl_doc("a/b/c", "x/y/z", unmapped=("GET /raw",)), exer_doc(("a/b/c", 1, 0))
        )
        self.assertEqual(self.kinds(result)["implemented-unspecified"], ["x/y/z", "unmapped:GET /raw"])

    def test_specified_but_never_exercised(self) -> None:
        result = reconcile.reconcile(spec_doc("a/b/c", "a/b/d"), impl_doc("a/b"), exer_doc(("a/b/c", 1, 0)))
        self.assertEqual(self.kinds(result)["specified-unexercised"], ["a/b/d"])

    def test_exercised_and_failing_reports_counts_and_where(self) -> None:
        result = reconcile.reconcile(spec_doc("a/b/c"), impl_doc("a/b/c"), exer_doc(("a/b/c", 3, 2)))
        failing = result["findings"]["exercised-failing"]
        self.assertEqual([(f["id"], f["fail"], f["pass"]) for f in failing], [("a/b/c", 2, 3)])
        self.assertEqual(failing[0]["samples"][0]["at"], "f:1")

    def test_a_requirement_level_id_covers_its_scenarios(self) -> None:
        result = reconcile.reconcile(spec_doc("a/b/c", "a/b/d"), impl_doc("a/b"), exer_doc(("a/b", 1, 0)))
        self.assertEqual(sum(len(v) for v in self.kinds(result).values()), 0)
        self.assertEqual([r["status"] for r in result["rows"]], ["passing", "passing"])

    def test_a_failure_at_requirement_level_fails_each_scenario_under_it(self) -> None:
        result = reconcile.reconcile(spec_doc("a/b/c", "a/b/d"), impl_doc("a/b"), exer_doc(("a/b", 0, 1)))
        self.assertEqual([r["status"] for r in result["rows"]], ["failing", "failing"])

    def test_exercised_outside_the_spec_is_informational(self) -> None:
        result = reconcile.reconcile(spec_doc("a/b/c"), impl_doc("a/b/c"), exer_doc(("a/b/c", 1, 0), ("q/r", 1, 0)))
        self.assertEqual(result["informational"]["exercised-unspecified"], [{"id": "q/r"}])
        self.assertEqual(reconcile.gate(result, [])["status"], "pass")

    def test_adapter_unresolved_entries_carry_their_set(self) -> None:
        bad = spec_doc("a/b/c")
        bad["unresolved"] = [{"id": "duplicate:a/b/c", "kind": "duplicate-id", "reason": "twice"}]
        result = reconcile.reconcile(bad, impl_doc("a/b/c"), exer_doc(("a/b/c", 1, 0)))
        self.assertEqual(result["unresolved"][0]["set"], "specified")


class GateTest(unittest.TestCase):
    def result(self) -> dict:
        return reconcile.reconcile(
            spec_doc("a/b/c", "a/b/d", "a/e/f"), impl_doc("a/b/c", "a/e/f"), exer_doc(("a/b/c", 1, 1))
        )

    def rules(self, verdict: dict) -> list[tuple[str, str]]:
        return sorted((f["rule"], f["subject"]) for f in verdict["failures"])

    def test_every_finding_fails_without_an_exclusion(self) -> None:
        self.assertEqual(
            self.rules(reconcile.gate(self.result(), [])),
            [("exercised-failing", "a/b/c"), ("specified-unexercised", "a/e/f"),
             ("specified-unimplemented", "a/b/d")],
        )

    def test_an_exclusion_with_a_reason_waives_matching_findings(self) -> None:
        verdict = reconcile.gate(self.result(), [
            {"capability": "a/b/*", "reason": "tracked"},
            {"capability": "a/e/f", "kind": "specified-unexercised", "reason": "needs hardware"},
        ])
        self.assertEqual(verdict["status"], "pass")
        self.assertEqual(sorted(e["reason"] for e in verdict["excluded"]), ["needs hardware", "tracked", "tracked"])

    def test_an_exclusion_for_one_kind_does_not_waive_another(self) -> None:
        verdict = reconcile.gate(self.result(), [
            {"capability": "a/b/c", "kind": "specified-unexercised", "reason": "wrong kind"},
        ])
        self.assertIn(("exercised-failing", "a/b/c"), self.rules(verdict))
        self.assertIn(("unused-exclusion", "a/b/c"), self.rules(verdict))

    def test_an_exclusion_without_a_reason_does_not_apply(self) -> None:
        verdict = reconcile.gate(self.result(), [{"capability": "a/b/d", "reason": "  "}])
        rules = self.rules(verdict)
        self.assertIn(("incomplete-exclusion", "a/b/d"), rules)
        self.assertIn(("specified-unimplemented", "a/b/d"), rules)

    def test_an_exclusion_that_waives_nothing_fails(self) -> None:
        verdict = reconcile.gate(self.result(), [{"capability": "z/z/z", "reason": "gone"}])
        self.assertIn(("unused-exclusion", "z/z/z"), self.rules(verdict))

    def test_an_unknown_kind_is_malformed(self) -> None:
        verdict = reconcile.gate(self.result(), [{"capability": "a/b/d", "kind": "nope", "reason": "r"}])
        self.assertIn(("malformed-exclusion", "a/b/d"), self.rules(verdict))

    def test_unresolved_entries_gate_and_can_be_excluded_by_id(self) -> None:
        exer = exer_doc(("a/b/c", 1, 0))
        exer["unresolved"] = [{"id": "no-input:audit", "kind": "no-input", "reason": "no files"}]
        result = reconcile.reconcile(spec_doc("a/b/c"), impl_doc("a/b/c"), exer)
        self.assertEqual(self.rules(reconcile.gate(result, [])), [("unresolved", "no-input:audit")])
        waived = reconcile.gate(result, [{"capability": "no-input:*", "kind": "unresolved", "reason": "offline"}])
        self.assertEqual(waived["status"], "pass")

    def test_summary_says_a_pass_is_a_floor(self) -> None:
        result = self.result()
        text = reconcile.summary_text(result, reconcile.gate(result, []))
        self.assertIn("specified-unimplemented: 1", text)
        self.assertIn("a floor", text)


class CliTest(unittest.TestCase):
    TOML = """\
        [capabilities.specified]
        kind = "openspec"
        root = "openspec/specs"

        [[capabilities.implemented]]
        surface = "POST /v1/sessions"
        capability = "sessions/open-a-session"

        [[capabilities.exercised]]
        name = "audit"
        glob = "logs/*.log"
        format = "logfmt"
        match = { contains = "axon-audit" }
        capability = "sessions/open-a-session/{scenario}"
        fail_when = [{ field = "result", op = "eq", value = "error" }]
        """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        write(self.root, "capcov.toml", self.TOML)
        write(self.root, "openspec/specs/sessions/spec.md", SPEC)
        (self.root / "capabilities.json").write_text(json.dumps({
            "schema_version": 1, "kind": "capabilities",
            "surfaces": [{"id": "POST /v1/sessions"}],
        }))

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli_main([*argv, "--target", str(self.root)])
        return code, out.getvalue(), err.getvalue()

    def test_check_fails_naming_each_finding_and_writes_json(self) -> None:
        write(self.root, "logs/a.log",
              "axon-audit scenario=operator-opens-a-session result=ok\n"
              "axon-audit scenario=open-fails-when-the-quota-is-spent result=error\n")
        code, out, _ = self.run_cli("check")
        self.assertEqual(code, 1)
        self.assertIn("exercised-failing: 1", out)
        self.assertIn("specified-unimplemented: 1", out)
        written = json.loads((self.root / "capabilities-out/reconciliation.json").read_text())
        self.assertEqual(written["gate"]["status"], "fail")
        self.assertEqual(
            [f["id"] for f in written["findings"]["specified-unimplemented"]], ["sessions/close-a-session"]
        )

    def test_check_passes_when_the_sets_agree_or_are_excluded(self) -> None:
        write(self.root, "logs/a.log",
              "axon-audit scenario=operator-opens-a-session result=ok\n"
              "axon-audit scenario=open-fails-when-the-quota-is-spent result=ok\n")
        (self.root / "capcov.toml").write_text(
            textwrap.dedent(self.TOML)
            + '\n[[capabilities.exclude]]\ncapability = "sessions/close-a-session"\n'
            'reason = "close ships next"\n'
        )
        code, out, _ = self.run_cli("check")
        self.assertEqual(code, 0, out)
        self.assertIn("1 excluded with a reason", out)

    def test_json_flag_prints_the_document(self) -> None:
        write(self.root, "logs/a.log", "axon-audit scenario=operator-opens-a-session result=ok\n")
        _, out, _ = self.run_cli("check", "--json")
        self.assertEqual(json.loads(out)["kind"], "capabilities-reconciliation")

    def test_steps_write_their_set_and_reconcile_reads_them(self) -> None:
        write(self.root, "logs/a.log", "axon-audit scenario=operator-opens-a-session result=ok\n")
        for step in ("specified", "implemented", "exercised"):
            self.assertEqual(self.run_cli(step)[0], 0)
        code, out, _ = self.run_cli("reconcile")
        self.assertEqual(code, 1)
        self.assertIn("specified 3, implemented 1, exercised 1", out)

    def test_input_flag_replaces_the_glob(self) -> None:
        path = write(self.root, "elsewhere/x.log", "axon-audit scenario=operator-opens-a-session result=ok\n")
        code, _, _ = self.run_cli("exercised", "--input", f"audit={path}")
        self.assertEqual(code, 0)
        written = json.loads((self.root / "capabilities-out/exercised.json").read_text())
        self.assertEqual(written["unresolved"], [])

    def test_errors_exit_two(self) -> None:
        self.assertEqual(self.run_cli("exercised", "--input", "nope=x")[0], 2)
        (self.root / "capabilities.json").unlink()
        code, _, err = self.run_cli("implemented")
        self.assertEqual(code, 2)
        self.assertIn("run `capcov discover`", err)

    def test_the_console_entry_point_routes_capabilities(self) -> None:
        from capcov.cli import main

        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["capabilities", "specified", "--target", str(self.root)])
        self.assertEqual(code, 0)
        self.assertIn("3 specified", out.getvalue())


if __name__ == "__main__":
    unittest.main()
