"""Laravel validator rules -> bodies: the mapping is fixed, reviewed and deterministic.

The fixtures are rules() results in the exact shape ``reflect_requests.php``
emits (string rules, framework rule objects with their class, closures marked
unresolvable), for nine request classes covering the idioms a real application
mixes: nullable everything, ``in`` with quoted choices, ``exists`` and ``file``
placeholders, wildcard children, top-level list bodies, ``prohibited`` /
``exclude_if`` fields, conditionally-required elements, closures beside string
rules. The expected bodies were reviewed by hand against the rules; the
generator must reproduce them byte for byte, because a recorded vector is only
replayable if a rerun derives the same body.
"""

from __future__ import annotations

import copy
import json
import unittest

from capcov_contrib import laravel_validators as lv

_IN = "Illuminate\\Validation\\Rules\\In"
_EXISTS = "Illuminate\\Validation\\Rules\\Exists"
_CLOSURE = {"class": "Closure", "unresolvable": True}


def rules(*items: str) -> list[dict]:
    return [{"rule": item} for item in items]


FIXTURES = [
    {
        "request_class": "App\\Http\\Requests\\StoreTicketRequest",
        "rules": {
            "description": rules("nullable", "string", "max:8192"),
            "status": rules("nullable", "integer"),
            "priority": rules("nullable", "integer"),
            "impact_status": [{"rule": "nullable"}, {"rule": "integer"}, {"rule": 'in:"0","1","2","3","4"', "class": _IN}],
            "impact": rules("nullable", "integer", "min:1", "max:9999"),
            "cost_status": [{"rule": "nullable"}, {"rule": "integer"}, {"rule": 'in:"0","1","2","3","4"', "class": _IN}],
            "cost": rules("nullable", "numeric"),
            "category_id": rules("nullable", "integer"),
            "cause_id": rules("nullable", "integer"),
            "owner_id": rules("nullable", "integer"),
            "owner_group_id": rules("nullable", "integer"),
            "participants": rules("nullable"),
            "notify": rules("nullable", "array"),
            "notify.*.email": rules("nullable", "email"),
            "drawing_ref": rules("nullable", "string", "max:128"),
            "spec_ref": rules("nullable", "string", "max:128"),
            "internal_ref": rules("nullable", "string", "max:128"),
            "due_on": rules("nullable", "date"),
            "reported_on": rules("nullable", "date"),
            "recommendations": rules("nullable", "string", "max:8192"),
            "lessons_learned": [{"rule": "nullable"}, {"rule": "string"}, {"rule": 'in:"Yes","No"', "class": _IN}],
            "lessons_learned_text": rules("nullable", "string"),
            "copy_location": rules("nullable", "boolean"),
            "asset_id": rules("nullable", "integer"),
            "group_id": rules("nullable", "integer"),
            "asset_type_id": rules("nullable", "integer"),
            "form_id": [{"rule": "nullable"}, {"rule": "integer"}, {"rule": 'exists:forms,id,project_id,"0"', "class": _EXISTS}],
            "task_id": rules("nullable", "integer"),
            "question_id": rules("nullable", "integer"),
        },
        "valid": {
            "description": "v", "status": 1, "priority": 1, "impact_status": 0, "impact": 1, "cost_status": 0,
            "cost": 1, "category_id": 1, "cause_id": 1, "owner_id": 1, "owner_group_id": 1, "participants": "v",
            "notify": [], "drawing_ref": "v", "spec_ref": "v", "internal_ref": "v", "due_on": "2026-01-01",
            "reported_on": "2026-01-01", "recommendations": "v", "lessons_learned": "Yes",
            "lessons_learned_text": "v", "copy_location": True, "asset_id": 1, "group_id": 1, "asset_type_id": 1,
            "form_id": "{{id:forms}}", "task_id": 1, "question_id": 1,
        },
        "violates": {"field": "description", "rule": "string", "how": "wrong type"},
        "placeholders": {"form_id": {"kind": "id", "table": "forms", "column": "id", "extra": ["project_id", '"0"']}},
        "skipped": ["notify.*.email"],
    },
    {
        "request_class": "App\\Http\\Requests\\ImportSheetRequest",
        "rules": {
            "file": rules("required", "file", "mimes:xlsx,xls", "max:20480"),
            "column_map": rules("required", "array", "min:1", "max:1000"),
            "column_map.*.name": rules("required", "string"),
            "column_map.*.action": rules("required", "string"),
            "column_map.*.letter": rules("required", "string", "regex:/^[A-Z]{1,3}$/"),
            "locations": rules("sometimes", "nullable", "in:0,1"),
            "sheets": rules("prohibited"),
        },
        "valid": {"file": "{{file:xlsx}}", "column_map": [{"name": "v", "action": "v", "letter": "v"}], "locations": 0},
        "violates": {"field": "file", "rule": "required", "how": "omitted"},
        "placeholders": {"file": {"kind": "file", "extension": "xlsx", "carrier": "multipart"}},
        "skipped": ["sheets"],
    },
    {
        "request_class": "App\\Http\\Requests\\RegisterDeveloperRequest",
        "rules": {
            "email": rules("email", "unique:users"),
            "first_name": rules("string"),
            "last_name": rules("string"),
            "secret": rules("string", "nullable"),
            "company_name": rules("string", "nullable"),
            "password": rules("min:8", "regex:/^((?=.*[a-z])(?=.*[A-Z]))((?=.*\\d)(?=.*[_\\-@\\.,?\\/!~#$%\\^&\\*(\\)\\{\\}[\\]\\+\\=]+))[a-zA-Z\\d\\-_@\\.,?\\/!~#$%\\^&\\*(\\)\\{\\}[\\]\\+\\=]{8,}$/"),
        },
        "valid": {"email": "a@example.invalid", "first_name": "v", "last_name": "v", "secret": "v", "company_name": "v", "password": "vvvvvvvv"},
        "violates": {"field": "email", "rule": "email", "how": "wrong type"},
        "placeholders": {},
        "skipped": [],
    },
    {
        "request_class": "App\\Http\\Requests\\ExportRowsRequest",
        "rules": {
            "enabled_filters": rules("required", "array", "min:1"),
            "enabled_filters.*.class": rules("required", "string"),
            "enabled_filters.*.value": rules("present"),
        },
        "valid": {"enabled_filters": [{"class": "v", "value": "v"}]},
        "violates": {"field": "enabled_filters", "rule": "required", "how": "omitted"},
        "placeholders": {},
        "skipped": [],
    },
    {
        "request_class": "App\\Http\\Requests\\UpdateNoteRequest",
        "rules": {
            "status": rules("nullable", "array"),
            "status.value": rules("numeric"),
            "name": rules("exclude_if:true,true"),
            "project": rules("exclude_if:true,true"),
            "seq_num": rules("exclude_if:true,true"),
            "summary": rules("string", "nullable"),
            "description": rules("string", "nullable"),
            "drawing_ref": rules("string", "nullable"),
            "spec_ref": rules("string", "nullable"),
            "reported_on": rules("date"),
            "reported_by": rules("nullable", "array"),
            "reported_by.id": [{"rule": "numeric"}, {"rule": "nullable"}, {"rule": "exists:users,id", "class": _EXISTS}],
            "mob_id": rules("string", "nullable"),
            "category": rules("string", "nullable"),
            "recommendations": rules("string", "nullable"),
            "cost_to_resolve": rules("string", "nullable"),
            "energy_savings": rules("string", "nullable"),
            "emission_reduction": rules("string", "nullable"),
            "task": rules("nullable", "array"),
            "task.id": [{"rule": "numeric"}, {"rule": "exists:tasks,id", "class": _EXISTS}],
            "related_asset": rules("nullable"),
            "related_asset.id": rules("nullable", "numeric"),
            "related_asset.type": rules("nullable", "string"),
            "location": rules("nullable"),
            "location.id": rules("nullable", "numeric", "exists:locations,id"),
            "new_files": rules("nullable", "array"),
            "prerequisites": rules("nullable", "array"),
            "prerequisites.*.type": [{"rule": "required_with:prerequisites"}, {"rule": 'in:"10","9"', "class": _IN}],
            "prerequisites.*.id": rules("required_with:prerequisites"),
        },
        "valid": {
            "status": {"value": 1}, "summary": "v", "description": "v", "drawing_ref": "v", "spec_ref": "v",
            "reported_on": "2026-01-01", "reported_by": {"id": "{{id:users}}"}, "mob_id": "v", "category": "v",
            "recommendations": "v", "cost_to_resolve": "v", "energy_savings": "v", "emission_reduction": "v",
            "task": {"id": "{{id:tasks}}"}, "related_asset": {"id": 1, "type": "v"}, "location": {"id": "{{id:locations}}"},
            "new_files": [], "prerequisites": [{"type": 10, "id": "v"}],
        },
        "violates": {"field": "status", "rule": "array", "how": "wrong type"},
        "placeholders": {
            "reported_by.id": {"kind": "id", "table": "users", "column": "id", "extra": []},
            "task.id": {"kind": "id", "table": "tasks", "column": "id", "extra": []},
            "location.id": {"kind": "id", "table": "locations", "column": "id", "extra": []},
        },
        "skipped": ["name", "project", "seq_num"],
    },
    {
        "request_class": "App\\Http\\Requests\\GetChangesRequest",
        "rules": {
            "entity_types": rules("required", "array", "min:1"),
            "date_last_update": rules("required", "date", "before:now"),
        },
        "valid": {"entity_types": ["v"], "date_last_update": "2026-01-01"},
        "violates": {"field": "entity_types", "rule": "required", "how": "omitted"},
        "placeholders": {},
        "skipped": [],
    },
    {
        "request_class": "App\\Http\\Requests\\GenerateOutlineRequest",
        "rules": {
            "project_id": rules("nullable", "integer", "min:1"),
            "entity_type": rules("required", "integer", "in:8,15"),
            "entity_id": [{"rule": "nullable"}, {"rule": "integer"}, {"rule": "exists:forms,id", "class": _EXISTS}],
            "input": rules("required", "array"),
            "input.custom_instructions": [{"rule": "required"}, {"rule": "string"}, {"rule": "max:4000"}, _CLOSURE],
            "input.context": rules("nullable", "array"),
            "input.context.asset_name": [{"rule": "required_without:project_id"}, {"rule": "nullable"}, {"rule": "string"}, {"rule": "max:127"}, _CLOSURE],
            "input.sections": rules("nullable", "array"),
            "input.sections.*.name": [{"rule": "required_with:input.sections"}, {"rule": "string"}, _CLOSURE],
            "input.sections.*.description": [{"rule": "nullable"}, {"rule": "string"}, {"rule": "max:4000"}, _CLOSURE],
            "input.sections.*.subsections": rules("nullable", "array"),
            "input.sections.*.subsections.*.name": [{"rule": "required_with:input.sections.*.subsections"}, {"rule": "string"}, _CLOSURE],
            "files": rules("nullable", "array"),
            "files.*": [{"rule": "file"}, _CLOSURE],
            "file_ids": rules("nullable", "array"),
            "file_ids.*": [{"rule": "integer"}, {"rule": "exists:files,id", "class": _EXISTS}],
        },
        "valid": {
            "project_id": 1, "entity_type": 8, "entity_id": "{{id:forms}}",
            "input": {"custom_instructions": "v", "context": {}, "sections": []},
            "files": [], "file_ids": [],
        },
        "violates": {"field": "entity_type", "rule": "required", "how": "omitted"},
        "placeholders": {"entity_id": {"kind": "id", "table": "forms", "column": "id", "extra": []}},
        "skipped": [
            "file_ids.*", "files.*", "input.context.asset_name", "input.sections.*.description",
            "input.sections.*.name", "input.sections.*.subsections", "input.sections.*.subsections.*.name",
        ],
    },
    {
        "request_class": "App\\Http\\Requests\\CreateFilterRowsRequest",
        "rules": {
            "*.view_id": rules("required", "integer"),
            "*.config_id": rules("required", "integer"),
            "*.value": rules("required"),
        },
        "valid": [{"view_id": 1, "config_id": 1, "value": "v"}],
        "violates": {"field": "*.view_id", "rule": "required", "how": "omitted"},
        "placeholders": {},
        "skipped": [],
    },
    {
        "request_class": "App\\Http\\Requests\\UpdateRecipientsRequest",
        "rules": {
            "entity": [{"rule": "required"}, {"rule": 'in:"supplier","category"', "class": _IN}],
            "recipient_emails": rules("present", "array", "max:5"),
            "recipient_emails.*": rules("bail", "required", "string", "email:filter", "not_regex:/[<>\\r\\n]/", "max:254", "distinct:ignore_case"),
        },
        "valid": {"entity": "supplier", "recipient_emails": ["a@example.invalid"]},
        "violates": {"field": "entity", "rule": "required", "how": "omitted"},
        "placeholders": {},
        "skipped": [],
    },
]


