"""Lossless bridge from review fixtures to the authoritative claims IR."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from capcov.claims import (Atom, Bundle, Claim, Constant, Evidence, EvidenceEffect,
                           EvidenceMapping, RelationDecl, Rule, Variable,
                           bundle_from_json, canonical_dict)  # noqa: E402

ROOT = Path(__file__).parent / "corpus"


def _term(value: Any, type_name: str) -> dict[str, Any]:
    return {"value": value, "type": type_name}


def bundle_payload(fixture: dict[str, Any]) -> dict[str, Any]:
    schema = json.loads((ROOT / "schema-v1.json").read_text(encoding="utf-8"))
    if schema.get("program_schema_version") != 1 or fixture.get("program_schema_version") != 1 or "rules" not in fixture:
        raise ValueError("fixture lacks required schema-v1 semantic program declarations")
    relations = []
    for name, declaration in schema["relations"].items():
        relations.append({"name": name, **{k: v for k, v in declaration.items() if k != "arg_order"}})
    for declaration in fixture.get("program_relations", ()):
        if set(declaration) != {"name", "columns", "modality", "polarity", "binding", "primitive", "producer_classes", "context_indices", "completes", "finite", "nonempty", "compatibility_targets", "compatibility_context_indices"}:
            raise ValueError("program relation declaration has unknown or missing fields")
        if any(r["name"] == declaration["name"] for r in relations): raise ValueError("duplicate program relation")
        relations.append(declaration)
        schema["relations"][declaration["name"]] = declaration

    def terms(entry: dict[str, Any]) -> list[dict[str, Any]]:
        declaration = schema["relations"][entry["relation"]]
        columns = declaration["columns"]
        expected_order = [column["name"] for column in columns]
        if entry.get("arg_order") != expected_order:
            raise ValueError(f"{entry.get('id', entry['relation'])} arg_order does not match its relation")
        if len(entry.get("args", ())) != len(columns):
            raise ValueError(f"{entry.get('id', entry['relation'])} arity does not match its relation")
        return [_term(entry["args"][index], column["type"])
                for index, column in enumerate(columns)]

    all_entries = [*fixture["facts"], *fixture["assumptions"]]
    facts = [{"relation": entry["relation"], "terms": terms(entry)} for entry in all_entries]
    evidence = [{"id": entry["id"], "relation": entry["relation"], "terms": terms(entry),
                 "context": {k: dict(zip(entry["arg_order"], entry["args"]))[k] for k in schema["relations"][entry["relation"]]["context_indices"]},
                 "source": entry.get("source", entry.get("provenance", {}).get("source", "fixture")),
                 "depends_on": entry.get("provenance", {}).get("depends_on", []),
                 "kind": "assumption" if entry in fixture["assumptions"] else "fact"} for entry in all_entries]
    claims = []
    for entry in fixture["claims"]:
        declaration = schema["relations"][entry["relation"]]
        context = {key: entry["context"][key] for key in declaration["context_indices"]}
        claims.append({"id": entry["id"], "relation": entry["relation"], "terms": terms(entry), "context": context, "quantifier": entry["quantifier"], "domain": entry["domain"]})
    # Compile only declarations supplied by the fixture input.  No expected
    # verdicts, fixture IDs, or relation-name heuristics participate here.
    rules, mappings, diagnostics = [], [], []
    declarations = fixture.get("rules", ())
    claim_by_id = {entry["id"]: entry for entry in fixture["claims"]}
    rule_names = set()
    for declaration in declarations:
        if set(declaration) - {"name", "claim_id", "premises", "head_relation", "head_bindings", "aggregation"}:
            raise ValueError("rule declaration has unknown or missing fields")
        if not isinstance(declaration["name"], str) or not declaration["name"] or declaration["name"] in rule_names:
            raise ValueError("rule names must be unique non-empty strings")
        rule_names.add(declaration["name"])
        if declaration["claim_id"] not in claim_by_id or not isinstance(declaration["premises"], list) or not declaration["premises"]:
            raise ValueError("rule declaration claim_id/premises are invalid")
        claim_entry = claim_by_id[declaration["claim_id"]]
        cdecl = schema["relations"][claim_entry["relation"]]
        head_relation = declaration.get("head_relation", claim_entry["relation"])
        claim_values = dict(zip((c["name"] for c in cdecl["columns"]), claim_entry["args"]))
        body = []
        for premise in declaration["premises"]:
            if set(premise) - {"relation", "bindings", "predicates", "scope"} or "relation" not in premise or "bindings" not in premise:
                raise ValueError("premise declaration has unknown or missing fields")
            if not isinstance(premise["bindings"], list) or any(set(binding) != {"claim_column", "evidence_column", "type"} for binding in premise["bindings"]):
                raise ValueError("binding declaration has unknown or missing fields")
            if len({b["claim_column"] for b in premise["bindings"]}) != len(premise["bindings"]) or len({b["evidence_column"] for b in premise["bindings"]}) != len(premise["bindings"]):
                raise ValueError("duplicate rule bindings")
            if len({p["column"] for p in premise.get("predicates", ())}) != len(premise.get("predicates", ())):
                raise ValueError("duplicate rule predicates")
            if {b["evidence_column"] for b in premise["bindings"]} & {p["column"] for p in premise.get("predicates", ())}:
                raise ValueError("binding and predicate cannot target the same column")
            if not premise["bindings"] and premise.get("scope") != "global":
                raise ValueError("unbound premise requires explicit global scope")
            if premise.get("scope") not in (None, "global", "claim"):
                raise ValueError("unknown premise scope")
            if any(set(predicate) != {"column", "type", "value"} for predicate in premise.get("predicates", ())):
                raise ValueError("predicate declaration has unknown or missing fields")
            sdecl = schema["relations"][premise["relation"]]
            if premise.get("scope", "claim") == "claim":
                claim_context = set(cdecl["context_indices"])
                source_context = set(sdecl["context_indices"]) & claim_context
                if not source_context.issubset({b["claim_column"] for b in premise["bindings"]}):
                    raise ValueError("claim-scoped premise is missing context bindings")
            bindings = {binding["evidence_column"]: binding for binding in premise["bindings"]}
            predicates = {predicate["column"]: predicate for predicate in premise.get("predicates", ())}
            claim_columns = {column["name"]: column for column in cdecl["columns"]}
            source_terms = []
            for col in sdecl["columns"]:
                binding = bindings.get(col["name"])
                predicate = predicates.get(col["name"])
                if predicate:
                    if predicate["type"] != col["type"]: raise ValueError("typed predicate does not match evidence column")
                    source_terms.append(_term(predicate["value"], predicate["type"]))
                elif binding:
                    if binding["claim_column"] not in claim_columns or binding["type"] != col["type"] or binding["type"] != claim_columns[binding["claim_column"]]["type"]:
                        raise ValueError("typed rule binding does not match claim/evidence columns")
                    source_terms.append({"variable": f"_{col['name']}"} if head_relation != claim_entry["relation"] else _term(claim_values[binding["claim_column"]], binding["type"]))
                else: source_terms.append({"variable": f"_{col['name']}"})
            body.append({"relation": premise["relation"], "terms": source_terms})
        head_decl = next((r for r in relations if r["name"] == head_relation), None)
        if not head_decl: raise ValueError("unknown rule head relation")
        head_bindings = {b["head_column"]: b for b in declaration.get("head_bindings", ())}
        head_terms = []
        for column in head_decl["columns"]:
            binding = head_bindings.get(column["name"])
            if binding:
                if binding["type"] != column["type"]: raise ValueError("typed head binding mismatch")
                head_terms.append({"variable": f"_{binding['premise_column']}"})
            elif head_relation == claim_entry["relation"]:
                head_terms.append(_term(dict(zip((c["name"] for c in cdecl["columns"]), claim_entry["args"]))[column["name"]], column["type"]))
            else: raise ValueError("derived head requires complete bindings")
        rule = {"name": declaration["name"], "head": {"relation": head_relation, "terms": head_terms}, "body": body}
        if declaration.get("head_bindings"):
            for binding in declaration["head_bindings"]:
                if set(binding) != {"head_column", "premise_relation", "premise_column", "type"}: raise ValueError("invalid head binding")
        if declaration.get("aggregation"):
            agg = declaration["aggregation"]
            if set(agg) != {"name", "relation", "group_by", "value_variable", "operator", "domain", "closure_witness"}: raise ValueError("invalid aggregation declaration")
            rule["aggregation"] = agg
        rules.append(rule)
    for claim_entry in fixture["claims"]:
        cdecl = schema["relations"][claim_entry["relation"]]
        for mapping in claim_entry.get("mappings", ()):
            mapping = dict(mapping); mapping["claim_relation"] = claim_entry["relation"]; mapping["claim_id"] = claim_entry["id"]
            mappings.append(mapping)
        for diagnostic in claim_entry.get("diagnostics", ()):
            diagnostic = dict(diagnostic); diagnostic["claim_id"] = claim_entry["id"]
            diagnostics.append(diagnostic)
    outputs = []
    for output in fixture.get("outputs", ()):
        output = dict(output)
        if "claim_id" not in output: raise ValueError("output template requires claim_id")
        outputs.append(output)
    semantic_inputs = {
        "fixture_id": fixture["id"],
        "context": fixture["context"],
        "facts": [dict(entry) for entry in fixture["facts"]],
        "assumptions": [dict(entry) for entry in fixture["assumptions"]],
        "claims": [dict(entry) for entry in fixture["claims"]],
    }
    return {"schema_version": 1, "relations": relations, "facts": facts, "evidence": evidence,
            "mappings": mappings, "diagnostics": diagnostics, "outputs": outputs, "rules": rules, "claims": claims,
            "diagnostic_policy": {"missing_premises": "unresolved", "inconsistent_premises": "inconsistent-premises",
                                   "out_of_scope": "out-of-scope", "forbidden_evidence": "invalid-input",
                                   "revocation": "refutation", "completeness": "required"},
            "metadata": {"fixture_id": fixture["id"], "semantic_inputs": semantic_inputs}}


def load_fixture(path: Path) -> Any:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    return bundle_from_json(bundle_payload(fixture), validate=True)


def canonical_metadata(bundle: Any) -> dict[str, Any]:
    """Expose the parser's frozen metadata in JSON-compatible form for tests."""
    return dict(canonical_dict(bundle)["metadata"])
