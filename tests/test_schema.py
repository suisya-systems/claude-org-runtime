"""Tests for the Step B schema package."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_org_runtime.schema import (
    JournalEvent,
    JournalEventType,
    WorkerDirEntry,
    WorkerStatus,
    parse_worker_directory_registry,
)
from claude_org_runtime.schema.json_schema import (
    journal_event_schema,
    worker_dir_entry_schema,
)

FIXTURES = Path(__file__).parent / "fixtures" / "synthetic"


def test_enums_are_string_serialisable() -> None:
    assert WorkerStatus.IN_USE.value == "in_use"
    assert json.dumps(WorkerStatus.IN_USE.value) == '"in_use"'
    assert JournalEventType.MISC.value == "misc"


def test_journal_event_round_trip_preserves_extra() -> None:
    raw = {
        "ts": "2026-04-22T00:00:00Z",
        "event": "worker_spawned",
        "task_id": "demo",
        "pane_id": 5,
        "future_field": "hello",
    }
    ev = JournalEvent.from_dict(raw)
    assert ev.event is JournalEventType.WORKER_SPAWNED
    assert ev.extra == {"future_field": "hello"}
    again = ev.to_dict()
    assert again["future_field"] == "hello"
    assert JournalEvent.from_dict(again) == ev


def test_journal_event_unknown_event_falls_back_to_misc() -> None:
    ev = JournalEvent.from_dict(
        {"ts": "2026-04-22T00:00:00Z", "event": "totally_new_event"}
    )
    assert ev.event is JournalEventType.MISC
    assert ev.original_event == "totally_new_event"
    out = ev.to_dict()
    assert out["event"] == "misc"
    assert out["original_event"] == "totally_new_event"


def test_journal_event_schema_validates_known_events() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = journal_event_schema()
    text = (FIXTURES / "journal_v1_sample.jsonl").read_text(encoding="utf-8")
    # Migrate first so the catch-all "phase_d_force_push" rewrites to misc
    from claude_org_runtime.migrate.v1_to_v2 import migrate_journal_event

    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        obj = json.loads(raw_line)
        migrated = migrate_journal_event(obj)
        jsonschema.validate(migrated, schema)


def test_org_state_parser_accepts_legacy_columns() -> None:
    text = (FIXTURES / "org_state_v1_sample.md").read_text(encoding="utf-8")
    rows = parse_worker_directory_registry(text)
    assert len(rows) == 3
    first, second, third = rows
    assert isinstance(first, WorkerDirEntry)
    # legacy "worker" column is exposed both as task_id (canonical) and worker
    assert first.task_id == "sample-worker-001"
    assert first.worker == "sample-worker-001"
    assert first.pane_id == 5
    assert first.dir == "C:/tmp/wt/sample-001"
    assert first.worker_dir == "C:/tmp/wt/sample-001"
    assert first.status is WorkerStatus.IN_USE
    # second row carries a named pane (string -> pane_name)
    assert second.pane_id is None
    assert second.pane_name == "worker-demo"
    # third row has a "-" (treated as missing) for pane
    assert third.pane_id is None
    assert third.pane_name is None
    assert third.status is WorkerStatus.SUSPENDED


def test_org_state_parser_handles_canonical_columns() -> None:
    text = (
        "## Worker Directory Registry\n\n"
        "| task_id | pane_id | pane_name | worker_dir | status |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| demo | 7 | worker-demo | C:/tmp/wt/demo | available |\n"
    )
    rows = parse_worker_directory_registry(text)
    assert len(rows) == 1
    assert rows[0].task_id == "demo"
    assert rows[0].pane_id == 7
    assert rows[0].pane_name == "worker-demo"
    assert rows[0].worker_dir == "C:/tmp/wt/demo"
    assert rows[0].status is WorkerStatus.AVAILABLE


def test_worker_dir_entry_schema_validates_polymorphic_row() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = worker_dir_entry_schema()
    legacy = {"worker": "sample", "pane": "worker-x", "dir": "C:/tmp"}
    canonical = {"task_id": "sample", "pane_name": "worker-x", "worker_dir": "C:/tmp"}
    jsonschema.validate(legacy, schema)
    jsonschema.validate(canonical, schema)
