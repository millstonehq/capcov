"""Laravel route reader: group prefixes composed, both handler styles, resources.

A large production backend can declare hundreds of routes across dozens of Route::group
prefixes. The generic tree-sitter query sees the literal path only, so 'GET /'
under /account and 'GET /' under /project collapse to one surface and the
inventory reads 323. This reader walks the nesting and composes the path a
runtime probe will actually see. It refuses to guess: a non-literal path or
prefix is an unresolved obligation, a duplicate composed route is reported under
excluded_surfaces, never silently dropped.
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

from capcov.adapters import load

_HAVE_TS = (
    importlib.util.find_spec("tree_sitter") is not None
    and importlib.util.find_spec("tree_sitter_language_pack") is not None
)

ROUTES = r"""<?php
use App\Http\Controllers\AccountController;
use Illuminate\Support\Facades\Route;

Route::group(['prefix' => 'api/v1', 'middleware' => ['auth']], function () {
    Route::get('/', [AccountController::class, 'index']);
    Route::group(['prefix' => 'account'], function () {
        Route::get('/', [AccountController::class, 'show']);
        Route::post('settings', ['uses' => 'AccountController@update']);
        Route::delete('tokens/{token_id}', 'AccountController@revoke');
    });
    Route::prefix('billing')->middleware('auth')->group(function () {
        Route::get('invoices', [AccountController::class, 'invoices']);
    });
    Route::resource('photos', PhotoController::class)->only(['index', 'store', 'destroy']);
    Route::apiResource('tags', TagController::class);
    Route::get($dynamic, [AccountController::class, 'dyn']);
    Route::group(['prefix' => $tenant], function () {
        Route::get('inside', [AccountController::class, 'inside']);
    });
    Route::get('/', [AccountController::class, 'index']);
});
\Route::get('health', fn () => 'ok');
"""


@unittest.skipUnless(_HAVE_TS, "treesitter extra not installed")
class LaravelRoutesTests(unittest.TestCase):
    def _core(self) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "routes").mkdir()
            (root / "routes" / "api.php").write_text(ROUTES)
            module = load("laravel-routes", plugin="capcov_contrib.laravel_routes:discover")
            return module.discover(root, root, config={"globs": ["routes/**/*.php"]})

    def test_group_prefixes_compose_into_the_path(self) -> None:
        ids = {s["id"] for s in self._core()["surfaces"]}
        self.assertIn("http:GET /api/v1", ids)
        self.assertIn("http:GET /api/v1/account", ids)
        self.assertIn("http:POST /api/v1/account/settings", ids)
        self.assertIn("http:DELETE /api/v1/account/tokens/{token_id}", ids)

    def test_fluent_prefix_group_composes_too(self) -> None:
        ids = {s["id"] for s in self._core()["surfaces"]}
        self.assertIn("http:GET /api/v1/billing/invoices", ids)

    def test_both_handler_styles_are_read(self) -> None:
        by_id = {s["id"]: s for s in self._core()["surfaces"]}
        self.assertEqual(by_id["http:GET /api/v1/account"]["handler_symbol"], "AccountController@show")
        self.assertEqual(by_id["http:POST /api/v1/account/settings"]["handler_symbol"], "AccountController@update")
        self.assertEqual(by_id["http:DELETE /api/v1/account/tokens/{token_id}"]["handler_symbol"], "AccountController@revoke")
        self.assertEqual(by_id["http:GET /health"]["handler_symbol"], "closure")

    def test_resource_expands_with_only_and_api_resource_omits_forms(self) -> None:
        ids = {s["id"] for s in self._core()["surfaces"]}
        self.assertIn("http:GET /api/v1/photos", ids)
        self.assertIn("http:POST /api/v1/photos", ids)
        self.assertIn("http:DELETE /api/v1/photos/{photo}", ids)
        self.assertNotIn("http:GET /api/v1/photos/create", ids)
        self.assertNotIn("http:GET /api/v1/photos/{photo}", ids)
        for verb, path in [("GET", "/api/v1/tags"), ("POST", "/api/v1/tags"), ("GET", "/api/v1/tags/{tag}"),
                           ("PUT", "/api/v1/tags/{tag}"), ("PATCH", "/api/v1/tags/{tag}"),
                           ("DELETE", "/api/v1/tags/{tag}")]:
            self.assertIn(f"http:{verb} {path}", ids)
        self.assertNotIn("http:GET /api/v1/tags/create", ids)
        self.assertNotIn("http:GET /api/v1/tags/{tag}/edit", ids)

    def test_dynamic_path_and_dynamic_prefix_are_unresolved_not_guessed(self) -> None:
        core = self._core()
        kinds = [(u["kind"], u["reason"]) for u in core["unresolved"]]
        self.assertTrue(any(k == "dynamic-route" for k, _ in kinds), kinds)
        self.assertTrue(any(k == "dynamic-prefix" for k, _ in kinds), kinds)
        ids = {s["id"] for s in core["surfaces"]}
        self.assertFalse(any(i.endswith("/inside") for i in ids), ids)

    def test_duplicate_composed_route_is_reported_not_dropped(self) -> None:
        core = self._core()
        dup = [s for s in core["excluded_surfaces"]["surfaces"] if s["path"] == "/api/v1" and s["method"] == "GET"]
        self.assertEqual(len(dup), 1)
        self.assertIn("duplicate", dup[0]["reason"])
        self.assertEqual(core["excluded_surfaces"]["count"], len(core["excluded_surfaces"]["surfaces"]))

    def test_leading_backslash_facade_is_read(self) -> None:
        ids = {s["id"] for s in self._core()["surfaces"]}
        self.assertIn("http:GET /health", ids)

    def test_no_route_files_is_an_unresolved_entry_not_an_empty_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = load("laravel-routes", plugin="capcov_contrib.laravel_routes:discover")
            core = module.discover(root, root, config={"globs": ["routes/**/*.php"]})
        self.assertEqual(core["surfaces"], [])
        self.assertTrue(any(u["kind"] == "no-surfaces" for u in core["unresolved"]))


def _core_from(files: dict[str, str], config: dict) -> dict:
    """Run the reader over an in-memory tree: {relative path: php source}."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for relative, source in files.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source)
        module = load("laravel-routes", plugin="capcov_contrib.laravel_routes:discover")
        return module.discover(root, root, config=config)


