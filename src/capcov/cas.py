"""Small, opt-in content-addressed storage for immutable capcov artifacts.

The store deliberately has no mutable names or "latest" pointer.  Objects are
addressed only by the SHA-256 digest of their exact bytes, and snapshots are
canonical JSON objects stored in that same object namespace.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, TypeAlias


Digest: TypeAlias = str
JSONValue: TypeAlias = (
    None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]
)

_DIGEST_LENGTH = hashlib.sha256().digest_size * 2
_SNAPSHOT_TYPE = "capcov.snapshot.v1"
_COPY_BUFFER_SIZE = 1024 * 1024


class InvalidDigestError(ValueError):
    """A supplied digest is not canonical lowercase SHA-256 hex."""


class ObjectNotFoundError(FileNotFoundError):
    """A requested content-addressed object is absent."""


class IntegrityError(OSError):
    """Bytes at a content-addressed path do not match its digest."""


class InvalidManifestError(ValueError):
    """A snapshot manifest is malformed or non-canonical."""


class MissingBlobError(ObjectNotFoundError):
    """A snapshot references an absent or corrupt blob."""


def _validate_digest(digest: str) -> Digest:
    if not isinstance(digest, str):
        raise InvalidDigestError("digest must be a string")
    if len(digest) != _DIGEST_LENGTH or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise InvalidDigestError(
            "digest must be 64 lowercase hexadecimal SHA-256 characters"
        )
    return digest


def _file_digest(path: Path) -> Digest:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(_COPY_BUFFER_SIZE):
            hasher.update(chunk)
    return hasher.hexdigest()


def _sync_directory(path: Path) -> None:
    """Best-effort directory sync; unsupported platforms remain usable."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


