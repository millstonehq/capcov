"""The ``shell`` driver: commands, queued jobs and scheduled events as ``argv``.

The engine runs no console of its own: a command, a job class or a scheduled
event is executed by the consumer's oracle, in-process against the disposable
stores, through whatever helper the consumer ships (``oracle.run_shell``). This
driver builds the protocol-neutral argument list that helper reads; the helper
prepends its own interpreter and path.

``Operation.request`` keys read here:

* ``mode``    ``command`` | ``job`` | ``schedule`` (required).
* ``target``  the command signature, the job class, or the schedule symbol
              (required).
* ``events``  for ``schedule`` only: ``"dynamic"`` when the target registers
              its events at run time, so the names must come from the step's
              ``args.events``; otherwise the target itself is the one event.

The ``argv`` contract, per mode (``args`` is the step's ``args`` dict as JSON,
sorted keys so a recorded request is stable):

    command   ["command", <target>, <args-json>]
    job       ["job", <target>, <args-json>, "drain" | "leave"]
    schedule  ["schedule", <names-json>, "due" | "not-due"]

``drain`` / ``leave`` mirrors the step's ``drain`` flag so the helper can leave
a dispatched job on the queue for the ``undrained`` cell. ``when`` defaults to
``due``.

Refusals: an unknown mode is a gap naming it; a dynamic scheduler with no
``args.events`` is a gap saying where to name them. The step's ``method`` /
``path`` override ``mode`` / ``target`` when set, matching the http driver.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from . import DriverGap, Request, step_value

NAME = "shell"
MODES = ("command", "job", "schedule")


def _args_json(args: Mapping) -> str:
    return json.dumps(dict(args), sort_keys=True)


def drive(op, step, credential: Mapping | None, base_url: str, inputs: Mapping):
    request = op.request
    mode = step_value(step, "method") or request.get("mode")
    target = step_value(step, "path") or request.get("target")
    if not mode or not target:
        return DriverGap(f"operation {op.id!r} request has no mode/target for the shell driver", NAME)
    args = dict(step_value(step, "args", {}))

    if mode == "command":
        return [Request("shell", "command", f"command {target}", argv=["command", str(target), _args_json(args)])]
    if mode == "job":
        drain = "drain" if step_value(step, "drain", True) else "leave"
        return [Request("shell", "job", f"job {target}", argv=["job", str(target), _args_json(args), drain])]
    if mode == "schedule":
        when = str(step_value(step, "when") or "due")
        if when not in ("due", "not-due"):
            return DriverGap(f"schedule step has when={when!r}; have due, not-due", NAME)
        names = [str(target)]
        if request.get("events") == "dynamic":
            names = [str(name) for name in (args.get("events") or [])]
            if not names:
                return DriverGap(
                    f"{target} registers its events dynamically; name them in inputs.args.events", NAME
                )
        return [Request("shell", "schedule", f"schedule {names} {when}", argv=["schedule", json.dumps(names), when])]
    return DriverGap(f"shell driver has no mode {mode!r}; have {', '.join(MODES)}", NAME)
