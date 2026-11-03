"""Acceptance test: ``org cockpit`` starts or recovers the secretary in home.

One command both starts the org and reconnects (Issue #250 follow-up). tmux is
faked (no tmux server, network or HOME needed). New symbols are looked up
inside the tests and asserted, so this file imports cleanly on the code before
the change and fails there by assertion.
"""

from __future__ import annotations

import pytest

from claude_org_runtime.terminal import cockpit
from claude_org_runtime.terminal import tmux as tmux_mod
from claude_org_runtime.terminal.tmux import TmuxAdapter

MARK = "@org_secretary"
NO_STATE = "/nonexistent/claude-org-cockpit-acceptance"


def _start():
    fn = getattr(cockpit, "start_secretary", None)
    assert fn is not None, "cockpit.start_secretary is missing"
    return fn


def _args(*extra: str):
    from claude_org_runtime.cli import build_parser
    return build_parser().parse_args(["org", "cockpit", *extra])


@pytest.fixture
def adapter(monkeypatch: pytest.MonkeyPatch, fake_run) -> TmuxAdapter:
    monkeypatch.setattr(tmux_mod.subprocess, "run", fake_run)
    monkeypatch.delenv("ORG_BACKEND", raising=False)
    a = TmuxAdapter(exe="tmux")
    a._fake = fake_run  # type: ignore[attr-defined]
    return a


def _calls(a) -> list[list[str]]:
    return [c[3:] for c in a._fake.calls]


def _respawns(a) -> list[list[str]]:
    return [c for c in _calls(a) if c and c[0] == "respawn-pane"]


def _command_after(argv: list[str], *words: str) -> list[str]:
    for i in range(len(argv) - len(words) + 1):
        if argv[i:i + len(words)] == list(words):
            return argv[i + len(words):]
    raise AssertionError(f"{' '.join(words)!r} not in {argv}")


def _parsed(sub: str, tail: list[str]):
    """Parse a forwarded command tail with the real parser (flag form-agnostic)."""
    from claude_org_runtime.cli import build_parser
    return build_parser().parse_args(["org", sub, *tail])


def _panes(*rows: str) -> tuple[int, str, str]:
    # pane_id, @cockpit_home, @org_secretary, pane_dead, pane_current_command
    return (0, "".join(r + "\n" for r in rows), "")


ENV = {"PATH": "/x/bin", "FOO": "bar baz", "TMUX": "/s,1,0", "TMUX_PANE": "%9"}


def test_idle_home_shell_starts_the_secretary_with_org_up(adapter) -> None:
    start = _start()
    adapter._fake.queue(_panes("%1\t1\t\t0\tzsh", "%7\t\t\t0\tclaude"))
    rc = start(adapter, _args("--model", "opus", "--state-dir", "/s"),
               env=ENV, cwd="/repo", daemon_alive=lambda: False)
    assert rc == 0
    calls = _calls(adapter)
    (respawn,) = _respawns(adapter)
    mark = ["set-option", "-p", "-t", "%1", MARK, "1"]
    assert mark in calls and calls.index(mark) < calls.index(respawn), \
        "the pane must be marked before it is respawned"
    assert "-k" in respawn and respawn[respawn.index("-t") + 1] == "%1"
    assert respawn[respawn.index("-c") + 1] == "/repo"
    assert "PATH=/x/bin" in respawn and "FOO=bar baz" in respawn
    assert not any(x.startswith(("TMUX=", "TMUX_PANE=")) for x in respawn)
    up = _parsed("up", _command_after(respawn, "org", "up"))
    assert up.model == "opus" and up.state_dir == "/s"