def _ids(core: dict) -> set[str]:
    return {s["id"] for s in core["surfaces"]}


def _kinds(core: dict) -> list[str]:
    return [u["kind"] for u in core["unresolved"]]


@unittest.skipUnless(_HAVE_TS, "treesitter extra not installed")
class LaravelMountTests(unittest.TestCase):
    """A service provider mounts route files from OUTSIDE them; the reader must be told."""

    FILES = {
        "app/Rest/V1/Routes/projects.php": "<?php\nRoute::get('projects', [ProjectController::class, 'v1']);\n",
        "app/Rest/V2/Routes/projects.php": "<?php\nRoute::get('projects', [ProjectController::class, 'v2']);\n",
        "routes/web.php": "<?php\nRoute::get('login', [LoginController::class, 'show']);\n",
    }
    MOUNTS = [
        {"glob": "app/Rest/V1/Routes/**/*.php", "prefix": "/api/v1"},
        {"glob": "app/Rest/V2/Routes/**/*.php", "prefix": "/api/v2"},
    ]

    def test_same_relative_path_under_two_mounts_is_two_surfaces_not_a_duplicate(self) -> None:
        core = _core_from(self.FILES, {"globs": ["app/**/Routes/**/*.php", "routes/*.php"], "mounts": self.MOUNTS})
        self.assertIn("http:GET /api/v1/projects", _ids(core))
        self.assertIn("http:GET /api/v2/projects", _ids(core))
        self.assertEqual(core["excluded_surfaces"]["count"], 0)

    def test_file_matching_no_mount_seeds_with_an_empty_prefix_and_is_named(self) -> None:
        core = _core_from(self.FILES, {"globs": ["app/**/Routes/**/*.php", "routes/*.php"], "mounts": self.MOUNTS})
        self.assertIn("http:GET /login", _ids(core))
        # mounts ARE configured here, so a routed file matching none of them is a
        # LIMIT of this config, not a silent "": it is named for the gate.
        unmounted = [u for u in core["unresolved"] if u["kind"] == "unmounted-file"]
        self.assertEqual([u["file"] for u in unmounted], ["routes/web.php"])
        self.assertEqual(unmounted[0]["id"], "laravel-routes:routes/web.php:unmounted-file")

    def test_without_mounts_the_empty_prefix_is_the_contract_and_stays_silent(self) -> None:
        core = _core_from(self.FILES, {"globs": ["app/**/Routes/**/*.php", "routes/*.php"]})
        self.assertIn("http:GET /login", _ids(core))
        self.assertEqual([u for u in core["unresolved"] if u["kind"] == "unmounted-file"], [])

    def test_mount_glob_matching_no_file_is_refused_not_silently_accepted(self) -> None:
        # One mis-cased entry leaves a whole directory unmounted while the run
        # still reports zero unresolved -- the bug mounts were added to fix.
        mounts = [{"glob": "app/Rest/v1/Routes/**/*.php", "prefix": "/api/v1"}]
        with self.assertRaises(ValueError) as caught:
            _core_from(self.FILES, {"globs": ["app/**/Routes/**/*.php"], "mounts": mounts})
        self.assertIn("app/Rest/v1/Routes/**/*.php", str(caught.exception))

    def test_first_matching_mount_wins(self) -> None:
        mounts = [{"glob": "app/**/*.php", "prefix": "/first"}] + self.MOUNTS
        core = _core_from(self.FILES, {"globs": ["app/**/Routes/**/*.php"], "mounts": mounts})
        self.assertIn("http:GET /first/projects", _ids(core))
        self.assertNotIn("http:GET /api/v1/projects", _ids(core))

    def test_mount_prefix_must_start_with_a_slash(self) -> None:
        with self.assertRaises(ValueError):
            _core_from(self.FILES, {"globs": ["routes/*.php"], "mounts": [{"glob": "routes/*.php", "prefix": "api"}]})


