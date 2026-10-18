#!/usr/bin/env python3
"""Run the unit suite and fail on any skip that is not in the committed allowlist.

A test that skips because a tool is missing reports nothing about the code, and
unittest still exits 0. This runner makes the skip set part of the result: the
run fails when a test skips that the allowlist does not name, and when a test the
allowlist names did not skip. Every allowlist entry carries the reason the skip
is accepted.

Usage: python scripts/check-skips.py [--allowlist FILE] [MODULE ...]
       python scripts/check-skips.py [--family NAME] --shard I/N --result FILE
       python scripts/check-skips.py [--allowlist FILE] --merge FILE [FILE ...]

With no MODULE the whole of tests/ is discovered, as
`python -m unittest discover -s tests -t .` does. Run it from the project root.

--shard I/N runs one of N parts of the discovered suite and writes what it ran,
what skipped and how long each test took to FILE. It fails when a test fails and
does not judge the skips. --merge reads the N files, discovers the suite again and
fails unless the shards ran every discovered test exactly once; it then judges the
union of their skips against the allowlist, as an unsharded run does.

--family NAME restricts a shard to the test classes of one tool family, so that it
can run where only that family's tools are installed. tests/tool-families.json
lists the classes that need souffle, the Shen runtime or the SCIP indexers; every
other class is in the family `base`, which needs none of them. A class that needs
a tool and is not listed runs in `base`, skips or fails there, and fails the run.
--shard I/N then splits that family. The merge takes the shards of every family
and still requires that together they ran every discovered test exactly once.

Test classes are assigned to shards whole, heaviest first, each to the shard with
the least assigned time. The times come from tests/shard-timings.json, which
`--merge ... --write-timings tests/shard-timings.json` rewrites from a run. A class
that took more than a tenth of the run is written test by test, and each test the
file names is assigned on its own, so the tests of such a class can go to different
shards; each of those shards sets up the class fixture. A class or test the file
does not name is assigned with its class, at the median time.

--jobs N runs a shard's test classes in N worker processes (default: the number of
CPUs, or $CAPCOV_TEST_JOBS). A class runs whole in one worker, so its class fixture
is set up once, in the process its tests run in; the classes are handed out
heaviest first by the same timings. Each worker discovers the suite itself and
must find the one the shard discovered, gets a TMPDIR of its own, and runs with
CAPCOV_TEST_JOBS=1. A worker that dies fails the shard, and the tests of the class
it was running are left out of what the shard ran. --jobs 1 runs the shard in this
process, in discovery order, as plain unittest does. The seconds a parallel run
records include the contention between its workers.

Allowlist format (JSON):

    {"version": 1,
     "accepted": [{"reason": "why these may skip", "tests": ["<test id>", ...]}]}
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

DEFAULT_ALLOWLIST = Path("tests") / "skips-allowed.json"
DEFAULT_TIMINGS = Path("tests") / "shard-timings.json"
DEFAULT_FAMILIES = Path("tests") / "tool-families.json"
# A class above this share of a run's time is recorded, and assigned, test by test.
SPLIT_SHARE = 0.1
# The number of worker processes a shard runs its classes in, when --jobs is not given.
JOBS_ENV = "CAPCOV_TEST_JOBS"


class AllowlistError(ValueError):
    """The allowlist file is not usable."""


def load_allowlist(path: Path) -> dict[str, str]:
    """Map each accepted test id to the reason it may skip."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AllowlistError(f"{path}: {exc}") from exc
    if not isinstance(document, dict) or document.get("version") != 1:
        raise AllowlistError(f"{path}: expected an object with \"version\": 1")
    accepted: dict[str, str] = {}
    for index, group in enumerate(document.get("accepted", [])):
        reason = group.get("reason") if isinstance(group, dict) else None
        tests = group.get("tests") if isinstance(group, dict) else None
        if not isinstance(reason, str) or not reason.strip():
            raise AllowlistError(f"{path}: accepted[{index}] has no reason")
        if not isinstance(tests, list) or not tests or not all(isinstance(t, str) for t in tests):
            raise AllowlistError(f"{path}: accepted[{index}] must list its tests")
        for test in tests:
            if test in accepted:
                raise AllowlistError(f"{path}: {test} is listed twice")
            accepted[test] = reason
    return accepted


