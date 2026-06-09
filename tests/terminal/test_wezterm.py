"""Tests for ``claude_org_runtime.terminal.wezterm.WezTermAdapter``.

The live Windows behaviour runs in the fork harness (needs a real WezTerm
GUI). Here ``subprocess.run`` is stubbed to pin the ``wezterm cli`` command
construction: the always-present ``--pane-id`` target, the bracketed-paste vs
``--no-paste`` distinction (the WezTerm-specific keystroke ritual), the
``spawn`` window/tab back-fill and ``list`` JSON parsing.
"""

from __future__ import annotations

import json

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


def test_spawn_without_new_window(adapter: WezTermAdapter) -> None:
    adapter._fake.queue(
        (0, "7\n", ""),
        (0, json.dumps([{"pane_id": 7, "tab_id": 0, "window_id": 0}]), ""),
    )
    adapter.spawn(["cat"], new_window=False)
    assert "--new-window" not in _args(adapter._fake.calls[0])


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
