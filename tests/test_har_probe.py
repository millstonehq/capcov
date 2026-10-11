"""The `har` probe: runtime bindings harvested from an existing browser run.

Many targets have no Python test suite to hook, but already run
Playwright suites that can record a HAR. This probe projects those requests
onto the static surfaces so a route that a real session reached lands in
`both` and one nobody reached stays `static_only` -- the split that would have
caught a feature that was merged, deployed and never exercised.
"""

from __future__ import annotations

import json
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from capcov.probes import har_probe
from capcov.probes.har_probe import (
    candidates_indexed,
    index_surfaces,
    observe,
    project_har,
    template_regex,
)


def _match(method: str, path: str, surfaces: list[dict]) -> str | None:
    """Test-local one-shot matcher: the surface a request reached, or None.

    The probe indexes the inventory once and calls `candidates_indexed`
    directly; nothing in production wants a per-call index rebuild. This
    convenience lives here so a template test reads as one assertion.
    """
    candidates = candidates_indexed(method, path, index_surfaces(surfaces))
    return candidates[0] if len(candidates) == 1 else None


SURFACES = [
    {"id": "http:GET /api/jobs", "method": "GET", "path": "/api/jobs"},
    {"id": "http:GET /api/jobs/{id}", "method": "GET", "path": "/api/jobs/{id}"},
    {"id": "http:POST /api/jobs/:id/close", "method": "POST", "path": "/api/jobs/:id/close"},
    {"id": "http:DELETE /api/jobs/{id}", "method": "DELETE", "path": "/api/jobs/{id}"},
]


def _har(entries: list[tuple[str, str]]) -> dict:
    return {"log": {"entries": [
        {"request": {"method": m, "url": f"https://app.example.com{u}"},
         "response": {"status": 200}} for m, u in entries]}}


class TemplateTests(unittest.TestCase):
    def test_brace_and_colon_params_match_one_segment(self) -> None:
        self.assertTrue(template_regex("/api/jobs/{id}").fullmatch("/api/jobs/42"))
        self.assertTrue(template_regex("/api/jobs/:id/close").fullmatch("/api/jobs/42/close"))
        self.assertFalse(template_regex("/api/jobs/{id}").fullmatch("/api/jobs/42/close"))

    def test_literal_beats_template_when_both_match(self) -> None:
        surfaces = SURFACES + [{"id": "http:GET /api/jobs/new", "method": "GET", "path": "/api/jobs/new"}]
        self.assertEqual(_match("GET", "/api/jobs/new", surfaces), "http:GET /api/jobs/new")
        self.assertEqual(_match("GET", "/api/jobs/7", surfaces), "http:GET /api/jobs/{id}")

    def test_method_must_match(self) -> None:
        self.assertIsNone(_match("PUT", "/api/jobs/7", SURFACES))

    def test_trailing_slash_is_ignored_on_both_sides(self) -> None:
        self.assertEqual(_match("GET", "/api/jobs/", SURFACES), "http:GET /api/jobs")
        self.assertEqual(_match("GET", "/api/jobs/7/", SURFACES), "http:GET /api/jobs/{id}")
        surfaces = [{"id": "http:GET /api/jobs/", "method": "GET", "path": "/api/jobs/"}]
        self.assertEqual(_match("GET", "/api/jobs", surfaces), "http:GET /api/jobs/")

    def test_slashless_surface_path_matches_rooted_request(self) -> None:
        surfaces = [{"id": "http:GET admin/tows", "method": "GET", "path": "admin/tows"}]
        self.assertEqual(_match("GET", "/admin/tows", surfaces), "http:GET admin/tows")


