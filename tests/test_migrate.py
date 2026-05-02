"""Tests for the v1 -> v2 polymorphic migrate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_org_runtime.migrate.v1_to_v2 import (
    main,
    migrate_journal_event,
    migrate_journal_lines,
    migrate_org_state_markdown,
)
from claude_org_runtime.schema.json_schema import (
    journal_event_schema,
    worker_dir_entry_schema,
)
from claude_org_runtime.schema.org_state import parse_worker_directory_registry

FIXTURES = Path(__file__).parent / "fixtures" / "synthetic"


def test_migrate_event_keeps_legacy_keys_alongside_canonical() -> None:
    legacy = {
        "ts": "2026-04-19T21:00:30Z",
        "event": "worker_spawned",
        "worker": "sample-worker-001",
        "task": "demo-task",
        "dir": "C:/tmp/wt/sample",
        "pane": 5,
    }
    out = migrate_journal_event(legacy)
    # legacy keys preserved
    assert out["worker"] == "sample-worker-001"
    assert out["dir"] == "C:/tmp/wt/sample"
    assert out["pane"] == 5
    # canonical keys added; task slug wins over opaque worker handle
    assert out["task_id"] == "demo-task"
    assert out["worker_dir"] == "C:/tmp/wt/sample"
    assert out["pane_id"] == 5


def test_migrate_event_falls_back_to_worker_when_task_absent() -> None:
    out = migrate_journal_event(
        {"ts": "x", "event": "pane_closed", "worker": "sample-worker-001"}
    )
    assert out["task_id"] == "sample-worker-001"
    assert out["worker"] == "sample-worker-001"


def test_migrate_event_polymorphic_pane_string_named() -> None:
    out = migrate_journal_event(
        {"ts": "x", "event": "pane_closed", "pane": "worker-demo"}
    )
    assert out["pane_name"] == "worker-demo"
    assert "pane_id" not in out


def test_migrate_event_unknown_event_becomes_misc() -> None:
    out = migrate_journal_event(
        {"ts": "x", "event": "totally_unseen_event_xyz", "note": "n"}
    )
    assert out["event"] == "misc"
    assert out["original_event"] == "totally_unseen_event_xyz"
    assert out["note"] == "n"


def test_migrate_event_canonical_input_is_idempotent() -> None:
    once = migrate_journal_event(
        {
            "ts": "x",
            "event": "worker_spawned",
            "task_id": "demo",
            "worker_dir": "C:/tmp",
        }
    )
    twice = migrate_journal_event(once)
    assert once == twice


def test_journal_round_trip_validates_against_schema() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = journal_event_schema()
    src = (FIXTURES / "journal_v1_sample.jsonl").read_text(encoding="utf-8")
    migrated = list(migrate_journal_lines(src.splitlines()))
    assert len(migrated) == len(src.splitlines())
    for line in migrated:
        if not line.strip():
            continue
        obj = json.loads(line)
        jsonschema.validate(obj, schema)


def test_org_state_markdown_migration_preserves_rows() -> None:
    src = (FIXTURES / "org_state_v1_sample.md").read_text(encoding="utf-8")
    migrated = migrate_org_state_markdown(src)

    rows_before = parse_worker_directory_registry(src)
    rows_after = parse_worker_directory_registry(migrated)
    assert len(rows_before) == len(rows_after) == 3
    # canonical columns now populated directly from the rewritten header
    assert rows_after[0].task_id == "sample-worker-001"
    assert rows_after[0].worker_dir == "C:/tmp/wt/sample-001"
    # legacy columns still present in the raw markdown
    assert "| worker " in migrated
    assert "| dir " in migrated
    assert "| task_id " in migrated
    assert "| worker_dir " in migrated
    # canonical pane_id / pane_name columns inserted alongside legacy pane
    assert "| pane " in migrated
    assert "| pane_id " in migrated
    assert "| pane_name " in migrated
    # numeric pane (5) routes to pane_id; named pane (worker-demo) to pane_name
    assert rows_after[0].pane_id == 5
    assert rows_after[0].pane_name is None
    assert rows_after[1].pane_id is None
    assert rows_after[1].pane_name == "worker-demo"


def test_migrated_org_state_rows_validate_against_schema() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    schema = worker_dir_entry_schema()
    src = (FIXTURES / "org_state_v1_sample.md").read_text(encoding="utf-8")
    migrated = migrate_org_state_markdown(src)
    rows = parse_worker_directory_registry(migrated)
    for row in rows:
        payload = {
            "task_id": row.task_id,
            "pane_id": row.pane_id,
            "pane_name": row.pane_name,
            "worker_dir": row.worker_dir,
            "status": row.status.value if row.status else None,
            "note": row.note,
            "worker": row.worker,
            "dir": row.dir,
        }
        jsonschema.validate(payload, schema)


def test_cli_writes_files(tmp_path: Path) -> None:
    src = FIXTURES / "journal_v1_sample.jsonl"
    dst = tmp_path / "out.jsonl"
    rc = main(["--in", str(src), "--out", str(dst)])
    assert rc == 0
    assert dst.exists()
    # Each non-empty input line yields one non-empty output line
    in_lines = [l for l in src.read_text(encoding="utf-8").splitlines() if l.strip()]
    out_lines = [l for l in dst.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(in_lines) == len(out_lines)


def test_cli_org_state(tmp_path: Path) -> None:
    src = FIXTURES / "org_state_v1_sample.md"
    dst = tmp_path / "out.md"
    rc = main(["--in", str(src), "--out", str(dst)])
    assert rc == 0
    assert "task_id" in dst.read_text(encoding="utf-8")
