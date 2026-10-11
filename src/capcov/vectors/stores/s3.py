"""Object-store adapter: the listing (key -> etag) under a prefix, via a command.

An object store has no dump. What a vector can observe is which keys exist and
what their content hash is, so the snapshot IS the listing and inspection
reports it again; the diff on either side of an operation shows uploads,
deletions and overwrites by key.

The listing command is injectable (default: ``aws s3api list-objects-v2`` with
JSON output) and must print ``{"Contents": [{"Key", "ETag"}, ...]}``; anything
else is ``ListingError``. A local S3-compatible service is reached by pointing
the argv at it (``--endpoint-url``); the adapter does not know the difference.

Restore is honest about what it cannot do. Keys ADDED since the snapshot are
removed through ``delete_argv`` (a template with ``{key}``) when one is
configured. Keys REMOVED or REWRITTEN since the snapshot cannot be put back from
a listing, and keys added with no ``delete_argv`` cannot be removed, so restore
raises ``S3RestoreGap`` naming every such key rather than report a restore it
did not make.
"""

from __future__ import annotations

import json

from .runner import CommandRunner


class ListingError(RuntimeError):
    """The listing command did not print the expected JSON shape."""


class S3RestoreGap(RuntimeError):
    """Restore could not return the listing to the snapshot; names the keys."""

    def __init__(self, added: list[str], removed: list[str], changed: list[str]) -> None:
        parts = []
        if added:
            parts.append(f"added (no delete_argv to remove): {', '.join(added)}")
        if removed:
            parts.append(f"removed (no bytes to put back): {', '.join(removed)}")
        if changed:
            parts.append(f"rewritten (no bytes to put back): {', '.join(changed)}")
        super().__init__("object store not restored -- " + "; ".join(parts))
        self.added, self.removed, self.changed = added, removed, changed


class S3Store:
    def __init__(
        self,
        runner: CommandRunner,
        bucket: str,
        prefix: str = "",
        listing_argv: tuple[str, ...] | list[str] | None = None,
        delete_argv: tuple[str, ...] | list[str] | None = None,
    ) -> None:
        if not bucket:
            raise ValueError("S3Store: bucket is empty")
        self.runner = runner
        self.bucket = bucket
        self.prefix = prefix
        self.listing_argv = list(listing_argv) if listing_argv else [
            "aws", "s3api", "list-objects-v2", "--bucket", bucket, "--prefix", prefix, "--output", "json",
        ]
        self.delete_argv = list(delete_argv) if delete_argv else None

    @property
    def label(self) -> str:
        return f"{self.bucket}/{self.prefix}" if self.prefix else self.bucket

    def listing(self) -> dict[str, str]:
        """``{key: etag}`` for every object under the prefix; an empty prefix lists nothing at all as ``{}``."""
        out = self.runner.run(self.listing_argv).decode(errors="replace")
        if not out.strip():
            return {}  # the aws CLI prints nothing for an empty prefix
        try:
            parsed = json.loads(out)
        except ValueError:
            raise ListingError(f"{self.label}: listing did not print JSON: {out[:400]!r}") from None
        contents = parsed.get("Contents", []) if isinstance(parsed, dict) else None
        if contents is None or not all(isinstance(item, dict) and "Key" in item for item in contents):
            raise ListingError(f"{self.label}: listing JSON has no Contents[].Key")
        return {item["Key"]: str(item.get("ETag", "")).strip('"') for item in contents}

    def snapshot(self) -> dict[str, str]:
        return self.listing()

    def inspect(self) -> dict:
        return {"objects": {self.label: self.listing()}}

    def restore(self, token: dict[str, str]) -> None:
        if not isinstance(token, dict):
            raise ValueError("S3Store.restore: token is not a listing")
        current = self.listing()
        added = sorted(key for key in current if key not in token)
        removed = sorted(key for key in token if key not in current)
        changed = sorted(key for key in token if key in current and current[key] != token[key])
        if added and self.delete_argv:
            for key in added:
                self.runner.run([part.replace("{key}", key) for part in self.delete_argv])
            added = []
        if added or removed or changed:
            raise S3RestoreGap(added, removed, changed)