@unittest.skipUnless(_HAVE_TS, "treesitter extra not installed")
class LaravelUnresolvedIdTests(unittest.TestCase):
    def test_each_unresolved_entry_has_its_own_gate_id(self) -> None:
        source = "<?php\nRoute::get($a, 'C@a');\nRoute::get($b, 'C@b');\n"
        core = _core_from({"routes/api.php": source}, {"globs": ["routes/*.php"]})
        dynamic = [u for u in core["unresolved"] if u["kind"] == "dynamic-route"]
        self.assertEqual(len(dynamic), 2)
        self.assertEqual(len({u["id"] for u in dynamic}), 2)
        for u in dynamic:
            self.assertEqual(
                u["id"],
                f"laravel-routes:{u['file']}:{u['line']}:{u['column']}:dynamic-route",
            )

    def test_two_limits_on_one_line_still_get_distinct_gate_ids(self) -> None:
        source = "<?php\nRoute::get($a, 'C@a'); Route::get($b, 'C@b');\n"
        core = _core_from({"routes/api.php": source}, {"globs": ["routes/*.php"]})
        dynamic = [u for u in core["unresolved"] if u["kind"] == "dynamic-route"]
        self.assertEqual([u["line"] for u in dynamic], [2, 2])
        self.assertEqual(len({u["id"] for u in dynamic}), 2)

    def test_a_one_line_group_and_the_route_in_it_do_not_share_an_id(self) -> None:
        source = "<?php\nRoute::prefix($t)->group(function () { Route::get('x', 'C@x'); });\n"
        core = _core_from({"routes/api.php": source}, {"globs": ["routes/*.php"]})
        entries = [u for u in core["unresolved"] if u["kind"] == "dynamic-prefix"]
        self.assertEqual(len(entries), 2)
        self.assertEqual(len({u["id"] for u in entries}), 2)

    def test_no_surfaces_entry_stays_id_less(self) -> None:
        core = _core_from({}, {"globs": ["routes/*.php"]})
        entry = next(u for u in core["unresolved"] if u["kind"] == "no-surfaces")
        self.assertNotIn("id", entry)


