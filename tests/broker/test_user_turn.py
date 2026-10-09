# -*- coding: utf-8 -*-
"""send_message(deliver="user_turn") (Issue #163, renga #323 parity)."""

from __future__ import annotations

import json

import jsonschema
import pytest

from claude_org_runtime.broker import surface
from claude_org_runtime.broker.server import Broker
from claude_org_runtime.broker.surface import ToolArgError, dispatch_tool
from claude_org_runtime.broker.user_turn import (
    BUSY, DRAFT, EMPTY, MAX_BODY_BYTES, NOT_READY, assess_screen,
)
from claude_org_runtime.schema import broker_queue_event_schema

RULE = "─" * 40


def composer(content: str = "", footer: str = "  ? for shortcuts") -> str:
    return f"some transcript\n\n{RULE}\n❯ {content}\n{RULE}\n{footer}\n"


class ComposerAdapter:
    """Models a Claude composer and logs every PTY write."""

    bracketed_paste = False

    def __init__(self, screen: str | None = None, submit_works: bool = True) -> None:
        self.draft = ""
        self.fixed_screen = screen     # when set, the screen never changes
        self.submit_works = submit_works
        self.after_type: str | None = None  # screen to show once text is typed
        self.writes: list[tuple] = []
        self.read_error: Exception | None = None

    def get_text(self, pane_id, escapes=False) -> str:
        if self.read_error:
            raise self.read_error
        if self.fixed_screen is not None:
            return self.fixed_screen
        if self.after_type is not None and self.draft:
            return self.after_type
        return composer(self.draft)

    def type_text(self, pane_id, text) -> None:
        self.writes.append(("type", text))
        self.draft += text

    def send_enter(self, pane_id) -> None:
        self.writes.append(("enter",))
        if self.submit_works:
            self.draft = ""

    def send_line(self, pane_id, text, settle=0.15) -> None:
        self.writes.append(("line", text))


def make_broker(tmp_path, adapter, *, sender_role="dispatcher", kind="claude", pane_id=7):
    b = Broker(state_dir=tmp_path / "broker", adapter=adapter)
    b.user_turn_settle = 0.0
    b.user_turn_poll = 0.001
    b.user_turn_settle_timeout = 0.05
    b.user_turn_submit_timeout = 0.05
    src = b.issue_token("src", "src", sender_role)
    b.register_local(src)
    dst = b.issue_token("dst", "dst", "worker", pane_id=pane_id, kind=kind)
    b.register_local(dst)
    return b, b.get_bind(src)


def send(b, bind, message="/clear", deliver="user_turn", to="dst"):
    args = {"to_id": to, "message": message}
    if deliver is not None:
        args["deliver"] = deliver
    res = dispatch_tool(b, bind, "send_message", args)
    if res.get("isError"):
        return res
    return json.loads(res["content"][0]["text"])


# ---------------------------------------------------------------- predicate
@pytest.mark.parametrize("screen,want", [
    (composer(), EMPTY),
    (composer().replace("❯ ", "❯ "), EMPTY),          # NBSP after the glyph
    (composer("half typed"), DRAFT),
    (composer(footer="✻ Working… (esc to interrupt)"), BUSY),
    # permission menu: cursor row is an option, never an empty composer
    ("Do you want to proceed?\n❯ 1. Yes\n  2. No\n\nEnter to confirm · Esc to cancel", NOT_READY),
    ("Do you want to proceed?\n❯ 1. Yes\n  2. No\n", NOT_READY),
    (composer() + "\n ↑/↓ to navigate · Enter to select\n", NOT_READY),
    (f"{RULE}\n❯ \n(no closing rule)\n", NOT_READY),          # not fenced below
    ("❯ \n" + RULE, NOT_READY),                               # not fenced above
    ("$ ls\nfile\n", NOT_READY),
    ("", NOT_READY),
])
def test_assess_screen(screen, want):
    assert assess_screen(screen) == want


# ---------------------------------------------------------------- happy path
def test_user_turn_types_then_submits_with_separate_enter(tmp_path):
    a = ComposerAdapter()
    b, src = make_broker(tmp_path, a)
    res = send(b, src, "/clear")
    assert res == {"ok": True, "delivered_to": "dst", "deliver": "user_turn",
                   "status": "submitted"}
    assert a.writes == [("type", "/clear"), ("enter",)]
    assert not b._rows  # the queue is bypassed: no second delivery via channel


