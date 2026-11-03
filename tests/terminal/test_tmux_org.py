"""Issue #250 (first step): org tmux config, agent window options, safety.

Command construction is pinned with the stubbed ``subprocess.run``. The live
test at the bottom needs a tmux server and skips when one cannot be started
(CI has no tmux; a sandbox may deny Unix-socket connect).
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import uuid
from importlib import resources
from pathlib import Path

import pytest

from claude_org_runtime.terminal import tmux as tmux_mod
from claude_org_runtime.terminal.tmux import ORG_CONF, TmuxAdapter

SRC = Path(tmux_mod.__file__).resolve().parents[1]
CONF_TEXT = (resources.files("claude_org_runtime.terminal") / ORG_CONF).read_text(
    encoding="utf-8")
# Commands that kill or respawn, or open a chooser / menu that offers to.
DESTRUCTIVE = re.compile(r"kill|respawn|choose-tree|find-window|confirm")


@pytest.fixture
def adapter(monkeypatch: pytest.MonkeyPatch, fake_run) -> TmuxAdapter:
    monkeypatch.setattr(tmux_mod.subprocess, "run", fake_run)
    a = TmuxAdapter(exe="tmux")
    a._fake = fake_run  # type: ignore[attr-defined]
    return a


def _args(call: list[str]) -> list[str]:
    assert call[:3] == ["tmux", "-L", "claude-org-broker"]
    return call[3:]


# ------------------------------------------------------------ config apply
def test_spawn_sources_org_config_then_links_into_cockpit(adapter) -> None:
    adapter._fake.queue((0, "%2\t@1\tclaude-org-broker-9-1", ""))
    adapter.spawn(["cat"])
    new_session, source, link = (_args(c) for c in adapter._fake.calls)
    assert new_session[0] == "new-session"
    # @org_runtime (the CLI the tab menu's peek runs) is set before sourcing.
    assert source[:3] == ["set-option", "-g", "@org_runtime"]
    assert source[3].endswith("-m claude_org_runtime.cli")
    assert source[4:6] == [";", "source-file"]
    assert Path(source[6]).name == ORG_CONF and Path(source[6]).is_file()
    assert link == ["link-window", "-d", "-s", "@1", "-t", "=org:"]


def test_list_panes_hides_cockpit_windows(adapter) -> None:
    adapter.list_panes()
    args = _args(adapter._fake.last)
    assert args[:4] == ["list-panes", "-a", "-f", "#{?#{@cockpit_home},0,1}"]


def test_config_error_does_not_fail_spawn(adapter) -> None:
    # The pane already exists: a failing source-file must not orphan it.
    adapter._fake.queue((0, "%2\t@1\tclaude-org-broker-9-1", ""),
                        (1, "", "bad config"))
    assert adapter.spawn(["cat"]).pane_id == "%2"


def test_config_timeout_does_not_fail_spawn(adapter, monkeypatch) -> None:
    def slow(*_a, **_k):
        raise subprocess.TimeoutExpired("tmux source-file", 15)
    adapter._fake.queue((0, "%2\t@1\tclaude-org-broker-9-1", ""))
    monkeypatch.setattr(adapter, "apply_org_config", slow)
    assert adapter.spawn(["cat"]).pane_id == "%2"


# --------------------------------------------------------- window options
def test_set_agent_options_writes_window_options_then_refreshes(adapter) -> None:
    adapter.set_agent_options("%3", role="worker", task="fix-1", state="working")
    opts, refresh = (_args(c) for c in adapter._fake.calls)
    assert opts == [
        "set-option", "-w", "-t", "%3", "@role", "worker", ";",
        "set-option", "-w", "-t", "%3", "@task", "fix-1", ";",
        "set-option", "-w", "-t", "%3", "@state", "working",
    ]
    assert refresh == ["refresh-client", "-S"]


def test_refresh_without_client_is_not_an_error(adapter) -> None:
    adapter._fake.queue((0, "", ""), (1, "", "no current client"))
    adapter.set_agent_options("%3", state="idle")  # must not raise


def test_list_panes_dedupes_a_window_linked_into_two_sessions(adapter) -> None:
    line = "%1\t@0\t{s}\t0\t0\t220\t50\t5\t10\t1\t12345"
    adapter._fake.queue((0, line.format(s="agent") + "\n" + line.format(s="org") + "\n", ""))
    panes = adapter.list_panes()
    assert [(p["pane_id"], p["session"]) for p in panes] == [("%1", "agent")]


# ------------------------------------------------------------------ safety
def _lines() -> list[str]:
    return [ln.strip() for ln in CONF_TEXT.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def test_config_binds_nothing_destructive() -> None:
    for ln in _lines():
        if ln.startswith("bind"):
            assert not DESTRUCTIVE.search(ln), ln


@pytest.mark.parametrize("key", [
    "-T prefix x", "-T prefix '&'", "-T prefix '<'", "-T prefix '>'",
    "-T prefix s", "-T prefix w", "-T prefix f", "-T prefix C-b",
    "-n M-MouseDown3Pane", "-n MouseDown3StatusLeft", "-n M-MouseDown3StatusLeft",
    "-n M-MouseDown3Status",
])
def test_config_unbinds_default_killers(key: str) -> None:
    assert f"unbind {key}" in _lines()


def test_config_replaces_default_right_click_menus() -> None:
    lines = _lines()
    assert any(ln.startswith("bind -n MouseDown3Status if -F -t = ") for ln in lines)
    assert "bind -n MouseDown3Pane select-pane -t= \\; send -M" in lines
    assert "bind -T prefix '&' unlink-window" in lines


def test_config_safety_comes_first() -> None:
    lines = _lines()
    assert lines[0].startswith("unbind")
    assert lines.index("unbind -T prefix x") < lines.index("set -g mouse on")


@pytest.mark.parametrize("line", [
    "set -g mouse on", "set -s escape-time 10", "set -g status-right ''",
    "set -g allow-passthrough on", "set -g focus-events on",
    "set -s extended-keys on", "set -s terminal-features[90] 'xterm*:extkeys'",
    "set -g prefix C-Space", "set -s set-clipboard external",
])
def test_config_sets(line: str) -> None:
    assert line in _lines()


def test_config_is_idempotent_no_array_appends() -> None:
    for ln in _lines():
        assert not re.match(r"set(-option)? -\w*a", ln), ln


def test_runtime_never_kills_sessions_windows_or_the_server() -> None:
    # Invariant: agents are closed with kill-pane. kill-session leaves a dead
    # link in the cockpit and kill-window on a linked window kills the agent.
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "kill-session" not in text and "kill-window" not in text, path
        if path.name != "tmux.py":  # only the self-test, on its own socket
            assert "kill_server(" not in text, path


# -------------------------------------------------------------------- live
def _live_socket() -> str | None:
    if shutil.which("tmux") is None:
        return None
    name = f"claude-org-test-{uuid.uuid4().hex[:8]}"
    proc = subprocess.run(
        ["tmux", "-L", name, "-f", "/dev/null", "new-session", "-d", "-s", "agent",
         "-x", "120", "-y", "30", "sleep 600"],
        capture_output=True, text=True,
    )
    return name if proc.returncode == 0 else None


@pytest.mark.skipif(os.name == "nt", reason="tmux is POSIX only")
def test_live_tmux_config_and_linked_window_options() -> None:
    sock = _live_socket()
    if sock is None:
        pytest.skip("cannot start a tmux server here (no tmux, or Unix sockets denied)")
    a = TmuxAdapter(socket=sock)

    def tmux(*args: str) -> str:
        return a._tmux(*args).stdout

    try:
        a.apply_org_config()
        a.apply_org_config()  # re-source: must stay idempotent
        for table in ("root", "prefix"):
            keys = tmux("list-keys", "-T", table)
            bad = [k for k in keys.splitlines() if DESTRUCTIVE.search(k)]
            assert not bad, bad
        assert tmux("show-options", "-gv", "mouse").strip() == "on"
        assert tmux("show-options", "-gv", "prefix").strip() == "C-Space"
        features = tmux("show-options", "-s", "terminal-features")
        assert features.count("extkeys") == 1
        # Window options are visible through a window linked into the cockpit.
        pane = tmux("display-message", "-p", "-t", "agent", "#{pane_id}").strip()
        a.set_agent_options(pane, role="worker", task="fix-1", state="waiting")
        tmux("new-session", "-d", "-s", "org", "sleep 600")
        tmux("link-window", "-s", "agent:0", "-t", "org:9")
        shown = tmux("display-message", "-p", "-t", "org:9", "#{@task} #{@state}")
        assert shown.strip() == "fix-1 waiting"
        assert len([p for p in a.list_panes() if p["pane_id"] == pane]) == 1
    finally:
        a.kill_server()  # this test's own socket only


def test_runtime_path_with_spaces_survives_the_menu_quoting(adapter, monkeypatch) -> None:
    # The tab menu runs '#{@org_runtime} org peek ...' inside tmux single
    # quotes, then sh: backslash escapes survive both, nested quotes do not.
    monkeypatch.setattr(tmux_mod.sys, "executable", "/home/a b/.venv/bin/python")
    adapter.apply_org_config()
    runtime = _args(adapter._fake.calls[0])[3]
    assert runtime == "/home/a\\ b/.venv/bin/python -m claude_org_runtime.cli"
    assert "'" not in runtime
    # sh word splitting (POSIX rules; subprocess.run is stubbed here).
    assert shlex.split(runtime) == ["/home/a b/.venv/bin/python", "-m",
                                    "claude_org_runtime.cli"]