@unittest.skipUnless(_HAVE_TS, "treesitter extra not installed")
class LaravelFluentAndReceiverTests(unittest.TestCase):
    def test_fluent_verb_call_composes_its_chain_prefix(self) -> None:
        source = "<?php\nRoute::middleware('auth')->prefix('admin')->get('users', [UserController::class, 'index']);\n"
        core = _core_from({"routes/api.php": source}, {"globs": ["routes/*.php"]})
        self.assertEqual(_ids(core), {"http:GET /admin/users"})

    def test_fluent_group_without_prefix_composes_nothing(self) -> None:
        source = "<?php\nRoute::name('n.')->group(function () {\n    Route::get('q', 'C@q');\n});\n"
        core = _core_from({"routes/api.php": source}, {"globs": ["routes/*.php"]})
        self.assertEqual(_ids(core), {"http:GET /q"})

    def test_last_fluent_prefix_wins_as_the_registrar_overwrites(self) -> None:
        source = "<?php\nRoute::prefix('a')->prefix('b')->group(function () {\n    Route::get('x', 'C@x');\n});\n"
        core = _core_from({"routes/api.php": source}, {"globs": ["routes/*.php"]})
        self.assertEqual(_ids(core), {"http:GET /b/x"})

    def test_non_facade_router_verb_is_unresolved_naming_the_receiver(self) -> None:
        source = "<?php\n$router->get('x', 'C@x');\n"
        core = _core_from({"routes/api.php": source}, {"globs": ["routes/*.php"]})
        self.assertEqual(_ids(core), set())
        entry = next(u for u in core["unresolved"] if u["kind"] == "dynamic-route")
        self.assertIn("$router", entry["reason"])

    def test_dynamic_fluent_prefix_on_a_verb_is_one_entry_not_two(self) -> None:
        # Per-site ids exist so the gate can exempt ONE obligation at a time; two
        # entries at one node carrying one id collapse that.
        source = "<?php\nRoute::get('ok', 'C@ok');\nRoute::prefix($tenant)->get('users', 'C@i');\n"
        core = _core_from({"routes/api.php": source}, {"globs": ["routes/*.php"]})
        self.assertEqual(_ids(core), {"http:GET /ok"})
        self.assertEqual(len(core["unresolved"]), 1, core["unresolved"])
        self.assertEqual(core["unresolved"][0]["kind"], "dynamic-prefix")

    def test_dynamic_fluent_prefix_on_a_group_is_still_named_at_the_group(self) -> None:
        source = "<?php\nRoute::prefix($tenant)->group(function () {\n    Route::get('x', 'C@x');\n});\n"
        core = _core_from({"routes/api.php": source}, {"globs": ["routes/*.php"]})
        self.assertEqual(_ids(core), set())
        lines = sorted(u["line"] for u in core["unresolved"] if u["kind"] == "dynamic-prefix")
        self.assertEqual(lines, [2, 3])  # the group call, and the route beneath it
        self.assertEqual(len({u["id"] for u in core["unresolved"] if "id" in u}), 2)

    def test_ordinary_php_get_on_a_collection_is_not_a_fabricated_limit(self) -> None:
        # `$config->get('a')` is not a route declaration. A manufactured gate
        # blocker is as dishonest as a silent drop.
        source = (
            "<?php\n$config = collect(['a' => 1]);\n$base = $config->get('a');\n"
            "Route::get('real', 'C@real');\n"
        )
        core = _core_from({"routes/api.php": source}, {"globs": ["routes/*.php"]})
        self.assertEqual(_ids(core), {"http:GET /real"})
        self.assertEqual(core["unresolved"], [])

    def test_route_shaped_call_on_a_router_variable_is_still_reported(self) -> None:
        source = "<?php\n$router->get('/x', 'C@i');\n"
        core = _core_from({"routes/api.php": source}, {"globs": ["routes/*.php"]})
        self.assertEqual(_ids(core), set())
        entry = next(u for u in core["unresolved"] if u["kind"] == "dynamic-route")
        self.assertIn("$router", entry["reason"])

    def test_non_facade_group_with_non_literal_attributes_is_named_not_walked(self) -> None:
        # The shape gate must not let a group whose prefix the reader never saw
        # leak the routes inside it at an unprefixed path.
        source = (
            "<?php\n$router->group($attrs, function () use ($router) {\n"
            "    Route::get('in', 'C@in');\n});\n"
        )
        core = _core_from({"routes/api.php": source}, {"globs": ["routes/*.php"]})
        self.assertEqual(_ids(core), set())
        entry = next(u for u in core["unresolved"] if u["kind"] == "dynamic-route")
        self.assertIn("$router", entry["reason"])

    def test_non_facade_verb_with_a_non_literal_path_is_still_named(self) -> None:
        # The facade twin `Route::get($path, ...)` is named; so is this.
        source = "<?php\n$router->get($path, 'C@i');\n"
        core = _core_from({"routes/api.php": source}, {"globs": ["routes/*.php"]})
        self.assertEqual(_ids(core), set())
        entry = next(u for u in core["unresolved"] if u["kind"] == "dynamic-route")
        self.assertIn("$router", entry["reason"])

    def test_cache_get_with_a_plain_default_is_not_a_fabricated_limit(self) -> None:
        # Two arguments, neither a literal path nor a handler: ordinary PHP.
        source = (
            "<?php\n$value = $cache->get($key, $fallback);\n"
            "Route::get('real', 'C@real');\n"
        )
        core = _core_from({"routes/api.php": source}, {"globs": ["routes/*.php"]})
        self.assertEqual(_ids(core), {"http:GET /real"})
        self.assertEqual(core["unresolved"], [])

    def test_non_facade_router_group_is_unresolved_and_its_body_is_not_guessed(self) -> None:
        source = (
            "<?php\n$router->group(['prefix' => 'pp'], function () use ($router) {\n"
            "    Route::get('in', 'C@in');\n});\n"
        )
        core = _core_from({"routes/api.php": source}, {"globs": ["routes/*.php"]})
        self.assertFalse(any(i.endswith("/in") for i in _ids(core)), _ids(core))
        self.assertIn("dynamic-route", _kinds(core))


