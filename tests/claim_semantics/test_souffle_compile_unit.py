"""Compiled-checker identity, caching and failure naming without a Souffle binary.

Everything here patches ``subprocess`` inside ``capcov.claims.souffle.compile``:
a fake compiler writes a ``checker`` file (or fails), so the tests pin the
program-identity contract (``program_for_pack`` is fact-independent and shared
by every case of a pack), the ``compile_key`` and ``provenance.json``
contracts, cache reuse / invalidation, and the operational-failure names the
differential assigns.  ``test_souffle_compiled_kernel`` runs the real thing.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from capcov.claims import (Atom, Claim, Column, Constant, DiagnosticRule, Evidence,
                          RelationDecl, Rule, Variable, canonical_json)
from capcov.claims import souffle
from capcov.claims.differential import run_souffle_compiled
from capcov.claims.replay import join as replay_join
from capcov.claims.replay import pack as replay_pack
from capcov.claims.souffle import compile as compiled
from tests.claim_fixtures import Bundle

try:
    from .replay_rules import adapter as replay_adapter
    from .static_rules import adapter as static_adapter
except ImportError:  # unittest discover -s imports this directory as top-level
    from replay_rules import adapter as replay_adapter
    from static_rules import adapter as static_adapter

HEX64 = r"^[0-9a-f]{64}$"
SYNTHETIC_RECEIPT_DIR = Path(__file__).resolve().parent / "fixtures" / "replay_receipt_min"
CONTRACT_KEYS = {"schema", "compile_key", "program_digest", "binary_sha256", "souffle_path",
                 "souffle_sha256", "souffle_version", "compiler_config_sha256", "compile_flags",
                 "compile_seconds"}


def _forbidden_assumption_bundle():
    """A claim whose support depends on a forbidden assumption row.

    The claim fold answers it from a second closure over the eligible bundle
    (the evidence that does not descend from the forbidden assumption), so
    evaluating this bundle starts one process per closure.
    """
    rejected = RelationDecl("source__rejected", (Column("x", "symbol"),), modality="assumption")
    observed = RelationDecl("observed", (Column("x", "symbol"),))
    claimed = RelationDecl("claimed", (Column("x", "symbol"),), modality="claim")
    trigger = Atom("source__rejected", (Constant("v"),))
    observation = Atom("observed", (Constant("v"),))
    return Bundle(
        (rejected, observed, claimed), facts=(trigger, observation),
        evidence=(Evidence("blocked-assumption", trigger, source="test"),
                  Evidence("a-tainted", observation, source="test", depends_on=("blocked-assumption",)),
                  Evidence("z-independent", observation, source="test")),
        rules=(Rule(Atom("claimed", (Variable("x"),)), (Atom("observed", (Variable("x"),)),), "derive"),),
        claims=(Claim("claimed", (Constant("v"),), id="claim"),),
        diagnostics=(DiagnosticRule("source__rejected", "forbidden", claim_id="claim"),))
CONTRACT_KEYS = {"schema", "compile_key", "program_digest", "binary_sha256", "souffle_path",
                 "souffle_sha256", "souffle_version", "compiler_config_sha256", "compile_flags",
                 "compile_seconds"}


def _all_outputs(bundle):
    return tuple(decl.name for decl in bundle.relations)


class ProgramIdentityTests(unittest.TestCase):
    """program_for_pack strips facts and yields one program per (schema, pack) pair."""

    @classmethod
    def setUpClass(cls) -> None:
        pack = replay_adapter.load_pack()
        cls.replay_cases = {path.stem: replay_adapter.load_case(path, pack) for path in replay_adapter.case_paths()}
        static_pack = static_adapter.load_pack()
        cls.static_cases = {path.stem: static_adapter.load_case(path, static_pack)
                            for path in static_adapter.case_paths()}

    def test_program_digest_is_fact_independent_for_every_case(self) -> None:
        self.assertGreaterEqual(len(self.replay_cases), 10)
        self.assertGreaterEqual(len(self.static_cases), 10)
        for label, cases in (("replay", self.replay_cases), ("static", self.static_cases)):
            for stem, bundle in cases.items():
                with self.subTest(pack=label, case=stem):
                    self.assertTrue(bundle.facts, "a case without facts would prove nothing")
                    full = souffle.translate_bundle(bundle, outputs=_all_outputs(bundle))
                    pack_program = souffle.program_for_pack(bundle)
                    self.assertEqual(pack_program.program_digest, full.program_digest)
                    self.assertEqual(pack_program.program, full.program)
                    self.assertRegex(pack_program.program_digest, HEX64)
                    self.assertEqual(pack_program.program_digest,
                                     hashlib.sha256(pack_program.program.encode("utf-8")).hexdigest())
                    self.assertEqual(set(pack_program.outputs), set(_all_outputs(bundle)))
                    # the stripped program carries no facts at all
                    self.assertEqual(set(pack_program.facts.values()), {""})

    def test_every_case_of_a_pack_shares_one_program_digest(self) -> None:
        replay_digests = {souffle.program_for_pack(b).program_digest for b in self.replay_cases.values()}
        static_digests = {souffle.program_for_pack(b).program_digest for b in self.static_cases.values()}
        self.assertEqual(len(replay_digests), 1, replay_digests)
        self.assertEqual(len(static_digests), 1, static_digests)
        self.assertNotEqual(replay_digests, static_digests)

    def test_a_rule_change_changes_the_program_digest(self) -> None:
        bundle = next(iter(self.replay_cases.values()))
        fewer_rules = replace(bundle, rules=bundle.rules[:-1])
        self.assertNotEqual(souffle.program_for_pack(fewer_rules).program_digest,
                            souffle.program_for_pack(bundle).program_digest)

    def test_the_replay_join_has_the_recorded_program_digest(self) -> None:
        """The judge's combined bundle (exported schema first, then the pack) is its own program."""
        join = replay_join.build(SYNTHETIC_RECEIPT_DIR)
        self.assertIsNotNone(join.bundle, join.contract_findings)
        program = souffle.program_for_pack(join.bundle)
        # model_operation_checked (operation-scoped checker authority) is the 116th relation
        self.assertEqual(program.program_digest, "911483584280a1fa6a8fa175ea7556152971ea24115f1b7e0616d0e089cf1c60")
        self.assertEqual(len(program.outputs), 116)
        self.assertEqual(len(program.program.splitlines()), 357)
        # the exported schema declares the pack's primitives in the same order the pack does, so
        # the review cases and the judge's join are one program: one compile serves both
        self.assertEqual(program.program_digest, souffle.program_for_pack(
            next(iter(self.replay_cases.values()))).program_digest)

    def test_src_pack_loader_equals_the_test_adapter(self) -> None:
        """Two loaders, two files, one pack: the shipped mirror and the reviewed original.

        ``replay_pack`` reads package data (so the judge works from a wheel) and
        the corpus adapter reads ``experiments/`` beside its cases.  The paths
        differ on purpose; the bundles and the bytes must not.
        ``test_replay_pack_package_data`` owns the byte-identity contract.
        """
        self.assertEqual(replay_pack.pack_bundle(), replay_adapter.pack_bundle())
        self.assertEqual(replay_pack.load_pack(), replay_adapter.load_pack())
        self.assertEqual(replay_pack.PACK_PATH.read_bytes(), replay_adapter.PACK_PATH.read_bytes())
        self.assertNotEqual(replay_pack.PACK_PATH, replay_adapter.PACK_PATH)


