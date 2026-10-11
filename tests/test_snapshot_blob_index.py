"""The shared, git-blob-keyed digest index behind `artifacts.snapshot_tree`.

A fresh `git worktree add` gives every file new metadata, so the per-root
manifest starts cold.  These tests pin what the blob index may and may not do
about that: reuse digests only for tracked files git itself judges clean and
would not convert on checkout; count that reuse as inexact; resolve any
disagreement between caches by reading bytes; never learn a digest for a file
that moved under it; ignore a shard that fails its integrity check; and stay
out of every verification walk.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capcov import artifacts

GIT = shutil.which("git")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@unittest.skipUnless(GIT, "git is not on PATH")
class BlobIndexTest(unittest.TestCase):
    PATTERNS = ("**/*.txt", "**/*.bin")
    FILES = {"a.txt": b"alpha\n", "sub/b.txt": b"beta\n", "sub/deep/c.txt": b"gamma\n"}

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="capcov-blob-index-")
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        # A clean git identity for BOTH the test's and the module's git calls:
        # no global hooks, filters or autocrlf from the developer's machine.
        env = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
               "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        env.pop("CAPCOV_NO_CACHE", None)
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("CAPCOV_NO_CACHE", None)
        self.repo = base / "repo"
        self.cache = base / "cache"
        self.blobs = base / "cache-blobs"  # what `_blob_index_dir_for` derives
        self.repo.mkdir()
        self._git("init", "-q", "-b", "main")
        for relative, data in self.FILES.items():
            (self.repo / relative).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / relative).write_bytes(data)
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "initial")

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        result = subprocess.run([GIT, *args], cwd=cwd or self.repo, capture_output=True,
                                text=True, check=True)
        return result.stdout

    def _worktree(self, name: str = "wt") -> Path:
        path = Path(self.tmp.name).resolve() / name
        self._git("worktree", "add", "-q", str(path), "HEAD")
        return path

    def _snapshot(self, root: Path, **kwargs) -> artifacts.SourceSnapshot:
        return artifacts.snapshot_tree(root, self.PATTERNS, cache_dir=self.cache, **kwargs)

    def _shard_entries(self) -> dict[str, str]:
        entries: dict[str, str] = {}
        for shard in self.blobs.glob("*.json"):
            entries.update(artifacts._read_blob_shard(shard))
        return entries

    # -- the fast path ----------------------------------------------------------

    def test_fresh_worktree_reuses_digests_learned_elsewhere(self) -> None:
        first = self._snapshot(self.repo)
        self.assertEqual(first.verification["files_hashed"], 3)
        self.assertEqual(first.verification["blobs_learned"], 3)
        self.assertEqual(first.verification["blob_index"], "learned")
        self.assertTrue(first.exact)
        learned = self._shard_entries()
        self.assertEqual(set(learned.values()), {_sha256(data) for data in self.FILES.values()})

        worktree = self._worktree()
        second = self._snapshot(worktree)
        self.assertEqual(second.digest, first.digest)
        self.assertEqual(second.files, 3)
        self.assertEqual(second.verification["files_hashed"], 0)
        self.assertEqual(second.verification["files_reused_by_blob"], 3)
        self.assertEqual(second.verification["files_reused"], 3)
        self.assertEqual(second.verification["blob_index"], "hit")
        # The per-root manifest was cold -- that is exactly the case this
        # index exists for -- and reuse from any cache is inexact.
        self.assertEqual(second.verification["cache"], "miss")
        self.assertFalse(second.exact)
        # A verification walk never consults either cache.
        verified = second.verify()
        self.assertTrue(verified.exact)
        self.assertEqual(verified.verification["blob_index"], "disabled")
        self.assertEqual(verified.digest, first.digest)

    def test_modified_untracked_and_symlinked_files_are_hashed(self) -> None:
        self._snapshot(self.repo)
        worktree = self._worktree()
        (worktree / "a.txt").write_bytes(b"alpha, edited\n")     # tracked, dirty, unstaged
        (worktree / "sub" / "new.txt").write_bytes(b"untracked\n")
        os.symlink("b.txt", worktree / "sub" / "link.txt")        # blob would be the link text
        snapshot = self._snapshot(worktree)
        self.assertEqual(snapshot.files, 5)
        self.assertEqual(snapshot.verification["files_reused_by_blob"], 2)
        self.assertEqual(snapshot.verification["files_hashed"], 3)
        entries = dict(snapshot.entries)
        self.assertEqual(entries["a.txt"], _sha256(b"alpha, edited\n"))
        self.assertEqual(entries["sub/link.txt"], _sha256(b"beta\n"))
        # The edited file's ORIGINAL blob keeps its original digest: nothing
        # learned a digest for bytes git did not judge to be that blob's.
        self.assertIn(_sha256(b"alpha\n"), self._shard_entries().values())
        self.assertNotIn(_sha256(b"alpha, edited\n"), self._shard_entries().values())

    def test_root_below_the_repository_top_level(self) -> None:
        self._snapshot(self.repo / "sub")
        worktree = self._worktree()
        clean = self._snapshot(worktree / "sub")
        self.assertEqual(clean.verification["files_reused_by_blob"], 2)
        self.assertEqual(clean.verification["files_hashed"], 0)
        (worktree / "sub" / "deep" / "c.txt").write_bytes(b"gamma, edited\n")
        dirty = self._snapshot(worktree / "sub")
        self.assertEqual(dirty.verification["files_hashed"], 1)
        self.assertEqual(dirty.verification["files_reused"], 1)  # b.txt, now via the per-root manifest
        self.assertEqual(dict(dirty.entries)["deep/c.txt"], _sha256(b"gamma, edited\n"))

    # -- what is never keyed by blob ----------------------------------------------

    def test_checkout_conversion_attributes_and_autocrlf_opt_out(self) -> None:
        (self.repo / ".gitattributes").write_text("*.bin filter=lfs\nsub/b.txt eol=crlf\n")
        (self.repo / "blob.bin").write_bytes(b"\x00\x01\x02")
        self._git("add", "-A")
        self._git("commit", "-q", "-m", "attributes")
        self._snapshot(self.repo)
        worktree = self._worktree()
        snapshot = self._snapshot(worktree)
        self.assertEqual(snapshot.files, 4)
        # a.txt and sub/deep/c.txt by blob; blob.bin (filter) and sub/b.txt (crlf) from bytes
        self.assertEqual(snapshot.verification["files_reused_by_blob"], 2)
        self.assertEqual(snapshot.verification["files_hashed"], 2)
        self._git("config", "core.autocrlf", "true", cwd=worktree)
        disabled = self._snapshot(worktree)
        self.assertEqual(disabled.verification["blob_index"], "disabled: core.autocrlf")
        self.assertEqual(disabled.verification["files_reused_by_blob"], 0)

    def test_not_a_repository_and_disabled_walks(self) -> None:
        plain = Path(self.tmp.name).resolve() / "plain"
        shutil.copytree(self.repo, plain, ignore=shutil.ignore_patterns(".git"))
        snapshot = self._snapshot(plain)
        self.assertEqual(snapshot.verification["blob_index"], "not-a-repo")
        self.assertEqual(snapshot.verification["files_hashed"], 3)
        self._snapshot(self.repo)
        worktree = self._worktree()
        exact = self._snapshot(worktree, trust_cache=False)
        self.assertEqual(exact.verification["blob_index"], "disabled")
        self.assertEqual(exact.verification["files_hashed"], 3)
        with mock.patch.dict(os.environ, {"CAPCOV_NO_CACHE": "1"}):
            off = self._snapshot(worktree)
        self.assertEqual(off.verification["blob_index"], "disabled")
        self.assertEqual(off.verification["files_hashed"], 3)
        with mock.patch.object(artifacts.shutil, "which", return_value=None):
            nogit = self._snapshot(worktree)
        self.assertEqual(nogit.verification["blob_index"], "unavailable: git")
        self.assertEqual(nogit.verification["files_hashed"], 3)

    # -- integrity ----------------------------------------------------------------

    def test_file_that_moves_after_gits_judgement_is_hashed_and_not_learned(self) -> None:
        self._snapshot(self.repo)
        worktree = self._worktree()
        real = artifacts._git_clean_blobs

        def judged_then_edited(root, relatives):
            status, blobs = real(root, relatives)
            (worktree / "a.txt").write_bytes(b"alpha, edited after git looked\n")
            return status, blobs

        with mock.patch.object(artifacts, "_git_clean_blobs", judged_then_edited):
            snapshot = self._snapshot(worktree)
        self.assertEqual(snapshot.verification["files_hashed"], 1)
        self.assertEqual(snapshot.verification["files_reused_by_blob"], 2)
        self.assertEqual(dict(snapshot.entries)["a.txt"], _sha256(b"alpha, edited after git looked\n"))
        self.assertNotIn(_sha256(b"alpha, edited after git looked\n"), self._shard_entries().values())

    def test_disagreeing_caches_are_resolved_by_reading_bytes(self) -> None:
        self._snapshot(self.repo)
        manifest = artifacts._cache_path(self.repo, self.PATTERNS, self.cache)
        entries = artifacts._read_cache(manifest, self.repo, self.PATTERNS)
        entries["a.txt"]["sha256"] = "0" * 64
        artifacts._write_cache(manifest, self.repo, self.PATTERNS, entries)
        snapshot = self._snapshot(self.repo)
        self.assertEqual(snapshot.verification["files_hashed"], 1)
        self.assertEqual(dict(snapshot.entries)["a.txt"], _sha256(b"alpha\n"))
        self.assertNotIn("0" * 64, self._shard_entries().values())

    def test_shard_failing_integrity_or_shape_is_ignored(self) -> None:
        self._snapshot(self.repo)
        worktree = self._worktree()
        [shard] = [p for p in self.blobs.glob("*.json") if _sha256(b"alpha\n") in p.read_text()]
        document = json.loads(shard.read_text())
        document["entries"] = {k: ("f" * 64) for k in document["entries"]}  # tampered, stale integrity
        shard.write_text(json.dumps(document))
        snapshot = self._snapshot(worktree)
        self.assertEqual(snapshot.verification["blob_index"], "fallback")
        self.assertNotIn("f" * 64, dict(snapshot.entries).values())
        self.assertEqual(dict(snapshot.entries)["a.txt"], _sha256(b"alpha\n"))
        # The tampered shard was replaced by what this walk learned from bytes,
        # so a third checkout reuses again.
        self.assertIn(_sha256(b"alpha\n"), self._shard_entries().values())
        again = self._snapshot(self._worktree("wt2"))
        self.assertEqual(again.verification["files_reused_by_blob"], 3)
        # A shape violation with a CORRECT integrity is rejected too.
        odd = Path(self.tmp.name) / "odd.json"
        artifacts._write_blob_shard(odd, {"not-hex": "0" * 64})
        with self.assertRaises(ValueError):
            artifacts._read_blob_shard(odd)

    def test_shared_index_is_not_written_inside_a_public_directory(self) -> None:
        self._snapshot(self.repo)
        worktree = self._worktree()
        public = Path(self.tmp.name).resolve() / "public-blobs"
        public.mkdir()
        os.chmod(public, 0o777)
        snapshot = self._snapshot(worktree, blob_index_dir=public)
        self.assertEqual(snapshot.verification["blob_index"], "disabled")
        self.assertEqual(snapshot.verification["files_hashed"], 3)
        self.assertEqual(list(public.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