class IndexTests(unittest.TestCase):
    def test_the_index_resolves_each_request_to_the_named_surface(self) -> None:
        # Literal expectations, not a function compared with itself: dropping a
        # template from the index has to make this fail.
        surfaces = SURFACES + [
            {"id": "http:GET /api/jobs/new", "method": "GET", "path": "/api/jobs/new"},
            {"id": "http:GET admin/tows", "method": "GET", "path": "admin/tows"},
        ]
        index = index_surfaces(surfaces)
        expected = {
            ("GET", "/api/jobs"): ["http:GET /api/jobs"],
            ("GET", "/api/jobs/new"): ["http:GET /api/jobs/new"],
            ("GET", "/api/jobs/7"): ["http:GET /api/jobs/{id}"],
            ("GET", "/api/jobs/7/"): ["http:GET /api/jobs/{id}"],
            ("POST", "/api/jobs/7/close"): ["http:POST /api/jobs/:id/close"],
            ("DELETE", "/api/jobs/7"): ["http:DELETE /api/jobs/{id}"],
            ("PUT", "/api/jobs/7"): [],
            ("GET", "/admin/tows"): ["http:GET admin/tows"],
            ("GET", "/nowhere"): [],
        }
        for (method, path), ids in expected.items():
            with self.subTest(method=method, path=path):
                self.assertEqual(candidates_indexed(method, path, index), ids)

    def test_a_dropped_template_is_visible_to_this_oracle(self) -> None:
        # The guard on the guard: the previous form of the test above compared
        # `match_indexed` with `match_surface`, which was the same function, so
        # an index that lost every template still reported zero disagreements.
        surfaces = SURFACES
        crippled = (index_surfaces(surfaces)[0], [])
        self.assertEqual(candidates_indexed("GET", "/api/jobs/7", crippled), [])
        self.assertEqual(
            candidates_indexed("GET", "/api/jobs/7", index_surfaces(surfaces)),
            ["http:GET /api/jobs/{id}"],
        )

    def test_index_normalises_missing_slash_and_splits_literals_from_templates(self) -> None:
        literals, templates = index_surfaces(
            [{"id": "a", "method": "get", "path": "admin/tows"},
             {"id": "b", "method": "GET", "path": "/admin/tows/{id}"}]
        )
        self.assertEqual(literals, {("GET", "/admin/tows"): ["a"]})
        self.assertEqual([(m, sid) for m, _, sid in templates], [("GET", "b")])

    def test_repeated_requests_hit_the_memo_not_the_index(self) -> None:
        calls = []
        real = har_probe.candidates_indexed

        def counting(method, path, index):
            calls.append((method, path))
            return real(method, path, index)

        har = _har([("GET", "/api/jobs/7"), ("GET", "/api/jobs/7?page=2"), ("GET", "/api/jobs/7")])
        with unittest.mock.patch.object(har_probe, "candidates_indexed", counting):
            result = project_har({"a.har": har}, SURFACES, strip_prefixes=[])
        self.assertEqual(calls, [("GET", "/api/jobs/7")])
        self.assertEqual(result["requests"], 3)
        self.assertEqual(result["bindings"][0]["surface"], "http:GET /api/jobs/{id}")


