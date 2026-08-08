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


def test_scan_demote_tier_pending_emits_normal_via_real_config(
    tmp_path: Path, capsys
) -> None:
    """End-to-end check that ``max ≤ age < drop`` produces ``normal``.

    Round-4 codex caught that the pre-fix ``cfg.notify`` shape pre-
    filled DEFAULT_NOTIFY, which fooled :func:`_severity_for` into
    treating every default as an explicit operator override and
    bypassing TTL demote. This test runs the real
    :class:`AttentionConfig` defaults through the CLI to guard against
    a regression of that shape.
    """
    state_dir = tmp_path / ".state"
    state_dir.mkdir()
    make_state_db(state_dir / "state.db", [])
    write_pending_decisions(state_dir / "pending_decisions.json", [
        {
            "task_id": "T-demote",
            # 1500 min ≈ 25h, > default ``pending_decision_max`` (24h)
            # but < default ``pending_decision_drop`` (7d).
            "received_at": _stale_iso(1500),
            "raw_message": "demote",
            "status": "pending",
        },
    ])

    parser = build_top_parser()
    args = parser.parse_args([
        "attention", "scan",
        "--state-dir", str(state_dir),
        "--dry-run",
        "--json",
    ])
    args.func(args)
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    demoted = [
        ev for ev in payload
        if ev.get("task_id") == "T-demote"
        and ev.get("kind") == "pending_decision"
    ]
    assert demoted, payload
    assert demoted[0]["severity"] == "normal"
    assert demoted[0].get("suppressed") is not True


def test_scan_pending_explicit_severity_override_resists_ttl_demote(
    tmp_path: Path, capsys
) -> None:
    """An explicit ``notify`` config override must beat the TTL demote.

    The companion to ``test_scan_demote_tier_pending_emits_normal_via_real_config``:
    when the operator pins ``pending_decision: urgent`` in the config,
    even a 25h-old row must surface as ``urgent`` rather than the
    TTL-demoted ``normal``. Round-4 fix kept this path working by
    making ``cfg.notify`` sparse so a real override is still
    distinguishable.
    """
    state_dir = tmp_path / ".state"
    state_dir.mkdir()
    make_state_db(state_dir / "state.db", [])
    write_pending_decisions(state_dir / "pending_decisions.json", [
        {
            "task_id": "T-pinned",
            "received_at": _stale_iso(1500),  # demote tier age.
            "raw_message": "pinned",
            "status": "pending",
        },
    ])
    cfg_path = tmp_path / "attention.json"
    cfg_path.write_text(json.dumps({
        "notify": {"pending_decision": "urgent"},
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
    pinned = [
        ev for ev in payload
        if ev.get("task_id") == "T-pinned"
        and ev.get("kind") == "pending_decision"
    ]
    assert pinned, payload
    assert pinned[0]["severity"] == "urgent"


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


# ---------------------------------------------------------------------------
# Broker journal consumer (Issue #167)
# ---------------------------------------------------------------------------


def _write_duplicate(
    broker_dir: Path,
    *,
    age_sec: float = 5.0,
    owner: str = "secretary",
    instances=("inst-a", "inst-b"),
) -> Path:
    """Append one ``duplicate_sidecar_detected`` line aged off the frozen now."""
    broker_dir.mkdir(parents=True, exist_ok=True)
    path = broker_dir / "queue.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": _FROZEN_NOW.timestamp() - age_sec,
            "event": "duplicate_sidecar_detected",
            "owner": owner,
            "instances": list(instances),
        }) + "\n")
    return path


def _scan_json(parser, argv: list[str]) -> list[dict]:
    import io
    import sys
    buf = io.StringIO()
    real_stdout = sys.stdout
    sys.stdout = buf
    try:
        args = parser.parse_args(argv)
        assert args.func(args) == 0
    finally:
        sys.stdout = real_stdout
    return json.loads(buf.getvalue())


def test_scan_surfaces_duplicate_sidecar_from_broker_journal(
    tmp_path: Path,
) -> None:
    """Issue #167 acceptance: the journal line becomes an operator signal."""
    state_dir = tmp_path / ".state"
    state_dir.mkdir()
    _write_duplicate(state_dir / "broker")

    payload = _scan_json(build_top_parser(), [
        "attention", "scan", "--state-dir", str(state_dir), "--json",
    ])
    dups = [ev for ev in payload if ev["kind"] == "duplicate_sidecar"]
    assert len(dups) == 1
    assert dups[0]["severity"] == "urgent"
    assert dups[0]["worker"] == "secretary"
    assert "inst-a" in dups[0]["body"] and "inst-b" in dups[0]["body"]
    assert dups[0]["delivered"] is True


