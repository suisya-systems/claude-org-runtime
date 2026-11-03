"""Issue #250: cockpit ``@state`` mapping and the broker's option publishing."""

from __future__ import annotations

import pytest

from claude_org_runtime.broker.server import Broker
from claude_org_runtime.broker.user_turn import agent_state

from .conftest import FakeAdapter

RULE = "─" * 40


def composer(content: str = "", footer: str = "  ? for shortcuts") -> str:
    return f"some transcript\n\n{RULE}\n❯ {content}\n{RULE}\n{footer}\n"


@pytest.mark.parametrize("screen,want", [
    (composer(), "idle"),
    (composer("half typed"), "idle"),
    (composer("1. first\n  2. second"), "idle"),          # a numbered draft
    (composer(footer="✻ Working… (esc to interrupt)"), "working"),
    # permission menu: its "Esc to cancel" is also a busy marker
    ("Do you want to proceed?\n❯ 1. Yes\n  2. No\n\nEsc to cancel", "waiting"),
    ("Do you want to proceed?\n❯ 1. Yes\n  2. No\n", "waiting"),
    # boxed menu, cursor on option 2 (spawn_prompt fixture shape)
    ("│   1. Yes, proceed │\n│ ❯ 2. No, exit      │", "waiting"),
    ("Pick\n  Option\n\nEnter to confirm · Esc to cancel", "waiting"),
    ("Do you want to proceed?\n❯ 1. Yes\n  2. No\n" + "\n" * 30, "waiting"),
    ("$ ls\nfile\n", None),
    ("", None),
])
def test_agent_state(screen: str, want: str | None) -> None:
    assert agent_state(screen) == want


class OptionAdapter(FakeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.options: list[tuple[str, dict]] = []
        self.screen = composer()

    def set_agent_options(self, pane_id, **opts) -> None:
        self.options.append((pane_id, opts))

    def get_text(self, pane_id, escapes=False) -> str:
        return self.screen


def _broker(tmp_path, adapter) -> Broker:
    b = Broker(state_dir=tmp_path / "broker", adapter=adapter)
    b.agent_state_interval = 0.01
    return b


def test_register_publishes_role_and_task(tmp_path) -> None:
    a = OptionAdapter()
    b = _broker(tmp_path, a)
    b._register_pane("%1", "fix-1", "fix-1", "worker", None, "claude", None)
    b._register_pane("%2", "claude-3", None, None, None, "claude", None)
    assert a.options == [("%1", {"role": "worker", "task": "fix-1"}),
                         ("%2", {"role": "", "task": "claude-3"})]


def test_no_option_writes_without_capability(tmp_path) -> None:
    b = _broker(tmp_path, FakeAdapter())
    b._register_pane("%1", "fix-1", "fix-1", "worker", None, "claude", None)
    assert b._agent_state_thread is None  # not started, and register did not fail


def test_option_failure_never_fails_the_caller(tmp_path) -> None:
    a = OptionAdapter()

    def boom(pane_id, **opts):
        raise RuntimeError("server gone")
    a.set_agent_options = boom  # type: ignore[method-assign]
    b = _broker(tmp_path, a)
    b._register_pane("%1", "fix-1", "fix-1", "worker", None, "claude", None)


def _poll_once(b: Broker) -> None:
    # Drive one loop iteration deterministically: wait() returns False once.
    calls = iter([False, True])
    b._agent_state_stop.wait = lambda _t: next(calls)  # type: ignore[method-assign]
    b._agent_state_loop()


def test_poller_writes_state_only_on_change(tmp_path) -> None:
    a = OptionAdapter()
    b = _broker(tmp_path, a)
    b._register_pane("%1", "fix-1", "fix-1", "worker", None, "claude", None)
    b._register_pane("%2", "cx", "cx", "worker", None, "codex", None)
    a.options.clear()
    _poll_once(b)
    assert a.options == [("%1", {"state": "idle"})]  # codex is skipped
    _poll_once(b)
    assert a.options == [("%1", {"state": "idle"})]  # unchanged: no write
    a.screen = composer(footer="✻ Working… (esc to interrupt)")
    _poll_once(b)
    assert a.options[-1] == ("%1", {"state": "working"})
    a.screen = "$ unrecognised\n"
    _poll_once(b)
    assert len(a.options) == 2  # unknown screen keeps the previous value


def test_poller_skips_logical_panes(tmp_path) -> None:
    a = OptionAdapter()
    b = _broker(tmp_path, a)
    tok = b.issue_token("secretary", "secretary", "secretary", kind="claude")
    b.register_logical_pane(tok)
    _poll_once(b)
    assert a.options == []


def test_poller_thread_starts_and_stops_with_broker(tmp_path) -> None:
    a = OptionAdapter()
    b = _broker(tmp_path, a)
    b.start()
    try:
        assert b._agent_state_thread is not None and b._agent_state_thread.is_alive()
    finally:
        b.stop()
    assert b._agent_state_thread is None


def test_identity_change_republishes_role_and_task(tmp_path) -> None:
    a = OptionAdapter()
    b = _broker(tmp_path, a)
    handle = a.add_pane()
    b._register_pane(handle, "claude-1", None, None, None, "claude", None)
    a.options.clear()
    b.set_pane_identity(str(handle), True, "fix-2", True, "worker")
    assert a.options == [(handle, {"role": "worker", "task": "fix-2"})]