def test_multiline_needs_bracketed_paste(tmp_path):
    a = ComposerAdapter()
    a.bracketed_paste = True
    b, src = make_broker(tmp_path, a)
    assert send(b, src, "line one\nline two")["status"] == "submitted"


def test_journal_lines_validate(tmp_path):
    a = ComposerAdapter(submit_works=False)
    b, src = make_broker(tmp_path, a)
    send(b, src, "/clear")
    a.draft, a.submit_works = "", True
    send(b, src, "/loop 5m check")
    validator = jsonschema.Draft202012Validator(broker_queue_event_schema())
    events = []
    for ln in (tmp_path / "broker" / "queue.jsonl").read_text(encoding="utf-8").splitlines():
        rec = json.loads(ln)
        validator.validate(rec)
        events.append(rec["event"])
    assert "user_turn_stalled" in events and "user_turn_submitted" in events


# ---------------------------------------------------------------- refusals
_DRAFT = composer("someone's draft")
_BUSY = composer(footer="✻ Working… (esc to interrupt)")


@pytest.mark.parametrize("case,code", [
    ("busy", "user_turn_busy"),
    ("draft", "user_turn_not_ready"),
    ("dialog", "user_turn_not_ready"),
    ("unknown_screen", "user_turn_not_ready"),
    ("in_flight", "user_turn_not_ready"),
    ("codex", "user_turn_unsupported_target"),
    ("no_pane", "user_turn_unsupported_target"),
    ("no_adapter", "user_turn_unsupported_target"),
    ("read_error", "user_turn_unsupported_target"),
    ("empty_body", "user_turn_invalid_body"),
    ("control_char", "user_turn_invalid_body"),
    ("tab", "user_turn_invalid_body"),
    ("cr", "user_turn_invalid_body"),
    ("too_large", "user_turn_invalid_body"),
    ("multiline_no_paste", "user_turn_invalid_body"),
    ("unknown_peer", "peer_not_found"),
])
def test_refusal_writes_zero_bytes(tmp_path, case, code):
    a = ComposerAdapter()
    kw = {}
    body, to = "/clear", "dst"
    if case == "busy":
        a.fixed_screen = _BUSY
    elif case == "draft":
        a.fixed_screen = _DRAFT
    elif case == "dialog":
        a.fixed_screen = composer() + "\nEnter to confirm · Esc to exit\n"
    elif case == "unknown_screen":
        a.fixed_screen = "$ "
    elif case == "codex":
        kw["kind"] = "codex"
    elif case == "no_pane":
        kw["pane_id"] = None
    elif case == "read_error":
        a.read_error = RuntimeError("pane gone")
    elif case == "empty_body":
        body = "  \n "
    elif case == "control_char":
        body = "/clear\x1b[A"
    elif case == "tab":
        body = "a\tb"
    elif case == "cr":
        body = "/clear\r"
    elif case == "too_large":
        body = "x" * (MAX_BODY_BYTES + 1)
    elif case == "multiline_no_paste":
        body = "a\nb"
    elif case == "unknown_peer":
        to = "nobody"
    b, src = make_broker(tmp_path, a, **kw)
    if case == "no_adapter":
        b.adapter = None
    held = b._pane_write_lock("7")
    if case == "in_flight":
        held.acquire()
    try:
        res = send(b, src, body, to=to)
    finally:
        if case == "in_flight":
            held.release()
    assert res["ok"] is False
    assert res["error"].startswith(f"[{code}]"), res
    assert a.writes == []
    assert not b._rows


def test_worker_tier_cannot_use_user_turn(tmp_path):
    a = ComposerAdapter()
    b, src = make_broker(tmp_path, a, sender_role="worker")
    res = send(b, src, "/clear")
    assert res["isError"] is True
    assert res["content"][0]["text"].startswith("[tool_not_authorized]")
    assert a.writes == []
    # the channel path stays open to workers
    assert send(b, src, "hi", deliver=None)["ok"] is True


def test_invalid_deliver_value_is_arg_error(tmp_path):
    b, src = make_broker(tmp_path, ComposerAdapter())
    with pytest.raises(ToolArgError):
        send(b, src, "hi", deliver="pty")


