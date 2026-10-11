"""The store adapters and the composed fixture, with no docker, no daemon, no network.

Every adapter reaches the world through one seam: a ``CommandRunner`` for the
command-driven stores (MySQL, Mongo, S3) and a loopback socket for Redis. So the
MySQL/Mongo/S3 tests use a ``FakeRunner`` that returns canned bytes and records
the exact argv -- the assertion is on what the adapter ASKED for, since that is
the whole of its behaviour -- and the Redis tests run the real RESP client
against an in-process fake server that speaks enough of the protocol (SCAN with
a cursor that pages, DUMP, PTTL, RESTORE REPLACE, FLUSHALL, LLEN, PING, SELECT).

The composition tests use fake stores and prove the honest parts: a token that
does not cover every store is refused, a name two stores both report is refused,
and a drain that never empties the queues raises naming them instead of
returning.
"""

from __future__ import annotations

import hashlib
import json
import socketserver
import sys
import threading
import unittest

from capcov.vectors import fixture as fixture_mod
from capcov.vectors.fixture import ComposedFixture, Fixture, FixtureError, QueuesNotDrained, Store
from capcov.vectors.stores import (
    CommandFailed,
    DockerExecRunner,
    ListingError,
    LocalRunner,
    MongoInspectError,
    MongoSnapshotUnsupported,
    MongoStore,
    MySQLStore,
    RedisError,
    RedisStore,
    S3RestoreGap,
    S3Store,
    TableReadError,
    parse_batch,
)