@unittest.skipUnless(_HAVE_TS, "treesitter extra not installed")
class LaravelGroupAttributeTests(unittest.TestCase):
    def test_non_array_group_attributes_are_a_dynamic_prefix(self) -> None:
        source = "<?php\nRoute::group($attrs, function () {\n    Route::get('z', 'C@z');\n});\n"
        core = _core_from({"routes/api.php": source}, {"globs": ["routes/*.php"]})
        self.assertFalse(any(i.endswith("/z") for i in _ids(core)), _ids(core))
        self.assertIn("dynamic-prefix", _kinds(core))

    def test_group_whose_body_is_a_file_include_is_a_dynamic_prefix(self) -> None:
        source = "<?php\nRoute::group(['prefix' => 'g'], base_path('routes/x.php'));\n"
        core = _core_from({"routes/api.php": source}, {"globs": ["routes/*.php"]})
        self.assertEqual(_ids(core), set())
        self.assertIn("dynamic-prefix", _kinds(core))


@unittest.skipUnless(_HAVE_TS, "treesitter extra not installed")
class LaravelResourceTests(unittest.TestCase):
    def test_singular_strips_es_only_after_sibilants(self) -> None:
        source = (
            "<?php\nRoute::apiResource('cases', CaseController::class);\n"
            "Route::apiResource('boxes', BoxController::class);\n"
            "Route::apiResource('statuses', StatusController::class);\n"
        )
        ids = _ids(_core_from({"routes/api.php": source}, {"globs": ["routes/*.php"]}))
        self.assertIn("http:GET /cases/{case}", ids)
        self.assertIn("http:GET /boxes/{box}", ids)
        self.assertIn("http:GET /statuses/{status}", ids)

    def test_resource_wildcard_replaces_dashes_as_laravel_does(self) -> None:
        # ResourceRegistrar::getResourceWildcard ends in an unconditional
        # str_replace('-', '_'), so {work-order} is an id no probe can match.
        source = "<?php\nRoute::apiResource('work-orders', WorkOrderController::class);\n"
        ids = _ids(_core_from({"routes/api.php": source}, {"globs": ["routes/*.php"]}))
        self.assertIn("http:GET /work-orders/{work_order}", ids)
        self.assertNotIn("http:GET /work-orders/{work-order}", ids)

    def test_parameters_override_beats_the_singularisation_heuristic(self) -> None:
        source = (
            "<?php\nRoute::resource('work-orders', WorkOrderController::class)"
            "->parameters(['work-orders' => 'order']);\n"
        )
        ids = _ids(_core_from({"routes/api.php": source}, {"globs": ["routes/*.php"]}))
        self.assertIn("http:GET /work-orders/{order}", ids)
        self.assertNotIn("http:GET /work-orders/{work_order}", ids)

    def test_parameters_override_still_gets_laravels_dash_replacement(self) -> None:
        source = (
            "<?php\nRoute::apiResource('warehouses', WarehouseController::class)"
            "->parameters(['warehouses' => 'ware-house']);\n"
        )
        ids = _ids(_core_from({"routes/api.php": source}, {"globs": ["routes/*.php"]}))
        self.assertIn("http:GET /warehouses/{ware_house}", ids)

    def test_documented_singular_misfires_stay_visible(self) -> None:
        # English gives no structural rule separating `statuses` from `houses`,
        # so these two classes stay wrong and stay DOCUMENTED; Laravel's own
        # escape hatch, ->parameters([...]), is the fix.
        source = (
            "<?php\nRoute::apiResource('warehouses', WarehouseController::class);\n"
            "Route::apiResource('movies', MovieController::class);\n"
        )
        ids = _ids(_core_from({"routes/api.php": source}, {"globs": ["routes/*.php"]}))
        self.assertIn("http:GET /warehouses/{warehous}", ids)
        self.assertIn("http:GET /movies/{movy}", ids)

    def test_unreadable_parameters_map_is_named_not_silently_heuristic(self) -> None:
        source = "<?php\nRoute::apiResource('photos', PhotoController::class)->parameters($map);\n"
        core = _core_from({"routes/api.php": source}, {"globs": ["routes/*.php"]})
        self.assertIn("http:GET /photos/{photo}", _ids(core))
        entry = next(u for u in core["unresolved"] if u["kind"] == "dynamic-parameters")
        self.assertIn("parameters", entry["reason"])

    def test_nested_resource_name_is_unresolved_not_a_wrong_path(self) -> None:
        source = "<?php\nRoute::resource('photos.comments', CommentController::class);\n"
        core = _core_from({"routes/api.php": source}, {"globs": ["routes/*.php"]})
        self.assertFalse(any("photos.comments" in i or "/comments" in i for i in _ids(core)), _ids(core))
        self.assertIn("dynamic-route", _kinds(core))

    def test_only_accepts_the_variadic_string_form(self) -> None:
        source = "<?php\nRoute::resource('photos', PhotoController::class)->only('index', 'show');\n"
        ids = _ids(_core_from({"routes/api.php": source}, {"globs": ["routes/*.php"]}))
        self.assertEqual(ids, {"http:GET /photos", "http:GET /photos/{photo}"})

    def test_except_removes_actions(self) -> None:
        source = "<?php\nRoute::apiResource('photos', PhotoController::class)->except(['destroy', 'update']);\n"
        ids = _ids(_core_from({"routes/api.php": source}, {"globs": ["routes/*.php"]}))
        self.assertEqual(ids, {"http:GET /photos", "http:POST /photos", "http:GET /photos/{photo}"})


