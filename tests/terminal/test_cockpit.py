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
        "org\t@0\t\thome\toff",                       # home
        "org\t@3\tfix-1\tfix-1\toff",                  # already linked
        "claude-org-broker-1-1\t@3\tfix-1\tfix-1\toff",
        "claude-org-broker-1-2\t@5\tfix-2\tfix-2\toff",  # agent, not linked yet
        "scratch\t@7\t\tsh\toff",                   # not an agent (no @task)
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
    adapter._fake.queue((0, "", ""), (0, "", ""), (0, "org\t@0\t\thome\toff\n", ""), (0, "1\n", ""))
    assert cockpit.ensure_cockpit(adapter) == []
    calls = [_args(c) for c in adapter._fake.calls]
    assert not any(c[0] in ("new-session", "new-window") for c in calls)


def test_home_keeps_its_window_after_the_shell_exits(adapter) -> None:
    adapter._fake.queue((1, "", "can't find session"))
    cockpit.ensure_cockpit(adapter)
    create = _args(adapter._fake.calls[1])
    # Pane scope: peek splits in home must still close when their mirror exits.
    assert ["set-option", "-p", "-t", "=org:", "remain-on-exit", "on"] == create[-6:]


def test_ensure_recreates_a_lost_home_as_the_first_window(adapter) -> None:
    # Peek splits =org:^; without home that would be an agent's window.
    adapter._fake.queue((0, "", ""), (0, "", ""), (0, "org\t@3\tfix-1\tfix-1\toff\n", ""),
                        (0, "\n", ""), (0, "@9\n", ""))
    cockpit.ensure_cockpit(adapter)
    calls = [_args(c) for c in adapter._fake.calls]
    assert calls[4][:6] == ["new-window", "-b", "-d", "-t", "=org:^", "-n"]
    assert ["set-option", "-w", "-t", "@9", "@cockpit_home", "1"] == calls[5][:6]


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
        # Home survives its shell, and a lost home comes back as the first window.
        home = tmux("display-message", "-p", "-t", "=org:^", "#{window_id}")
        assert tmux("show-options", "-pv", "-t", home, "remain-on-exit") == "on"
        assert tmux("show-options", "-wv", "-t", home, "remain-on-exit") != "on"
        a._tmux("kill-window", "-t", home)  # test only: simulate a lost home
        cockpit.ensure_cockpit(a)
        assert tmux("display-message", "-p", "-t", "=org:^", "#{@cockpit_home}") == "1"
        # kill-pane (the runtime's only close) removes the tab from the cockpit.
        a.kill_pane(first.pane_id)
        org = tmux("list-windows", "-t", "=org", "-F", "#{window_id}").split()
        assert first.window_id not in org and second.window_id in org
    finally:
        a.kill_server()  # this test's own socket only


@pytest.mark.skipif(os.name == "nt", reason="tmux is POSIX only")
def test_live_cockpit_starts_keeps_and_recovers_the_secretary(monkeypatch) -> None:
    if shutil.which("tmux") is None:
        pytest.skip("tmux not on PATH")
    a = TmuxAdapter(socket=f"claude-org-test-{uuid.uuid4().hex[:8]}")
    try:
        a._tmux("start-server")
        cockpit.ensure_cockpit(a)
    except (RuntimeError, OSError, subprocess.SubprocessError):
        pytest.skip("cannot start a tmux server here (no tmux, or Unix sockets denied)")
    # Stand-in secretary: shows the env it got, then cat, which exits on C-d.
    asked = []
    monkeypatch.setattr(
        cockpit, "secretary_command",
        lambda action, args, alive: asked.append((action, alive))
        or ["sh", "-c", 'echo "env=$COCKPIT_T"; exec cat'])

    def tmux(*args: str) -> str:
        return a._tmux(*args).stdout.strip()

    def start(alive: bool) -> int:
        return cockpit.start_secretary(a, _cockpit_args(), env={"COCKPIT_T": "1"},
                                       cwd="/", daemon_alive=lambda: alive)

    def wait_for(fmt: str, want: str | frozenset) -> None:
        for _ in range(50):
            got = tmux("display-message", "-p", "-t", home, fmt)
            if got == want or (isinstance(want, frozenset) and got in want):
                return
            __import__("time").sleep(0.1)
        raise AssertionError(f"{fmt} never became {want!r}")

    try:
        home = tmux("display-message", "-p", "-t", "=org:^", "#{pane_id}")
        wait_for("#{pane_current_command}", cockpit._SHELLS)  # home shell is up
        assert start(alive=False) == 0 and asked == [("launch_kill", False)]
        wait_for("#{pane_current_command}", "cat")
        assert tmux("show-options", "-pv", "-t", home, "@org_secretary") == "1"
        assert "env=1" in tmux("capture-pane", "-p", "-t", home)  # caller's env
        assert "COCKPIT_T" not in a._tmux("show-environment", "-g", check=False).stdout
        assert start(alive=True) == 0 and len(asked) == 1  # running: untouched
        a._tmux("send-keys", "-t", home, "C-d")  # the secretary exits
        wait_for("#{pane_dead}", "1")
        assert start(alive=True) == 0 and asked[-1] == ("relaunch", True)
        wait_for("#{pane_dead}", "0")
        assert tmux("display-message", "-p", "-t", "=org:^", "#{pane_id}") == home
    finally:
        a.kill_server()  # this test's own socket only


