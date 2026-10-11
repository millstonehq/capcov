"""Artifact formats, provenance, and the tree hash everything is relative to.

There is no complete-in-the-abstract. What this framework asserts is
completeness *with respect to a named artifact*, so every file it writes carries
`derived_from`: the artifact, its hash, the extractor, and when. `reconcile`
refuses to compare two artifacts whose `artifact_sha256` disagree -- a static
run against one tree and a runtime run against another produce a diff that
looks authoritative and means nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat as stat_module
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 1

# The source glob set a tree hash is taken over when nothing else is declared.
DEFAULT_PATTERNS = ("**/*.py",)
# Every language the tree-sitter discovery path can actually parse. A language
# outside this table has no honest default glob, so `language_pattern` refuses
# rather than hashing some other language's files under its name.
LANGUAGE_PATTERNS = {
    "python": "**/*.py",
    "go": "**/*.go",
    "php": "**/*.php",
    "javascript": "**/*.js",
    "typescript": "**/*.ts",
    "tsx": "**/*.tsx",
}

# Set to an affirmative value to bind every digest to exact bytes, at the cost
# of the incremental fast path. The verification walks are always exact.
ENV_NO_CACHE = "CAPCOV_NO_CACHE"

TREE_CACHE_VERSION = 1
MAX_TREE_CACHE_BYTES = 32 * 1024 * 1024
MAX_TREE_CACHE_ENTRIES = 500_000

# --- shared blob index -------------------------------------------------------
# A second, machine-wide cache keyed by git BLOB ID rather than by root path.
# `git worktree add` gives every file a fresh inode and mtime, so the per-root
# manifest starts cold for each new checkout and re-hashes the whole tree.  Git
# already knows, without reading a byte, which tracked files are byte-equal to
# an index blob (`git diff-files` is the judgement `git commit` relies on).  For
# those files the sha256 of the bytes is a fact about the blob, not about the
# checkout, so it is shared by every worktree and commit that contains it.  The
# digest formula is unchanged: a manifest still binds to exact bytes; only who
# read them first changes.
#
# Trust model: identical to the per-root manifest.  The index lives in the
# user's cache, is consulted only on the incremental path (`trust_cache=True`,
# `CAPCOV_NO_CACHE` unset), never by `SourceSnapshot.verify`, and a file that
# reused a blob digest counts as reused, so the snapshot is not `exact`.  Files
# git would convert on checkout (filters, CRLF, ident, working-tree encodings)
# are never keyed by blob; neither are symlinks, submodules, unmerged,
# skip-worktree or assume-unchanged entries.  Nothing here writes to the
# repository: every git call is read-only plumbing.
BLOB_INDEX_VERSION = 1
MAX_BLOB_SHARD_BYTES = 8 * 1024 * 1024
MAX_BLOB_SHARD_ENTRIES = 200_000
GIT_TIMEOUT_SECONDS = 120
_GIT_CONVERSION_ATTRIBUTES = ("filter", "eol", "ident", "working-tree-encoding")

# `derived_from` describes the RUN -- when it happened and against which exact
# bytes. `--check` asks a different question: has what the system can do changed?
# Diffing the provenance too would fail the check on every reformatted line,
# which teaches people to regenerate without reading, and a check nobody reads
# is the thing this framework exists to replace.
#
# The hash still does its job. `reconcile` uses it to refuse a static run and a
# runtime run taken from different trees, which is a comparison across two
# systems dressed up as a finding.
VOLATILE = ("derived_from", "timing")


def language_pattern(language: object) -> str:
    """The source default for a configured language.

    An absent language retains the historic Python default. A NAMED language
    this table does not know is refused: silently handing it `**/*.py` hashed
    one language's tree and called it another's, which is a false provenance
    claim, and it never becomes an empty pattern set (whose shared empty digest
    would make unrelated sources appear identical).
    """
    if language is None or str(language).strip() == "":
        return DEFAULT_PATTERNS[0]
    name = str(language).strip().lower()
    try:
        return LANGUAGE_PATTERNS[name]
    except KeyError:
        raise ValueError(
            f"no source glob default for language {name!r}; "
            f"declare 'globs' or 'files' explicitly "
            f"(known: {', '.join(sorted(LANGUAGE_PATTERNS))})"
        ) from None


def normalise_patterns(patterns: tuple[str, ...]) -> tuple[str, ...]:
    """The one glob-set normalization: de-duplicated, empties dropped.

    Public because callers that COMPARE a recorded pattern set against a
    snapshot's must normalize both sides, or a harmless duplicate reads as a
    stale inventory.
    """
    normalized = tuple(dict.fromkeys(str(pattern) for pattern in patterns if pattern))
    return normalized or DEFAULT_PATTERNS


# Historic private spelling, kept for in-module call sites.
_patterns = normalise_patterns


def _default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    if base:
        return Path(base) / "capcov" / "tree-manifests-v1"
    return Path.home() / ".cache" / "capcov" / "tree-manifests-v1"


def _default_blob_index_dir() -> Path:
    return _default_cache_dir().parent / "blob-sha256-v1"


def _blob_index_dir_for(
    cache_dir: Path | str | None, blob_index_dir: Path | str | None
) -> Path:
    """Where the shared blob index lives: beside an explicit per-root cache dir
    (so a caller's private cache stays self-contained), else under the user
    cache next to the per-root manifests."""
    if blob_index_dir is not None:
        return Path(blob_index_dir)
    if cache_dir is None:
        return _default_blob_index_dir()
    requested = Path(cache_dir)
    return requested.parent / f"{requested.name}-blobs"


def _cache_path(root: Path, patterns: tuple[str, ...], cache_dir: Path) -> Path:
    identity = json.dumps(
        {"root": str(root.resolve()), "patterns": list(patterns)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return cache_dir / f"{hashlib.sha256(identity).hexdigest()}.json"


def _cache_dir_is_private(directory: Path) -> bool:
    """A cache is only usable when nobody else can write the digests we trust.

    On a first run nothing under the cache root exists yet, so the nearest
    existing ancestor is what decides whether it is safe to create it there.
    """
    candidate = directory if directory.is_absolute() else directory.absolute()
    for path in (candidate, *candidate.parents):
        try:
            info = path.stat()
        except OSError:
            continue
        if info.st_uid != os.getuid():
            return False
        return not info.st_mode & (stat_module.S_IWGRP | stat_module.S_IWOTH)
    return False


def _read_cache(path: Path, root: Path, patterns: tuple[str, ...]) -> dict[str, dict]:
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & (
        stat_module.S_IWGRP | stat_module.S_IWOTH
    ):
        raise ValueError("tree cache is not private to this user")
    if info.st_size > MAX_TREE_CACHE_BYTES:
        raise ValueError("tree cache exceeds the read bound")
    document = json.loads(path.read_bytes())
    integrity = document.get("integrity_sha256") if isinstance(document, dict) else None
    payload = {
        key: value for key, value in document.items() if key != "integrity_sha256"
    } if isinstance(document, dict) else {}
    expected_integrity = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if (
        not isinstance(document, dict)
        or integrity != expected_integrity
        or document.get("version") != TREE_CACHE_VERSION
        or document.get("root") != str(root.resolve())
        or document.get("patterns") != list(patterns)
        or not isinstance(document.get("entries"), dict)
        or len(document["entries"]) > MAX_TREE_CACHE_ENTRIES
    ):
        raise ValueError("invalid tree cache manifest")
    entries = document["entries"]
    for relative, entry in entries.items():
        if (
            not isinstance(relative, str)
            or not relative
            or relative.startswith("/")
            or ".." in Path(relative).parts
            or "\\" in relative
            or not isinstance(entry, dict)
            or not isinstance(entry.get("sha256"), str)
            or len(entry["sha256"]) != 64
            or entry["sha256"] != entry["sha256"].lower()
        ):
            raise ValueError("invalid tree cache entry")
    return entries


def _file_identity(stat: os.stat_result) -> dict[str, int]:
    # ctime/inode/device make the size+mtime fast path conservative without
    # changing the source-bound digest, which remains path + exact-byte SHA-256.
    return {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "ctime_ns": stat.st_ctime_ns,
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "mode": stat.st_mode,
    }


def _hash_file(path: Path, before: os.stat_result) -> tuple[str, os.stat_result]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    after = path.stat()
    if _file_identity(before) != _file_identity(after):
        raise OSError(f"source file changed while hashing: {path}")
    return digest.hexdigest(), after


def _write_cache(
    path: Path, root: Path, patterns: tuple[str, ...], entries: dict[str, dict]
) -> bool:
    """Replace this index in one rename. Returns False when it does not fit.

    There is exactly one live manifest per (root, patterns), it is never shared
    with a second referent, and `os.replace` is already atomic -- so the index
    IS the manifest. An indirection through a content-addressed object bought
    nothing here and leaked one orphaned object per edit, forever.
    """
    document = {
        "version": TREE_CACHE_VERSION,
        "root": str(root.resolve()),
        "patterns": list(patterns),
        "entries": entries,
    }
    document["integrity_sha256"] = hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    value = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    if len(value) > MAX_TREE_CACHE_BYTES:
        return False
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # An index written by the superseded content-addressed format leaves its
    # object store behind; drop it the first time we replace that index.
    shutil.rmtree(path.parent / "cas", ignore_errors=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def _is_lower_hex(value: object, lengths: tuple[int, ...]) -> bool:
    if not isinstance(value, str) or len(value) not in lengths or value != value.lower():
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _read_blob_shard(path: Path) -> dict[str, str]:
    info = path.stat()
    if info.st_uid != os.getuid() or info.st_mode & (
        stat_module.S_IWGRP | stat_module.S_IWOTH
    ):
        raise ValueError("blob index shard is not private to this user")
    if info.st_size > MAX_BLOB_SHARD_BYTES:
        raise ValueError("blob index shard exceeds the read bound")
    document = json.loads(path.read_bytes())
    if not isinstance(document, dict):
        raise ValueError("invalid blob index shard")
    integrity = document.get("integrity_sha256")
    payload = {key: value for key, value in document.items() if key != "integrity_sha256"}
    expected = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    entries = document.get("entries")
    if (
        integrity != expected
        or document.get("version") != BLOB_INDEX_VERSION
        or not isinstance(entries, dict)
        or len(entries) > MAX_BLOB_SHARD_ENTRIES
    ):
        raise ValueError("invalid blob index shard")
    for oid, digest in entries.items():
        if not _is_lower_hex(oid, (40, 64)) or not _is_lower_hex(digest, (64,)):
            raise ValueError("invalid blob index entry")
    return dict(entries)


def _write_blob_shard(path: Path, entries: dict[str, str]) -> bool:
    """Replace one shard in one rename. Returns False when it does not fit."""
    document = {"version": BLOB_INDEX_VERSION, "entries": entries}
    document["integrity_sha256"] = hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    value = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    if len(value) > MAX_BLOB_SHARD_BYTES or len(entries) > MAX_BLOB_SHARD_ENTRIES:
        return False
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


class _BlobIndex:
    """Blob id -> sha256, sharded on disk by the first two hex digits."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._shards: dict[str, dict[str, str]] = {}
        self._dirty: set[str] = set()
        self.read_errors = 0
        self.write_errors = 0

    def _load(self, prefix: str) -> dict[str, str]:
        try:
            return _read_blob_shard(self.directory / f"{prefix}.json")
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self.read_errors += 1
            return {}

    def _shard(self, prefix: str) -> dict[str, str]:
        loaded = self._shards.get(prefix)
        if loaded is None:
            loaded = self._load(prefix)
            self._shards[prefix] = loaded
        return loaded

    def get(self, oid: str) -> str | None:
        return self._shard(oid[:2]).get(oid)

    def put(self, oid: str, digest: str) -> None:
        shard = self._shard(oid[:2])
        if shard.get(oid) != digest:
            shard[oid] = digest
            self._dirty.add(oid[:2])

    def flush(self) -> None:
        """Write every shard that learned something, merged over what is on
        disk NOW, so a concurrent writer's additions survive instead of being
        clobbered by a stale copy."""
        for prefix in sorted(self._dirty):
            merged = {**self._load(prefix), **self._shards[prefix]}
            try:
                if not _write_blob_shard(self.directory / f"{prefix}.json", merged):
                    self.write_errors += 1
            except OSError:
                self.write_errors += 1
        self._dirty.clear()


@dataclass(frozen=True)
class _GitResult:
    returncode: int
    stdout: bytes


def _git(root: Path, *args: str, stdin: bytes | None = None) -> _GitResult | None:
    """Run one read-only git plumbing command; None when it cannot be run.

    `Popen` is used directly rather than `subprocess.run` so that a consumer
    faking `subprocess.run` around its own command (as capcov's outcome runner
    tests do) does not see, and cannot break, these queries.  Anything the
    process layer raises is cache infrastructure failing, and cache failure is
    optional: the caller hashes bytes.  It must never become a snapshot failure.
    """
    executable = shutil.which("git")
    if executable is None:
        return None
    try:
        process = subprocess.Popen(
            [executable, "-C", str(root), *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            stdout, _ = process.communicate(stdin, timeout=GIT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            return None
        if not isinstance(stdout, bytes) or not isinstance(process.returncode, int):
            return None
        return _GitResult(process.returncode, stdout)
    except Exception:  # noqa: BLE001 -- see docstring
        return None


def _git_setting(root: Path, key: str) -> str | None:
    result = _git(root, "config", "--get", key)
    if result is None or result.returncode not in (0, 1):
        return None
    if result.returncode == 1:
        return ""
    return result.stdout.decode("utf-8", "replace").strip().lower()


def _converts_on_checkout(attribute: str, value: str) -> bool:
    # `eol=lf` never converts on checkout; `crlf` does.  `ident` expands
    # $Id$ only when set.  Any filter or working-tree encoding rewrites bytes.
    if attribute == "eol":
        return value == "crlf"
    if attribute == "ident":
        return value == "set"
    return value not in ("unspecified", "unset")


def _git_clean_blobs(root: Path, relatives: Iterable[str]) -> tuple[str, dict[str, str]]:
    """Blob ids for the tracked regular files under `root` that git judges
    byte-identical to their index entry and would not convert on checkout.

    Read-only plumbing only.  Any failure disables the layer for this walk
    with a named status rather than a guess; the walk then hashes bytes.
    """
    if sys.platform == "win32":
        return "disabled: platform", {}
    if shutil.which("git") is None:
        return "unavailable: git", {}
    top = _git(root, "rev-parse", "--show-toplevel")
    if top is None:
        return "unavailable: git", {}
    if top.returncode != 0:
        return "not-a-repo", {}
    prefix_result = _git(root, "rev-parse", "--show-prefix")
    if prefix_result is None or prefix_result.returncode != 0:
        return "unavailable: git rev-parse", {}
    prefix = prefix_result.stdout.decode("utf-8", "replace").strip()
    settings = {
        key: _git_setting(root, key) for key in ("core.autocrlf", "core.eol", "core.fsmonitor")
    }
    if any(value is None for value in settings.values()):
        return "unavailable: git config", {}
    if settings["core.autocrlf"] not in ("", "false", "input"):
        return "disabled: core.autocrlf", {}
    if settings["core.eol"] not in ("", "lf", "native"):
        return "disabled: core.eol", {}
    if settings["core.fsmonitor"] not in ("", "false"):
        return "disabled: core.fsmonitor", {}
    listed = _git(root, "ls-files", "-s", "-v", "-z")
    if listed is None or listed.returncode != 0:
        return "unavailable: git ls-files", {}
    wanted = set(relatives)
    blobs: dict[str, str] = {}
    for record in listed.stdout.split(b"\0"):
        head, separator, raw_path = record.partition(b"\t")
        fields = head.split()
        if not separator or len(fields) != 4:
            continue
        tag, mode, oid, stage = (field.decode("ascii", "replace") for field in fields)
        # 'H' is cached and, as far as git is concerned, up to date.  Lowercase
        # is assume-unchanged, 'S' skip-worktree, 'M' unmerged: git does not
        # look at those files, so neither may this fast path.  Regular files
        # only: a symlink's blob is its target text, a gitlink is a submodule.
        if tag != "H" or stage != "0" or mode not in ("100644", "100755"):
            continue
        try:
            relative = raw_path.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if relative in wanted and _is_lower_hex(oid, (40, 64)):
            blobs[relative] = oid
    if not blobs:
        return "ready", {}
    modified = _git(root, "diff-files", "--name-only", "-z")
    if modified is None or modified.returncode != 0:
        return "unavailable: git diff-files", {}
    for record in modified.stdout.split(b"\0"):
        if not record:
            continue
        reported = record.decode("utf-8", "replace")
        # Plumbing reports top-level-relative paths; drop both spellings so a
        # modified file is excluded whichever convention this git uses.
        blobs.pop(reported, None)
        if prefix and reported.startswith(prefix):
            blobs.pop(reported[len(prefix):], None)
    if not blobs:
        return "ready", {}
    query = b"".join(relative.encode("utf-8") + b"\0" for relative in sorted(blobs))
    attributes = _git(
        root, "check-attr", "-z", "--stdin", *_GIT_CONVERSION_ATTRIBUTES, stdin=query
    )
    if attributes is None or attributes.returncode != 0:
        return "unavailable: git check-attr", {}
    parts = attributes.stdout.split(b"\0")
    for index in range(0, len(parts) - 2, 3):
        reported, attribute, value = (
            part.decode("utf-8", "replace") for part in parts[index:index + 3]
        )
        if _converts_on_checkout(attribute, value):
            blobs.pop(reported, None)
            if prefix and reported.startswith(prefix):
                blobs.pop(reported[len(prefix):], None)
    return "ready", blobs


@dataclass(frozen=True)
class SourceSnapshot:
    """One exact tree identity plus bounded, non-secret verification metrics."""

    root: Path
    patterns: tuple[str, ...]
    digest: str
    files: int
    verification: dict
    # Per-file digests are retained only in memory.  They let consumers whose
    # declared inputs include this exact source root reuse the authoritative
    # manifest instead of opening every file a second time.  The public tree
    # hash API remains the compact ``(digest, files)`` pair.
    entries: tuple[tuple[str, str], ...] = ()
    # True when this identity was handed over by a driving `capcov observe`
    # rather than captured here.  The driver owns the one exact verification
    # at the publication boundary; a carried snapshot never verifies itself.
    carried: bool = False

    @property
    def exact(self) -> bool:
        """Every digest in this snapshot came from bytes read by this process.

        A cache miss and a `trust_cache=False` walk are both exact -- nothing
        was reused -- so this is a property of what happened, not of a flag.
        A carried identity is never exact: the process holding it read nothing.
        """
        return not self.carried and self.verification.get("files_reused") == 0

    def provenance(self, artifact: str, extractor: str) -> dict:
        return provenance(
            artifact,
            self.digest,
            extractor,
            self.files,
            self.patterns,
            snapshot=self,
        )

    def verify(self) -> "SourceSnapshot":
        """Re-read the tree FROM BYTES and require it to still be this tree.

        This is THE exact walk of a run, and there is one: at the publication
        boundary, in the process that publishes.  Never from the cache --
        anything running as this user can write `~/.cache`, including the
        exercise a freshness guard exists to distrust, so a cache-backed
        verification lets the observed process answer the question being
        asked about it.  The returned snapshot is exact; publish from it.
        """
        current = snapshot_tree(self.root, self.patterns, trust_cache=False)
        if current.digest != self.digest or current.files != self.files:
            raise ValueError("source changed since the source snapshot was captured")
        return current


def _cache_disabled_by_env() -> bool:
    return os.environ.get(ENV_NO_CACHE, "").strip().lower() in {"1", "true", "yes", "on"}


def snapshot_tree(
    root: Path,
    patterns: tuple[str, ...] = DEFAULT_PATTERNS,
    *,
    cache_dir: Path | str | None = None,
    trust_cache: bool = True,
    blob_index_dir: Path | str | None = None,
) -> SourceSnapshot:
    """Capture an exact source manifest, reusing only metadata-matched entries.

    Two caches feed the incremental path.  The per-root manifest reuses a
    digest when a file's size, times, inode and mode are unchanged.  The shared
    blob index (module comment above) reuses a digest learned in ANY checkout
    when git judges the tracked file byte-identical to its index blob -- so a
    fresh `git worktree add` of a known commit hashes nothing.  Both count as
    reuse; both are off under `trust_cache=False` and `CAPCOV_NO_CACHE`.

    `trust_cache=False` hashes every selected file from bytes.  That is what
    every verification walk does, because the shared cache is writable by
    anything running as this user -- including the exercise a freshness guard
    exists to distrust -- so a cache-backed verification would let the observed
    process answer the question asked about it.  `CAPCOV_NO_CACHE=1` forces the
    same for every walk.

    Cache failure is deliberately optional: a missing, corrupt, oversized, or
    unwritable cache falls back to exact content hashing.  Source read/stat
    failures still raise and therefore can never be converted into cached
    success.
    """
    started = time.perf_counter_ns()
    root = Path(root).resolve()
    patterns = _patterns(patterns)
    requested_cache = Path(cache_dir) if cache_dir is not None else _default_cache_dir()
    try:
        cache_inside_source = requested_cache.resolve().is_relative_to(root)
    except OSError:
        cache_inside_source = False
    cache_path = _cache_path(root, patterns, requested_cache)
    cached: dict[str, dict] = {}
    cache_read_error = False
    cache_enabled = (
        trust_cache
        and not cache_inside_source
        and not _cache_disabled_by_env()
        and _cache_dir_is_private(requested_cache)
    )
    if cache_enabled:
        try:
            cached = _read_cache(cache_path, root, patterns)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            cache_read_error = True

    paths: dict[str, Path] = {}
    for pattern in patterns:
        for path in root.glob(pattern):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            relative = path.relative_to(root).as_posix()
            paths[relative] = path
    # Past the index bound the tree is still hashed exactly; only the
    # incremental index is given up.  A large tree must stay slow, not fail.
    unbounded = len(paths) > MAX_TREE_CACHE_ENTRIES
    if unbounded:
        cached = {}

    # One stat per file before asking git anything: its cleanliness judgement
    # is trusted below only for a file whose identity has not moved since.
    stats = {relative: paths[relative].stat() for relative in sorted(paths)}
    blob_status = "disabled"
    blob_ids: dict[str, str] = {}
    blob_index: _BlobIndex | None = None
    if cache_enabled and not unbounded and stats:
        blob_dir = _blob_index_dir_for(cache_dir, blob_index_dir)
        try:
            blob_inside_source = blob_dir.resolve().is_relative_to(root)
        except OSError:
            blob_inside_source = False
        if not blob_inside_source and _cache_dir_is_private(blob_dir):
            blob_status, blob_ids = _git_clean_blobs(root, stats)
            if blob_status == "ready":
                blob_index = _BlobIndex(blob_dir)

    fresh: dict[str, dict] = {}
    reused = 0
    reused_by_blob = 0
    learned = 0
    hashed = 0
    manifest_entries: list[str] = []
    for relative, path in sorted(paths.items()):
        stat = stats[relative]
        identity = _file_identity(stat)
        digest: str | None = None
        from_blob = False
        disagreement = False
        stable = True  # identity unchanged since `stats` was taken
        blob = blob_ids.get(relative) if blob_index is not None else None
        known = blob_index.get(blob) if blob is not None else None
        previous = cached.get(relative)
        if previous is not None and all(
            previous.get(key) == value for key, value in identity.items()
        ):
            candidate = previous["sha256"]
            # Cache parsing validates shape; conversion validates hexadecimal.
            try:
                int(candidate, 16)
            except ValueError:
                candidate = None
            # Two caches that disagree about the same bytes are resolved by
            # reading the bytes, never by preferring one of them.
            if candidate is not None and known is not None and known != candidate:
                disagreement = True
            elif candidate is not None:
                digest = candidate
                reused += 1
        if digest is None and known is not None and not disagreement:
            # Git judged the file clean after `stats` was taken; the shared
            # digest is used only if nothing about the file moved in between.
            if _file_identity(path.stat()) == identity:
                digest = known
                from_blob = True
                reused += 1
                reused_by_blob += 1
        if digest is None:
            current = path.stat()
            digest, current = _hash_file(path, current)
            now = _file_identity(current)
            stable = now == identity
            identity = now
            hashed += 1
        if blob is not None and not from_blob and stable and known != digest:
            # These are the blob's bytes (git's judgement, same identity before
            # and after): remember the digest for every other checkout.
            blob_index.put(blob, digest)
            learned += 1
        fresh[relative] = {**identity, "sha256": digest}
        manifest_entries.append(f"{relative} {digest}")

    manifest = "\n".join(manifest_entries)
    digest = hashlib.sha256(manifest.encode()).hexdigest()
    cache_write_error = False
    oversized = False
    if cache_enabled and not unbounded:
        try:
            oversized = not _write_cache(cache_path, root, patterns, fresh)
        except OSError:
            cache_write_error = True
    if blob_index is not None:
        blob_index.flush()
        if blob_index.read_errors or blob_index.write_errors:
            blob_status = "fallback"
        elif reused_by_blob:
            blob_status = "hit"
        elif learned:
            blob_status = "learned"
    if unbounded:
        status = "unbounded"
    elif not cache_enabled:
        status = "disabled"
    elif oversized:
        status = "oversized"
    elif cache_read_error or cache_write_error:
        status = "fallback"
    elif paths and reused == len(paths) and set(cached) == set(paths):
        status = "hit"
    else:
        status = "miss"
    elapsed_ms = max(0, (time.perf_counter_ns() - started) // 1_000_000)
    return SourceSnapshot(
        root=root,
        patterns=patterns,
        digest=digest,
        files=len(paths),
        verification={
            "cache": status,
            "cache_hit": status == "hit",
            "files_hashed": hashed,
            "files_reused": reused,
            "files_reused_by_blob": reused_by_blob,
            "blobs_learned": learned,
            "blob_index": blob_status,
            "exact": reused == 0,
            "duration_ms": min(elapsed_ms, 86_400_000),
        },
        entries=tuple((relative, entry["sha256"]) for relative, entry in sorted(fresh.items())),
    )


def tree_sha256(
    root: Path,
    patterns: tuple[str, ...] = DEFAULT_PATTERNS,
    *,
    cache_dir: Path | str | None = None,
) -> tuple[str, int]:
    """Hash a source tree: sha256 over a sorted manifest of per-file hashes.

    Returns (hash, file_count). The manifest is hashed rather than the
    concatenated bytes so that a renamed file changes the hash -- a file moving
    between packages moves its surfaces, and the artifact must not claim
    otherwise.
    """
    snapshot = snapshot_tree(root, patterns, cache_dir=cache_dir)
    return snapshot.digest, snapshot.files


def provenance(
    artifact: str,
    artifact_sha256: str,
    extractor: str,
    files: int,
    patterns: tuple[str, ...] | None = None,
    *,
    snapshot: SourceSnapshot | None = None,
) -> dict:
    """Describe the run: which bytes, read by whom, when -- and over which globs.

    `artifact_sha256` is only meaningful together with the pattern set it was
    taken over: the same tree hashed as `**/*.py` and as `*.go` gives two
    different, equally valid answers. Recording the patterns is what lets a LATER
    reader (`capcov outcomes`) recompute the same hash instead of silently
    recomputing a different one and calling the inventory stale. Omitted for an
    artifact written over the Python default, so old artifacts keep reading.
    """
    doc = {
        "artifact": artifact,
        "artifact_sha256": artifact_sha256,
        "artifact_files": files,
        "extractor": extractor,
        "extracted_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    if patterns is not None and tuple(patterns) != DEFAULT_PATTERNS:
        doc["source_patterns"] = list(patterns)
    if snapshot is not None:
        if snapshot.digest != artifact_sha256 or snapshot.files != files:
            raise ValueError("source snapshot does not match artifact provenance")
        # `exact`: every digest was read from bytes by the publishing process.
        # `cached`: the fast path was used and this identity is PROVISIONAL --
        # it becomes authoritative only when an exact artifact over the same
        # tree agrees with it (reconcile enforces this), never on its own.
        doc["source_snapshot"] = {
            "version": 1,
            "manifest_sha256": snapshot.digest,
            "files": snapshot.files,
            "verification": "exact" if snapshot.exact else "cached",
        }
    return doc


SNAPSHOT_VERIFICATIONS = ("exact", "cached")


def snapshot_verification_of(derived_from: dict) -> str:
    """How an artifact's digest was established: ``exact`` or ``cached``.

    Artifacts written before the cache existed have no ``source_snapshot`` and
    were always hashed from bytes, so their absence reads as exact.  A snapshot
    block that does not say it was read from bytes was not: it is provisional.
    """
    optional = derived_from.get("source_snapshot")
    if optional is None:
        return "exact"
    if not isinstance(optional, dict) or optional.get("version") != 1:
        raise ValueError("invalid serialized source snapshot")
    verification = optional.get("verification", "cached")
    if verification not in SNAPSHOT_VERIFICATIONS:
        raise ValueError(f"unknown source snapshot verification {verification!r}")
    return verification


def mark_provisional(derived_from: dict) -> dict:
    """What a driver hands to a probe: an identity nobody has yet verified.

    The driver's begin-side capture may itself have been exact (a cold miss),
    but that was the tree BEFORE the exercise.  The artifact is published
    after it, in a process the driver cannot vouch for, so the identity it
    carries is provisional until the driver's own exact walk stamps it --
    otherwise a driver that dies mid-run leaves an artifact reconcile accepts.
    """
    optional = derived_from.get("source_snapshot") or {
        "version": 1,
        "manifest_sha256": derived_from.get("artifact_sha256"),
        "files": derived_from.get("artifact_files"),
    }
    return {
        **derived_from,
        "source_snapshot": {**optional, "verification": "cached"},
    }


def mark_exact(derived_from: dict, verified: SourceSnapshot) -> dict:
    """Carry an exact verification outward onto a provisional provenance.

    The one process that read the bytes stamps the artifact; a child that was
    handed a carried identity cannot, because it read nothing.
    """
    if not verified.exact:
        raise ValueError("only an exact snapshot can mark provenance exact")
    if (
        derived_from.get("artifact_sha256") != verified.digest
        or derived_from.get("artifact_files") != verified.files
    ):
        raise ValueError("exact snapshot does not match the provenance it would mark")
    optional = derived_from.get("source_snapshot") or {
        "version": 1,
        "manifest_sha256": verified.digest,
        "files": verified.files,
    }
    return {
        **derived_from,
        "source_snapshot": {**optional, "verification": "exact"},
    }


def source_snapshot_of(root: Path, derived_from: dict) -> SourceSnapshot:
    """Rebuild and validate a serialized provenance snapshot.

    Old artifacts have no ``source_snapshot`` and remain valid: the authoritative
    fields have always been ``artifact_sha256`` and ``artifact_files``.
    """
    patterns = source_patterns_of(derived_from)
    # A validation helper answers "is this exactly the tree it names", so it
    # reads bytes; a cached answer here would be the cache validating itself.
    current = snapshot_tree(root, patterns, trust_cache=False)
    expected_digest = derived_from.get("artifact_sha256")
    expected_files = derived_from.get("artifact_files")
    if type(expected_files) is not int:
        # Defaulting this to the value under test made the check vacuous.
        raise ValueError("source provenance lacks an exact file count")
    optional = derived_from.get("source_snapshot")
    if optional is not None:
        if not isinstance(optional, dict) or optional.get("version") != 1:
            raise ValueError("invalid serialized source snapshot")
        if (
            optional.get("manifest_sha256") != expected_digest
            or optional.get("files") != expected_files
        ):
            raise ValueError("serialized source snapshot disagrees with derived_from")
    if current.digest != expected_digest or current.files != expected_files:
        raise ValueError("source provenance does not match the current tree")
    return current


def carried_source_snapshot(root: Path, derived_from: dict) -> SourceSnapshot:
    """Deserialize a snapshot identity without re-reading the source tree.

    This is used only for the begin-side of an observe freshness guard.  The
    guard always captures an authoritative current snapshot after the exercise
    and compares it with this identity before publishing evidence.
    """
    digest = derived_from.get("artifact_sha256")
    files = derived_from.get("artifact_files")
    if not isinstance(digest, str) or len(digest) != 64 or type(files) is not int:
        raise ValueError("source provenance lacks an exact digest and file count")
    try:
        int(digest, 16)
    except ValueError as exc:
        raise ValueError("source provenance has an invalid SHA-256 digest") from exc
    optional = derived_from.get("source_snapshot")
    if optional is not None and (
        not isinstance(optional, dict)
        or optional.get("version") != 1
        or optional.get("manifest_sha256") != digest
        or optional.get("files") != files
    ):
        raise ValueError("serialized source snapshot disagrees with derived_from")
    snapshot_verification_of(derived_from)
    return SourceSnapshot(
        root=Path(root).resolve(),
        patterns=source_patterns_of(derived_from),
        digest=digest,
        files=files,
        verification={
            # Nothing was read to build this: it is an identity handed over by
            # the driver, which verifies it from bytes before publication.
            "cache": "carried",
            "cache_hit": False,
            "files_hashed": 0,
            "files_reused": 0,
            "exact": False,
            "duration_ms": 0,
        },
        entries=(),
        carried=True,
    )


def source_patterns_of(derived_from: dict) -> tuple[str, ...]:
    """The globs an artifact's `artifact_sha256` was taken over.

    An artifact written before provenance carried the field, or written over the
    Python default, has none -- that is the default, not an error.
    """
    return tuple(derived_from.get("source_patterns") or DEFAULT_PATTERNS)


def write(path: Path, kind: str, derived_from: dict, body: dict) -> None:
    doc = {"schema_version": SCHEMA_VERSION, "kind": kind, "derived_from": derived_from}
    doc.update(body)
    write_document(path, doc)


def write_document(path: Path, doc: dict) -> None:
    """Atomically publish a complete JSON artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    value = (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode()
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read(path: Path, expect_kind: str | None = None) -> dict:
    doc = json.loads(path.read_text())
    if expect_kind and doc.get("kind") != expect_kind:
        raise SystemExit(
            f"{path}: expected a {expect_kind!r} artifact, found {doc.get('kind')!r}"
        )
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise SystemExit(
            f"{path}: schema_version {doc.get('schema_version')}, "
            f"this capcov speaks {SCHEMA_VERSION}"
        )
    return doc


def normalise(doc: dict) -> str:
    """Render an artifact's DERIVED CONTENT, for --check diffs."""
    clone = json.loads(json.dumps(doc))
    for field in VOLATILE:
        clone.pop(field, None)
    return json.dumps(clone, indent=2, sort_keys=True) + "\n"


def same_artifact(a: dict, b: dict) -> tuple[bool, str]:
    """Do two artifacts describe the same tree?"""
    ah = a.get("derived_from", {}).get("artifact_sha256")
    bh = b.get("derived_from", {}).get("artifact_sha256")
    if ah and bh and ah == bh:
        return True, ""
    return False, (
        f"static ran against {ah or '<none>'}, runtime against {bh or '<none>'}. "
        "Re-run both against the same tree; a diff across two trees is not a finding."
    )

# --- SCIP fact exporter helpers (experiment/claim-semantics) -----------------
# The static-claim exporter needs the file list itself (which files *should*
# have been indexed) and the tree digest, walking the tree exactly once and
# exactly the way the digest did.  Built on ``snapshot_tree`` so the incremental
# cache and the digest formula are shared, not duplicated.

def patterns_for(language: str) -> tuple[str, ...]:
    """The glob set a tree of ``language`` is hashed over.

    Raises ``KeyError`` for a language with no pattern set rather than falling
    back to the Python default: hashing a Go tree as ``**/*.py`` yields the
    empty manifest, a digest that would agree across every Go tree.
    """
    if language not in LANGUAGE_PATTERNS:
        raise KeyError(language)
    return (LANGUAGE_PATTERNS[language],)


def tree_manifest(
    root: Path, patterns: tuple[str, ...] = DEFAULT_PATTERNS, *,
    cache_dir: Path | str | None = None,
) -> list[tuple[str, str]]:
    """The sorted ``(tree-relative posix path, sha256)`` manifest ``tree_sha256``
    hashes, from one snapshot walk."""
    return list(snapshot_tree(root, patterns, cache_dir=cache_dir).entries)


def manifest_sha256(entries: list[tuple[str, str]]) -> str:
    """The tree digest of a ``tree_manifest``: sha256 over ``"<path> <sha256>"``
    lines sorted by path -- the formula ``snapshot_tree`` uses, so a consumer
    holding the manifest reproduces the digest without a second walk."""
    manifest = "\n".join(sorted(set(f"{rel} {digest}" for rel, digest in entries)))
    return hashlib.sha256(manifest.encode()).hexdigest()
