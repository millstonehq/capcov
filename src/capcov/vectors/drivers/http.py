"""The ``http`` driver: a routed endpoint with path parameters and one carrier.

``Operation.request`` keys read here:

* ``method``   the HTTP verb (required).
* ``path``     the route template, ``{param}`` / ``{param?}`` placeholders
               (required; ``uri`` is accepted as an alias for loaders that
               copy a router's own field name).
* ``prefix``   prepended to every path, for a router mounted under one
               (``/api/v2``); the template stays what the route file declares.
* ``carrier``  how the body travels: ``json`` (default for a write),
               ``query`` (default for GET / HEAD / OPTIONS: body fields become
               query parameters), ``multipart`` (``multipart/form-data`` for a
               body carrying a ``{{file:ext}}`` placeholder). The inputs entry
               may override it under the same key.

The step's ``method`` / ``path`` override the operation's when set; its
``params`` fill the placeholders, ``query`` goes on the URL, ``headers`` are
applied last so an input can override ``Accept``.

Refusals: a required placeholder with no value in the step's params is a gap
naming it (the manifest owns ids; guessing one would record a 404 as the
operation's behaviour). A step with an actor and no usable credential is a gap
(see ``actor_gap``). A ``multipart`` carrier with a body that is not a dict is
a gap: there is no mechanical way to split it into parts.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from . import READ_METHODS, DriverGap, Request, actor_gap, credential_headers, fill_path, step_value, with_query

NAME = "http"

# Fixed so a recorded multipart request is byte-stable across runs; a real
# client randomises it, and nothing here depends on that.
MULTIPART_BOUNDARY = "capcov-vectors-boundary-7f1e"
FILE_PLACEHOLDER_PREFIX = "{{file:"
FILE_PLACEHOLDER_CONTENT = b"capcov fixture file\n"


def carrier_for(method: str, request: Mapping, inputs: Mapping) -> str:
    """The body carrier: inputs override the operation, which overrides the default."""
    given = inputs.get("carrier") or request.get("carrier")
    if given:
        return str(given)
    return "query" if method in READ_METHODS else "json"


def multipart_encode(fields: Mapping) -> bytes:
    """``multipart/form-data`` with ``{{file:ext}}`` values sent as file parts.

    Lists become ``key[]`` repeats and nested dicts are JSON-encoded, the same
    flattening the form carrier uses. Line endings are CRLF as the format
    requires; the file content is a fixed fixture, so the recorded request is
    deterministic.
    """
    parts: list[bytes] = []
    for key, value in fields.items():
        values = value if isinstance(value, (list, tuple)) else [value]
        name = f"{key}[]" if isinstance(value, (list, tuple)) else key
        for item in values:
            if isinstance(item, str) and item.startswith(FILE_PLACEHOLDER_PREFIX) and item.endswith("}}"):
                extension = item[len(FILE_PLACEHOLDER_PREFIX):-2] or "bin"
                parts.append(
                    f'--{MULTIPART_BOUNDARY}\r\nContent-Disposition: form-data; name="{name}"; '
                    f'filename="upload.{extension}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode()
                    + FILE_PLACEHOLDER_CONTENT
                    + b"\r\n"
                )
                continue
            if isinstance(item, Mapping):
                text = json.dumps(item, sort_keys=True)
            else:
                text = "" if item is None else str(item)
            parts.append(
                f'--{MULTIPART_BOUNDARY}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
                + text.encode()
                + b"\r\n"
            )
    return b"".join(parts) + f"--{MULTIPART_BOUNDARY}--\r\n".encode()


def drive(op, step, credential: Mapping | None, base_url: str, inputs: Mapping):
    request = op.request
    method = str(step_value(step, "method") or request.get("method") or "").upper()
    template = step_value(step, "path") or request.get("path") or request.get("uri")
    if not method or not template:
        return DriverGap(f"operation {op.id!r} request has no method/path for the http driver", NAME)
    gap = actor_gap(step, credential, NAME)
    if gap:
        return gap

    prefix = str(request.get("prefix") or "").rstrip("/")
    path, missing = fill_path(prefix + "/" + str(template).lstrip("/"), step_value(step, "params", {}))
    if missing:
        return DriverGap(
            f"no value for path parameter(s) {missing} in manifest/inputs for {method} {template}", NAME
        )

    headers = {"Accept": "application/json", **credential_headers(credential), **step_value(step, "headers", {})}
    query = dict(step_value(step, "query", {}))
    body = step_value(step, "body")
    carrier = carrier_for(method, request, inputs)

    if isinstance(body, (bytes, str)):
        # Raw body: the input owns the encoding and says so in its headers.
        headers.setdefault("Content-Type", "application/octet-stream")
        return [Request("http", method, with_query(path, query), headers, body if isinstance(body, bytes) else body.encode())]

    if carrier == "query":
        if isinstance(body, Mapping):
            query = {**body, **query}
        elif body is not None:
            return DriverGap(f"query carrier cannot carry a body of type {type(body).__name__}", NAME)
        return [Request("http", method, with_query(path, query), headers, None)]

    if body is None and method in (READ_METHODS | {"DELETE"}):
        # No body given and the verb does not expect one: send none, so a
        # recorded DELETE matches what a real client sends.
        return [Request("http", method, with_query(path, query), headers, None)]

    if carrier == "multipart":
        if not isinstance(body, Mapping):
            return DriverGap(f"multipart carrier needs a dict body, got {type(body).__name__}", NAME)
        headers["Content-Type"] = f"multipart/form-data; boundary={MULTIPART_BOUNDARY}"
        return [Request("http", method, with_query(path, query), headers, multipart_encode(body))]

    if carrier != "json":
        return DriverGap(f"unknown carrier {carrier!r}; have json, query, multipart", NAME)
    headers["Content-Type"] = "application/json"
    payload = {} if body is None else body
    return [Request("http", method, with_query(path, query), headers, json.dumps(payload).encode())]
