"""Tests for ``claude_org_runtime.terminal.tmux.TmuxAdapter``.

The live AC-1/AC-2 behaviour runs in the fork harness (needs a real tmux
server). Here ``subprocess.run`` is stubbed so the tests pin the *command
construction* — the flags, ``-t`` target plumbing, bracketed-paste ritual and
``list-panes`` parsing that must survive the flat-spike -> package port — plus
the benign-vs-fatal error policy of ``list_panes``.
"""

from __future__ import annotations

import subprocess

import pytest

from claude_org_runtime.terminal import tmux as tmux_mod
from claude_org_runtime.terminal.tmux import TmuxAdapter

# A full list-panes -F record (11 tab fields in _LIST_FIELDS order).
_PANE_LINE = "%1\t@0\tspike-1-1\t0\t0\t220\t50\t5\t10\t1\t12345"


@pytest.fixture
def adapter(monkeypatch: pytest.MonkeyPatch, fake_run) -> TmuxAdapter:
    monkeypatch.setattr(tmux_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(tmux_mod.time, "sleep", lambda _s: None)
    a = TmuxAdapter(exe="tmux")  # skip find_tmux PATH probe
    a._fake = fake_run  # type: ignore[attr-defined]  (test handle)
    return a


def _args(call: list[str]) -> list[str]:
    # Strip the [exe, "-L", socket] prefix the adapter always prepends.
    assert call[:3] == ["tmux", "-L", "claude-org-spike"]
    return call[3:]


# --------------------------------------------------------------------------
# send-keys primitives
# --------------------------------------------------------------------------

def test_send_enter(adapter: TmuxAdapter) -> None:
    adapter.send_enter("%1")
    assert _args(adapter._fake.last) == ["send-keys", "-t", "%1", "Enter"]


def test_send_interrupt(adapter: TmuxAdapter) -> None:
    adapter.send_interrupt("%1")
    assert _args(adapter._fake.last) == ["send-keys", "-t", "%1", "C-c"]


def test_type_text_uses_bracketed_paste(adapter: TmuxAdapter) -> None:
    adapter.type_text("%1", "line one\nline two")
    set_buf, paste = adapter._fake.calls
    sb = _args(set_buf)
    pb = _args(paste)
    # set-buffer -b <buf> -- <text>  (literal text, not interpreted as keys)
    assert sb[0] == "set-buffer" and sb[1] == "-b"
    assert sb[-2:] == ["--", "line one\nline two"]
    # paste-buffer -t %1 -b <buf> -p -d   (-p bracketed, -d cleans the buffer)
    assert pb[0] == "paste-buffer"
    assert pb[1:3] == ["-t", "%1"]
    assert "-p" in pb and "-d" in pb
    # both reference the same generated buffer name
    assert sb[2] == pb[4]


def test_send_line_literal_then_enter(adapter: TmuxAdapter) -> None:
    adapter.send_line("%1", "hello")
    first, second = adapter._fake.calls
    assert _args(first) == ["send-keys", "-t", "%1", "-l", "--", "hello"]
    assert _args(second) == ["send-keys", "-t", "%1", "Enter"]


# --------------------------------------------------------------------------
# capture-pane
# --------------------------------------------------------------------------

def test_get_text_default(adapter: TmuxAdapter) -> None:
    adapter._fake.queue((0, "screen contents", ""))
    out = adapter.get_text("%1")
    assert out == "screen contents"
    assert _args(adapter._fake.last) == ["capture-pane", "-t", "%1", "-p"]


def test_get_text_with_escapes(adapter: TmuxAdapter) -> None:
    adapter.get_text("%1", escapes=True)
    assert _args(adapter._fake.last) == ["capture-pane", "-t", "%1", "-p", "-e"]


# --------------------------------------------------------------------------
# spawn
# --------------------------------------------------------------------------

def test_spawn_constructs_new_session(adapter: TmuxAdapter) -> None:
    adapter._fake.queue((0, "%2\t@1\tspike-9-1", ""))
    ref = adapter.spawn(["claude", "--flag"], cwd="/work/dir")
    assert (ref.pane_id, ref.window_id, ref.tab_id) == ("%2", "@1", "@1")
    args = _args(adapter._fake.last)
    assert args[0] == "new-session"
    assert "-d" in args
    assert ["-x", "220"] == args[args.index("-x"):args.index("-x") + 2]
    assert ["-y", "50"] == args[args.index("-y"):args.index("-y") + 2]
    assert ["-c", "/work/dir"] == args[args.index("-c"):args.index("-c") + 2]
    # argv is shlex-joined into the trailing shell-command string.
    assert args[-1] == "claude --flag"


def test_spawn_without_cwd_omits_c_flag(adapter: TmuxAdapter) -> None:
    adapter._fake.queue((0, "%3\t@2\tspike-9-2", ""))
    adapter.spawn(["cat"])
    assert "-c" not in _args(adapter._fake.last)


# --------------------------------------------------------------------------
# list-panes parsing + error policy
# --------------------------------------------------------------------------

def test_list_panes_parses_record(adapter: TmuxAdapter) -> None:
    adapter._fake.queue((0, _PANE_LINE + "\n", ""))
    panes = adapter.list_panes()
    assert len(panes) == 1
    rec = panes[0]
    assert rec["pane_id"] == "%1"
    assert rec["window_id"] == "@0"
    assert rec["session"] == "spike-1-1"
    # geometry / cursor / pid coerced to int
    for k in ("left", "top", "width", "height", "cursor_x", "cursor_y", "pane_pid"):
        assert isinstance(rec[k], int)
    assert rec["width"] == 220 and rec["height"] == 50
    assert rec["active"] is True  # "1" -> bool


def test_list_panes_skips_malformed_rows(adapter: TmuxAdapter) -> None:
    adapter._fake.queue((0, "too\tfew\tfields\n" + _PANE_LINE + "\n", ""))
    panes = adapter.list_panes()
    assert len(panes) == 1  # the short row is dropped, the valid one kept


@pytest.mark.parametrize(
    "stderr",
    [
        "no server running on /tmp/sock",
        "error connecting to /tmp/sock (No such file or directory)",
        "no sessions",
    ],
)
def test_list_panes_benign_errors_return_empty(adapter: TmuxAdapter, stderr: str) -> None:
    adapter._fake.queue((1, "", stderr))
    assert adapter.list_panes() == []


def test_list_panes_fatal_error_raises(adapter: TmuxAdapter) -> None:
    # A non-benign failure must NOT be silently flattened to [] — otherwise
    # pane_exists() would misread "backend unreachable" as "pane missing".
    adapter._fake.queue((1, "", "permission denied"))
    with pytest.raises(RuntimeError):
        adapter.list_panes()


def test_pane_exists_uses_list(adapter: TmuxAdapter) -> None:
    adapter._fake.queue((0, _PANE_LINE + "\n", ""))
    assert adapter.pane_exists("%1") is True
    adapter._fake.queue((0, _PANE_LINE + "\n", ""))
    assert adapter.pane_exists("%99") is False


# --------------------------------------------------------------------------
# _tmux check policy
# --------------------------------------------------------------------------

def test_tmux_raises_on_checked_nonzero(adapter: TmuxAdapter) -> None:
    adapter._fake.queue((2, "", "boom"))
    with pytest.raises(RuntimeError):
        adapter.send_enter("%1")  # send_enter uses default check=True


def test_kill_pane_is_unchecked(adapter: TmuxAdapter) -> None:
    # kill_pane passes check=False, so a nonzero exit must not raise.
    adapter._fake.queue((1, "", "no such pane"))
    adapter.kill_pane("%1")  # should not raise
    assert _args(adapter._fake.last) == ["kill-pane", "-t", "%1"]


def test_find_tmux_raises_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tmux_mod.shutil, "which", lambda _n: None)
    with pytest.raises(FileNotFoundError):
        tmux_mod.find_tmux()
