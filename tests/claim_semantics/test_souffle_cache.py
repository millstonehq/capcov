from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from capcov.claims import Atom, Column, Constant, RelationDecl
from tests.claim_fixtures import Bundle  # attributed fixture bundles
from capcov.claims import souffle


class _FakePopen:
    calls = 0

    def __init__(self, _command, *, cwd, stdout, stderr, text):
        del stdout, stderr, text
        type(self).calls += 1
        root = Path(cwd)
        for fact_path in (root / "facts").glob("*.facts"):
            (root / "outputs" / f"{fact_path.stem}.csv").write_bytes(
                fact_path.read_bytes())
        self.returncode = 0

    def poll(self):
        return self.returncode

    def communicate(self):
        return "Version: fake-souffle\n", ""

    def kill(self):
        self.returncode = -9


class SouffleTranslationCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.executable = self._make_executable("souffle-a", b"fake executable a\n")
        self.other_executable = self._make_executable("souffle-b", b"fake executable b\n")
        self.cache_dir = self.root / "cache"
        left = RelationDecl("left", (Column("value", "symbol"),))
        right = RelationDecl("right", (Column("value", "symbol"),))
        self.bundle = Bundle(
            (left, right),
            facts=(Atom("left", (Constant("l"),)),
                   Atom("right", (Constant("r"),))),
        )
        _FakePopen.calls = 0

    def _make_executable(self, name: str, content: bytes) -> Path:
        path = self.root / name
        path.write_bytes(content)
        path.chmod(0o755)
        return path

    def _run(self, **kwargs):
        return souffle.run_bundle(
            self.bundle,
            executable=str(self.executable),
            timeout=1.0,
            max_rows=50,
            max_output_bytes=1_000,
            max_processes=4,
            **kwargs,
        )

    def test_cache_hit_reuses_translation_but_still_executes_souffle(self):
        with (patch.object(souffle.subprocess, "Popen", _FakePopen),
              patch.object(souffle, "translate_bundle",
                           wraps=souffle.translate_bundle) as translate):
            first = self._run(cache_dir=self.cache_dir, outputs=("left",))
            second = self._run(cache_dir=self.cache_dir, outputs=("left",))

        self.assertEqual(translate.call_count, 1)
        self.assertEqual(_FakePopen.calls, 2)
        self.assertEqual(first.relations, second.relations)
        self.assertEqual(first.output_digest, second.output_digest)
        self.assertEqual(len(tuple(self.cache_dir.glob("translation-*.json"))), 1)

    def test_cache_key_separates_bundle_outputs_executable_and_every_limit(self):
        variants = (
            {"outputs": ("left",)},
            {"outputs": ("right",)},
            {"outputs": ("left",), "timeout": 2.0},
            {"outputs": ("left",), "max_rows": 51},
            {"outputs": ("left",), "max_output_bytes": 1_001},
            {"outputs": ("left",), "max_processes": 5},
            {"outputs": ("left",), "executable": str(self.other_executable)},
        )
        defaults = {"executable": str(self.executable), "timeout": 1.0,
                    "max_rows": 50, "max_output_bytes": 1_000,
                    "max_processes": 4, "cache_dir": self.cache_dir}
        changed_bundle = Bundle(
            self.bundle.relations,
            facts=(Atom("left", (Constant("changed"),)),
                   Atom("right", (Constant("r"),))),
        )
        with (patch.object(souffle.subprocess, "Popen", _FakePopen),
              patch.object(souffle, "translate_bundle",
                           wraps=souffle.translate_bundle) as translate):
            souffle.run_bundle(self.bundle, **(defaults | variants[0]))
            souffle.run_bundle(self.bundle, **(defaults | variants[0]))
            for variant in variants[1:]:
                souffle.run_bundle(self.bundle, **(defaults | variant))
            souffle.run_bundle(changed_bundle, **(defaults | variants[0]))

        self.assertEqual(translate.call_count, len(variants) + 1)
        self.assertEqual(len(tuple(self.cache_dir.glob("translation-*.json"))),
                         len(variants) + 1)

    def test_corrupt_and_malformed_entries_fall_back_and_are_replaced(self):
        with (patch.object(souffle.subprocess, "Popen", _FakePopen),
              patch.object(souffle, "translate_bundle",
                           wraps=souffle.translate_bundle) as translate):
            self._run(cache_dir=self.cache_dir)
            cache_path, = self.cache_dir.glob("translation-*.json")

            cache_path.write_text("{not-json", encoding="utf-8")
            self._run(cache_dir=self.cache_dir)

            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            payload["program"]["facts"]["left"] = "corrupt\n"
            cache_path.write_text(json.dumps(payload), encoding="utf-8")
            self._run(cache_dir=self.cache_dir)

            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            payload["schema"] = "wrong-schema"
            cache_path.write_text(json.dumps(payload), encoding="utf-8")
            self._run(cache_dir=self.cache_dir)

        self.assertEqual(translate.call_count, 4)
        repaired = json.loads(cache_path.read_text(encoding="utf-8"))
        self.assertEqual(repaired["schema"], souffle._TRANSLATION_CACHE_SCHEMA)

    def test_default_does_not_read_or_persist_a_cache(self):
        with (patch.object(souffle.subprocess, "Popen", _FakePopen),
              patch.object(souffle, "translate_bundle",
                           wraps=souffle.translate_bundle) as translate):
            self._run()
            self._run()

        self.assertEqual(translate.call_count, 2)
        self.assertEqual(_FakePopen.calls, 2)
        self.assertFalse(self.cache_dir.exists())


if __name__ == "__main__":
    unittest.main()
