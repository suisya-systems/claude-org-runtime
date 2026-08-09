"""Tests for ``claude_org_runtime.attention.readers``."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from claude_org_runtime.attention.readers import (
    read_broker_delivery_signals,
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


def test_read_broker_duplicates_walk_inspects_each_chunk_once(
    tmp_path: Path, monkeypatch,
) -> None:
    """The backward walk must stay linear in the bytes it reads.

    Re-inspecting the whole accumulated tail on every chunk would make
    the walk quadratic, and ``attention watch`` pays it on every poll.
    Pin the shape: each cutoff check sees only the chunk just read.
    """
    from claude_org_runtime.attention import readers as readers_mod

    records: list[dict] = [
        {"ts": 900.0 + i * 0.001, "event": "claimed", "owner": "sec"}
        for i in range(400)
    ]
    records.append(_dup(995.0))
    _write_journal(tmp_path / "broker", records)

    seen: list[int] = []
    real = readers_mod._chunk_reaches_cutoff

    def _spy(chunk, cutoff, *, at_file_start):
        seen.append(len(chunk))
        return real(chunk, cutoff, at_file_start=at_file_start)

    monkeypatch.setattr(readers_mod, "_chunk_reaches_cutoff", _spy)
    out = read_broker_duplicates(
        tmp_path / "broker", now_epoch=1000.0, window_sec=300.0,
        chunk_bytes=256,
    )
    assert [r["ts"] for r in out] == [995.0]
    assert len(seen) > 1              # the walk really did span chunks
    assert max(seen) <= 256           # never re-reads the accumulated tail


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


def test_read_broker_duplicates_projection_is_exactly_three_keys(
    tmp_path: Path,
) -> None:
    """The published row shape must survive the shared-engine refactor.

    Issue #166 moved this reader onto the same tail-walk as the
    delivery-ownership one, which hands back whole journal records. If
    the projection stopped narrowing them, ``event`` / ``instance`` /
    anything else the daemon writes would start leaking into a payload
    a downstream repo parses. Pin the key set exactly, not just the
    three values.
    """
    _write_journal(tmp_path / "broker", [_dup(995.0)])
    out = read_broker_duplicates(
        tmp_path / "broker", now_epoch=1000.0, window_sec=300.0,
    )
    assert [set(r) for r in out] == [{"ts", "owner", "instances"}]


# ---------------------------------------------------------------------------
# Broker journal — delivery-ownership signals (Issue #166)
# ---------------------------------------------------------------------------


def _superseded(
    ts: float, owner: str = "sec", instance: str = "inst-old",
) -> dict:
    """One ``delivery_register_superseded`` line as the store writes it."""
    return {
        "ts": ts, "event": "delivery_register_superseded",
        "owner": owner, "instance": instance,
        "state": "active", "latched": True,
    }


def _adopt_expired(
    ts: float, owner: str = "sec", adoption_id: str = "ad0011",
    restored: bool = True,
) -> dict:
    """One ``delivery_adopt_expired`` line as the store writes it."""
    return {
        "ts": ts, "event": "delivery_adopt_expired",
        "owner": owner, "adoption_id": adoption_id,
        "armed_seconds": 300.0, "lease_dropped": True,
        "generation": 4, "restored": restored,
        "restored_generation": 3 if restored else None,
    }


def test_read_broker_delivery_signals_missing_journal_returns_empty(
    tmp_path: Path,
) -> None:
    """A broker that never ran must not crash the watcher's first poll."""
    assert read_broker_delivery_signals(
        tmp_path / "broker", now_epoch=1000.0, window_sec=3600.0,
    ) == []


def test_read_broker_delivery_signals_picks_only_the_two_ownership_events(
    tmp_path: Path,
) -> None:
    """Exactly two event names may reach the delivery classifier.

    The journal carries every kind of line the daemon writes. Widening
    this filter would page the operator about routine traffic, and
    picking up ``duplicate_sidecar_detected`` here would re-notify a
    live double sidecar on this reader's hour-long window instead of
    the short one that signal is designed around.
    """
    _write_journal(tmp_path / "broker", [
        {"ts": 950.0, "event": "message_enqueued", "to_id": "sec"},
        {"ts": 960.0, "event": "duplicate_sidecar_detected",
         "owner": "sec", "instances": ["a", "b"]},
        {"ts": 970.0, "event": "lease_reaped", "owner": "sec"},
        _superseded(980.0),
        {"ts": 985.0, "event": "delivery_adopt_started",
         "owner": "sec", "adoption_id": "ad0011"},
        _adopt_expired(990.0),
        {"ts": 995.0, "event": "delivery_generation_registered",
         "owner": "sec", "generation": 5},
    ])
    out = read_broker_delivery_signals(
        tmp_path / "broker", now_epoch=1000.0, window_sec=3600.0,
    )
    assert [r["event"] for r in out] == [
        "delivery_register_superseded", "delivery_adopt_expired",
    ]


def test_read_broker_delivery_signals_drops_rows_outside_the_window(
    tmp_path: Path,
) -> None:
    """A long-settled incident must not be replayed as if it were now.

    Neither event repeats, so both sit in the append-only journal
    forever. Without the cutoff every scan would re-surface an adopt
    that expired weeks ago, and the operator would learn to ignore the
    one signal that means "nobody is receiving push right now".
    """
    _write_journal(tmp_path / "broker", [
        _adopt_expired(900.0, adoption_id="ancient"),    # 3700s ago
        _superseded(4000.0, instance="live-inst"),       # 600s ago
    ])
    out = read_broker_delivery_signals(
        tmp_path / "broker", now_epoch=4600.0, window_sec=3600.0,
    )
    assert [r["event"] for r in out] == ["delivery_register_superseded"]
    assert out[0]["instance"] == "live-inst"


