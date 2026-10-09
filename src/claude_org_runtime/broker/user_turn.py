# -*- coding: utf-8 -*-
"""``send_message(deliver="user_turn")`` (Issue #163, renga #323 parity).

Delivers a body as a real user turn: the body is typed into the recipient
Claude's composer and submitted with a separate Enter, so slash commands and
directives that a channel tag does not arm (``/clear``, ``/loop`` ...) fire.

Safety model (same as renga): refuse unless a safe target is **positively**
proven -- a registered Claude agent, not busy, no selection dialog, and an
empty composer drawn the way Claude Code draws it (a ``❯`` row fenced by
rules directly above and below). Every refusal writes zero bytes. After the
body is typed, Enter is only sent once the draft is visible and stable; after
Enter, success is only reported once submission is observed (composer empty
again, or the agent busy). Otherwise ``[user_turn_stalled]``.

Refusal codes and the success ``status`` values match renga's contract so the
two transports stay interchangeable for callers.

Known weaker guarantees than renga (no parser lock / cursor proof here):
the screen is re-read right before each write, but a dialog drawn in the
gap between that read and the write is not excluded; the caret position is
not checked (backends do not report it uniformly).
"""

from __future__ import annotations

import time
import unicodedata
from typing import TYPE_CHECKING

from ..terminal.base import _BUSY_MARKERS

if TYPE_CHECKING:  # pragma: no cover
    from .server import Broker
    from .tokens import AgentBind

#: Body cap in UTF-8 bytes (renga parity: a user turn is a prompt, not a file).
MAX_BODY_BYTES = 4096
#: Duplicate window: an identical body to the same pane is suppressed so a retry
#: after ``user_turn_stalled`` cannot fire a second ``/clear`` (renga parity).
DUPLICATE_WINDOW = 5.0

# Selection-dialog footers (spaceless compare; same set as
# dispatcher.spawn_prompt._SELECTION_FOOTERS). Any of them = a modal is up.
_DIALOG_FOOTERS = ("entertoconfirm", "entertoselect", "↑/↓tonavigate")
_FENCE_CHARS = set("─━")

BUSY = "busy"
EMPTY = "empty"          # proven empty composer: safe to type
DRAFT = "draft"          # composer holds text (ours after typing, else someone's)
NOT_READY = "not_ready"  # dialog / unrecognised screen


def _is_fence(line: str) -> bool:
    # Composer borders start at column 0; an indented rule is draft content.
    s = line.rstrip()
    return len(s) >= 8 and set(s) <= _FENCE_CHARS


def assess_screen(screen: str) -> str:
    return _parse(screen)[0]


def _parse(screen: str) -> tuple[str, list[str]]:
    """Classify a visible screen for user-turn delivery.

    Stricter than :func:`~claude_org_runtime.terminal.base.classify_pane_state`:
    ``EMPTY`` needs the composer row fenced on both sides, and any selection
    dialog footer makes the screen ``NOT_READY``. Busy markers can only push
    toward refusal.
    """
    lines = [ln.rstrip() for ln in screen.splitlines()]
    # Captures include the pane's blank rows below a short UI (fresh / cleared
    # pane): anchor the marker window to content, not to the physical bottom.
    while lines and not lines[-1]:
        lines.pop()
    # Locate the lowest composer: a ❯ row with a rule directly above, up to the
    # next rule below. Its rows are the draft (a body may contain marker text).
    region = None
    for i in range(len(lines) - 1, 0, -1):
        if lines[i].strip().startswith("❯") and _is_fence(lines[i - 1]):
            end = next((j for j in range(i + 1, len(lines)) if _is_fence(lines[j])), None)
            if end is not None:
                region = (i, end)
                break
    start, end = region if region else (len(lines), len(lines))
    outside = lines[:max(start - 1, 0)] + lines[end + 1:]
    low = "\n".join(outside[-20:]).lower()
    # Dialog first: a permission menu's "Esc to cancel" is also a busy marker,
    # but the caller needs to know a blocker (not a turn) is in the way.
    spaceless = unicodedata.normalize("NFKC", low).replace(" ", "")
    if any(f in spaceless for f in _DIALOG_FOOTERS):
        return NOT_READY, []
    if any(m in low for m in _BUSY_MARKERS):
        return BUSY, []
    if region is None:
        return NOT_READY, []
    # A cursor row below the composer is a selector (the lowest ❯ decides; an
    # older composer frame above it must not count).
    if any(ln.strip().startswith("❯") for ln in lines[end + 1:]):
        return NOT_READY, []
    draft = [lines[start].strip()[1:]] + lines[start + 1:end]
    return (DRAFT if any(ln.strip() for ln in draft) else EMPTY), draft


def body_error(message: str, multiline_ok: bool) -> str | None:
    """Return why ``message`` cannot be a user turn, or None if it can."""
    if not message.strip():
        return "empty body"
    if len(message.encode("utf-8")) > MAX_BODY_BYTES:
        return f"body exceeds {MAX_BODY_BYTES} bytes (use deliver='channel')"
    for ch in message:
        if ch == "\n" or (ch == "\t" and "\n" in message):
            continue  # a tab is only a Tab keypress in a single-line body (renga)
        if unicodedata.category(ch) == "Cc":
            return f"control character U+{ord(ch):04X} in body"
    if "\n" in message and not multiline_ok:
        return "multi-line body needs bracketed paste, which this terminal backend does not declare"
    return None


