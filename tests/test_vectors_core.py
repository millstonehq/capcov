"""capcov.vectors core: schema round-trips, fixed templates, normalization, deltas.

Every cell of a template lands in exactly one of (cells, gaps); a policy that
masks volatility is idempotent; a delta names what changed and which side of
the declared line it is on; a difference is a single path a person can read.
"""

from __future__ import annotations

import unittest

from capcov.vectors import diff, normalize, schema, templates
from capcov.vectors.schema import Cell, Operation, Request, StepSpec


def op(access: str = "write", **overrides) -> Operation:
    fields = {
        "id": "orders.update",
        "kind": "http-route",
        "access": access,
        "driver": "http",
        "request": {"method": "PUT", "path": "/api/orders/{order_id}"},
        "feature": "orders",
        "label": "PUT /api/orders/{order_id}",
    }
    fields.update(overrides)
    return Operation(**fields)


MANIFEST = {
    "actors": {"permitted": 11, "no_permission": 12, "other_tenant": 14, "overrides": {}},
    "params": {"order": 31, "overrides": {}},
    "missing": {"order": 999, "overrides": {}},
    "other_tenant": {"order": 39, "overrides": {}},
}


class OperationTests(unittest.TestCase):
    def test_keys_are_id_label_aliases_deduplicated_in_order(self) -> None:
        o = op(aliases=["orders.update", "PUT /api/orders/{order_id}", "orders.put", ""])
        self.assertEqual(o.keys(), ["orders.update", "PUT /api/orders/{order_id}", "orders.put"])

    def test_refuses_empty_id_kind_or_access(self) -> None:
        with self.assertRaises(ValueError):
            op(id="")
        with self.assertRaises(ValueError):
            op(kind="")
        with self.assertRaises(ValueError):
            op(access="")

    def test_round_trip(self) -> None:
        o = op(aliases=["orders.put"], notes=["not drivable: no route"])
        self.assertEqual(Operation.from_json(o.to_json()), o)


class RequestTests(unittest.TestCase):
    def test_credential_headers_never_reach_json(self) -> None:
        r = Request("http", "GET", "/api/orders", headers={"Authorization": "Bearer x", "Cookie": "s=1", "Accept": "*/*"})
        self.assertEqual(r.to_json()["headers"], {"Accept": "*/*"})

    def test_binary_body_is_named_not_embedded(self) -> None:
        r = Request("http", "POST", "/upload", body=b"\xff\xfe\x00")
        self.assertEqual(r.to_json()["body"], "<binary 3 bytes>")

    def test_text_body_round_trips(self) -> None:
        r = Request("http", "POST", "/api/orders", body=b'{"a": 1}')
        self.assertEqual(Request.from_json(r.to_json()), r)

    def test_shell_request_round_trips(self) -> None:
        r = Request("shell", "artisan", "orders:sync", argv=["orders:sync", "--dry-run"])
        self.assertEqual(Request.from_json(r.to_json()), r)

    def test_unknown_kind_refused(self) -> None:
        with self.assertRaises(ValueError):
            Request("grpc", "Call", "/x")


