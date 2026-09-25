"""Decide the next keystroke for the startup prompts of a freshly spawned worker.

Incident (2026-09-25): the delegate plan sent a blind Enter right after
``spawn_claude_pane``. In a new worktree Claude Code shows the folder-trust
dialog with the cursor on "No, exit", so the blind Enter exited the worker.
In a trusted directory only the dev-channel dialog appears; both can appear in
sequence, and right after spawn neither may be rendered yet.

This module is the pure, transport-independent decision core. The caller
(Dispatcher via MCP, or code) loops ``inspect_pane`` -> :func:`decide` ->
act, until the action is ``done`` or ``escalate``. Design points:

- Stateless and screen-driven: every decision is derived from the current
  screen (plus the previous one), so a lost keystroke or a re-render never
  desynchronises a hidden state machine.
- Dialogs are matched by option *text*, never by position or number: the
  folder-trust option order changed across Claude Code versions (2.1.282
  renders "No, exit" first; older versions numbered "1. Yes, proceed").
- Any key (Enter, Up, Down) is sent only when two consecutive identical
  observations taken *after the last key* agree (rendering has settled); Enter
  additionally needs the cursor on the accept row. Never a key past the deadline.
- Caller contract: pass ``previous_screen=None`` after every ``send_keys``, so
  a stale frame of the pre-key screen can never confirm a second key.
- Ambiguous screens (more than one cursor row, or duplicate accept / reject
  rows, e.g. a leftover dialog above a new one) are ``unknown``: wait, never key.

There is no official way to pre-trust a directory for an interactive session.
Trust is only inherited: a linked worktree uses the main checkout's root
(https://code.claude.com/docs/en/permissions#project-allow-rules-and-workspace-trust);
there is no setting, environment variable or CLI flag for it
(https://code.claude.com/docs/en/settings , https://code.claude.com/docs/en/env-vars ,
https://code.claude.com/docs/en/cli-reference). The dev-channel dialog is shown
on every launch (https://code.claude.com/docs/en/channels-reference).

Key names use TitleCase ("Down" / "Up"), which both renga-peers and org-broker
``send_keys`` accept. The WezTerm broker adapter has no Up/Down: the caller
must escalate on ``[key_unsupported]`` instead of falling back to Enter.

CLI (``claude-org-runtime dispatcher spawn-prompt-step`` or
``python -m claude_org_runtime.dispatcher.runner spawn-prompt-step``): reads
``{"screen", "previous_screen", "elapsed_ms"}`` as one JSON object on stdin and
prints the decision plus ``elapsed_ms`` / ``deadline_ms`` as ASCII JSON.

Exit codes:
  0  -- decision emitted (action ``send_keys`` / ``wait`` / ``done``)
  10 -- action ``escalate`` (10, not 1, so a Python traceback is never
        misread as an escalation or vice versa)
  2  -- invalid input (bad JSON, missing / invalid field, unrecognized screen
        shape); stdout carries ``{"error": ...}``
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from typing import Any

STATE_FOLDER_TRUST = "folder_trust"
STATE_DEV_CHANNEL = "dev_channel"
STATE_NOT_SHOWN = "not_shown"
STATE_UNKNOWN = "unknown"
STATE_DONE = "done"

ACTION_SEND_KEYS = "send_keys"
ACTION_WAIT = "wait"
ACTION_DONE = "done"
ACTION_ESCALATE = "escalate"

# Each loop pass is an LLM Dispatcher turn (inspect + helper call, plus a
# send_keys on key passes), several seconds apiece; a folder-trust then
# dev-channel spawn takes ~8 passes. 120s leaves room for a slow but healthy pass.
DEFAULT_APPROVAL_DEADLINE_MS = 120000

_CURSORS = ("❯", ">")
# "esc to cancel" is deliberately absent: it is also a busy marker.
_SELECTION_FOOTERS = ("enter to confirm", "enter to select", "↑/↓ to navigate")
_FENCE_CHARS = frozenset("─━-")
_OPTION_NUMBER = re.compile(r"^\d+[.)]\s*")


def screen_lines(obj: Any) -> list[str]:
    """Normalise an ``inspect_pane`` result (renga / broker / MCP envelope) to rows."""
    if isinstance(obj, str):
        return obj.splitlines()
    if isinstance(obj, list):
        if all(isinstance(x, str) for x in obj):
            return list(obj)
        if all(isinstance(x, dict) and isinstance(x.get("text"), str) for x in obj):
            rows = obj
            if all("row" in x for x in obj):
                rows = sorted(obj, key=lambda x: x["row"])
            return [x["text"] for x in rows]
    elif isinstance(obj, dict):
        if isinstance(obj.get("structuredContent"), dict):
            return screen_lines(obj["structuredContent"])
        for key in ("lines", "grid"):
            if isinstance(obj.get(key), list):
                return screen_lines(obj[key])
        if isinstance(obj.get("text"), str):
            return obj["text"].splitlines()
        if isinstance(obj.get("content"), list):
            text = "\n".join(
                c["text"]
                for c in obj["content"]
                if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str)
            )
            try:
                return screen_lines(json.loads(text))
            except ValueError:
                return text.splitlines()
    raise ValueError(f"unrecognized inspect_pane shape: {type(obj).__name__}")


def _norm(line: str) -> str:
    # NFKC folds the NBSP renga renders after the cursor glyph into a space.
    return " ".join(unicodedata.normalize("NFKC", line).split()).lower()


def _unbox(norm_line: str) -> str:
    """Strip a surrounding box border ("│ ... │") from a normalized row."""
    return norm_line.strip("│| ")


def _label(norm_line: str) -> str:
    """Option label with box border, cursor glyph and an optional "N." number stripped."""
    s = _unbox(norm_line)
    for c in _CURSORS:
        if s.startswith(c):
            s = s[len(c):].lstrip()
            break
    return _OPTION_NUMBER.sub("", s)


def _has_cursor(norm_line: str) -> bool:
    return _unbox(norm_line).startswith(_CURSORS)


def _find_dialog(norm: list[str], state: str) -> tuple[list[int], list[int]]:
    """Return (accept row indices, reject row indices) for one dialog kind."""
    accepts: list[int] = []
    rejects: list[int] = []
    for i, ln in enumerate(norm):
        label = _label(ln)
        if state == STATE_FOLDER_TRUST:
            is_accept = "yes, i trust this folder" in label or "yes, proceed" in label
            is_reject = "no, exit" in label
        else:
            is_accept = "i am using this for local development" in label
            is_reject = label == "exit"
        if is_accept:
            accepts.append(i)
        elif is_reject:
            rejects.append(i)
    return accepts, rejects


def _is_fence(norm_line: str) -> bool:
    s = norm_line.strip("╭╮╰╯")
    return len(s) >= 8 and set(s) <= _FENCE_CHARS


def _composer_visible(norm: list[str]) -> bool:
    for i, ln in enumerate(norm):
        if ln.startswith("❯") or ln.startswith("│ >"):
            near = norm[max(0, i - 2):i] + norm[i + 1:i + 3]
            if any(_is_fence(n) for n in near):
                return True
    return False


def _decide(screen: list[str], previous_screen: list[str] | None) -> dict[str, Any]:
    norm = [_norm(ln) for ln in screen]
    found = {s: _find_dialog(norm, s) for s in (STATE_FOLDER_TRUST, STATE_DEV_CHANNEL)}
    recognized = [s for s, (accepts, _) in found.items() if accepts]

    if len(recognized) > 1:
        return {"state": STATE_UNKNOWN, "action": ACTION_WAIT,
                "reason": "both folder-trust and dev-channel accept rows are visible"}
    if recognized:
        state = recognized[0]
        accepts, rejects = found[state]
        # Newer output renders below older output, so a cursor row anywhere
        # from the dialog's first option down (e.g. a new, unrecognized dialog
        # under a leftover one) makes the screen ambiguous. Rows above it (a
        # "❯"-themed shell prompt that launched claude) are ignored.
        top = min(accepts + rejects)
        cursors = [i for i, ln in enumerate(norm) if i >= top and _has_cursor(ln)]
        if len(accepts) > 1 or len(rejects) > 1 or len(cursors) > 1:
            return {"state": STATE_UNKNOWN, "action": ACTION_WAIT,
                    "reason": "ambiguous screen: duplicate option rows or more than one cursor row"}
        accept = accepts[0]
        cursor = next((i for i in cursors if i == accept or i in rejects), None)
        if cursor is None:
            return {"state": state, "action": ACTION_WAIT,
                    "reason": "dialog visible but no cursor on an option row yet"}
        if previous_screen is None or not _settled(norm, previous_screen):
            return {"state": state, "action": ACTION_WAIT,
                    "reason": "confirm with a second identical inspect (taken after the last key) before any key"}
        if cursor == accept:
            return {"state": state, "action": ACTION_SEND_KEYS,
                    "send_keys": {"enter": True},
                    "reason": "cursor on accept row in two identical inspects"}
        key = "Down" if accept > cursor else "Up"
        return {"state": state, "action": ACTION_SEND_KEYS,
                "send_keys": {"keys": [key]},
                "reason": f"cursor not on accept row; move {key}"}

    if any(f in ln for ln in norm for f in _SELECTION_FOOTERS):
        return {"state": STATE_UNKNOWN, "action": ACTION_WAIT,
                "reason": "unrecognized selection dialog is visible"}
    if _composer_visible(norm):
        return {"state": STATE_DONE, "action": ACTION_DONE,
                "reason": "Claude composer is visible and no dialog remains"}
    return {"state": STATE_NOT_SHOWN, "action": ACTION_WAIT,
            "reason": "no dialog or composer rendered yet"}


def _settled(norm: list[str], previous_screen: list[str]) -> bool:
    def trim(rows: list[str]) -> list[str]:
        while rows and not rows[-1]:
            rows = rows[:-1]
        return rows

    return trim(norm) == trim([_norm(ln) for ln in previous_screen])


def decide(
    screen: list[str],
    previous_screen: list[str] | None,
    elapsed_ms: int,
    deadline_ms: int = DEFAULT_APPROVAL_DEADLINE_MS,
) -> dict[str, Any]:
    """Return the single next action for the startup-prompt screen.

    ``{"state", "action", "send_keys"?, "reason"}``. Past ``deadline_ms`` any
    action other than ``done`` becomes ``escalate`` (never a key late).

    ``previous_screen`` must be an observation taken after the most recent
    ``send_keys``; pass ``None`` after any keystroke. Otherwise a not-yet-redrawn
    frame of the pre-key screen would look "settled" and trigger a second key.
    """
    result = _decide(screen, previous_screen)
    if elapsed_ms >= deadline_ms and result["action"] != ACTION_DONE:
        return {"state": result["state"], "action": ACTION_ESCALATE,
                "reason": f"deadline {deadline_ms}ms reached: {result['reason']}"}
    return result


EXIT_OK = 0
EXIT_INVALID = 2
EXIT_ESCALATE = 10


def cmd_spawn_prompt_step(args: argparse.Namespace) -> int:
    """One iteration of the approve_spawn_prompts loop: stdin JSON -> decision JSON."""
    try:
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict):
            raise ValueError("input must be a JSON object")
        if "screen" not in payload:
            raise ValueError("missing field: screen")
        elapsed_ms = payload.get("elapsed_ms")
        if isinstance(elapsed_ms, bool) or not isinstance(elapsed_ms, int) or elapsed_ms < 0:
            raise ValueError("elapsed_ms must be a non-negative integer")
        screen = screen_lines(payload["screen"])
        previous = payload.get("previous_screen")
        previous_lines = None if previous is None else screen_lines(previous)
    # json.JSONDecodeError is a ValueError; TypeError = unsortable "row" values,
    # RecursionError = pathologically nested input. All are invalid input (2).
    except (ValueError, TypeError, RecursionError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=True))
        return EXIT_INVALID
    result = decide(screen, previous_lines, elapsed_ms, args.deadline_ms)
    result["elapsed_ms"] = elapsed_ms
    result["deadline_ms"] = args.deadline_ms
    # ensure_ascii: the cursor glyph in a reason must not crash a cp932 console.
    print(json.dumps(result, ensure_ascii=True))
    return EXIT_ESCALATE if result["action"] == ACTION_ESCALATE else EXIT_OK


def add_subparser(sub: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    sp = sub.add_parser(
        "spawn-prompt-step",
        help=("decide the next keystroke for a freshly spawned worker's startup "
              "prompts (folder-trust / dev-channel) from one inspect_pane result"),
        description=(
            "Reads {\"screen\": <inspect_pane result>, \"previous_screen\": "
            "<inspect result or null>, \"elapsed_ms\": int} on stdin and prints "
            "the decision JSON. Exit codes: 0 decision emitted (send_keys / wait "
            "/ done), 10 escalate (send no key), 2 invalid input."
        ),
    )
    sp.add_argument(
        "--deadline-ms", type=int, default=DEFAULT_APPROVAL_DEADLINE_MS,
        help=("escalate instead of keying once elapsed_ms reaches this "
              f"(default: {DEFAULT_APPROVAL_DEADLINE_MS})"),
    )
    sp.set_defaults(func=cmd_spawn_prompt_step)
