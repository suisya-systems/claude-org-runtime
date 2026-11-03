"""Issue #250 PR 2: the tmux cockpit (tabs, waiting line, tab menu, peek).

Command construction runs against the stubbed ``subprocess.run``. The live
test needs a tmux server and skips when one cannot be started.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import uuid
from importlib import resources

import pytest

from claude_org_runtime.terminal import cockpit
from claude_org_runtime.terminal import tmux as tmux_mod
from claude_org_runtime.terminal.tmux import ORG_CONF, TmuxAdapter

CONF = (resources.files("claude_org_runtime.terminal") / ORG_CONF).read_text(encoding="utf-8")


@pytest.fixture
def adapter(monkeypatch: pytest.MonkeyPatch, fake_run) -> TmuxAdapter:
    monkeypatch.setattr(tmux_mod.subprocess, "run", fake_run)
    a = TmuxAdapter(exe="tmux")
    a._fake = fake_run  # type: ignore[attr-defined]
    return a


def _args(call: list[str]) -> list[str]:
    assert call[:3] == ["tmux", "-L", "claude-org-broker"]
    return call[3:]


# ------------------------------------------------------------- config text
def _menu_branches() -> tuple[str, str]:
    block = CONF[CONF.index("\nbind -n MouseDown3Status if "):]
    working, rest = block.split("} {", 1)
    return working, rest[:rest.index("\n}")]


def test_interrupt_only_in_the_working_menu() -> None:
    working, other = _menu_branches()
    assert "'#{==:#{@state},working}' {" in working
    assert "Interrupt (Esc)" in working and "Interrupt" not in other
    # Re-checked at click time: ## survives the menu's own format expansion.
    assert "'##{==:##{@state},working}' 'send-keys -t #{pane_id} Escape'" in working


def test_menus_never_approve_or_kill() -> None:
    for branch in _menu_branches():
        assert not re.search(r"approve|Enter|kill|respawn", branch, re.I), branch
        for item in ("'Switch' s", "'Peek (read-only)' p", "'Back to secretary' b"):
            assert item in branch


def test_peek_never_splits_or_links_the_agent_window() -> None:
    for branch in _menu_branches():
        peek = next(ln for ln in branch.splitlines() if "'Peek (read-only)'" in ln)
        assert "split-window -h -d -t =org:^ " in peek
        assert "link-window" not in peek and "#{window_id}" not in peek
        assert "org peek #{pane_id}" in peek


def test_tabs_show_task_and_state_not_window_numbers() -> None:
    for opt in ("window-status-format", "window-status-current-format"):
        line = next(ln for ln in CONF.splitlines() if ln.startswith(f"set -g {opt} "))
        assert "#{@task}" in line and "#{@state}" in line
        assert "#I" not in line and "window_index" not in line


def test_waiting_line_is_clickable_and_yellow() -> None:
    line = next(ln for ln in CONF.splitlines() if ln.startswith("set -g status-format[1] "))
    assert "set -g status 2" in CONF
    assert "#[range=window|#{window_index}]" in line and "range=user" not in line
    assert "fill=yellow" in line
    assert line.count("#{m/r:^(waiting|needs-decision|done)$,#{@state}}") == 2


# ---------------------------------------------------------- ensure_cockpit
def test_ensure_creates_org_with_home_and_links_missing_agents(adapter) -> None:
    windows = "\n".join([
        "org\t@0\t",                       # home
        "org\t@3\tfix-1",                  # already linked
        "claude-org-broker-1-1\t@3\tfix-1",
        "claude-org-broker-1-2\t@5\tfix-2",  # agent, not linked yet
        "scratch\t@7\t",                   # not an agent (no @task)
    ]) + "\n"
    adapter._fake.queue((1, "", "can't find session"), (0, "", ""), (0, "", ""),
                        (0, windows, ""))
    assert cockpit.ensure_cockpit(adapter) == ["@5"]
    calls = [_args(c) for c in adapter._fake.calls]
    assert calls[0] == ["has-session", "-t", "=org"]
    assert calls[1][:6] == ["new-session", "-d", "-s", "org", "-n", "home"]
    assert ["set-option", "-w", "-t", "=org:", "@cockpit_home", "1"] == calls[1][7:13]
    assert calls[2][-2] == "source-file"
    assert calls[-1] == ["link-window", "-d", "-s", "@5", "-t", "=org:"]


def test_ensure_reuses_an_existing_cockpit(adapter) -> None:
    adapter._fake.queue((0, "", ""), (0, "", ""), (0, "org\t@0\t\n", ""))
    assert cockpit.ensure_cockpit(adapter) == []
    assert not any(_args(c)[0] == "new-session" for c in adapter._fake.calls)


# ------------------------------------------------------------------ attach
def test_attach_from_a_plain_terminal() -> None:
    a = TmuxAdapter(exe="tmux")
    argv, env = cockpit.attach_argv(a, {"HOME": "/h"})
    assert argv == ["tmux", "-L", "claude-org-broker", "attach-session", "-t", "=org"]
    assert env == {"HOME": "/h"}


def test_attach_from_inside_the_broker_socket_switches() -> None:
    a = TmuxAdapter(exe="tmux")
    argv, _ = cockpit.attach_argv(a, {"TMUX": "/tmp/tmux-1000/claude-org-broker,42,0"})
    assert argv[3:] == ["switch-client", "-t", "=org"]


def test_attach_from_another_tmux_nests_without_tmux_env() -> None:
    a = TmuxAdapter(exe="tmux")
    argv, env = cockpit.attach_argv(a, {"TMUX": "/tmp/tmux-1000/default,42,0", "X": "1"})
    assert argv[3] == "attach-session" and env == {"X": "1"}


# -------------------------------------------------------------------- peek
class _Screens:
    def __init__(self, screens: list[str]) -> None:
        self.screens = screens
        self.calls: list[tuple] = []

    def get_text(self, pane_id, escapes=False):
        self.calls.append((pane_id, escapes))
        if not self.screens:
            raise RuntimeError("can't find pane")
        return self.screens.pop(0)


def test_peek_mirrors_until_the_pane_is_gone() -> None:
    src = _Screens(["hello\nworld\n", "second frame\n"])
    out = io.StringIO()
    assert cockpit.peek(src, "%5", out=out, sleep=lambda _s: None) == 0
    text = out.getvalue()
    assert "peek %5 - read-only" in text and "world" in text and "second frame" in text
    assert "pane %5 is gone" in text
    assert src.calls == [("%5", True)] * 3  # reads only, with escapes
    assert text.endswith("\x1b[0m\x1b[?7h\x1b[?25h")  # terminal restored


def test_peek_exits_on_ctrl_c() -> None:
    def interrupt(_s):
        raise KeyboardInterrupt
    out = io.StringIO()
    assert cockpit.peek(_Screens(["x\n"]), "%5", out=out, sleep=interrupt) == 0
    assert out.getvalue().endswith("\x1b[?25h")


def test_cli_help_is_ascii() -> None:
    from claude_org_runtime.cli import build_parser
    for argv in (["org", "cockpit", "--help"], ["org", "peek", "--help"]):
        buf = io.StringIO()
        with pytest.raises(SystemExit), __import__("contextlib").redirect_stdout(buf):
            build_parser().parse_args(argv)
        buf.getvalue().encode("ascii")


# -------------------------------------------------------------------- live
@pytest.mark.skipif(os.name == "nt", reason="tmux is POSIX only")
def test_live_cockpit_end_to_end() -> None:
    if shutil.which("tmux") is None:
        pytest.skip("tmux not on PATH")
    a = TmuxAdapter(socket=f"claude-org-test-{uuid.uuid4().hex[:8]}")
    try:
        first = a.spawn(["sleep", "600"])
    except (RuntimeError, OSError, subprocess.SubprocessError):
        pytest.skip("cannot start a tmux server here (no tmux, or Unix sockets denied)")

    def tmux(*args: str) -> str:
        return a._tmux(*args).stdout.strip()

    try:
        # The whole config, menus and status formats included, parses cleanly.
        with resources.as_file(resources.files("claude_org_runtime.terminal") / ORG_CONF) as conf:
            a._tmux("source-file", str(conf))  # check=True: any error fails here
        assert "display-menu" in tmux("list-keys", "-T", "root", "MouseDown3Status")
        a.set_agent_options(first.pane_id, role="worker", task="fix-1", state="idle")
        assert cockpit.ensure_cockpit(a) == [first.window_id]
        assert cockpit.ensure_cockpit(a) == []  # idempotent
        # Spawned after the cockpit exists: linked by spawn itself.
        second = a.spawn(["sleep", "600"])
        a.set_agent_options(second.pane_id, role="worker", task="fix-2", state="waiting")
        org = tmux("list-windows", "-t", "=org", "-F", "#{window_id}").split()
        assert first.window_id in org and second.window_id in org
        # The home shell is not a broker pane; linked agents are listed once.
        panes = [p["pane_id"] for p in a.list_panes()]
        assert sorted(panes) == sorted([first.pane_id, second.pane_id])
        line2 = tmux("display-message", "-p", "-t", "=org:", "#{E:status-format[1]}")
        assert "Needs you:" in line2 and "fix-2 (waiting)" in line2 and "fix-1" not in line2
        assert re.search(r"range=window\|\d+", line2)
        tab = tmux("display-message", "-p", "-t", second.pane_id, "#{E:window-status-format}")
        assert "fix-2 waiting" in tab
        # kill-pane (the runtime's only close) removes the tab from the cockpit.
        a.kill_pane(first.pane_id)
        org = tmux("list-windows", "-t", "=org", "-F", "#{window_id}").split()
        assert first.window_id not in org and second.window_id in org
    finally:
        a.kill_server()  # this test's own socket only
