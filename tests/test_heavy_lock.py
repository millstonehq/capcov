"""`scripts/with-heavy-lock.py`: one machine-wide lock around disk-heavy steps."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "with-heavy-lock.py"


@unittest.skipUnless(hasattr(os, "fork") and SCRIPT.exists(), "POSIX only")
class HeavyLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="capcov-heavy-lock-")
        self.addCleanup(self.tmp.cleanup)
        self.lock = Path(self.tmp.name) / "heavy.lock"

    def _run(self, *args: str, wait: bool = True, **kwargs):
        command = [sys.executable, str(SCRIPT), "--lock", str(self.lock), *args]
        if wait:
            return subprocess.run(command, capture_output=True, text=True, **kwargs)
        return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)

    def _hold(self, seconds: float) -> subprocess.Popen:
        holder = self._run("--label", "holder", "--", sys.executable, "-c",
                           f"import time; time.sleep({seconds})", wait=False)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.lock.exists() and "pid" in self.lock.read_text(errors="replace"):
                return holder
            time.sleep(0.05)
        holder.kill()
        self.fail("holder never took the lock")

    def test_exit_status_passes_through_and_lock_is_released(self) -> None:
        result = self._run("--", sys.executable, "-c", "import sys; sys.exit(7)")
        self.assertEqual(result.returncode, 7)
        self.assertEqual(self.lock.read_text(), "")  # released: holder line cleared

    def test_second_holder_waits_until_the_first_releases(self) -> None:
        holder = self._hold(2.0)
        try:
            started = time.monotonic()
            waiter = self._run("--timeout", "30", "--label", "waiter", "--", sys.executable, "-c", "pass")
            elapsed = time.monotonic() - started
        finally:
            holder.wait(timeout=30)
        self.assertEqual(waiter.returncode, 0, waiter.stderr)
        self.assertGreaterEqual(elapsed, 1.0)
        self.assertIn("waiting for", waiter.stderr)
        self.assertIn("holder", waiter.stderr)

    def test_no_wait_fails_fast_with_tempfail(self) -> None:
        holder = self._hold(3.0)
        try:
            started = time.monotonic()
            refused = self._run("--timeout", "0", "--", sys.executable, "-c", "pass")
            elapsed = time.monotonic() - started
        finally:
            holder.terminate()
            holder.wait(timeout=30)
        self.assertEqual(refused.returncode, 75)
        self.assertLess(elapsed, 2.5)
        self.assertIn("giving up", refused.stderr)

    def test_missing_command_is_a_usage_error(self) -> None:
        result = self._run()
        self.assertEqual(result.returncode, 64)


if __name__ == "__main__":
    unittest.main()
