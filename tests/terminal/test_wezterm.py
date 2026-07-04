"""Tests for ``claude_org_runtime.terminal.wezterm.WezTermAdapter``.

The live Windows behaviour runs in the fork harness (needs a real WezTerm
GUI). Here ``subprocess.run`` is stubbed to pin the ``wezterm cli`` command
construction: the always-present ``--pane-id`` target, the bracketed-paste vs
``--no-paste`` distinction (the WezTerm-specific keystroke ritual), the
``spawn`` window/tab back-fill and ``list`` JSON parsing.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from claude_org_runtime.terminal import wezterm as wez_mod
from claude_org_runtime.terminal.wezterm import WezTermAdapter


@pytest.fixture
def adapter(monkeypatch: pytest.MonkeyPatch, fake_run) -> WezTermAdapter:
    monkeypatch.setattr(wez_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(wez_mod.time, "sleep", lambda _s: None)
    a = WezTermAdapter(exe="wezterm")  # skip find_wezterm PATH probe
    a._fake = fake_run  # type: ignore[attr-defined]
    return a


def _args(call: list[str]) -> list[str]:
    assert call[:3] == ["wezterm", "cli", "--no-auto-start"]
    return call[3:]


# --------------------------------------------------------------------------
# CREATE_NO_WINDOW guard (Windows window-flicker fix)
# --------------------------------------------------------------------------

@pytest.mark.skipif(
    not hasattr(subprocess, "CREATE_NO_WINDOW"),
    reason="CREATE_NO_WINDOW is a Windows-only subprocess constant",
)
def test_cli_passes_create_no_window_on_windows(
    adapter: WezTermAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    # wezterm.exe is a GUI-subsystem binary, so every `cli` call (fired on each
    # monitoring poll / message exchange) flashes a console window on Windows.
    # CREATE_NO_WINDOW must be passed to subprocess.run to suppress it.
    monkeypatch.setattr(wez_mod.os, "name", "nt")
    adapter.get_text(5)
    assert (
        adapter._fake.last_kwargs.get("creationflags")
        == subprocess.CREATE_NO_WINDOW
    )


def test_cli_omits_creationflags_on_posix(
    adapter: WezTermAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    # On POSIX there is no window to suppress; creationflags must not be passed
    # (CREATE_NO_WINDOW is a Windows-only constant).
    monkeypatch.setattr(wez_mod.os, "name", "posix")
    adapter.get_text(5)
    assert "creationflags" not in adapter._fake.last_kwargs


# --------------------------------------------------------------------------
# send-text family
# --------------------------------------------------------------------------

def test_send_text_paste_default(adapter: WezTermAdapter) -> None:
    adapter.send_text(5, "hello")
    # default = bracketed paste: no --no-paste flag
    assert _args(adapter._fake.last) == ["send-text", "--pane-id", "5", "--", "hello"]


def test_send_text_no_paste(adapter: WezTermAdapter) -> None:
    adapter.send_text(5, "\r", no_paste=True)
    assert _args(adapter._fake.last) == [
        "send-text", "--pane-id", "5", "--no-paste", "--", "\r",
    ]


def test_send_enter_is_raw_cr(adapter: WezTermAdapter) -> None:
    adapter.send_enter(5)
    # Enter == raw CR with --no-paste so it is interpreted as a keypress.
    assert _args(adapter._fake.last) == [
        "send-text", "--pane-id", "5", "--no-paste", "--", "\r",
    ]


def test_send_interrupt_is_raw_etx(adapter: WezTermAdapter) -> None:
    adapter.send_interrupt(5)
    assert _args(adapter._fake.last) == [
        "send-text", "--pane-id", "5", "--no-paste", "--", "\x03",
    ]


def test_type_text_is_paste(adapter: WezTermAdapter) -> None:
    adapter.type_text(5, "multi\nline")
    assert _args(adapter._fake.last) == [
        "send-text", "--pane-id", "5", "--", "multi\nline",
    ]


# --------------------------------------------------------------------------
# send_named_keys (raw-key vocabulary subset, Issue #108)
# --------------------------------------------------------------------------

def test_supported_named_keys_is_enter_and_ctrl_c_subset() -> None:
    # WezTerm declares the subset it can already emit, not the full vocabulary.
    from claude_org_runtime.terminal.keys import CANONICAL_KEYS

    assert WezTermAdapter.supported_named_keys == frozenset({"enter", "ctrl+c"})
    # drift guard: every declared key must be a real canonical key (parity with
    # the tmux/herdr map guards), so a rename in keys.py can't silently desync it.
    assert WezTermAdapter.supported_named_keys <= CANONICAL_KEYS


def test_send_named_keys_subset_routes_to_cr_and_etx(adapter: WezTermAdapter) -> None:
    adapter.send_named_keys(5, ["enter", "ctrl+c"])
    first, second = adapter._fake.calls
    assert _args(first) == ["send-text", "--pane-id", "5", "--no-paste", "--", "\r"]
    assert _args(second) == ["send-text", "--pane-id", "5", "--no-paste", "--", "\x03"]


def test_send_named_keys_rejects_out_of_subset_key(adapter: WezTermAdapter) -> None:
    # Defensive: broker preflight normally blocks this, but the adapter must
    # not silently drop an unsupported canonical key.
    with pytest.raises(ValueError):
        adapter.send_named_keys(5, ["up"])


def test_send_named_keys_mixed_batch_is_all_or_nothing(adapter: WezTermAdapter) -> None:
    # A supported key BEFORE an unsupported one must not be emitted: the whole
    # batch is validated first, so nothing reaches the pty (all-or-nothing).
    with pytest.raises(ValueError):
        adapter.send_named_keys(5, ["enter", "up"])
    assert adapter._fake.calls == []


def test_send_line_pastes_then_enters(adapter: WezTermAdapter) -> None:
    adapter.send_line(5, "a line")
    first, second = adapter._fake.calls
    # body via paste...
    assert _args(first) == ["send-text", "--pane-id", "5", "--", "a line"]
    # ...then the confirming CR via --no-paste.
    assert _args(second) == ["send-text", "--pane-id", "5", "--no-paste", "--", "\r"]


# --------------------------------------------------------------------------
# get-text
# --------------------------------------------------------------------------

def test_get_text_default(adapter: WezTermAdapter) -> None:
    adapter._fake.queue((0, "the screen", ""))
    assert adapter.get_text(5) == "the screen"
    assert _args(adapter._fake.last) == ["get-text", "--pane-id", "5"]


def test_get_text_with_escapes(adapter: WezTermAdapter) -> None:
    adapter.get_text(5, escapes=True)
    assert _args(adapter._fake.last) == ["get-text", "--pane-id", "5", "--escapes"]


# --------------------------------------------------------------------------
# list / spawn
# --------------------------------------------------------------------------

def test_list_panes_parses_json(adapter: WezTermAdapter) -> None:
    payload = [{"pane_id": 5, "tab_id": 2, "window_id": 1}]
    adapter._fake.queue((0, json.dumps(payload), ""))
    panes = adapter.list_panes()
    assert panes == payload
    assert _args(adapter._fake.last) == ["list", "--format", "json"]


def test_spawn_backfills_window_and_tab(adapter: WezTermAdapter) -> None:
    # first call (spawn) returns the new pane id; second call (list) supplies
    # tab_id / window_id which the adapter back-fills into the PaneRef.
    adapter._fake.queue(
        (0, "5\n", ""),
        (0, json.dumps([{"pane_id": 5, "tab_id": 2, "window_id": 1}]), ""),
    )
    ref = adapter.spawn(["claude", "--flag"], cwd="/work")
    assert (ref.pane_id, ref.tab_id, ref.window_id) == (5, 2, 1)
    spawn_args = _args(adapter._fake.calls[0])
    assert spawn_args[0] == "spawn"
    assert "--new-window" in spawn_args
    assert ["--cwd", "/work"] == spawn_args[
        spawn_args.index("--cwd"):spawn_args.index("--cwd") + 2
    ]
    # argv passed verbatim after the -- separator
    assert spawn_args[-3:] == ["--", "claude", "--flag"]


def test_spawn_injects_env_via_posix_env_prefix(
    adapter: WezTermAdapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Issue #122: WezTerm mux は cli client の env を pane に届けないため、起動 argv 自体に
    # env 設定を織り込む。POSIX: `env KEY=VALUE <argv>` を -- の後に置く。
    monkeypatch.setattr(wez_mod.os, "name", "posix")
    adapter._fake.queue(
        (0, "5\n", ""),
        (0, json.dumps([{"pane_id": 5, "tab_id": 2, "window_id": 1}]), ""),
    )
    adapter.spawn(["claude", "--flag"], env={"ORG_BROKER_STATE_DIR": "/abs/state"})
    spawn_args = _args(adapter._fake.calls[0])
    sep = spawn_args.index("--")
    assert spawn_args[sep + 1:] == [
        "env", "ORG_BROKER_STATE_DIR=/abs/state", "claude", "--flag"
    ]


def test_spawn_carries_venv_login_shell_wrapper_posix(
    adapter: WezTermAdapter, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Issue #130: on POSIX the broker wraps argv in a post-profile login shell
    # (venv_pane_prep). wezterm's `_env_wrapped_argv` then prefixes `env
    # VIRTUAL_ENV=...` in front of that wrapper, so the pane gets both the
    # VIRTUAL_ENV and the `export PATH=<venv>/bin` after `--`.
    from claude_org_runtime.terminal.base import venv_pane_prep

    monkeypatch.setattr(wez_mod.os, "name", "posix")
    monkeypatch.setenv("SHELL", "/bin/bash")
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python").write_text("")
    wrapped, env = venv_pane_prep(["claude", "--flag"], str(tmp_path), None)

    adapter._fake.queue(
        (0, "5\n", ""),
        (0, json.dumps([{"pane_id": 5, "tab_id": 2, "window_id": 1}]), ""),
    )
    adapter.spawn(wrapped, env=env)
    spawn_args = _args(adapter._fake.calls[0])
    after = spawn_args[spawn_args.index("--") + 1:]
    # env prefix carries VIRTUAL_ENV, then the login-shell PATH-prepend wrapper.
    assert after[0] == "env"
    assert f"VIRTUAL_ENV={tmp_path}/.venv" in after
    assert "/bin/bash" in after and "-lc" in after
    assert any(f'export PATH={tmp_path}/.venv/bin:"$PATH"' in tok for tok in after)
    assert after[-2:] == ["claude", "--flag"]


def test_env_wrapped_argv_windows_uses_cmd_set(monkeypatch: pytest.MonkeyPatch) -> None:
    # native Windows には `env` が無いので `cmd /d /c set "K=V"&& <argv>` で注入する。
    # helper を直接検証する (os.name='nt' を spawn 経路に流すと GUI-suppress の
    # CREATE_NO_WINDOW 分岐が非 Windows で AttributeError になるため)。
    monkeypatch.setattr(wez_mod.os, "name", "nt")
    wrapped = wez_mod._env_wrapped_argv(
        ["claude", "--flag"], {"ORG_BROKER_STATE_DIR": r"C:\st ate"}
    )
    assert wrapped[:3] == ["cmd", "/d", "/c"]
    assert wrapped[3].startswith('set "ORG_BROKER_STATE_DIR=C:\\st ate"&& ')
    assert "claude" in wrapped[3]


def test_env_wrapped_argv_windows_escapes_cmd_metachars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Injection guard (#122): a caller-supplied argv value containing a cmd
    # separator ('&') must be caret-escaped so cmd.exe cannot run an injected
    # command. The intentional '&&' after `set` must stay intact.
    monkeypatch.setattr(wez_mod.os, "name", "nt")
    wrapped = wez_mod._env_wrapped_argv(
        ["claude", "--append-system-prompt", "x & calc"],
        {"ORG_BROKER_STATE_DIR": "/s"},
    )
    line = wrapped[3]
    assert line.startswith('set "ORG_BROKER_STATE_DIR=/s"&& ')
    prog = line.split("&& ", 1)[1]
    # every '&' in the program portion is caret-escaped (never a bare separator);
    # list2cmdline quotes the spaced value, and those quotes are caret-escaped too.
    assert "^&" in prog
    for i, ch in enumerate(prog):
        if ch == "&":
            assert i > 0 and prog[i - 1] == "^", f"bare '&' at {i} in {prog!r}"
    assert '^"' in prog
    # 空 / None は argv をそのまま返す (両 OS 分岐で従来挙動)。
    monkeypatch.setattr(wez_mod.os, "name", "nt")
    assert wez_mod._env_wrapped_argv(["claude"], None) == ["claude"]
    monkeypatch.setattr(wez_mod.os, "name", "posix")
    assert wez_mod._env_wrapped_argv(["claude"], {}) == ["claude"]


def test_spawn_without_env_leaves_argv_verbatim(adapter: WezTermAdapter) -> None:
    adapter._fake.queue(
        (0, "5\n", ""),
        (0, json.dumps([{"pane_id": 5, "tab_id": 2, "window_id": 1}]), ""),
    )
    adapter.spawn(["claude", "--flag"])  # no env -> unchanged (backward compat)
    spawn_args = _args(adapter._fake.calls[0])
    assert spawn_args[-3:] == ["--", "claude", "--flag"]


def test_spawn_without_new_window(adapter: WezTermAdapter) -> None:
    adapter._fake.queue(
        (0, "7\n", ""),
        (0, json.dumps([{"pane_id": 7, "tab_id": 0, "window_id": 0}]), ""),
    )
    adapter.spawn(["cat"], new_window=False)
    assert "--new-window" not in _args(adapter._fake.calls[0])


# --------------------------------------------------------------------------
# anchor-window tab consolidation (Issue #86)
# --------------------------------------------------------------------------

def test_first_child_spawns_new_window_and_anchors(adapter: WezTermAdapter) -> None:
    # The very first child (no anchor yet) opens a new window and records its
    # window_id as the anchor for subsequent children.
    assert adapter._anchor_window_id is None
    adapter._fake.queue(
        (0, "5\n", ""),
        (0, json.dumps([{"pane_id": 5, "tab_id": 2, "window_id": 1}]), ""),
    )
    adapter.spawn(["claude"])
    assert "--new-window" in _args(adapter._fake.calls[0])
    assert adapter._anchor_window_id == 1


def test_second_child_spawns_tab_into_anchor(adapter: WezTermAdapter) -> None:
    # Chain two real spawns: the first backfills window_id=1 and anchors; the
    # second reads that anchor, confirms it is alive, and spawns a tab via
    # --window-id 1 (and must NOT pass --new-window — they are exclusive).
    adapter._fake.queue(
        # first spawn (new window) + its backfill list
        (0, "5\n", ""),
        (0, json.dumps([{"pane_id": 5, "tab_id": 2, "window_id": 1}]), ""),
        # second spawn: alive-check list, spawn id, backfill list
        (0, json.dumps([{"pane_id": 5, "tab_id": 2, "window_id": 1}]), ""),
        (0, "6\n", ""),
        (0, json.dumps([{"pane_id": 6, "tab_id": 3, "window_id": 1}]), ""),
    )
    adapter.spawn(["claude"])
    ref = adapter.spawn(["claude"])
    # calls: [0]=spawn1, [1]=backfill1, [2]=alive-check, [3]=spawn2, [4]=backfill2
    tab_spawn = _args(adapter._fake.calls[3])
    assert "--new-window" not in tab_spawn
    idx = tab_spawn.index("--window-id")
    assert tab_spawn[idx:idx + 2] == ["--window-id", "1"]
    assert ref.window_id == 1


def test_spawn_falls_back_to_new_window_when_anchor_dead(
    adapter: WezTermAdapter,
) -> None:
    # If the anchor window has been killed, the alive-check list omits it and
    # the next child opens a fresh window (--new-window, no --window-id) and
    # re-anchors on the new window_id.
    adapter._fake.queue(
        # first spawn (new window, anchors window 1) + backfill
        (0, "5\n", ""),
        (0, json.dumps([{"pane_id": 5, "tab_id": 2, "window_id": 1}]), ""),
        # second spawn: alive-check list NO LONGER shows window 1
        (0, json.dumps([{"pane_id": 9, "tab_id": 0, "window_id": 2}]), ""),
        (0, "10\n", ""),
        (0, json.dumps([{"pane_id": 10, "tab_id": 0, "window_id": 3}]), ""),
    )
    adapter.spawn(["claude"])
    adapter.spawn(["claude"])
    # calls: [0]=spawn1, [1]=backfill1, [2]=alive-check, [3]=spawn2, [4]=backfill2
    fallback_spawn = _args(adapter._fake.calls[3])
    assert "--new-window" in fallback_spawn
    assert "--window-id" not in fallback_spawn
    assert adapter._anchor_window_id == 3  # re-anchored on the new window


def test_spawn_without_new_window_ignores_anchor(adapter: WezTermAdapter) -> None:
    # new_window=False is a pure passthrough: no consolidation, no anchor read
    # or write, and neither --new-window nor --window-id is emitted.
    adapter._anchor_window_id = 1
    adapter._fake.queue(
        (0, "7\n", ""),
        (0, json.dumps([{"pane_id": 7, "tab_id": 0, "window_id": 9}]), ""),
    )
    adapter.spawn(["cat"], new_window=False)
    spawn_args = _args(adapter._fake.calls[0])
    assert "--new-window" not in spawn_args
    assert "--window-id" not in spawn_args
    assert adapter._anchor_window_id == 1  # untouched


def test_concurrent_spawns_establish_single_anchor(
    adapter: WezTermAdapter,
) -> None:
    # The broker calls spawn() concurrently from ThreadingHTTPServer handlers
    # outside its own lock. Without the adapter's _spawn_lock, two spawns could
    # both observe _anchor_window_id is None and both emit --new-window. The
    # lock serialises the check->spawn->set so exactly one window is opened and
    # the rest become tabs. FakeRun's queue is consumed FIFO under the lock, so
    # whichever thread wins the lock first takes the new-window shape (2
    # responses) and the other takes the tab shape (3 responses).
    import threading as _t

    adapter._fake.queue(
        # first (lock winner): new window + backfill
        (0, "5\n", ""),
        (0, json.dumps([{"pane_id": 5, "tab_id": 2, "window_id": 1}]), ""),
        # second: alive-check, spawn, backfill
        (0, json.dumps([{"pane_id": 5, "tab_id": 2, "window_id": 1}]), ""),
        (0, "6\n", ""),
        (0, json.dumps([{"pane_id": 6, "tab_id": 3, "window_id": 1}]), ""),
    )
    start = _t.Barrier(2)

    def _spawn() -> None:
        start.wait()
        adapter.spawn(["claude"])

    threads = [_t.Thread(target=_spawn) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    spawn_cmds = [
        a for c in adapter._fake.calls if (a := _args(c))[0] == "spawn"
    ]
    assert len(spawn_cmds) == 2
    new_window = [a for a in spawn_cmds if "--new-window" in a]
    tabbed = [a for a in spawn_cmds if "--window-id" in a]
    assert len(new_window) == 1  # exactly one real window opened
    assert len(tabbed) == 1  # the other consolidated into a tab
    assert adapter._anchor_window_id == 1


def test_pane_exists(adapter: WezTermAdapter) -> None:
    payload = json.dumps([{"pane_id": 5, "tab_id": 2, "window_id": 1}])
    adapter._fake.queue((0, payload, ""))
    assert adapter.pane_exists(5) is True
    adapter._fake.queue((0, payload, ""))
    assert adapter.pane_exists(99) is False


# --------------------------------------------------------------------------
# error policy / exe discovery
# --------------------------------------------------------------------------

def test_cli_raises_on_checked_nonzero(adapter: WezTermAdapter) -> None:
    adapter._fake.queue((1, "", "boom"))
    with pytest.raises(RuntimeError):
        adapter.get_text(5)  # default check=True


def test_kill_pane_is_unchecked(adapter: WezTermAdapter) -> None:
    adapter._fake.queue((1, "", "no such pane"))
    adapter.kill_pane(5)  # check=False -> must not raise
    assert _args(adapter._fake.last) == ["kill-pane", "--pane-id", "5"]


def test_find_wezterm_prefers_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wez_mod.shutil, "which", lambda _n: "/usr/bin/wezterm")
    assert wez_mod.find_wezterm() == "/usr/bin/wezterm"


def test_find_wezterm_raises_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wez_mod.shutil, "which", lambda _n: None)
    monkeypatch.setattr(wez_mod.os.path, "exists", lambda _p: False)
    with pytest.raises(FileNotFoundError):
        wez_mod.find_wezterm()


def test_wezterm_is_not_isolated_session() -> None:
    # wezterm cli list は global mux を見せる (dedicated socket 分離なし) ため
    # isolated_session=False。broker は論理ペイン (窓口) を last-pane 計上しない。
    assert WezTermAdapter.isolated_session is False
