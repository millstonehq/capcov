"""The three-kernel contract of ``compare_three``, driven by injected runners.

No Souffle binary and no compiler: the python, souffle and souffle-compiled
reports are supplied directly, so the tests pin what ``compare_three`` admits
rather than what a particular closure happens to be.  The properties under
test are the ones a compiled kernel could silently break -- a compiled-side
difference in any ``COMPARABLE_CLAIM_FIELDS`` field or in any relation must
block and persist the bundle, an identical named failure on all three sides is
not agreement, and ``matched`` requires all three canonical digests to be one.
``test_souffle_compiled_kernel`` runs the real binary over both corpora.
"""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from capcov.claims import Atom, Column, Constant, RelationDecl, canonical_json, digest
from capcov.claims.differential import (COMPARABLE_CLAIM_FIELDS, CompiledKernelMismatch,
                                        DifferentialMismatch, KernelClaim, KernelReport,
                                        compare_three, reports_match)
from tests.claim_fixtures import Bundle

CLOSURE = "c" * 64


def _bundle() -> Bundle:
    left = RelationDecl("left", (Column("value", "symbol"),))
    derived = RelationDecl("derived", (Column("value", "symbol"),),
                           modality="derived", primitive=False)
    return Bundle((left, derived), facts=(Atom("left", (Constant("l"),)),))


def _report(backend: str, *, rows=(("l",),), semantic="supported", operational="complete",
            basis="evidence", missing=(), failure=None, closure=CLOSURE) -> KernelReport:
    if failure is not None:
        return KernelReport(backend, (), (), failure, "boom")
    relations = (("left", tuple(rows)), ("derived", tuple(rows)))
    claim = KernelClaim("claim-a", 0, semantic, operational, basis, tuple(missing))
    return KernelReport(backend, relations, (claim,), closure_digest=closure)


class CompareThreeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="capcov-three-kernel-")
        self.addCleanup(self.temporary.cleanup)
        self.replay_root = Path(self.temporary.name)
        self.bundle = _bundle()

    def _run(self, compiled: KernelReport, *, python: KernelReport | None = None,
             souffle: KernelReport | None = None):
        return compare_three(
            self.bundle,
            python_runner=lambda b: python or _report("python", closure=None),
            souffle_runner=lambda b: souffle or _report("souffle"),
            compiled_runner=lambda b: compiled,
            replay_root=self.replay_root)

    def _replays(self) -> list[Path]:
        return sorted(self.replay_root.glob("compiled-*.json"))

    def test_agreement_returns_all_three_reports_and_timings(self) -> None:
        result = self._run(_report("souffle-compiled"))
        self.assertTrue(result.matched)
        self.assertTrue(result.closure_digest_equal)
        self.assertIsNone(result.replay_path)
        self.assertEqual([r.backend for r in (result.python, result.souffle, result.compiled)],
                         ["python", "souffle", "souffle-compiled"])
        self.assertEqual(len({result.python.canonical_digest, result.souffle.canonical_digest,
                              result.compiled.canonical_digest}), 1)
        self.assertEqual([name for name, _ in result.timings],
                         ["python", "souffle", "souffle-compiled"])
        self.assertEqual(self._replays(), [], "an agreement must not persist a replay")

    def test_each_comparable_claim_field_blocks_on_the_compiled_side(self) -> None:
        differences = {
            "semantic": {"semantic": "unresolved"},
            "operational": {"operational": "incomplete"},
            "basis": {"basis": "assumption"},
            "missing_premises": {"missing": ('{"relation":"left"}',)},
        }
        self.assertEqual(sorted(differences), sorted(COMPARABLE_CLAIM_FIELDS))
        for field, override in differences.items():
            with self.subTest(field=field):
                for stale in self._replays():
                    stale.unlink()
                with self.assertRaises(CompiledKernelMismatch) as caught:
                    self._run(_report("souffle-compiled", **override))
                result = caught.exception.result
                self.assertFalse(result.matched)
                self.assertEqual(result.compiled.backend, "souffle-compiled")
                self.assertFalse(reports_match(result.souffle, result.compiled))
                self.assertNotEqual(result.souffle.canonical_digest,
                                    result.compiled.canonical_digest)
                persisted = self._replays()
                self.assertEqual([p.name for p in persisted],
                                 [f"compiled-{digest(self.bundle)}.json"])
                self.assertEqual(result.replay_path, str(persisted[0]))
                # the bundle is persisted unshrunk: the pairwise shrinker is not run
                self.assertEqual(json.loads(persisted[0].read_text()),
                                 json.loads(canonical_json(self.bundle)))

    def test_a_relation_difference_alone_blocks(self) -> None:
        with self.assertRaises(CompiledKernelMismatch) as caught:
            self._run(_report("souffle-compiled", rows=(("l",), ("extra",))))
        result = caught.exception.result
        self.assertFalse(result.matched)
        self.assertEqual(dict(result.compiled.relations)["derived"], (("l",), ("extra",)))
        self.assertEqual(dict(result.souffle.relations)["derived"], (("l",),))
        self.assertEqual(len(self._replays()), 1)

    def test_an_identical_failure_name_on_all_three_sides_is_not_agreement(self) -> None:
        failed = _report("souffle-compiled", failure="souffle-execution-failed")
        with self.assertRaises(DifferentialMismatch):
            # python and souffle already disagree by failing: the pairwise
            # differential blocks first, before the compiled kernel is asked.
            self._run(failed,
                      python=_report("python", failure="souffle-execution-failed"),
                      souffle=_report("souffle", failure="souffle-execution-failed"))
        with self.assertRaises(CompiledKernelMismatch) as caught:
            self._run(failed)
        result = caught.exception.result
        self.assertFalse(result.matched)
        self.assertEqual(result.compiled.operational_failure, "souffle-execution-failed")
        self.assertFalse(reports_match(result.compiled, result.compiled),
                         "a failing report never matches, not even itself")

    def test_matched_requires_equal_closure_digests_too(self) -> None:
        with self.assertRaises(CompiledKernelMismatch) as caught:
            self._run(_report("souffle-compiled", closure="d" * 64))
        result = caught.exception.result
        self.assertTrue(result.matched, "the semantic payloads are identical")
        self.assertFalse(result.closure_digest_equal)
        self.assertEqual(len(self._replays()), 1)

    def test_a_compiled_failure_blocks_and_is_never_a_fallback(self) -> None:
        for failure in ("souffle-unavailable", "souffle-compile-failed",
                        "compiled-program-mismatch"):
            with self.subTest(failure=failure):
                with self.assertRaises(CompiledKernelMismatch) as caught:
                    self._run(_report("souffle-compiled", failure=failure))
                result = caught.exception.result
                self.assertEqual(result.compiled.operational_failure, failure)
                self.assertFalse(result.matched)
                self.assertFalse(result.closure_digest_equal)


if __name__ == "__main__":
    unittest.main()