class TemplateTests(unittest.TestCase):
    def ids(self, cells, gaps):
        return [c.id for c in cells], [g["cell"] for g in gaps]

    def test_read_cells(self) -> None:
        cells, gaps = templates.cells_for(op("read"), MANIFEST, {})
        self.assertEqual(self.ids(cells, gaps), (["permitted", "no-permission", "other-tenant", "missing-record", "anonymous"], []))
        by_id = {c.id: c for c in cells}
        self.assertEqual(by_id["permitted"].steps[0].actor, 11)
        self.assertEqual(by_id["no-permission"].steps[0].actor, 12)
        self.assertEqual(by_id["other-tenant"].steps[0].actor, 14)
        self.assertEqual(by_id["other-tenant"].steps[0].params, {"order": 31})
        self.assertEqual(by_id["missing-record"].steps[0].params, {"order": 999})
        self.assertIsNone(by_id["anonymous"].steps[0].actor)
        self.assertEqual(by_id["anonymous"].steps[0].role, "anonymous")

    def test_write_cells(self) -> None:
        inputs = {"orders.update": {"body": {"status": "open"}, "invalid": {"status": "no-such"}}}
        cells, gaps = templates.cells_for(op("write"), MANIFEST, inputs)
        self.assertEqual(self.ids(cells, gaps), (["authorized", "no-permission", "other-tenant", "invalid-input", "retry", "anonymous"], []))
        by_id = {c.id: c for c in cells}
        self.assertEqual(by_id["authorized"].steps[0].body, {"status": "open"})
        self.assertEqual(by_id["invalid-input"].steps[0].body, {"status": "no-such"})
        self.assertEqual(len(by_id["retry"].steps), 2)
        self.assertEqual(by_id["retry"].steps[0], by_id["retry"].steps[1])
        self.assertIsNot(by_id["retry"].steps[0], by_id["retry"].steps[1])

    def test_write_invalid_input_defaults_to_empty_body(self) -> None:
        cells, _ = templates.cells_for(op("write"), MANIFEST, {"orders.update": {"body": {"status": "open"}}})
        self.assertEqual({c.id: c for c in cells}["invalid-input"].steps[0].body, {})

    def test_webhook_cells_need_no_actor(self) -> None:
        manifest = {"actors": {}, "params": {}, "missing": {}, "other_tenant": {}}
        cells, gaps = templates.cells_for(op("webhook", id="hooks.payment"), manifest, {})
        self.assertEqual(self.ids(cells, gaps), (["valid-signature", "invalid-signature", "duplicate"], []))
        by_id = {c.id: c for c in cells}
        self.assertEqual(by_id["valid-signature"].steps[0].signature, "valid")
        self.assertEqual(by_id["invalid-signature"].steps[0].signature, "invalid")
        self.assertEqual([s.signature for s in by_id["duplicate"].steps], ["valid", "valid"])

    def test_scheduled_cells_and_retry_gap_without_fault(self) -> None:
        cells, gaps = templates.cells_for(op("scheduled", id="jobs.nightly"), MANIFEST, {})
        self.assertEqual(self.ids(cells, gaps), (["due", "not-due"], ["retry-after-failure"]))
        self.assertIn("`fault`", gaps[0]["reason"])
        by_id = {c.id: c for c in cells}
        self.assertEqual(by_id["due"].steps[0].when, "due")
        self.assertEqual(by_id["not-due"].steps[0].when, "not-due")
        self.assertIsNone(by_id["due"].steps[0].actor)
        self.assertEqual(by_id["due"].steps[0].role, "system")

    def test_scheduled_retry_runs_when_fault_given(self) -> None:
        cells, gaps = templates.cells_for(op("scheduled", id="jobs.nightly"), MANIFEST, {"jobs.nightly": {"fault": {"kill": "worker"}}})
        self.assertEqual(self.ids(cells, gaps), (["due", "not-due", "retry-after-failure"], []))
        self.assertEqual(cells[2].steps[0].fault, {"kill": "worker"})
        self.assertIsNone(cells[0].steps[0].fault)

    def test_queued_cells_carry_drain_flag(self) -> None:
        cells, gaps = templates.cells_for(op("queued", id="jobs.email"), MANIFEST, {"jobs.email": {"fault": True}})
        self.assertEqual(self.ids(cells, gaps), (["dispatched", "undrained", "retry-after-failure"], []))
        self.assertEqual([c.steps[0].drain for c in cells], [True, False, True])

    def test_command_cells(self) -> None:
        cells, gaps = templates.cells_for(op("command", id="cli.sync"), MANIFEST, {"cli.sync": {"args": {"--force": True}}})
        self.assertEqual(self.ids(cells, gaps), (["run", "retry"], []))
        self.assertEqual(len(cells[1].steps), 2)
        self.assertEqual(cells[0].steps[0].args, {"--force": True})

    def test_other_scope_fallback_when_no_foreign_actor(self) -> None:
        manifest = {**MANIFEST, "actors": {"permitted": 11, "no_permission": 12, "other_tenant": None}}
        cells, gaps = templates.cells_for(op("read"), manifest, {})
        ids = [c.id for c in cells]
        self.assertIn("other-scope", ids)
        self.assertNotIn("other-tenant", ids)
        self.assertEqual(gaps, [])
        scope = {c.id: c for c in cells}["other-scope"]
        self.assertEqual(scope.steps[0].actor, 11)
        self.assertEqual(scope.steps[0].role, "permitted")
        self.assertEqual(scope.steps[0].params, {"order": 39})
        self.assertIn("another scope", scope.note)

    def test_other_tenant_gap_when_neither_actor_nor_ids(self) -> None:
        manifest = {**MANIFEST, "actors": {"permitted": 11, "no_permission": 12}, "other_tenant": {}}
        cells, gaps = templates.cells_for(op("read"), manifest, {})
        self.assertEqual([c.id for c in cells], ["permitted", "no-permission", "missing-record", "anonymous"])
        self.assertEqual(gaps, [{"cell": "other-tenant", "reason": "manifest has no actor for role 'other_tenant'"}])

    def test_missing_record_gap_without_missing_ids(self) -> None:
        manifest = {**MANIFEST, "missing": {}}
        cells, gaps = templates.cells_for(op("read"), manifest, {})
        self.assertEqual([g["cell"] for g in gaps], ["missing-record"])
        self.assertIn("`missing`", gaps[0]["reason"])

    def test_no_permission_gap_without_actor(self) -> None:
        manifest = {**MANIFEST, "actors": {"permitted": 11, "other_tenant": 14}}
        _, gaps = templates.cells_for(op("read"), manifest, {})
        self.assertEqual(gaps, [{"cell": "no-permission", "reason": "manifest has no actor for role 'no_permission'"}])

    def test_unknown_access_is_one_named_gap(self) -> None:
        cells, gaps = templates.cells_for(op("hook", id="hooks.saved"), MANIFEST, {})
        self.assertEqual(cells, [])
        self.assertEqual(gaps, [{"cell": "none", "reason": "no template for access 'hook'"}])

    def test_inputs_access_override_changes_template(self) -> None:
        cells, _ = templates.cells_for(op("read"), MANIFEST, {"orders.update": {"access": "write"}})
        self.assertEqual([c.id for c in cells], templates.cell_ids("write"))

    def test_manifest_overrides_apply_by_operation_key(self) -> None:
        manifest = {
            **MANIFEST,
            "actors": {"permitted": 11, "no_permission": 12, "other_tenant": 14,
                       "overrides": {"PUT /api/orders/{order_id}": {"permitted": 13}}},
            "params": {"order": 31, "overrides": {"orders.update": {"order": 32}}},
        }
        cells, _ = templates.cells_for(op("write"), manifest, {})
        auth = {c.id: c for c in cells}["authorized"]
        self.assertEqual(auth.steps[0].actor, 13)
        self.assertEqual(auth.steps[0].params, {"order": 32})

    def test_inputs_later_keys_override_and_params_merge(self) -> None:
        o = op("read", aliases=["orders.get"])
        inputs = {"orders.update": {"query": {"per_page": 5}, "params": {"order": 77}},
                  "orders.get": {"query": {"per_page": 10}}}
        cells, _ = templates.cells_for(o, MANIFEST, inputs)
        step = cells[0].steps[0]
        self.assertEqual(step.query, {"per_page": 10})
        self.assertEqual(step.params, {"order": 77})

    def test_every_template_cell_lands_in_cells_or_gaps(self) -> None:
        bare = {"actors": {}, "params": {}, "missing": {}, "other_tenant": {}}
        for access in schema.ACCESS_CLASSES:
            cells, gaps = templates.cells_for(op(access, id=f"x.{access}"), bare, {})
            self.assertEqual(len(cells) + len(gaps), len(templates.TEMPLATES[access]), access)

    def test_declared_for_defaults_to_star(self) -> None:
        self.assertEqual(templates.declared_for(op(), {}), "*")
        self.assertEqual(templates.declared_for(op(), {"orders.update": {"declared": {"rows": ["orders"]}}}), {"rows": ["orders"]})