class FakeRunner:
    """Canned stdout per call, in order; records every (argv, input) it was handed."""

    def __init__(self, responses=None, script=None):
        self.responses = list(responses or [])
        self.script = script
        self.calls: list[tuple[list[str], bytes | None]] = []

    def run(self, argv, input=None):
        self.calls.append((list(argv), input))
        if self.script is not None:
            return self.script(list(argv), input)
        if not self.responses:
            raise AssertionError(f"FakeRunner: no canned response left for {argv}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    @property
    def argvs(self):
        return [argv for argv, _ in self.calls]


# --------------------------------------------------------------------------- runner


class RunnerTests(unittest.TestCase):
    def test_docker_exec_prefixes_argv_and_passes_stdin(self):
        inner = FakeRunner([b"out"])
        runner = DockerExecRunner("db-1", inner=inner)
        self.assertEqual(runner.run(["mariadb", "-uroot", "app"], input=b"SQL"), b"out")
        self.assertEqual(inner.calls, [(["docker", "exec", "-i", "db-1", "mariadb", "-uroot", "app"], b"SQL")])

    def test_docker_exec_refuses_empty_container(self):
        with self.assertRaises(ValueError):
            DockerExecRunner("")

    def test_local_runner_returns_stdout_and_feeds_stdin(self):
        out = LocalRunner(timeout=30).run(
            [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read().upper())"], input=b"abc"
        )
        self.assertEqual(out, b"ABC")

    def test_local_runner_raises_named_failure_on_nonzero_exit(self):
        with self.assertRaises(CommandFailed) as caught:
            LocalRunner(timeout=30).run([sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"])
        self.assertEqual(caught.exception.returncode, 3)
        self.assertIn("boom", str(caught.exception))
        self.assertEqual(caught.exception.argv[0], sys.executable)


# --------------------------------------------------------------------------- mysql


class BatchReaderTests(unittest.TestCase):
    def test_header_and_rows_with_escapes_and_null(self):
        text = "id\tname\tnote\n1\talpha\tNULL\n2\tbe\\tta\tline\\nbreak \\\\ end\n"
        self.assertEqual(
            parse_batch(text, "orders"),
            [
                {"id": "1", "name": "alpha", "note": None},
                {"id": "2", "name": "be\tta", "note": "line\nbreak \\ end"},
            ],
        )

    def test_no_output_is_an_empty_table(self):
        self.assertEqual(parse_batch("", "orders"), [])

    def test_ragged_row_is_an_error_naming_the_table(self):
        with self.assertRaises(TableReadError) as caught:
            parse_batch("id\tname\n1\n", "orders")
        self.assertIn("orders", str(caught.exception))


class MySQLStoreTests(unittest.TestCase):
    def test_snapshot_runs_the_dump_and_returns_its_bytes(self):
        runner = FakeRunner([b"-- dump --"])
        store = MySQLStore(runner, "app")
        self.assertEqual(store.snapshot(), b"-- dump --")
        self.assertEqual(runner.argvs, [["mariadb-dump", "-uroot", "--skip-comments", "app"]])

    def test_restore_pipes_the_dump_through_the_client(self):
        runner = FakeRunner([b""])
        MySQLStore(runner, "app", client_argv=("mysql", "-uapp", "-psecret")).restore(b"-- dump --")
        self.assertEqual(runner.calls, [(["mysql", "-uapp", "-psecret", "app"], b"-- dump --")])

    def test_restore_refuses_an_empty_token(self):
        with self.assertRaises(ValueError):
            MySQLStore(FakeRunner(), "app").restore(b"")

    def test_inspect_reads_every_table_and_omits_empty_ones(self):
        def script(argv, _input):
            if "SHOW TABLES" in argv:
                return b"orders\nmigrations\nempty_table\n"
            statement = argv[-1]
            if statement == "SELECT * FROM `orders`":
                return b"id\ttotal\n7\t12.50\n"
            if statement == "SELECT * FROM `empty_table`":
                return b""
            raise AssertionError(f"unexpected {argv}")

        runner = FakeRunner(script=script)
        store = MySQLStore(runner, "app")
        self.assertEqual(store.inspect(exclude=("migrations",)), {"rows": {"orders": [{"id": "7", "total": "12.50"}]}})
        self.assertEqual(runner.argvs[0], ["mariadb", "-uroot", "--batch", "--skip-column-names", "app", "-e", "SHOW TABLES"])
        self.assertEqual(runner.argvs[1], ["mariadb", "-uroot", "--batch", "app", "-e", "SELECT * FROM `orders`"])
        self.assertNotIn("SELECT * FROM `migrations`", [argv[-1] for argv in runner.argvs])

    def test_inspect_named_tables_skips_the_listing(self):
        runner = FakeRunner([b"id\n1\n"])
        self.assertEqual(MySQLStore(runner, "app").inspect(tables=["orders"]), {"rows": {"orders": [{"id": "1"}]}})
        self.assertEqual(len(runner.calls), 1)

    def test_read_table_refuses_a_backtick_in_the_name(self):
        with self.assertRaises(ValueError):
            MySQLStore(FakeRunner(), "app").read_table("a`b")

    def test_execute_streams_a_script_on_stdin(self):
        runner = FakeRunner([b""])
        MySQLStore(runner, "app").execute("", input=b"UPDATE orders SET total = 0;")
        self.assertEqual(runner.calls, [(["mariadb", "-uroot", "--batch", "app"], b"UPDATE orders SET total = 0;")])

    def test_adapter_satisfies_the_store_protocol(self):
        self.assertIsInstance(MySQLStore(FakeRunner(), "app"), Store)


# --------------------------------------------------------------------------- mongo


class MongoStoreTests(unittest.TestCase):
    def test_inspect_evals_one_script_naming_the_database_and_drops_empty_collections(self):
        runner = FakeRunner([json.dumps({"audit": [{"_id": {"$oid": "a"}, "n": 1}], "empty": []}).encode()])
        store = MongoStore(runner, "app")
        self.assertEqual(store.inspect(), {"collections": {"audit": [{"_id": {"$oid": "a"}, "n": 1}]}})
        argv = runner.argvs[0]
        self.assertEqual(argv[:3], ["mongosh", "--quiet", "--eval"])
        self.assertIn('getSiblingDB("app")', argv[3])
        self.assertIn("EJSON.stringify", argv[3])

    def test_inspect_refuses_non_json_output(self):
        with self.assertRaises(MongoInspectError) as caught:
            MongoStore(FakeRunner([b"MongoServerError: not authorized"]), "app").inspect()
        self.assertIn("not authorized", str(caught.exception))

    def test_drop_mode_snapshot_of_an_empty_database_and_restore_drops_it(self):
        runner = FakeRunner([b"{}", b""])
        store = MongoStore(runner, "app")
        token = store.snapshot()
        self.assertEqual(token, {"mode": "drop"})
        store.restore(token)
        self.assertIn('getSiblingDB("app").dropDatabase()', runner.argvs[1][3])

    def test_drop_mode_refuses_to_snapshot_a_non_empty_database(self):
        runner = FakeRunner([json.dumps({"audit": [{"n": 1}, {"n": 2}]}).encode()])
        with self.assertRaises(MongoSnapshotUnsupported) as caught:
            MongoStore(runner, "app").snapshot()
        self.assertIn("audit=2", str(caught.exception))
        self.assertIn("dump_argv", str(caught.exception))

    def test_archive_mode_round_trips_the_dump_bytes(self):
        runner = FakeRunner([b"ARCHIVE", b""])
        store = MongoStore(
            runner, "app",
            dump_argv=("mongodump", "--archive", "--db", "app"),
            restore_argv=("mongorestore", "--archive", "--drop"),
        )
        token = store.snapshot()
        self.assertEqual(token, {"mode": "archive", "archive": b"ARCHIVE"})
        store.restore(token)
        self.assertEqual(runner.calls[0], (["mongodump", "--archive", "--db", "app"], None))
        self.assertEqual(runner.calls[1], (["mongorestore", "--archive", "--drop"], b"ARCHIVE"))

    def test_token_from_the_other_mode_is_refused(self):
        with self.assertRaises(ValueError):
            MongoStore(FakeRunner(), "app").restore({"mode": "archive", "archive": b""})

    def test_dump_argv_without_restore_argv_is_refused(self):
        with self.assertRaises(ValueError):
            MongoStore(FakeRunner(), "app", dump_argv=("mongodump",))


# --------------------------------------------------------------------------- redis


class _FakeRedisState:
    """What the fake server holds: values, ttls, and a log of the commands it saw."""

    def __init__(self):
        self.values: dict[bytes, tuple[str, object]] = {}
        self.ttls: dict[bytes, int] = {}
        self.commands: list[list[bytes]] = []
        self.selected: list[int] = []
        self.lock = threading.Lock()
        self.page = 2  # SCAN returns at most this many keys per call, so the cursor is exercised


class _FakeRedisHandler(socketserver.StreamRequestHandler):
    def _read_command(self):
        head = self.rfile.readline()
        if not head:
            return None
        if not head.startswith(b"*"):
            raise ValueError("fake redis: expected an array")
        count = int(head[1:].strip())
        parts = []
        for _ in range(count):
            size = int(self.rfile.readline()[1:].strip())
            parts.append(self.rfile.read(size))
            self.rfile.read(2)
        return parts

    def _bulk(self, data):
        if data is None:
            return b"$-1\r\n"
        return b"$%d\r\n%s\r\n" % (len(data), data)

    def _reply(self, args):
        state: _FakeRedisState = self.server.state
        name = args[0].upper()
        state.commands.append(args)
        if name == b"PING":
            return b"+PONG\r\n"
        if name == b"SELECT":
            state.selected.append(int(args[1]))
            return b"+OK\r\n"
        if name == b"FLUSHALL":
            state.values.clear()
            state.ttls.clear()
            return b"+OK\r\n"
        if name == b"DBSIZE":
            return b":%d\r\n" % len(state.values)
        if name == b"SCAN":
            keys = sorted(state.values)
            start = int(args[1])
            page = keys[start:start + state.page]
            nxt = start + state.page
            cursor = b"0" if nxt >= len(keys) else str(nxt).encode()
            return b"*2\r\n" + self._bulk(cursor) + b"*%d\r\n" % len(page) + b"".join(self._bulk(k) for k in page)
        if name == b"DUMP":
            entry = state.values.get(args[1])
            if entry is None:
                return self._bulk(None)
            kind, value = entry
            payload = json.dumps({"kind": kind, "value": value if kind == "string" else list(value)}).encode()
            return self._bulk(b"DUMP:" + payload)
        if name == b"PTTL":
            if args[1] not in state.values:
                return b":-2\r\n"
            return b":%d\r\n" % state.ttls.get(args[1], -1)
        if name == b"RESTORE":
            key, ttl, payload = args[1], int(args[2]), args[3]
            replace = len(args) > 4 and args[4].upper() == b"REPLACE"
            if key in state.values and not replace:
                return b"-BUSYKEY Target key name already exists.\r\n"
            if not payload.startswith(b"DUMP:"):
                return b"-ERR DUMP payload version or checksum are wrong\r\n"
            decoded = json.loads(payload[5:])
            state.values[key] = (decoded["kind"], decoded["value"])
            if ttl > 0:
                state.ttls[key] = ttl
            else:
                state.ttls.pop(key, None)
            return b"+OK\r\n"
        if name == b"LLEN":
            entry = state.values.get(args[1])
            if entry is None:
                return b":0\r\n"
            if entry[0] != "list":
                return b"-WRONGTYPE Operation against a key holding the wrong kind of value\r\n"
            return b":%d\r\n" % len(entry[1])
        if name == b"SET":
            state.values[args[1]] = ("string", args[2].decode())
            return b"+OK\r\n"
        if name == b"RPUSH":
            kind, items = state.values.get(args[1], ("list", []))
            items = list(items) + [a.decode() for a in args[2:]]
            state.values[args[1]] = ("list", items)
            return b":%d\r\n" % len(items)
        if name == b"PEXPIRE":
            state.ttls[args[1]] = int(args[2])
            return b":1\r\n"
        return b"-ERR unknown command '%s'\r\n" % args[0]

    def handle(self):
        while True:
            try:
                args = self._read_command()
            except (ValueError, ConnectionError):
                return
            if args is None:
                return
            with self.server.state.lock:
                reply = self._reply(args)
            self.wfile.write(reply)
            self.wfile.flush()


class _FakeRedisServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class RedisStoreTests(unittest.TestCase):
    def setUp(self):
        self.state = _FakeRedisState()
        self.server = _FakeRedisServer(("127.0.0.1", 0), _FakeRedisHandler)
        self.server.state = self.state
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def store(self, **kwargs) -> RedisStore:
        return RedisStore("127.0.0.1", self.port, **kwargs)

    def seed(self):
        client = self.store().connect()
        try:
            client.call("SET", "session:1", "alice")
            client.call("SET", "session:2", "bob")
            client.call("PEXPIRE", "session:2", 60000)
            client.call("RPUSH", "queues:default", "job-a", "job-b")
            client.call("SET", "cache:x", "1")
            client.call("SET", "cache:y", "2")
        finally:
            client.close()

    def test_ping(self):
        self.assertTrue(self.store().ping())

    def test_snapshot_pages_through_scan_and_carries_ttls(self):
        self.seed()
        entries = self.store().snapshot()
        self.assertEqual(sorted(key for key, _, _ in entries),
                         [b"cache:x", b"cache:y", b"queues:default", b"session:1", b"session:2"])
        ttls = {key: ttl for key, ttl, _ in entries}
        self.assertEqual(ttls[b"session:2"], 60000)
        self.assertEqual(ttls[b"session:1"], 0)  # PTTL -1 becomes "no expiry"
        scans = [c for c in self.state.commands if c[0].upper() == b"SCAN"]
        self.assertGreater(len(scans), 1, "five keys at two per page must take more than one SCAN")
        self.assertEqual(scans[0][1], b"0")
        self.assertNotEqual(scans[1][1], b"0", "the second SCAN must resume from the returned cursor")

    def test_restore_flushes_then_restores_every_key_with_replace(self):
        self.seed()
        store = self.store()
        token = store.snapshot()
        client = store.connect()
        client.call("SET", "session:1", "mallory")     # mutate
        client.call("SET", "cache:z", "3")             # add
        client.call("FLUSHALL")                        # and lose the rest
        client.close()
        store.restore(token)
        self.assertEqual(self.state.values[b"session:1"], ("string", "alice"))
        self.assertNotIn(b"cache:z", self.state.values)
        self.assertEqual(self.state.values[b"queues:default"], ("list", ["job-a", "job-b"]))
        self.assertEqual(self.state.ttls[b"session:2"], 60000)
        restores = [c for c in self.state.commands if c[0].upper() == b"RESTORE"]
        self.assertEqual(len(restores), 5)
        self.assertTrue(all(c[-1].upper() == b"REPLACE" for c in restores))

    def test_inspect_lists_keys_and_queue_depths(self):
        self.seed()
        inspection = self.store(queues={"default": "queues:default", "mail": "queues:mail"}).inspect()
        self.assertEqual(inspection, {
            "redis_keys": ["cache:x", "cache:y", "queues:default", "session:1", "session:2"],
            "redis": {key.decode(): {"sha256": hashlib.sha256(payload).hexdigest(), "expiring": ttl > 0}
                      for key, ttl, payload in self.store().snapshot()},
            "queues": {"default": 2, "mail": 0},
        })

    def test_observer_detects_same_key_value_and_expiry_changes_without_leaking_values(self):
        self.seed()
        store = self.store()
        mark = store.mark()
        client = store.connect()
        try:
            client.call("SET", "session:1", "private-new-value")
            client.call("PEXPIRE", "cache:x", 60000)
        finally:
            client.close()
        changes = store.changes_since(mark)
        self.assertEqual(changes["informational"]["redis.values"]["changed"],
                         ["cache:x", "session:1"])
        self.assertNotIn("private-new-value", json.dumps(changes))

    def test_select_is_issued_for_a_non_default_db(self):
        self.store(db=3).ping()
        self.assertEqual(self.state.selected, [3])
        self.store().ping()
        self.assertEqual(self.state.selected, [3], "db 0 issues no SELECT")

    def test_server_error_reply_is_a_named_error(self):
        self.seed()
        with self.assertRaises(RedisError) as caught:
            self.store(queues={"bad": "session:1"}).inspect()
        self.assertIn("WRONGTYPE", str(caught.exception))

    def test_adapter_satisfies_the_store_protocol(self):
        self.assertIsInstance(self.store(), Store)


# --------------------------------------------------------------------------- s3


def _listing(*items):
    return json.dumps({"Contents": [{"Key": key, "ETag": f'"{etag}"'} for key, etag in items]}).encode()


class S3StoreTests(unittest.TestCase):
    def test_listing_uses_the_default_argv_and_strips_etag_quotes(self):
        runner = FakeRunner([_listing(("uploads/a.txt", "e1"), ("uploads/b.txt", "e2"))])
        store = S3Store(runner, "bucket-a", "uploads/")
        self.assertEqual(store.snapshot(), {"uploads/a.txt": "e1", "uploads/b.txt": "e2"})
        self.assertEqual(runner.argvs[0], ["aws", "s3api", "list-objects-v2", "--bucket", "bucket-a",
                                           "--prefix", "uploads/", "--output", "json"])

    def test_empty_output_is_an_empty_listing_and_inspect_is_keyed_by_label(self):
        runner = FakeRunner([b"", b""])
        store = S3Store(runner, "bucket-a", "uploads/", listing_argv=("mc", "ls", "--json", "local/bucket-a/uploads/"))
        self.assertEqual(store.listing(), {})
        self.assertEqual(store.inspect(), {"objects": {"bucket-a/uploads/": {}}})
        self.assertEqual(runner.argvs[0], ["mc", "ls", "--json", "local/bucket-a/uploads/"])

    def test_non_json_listing_is_refused(self):
        with self.assertRaises(ListingError):
            S3Store(FakeRunner([b"An error occurred (AccessDenied)"]), "bucket-a").listing()

    def test_restore_with_nothing_changed_runs_only_the_listing(self):
        runner = FakeRunner([_listing(("k", "e1")), _listing(("k", "e1"))])
        store = S3Store(runner, "bucket-a")
        store.restore(store.snapshot())
        self.assertEqual(len(runner.calls), 2)

    def test_restore_deletes_added_keys_when_a_delete_command_is_configured(self):
        runner = FakeRunner([_listing(("k", "e1")), _listing(("k", "e1"), ("new", "e9")), b""])
        store = S3Store(runner, "bucket-a", delete_argv=("aws", "s3api", "delete-object", "--bucket", "bucket-a", "--key", "{key}"))
        store.restore(store.snapshot())
        self.assertEqual(runner.argvs[2], ["aws", "s3api", "delete-object", "--bucket", "bucket-a", "--key", "new"])

    def test_restore_names_what_it_cannot_undo(self):
        runner = FakeRunner([_listing(("kept", "e1"), ("gone", "e2"), ("rewritten", "e3")),
                             _listing(("kept", "e1"), ("rewritten", "e4"), ("added", "e5"))])
        store = S3Store(runner, "bucket-a")
        token = store.snapshot()
        with self.assertRaises(S3RestoreGap) as caught:
            store.restore(token)
        gap = caught.exception
        self.assertEqual((gap.added, gap.removed, gap.changed), (["added"], ["gone"], ["rewritten"]))
        for key in ("added", "gone", "rewritten"):
            self.assertIn(key, str(gap))


# --------------------------------------------------------------------------- composition


class _FakeStore:
    def __init__(self, name, inspection, token="tok"):
        self.name, self.inspection, self.token = name, inspection, token
        self.restored = []

    def snapshot(self):
        return self.token

    def restore(self, token):
        self.restored.append(token)

    def inspect(self):
        return self.inspection() if callable(self.inspection) else self.inspection


class ComposedFixtureTests(unittest.TestCase):
    def test_snapshot_and_restore_go_to_every_store_with_its_own_token(self):
        db = _FakeStore("db", {"rows": {"orders": [{"id": "1"}]}}, token=b"dump")
        kv = _FakeStore("kv", {"redis_keys": ["a"], "queues": {"default": 0}}, token=[(b"a", 0, b"p")])
        fixture = ComposedFixture({"db": db, "kv": kv})
        token = fixture.snapshot()
        self.assertEqual(token, {"db": b"dump", "kv": [(b"a", 0, b"p")]})
        fixture.restore(token)
        self.assertEqual(db.restored, [b"dump"])
        self.assertEqual(kv.restored, [[(b"a", 0, b"p")]])
        self.assertIsInstance(fixture, Fixture)

    def test_token_that_does_not_cover_every_store_is_refused(self):
        fixture = ComposedFixture({"db": _FakeStore("db", {}), "kv": _FakeStore("kv", {})})
        with self.assertRaises(FixtureError) as caught:
            fixture.restore({"db": b"dump"})
        self.assertIn("kv", str(caught.exception))

    def test_inspect_merges_every_store_into_the_five_key_shape(self):
        fixture = ComposedFixture({
            "db": _FakeStore("db", {"rows": {"orders": [{"id": "1"}]}}),
            "docs": _FakeStore("docs", {"collections": {"audit": [{"n": 1}]}}),
            "kv": _FakeStore("kv", {"redis_keys": ["b", "a"], "queues": {"default": 1}}),
            "files": _FakeStore("files", {"objects": {"bucket-a": {"k": "e"}}}),
        })
        self.assertEqual(fixture.inspect(), {
            "rows": {"orders": [{"id": "1"}]},
            "collections": {"audit": [{"n": 1}]},
            "queues": {"default": 1},
            "redis_keys": ["a", "b"],
            "redis": {},
            "objects": {"bucket-a": {"k": "e"}},
        })

    def test_a_name_reported_by_two_stores_is_refused(self):
        fixture = ComposedFixture({
            "one": _FakeStore("one", {"rows": {"orders": []}}),
            "two": _FakeStore("two", {"rows": {"orders": []}}),
        })
        with self.assertRaises(FixtureError) as caught:
            fixture.inspect()
        self.assertIn("'one'", str(caught.exception))
        self.assertIn("'two'", str(caught.exception))

    def test_an_unknown_inspection_key_is_refused(self):
        with self.assertRaises(FixtureError):
            ComposedFixture({"x": _FakeStore("x", {"tables": {}})}).inspect()

    def test_a_store_without_the_three_verbs_is_refused(self):
        with self.assertRaises(FixtureError):
            ComposedFixture({"x": object()})
        with self.assertRaises(FixtureError):
            ComposedFixture({})

    def test_drain_calls_the_worker_until_queues_are_empty(self):
        depth = {"default": 3}
        calls = []

        def drain():
            calls.append(1)
            depth["default"] -= 1

        kv = _FakeStore("kv", lambda: {"queues": dict(depth), "redis_keys": []})
        ComposedFixture({"kv": kv}, drain=drain).drain()
        self.assertEqual(len(calls), 3)
        self.assertEqual(depth, {"default": 0})

    def test_drain_is_bounded_and_names_the_stuck_queues(self):
        kv = _FakeStore("kv", {"queues": {"default": 0, "stuck": 4}, "redis_keys": []})
        calls = []
        with self.assertRaises(QueuesNotDrained) as caught:
            ComposedFixture({"kv": kv}, drain=lambda: calls.append(1), max_drain_rounds=2).drain()
        self.assertEqual(len(calls), 2)
        self.assertEqual(caught.exception.queues, {"stuck": 4})
        self.assertIn("stuck=4", str(caught.exception))
        self.assertNotIn("default", str(caught.exception))

    def test_drain_with_no_worker_and_pending_work_raises_at_once(self):
        kv = _FakeStore("kv", {"queues": {"default": 1}})
        with self.assertRaises(QueuesNotDrained):
            ComposedFixture({"kv": kv}).drain()

    def test_drain_with_no_worker_and_nothing_pending_is_fine(self):
        ComposedFixture({"kv": _FakeStore("kv", {"queues": {"default": 0}})}).drain()

    def test_merge_helper_always_returns_every_key(self):
        self.assertEqual(fixture_mod.merge_inspections({}), fixture_mod.empty_inspection())


if __name__ == "__main__":
    unittest.main()
