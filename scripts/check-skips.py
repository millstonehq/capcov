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

Allowlist format (JSON):

    {"version": 1,
     "accepted": [{"reason": "why these may skip", "tests": ["<test id>", ...]}]}
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
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


def run_shard(suite: unittest.TestSuite, index: int, count: int, timings: dict[str, float],
              result_path: Path, family: str = "", families: Families | None = None) -> int:
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
    result = unittest.TextTestRunner(verbosity=1, resultclass=TimedResult).run(
        unittest.TestSuite(selected))
    document = {
        "version": 1,
        "family": family,
        "index": index,
        "count": count,
        "discovered": discovered,
        "ran": sorted(result.seconds),
        "skipped": {test.id(): message for test, message in result.skipped},
        "seconds": result.seconds,
        "wall_seconds": time.perf_counter() - started,
        "successful": result.wasSuccessful(),
    }
    result_path.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if result.wasSuccessful() else 1


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
    parser.add_argument("modules", nargs="*", help="test modules; default: discover tests/")
    args = parser.parse_args(argv)
    if (args.shard or args.merge) and args.modules:
        parser.error("--shard and --merge work on the discovered suite; give no MODULE")
    if bool(args.shard) != bool(args.result) or (args.shard and args.merge):
        parser.error("--shard needs --result, and cannot be combined with --merge")
    if args.family and not args.shard:
        parser.error("--family needs --shard; use --shard 1/1 for the whole family")

    try:
        # A shard does not judge its skips, so it reads no allowlist.
        accepted = {} if args.shard else load_allowlist(args.allowlist)
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
    if args.shard:
        try:
            index, count = parse_shard(args.shard)
            families = load_families(args.families) if args.family else None
            return run_shard(suite, index, count, load_timings(args.timings), args.result,
                             args.family or "", families)
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