class NormalizeTests(unittest.TestCase):
    def test_masks_volatile_keys_by_name(self) -> None:
        out = normalize.normalize({"created_at": "x", "updated": 5, "access_token": "t", "name": "n", "expires": 1})
        self.assertEqual(out, {"created_at": "<volatile>", "updated": "<volatile>", "access_token": "<volatile>",
                               "name": "n", "expires": "<volatile>"})

    def test_empty_volatile_values_stay_visible(self) -> None:
        out = normalize.normalize({"deleted_at": None, "modified": "", "count_updated": 0})
        self.assertEqual(out, {"deleted_at": None, "modified": "", "count_updated": 0})

    def test_masks_datetime_shaped_string_values_anywhere(self) -> None:
        out = normalize.normalize({"last_response": "2026-01-02 03:04:05", "items": ["2026-01-02T03:04:05.123Z", "plain"],
                                   "nested": {"when": "2026-01-02T03:04:05+01:00"}})
        self.assertEqual(out, {"last_response": "<volatile-datetime>", "items": ["<volatile-datetime>", "plain"],
                               "nested": {"when": "<volatile-datetime>"}})
        self.assertEqual(normalize.normalize("2026-01-02"), "2026-01-02")

    def test_drops_document_encoder_keys(self) -> None:
        out = normalize.normalize({"_id": {"$oid": "abc"}, "when": {"$date": 1}, "value": 2})
        self.assertEqual(out, {"when": {}, "value": 2})

    def test_extra_volatile_keys_are_exact_names(self) -> None:
        out = normalize.normalize({"nonce": "abc", "nonce_count": 3}, extra_volatile_keys=("nonce",))
        self.assertEqual(out, {"nonce": "<volatile>", "nonce_count": 3})

    def test_idempotent(self) -> None:
        raw = {"created_at": "2026-01-02 03:04:05", "_id": 1, "rows": [{"token": "t", "seen": "2026-01-02T03:04:05Z"}],
               "empty_at": None}
        once = normalize.normalize(raw)
        self.assertEqual(normalize.normalize(once), once)

    def test_scalars_and_tuples(self) -> None:
        self.assertEqual(normalize.normalize(3), 3)
        self.assertEqual(normalize.normalize(None), None)
        self.assertEqual(normalize.normalize(("a", "2026-01-02 03:04:05")), ["a", "<volatile-datetime>"])