# ---------------------------------------------------------------- stalls
def test_enter_not_observed_is_stalled(tmp_path):
    a = ComposerAdapter(submit_works=False)
    b, src = make_broker(tmp_path, a)
    res = send(b, src, "/clear")
    assert res["ok"] is False and res["error"].startswith("[user_turn_stalled]")
    assert a.writes == [("type", "/clear"), ("enter",)]


def test_dialog_after_typing_withholds_enter(tmp_path):
    a = ComposerAdapter()
    a.after_type = "Allow?\n❯ 1. Yes\n  2. No\nEnter to confirm\n"
    b, src = make_broker(tmp_path, a)
    res = send(b, src, "/clear")
    assert res["error"].startswith("[user_turn_stalled]")
    assert a.writes == [("type", "/clear")]  # no bare Enter into the dialog


def test_duplicate_within_window_is_suppressed(tmp_path):
    a = ComposerAdapter(submit_works=False)
    b, src = make_broker(tmp_path, a)
    assert send(b, src, "/clear")["error"].startswith("[user_turn_stalled]")
    a.draft, a.submit_works = "", True
    n = len(a.writes)
    assert send(b, src, "/clear")["status"] == "duplicate_suppressed"
    assert len(a.writes) == n
    assert send(b, src, "/compact")["status"] == "submitted"


# ---------------------------------------------------------------- channel parity
def test_channel_default_is_unchanged(tmp_path):
    a = ComposerAdapter(screen="busy (esc to interrupt)")  # keep nudges from writing
    b, src = make_broker(tmp_path, a)
    implicit = send(b, src, "hi", deliver=None)
    explicit = send(b, src, "hi", deliver="channel")
    assert implicit == explicit == {"ok": True, "delivered_to": "dst"}
    assert len(b._rows) == 2


def test_send_message_schema_advertises_deliver():
    tool = next(t for t in surface.TOOLS if t["name"] == "send_message")
    props = tool["inputSchema"]["properties"]
    assert props["deliver"]["enum"] == ["channel", "user_turn"]
    assert tool["inputSchema"]["required"] == ["to_id", "message"]


# ---------------------------------------------------------------- review follow-ups
PAD = "\n" * 30  # tmux capture-pane includes the blank rows below a short UI


@pytest.mark.parametrize("screen,want", [
    (composer(footer="✻ Working… (esc to interrupt)") + PAD, BUSY),
    (composer() + "\n ↑/↓ to navigate · Enter to select\n" + PAD, NOT_READY),
    (composer() + PAD, EMPTY),
    # stale composer frame above a footerless selector: the lowest ❯ decides
    (composer() + "Do you want to proceed?\n❯ 1. Yes\n  2. No\n", NOT_READY),
])
def test_assess_screen_edge_cases(screen, want):
    assert assess_screen(screen) == want


def test_classify_pane_state_ignores_trailing_blank_rows():
    from claude_org_runtime.terminal.base import classify_pane_state
    assert classify_pane_state(composer(footer="(esc to interrupt)") + PAD) == "busy"


def test_scrolled_back_pane_is_refused_with_zero_bytes(tmp_path):
    a = ComposerAdapter()
    a.pane_in_mode = lambda pane_id: True
    b, src = make_broker(tmp_path, a)
    res = send(b, src, "/clear")
    assert res["error"].startswith("[user_turn_not_ready]")
    assert a.writes == []


def test_tab_allowed_only_in_multiline_body(tmp_path):
    a = ComposerAdapter()
    a.bracketed_paste = True
    b, src = make_broker(tmp_path, a)
    assert send(b, src, "a\tb\nc")["status"] == "submitted"
    assert send(b, src, "a\tb")["error"].startswith("[user_turn_invalid_body]")


def test_refusal_does_not_arm_duplicate_window(tmp_path):
    a = ComposerAdapter(screen=_BUSY)
    b, src = make_broker(tmp_path, a)
    assert send(b, src, "/clear")["error"].startswith("[user_turn_busy]")
    a.fixed_screen = None
    assert send(b, src, "/clear")["status"] == "submitted"


