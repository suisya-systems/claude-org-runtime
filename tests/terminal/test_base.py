"""Tests for ``claude_org_runtime.terminal.base`` (backend-independent core).

Covers the screen-state heuristic (every ``classify_pane_state`` branch and
its measured calibration), ``wait_for_state`` polling, the ``default_backend``
environment resolution, the ``make_adapter`` factory dispatch, and the
``TerminalAdapter`` structural Protocol / ``PaneRef`` shape.
"""

from __future__ import annotations

import pytest

from claude_org_runtime.terminal import base
from claude_org_runtime.terminal.base import (
    NUDGE_TEXT,
    VALID_BACKENDS,
    PaneRef,
    TerminalAdapter,
    classify_pane_state,
    default_backend,
    make_adapter,
    wait_for_state,
)

_RULE = "─" * 40


# --------------------------------------------------------------------------
# classify_pane_state
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "marker",
    ["esc to interrupt", "ctrl+c to stop", "esc to cancel"],
)
def test_classify_busy_markers(marker: str) -> None:
    # Any of the response-in-progress hints near the tail => "busy".
    screen = "\n".join(["some output", f"… ({marker})"])
    assert classify_pane_state(screen) == "busy"


def test_classify_busy_is_case_insensitive() -> None:
    assert classify_pane_state("WORKING (ESC TO INTERRUPT)") == "busy"


def test_classify_busy_only_scans_tail_20_lines() -> None:
    # A busy marker buried above the last 20 lines must not flip to busy.
    screen = "\n".join(["(esc to interrupt)"] + ["filler"] * 25 + [_RULE, "❯ ", _RULE])
    assert classify_pane_state(screen) == "idle"


def test_classify_idle_caret_prompt() -> None:
    # Empty "❯ " prompt framed by horizontal rules => idle.
    screen = "\n".join([_RULE, "❯ ", _RULE])
    assert classify_pane_state(screen) == "idle"


def test_classify_input_pending_caret_prompt() -> None:
    # Non-empty content after the caret => unsent input pending.
    screen = "\n".join([_RULE, "❯ hello world", _RULE])
    assert classify_pane_state(screen) == "input_pending"


def test_classify_idle_legacy_frame() -> None:
    # Legacy "│ > │" box form (older claude) falls back to the same logic.
    assert classify_pane_state("│ >  │") == "idle"


def test_classify_input_pending_legacy_frame() -> None:
    assert classify_pane_state("│ > draft text │") == "input_pending"


def test_classify_unknown_when_no_prompt() -> None:
    assert classify_pane_state("just some scrollback\nno prompt here") == "unknown"


def test_classify_scans_from_bottom_up() -> None:
    # The lowest prompt line wins (most recent screen state).
    screen = "\n".join(["❯ old stale line", _RULE, "❯ ", _RULE])
    assert classify_pane_state(screen) == "idle"


# --------------------------------------------------------------------------
# wait_for_state
# --------------------------------------------------------------------------

class _ScriptedAdapter:
    """Minimal adapter whose ``get_text`` returns queued screens in order."""

    def __init__(self, screens: list[str]) -> None:
        self._screens = screens
        self._i = 0

    def get_text(self, pane_id, escapes: bool = False) -> str:  # noqa: ARG002
        screen = self._screens[min(self._i, len(self._screens) - 1)]
        self._i += 1
        return screen


def test_wait_for_state_reaches_target(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(base.time, "sleep", lambda _s: None)
    adapter = _ScriptedAdapter(["busy output (esc to interrupt)", "\n".join([_RULE, "❯ ", _RULE])])
    assert wait_for_state(adapter, "%1", "idle", timeout=5.0, interval=0.0) is True


def test_wait_for_state_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    # monotonic advances past the deadline so the loop exits False.
    ticks = iter([0.0, 0.5, 1.0, 1.5, 2.0])
    monkeypatch.setattr(base.time, "sleep", lambda _s: None)
    monkeypatch.setattr(base.time, "monotonic", lambda: next(ticks))
    adapter = _ScriptedAdapter(["never idle output"])
    assert wait_for_state(adapter, "%1", "idle", timeout=1.0, interval=0.0) is False


# --------------------------------------------------------------------------
# default_backend
# --------------------------------------------------------------------------

def test_default_backend_env_override_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPIKE_BACKEND", "wezterm")
    monkeypatch.setattr(base.os, "name", "posix")
    assert default_backend() == "wezterm"


def test_default_backend_posix_is_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SPIKE_BACKEND", raising=False)
    monkeypatch.setattr(base.os, "name", "posix")
    monkeypatch.setattr(base.sys, "platform", "linux")
    assert default_backend() == "tmux"


def test_default_backend_windows_is_wezterm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SPIKE_BACKEND", raising=False)
    monkeypatch.setattr(base.os, "name", "nt")
    monkeypatch.setattr(base.sys, "platform", "win32")
    assert default_backend() == "wezterm"


# --------------------------------------------------------------------------
# make_adapter
# --------------------------------------------------------------------------

def test_make_adapter_unknown_backend_raises() -> None:
    with pytest.raises(ValueError) as exc:
        make_adapter("zellij")
    msg = str(exc.value)
    assert "zellij" in msg
    for b in VALID_BACKENDS:
        assert b in msg


def test_make_adapter_tmux_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    # Patch the lookup site (terminal.tmux.find_tmux), not base, since
    # make_adapter lazily imports TmuxAdapter whose default_factory binds it.
    from claude_org_runtime.terminal import tmux as tmux_mod

    monkeypatch.setattr(tmux_mod, "find_tmux", lambda: "tmux")
    adapter = make_adapter("tmux")
    assert isinstance(adapter, tmux_mod.TmuxAdapter)


def test_make_adapter_wezterm_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    from claude_org_runtime.terminal import wezterm as wez_mod

    monkeypatch.setattr(wez_mod, "find_wezterm", lambda: "wezterm")
    adapter = make_adapter("wezterm")
    assert isinstance(adapter, wez_mod.WezTermAdapter)


def test_make_adapter_uses_default_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    from claude_org_runtime.terminal import tmux as tmux_mod

    monkeypatch.setenv("SPIKE_BACKEND", "tmux")
    monkeypatch.setattr(tmux_mod, "find_tmux", lambda: "tmux")
    assert isinstance(make_adapter(), tmux_mod.TmuxAdapter)


# --------------------------------------------------------------------------
# PaneRef / TerminalAdapter structural typing
# --------------------------------------------------------------------------

def test_paneref_defaults() -> None:
    ref = PaneRef(pane_id="%3")
    assert ref.pane_id == "%3"
    assert ref.tab_id is None
    assert ref.window_id is None


def test_terminal_adapter_is_runtime_checkable() -> None:
    # Both ported adapters must structurally satisfy the Protocol.
    from claude_org_runtime.terminal.tmux import TmuxAdapter
    from claude_org_runtime.terminal.wezterm import WezTermAdapter

    assert issubclass(TmuxAdapter, TerminalAdapter)
    assert issubclass(WezTermAdapter, TerminalAdapter)


def test_nudge_text_is_shared_constant() -> None:
    # The single nudge line is defined in base and re-exported by adapters.
    from claude_org_runtime.terminal import tmux, wezterm

    assert tmux.NUDGE_TEXT is NUDGE_TEXT
    assert wezterm.NUDGE_TEXT is NUDGE_TEXT