def inspection(rows=None, collections=None, queues=None, redis_keys=()):
    return {"rows": rows or {}, "collections": collections or {}, "queues": queues or {}, "redis_keys": list(redis_keys)}


class StoreDeltaTests(unittest.TestCase):
    def test_inserted_updated_deleted_rows(self) -> None:
        before = inspection(rows={"orders": [{"id": 1, "status": "open", "updated_at": "a"},
                                             {"id": 2, "status": "open", "updated_at": "a"}]})
        after = inspection(rows={"orders": [{"id": 1, "status": "closed", "updated_at": "b"},
                                            {"id": 3, "status": "open", "updated_at": "c"}]})
        compared, info = diff.store_delta(before, after, "*")
        self.assertEqual(info, {})
        change = compared["rows.orders"]
        self.assertEqual(change["inserted"], [{"id": 3, "status": "open", "updated_at": "<volatile>"}])
        self.assertEqual(change["deleted"], [{"id": 2, "status": "open", "updated_at": "<volatile>"}])
        self.assertEqual(change["updated"], [{"id": 1, "changed": {"status": ["open", "closed"]}}])

    def test_unchanged_tables_do_not_appear(self) -> None:
        same = inspection(rows={"orders": [{"id": 1, "status": "open"}], "items": []})
        self.assertEqual(diff.store_delta(same, same), ({}, {}))

    def test_rows_without_id_are_keyed_by_content(self) -> None:
        before = inspection(rows={"links": [{"a": 1, "b": 2}]})
        after = inspection(rows={"links": [{"a": 1, "b": 2}, {"a": 1, "b": 3}]})
        compared, _ = diff.store_delta(before, after)
        self.assertEqual(compared["rows.links"], {"inserted": [{"a": 1, "b": 3}], "updated": [], "deleted": []})

    def test_declared_vs_informational_split(self) -> None:
        before = inspection(rows={"orders": [], "audit_log": [], "failed_jobs": []},
                            collections={"events": [], "traces": []})
        after = inspection(rows={"orders": [{"id": 1}], "audit_log": [{"id": 1}], "failed_jobs": [{"id": 9}]},
                           collections={"events": [{"kind": "x"}], "traces": [{"kind": "y"}]})
        declared = {"rows": ["orders", "failed_jobs"], "collections": ["events"]}
        compared, info = diff.store_delta(before, after, declared, informational=["failed_jobs"])
        self.assertEqual(sorted(compared), ["collections.events", "rows.orders"])
        self.assertEqual(sorted(info), ["collections.traces", "rows.audit_log", "rows.failed_jobs"])

    def test_declared_accepts_store_technology_spellings(self) -> None:
        before = inspection(rows={"orders": []}, collections={"events": []})
        after = inspection(rows={"orders": [{"id": 1}]}, collections={"events": [{"k": 1}]})
        compared, info = diff.store_delta(before, after, {"mysql": ["orders"], "mongo": ["events"]})
        self.assertEqual(sorted(compared), ["collections.events", "rows.orders"])
        self.assertEqual(info, {})

    def test_declared_shape_refused(self) -> None:
        with self.assertRaises(ValueError):
            diff.store_delta(inspection(), inspection(), ["orders"])

    def test_appended_documents(self) -> None:
        before = inspection(collections={"events": [{"_id": 1, "kind": "a"}]})
        after = inspection(collections={"events": [{"_id": 1, "kind": "a"}, {"_id": 2, "kind": "b", "at": "2026-01-02 03:04:05"}]})
        compared, _ = diff.store_delta(before, after)
        self.assertEqual(compared["collections.events"],
                         {"appended": [{"kind": "b", "at": "<volatile-datetime>"}], "removed": 0, "rewritten": 0, "count": [1, 2]})

    def test_removed_and_rewritten_documents_are_named(self) -> None:
        before = inspection(collections={"events": [{"kind": "a"}, {"kind": "b"}, {"kind": "c"}]})
        after = inspection(collections={"events": [{"kind": "a"}, {"kind": "z"}]})
        compared, _ = diff.store_delta(before, after)
        self.assertEqual(compared["collections.events"], {"appended": [], "removed": 1, "rewritten": 1, "count": [3, 2]})

    def test_redis_keys_and_queues_are_always_informational(self) -> None:
        before = inspection(redis_keys=["a", "b"], queues={"default": 0})
        after = inspection(redis_keys=["b", "c"], queues={"default": 2, "mail": 0})
        compared, info = diff.store_delta(before, after, "*")
        self.assertEqual(compared, {})
        self.assertEqual(info, {"redis.keys": {"added": ["c"], "removed": ["a"]}, "queues": {"default": 2, "mail": 0}})

    def test_extra_volatile_keys_reach_row_comparison(self) -> None:
        before = inspection(rows={"orders": [{"id": 1, "nonce": "a"}]})
        after = inspection(rows={"orders": [{"id": 1, "nonce": "b"}]})
        self.assertEqual(diff.store_delta(before, after, extra_volatile_keys=("nonce",)), ({}, {}))
        self.assertIn("rows.orders", diff.store_delta(before, after)[0])


