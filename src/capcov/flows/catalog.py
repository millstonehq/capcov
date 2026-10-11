"""Derive reviewable flow families and a work queue from an evidence graph.

Generation accounts for source; it never supplies its own expected business
results. Every candidate keeps its missing oracle and target binding visible.
"""

from __future__ import annotations

from collections import Counter, defaultdict, deque

from .model import digest

TRAVERSE = {
    "offers-action",
    "invokes",
    "calls",
    "triggers",
    "opens",
    "page-reference",
    "has-branch",
    "external-effect",
    "guarded-by",
    "reads-field",
    "writes-state",
    "has-field",
    "reads",
    "edits",
    "creates",
    "deletes",
    "writes-field",
    "changes-input",
    "resolves-to",
}
ROOT_KINDS = {
    "surface",
    "action",
    "workflow",
    "schedule",
    "grant",
    "platform-operation",
    "configuration",
}


def build_catalog(inventory: dict) -> dict:
    nodes = {n["id"]: n for n in inventory["obligations"]}
    if len(nodes) != len(inventory["obligations"]) or not nodes:
        raise ValueError("catalog requires nonempty unique obligations")
    edges = [e for g in inventory.get("graphs", []) for e in g["edges"]]
    outgoing: dict[str, list[dict]] = defaultdict(list)
    incoming: dict[str, list[dict]] = defaultdict(list)
    for edge in edges:
        outgoing[edge["from"]].append(edge)
        incoming[edge["to"]].append(edge)
    families = []
    ownership: dict[str, list[str]] = defaultdict(list)
    roots = sorted(n for n, node in nodes.items() if node["kind"] in ROOT_KINDS)

    # Cycles terminate at visited
    # nodes; no depth cap silently drops a transitive effect.
    for root in roots:
        reached = {root}
        queue = deque([root])
        # Grants remain an independent actor/operation case; reaching an allowed
        # screen does not make this profile own every downstream action's oracle.
        while queue:
            current = queue.popleft()
            for edge in outgoing[current]:
                target = edge["to"]
                if edge["relation"] in TRAVERSE and target in nodes and target not in reached:
                    reached.add(target)
                    queue.append(target)
        obligations = sorted(reached)
        for name in obligations:
            ownership[name].append(root)
        navigation = [
            nodes[e["from"]].get("path", [])
            for e in incoming[root]
            if e["relation"] == "navigates-to" and e["from"] in nodes
        ]
        branches = [n for n in obligations if nodes[n]["kind"] == "branch-candidate"]
        effects = [n for n in obligations if nodes[n]["kind"] in {"effect", "platform-operation"}]
        predicates = [n for n in obligations if nodes[n]["kind"] == "predicate"]
        kind = nodes[root]["kind"]
        required_cases = ["observable-success"]
        if branches or predicates:
            required_cases += ["each-branch-outcome", "guard-rejection"]
        if kind == "grant":
            required_cases += ["granted-actor", "ungranted-actor"]
        if kind == "schedule":
            required_cases += ["not-due", "due", "repeat-invocation"]
        if effects:
            required_cases += ["external-success", "external-failure"]
        families.append(
            {
                "id": root,
                "kind": kind,
                "label": nodes[root].get("label", root),
                "source": nodes[root]["source"],
                "navigation": navigation,
                "actor": nodes[root].get("actor"),
                "operation": nodes[root].get("operation"),
                "reachable_obligations": len(obligations),
                "branch_count": len(branches),
                "effect_count": len(effects),
                "predicate_count": len(predicates),
                "required_cases": required_cases,
                "status": "candidate",
                "missing": ["confirmed-business-outcome", "fixture", "target-binding"],
            }
        )

    # A function nobody reaches is still accounted for as a finding. It is not
    # declared dead or converted into an invented user flow.
    unrooted = [
        n
        for n in sorted(nodes)
        if n not in ownership
        and nodes[n]["kind"] not in {"navigation", "profile", "role", "unresolved"}
    ]
    # Writer -> shared state field -> filtered screen is an inferred prerequisite,
    # not a control-flow edge. Retain the exact function-call chain as evidence.
    writers: dict[str, list[dict]] = defaultdict(list)
    readers: dict[str, list[dict]] = defaultdict(list)
    for edge in edges:
        if edge["relation"] == "writes-state":
            writers[edge["to"]].append(edge)
        elif edge["relation"] == "reads-field":
            readers[edge["to"]].append(edge)
    dependencies = []
    for field in sorted(set(writers) & set(readers)):
        for write in writers[field]:
            for read in readers[field]:
                predicate = nodes.get(read["from"], {})
                target = predicate.get("surface")
                if not target:
                    continue
                # Only actual workflow triggers are proposed as prerequisites.
                # Other reports sharing a transitive helper are not user intents.
                for owner in ownership.get(write["from"], []):
                    if nodes[owner]["kind"] != "workflow":
                        continue
                    dependencies.append(
                        {
                            "from": owner,
                            "to": target,
                            "via": field,
                            "status": "candidate",
                            "reason": "writes state read by the report filter",
                            "evidence": [write["source"], read["source"]],
                        }
                    )
    dependencies = list({digest(d): d for d in dependencies}.values())
    census = [row for g in inventory.get("graphs", []) for row in g["census"]]
    residual = [n for n in nodes.values() if n["kind"] == "unresolved"]
    structural_gaps = [
        item
        for item in residual
        if item.get("reason", "").startswith(
            ("unsupported", "unrecognised", "missing ", "unbalanced", "unclosed")
        )
    ]
    files_accounted = all(row["classified"] for row in inventory.get("source_census", []))
    indexes = {name: index for index, name in enumerate(roots)}
    return {
        "version": 1,
        "inventory_sha256": digest(inventory),
        "scope": inventory["scope"],
        "families": families,
        "memberships": {
            name: [indexes[owner] for owner in owners] for name, owners in sorted(ownership.items())
        },
        "outcome_requirements": {
            name: node["outcomes"] for name, node in sorted(nodes.items()) if node.get("outcomes")
        },
        "dependencies": sorted(dependencies, key=lambda d: (d["from"], d["to"], d["via"])),
        "unrooted": unrooted,
        "unresolved": residual,
        "census": census,
        "summary": {
            "obligations": len(nodes),
            "flow_families": len(families),
            "rooted_obligations": len(ownership),
            "unrooted_obligations": len(unrooted),
            "unresolved_boundaries": len(residual),
            "candidate_dependencies": len(dependencies),
            "kinds": dict(sorted(Counter(n["kind"] for n in nodes.values()).items())),
        },
        "declarations_accounted": bool(census)
        and all(c["declared"] == c["classified"] for c in census)
        and not structural_gaps
        and files_accounted,
        "references_resolved": not any(g["unresolved"] for g in inventory.get("graphs", [])),
        "source_files_accounted": files_accounted,
        "behaviorally_complete": False,
    }


