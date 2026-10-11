"""Compare the specified, implemented and exercised sets, and gate the result.

Findings, each over capability IDs (`ids.covers` decides when a shorter ID
applies to a longer one):

    specified-unimplemented   a specified ID no implemented ID covers
    implemented-unspecified   an implemented ID the specification neither names nor
                              contains, or a discovered surface no entry maps
                              (reported as `unmapped:<surface>`)
    specified-unexercised     an implemented, specified ID no exercised ID covers.
                              Unimplemented IDs are not repeated here.
    exercised-failing         an exercised ID with at least one failing record

`exercised-unspecified` (an exercised ID outside the specification) is reported
under `informational` and does not gate.

The gate fails on every finding, and on every adapter `unresolved` entry (kind
"unresolved"), unless an `[[capabilities.exclude]]` entry names it:

    capability = "area/requirement/*"      fnmatch over the finding's ID
    kind       = "specified-unexercised"   optional; absent waives every kind
    reason     = "why this stays open"     required

An exclusion with no reason does not apply and fails. An exclusion that waives
nothing fails too, so the list cannot outlive what it explains.
"""

from __future__ import annotations

import fnmatch

from . import ids

SPECIFIED_UNIMPLEMENTED = "specified-unimplemented"
IMPLEMENTED_UNSPECIFIED = "implemented-unspecified"
SPECIFIED_UNEXERCISED = "specified-unexercised"
EXERCISED_FAILING = "exercised-failing"
UNRESOLVED = "unresolved"
KINDS = (
    SPECIFIED_UNIMPLEMENTED,
    IMPLEMENTED_UNSPECIFIED,
    SPECIFIED_UNEXERCISED,
    EXERCISED_FAILING,
)
EXCLUDABLE = (*KINDS, UNRESOLVED)


def reconcile(specified: dict, implemented: dict, exercised: dict) -> dict:
    spec_ids = [c["id"] for c in specified["capabilities"]]
    impl = {c["id"]: c for c in implemented["capabilities"]}
    exer = {c["id"]: c for c in exercised["capabilities"]}

    def covered_by(capability: str, pool: dict) -> list[str]:
        return [key for key in pool if ids.covers(key, capability)]

    findings: dict[str, list[dict]] = {kind: [] for kind in KINDS}
    rows = []
    for capability in spec_ids:
        implemented_by = covered_by(capability, impl)
        exercised_by = covered_by(capability, exer)
        passes = sum(exer[k]["pass"] for k in exercised_by)
        fails = sum(exer[k]["fail"] for k in exercised_by)
        if not implemented_by:
            findings[SPECIFIED_UNIMPLEMENTED].append({"id": capability})
            status = "unimplemented"
        elif not exercised_by:
            findings[SPECIFIED_UNEXERCISED].append({"id": capability, "implemented_by": implemented_by})
            status = "unexercised"
        else:
            status = "failing" if fails else "passing"
        rows.append(
            {
                "id": capability,
                "status": status,
                "implemented_by": implemented_by,
                "exercised": {"pass": passes, "fail": fails},
            }
        )

    for key in sorted(impl):
        if not any(ids.covers(key, s) or ids.covers(s, key) for s in spec_ids):
            findings[IMPLEMENTED_UNSPECIFIED].append({"id": key, "surfaces": impl[key]["surfaces"]})
    for surface in implemented.get("unmapped_surfaces", []):
        findings[IMPLEMENTED_UNSPECIFIED].append({"id": f"unmapped:{surface}", "surfaces": [surface]})

    for key in sorted(exer):
        if exer[key]["fail"]:
            findings[EXERCISED_FAILING].append(
                {
                    "id": key,
                    "fail": exer[key]["fail"],
                    "pass": exer[key]["pass"],
                    "samples": exer[key]["fail_samples"],
                }
            )

    unresolved = []
    for label, doc in (("specified", specified), ("implemented", implemented), ("exercised", exercised)):
        for entry in doc.get("unresolved", []):
            unresolved.append({"set": label, **entry})

    exercised_unspecified = [
        {"id": key}
        for key in sorted(exer)
        if not any(ids.covers(key, s) or ids.covers(s, key) for s in spec_ids)
    ]
    return {
        "kind": "capabilities-reconciliation",
        "summary": {
            "specified": len(spec_ids),
            "implemented": len(impl),
            "exercised": len(exer),
            "passing": sum(1 for r in rows if r["status"] == "passing"),
            **{kind: len(findings[kind]) for kind in KINDS},
            "unresolved": len(unresolved),
        },
        "rows": rows,
        "findings": findings,
        "informational": {"exercised-unspecified": exercised_unspecified},
        "unresolved": unresolved,
    }


