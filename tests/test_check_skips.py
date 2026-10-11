"""scripts/check-skips.py: the skip set of a run is compared with the allowlist."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-skips.py"

_spec = importlib.util.spec_from_file_location("check_skips", SCRIPT)
check_skips = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_skips)


class CompareTest(unittest.TestCase):
    def test_a_skip_outside_the_allowlist_is_reported(self) -> None:
        matches, lines = check_skips.report({"t.A.test_x": "no tool"}, {})
        self.assertFalse(matches)
        self.assertIn("UNEXPECTED skip: t.A.test_x", "\n".join(lines))
        self.assertIn("no tool", "\n".join(lines))

    def test_an_accepted_test_that_did_not_skip_is_reported(self) -> None:
        matches, lines = check_skips.report({}, {"t.A.test_x": "external fixture"})
        self.assertFalse(matches)
        self.assertIn("STALE allowlist entry", "\n".join(lines))

    def test_the_same_set_matches_and_prints_each_reason(self) -> None:
        matches, lines = check_skips.report({"t.A.test_x": "no fixture"}, {"t.A.test_x": "external fixture"})
        self.assertTrue(matches)
        self.assertEqual(lines[0], "skipped: 1; accepted by the allowlist: 1")
        self.assertIn("    reason: external fixture", lines)

    def test_same_count_but_different_tests_does_not_match(self) -> None:
        unexpected, stale = check_skips.compare({"t.A.test_x": "m"}, {"t.A.test_y": "r"})
        self.assertEqual((unexpected, stale), (["t.A.test_x"], ["t.A.test_y"]))


class AllowlistTest(unittest.TestCase):
    def _load(self, document: object) -> dict[str, str]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "allow.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            return check_skips.load_allowlist(path)

    def test_every_entry_needs_a_reason(self) -> None:
        for reason in (None, "", "  "):
            with self.subTest(reason=reason), self.assertRaises(check_skips.AllowlistError):
                self._load({"version": 1, "accepted": [{"reason": reason, "tests": ["t.A.test_x"]}]})

    def test_a_test_listed_twice_is_refused(self) -> None:
        with self.assertRaises(check_skips.AllowlistError):
            self._load({"version": 1, "accepted": [{"reason": "r", "tests": ["t.A.test_x", "t.A.test_x"]}]})

    def test_the_committed_allowlists_load(self) -> None:
        for name in ("skips-allowed.json", "skips-allowed-plain-python.json"):
            with self.subTest(name=name):
                accepted = check_skips.load_allowlist(ROOT / "tests" / name)
                self.assertTrue(accepted)


class RunTest(unittest.TestCase):
    """The script end to end over a two-test module: one passes, one skips."""

    def _run(self, accepted: list[dict]) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sample_mod.py").write_text(textwrap.dedent("""
                import unittest

                class Sample(unittest.TestCase):
                    def test_runs(self):
                        pass

                    @unittest.skip("tool is absent")
                    def test_skips(self):
                        pass
            """), encoding="utf-8")
            allow = root / "allow.json"
            allow.write_text(json.dumps({"version": 1, "accepted": accepted}), encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(SCRIPT), "--allowlist", str(allow), "sample_mod"],
                cwd=root, capture_output=True, text=True, timeout=120)

    def test_an_unlisted_skip_fails_the_run(self) -> None:
        done = self._run([])
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("UNEXPECTED skip: sample_mod.Sample.test_skips", done.stderr)

    def test_a_listed_skip_passes_and_is_printed(self) -> None:
        done = self._run([{"reason": "no such tool exists", "tests": ["sample_mod.Sample.test_skips"]}])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("skipped: 1; accepted by the allowlist: 1", done.stderr)

    def test_a_listed_test_that_ran_fails_the_run(self) -> None:
        done = self._run([{"reason": "r", "tests": ["sample_mod.Sample.test_skips", "sample_mod.Sample.test_runs"]}])
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("STALE allowlist entry", done.stderr)


class AssignmentTest(unittest.TestCase):
    IDS = [f"tests.m{m}.C{c}.test_{t}" for m in range(4) for c in range(3) for t in range(5)]

    def test_every_test_gets_one_shard_and_a_class_stays_whole(self) -> None:
        shard_of = check_skips.assign_shards(self.IDS, 4, {})
        self.assertEqual(sorted(shard_of), sorted(self.IDS))
        self.assertEqual(set(shard_of.values()), {1, 2, 3, 4})
        for test_id, shard in shard_of.items():
            self.assertEqual(shard, shard_of[check_skips.class_of(test_id) + ".test_0"])

    def test_the_assignment_ignores_the_order_of_the_ids(self) -> None:
        timings = {"tests.m0.C0": 90.0, "tests.m1.C1": 40.0}
        self.assertEqual(check_skips.assign_shards(self.IDS, 3, timings),
                         check_skips.assign_shards(list(reversed(self.IDS)), 3, timings))

    def test_the_heaviest_class_gets_a_shard_to_itself_when_it_outweighs_the_rest(self) -> None:
        timings = {check_skips.class_of(test_id): 1.0 for test_id in self.IDS}
        timings["tests.m0.C0"] = 100.0
        shard_of = check_skips.assign_shards(self.IDS, 2, timings)
        heavy = shard_of["tests.m0.C0.test_0"]
        self.assertEqual({check_skips.class_of(t) for t, s in shard_of.items() if s == heavy},
                         {"tests.m0.C0"})

    def test_a_test_the_timings_name_is_assigned_apart_from_its_class(self) -> None:
        timings = {"tests.m0.C0.test_0": 50.0, "tests.m0.C0.test_1": 50.0, "tests.m1.C1": 1.0}
        shard_of = check_skips.assign_shards(self.IDS, 2, timings)
        self.assertNotEqual(shard_of["tests.m0.C0.test_0"], shard_of["tests.m0.C0.test_1"])
        # the tests of that class the timings do not name stay together
        self.assertEqual(len({shard_of[f"tests.m0.C0.test_{t}"] for t in (2, 3, 4)}), 1)

    def test_a_class_above_the_split_share_is_recorded_test_by_test(self) -> None:
        seconds = {test_id: 1.0 for test_id in self.IDS}
        seconds["tests.m0.C0.test_0"] = 40.0
        document = check_skips.timings_of(seconds)
        self.assertEqual(document["tests.m0.C0.test_0"], 40.0)
        self.assertEqual(document["tests.m0.C0.test_1"], 1.0)
        self.assertNotIn("tests.m0.C0", document)
        self.assertEqual(document["tests.m1.C1"], 5.0)
        shard_of = check_skips.assign_shards(self.IDS, 4, document)
        self.assertEqual(sorted(shard_of), sorted(self.IDS))

    def test_a_malformed_shard_argument_is_refused(self) -> None:
        for text in ("0/4", "5/4", "1", "a/b"):
            with self.subTest(text=text), self.assertRaises(check_skips.ShardError):
                check_skips.parse_shard(text)


class PartitionTest(unittest.TestCase):
    DISCOVERED = ["t.A.test_1", "t.A.test_2", "t.B.test_1"]

    def test_each_family_must_have_all_of_its_shards(self) -> None:
        shards = [self._shard(1, ["t.A.test_1", "t.A.test_2"], family="base", count=1),
                  self._shard(2, ["t.B.test_1"], family="tool", count=2)]
        problems = "\n".join(check_skips.check_partition(self.DISCOVERED, shards))
        self.assertIn("family tool: expected shards 1..2, got [2]", problems)
        self.assertNotIn("family base", problems)

    def _shard(self, index: int, ran: list[str], **changes: object) -> dict:
        return {"index": index, "count": 2, "discovered": list(self.DISCOVERED), "ran": ran, **changes}

    def test_an_exact_partition_has_no_problem(self) -> None:
        shards = [self._shard(1, ["t.A.test_1", "t.A.test_2"]), self._shard(2, ["t.B.test_1"])]
        self.assertEqual(check_skips.check_partition(self.DISCOVERED, shards), [])

    def test_a_test_no_shard_ran_is_reported(self) -> None:
        shards = [self._shard(1, ["t.A.test_1"]), self._shard(2, ["t.B.test_1"])]
        self.assertIn("ran in no shard", "\n".join(check_skips.check_partition(self.DISCOVERED, shards)))

    def test_a_test_two_shards_ran_is_reported(self) -> None:
        shards = [self._shard(1, ["t.A.test_1", "t.A.test_2"]), self._shard(2, ["t.A.test_2", "t.B.test_1"])]
        self.assertIn("more than one shard", "\n".join(check_skips.check_partition(self.DISCOVERED, shards)))

    def test_a_missing_shard_is_reported(self) -> None:
        shards = [self._shard(1, ["t.A.test_1", "t.A.test_2"])]
        problems = "\n".join(check_skips.check_partition(self.DISCOVERED, shards))
        self.assertIn("expected shards 1..2, got [1]", problems)
        self.assertIn("ran in no shard", problems)

    def test_a_shard_that_discovered_another_suite_is_reported(self) -> None:
        shards = [self._shard(1, ["t.A.test_1", "t.A.test_2"]),
                  self._shard(2, ["t.B.test_1"], discovered=["t.B.test_1"])]
        self.assertIn("discovered a different suite",
                      "\n".join(check_skips.check_partition(self.DISCOVERED, shards)))

    def test_a_test_discovery_does_not_yield_is_reported(self) -> None:
        shards = [self._shard(1, ["t.A.test_1", "t.A.test_2"]), self._shard(2, ["t.B.test_1", "t.C.test_9"])]
        self.assertIn("discovery does not yield",
                      "\n".join(check_skips.check_partition(self.DISCOVERED, shards)))


class ShardedRunTest(unittest.TestCase):
    """The script end to end over a small tests/ package, in two shards and a merge."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        tests = self.root / "tests"
        tests.mkdir()
        (tests / "__init__.py").write_text("", encoding="utf-8")
        (tests / "test_one.py").write_text(textwrap.dedent("""
            import unittest

            class First(unittest.TestCase):
                def test_runs(self):
                    pass

                @unittest.skip("tool is absent")
                def test_skips(self):
                    pass
        """), encoding="utf-8")
        (tests / "test_two.py").write_text(textwrap.dedent("""
            import unittest

            class Second(unittest.TestCase):
                def test_runs(self):
                    pass
        """), encoding="utf-8")

    def _script(self, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SCRIPT), *arguments],
                              cwd=self.root, capture_output=True, text=True, timeout=120)

    def _shards(self, count: int = 2) -> list[str]:
        names = []
        for index in range(1, count + 1):
            done = self._script("--shard", f"{index}/{count}", "--result", f"shard-{index}.json")
            self.assertEqual(done.returncode, 0, done.stderr)
            names.append(f"shard-{index}.json")
        return names

    def _allow(self, tests: list[str]) -> str:
        accepted = [{"reason": "no such tool exists", "tests": tests}] if tests else []
        (self.root / "allow.json").write_text(
            json.dumps({"version": 1, "accepted": accepted}), encoding="utf-8")
        return "allow.json"

    def test_a_shard_does_not_judge_its_skips(self) -> None:
        self._allow([])
        self._shards()

    def test_the_shards_run_each_test_once_and_the_merge_accepts_the_listed_skip(self) -> None:
        names = self._shards()
        ran = [test for name in names for test in json.loads((self.root / name).read_text())["ran"]]
        self.assertEqual(sorted(ran), ["tests.test_one.First.test_runs", "tests.test_one.First.test_skips",
                                       "tests.test_two.Second.test_runs"])
        done = self._script("--allowlist", self._allow(["tests.test_one.First.test_skips"]), "--merge", *names)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("discovered: 3; ran across 2 shards: 3", done.stderr)
        self.assertIn("skipped: 1; accepted by the allowlist: 1", done.stderr)

    def test_the_merge_fails_on_an_unlisted_skip(self) -> None:
        done = self._script("--allowlist", self._allow([]), "--merge", *self._shards())
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("UNEXPECTED skip: tests.test_one.First.test_skips", done.stderr)

    def test_the_merge_fails_on_a_listed_test_that_ran(self) -> None:
        allow = self._allow(["tests.test_one.First.test_skips", "tests.test_two.Second.test_runs"])
        done = self._script("--allowlist", allow, "--merge", *self._shards())
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("STALE allowlist entry", done.stderr)

    def test_the_merge_fails_when_a_shard_is_left_out(self) -> None:
        allow = self._allow(["tests.test_one.First.test_skips"])
        for name in self._shards():
            with self.subTest(only=name):
                done = self._script("--allowlist", allow, "--merge", name)
                self.assertEqual(done.returncode, 1, done.stderr)
                self.assertIn("expected shards 1..2", done.stderr)

    def test_the_merge_fails_when_a_test_was_added_after_the_shards_ran(self) -> None:
        names = self._shards()
        (self.root / "tests" / "test_three.py").write_text(textwrap.dedent("""
            import unittest

            class Third(unittest.TestCase):
                def test_runs(self):
                    pass
        """), encoding="utf-8")
        done = self._script("--allowlist", self._allow(["tests.test_one.First.test_skips"]), "--merge", *names)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("ran in no shard", done.stderr)

    def _families(self, classes: list[str]) -> str:
        (self.root / "families.json").write_text(json.dumps({
            "version": 1, "default": "base",
            "families": {"tool": {"needs": "a tool", "classes": classes}}}), encoding="utf-8")
        return "families.json"

    def _family_shard(self, family: str, families: str) -> str:
        name = f"{family}.json"
        done = self._script("--families", families, "--family", family, "--shard", "1/1", "--result", name)
        self.assertEqual(done.returncode, 0, done.stderr)
        return name

    def test_family_shards_partition_the_suite_and_an_unlisted_class_runs_in_the_default(self) -> None:
        families = self._families(["tests.test_one.First"])
        names = [self._family_shard(family, families) for family in ("base", "tool")]
        ran = {name: json.loads((self.root / name).read_text())["ran"] for name in names}
        self.assertEqual(ran["tool.json"], ["tests.test_one.First.test_runs", "tests.test_one.First.test_skips"])
        self.assertEqual(ran["base.json"], ["tests.test_two.Second.test_runs"])
        done = self._script("--allowlist", self._allow(["tests.test_one.First.test_skips"]),
                            "--families", families, "--merge", *names)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("discovered: 3; ran across 2 shards: 3", done.stderr)

    def test_the_merge_fails_when_a_family_is_left_out(self) -> None:
        families = self._families(["tests.test_one.First"])
        done = self._script("--allowlist", self._allow([]), "--families", families,
                            "--merge", self._family_shard("base", families))
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("ran in no shard", done.stderr)

    def test_the_merge_fails_when_the_families_file_names_a_class_that_is_gone(self) -> None:
        families = self._families(["tests.test_one.First", "tests.test_gone.Gone"])
        names = [self._family_shard(family, families) for family in ("base", "tool")]
        done = self._script("--allowlist", self._allow(["tests.test_one.First.test_skips"]),
                            "--families", families, "--merge", *names)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("no longer exist", done.stderr)

    def test_an_unknown_family_is_refused(self) -> None:
        done = self._script("--families", self._families([]), "--family", "nope",
                            "--shard", "1/1", "--result", "x.json")
        self.assertEqual(done.returncode, 2, done.stderr)

    def test_the_committed_families_file_loads_and_names_classes_that_exist(self) -> None:
        families = check_skips.load_families(ROOT / "tests" / "tool-families.json")
        self.assertEqual(families.names(), ["base", "indexers", "shen", "souffle"])
        for class_id in families.family_of_class:
            module, _, class_name = class_id.rpartition(".")
            source = (ROOT / (module.replace(".", "/") + ".py")).read_text(encoding="utf-8")
            self.assertIn(f"class {class_name}(", source, class_id)

    def test_a_failing_test_fails_its_shard_and_the_merge(self) -> None:
        (self.root / "tests" / "test_two.py").write_text(textwrap.dedent("""
            import unittest

            class Second(unittest.TestCase):
                def test_runs(self):
                    self.fail("broken")
        """), encoding="utf-8")
        codes = []
        for index in (1, 2):
            codes.append(self._script("--shard", f"{index}/2", "--result", f"shard-{index}.json").returncode)
        self.assertEqual(sorted(codes), [0, 1])
        done = self._script("--allowlist", self._allow(["tests.test_one.First.test_skips"]),
                            "--merge", "shard-1.json", "shard-2.json")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("had failing tests", done.stderr)

    def test_the_committed_timings_load(self) -> None:
        self.assertTrue(check_skips.load_timings(ROOT / "tests" / "shard-timings.json"))


if __name__ == "__main__":
    unittest.main()