def compare(skipped: dict[str, str], accepted: dict[str, str]) -> tuple[list[str], list[str]]:
    """Return (skipped but not accepted, accepted but not skipped), both sorted."""
    unexpected = sorted(set(skipped) - set(accepted))
    stale = sorted(set(accepted) - set(skipped))
    return unexpected, stale


def report(skipped: dict[str, str], accepted: dict[str, str]) -> tuple[bool, list[str]]:
    """Return (the skip set matches the allowlist, the lines to print)."""
    unexpected, stale = compare(skipped, accepted)
    lines = [f"skipped: {len(skipped)}; accepted by the allowlist: {len(accepted)}"]
    for test in sorted(set(skipped) & set(accepted)):
        lines.append(f"  accepted skip: {test}")
        lines.append(f"    reason: {accepted[test]}")
    for test in unexpected:
        lines.append(f"  UNEXPECTED skip: {test}")
        lines.append(f"    unittest said: {skipped[test]}")
    for test in stale:
        lines.append(f"  STALE allowlist entry (the test ran, or no longer exists): {test}")
    if unexpected:
        lines.append(f"{len(unexpected)} test(s) skipped outside the allowlist. Provide the missing "
                     "tool, or add the test to the allowlist with the reason.")
    if stale:
        lines.append(f"{len(stale)} allowlist entry(ies) did not skip. Remove them from the allowlist.")
    return not unexpected and not stale, lines


class ShardError(ValueError):
    """A shard argument, a timings file or a shard result is not usable."""


def iter_tests(suite: unittest.TestSuite) -> Iterator[unittest.TestCase]:
    """Yield the test cases of a suite in the order unittest would run them."""
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from iter_tests(item)
        else:
            yield item


def class_of(test_id: str) -> str:
    """The id of the class a test belongs to: the test id without the method name."""
    return test_id.rpartition(".")[0]


class Families:
    """Which tool family each test class runs in."""

    def __init__(self, default: str, family_of_class: dict[str, str]) -> None:
        self.default = default
        self.family_of_class = family_of_class

    def names(self) -> list[str]:
        return sorted({self.default, *self.family_of_class.values()})

    def of(self, test_id: str) -> str:
        return self.family_of_class.get(class_of(test_id), self.default)