def _refuse(code: str, detail: str) -> dict:
    return {"ok": False, "error": f"[{code}] {detail}"}


def deliver_user_turn(broker: "Broker", from_bind: "AgentBind", to_id: str, message: str) -> dict:
    """Body of :meth:`Broker.deliver_user_turn`. Never holds ``broker._lock``
    across adapter I/O (server-wide deadlock contract)."""
    target = broker._find_registered_target(to_id)
    if target is None:
        return _refuse("peer_not_found", f"no agent '{to_id}'")
    adapter = broker.adapter
    if adapter is None or target.pane_id is None:
        return _refuse("user_turn_unsupported_target",
                       f"agent '{to_id}' has no broker-managed pane to type into")
    if target.kind != "claude":
        # Codex composer rendering has no verified model here; fail closed.
        return _refuse("user_turn_unsupported_target",
                       f"agent '{to_id}' is not a Claude pane (kind={target.kind!r})")
    err = body_error(message, bool(getattr(adapter, "bracketed_paste", False)))
    if err:
        return _refuse("user_turn_invalid_body", err)

    pane_id = target.pane_id
    key = str(pane_id)
    result = {"ok": True, "delivered_to": target.agent_id, "deliver": "user_turn"}
    lock = broker._pane_write_lock(key)
    if not lock.acquire(blocking=False):
        return _refuse("user_turn_not_ready", "another delivery to this pane is in flight")
    try:
        now = time.monotonic()
        last = broker._user_turn_last.get(key)
        if last and last[0] == message and now - last[1] < DUPLICATE_WINDOW:
            return {**result, "status": "duplicate_suppressed"}
        try:
            state = assess_screen(adapter.get_text(pane_id))
        except Exception as e:  # pane gone / backend down
            return _refuse("user_turn_unsupported_target", f"cannot read pane: {e}")
        if state == BUSY:
            return _refuse("user_turn_busy", "agent is mid-turn; retry when idle")
        if state != EMPTY:
            return _refuse("user_turn_not_ready",
                           "no empty composer could be proven (dialog, draft or unrecognised screen)")
        # A scrolled-back pane (tmux copy mode) still captures the live screen,
        # but would swallow the paste brackets and the Enter.
        in_mode = getattr(adapter, "pane_in_mode", None)
        try:
            scrolled = bool(in_mode and in_mode(pane_id))
        except Exception as e:
            return _refuse("user_turn_unsupported_target", f"cannot read pane mode: {e}")
        if scrolled:
            return _refuse("user_turn_not_ready", "pane is scrolled back (copy/view mode)")

        # --- bytes are written from here on ---------------------------------
        try:
            adapter.type_text(pane_id, message)
            broker._user_turn_last[key] = (message, now)
            time.sleep(broker.user_turn_settle)
            if not _wait_stable_draft(broker, adapter, pane_id, message):
                return _stalled(broker, from_bind, target, message,
                                "typed body did not settle into the composer; Enter not sent")
            adapter.send_enter(pane_id)
            if not _wait_submitted(broker, adapter, pane_id):
                return _stalled(broker, from_bind, target, message,
                                "Enter sent but the draft was not observed to be consumed")
        except Exception as e:  # backend failed mid-sequence: bytes may be on screen
            return _stalled(broker, from_bind, target, message, f"terminal backend error: {e}")
    finally:
        lock.release()
    broker._journal("user_turn_submitted", from_id=from_bind.agent_id,
                    to_id=target.agent_id, pane_id=pane_id, chars=len(message))
    return {**result, "status": "submitted"}


def _poll(broker: "Broker", adapter, pane_id, timeout: float):
    deadline = time.monotonic() + timeout
    while True:
        yield adapter.get_text(pane_id)
        if time.monotonic() >= deadline:
            return
        time.sleep(broker.user_turn_poll)


def _is_own_draft(draft: list[str], message: str) -> bool:
    """For a single-line body, the whole visible draft (wrapped rows joined,
    whitespace ignored) must equal it, so text typed concurrently (e.g. a raw
    send_keys) is not submitted as ours. Multi-line pastes may render as a
    placeholder, so they are not compared."""
    if "\n" in message:
        return True
    return "".join("".join(draft).split()) == "".join(message.split())


def _wait_stable_draft(broker: "Broker", adapter, pane_id, message: str) -> bool:
    """Our draft visible in the composer, no busy/dialog, two identical frames."""
    prev = None
    for screen in _poll(broker, adapter, pane_id, broker.user_turn_settle_timeout):
        state, draft = _parse(screen)
        if state != DRAFT or not _is_own_draft(draft, message):
            prev = None
            continue
        if screen == prev:
            return True
        prev = screen
    return False


def _wait_submitted(broker: "Broker", adapter, pane_id) -> bool:
    for screen in _poll(broker, adapter, pane_id, broker.user_turn_submit_timeout):
        if assess_screen(screen) in (BUSY, EMPTY):
            return True
    return False


def _stalled(broker: "Broker", from_bind, target, message: str, detail: str) -> dict:
    broker._journal("user_turn_stalled", from_id=from_bind.agent_id,
                    to_id=target.agent_id, pane_id=target.pane_id,
                    chars=len(message), error=detail)
    return _refuse("user_turn_stalled", f"{detail}; inspect the pane before retrying")
