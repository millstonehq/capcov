"""Drivers: an operation plus a step becomes exactly the request a client sends.

Every driver is a pure function of (operation, step, credential, config), so
the recorded request is byte-stable across runs and the replay re-drives the
same step through the same code. The tests pin that: path parameters are
filled by name with the id suffixes stripped, the carrier follows the verb
unless configured, a form-post puts its selector where the controller reads
it, a webhook signature verifies under the secret and an invalid one is
well-formed but wrong, a shell step is an argv the consumer's helper reads.

And what a driver refuses: a step whose parameter has no value, whose actor
has no credential, whose selector key was never resolved, whose scheduler
needs event names nobody gave -- each is a ``DriverGap`` naming the reason,
never a request built on a guess.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import unittest
import urllib.parse

from capcov.vectors import drivers
from capcov.vectors.drivers import http as http_driver
from capcov.vectors.schema import DriverGap, Operation, Request, StepSpec

BEARER = {"bearer": "token-for-actor-11"}
COOKIE = {"cookie": "session=abc"}


def plugin_drive(operation, step, credential, base_url, inputs):
    """A consumer's own protocol, resolved by dotted path exactly like an adapter plugin."""
    query = {"plugin": 1, **inputs}
    return [Request("http", "GET", drivers.with_query(operation.request["path"], query), drivers.credential_headers(credential))]


def op(driver: str, access: str = "write", **request) -> Operation:
    return Operation(id="op-1", kind="route", access=access, driver=driver, request=request, label="op one")


def parse_query(path: str) -> dict:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(path).query, keep_blank_values=True))


def one(result) -> Request:
    assert not isinstance(result, DriverGap), result
    assert len(result) == 1, result
    return result[0]


