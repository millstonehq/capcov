"""Lossless bridge from replay review cases plus rules-replay-v1.json to the IR.

Mirrors ``tests/claim_semantics/static_rules/adapter.py``: the replay rule
pack is kept in raw IR JSON wire form and merged verbatim; a case file
contributes facts, assumptions, claims, per-claim diagnostics/mappings and
output templates, and the pack contributes every relation declaration and
rule.  The case format is the static one (``facts``/``assumptions``/``claims``
/``outputs``/``expected``), so ``expected.json`` tooling is shared; the only
difference is the case ``context`` (``run``, ``model``, ``index`` instead of
``index``).  Nothing here evaluates rules or reads a case's ``expected`` table.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

SRC = Path(__file__).resolve().parents[3] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from capcov.claims import Bundle, bundle_from_json, canonical_dict  # noqa: E402

PACKAGE_ROOT = Path(__file__).resolve().parents[3]
REPLAY_ROOT = PACKAGE_ROOT / "experiments" / "claim-semantics" / "replay"
PACK_PATH = REPLAY_ROOT / "rules-replay-v1.json"
PACK_ID = "rules-replay-v1"
CASES_DIR = REPLAY_ROOT / "cases"
REJECTED_DIR = REPLAY_ROOT / "rejected"
EXPECTED_PATH = REPLAY_ROOT / "expected.json"
REJECTED_PATH = REPLAY_ROOT / "rejected.json"
FROZEN_PRIMITIVES = SRC / "capcov" / "claims" / "replay" / "schema_replay_v1.json"
CONTEXT_KEYS = ("run", "model", "index")

DIAGNOSTIC_POLICY = {"missing_premises": "unresolved", "inconsistent_premises": "inconsistent-premises",
                     "out_of_scope": "out-of-scope", "forbidden_evidence": "invalid-input",
                     "revocation": "refutation", "completeness": "required"}
RELATION_FIELDS = {"name", "columns", "modality", "polarity", "binding", "primitive", "producer_classes",
                   "context_indices", "completes", "finite", "nonempty", "compatibility_targets",
                   "compatibility_context_indices"}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_pack(path: Path = PACK_PATH) -> dict[str, Any]:
    pack = read_json(path)
    if pack.get("schema_version") != 1 or pack.get("id") != PACK_ID:
        raise ValueError("unsupported replay rule pack")
    for section in ("primitives", "supplementary_primitives", "derived", "rules"):
        if not isinstance(pack.get(section), list):
            raise ValueError(f"rule pack section {section!r} must be a list")
    return pack


def pack_relations(pack: dict[str, Any]) -> list[dict[str, Any]]:
    relations = [*pack["primitives"], *pack["supplementary_primitives"], *pack["derived"]]
    names = [relation["name"] for relation in relations]
    if len(names) != len(set(names)):
        raise ValueError("rule pack declares a relation twice")
    for relation in relations:
        if set(relation) != RELATION_FIELDS:
            raise ValueError(f"relation {relation.get('name')!r} has unknown or missing fields")
    return relations


def case_paths(directory: Path = CASES_DIR) -> list[Path]:
    return sorted(directory.glob("[0-9][0-9]-*.json"))


def _term(value: Any, type_name: str) -> dict[str, Any]:
    if isinstance(value, dict):
        if set(value) != {"variable"}:
            raise ValueError("a term is a JSON scalar or {\"variable\": name}")
        return {"variable": value["variable"]}
    return {"value": value, "type": type_name}


def bundle_payload(case: dict[str, Any], pack: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compile one case into strict schema-v1 bundle JSON without evaluating."""
    pack = pack or load_pack()
    if case.get("schema_version") != 1 or case.get("rule_pack") != pack["id"]:
        raise ValueError("case does not name the replay rule pack")
    if set(case.get("context", {})) != set(CONTEXT_KEYS):
        raise ValueError(f"case context must carry exactly {CONTEXT_KEYS}")
    relations = pack_relations(pack)
    by_name = {relation["name"]: relation for relation in relations}

    def terms(entry: dict[str, Any]) -> list[dict[str, Any]]:
        declaration = by_name[entry["relation"]]
        columns = declaration["columns"]
        if entry.get("arg_order") != [column["name"] for column in columns]:
            raise ValueError(f"{entry.get('id', entry['relation'])} arg_order does not match its relation")
        if len(entry.get("args", ())) != len(columns):
            raise ValueError(f"{entry.get('id', entry['relation'])} arity does not match its relation")
        return [_term(value, column["type"]) for value, column in zip(entry["args"], columns)]

    def context(entry: dict[str, Any]) -> dict[str, Any]:
        declaration = by_name[entry["relation"]]
        values = dict(zip(entry["arg_order"], entry["args"]))
        expected = {name: values[name] for name in declaration["context_indices"]}
        if entry.get("context") != expected:
            raise ValueError(f"{entry.get('id', entry['relation'])} context does not equal its relation context indices")
        return expected

    facts, evidence = [], []
    for kind, section in (("fact", "facts"), ("assumption", "assumptions")):
        for entry in case[section]:
            if entry.get("kind") != kind:
                raise ValueError(f"{entry.get('id')} is filed under {section} but has kind {entry.get('kind')!r}")
            fact_terms = terms(entry)
            facts.append({"relation": entry["relation"], "terms": fact_terms})
            evidence.append({"id": entry["id"], "relation": entry["relation"], "terms": fact_terms,
                             "context": context(entry), "source": entry["source"],
                             "depends_on": list(entry["provenance"]["depends_on"]), "kind": kind})
    claims, mappings, diagnostics = [], [], []
    for entry in case["claims"]:
        claims.append({"id": entry["id"], "relation": entry["relation"], "terms": terms(entry),
                       "context": context(entry), "quantifier": entry["quantifier"], "domain": entry["domain"]})
        for mapping in entry.get("mappings", ()):
            mappings.append({**mapping, "claim_relation": entry["relation"], "claim_id": entry["id"]})
        for diagnostic in entry.get("diagnostics", ()):
            diagnostics.append({**diagnostic, "claim_id": entry["id"]})
    outputs = []
    for output in case.get("outputs", ()):
        if "claim_id" not in output:
            raise ValueError("output template requires claim_id")
        outputs.append(dict(output))
    semantic_inputs = {"case_id": case["id"], "context": case["context"],
                       "facts": [dict(entry) for entry in case["facts"]],
                       "assumptions": [dict(entry) for entry in case["assumptions"]],
                       "claims": [dict(entry) for entry in case["claims"]]}
    return {"schema_version": 1, "relations": relations, "facts": facts, "evidence": evidence,
            "mappings": mappings, "diagnostics": diagnostics, "outputs": outputs,
            "rules": [dict(rule) for rule in pack["rules"]], "claims": claims,
            "diagnostic_policy": dict(DIAGNOSTIC_POLICY),
            "metadata": {"case_id": case["id"], "rule_pack": pack["id"],
                         **{key: case["context"][key] for key in CONTEXT_KEYS},
                         "semantic_inputs": semantic_inputs}}


def load_case(path: Path, pack: dict[str, Any] | None = None, *, validate: bool = True) -> Bundle:
    return bundle_from_json(bundle_payload(read_json(path), pack), validate=validate)


def pack_bundle(pack: dict[str, Any] | None = None) -> Bundle:
    """The rule pack alone (declarations and rules, no facts) as a validated Bundle."""
    pack = pack or load_pack()
    return bundle_from_json({"schema_version": 1, "relations": pack_relations(pack),
                             "rules": [dict(rule) for rule in pack["rules"]],
                             "diagnostic_policy": dict(DIAGNOSTIC_POLICY)}, validate=True)


def canonical_metadata(bundle: Bundle) -> dict[str, Any]:
    return dict(canonical_dict(bundle)["metadata"])