@unittest.skipUnless(_HAVE_TS, "treesitter extra not installed")
class LaravelHandlerTests(unittest.TestCase):
    def test_invokable_controller_reads_as_invoke(self) -> None:
        source = "<?php\nRoute::get('x', \\App\\Http\\Controllers\\PingController::class);\n"
        core = _core_from({"routes/api.php": source}, {"globs": ["routes/*.php"]})
        self.assertEqual(core["surfaces"][0]["handler_symbol"], "PingController@__invoke")

    def test_non_literal_handler_is_marked_unresolved_in_full(self) -> None:
        source = "<?php\nRoute::get('y', $handlers['y'] ?? $fallback);\n"
        core = _core_from({"routes/api.php": source}, {"globs": ["routes/*.php"]})
        self.assertEqual(core["surfaces"][0]["handler_symbol"], "unresolved:$handlers['y'] ?? $fallback")


@unittest.skipUnless(_HAVE_TS, "treesitter extra not installed")
class LaravelFanOutAndDuplicateTests(unittest.TestCase):
    def test_match_and_any_fan_out_per_verb(self) -> None:
        source = "<?php\nRoute::match(['get', 'post'], 'm', 'C@m');\nRoute::any('a', 'C@a');\n"
        ids = _ids(_core_from({"routes/api.php": source}, {"globs": ["routes/*.php"]}))
        self.assertEqual(ids, {"http:GET /m", "http:POST /m", "http:GET /a", "http:POST /a",
                               "http:PUT /a", "http:PATCH /a", "http:DELETE /a"})

    def test_any_omits_options_and_head_and_the_module_says_so(self) -> None:
        import capcov_contrib.laravel_routes as reader

        self.assertIn("OPTIONS", reader.__doc__)
        self.assertIn("HEAD", reader.__doc__)

    def test_duplicate_reason_says_the_runtime_serves_the_later_declaration(self) -> None:
        files = {
            "routes/a.php": "<?php\nRoute::get('dup', 'C@first');\n",
            "routes/b.php": "<?php\nRoute::get('dup', 'C@last');\n",
        }
        core = _core_from(files, {"globs": ["routes/*.php"]})
        reason = core["excluded_surfaces"]["surfaces"][0]["reason"]
        self.assertIn("overwrites", reason)
        self.assertIn("shadowed handler", reason)

    def test_cross_file_duplicate_names_the_first_file(self) -> None:
        files = {
            "routes/a.php": "<?php\nRoute::get('dup', 'C@a');\n",
            "routes/b.php": "<?php\nRoute::get('dup', 'C@b');\n",
        }
        core = _core_from(files, {"globs": ["routes/*.php"]})
        self.assertEqual(_ids(core), {"http:GET /dup"})
        dup = core["excluded_surfaces"]["surfaces"]
        self.assertEqual(len(dup), 1)
        self.assertEqual(dup[0]["file"], "routes/b.php")
        self.assertIn("routes/a.php:2", dup[0]["reason"])
