"""Dedup state for attention notifications.

State lives at ``.state/attention_notified.json``. Two namespaces:

* ``events`` — keyed by ``event:<events.id>``. Recorded once, never
  expires. The Watch loop must not replay the same DB event row.
* ``pending`` — keyed by ``pending:<task_id>:<kind>``. Cooldown-gated
  (``cooldown_sec``) so a stuck pending decision re-notifies on a slow
  cadence instead of silently rotting OR ringing on every poll.

Corruption recovery: any read failure (missing file, malformed JSON,
wrong shape) is downgraded to a warning and treated as empty state.
This matches §5 acceptance criterion "broken
``.state/attention_notified.json`` recovers".
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


@dataclass
class DedupState:
    """In-memory mirror of ``attention_notified.json``."""

    events: dict[str, str] = field(default_factory=dict)
    pending: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"events": dict(self.events), "pending": dict(self.pending)}


def load_state(path: Path) -> DedupState:
    """Read dedup state from ``path``; recover gracefully on corruption."""
    p = Path(path)
    if not p.exists():
        return DedupState()
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:
        print(
            f"warning: cannot read {p}: {exc}; recovering with empty state",
            file=sys.stderr,
        )
        return DedupState()
    if not raw.strip():
        return DedupState()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(
            f"warning: {p} contains invalid JSON ({exc}); "
            "recovering with empty state",
            file=sys.stderr,
        )
        return DedupState()
    if not isinstance(data, dict):
        print(
            f"warning: {p} top-level is not a JSON object; "
            "recovering with empty state",
            file=sys.stderr,
        )
        return DedupState()
    events_raw = data.get("events") if isinstance(data.get("events"), dict) else {}
    pending_raw = data.get("pending") if isinstance(data.get("pending"), dict) else {}
    return DedupState(
        events={
            k: str(v) for k, v in events_raw.items()
            if isinstance(k, str) and v is not None
        },
        pending={
            k: str(v) for k, v in pending_raw.items()
            if isinstance(k, str) and v is not None
        },
    )


def save_state(path: Path, state: DedupState) -> None:
    """Atomically write dedup state to ``path``."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".attention_notified.", dir=str(p.parent),
    )
    tmp = Path(tmp_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(
                state.to_dict(), fp, indent=2, ensure_ascii=False, sort_keys=True,
            )
            fp.write("\n")
        os.replace(tmp, p)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def should_notify(
    state: DedupState,
    key: str,
    *,
    source: str,
    cooldown_sec: int,
    now: datetime,
) -> bool:
    """Return True if ``key`` is unseen (or past cooldown)."""
    if source == "state.db.events":
        return key not in state.events
    # Anything not from state.db is treated as cooldown-gated.
    last = state.pending.get(key)
    if not last:
        return True
    last_dt = _parse_iso(last)
    if last_dt is None:
        # Garbled timestamp — treat as never notified rather than
        # silently swallowing the next alarm.
        return True
    return (now - last_dt).total_seconds() >= cooldown_sec


def record_notified(
    state: DedupState,
    key: str,
    *,
    source: str,
    now: datetime,
) -> None:
    ts = _iso_utc(now)
    if source == "state.db.events":
        state.events[key] = ts
    else:
        state.pending[key] = ts


def _iso_utc(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(s: str) -> Optional[datetime]:
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None
