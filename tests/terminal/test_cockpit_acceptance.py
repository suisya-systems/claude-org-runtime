"""Acceptance test for #250 PR 2 (the tmux cockpit).

Config-level and stubbed only: no tmux server, network or HOME needed. Symbols
added by the change are looked up inside the tests and asserted, so this file
imports cleanly on the code before the change and fails there by assertion.
"""

from __future__ import annotations

import importlib
import importlib.util
import io
import re
from importlib import resources

import pytest

from claude_org_runtime.terminal import tmux as tmux_mod
from claude_org_runtime.terminal.tmux import TmuxAdapter


def _conf_lines() -> list[str]:
    text = (resources.files("claude_org_runtime.terminal") / "org.tmux.conf").read_text(
        encoding="utf-8")
    return text.splitlines()


def _option(name: str) -> str:
    found = [ln for ln in _conf_lines() if ln.startswith(f"set -g {name} ")]
    assert found, f"org.tmux.conf does not set {name}"
    return found[0]


def _menu() -> tuple[str, str]:
    text = "\n".join(_conf_lines())
    start = text.find("\nbind -n MouseDown3Status if -F -t = '#{==:#{@state},working}' {")
    assert start >= 0, "no @state-dependent right-click tab menu"
    working, sep, rest = text[start:].partition("} {")
    assert sep, "the tab menu needs a working and a non-working branch"
    return working, rest[:rest.find("\n}")]


def _cockpit():
    assert importlib.util.find_spec("claude_org_runtime.terminal.cockpit") is not None, \
        "no claude_org_runtime.terminal.cockpit module"
    return importlib.import_module("claude_org_runtime.terminal.cockpit")


@pytest.fixture
def adapter(monkeypatch: pytest.MonkeyPatch, fake_run) -> TmuxAdapter:
    monkeypatch.setattr(tmux_mod.subprocess, "run", fake_run)
    a = TmuxAdapter(exe="tmux")
    a._fake = fake_run  # type: ignore[attr-defined]
    return a


# ------------------------------------------------------------------ config
def test_tabs_show_task_and_state_without_window_numbers() -> None:
    for name in ("window-status-format", "window-status-current-format"):
        line = _option(name)
        assert "#{@task}" in line and "#{@state}" in line
        assert "#I" not in line and "window_index" not in line


def test_second_status_line_lists_agents_that_need_the_operator() -> None:
    assert "set -g status 2" in _conf_lines()
    line = _option("status-format[1]")
    assert "#[range=window|#{window_index}]" in line and "range=user" not in line
    assert "#{m/r:^(waiting|needs-decision|done)$,#{@state}}" in line
    assert "yellow" in line


def test_tab_menu_interrupts_only_while_working_and_never_approves() -> None:
    working, other = _menu()
    for branch in (working, other):
        for item in ("'Switch'", "'Peek (read-only)'", "'Back to secretary'"):
            assert item in branch
        assert not re.search(r"approve|Enter|kill|respawn", branch, re.I)
    assert "'Interrupt (Esc)'" in working and "Interrupt" not in other
    assert "send-keys -t #{pane_id} Escape" in working


def test_peek_is_a_split_of_the_home_window_not_a_link() -> None:
    for branch in _menu():
        peek = [ln for ln in branch.splitlines() if "'Peek (read-only)'" in ln]
        assert peek and "split-window -h -d -t =org:^ " in peek[0]
        assert "link-window" not in peek[0] and "org peek #{pane_id}" in peek[0]


# ----------------------------------------------------------------- adapter
def test_list_panes_hides_the_cockpit_home_and_peek_panes(adapter) -> None:
    adapter.list_panes()
    args = adapter._fake.last[3:]
    assert args[:2] == ["list-panes", "-a"]
    assert "-f" in args and args[args.index("-f") + 1] == "#{?#{@cockpit_home},0,1}"


def test_spawn_links_the_new_window_into_the_cockpit(adapter) -> None:
    adapter._fake.queue((0, "%2\t@1\tclaude-org-broker-9-1", ""))
    adapter.spawn(["cat"])
    calls = [c[3:] for c in adapter._fake.calls]
    assert ["link-window", "-d", "-s", "@1", "-t", "=org:"] in calls


# ----------------------------------------------------------------- cockpit
def test_cockpit_creates_org_and_links_only_missing_agents(adapter) -> None:
    cockpit = _cockpit()
    windows = "org\t@0\t\norg\t@3\tfix-1\nagent-a\t@3\tfix-1\nagent-b\t@5\tfix-2\nmisc\t@7\t\n"
    adapter._fake.queue((1, "", "can't find session"), (0, "", ""), (0, "", ""),
                        (0, windows, ""))
    assert cockpit.ensure_cockpit(adapter) == ["@5"]
    calls = [c[3:] for c in adapter._fake.calls]
    assert calls[1][:6] == ["new-session", "-d", "-s", "org", "-n", "home"]
    assert "@cockpit_home" in calls[1]
    assert calls[-1] == ["link-window", "-d", "-s", "@5", "-t", "=org:"]


def test_peek_only_reads_the_pane() -> None:
    cockpit = _cockpit()

    class Src:
        def __init__(self) -> None:
            self.frames = ["agent screen\n"]
            self.reads = 0

        def get_text(self, pane_id, escapes=False):
            self.reads += 1
            if not self.frames:
                raise RuntimeError("gone")
            return self.frames.pop()

    src, out = Src(), io.StringIO()
    assert cockpit.peek(src, "%5", out=out, sleep=lambda _s: None) == 0
    assert "agent screen" in out.getvalue() and "read-only" in out.getvalue()
    assert src.reads == 2  # Src has no write methods: any write would raise
