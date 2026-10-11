import hashlib
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from capcov.cas import (
    ContentAddressedStore,
    IntegrityError,
    InvalidDigestError,
    InvalidManifestError,
    MissingBlobError,
    SnapshotStore,
)


class ContentAddressedStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = ContentAddressedStore(self.root / "cas")

    def test_digest_is_sha256_of_exact_bytes(self) -> None:
        data = b"evidence\x00with\r\nexact bytes"
        digest = self.store.put_bytes(data)
        self.assertEqual(digest, hashlib.sha256(data).hexdigest())
        self.assertEqual(self.store.get_bytes(digest), data)

    def test_identical_puts_deduplicate(self) -> None:
        digest = self.store.put_bytes(b"same")
        object_path = self.store._path(digest)
        inode = object_path.stat().st_ino
        self.assertEqual(self.store.put_bytes(b"same"), digest)
        self.assertEqual(object_path.stat().st_ino, inode)
        stored = list((self.root / "cas" / "objects").rglob(digest[2:]))
        self.assertEqual(len(stored), 1)

    def test_concurrent_identical_puts_publish_one_complete_object(self) -> None:
        data = b"concurrent evidence" * 1024
        with ThreadPoolExecutor(max_workers=8) as workers:
            digests = list(workers.map(self.store.put_bytes, [data] * 32))
        self.assertEqual(set(digests), {hashlib.sha256(data).hexdigest()})
        self.assertEqual(self.store.get_bytes(digests[0]), data)

    def test_put_does_not_replace_a_corrupt_existing_object(self) -> None:
        digest = self.store.put_bytes(b"trusted")
        object_path = self.store._path(digest)
        object_path.write_bytes(b"different")
        with self.assertRaises(IntegrityError):
            self.store.put_bytes(b"trusted")
        self.assertEqual(object_path.read_bytes(), b"different")

    def test_put_file_hashes_content_instead_of_metadata(self) -> None:
        source = self.root / "source"
        source.write_bytes(b"from a file")
        digest = self.store.put_file(source)
        self.assertEqual(digest, hashlib.sha256(b"from a file").hexdigest())
        self.assertEqual(self.store.get_bytes(digest), b"from a file")

    def test_materialization_atomically_replaces_a_file(self) -> None:
        digest = self.store.put_bytes(b"new evidence")
        destination = self.root / "result" / "artifact.bin"
        destination.parent.mkdir()
        destination.write_bytes(b"old evidence")
        self.store.materialize(digest, destination)
        self.assertEqual(destination.read_bytes(), b"new evidence")
        self.assertEqual(list(destination.parent.glob(".artifact.bin.*.tmp")), [])

    def test_corrupt_materialization_preserves_the_destination(self) -> None:
        digest = self.store.put_bytes(b"trusted")
        self.store._path(digest).write_bytes(b"corrupt")
        destination = self.root / "destination"
        destination.write_bytes(b"keep me")
        with self.assertRaises(IntegrityError):
            self.store.materialize(digest, destination)
        self.assertEqual(destination.read_bytes(), b"keep me")

    def test_malformed_and_traversing_digests_are_rejected(self) -> None:
        for digest in ("../outside", "a" * 63, "A" * 64, "g" * 64):
            with self.subTest(digest=digest):
                with self.assertRaises(InvalidDigestError):
                    self.store.get_bytes(digest)


class SnapshotStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.cas = ContentAddressedStore(self.root / "cas")
        self.snapshots = SnapshotStore(self.cas)
        self.one = self.cas.put_bytes(b"one")
        self.two = self.cas.put_bytes(b"two")

    def test_manifest_is_canonical_and_normalises_relative_paths(self) -> None:
        digest = self.snapshots.put(
            {"z.txt": self.two, "dir/./a.txt": self.one},
            metadata={"purpose": "test", "values": [2, 1]},
        )
        manifest = self.snapshots.load(digest)
        self.assertEqual(
            list(manifest["files"]), ["dir/a.txt", "z.txt"]
        )
        self.assertEqual(
            self.snapshots.read(digest),
            json.dumps(
                manifest,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8"),
        )
        self.assertEqual(
            self.snapshots.put(
                {"dir/a.txt": self.one, "z.txt": self.two},
                metadata={"values": [2, 1], "purpose": "test"},
            ),
            digest,
        )

    def test_parent_changes_snapshot_identity(self) -> None:
        parent = self.snapshots.put({"one": self.one})
        without_parent = self.snapshots.put({"two": self.two})
        with_parent = self.snapshots.put({"two": self.two}, parent=parent)
        self.assertNotEqual(with_parent, without_parent)
        self.assertEqual(self.snapshots.load(with_parent)["parent"], parent)

    def test_missing_blob_is_rejected_before_manifest_is_stored(self) -> None:
        missing = hashlib.sha256(b"missing").hexdigest()
        with self.assertRaises(MissingBlobError):
            self.snapshots.put({"evidence.json": missing})

    def test_traversal_and_path_collisions_are_rejected(self) -> None:
        unsafe = (
            {"../outside": self.one},
            {"/absolute": self.one},
            {"dir\\outside": self.one},
            {"a": self.one, "a/b": self.two},
            {"x/./y": self.one, "x/y": self.two},
        )
        for files in unsafe:
            with self.subTest(files=files):
                with self.assertRaises(InvalidManifestError):
                    self.snapshots.put(files)

    def test_missing_parent_is_rejected(self) -> None:
        missing = hashlib.sha256(b"missing parent").hexdigest()
        with self.assertRaises(FileNotFoundError):
            self.snapshots.put({"one": self.one}, parent=missing)

    def test_snapshot_materializes_only_after_every_blob_validates(self) -> None:
        snapshot = self.snapshots.put(
            {"nested/one.txt": self.one, "two.txt": self.two}
        )
        destination = self.root / "checkout"
        self.snapshots.materialize(snapshot, destination)
        self.assertEqual((destination / "nested" / "one.txt").read_bytes(), b"one")
        self.assertEqual((destination / "two.txt").read_bytes(), b"two")
        self.assertEqual(list(self.root.glob(".checkout.*.tmp")), [])

    def test_snapshot_materialization_leaves_no_partial_destination(self) -> None:
        snapshot = self.snapshots.put({"one": self.one, "two": self.two})
        self.cas._path(self.two).unlink()
        destination = self.root / "checkout"
        with self.assertRaises(MissingBlobError):
            self.snapshots.materialize(snapshot, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.root.glob(".checkout.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
