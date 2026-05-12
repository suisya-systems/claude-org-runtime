"""End-to-end CLI tests for ``claude-org-runtime attention``.

These exercise the wiring between readers → classifier → dedup →
notify (with the subprocess stubbed) and validate the §5 acceptance
criteria around ``--dry-run`` and dedup state recovery.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from claude_org_runtime.attention import cli as attention_cli
from claude_org_runtime.attention.config import AttentionConfig
from claude_org_runtime.cli import build_parser as build_top_parser

from .conftest import make_state_db, write_pending_decisions


_FROZEN_NOW = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _suppress_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent any real OS notification from firing during tests."""
    def _no_op_runner(cmd):
        return None
    monkeypatch.setattr(
        "claude_org_runtime.attention.notify._safe_subprocess_run",
        _no_op_runner,
    )


def _stale_iso(minutes: int) -> str:
    ts = _FROZEN_NOW - timedelta(minutes=minutes)
    return ts.isoformat().replace("+00:00", "Z")


def _populate_state(state_dir: Path) -> None:
    make_state_db(state_dir / "state.db", [
        {"kind": "notify_sent", "payload": {
            "kind": "approval_blocked", "task_id": "T1", "worker": "w1",
        }},
        {"kind": "ci_completed", "payload": {
            "status": "failed", "pr": 9, "task_id": "T2",
        }},
        {"kind": "worker_completed", "payload": {"task_id": "T3"}},
    ])
    write_pending_decisions(state_dir / "pending_decisions.json", [
        {
            "task_id": "T4",
            "received_at": _stale_iso(30),
            "raw_message": "?",
            "status": "pending",
        },
    ])


def test_scan_dry_run_emits_events_no_state_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    state_dir = tmp_path / ".state"
    state_dir.mkdir()
    _populate_state(state_dir)

    parser = build_top_parser()
    args = parser.parse_args([
        "attention", "scan",
        "--state-dir", str(state_dir),
        "--dry-run",
        "--json",
    ])
    rc = args.func(args)
    assert rc == 0

    captured = capsys.readouterr()
    # With --json, stdout is pure JSON; log lines go to stderr.
    payload = json.loads(captured.out)
    kinds = [ev["kind"] for ev in payload]
    assert "approval_blocked" in kinds
    assert "ci_failed" in kinds
    assert "worker_completed" in kinds
    assert "pending_decision" in kinds

    # No dedup state should be written in dry-run.
    assert not (state_dir / "attention_notified.json").exists()


def test_scan_records_dedup_state(tmp_path: Path) -> None:
    state_dir = tmp_path / ".state"
    state_dir.mkdir()
    _populate_state(state_dir)

    parser = build_top_parser()
    args = parser.parse_args([
        "attention", "scan", "--state-dir", str(state_dir),
    ])
    args.func(args)
    notified_path = state_dir / "attention_notified.json"
    assert notified_path.exists()
    data = json.loads(notified_path.read_text(encoding="utf-8"))
    assert any(k.startswith("event:") for k in data["events"])
    assert "pending:T4:pending_decision" in data["pending"]


def test_scan_second_run_dedupes(tmp_path: Path) -> None:
    """Same event row must not be classified twice."""
    state_dir = tmp_path / ".state"
    state_dir.mkdir()
    _populate_state(state_dir)

    parser = build_top_parser()
    first = parser.parse_args([
        "attention", "scan", "--state-dir", str(state_dir), "--json",
    ])
    first.func(first)

    captured: list[dict] = []
    import io
    import sys
    buf = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = buf
    try:
        second = parser.parse_args([
            "attention", "scan", "--state-dir", str(state_dir), "--json",
        ])
        second.func(second)
    finally:
        sys.stdout = real_stdout

    payload = json.loads(buf.getvalue())
    # Event rows already recorded -> no new notifications. Pending may
    # still be within cooldown -> also empty.
    assert payload == []


