import copy
import unittest

from capcov.flows.model import plan
from tests.test_flows import fixture


class FlowInputTests(unittest.TestCase):
    def model(self, command):
        _, model = fixture()
        model["transitions"][0]["bindings"]["browser"]["commands"].insert(0, command)
        return model

    def test_select_preserves_exact_option_values_including_empty(self):
        for value in ("10143", "", "vendor:with-space "):
            command = {"op": "select", "selector": "select[name=store]", "value": value}
            result = plan(self.model(command), "browser")
            self.assertEqual(result["scenarios"][0]["steps"][0]["commands"][0], command)

    def test_select_requires_a_typed_value_and_selector(self):
        for value in (None, False, 0, [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "string option value"):
                plan(self.model({"op": "select", "selector": "select", "value": value}), "browser")
        with self.assertRaisesRegex(ValueError, "string option value"):
            plan(self.model({"op": "select", "selector": "select"}), "browser")
        with self.assertRaisesRegex(ValueError, "selector required"):
            plan(self.model({"op": "select", "value": "one"}), "browser")

    def test_upload_preserves_multiple_paths_without_assertion_credit(self):
        command = {"op": "upload", "selector": "input[type=file]",
                   "files": ["fixtures/page 1.png", "fixtures/page2.png"]}
        model = self.model(command)
        result = plan(model, "browser")
        self.assertEqual(result["scenarios"][0]["steps"][0]["commands"][0], command)
        without_assertion = copy.deepcopy(model)
        without_assertion["transitions"][0]["bindings"]["browser"]["commands"] = [command]
        with self.assertRaisesRegex(ValueError, "no observable assertion"):
            plan(without_assertion, "browser")

    def test_upload_rejects_missing_malformed_and_nonportable_paths(self):
        for files in (None, [], "a.png", [None], [False], [3], [""], [" "],
                      ["/a.png"], ["../a.png"], ["a/../b.png"], ["a/./b.png"],
                      ["a//b.png"], ["a/"], ["C:/a.png"], ["a\\b.png"], ["a\0.png"]):
            with self.subTest(files=files), self.assertRaisesRegex(ValueError, "repository-relative"):
                plan(self.model({"op": "upload", "selector": "input", "files": files}), "browser")
        with self.assertRaisesRegex(ValueError, "repository-relative"):
            plan(self.model({"op": "upload", "selector": "input"}), "browser")
        with self.assertRaisesRegex(ValueError, "selector required"):
            plan(self.model({"op": "upload", "files": ["a.png"]}), "browser")
