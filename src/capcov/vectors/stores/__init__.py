"""Store adapters: one per state store a system keeps, all behind a command runner.

Each adapter has the same three verbs -- ``snapshot() -> token``,
``restore(token)``, ``inspect() -> dict`` -- so ``ComposedFixture`` can treat
MySQL, Mongo, Redis and an object store alike. A token is opaque to the fixture
(dump bytes, a listing, a key list); an inspection is a dict the fixture merges
by key (``rows``, ``collections``, ``queues``, ``redis_keys``, ``objects``).

External processes are reached only through ``runner.CommandRunner``; sockets
only through the Redis adapter's own stdlib client. No adapter imports a driver.
"""

from .runner import CommandFailed, CommandRunner, DockerExecRunner, LocalRunner  # noqa: F401
from .mysql import MySQLStore, TableReadError, parse_batch  # noqa: F401
from .mongo import MongoInspectError, MongoSnapshotUnsupported, MongoStore  # noqa: F401
from .redis import RedisError, RedisStore, RespClient  # noqa: F401
from .s3 import ListingError, S3RestoreGap, S3Store  # noqa: F401

__all__ = [
    "CommandFailed", "CommandRunner", "DockerExecRunner", "LocalRunner",
    "MySQLStore", "TableReadError", "parse_batch",
    "MongoInspectError", "MongoSnapshotUnsupported", "MongoStore",
    "RedisError", "RedisStore", "RespClient",
    "ListingError", "S3RestoreGap", "S3Store",
]