def load_families(path: Path) -> Families:
    """Read tests/tool-families.json: {"default": name, "families": {name: {"classes": [...]}}}."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if document.get("version") != 1 or not isinstance(document["default"], str):
            raise ValueError('expected "version": 1 and a "default" family')
        family_of_class: dict[str, str] = {}
        for name, entry in document["families"].items():
            if name == document["default"]:
                raise ValueError(f"the default family {name} lists no classes")
            for class_id in entry["classes"]:
                if class_id in family_of_class:
                    raise ValueError(f"{class_id} is in two families")
                family_of_class[class_id] = name
        return Families(document["default"], family_of_class)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ShardError(f"{path}: {exc}") from exc


def unit_of(test_id: str, timings: dict[str, float]) -> str:
    """What is assigned to a shard as a whole: the test when the timings name it, else its class."""
    return test_id if test_id in timings else class_of(test_id)


def load_timings(path: Path) -> dict[str, float]:
    """Map each test class, or test of a split class, to its seconds in the recorded run."""
    if not path.exists():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        seconds = document["seconds"]
        if document.get("version") != 1 or not isinstance(seconds, dict):
            raise ValueError("expected {\"version\": 1, \"seconds\": {class or test: seconds}}")
        return {str(name): float(value) for name, value in seconds.items()}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ShardError(f"{path}: {exc}") from exc


def assign_shards(test_ids: list[str], count: int, timings: dict[str, float]) -> dict[str, int]:
    """Map every test id to a shard in 1..count.

    The result depends only on the set of ids, the count and the timings, so
    every shard of a run computes the same assignment.
    """
    units = sorted({unit_of(test_id, timings) for test_id in test_ids})
    default = statistics.median(timings.values()) if timings else 1.0
    weight = {name: timings.get(name, default) for name in units}
    load = [0.0] * count
    shard_of_unit: dict[str, int] = {}
    for name in sorted(units, key=lambda name: (-weight[name], name)):
        index = min(range(count), key=lambda i: (load[i], i))
        load[index] += weight[name]
        shard_of_unit[name] = index + 1
    return {test_id: shard_of_unit[unit_of(test_id, timings)] for test_id in test_ids}


def timings_of(seconds: dict[str, float]) -> dict[str, float]:
    """The timings document of a run: seconds per class, or per test of a class above SPLIT_SHARE."""
    by_class: dict[str, float] = {}
    for test_id, value in seconds.items():
        by_class[class_of(test_id)] = by_class.get(class_of(test_id), 0.0) + value
    limit = SPLIT_SHARE * sum(by_class.values())
    document: dict[str, float] = {}
    for test_id, value in seconds.items():
        name = class_of(test_id)
        if by_class[name] > limit:
            document[test_id] = round(value, 1)
        else:
            document[name] = round(by_class[name], 1)
    return dict(sorted(document.items()))


def parse_shard(text: str) -> tuple[int, int]:
    """Read "I/N" with 1 <= I <= N."""
    try:
        index, count = (int(part) for part in text.split("/"))
    except ValueError as exc:
        raise ShardError(f"--shard wants I/N, got {text!r}") from exc
    if not 1 <= index <= count:
        raise ShardError(f"--shard wants 1 <= I <= N, got {text!r}")
    return index, count


class TimedResult(unittest.TextTestResult):
    """A text result that also records which tests ran and how long each took.

    A test's time runs from the end of the test before it, so it includes the
    class and module fixtures that were set up for it.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.seconds: dict[str, float] = {}
        self._mark = time.perf_counter()

    def stopTest(self, test: unittest.TestCase) -> None:
        super().stopTest(test)
        now = time.perf_counter()
        self.seconds[test.id()] = now - self._mark
        self._mark = now


def check_partition(discovered: list[str], shards: list[dict]) -> list[str]:
    """Return what stops the shards from being one run of the discovered suite."""
    problems: list[str] = []
    by_family: dict[str, list[dict]] = {}
    for shard in shards:
        by_family.setdefault(shard.get("family", ""), []).append(shard)
    for family, group in sorted(by_family.items()):
        label = f"family {family}: " if family else ""
        counts = {shard["count"] for shard in group}
        indexes = sorted(shard["index"] for shard in group)
        if len(counts) != 1:
            problems.append(f"{label}the shard files disagree on the number of shards: {sorted(counts)}")
        elif indexes != list(range(1, next(iter(counts)) + 1)):
            problems.append(f"{label}expected shards 1..{next(iter(counts))}, got {indexes}")
    repeated = sorted(test_id for test_id, n in Counter(discovered).items() if n > 1)
    if repeated:
        problems.append(f"discovery yields {len(repeated)} test id(s) more than once: {repeated[:5]}")
    wanted = set(discovered)
    ran: Counter[str] = Counter()
    for shard in shards:
        if sorted(shard["discovered"]) != sorted(discovered):
            problems.append(f"shard {shard['index']} discovered a different suite "
                            f"({len(shard['discovered'])} tests against {len(discovered)})")
        ran.update(shard["ran"])
    missing = sorted(wanted - set(ran))
    extra = sorted(set(ran) - wanted)
    twice = sorted(test_id for test_id, n in ran.items() if n > 1)
    if missing:
        problems.append(f"{len(missing)} discovered test(s) ran in no shard: {missing[:5]}")
    if extra:
        problems.append(f"{len(extra)} test(s) ran that discovery does not yield: {extra[:5]}")
    if twice:
        problems.append(f"{len(twice)} test(s) ran in more than one shard: {twice[:5]}")
    return problems