def test_live_marked_secretary_is_left_alone(adapter) -> None:
    start = _start()
    for cmd in ("claude", "zsh"):  # also during its login-shell phase
        adapter._fake.calls.clear()
        adapter._fake.responses.clear()
        adapter._fake._idx = 0
        adapter._fake.queue(_panes(f"%1\t1\t1\t0\t{cmd}", "%2\t1\t\t0\tzsh"))
        assert start(adapter, _args(), env=ENV, cwd="/repo",
                     daemon_alive=lambda: True) == 0
        assert not any(c[0] in ("respawn-pane", "set-option", "kill-pane")
                       for c in _calls(adapter))


def test_exited_secretary_with_live_daemon_is_adopted(adapter) -> None:
    start = _start()
    adapter._fake.queue(_panes("%1\t1\t1\t1\tclaude"))
    assert start(adapter, _args("--state-dir", "/s", "--claude-arg=--verbose"),
                 env=ENV, cwd="/repo", daemon_alive=lambda: True) == 0
    (respawn,) = _respawns(adapter)
    assert "-k" not in respawn, "a marked pane is never killed"
    adopt = _parsed("adopt", _command_after(respawn, "org", "adopt"))
    assert adopt.continue_session and adopt.state_dir == "/s"
    assert adopt.claude_arg == ["--verbose"]


def test_exited_secretary_without_daemon_runs_org_up(adapter) -> None:
    start = _start()
    adapter._fake.queue(_panes("%1\t1\t1\t1\tclaude"))
    assert start(adapter, _args(), env=ENV, cwd="/repo",
                 daemon_alive=lambda: False) == 0
    (respawn,) = _respawns(adapter)
    assert "-k" not in respawn
    _command_after(respawn, "org", "up")


def test_ambiguous_home_starts_nothing(adapter) -> None:
    start = _start()
    for rows in (("%1\t1\t1\t0\tclaude", "%2\t1\t1\t0\tclaude"),  # two marked
                 ("%3\t1\t\t0\tpython3", "%4\t\t\t0\tzsh")):    # no idle home shell
        adapter._fake.calls.clear()
        adapter._fake.responses.clear()
        adapter._fake._idx = 0
        adapter._fake.queue(_panes(*rows))
        assert start(adapter, _args(), env=ENV, cwd="/repo",
                     daemon_alive=lambda: True) != 0
        assert not _respawns(adapter)


def test_attach_only_never_starts_the_secretary(adapter, monkeypatch) -> None:
    _start()
    execs: list[list[str]] = []
    monkeypatch.setattr(cockpit, "_adapter", lambda: adapter)
    monkeypatch.setattr(cockpit.os, "execvpe", lambda f, argv, env: execs.append(argv))
    cockpit.cmd_cockpit(_args("--attach-only", "--state-dir", NO_STATE))
    assert not any(c[0] in ("list-panes", "respawn-pane") for c in _calls(adapter))
    assert execs and "attach-session" in execs[0]


def test_non_tmux_backend_is_a_clear_error(adapter, monkeypatch, capsys) -> None:
    _start()
    made: list[int] = []
    monkeypatch.setattr(cockpit, "_adapter", lambda: made.append(1) or adapter)
    monkeypatch.setenv("ORG_BACKEND", "herdr")
    rc = cockpit.cmd_cockpit(_args("--no-attach", "--state-dir", NO_STATE))
    assert rc != 0
    assert "tmux" in capsys.readouterr().err
    assert not made and not adapter._fake.calls


def test_exited_secretary_with_daemon_but_no_bind_runs_org_up(adapter) -> None:
    # Daemon is up but holds no live bind for the owner: adopt would die with
    # [unknown_owner], so the cockpit must start a fresh secretary instead.
    start = _start()
    adapter._fake.queue(_panes("%1\t1\t1\t1\tclaude"))
    import inspect
    extra = ({"adoptable": lambda: False}
             if "adoptable" in inspect.signature(start).parameters else {})
    assert start(adapter, _args(), env=ENV, cwd="/repo",
                 daemon_alive=lambda: True, **extra) == 0
    (respawn,) = _respawns(adapter)
    _command_after(respawn, "org", "up")