class HttpDriverTests(unittest.TestCase):
    def test_fills_path_params_by_name_with_id_suffixes_stripped(self) -> None:
        operation = op("http", access="read", method="GET", path="/projects/{project_id}/orders/{orderId}/{note_uuid?}")
        step = StepSpec(actor=11, params={"project": 21, "order": 31})
        request = one(drivers.drive(operation, step, BEARER, "http://oracle", {}))
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.path, "/projects/21/orders/31")
        self.assertEqual(request.headers["Authorization"], "Bearer token-for-actor-11")
        self.assertEqual(request.headers["Accept"], "application/json")
        self.assertIsNone(request.body)

    def test_missing_path_param_is_a_gap_naming_it(self) -> None:
        operation = op("http", access="read", method="GET", path="/orders/{order_id}")
        result = drivers.drive(operation, StepSpec(actor=11, params={}), BEARER, "", {})
        self.assertIsInstance(result, DriverGap)
        self.assertIn("order_id", result.reason)
        self.assertEqual(result.driver, "http")

    def test_get_carries_body_fields_in_the_query(self) -> None:
        operation = op("http", access="read", method="GET", path="/orders")
        step = StepSpec(actor=11, body={"status": "open"}, query={"per_page": 10})
        request = one(drivers.drive(operation, step, BEARER, "", {}))
        self.assertEqual(parse_query(request.path), {"status": "open", "per_page": "10"})
        self.assertIsNone(request.body)
        self.assertNotIn("Content-Type", request.headers)

    def test_post_carries_body_as_json_and_query_on_the_url(self) -> None:
        operation = op("http", method="POST", path="/orders")
        step = StepSpec(actor=11, body={"name": "v"}, query={"dry": 1})
        request = one(drivers.drive(operation, step, BEARER, "", {}))
        self.assertEqual(request.path, "/orders?dry=1")
        self.assertEqual(request.headers["Content-Type"], "application/json")
        self.assertEqual(json.loads(request.body), {"name": "v"})

    def test_post_with_no_body_sends_an_empty_object_and_delete_sends_none(self) -> None:
        post = one(drivers.drive(op("http", method="POST", path="/orders"), StepSpec(actor=11), BEARER, "", {}))
        self.assertEqual(post.body, b"{}")
        delete = one(drivers.drive(op("http", method="DELETE", path="/orders/1"), StepSpec(actor=11), BEARER, "", {}))
        self.assertIsNone(delete.body)

    def test_carrier_from_inputs_overrides_the_operation_which_overrides_the_verb(self) -> None:
        operation = op("http", method="POST", path="/search", carrier="query")
        step = StepSpec(actor=11, body={"q": "x"})
        by_operation = one(drivers.drive(operation, step, BEARER, "", {}))
        self.assertEqual(by_operation.path, "/search?q=x")
        self.assertIsNone(by_operation.body)
        by_inputs = one(drivers.drive(operation, step, BEARER, "", {"carrier": "json"}))
        self.assertEqual(json.loads(by_inputs.body), {"q": "x"})

    def test_multipart_carrier_sends_file_placeholders_as_file_parts(self) -> None:
        operation = op("http", method="POST", path="/imports", carrier="multipart")
        step = StepSpec(actor=11, body={"file": "{{file:xlsx}}", "columns": ["a", "b"], "map": {"k": 1}})
        request = one(drivers.drive(operation, step, BEARER, "", {}))
        self.assertTrue(request.headers["Content-Type"].startswith("multipart/form-data; boundary="))
        body = request.body.decode()
        self.assertIn('name="file"; filename="upload.xlsx"', body)
        self.assertIn('name="columns[]"\r\n\r\na\r\n', body)
        self.assertIn('name="map"\r\n\r\n{"k": 1}\r\n', body)
        self.assertTrue(body.endswith(f"--{http_driver.MULTIPART_BOUNDARY}--\r\n"))
        # Deterministic: the same step encodes to the same bytes.
        again = one(drivers.drive(operation, step, BEARER, "", {}))
        self.assertEqual(again.body, request.body)

    def test_multipart_needs_a_dict_body(self) -> None:
        operation = op("http", method="POST", path="/imports", carrier="multipart")
        result = drivers.drive(operation, StepSpec(actor=11, body=[1, 2]), BEARER, "", {})
        self.assertIsInstance(result, DriverGap)
        self.assertIn("multipart", result.reason)

    def test_prefix_is_prepended_to_the_template(self) -> None:
        operation = op("http", access="read", method="GET", path="orders/{order}", prefix="/legacy/rest")
        request = one(drivers.drive(operation, StepSpec(actor=11, params={"order": 5}), BEARER, "", {}))
        self.assertEqual(request.path, "/legacy/rest/orders/5")

    def test_cookie_credential_and_extra_headers(self) -> None:
        operation = op("http", method="POST", path="/orders")
        credential = {"cookie": "session=abc", "headers": {"X-Tenant": "t1"}}
        request = one(drivers.drive(operation, StepSpec(actor=11, headers={"Accept": "text/html"}), credential, "", {}))
        self.assertEqual(request.headers["Cookie"], "session=abc")
        self.assertEqual(request.headers["X-Tenant"], "t1")
        self.assertEqual(request.headers["Accept"], "text/html")
        self.assertNotIn("Cookie", request.to_json()["headers"])

    def test_anonymous_step_has_no_credential_header(self) -> None:
        operation = op("http", access="read", method="GET", path="/orders")
        request = one(drivers.drive(operation, StepSpec(actor=None, role="anonymous"), None, "", {}))
        self.assertNotIn("Authorization", request.headers)
        self.assertNotIn("Cookie", request.headers)

    def test_actor_without_credential_is_a_gap_not_an_anonymous_request(self) -> None:
        operation = op("http", access="read", method="GET", path="/orders")
        result = drivers.drive(operation, StepSpec(actor=11), None, "", {})
        self.assertIsInstance(result, DriverGap)
        self.assertIn("11", result.reason)
        empty = drivers.drive(operation, StepSpec(actor=11), {"headers": {}}, "", {})
        self.assertIsInstance(empty, DriverGap)

    def test_raw_body_is_sent_verbatim(self) -> None:
        operation = op("http", method="PUT", path="/blob")
        step = StepSpec(actor=11, body="<xml/>", headers={"Content-Type": "text/xml"})
        request = one(drivers.drive(operation, step, BEARER, "", {}))
        self.assertEqual(request.body, b"<xml/>")
        self.assertEqual(request.headers["Content-Type"], "text/xml")

    def test_step_method_and_path_override_the_operation(self) -> None:
        operation = op("http", method="POST", path="/orders")
        request = one(drivers.drive(operation, StepSpec(actor=11, method="patch", path="/orders/{id}", params={"id": 3}), BEARER, "", {}))
        self.assertEqual((request.method, request.path), ("PATCH", "/orders/3"))

    def test_a_plain_dict_step_is_read_like_a_stepspec(self) -> None:
        operation = op("http", access="read", method="GET", path="/orders/{order}")
        request = one(drivers.drive(operation, {"actor": 11, "params": {"order": 9}}, BEARER, "", {}))
        self.assertEqual(request.path, "/orders/9")


