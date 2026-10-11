"""The ``form_post`` driver: a front controller that dispatches on a field.

Some applications route every request through one URL and pick the action
from a selector field: ``GET /dispatch/?action=<key>`` for a read,
``POST /dispatch/`` with ``action=<key>`` among the form fields for a write.
The path names nothing; the key is the operation.

``Operation.request`` keys read here:

* ``path``      the front controller URL (default ``/``).
* ``key``       the selector value for this operation (required). A loader that
                could not resolve it (a constant it could not read) leaves it
                unset and the step is a gap naming ``key_expr`` when given.
* ``selector``  ``{"get": <field>, "post": <field>}`` -- the field name the
                controller reads on each verb (default ``action`` for both).
* ``method``    optional; otherwise ``GET`` when ``op.access == "read"``, else
                ``POST``.

For a read, the step's params, body fields and query all go on the URL beside
the selector. For a write, the step's params and body fields are form-encoded
in the body with the selector, and the step's ``query`` alone goes on the URL
(the dispatch parameters a controller reads from ``$_GET`` while it reads the
fields from ``$_POST``). A ``str`` / ``bytes`` body is sent raw: the selector
then moves to the URL because there is no form to carry it, and the
``Content-Type`` is the step's or ``application/octet-stream``.

Refusals: no ``key`` is a gap; a step with an actor and no usable credential
is a gap (a session-cookie controller silently treats it as logged out, which
would record the anonymous behaviour under the actor's name).
"""

from __future__ import annotations

from collections.abc import Mapping

from . import DriverGap, Request, actor_gap, credential_headers, form_encode, step_value, with_query

NAME = "form_post"
DEFAULT_SELECTOR = {"get": "action", "post": "action"}


def drive(op, step, credential: Mapping | None, base_url: str, inputs: Mapping):
    request = op.request
    key = request.get("key")
    if not key:
        expression = request.get("key_expr")
        detail = f" (expression {expression!r} is not a declared constant)" if expression else ""
        return DriverGap(f"operation {op.id!r} has no selector key for the form_post driver{detail}", NAME)
    gap = actor_gap(step, credential, NAME)
    if gap:
        return gap

    selector = {**DEFAULT_SELECTOR, **(request.get("selector") or {})}
    path = str(step_value(step, "path") or request.get("path") or "/")
    method = str(step_value(step, "method") or request.get("method") or ("GET" if op.access == "read" else "POST")).upper()
    headers = {**credential_headers(credential), **step_value(step, "headers", {})}
    query = dict(step_value(step, "query", {}))
    body = step_value(step, "body")
    fields = dict(step_value(step, "params", {}))

    if method == "GET":
        if isinstance(body, Mapping):
            fields.update(body)
        elif body is not None:
            return DriverGap(f"a GET dispatch cannot carry a body of type {type(body).__name__}", NAME)
        url = with_query(path, {selector["get"]: key, **fields, **query})
        return [Request("http", "GET", url, headers, None)]

    headers.setdefault("X-Requested-With", "XMLHttpRequest")
    if isinstance(body, (bytes, str)):
        headers.setdefault("Content-Type", "application/octet-stream")
        url = with_query(path, {selector["post"]: key, **fields, **query})
        return [Request("http", method, url, headers, body if isinstance(body, bytes) else body.encode())]

    if body is not None and not isinstance(body, Mapping):
        return DriverGap(f"form body must be a dict, str or bytes, got {type(body).__name__}", NAME)
    fields.update(body or {})
    headers["Content-Type"] = "application/x-www-form-urlencoded"
    form = {selector["post"]: key, **fields}
    return [Request("http", method, with_query(path, query), headers, form_encode(form))]
