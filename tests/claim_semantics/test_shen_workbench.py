"""Stage D: the executable Shen workbench against the pinned shen-go runtime.

These tests launch the real runtime through bifrost (``BIFROST_SHEN_GO`` must
name the pinned binary); nothing is mocked.  They are skipped, never faked,
when the runtime is absent.  Covered (EXPERIMENT-PLAN section 18):

(a) authority accepts rules-static-v1, the go_app bundle and every corpus
    rule pack, and rejects one planted bad rule per check id;
(b) derive on the go_app fixture for each static_reaches row yields a
    certificate that ``certificate.recheck`` accepts and that equals the
    Python extractor's certificate for the same row;
(c) planted invalid certificates are rejected by recheck;
(d) a non-derivable row yields a why-not with a missing premise and no
    certificate;
(f) a pack whose elaborated checksum differs from the frozen one is refused.

The timeout / malformed-output paths (e) live in test_shen_transport.py.
"""
from __future__ import annotations

import copy
import glob
import json
import os
import shutil
import unittest
from pathlib import Path

from capcov.claims import bundle_from_json, canonical_json
from capcov.claims import shen
from capcov.claims.evaluator import evaluate
from capcov.claims.static import certificate

try:
    from .adapter import bundle_payload as corpus_payload
    from .static_rules import go_app
    from .static_rules.adapter import case_paths, load_case, load_pack, pack_bundle
except ImportError:  # unittest discover -s imports this directory as top-level
    from adapter import bundle_payload as corpus_payload
    from static_rules import go_app
    from static_rules.adapter import case_paths, load_case, load_pack, pack_bundle

HERE = Path(__file__).resolve().parent
RUNTIME_PRESENT = bool(shutil.which("bifrost") and os.environ.get("BIFROST_SHEN_GO"))
SKIP_REASON = "bifrost on PATH and BIFROST_SHEN_GO are required; the Shen runtime is never mocked"
if os.environ.get("CAPCOV_SHEN_REQUIRED") and not RUNTIME_PRESENT:
    # fail-closed mode for the manifest runner: a missing runtime is an error, not a skip
    raise RuntimeError("CAPCOV_SHEN_REQUIRED is set but " + SKIP_REASON)

# The handoff fixture (.capcov/shen-handoff/go_app-static-bundle.json) is the
# same bundle go_app.go_app_bundle() rebuilds; pin its digests here so a
# drift in either shows up as a failure rather than a silently different run.
GO_APP_BUNDLE_DIGEST = "f64eddd206f42702b66ac7edb964f8853a38ce79397f05f430d1c117809accbe"
GO_APP_RULES_DIGEST = "3c7c80822404fc2ef221b91edd209db7ea8e73a40122a9f9764be20398a4d36c"

CHECK_IDS = (
    "undeclared-relation",
    "ungrounded-conclusion-variable",
    "ungrounded-side-condition-variable",
    "context-index-loss",
    "unsupported-context-widening",
    "negative-conclusion-without-completeness",
    "declaration-promoted-to-effect",
    "non-linear-recursion",
)


def _corpus_bundles():
    for path in sorted(glob.glob(str(HERE / "corpus" / "[0-9]*.json"))):
        with open(path, encoding="utf-8") as handle:
            yield Path(path).name, bundle_from_json(corpus_payload(json.load(handle)))


def _decl(name, columns, **kw):
    base = {"name": name, "columns": [{"name": c, "type": t, "context": c in kw.get("context_indices", ())}
                                       for c, t in columns],
            "modality": "observation", "polarity": "positive", "binding": "static", "primitive": True,
            "producer_classes": [], "context_indices": [], "completes": None, "finite": False,
            "nonempty": False, "compatibility_targets": [], "compatibility_context_indices": []}
    base.update(kw)
    return base


def _v(name):
    return {"variable": name}


def _atom(relation, *terms, negated=False):
    return {"relation": relation, "terms": list(terms), "negated": negated}


