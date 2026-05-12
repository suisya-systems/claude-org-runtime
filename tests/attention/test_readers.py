"""Tests for ``claude_org_runtime.attention.readers``."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from claude_org_runtime.attention.readers import (
    read_events,
    read_pending_decisions,
)

from .conftest import make_state_db, write_pending_decisions


def test_read_events_missing_file_returns_empty(tmp_path: Path) -> None:
    assert read_events(tmp_path / "nope.db") == []


def test_read_events_empty_db_no_table_returns_empty(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE _ (id INTEGER)")
    conn.close()
    assert read_events(db_path) == []


def test_read_events_filters_to_relevant_kinds(tmp_path: Path) -> None:
    db = make_state_db(tmp_path / "state.db", [
        {"kind": "heartbeat"},
        {"kind": "notify_sent", "payload": {"kind": "approval_blocked"}},
        {"kind": "anomaly_observed"},
        {"kind": "ci_completed", "payload": {"status": "failed", "pr": 1}},
        {"kind": "worker_completed", "payload": {"task_id": "t"}},
        {"kind": "pr_merged", "payload": {"pr": 1}},
    ])
    rows = read_events(db)
    kinds = [r["kind"] for r in rows]
    assert kinds == [
        "notify_sent", "ci_completed", "worker_completed", "pr_merged",
    ]
    # Payloads are JSON-decoded into dicts.
    assert rows[1]["payload"] == {"status": "failed", "pr": 1}


def test_read_events_returns_rows_ordered_by_id(tmp_path: Path) -> None:
    db = make_state_db(tmp_path / "state.db", [
        {"kind": "worker_completed", "payload": {"task_id": "a"}},
        {"kind": "worker_completed", "payload": {"task_id": "b"}},
    ])
    rows = read_events(db)
    assert [r["payload"]["task_id"] for r in rows] == ["a", "b"]
    assert rows[0]["id"] < rows[1]["id"]


def test_read_events_handles_invalid_payload_json(tmp_path: Path) -> None:
    # Construct a DB without the CHECK(json_valid()) clause so we can
    # exercise the reader's defensive JSON parse.
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "occurred_at TEXT, actor TEXT, kind TEXT, payload_json TEXT)"
    )
    conn.execute(
        "INSERT INTO events (kind, payload_json) VALUES (?, ?)",
        ("worker_completed", "not-json"),
    )
    conn.commit()
    conn.close()
    rows = read_events(db_path)
    assert rows[0]["payload"] == {}


def test_read_pending_decisions_missing_returns_empty(tmp_path: Path) -> None:
    assert read_pending_decisions(tmp_path / "nope.json") == []


def test_read_pending_decisions_malformed_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "pending.json"
    path.write_text("{not json", encoding="utf-8")
    assert read_pending_decisions(path) == []


def test_read_pending_decisions_wrong_type_returns_empty(tmp_path: Path) -> None:
    path = tmp_path / "pending.json"
    path.write_text(json.dumps({"oops": True}), encoding="utf-8")
    assert read_pending_decisions(path) == []


def test_read_pending_decisions_filters_non_dict_entries(tmp_path: Path) -> None:
    path = write_pending_decisions(tmp_path / "pending.json", [
        {"task_id": "ok", "received_at": "2026-05-12T00:00:00Z",
         "raw_message": "?", "status": "pending"},
        "not-a-dict",
        12345,
    ])
    out = read_pending_decisions(path)
    assert len(out) == 1
    assert out[0]["task_id"] == "ok"


def test_read_events_non_sqlite_file_returns_empty(
    tmp_path: Path, capsys
) -> None:
    """A garbage file at ``state.db`` must not crash the long-running watch."""
    fake_db = tmp_path / "state.db"
    fake_db.write_bytes(b"not-a-sqlite-database\x00\x01")
    assert read_events(fake_db) == []
    err = capsys.readouterr().err
    # Either the connect failed or the master-table read failed; both
    # paths must surface a warning rather than raise.
    assert "state DB" in err