def render_catalog(catalog: dict, focus: str | None = None) -> str:
    families = [f for f in catalog["families"] if focus is None or focus.lower() in f["id"].lower()]
    ids = {f["id"] for f in families}
    focused_indexes = {
        index for index, family in enumerate(catalog["families"]) if family["id"] in ids
    }
    reachable = {
        name for name, owners in catalog["memberships"].items() if focused_indexes & set(owners)
    }
    lines = [
        f"# {catalog['scope']}",
        "",
        "This is a source-derived flow catalog; expected outcomes and browser bindings "
        "remain review work.",
        "",
        f"Declarations accounted: {catalog['declarations_accounted']}. "
        f"References resolved: {catalog['references_resolved']}. "
        "Behavioral completeness: false.",
        "",
        "## Discovery census",
        "",
        "| Section | Declared | Classified |",
        "|---|---:|---:|",
    ]
    census_groups: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for row in catalog["census"]:
        key = row["section"]
        if "fields " in key:
            key = key.split("fields ")[0] + "form declaration blocks"
        census_groups[key][0] += row["declared"]
        census_groups[key][1] += row["classified"]
    for key, (declared, classified) in census_groups.items():
        lines.append(f"| {key} | {declared} | {classified} |")
    lines.extend(["", "## Candidate dependencies", ""])
    grouped: dict[tuple[str, str], set[str]] = defaultdict(set)
    for d in catalog["dependencies"]:
        if focus is None or d["from"] in reachable or d["to"] in ids:
            grouped[d["from"], d["via"]].add(d["to"])
    for (owner, field), targets in sorted(grouped.items()):
        sample = ", ".join(sorted(targets)[:4])
        lines.append(
            f"- `{owner}` → `{field}` → {len(targets)} report(s): {sample}. "
            "Candidate dependency; confirm behavior before planning."
        )
    lines.extend(["", "## Flow families", ""])
    for family in families:
        lines.extend([f"### {family['id']}", ""])
        if family["navigation"]:
            lines.append(
                "Navigation: " + "; ".join(" → ".join(p) for p in family["navigation"]) + "."
            )
        lines.append(f"Source: {family['source']['file']}:{family['source']['line']}.")
        lines.append(
            f"Obligations: {family['reachable_obligations']}; branches: {family['branch_count']}; "
            f"external effects: {family['effect_count']}."
        )
        lines.extend(
            [
                "Required cases: " + ", ".join(family["required_cases"]) + ".",
                "Missing: " + ", ".join(family["missing"]) + ".",
                "",
            ]
        )
    lines.extend(["## Unresolved source findings", ""])
    for item in catalog["unresolved"]:
        if focus is None or focus.lower() in item.get("owner", item["id"]).lower():
            lines.append(
                f"- {item.get('owner', item['id'])}: {item.get('reason', 'adapter boundary')} "
                f"({item['source']['line']})"
            )
    return "\n".join(lines) + "\n"