# --------------------------------------------------------------- secretary
def _cockpit_args(*extra: str):
    from claude_org_runtime.cli import build_parser
    return build_parser().parse_args(["org", "cockpit", *extra])


def test_live_daemon_without_a_marked_pane_starts_nothing(adapter) -> None:
    # The secretary runs outside the cockpit: adopting it later would fence it.
    adapter._fake.queue((0, "%1\t1\t\t0\tzsh\n", ""))
    assert cockpit.start_secretary(adapter, _cockpit_args(), env={}, cwd="/r",
                                   daemon_alive=lambda: True) == 0
    assert [_args(c)[0] for c in adapter._fake.calls] == ["list-panes"]


def test_failed_respawn_unmarks_the_pane(adapter) -> None:
    adapter._fake.queue((0, "%1\t1\t\t0\tzsh\n", ""), (0, "", ""), (1, "", "boom"))
    assert cockpit.start_secretary(adapter, _cockpit_args(), env={}, cwd="/r",
                                   daemon_alive=lambda: False) == 1
    assert _args(adapter._fake.last) == ["set-option", "-pu", "-t", "%1", "@org_secretary"]


@pytest.mark.skipif(os.name == "nt", reason="fcntl lock, POSIX only like tmux")
def test_secretary_runs_under_the_state_dir_lock(adapter, monkeypatch, tmp_path) -> None:
    import fcntl
    seen = []

    def start(a, args, **kw):
        with open(tmp_path / "cockpit.lock", "w") as fh, pytest.raises(BlockingIOError):
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        seen.append((kw["env"], kw["cwd"]))
        return 0

    monkeypatch.delenv("ORG_BACKEND", raising=False)
    monkeypatch.setattr(cockpit, "_adapter", lambda: adapter)
    monkeypatch.setattr(cockpit, "start_secretary", start)
    assert cockpit.cmd_cockpit(_cockpit_args("--no-attach", "--state-dir", str(tmp_path))) == 0
    assert seen and seen[0][1] == os.getcwd()
    with open(tmp_path / "cockpit.lock", "w") as fh:  # released afterwards
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_a_running_daemon_backend_wins(adapter, monkeypatch, tmp_path, capsys) -> None:
    (tmp_path / "daemon.json").write_text('{"backend": "herdr"}', encoding="utf-8")
    monkeypatch.setattr(cockpit, "_adapter", lambda: adapter)
    monkeypatch.setattr(cockpit, "_daemon_alive", lambda sd, owner: True)
    assert cockpit.cmd_cockpit(_cockpit_args("--backend", "tmux", "--state-dir",
                                             str(tmp_path))) == 2
    assert "'herdr'" in capsys.readouterr().err and not adapter._fake.calls


def test_a_stale_sidecar_backend_is_ignored(monkeypatch, tmp_path) -> None:
    (tmp_path / "daemon.json").write_text('{"backend": "herdr"}', encoding="utf-8")
    monkeypatch.setattr(cockpit, "_daemon_alive", lambda sd, owner: False)
    args = _cockpit_args("--backend", "tmux", "--state-dir", str(tmp_path))
    assert cockpit._backend(args, str(tmp_path)) == "tmux"


def test_no_daemon_is_not_alive(tmp_path) -> None:
    assert cockpit._daemon_alive(str(tmp_path), "secretary") is False


def test_flags_round_trip_through_org_up_and_adopt() -> None:
    from claude_org_runtime.cli import build_parser
    args = _cockpit_args("--model", "opus", "--claude-arg=--verbose", "--claude-arg=-x",
                         "--reap", "--name", "sec")
    up = cockpit.secretary_command("launch", args, daemon_alive=True)
    parsed = build_parser().parse_args(up[up.index("org"):])
    assert (parsed.cmd, parsed.model, parsed.claude_arg, parsed.reap, parsed.name) == \
        ("up", "opus", ["--verbose", "-x"], True, "sec")
    adopt = cockpit.secretary_command("relaunch", args, daemon_alive=True)
    parsed = build_parser().parse_args(adopt[adopt.index("org"):])
    assert (parsed.cmd, parsed.continue_session, parsed.owner) == ("adopt", True, "sec")


def test_ensure_names_attached_tabs_after_the_agent(adapter) -> None:
    """Issue #259: --attach-only links get the agent name, pinned."""
    windows = "org\t@0\t\thome\toff\nclaude-org-broker-1-2\t@5\tfix-2\tclaude\ton\n"
    adapter._fake.queue((0, "", ""), (0, "", ""), (0, windows, ""), (0, "1\n", ""))
    cockpit.ensure_cockpit(adapter)
    calls = [_args(c) for c in adapter._fake.calls]
    i = calls.index(["rename-window", "-t", "@5", "fix-2", ";",
                     "set-option", "-w", "-t", "@5", "automatic-rename", "off"])
    assert calls[i + 1][0] == "link-window"


def test_ensure_reads_tmux_numeric_automatic_rename(adapter) -> None:
    """Issue #263: tmux formats the flag as 0/1; only the unpinned one is fixed."""
    windows = "org\t@0\t\thome\t0\norg\t@3\tfix-1\tfix-1\t0\norg\t@4\tfix-2\tfix-2\t1\n"
    adapter._fake.queue((0, "", ""), (0, "", ""), (0, windows, ""), (0, "1\n", ""))
    cockpit.ensure_cockpit(adapter)
    renamed = [_args(c)[3] for c in adapter._fake.calls if "rename-window" in c]
    assert renamed == ["@4"]