class FirstDifferenceTests(unittest.TestCase):
    def test_equal_is_none(self) -> None:
        self.assertIsNone(diff.first_difference({"a": [1, {"b": None}]}, {"a": [1, {"b": None}]}))

    def test_dict_missing_and_unexpected_keys(self) -> None:
        self.assertEqual(diff.first_difference({"a": 1, "b": 2}, {"a": 1}), "/b: missing in candidate")
        self.assertEqual(diff.first_difference({"a": 1}, {"a": 1, "b": 2}), "/b: unexpected in candidate: 2")

    def test_nested_path_names_the_first_leaf(self) -> None:
        expected = {"steps": [{"status": 200, "body": {"ok": True}}, {"status": 200, "body": {"ok": True}}]}
        actual = {"steps": [{"status": 200, "body": {"ok": True}}, {"status": 403, "body": {"ok": False}}]}
        self.assertEqual(diff.first_difference(expected, actual), "/steps[1]/body/ok: expected true got false")

    def test_list_length_before_items(self) -> None:
        self.assertEqual(diff.first_difference([1, 2, 3], [1, 2]), "/: expected 3 items got 2")
        self.assertEqual(diff.first_difference({"x": [1]}, {"x": []}), "/x: expected 1 items got 0")

    def test_scalars(self) -> None:
        self.assertEqual(diff.first_difference(1, 2), "/: expected 1 got 2")
        self.assertEqual(diff.first_difference("a", "b"), "/: expected \"a\" got \"b\"")

    def test_type_mismatch_is_a_difference(self) -> None:
        self.assertEqual(diff.first_difference(1, 1.0), "/: expected 1 got 1.0")
        self.assertEqual(diff.first_difference(True, 1), "/: expected true got 1")
        self.assertEqual(diff.first_difference({"a": None}, {"a": {}}), "/a: expected null got {}")

    def test_dict_keys_visited_in_sorted_order(self) -> None:
        self.assertEqual(diff.first_difference({"z": 1, "a": 1}, {"z": 2, "a": 2}), "/a: expected 1 got 2")


