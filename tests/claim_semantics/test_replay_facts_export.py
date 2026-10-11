"""The replay fact exporter over the synthetic minimal receipt (Phase 4, judge side).

Every test here is pure: the receipt is the checked-in
``fixtures/replay_receipt_min`` (one run, three ops, five requests -- req-4
deletes an issue and req-5 repeats that delete -- the PHP system, the Go system
and the Shen model all agreeing statement for statement, three mutants all
killed, the PHP oracle stable across a bound selftest).  Variants are built by copying the fixture into a temporary directory
and editing one file.  No harness, no containers, no network.
"""
from __future__ import annotations

import copy
import hashlib
import json
import random
import shutil
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

from capcov.claims import (BundleIngestionError, Constant, bundle_from_json,
                           canonical_json, digest, validate_bundle)
from capcov.claims.replay import load_replay_schema, replay_facts

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "replay_receipt_min"
RUN = "run-fixture-1"


def _rows(bundle, relation: str) -> list[list]:
    return sorted([t.value for t in fact.terms] for fact in bundle.facts if fact.relation == relation)


def _evidence(bundle, relation: str):
    return [e for e in bundle.evidence if e.atom.relation == relation]


@contextmanager
def _variant(**edits):
    """A copy of the fixture with ``<file>.json`` documents rewritten by ``edits``.

    Each value is ``callable(document) -> document | None``; ``None`` deletes
    the file, and a callable receives ``None`` when the file does not exist.
    Keys use ``_`` for the file stem (``receipt=...``, ``php_effect=...``).
    """
    with tempfile.TemporaryDirectory(prefix="replay-receipt-") as d:
        root = Path(d) / "receipt"
        shutil.copytree(FIXTURE, root)
        for stem, edit in edits.items():
            path = root / f"{stem}.json"
            document = json.loads(path.read_text()) if path.exists() else None
            replacement = edit(document)
            if replacement is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(json.dumps(replacement, indent=1, sort_keys=True))
        yield root


