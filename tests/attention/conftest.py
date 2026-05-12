"""Shared fixtures for attention tests.

A handful of helpers used across files: an in-memory factory for a
fake ``state.db`` matching the production schema and a pending
decisions writer. Keeping them here avoids the per-file boilerplate
and gives every test the same canonical event payload shape.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable


def make_state_db(
    db_path: Path, events: Iterable[dict[str, Any]],
) -> Path:
    """Create a minimal ``state.db`` with an ``events`` table.

    Schema mirrors ``claude-org-ja/tools/state_db/schema.sql`` (only
    the columns the attention reader touches are projected). Tests pass
    a list of partial dicts; missing keys take SQL defaults.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE events (
              id           INTEGER PRIMARY KEY AUTOINCREMENT,
              occurred_at  TEXT NOT NULL DEFAULT '2026-05-12T00:00:00Z',
              actor        TEXT,
              kind         TEXT NOT NULL,
              run_id       INTEGER,
              workstream_id INTEGER,
              project_id   INTEGER,
              payload_json TEXT NOT NULL DEFAULT '{}'
            );
            """
        )
        for ev in events:
            conn.execute(
                "INSERT INTO events (occurred_at, actor, kind, payload_json) "
                "VALUES (?, ?, ?, ?)",
                (
                    ev.get("occurred_at", "2026-05-12T00:00:00Z"),
                    ev.get("actor"),
                    ev["kind"],
                    json.dumps(ev.get("payload", {})),
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return db_path


def write_pending_decisions(
    path: Path, entries: Iterable[dict[str, Any]],
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(list(entries), indent=2), encoding="utf-8")
    return path
