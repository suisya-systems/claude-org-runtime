"""Shared fixtures for terminal adapter tests.

The adapters shell out to ``tmux`` / ``wezterm cli`` via ``subprocess.run``.
CI runners (and most dev boxes) have neither binary, and the live AC-1/AC-2
behaviour is exercised by the fork harness, not here. These tests therefore
verify the *backend-independent* logic and the *command construction* of each
adapter by stubbing ``subprocess.run`` — never touching a real terminal.

``FakeRun`` records every argv it is called with and replays a queue of
canned :class:`subprocess.CompletedProcess` results, so a test can assert
exactly which flags an adapter method emits (the failure mode most likely to
regress when porting the flat spike modules into a package).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field

import pytest


@dataclass
class FakeRun:
    """Drop-in stub for ``subprocess.run`` used by the adapters.

    Each call pops the next canned ``(returncode, stdout, stderr)`` triple
    from ``responses`` (defaulting to a clean success) and records the full
    argv plus selected kwargs for assertions.
    """

    responses: list[tuple[int, str, str]] = field(default_factory=list)
    calls: list[list[str]] = field(default_factory=list)
    _idx: int = 0

    def __call__(self, cmd, *args, **kwargs) -> subprocess.CompletedProcess:
        self.calls.append(list(cmd))
        if self._idx < len(self.responses):
            rc, out, err = self.responses[self._idx]
        else:
            rc, out, err = (0, "", "")
        self._idx += 1
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr=err)

    # convenience accessors -------------------------------------------------
    @property
    def last(self) -> list[str]:
        return self.calls[-1]

    def queue(self, *responses: tuple[int, str, str]) -> "FakeRun":
        self.responses.extend(responses)
        return self


@pytest.fixture
def fake_run() -> FakeRun:
    return FakeRun()
