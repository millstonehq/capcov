#!/usr/bin/env python3
"""Serialize disk-heavy work across concurrent agents on one machine.

    with-heavy-lock.py [--lock PATH] [--timeout SECONDS] [--label TEXT] -- COMMAND [ARG...]

Takes an exclusive advisory lock (``fcntl.flock``) on one file shared by every
session -- ``$CAPCOV_HEAVY_LOCK``, else ``$XDG_CACHE_HOME/capcov/heavy.lock``,
else ``~/.cache/capcov/heavy.lock`` -- runs COMMAND with this process's stdio,
and releases the lock when it exits.  While waiting it says who holds the lock
(pid, label, since when), once a minute on stderr.

The child's exit status is returned unchanged.  Exit status 75 (EX_TEMPFAIL)
means the lock was not obtained within ``--timeout`` (default 3600 seconds;
``0`` means do not wait at all).  SIGINT and SIGTERM are forwarded to the child.

Nothing about the command's inputs or outputs changes; this only decides WHEN
it runs.  Wrap the steps that thrash a shared disk when several agents run at
once: ``nix develop`` builds, indexer runs, full-tree source snapshots, full
regressions.  A heavy step that is not wrapped still runs; it just contends.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

EX_TEMPFAIL = 75
EX_USAGE = 64
REPORT_EVERY_SECONDS = 60.0
POLL_SECONDS = 1.0


def default_lock_path() -> Path:
    explicit = os.environ.get("CAPCOV_HEAVY_LOCK")
    if explicit:
        return Path(explicit)
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "capcov" / "heavy.lock"


def _holder(fd: int) -> str:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        text = os.read(fd, 4096).decode("utf-8", "replace").strip()
    except OSError:
        return "unknown holder"
    return text or "unknown holder"


def acquire(path: Path, timeout: float, label: str) -> int | None:
    """Return a locked descriptor, or None when the deadline passes."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + timeout
    last_report = None
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            now = time.monotonic()
            if timeout <= 0 or now >= deadline:
                print(f"with-heavy-lock: {path} is held by {_holder(fd)}; giving up "
                      f"({'no wait requested' if timeout <= 0 else 'timeout'})", file=sys.stderr)
                os.close(fd)
                return None
            if last_report is None or now - last_report >= REPORT_EVERY_SECONDS:
                print(f"with-heavy-lock: waiting for {path} held by {_holder(fd)}"
                      f" (label {label!r})", file=sys.stderr)
                last_report = now
            time.sleep(min(POLL_SECONDS, max(0.0, deadline - now)))
    stamp = _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, f"pid {os.getpid()} {label} since {stamp}\n".encode())
    os.fsync(fd)
    return fd


def release(fd: int) -> None:
    try:
        os.ftruncate(fd, 0)
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def run(command: list[str]) -> int:
    child = subprocess.Popen(command)

    def forward(signum, _frame):
        try:
            child.send_signal(signum)
        except ProcessLookupError:
            pass

    previous = {sig: signal.signal(sig, forward) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        return child.wait()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="with-heavy-lock", description=__doc__.split("\n\n")[0],
        usage="%(prog)s [--lock PATH] [--timeout SECONDS] [--label TEXT] -- COMMAND [ARG...]")
    parser.add_argument("--lock", type=Path, default=None, help="lock file (default: see module doc)")
    parser.add_argument("--timeout", type=float, default=3600.0,
                        help="seconds to wait for the lock; 0 = fail immediately (default 3600)")
    parser.add_argument("--label", default="", help="who is holding the lock, for the waiting message")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- COMMAND [ARG...]")
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.print_usage(sys.stderr)
        return EX_USAGE
    label = args.label or os.path.basename(command[0])
    fd = acquire(args.lock or default_lock_path(), args.timeout, label)
    if fd is None:
        return EX_TEMPFAIL
    try:
        return run(command)
    finally:
        release(fd)


if __name__ == "__main__":
    sys.exit(main())
