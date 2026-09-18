"""Compare-time normalization: a versioned policy for what is volatile.

Recorded vectors stay raw. When replay compares a candidate's result with the
recorded one it applies THIS policy to both sides, so a timestamp the oracle
emitted at record time and the one the candidate emits at replay time compare
equal, and so a change to the policy never forces a re-record. Recording
provenance retains the version known at capture; the replay artifact separately
binds frozen config, source bytes and loaded implementation identity for the
policy executed during comparison.

Three rules, in order:

1. Keys that Mongo-style document encoders add (``_id``, ``$oid``, ``$date``)
   are dropped: they are generated per insert and carry no behaviour.
2. A value under a volatile KEY NAME (timestamps, tokens, expiries, generated
   transaction ids: ``VOLATILE_KEY``) is replaced by ``<volatile>``, unless it
   is empty (``None``, ``""``, ``0``) -- an empty timestamp against a filled
   one is a real difference and stays visible.
3. A string VALUE shaped like a datetime (``DATETIME``) is replaced by
   ``<volatile-datetime>`` wherever it appears, because systems emit timestamps
   under names no key pattern anticipates.

The policy is idempotent: normalizing a normalized value changes nothing, which
is what lets both sides be normalized without worrying whether one already was.
Callers add exact key names with ``extra_volatile_keys`` for a consumer's own
generated fields; the built-in pattern is not widened per consumer.
"""

from __future__ import annotations

import hashlib
import json
import marshal
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

NORMALIZATION_VERSION = "v2"
POLICY_IDENTITY_V1 = "capcov-vector-comparison-policy/v1"
POLICY_IDENTITY_FORMAT = "capcov-vector-comparison-policy/v2"

VOLATILE_KEY = re.compile(
    r"(_at$|_date$|^date|modified|created$|updated|timestamp|expires|token|^transaction$)", re.I
)
DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$"
)
DROPPED_KEYS = frozenset({"_id", "$oid", "$date"})

MASK_VOLATILE = "<volatile>"
MASK_DATETIME = "<volatile-datetime>"

try:
    # Pin the module location and bytes observed during import. Replay checks
    # that this source has not drifted before publishing artifacts; the policy
    # identity also fingerprints the actual loaded normalizer code objects.
    _IMPORTED_SOURCE_PATH = Path(__file__).resolve()
    _IMPORTED_SOURCE_SHA256 = hashlib.sha256(_IMPORTED_SOURCE_PATH.read_bytes()).hexdigest()
except OSError:
    _IMPORTED_SOURCE_PATH = None
    _IMPORTED_SOURCE_SHA256 = None


def _json_sha256(value) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class FrozenComparisonPolicy:
    """One replay's immutable normalization config and bound implementation."""

    normalization_version: str
    volatile_key_pattern: str
    volatile_key_flags: int
    datetime_pattern: str
    datetime_flags: int
    dropped_keys: frozenset[str]
    mask_volatile: str
    mask_datetime: str
    preserved_empty_volatile_values: tuple
    extra_volatile_keys: frozenset[str]
    source_sha256: str
    implementation_sha256: str
    python_implementation: str
    python_version: str
    _normalizer: object = field(repr=False, compare=False)

    def normalize(self, value):
        """Apply the closure built from this policy's frozen values."""
        return self._normalizer(value)

    def identity(self) -> dict:
        config = {
            "normalization_version": self.normalization_version,
            "volatile_key_pattern": self.volatile_key_pattern,
            "volatile_key_flags": self.volatile_key_flags,
            "datetime_pattern": self.datetime_pattern,
            "datetime_flags": self.datetime_flags,
            "dropped_keys": sorted(self.dropped_keys),
            "mask_volatile": self.mask_volatile,
            "mask_datetime": self.mask_datetime,
            "preserved_empty_volatile_values": list(self.preserved_empty_volatile_values),
            "extra_volatile_keys": sorted(self.extra_volatile_keys),
            "python_implementation": self.python_implementation,
            "python_version": self.python_version,
        }
        core = {
            "format": POLICY_IDENTITY_FORMAT,
            "normalization_version": self.normalization_version,
            "source_sha256": self.source_sha256,
            "implementation_sha256": self.implementation_sha256,
            "config": config,
            "config_sha256": _json_sha256(config),
        }
        return {**core, "policy_sha256": _json_sha256(core)}


