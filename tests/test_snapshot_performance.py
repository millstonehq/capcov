from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from capcov import artifacts, cli
from capcov.outcomes import provenance as outcome_provenance
from capcov.probes import load_probe, pytest_probe


def legacy_tree_digest(root: Path, patterns: tuple[str, ...]) -> tuple[str, int]:
    entries = []
    for pattern in patterns:
        for path in sorted(root.glob(pattern)):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            entries.append(
                f"{path.relative_to(root).as_posix()} "
                f"{hashlib.sha256(path.read_bytes()).hexdigest()}"
            )
    manifest = "\n".join(sorted(set(entries)))
    return hashlib.sha256(manifest.encode()).hexdigest(), len(set(entries))


class IncrementalTreeTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "source"
        self.cache = Path(temporary.name) / "cache"
        self.root.mkdir()
        (self.root / "a.py").write_text("x = 1\n")
        (self.root / "b.py").write_text("y = 2\n")

    def test_cold_and_warm_keep_the_exact_legacy_manifest_digest(self) -> None:
        cold = artifacts.snapshot_tree(self.root, cache_dir=self.cache)
        warm = artifacts.snapshot_tree(self.root, cache_dir=self.cache)
        self.assertEqual((cold.digest, cold.files), legacy_tree_digest(self.root, ("**/*.py",)))
        self.assertEqual((warm.digest, warm.files), (cold.digest, cold.files))
        self.assertEqual(cold.verification["cache"], "miss")
        self.assertEqual(cold.verification["files_hashed"], 2)
        self.assertEqual(warm.verification["cache"], "hit")
        self.assertEqual(warm.verification["files_reused"], 2)
        self.assertEqual(warm.verification["files_hashed"], 0)

    def test_metadata_mismatch_rehashes_even_when_content_is_unchanged(self) -> None:
        cold = artifacts.snapshot_tree(self.root, cache_dir=self.cache)
        path = self.root / "a.py"
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        refreshed = artifacts.snapshot_tree(self.root, cache_dir=self.cache)
        self.assertEqual(refreshed.digest, cold.digest)
        self.assertEqual(refreshed.verification["files_hashed"], 1)
        self.assertEqual(refreshed.verification["files_reused"], 1)

    def test_same_size_content_change_with_restored_mtime_is_not_a_false_hit(self) -> None:
        before = artifacts.snapshot_tree(self.root, cache_dir=self.cache)
        path = self.root / "a.py"
        stat = path.stat()
        path.write_text("x = 9\n")
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        # ctime/inode/device are conservative identity fields in addition to the
        # required relative path + size + mtime_ns key.
        after = artifacts.snapshot_tree(self.root, cache_dir=self.cache)
        self.assertNotEqual(after.digest, before.digest)
        self.assertGreaterEqual(after.verification["files_hashed"], 1)

    def test_corrupt_cache_falls_back_to_content_and_repairs_itself(self) -> None:
        expected = artifacts.snapshot_tree(self.root, cache_dir=self.cache)
        cache_file = next(self.cache.glob("*.json"))
        cache_file.write_text("{not-json")
        fallback = artifacts.snapshot_tree(self.root, cache_dir=self.cache)
        self.assertEqual(fallback.digest, expected.digest)
        self.assertEqual(fallback.verification["cache"], "fallback")
        self.assertEqual(fallback.verification["files_hashed"], 2)
        self.assertEqual(artifacts.snapshot_tree(self.root, cache_dir=self.cache).verification["cache"], "hit")

    def test_tampered_integrity_field_falls_back_to_content(self) -> None:
        expected = artifacts.snapshot_tree(self.root, cache_dir=self.cache)
        index = next(self.cache.glob("*.json"))
        document = json.loads(index.read_text())
        document["entries"]["a.py"]["sha256"] = "0" * 64
        index.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")))
        fallback = artifacts.snapshot_tree(self.root, cache_dir=self.cache)
        self.assertEqual((fallback.digest, fallback.files), (expected.digest, expected.files))
        self.assertEqual(fallback.verification["cache"], "fallback")
        self.assertEqual(fallback.verification["files_hashed"], 2)

    def test_index_is_replaced_in_place_and_never_accumulates_objects(self) -> None:
        for edit in range(8):
            (self.root / "a.py").write_text(f"x = {edit}\n")
            artifacts.snapshot_tree(self.root, cache_dir=self.cache)
        # One index per (root, patterns) and nothing else: the superseded
        # content-addressed store leaked one orphan object per edit.
        self.assertEqual(sorted(p.name for p in self.cache.iterdir()),
                         [next(self.cache.glob("*.json")).name])
        self.assertFalse((self.cache / "cas").exists())

    def test_a_writable_cache_cannot_forge_a_verified_digest(self) -> None:
        captured = artifacts.snapshot_tree(self.root, cache_dir=self.cache)
        # Exactly what an exercised process can do: change the source, then
        # rewrite the shared cache so the new stat identity maps to the old
        # digest. A cache-backed verification would call this unchanged.
        (self.root / "a.py").write_text("x = 99\n")
        info = (self.root / "a.py").stat()
        index = next(self.cache.glob("*.json"))
        document = json.loads(index.read_text())
        document["entries"]["a.py"].update(
            {
                "size": info.st_size,
                "mtime_ns": info.st_mtime_ns,
                "ctime_ns": info.st_ctime_ns,
                "device": info.st_dev,
                "inode": info.st_ino,
                "mode": info.st_mode,
            }
        )
        document.pop("integrity_sha256")
        document["integrity_sha256"] = hashlib.sha256(
            json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        index.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")))
        self.assertEqual(
            artifacts.snapshot_tree(self.root, cache_dir=self.cache).digest,
            captured.digest,
        )  # the cheap begin-side read is fooled ...
        with self.assertRaises(ValueError):  # ... the verification walk is not
            captured.verify()
        # And a cache poisoned BEFORE the capture is caught too, because the
        # verification walk re-reads bytes rather than a remembered identity:
        # whatever the begin-side believed, a published digest is content-bound.
        poisoned = artifacts.snapshot_tree(self.root, cache_dir=self.cache)
        self.assertEqual(poisoned.verification["cache"], "hit")
        with self.assertRaises(ValueError):
            poisoned.verify()
        self.assertEqual(
            artifacts.snapshot_tree(self.root, trust_cache=False).digest,
            legacy_tree_digest(self.root, ("**/*.py",))[0],
        )

    def test_env_opt_out_binds_every_walk_to_exact_bytes(self) -> None:
        artifacts.snapshot_tree(self.root, cache_dir=self.cache)
        with patch.dict(os.environ, {artifacts.ENV_NO_CACHE: "1"}):
            snapshot = artifacts.snapshot_tree(self.root, cache_dir=self.cache)
        self.assertEqual(snapshot.verification["cache"], "disabled")
        self.assertEqual(snapshot.verification["files_hashed"], 2)
        self.assertEqual(snapshot.digest, legacy_tree_digest(self.root, ("**/*.py",))[0])

    def test_group_writable_cache_is_refused(self) -> None:
        artifacts.snapshot_tree(self.root, cache_dir=self.cache)
        self.cache.chmod(0o770)
        snapshot = artifacts.snapshot_tree(self.root, cache_dir=self.cache)
        self.assertEqual(snapshot.verification["cache"], "disabled")
        self.assertEqual(snapshot.verification["files_hashed"], 2)

    def test_unwritable_or_invalid_cache_location_is_optional(self) -> None:
        blocked = self.cache
        blocked.write_text("not a directory")
        snapshot = artifacts.snapshot_tree(self.root, cache_dir=blocked)
        self.assertEqual((snapshot.digest, snapshot.files), legacy_tree_digest(self.root, ("**/*.py",)))
        self.assertEqual(snapshot.verification["cache"], "fallback")

    def test_cache_inside_source_is_disabled_and_never_changes_semantics(self) -> None:
        inside = self.root / ".capcov-cache"
        snapshot = artifacts.snapshot_tree(self.root, ("**/*",), cache_dir=inside)
        self.assertEqual(snapshot.verification["cache"], "disabled")
        self.assertFalse(inside.exists())


class PatternAndProvenanceTests(unittest.TestCase):
    def test_language_defaults_cover_every_discoverable_language(self) -> None:
        for language, expected in (
            ("go", "**/*.go"),
            ("php", "**/*.php"),
            ("javascript", "**/*.js"),
            ("typescript", "**/*.ts"),
            ("tsx", "**/*.tsx"),
        ):
            self.assertEqual(
                cli._source_patterns([("treesitter-routes", {"language": language})]),
                (expected,),
                language,
            )

    def test_unknown_language_is_refused_not_hashed_as_python(self) -> None:
        # Handing an unknown language `**/*.py` parsed one language's tree with
        # another's grammar and published the Python digest under its name.
        with self.assertRaises(SystemExit) as caught:
            cli._source_patterns([("treesitter-routes", {"language": "rust"})])
        self.assertIn("rust", str(caught.exception))
        self.assertIn("globs", str(caught.exception))
        # An ABSENT language keeps the historic default.
        self.assertEqual(artifacts.language_pattern(None), "**/*.py")

    def test_go_and_php_defaults_count_files_and_never_use_the_empty_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.go").write_text("package main\n")
            (root / "index.php").write_text("<?php echo 1;\n")
            empty = hashlib.sha256(b"").hexdigest()
            for language, pattern in (("go", "**/*.go"), ("php", "**/*.php")):
                digest, count = artifacts.tree_sha256(root, (pattern,), cache_dir=root.parent / f"cache-{language}")
                self.assertEqual(count, 1)
                self.assertNotEqual(digest, empty)
                derived = artifacts.provenance("src", digest, "test", count, (pattern,))
                self.assertEqual(derived["source_patterns"], [pattern])

    def test_carried_identity_is_published_provisional_and_walks_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "src"
            source.mkdir()
            (source / "pipeline.py").write_text("STAGES = []\n")
            snapshot = artifacts.snapshot_tree(source, cache_dir=root / "cache")
            # A cold miss IS exact in the driver's process, but what it hands
            # over is provisional: the artifact is published after the exercise.
            self.assertTrue(snapshot.exact)
            derived = artifacts.mark_provisional(
                snapshot.provenance("src", "capcov source-snapshot")
            )
            carried = artifacts.carried_source_snapshot(source, derived)
            self.assertTrue(carried.carried)
            self.assertFalse(carried.exact)
            with patch.object(
                artifacts, "snapshot_tree", side_effect=AssertionError("walked the tree")
            ):
                # Driven by capcov observe: the probe walks the oracle ZERO times.
                # The driver owns the one exact verification after this returns.
                result = load_probe.observe(
                    source_root=source,
                    out=root / "observed.json",
                    source_snapshot=carried,
                    source_provenance=derived,
                )
            for key in ("artifact", "artifact_sha256", "artifact_files"):
                self.assertEqual(result["derived_from"][key], derived[key])
            self.assertEqual(result["derived_from"]["extractor"], "capcov load-probe (stub)")
            # ... and what it publishes is provisional until the driver stamps it.
            self.assertEqual(
                artifacts.snapshot_verification_of(result["derived_from"]), "cached"
            )

    def test_standalone_probe_is_the_boundary_and_publishes_exact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "src"
            source.mkdir()
            (source / "pipeline.py").write_text("STAGES = []\n")
            # Warm the cache so the begin-side capture is a hit ...
            artifacts.snapshot_tree(source)
            result = load_probe.observe(source_root=source, out=root / "observed.json")
            # ... and the published digest is still exact: the guard's verify
            # read the bytes and the probe published from THAT snapshot.
            self.assertEqual(
                artifacts.snapshot_verification_of(result["derived_from"]), "exact"
            )
            self.assertTrue(result["timing"]["source_verification"]["exact"])

    def test_driver_performs_exactly_one_exact_walk_and_stamps_the_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "src"
            source.mkdir()
            (source / "a.py").write_text("x = 1\n")
            out = root / "observed.json"
            snapshot = artifacts.snapshot_tree(source, cache_dir=root / "cache")
            derived = snapshot.provenance("src", "capcov source-snapshot")
            self.assertEqual(derived["source_snapshot"]["verification"], "exact")  # cold miss
            warm = artifacts.snapshot_tree(source, cache_dir=root / "cache")
            provisional = warm.provenance("src", "capcov source-snapshot")
            self.assertEqual(provisional["source_snapshot"]["verification"], "cached")
            # What the child leaves behind: a provisional artifact.
            artifacts.write(out, "observed", {**provisional, "extractor": "probe"}, {"bindings": []})

            real_hash = artifacts._hash_file
            hashed: list[Path] = []

            def counting(path, before):
                hashed.append(path)
                return real_hash(path, before)

            with patch.object(artifacts, "_hash_file", side_effect=counting):
                rc = cli._publish_verified(warm, out, time.perf_counter_ns())
            self.assertEqual(rc, 0)
            # ONE exact walk: every selected file read from bytes, once.
            self.assertEqual(sorted(p.name for p in hashed), ["a.py"])
            stamped = artifacts.read(out, "observed")
            self.assertEqual(
                artifacts.snapshot_verification_of(stamped["derived_from"]), "exact"
            )
            self.assertTrue(stamped["timing"]["source_verification"]["exact"])
            self.assertEqual(stamped["derived_from"]["extractor"], "probe")

    def test_driver_discards_an_artifact_that_names_a_different_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "src"
            source.mkdir()
            (source / "a.py").write_text("x = 1\n")
            out = root / "observed.json"
            snapshot = artifacts.snapshot_tree(source, cache_dir=root / "cache")
            derived = snapshot.provenance("src", "capcov source-snapshot")
            artifacts.write(
                out, "observed", {**derived, "artifact_sha256": "f" * 64}, {"bindings": []}
            )
            rc = cli._publish_verified(snapshot, out, time.perf_counter_ns())
            self.assertEqual(rc, 1)
            self.assertFalse(out.exists())

    def test_reconcile_refuses_a_provisional_observed_and_confirms_a_cached_discover(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "src"
            source.mkdir()
            (source / "a.py").write_text("x = 1\n")
            artifacts.snapshot_tree(source, cache_dir=root / "cache")  # warm
            cached = artifacts.snapshot_tree(source, cache_dir=root / "cache")
            exact = artifacts.snapshot_tree(source, trust_cache=False)
            self.assertEqual(cached.digest, exact.digest)
            body = {"entities": [], "surfaces": [], "capabilities": [], "blind_spots": []}
            observed_body = {"bindings": [], "exercises": 1}
            capabilities = root / "capabilities.json"
            observed = root / "observed.json"
            coverage = root / "coverage.json"
            artifacts.write(
                capabilities, "capabilities", cached.provenance("src", "capcov discover"), body
            )

            # A provisional observed never reaches coverage.
            artifacts.write(
                observed, "observed", cached.provenance("src", "probe"), observed_body
            )
            with self.assertRaises(SystemExit) as refused:
                cli.main(
                    ["reconcile", str(capabilities), str(observed), "--out", str(coverage), "--quiet"]
                )
            self.assertIn("provisional", str(refused.exception))
            self.assertFalse(coverage.exists())

            # An exact observed over the same tree confirms the cached discover,
            # and the coverage artifact carries the confirmed identity.
            artifacts.write(
                observed, "observed", exact.provenance("src", "probe"), observed_body
            )
            rc = cli.main(
                ["reconcile", str(capabilities), str(observed), "--out", str(coverage), "--quiet"]
            )
            self.assertEqual(rc, 0)
            self.assertEqual(
                artifacts.snapshot_verification_of(
                    artifacts.read(coverage, "coverage")["derived_from"]
                ),
                "exact",
            )

    def test_a_forged_discover_digest_cannot_reach_coverage(self) -> None:
        """The P1 scenario end to end: discover's cache is forged, so its digest
        names bytes that are not on disk. The exact observed walk disagrees, and
        reconcile refuses -- a cached digest becomes evidence only by agreeing
        with one that was read."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "src"
            source.mkdir()
            (source / "a.py").write_text("x = 1\n")
            cache = root / "cache"
            honest = artifacts.snapshot_tree(source, cache_dir=cache)
            (source / "a.py").write_text("x = 2\n")
            info = (source / "a.py").stat()
            index = next(cache.glob("*.json"))
            document = json.loads(index.read_text())
            document["entries"]["a.py"].update(
                {
                    "size": info.st_size,
                    "mtime_ns": info.st_mtime_ns,
                    "ctime_ns": info.st_ctime_ns,
                    "device": info.st_dev,
                    "inode": info.st_ino,
                    "mode": info.st_mode,
                }
            )
            document.pop("integrity_sha256")
            document["integrity_sha256"] = hashlib.sha256(
                json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            index.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")))
            forged = artifacts.snapshot_tree(source, cache_dir=cache)
            self.assertEqual(forged.digest, honest.digest)  # discover is fooled ...
            self.assertFalse(forged.exact)  # ... but says so

            capabilities = root / "capabilities.json"
            observed = root / "observed.json"
            body = {"entities": [], "surfaces": [], "capabilities": [], "blind_spots": []}
            artifacts.write(
                capabilities, "capabilities", forged.provenance("src", "capcov discover"), body
            )
            exact = artifacts.snapshot_tree(source, trust_cache=False)
            artifacts.write(
                observed, "observed", exact.provenance("src", "probe"), {"bindings": [], "exercises": 1}
            )
            with self.assertRaises(SystemExit) as refused:
                cli.main(["reconcile", str(capabilities), str(observed), "--out", str(root / "c.json"), "--quiet"])
            self.assertIn("Re-run both against the same tree", str(refused.exception))

    def test_legacy_artifact_without_optional_snapshot_still_reads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "src"
            source.mkdir()
            (source / "a.py").write_text("x = 1\n")
            digest, count = artifacts.tree_sha256(source, cache_dir=root / "cache")
            legacy = {
                "schema_version": 1,
                "kind": "capabilities",
                "derived_from": {
                    "artifact": "src",
                    "artifact_sha256": digest,
                    "artifact_files": count,
                    "extractor": "legacy",
                    "extracted_at": "2026-01-01T00:00:00+00:00",
                },
            }
            path = root / "legacy.json"
            path.write_text(json.dumps(legacy))
            self.assertEqual(artifacts.read(path), legacy)
            self.assertEqual(artifacts.source_snapshot_of(source, legacy["derived_from"]).digest, digest)

    def test_outcome_provenance_reuses_selected_source_file_digests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "src"
            tests = root / "tests"
            source.mkdir()
            tests.mkdir()
            source_file = source / "a.py"
            source_file.write_text("x = 1\n")
            test_file = tests / "test_a.py"
            test_file.write_text("def test_a(): pass\n")
            snapshot = artifacts.snapshot_tree(source, cache_dir=root / "cache")
            inventory = {
                "schema_version": 1,
                "kind": "capabilities",
                "surfaces": [{"id": "GET /a"}],
                "derived_from": snapshot.provenance("src", "test"),
            }
            mapping = {
                "version": 1,
                "scope": "a",
                "environment": "fixture",
                "limitations": ["none"],
                "inputs": ["src", "tests"],
                "outcomes": [{
                    "id": "a",
                    "capability": "a",
                    "description": "a",
                    "source_refs": ["GET /a"],
                    "policy": "required",
                    "tests": ["tests/test_a.py::test_a"],
                }],
            }
            real_read_bytes = Path.read_bytes

            def reject_source_reads(path: Path) -> bytes:
                if path.resolve().is_relative_to(source.resolve()):
                    raise AssertionError(f"source was read again: {path}")
                return real_read_bytes(path)

            with patch.object(Path, "read_bytes", reject_source_reads):
                result = outcome_provenance(root, mapping, inventory, snapshot)
            self.assertEqual(
                result["input_files"]["src/a.py"],
                snapshot.entries[0][1],
            )

    def test_reconcile_aggregates_all_phase_timings_and_normalise_ignores_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            derived = {
                "artifact": "src",
                "artifact_sha256": "a" * 64,
                "artifact_files": 1,
                "extractor": "test",
                "extracted_at": "2026-01-01T00:00:00+00:00",
            }
            capabilities = {
                "schema_version": 1,
                "kind": "capabilities",
                "derived_from": derived,
                "timing": {"discover_ms": 7},
                "entities": [],
                "surfaces": [],
                "capabilities": [],
                "blind_spots": [],
                "residue": [],
            }
            observed = {
                "schema_version": 1,
                "kind": "observed",
                "derived_from": {**derived, "extractor": "probe"},
                "timing": {"observe_ms": 11},
                "bindings": [],
            }
            capabilities_path = root / "capabilities.json"
            observed_path = root / "observed.json"
            output = root / "coverage.json"
            artifacts.write_document(capabilities_path, capabilities)
            artifacts.write_document(observed_path, observed)
            self.assertEqual(
                cli.main(
                    [
                        "reconcile",
                        str(capabilities_path),
                        str(observed_path),
                        "--out",
                        str(output),
                        "--quiet",
                    ]
                ),
                0,
            )
            coverage = artifacts.read(output, "coverage")
            self.assertEqual(coverage["timing"]["discover_ms"], 7)
            self.assertEqual(coverage["timing"]["observe_ms"], 11)
            self.assertIn("reconcile_ms", coverage["timing"])
            self.assertEqual(
                coverage["timing"]["total_ms"],
                18 + coverage["timing"]["reconcile_ms"],
            )
            changed_timing = {**coverage, "timing": {"total_ms": 999999}}
            self.assertEqual(
                artifacts.normalise(coverage), artifacts.normalise(changed_timing)
            )

    def test_pytest_probe_under_a_driver_walks_nothing_and_keeps_the_carried_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "src"
            source.mkdir()
            (source / "a.py").write_text("x = 1\n")
            out = root / "observed.json"
            snapshot = artifacts.snapshot_tree(source, cache_dir=root / "cache")
            derived = artifacts.mark_provisional(
                snapshot.provenance("src", "capcov source-snapshot")
            )

            def fake_dump(path, source_root, exercises, **kwargs):
                # What python_probe.dump does with a carried provenance.
                artifacts.write(
                    path,
                    "observed",
                    {**kwargs["source_provenance"], "extractor": "capcov python-probe"},
                    {"bindings": []},
                )

            old_enabled, old_started = pytest_probe._ENABLED, pytest_probe._STARTED_NS
            self.addCleanup(setattr, pytest_probe, "_ENABLED", old_enabled)
            self.addCleanup(setattr, pytest_probe, "_STARTED_NS", old_started)
            pytest_probe._ENABLED = True
            pytest_probe._STARTED_NS = time.perf_counter_ns()
            with (
                patch.object(pytest_probe.python_probe, "dump", side_effect=fake_dump),
                patch.object(
                    artifacts, "snapshot_tree", side_effect=AssertionError("walked the tree")
                ),
                patch.dict(
                    os.environ,
                    {
                        "CAPCOV_OUT": str(out),
                        "CAPCOV_SOURCE_ROOT": str(source),
                        "CAPCOV_SOURCE_PROVENANCE": json.dumps(derived),
                    },
                    clear=False,
                ),
            ):
                pytest_probe.pytest_sessionfinish(SimpleNamespace(), 0)
            result = artifacts.read(out, "observed")
            self.assertEqual(result["derived_from"]["artifact_sha256"], snapshot.digest)
            self.assertEqual(result["derived_from"]["extractor"], "capcov python-probe")
            self.assertEqual(
                artifacts.snapshot_verification_of(result["derived_from"]), "cached"
            )
            self.assertIn("observe_ms", result["timing"])
            # No source_verification: this process read no bytes and says none.
            self.assertNotIn("source_verification", result["timing"])


if __name__ == "__main__":
    unittest.main()
