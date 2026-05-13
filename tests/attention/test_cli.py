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


# The CLI calls ``datetime.now(timezone.utc)`` to compute pending ages,
# so timestamps relative to a hard-coded ``_FROZEN_NOW`` drift over
# wall-clock time. Issue #26's TTL ladder makes that drift load-bearing
# (an old fixture eventually slides into ``demote``/``drop`` tiers and
# changes notify behavior). Anchor fixture timestamps to real now via
# :func:`_stale_iso` and freeze the classifier's clock via
# :func:`_freeze_now` so tests stay deterministic at any future date.
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


@pytest.fixture(autouse=True)
def _freeze_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze ``attention.cli`` clock to ``_FROZEN_NOW`` for determinism.

    Without this, ``_stale_iso(30)`` slides from "30 min old" to
    "30 min + (real now − _FROZEN_NOW)" old as the calendar advances,
    eventually pushing fixture rows past the Issue #26 demote/drop
    tiers and flipping notify behavior. The patch matches the import
    path the CLI module uses so its ``datetime.now(...)`` calls see a
    stable instant.
    """
    real_datetime = datetime

    class _FrozenDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            if tz is None:
                return _FROZEN_NOW.replace(tzinfo=None)
            return _FROZEN_NOW.astimezone(tz)

    monkeypatch.setattr(
        "claude_org_runtime.attention.cli.datetime",
        _FrozenDateTime,
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


def test_scan_drop_tier_pending_honors_template_overrides(
    tmp_path: Path, capsys
) -> None:
    """A suppressed drop-tier row must still go through ``render_text``.

    Otherwise the runtime-default English title/body shows up in
    ``--json`` while every other row carries the operator's template,
    breaking machine consumers that diff against a ja template.
    """
    state_dir = tmp_path / ".state"
    state_dir.mkdir()
    make_state_db(state_dir / "state.db", [])
    write_pending_decisions(state_dir / "pending_decisions.json", [
        {
            "task_id": "T-old",
            "received_at": _stale_iso(12000),
            "raw_message": "stale",
            "status": "pending",
        },
    ])
    cfg_path = tmp_path / "attention.json"
    cfg_path.write_text(json.dumps({
        "templates": {
            "pending_decision": {
                "title": "Stale Pending",
                "body": "task_id={task_id} kind={kind}",
            },
        },
        # Also exercise truncation: title shouldn't get cut here but a
        # tight ``max_*`` would catch a regression where template
        # rendering was skipped entirely for suppressed rows.
        "max_title_chars": 40,
        "max_body_chars": 80,
    }), encoding="utf-8")

    parser = build_top_parser()
    args = parser.parse_args([
        "attention", "scan",
        "--state-dir", str(state_dir),
        "--config", str(cfg_path),
        "--json",
    ])
    args.func(args)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    drops = [ev for ev in payload if ev.get("task_id") == "T-old"]
    assert drops, payload
    assert drops[0]["suppressed"] is True
    assert drops[0]["title"] == "Stale Pending"
    assert drops[0]["body"] == "task_id=T-old kind=pending_decision"


def test_scan_drop_tier_pending_surfaces_in_json_but_not_notified(
    tmp_path: Path, capsys
) -> None:
    """Issue #26 Part A: a pending row older than ``drop`` must appear
    in ``attention scan --json`` (marked ``suppressed=True`` and
    ``delivered=False``) but must NOT be routed to ``notify`` or to
    the dedup state — operators need a triage path that doesn't burn
    a notification cycle.
    """
    state_dir = tmp_path / ".state"
    state_dir.mkdir()
    # Empty state.db so the only row classified is the pending one.
    make_state_db(state_dir / "state.db", [])
    write_pending_decisions(state_dir / "pending_decisions.json", [
        {
            "task_id": "T-old",
            # 12000 min ≈ 8.3 d, > default ``pending_decision_drop`` (7d).
            "received_at": _stale_iso(12000),
            "raw_message": "old",
            "status": "pending",
        },
    ])

    parser = build_top_parser()
    args = parser.parse_args([
        "attention", "scan",
        "--state-dir", str(state_dir),
        "--json",
    ])
    rc = args.func(args)
    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    drops = [
        ev for ev in payload
        if ev.get("task_id") == "T-old" and ev.get("kind") == "pending_decision"
    ]
    assert drops, payload
    assert drops[0]["suppressed"] is True
    assert drops[0]["delivered"] is False
    assert drops[0]["desktop_dispatched"] is False
    # No dedup file should be written — suppressed rows must not lock
    # out a future urgent re-classification if the operator re-arms
    # the entry by trimming ``received_at``.
    assert not (state_dir / "attention_notified.json").exists()


def test_scan_invalid_config_exits_cleanly(
    tmp_path: Path, capsys
) -> None:
    """Round 3 Minor: garbled config JSON should produce a clean error."""
    state_dir = tmp_path / ".state"
    state_dir.mkdir()
    cfg_path = tmp_path / "broken.json"
    cfg_path.write_text("{ not json", encoding="utf-8")

    parser = build_top_parser()
    args = parser.parse_args([
        "attention", "scan",
        "--state-dir", str(state_dir),
        "--config", str(cfg_path),
    ])
    with pytest.raises(SystemExit) as exc:
        args.func(args)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "invalid attention config" in err


def test_scan_failed_dispatch_does_not_dedup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed desktop + suppressed bell must allow the next poll to retry.

    Reproduces the round-2 codex Major: previously ``record_notified``
    fired regardless of whether anything reached the user, so a
    silently-failing ``notify-send`` left the event permanently
    suppressed.
    """
    state_dir = tmp_path / ".state"
    state_dir.mkdir()
    _populate_state(state_dir)
    # sound=off so no bell fallback masks the failure.
    cfg_path = tmp_path / "attention.json"
    cfg_path.write_text(json.dumps({"sound": "off"}), encoding="utf-8")

    # Force every event onto the linux backend with a runner that always
    # returns non-zero, simulating ``notify-send`` failing for lack of
    # DBus. ``platform.detect_backend`` is replaced so test-host's real
    # backend does not interfere.
    monkeypatch.setattr(
        "claude_org_runtime.attention.notify.detect_backend",
        lambda **kw: "linux",
    )

    class FailingProc:
        returncode = 1

    monkeypatch.setattr(
        "claude_org_runtime.attention.notify._safe_subprocess_run",
        lambda cmd: FailingProc(),
    )

    parser = build_top_parser()
    args = parser.parse_args([
        "attention", "scan",
        "--state-dir", str(state_dir),
        "--config", str(cfg_path),
    ])
    args.func(args)

    notified_path = state_dir / "attention_notified.json"
    # No event was dedup'd because nothing reached the user.
    if notified_path.exists():
        data = json.loads(notified_path.read_text(encoding="utf-8"))
        assert data["events"] == {}
        assert data["pending"] == {}


def test_scan_json_payload_delivered_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The ``--json`` payload exposes ``delivered`` so machine consumers
    can distinguish "classified" from "actually reached the user"."""
    state_dir = tmp_path / ".state"
    state_dir.mkdir()
    _populate_state(state_dir)

    parser = build_top_parser()
    args = parser.parse_args([
        "attention", "scan",
        "--state-dir", str(state_dir),
        "--dry-run", "--json",
    ])
    args.func(args)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert all("delivered" in ev for ev in payload)