def freeze_comparison_policy(extra_volatile_keys=()) -> FrozenComparisonPolicy:
    """Snapshot module config and bind a recursive normalizer to that snapshot.

    The returned closure never reads the mutable module globals again. Its code
    objects are hashed separately from the import-time source bytes, so cached
    bytecode is described as loaded; drift of the source file since import is
    refused before replay publication. This is bookkeeping, not authentication.
    """
    if _IMPORTED_SOURCE_SHA256 is None:
        raise RuntimeError("cannot identify the imported comparison-policy source")
    version = NORMALIZATION_VERSION
    volatile_pattern, volatile_flags = VOLATILE_KEY.pattern, int(VOLATILE_KEY.flags)
    datetime_pattern, datetime_flags = DATETIME.pattern, int(DATETIME.flags)
    dropped_keys = frozenset(DROPPED_KEYS)
    mask_volatile, mask_datetime = MASK_VOLATILE, MASK_DATETIME
    preserved_empty = (None, "", 0)
    extra = frozenset(extra_volatile_keys)
    # Bind builtins and the compiled patterns to the closures too. Fixture and
    # endpoint callbacks cannot change this run by changing module globals.
    is_instance = isinstance
    to_bool = bool
    dict_type, list_type, tuple_type, str_type = dict, list, tuple, str
    volatile_re = re.compile(volatile_pattern, volatile_flags)
    datetime_re = re.compile(datetime_pattern, datetime_flags)

    def key_is_volatile(key):
        if not is_instance(key, str_type):
            return False
        return to_bool(volatile_re.search(key)) or key in extra

    def normalize_value(value):
        if is_instance(value, dict_type):
            out = {}
            for key, item in value.items():
                if key in dropped_keys:
                    continue
                if key_is_volatile(key) and item not in preserved_empty:
                    out[key] = mask_volatile
                else:
                    out[key] = normalize_value(item)
            return out
        if is_instance(value, list_type):
            return [normalize_value(item) for item in value]
        if is_instance(value, tuple_type):
            return [normalize_value(item) for item in value]
        if is_instance(value, str_type) and datetime_re.match(value):
            return mask_datetime
        return value

    implementation = hashlib.sha256(
        marshal.dumps(key_is_volatile.__code__) + marshal.dumps(normalize_value.__code__)
    ).hexdigest()
    return FrozenComparisonPolicy(
        normalization_version=version,
        volatile_key_pattern=volatile_pattern,
        volatile_key_flags=volatile_flags,
        datetime_pattern=datetime_pattern,
        datetime_flags=datetime_flags,
        dropped_keys=dropped_keys,
        mask_volatile=mask_volatile,
        mask_datetime=mask_datetime,
        preserved_empty_volatile_values=preserved_empty,
        extra_volatile_keys=extra,
        source_sha256=_IMPORTED_SOURCE_SHA256,
        implementation_sha256=implementation,
        python_implementation=sys.implementation.name,
        python_version=sys.version.split()[0],
        _normalizer=normalize_value,
    )


def assert_comparison_policy_source_unchanged(policy: FrozenComparisonPolicy) -> None:
    """Refuse replay publication if the imported source file drifted mid-run."""
    if _IMPORTED_SOURCE_PATH is None:
        raise RuntimeError("cannot verify the imported comparison-policy source")
    try:
        current = hashlib.sha256(_IMPORTED_SOURCE_PATH.read_bytes()).hexdigest()
    except OSError as exc:
        raise RuntimeError("cannot verify the imported comparison-policy source") from exc
    if current != _IMPORTED_SOURCE_SHA256 or policy.source_sha256 != _IMPORTED_SOURCE_SHA256:
        raise RuntimeError("comparison-policy source changed since import; refusing replay artifact publication")