class FormPostDriverTests(unittest.TestCase):
    def test_read_puts_the_selector_and_fields_on_the_url(self) -> None:
        operation = op("form_post", access="read", path="/dispatch/", key="listOrders", selector={"get": "action", "post": "xrq"})
        step = StepSpec(actor=11, params={"project": 21}, body={"status": "open"}, query={"page": 2})
        request = one(drivers.drive(operation, step, COOKIE, "", {}))
        self.assertEqual(request.method, "GET")
        self.assertEqual(urllib.parse.urlsplit(request.path).path, "/dispatch/")
        self.assertEqual(parse_query(request.path), {"action": "listOrders", "project": "21", "status": "open", "page": "2"})
        self.assertIsNone(request.body)
        self.assertEqual(request.headers["Cookie"], "session=abc")

    def test_write_puts_the_selector_and_fields_in_the_form_and_dispatch_params_on_the_url(self) -> None:
        operation = op("form_post", access="write", path="/dispatch/", key="saveOrder", selector={"get": "action", "post": "xrq"})
        step = StepSpec(actor=11, params={"order": 31}, body={"name": "v", "tags": ["a", "b"]}, query={"project": 21})
        request = one(drivers.drive(operation, step, COOKIE, "", {}))
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.path, "/dispatch/?project=21")
        self.assertEqual(request.headers["Content-Type"], "application/x-www-form-urlencoded")
        self.assertEqual(request.headers["X-Requested-With"], "XMLHttpRequest")
        form = urllib.parse.parse_qs(request.body.decode())
        self.assertEqual(form, {"xrq": ["saveOrder"], "order": ["31"], "name": ["v"], "tags[]": ["a", "b"]})

    def test_selector_names_default_to_action(self) -> None:
        operation = op("form_post", access="write", key="saveOrder")
        request = one(drivers.drive(operation, StepSpec(actor=11), COOKIE, "", {}))
        self.assertEqual(request.path, "/")
        self.assertEqual(urllib.parse.parse_qs(request.body.decode()), {"action": ["saveOrder"]})

    def test_raw_body_moves_the_selector_to_the_url(self) -> None:
        operation = op("form_post", access="write", path="/dispatch/", key="upload", selector={"post": "xrq"})
        step = StepSpec(actor=11, body=b"\x00\x01", headers={"Content-Type": "application/octet-stream"})
        request = one(drivers.drive(operation, step, COOKIE, "", {}))
        self.assertEqual(parse_query(request.path), {"xrq": "upload"})
        self.assertEqual(request.body, b"\x00\x01")

    def test_unresolved_key_is_a_gap(self) -> None:
        operation = op("form_post", access="read", path="/dispatch/", key_expr="SiteMap::ORDERS")
        result = drivers.drive(operation, StepSpec(actor=11), COOKIE, "", {})
        self.assertIsInstance(result, DriverGap)
        self.assertIn("SiteMap::ORDERS", result.reason)
        self.assertEqual(result.driver, "form_post")

    def test_actor_without_credential_is_a_gap(self) -> None:
        operation = op("form_post", access="read", key="listOrders")
        self.assertIsInstance(drivers.drive(operation, StepSpec(actor=11), None, "", {}), DriverGap)
        anonymous = one(drivers.drive(operation, StepSpec(actor=None), None, "", {}))
        self.assertNotIn("Cookie", anonymous.headers)


