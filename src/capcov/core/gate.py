"""Fail the build on anything unexplained -- and on any explanation that has
stopped being true.

An exemption list that only grows is a lie with a filename. Three rules keep
this one honest:

1. An entity in a failing cell with no exemption fails.
2. An exemption whose entity is no longer in the cell it exempts ALSO fails,
   naming itself for deletion. This is the rule usually missing, and without it
   the file rots into fiction while the build stays green.
3. Every exemption carries a reason and a date. An exemption with no reason is
   a silenced check.

The four cells are not the only obligations. A route/spec adapter that could not
resolve something -- an empty document, a dynamic (non-literal) route path, a
boundary that is the honest limit of static reading -- names it in `unresolved`
rather than dropping it. An unresolved obligation of surface/spec kind fails the
gate unless exempted, exactly as an unmapped obligation did: it is why empty
input has no green denominator. Its exemption uses the same cell-typed, dated
scheme (cell = "unresolved"), the sole exemption scheme. `excluded_surfaces`
(surfaces the query saw and a verb allowlist deliberately dropped) are legitimate
narrowing: reported so N stays legible, never auto-failed.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from .reconcile import FAILING_CELLS

# The pseudo-cell an exemption names to waive an `unresolved` obligation. It is
# not one of the four reconcile cells (an unresolved obligation never entered the
# diff), but it rides the same cell-typed, dated exemption scheme so there is one
# exemption format, not two.
UNRESOLVED_CELL = "unresolved"


def _gates(entry: dict) -> bool:
    """Whether an `unresolved` entry is a gate failure or reported-only.

    Surface/spec-kind material -- what a route/spec adapter could not resolve --
    gates: this is the honest denominator, and it is why empty input has no green
    denominator. That is the default, so an entry that names nothing special still
    counts (unresolved is never silently dropped). A runtime probe's own
    unevaluable / unattributed material is carried for legibility but is not a
    static surface/spec obligation, so it marks itself reported-only with
    `gating = False`.
    """
    return entry.get("gating", True) is not False


def _unresolved_key(entry: dict) -> str:
    """A stable identity an exemption can name.

    Prefer the obligation's own id (boundary obligations and dynamic-route
    obligations carry one); otherwise compose a deterministic key from the adapter
    and kind so even an adapter-produced-no-surface entry is addressable.
    """
    stated = entry.get("id")
    if stated:
        return stated
    return f"unresolved:{entry.get('adapter')}:{entry.get('kind')}"


def _explain_unresolved(entry: dict) -> str:
    reason = entry.get("reason") or "no reason recorded"
    return (
        f"static reading did not resolve this to a surface or spec "
        f"({entry.get('kind')}): {reason}. It is part of the denominator, not "
        "outside it. Either the target declares it in capcov.toml, the adapter "
        "grows to read it, or you record in the exemptions file (cell "
        f'"{UNRESOLVED_CELL}") why it stays unresolved.'
    )


class Failure:
    __slots__ = ("rule", "subject", "detail")

    def __init__(self, rule: str, subject: str, detail: str) -> None:
        self.rule, self.subject, self.detail = rule, subject, detail

    def __str__(self) -> str:
        return f"{self.rule}: {self.subject}\n    {self.detail}"


def load_exemptions(path: Path | None) -> tuple[dict[str, dict], list[Failure]]:
    if path is None or not path.exists():
        return {}, []
    doc = tomllib.loads(path.read_text())
    out: dict[str, dict] = {}
    problems: list[Failure] = []
    for item in doc.get("exempt", []):
        key = item.get("entity") or item.get("surface")
        if not key:
            problems.append(
                Failure("malformed-exemption", "<no entity or surface>", str(item))
            )
            continue
        missing = [f for f in ("reason", "date", "cell") if not item.get(f)]
        if missing:
            problems.append(
                Failure(
                    "incomplete-exemption",
                    key,
                    f"missing {', '.join(missing)}. An exemption with no reason is a "
                    "silenced check; an exemption with no cell exempts everything.",
                )
            )
            continue
        out[key] = item
    return out, problems


def gate(coverage: dict, exemptions_path: Path | None) -> list[Failure]:
    exemptions, failures = load_exemptions(exemptions_path)
    # Runtime assertion failures are not structural coverage gaps. Exempting a
    # route cannot waive its required behavioral checks.
    flow_failures = coverage.get("flows_failures", [])
    if not isinstance(flow_failures, list):
        failures.append(Failure("invalid-flow-evidence", "browser", "flows_failures must be a list"))
    else:
        for entry in flow_failures:
            if not isinstance(entry, dict) or not entry.get("id") or not entry.get("reason"):
                failures.append(Failure("invalid-flow-evidence", "browser", "malformed flow failure"))
            else:
                failures.append(Failure("required-flow-failed", entry["id"], entry["reason"]))
    used: set[str] = set()
    explained_runtime_only: set[str] = set()

    for row in coverage["rows"]:
        entity, cell = row["entity"], row["cell"]
        exemption = exemptions.get(entity)
        if cell in FAILING_CELLS:
            if exemption is None:
                failures.append(
                    Failure(
                        f"unexplained-{cell}",
                        entity,
                        _explain(row),
                    )
                )
            elif exemption["cell"] != cell:
                used.add(entity)
                failures.append(
                    Failure(
                        "stale-exemption",
                        entity,
                        f"exempted as {exemption['cell']!r}, now {cell!r}. "
                        "The reason recorded no longer describes what is happening.",
                    )
                )
            else:
                used.add(entity)
                if cell == "runtime_only":
                    explained_runtime_only.add(entity)
        elif exemption is not None:
            used.add(entity)
            failures.append(
                Failure(
                    "obsolete-exemption",
                    entity,
                    f"exempted as {exemption['cell']!r} on {exemption['date']}, now "
                    f"{cell!r}. Delete the exemption -- it is no longer needed, and "
                    "a list that only grows stops being read.",
                )
            )

    for surface in coverage["unknown_surfaces"]:
        # A divergence in Software Reflexion Model terms (Murphy 1995; see
        # ADR-0001): runtime reached a surface the declared model does not have.
        name = surface["surface"]
        exemption = exemptions.get(name)
        if exemption is None:
            failures.append(
                Failure(
                    "undiscovered-surface",
                    name,
                    "runtime reached this surface and discover never found it. "
                    f"It touched {', '.join(surface['entities'])}. Either the "
                    "adapter is missing an entry-point kind, or the target has "
                    "not declared it in capcov.toml.",
                )
            )
        else:
            used.add(name)

    for orphan in coverage["orphan_tests"]:
        # A generated/dynamic table can be observed by a legitimate test without
        # appearing in the static inventory. The exact, validated runtime-only
        # exemption already accounts for that observation. Keep it in the report
        # without failing it twice; stale/missing-row exemptions cannot reach here.
        if orphan["entity"] in explained_runtime_only:
            continue
        failures.append(
            Failure(
                "orphan-test",
                orphan["test"],
                f"exercises {orphan['entity']!r}, which is not declared in this inventory. "
                "It may be generated, dynamic, or removed; inspect the source and runtime "
                "evidence before classifying it.",
            )
        )

    for entry in coverage.get("unresolved", []):
        if not _gates(entry):
            # Reported-only (a runtime probe's unevaluable/unattributed material).
            # Carried into coverage for legibility, but not a static surface/spec
            # obligation, so it does not fail the gate. It is still visible in the
            # coverage artifact -- N stays legible.
            continue
        key = _unresolved_key(entry)
        exemption = exemptions.get(key)
        if exemption is None:
            failures.append(
                Failure("unresolved-obligation", key, _explain_unresolved(entry))
            )
        elif exemption["cell"] != UNRESOLVED_CELL:
            used.add(key)
            failures.append(
                Failure(
                    "stale-exemption",
                    key,
                    f"exempted as {exemption['cell']!r}, but this is an unresolved "
                    f'obligation (cell "{UNRESOLVED_CELL}"). The reason recorded no '
                    "longer describes what is happening.",
                )
            )
        else:
            used.add(key)

    for key in sorted(set(exemptions) - used):
        failures.append(
            Failure(
                "unused-exemption",
                key,
                "nothing in the coverage report matches this exemption. It names "
                "something that is gone; delete it.",
            )
        )

    return failures


def _explain(row: dict) -> str:
    # Reflexion-inspired structural categories, not behavioral or dead-code proof.
    if row["cell"] == "static_only":
        return (
            f"reachable from {', '.join(row['static_surfaces'][:3])}"
            f"{'...' if len(row['static_surfaces']) > 3 else ''} and no exercise "
            "touched it. This is a coverage gap: write the test."
        )
    if row["cell"] == "runtime_only":
        return (
            f"observed at {', '.join(row['runtime_surfaces'][:3])} and static "
            "analysis found no path. Either the adapter missed an entry point, "
            "or the access is dynamic -- check the blind-spot list."
        )
    return (
        "declared in the schema but neither statically reached nor observed in this scope. "
        "Investigate discovery or execution gaps, or record a justified disposition."
    )