def comparison_policy_identity() -> dict:
    """Describe a frozen default policy (local execution disclosure only)."""
    return freeze_comparison_policy().identity()


def validate_comparison_policy_identity(identity) -> bool:
    """Check a replay's source/config identity for exact shape and self-consistency.

    Historical replay artifacts are not compared with today's normalizer. The
    recorded identity is a local execution disclosure, not an authenticated
    attestation of which code actually ran.
    """
    if not isinstance(identity, dict):
        return False
    identity_format = identity.get("format")
    if identity_format == POLICY_IDENTITY_V1:
        fields = {"format", "normalization_version", "source_sha256", "config",
                  "config_sha256", "policy_sha256"}
        config_fields = {
            "normalization_version", "volatile_key_pattern", "volatile_key_flags",
            "datetime_pattern", "datetime_flags", "dropped_keys", "mask_volatile",
            "mask_datetime", "preserved_empty_volatile_values", "extra_volatile_keys",
        }
    elif identity_format == POLICY_IDENTITY_FORMAT:
        fields = {"format", "normalization_version", "source_sha256", "implementation_sha256",
                  "config", "config_sha256", "policy_sha256"}
        config_fields = {
            "normalization_version", "volatile_key_pattern", "volatile_key_flags",
            "datetime_pattern", "datetime_flags", "dropped_keys", "mask_volatile",
            "mask_datetime", "preserved_empty_volatile_values", "extra_volatile_keys",
            "python_implementation", "python_version",
        }
    else:
        return False
    if set(identity) != fields:
        return False
    version = identity.get("normalization_version")
    source_digest = identity.get("source_sha256")
    implementation_digest = identity.get("implementation_sha256")
    config = identity.get("config")
    if (not isinstance(version, str) or not version
            or not isinstance(source_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", source_digest) is None):
        return False
    if (identity_format == POLICY_IDENTITY_FORMAT
            and (not isinstance(implementation_digest, str)
                 or re.fullmatch(r"[0-9a-f]{64}", implementation_digest) is None)):
        return False
    if not isinstance(config, dict) or set(config) != config_fields:
        return False
    if (config.get("normalization_version") != version
            or not isinstance(config.get("volatile_key_pattern"), str)
            or type(config.get("volatile_key_flags")) is not int
            or not isinstance(config.get("datetime_pattern"), str)
            or type(config.get("datetime_flags")) is not int
            or not isinstance(config.get("dropped_keys"), list)
            or any(not isinstance(item, str) for item in config["dropped_keys"])
            or not isinstance(config.get("mask_volatile"), str)
            or not isinstance(config.get("mask_datetime"), str)
            or not isinstance(config.get("preserved_empty_volatile_values"), list)
            or not isinstance(config.get("extra_volatile_keys"), list)
            or any(not isinstance(item, str) for item in config["extra_volatile_keys"])
            or (identity_format == POLICY_IDENTITY_FORMAT
                and (not isinstance(config.get("python_implementation"), str)
                     or not config["python_implementation"]
                     or not isinstance(config.get("python_version"), str)
                     or not config["python_version"]))):
        return False
    config_digest = identity.get("config_sha256")
    policy_digest = identity.get("policy_sha256")
    if any(not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
           for value in (config_digest, policy_digest)):
        return False
    if _json_sha256(config) != config_digest:
        return False
    core = {key: value for key, value in identity.items() if key != "policy_sha256"}
    return _json_sha256(core) == policy_digest


def is_volatile_key(key: object, extra_volatile_keys: tuple[str, ...] | frozenset[str] = ()) -> bool:
    """Whether a dict key names a volatile value under this policy."""
    if not isinstance(key, str):
        return False
    return bool(VOLATILE_KEY.search(key)) or key in extra_volatile_keys


def normalize(value, extra_volatile_keys=()):
    """Apply the policy to ``value`` recursively; returns a new structure."""
    return freeze_comparison_policy(extra_volatile_keys).normalize(value)
