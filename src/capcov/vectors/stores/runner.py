"""The one seam between a store adapter and the outside world: a command runner.

A store adapter never calls ``subprocess`` or greps ``docker ps``. It hands an
argv to a runner and reads bytes back. That is what makes every adapter testable
with a fake that returns canned bytes and records what it was asked, and what
lets the same adapter reach a client binary on the host (``LocalRunner``) or one
inside a container (``DockerExecRunner``) without knowing which.

Refusal: a non-zero exit is ``CommandFailed`` naming the argv and carrying the
stderr. A runner never returns partial stdout as if the command had succeeded.
"""

from __future__ import annotations

import subprocess
from typing import Protocol


class CommandFailed(RuntimeError):
    """A command exited non-zero. Carries argv, code and stderr for the caller."""

    def __init__(self, argv: list[str], returncode: int, stderr: bytes) -> None:
        text = stderr.decode(errors="replace")
        if len(text) > 3000:
            # A SQL client echoes the failing statement before the error; keep both ends.
            text = text[:1000] + "\n...\n" + text[-2000:]
        super().__init__(f"{argv[0]} exited {returncode}: {text}")
        self.argv = list(argv)
        self.returncode = returncode
        self.stderr = stderr


class CommandRunner(Protocol):
    def run(self, argv: list[str], input: bytes | None = None) -> bytes:
        """Run argv to completion; return stdout. Raise CommandFailed on non-zero exit."""


class LocalRunner:
    """Run the command on this host. ``timeout`` is seconds; ``env`` replaces the environment."""

    def __init__(self, timeout: float = 600, env: dict | None = None) -> None:
        self.timeout = timeout
        self.env = env

    def run(self, argv: list[str], input: bytes | None = None) -> bytes:
        result = subprocess.run(
            list(argv), input=input, capture_output=True, timeout=self.timeout, env=self.env
        )
        if result.returncode:
            raise CommandFailed(list(argv), result.returncode, result.stderr)
        return result.stdout


class DockerExecRunner:
    """Run the command inside a named container, through an inner runner.

    The container NAME is given by the caller (a consumer resolves it from its
    own environment); the adapter never lists containers itself. ``-i`` is always
    passed so a restore can stream its dump on stdin. The inner runner defaults
    to ``LocalRunner`` and is injectable so a test can see the exact argv.
    """

    def __init__(self, container: str, inner: CommandRunner | None = None, docker: str = "docker") -> None:
        if not container:
            raise ValueError("DockerExecRunner: container name is empty")
        self.container = container
        self.inner = inner or LocalRunner()
        self.docker = docker

    def run(self, argv: list[str], input: bytes | None = None) -> bytes:
        return self.inner.run([self.docker, "exec", "-i", self.container, *argv], input=input)