def _planted_packs():
    """One minimal pack per authority check id; each fails exactly that check."""
    obs = _decl("obs", [("index", "digest"), ("x", "symbol")], context_indices=["index"])
    obs2 = _decl("obs2", [("index", "digest"), ("x", "symbol")], context_indices=["index"])
    derived = _decl("derived", [("index", "digest"), ("x", "symbol")], context_indices=["index"],
                    modality="derived", primitive=False)
    derived_noctx = _decl("derived_noctx", [("x", "symbol")], modality="derived", primitive=False)
    closed = _decl("obs_closed", [("index", "digest")], context_indices=["index"], modality="completeness",
                   completes="obs")
    runtime_claim = _decl("effect", [("index", "digest"), ("x", "symbol")], context_indices=["index"],
                          modality="claim", binding="runtime", primitive=False)
    assumption = _decl("assumed", [("index", "digest"), ("x", "symbol")], context_indices=["index"],
                       modality="assumption", binding="static")
    rec = _decl("rec", [("index", "digest"), ("x", "symbol"), ("y", "symbol")], context_indices=["index"],
                modality="derived", primitive=False)
    packs = {
        "undeclared-relation": ([obs, derived], [
            {"name": "bad", "head": _atom("derived", _v("IX"), _v("X")),
             "body": [_atom("obs", _v("IX"), _v("X")), _atom("ghost", _v("IX"))]}]),
        "ungrounded-conclusion-variable": ([obs, derived], [
            {"name": "bad", "head": _atom("derived", _v("IX"), _v("Y")),
             "body": [_atom("obs", _v("IX"), _v("X"))]}]),
        "ungrounded-side-condition-variable": ([obs, obs2, derived], [
            {"name": "bad", "head": _atom("derived", _v("IX"), _v("X")),
             "body": [_atom("obs", _v("IX"), _v("X")), _atom("obs2", _v("IX"), _v("Z"), negated=True)]}]),
        "context-index-loss": ([obs, derived, derived_noctx], [
            {"name": "bad", "head": _atom("derived", _v("X"), _v("X")),
             "body": [_atom("obs", _v("IX"), _v("X"))]}]),
        "unsupported-context-widening": ([obs, obs2, derived], [
            {"name": "bad", "head": _atom("derived", _v("IX"), _v("X")),
             "body": [_atom("obs", _v("IX"), _v("X")), _atom("obs2", _v("IY"), _v("X"))]}]),
        "negative-conclusion-without-completeness": ([obs, obs2, derived, closed], [
            {"name": "bad", "head": _atom("derived", _v("IX"), _v("X")),
             "body": [_atom("obs2", _v("IX"), _v("X")), _atom("obs", _v("IX"), _v("X"), negated=True)]}]),
        "declaration-promoted-to-effect": ([assumption, runtime_claim], [
            {"name": "bad", "head": _atom("effect", _v("IX"), _v("X")),
             "body": [_atom("assumed", _v("IX"), _v("X"))]}]),
        "non-linear-recursion": ([obs, rec], [
            {"name": "base", "head": _atom("rec", _v("IX"), _v("X"), _v("X")),
             "body": [_atom("obs", _v("IX"), _v("X"))]},
            {"name": "bad", "head": _atom("rec", _v("IX"), _v("X"), _v("Z")),
             "body": [_atom("rec", _v("IX"), _v("X"), _v("Y")), _atom("rec", _v("IX"), _v("Y"), _v("Z"))]}]),
    }
    for check_id, (relations, rules) in packs.items():
        yield check_id, {"relations": relations, "rules": rules}


@unittest.skipUnless(RUNTIME_PRESENT, SKIP_REASON)
class ShenAuthorityTest(unittest.TestCase):
    def test_rules_static_v1_is_accepted_and_its_elaboration_is_canonical(self) -> None:
        pack = pack_bundle()
        report = shen.authority(rules=pack)
        self.assertTrue(report.ok, report.failed_checks())
        self.assertEqual(len(report.rules), len(pack.rules))
        for verdict in report.rules:
            self.assertEqual([c["id"] for c in verdict["checks"]], list(CHECK_IDS))
        # Shen orders rules by its own canonical rendering; that order must be
        # the bundle's canonical order (both are sorted canonical_json(rule)).
        self.assertEqual([r["name"] for r in report.elaborated["rules"]], [r.name for r in pack.rules])
        self.assertEqual([r["canonical"] for r in report.elaborated["rules"]],
                         [json.loads(canonical_json(r)) for r in pack.rules])
        # The checksum the Shen side computed over its printed-back pack is
        # reproduced by Python over the re-serialised text.
        self.assertEqual(report.elaborated_checksum,
                         shen.pack_checksum(canonical_json(report.elaborated)))
        hashes = report.provenance.as_dict()["hashes"]
        for key in ("canonical_input_sha256", "generated_shen_sha256", "runtime_binary_sha256",
                    "elaborated_pack_sha256"):
            self.assertRegex(hashes[key], r"^[0-9a-f]{64}$", key)
        kinds = {r["name"]: r["kind"] for r in report.elaborated["rules"]}
        self.assertEqual(kinds["static_reaches_base"], "base")
        self.assertEqual(kinds["static_reaches_step"], "step")
        self.assertEqual(kinds["static_edge"], "plain")
        strata = {r["name"]: r["stratum"] for r in report.elaborated["rules"]}
        self.assertGreater(strata["static_route_authorization_gap"], strata["static_route_authorized"])

    def test_authority_accepts_the_go_app_bundle_and_every_corpus_pack(self) -> None:
        bundle, _ = go_app.go_app_bundle()
        self.assertTrue(shen.authority(bundle).ok)
        for name, corpus_bundle in _corpus_bundles():
            with self.subTest(case=name):
                report = shen.authority(corpus_bundle)
                self.assertTrue(report.ok, (name, report.failed_checks()))
        for path in case_paths()[:2]:
            with self.subTest(case=path.name):
                self.assertTrue(shen.authority(load_case(path)).ok)

    def test_authority_rejects_one_planted_bad_rule_per_check_id(self) -> None:
        seen = set()
        for check_id, pack in _planted_packs():
            with self.subTest(check=check_id):
                report = shen.authority(rules=pack, validate=False)
                self.assertFalse(report.ok)
                failed = {c for r, c in report.failed_checks() if r == "bad"}
                self.assertIn(check_id, failed)
                other = {r for r, _ in report.failed_checks() if r != "bad"}
                self.assertEqual(other, set(), "only the planted rule may fail")
                seen.add(check_id)
        self.assertEqual(seen, set(CHECK_IDS))