class ShellDriverTests(unittest.TestCase):
    def test_command_argv(self) -> None:
        operation = op("shell", access="command", mode="command", target="reports:rebuild")
        request = one(drivers.drive(operation, StepSpec(args={"--project": 21}), None, "", {}))
        self.assertEqual(request.kind, "shell")
        self.assertEqual(request.method, "command")
        self.assertEqual(request.argv, ["command", "reports:rebuild", '{"--project": 21}'])

    def test_job_argv_carries_the_drain_flag(self) -> None:
        operation = op("shell", access="queued", mode="job", target="App\\Jobs\\SyncOrder")
        drained = one(drivers.drive(operation, StepSpec(args={"orderId": 3}, drain=True), None, "", {}))
        self.assertEqual(drained.argv, ["job", "App\\Jobs\\SyncOrder", '{"orderId": 3}', "drain"])
        left = one(drivers.drive(operation, StepSpec(args={"orderId": 3}, drain=False), None, "", {}))
        self.assertEqual(left.argv[-1], "leave")

    def test_schedule_argv_names_the_event_and_when(self) -> None:
        operation = op("shell", access="scheduled", mode="schedule", target="App\\Console\\Kernel::nightly")
        due = one(drivers.drive(operation, StepSpec(when="due"), None, "", {}))
        self.assertEqual(due.argv, ["schedule", '["App\\\\Console\\\\Kernel::nightly"]', "due"])
        not_due = one(drivers.drive(operation, StepSpec(when="not-due"), None, "", {}))
        self.assertEqual(not_due.argv[-1], "not-due")
        self.assertEqual(one(drivers.drive(operation, StepSpec(), None, "", {})).argv[-1], "due")

    def test_dynamic_scheduler_needs_event_names_else_gap(self) -> None:
        operation = op("shell", access="scheduled", mode="schedule", target="App\\Schedulers\\Digest::schedule", events="dynamic")
        result = drivers.drive(operation, StepSpec(when="due"), None, "", {})
        self.assertIsInstance(result, DriverGap)
        self.assertIn("inputs.args.events", result.reason)
        named = one(drivers.drive(operation, StepSpec(when="due", args={"events": ["digest:daily"]}), None, "", {}))
        self.assertEqual(named.argv, ["schedule", '["digest:daily"]', "due"])

    def test_unknown_mode_and_unknown_when_are_gaps(self) -> None:
        self.assertIsInstance(drivers.drive(op("shell", mode="cron", target="x"), StepSpec(), None, "", {}), DriverGap)
        operation = op("shell", access="scheduled", mode="schedule", target="x")
        self.assertIsInstance(drivers.drive(operation, StepSpec(when="soon"), None, "", {}), DriverGap)


