import json
import tempfile
import unittest
from pathlib import Path

from capcov import artifacts
from capcov.probes import python_probe


class StatementReadingTest(unittest.TestCase):
    def setUp(self) -> None:
        python_probe._BINDINGS.clear()
        python_probe.set_exercise("tests/test_x.py::test_y")
        self.addCleanup(python_probe.set_exercise, None)

    def rows(self) -> set[tuple[str, str, str]]:
        return {(s, e, o) for (s, e, o) in python_probe._BINDINGS}

    def test_the_sql_verb_is_the_operation(self) -> None:
        python_probe._record("INSERT INTO jobs (id) VALUES (?)")
        python_probe._record("SELECT jobs.id FROM jobs WHERE jobs.id = ?")
        python_probe._record("UPDATE jobs SET status=? WHERE id=?")
        python_probe._record("DELETE FROM jobs WHERE id=?")
        self.assertEqual(
            {op for _, _, op in self.rows()}, {"create", "read", "update", "delete"}
        )

    def test_a_join_binds_both_tables(self) -> None:
        python_probe._record(
            'SELECT * FROM jobs JOIN job_photos ON job_photos.job_id = jobs.id'
        )
        self.assertEqual({e for _, e, _ in self.rows()}, {"jobs", "job_photos"})

    def test_a_system_catalog_is_not_an_entity(self) -> None:
        """A test that introspects the schema is exercising the database, not a
        capability -- and no amount of test-writing could ever move that row."""
        python_probe._record("SELECT relname FROM pg_catalog.pg_class")
        python_probe._record("SELECT name FROM sqlite_master WHERE type='table'")
        self.assertEqual(self.rows(), set())

    def test_no_parameter_ever_reaches_the_output(self) -> None:
        python_probe._record("SELECT * FROM jobs WHERE email = 'someone@example.com'")
        blob = json.dumps(sorted(map(list, self.rows())))
        self.assertNotIn("someone@example.com", blob)

    def test_without_a_surface_the_exercise_carries_the_attribution(self) -> None:
        python_probe._record("SELECT * FROM jobs")
        self.assertEqual(
            self.rows(), {("test:tests/test_x.py::test_y", "jobs", "read")}
        )

    def test_inside_a_named_surface_the_surface_carries_it(self) -> None:
        with python_probe.surface("worker:process_one"):
            python_probe._record("UPDATE delivery_attempts SET n=1")
        self.assertEqual(
            self.rows(), {("worker:process_one", "delivery_attempts", "update")}
        )

    def test_a_resolved_route_template_replaces_the_pending_label(self) -> None:
        """The ordering wrinkle: the matched route is only known on the way out."""
        token = python_probe.SURFACE.set("<in-request:7>")
        python_probe._record("SELECT * FROM jobs")
        python_probe._relabel("<in-request:7>", "GET /jobs/{job_id}")
        python_probe.SURFACE.reset(token)
        self.assertEqual(self.rows(), {("GET /jobs/{job_id}", "jobs", "read")})


class ArtifactTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)

    def test_a_rename_changes_the_tree_hash(self) -> None:
        (self.root / "a.py").write_text("x = 1\n")
        before, count = artifacts.tree_sha256(self.root)
        self.assertEqual(count, 1)
        (self.root / "a.py").rename(self.root / "b.py")
        after, _ = artifacts.tree_sha256(self.root)
        self.assertNotEqual(before, after, "a file moving moves its surfaces")

    def test_check_compares_content_not_provenance(self) -> None:
        """A reformatted line moves the tree hash and changes no capability."""
        a = {"derived_from": {"extracted_at": "2026-01-01", "artifact_sha256": "x"},
             "capabilities": [1]}
        b = {"derived_from": {"extracted_at": "2026-09-06", "artifact_sha256": "y"},
             "capabilities": [1]}
        self.assertEqual(artifacts.normalise(a), artifacts.normalise(b))

    def test_check_still_fires_when_a_capability_appears(self) -> None:
        a = {"derived_from": {"artifact_sha256": "x"}, "capabilities": [1]}
        b = {"derived_from": {"artifact_sha256": "x"}, "capabilities": [1, 2]}
        self.assertNotEqual(artifacts.normalise(a), artifacts.normalise(b))

    def test_two_artifacts_from_different_trees_are_refused(self) -> None:
        ok, _ = artifacts.same_artifact(
            {"derived_from": {"artifact_sha256": "a"}},
            {"derived_from": {"artifact_sha256": "b"}},
        )
        self.assertFalse(ok)

    def test_an_artifact_with_no_hash_is_refused_rather_than_assumed_to_match(self) -> None:
        ok, why = artifacts.same_artifact({"derived_from": {}}, {"derived_from": {}})
        self.assertFalse(ok)
        self.assertIn("<none>", why)


if __name__ == "__main__":
    unittest.main()