class _Exported(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.receipt = json.loads((FIXTURE / "receipt.json").read_text())
        cls.result = cls.export()
        assert cls.result.status == replay_facts.STATUS_COMPLETE, cls.result.messages
        cls.bundle = cls.result.bundle
        cls.identity = dict(cls.bundle.metadata)["replay_digest"]
        cls.model = cls.receipt["model"]

    @classmethod
    def export(cls, receipt_dir=None, **kwargs):
        return replay_facts.export_bundle(
            receipt_dir if receipt_dir is not None else FIXTURE,
            run=kwargs.pop("run", RUN), **kwargs)


class BundleShapeTest(_Exported):
    def test_bundle_validates_and_round_trips_through_strict_json(self) -> None:
        self.assertEqual(validate_bundle(self.bundle), ())
        again = bundle_from_json(canonical_json(self.bundle), validate=True)
        self.assertEqual(digest(again), digest(self.bundle))
        self.assertEqual(self.bundle.metadata, again.metadata)

    def test_relations_are_the_frozen_schema_plus_declared_derived_targets(self) -> None:
        frozen = [r["name"] for r in load_replay_schema()["relations"]]
        declared = {r.name for r in self.bundle.relations}
        self.assertTrue(set(frozen) <= declared)
        stubs = declared - set(frozen)
        self.assertEqual(stubs, {"mutant_killed_in", "op_qualified_rt"})
        self.assertEqual(stubs, set(replay_facts.STUB_RELATIONS))
        for r in self.bundle.relations:
            if r.name in stubs:
                self.assertFalse(r.primitive)
                self.assertEqual(r.modality.value, "derived")
                self.assertEqual(r.binding.value, "runtime")
        self.assertEqual(self.bundle.rules, ())
        self.assertEqual(self.bundle.claims, ())
        # the frozen file carries exactly the static schema's per-relation field set
        static_fields = {"name", "columns", "modality", "polarity", "binding", "primitive",
                         "producer_classes", "context_indices", "completes", "finite", "nonempty",
                         "compatibility_targets", "compatibility_context_indices"}
        for raw in load_replay_schema()["relations"]:
            self.assertEqual(set(raw), static_fields, raw["name"])

    def test_every_fact_has_exactly_the_evidence_the_exporter_declares(self) -> None:
        fact_keys = {canonical_json(f) for f in self.bundle.facts}
        evidence_keys = [canonical_json(e.atom) for e in self.bundle.evidence]
        self.assertEqual(fact_keys, set(evidence_keys))
        self.assertEqual(len(evidence_keys), len(set(evidence_keys)))
        self.assertEqual(len(self.bundle.facts), len(self.bundle.evidence))
        self.assertEqual(self.result.counts, dict(self.bundle.metadata)["row_counts"])
        self.assertEqual(sum(self.result.counts.values()), len(self.bundle.facts))
        decls = {r.name: r for r in self.bundle.relations}
        for record in self.bundle.evidence:
            self.assertEqual(record.kind, "assumption" if record.atom.relation == "model_scope_exclusion" else "fact")
            self.assertTrue(record.source)
            decl = decls[record.atom.relation]
            self.assertEqual(set(record.context.as_dict()), set(decl.context_indices))
            if "run" in decl.context_indices:
                self.assertEqual(record.context.as_dict()["run"], RUN)
            if "model" in decl.context_indices:
                self.assertEqual(record.context.as_dict()["model"], self.model)

    def test_fixture_content_is_exported_in_full(self) -> None:
        self.assertEqual(_rows(self.bundle, "replay_run"), [[
            RUN, self.receipt["nonce"], self.receipt["php_commit"], self.receipt["go_commit"],
            self.receipt["snapshot"], self.model]])
        self.assertEqual(len(_rows(self.bundle, "replay_request")), 5)
        self.assertEqual({op for *_, op in _rows(self.bundle, "replay_request")},
                         {"issues.create", "issues.close", "delete-issue"})
        # the three systems agree row for row
        php = [row[1:] for row in _rows(self.bundle, "php_effect")]
        go = [row[1:] for row in _rows(self.bundle, "go_effect")]
        model = [row[2:] for row in _rows(self.bundle, "model_effect")]
        self.assertEqual(php, go)
        self.assertEqual(php, model)
        self.assertEqual([row[1:] for row in _rows(self.bundle, "php_post_state")],
                         [row[1:] for row in _rows(self.bundle, "go_post_state")])
        self.assertEqual([row[2:] for row in _rows(self.bundle, "model_admissible")],
                         [row[1:] for row in _rows(self.bundle, "php_post_state")])
        self.assertEqual(_rows(self.bundle, "mutant"),
                         [[self.model, "m-1", "issues.create"], [self.model, "m-2", "issues.close"],
                          [self.model, "m-3", "delete-issue"]])
        self.assertEqual(_rows(self.bundle, "mutant_killed"),
                         [[RUN, "m-1", "req-1"], [RUN, "m-2", "req-2"], [RUN, "m-3", "req-4"]])
        self.assertEqual(_rows(self.bundle, "model_writes"),
                         [[self.model, op, table]
                          for op in ("delete-issue", "issues.close", "issues.create")
                          for table in ("entity_statistics", "issue")])

    def test_evidence_ids_are_unique_and_content_derived_with_the_producer_prefix(self) -> None:
        ids = [e.id for e in self.bundle.evidence]
        self.assertEqual(len(ids), len(set(ids)))
        decls = {r.name: r for r in self.bundle.relations}
        expected_prefix = {
            "replay_run": "replay", "replay_request": "replay", "php_effect": "php",
            "php_post_state": "php", "go_effect": "go", "go_post_state": "go",
            "model_effect": "shen", "model_admissible": "shen", "model_writes": "shen",
            "mutant": "mut", "mutant_killed": "mut",
            # witnesses and compatibility rows are owned by the class that vouches for them
            "replay_requests_closed": "replay", "php_effects_closed": "replay", "go_effects_closed": "replay",
            "php_post_states_closed": "replay", "go_post_states_closed": "replay", "mutant_kills_closed": "replay",
            "model_admissible_closed": "shen", "model_writes_closed": "shen", "model_describes_run": "shen",
            "mutants_closed": "mut", "index_describes_replay": "reviewer",
            "model_scope_exclusion": "reviewer", "model_scope_exclusions_closed": "reviewer",
        }
        self.assertTrue(all(decl.producer_classes for decl in decls.values() if decl.primitive),
                        "every frozen primitive relation names its producer class")
        for record in self.bundle.evidence:
            row = [t.value for t in record.atom.terms]
            decl = decls[record.atom.relation]
            self.assertEqual(record.id, replay_facts.evidence_id(self.identity, decl, row))
            prefix = replay_facts.evidence_prefix(decl)
            self.assertEqual(
                record.id,
                f"{prefix}:{self.identity[:12]}:{decl.name}:{replay_facts.row_digest(decl.name, row)[:12]}")
            if decl.name in expected_prefix:
                self.assertEqual(prefix, expected_prefix[decl.name], decl.name)
            # the producer class of the source is the class the prefix names
            if decl.producer_classes:
                self.assertIn(record.source.split(" ", 1)[0], decl.producer_classes)
        # php-census shares the php prefix
        self.assertEqual(replay_facts.evidence_prefix(decls["op_declared"]), "php")
        # the same row under another identity has another id
        other = replay_facts.evidence_id("f" * 64, decls["mutant"], [self.model, "m-1", "issues.create"])
        self.assertTrue(other.startswith("mut:ffffffffffff:mutant:"))

    def test_sources_carry_the_file_producer_or_the_schema_default(self) -> None:
        for record in _evidence(self.bundle, "php_effect"):
            self.assertEqual(record.source, "php target-cloud " + self.receipt["php_commit"])
        for record in _evidence(self.bundle, "go_post_state"):
            self.assertEqual(record.source, "go target-go " + self.receipt["go_commit"])
        for record in _evidence(self.bundle, "mutant"):
            self.assertEqual(record.source, "mut shen-mutator v1")
        # replay_request.json names no producer: the schema's class then the transcriber
        for record in _evidence(self.bundle, "replay_request"):
            self.assertEqual(record.source, "replay capcov.claims.replay.replay_facts v1")
        [run_record] = _evidence(self.bundle, "replay_run")
        self.assertEqual(run_record.source, "replay capcov.claims.replay.replay_facts v1")
        producers = dict(dict(self.bundle.metadata)["producers"])
        self.assertEqual(producers["php_effect"], "php target-cloud " + self.receipt["php_commit"])
        self.assertEqual(producers["exporter"], replay_facts.PRODUCER)

    def test_depends_on_chains(self) -> None:
        by_id = {e.id: e for e in self.bundle.evidence}
        [run_eid] = [e.id for e in _evidence(self.bundle, "replay_run")]
        run_record = by_id[run_eid]
        for external in (f"external:git-commit:{self.receipt['php_commit']}",
                         f"external:git-commit:{self.receipt['go_commit']}",
                         f"external:snapshot:{self.receipt['snapshot']}",
                         f"external:model:{self.model}"):
            self.assertIn(external, run_record.depends_on)
        for record in self.bundle.evidence:
            for dep in record.depends_on:
                self.assertNotEqual(dep, record.id)
                if not dep.startswith("external:"):
                    self.assertIn(dep, by_id, f"{record.id} depends on unknown {dep}")
        request_eids = {e.atom.terms[1].value: e.id for e in _evidence(self.bundle, "replay_request")}
        for record in _evidence(self.bundle, "replay_request"):
            self.assertIn(run_eid, record.depends_on)
        # every request-scoped row depends on its run and its request row
        for relation, req_position in (("php_effect", 1), ("go_effect", 1), ("php_post_state", 1),
                                       ("go_post_state", 1), ("model_effect", 2),
                                       ("model_admissible", 2), ("mutant_killed", 2)):
            records = _evidence(self.bundle, relation)
            self.assertTrue(records, relation)
            for record in records:
                self.assertIn(run_eid, record.depends_on, relation)
                self.assertIn(request_eids[record.atom.terms[req_position].value], record.depends_on)
        # model-scoped rows depend on the model identity the receipt only names
        for relation in ("model_effect", "model_admissible", "model_writes", "mutant",
                         "model_writes_closed", "mutants_closed", "model_admissible_closed",
                         "model_describes_run", "model_scope_exclusion", "model_scope_exclusions_closed"):
            for record in _evidence(self.bundle, relation):
                self.assertIn(f"external:model:{self.model}", record.depends_on, relation)
        # the stability row names the two selftest runs it was matched against
        for record in _evidence(self.bundle, "replay_stability"):
            run_a, run_b = record.atom.terms[1].value, record.atom.terms[2].value
            self.assertIn(f"external:run:{run_a}", record.depends_on)
            self.assertIn(f"external:run:{run_b}", record.depends_on)
            self.assertIn(run_eid, record.depends_on)
        for relation in ("replay_requests_closed", "php_effects_closed", "go_effects_closed",
                         "php_post_states_closed", "go_post_states_closed",
                         "model_admissible_closed", "mutant_kills_closed"):
            [record] = _evidence(self.bundle, relation)
            self.assertIn(run_eid, record.depends_on)

    def test_model_describes_run_and_index_describes_replay_carry_both_contexts(self) -> None:
        [record] = _evidence(self.bundle, "model_describes_run")
        self.assertEqual(record.context.as_dict(), {"model": self.model, "run": RUN})
        self.assertEqual(_rows(self.bundle, "index_describes_replay"), [])
        index = "a" * 64
        result = self.export(describes_indexes=(index, index))
        self.assertEqual(result.status, replay_facts.STATUS_COMPLETE, result.messages)
        [record] = _evidence(result.bundle, "index_describes_replay")
        self.assertEqual(record.context.as_dict(), {"index": index, "run": RUN})
        self.assertIn(f"external:index:{index}", record.depends_on)
        self.assertEqual(validate_bundle(result.bundle), ())
        self.assertEqual(dict(result.bundle.metadata)["describes_indexes"], (index,))
        # compatibility rows are keyed by the identity, never part of it
        self.assertEqual(dict(result.bundle.metadata)["replay_digest"], self.identity)
        self.assertNotEqual(digest(result.bundle), digest(self.bundle))
        self.assertEqual(self.export(describes_indexes=("",)).status, replay_facts.STATUS_INVALID_INPUT)


class MetadataTest(_Exported):
    def test_metadata_carries_identity_header_closure_and_receipts(self) -> None:
        meta = dict(self.bundle.metadata)
        self.assertEqual(meta["run"], RUN)
        self.assertRegex(self.identity, r"^[0-9a-f]{64}$")
        self.assertEqual(meta["replay_digest_kind"], replay_facts.REPLAY_IDENTITY)
        self.assertEqual(meta["export_version"], "v1")
        self.assertEqual(meta["exporter"], "capcov.claims.replay.replay_facts")
        for key in ("nonce", "snapshot", "model", "php_commit", "go_commit"):
            self.assertEqual(meta[key], self.receipt[key])
        self.assertEqual(dict(meta["closed"]), {k: True for k in (
            "replay_requests", "php_effects", "go_effects", "php_post_states", "go_post_states",
            "model_admissible", "mutant_kills", "model_scope_exclusions",
            "php_effect_seqs", "go_effect_seqs", "model_effect_seqs", "replay_request_seqs",
            "php_responses", "go_responses", "replay_stability")})
        # the receipts are carried, reported, and are not identity
        self.assertEqual(json.loads(canonical_json(meta["receipts"])), self.receipt["receipts"])
        self.assertEqual(meta["receipt_dir"], str(FIXTURE))
        self.assertTrue(any(self.identity in m and "receipt" in m for m in self.result.messages))
        self.assertEqual(replay_facts.RECEIPT_METADATA_KEYS, ("receipts", "receipt_dir"))

    def test_the_identity_recipe_is_public_and_recomputable_from_the_rows(self) -> None:
        rows = {}
        for fact in self.bundle.facts:
            decl = next(r for r in self.bundle.relations if r.name == fact.relation)
            if decl.modality.value == "compatibility":
                continue
            rows.setdefault(fact.relation, []).append([t.value for t in fact.terms])
        self.assertEqual(
            replay_facts.replay_relations_identity(rows, (r.name for r in self.bundle.relations)),
            self.identity)
        self.assertEqual(self.identity, hashlib.sha256((
            "replay-relations-v1:" + canonical_json({
                "relations": sorted(r.name for r in self.bundle.relations),
                "rows": {rel: sorted(rs, key=canonical_json) for rel, rs in rows.items()}})
        ).encode()).hexdigest())


class DeterminismTest(_Exported):
    def test_digest_is_identical_under_any_permutation_of_the_rows(self) -> None:
        reference = digest(self.bundle)
        for seed in (1, 7, 42):
            rng = random.Random(seed)

            def shuffle(document, rng=rng):
                document = copy.deepcopy(document)
                rng.shuffle(document["rows"])
                return document

            def shuffle_receipt(document, rng=rng):
                document = copy.deepcopy(document)
                rng.shuffle(document["model_writes_closed"])
                rng.shuffle(document["mutants_closed"])
                return document

            edits = {name: shuffle for name in replay_facts.OBSERVATION_FILES}
            with _variant(receipt=shuffle_receipt, **edits) as root:
                result = self.export(root)
                self.assertEqual(result.status, replay_facts.STATUS_COMPLETE, result.messages)
                self.assertEqual(replay_facts.bundle_digest(result.bundle),
                                 replay_facts.bundle_digest(self.bundle), f"seed {seed}")
                self.assertEqual(dict(result.bundle.metadata)["replay_digest"], self.identity)
                self.assertEqual({e.id for e in result.bundle.evidence},
                                 {e.id for e in self.bundle.evidence})
                # only the receipt_dir differs from the fixture export
                self.assertNotEqual(digest(result.bundle), reference)

    def test_exporting_twice_is_bit_for_bit_identical(self) -> None:
        again = self.export()
        self.assertEqual(canonical_json(again.bundle), canonical_json(self.bundle))

    def test_bundle_digest_excludes_the_receipts(self) -> None:
        def other_receipts(document):
            document = copy.deepcopy(document)
            document["receipts"] = {
                "wall_clock": {"started_at": "2027-01-01T00:00:00Z", "finished_at": "2027-01-01T00:00:09Z"},
                "paths": {"snapshot": "/elsewhere/fixture.sql.gz"},
                "container_ids": {"php": "deadbeef", "go": "deadbeef", "shen": "deadbeef"},
            }
            return document

        with _variant(receipt=other_receipts) as root:
            result = self.export(root)
        self.assertEqual(result.status, replay_facts.STATUS_COMPLETE, result.messages)
        meta = dict(result.bundle.metadata)
        self.assertEqual(meta["replay_digest"], self.identity)
        self.assertEqual({e.id for e in result.bundle.evidence}, {e.id for e in self.bundle.evidence})
        self.assertEqual(sorted(canonical_json(f) for f in result.bundle.facts),
                         sorted(canonical_json(f) for f in self.bundle.facts))
        self.assertEqual(replay_facts.bundle_digest(result.bundle), replay_facts.bundle_digest(self.bundle))
        # the receipts are the only difference the plain IR digest sees
        self.assertNotEqual(digest(result.bundle), digest(self.bundle))
        stripped = lambda b: {k: v for k, v in b.metadata if k not in replay_facts.RECEIPT_METADATA_KEYS}  # noqa: E731
        self.assertEqual(stripped(result.bundle), stripped(self.bundle))
        self.assertNotEqual(meta["receipts"], dict(self.bundle.metadata)["receipts"])
        self.assertNotEqual(meta["receipt_dir"], dict(self.bundle.metadata)["receipt_dir"])

    def test_changed_content_is_a_different_identity_and_the_placeholder_never_leaks(self) -> None:
        def extra_effect(document):
            document = copy.deepcopy(document)
            document["rows"].append(dict(document["rows"][0], req="req-3", kind="update"))
            return document

        with _variant(php_effect=extra_effect) as root:
            result = self.export(root)
        self.assertEqual(result.status, replay_facts.STATUS_COMPLETE, result.messages)
        self.assertNotEqual(dict(result.bundle.metadata)["replay_digest"], self.identity)
        self.assertNotEqual(replay_facts.bundle_digest(result.bundle), replay_facts.bundle_digest(self.bundle))
        self.assertNotIn(replay_facts._PLACEHOLDER_IDENTITY[:12] + ":", canonical_json(self.bundle))
        self.assertNotIn(replay_facts._PLACEHOLDER_IDENTITY, canonical_json(self.bundle))

    def test_rebase_recomputes_ids_and_dependencies_without_touching_rows(self) -> None:
        relations = {decl.name: decl for decl in replay_facts.replay_relations()}
        facts = replay_facts._Facts(replay_facts._PLACEHOLDER_IDENTITY, relations)
        run_eid = facts.add("replay_run", {
            "run": RUN, "nonce": "1" * 64, "php_commit": "p", "go_commit": "g",
            "snapshot": "2" * 64, "model": "3" * 64}, source="replay x", depends_on=["external:model:" + "3" * 64])
        req_eid = facts.add("replay_request", {"run": RUN, "req": "r", "tenant": "t", "route": "R", "op": "o"},
                            source="replay x", depends_on=[run_eid])
        self.assertTrue(run_eid.startswith("replay:000000000000:replay_run:"))
        before = {key: list(entry["row"]) for key, entry in facts.rows.items()}
        identity = facts.identity()
        facts.rebase(identity)
        self.assertEqual({key: list(entry["row"]) for key, entry in facts.rows.items()}, before)
        ids = {entry["id"] for entry in facts.rows.values()}
        self.assertTrue(all(identity[:12] in eid for eid in ids))
        [request] = [entry for (relation, _), entry in facts.rows.items() if relation == "replay_request"]
        [run] = [entry for (relation, _), entry in facts.rows.items() if relation == "replay_run"]
        self.assertEqual(request["depends_on"], {run["id"]})
        self.assertEqual(run["depends_on"], {"external:model:" + "3" * 64})
        self.assertNotIn(req_eid, ids)
        # identity is over the rows, so rebasing again is a no-op
        self.assertEqual(facts.identity(), identity)


class TypeCheckTest(_Exported):
    def _status(self, expected, **edits):
        with _variant(**edits) as root:
            result = self.export(root)
        self.assertEqual(result.status, expected, result.messages)
        self.assertIsNone(result.bundle)
        self.assertTrue(result.messages)
        return result.messages

    @staticmethod
    def _edit_row(index, **changes):
        def edit(document):
            document = copy.deepcopy(document)
            document["rows"][index].update(changes)
            return document
        return edit

    def test_a_value_of_the_wrong_type_is_invalid_input_naming_the_column(self) -> None:
        [message] = self._status(replay_facts.STATUS_INVALID_INPUT,
                                 php_post_state=self._edit_row(0, state_digest=7))
        self.assertIn("php_post_state.state_digest", message)
        [message] = self._status(replay_facts.STATUS_INVALID_INPUT,
                                 replay_request=self._edit_row(1, op=None))
        self.assertIn("replay_request.op", message)
        [message] = self._status(replay_facts.STATUS_INVALID_INPUT,
                                 mutant=self._edit_row(0, mutant=True))
        self.assertIn("mutant.mutant", message)

    def test_unknown_missing_and_foreign_columns_are_invalid_input(self) -> None:
        [message] = self._status(replay_facts.STATUS_INVALID_INPUT,
                                 go_effect=self._edit_row(0, extra="x"))
        self.assertIn("unknown columns ['extra']", message)

        def drop_table(document):
            document = copy.deepcopy(document)
            del document["rows"][2]["table"]
            return document

        [message] = self._status(replay_facts.STATUS_INVALID_INPUT, go_effect=drop_table)
        self.assertIn("lacks columns ['table']", message)
        [message] = self._status(replay_facts.STATUS_INVALID_INPUT,
                                 model_effect=self._edit_row(0, model="9" * 64))
        self.assertIn("names model", message)

    def test_a_row_of_another_run_is_stale(self) -> None:
        [message] = self._status(replay_facts.STATUS_STALE,
                                 php_effect=self._edit_row(1, run="run-fixture-0"))
        self.assertIn("run-fixture-0", message)
        self.assertIn("run-fixture-1", message)
        # a row may omit run/model: they are the receipt's
        def strip_context(document):
            document = copy.deepcopy(document)
            for row in document["rows"]:
                row.pop("run", None)
                row.pop("model", None)
            return document

        with _variant(model_admissible=strip_context, mutant=strip_context) as root:
            result = self.export(root)
        self.assertEqual(result.status, replay_facts.STATUS_COMPLETE, result.messages)
        self.assertEqual(replay_facts.bundle_digest(result.bundle), replay_facts.bundle_digest(self.bundle))

    def test_the_receipt_header_is_checked(self) -> None:
        def set_key(**changes):
            def edit(document):
                document = copy.deepcopy(document)
                document.update(changes)
                return document
            return edit

        self.assertIn("caller asked for", self._status(replay_facts.STATUS_INVALID_INPUT,
                                                       receipt=set_key(run="run-other"))[0])
        self.assertIn("version must be 1", self._status(replay_facts.STATUS_INVALID_INPUT,
                                                        receipt=set_key(version=2))[0])
        self.assertIn("unknown keys ['wall_clock']", self._status(
            replay_facts.STATUS_INVALID_INPUT, receipt=set_key(wall_clock="now"))[0])
        self.assertIn("'model' must be a non-empty string", self._status(
            replay_facts.STATUS_INVALID_INPUT, receipt=set_key(model=""))[0])
        self.assertIn("closed.php_effects must be a boolean", self._status(
            replay_facts.STATUS_INVALID_INPUT, receipt=set_key(closed={"php_effects": "yes"}))[0])
        self.assertIn("unknown 'closed' keys", self._status(
            replay_facts.STATUS_INVALID_INPUT, receipt=set_key(closed={"everything": True}))[0])
        self.assertIn("names model", self._status(
            replay_facts.STATUS_INVALID_INPUT,
            receipt=set_key(mutants_closed=[{"model": "9" * 64, "op": "issues.create"}]))[0])
        self.assertIn("is missing", self._status(replay_facts.STATUS_INVALID_INPUT,
                                                 receipt=lambda _: None)[0])
        self.assertIn("must be {rows, producer?}", self._status(
            replay_facts.STATUS_INVALID_INPUT, php_effect=lambda _: "not an object")[0])
        self.assertIn("must be {rows, producer?}", self._status(
            replay_facts.STATUS_INVALID_INPUT, php_effect=lambda d: {"rows": d["rows"], "x": 1})[0])
        with _variant() as root:
            (root / "php_effect.json").write_text("{not json")
            result = self.export(root)
        self.assertEqual(result.status, replay_facts.STATUS_INVALID_INPUT)
        self.assertIsNone(result.bundle)
        self.assertIn("not valid JSON", result.messages[0])
        # the caller's own arguments
        result = replay_facts.export_bundle(FIXTURE, run="")
        self.assertEqual(result.status, replay_facts.STATUS_INVALID_INPUT)
        result = replay_facts.export_bundle(FIXTURE / "nowhere", run=RUN)
        self.assertEqual(result.status, replay_facts.STATUS_INVALID_INPUT)
        self.assertIn("does not exist", result.messages[0])

    def test_limits_trip_to_resource_exhausted_without_an_exception(self) -> None:
        for limits in (replay_facts.ExportLimits(rows=5), replay_facts.ExportLimits(file_bytes=64)):
            with self.subTest(limits=limits):
                result = self.export(limits=limits)
                self.assertIsNone(result.bundle)
                self.assertTrue(result.messages)
        exhausted = self.export(limits=replay_facts.ExportLimits(rows=5))
        self.assertEqual(exhausted.status, replay_facts.STATUS_RESOURCE_EXHAUSTED)
        self.assertEqual(self.export(limits=replay_facts.ExportLimits()).status, "complete")


class OrderingRelationTest(_Exported):
    """The ordering, cross-request and cross-run observations (module docstring)."""

    SEQ_RELATIONS = ("php_effect_seq", "go_effect_seq", "model_effect_seq", "replay_request_seq",
                     "php_response", "go_response", "replay_stability")

    @staticmethod
    def _edit_row(index, **changes):
        def edit(document):
            document = copy.deepcopy(document)
            document["rows"][index].update(changes)
            return document
        return edit

    def test_every_new_relation_is_exported_under_its_own_producer_class(self) -> None:
        decls = {r.name: r for r in self.bundle.relations}
        expected = {"php_effect_seq": "php", "go_effect_seq": "go", "model_effect_seq": "shen",
                    "replay_request_seq": "replay", "php_response": "php", "go_response": "go",
                    "replay_stability": "replay"}
        self.assertEqual(set(expected), set(self.SEQ_RELATIONS))
        for relation, producer in expected.items():
            self.assertEqual(decls[relation].producer_classes, (producer,), relation)
            records = _evidence(self.bundle, relation)
            self.assertTrue(records, relation)
            for record in records:
                self.assertEqual(record.source.split(" ", 1)[0], producer, relation)
                self.assertTrue(record.id.startswith(f"{replay_facts.evidence_prefix(decls[relation])}:"))

    def test_the_declared_order_is_exported_as_rows_the_rules_can_compare(self) -> None:
        # req-1 writes issue (seq 1) then entity_statistics (seq 2) on all three sides
        for relation in ("php_effect_seq", "go_effect_seq"):
            rows = [r for r in _rows(self.bundle, relation) if r[1] == "req-1"]
            self.assertEqual([(row[2], row[3]) for row in rows], [(1, "issue"), (2, "entity_statistics")], relation)
        model = [r for r in _rows(self.bundle, "model_effect_seq") if r[2] == "req-1"]
        self.assertEqual([(row[3], row[4]) for row in model], [(1, "issue"), (2, "entity_statistics")])
        # the tape order and the two response tables
        self.assertEqual([(row[1], row[2], row[3]) for row in _rows(self.bundle, "replay_request_seq")],
                         [("req-1", 1, "POST /api/issues"), ("req-2", 2, "POST /api/issues/1/close"),
                          ("req-3", 3, "POST /api/issues"), ("req-4", 4, "DELETE /api/issues/1"),
                          ("req-5", 5, "DELETE /api/issues/1")],
                         "req-4 and req-5 share a target: that is what makes req-5 a repeat")
        for relation in ("php_response", "go_response"):
            self.assertEqual([(row[1], row[2]) for row in _rows(self.bundle, relation)],
                             [("req-1", 201), ("req-2", 200), ("req-3", 201), ("req-4", 200), ("req-5", 404)],
                             relation)
        self.assertEqual(_rows(self.bundle, "replay_stability"),
                         [[RUN, "run-fixture-1a", "run-fixture-1b", "php", "true"]])

    def test_an_unsigned_column_must_arrive_as_a_json_integer(self) -> None:
        for relation, column, quoted in (("php_effect_seq", "seq", "1"), ("go_effect_seq", "seq", "1"),
                                         ("model_effect_seq", "seq", "1"), ("replay_request_seq", "seq", "1"),
                                         ("php_response", "status", "201"), ("go_response", "status", "201")):
            with self.subTest(relation=relation), _variant(**{relation: self._edit_row(0, **{column: quoted})}) as root:
                result = self.export(root)
                self.assertEqual(result.status, replay_facts.STATUS_INVALID_INPUT, result.messages)
                self.assertIsNone(result.bundle)
                [message] = result.messages
                self.assertEqual(message, f"{relation}.{column}: expected unsigned, got {quoted!r}")

    def test_two_rows_for_one_sequence_position_are_invalid_input(self) -> None:
        def conflicting(document):
            document = copy.deepcopy(document)
            document["rows"].append(dict(document["rows"][0], table="audit_log"))
            return document

        with _variant(php_effect_seq=conflicting) as root:
            result = self.export(root)
        self.assertEqual(result.status, replay_facts.STATUS_INVALID_INPUT, result.messages)
        [message] = result.messages
        self.assertIn("php_effect_seq.json: rows[0] and rows[7]", message)
        self.assertIn("'seq': 1", message)
        self.assertIn("one observation per php_effect_seq key", message)
        # the model's sequence is keyed by the model as well
        self.assertEqual(replay_facts.UNIQUE_KEYS["model_effect_seq"], ("run", "model", "req", "seq"))
        self.assertEqual(replay_facts.UNIQUE_KEYS["replay_stability"], ("run", "run_a", "run_b", "side"))

    def test_a_stability_row_naming_another_run_is_stale(self) -> None:
        with _variant(replay_stability=self._edit_row(0, run="run-somewhere-else")) as root:
            result = self.export(root)
        self.assertEqual(result.status, replay_facts.STATUS_STALE, result.messages)
        self.assertIsNone(result.bundle)
        [message] = result.messages
        self.assertIn("replay_stability.json: rows[0] names run 'run-somewhere-else'", message)
        self.assertIn(f"the receipt is for {RUN!r}", message)

    def test_each_new_closure_can_be_withheld_on_its_own(self) -> None:
        keys = {"php_effect_seqs": "php_effect_seqs_closed", "go_effect_seqs": "go_effect_seqs_closed",
                "model_effect_seqs": "model_effect_seqs_closed", "replay_request_seqs": "replay_request_seqs_closed",
                "php_responses": "php_responses_closed", "go_responses": "go_responses_closed",
                "replay_stability": "replay_stability_closed"}
        self.assertTrue(set(keys) <= set(replay_facts._RUN_WITNESSES))
        for key, witness in keys.items():
            def open_one(document, key=key):
                document = copy.deepcopy(document)
                document["closed"][key] = False
                return document

            with self.subTest(key=key), _variant(receipt=open_one) as root:
                result = self.export(root)
                self.assertEqual(result.status, replay_facts.STATUS_COMPLETE, result.messages)
                self.assertEqual(_rows(result.bundle, witness), [], witness)
                self.assertTrue(any(f"closed.{key} is false" in m for m in result.messages), result.messages)
                self.assertEqual(validate_bundle(result.bundle), ())
        # every other witness is still there when one is withheld
        with _variant(receipt=lambda d: {**copy.deepcopy(d), "closed": {**d["closed"], "php_responses": False}}) as root:
            result = self.export(root)
        self.assertEqual(_rows(result.bundle, "go_responses_closed"), [[RUN]])
        self.assertEqual(len(_rows(result.bundle, "php_response")), 5, "rows stay; only the witness goes")

    def test_an_unknown_closed_key_is_still_refused(self) -> None:
        def bogus(document):
            document = copy.deepcopy(document)
            document["closed"]["php_effect_seq"] = True      # the relation name, not the closure key
            return document

        with _variant(receipt=bogus) as root:
            result = self.export(root)
        self.assertEqual(result.status, replay_facts.STATUS_INVALID_INPUT, result.messages)
        self.assertIsNone(result.bundle)
        [message] = result.messages
        self.assertEqual(message, "receipt.json: unknown 'closed' keys ['php_effect_seq']")


class WitnessTest(_Exported):
    def test_every_witness_names_its_predicate_version(self) -> None:
        expected = {
            "replay_requests_closed": "requests-closed-v1",
            "php_effects_closed": "php-effects-closed-v1",
            "go_effects_closed": "go-effects-closed-v1",
            "php_post_states_closed": "php-post-states-closed-v1",
            "go_post_states_closed": "go-post-states-closed-v1",
            "model_admissible_closed": "model-admissible-closed-v1",
            "mutant_kills_closed": "mutant-kills-closed-v1",
            "model_writes_closed": "model-writes-closed-v1",
            "mutants_closed": "mutants-closed-v1",
            "model_scope_exclusions_closed": "model-scope-exclusions-closed-v1",
        }
        decls = {r.name: r for r in self.bundle.relations}
        for relation, predicate in expected.items():
            records = _evidence(self.bundle, relation)
            self.assertTrue(records, relation)
            owner = f"{decls[relation].producer_classes[0]} " if decls[relation].producer_classes else ""
            for record in records:
                self.assertEqual(record.source, f"{owner}capcov.claims.replay.replay_facts {predicate}")
                self.assertEqual(record.source, replay_facts.witness_source(decls[relation], predicate))
        # the schema attributes the post-state closures to the harness
        for relation in ("php_post_states_closed", "go_post_states_closed"):
            [record] = _evidence(self.bundle, relation)
            self.assertTrue(record.source.startswith("replay "), record.source)

    def test_all_witnesses_hold_on_the_clean_fixture(self) -> None:
        for relation in ("replay_requests_closed", "php_effects_closed", "go_effects_closed",
                         "php_post_states_closed", "go_post_states_closed", "mutant_kills_closed"):
            self.assertEqual(_rows(self.bundle, relation), [[RUN]], relation)
        self.assertEqual(_rows(self.bundle, "model_admissible_closed"), [[RUN, self.model]])
        for relation in ("php_effect_seqs_closed", "go_effect_seqs_closed", "replay_request_seqs_closed",
                         "php_responses_closed", "go_responses_closed", "replay_stability_closed"):
            self.assertEqual(_rows(self.bundle, relation), [[RUN]], relation)
        self.assertEqual(_rows(self.bundle, "model_effect_seqs_closed"), [[RUN, self.model]],
                         "the model's order closure carries the model, like model_admissible_closed")
        ops = [[self.model, op] for op in ("delete-issue", "issues.close", "issues.create")]
        self.assertEqual(_rows(self.bundle, "model_writes_closed"), ops)
        self.assertEqual(_rows(self.bundle, "mutants_closed"), ops)
        self.assertEqual(_rows(self.bundle, "model_scope_exclusions_closed"), [[self.model]])

    def test_a_false_closed_flag_withholds_the_witness_and_says_so(self) -> None:
        def open_php(document):
            document = copy.deepcopy(document)
            document["closed"]["php_effects"] = False
            del document["closed"]["mutant_kills"]   # absent means false
            document["model_writes_closed"] = []
            return document

        with _variant(receipt=open_php) as root:
            result = self.export(root)
        bundle = result.bundle
        self.assertEqual(result.status, replay_facts.STATUS_COMPLETE, result.messages)
        self.assertEqual(_rows(bundle, "php_effects_closed"), [])
        self.assertEqual(_rows(bundle, "mutant_kills_closed"), [])
        self.assertEqual(_rows(bundle, "model_writes_closed"), [])
        self.assertEqual(_rows(bundle, "go_effects_closed"), [[RUN]])
        self.assertEqual(len(_rows(bundle, "php_effect")), 7, "rows stay; only the witness goes")
        self.assertTrue(any("closed.php_effects is false" in m for m in result.messages))
        self.assertTrue(any("closed.mutant_kills is false" in m for m in result.messages))
        self.assertEqual(validate_bundle(bundle), ())
        self.assertNotEqual(dict(bundle.metadata)["replay_digest"], self.identity)

    def test_a_missing_file_is_zero_rows_and_the_witness_follows_closed(self) -> None:
        with _variant(php_effect=lambda _: None) as root:
            closed = self.export(root)
        self.assertEqual(closed.status, replay_facts.STATUS_COMPLETE, closed.messages)
        self.assertEqual(_rows(closed.bundle, "php_effect"), [])
        self.assertEqual(_rows(closed.bundle, "php_effects_closed"), [[RUN]],
                         "closed says so: zero rows and no rows exist")
        self.assertTrue(any("php_effect.json absent" in m for m in closed.messages))

        def open_php(document):
            document = copy.deepcopy(document)
            document["closed"]["php_effects"] = False
            return document

        with _variant(php_effect=lambda _: None, receipt=open_php) as root:
            unknown = self.export(root)
        self.assertEqual(unknown.status, replay_facts.STATUS_COMPLETE, unknown.messages)
        self.assertEqual(_rows(unknown.bundle, "php_effect"), [])
        self.assertEqual(_rows(unknown.bundle, "php_effects_closed"), [],
                         "zero rows is not a closure statement")
        self.assertEqual(validate_bundle(unknown.bundle), ())


class UniqueObservationTest(_Exported):
    """One observation per write and per post-state (``UNIQUE_KEYS``)."""

    def test_two_php_effect_rows_for_one_write_with_different_columns_are_invalid_input(self) -> None:
        def conflicting(document):
            document = copy.deepcopy(document)
            document["rows"].append(dict(document["rows"][0], cols_digest="c" * 64))
            return document

        with _variant(php_effect=conflicting) as root:
            result = self.export(root)
        self.assertEqual(result.status, replay_facts.STATUS_INVALID_INPUT, result.messages)
        self.assertIsNone(result.bundle)
        [message] = result.messages
        self.assertIn("php_effect.json: rows[0] and rows[7]", message)
        self.assertIn("'req': 'req-1'", message)
        self.assertIn("['cols_digest']", message)
        self.assertIn("one observation per php_effect key", message)

    def test_every_keyed_relation_is_checked_and_a_verbatim_repeat_is_one_fact(self) -> None:
        self.assertEqual(set(replay_facts.UNIQUE_KEYS),
                         {"php_effect", "go_effect", "model_effect", "php_post_state", "go_post_state",
                          "php_effect_seq", "go_effect_seq", "model_effect_seq", "replay_request_seq",
                          "php_response", "go_response", "replay_stability", "model_well_formed",
                          "model_operation_checked",
                          # a prediction is a set of admissible states per tape position, so the
                          # state digest is part of its key; the oracle answered once per position
                          "learn_prediction", "learn_observation"})
        for relation, column, value in (("go_effect", "cols_digest", "d" * 64), ("model_effect", "cols_digest", "d" * 64),
                                        ("php_post_state", "state_digest", "e" * 64),
                                        ("go_post_state", "state_digest", "f" * 64)):
            def conflicting(document, column=column, value=value):
                document = copy.deepcopy(document)
                document["rows"].append(dict(document["rows"][1], **{column: value}))
                return document

            with self.subTest(relation=relation), _variant(**{relation: conflicting}) as root:
                result = self.export(root)
                self.assertEqual(result.status, replay_facts.STATUS_INVALID_INPUT, result.messages)
                self.assertIn(f"one observation per {relation} key", result.messages[0])

        def repeated(document):
            document = copy.deepcopy(document)
            document["rows"].append(dict(document["rows"][2]))
            return document

        with _variant(php_effect=repeated, go_post_state=repeated) as root:
            result = self.export(root)
        self.assertEqual(result.status, replay_facts.STATUS_COMPLETE, result.messages)
        self.assertEqual(len(_rows(result.bundle, "php_effect")), 7)
        self.assertEqual(len(_rows(result.bundle, "go_post_state")), 5)
        self.assertEqual(replay_facts.bundle_digest(result.bundle), replay_facts.bundle_digest(self.bundle))
        # model_admissible is a set: two admissible states for one request coexist
        def second_state(document):
            document = copy.deepcopy(document)
            document["rows"].append(dict(document["rows"][0], state_digest="a" * 64))
            return document

        with _variant(model_admissible=second_state) as root:
            result = self.export(root)
        self.assertEqual(result.status, replay_facts.STATUS_COMPLETE, result.messages)
        self.assertEqual(len(_rows(result.bundle, "model_admissible")), 6)


class ScopeExclusionTest(_Exported):
    """The reviewer's scope exclusions: assumption-kind, reviewer-owned, bound to the model reviewed."""

    def test_exclusions_export_as_reviewer_assumptions_naming_what_was_reviewed(self) -> None:
        records = _evidence(self.bundle, "model_scope_exclusion")
        self.assertEqual(sorted(r.atom.terms[1].value for r in records), ["authentication", "redis"])
        for record in records:
            self.assertEqual(record.kind, "assumption")
            self.assertEqual(record.source, f"reviewer fixture-reviewer 2026-09-16 model:{self.model[:12]} run:{RUN}")
            self.assertTrue(record.id.startswith("reviewer:"))
            self.assertIn(f"external:model:{self.model}", record.depends_on)
        [closure] = _evidence(self.bundle, "model_scope_exclusions_closed")
        self.assertEqual(closure.source, "reviewer capcov.claims.replay.replay_facts model-scope-exclusions-closed-v1")
        self.assertEqual(dict(dict(self.bundle.metadata)["producers"])["model_scope_exclusion"], records[0].source)
        self.assertEqual(validate_bundle(self.bundle), ())

    def test_a_file_producer_names_the_reviewer_and_a_non_reviewer_is_refused(self) -> None:
        with _variant(model_scope_exclusions=lambda d: {**d, "producer": "reviewer alice"}) as root:
            result = self.export(root)
        self.assertEqual(result.status, replay_facts.STATUS_COMPLETE, result.messages)
        for record in _evidence(result.bundle, "model_scope_exclusion"):
            self.assertEqual(record.source, f"reviewer alice 2026-09-16 model:{self.model[:12]} run:{RUN}")
        with _variant(model_scope_exclusions=lambda d: {**d, "producer": "replay replay-tool v1"}) as root:
            result = self.export(root)
        self.assertEqual(result.status, replay_facts.STATUS_INVALID_INPUT)
        self.assertEqual(len(result.messages), 2)
        for message in result.messages:
            self.assertTrue(message.startswith("evidence-producer:"), message)
            self.assertIn("'model_scope_exclusion' admits ('reviewer',)", message)

    def test_a_stale_review_and_a_malformed_file_are_refused_with_their_own_messages(self) -> None:
        def stale(document):
            document = copy.deepcopy(document)
            document["reviewed_against"]["model"] = "9" * 64
            return document

        with _variant(model_scope_exclusions=stale) as root:
            result = self.export(root)
        self.assertEqual(result.status, replay_facts.STATUS_INVALID_INPUT)
        [message] = result.messages
        self.assertIn("stale review: reviewed against model '999999999999'", message)
        for edit, needle in ((lambda d: {k: v for k, v in d.items() if k != "reviewer"}, "'reviewer' must be a non-empty string"),
                             (lambda d: {**d, "reviewed_against": {"model": d["reviewed_against"]["model"]}},
                              "'reviewed_against' must be {model, run}"),
                             (lambda d: {**d, "extra": 1}, "must be {reviewer, reviewed_at, reviewed_against, rows, producer?}"),
                             (lambda d: {**d, "rows": [{"table": "x"}]}, "rows[0].reason must be a non-empty string"),
                             (lambda d: {**d, "rows": [{"model": "8" * 64, "table": "x", "reason": "y"}]}, "names model")):
            with self.subTest(needle=needle), _variant(model_scope_exclusions=edit) as root:
                result = self.export(root)
                self.assertEqual(result.status, replay_facts.STATUS_INVALID_INPUT)
                self.assertIn(needle, result.messages[0])

    def test_without_the_file_or_the_closure_the_judge_sees_no_exclusions(self) -> None:
        def open_exclusions(document):
            document = copy.deepcopy(document)
            document["closed"]["model_scope_exclusions"] = False
            return document

        with _variant(model_scope_exclusions=lambda _: None, receipt=open_exclusions) as root:
            result = self.export(root)
        self.assertEqual(result.status, replay_facts.STATUS_COMPLETE, result.messages)
        self.assertEqual(_rows(result.bundle, "model_scope_exclusion"), [])
        self.assertEqual(_rows(result.bundle, "model_scope_exclusions_closed"), [])
        self.assertTrue(any("model_scope_exclusions.json absent" in m for m in result.messages))
        # rows without the closure are exported (the reviewer said something) but nothing is closed
        with _variant(receipt=open_exclusions) as root:
            result = self.export(root)
        self.assertEqual(len(_rows(result.bundle, "model_scope_exclusion")), 2)
        self.assertEqual(_rows(result.bundle, "model_scope_exclusions_closed"), [])


class ModelWellFormedTest(_Exported):
    """Structural validity, operation-scoped coverage, and external authority."""

    OPERATION = "delete-issue"
    CHECKED = json.loads((FIXTURE / "model_operation_checked.json").read_text())["rows"]
    OP_ROW = next(row for row in CHECKED if row["operation"] == "delete-issue")
    CERTIFICATE = OP_ROW["certificate"]
    BINARY = OP_ROW["checker_binary"]

    def admission(self, **changes):
        entry = {
            "producer": "reviewer replay-authority-test-admission-v1",
            "model": self.model,
            "checker": "stage-d-typecheck",
            "checker_version": "0.1-pending",
            "operation": self.OPERATION,
            "checker_binary": self.BINARY,
            "certificate": self.CERTIFICATE,
        }
        entry.update(changes)
        return entry

    def test_the_certificate_exports_under_the_modelcheck_class(self) -> None:
        [record] = _evidence(self.bundle, "model_well_formed")
        self.assertEqual([t.value for t in record.atom.terms],
                         [self.model, "stage-d-typecheck", "0.1-pending", "b" * 64,
                          json.loads((FIXTURE / "model_well_formed.json").read_text())["rows"][0]["certificate"]])
        self.assertEqual(record.kind, "fact")
        self.assertEqual(record.source, f"modelcheck stage-d-typecheck 0.1-pending model:{self.model[:12]}")
        self.assertTrue(record.id.startswith("modelcheck:"))
        self.assertEqual(record.context.as_dict(), {"model": self.model})
        self.assertEqual(record.depends_on, (f"external:model:{self.model}",))
        decls = {r.name: r for r in self.bundle.relations}
        self.assertEqual(decls["model_well_formed"].producer_classes, ("modelcheck",))
        self.assertEqual(replay_facts.EVIDENCE_PREFIXES["modelcheck"], "modelcheck")
        # The receipt's own model_checkers.json is not reviewer authority.
        self.assertEqual(_evidence(self.bundle, "model_checker_admitted"), [])
        self.assertTrue(any("model_checkers.json ignored" in m for m in self.result.messages))
        self.assertTrue(any("no caller-supplied reviewer admissions" in m
                            for m in self.result.messages))

    def test_external_admission_pins_and_depends_on_the_exact_certificate(self) -> None:
        result = self.export(reviewer_admissions=[self.admission()])
        self.assertEqual(result.status, replay_facts.STATUS_COMPLETE, result.messages)
        checked = next(e for e in _evidence(result.bundle, "model_operation_checked")
                       if e.atom.terms[1].value == self.OPERATION)
        [admitted] = _evidence(result.bundle, "model_checker_admitted")
        self.assertEqual([t.value for t in admitted.atom.terms],
                         [self.model, self.OPERATION, "stage-d-typecheck", "0.1-pending",
                          self.BINARY, self.CERTIFICATE])
        self.assertEqual(admitted.source,
                         "reviewer replay-authority-test-admission-v1")
        self.assertTrue(admitted.id.startswith("reviewer:"))
        self.assertEqual(admitted.context.as_dict(), {"model": self.model, "operation": self.OPERATION})
        self.assertEqual(admitted.depends_on, (checked.id,))
        self.assertEqual(dict(dict(result.bundle.metadata)["producers"])["model_checker_admitted"],
                         admitted.source)

    def test_admitting_one_checked_operation_does_not_admit_other_operations(self) -> None:
        def only_delete(document):
            return {**document, "rows": [row for row in document["rows"]
                                         if row["operation"] == self.OPERATION]}
        with _variant(model_operation_checked=only_delete) as root:
            result = self.export(root, reviewer_admissions=[self.admission()])
        self.assertEqual(result.status, replay_facts.STATUS_COMPLETE, result.messages)
        self.assertEqual([row[1] for row in _rows(result.bundle, "model_operation_checked")],
                         [self.OPERATION])
        self.assertEqual([row[1] for row in _rows(result.bundle, "model_checker_admitted")],
                         [self.OPERATION])

    def test_receipt_self_admission_is_ignored_even_when_malformed_or_hostile(self) -> None:
        with _variant(model_checkers=lambda _: {
                "producer": "shen receipt-self-admission",
                "rows": [{"checker": "stage-d-typecheck"}],
                "private": "/not/read/by/exporter",
        }) as root:
            result = self.export(root)
        self.assertEqual(result.status, replay_facts.STATUS_COMPLETE, result.messages)
        self.assertEqual(_rows(result.bundle, "model_checker_admitted"), [])
        self.assertTrue(any("model_checkers.json ignored" in m for m in result.messages))
        self.assertEqual(dict(result.bundle.metadata)["replay_digest"], self.identity,
                         "ignored receipt-local authority must not affect identity")

    def test_placeholder_external_reviewers_contribute_no_admission(self) -> None:
        placeholders = [
            "unassigned", "unsigned", "none", "nobody", "tbd", "pending", "placeholder",
            "TBD pending", "  [TBD pending]  ", "ＴＢＤ pending", "placeholder-reviewer",
            "Placeholder_Reviewer", "none yet", "NONE—yet",
        ]
        for reviewer in placeholders:
            with self.subTest(reviewer=reviewer):
                result = self.export(reviewer_admissions=[
                    self.admission(producer=f"reviewer {reviewer}")])
                self.assertEqual(result.status, replay_facts.STATUS_COMPLETE, result.messages)
                self.assertEqual(_rows(result.bundle, "model_checker_admitted"), [])
                self.assertTrue(any("placeholder" in message and "contributes no authority" in message
                                    for message in result.messages), result.messages)

    def test_placeholder_words_inside_names_are_not_substring_matches(self) -> None:
        for reviewer in ("Tbderson", "Placeholderly", "Pendington", "Nonesuch"):
            with self.subTest(reviewer=reviewer):
                result = self.export(reviewer_admissions=[
                    self.admission(producer=f"reviewer {reviewer}")])
                self.assertEqual(result.status, replay_facts.STATUS_COMPLETE, result.messages)
                self.assertEqual(len(_rows(result.bundle, "model_checker_admitted")), 1)

    def test_the_model_host_may_not_certify_its_own_model(self) -> None:
        for producer in ("shen shen-model-host v1", "reviewer fixture-reviewer", "replay replay-tool v1"):
            with self.subTest(producer=producer), \
                    _variant(model_well_formed=lambda d, p=producer: {**d, "producer": p}) as root:
                result = self.export(root)
                self.assertEqual(result.status, replay_facts.STATUS_INVALID_INPUT)
                [message] = result.messages
                self.assertTrue(message.startswith("evidence-producer:"), message)
                self.assertIn("'model_well_formed' admits ('modelcheck',)", message)

    def test_a_certificate_for_another_model_is_stale_with_its_own_message(self) -> None:
        def other(document):
            document = copy.deepcopy(document)
            document["rows"][0]["model"] = "a" * 64
            return document

        with _variant(model_well_formed=other) as root:
            result = self.export(root)
        self.assertEqual(result.status, replay_facts.STATUS_STALE)
        [message] = result.messages
        self.assertIn("certifies model 'aaaaaaaaaaaa'", message)
        self.assertIn("the certificate is for another model", message)
        self.assertEqual(replay_facts.STALE_ON_FOREIGN_MODEL,
                         frozenset({"model_well_formed", "model_operation_checked"}))
        # a model-scoped relation that is merely *about* this model stays invalid-input
        with _variant(model_writes=other) as root:
            result = self.export(root)
        self.assertEqual(result.status, replay_facts.STATUS_INVALID_INPUT)
        self.assertIn("names model 'aaaaaaaaaaaa'", result.messages[0])

    def test_certificate_absence_and_no_external_admission_are_simply_no_rows(self) -> None:
        with _variant(model_well_formed=lambda _: None, model_checkers=lambda _: None) as root:
            result = self.export(root)
        self.assertEqual(result.status, replay_facts.STATUS_COMPLETE, result.messages)
        self.assertEqual(_rows(result.bundle, "model_well_formed"), [])
        self.assertEqual(_rows(result.bundle, "model_checker_admitted"), [])
        self.assertTrue(any("model_well_formed.json absent" in m for m in result.messages))
        self.assertTrue(any("no caller-supplied reviewer admissions" in m for m in result.messages))
        # nothing in the schema completes either relation: they are read positively
        completes = {r.completes for r in result.bundle.relations if r.completes}
        self.assertNotIn("model_well_formed", completes)
        self.assertNotIn("model_checker_admitted", completes)
        self.assertNotIn("model_well_formed", replay_facts._RUN_WITNESSES.values())

    def test_a_malformed_certificate_row_is_invalid_input(self) -> None:
        for edits, needle in (
                ({"model_well_formed": lambda d: {**d, "rows": [{"model": d["rows"][0]["model"],
                                                                 "checker": "c", "checker_version": "v",
                                                                 "checker_binary": "b" * 64}]}},
                 "lacks columns ['certificate']"),
                ({"model_well_formed": lambda d: {**d, "rows": [{**d["rows"][0], "checker": 3}]}},
                 "model_well_formed.checker: expected str")):
            with self.subTest(needle=needle), _variant(**edits) as root:
                result = self.export(root)
                self.assertEqual(result.status, replay_facts.STATUS_INVALID_INPUT)
                self.assertIn(needle, result.messages[0])

    def test_external_admission_must_match_every_certificate_identity_field(self) -> None:
        mismatches = {
            "model": "a" * 64,
            "operation": "other-op",
            "checker": "other-checker",
            "checker_version": "other-version",
            "checker_binary": "a" * 64,
            "certificate": "b" * 64,
        }
        for field, value in mismatches.items():
            with self.subTest(field=field):
                result = self.export(reviewer_admissions=[self.admission(**{field: value})])
                self.assertEqual(result.status, replay_facts.STATUS_INVALID_INPUT)
                self.assertEqual(_rows(result.bundle, "model_checker_admitted")
                                 if result.bundle else [], [])
                self.assertIn("reviewer_admissions[0]", result.messages[0])

    def test_external_admission_contract_is_strict_and_reviewer_owned(self) -> None:
        invalid = (
            ({"reviewer_admissions": self.admission()}, "iterable of admission objects"),
            ({"reviewer_admissions": ["not-an-object"]}, "must be an object"),
            ({"reviewer_admissions": [{**self.admission(), "extra": "x"}]},
             "must have exactly"),
            ({"reviewer_admissions": [{k: v for k, v in self.admission().items()
                                        if k != "certificate"}]}, "must have exactly"),
            ({"reviewer_admissions": [self.admission(producer="shen self-admitted")]},
             "reviewer producer class"),
            ({"reviewer_admissions": [self.admission(certificate="not-a-digest")]},
             "lowercase sha256"),
            ({"reviewer_admissions": [self.admission(), self.admission()]},
             "duplicate admission"),
        )
        for kwargs, needle in invalid:
            with self.subTest(needle=needle):
                result = self.export(**kwargs)
                self.assertEqual(result.status, replay_facts.STATUS_INVALID_INPUT)
                self.assertIn(needle, result.messages[0])

    def test_ambiguous_checker_result_is_rejected_before_it_can_be_admitted(self) -> None:
        def ambiguous(document):
            document = copy.deepcopy(document)
            row = next(row for row in document["rows"] if row["operation"] == self.OPERATION)
            document["rows"].append({**row, "certificate": "b" * 64})
            return document

        with _variant(model_operation_checked=ambiguous) as root:
            result = self.export(root, reviewer_admissions=[self.admission()])
        self.assertEqual(result.status, replay_facts.STATUS_INVALID_INPUT)
        self.assertIn("one observation per model_operation_checked key", result.messages[0])


class ProducerAuthorityTest(_Exported):
    """Step 1's ``evidence-producer`` is the boundary the exporter relies on."""

    def test_php_effect_rows_under_the_shen_producer_are_rejected(self) -> None:
        def as_shen(document):
            return {**document, "producer": "shen shen-model-runner v1"}

        with _variant(php_effect=as_shen) as root:
            result = self.export(root)
        self.assertEqual(result.status, replay_facts.STATUS_INVALID_INPUT)
        self.assertIsNone(result.bundle)
        producer_issues = [m for m in result.messages if m.startswith("evidence-producer:")]
        self.assertEqual(len(producer_issues), 7, result.messages)
        for message in producer_issues:
            self.assertIn("'php_effect' admits ('php',)", message)
            self.assertIn("'shen shen-model-runner v1'", message)
        self.assertEqual(producer_issues, list(result.messages),
                         "the producer mismatch is the only thing wrong with the variant")

    def test_a_class_admitted_elsewhere_in_the_schema_does_not_help(self) -> None:
        # go is a real producer class, just not php_effect's
        with _variant(php_effect=lambda d: {**d, "producer": "go target-go abc"}) as root:
            result = self.export(root)
        self.assertEqual(result.status, replay_facts.STATUS_INVALID_INPUT)
        self.assertTrue(all(m.startswith("evidence-producer:") for m in result.messages))
        # and a producer string whose first token is the right class passes,
        # whatever it goes on to say
        with _variant(php_effect=lambda d: {**d, "producer": "php anything at all"}) as root:
            result = self.export(root)
        self.assertEqual(result.status, replay_facts.STATUS_COMPLETE, result.messages)
        for record in _evidence(result.bundle, "php_effect"):
            self.assertEqual(record.source, "php anything at all")

    def test_the_strict_wire_boundary_rejects_a_relabelled_bundle(self) -> None:
        raw = json.loads(canonical_json(self.bundle))
        relabelled = 0
        for record in raw["evidence"]:
            if record["atom"]["relation"] == "php_effect":
                record["source"] = "shen shen-model-runner v1"
                relabelled += 1
        self.assertEqual(relabelled, 7)
        with self.assertRaises(BundleIngestionError) as ctx:
            bundle_from_json(json.dumps(raw), validate=True)
        self.assertIn("evidence-producer", str(ctx.exception))
        # ingested without validation, the validator reports exactly the three
        lenient = bundle_from_json(json.dumps(raw), validate=False)
        issues = validate_bundle(lenient)
        self.assertEqual([issue.code for issue in issues], ["evidence-producer"] * 7)
        # a witness the schema leaves unowned accepts any producer string; an
        # owned witness (the post-state closures belong to the harness) does not
        decls = {r.name: r for r in self.bundle.relations}
        owned = sorted(name for name, decl in decls.items() if name.endswith("_closed") and decl.producer_classes)
        self.assertEqual(set(owned), {name for name in decls if name.endswith("_closed")})
        for record in raw["evidence"]:
            if record["atom"]["relation"] == "php_effect":
                record["source"] = "php target-cloud x"
            if record["atom"]["relation"].endswith("_closed") and not decls[record["atom"]["relation"]].producer_classes:
                record["source"] = "anyone at all"
        self.assertEqual(validate_bundle(bundle_from_json(json.dumps(raw), validate=False)), ())
        for record in raw["evidence"]:
            if record["atom"]["relation"] in owned:
                record["source"] = "anyone at all"
        issues = validate_bundle(bundle_from_json(json.dumps(raw), validate=False))
        self.assertEqual({issue.code for issue in issues}, {"evidence-producer"})
        self.assertEqual(len(issues), sum(1 for r in raw["evidence"] if r["atom"]["relation"] in owned))


if __name__ == "__main__":
    unittest.main()
