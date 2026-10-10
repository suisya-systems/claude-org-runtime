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

Codex panes (Issue #208) use their own model (:func:`_parse_codex`): the
lowest ``›`` row at column 0 is the composer, and it is proven empty only
when everything after the glyph is blank or dim (Codex paints its rotating
placeholder dim; typed text never is), so Codex screens are read with
escapes. Busy = a ``•`` status line with an interrupt hint just above the
composer. A Codex body must be one line that fits on the composer row:
renga has no verified model of a wrapped Codex composer either, so the
backend must report the pane width (``pane_width``) or the turn is refused.

Known weaker guarantees than renga (no parser lock / cursor proof here):
the screen is re-read right before each write, but a dialog drawn in the
gap between that read and the write is not excluded; the caret position is
not checked (backends do not report it uniformly).
"""

from __future__ import annotations

import re
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


# --- Codex (calibrated on renga's codex v0.153.4 fixtures) ------------------
_CODEX_GLYPH = "\u203a"  # ›
_CODEX_FOOTERS = _DIALOG_FOOTERS + ("entertocontinue",)  # update / trust prompts
_CODEX_MENU_ROW = re.compile("\u203a\\s*\\d+\\.\\s")      # "› 1. Yes, proceed"
_ESC_SEQ = re.compile(r"\x1b(?:\[([0-?]*)[ -/]*([@-~])|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")


def _sgr_dim(params: str, dim: bool) -> bool:
    codes = params.split(";")
    i = 0
    while i < len(codes):
        c = codes[i]
        if c in ("", "0", "22"):
            dim = False
        elif c == "2":
            dim = True
        elif c in ("38", "48", "58") and i + 1 < len(codes):
            # ";"-form colour args (5;n / 2;r;g;b) are not attributes; the
            # ":"-form ("38:2::r:g:b") is one param and never matches here.
            i += {"5": 2, "2": 4}.get(codes[i + 1], 0)
        i += 1
    return dim


def _undim(screen: str) -> tuple[list[str], list[str]]:
    """Strip escapes; return (rows, rows with dim text blanked). SGR state
    carries across rows (tmux ``capture-pane -e`` only emits changes)."""
    full, lit, dim, pos = [], [], False, 0
    for m in [*_ESC_SEQ.finditer(screen), None]:
        seg = screen[pos:m.start() if m else len(screen)]
        full.append(seg)
        lit.append(re.sub(r"[^\n]", " ", seg) if dim else seg)
        if m is None:
            break
        if m.group(2) == "m":
            dim = _sgr_dim(m.group(1), dim)
        pos = m.end()
    rows = [ln.rstrip() for ln in "".join(full).split("\n")]
    return rows, [ln.rstrip() for ln in "".join(lit).split("\n")]


def _parse_codex(screen: str) -> tuple[str, list[str]]:
    """Classify a Codex screen (read with escapes). Draft = the composer row's
    non-dim text after the glyph."""
    lines, lit = _undim(screen)
    while lines and not lines[-1]:
        lines.pop()
    prompt = next((i for i in range(len(lines) - 1, -1, -1)
                   if lines[i].startswith(_CODEX_GLYPH)), None)
    outside = [ln for i, ln in enumerate(lines) if i != prompt]
    spaceless = unicodedata.normalize("NFKC", "\n".join(outside[-20:]).lower()).replace(" ", "")
    if any(f in spaceless for f in _CODEX_FOOTERS):
        return NOT_READY, []
    if prompt is None or _CODEX_MENU_ROW.match(lines[prompt]):
        return NOT_READY, []
    # Status line ("• Working (2s • esc to interrupt)") sits a spacer row or
    # two above the composer; bounded so transcript text cannot pin busy.
    above = [ln.lower() for ln in lines[max(prompt - 4, 0):prompt]]
    near = "\n".join(above[-1:] + [ln.lower() for ln in lines[prompt + 1:]])
    if any(m in near for m in _BUSY_MARKERS) or any(
            ln.lstrip().startswith("•") and any(m in ln for m in _BUSY_MARKERS) for ln in above):
        return BUSY, []
    draft = [lit[prompt][1:]]
    return (DRAFT if draft[0].strip() else EMPTY), draft


def _cells(text: str) -> int:
    return sum(0 if unicodedata.combining(ch) else
               2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


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
    if broker._pane_adopted_away(target.pane_id):
        # Ownership moved to another session (#166): the old pane is a husk and
        # typing there would not reach the current recipient.
        return _refuse("user_turn_unsupported_target",
                       f"agent '{to_id}' was adopted by another session; its old pane is detached")
    pane_id = target.pane_id
    codex = target.kind == "codex"
    if not codex and target.kind != "claude":
        return _refuse("user_turn_unsupported_target",
                       f"agent '{to_id}' is not a Claude or Codex pane (kind={target.kind!r})")
    if codex and "\n" in message:
        # Only the single composer row is modelled for Codex (decided, #208).
        return _refuse("user_turn_unsupported_target",
                       "multi-line user_turn to a Codex pane is not supported; use deliver='channel'")
    err = body_error(message, bool(getattr(adapter, "bracketed_paste", False)))
    if err:
        return _refuse("user_turn_invalid_body", err)
    if codex:
        width = getattr(adapter, "pane_width", None)
        try:
            cols = width(pane_id) if width else None
        except Exception as e:
            return _refuse("user_turn_unsupported_target", f"cannot read pane width: {e}")
        if not cols:
            return _refuse("user_turn_unsupported_target",
                           "terminal backend cannot report the pane width a Codex body must fit")
        # Glyph + space, and a cell left for the caret (renga parity).
        if _cells(message) > cols - 3:
            return _refuse("user_turn_invalid_body",
                           "body does not fit on one row of the Codex composer; "
                           "send a shorter single line or use deliver='channel'")
    parse = _parse_codex if codex else _parse

    def read() -> str:
        return adapter.get_text(pane_id, escapes=True) if codex else adapter.get_text(pane_id)

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
            state = parse(read())[0]
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
            if not _wait_stable_draft(broker, read, parse, message):
                return _stalled(broker, from_bind, target, message,
                                "typed body did not settle into the composer; Enter not sent")
            adapter.send_enter(pane_id)
            if not _wait_submitted(broker, read, parse):
                return _stalled(broker, from_bind, target, message,
                                "Enter sent but the draft was not observed to be consumed")
        except Exception as e:  # backend failed mid-sequence: bytes may be on screen
            return _stalled(broker, from_bind, target, message, f"terminal backend error: {e}")
    finally:
        lock.release()
    broker._journal("user_turn_submitted", from_id=from_bind.agent_id,
                    to_id=target.agent_id, pane_id=pane_id, chars=len(message))
    return {**result, "status": "submitted"}


def _poll(broker: "Broker", read, timeout: float):
    deadline = time.monotonic() + timeout
    while True:
        yield read()
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


def _wait_stable_draft(broker: "Broker", read, parse, message: str) -> bool:
    """Our draft visible in the composer, no busy/dialog, two identical frames."""
    prev = None
    for screen in _poll(broker, read, broker.user_turn_settle_timeout):
        state, draft = parse(screen)
        if state != DRAFT or not _is_own_draft(draft, message):
            prev = None
            continue
        if screen == prev:
            return True
        prev = screen
    return False


def _wait_submitted(broker: "Broker", read, parse) -> bool:
    for screen in _poll(broker, read, broker.user_turn_submit_timeout):
        if parse(screen)[0] in (BUSY, EMPTY):
            return True
    return False


def _stalled(broker: "Broker", from_bind, target, message: str, detail: str) -> dict:
    broker._journal("user_turn_stalled", from_id=from_bind.agent_id,
                    to_id=target.agent_id, pane_id=target.pane_id,
                    chars=len(message), error=detail)
    return _refuse("user_turn_stalled", f"{detail}; inspect the pane before retrying")
