"""Tests for ``claude_org_runtime.attention.readers``."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from claude_org_runtime.attention.readers import (
    read_broker_duplicates,
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


# ---------------------------------------------------------------------------
# Broker journal — duplicate_sidecar_detected (Issue #167)
# ---------------------------------------------------------------------------


def _write_journal(state_dir: Path, records: list[dict]) -> Path:
    """Write ``queue.jsonl`` lines the way ``store._journal`` does."""
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "queue.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


def _dup(ts: float, owner: str = "sec", instances=("a", "b")) -> dict:
    return {
        "ts": ts, "event": "duplicate_sidecar_detected",
        "owner": owner, "instances": list(instances),
    }


def test_read_broker_duplicates_missing_journal_returns_empty(
    tmp_path: Path,
) -> None:
    assert read_broker_duplicates(
        tmp_path / "broker", now_epoch=1000.0, window_sec=300.0,
    ) == []


def test_read_broker_duplicates_picks_only_the_duplicate_event(
    tmp_path: Path,
) -> None:
    _write_journal(tmp_path / "broker", [
        {"ts": 990.0, "event": "message_enqueued", "to_id": "sec"},
        _dup(995.0),
        {"ts": 999.0, "event": "claimed", "owner": "sec"},
    ])
    out = read_broker_duplicates(
        tmp_path / "broker", now_epoch=1000.0, window_sec=300.0,
    )
    assert len(out) == 1
    assert out[0]["owner"] == "sec"
    assert out[0]["instances"] == ["a", "b"]
    assert out[0]["ts"] == 995.0


def test_read_broker_duplicates_drops_rows_outside_window(
    tmp_path: Path,
) -> None:
    _write_journal(tmp_path / "broker", [
        _dup(500.0, instances=("old-a", "old-b")),   # 500s ago -> stale
        _dup(950.0, instances=("new-a", "new-b")),   # 50s ago -> live
    ])
    out = read_broker_duplicates(
        tmp_path / "broker", now_epoch=1000.0, window_sec=300.0,
    )
    assert [r["instances"] for r in out] == [["new-a", "new-b"]]


def test_read_broker_duplicates_skips_corrupt_and_undateable_rows(
    tmp_path: Path,
) -> None:
    """Malformed lines are skipped, not surfaced as fresh.

    An undateable row would sit in the tail and re-alert every cooldown
    forever; the signal repeats on its own, so skipping costs at most
    one lease window of delay.
    """
    path = _write_journal(tmp_path / "broker", [_dup(990.0)])
    with path.open("a", encoding="utf-8") as f:
        f.write("{not json\n")
        f.write("[1, 2, 3]\n")                       # not an object
        f.write("\n")                                # blank
        f.write(json.dumps({"event": "duplicate_sidecar_detected"}) + "\n")
        f.write(json.dumps(
            {"ts": "990", "event": "duplicate_sidecar_detected"}) + "\n")
        f.write(json.dumps(
            {"ts": True, "event": "duplicate_sidecar_detected"}) + "\n")
        f.write('{"ts": NaN, "event": "duplicate_sidecar_detected"}\n')
        f.write('{"ts": Infinity, "event": "duplicate_sidecar_detected"}\n')
    out = read_broker_duplicates(
        tmp_path / "broker", now_epoch=1000.0, window_sec=300.0,
    )
    assert [r["ts"] for r in out] == [990.0]


def test_read_broker_duplicates_scan_follows_window_not_a_byte_cap(
    tmp_path: Path,
) -> None:
    """A busy journal must not push a still-live incident out of view.

    The detection is re-emitted only once per lease window, so whatever
    the daemon journals in between (claims, deliveries, nudges) sits
    between the signal and the tail. A fixed-size tail would drop the
    signal while it is still inside the freshness window; the backward
    walk keeps reading until it passes the window.
    """
    records: list[dict] = [_dup(950.0, instances=("live-a", "live-b"))]
    records += [
        {"ts": 950.0 + i * 0.01, "event": "claimed", "owner": "sec",
         "ids": ["row-" + "x" * 40]}
        for i in range(200)
    ]
    _write_journal(tmp_path / "broker", records)
    out = read_broker_duplicates(
        tmp_path / "broker", now_epoch=1000.0, window_sec=300.0,
        chunk_bytes=256,   # far smaller than the trailing traffic
    )
    assert [r["instances"] for r in out] == [["live-a", "live-b"]]


def test_read_broker_duplicates_stops_walking_past_the_window(
    tmp_path: Path, capsys,
) -> None:
    """The walk ends at the first line older than the window.

    Asserted through the scan cap: reading the whole file would exceed
    ``max_scan_bytes`` and warn, so a silent run proves the walk stopped
    at the window boundary instead.
    """
    records: list[dict] = [
        {"ts": 100.0 + i, "event": "claimed", "owner": "sec"}
        for i in range(100)
    ]
    records.append(_dup(995.0))
    _write_journal(tmp_path / "broker", records)
    out = read_broker_duplicates(
        tmp_path / "broker", now_epoch=1000.0, window_sec=300.0,
        chunk_bytes=128, max_scan_bytes=1024,
    )
    assert [r["ts"] for r in out] == [995.0]
    assert capsys.readouterr().err == ""


def test_read_broker_duplicates_reports_a_capped_scan(
    tmp_path: Path, capsys,
) -> None:
    """Hitting the safety cap is said out loud, not silently truncated."""
    records: list[dict] = [
        {"event": "claimed", "owner": "sec", "note": "no ts at all"}
        for _ in range(100)
    ]
    records.append(_dup(995.0))
    _write_journal(tmp_path / "broker", records)
    out = read_broker_duplicates(
        tmp_path / "broker", now_epoch=1000.0, window_sec=300.0,
        chunk_bytes=128, max_scan_bytes=512,
    )
    # Partial degradation: what was reached is still reported.
    assert [r["ts"] for r in out] == [995.0]
    err = capsys.readouterr().err
    assert "freshness window" in err


def test_read_broker_duplicates_survives_multibyte_chunk_cut(
    tmp_path: Path,
) -> None:
    """A chunk boundary inside a UTF-8 codepoint must not kill the read."""
    _write_journal(tmp_path / "broker", [
        {"ts": 100.0, "event": "claimed", "owner": "old"},
        _dup(990.0, owner="ワーカー日本語", instances=("x", "y")),
        _dup(995.0, owner="sec"),
    ])
    data = (tmp_path / "broker" / "queue.jsonl").read_bytes()
    # Size the chunk so the first read boundary lands one byte into the
    # 3-byte codepoint opening the middle line's owner value.
    chunk = len(data) - (data.index("ワーカー日本語".encode("utf-8")) + 1)
    out = read_broker_duplicates(
        tmp_path / "broker", now_epoch=1000.0, window_sec=300.0,
        chunk_bytes=chunk,
    )
    # The damaged line is dropped; the next chunk brings back the rest.
    assert [r["owner"] for r in out] == ["ワーカー日本語", "sec"]


def test_read_broker_duplicates_unreadable_journal_warns(
    tmp_path: Path, capsys,
) -> None:
    """A directory where the journal should be degrades to no signals."""
    (tmp_path / "broker" / "queue.jsonl").mkdir(parents=True)
    assert read_broker_duplicates(
        tmp_path / "broker", now_epoch=1000.0, window_sec=300.0,
    ) == []
    assert "broker journal" in capsys.readouterr().err