def parse_jobs(text: str, source: str) -> int:
    """Read a worker count of at least 1."""
    try:
        jobs = int(text)
    except ValueError:
        jobs = 0
    if jobs < 1:
        raise ShardError(f"{source} wants a whole number of at least 1, got {text!r}")
    return jobs


def default_jobs() -> int:
    text = os.environ.get(JOBS_ENV, "").strip()
    if text:
        return parse_jobs(text, f"${JOBS_ENV}")
    # The CPUs this process may run on (Python 3.13+), else all of them.
    return getattr(os, "process_cpu_count", os.cpu_count)() or 1


def suite_digest(test_ids: list[str]) -> str:
    """What a worker and its shard compare to know they discovered the same suite."""
    return hashlib.sha256("\n".join(test_ids).encode("utf-8")).hexdigest()


def class_units(positions: list[int], test_ids: list[str],
                timings: dict[str, float]) -> list[tuple[str, list[int], float]]:
    """Group the selected tests by class: (class id, positions in discovery order, weight).

    A class is weighed as the timings name it, as the sum of its tests the timings
    name, or at the median; the heaviest comes first.
    """
    members: dict[str, list[int]] = {}
    for position in positions:
        members.setdefault(class_of(test_ids[position]), []).append(position)
    default = statistics.median(timings.values()) if timings else 1.0
    units = []
    for name, group in members.items():
        named = [timings[test_ids[p]] for p in group if test_ids[p] in timings]
        weight = timings.get(name, sum(named) if named else default)
        units.append((name, group, weight))
    return sorted(units, key=lambda unit: (-unit[2], unit[0]))


def run_class(tests: list[unittest.TestCase]) -> dict:
    """Run the tests of one class as a suite of their own; return what the shard records.

    A fresh result per class, so that the class fixture is torn down at the end of
    this class and its time is not charged to the next one.
    """
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=1, resultclass=TimedResult).run(
        unittest.TestSuite(tests))
    errors = [[flavour, result.getDescription(test), text]
              for flavour, listed in (("ERROR", result.errors), ("FAIL", result.failures))
              for test, text in listed]
    return {
        "seconds": result.seconds,
        "skipped": [[test.id(), message] for test, message in result.skipped],
        "errors": errors,
        "unexpected_successes": [result.getDescription(test) for test in result.unexpectedSuccesses],
        "expected_failures": len(result.expectedFailures),
        "tests_run": result.testsRun,
        "successful": result.wasSuccessful(),
    }


def serve_worker(suite: unittest.TestSuite, channel: str) -> int:
    """The worker side of --jobs: run the classes the shard sends until it closes the pipe.

    The shard and the worker talk over two pipes the shard opened, one JSON line at a
    time, so that nothing a test prints or reads on the standard streams reaches them.
    """
    read_fd, write_fd = (int(fd) for fd in channel.split(","))
    for fd in (read_fd, write_fd):
        os.set_inheritable(fd, False)
    tests = list(iter_tests(suite))
    with os.fdopen(read_fd, "r", encoding="utf-8") as requests, \
            os.fdopen(write_fd, "w", encoding="utf-8") as replies:
        replies.write(json.dumps({"suite": suite_digest([test.id() for test in tests])}) + "\n")
        replies.flush()
        for line in requests:
            reply = run_class([tests[position] for position in json.loads(line)])
            replies.write(json.dumps(reply) + "\n")
            replies.flush()
    return 0


