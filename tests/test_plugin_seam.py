"""The plugin-adapter seam: bespoke readers at the edge, in BOTH dispatch paths.

capcov's two generic adapters (tree-sitter for code, structured-spec for
contracts) are two generic mechanisms, not the only two allowed. A clean-room
rebuild out of a legacy/low-code source (Deluge/Apex/COBOL/a Retool export) has no
grammar and no structured form, so it brings its own reader as
``plugin = "module:callable"``. These tests prove the seam is real in both paths,
that a plugin's honest-denominator carriers survive, and that an unknown adapter
with no plugin fails LOUDLY and NAMES the seam (never a silent drop).
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

# Make the fixture bespoke reader importable by its dotted path, exactly as a
# consumer repo's own package would be on the runtime's sys.path.
_FIXTURES = Path(__file__).resolve().parent / "fixtures"
if str(_FIXTURES) not in sys.path:
    sys.path.insert(0, str(_FIXTURES))

from capcov import adapters  # noqa: E402
from capcov.flows.discovery import discover  # noqa: E402

_FLOWS_PLUGIN = "capcov_plugin_demo:discover_flows"
_CORE_PLUGIN = "capcov_plugin_demo:discover_core"
# The exact carrier strings the fixture emits; asserting on them proves the
# honest-denominator content flows through UNCHANGED, not merely that a list is
# non-empty.
_EXCLUDED_REASON = "Deluge validation trigger is not a runtime-reachable surface"
_UNRESOLVED_REASON = (
    "Deluge invokeurl connections to external services are not statically resolvable"
)


class FlowsPathSeamTests(unittest.TestCase):
    """PRIMARY path: flows.discovery.discover(config) loads and merges a plugin."""

    def _run(self, adapter_entry: dict) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "discovery.json"
            config.write_text(
                json.dumps(
                    {"scope": "demo", "root": ".", "adapters": [adapter_entry]}
                )
            )
            return discover(config)

    def test_plugin_obligations_and_carriers_appear_in_inventory(self) -> None:
        inventory = self._run(
            {"kind": "demo", "plugin": _FLOWS_PLUGIN, "id_prefix": "deluge:"}
        )

        # (a) the plugin's obligations are in the inventory.
        surfaces = {
            o["id"] for o in inventory["obligations"] if o["kind"] == "surface"
        }
        self.assertEqual(surfaces, {"deluge:Lead.onCreate", "deluge:Contact.onEdit"})

        # (d) excluded_surfaces flow through UNCHANGED.
        excluded = inventory["excluded_surfaces"]
        self.assertEqual(excluded["count"], 1)
        self.assertEqual(excluded["surfaces"][0]["reason"], _EXCLUDED_REASON)

        # (d) the unresolved carrier flows through UNCHANGED; the seam stamps this
        # entry's index as `adapter` so it merges exactly like a built-in's.
        reasons = [u.get("reason") for u in inventory["unresolved"]]
        self.assertIn(_UNRESOLVED_REASON, reasons)
        carrier = next(
            u for u in inventory["unresolved"] if u.get("reason") == _UNRESOLVED_REASON
        )
        self.assertEqual(carrier["adapter"], 0)
        self.assertEqual(carrier["kind"], "boundary")

    def test_kind_is_a_free_label_when_plugin_given(self) -> None:
        # `kind` is not a built-in, but with a plugin it is just a label: no raise.
        inventory = self._run({"kind": "totally-made-up", "plugin": _FLOWS_PLUGIN})
        surfaces = [o for o in inventory["obligations"] if o["kind"] == "surface"]
        self.assertEqual(len(surfaces), 2)

    def test_unknown_adapter_no_plugin_names_the_seam(self) -> None:
        # (c) unknown kind, NO plugin -> clear error that NAMES the plugin= seam.
        with self.assertRaises(ValueError) as caught:
            self._run({"kind": "cobol"})
        message = str(caught.exception)
        self.assertIn("unsupported", message)  # keeps the existing loud-failure floor
        self.assertIn("plugin", message)
        self.assertIn("module:callable", message)
        self.assertIn("cobol", message)

    def test_malformed_plugin_string_raises_specifically(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self._run({"kind": "demo", "plugin": "no_colon_here"})
        self.assertIn("module:callable", str(caught.exception))


class CorePathSeamTests(unittest.TestCase):
    """CORE path (capcov.toml [[adapters]] parity): adapters.load(name, plugin=...)."""

    def _core_dict(self) -> dict:
        shim = adapters.load("demo", plugin=_CORE_PLUGIN)
        # The shim is module-shaped so cli._run_adapter / cli.cmd_discover drive it
        # exactly like a built-in adapter module.
        self.assertIsNone(getattr(shim, "LANGUAGE", "unset"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            return shim.discover(
                root, root, config={"name": "demo", "plugin": _CORE_PLUGIN}
            )

    def test_plugin_loads_as_a_core_adapter(self) -> None:
        # (b) the fixture loads and produces a core dict; merge folds it like a
        # built-in (a lone adapter merges to itself).
        merged = adapters.merge([self._core_dict()])

        surface_ids = {s["id"] for s in merged["surfaces"]}
        self.assertEqual(surface_ids, {"http:POST /leads", "http:PUT /contacts"})
        entity_names = {e["name"] for e in merged["entities"]}
        self.assertEqual(entity_names, surface_ids)

        # (d) carriers survive the core build + merge unchanged.
        self.assertEqual(merged["excluded_surfaces"]["count"], 1)
        self.assertEqual(
            merged["excluded_surfaces"]["surfaces"][0]["reason"], _EXCLUDED_REASON
        )
        self.assertEqual(
            [u["reason"] for u in merged["unresolved"]], [_UNRESOLVED_REASON]
        )

    def test_unknown_adapter_no_plugin_names_the_seam(self) -> None:
        # (c), core path: an unknown name with no plugin fails loudly and points
        # at the seam.
        with self.assertRaises(SystemExit) as caught:
            adapters.load("cobol")
        message = str(caught.exception)
        self.assertIn("plugin", message)
        self.assertIn("module:callable", message)

    def test_malformed_plugin_string_raises_systemexit(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            adapters.load("demo", plugin="capcov_plugin_demo")  # no colon
        self.assertIn("module:callable", str(caught.exception))

    def test_missing_attribute_raises(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            adapters.load("demo", plugin="capcov_plugin_demo:not_a_real_callable")
        self.assertIn("not_a_real_callable", str(caught.exception))


class LoaderHelperTests(unittest.TestCase):
    """import_plugin_callable is the single resolver both paths share."""

    def test_resolves_a_callable(self) -> None:
        fn = adapters.import_plugin_callable(_CORE_PLUGIN)
        self.assertTrue(callable(fn))

    def test_rejects_a_non_callable_target(self) -> None:
        with self.assertRaises(ValueError):
            adapters.import_plugin_callable("capcov_plugin_demo:_UNRESOLVED_REASON")


if __name__ == "__main__":
    unittest.main()