class ContentAddressedStore:
    """A local filesystem store keyed by exact-byte SHA-256 digests.

    Publication uses a temporary file in the destination directory followed by
    an atomic, no-clobber hard link.  This is sufficient for cooperating local
    writers; it is intentionally not a distributed locking protocol.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self._objects = self.root / "objects" / "sha256"

    def _path(self, digest: str) -> Path:
        digest = _validate_digest(digest)
        return self._objects / digest[:2] / digest[2:]

    def contains(self, digest: str) -> bool:
        path = self._path(digest)
        return path.is_file() and _file_digest(path) == digest

    def put_bytes(self, data: bytes | bytearray | memoryview) -> Digest:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("data must be bytes-like")
        exact_bytes = bytes(data)
        digest = hashlib.sha256(exact_bytes).hexdigest()
        return self._put_chunks((exact_bytes,), digest)

    def put_file(self, path: str | os.PathLike[str]) -> Digest:
        source_path = Path(path)
        hasher = hashlib.sha256()
        destination_directory = self._objects / ".incoming"
        destination_directory.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="object-", dir=destination_directory
        )
        temporary_path = Path(temporary_name)
        try:
            with source_path.open("rb") as source, os.fdopen(
                descriptor, "wb"
            ) as destination:
                descriptor = -1
                while chunk := source.read(_COPY_BUFFER_SIZE):
                    hasher.update(chunk)
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            digest = hasher.hexdigest()
            self._publish(temporary_path, digest)
            return digest
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary_path.unlink(missing_ok=True)

    def _put_chunks(self, chunks: tuple[bytes, ...], digest: Digest) -> Digest:
        destination_directory = self._path(digest).parent
        destination_directory.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".object-", dir=destination_directory
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as destination:
                descriptor = -1
                for chunk in chunks:
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
            self._publish(temporary_path, digest)
            return digest
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary_path.unlink(missing_ok=True)

    def _publish(self, temporary_path: Path, digest: Digest) -> None:
        destination = self._path(digest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            self._require_integrity(destination, digest)
            return
        try:
            os.link(temporary_path, destination)
        except FileExistsError:
            # Another local writer won the publication race.  Its bytes still
            # have to be the object its pathname claims they are.
            self._require_integrity(destination, digest)
        _sync_directory(destination.parent)

    @staticmethod
    def _require_integrity(path: Path, digest: Digest) -> None:
        if not path.is_file() or _file_digest(path) != digest:
            raise IntegrityError(f"object at {path} does not match digest {digest}")

    def get_bytes(self, digest: str) -> bytes:
        path = self._path(digest)
        try:
            data = path.read_bytes()
        except FileNotFoundError as error:
            raise ObjectNotFoundError(f"object {digest} is not present") from error
        if hashlib.sha256(data).hexdigest() != digest:
            raise IntegrityError(f"object {digest} failed its integrity check")
        return data

    def materialize(
        self, digest: str, destination: str | os.PathLike[str]
    ) -> Path:
        source = self._path(digest)
        if not source.is_file():
            raise ObjectNotFoundError(f"object {digest} is not present")

        destination_path = Path(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination_path.name}.",
            suffix=".tmp",
            dir=destination_path.parent,
        )
        temporary_path = Path(temporary_name)
        hasher = hashlib.sha256()
        try:
            with source.open("rb") as stored, os.fdopen(
                descriptor, "wb"
            ) as output:
                descriptor = -1
                while chunk := stored.read(_COPY_BUFFER_SIZE):
                    hasher.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if hasher.hexdigest() != digest:
                raise IntegrityError(f"object {digest} failed its integrity check")
            os.replace(temporary_path, destination_path)
            _sync_directory(destination_path.parent)
            return destination_path
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary_path.unlink(missing_ok=True)


def _normalise_relative_path(path: str | os.PathLike[str]) -> str:
    try:
        raw = os.fspath(path)
    except TypeError as error:
        raise InvalidManifestError("snapshot paths must be path-like text") from error
    if not isinstance(raw, str):
        raise InvalidManifestError("snapshot paths must be text")
    if not raw or "\x00" in raw or "\\" in raw:
        raise InvalidManifestError(f"unsafe snapshot path: {raw!r}")
    windows_path = PureWindowsPath(raw)
    posix_path = PurePosixPath(raw)
    if windows_path.drive or windows_path.is_absolute() or posix_path.is_absolute():
        raise InvalidManifestError(f"snapshot path must be relative: {raw!r}")
    if ".." in posix_path.parts:
        raise InvalidManifestError(f"snapshot path traverses its root: {raw!r}")
    normalised = posix_path.as_posix()
    if normalised in ("", "."):
        raise InvalidManifestError("snapshot path must name a file")
    return normalised


def _normalise_files(files: Mapping[str | os.PathLike[str], str]) -> dict[str, Digest]:
    if not isinstance(files, Mapping):
        raise InvalidManifestError("snapshot files must be a mapping")
    normalised: dict[str, Digest] = {}
    for path, digest in files.items():
        canonical_path = _normalise_relative_path(path)
        if canonical_path in normalised:
            raise InvalidManifestError(
                f"multiple paths normalise to {canonical_path!r}"
            )
        normalised[canonical_path] = _validate_digest(digest)

    ordered_paths = sorted(normalised)
    for earlier, later in zip(ordered_paths, ordered_paths[1:]):
        if later.startswith(earlier + "/"):
            raise InvalidManifestError(
                f"snapshot paths conflict as file and directory: {earlier!r}"
            )
    return dict(sorted(normalised.items()))


def _json_value(value: Any, *, location: str = "metadata") -> JSONValue:
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidManifestError(f"{location} contains a non-finite number")
        return value
    if isinstance(value, list):
        return [
            _json_value(item, location=f"{location}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, Mapping):
        result: dict[str, JSONValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise InvalidManifestError(f"{location} object keys must be strings")
            result[key] = _json_value(item, location=f"{location}.{key}")
        return result
    raise InvalidManifestError(
        f"{location} contains a non-JSON value of type {type(value).__name__}"
    )


def _canonical_json(value: JSONValue) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidManifestError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


class SnapshotStore:
    """Immutable canonical manifests whose objects live in a CAS."""

    def __init__(
        self, root: str | os.PathLike[str] | ContentAddressedStore
    ) -> None:
        self.cas = (
            root
            if isinstance(root, ContentAddressedStore)
            else ContentAddressedStore(root)
        )

    def put(
        self,
        files: Mapping[str | os.PathLike[str], str],
        *,
        parent: str | None = None,
        metadata: JSONValue | None = None,
    ) -> Digest:
        canonical_files = _normalise_files(files)
        if parent is not None:
            parent = _validate_digest(parent)
            self.load(parent)
        for path, digest in canonical_files.items():
            if not self.cas.contains(digest):
                raise MissingBlobError(
                    f"snapshot path {path!r} references missing blob {digest}"
                )

        manifest: JSONValue = {
            "files": canonical_files,
            "metadata": _json_value({} if metadata is None else metadata),
            "parent": parent,
            "type": _SNAPSHOT_TYPE,
        }
        return self.cas.put_bytes(_canonical_json(manifest))

    create = put
    create_snapshot = put

    def read(self, snapshot_digest: str) -> bytes:
        self.load(snapshot_digest)
        return self.cas.get_bytes(snapshot_digest)

    read_bytes = read

    def load(self, snapshot_digest: str) -> dict[str, Any]:
        return self._load(snapshot_digest, ancestors=set())

    def _load(
        self, snapshot_digest: str, *, ancestors: set[Digest]
    ) -> dict[str, Any]:
        snapshot_digest = _validate_digest(snapshot_digest)
        if snapshot_digest in ancestors:
            raise InvalidManifestError("snapshot parent cycle detected")
        try:
            encoded = self.cas.get_bytes(snapshot_digest)
        except ObjectNotFoundError as error:
            raise ObjectNotFoundError(
                f"snapshot {snapshot_digest} is not present"
            ) from error
        try:
            manifest = json.loads(
                encoded,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    InvalidManifestError(f"invalid JSON number: {value}")
                ),
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise InvalidManifestError(
                f"object {snapshot_digest} is not a snapshot manifest"
            ) from error
        if not isinstance(manifest, dict) or set(manifest) != {
            "files",
            "metadata",
            "parent",
            "type",
        }:
            raise InvalidManifestError("snapshot manifest has an invalid schema")
        if manifest["type"] != _SNAPSHOT_TYPE:
            raise InvalidManifestError("object is not a capcov snapshot manifest")

        files = _normalise_files(manifest["files"])
        if files != manifest["files"]:
            raise InvalidManifestError("snapshot paths are not canonical")
        metadata = _json_value(manifest["metadata"])
        parent = manifest["parent"]
        if parent is not None:
            parent = _validate_digest(parent)

        canonical_manifest: JSONValue = {
            "files": files,
            "metadata": metadata,
            "parent": parent,
            "type": _SNAPSHOT_TYPE,
        }
        if _canonical_json(canonical_manifest) != encoded:
            raise InvalidManifestError("snapshot manifest JSON is not canonical")
        for path, digest in files.items():
            if not self.cas.contains(digest):
                raise MissingBlobError(
                    f"snapshot path {path!r} references missing blob {digest}"
                )
        if parent is not None:
            self._load(parent, ancestors=ancestors | {snapshot_digest})
        return canonical_manifest

    def materialize(
        self, snapshot_digest: str, destination: str | os.PathLike[str]
    ) -> Path:
        manifest = self.load(snapshot_digest)
        destination_path = Path(destination)
        if destination_path.exists() or destination_path.is_symlink():
            raise FileExistsError(
                f"snapshot destination already exists: {destination_path}"
            )
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        staging_path = Path(
            tempfile.mkdtemp(
                prefix=f".{destination_path.name}.",
                suffix=".tmp",
                dir=destination_path.parent,
            )
        )
        try:
            for relative_path, digest in manifest["files"].items():
                target = staging_path.joinpath(*PurePosixPath(relative_path).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                self.cas.materialize(digest, target)
            os.rename(staging_path, destination_path)
            _sync_directory(destination_path.parent)
            return destination_path
        finally:
            if staging_path.exists():
                shutil.rmtree(staging_path)


__all__ = [
    "ContentAddressedStore",
    "IntegrityError",
    "InvalidDigestError",
    "InvalidManifestError",
    "MissingBlobError",
    "ObjectNotFoundError",
    "SnapshotStore",
]