@unittest.skipUnless(RUNTIME_PRESENT, SKIP_REASON)
class ShenDeriveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bundle, _ = go_app.go_app_bundle()
        cls.relations = certificate.normalize_relations(evaluate(cls.bundle).relations)
        cls.rows = cls.relations["static_reaches"]

    def test_fixture_is_the_handoff_bundle(self) -> None:
        self.assertEqual(certificate.digest(self.bundle), GO_APP_BUNDLE_DIGEST)
        self.assertEqual(certificate.rules_digest(self.bundle), GO_APP_RULES_DIGEST)
        self.assertEqual(len(self.rows), 5)

    def test_every_static_reaches_row_certificate_rechecks_and_equals_python(self) -> None:
        frozen = shen.authority(self.bundle).elaborated_checksum
        timings = []
        for row in self.rows:
            with self.subTest(row=row[2]):
                result = shen.evaluate(self.bundle, None, "static_reaches", row, frozen=frozen)
                self.assertEqual(result.outcome, "positive")
                cert = result.certificate
                self.assertEqual(cert["certificate_version"], certificate.CERTIFICATE_VERSION)
                self.assertEqual(cert["bundle_digest"], GO_APP_BUNDLE_DIGEST)
                self.assertEqual(cert["rules_digest"], GO_APP_RULES_DIGEST)
                self.assertFalse(cert["truncated"])
                self.assertEqual(certificate.recheck(self.bundle, cert, self.relations),
                                 certificate.RecheckResult(True, (), ()))
                python_cert = certificate.certify(self.bundle, self.relations, "static_reaches", row)
                strip = lambda c: {k: v for k, v in c.items() if k not in ("bundle_digest", "rules_digest")}  # noqa: E731
                self.assertEqual(strip(cert), strip(python_cert))
                self.assertEqual(cert, python_cert)
                timings.append(result.provenance.elapsed_seconds)
                self.assertEqual(result.provenance.elaborated_checksum, frozen)
        self.assertLess(max(timings), shen.DEFAULT_TIMEOUT / 4, timings)

    def test_derive_returns_the_certificate_and_rule_pack_must_be_contained(self) -> None:
        cert = shen.derive(self.bundle, load_pack(), "static_reaches", self.rows[0])
        self.assertTrue(certificate.recheck(self.bundle, cert, self.relations).ok)
        foreign = copy.deepcopy(load_pack())
        foreign["rules"][0]["name"] = "renamed-so-its-digest-changes"
        with self.assertRaises(shen.ShenFailure) as ctx:
            shen.derive(self.bundle, foreign, "static_reaches", self.rows[0])
        self.assertEqual(ctx.exception.kind, "rule-pack-mismatch")

    def test_planted_invalid_certificates_are_rejected_by_recheck(self) -> None:
        cert = shen.derive(self.bundle, None, "static_reaches", self.rows[0])
        self.assertTrue(certificate.recheck(self.bundle, cert, self.relations).ok)

        def leaf(node):
            if node["kind"] == "fact":
                return node
            return leaf(node["premises"][0])

        substituted = copy.deepcopy(cert)
        other = next(record.id for record in self.bundle.evidence
                     if record.atom.relation != leaf(substituted["derivation"])["relation"])
        leaf(substituted["derivation"])["evidence"] = [other]
        self.assertFalse(certificate.recheck(self.bundle, substituted, self.relations).ok)

        dropped = copy.deepcopy(cert)
        target = leaf(dropped["derivation"])
        target["evidence"] = []
        self.assertFalse(certificate.recheck(self.bundle, dropped, self.relations).ok)

        wrong_rules = copy.deepcopy(cert)
        wrong_rules["rules_digest"] = "0" * 64
        self.assertFalse(certificate.recheck(self.bundle, wrong_rules, self.relations).ok)

        extra_step = copy.deepcopy(cert)
        extra_step["derivation"]["premises"].append(copy.deepcopy(extra_step["derivation"]["premises"][0]))
        self.assertFalse(certificate.recheck(self.bundle, extra_step, self.relations).ok)

        wrong_row = copy.deepcopy(cert)
        wrong_row["conclusion"]["row"][2] = "scip-go gomod github.com/example/jobsvc . `nowhere`/Missing()."
        self.assertFalse(certificate.recheck(self.bundle, wrong_row, self.relations).ok)

    def test_negative_control_yields_why_not_and_no_certificate(self) -> None:
        row = list(self.rows[0])
        row[2] = "scip-go gomod github.com/example/jobsvc . `github.com/example/jobsvc/nowhere`/Missing()."
        result = shen.evaluate(self.bundle, None, "static_reaches", row)
        self.assertEqual(result.outcome, "negative")
        self.assertIsNone(result.certificate)
        self.assertFalse(result.why_not["present"])
        self.assertGreaterEqual(len(result.why_not["alternatives"]), 1)
        missing = [m for alt in result.why_not["alternatives"] for m in alt["missing"]]
        self.assertTrue(missing)
        self.assertTrue(any(m["kind"] == "premise" for m in missing))
        self.assertIn("truncated", result.why_not)
        with self.assertRaises(shen.NotDerivable) as ctx:
            shen.derive(self.bundle, None, "static_reaches", row)
        self.assertEqual(ctx.exception.why_not["relation"], "static_reaches")
        report = shen.why_not(self.bundle, None, "static_reaches", row)
        self.assertFalse(report["present"])
        self.assertEqual([a["rule"] for a in report["alternatives"]],
                         [a["rule"] for a in result.why_not["alternatives"]])
        self.assertIn("hashes", report["provenance"])
        # a derivable row has nothing to explain
        present = shen.why_not(self.bundle, None, "static_reaches", self.rows[0])
        self.assertTrue(present["present"])
        self.assertEqual(present["alternatives"], [])

    def test_truncated_search_is_reported_not_certified(self) -> None:
        result = shen.evaluate(self.bundle, None, "static_reaches", self.rows[0], max_nodes=3)
        self.assertEqual(result.outcome, "truncated")
        self.assertTrue(result.certificate["truncated"])
        self.assertIsNone(result.certificate["derivation"])
        self.assertFalse(certificate.recheck(self.bundle, result.certificate, self.relations).ok)

    def test_unknown_relation_and_bad_arity_are_named_input_failures(self) -> None:
        with self.assertRaises(shen.ShenFailure) as ctx:
            shen.evaluate(self.bundle, None, "no_such_relation", ["a"])
        self.assertEqual(ctx.exception.kind, "invalid-input")
        with self.assertRaises(shen.ShenFailure) as ctx:
            shen.evaluate(self.bundle, None, "static_reaches", ["only-one-column"])
        self.assertEqual(ctx.exception.kind, "invalid-input")


@unittest.skipUnless(RUNTIME_PRESENT, SKIP_REASON)
class ShenFrozenPackTest(unittest.TestCase):
    def test_mutated_pack_is_refused_against_the_frozen_checksum(self) -> None:
        pack = pack_bundle()
        frozen = shen.authority(rules=pack).elaborated_checksum
        self.assertTrue(shen.authority(rules=pack, frozen=frozen).ok)
        mutated = copy.deepcopy(load_pack())
        mutated["rules"][0]["name"] = mutated["rules"][0]["name"] + "-mutated"
        with self.assertRaises(shen.ShenFailure) as ctx:
            shen.authority(rules=mutated, frozen=frozen)
        self.assertEqual(ctx.exception.kind, "frozen-pack-mismatch")
        with self.assertRaises(shen.ShenFailure) as ctx:
            shen.authority(rules=pack, frozen="ck2-1-1")
        self.assertEqual(ctx.exception.kind, "frozen-pack-mismatch")


if __name__ == "__main__":
    unittest.main()
