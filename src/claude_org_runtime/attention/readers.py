"""Read-only loaders for the attention watcher.

The classifier is pure; this module is the only place that touches the
filesystem and SQLite. Each loader tolerates missing files and returns
an empty list — first-start environments (no ``state.db``, no
``pending_decisions.json``) must not crash the watcher per §11.5.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

# Event ``kind`` column values relevant to attention classification.
# Narrowing the SELECT keeps `scan` cheap on busy DBs (events grows
# unbounded) and gives the unit tests a fixed surface to assert.
RELEVANT_EVENT_KINDS: tuple[str, ...] = (
    "notify_sent",
    "ci_completed",
    "worker_completed",
    "pr_merged",
)


def read_events(state_db_path: Path) -> list[dict[str, Any]]:
    """Return rows from ``events`` that may produce attention events.

    Returns ``[]`` for any read error — missing file, missing
    ``events`` table, non-SQLite file, corrupt page, or query-time
    SQLite errors. A long-running ``watch`` must not crash because of
    a transient DB issue; we log a one-line warning and let the next
    poll retry.
    """
    p = Path(state_db_path)
    if not p.exists():
        return []
    uri = f"file:{p.as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        print(
            f"warning: cannot open state DB {p}: {exc}; "
            "treating as no events",
            file=sys.stderr,
        )
        return []
    try:
        conn.row_factory = sqlite3.Row
        try:
            has_events = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='events'"
            ).fetchone()
        except sqlite3.Error as exc:
            print(
                f"warning: state DB {p} is unreadable ({exc}); "
                "treating as no events",
                file=sys.stderr,
            )
            return []
        if has_events is None:
            return []
        placeholders = ",".join("?" * len(RELEVANT_EVENT_KINDS))
        try:
            cur = conn.execute(
                f"SELECT id, occurred_at, actor, kind, payload_json "
                f"FROM events WHERE kind IN ({placeholders}) "
                f"ORDER BY id ASC",
                RELEVANT_EVENT_KINDS,
            )
        except sqlite3.Error as exc:
            print(
                f"warning: state DB events query failed ({exc}); "
                "treating as no events",
                file=sys.stderr,
            )
            return []
        out: list[dict[str, Any]] = []
        for r in cur:
            out.append({
                "id": r["id"],
                "occurred_at": r["occurred_at"],
                "actor": r["actor"],
                "kind": r["kind"],
                "payload": _safe_payload(r["payload_json"]),
            })
        return out
    finally:
        conn.close()


def read_pending_decisions(pending_path: Path) -> list[dict[str, Any]]:
    """Return entries from ``pending_decisions.json`` (or ``[]`` if absent).

    Tolerates malformed JSON: a corrupt register must not crash the
    watcher (the register is owned by the Secretary pane, not the
    watcher, and may briefly be inconsistent while being rewritten).
    """
    p = Path(pending_path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [d for d in data if isinstance(d, dict)]


def _safe_payload(raw: Any) -> dict[str, Any]:
    """Coerce ``events.payload_json`` to a plain dict (or empty)."""
    if raw is None or raw == "":
        return {}
    try:
        v = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}