class RuleParsing(unittest.TestCase):
    def test_in_uses_csv_quotes_like_the_framework(self):
        self.assertEqual(lv.parse_rule('in:"0","1","2"'), ("in", ["0", "1", "2"]))
        self.assertEqual(lv.parse_rule('in:"supplier","category"'), ("in", ["supplier", "category"]))
        self.assertEqual(lv.parse_rule("in:8,15"), ("in", ["8", "15"]))

    def test_regex_keeps_its_pipes_and_commas(self):
        self.assertEqual(lv.parse_rule("regex:/^(a|b),c$/"), ("regex", ["/^(a|b),c$/"]))

    def test_exists_parameters(self):
        self.assertEqual(lv.parse_rule('exists:forms,id,project_id,"0"'), ("exists", ["forms", "id", "project_id", '"0"']))

    def test_unresolvable_items_are_separated(self):
        parsed, unresolvable = lv.field_rules([{"rule": "required"}, _CLOSURE, {"class": "App\\Rules\\SafeText", "unresolvable": True}])
        self.assertEqual(parsed, [("required", [])])
        self.assertEqual(unresolvable, ["Closure", "App\\Rules\\SafeText"])


class ValueMapping(unittest.TestCase):
    def value(self, *items: str, field="f"):
        parsed = [lv.parse_rule(r) for r in items]
        placeholders, notes = {}, []
        return lv.value_for(field, parsed, [], placeholders, notes), placeholders, notes

    def test_scalars(self):
        self.assertEqual(self.value("required", "string")[0], "v")
        self.assertEqual(self.value("integer")[0], 1)
        self.assertEqual(self.value("int")[0], 1)
        self.assertEqual(self.value("numeric")[0], 1)
        self.assertEqual(self.value("boolean")[0], True)
        self.assertEqual(self.value("bool")[0], True)
        self.assertEqual(self.value("email")[0], "a@example.invalid")
        self.assertEqual(self.value("uuid")[0], lv.FIXED_UUID)
        self.assertEqual(self.value("date")[0], "2026-01-01")
        self.assertEqual(self.value("url")[0], "https://example.invalid/")
        self.assertEqual(self.value("array")[0], [])
        self.assertEqual(self.value("nullable")[0], "v")

    def test_in_picks_first_choice_with_the_declared_type(self):
        self.assertEqual(self.value("integer", 'in:"0","1","2"')[0], 0)
        self.assertEqual(self.value("string", 'in:"Yes","No"')[0], "Yes")
        self.assertEqual(self.value("in:0,1")[0], 0)
        self.assertEqual(self.value("required", 'in:"supplier","category"')[0], "supplier")

    def test_bounds_shape_the_value(self):
        self.assertEqual(self.value("integer", "min:1", "max:9999")[0], 1)
        self.assertEqual(self.value("integer", "min:5")[0], 5)
        self.assertEqual(self.value("integer", "max:0")[0], 0)
        self.assertEqual(self.value("min:8")[0], "vvvvvvvv")
        self.assertEqual(self.value("string", "size:3")[0], "vvv")
        self.assertEqual(self.value("digits:4")[0], 1000)

    def test_date_format(self):
        self.assertEqual(self.value("date_format:Y-m-d H:i:s")[0], "2026-01-01 00:00:00")
        self.assertEqual(self.value("date_format:Y-m-d\\TH:i")[0], "2026-01-01T00:00")

    def test_exists_becomes_a_manifest_placeholder(self):
        value, placeholders, _ = self.value("nullable", "integer", 'exists:forms,id,project_id,"0"', field="form_id")
        self.assertEqual(value, "{{id:forms}}")
        self.assertEqual(placeholders["form_id"], {"kind": "id", "table": "forms", "column": "id", "extra": ["project_id", '"0"']})
        value, placeholders, _ = self.value("exists:users,id", field="reported_by.id")
        self.assertEqual(value, "{{id:users}}")

    def test_file_becomes_a_multipart_placeholder(self):
        value, placeholders, _ = self.value("required", "file", "mimes:xlsx,xls", "max:20480", field="file")
        self.assertEqual(value, "{{file:xlsx}}")
        self.assertEqual(placeholders["file"]["carrier"], "multipart")

    def test_regex_is_flagged_not_solved(self):
        value, _, notes = self.value("required", "string", "regex:/^[A-Z]{1,3}$/", field="letter")
        self.assertEqual(value, "v")
        self.assertTrue(any("regex" in n for n in notes))

    def test_corruption_targets_the_first_typed_rule(self):
        self.assertEqual(lv.corrupt_value([lv.parse_rule("nullable"), lv.parse_rule("boolean")]), ("boolean", "not-a-boolean", "wrong type"))
        self.assertEqual(lv.corrupt_value([lv.parse_rule("min:8"), lv.parse_rule("regex:/x/")]), ("min", "", "below minimum length"))
        self.assertIsNone(lv.corrupt_value([lv.parse_rule("nullable")]))