class WebhookDriverTests(unittest.TestCase):
    SECRET = {"secret": "shared-secret"}

    def test_signs_the_body_under_the_secret(self) -> None:
        operation = op("webhook", access="webhook", path="/hooks/orders", signature_header="X-Hook-Signature")
        step = StepSpec(role="webhook", body={"event": "order.paid", "id": 7}, signature="valid")
        request = one(drivers.drive(operation, step, self.SECRET, "", {}))
        self.assertEqual((request.method, request.path), ("POST", "/hooks/orders"))
        self.assertEqual(request.headers["Content-Type"], "application/json")
        expected = hmac.new(b"shared-secret", request.body, "sha256").hexdigest()
        self.assertEqual(request.headers["X-Hook-Signature"], expected)
        self.assertEqual(json.loads(request.body), {"event": "order.paid", "id": 7})

    def test_invalid_signature_is_well_formed_but_does_not_verify(self) -> None:
        operation = op("webhook", access="webhook", path="/hooks/orders")
        body = {"event": "order.paid"}
        valid = one(drivers.drive(operation, StepSpec(body=body, signature="valid"), self.SECRET, "", {}))
        invalid = one(drivers.drive(operation, StepSpec(body=body, signature="invalid"), self.SECRET, "", {}))
        self.assertEqual(valid.body, invalid.body)
        self.assertNotEqual(valid.headers["X-Signature"], invalid.headers["X-Signature"])
        self.assertEqual(len(valid.headers["X-Signature"]), len(invalid.headers["X-Signature"]))
        self.assertTrue(all(c in "0123456789abcdef" for c in invalid.headers["X-Signature"]))

    def test_duplicate_delivery_is_byte_identical_including_the_delivery_id(self) -> None:
        operation = op("webhook", access="webhook", path="/hooks/orders", delivery_header="X-Delivery-Id")
        step = StepSpec(body={"event": "order.paid"}, signature="valid")
        first = one(drivers.drive(operation, step, self.SECRET, "", {}))
        second = one(drivers.drive(operation, step, self.SECRET, "", {}))
        self.assertEqual(first.to_json(), second.to_json())
        self.assertEqual(first.headers["X-Delivery-Id"], hashlib.sha256(first.body).hexdigest()[:32])

    def test_base64_encoding_with_prefix_and_algorithm(self) -> None:
        operation = op("webhook", access="webhook", path="/hooks/orders", algorithm="sha1", encoding="base64", prefix="sha1=")
        request = one(drivers.drive(operation, StepSpec(body="raw payload"), self.SECRET, "", {}))
        digest = base64.b64encode(hmac.new(b"shared-secret", b"raw payload", "sha1").digest()).decode()
        self.assertEqual(request.headers["X-Signature"], "sha1=" + digest)
        self.assertEqual(request.body, b"raw payload")

    def test_no_secret_is_a_gap_and_path_params_are_filled(self) -> None:
        operation = op("webhook", access="webhook", path="/hooks/{account_id}")
        self.assertIsInstance(drivers.drive(operation, StepSpec(body={}), None, "", {}), DriverGap)
        missing = drivers.drive(operation, StepSpec(body={}), self.SECRET, "", {})
        self.assertIsInstance(missing, DriverGap)
        self.assertIn("account_id", missing.reason)
        filled = one(drivers.drive(operation, StepSpec(body={}, params={"account": 4}), self.SECRET, "", {}))
        self.assertEqual(filled.path, "/hooks/4")

    def test_unknown_algorithm_is_configuration_and_raises(self) -> None:
        operation = op("webhook", access="webhook", path="/hooks", algorithm="rot13")
        with self.assertRaises(ValueError):
            drivers.drive(operation, StepSpec(body={}), self.SECRET, "", {})


class DriveDispatchTests(unittest.TestCase):
    def test_operation_without_driver_is_a_gap_carrying_the_loader_note(self) -> None:
        operation = Operation(id="hook-1", kind="model-hook", access="hook", driver=None, notes=["observed through deltas"])
        result = drivers.drive(operation, StepSpec(), None, "", {})
        self.assertIsInstance(result, DriverGap)
        self.assertEqual(result.reason, "observed through deltas")
        self.assertIsNone(result.driver)

    def test_unknown_driver_name_raises_naming_the_seam(self) -> None:
        with self.assertRaises(ValueError) as caught:
            drivers.drive(op("grpc", method="X"), StepSpec(), None, "", {})
        self.assertIn("grpc", str(caught.exception))
        self.assertIn("dotted.module:callable", str(caught.exception))

    def test_a_consumer_driver_comes_in_through_the_plugin_seam(self) -> None:
        operation = op(f"{__name__}:plugin_drive", access="read", path="/x")
        result = drivers.drive(operation, StepSpec(actor=11), BEARER, "http://oracle", {"k": 1})
        self.assertEqual(one(result).path, "/x?plugin=1&k=1")
        self.assertEqual(one(result).headers["Authorization"], "Bearer token-for-actor-11")

    def test_fill_path_reports_missing_and_collapses_slashes(self) -> None:
        path, missing = drivers.fill_path("/a/{b}/{c?}/", {"b": "x y"})
        self.assertEqual((path, missing), ("/a/x%20y", []))
        path, missing = drivers.fill_path("/a/{b_id}", {})
        self.assertEqual((path, missing), ("/a/{b_id}", ["b_id"]))


if __name__ == "__main__":
    unittest.main()
