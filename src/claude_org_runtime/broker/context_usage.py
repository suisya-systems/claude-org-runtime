"""Context-window usage of a Claude Code agent, for the cockpit's ``@ctx`` (#262).

Same source and arithmetic as renga's sidebar (``claude_monitor.rs``): the newest
``~/.claude/projects/<encoded cwd>/*.jsonl``; the last assistant message's
``usage`` (input + cache_read + cache_creation tokens) over the model's window.
Anything unknown returns ``None`` so the cockpit renders nothing, never a guess.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

#: Start with this much of the transcript's end and double (up to the cap) while
#: no usage record is in it: a huge tool result can sit after the last turn.
_TAIL_BYTES = 256 * 1024
_MAX_TAIL_BYTES = 16 * 1024 * 1024


def project_dir_name(cwd: str) -> str:
    """Claude Code's project dir name: every non ``[A-Za-z0-9.]`` char -> ``-``."""
    return re.sub(r"[^A-Za-z0-9.]", "-", cwd)


def context_limit(model: str | None) -> int:
    """Window size in tokens. The transcript holds the plain model id (no
    ``[1m]``), so 1M-by-default families are listed; unknown models get 200K."""
    m = model or ""
    if "[1m]" in m or "-1m" in m or "fable" in m or "mythos" in m:
        return 1_000_000
    # Docs (context-windows, "sizes by model"): Opus 4.6+, Sonnet 4.6+/5.x and
    # Haiku 5.x are 1M; Sonnet 4.5 and Haiku 4.x stay 200K.
    if re.search(r"(opus|sonnet)-(4-[6-9]|[5-9])|haiku-[5-9]", m):
        return 1_000_000
    if "opus" in m and "haiku" not in m and "sonnet" not in m:
        return 500_000
    return 200_000


def _last_usage(path: Path) -> tuple[str | None, int] | None:
    size = _TAIL_BYTES
    while True:
        with path.open("rb") as f:
            f.seek(0, 2)
            whole = f.tell() <= size
            f.seek(max(0, f.tell() - size))
            lines = f.read().decode("utf-8", errors="replace").splitlines()
        got = _scan(lines)
        if got or whole or size >= _MAX_TAIL_BYTES:
            return got
        size *= 2


def _scan(lines: list[str]) -> tuple[str | None, int] | None:
    for line in reversed(lines):
        try:
            rec = json.loads(line)
            msg = rec["message"]
            u = msg["usage"]
            if rec.get("type") != "assistant" or rec.get("isSidechain"):
                continue
            if msg.get("model") == "<synthetic>":
                continue
            tokens = (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                      + u.get("cache_creation_input_tokens", 0))
        except (ValueError, KeyError, TypeError, AttributeError):
            continue  # a partial first line of the tail lands here too
        if tokens > 0:
            return msg.get("model"), tokens
    return None


def context_percent(cwd: str | None, home: Path | None = None) -> str | None:
    """``"42%"`` for the agent working in ``cwd``, or ``None`` when unknown."""
    if not cwd:
        return None
    root = (home or Path.home()) / ".claude" / "projects"
    # Current Claude Code turns '.' into '-' too (".dispatcher" -> "-dispatcher");
    # renga's older rule keeps the dot. Look in both, newest transcript wins.
    names = {project_dir_name(cwd), re.sub(r"[^A-Za-z0-9]", "-", cwd)}
    try:
        files = [f for n in names for f in (root / n).glob("*.jsonl")]
        newest = max(files, key=lambda p: p.stat().st_mtime, default=None)
        got = newest and _last_usage(newest)
    except OSError:
        return None
    if not got:
        return None
    model, tokens = got
    return f"{min(100, int(tokens * 100 / context_limit(model) + 0.5))}%"