def test_scan_recovers_from_broken_dedup_state(
    tmp_path: Path, capsys
) -> None:
    state_dir = tmp_path / ".state"
    state_dir.mkdir()
    _populate_state(state_dir)
    (state_dir / "attention_notified.json").write_text(
        "{ broken", encoding="utf-8",
    )

    parser = build_top_parser()
    args = parser.parse_args([
        "attention", "scan", "--state-dir", str(state_dir),
    ])
    rc = args.func(args)
    assert rc == 0
    # After recovery the state file should now be valid JSON.
    data = json.loads(
        (state_dir / "attention_notified.json").read_text(encoding="utf-8"),
    )
    assert isinstance(data, dict)


def test_scan_no_state_dir_no_op(tmp_path: Path) -> None:
    state_dir = tmp_path / ".state"
    state_dir.mkdir()
    parser = build_top_parser()
    args = parser.parse_args([
        "attention", "scan", "--state-dir", str(state_dir), "--json",
    ])
    rc = args.func(args)
    assert rc == 0
    # No state.db / pending file → no notifications, no state writes.
    assert not (state_dir / "attention_notified.json").exists()


def test_watch_exits_on_max_iterations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hidden ``--max-iterations`` flag lets the watch loop terminate."""
    state_dir = tmp_path / ".state"
    state_dir.mkdir()
    _populate_state(state_dir)

    monkeypatch.setattr(attention_cli.time, "sleep", lambda _s: None)
    parser = build_top_parser()
    args = parser.parse_args([
        "attention", "watch",
        "--state-dir", str(state_dir),
        "--max-iterations", "2",
    ])
    rc = args.func(args)
    assert rc == 0


def test_scan_with_template_config(tmp_path: Path, capsys) -> None:
    """§6 integration: template override flows end-to-end."""
    state_dir = tmp_path / ".state"
    state_dir.mkdir()
    _populate_state(state_dir)

    cfg_path = tmp_path / "attention.json"
    cfg_path.write_text(json.dumps({
        "templates": {
            "ci_failed": {
                "title": "CI が失敗しました",
                "body": "PR #{pr} の CI が {status} で完了しました。",
            },
        },
    }), encoding="utf-8")

    parser = build_top_parser()
    args = parser.parse_args([
        "attention", "scan",
        "--state-dir", str(state_dir),
        "--config", str(cfg_path),
        "--dry-run",
    ])
    args.func(args)
    captured = capsys.readouterr()
    # Log lines go to stdout when --json is absent.
    assert "CI が失敗しました" in captured.out


def test_scan_json_reflects_rendered_template(tmp_path: Path, capsys) -> None:
    """``--json`` payload must show the rendered (not raw) title/body."""
    state_dir = tmp_path / ".state"
    state_dir.mkdir()
    _populate_state(state_dir)

    cfg_path = tmp_path / "attention.json"
    cfg_path.write_text(json.dumps({
        "templates": {
            "ci_failed": {
                "title": "CI Failed Override",
                "body": "PR #{pr} status={status}",
            },
        },
    }), encoding="utf-8")

    parser = build_top_parser()
    args = parser.parse_args([
        "attention", "scan",
        "--state-dir", str(state_dir),
        "--config", str(cfg_path),
        "--dry-run", "--json",
    ])
    args.func(args)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    ci = next(ev for ev in payload if ev["kind"] == "ci_failed")
    assert ci["title"] == "CI Failed Override"
    assert ci["body"].startswith("PR #")
    assert "status=failed" in ci["body"]


def test_scan_severity_override_via_config(tmp_path: Path, capsys) -> None:
    """``config.notify`` overrides reach the JSON payload."""
    state_dir = tmp_path / ".state"
    state_dir.mkdir()
    _populate_state(state_dir)

    cfg_path = tmp_path / "attention.json"
    cfg_path.write_text(json.dumps({
        "notify": {"worker_completed": "urgent"},
    }), encoding="utf-8")

    parser = build_top_parser()
    args = parser.parse_args([
        "attention", "scan",
        "--state-dir", str(state_dir),
        "--config", str(cfg_path),
        "--dry-run", "--json",
    ])
    args.func(args)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    wc = next(ev for ev in payload if ev["kind"] == "worker_completed")
    assert wc["severity"] == "urgent"
