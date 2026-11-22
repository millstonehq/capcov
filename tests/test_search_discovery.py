"""Agent discovery from the installed command's ordinary help surface."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class SearchDiscoveryTests(unittest.TestCase):
    def cli(self, *args):
        return subprocess.check_output([sys.executable, "-m", "capcov", *args], text=True)

    def test_help_routes_to_a_working_operation(self):
        self.assertIn("DreamCoder-style", self.cli("--help"))
        self.assertIn("next", self.cli("reason", "--help"))
        self.assertIn("--project", self.cli("reason", "next", "--help"))
        self.assertIn("DreamCoder-style", self.cli("reason", "run", "--help"))
        project = Path(__file__).resolve().parents[1] / "examples/search-projects/invention.json"
        with tempfile.TemporaryDirectory() as state:
            args = ["--project", str(project), "--operation", "invention-demo", "--state-dir", state, "--json"]
            action = json.loads(self.cli("reason", "next", *args))["next_action"]
            self.assertEqual(action["argv"][:3], ["capcov", "reason", "run"])
            result = json.loads(self.cli(*action["argv"][1:]))["search"]
            self.assertTrue(result["inventions"])
            self.assertEqual(result["evaluation"]["assisted"]["program"]["op"], "call")