class ProjectTests(unittest.TestCase):
    def test_requests_become_bindings_with_crud_and_test_name(self) -> None:
        har = _har([("GET", "/api/jobs"), ("GET", "/api/jobs/7?x=1"), ("DELETE", "/api/jobs/7")])
        result = project_har({"smoke.har": har}, SURFACES, strip_prefixes=[])
        by_surface = {b["surface"]: b for b in result["bindings"]}
        self.assertEqual(by_surface["http:GET /api/jobs"]["operations"], ["read"])
        self.assertEqual(by_surface["http:GET /api/jobs"]["entity"], "http:GET /api/jobs")
        self.assertEqual(by_surface["http:GET /api/jobs"]["tests"], ["smoke.har"])
        self.assertEqual(by_surface["http:DELETE /api/jobs/{id}"]["operations"], ["delete"])
        self.assertEqual(result["unresolved"], [])

    def test_unmatched_requests_are_reported_not_dropped(self) -> None:
        har = _har([("GET", "/api/jobs"), ("GET", "/static/app.js"), ("GET", "/api/ghost")])
        result = project_har({"smoke.har": har}, SURFACES, strip_prefixes=[])
        self.assertEqual(len(result["bindings"]), 1)
        self.assertEqual(len(result["unresolved"]), 1)
        entry = result["unresolved"][0]
        self.assertEqual(entry["kind"], "unmatched-requests")
        self.assertFalse(entry["gating"])
        self.assertEqual(entry["count"], 2)
        self.assertIn("GET app.example.com/api/ghost", entry["samples"])

    def test_strip_prefixes_before_matching(self) -> None:
        har = _har([("GET", "/v2/api/jobs")])
        result = project_har({"a.har": har}, SURFACES, strip_prefixes=["/v2"])
        self.assertEqual([b["surface"] for b in result["bindings"]], ["http:GET /api/jobs"])

    def test_same_surface_from_two_runs_merges_tests(self) -> None:
        result = project_har(
            {"a.har": _har([("GET", "/api/jobs")]), "b.har": _har([("GET", "/api/jobs")])},
            SURFACES, strip_prefixes=[],
        )
        self.assertEqual(result["bindings"][0]["tests"], ["a.har", "b.har"])

    def test_hars_are_keyed_by_path_and_labelled_by_basename(self) -> None:
        result = project_har(
            {"ci/smoke.har": _har([("GET", "/api/jobs")]), "local/other.har": _har([("GET", "/api/jobs")])},
            SURFACES, strip_prefixes=[],
        )
        self.assertEqual(result["bindings"][0]["tests"], ["other.har", "smoke.har"])

    def test_two_hars_sharing_a_basename_are_refused_by_name(self) -> None:
        with self.assertRaises(ValueError) as caught:
            project_har(
                {"ci/smoke.har": _har([]), "local/smoke.har": _har([])}, SURFACES, strip_prefixes=[]
            )
        self.assertIn("ci/smoke.har", str(caught.exception))
        self.assertIn("local/smoke.har", str(caught.exception))

    def test_samples_carry_the_host_and_non_http_urls_are_counted(self) -> None:
        har = {"log": {"entries": [
            {"request": {"method": "GET", "url": "https://cdn.example.com/font.woff2"}},
            {"request": {"method": "GET", "url": "data:image/png;base64,AAAA"}},
            {"request": {"method": "GET", "url": "blob:https://app.example.com/1234"}},
            {"request": {"method": "GET", "url": "about:blank"}},
            {"request": {"method": "GET", "url": "/api/jobs"}},
        ]}}
        result = project_har({"a.har": har}, SURFACES, strip_prefixes=[])
        self.assertEqual([b["surface"] for b in result["bindings"]], ["http:GET /api/jobs"])
        self.assertEqual(result["requests"], 5)
        entry = result["unresolved"][0]
        self.assertEqual(entry["kind"], "unmatched-requests")
        self.assertEqual(entry["count"], 1)
        self.assertEqual(entry["samples"], ["GET cdn.example.com/font.woff2"])
        self.assertEqual(entry["non_http"], {"about": 1, "blob": 1, "data": 1})

    def test_ambiguous_template_hits_are_reported_not_bound(self) -> None:
        surfaces = [
            {"id": "http:GET /x/{a}", "method": "GET", "path": "/x/{a}"},
            {"id": "http:GET /{regionCode}/zones", "method": "GET", "path": "/{regionCode}/zones"},
        ]
        result = project_har({"a.har": _har([("GET", "/x/zones"), ("GET", "/x/1")])}, surfaces, strip_prefixes=[])
        self.assertEqual([b["surface"] for b in result["bindings"]], ["http:GET /x/{a}"])
        self.assertEqual(result["bindings"][0]["tests"], ["a.har"])
        entry = result["unresolved"][0]
        self.assertEqual(entry["kind"], "ambiguous-match")
        self.assertFalse(entry["gating"])
        self.assertEqual(entry["count"], 1)
        self.assertEqual(
            entry["samples"],
            ["GET app.example.com/x/zones -> [http:GET /x/{a}, http:GET /{regionCode}/zones]"],
        )

    def test_non_crud_verbs_bind_the_surface_and_claim_no_operation(self) -> None:
        # The browser probe's actual rule (browser_probe._crud_for_surface): bind
        # the surface the request reached, claim no operation for a verb outside
        # the CRUD map. Refusing to bind would leave a declared HEAD route in
        # `static_only` forever -- an "unexplained-static_only" no test can close,
        # because the only way to exercise a HEAD route is to send HEAD.
        surfaces = SURFACES + [
            {"id": "http:HEAD /api/jobs", "method": "HEAD", "path": "/api/jobs"},
            {"id": "http:OPTIONS /api/jobs", "method": "OPTIONS", "path": "/api/jobs"},
        ]
        har = _har([("HEAD", "/api/jobs"), ("OPTIONS", "/api/jobs"), ("PROPFIND", "/api/jobs"), ("GET", "/api/jobs")])
        result = project_har({"a.har": har}, surfaces, strip_prefixes=[])
        by_surface = {b["surface"]: b for b in result["bindings"]}
        self.assertEqual(
            sorted(by_surface),
            ["http:GET /api/jobs", "http:HEAD /api/jobs", "http:OPTIONS /api/jobs"],
        )
        self.assertEqual(by_surface["http:HEAD /api/jobs"]["operations"], [])
        self.assertEqual(by_surface["http:HEAD /api/jobs"]["entity"], "http:HEAD /api/jobs")
        self.assertEqual(by_surface["http:HEAD /api/jobs"]["tests"], ["a.har"])
        self.assertEqual(by_surface["http:OPTIONS /api/jobs"]["operations"], [])
        self.assertEqual(by_surface["http:GET /api/jobs"]["operations"], ["read"])
        kinds = {e["kind"]: e for e in result["unresolved"]}
        self.assertEqual(set(kinds), {"non-binding-verbs", "unmatched-requests"})
        self.assertFalse(kinds["non-binding-verbs"]["gating"])
        self.assertEqual(kinds["non-binding-verbs"]["count"], 2)
        self.assertEqual(
            kinds["non-binding-verbs"]["samples"],
            ["HEAD app.example.com/api/jobs", "OPTIONS app.example.com/api/jobs"],
        )
        # A non-CRUD verb that matched nothing is still unmatched, not a bound verb.
        self.assertEqual(
            kinds["unmatched-requests"]["samples"], ["PROPFIND app.example.com/api/jobs"]
        )

    def test_a_non_crud_verb_matching_no_surface_falls_through_to_unmatched(self) -> None:
        har = _har([("HEAD", "/static/app.js")])
        result = project_har({"a.har": har}, SURFACES, strip_prefixes=[])
        self.assertEqual(result["bindings"], [])
        kinds = {e["kind"]: e for e in result["unresolved"]}
        self.assertEqual(set(kinds), {"unmatched-requests"})
        self.assertEqual(kinds["unmatched-requests"]["samples"], ["HEAD app.example.com/static/app.js"])

    def test_colliding_literal_surfaces_bind_nothing_and_are_named(self) -> None:
        # `admin/tows` and `/admin/tows` normalise onto one key. Keeping the
        # first silently made the second unbindable forever.
        surfaces = [
            {"id": "http:GET admin/tows", "method": "GET", "path": "admin/tows"},
            {"id": "http:GET /admin/tows", "method": "GET", "path": "/admin/tows"},
        ]
        result = project_har({"a.har": _har([("GET", "/admin/tows")])}, surfaces, strip_prefixes=[])
        self.assertEqual(result["bindings"], [])
        kinds = {e["kind"]: e for e in result["unresolved"]}
        self.assertEqual(set(kinds), {"ambiguous-match", "colliding-surfaces"})
        self.assertEqual(
            kinds["ambiguous-match"]["samples"],
            ["GET app.example.com/admin/tows -> [http:GET admin/tows, http:GET /admin/tows]"],
        )
        self.assertFalse(kinds["colliding-surfaces"]["gating"])
        self.assertEqual(
            kinds["colliding-surfaces"]["samples"],
            ["GET /admin/tows -> [http:GET admin/tows, http:GET /admin/tows]"],
        )

    def test_trailing_slash_collision_is_named_not_dropped(self) -> None:
        surfaces = [
            {"id": "http:GET /jobs", "method": "GET", "path": "/jobs"},
            {"id": "http:GET /jobs/", "method": "GET", "path": "/jobs/"},
        ]
        result = project_har({"a.har": _har([("GET", "/jobs")])}, surfaces, strip_prefixes=[])
        self.assertEqual(result["bindings"], [])
        kinds = {e["kind"]: e for e in result["unresolved"]}
        self.assertEqual(set(kinds), {"ambiguous-match", "colliding-surfaces"})
        self.assertEqual(kinds["colliding-surfaces"]["count"], 1)
        self.assertEqual(
            kinds["colliding-surfaces"]["samples"],
            ["GET /jobs -> [http:GET /jobs, http:GET /jobs/]"],
        )

    def test_a_collision_no_request_reached_is_still_named(self) -> None:
        surfaces = [
            {"id": "http:GET /jobs", "method": "GET", "path": "/jobs"},
            {"id": "http:GET /jobs/", "method": "GET", "path": "/jobs/"},
        ]
        result = project_har({"a.har": _har([])}, surfaces, strip_prefixes=[])
        self.assertEqual([e["kind"] for e in result["unresolved"]], ["colliding-surfaces"])

    def test_entries_without_url_or_method_never_reach_the_root_surface(self) -> None:
        surfaces = SURFACES + [{"id": "http:GET /", "method": "GET", "path": "/"}]
        har = {"log": {"entries": [
            {"request": {"method": "GET"}},
            {"request": {"url": "https://app.example.com/api/jobs"}},
            {"response": {"status": 200}},
        ]}}
        result = project_har({"a.har": har}, surfaces, strip_prefixes=[])
        self.assertEqual(result["bindings"], [])
        self.assertEqual(result["requests"], 3)
        entry = result["unresolved"][0]
        self.assertEqual(entry["kind"], "malformed-entries")
        self.assertFalse(entry["gating"])
        self.assertEqual(entry["count"], 3)