class Bodies(unittest.TestCase):
    def test_nested_paths_and_wildcards(self):
        body = {}
        lv.set_path(body, ["notify", "*", "email"], "a@example.invalid")
        lv.set_path(body, ["status", "value"], 1)
        self.assertEqual(body, {"notify": [{"email": "a@example.invalid"}], "status": {"value": 1}})
        self.assertEqual(lv.get_path(body, ["notify", "*", "email"]), "a@example.invalid")
        self.assertTrue(lv.del_path(body, ["notify", "*", "email"]))
        self.assertEqual(body["notify"], [{}])

    def test_top_level_list_body(self):
        derived = lv.bodies_from_rules({"*.id": rules("required", "integer"), "*.value": rules("required")})
        self.assertEqual(derived["valid"], [{"id": 1, "value": "v"}])
        self.assertEqual(derived["invalid"]["body"], [{"value": "v"}])
        self.assertEqual(derived["invalid"]["violates"]["field"], "*.id")

    def test_prohibited_and_excluded_fields_are_omitted(self):
        derived = lv.bodies_from_rules({"a": rules("required", "string"), "b": rules("prohibited"),
                                        "c": rules("exclude_if:true,true"), "b.x": rules("required")})
        self.assertEqual(derived["valid"], {"a": "v"})
        self.assertEqual(set(derived["skipped"]), {"b", "c", "b.x"})

    def test_optional_wildcard_children_do_not_conjure_elements(self):
        derived = lv.bodies_from_rules({"sections": rules("nullable", "array"),
                                        "sections.*.name": [{"rule": "required_with:sections"}, _CLOSURE],
                                        "sections.*.note": rules("nullable", "string")})
        self.assertEqual(derived["valid"], {"sections": []})

    def test_min_array_gets_elements(self):
        derived = lv.bodies_from_rules({"ids": rules("required", "array", "min:2"), "ids.*": rules("integer")})
        self.assertEqual(derived["valid"], {"ids": [1, 1]})
        derived = lv.bodies_from_rules({"types": rules("required", "array", "min:1")})
        self.assertEqual(derived["valid"], {"types": ["v"]})
        self.assertTrue(derived["uncertain"])

    def test_unresolvable_required_field_is_best_effort_and_flagged(self):
        derived = lv.bodies_from_rules({"name": [{"rule": "required"}, {"rule": "string"}, {"class": "App\\Rules\\SafeText", "unresolvable": True}]})
        self.assertEqual(derived["valid"], {"name": "v"})
        self.assertEqual(derived["unresolvable"], {"name": ["App\\Rules\\SafeText"]})
        self.assertTrue(any("best effort" in n for n in derived["uncertain"]))

    def test_no_rules_means_empty_body_and_no_invalid(self):
        derived = lv.bodies_from_rules({})
        self.assertEqual(derived["valid"], {})
        self.assertIsNone(derived["invalid"])
        self.assertEqual(derived["uncertain"], [])

    def test_rules_with_nothing_required_or_typed_say_no_invalid_body_can_be_derived(self):
        derived = lv.bodies_from_rules({"note": rules("nullable")})
        self.assertIsNone(derived["invalid"])
        self.assertTrue(any("no invalid body" in n for n in derived["uncertain"]))

    def test_unknown_rule_names_are_reported(self):
        derived = lv.bodies_from_rules({"x": rules("required", "participant")})
        self.assertEqual(derived["unknown_rules"], ["participant"])

    def test_carrier_follows_the_placeholders_then_the_verb(self):
        self.assertEqual(lv.carrier_for(["POST"], {"file": {"kind": "file", "carrier": "multipart"}}), "multipart")
        self.assertEqual(lv.carrier_for(["GET"], {}), "query")
        self.assertEqual(lv.carrier_for(["PUT"], {}), "json")