class WorkerPool:
    """Run classes in worker processes, each class once, and gather what they report.

    One thread per worker hands it the next class, heaviest first, and reads the
    reply. A worker that dies is replaced; the class it was running is reported as
    crashed and not run again. A worker that cannot start, or discovers another
    suite, ends its thread, and a class no worker took is reported as never run.
    """

    def __init__(self, tests: list[unittest.TestCase], units: list[tuple[str, list[int], float]],
                 jobs: int) -> None:
        self.test_ids = [test.id() for test in tests]
        self.digest = suite_digest(self.test_ids)
        self.pending = list(units)
        self.jobs = max(1, min(jobs, len(units)))
        self.replies: list[tuple[str, dict]] = []
        # The classes that were handed out and came back with no reply.
        self.lost: list[str] = []
        # The class each worker is running.
        self.running: dict[int, str] = {}
        self.problems: list[str] = []
        self.done = 0
        self.lock = threading.Lock()
        self.processes: set[subprocess.Popen] = set()

    def _next(self) -> tuple[str, list[int], float] | None:
        with self.lock:
            return self.pending.pop(0) if self.pending else None

    def _problem(self, text: str) -> None:
        with self.lock:
            self.problems.append(text)
        print(f"check-skips: {text}", file=sys.stderr, flush=True)

    def _start(self, slot: int, temp_root: Path):
        to_worker, from_shard = os.pipe()
        to_shard, from_worker = os.pipe()
        temp = Path(tempfile.mkdtemp(prefix=f"w{slot}-", dir=temp_root))
        env = {**os.environ, JOBS_ENV: "1", "TMPDIR": str(temp), "CAPCOV_TEST_WORKER": str(slot)}
        try:
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--worker", f"{to_worker},{from_worker}"],
                stdin=subprocess.DEVNULL, pass_fds=(to_worker, from_worker), env=env)
        finally:
            os.close(to_worker)
            os.close(from_worker)
        with self.lock:
            self.processes.add(process)
        return (process, os.fdopen(from_shard, "w", encoding="utf-8"),
                os.fdopen(to_shard, "r", encoding="utf-8"))

    def _stop(self, process: subprocess.Popen, requests, replies) -> int:
        for pipe in (requests, replies):
            try:
                pipe.close()
            except OSError:
                pass
        try:
            code = process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            process.kill()
            code = process.wait()
        with self.lock:
            self.processes.discard(process)
        return code

    @staticmethod
    def _read(replies) -> dict | None:
        """The next whole JSON line a worker wrote, or None when it ended or broke off."""
        try:
            line = replies.readline()
            return json.loads(line) if line.endswith("\n") else None
        except (OSError, ValueError):
            return None

    def _lose(self, name: str, text: str) -> None:
        with self.lock:
            self.lost.append(name)
        self._problem(text)

    def _drive(self, slot: int, temp_root: Path, total: int) -> None:
        try:
            self._serve(slot, temp_root, total)
        except BaseException as exc:  # a thread that fails must not lose its class silently
            with self.lock:
                name = self.running.pop(slot, None)
            if name is not None:
                self._lose(name, f"worker {slot} failed while it ran {name}: {exc!r}")
            else:
                self._problem(f"worker {slot} failed: {exc!r}")

    def _serve(self, slot: int, temp_root: Path, total: int) -> None:
        while True:
            process, requests, replies = self._start(slot, temp_root)
            hello = self._read(replies)
            if hello is None or hello.get("suite") != self.digest:
                code = self._stop(process, requests, replies)
                self._problem(f"worker {slot} " + ("discovered another suite" if hello
                              else f"exited with {code} before it was ready"))
                return
            while (unit := self._next()) is not None:
                name, positions, _ = unit
                with self.lock:
                    self.running[slot] = name
                try:
                    requests.write(json.dumps(positions) + "\n")
                    requests.flush()
                except OSError:
                    pass
                reply = self._read(replies)
                if reply is None:
                    code = self._stop(process, requests, replies)
                    with self.lock:
                        del self.running[slot]
                    self._lose(name, f"worker {slot} exited with {code} while it ran {name}; "
                                     f"its {len(positions)} test(s) did not run")
                    break
                with self.lock:
                    del self.running[slot]
                    self.replies.append((name, reply))
                    self.done += 1
                    done = self.done
                mark = "" if reply["successful"] else "  FAILED"
                print(f"check-skips: [{done}/{total}] {sum(reply['seconds'].values()):6.1f} s  "
                      f"{name} ({reply['tests_run']} tests){mark}", file=sys.stderr, flush=True)
            else:
                self._stop(process, requests, replies)
                return

    def run(self) -> None:
        total = len(self.pending)
        temp_root = Path(tempfile.mkdtemp(prefix="check-skips-"))
        threads = [threading.Thread(target=self._drive, args=(slot, temp_root, total), daemon=True)
                   for slot in range(1, self.jobs + 1)]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        finally:
            with self.lock:
                for process in self.processes:
                    process.kill()
            shutil.rmtree(temp_root, ignore_errors=True)
        for name, positions, _ in self.pending:
            self._lose(name, f"no worker ran {name}; its {len(positions)} test(s) did not run")


