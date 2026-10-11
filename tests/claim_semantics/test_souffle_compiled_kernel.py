"""The compiled Souffle checker as a third kernel over both corpora (live binary).

Like the adversarial suites this never skips: souffle is a precondition, and
``souffle-compile.py`` plus a C++ toolchain are too.  One compile per pack
(replay, static) is shared by every case through a class-level cache -- the
cache directory is ``CAPCOV_SOUFFLE_CACHE_DIR`` when set, so a warm CI cache
skips the ~50 s compile; otherwise a temp dir compiles once.

Per case the three kernels must agree completely: no operational failure
anywhere, identical relations, identical claim verdict/status/basis/missing
premises, one canonical digest, and ``compare_three`` admitting the bundle.
The rejected replay cases check the other side of the contract: an invalid
bundle is refused by the compiled kernel under exactly the interpreter's
failure name, and is still non-admissible.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import unittest

from capcov.claims import souffle
from capcov.claims.differential import (CompiledKernelMismatch, DifferentialMismatch,
                                        compare_three, reports_match, run_python,
                                        run_souffle, run_souffle_compiled)
from capcov.claims.souffle import compile as compiled
from capcov.claims.static.certificate import certify, claim_conclusions, recheck

try:
    from .replay_rules import adapter as replay_adapter
    from .static_rules import adapter as static_adapter
except ImportError:  # unittest discover -s imports this directory as top-level
    from replay_rules import adapter as replay_adapter
    from static_rules import adapter as static_adapter

HEX64 = r"^[0-9a-f]{64}$"
CACHE_ENV = "CAPCOV_SOUFFLE_CACHE_DIR"
REJECTED_STEMS = sorted(
    path.stem for path in replay_adapter.case_paths(replay_adapter.REJECTED_DIR))


def cache_dir() -> Path:
    """The shared compiled-checker cache: ``CAPCOV_SOUFFLE_CACHE_DIR`` or a temp dir."""
    configured = os.environ.get(CACHE_ENV)
    if configured:
        path = Path(configured)
        path.mkdir(parents=True, exist_ok=True)
        return path
    return Path(tempfile.mkdtemp(prefix="capcov-compiled-cache-"))


class SouffleCompiledCorpusTests(unittest.TestCase):
    """Every reviewed case of both packs, in python, interpreted and compiled Souffle."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.cache = cache_dir()
        cls.owns_cache = not os.environ.get(CACHE_ENV)
        cls.replay_root = tempfile.mkdtemp(prefix="capcov-compiled-differential-")
        cls.checkers: dict[str, compiled.CompiledChecker] = {}
        cls.cases: dict[str, dict[str, object]] = {}
        cls.compile_error: str | None = None
        if shutil.which("souffle") is None:
            return
        replay_pack = replay_adapter.load_pack()
        static_pack = static_adapter.load_pack()
        cls.cases = {
            "replay": {p.stem: replay_adapter.load_case(p, replay_pack)
                       for p in replay_adapter.case_paths()},
            "static": {p.stem: static_adapter.load_case(p, static_pack)
                       for p in static_adapter.case_paths()},
        }
        cls.rejected = {p.stem: replay_adapter.load_case(p, replay_pack, validate=False)
                        for p in replay_adapter.case_paths(replay_adapter.REJECTED_DIR)}
        try:
            for pack, cases in cls.cases.items():
                program = souffle.program_for_pack(next(iter(cases.values())))
                cls.checkers[pack] = compiled.compile_program(program, cache_dir=cls.cache)
        except (compiled.CompileError, souffle.SouffleUnavailable) as exc:
            cls.compile_error = f"{type(exc).__name__}: {exc}"

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.owns_cache:
            shutil.rmtree(cls.cache, ignore_errors=True)
        shutil.rmtree(cls.replay_root, ignore_errors=True)

    def setUp(self) -> None:
        self.assertIsNotNone(shutil.which("souffle"),
                             "souffle must be on PATH: run inside the nix devShell")
        self.assertIsNone(self.compile_error, self.compile_error)

    def test_one_compile_per_pack_serves_every_case_of_that_pack(self) -> None:
        self.assertEqual(sorted(self.checkers), ["replay", "static"])
        self.assertNotEqual(self.checkers["replay"].compile_key,
                            self.checkers["static"].compile_key)
        for pack, checker in sorted(self.checkers.items()):
            self.assertRegex(checker.binary_sha256, HEX64)
            self.assertRegex(checker.program_digest, HEX64)
            self.assertGreaterEqual(len(self.cases[pack]), 10)
            for stem, bundle in sorted(self.cases[pack].items()):
                with self.subTest(pack=pack, case=stem):
                    self.assertEqual(souffle.program_for_pack(bundle).program_digest,
                                     checker.program_digest)
            entry = Path(checker.binary_path).parent
            self.assertEqual(entry.name, f"compiled-{checker.compile_key}")
            # a second request is a cache hit: the binary is not rebuilt
            again = compiled.compile_program(souffle.program_for_pack(
                next(iter(self.cases[pack].values()))), cache_dir=self.cache)
            self.assertEqual(again.binary_sha256, checker.binary_sha256)
            self.assertEqual(again.binary_path, checker.binary_path)

    def test_three_kernels_agree_on_every_reviewed_case(self) -> None:
        for pack, cases in sorted(self.cases.items()):
            checker = self.checkers[pack]
            for stem, bundle in sorted(cases.items()):
                with self.subTest(pack=pack, case=stem):
                    python = run_python(bundle)
                    interpreted = run_souffle(bundle)
                    native = run_souffle_compiled(bundle, checker=checker)
                    for report in (python, interpreted, native):
                        self.assertIsNone(report.operational_failure,
                                          f"{report.backend}: {report.message[:400]}")
                    self.assertEqual(interpreted.relations, native.relations)
                    self.assertEqual(python.relations, native.relations)
                    self.assertEqual(interpreted.claims, native.claims)
                    self.assertEqual(python.claims, native.claims)
                    self.assertEqual(len({python.canonical_digest, interpreted.canonical_digest,
                                          native.canonical_digest}), 1)
                    self.assertEqual(interpreted.closure_digest, native.closure_digest)
                    self.assertTrue(reports_match(interpreted, native))
                    result = compare_three(bundle, checker=checker,
                                           replay_root=self.replay_root)
                    self.assertTrue(result.matched)
                    self.assertTrue(result.closure_digest_equal)
                    self.assertEqual(result.compiled.backend, "souffle-compiled")

    def test_the_replay_pack_ships_at_least_one_rejected_case(self) -> None:
        self.assertTrue(self.rejected)
        self.assertEqual(sorted(self.rejected), REJECTED_STEMS)

    def _rejected_case_fails_the_same_way_in_both_souffle_kernels(self, stem: str) -> None:
        checker = self.checkers["replay"]
        bundle = self.rejected[stem]
        interpreted = run_souffle(bundle)
        native = run_souffle_compiled(bundle, checker=checker)
        self.assertEqual(interpreted.operational_failure, "invalid-input")
        self.assertEqual(native.operational_failure, interpreted.operational_failure)
        # an identical failure name is still not admissible
        self.assertFalse(reports_match(interpreted, native))
        # max_steps=1: the refusal is what this asserts. Minimising the replay of an
        # invalid bundle is the shrinker's work and test_shrinker_bundle covers it.
        with self.assertRaises((DifferentialMismatch, CompiledKernelMismatch)):
            compare_three(bundle, checker=checker, replay_root=self.replay_root, max_steps=1)


def _add_rejected_case_tests() -> None:
    """One test per rejected case, so a sharded run can place the cases on different shards."""
    for stem in REJECTED_STEMS:
        def test(self: SouffleCompiledCorpusTests, stem: str = stem) -> None:
            self._rejected_case_fails_the_same_way_in_both_souffle_kernels(stem)
        name = "test_rejected_case_" + re.sub(r"\W", "_", stem) + "_fails_the_same_way_in_both_souffle_kernels"
        test.__name__ = name
        test.__qualname__ = f"SouffleCompiledCorpusTests.{name}"
        setattr(SouffleCompiledCorpusTests, name, test)


_add_rejected_case_tests()


if __name__ == "__main__":
    unittest.main()