def test_type_error_does_not_arm_duplicate_window(tmp_path):
    a = ComposerAdapter()
    real_type = a.type_text

    def boom(pane_id, text):
        raise RuntimeError("backend down")
    a.type_text = boom
    b, src = make_broker(tmp_path, a)
    assert send(b, src, "/clear")["error"].startswith("[user_turn_stalled]")
    a.type_text = real_type
    assert send(b, src, "/clear")["status"] == "submitted"


def test_nudge_waits_for_user_turn_and_rechecks_screen(tmp_path):
    import threading
    a = ComposerAdapter()  # idle composer
    b, src = make_broker(tmp_path, a)
    b.nudge_defer_interval = 0.01
    b.nudge_defer_max_tries = 3
    lock = b._pane_write_lock("7")
    lock.acquire()
    send(b, src, "hi", deliver="channel")  # spawns the nudge worker
    t = b._nudge_threads["dst"]
    t.join(0.1)
    assert t.is_alive() and a.writes == []  # blocked behind the in-flight write
    a.fixed_screen = _BUSY                  # the turn we "delivered" is running
    lock.release()
    t.join(2)
    assert a.writes == []                   # re-checked under the lock: no nudge
    events = [json.loads(ln)["event"] for ln in
              (tmp_path / "broker" / "queue.jsonl").read_text(encoding="utf-8").splitlines()]
    assert "nudge_deferred" in events and "nudge_sent" not in events


def test_nudge_read_error_under_lock_is_journaled(tmp_path):
    a = ComposerAdapter()
    b, src = make_broker(tmp_path, a)
    calls = {"n": 0}
    real = a.get_text

    def flaky(pane_id, escapes=False):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("pane gone")
        return real(pane_id)
    a.get_text = flaky
    send(b, src, "hi", deliver="channel")
    b._nudge_threads["dst"].join(2)
    events = [json.loads(ln) for ln in
              (tmp_path / "broker" / "queue.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(e["event"] == "nudge_failed" and e.get("error") == "pane gone" for e in events)
    assert a.writes == []


# ---------------------------------------------------------------- codex round 1
@pytest.mark.parametrize("screen,want", [
    # body starting with a blank line: ❯ row empty, content below it
    (f"{RULE}\n❯ \n  Please review this\n{RULE}\n? for shortcuts\n", DRAFT),
    # marker text inside the draft is the body, not UI state
    (composer("Please explain esc to interrupt"), DRAFT),
    (composer("press Enter to confirm"), DRAFT),
    (f"{RULE}\n❯ a\n  ↑/↓ to navigate\n{RULE}\n", DRAFT),
])
def test_assess_screen_draft_content_is_not_ui(screen, want):
    assert assess_screen(screen) == want


@pytest.mark.parametrize("body", ["Please explain esc to interrupt", "\nleading blank line"])
def test_body_with_marker_text_or_leading_blank_submits(tmp_path, body):
    class Multiline(ComposerAdapter):
        bracketed_paste = True

        def get_text(self, pane_id, escapes=False):
            rows = self.draft.split("\n") if self.draft else [""]
            inner = "\n".join("  " + r for r in rows[1:])
            return f"{RULE}\n❯ {rows[0]}\n{inner + chr(10) if inner else ''}{RULE}\n? for shortcuts\n"
    a = Multiline()
    b, src = make_broker(tmp_path, a)
    assert send(b, src, body)["status"] == "submitted"


# ---------------------------------------------------------------- codex round 2
def test_foreign_text_in_draft_withholds_enter(tmp_path):
    # e.g. a concurrent raw send_keys typed into the composer during the settle
    a = ComposerAdapter()
    real = a.type_text

    def clobbered(pane_id, text):
        real(pane_id, text)
        a.draft = "y" + a.draft
    a.type_text = clobbered
    b, src = make_broker(tmp_path, a)
    assert send(b, src, "/clear")["error"].startswith("[user_turn_stalled]")
    assert ("enter",) not in a.writes


def test_wrapped_single_line_draft_is_own(tmp_path):
    long = "/loop 5m " + "check the queue " * 10

    class Wrapping(ComposerAdapter):
        def get_text(self, pane_id, escapes=False):
            if not self.draft:
                return composer()
            return f"{RULE}\n❯ {self.draft[:40]}\n  {self.draft[40:]}\n{RULE}\n"
    a = Wrapping()
    b, src = make_broker(tmp_path, a)
    assert send(b, src, long)["status"] == "submitted"
