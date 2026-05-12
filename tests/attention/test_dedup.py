"""Tests for ``claude_org_runtime.attention.dedup``."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from claude_org_runtime.attention.dedup import (
    DedupState,
    load_state,
    record_notified,
    save_state,
    should_notify,
)

_NOW = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)


def test_load_missing_returns_empty(tmp_path: Path) -> None:
    state = load_state(tmp_path / "missing.json")
    assert state == DedupState()


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "attention_notified.json"
    state = DedupState(
        events={"event:1": "2026-05-12T10:00:00Z"},
        pending={"pending:t:pending_decision": "2026-05-12T10:00:00Z"},
    )
    save_state(path, state)
    loaded = load_state(path)
    assert loaded.events == state.events
    assert loaded.pending == state.pending


def test_load_recovers_from_broken_json(
    tmp_path: Path, capsys
) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{ this is not json", encoding="utf-8")
    state = load_state(path)
    assert state == DedupState()
    err = capsys.readouterr().err
    assert "recovering" in err


def test_load_recovers_from_non_object(tmp_path: Path, capsys) -> None:
    path = tmp_path / "arr.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    state = load_state(path)
    assert state == DedupState()
    err = capsys.readouterr().err
    assert "recovering" in err


def test_load_partial_shape(tmp_path: Path) -> None:
    path = tmp_path / "partial.json"
    path.write_text(
        json.dumps({"events": {"event:5": "2026-01-01T00:00:00Z"}}),
        encoding="utf-8",
    )
    state = load_state(path)
    assert state.events == {"event:5": "2026-01-01T00:00:00Z"}
    assert state.pending == {}


def test_event_dedup_blocks_second_call() -> None:
    state = DedupState()
    assert should_notify(
        state, "event:7", source="state.db.events",
        cooldown_sec=300, now=_NOW,
    )
    record_notified(state, "event:7", source="state.db.events", now=_NOW)
    assert not should_notify(
        state, "event:7", source="state.db.events",
        cooldown_sec=300, now=_NOW + timedelta(days=365),
    )


def test_pending_cooldown_blocks_within_window() -> None:
    state = DedupState()
    key = "pending:T:pending_decision"
    record_notified(state, key, source="pending_decisions", now=_NOW)
    assert not should_notify(
        state, key, source="pending_decisions",
        cooldown_sec=300, now=_NOW + timedelta(seconds=200),
    )


def test_pending_cooldown_clears_after_window() -> None:
    state = DedupState()
    key = "pending:T:pending_decision"
    record_notified(state, key, source="pending_decisions", now=_NOW)
    assert should_notify(
        state, key, source="pending_decisions",
        cooldown_sec=300, now=_NOW + timedelta(seconds=400),
    )


def test_pending_garbled_timestamp_allows_notify() -> None:
    state = DedupState(pending={"pending:X:pending_decision": "garbled"})
    assert should_notify(
        state, "pending:X:pending_decision",
        source="pending_decisions",
        cooldown_sec=300, now=_NOW,
    )


def test_save_is_atomic_replaces_existing(tmp_path: Path) -> None:
    path = tmp_path / "attention_notified.json"
    path.write_text("old content", encoding="utf-8")
    save_state(path, DedupState(events={"event:1": "2026-05-12T10:00:00Z"}))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {
        "events": {"event:1": "2026-05-12T10:00:00Z"},
        "pending": {},
    }
    # No stray tmp file left behind in the dir.
    leftovers = [
        p for p in path.parent.iterdir() if p.name != path.name
    ]
    assert leftovers == []
