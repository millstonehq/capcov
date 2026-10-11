"""Load the reviewed replay rule pack (``rules-replay-v1.json``) as a validated Bundle.

The pack **ships as package data** beside this module -- exactly as
``schema_replay_v1.json`` does -- and is read through ``importlib.resources``,
so ``capcov.claims.replay.join``, ``scripts/compiled_checker.py`` and
``capcov experiment claims assumptions`` build the judge's combined bundle from
an installed wheel with no source tree on disk.  Reading it off a path relative
to this file was the older arrangement and it was a lie about the installed
package: ``Path(__file__).parents[4]`` names the repository only in a source
checkout, and in site-packages it names a directory that does not exist.

The **reviewed source stays** ``experiments/claim-semantics/replay/rules-replay-v1.json``:
that is the file a reviewer edits and the corpus adapter reads next to its
cases.  That repository path is named by the tests, which live in the
repository, and never by this module, which may be an installed wheel.  The
shipped copy is a byte-identical mirror of it, and
``tests/claim_semantics/test_replay_pack_package_data.py`` fails when the two drift -- so the experiment file remains canonical and the mirror
can never quietly become a second opinion.

The pack is kept in raw IR JSON wire form and merged verbatim: every relation
declaration and rule comes from the file, nothing is evaluated here.  This is
the same loader ``tests/claim_semantics/replay_rules/adapter.py`` implements;
it is in ``src`` so the judge can build its bundle without the tests on
``sys.path`` -- and build it identically, because the compiled checker's
program identity depends on the declaration order the combiner yields.
"""
from __future__ import annotations

from importlib import resources
import json
from pathlib import Path
from typing import Any

from ..ir import Bundle, bundle_from_json

PACK_ID = "rules-replay-v1"
PACK_NAME = "rules-replay-v1.json"
#: The shipped mirror: package data, present in a source checkout and in a wheel.
PACK_PATH = Path(__file__).resolve().parent / PACK_NAME

DIAGNOSTIC_POLICY = {"missing_premises": "unresolved", "inconsistent_premises": "inconsistent-premises",
                     "out_of_scope": "out-of-scope", "forbidden_evidence": "invalid-input",
                     "revocation": "refutation", "completeness": "required"}
RELATION_FIELDS = {"name", "columns", "modality", "polarity", "binding", "primitive", "producer_classes",
                   "context_indices", "completes", "finite", "nonempty", "compatibility_targets",
                   "compatibility_context_indices"}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_pack_text() -> str:
    """The shipped pack's bytes, as ``importlib.resources`` hands them over."""
    return resources.files(__package__).joinpath(PACK_NAME).read_text(encoding="utf-8")


def load_pack(path: Path | None = None) -> dict[str, Any]:
    """The rule pack document.  ``path=None`` reads the shipped package data."""
    pack = read_json(path) if path is not None else json.loads(read_pack_text())
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


def pack_bundle(pack: dict[str, Any] | None = None) -> Bundle:
    """The rule pack alone (declarations and rules, no facts) as a validated Bundle."""
    pack = pack or load_pack()
    return bundle_from_json({"schema_version": 1, "relations": pack_relations(pack),
                             "rules": [dict(rule) for rule in pack["rules"]],
                             "diagnostic_policy": dict(DIAGNOSTIC_POLICY)}, validate=True)


#: The reviewed original this file mirrors is
#: ``experiments/claim-semantics/replay/rules-replay-v1.json``, a repository
#: path.  It is deliberately NOT a constant here: an installed module that
#: names a repository directory is the defect this loader was changed to
#: remove.  The tests, which do live in the repository, own that path
#: (``tests/claim_semantics/test_replay_pack_package_data.py``).
__all__ = ["PACK_PATH", "PACK_NAME", "PACK_ID", "DIAGNOSTIC_POLICY", "RELATION_FIELDS",
           "read_json", "read_pack_text", "load_pack", "pack_relations", "pack_bundle"]