def run_parallel(tests: list[unittest.TestCase], selected: list[int], jobs: int,
                 timings: dict[str, float]) -> tuple[dict[str, float], dict[str, str], bool]:
    """Run the selected tests class by class in worker processes; (seconds, skipped, successful)."""
    units = class_units(selected, [test.id() for test in tests], timings)
    pool = WorkerPool(tests, units, jobs)
    print(f"check-skips: {len(units)} classes in {pool.jobs} worker processes", file=sys.stderr)
    started = time.perf_counter()
    pool.run()
    elapsed = time.perf_counter() - started

    seconds: dict[str, float] = {}
    skipped: dict[str, str] = {}
    problems = list(pool.problems)
    # Every class came back once, or was reported lost; nothing else may happen to one.
    accounted = Counter([name for name, _ in pool.replies] + pool.lost)
    if accounted != Counter(name for name, _, _ in units):
        problems.append("the classes the workers answered for are not the classes the shard sent")
    wanted = {pool.test_ids[position] for position in selected}
    errors, unexpected = [], []
    for _, reply in pool.replies:
        for test_id in reply["seconds"]:
            if test_id in seconds or test_id not in wanted:
                problems.append(f"a worker reported {test_id} twice, or a test the shard did not send")
        seconds.update(reply["seconds"])
        skipped.update(dict(reply["skipped"]))
        errors.extend(reply["errors"])
        unexpected.extend(reply["unexpected_successes"])

    # The report a TextTestRunner prints at the end of a run, for the run as a whole.
    stream = sys.stderr
    print(file=stream)
    for flavour, description, text in errors:
        print(unittest.TextTestResult.separator1, file=stream)
        print(f"{flavour}: {description}", file=stream)
        print(unittest.TextTestResult.separator2, file=stream)
        print(text, file=stream)
    if unexpected:
        print(unittest.TextTestResult.separator1, file=stream)
        for description in unexpected:
            print(f"UNEXPECTED SUCCESS: {description}", file=stream)
    for problem in problems:
        print(f"check-skips: {problem}", file=stream)
    tests_run = sum(reply["tests_run"] for _, reply in pool.replies)
    print(unittest.TextTestResult.separator2, file=stream)
    print(f"Ran {tests_run} test{'s' if tests_run != 1 else ''} in {elapsed:.3f}s", file=stream)
    print(file=stream)
    successful = not problems and all(reply["successful"] for _, reply in pool.replies)
    failures = sum(1 for flavour, _, _ in errors if flavour == "FAIL")
    infos = [f"{label}={n}" for label, n in (
        ("failures", failures), ("errors", len(errors) - failures), ("skipped", len(skipped)),
        ("expected failures", sum(reply["expected_failures"] for _, reply in pool.replies)),
        ("unexpected successes", len(unexpected)), ("worker problems", len(problems))) if n]
    print(("OK" if successful else "FAILED") + (f" ({', '.join(infos)})" if infos else ""), file=stream)
    return seconds, skipped, successful


