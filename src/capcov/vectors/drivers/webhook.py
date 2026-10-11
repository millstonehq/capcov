"""The ``webhook`` driver: an inbound delivery with an HMAC over the body.

A webhook endpoint is a write whose caller is not a user but a peer system,
authenticated by a signature of the payload under a shared secret. The three
template cells are ``valid-signature``, ``invalid-signature`` and
``duplicate``; this driver builds all three from one operation.

``Operation.request`` keys read here:

* ``path``              the endpoint (required); ``{param}`` placeholders are
                        filled from the step's params like the http driver.
* ``method``            default ``POST``.
* ``signature_header``  the header carrying the signature (default
                        ``X-Signature``).
* ``algorithm``         a ``hashlib`` digest name (default ``sha256``).
* ``encoding``          ``hex`` (default) or ``base64``.
* ``prefix``            prepended to the encoded digest (``"sha256="`` for
                        the GitHub style; default empty).
* ``delivery_header``   optional; when set, carries a delivery id derived from
                        the body, so a repeated step is a repeated delivery id
                        as well as a repeated payload.

The credential is ``{"secret": ...}``. The step's ``signature`` is ``valid``
(default) or ``invalid``; an invalid signature is the same digest computed
under a different key, so it is well-formed and the endpoint's signature check,
not its parser, is what rejects it. A duplicate is the template repeating the
step: everything here is a pure function of (secret, body, config), so the two
requests are identical bytes.

Refusals: no secret is a gap (a webhook driven unsigned would record the
endpoint's rejection as the operation's behaviour); a missing path parameter is
a gap; an algorithm ``hashlib`` does not know raises, since that is
configuration, not a per-vector condition.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping

from . import DriverGap, Request, fill_path, step_value, with_query

NAME = "webhook"
INVALID_KEY_SUFFIX = b"-not-the-secret"


def sign(secret: bytes, body: bytes, algorithm: str = "sha256", encoding: str = "hex", prefix: str = "") -> str:
    """The signature header value for ``body`` under ``secret``."""
    if algorithm not in hashlib.algorithms_available:
        raise ValueError(f"webhook driver: hashlib has no algorithm {algorithm!r}")
    digest = hmac.new(secret, body, algorithm).digest()
    if encoding == "hex":
        encoded = digest.hex()
    elif encoding == "base64":
        encoded = base64.b64encode(digest).decode()
    else:
        raise ValueError(f"webhook driver: encoding must be hex or base64, got {encoding!r}")
    return prefix + encoded


def encode_body(body) -> bytes:
    if body is None:
        return b"{}"
    if isinstance(body, bytes):
        return body
    if isinstance(body, str):
        return body.encode()
    return json.dumps(body, sort_keys=True).encode()


def drive(op, step, credential: Mapping | None, base_url: str, inputs: Mapping):
    request = op.request
    template = step_value(step, "path") or request.get("path")
    if not template:
        return DriverGap(f"operation {op.id!r} request has no path for the webhook driver", NAME)
    secret = (credential or {}).get("secret")
    if not secret:
        return DriverGap(f"no webhook secret for operation {op.id!r} (credential needs {{'secret': ...}})", NAME)
    path, missing = fill_path("/" + str(template).lstrip("/"), step_value(step, "params", {}))
    if missing:
        return DriverGap(f"no value for path parameter(s) {missing} for webhook {template}", NAME)

    method = str(step_value(step, "method") or request.get("method") or "POST").upper()
    body = step_value(step, "body")
    payload = encode_body(body)
    key = secret.encode() if isinstance(secret, str) else bytes(secret)
    if step_value(step, "signature", "valid") == "invalid":
        key = key + INVALID_KEY_SUFFIX
    signature = sign(
        key,
        payload,
        algorithm=str(request.get("algorithm") or "sha256"),
        encoding=str(request.get("encoding") or "hex"),
        prefix=str(request.get("prefix") or ""),
    )

    headers = {"Accept": "application/json", **step_value(step, "headers", {})}
    if not isinstance(body, (bytes, str)):
        headers.setdefault("Content-Type", "application/json")
    headers[str(request.get("signature_header") or "X-Signature")] = signature
    delivery_header = request.get("delivery_header")
    if delivery_header:
        headers[str(delivery_header)] = hashlib.sha256(payload).hexdigest()[:32]
    url = with_query(path, step_value(step, "query", {}))
    return [Request("http", method, url, headers, payload)]