def gate(reconciliation: dict, exclusions: list[dict]) -> dict:
    failures: list[dict] = []
    valid: list[dict] = []
    for index, item in enumerate(exclusions):
        subject = item.get("capability") or f"capabilities.exclude[{index}]"
        if not item.get("capability"):
            failures.append(_fail("malformed-exclusion", subject, "capability is required"))
        elif not str(item.get("reason") or "").strip():
            failures.append(
                _fail(
                    "incomplete-exclusion",
                    subject,
                    "an exclusion with no reason is a silenced check, so it does not apply",
                )
            )
        elif item.get("kind") is not None and item["kind"] not in EXCLUDABLE:
            failures.append(
                _fail("malformed-exclusion", subject, f"kind must be one of {', '.join(EXCLUDABLE)}")
            )
        else:
            valid.append(item)

    used: set[int] = set()
    excluded: list[dict] = []

    def judge(kind: str, subject: str, detail: str) -> None:
        for number, item in enumerate(valid):
            if fnmatch.fnmatchcase(subject, item["capability"]) and item.get("kind") in (None, kind):
                used.add(number)
                excluded.append({"kind": kind, "id": subject, "reason": item["reason"]})
                return
        failures.append(_fail(kind, subject, detail))

    for kind in KINDS:
        for finding in reconciliation["findings"][kind]:
            judge(kind, finding["id"], _explain(kind, finding))
    for entry in reconciliation["unresolved"]:
        judge(UNRESOLVED, entry["id"], f"{entry['set']} adapter: {entry['reason']}")

    for number, item in enumerate(valid):
        if number not in used:
            failures.append(
                _fail(
                    "unused-exclusion",
                    item["capability"],
                    "nothing in the reconciliation matches this exclusion; delete it",
                )
            )
    return {
        "status": "fail" if failures else "pass",
        "failures": failures,
        "excluded": excluded,
    }


def _fail(rule: str, subject: str, detail: str) -> dict:
    return {"rule": rule, "subject": subject, "detail": detail}


def _explain(kind: str, finding: dict) -> str:
    if kind == SPECIFIED_UNIMPLEMENTED:
        return "the specification names this and no discovered surface is mapped to it"
    if kind == IMPLEMENTED_UNSPECIFIED:
        return "discovery found this and the specification does not name it"
    if kind == SPECIFIED_UNEXERCISED:
        return "implemented, and no log record shows it running"
    first = (finding.get("samples") or [{}])[0]
    return (
        f"{finding['fail']} failing record(s), {finding['pass']} passing. "
        f"First failure: {first.get('at', '?')}: {first.get('line', '')}"
    )


def summary_text(reconciliation: dict, verdict: dict | None = None) -> str:
    s = reconciliation["summary"]
    lines = [
        f"specified {s['specified']}, implemented {s['implemented']}, exercised {s['exercised']}, "
        f"passing {s['passing']}",
    ]
    for kind in KINDS:
        found = reconciliation["findings"][kind]
        lines.append(f"{kind}: {len(found)}")
        lines.extend(f"  {f['id']}" for f in found[:20])
        if len(found) > 20:
            lines.append(f"  ... {len(found) - 20} more in the JSON")
    if reconciliation["unresolved"]:
        lines.append(f"unresolved: {len(reconciliation['unresolved'])}")
        lines.extend(f"  [{u['set']}] {u['id']}: {u['reason']}" for u in reconciliation["unresolved"][:20])
    informational = reconciliation["informational"]["exercised-unspecified"]
    if informational:
        lines.append(f"exercised-unspecified (not gated): {len(informational)}")
    if verdict is not None:
        lines.append(
            f"gate: {verdict['status'].upper()}, {len(verdict['failures'])} unexplained, "
            f"{len(verdict['excluded'])} excluded with a reason"
        )
        lines.extend(f"  {f['rule']}: {f['subject']}: {f['detail']}" for f in verdict["failures"][:40])
        lines.append("A green gate is a floor: it says the three sets agree, not that the system is correct.")
    return "\n".join(lines)