def run_shard(suite: unittest.TestSuite, index: int, count: int, timings: dict[str, float],
              result_path: Path, family: str = "", families: Families | None = None,
              jobs: int = 1) -> int:
    tests = list(iter_tests(suite))
    discovered = [test.id() for test in tests]
    if family:
        assert families is not None
        if family not in families.names():
            raise ShardError(f"unknown family {family!r}; the families are {families.names()}")
        candidates = [test for test in tests if families.of(test.id()) == family]
    else:
        candidates = tests
    shard_of = assign_shards([test.id() for test in candidates], count, timings)
    selected = [test for test in candidates if shard_of[test.id()] == index]
    label = f"{family} " if family else ""
    print(f"check-skips: {label}shard {index}/{count} runs {len(selected)} of {len(discovered)} tests",
          file=sys.stderr)
    started = time.perf_counter()
    if jobs > 1 and selected:
        chosen = {id(test) for test in selected}
        seconds, skipped, successful = run_parallel(
            tests, [position for position, test in enumerate(tests) if id(test) in chosen], jobs, timings)
    else:
        result = unittest.TextTestRunner(verbosity=1, resultclass=TimedResult).run(
            unittest.TestSuite(selected))
        seconds = result.seconds
        skipped = {test.id(): message for test, message in result.skipped}
        successful = result.wasSuccessful()
    document = {
        "version": 1,
        "family": family,
        "index": index,
        "count": count,
        "discovered": discovered,
        "ran": sorted(seconds),
        "skipped": skipped,
        "seconds": seconds,
        "wall_seconds": time.perf_counter() - started,
        "successful": successful,
    }
    result_path.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if successful else 1


def stale_family_classes(discovered: list[str], families: Families | None) -> list[str]:
    """Classes the families file lists that discovery does not yield."""
    if families is None:
        return []
    classes = {class_of(test_id) for test_id in discovered}
    return sorted(set(families.family_of_class) - classes)