class CompileKeyTests(unittest.TestCase):
    def test_compile_key_is_64_hex_and_separates_every_input(self) -> None:
        program = "a" * 64
        souffle_sha = "b" * 64
        base = compiled.compile_key(program, souffle_sha)
        self.assertRegex(base, HEX64)
        self.assertEqual(base, compiled.compile_key(program, souffle_sha, compiled.COMPILE_FLAGS))
        self.assertNotEqual(base, compiled.compile_key("c" * 64, souffle_sha))
        self.assertNotEqual(base, compiled.compile_key(program, "c" * 64))
        self.assertNotEqual(base, compiled.compile_key(program, souffle_sha, ("--no-preprocessor", "-j2", "-o")))
        # the C++ toolchain is part of the identity: souffle-compile.py embeds it
        self.assertNotEqual(base, compiled.compile_key(program, souffle_sha,
                                                       compiler_config_sha256="d" * 64))
        expected = hashlib.sha256(canonical_json({
            "schema": "capcov-souffle-compiled-v1", "program_digest": program,
            "souffle_sha256": souffle_sha, "compile_flags": list(compiled.COMPILE_FLAGS),
            "compiler_config_sha256": "",
        }).encode("utf-8")).hexdigest()
        self.assertEqual(base, expected)
        self.assertEqual(souffle.PROGRAM_SCHEMA, "capcov-souffle-compiled-v1")