def test_read_broker_delivery_signals_keep_the_raw_per_event_fields(
    tmp_path: Path,
) -> None:
    """The rows arrive raw, because the two events share almost no fields.

    ``read_broker_duplicates`` projects down to three keys; doing that
    here would drop ``adoption_id`` and ``instance``, and the classifier
    would have nothing left to build a per-incident dedup key from — a
    second session going mute would be swallowed by the cooldown of the
    first. ``ts`` is still normalized to a float so the window math and
    the ISO conversion have one type to work with.
    """
    _write_journal(tmp_path / "broker", [
        _superseded(980, instance="inst-old"),
        _adopt_expired(990, adoption_id="ad0011", restored=False),
    ])
    sup, exp = read_broker_delivery_signals(
        tmp_path / "broker", now_epoch=1000.0, window_sec=3600.0,
    )
    assert sup["instance"] == "inst-old"
    assert sup["latched"] is True
    assert sup["state"] == "active"
    assert exp["adoption_id"] == "ad0011"
    assert exp["restored"] is False
    # Written as ints above; both must come back as floats.
    assert isinstance(sup["ts"], float) and sup["ts"] == 980.0
    assert isinstance(exp["ts"], float) and exp["ts"] == 990.0


def test_read_broker_delivery_signals_skips_corrupt_and_undateable_rows(
    tmp_path: Path,
) -> None:
    """A half-written tail line must not take the whole signal down.

    The daemon appends while the watcher reads, so a torn last line is
    normal. Raising here would kill the poll that was supposed to report
    an owner going mute — the failure mode this reader exists to catch.
    """
    path = _write_journal(tmp_path / "broker", [_adopt_expired(990.0)])
    with path.open("a", encoding="utf-8") as f:
        f.write("{not json\n")
        f.write("[1, 2, 3]\n")                       # not an object
        f.write("\n")                                # blank
        f.write(json.dumps({"event": "delivery_adopt_expired"}) + "\n")
        f.write(json.dumps(
            {"ts": "990", "event": "delivery_adopt_expired"}) + "\n")
        f.write(json.dumps(
            {"ts": True, "event": "delivery_register_superseded"}) + "\n")
        f.write('{"ts": NaN, "event": "delivery_adopt_expired"}\n')
        f.write('{"ts": Infinity, "event": "delivery_adopt_expired"}\n')
    out = read_broker_delivery_signals(
        tmp_path / "broker", now_epoch=1000.0, window_sec=3600.0,
    )
    assert [r["ts"] for r in out] == [990.0]


def test_read_broker_delivery_signals_unreadable_journal_warns(
    tmp_path: Path, capsys,
) -> None:
    """A degraded read says which journal went quiet, then returns empty.

    Silently returning ``[]`` would be indistinguishable from "nothing
    is wrong", which is exactly the wrong answer for a consumer whose
    whole job is reporting silence.
    """
    (tmp_path / "broker" / "queue.jsonl").mkdir(parents=True)
    assert read_broker_delivery_signals(
        tmp_path / "broker", now_epoch=1000.0, window_sec=3600.0,
    ) == []
    assert "broker journal" in capsys.readouterr().err


def test_read_broker_delivery_signals_reports_a_capped_scan(
    tmp_path: Path, capsys,
) -> None:
    """Hitting the safety cap is said out loud, not silently truncated.

    These signals are one-shot, so a truncated walk does not just delay
    the alert — it loses it. The operator has to be told the window was
    not fully covered.
    """
    records: list[dict] = [
        {"event": "claimed", "owner": "sec", "note": "no ts at all"}
        for _ in range(100)
    ]
    records.append(_adopt_expired(995.0))
    _write_journal(tmp_path / "broker", records)
    out = read_broker_delivery_signals(
        tmp_path / "broker", now_epoch=1000.0, window_sec=3600.0,
        chunk_bytes=128, max_scan_bytes=512,
    )
    # Partial degradation: what was reached is still reported.
    assert [r["ts"] for r in out] == [995.0]
    assert "freshness window" in capsys.readouterr().err


def test_read_broker_delivery_signals_scan_follows_the_window(
    tmp_path: Path, capsys,
) -> None:
    """A busy daemon must not push a one-shot signal out of view.

    Nothing re-emits these lines, so whatever the daemon journals after
    one (claims, deliveries, nudges) is all that stands between it and
    the tail. A fixed-size tail read would drop a still-unresolved mute;
    the backward walk keeps going until it passes the window.
    """
    records: list[dict] = [_adopt_expired(950.0, adoption_id="still-live")]
    records += [
        {"ts": 950.0 + i * 0.01, "event": "claimed", "owner": "sec",
         "ids": ["row-" + "x" * 40]}
        for i in range(200)
    ]
    _write_journal(tmp_path / "broker", records)
    out = read_broker_delivery_signals(
        tmp_path / "broker", now_epoch=1000.0, window_sec=3600.0,
        chunk_bytes=256,   # far smaller than the trailing traffic
    )
    assert [r["adoption_id"] for r in out] == ["still-live"]
    assert capsys.readouterr().err == ""