class ObserveTests(unittest.TestCase):
    def test_observe_writes_a_valid_observed_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "src" / "app.php").write_text("<?php\n")
            (root / "capcov.capabilities.json").write_text(json.dumps({"surfaces": SURFACES}))
            (root / "run.har").write_text(json.dumps(_har([("GET", "/api/jobs")])))
            observed = observe(source_root=root / "src", target=root, out=root / "observed.json",
                               nonce="n1", har_paths=[root / "run.har"])
            written = json.loads((root / "observed.json").read_text())
        self.assertEqual(written["kind"], "observed")
        self.assertEqual(written["bindings"][0]["surface"], "http:GET /api/jobs")
        self.assertEqual(written["exercises"], 1)
        self.assertEqual(observed["bindings"], written["bindings"])

    def test_observe_reads_har_surfaces_and_strip_prefixes_from_capcov_toml(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "capcov.toml").write_text(
                '[capcov]\nhar_surfaces = "build/inventory.json"\nhar_strip_prefixes = ["/v2"]\n'
            )
            (root / "build").mkdir()
            (root / "build" / "inventory.json").write_text(json.dumps({"surfaces": SURFACES}))
            (root / "run.har").write_text(json.dumps(_har([("GET", "/v2/api/jobs/9")])))
            observed = observe(source_root=root / "src", target=root, out=root / "observed.json",
                               nonce="n1", har_paths=[root / "run.har"])
        self.assertEqual([b["surface"] for b in observed["bindings"]], ["http:GET /api/jobs/{id}"])
        self.assertEqual(observed["unresolved"], [])

    def test_observe_refuses_a_missing_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "run.har").write_text(json.dumps(_har([])))
            with self.assertRaises(ValueError) as caught:
                observe(source_root=root / "src", target=root, out=root / "observed.json",
                        nonce="n1", har_paths=[root / "run.har"])
            self.assertFalse((root / "observed.json").exists())
        self.assertIn("capcov.capabilities.json", str(caught.exception))

    def test_observe_refuses_the_same_har_path_named_twice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "capcov.capabilities.json").write_text(json.dumps({"surfaces": SURFACES}))
            (root / "run.har").write_text(json.dumps(_har([("GET", "/api/jobs")])))
            with self.assertRaises(ValueError) as caught:
                observe(source_root=root / "src", target=root, out=root / "observed.json",
                        nonce="n1", har_paths=[root / "run.har", root / "run.har"])
            self.assertFalse((root / "observed.json").exists())
        self.assertIn("run.har", str(caught.exception))

    def test_observe_refuses_two_hars_with_one_basename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "capcov.capabilities.json").write_text(json.dumps({"surfaces": SURFACES}))
            for sub in ("ci", "local"):
                (root / sub).mkdir()
                (root / sub / "smoke.har").write_text(json.dumps(_har([("GET", "/api/jobs")])))
            with self.assertRaises(ValueError) as caught:
                observe(source_root=root / "src", target=root, out=root / "observed.json",
                        nonce="n1", har_paths=[root / "ci" / "smoke.har", root / "local" / "smoke.har"])
        self.assertIn("smoke.har", str(caught.exception))