class _FakeCompiler:
    """Two-stage fake: generate canonicalizable C++, then write a fake binary."""
    calls: list[list[str]] = []
    returncodes = [0, 0]
    write_native = True
    stderr = "Warning: variable only occurs once\n" * 3
    body = b"#!/bin/sh\nexit 0\n"
    generated = """
void Sf_checker::loadAll([[maybe_unused]] std::string inputDirectoryArg){
try {std::map<std::string, std::string> directiveMap({{R\"_(name)_\",R\"_(left)_\"}});
} catch (std::exception& e) {std::cerr << e.what();exit(1);}
}

void Sf_checker::printAll([[maybe_unused]] std::string outputDirectoryArg){
try {std::map<std::string, std::string> directiveMap({{R\"_(name)_\",R\"_(left)_\"}});
} catch (std::exception& e) {std::cerr << e.what();exit(1);}
}

void Sf_checker::dumpInputs(){
try {std::map<std::string, std::string> rwOperation;
rwOperation[\"name\"] = \"left\";
} catch (std::exception& e) {std::cerr << e.what();exit(1);}
}

void Sf_checker::dumpOutputs(){
try {std::map<std::string, std::string> rwOperation;
rwOperation[\"name\"] = \"left\";
} catch (std::exception& e) {std::cerr << e.what();exit(1);}
}

void Sf_checker::nextMethod(){
}
"""

    def __init__(self, argv, *, cwd, stdout, stderr, text):
        del stdout, stderr, text
        type(self).calls.append(list(argv))
        self.cwd = Path(cwd)
        self.returncode = type(self).returncodes[0 if "-g" in argv else 1]
        if "-g" in argv and self.returncode == 0:
            (self.cwd / argv[argv.index("-g") + 1]).write_text(type(self).generated, encoding="utf-8")
        elif "-o" in argv and self.returncode == 0 and type(self).write_native:
            (self.cwd / argv[argv.index("-o") + 1]).write_bytes(type(self).body)

    def communicate(self, timeout=None):
        del timeout
        return "", type(self).stderr

    def kill(self):
        self.returncode = -9

    def poll(self):
        return self.returncode


def _fake_version(argv, **kwargs):
    del kwargs
    return subprocess.CompletedProcess(argv, 0, "Version: fake-2.5\n", "")


class _RecordingPopen:
    """A compiled checker that echoes its facts back out, recording every spawn.

    Patched over ``souffle.subprocess.Popen``, so every process the shared
    execution path starts -- the bundle's own run and every eligible-bundle
    re-run -- is recorded with its argv, cwd and temp-root contents.
    """
    spawns: list[dict[str, object]] = []

    def __init__(self, argv, *, cwd, stdout, stderr, text):
        del stdout, stderr, text
        root = Path(cwd)
        type(self).spawns.append({
            "argv": list(argv), "cwd": str(root),
            "files": sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()),
        })
        for fact_path in (root / "facts").glob("*.facts"):
            (root / "outputs" / f"{fact_path.stem}.csv").write_bytes(fact_path.read_bytes())
        self.returncode = 0

    def poll(self):
        return 0

    def communicate(self):
        return "", "Warning: OpenMP was not enabled\n"