class ReviewedRequestClasses(unittest.TestCase):
    """Each fixture is a reviewed rules() result; the generator must reproduce the reviewed bodies."""

    def test_at_least_five_request_classes(self):
        self.assertGreaterEqual(len({f["request_class"] for f in FIXTURES}), 5)

    def test_valid_bodies(self):
        for fixture in FIXTURES:
            with self.subTest(fixture["request_class"]):
                derived = lv.bodies_from_rules(fixture["rules"])
                self.assertEqual(derived["valid"], fixture["valid"])
                self.assertEqual(derived["placeholders"], fixture["placeholders"])
                self.assertEqual(sorted(derived["skipped"]), fixture["skipped"])

    def test_invalid_bodies_violate_exactly_one_field(self):
        for fixture in FIXTURES:
            with self.subTest(fixture["request_class"]):
                derived = lv.bodies_from_rules(fixture["rules"])
                self.assertEqual(derived["invalid"]["violates"], fixture["violates"])
                path = lv.split_path(fixture["violates"]["field"])
                expected, actual = copy.deepcopy(derived["valid"]), copy.deepcopy(derived["invalid"]["body"])
                if fixture["violates"]["how"] == "omitted":
                    self.assertIsNone(lv.get_path(actual, path))
                    lv.del_path(expected, path)
                else:
                    self.assertNotEqual(lv.get_path(actual, path), lv.get_path(expected, path))
                    lv.set_path(expected, path, lv.get_path(actual, path))
                self.assertEqual(expected, actual, "invalid body differs from valid beyond the violated field")

    def test_first_required_field_is_the_one_omitted(self):
        for fixture in FIXTURES:
            required = [f for f, items in fixture["rules"].items()
                        if lv.is_required(lv.field_rules(items)[0]) and not lv.is_omitted(lv.field_rules(items)[0])]
            if required:
                with self.subTest(fixture["request_class"]):
                    self.assertEqual(fixture["violates"], {"field": required[0], "rule": "required", "how": "omitted"})

    def test_every_required_field_is_present_in_the_valid_body(self):
        for fixture in FIXTURES:
            derived = lv.bodies_from_rules(fixture["rules"])
            for field, items in fixture["rules"].items():
                parsed, _ = lv.field_rules(items)
                if lv.is_required(parsed) and "*" not in field:
                    with self.subTest(f"{fixture['request_class']}.{field}"):
                        self.assertIsNotNone(lv.get_path(derived["valid"], lv.split_path(field)))

    def test_deterministic(self):
        for fixture in FIXTURES:
            first = json.dumps(lv.bodies_from_rules(fixture["rules"]), sort_keys=True)
            second = json.dumps(lv.bodies_from_rules(copy.deepcopy(fixture["rules"])), sort_keys=True)
            self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