def test_scan_ignores_duplicate_older_than_window(tmp_path: Path) -> None:
    """A resolved incident falls silent once it stops re-firing."""
    state_dir = tmp_path / ".state"
    state_dir.mkdir()
    _write_duplicate(state_dir / "broker", age_sec=3600.0)

    payload = _scan_json(build_top_parser(), [
        "attention", "scan", "--state-dir", str(state_dir), "--json",
    ])
    assert [ev for ev in payload if ev["kind"] == "duplicate_sidecar"] == []


def test_scan_duplicate_sidecar_dedupes_within_cooldown(tmp_path: Path) -> None:
    """Repeated journal lines for one pair must not ring on every poll."""
    state_dir = tmp_path / ".state"
    state_dir.mkdir()
    _write_duplicate(state_dir / "broker", age_sec=60.0)
    _write_duplicate(state_dir / "broker", age_sec=30.0)
    _write_duplicate(state_dir / "broker", age_sec=5.0)

    parser = build_top_parser()
    argv = ["attention", "scan", "--state-dir", str(state_dir), "--json"]
    first = _scan_json(parser, argv)
    assert len([ev for ev in first if ev["kind"] == "duplicate_sidecar"]) == 1
    # Cooldown-gated (not write-once): the key lands in the ``pending``
    # namespace so it re-alerts later, but not on the next poll.
    dedup = json.loads(
        (state_dir / "attention_notified.json").read_text(encoding="utf-8"),
    )
    assert any(
        k.startswith("broker:duplicate_sidecar:secretary:")
        for k in dedup["pending"]
    )
    second = _scan_json(parser, argv)
    assert [ev for ev in second if ev["kind"] == "duplicate_sidecar"] == []


def test_scan_duplicate_sidecar_new_pair_is_not_swallowed(
    tmp_path: Path,
) -> None:
    """Killing one session and getting a new competitor is a new incident."""
    state_dir = tmp_path / ".state"
    state_dir.mkdir()
    _write_duplicate(state_dir / "broker", instances=("inst-a", "inst-b"))

    parser = build_top_parser()
    argv = ["attention", "scan", "--state-dir", str(state_dir), "--json"]
    _scan_json(parser, argv)
    _write_duplicate(state_dir / "broker", instances=("inst-a", "inst-c"))
    second = _scan_json(parser, argv)
    dups = [ev for ev in second if ev["kind"] == "duplicate_sidecar"]
    assert len(dups) == 1
    assert "inst-c" in dups[0]["body"]


def test_scan_without_broker_journal_is_a_no_op(tmp_path: Path) -> None:
    """No broker state dir (broker never ran) must not disturb the scan."""
    state_dir = tmp_path / ".state"
    state_dir.mkdir()
    _populate_state(state_dir)
    payload = _scan_json(build_top_parser(), [
        "attention", "scan", "--state-dir", str(state_dir), "--json",
    ])
    assert [ev for ev in payload if ev["kind"] == "duplicate_sidecar"] == []
    assert payload  # the ordinary .state events still classify


def test_scan_broker_state_dir_override(tmp_path: Path) -> None:
    """A daemon started with a non-default --state-dir is still reachable."""
    state_dir = tmp_path / ".state"
    state_dir.mkdir()
    elsewhere = tmp_path / "elsewhere" / "broker"
    _write_duplicate(elsewhere)

    payload = _scan_json(build_top_parser(), [
        "attention", "scan", "--state-dir", str(state_dir),
        "--broker-state-dir", str(elsewhere), "--json",
    ])
    assert [ev["kind"] for ev in payload] == ["duplicate_sidecar"]


def test_watch_surfaces_duplicate_sidecar(tmp_path: Path) -> None:
    """The watch loop (not just one-shot scan) reads the broker journal."""
    state_dir = tmp_path / ".state"
    state_dir.mkdir()
    _write_duplicate(state_dir / "broker")

    parser = build_top_parser()
    args = parser.parse_args([
        "attention", "watch", "--state-dir", str(state_dir),
        "--max-iterations", "1",
    ])
    assert args.func(args) == 0
    dedup = json.loads(
        (state_dir / "attention_notified.json").read_text(encoding="utf-8"),
    )
    assert any(
        k.startswith("broker:duplicate_sidecar:") for k in dedup["pending"]
    )