class CanonicalGeneratedCppTests(unittest.TestCase):
    @staticmethod
    def _source(order: tuple[str, ...]) -> str:
        methods = []
        for method in ("loadAll", "printAll", "dumpInputs", "dumpOutputs"):
            blocks = []
            for relation in order:
                if method in ("loadAll", "printAll"):
                    operation = (
                        'std::map<std::string, std::string> directiveMap('
                        f'{{{{R"_(name)_",R"_({relation})_"}}}});')
                else:
                    operation = (
                        'std::map<std::string, std::string> rwOperation;\n'
                        f'rwOperation["name"] = "{relation}";')
                blocks.append(
                    f"try {{{operation}\n}} catch (std::exception& e) "
                    "{std::cerr << e.what();\nexit(1);\n}\n")
            methods.append(
                f"\nvoid Sf_checker::{method}(){{\n{''.join(blocks)}\n}}\n")
        return "".join(methods) + "\nSymbolTable& Sf_checker::getSymbolTable(){\nreturn symTable;\n}\n"

    def test_relation_io_order_is_canonical_across_all_four_methods(self) -> None:
        left = compiled._canonicalize_output_blocks(self._source(("zeta", "alpha")))
        right = compiled._canonicalize_output_blocks(self._source(("alpha", "zeta")))
        self.assertEqual(left, right)

    def test_relation_io_separator_whitespace_is_canonical(self) -> None:
        spaced = self._source(("alpha", "zeta"))
        joined = spaced.replace("}\ntry {", "}try {")
        self.assertEqual(compiled._canonicalize_output_blocks(spaced),
                         compiled._canonicalize_output_blocks(joined))

    def test_duplicate_relation_output_fails_closed(self) -> None:
        with self.assertRaisesRegex(compiled.CompileError, "duplicate relation outputs"):
            compiled._canonicalize_output_blocks(self._source(("alpha", "alpha")))

    def test_two_name_directives_in_one_block_fail_closed(self) -> None:
        source = self._source(("alpha",))
        name = 'R"_(name)_",R"_(alpha)_"'
        with self.assertRaisesRegex(compiled.CompileError, "exactly one relation name"):
            compiled._canonicalize_output_blocks(source.replace(name, name + name, 1))

    def test_missing_generated_method_fails_closed(self) -> None:
        with self.assertRaisesRegex(compiled.CompileError, "has no dumpOutputs method"):
            compiled._canonicalize_output_blocks(
                self._source(("alpha",)).replace("dumpOutputs", "notDumpOutputs"))


class CompileProgramTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="capcov-compile-unit-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.executable = self.root / "souffle"
        self.executable.write_bytes(b"fake souffle bytes\n")
        self.executable.chmod(0o755)
        (self.root / "souffle-compile.py").write_text("compiler = '/fake/clang++'\n", encoding="utf-8")
        self.cache_dir = self.root / "cache"
        left = RelationDecl("left", (Column("value", "symbol"),))
        derived = RelationDecl("derived", (Column("value", "symbol"),), modality="derived", primitive=False)
        self.bundle = Bundle((left, derived), facts=(Atom("left", (Constant("l"),)),))
        self.program = souffle.program_for_pack(self.bundle)
        _FakeCompiler.calls = []
        _FakeCompiler.returncodes = [0, 0]
        _FakeCompiler.write_native = True

    def _compile(self, **kwargs):
        with (patch.object(compiled.subprocess, "Popen", _FakeCompiler),
              patch.object(compiled.subprocess, "run", _fake_version)):
            return compiled.compile_program(self.program, executable=str(self.executable),
                                            cache_dir=self.cache_dir, **kwargs)

    def test_compiler_config_binds_canonicalizer_source_and_recipe(self) -> None:
        original = compiled._compiler_config_sha256(str(self.executable))
        with patch.object(compiled.inspect, "getsource", return_value="different canonicalizer"):
            changed_source = compiled._compiler_config_sha256(str(self.executable))
        with patch.object(compiled, "COMPILE_FLAGS", (*compiled.COMPILE_FLAGS, "different-recipe")):
            changed_recipe = compiled._compiler_config_sha256(str(self.executable))
        with patch.object(compiled, "_IO_BLOCK", compiled.re.compile("different-pattern")):
            changed_pattern = compiled._compiler_config_sha256(str(self.executable))
        self.assertNotEqual(original, changed_source)
        self.assertNotEqual(original, changed_recipe)
        self.assertNotEqual(original, changed_pattern)

    def test_compile_writes_the_contract_provenance_and_the_cache_layout(self) -> None:
        checker = self._compile()
        self.assertEqual(_FakeCompiler.calls, [
            [str(self.executable), "--no-preprocessor", "-j1", "-g", "checker.cpp", "program.dl"],
            [str((self.root / "souffle-compile.py").resolve()), "checker.cpp", "-o", "checker"],
        ])
        souffle_sha = hashlib.sha256(b"fake souffle bytes\n").hexdigest()
        config_sha = compiled._compiler_config_sha256(str(self.executable))
        key = compiled.compile_key(self.program.program_digest, souffle_sha,
                                   compiler_config_sha256=config_sha)
        entry = self.cache_dir / f"compiled-{key}"
        self.assertEqual(sorted(p.name for p in entry.iterdir()), ["checker", "provenance.json"])
        self.assertEqual(Path(checker.binary_path), entry / "checker")
        self.assertEqual((entry / "checker").stat().st_mode & 0o777, 0o755)
        provenance = json.loads((entry / "provenance.json").read_text(encoding="utf-8"))
        self.assertEqual(set(provenance), CONTRACT_KEYS)
        self.assertEqual(provenance, checker.provenance())
        self.assertEqual(provenance["schema"], "capcov-souffle-compiled-v1")
        self.assertEqual(provenance["compile_key"], key)
        self.assertEqual(provenance["program_digest"], self.program.program_digest)
        self.assertEqual(provenance["binary_sha256"], hashlib.sha256(_FakeCompiler.body).hexdigest())
        self.assertEqual(provenance["souffle_sha256"], souffle_sha)
        self.assertEqual(provenance["souffle_path"], str(self.executable.resolve()))
        self.assertEqual(provenance["souffle_version"], "fake-2.5")
        self.assertEqual(provenance["compiler_config_sha256"], config_sha)
        self.assertEqual(provenance["compile_flags"], list(compiled.COMPILE_FLAGS))
        self.assertIsInstance(provenance["compile_seconds"], float)
        self.assertNotIn("compiled_at", provenance)
        for name in ("compile_key", "program_digest", "binary_sha256", "souffle_sha256", "compiler_config_sha256"):
            self.assertRegex(provenance[name], HEX64, name)
        self.assertEqual(checker.runtime, "souffle-compiled:" + provenance["binary_sha256"][:16])

    def test_second_compile_reuses_the_cached_binary(self) -> None:
        first = self._compile()
        second = self._compile()
        self.assertEqual(len(_FakeCompiler.calls), 2)
        self.assertEqual(first, second)
        self.assertFalse(first.cache_hit)
        self.assertTrue(second.cache_hit)

    def test_a_corrupted_checker_byte_forces_a_recompile(self) -> None:
        checker = self._compile()
        binary = Path(checker.binary_path)
        binary.write_bytes(_FakeCompiler.body[:-1] + b"X")
        again = self._compile()
        self.assertEqual(len(_FakeCompiler.calls), 4)
        self.assertEqual(again.binary_sha256, hashlib.sha256(_FakeCompiler.body).hexdigest())
        self.assertEqual(hashlib.sha256(binary.read_bytes()).hexdigest(), again.binary_sha256)

    def test_an_edited_provenance_forces_a_recompile(self) -> None:
        checker = self._compile()
        provenance_path = Path(checker.binary_path).parent / "provenance.json"
        payload = json.loads(provenance_path.read_text(encoding="utf-8"))
        payload["souffle_version"] = "someone else's souffle"
        provenance_path.write_text(json.dumps(payload), encoding="utf-8")
        self._compile()
        self.assertEqual(len(_FakeCompiler.calls), 4)
        restored = json.loads(provenance_path.read_text(encoding="utf-8"))
        self.assertEqual(restored["souffle_version"], "fake-2.5")
        provenance_path.write_text("{not json", encoding="utf-8")
        self._compile()
        self.assertEqual(len(_FakeCompiler.calls), 6)
        payload = json.loads(provenance_path.read_text(encoding="utf-8"))
        payload["extra"] = True
        provenance_path.write_text(json.dumps(payload), encoding="utf-8")
        self._compile()
        self.assertEqual(len(_FakeCompiler.calls), 8)

    def test_a_different_souffle_executable_is_another_cache_entry_and_prunes_the_old(self) -> None:
        self._compile()
        other = self.root / "souffle-b"
        other.write_bytes(b"other souffle bytes\n")
        other.chmod(0o755)
        with (patch.object(compiled.subprocess, "Popen", _FakeCompiler),
              patch.object(compiled.subprocess, "run", _fake_version)):
            checker = compiled.compile_program(self.program, executable=str(other), cache_dir=self.cache_dir)
        self.assertEqual(len(_FakeCompiler.calls), 4)
        entries = sorted(p.name for p in self.cache_dir.glob("compiled-*"))
        self.assertEqual(entries, [Path(checker.binary_path).parent.name],
                         "entries built by another souffle are pruned")
        with (patch.object(compiled.subprocess, "Popen", _FakeCompiler),
              patch.object(compiled.subprocess, "run", _fake_version)):
            kept = compiled.compile_program(self.program, executable=str(other), cache_dir=self.cache_dir,
                                            prune=False)
            self._compile(prune=False)
        self.assertEqual(len(_FakeCompiler.calls), 6)
        # a cached checker is the checker it reproduces: cache_hit records how
        # it was obtained and is deliberately outside equality and provenance
        self.assertEqual(kept, checker)
        self.assertEqual(kept.provenance(), checker.provenance())
        self.assertTrue(kept.cache_hit)
        self.assertFalse(checker.cache_hit)
        self.assertEqual(len(list(self.cache_dir.glob("compiled-*"))), 2)

    def test_compiler_failure_is_a_compile_error_with_stderr(self) -> None:
        _FakeCompiler.returncodes = [1, 0]
        _FakeCompiler.stderr = "Error: cannot find clang++\n"
        try:
            with self.assertRaises(compiled.CompileError) as raised:
                self._compile()
        finally:
            _FakeCompiler.returncodes = [0, 0]
            _FakeCompiler.stderr = "Warning: variable only occurs once\n" * 3
        self.assertIn("cannot find clang++", str(raised.exception))
        self.assertEqual(list(self.cache_dir.glob("compiled-*")), [])

    def test_native_compiler_failure_is_a_compile_error_with_stderr(self) -> None:
        _FakeCompiler.returncodes = [0, 1]
        _FakeCompiler.stderr = "Error: native compiler rejected generated C++\n"
        with self.assertRaisesRegex(compiled.CompileError, "native compiler rejected"):
            self._compile()
        self.assertEqual(len(_FakeCompiler.calls), 2)
        self.assertEqual(list(self.cache_dir.glob("compiled-*")), [])

    def test_native_compiler_missing_output_fails_closed(self) -> None:
        _FakeCompiler.write_native = False
        with self.assertRaisesRegex(compiled.CompileError, "wrote no checker binary"):
            self._compile()
        self.assertEqual(len(_FakeCompiler.calls), 2)
        self.assertEqual(list(self.cache_dir.glob("compiled-*")), [])

    def test_warnings_on_stderr_with_rc_zero_are_not_failures(self) -> None:
        _FakeCompiler.stderr = "Warning: variable only occurs once in rule\n" * 60
        checker = self._compile()
        self.assertTrue(Path(checker.binary_path).is_file())

    def test_missing_souffle_is_unavailable(self) -> None:
        with self.assertRaises(souffle.SouffleUnavailable):
            compiled.compile_program(self.program, executable=str(self.root / "absent"), cache_dir=self.cache_dir)

    def test_a_missing_compiler_configuration_is_unavailable_not_a_compile_failure(self) -> None:
        """No ``souffle-compile.py`` beside souffle is exit 4, never a kernel verdict."""
        (self.root / "souffle-compile.py").unlink()
        with self.assertRaises(souffle.SouffleUnavailable) as raised:
            self._compile()
        self.assertIn("souffle-compile.py", str(raised.exception))
        self.assertEqual(_FakeCompiler.calls, [], "nothing is compiled without a compiler")
        self.assertEqual(list(self.cache_dir.glob("compiled-*")), [])

    def test_an_unusable_cache_directory_is_unavailable(self) -> None:
        """A cache that cannot be written is the judge's environment, not the receipt's."""
        blocker = self.root / "not-a-directory"
        blocker.write_text("", encoding="utf-8")
        with (patch.object(compiled.subprocess, "Popen", _FakeCompiler),
              patch.object(compiled.subprocess, "run", _fake_version)):
            with self.assertRaises(souffle.SouffleUnavailable) as raised:
                compiled.compile_program(self.program, executable=str(self.executable),
                                         cache_dir=blocker)
        self.assertIn("cache directory is not usable", str(raised.exception))

    def test_an_entry_a_concurrent_compile_installed_is_kept_not_replaced(self) -> None:
        """Two processes miss the same key: the later one must not delete the binary the
        earlier one installed, which that process may be running."""
        first = self._compile()
        installed = Path(first.binary_path)
        inode = installed.stat().st_ino
        real = compiled._cached_checker
        lookups = []

        def lookup(entry, expected):
            # the first lookup misses, as it did before the other compile finished
            lookups.append(entry)
            return None if len(lookups) == 1 else real(entry, expected)

        with patch.object(compiled, "_cached_checker", side_effect=lookup):
            second = self._compile()
        self.assertEqual(len(_FakeCompiler.calls), 4)
        self.assertTrue(second.cache_hit)
        self.assertEqual(second.binary_path, first.binary_path)
        self.assertEqual(installed.stat().st_ino, inode)
        self.assertEqual(sorted(p.name for p in self.cache_dir.iterdir()), [installed.parent.name])

    def test_a_failed_cache_write_leaves_no_partial_entry(self) -> None:
        """A simulated OSError mid-write: no ``compiled-*``, no ``.compiled-*`` residue."""
        with (patch.object(compiled.subprocess, "Popen", _FakeCompiler),
              patch.object(compiled.subprocess, "run", _fake_version),
              patch.object(compiled.shutil, "move", side_effect=OSError("No space left on device"))):
            with self.assertRaises(souffle.SouffleUnavailable) as raised:
                compiled.compile_program(self.program, executable=str(self.executable),
                                         cache_dir=self.cache_dir)
        self.assertIn("could not be written", str(raised.exception))
        self.assertEqual(sorted(p.name for p in self.cache_dir.iterdir()), [])
        # and the next attempt still compiles into a complete entry
        checker = self._compile()
        self.assertTrue(Path(checker.binary_path).is_file())
        self.assertEqual(sorted(p.name for p in Path(checker.binary_path).parent.iterdir()),
                         ["checker", "provenance.json"])

    def test_a_relative_cache_dir_still_yields_a_runnable_binary_path(self) -> None:
        """The checker is spawned from a temp root: a relative argv[0] would not resolve."""
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        relative = Path(".capcov") / "compiled"
        with (patch.object(compiled.subprocess, "Popen", _FakeCompiler),
              patch.object(compiled.subprocess, "run", _fake_version)):
            checker = compiled.compile_program(self.program, executable=str(self.executable),
                                               cache_dir=relative)
            again = compiled.compile_program(self.program, executable=str(self.executable),
                                             cache_dir=relative)
        self.assertTrue(Path(checker.binary_path).is_absolute(), checker.binary_path)
        self.assertTrue(again.cache_hit)
        self.assertEqual(again.binary_path, checker.binary_path)
        self.assertEqual(Path(checker.binary_path).parent.parent, (Path.cwd() / relative))

        # the real defect: spawned with cwd = a temp root, the relative
        # spelling raises ENOENT while the recorded path runs
        elsewhere = Path(tempfile.mkdtemp(dir=self.root))
        self.assertEqual(subprocess.run([checker.binary_path], cwd=elsewhere,
                                        capture_output=True).returncode, 0)
        relative_argv = str(relative / Path(checker.binary_path).parent.name / "checker")
        with self.assertRaises(OSError):
            subprocess.run([relative_argv], cwd=elsewhere, capture_output=True)

        # and through the shared execution path the argv is that absolute binary
        _RecordingPopen.spawns = []
        with patch.object(souffle.subprocess, "Popen", _RecordingPopen):
            compiled.run_compiled(self.bundle, checker)
        self.assertEqual([spawn["argv"] for spawn in _RecordingPopen.spawns],
                         [[checker.binary_path, "-j1"]])


class RunCompiledTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="capcov-run-compiled-unit-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        left = RelationDecl("left", (Column("value", "symbol"),))
        derived = RelationDecl("derived", (Column("value", "symbol"),), modality="derived", primitive=False)
        self.bundle = Bundle((left, derived), facts=(Atom("left", (Constant("l"),)),))
        binary = self.root / "checker"
        binary.write_bytes(b"#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
        self.checker = compiled.CompiledChecker(
            program_digest="0" * 64, binary_path=str(binary), binary_sha256="1" * 64,
            souffle_path="/fake/souffle", souffle_sha256="2" * 64, souffle_version="",
            compile_flags=compiled.COMPILE_FLAGS, compile_key="3" * 64, compiler_config_sha256="4" * 64)

    def test_program_mismatch_is_refused_before_anything_runs(self) -> None:
        with patch.object(souffle.subprocess, "Popen") as popen:
            with self.assertRaises(compiled.CompiledProgramMismatch):
                compiled.run_compiled(self.bundle, self.checker)
        popen.assert_not_called()
        report = run_souffle_compiled(self.bundle, checker=self.checker)
        self.assertEqual(report.backend, "souffle-compiled")
        self.assertEqual(report.operational_failure, "compiled-program-mismatch")
        self.assertEqual(report.relations, ())
        self.assertIsNone(report.closure_digest)

    def test_compile_failure_maps_to_souffle_compile_failed(self) -> None:
        with patch.object(compiled, "compile_program", side_effect=compiled.CompileError("clang++ missing")):
            with patch("capcov.claims.differential.compile_program",
                       side_effect=compiled.CompileError("clang++ missing")):
                report = run_souffle_compiled(self.bundle, cache_dir=self.root / "cache")
        self.assertEqual(report.operational_failure, "souffle-compile-failed")
        self.assertIn("clang++ missing", report.message)

    def test_missing_souffle_maps_to_souffle_unavailable(self) -> None:
        report = run_souffle_compiled(self.bundle, cache_dir=self.root / "cache",
                                      executable=str(self.root / "no-such-souffle"))
        self.assertEqual(report.operational_failure, "souffle-unavailable")

    def test_partial_outputs_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            compiled.run_compiled(self.bundle, self.checker, outputs=("left",))

    def test_the_binary_runs_with_the_compiled_argv_in_the_shared_temp_root(self) -> None:
        program = souffle.program_for_pack(self.bundle)
        checker = replace(self.checker, program_digest=program.program_digest)
        _RecordingPopen.spawns = []
        with patch.object(souffle.subprocess, "Popen", _RecordingPopen):
            result = compiled.run_compiled(self.bundle, checker)
        self.assertEqual(len(_RecordingPopen.spawns), 1)
        spawn = _RecordingPopen.spawns[0]
        self.assertEqual(spawn["argv"], [str(self.root / "checker"), "-j1"])
        self.assertEqual(spawn["files"], ["facts/derived.facts", "facts/left.facts", "program.dl"])
        self.assertEqual(result.runtime, "souffle-compiled:" + "1" * 16)
        self.assertEqual(result.program_digest, program.program_digest)
        self.assertEqual(result.relations["left"], (("l",),))

    def test_the_eligible_bundle_rerun_stays_inside_the_compiled_binary(self) -> None:
        """The fold's forbidden-assumption re-run is the binary again, not the interpreter.

        The shared ``_execute`` recomputes the closure on the eligible bundle
        through the ``rerun`` its caller passes.  A compiled kernel whose
        ``rerun`` fell back to ``run_bundle`` would answer claim rows from the
        interpreter while reporting a compiled runtime, and no corpus case
        reaches this path often enough to notice: every spawn must be the
        checker binary, and the budget must be charged for each one.
        """
        bundle = _forbidden_assumption_bundle()
        program = souffle.program_for_pack(bundle)
        checker = replace(self.checker, program_digest=program.program_digest)
        budget = souffle._ExecutionBudget(time.monotonic() + 60.0, 8)
        _RecordingPopen.spawns = []
        with patch.object(souffle.subprocess, "Popen", _RecordingPopen):
            result = compiled.run_compiled(bundle, checker, _budget=budget)
        self.assertGreater(len(_RecordingPopen.spawns), 1,
                           "the forbidden assumption must force an eligible-bundle re-run")
        for index, spawn in enumerate(_RecordingPopen.spawns):
            with self.subTest(spawn=index):
                self.assertEqual(spawn["argv"], [str(self.root / "checker"), "-j1"])
                self.assertNotIn("program.dl", spawn["argv"], "the interpreter's argv")
        self.assertEqual(budget.remaining_processes, 8 - len(_RecordingPopen.spawns),
                         "every re-run is charged to the shared process budget")
        self.assertEqual(result.runtime, "souffle-compiled:" + "1" * 16)


if __name__ == "__main__":
    unittest.main()