class ArtifactRoundTripTests(unittest.TestCase):
    def test_vectors_artifact(self) -> None:
        cells, gaps = templates.cells_for(op("write"), MANIFEST, {"orders.update": {"body": {"status": "open"}}})
        vectors = [schema.Vector(id=c.id, cell=c.id, actor=c.actor, steps=c.steps,
                                 expected={"steps": [{"status": 200, "body": {"ok": True}}], "delta": {}, "informational": {}},
                                 seconds=0.5, note=c.note) for c in cells]
        artifact = schema.VectorsArtifact(operation="orders.update", template_version=templates.TEMPLATE_VERSION,
                                          declared_stores={"rows": ["orders"]}, provenance={"snapshot_sha256": "abc"},
                                          vectors=vectors, gaps=gaps,
                                          required_cells=[cell.id for cell in cells] + [item["cell"] for item in gaps])
        data = artifact.to_json()
        self.assertEqual(data["version"], 2)
        self.assertEqual(data["vectors"][0]["input"]["actor"], 11)
        self.assertEqual(data["vectors"][0]["input"]["steps"][0]["body"], {"status": "open"})
        self.assertEqual(sorted(data), ["declared_stores", "gaps", "operation", "provenance", "required_cells", "template_version", "vectors", "version"])
        self.assertEqual(schema.VectorsArtifact.from_json(data), artifact)

    def test_replay_artifact(self) -> None:
        artifact = schema.ReplayArtifact(operation="orders.update", mutant="no-permission-check", vectors_recorded=2,
                                         vectors_passing=1, gaps_recorded=1, candidate_sha256="def",
                                         vectors_provenance={"snapshot_sha256": "abc"},
                                         comparison_policy=normalize.comparison_policy_identity(),
                                         results=[schema.ReplayResult("authorized", "authorized happy path", True),
                                                  schema.ReplayResult("no-permission", "actor without the permission", False,
                                                                      "/steps[0]/status: expected 403 got 200")])
        data = artifact.to_json()
        self.assertEqual(data["version"], 3)
        self.assertEqual(data["results"][1]["pass"], False)
        self.assertEqual(data["gaps_recorded"], 1)
        self.assertEqual(schema.ReplayArtifact.from_json(data), artifact)
        missing_policy = {**data}
        del missing_policy["comparison_policy"]
        with self.assertRaisesRegex(ValueError, "comparison_policy"):
            schema.ReplayArtifact.from_json(missing_policy)
        # Legacy v2 replay data has only recording provenance. Do not infer an
        # executed policy identity while loading it for historical judging.
        legacy_v2 = {**data, "version": 2}
        with self.assertRaisesRegex(ValueError, "version must be 3"):
            schema.ReplayArtifact.from_json(legacy_v2)

    def test_unknown_versions_are_refused(self) -> None:
        with self.assertRaises(ValueError) as caught:
            schema.VectorsArtifact.from_json({"version": 999, "operation": "x"})
        self.assertIn("vectors artifact", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            schema.ReplayArtifact.from_json({"operation": "x"})
        self.assertIn("replay artifact", str(caught.exception))

    def test_cell_and_step_round_trip(self) -> None:
        cell = Cell(id="retry", note="retry of the same write",
                    steps=[StepSpec(actor=11, role="permitted", params={"order": 1}, body={"a": 1}, drain=False)] * 2)
        self.assertEqual(Cell.from_json(cell.to_json()), cell)


if __name__ == "__main__":
    unittest.main()