def merge_shards(suite: unittest.TestSuite, paths: list[Path], accepted: dict[str, str],
                 write_timings: Path | None, families: Families | None = None) -> int:
    try:
        shards = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    except (OSError, ValueError) as exc:
        print(f"check-skips: {exc}", file=sys.stderr)
        return 2
    discovered = [test.id() for test in iter_tests(suite)]
    problems = check_partition(discovered, shards)
    failed = sorted(f"{shard.get('family', '')} {shard['index']}".strip()
                    for shard in shards if not shard["successful"])
    if failed:
        problems.append(f"shard(s) {failed} had failing tests")
    stale = stale_family_classes(discovered, families)
    if stale:
        problems.append(f"the families file lists {len(stale)} class(es) that no longer exist: {stale[:5]}")

    seconds: dict[str, float] = {}
    skipped: dict[str, str] = {}
    for shard in sorted(shards, key=lambda shard: (shard.get("family", ""), shard["index"])):
        seconds.update(shard["seconds"])
        skipped.update(shard["skipped"])
        label = f"{shard['family']} " if shard.get("family") else ""
        print(f"{label}shard {shard['index']}/{shard['count']}: {len(shard['ran'])} tests, "
              f"{shard['wall_seconds']:.0f} s", file=sys.stderr)
    print(f"discovered: {len(discovered)}; ran across {len(shards)} shards: {len(seconds)}",
          file=sys.stderr)
    for test_id, value in sorted(seconds.items(), key=lambda item: -item[1])[:15]:
        print(f"  {value:7.1f} s  {test_id}", file=sys.stderr)
    for problem in problems:
        print(f"check-skips: {problem}", file=sys.stderr)

    matches, lines = report(skipped, accepted)
    print("\n".join(lines), file=sys.stderr)
    if write_timings is not None:
        document = {"version": 1, "seconds": timings_of(seconds)}
        write_timings.write_text(json.dumps(document, indent=1) + "\n", encoding="utf-8")
    if not discovered:
        print("check-skips: no tests were discovered", file=sys.stderr)
        return 1
    return 0 if matches and not problems else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--allowlist", type=Path, default=DEFAULT_ALLOWLIST)
    parser.add_argument("--shard", metavar="I/N", help="run part I of N of the discovered suite")
    parser.add_argument("--family", metavar="NAME",
                        help="with --shard: split only the test classes of this tool family")
    parser.add_argument("--families", type=Path, default=DEFAULT_FAMILIES,
                        help="which test classes belong to which tool family")
    parser.add_argument("--result", type=Path, help="with --shard: where to write the shard's result")
    parser.add_argument("--timings", type=Path, default=DEFAULT_TIMINGS,
                        help="seconds per test class, used to balance the shards")
    parser.add_argument("--merge", nargs="+", type=Path, metavar="FILE",
                        help="judge the shard results in these files as one run")
    parser.add_argument("--write-timings", type=Path, metavar="FILE",
                        help="with --merge: write the seconds per test class of this run")
    parser.add_argument("--jobs", metavar="N",
                        help=f"with --shard: run the test classes in N worker processes "
                             f"(default: ${JOBS_ENV}, else the number of CPUs)")
    parser.add_argument("--worker", metavar="R,W", help=argparse.SUPPRESS)
    parser.add_argument("modules", nargs="*", help="test modules; default: discover tests/")
    args = parser.parse_args(argv)
    if args.jobs and not args.shard:
        parser.error("--jobs needs --shard; use --shard 1/1 for the whole suite")
    if (args.shard or args.merge) and args.modules:
        parser.error("--shard and --merge work on the discovered suite; give no MODULE")
    if bool(args.shard) != bool(args.result) or (args.shard and args.merge):
        parser.error("--shard needs --result, and cannot be combined with --merge")
    if args.family and not args.shard:
        parser.error("--family needs --shard; use --shard 1/1 for the whole family")

    try:
        # A shard does not judge its skips, so it reads no allowlist.
        accepted = {} if args.shard or args.worker else load_allowlist(args.allowlist)
    except AllowlistError as exc:
        print(f"check-skips: {exc}", file=sys.stderr)
        return 2

    # `python -m unittest` puts the working directory on sys.path; a script does not.
    if "" not in sys.path and str(Path.cwd()) not in sys.path:
        sys.path.insert(0, str(Path.cwd()))
    loader = unittest.defaultTestLoader
    if args.modules:
        suite = loader.loadTestsFromNames(args.modules)
    else:
        suite = loader.discover("tests", top_level_dir=".")
    if args.worker:
        return serve_worker(suite, args.worker)
    if args.shard:
        try:
            index, count = parse_shard(args.shard)
            jobs = parse_jobs(args.jobs, "--jobs") if args.jobs else default_jobs()
            families = load_families(args.families) if args.family else None
            return run_shard(suite, index, count, load_timings(args.timings), args.result,
                             args.family or "", families, jobs)
        except ShardError as exc:
            print(f"check-skips: {exc}", file=sys.stderr)
            return 2
    if args.merge:
        try:
            families = load_families(args.families) if args.families.exists() else None
        except ShardError as exc:
            print(f"check-skips: {exc}", file=sys.stderr)
            return 2
        return merge_shards(suite, args.merge, accepted, args.write_timings, families)
    result = unittest.TextTestRunner(verbosity=1).run(suite)

    skipped = {test.id(): message for test, message in result.skipped}
    matches, lines = report(skipped, accepted)
    print("\n".join(lines), file=sys.stderr)
    if not result.wasSuccessful():
        return 1
    if result.testsRun == 0:
        print("check-skips: no tests ran", file=sys.stderr)
        return 1
    return 0 if matches else 1


if __name__ == "__main__":
    sys.exit(main())
