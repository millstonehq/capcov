"""Drivers: one operation plus one vector step -> the concrete requests to send.

A driver speaks one protocol. The template says WHAT a cell exercises (which
actor, which params, which body); the driver says HOW that reaches the system
under test. Four ship, and each reads its own keys from ``Operation.request``,
which the consumer's operations loader fills:

* ``http``       a routed HTTP endpoint. ``{method, path}`` with ``{param}`` /
                 ``{param?}`` placeholders, an optional ``prefix`` prepended to
                 every path, an optional ``carrier`` (``json`` | ``query`` |
                 ``multipart``). Path params are filled by name from the step's
                 params with the suffixes ``_id`` / ``_uuid`` / ``Id`` stripped
                 (``{order_id}`` <- ``order``).
* ``form_post``  a front-controller that dispatches on a selector FIELD rather
                 than a path: ``{path, key, selector: {get, post}}``. Reads are
                 ``GET path?<get-selector>=key&...``; writes are ``POST path``
                 with ``<post-selector>=key`` and the fields form-encoded, the
                 step's query on the URL. A ``str``/``bytes`` body is sent raw
                 and the selector moves to the URL.
* ``shell``      a console-side invocation: ``{mode, target}`` where mode is
                 ``command`` | ``job`` | ``schedule``. The driver builds a
                 protocol-neutral ``argv``; the consumer's oracle ``run_shell``
                 prepends its own helper (the engine has no PHP, no artisan).
* ``webhook``    an inbound HMAC-signed delivery: ``{path, signature_header,
                 algorithm, encoding, prefix, delivery_header}``. The step's
                 ``signature`` selects a valid or an invalid signature; a
                 duplicate is the template repeating the step, and the driver
                 is deterministic, so both deliveries are byte-identical.

The one interface::

    drive(op, step, actor_credential, base_url, inputs) -> list[Request] | DriverGap

``actor_credential`` is whatever the recorder resolved for the step's actor:
``{"bearer": token}`` and/or ``{"cookie": "name=value"}`` and/or
``{"headers": {...}}`` for HTTP, ``{"secret": ...}`` for a webhook, ``None``
for an anonymous or system step. Credentials are applied here and redacted by
``Request.to_json``; they never reach an artifact.

A driver never fakes. When a step cannot be built mechanically it returns a
``DriverGap`` naming the reason: a path parameter with no value, a selector
key the loader could not resolve, an actor with no credential, a mode the
driver has no protocol for. The recorder records gaps beside vectors, so a
cell that could not be driven is counted, never silently absent.

A driver name the engine does not know is a wiring error, not a per-vector
gap, and ``drive`` raises naming this seam. A consumer's own protocol comes in
the same way an adapter does: ``driver = "dotted.module:callable"`` resolves
through ``capcov.adapters.import_plugin_callable`` and is called with the same
five arguments.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Mapping

from ..schema import DriverGap, Request

PARAM = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(\??)\}")
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def step_value(step, name: str, default=None):
    """Read one field of a step, whether it is a ``StepSpec`` or a plain dict.

    A recorded vector round-trips through JSON, and a consumer's own planner may
    hand the driver a dict; both carry the same field names. ``None`` in either
    form means "not given" and yields the default.
    """
    if isinstance(step, Mapping):
        value = step.get(name)
    else:
        value = getattr(step, name, None)
    return default if value is None else value


def fill_path(template: str, params: Mapping) -> tuple[str, list[str]]:
    """Substitute ``{param}`` / ``{param?}`` placeholders. Returns (path, missing).

    A param is matched by its exact name first, then with the ``_id``,
    ``_uuid`` and ``Id`` suffixes stripped, so a manifest that says
    ``{"order": 31}`` fills ``{order_id}``. An optional placeholder with no
    value becomes empty; a required one is reported in ``missing`` and left in
    place so the caller can name it.
    """
    missing: list[str] = []

    def substitute(match: re.Match) -> str:
        name, optional = match.group(1), match.group(2)
        for alias in (name, name.removesuffix("_id"), name.removesuffix("_uuid"), name.removesuffix("Id")):
            if alias in params and params[alias] is not None:
                return urllib.parse.quote(str(params[alias]), safe="")
        if optional:
            return ""
        missing.append(name)
        return match.group(0)

    path = PARAM.sub(substitute, template)
    path = re.sub(r"/{2,}", "/", path).rstrip("/") or "/"
    return path, missing


def with_query(path: str, query: Mapping) -> str:
    """Append a query string, joining onto one that is already there."""
    if not query:
        return path
    return path + ("&" if "?" in path else "?") + urllib.parse.urlencode(_flatten(query), doseq=True)


def _flatten(fields: Mapping) -> list[tuple[str, str]]:
    """Form-field pairs: lists become ``key[]`` repeats, dicts are JSON-encoded."""
    flat: list[tuple[str, str]] = []
    for key, value in fields.items():
        if isinstance(value, (list, tuple)):
            for item in value:
                flat.append((f"{key}[]", "" if item is None else str(item)))
        elif isinstance(value, Mapping):
            flat.append((key, json.dumps(value, sort_keys=True)))
        else:
            flat.append((key, "" if value is None else str(value)))
    return flat


def form_encode(fields: Mapping) -> bytes:
    """``application/x-www-form-urlencoded`` bytes for a dict of fields."""
    return urllib.parse.urlencode(_flatten(fields)).encode()


def credential_headers(credential: Mapping | None) -> dict:
    """The headers a credential contributes. Empty for none.

    The credential is protocol-neutral so the same manifest serves every driver:
    ``bearer`` becomes ``Authorization: Bearer ...``, ``cookie`` becomes
    ``Cookie: ...``, and ``headers`` is applied verbatim for any other scheme.
    """
    headers: dict = {}
    if not credential:
        return headers
    if credential.get("bearer"):
        headers["Authorization"] = "Bearer " + str(credential["bearer"])
    if credential.get("cookie"):
        headers["Cookie"] = str(credential["cookie"])
    headers.update(credential.get("headers") or {})
    return headers


def actor_gap(step, credential: Mapping | None, driver: str) -> DriverGap | None:
    """The gap for a step whose actor has no usable credential, else ``None``.

    An anonymous step (no actor) legitimately carries no credential. A step
    WITH an actor and no credential, or a credential that yields no header,
    would be sent anonymously and recorded as if it were the actor's -- the
    kind of silent substitution the recorder exists to refuse.
    """
    actor = step_value(step, "actor")
    if actor is None:
        return None
    if not credential:
        return DriverGap(f"no credential for actor {actor!r}", driver)
    if not credential_headers(credential):
        return DriverGap(
            f"credential for actor {actor!r} carries no bearer, cookie or headers", driver
        )
    return None


# The modules below import the helpers above, so they are imported last: by the
# time they run, every helper name is bound on this partially initialised
# package. Keep new helpers above this line.
from . import form_post as _form_post  # noqa: E402
from . import http as _http  # noqa: E402
from . import shell as _shell  # noqa: E402
from . import webhook as _webhook  # noqa: E402

DRIVERS = {
    "http": _http.drive,
    "form_post": _form_post.drive,
    "shell": _shell.drive,
    "webhook": _webhook.drive,
}


def resolve(name: str):
    """Driver name -> callable. A dotted ``module:callable`` is a consumer's own.

    Unknown names raise: a driver that does not exist must not turn every
    vector of an operation into a gap that reads like the operation's fault.
    """
    if name in DRIVERS:
        return DRIVERS[name]
    if ":" in name:
        from ...adapters import import_plugin_callable

        return import_plugin_callable(name)
    raise ValueError(
        f"unknown vectors driver {name!r}; have: {', '.join(sorted(DRIVERS))} "
        "or a plugin as 'dotted.module:callable'"
    )


def drive(op, step, actor_credential: Mapping | None, base_url: str, inputs: Mapping | None = None):
    """One interface for every driver; ``DriverGap`` when the step cannot be built."""
    name = getattr(op, "driver", None)
    if not name:
        notes = list(getattr(op, "notes", None) or [])
        return DriverGap(notes[-1] if notes else f"no driver for kind {op.kind!r}", None)
    return resolve(name)(op, step, actor_credential, base_url, inputs or {})


__all__ = [
    "DRIVERS",
    "PARAM",
    "READ_METHODS",
    "DriverGap",
    "Request",
    "actor_gap",
    "credential_headers",
    "drive",
    "fill_path",
    "form_encode",
    "resolve",
    "step_value",
    "with_query",
]
