"""Tests for ``claude_org_runtime.attention.classifier``.

Covers every §5 classification row plus the §6 "default title/body
reflects the AttentionEvent fields" baseline.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from claude_org_runtime.attention.classifier import (
    AttentionEvent,
    classify_all,
    classify_event,
    classify_pending,
)

_NOW = datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc)


def _row(
    *,
    id: int = 1,
    kind: str,
    payload: dict | None = None,
    actor: str | None = None,
    occurred_at: str = "2026-05-12T11:30:00Z",
) -> dict:
    return {
        "id": id,
        "occurred_at": occurred_at,
        "actor": actor,
        "kind": kind,
        "payload": payload or {},
    }


# ---------------------------------------------------------------------------
# notify_sent subtypes
# ---------------------------------------------------------------------------


def test_notify_sent_approval_blocked_urgent() -> None:
    ev = classify_event(_row(
        kind="notify_sent",
        payload={
            "kind": "approval_blocked",
            "task_id": "issue-19-20",
            "worker": "worker-foo",
        },
    ))
    assert ev is not None
    assert ev.kind == "approval_blocked"
    assert ev.severity == "urgent"
    assert ev.task_id == "issue-19-20"
    assert ev.worker == "worker-foo"
    assert ev.key == "event:1"


def test_notify_sent_relay_gap_urgent() -> None:
    ev = classify_event(_row(
        kind="notify_sent",
        payload={"kind": "relay_gap_suspected", "task_id": "T1"},
    ))
    assert ev is not None
    assert ev.kind == "relay_gap_suspected"
    assert ev.severity == "urgent"


def test_notify_sent_silent_worker_urgent() -> None:
    ev = classify_event(_row(
        kind="notify_sent",
        payload={"kind": "pane_output_without_peer_msg", "worker": "wkr"},
    ))
    assert ev is not None
    assert ev.kind == "silent_worker_output"
    assert ev.severity == "urgent"


def test_notify_sent_unknown_subkind_ignored() -> None:
    ev = classify_event(_row(
        kind="notify_sent", payload={"kind": "heartbeat"},
    ))
    assert ev is None


# ---------------------------------------------------------------------------
# ci_completed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["failed", "canceled", "incomplete"])
def test_ci_completed_failure_urgent(status: str) -> None:
    ev = classify_event(_row(
        kind="ci_completed",
        payload={"status": status, "pr": 42, "task_id": "ci-pr-42"},
    ))
    assert ev is not None
    assert ev.kind == "ci_failed"
    assert ev.severity == "urgent"
    assert ev.pr == 42
    assert ev.status == status


def test_ci_completed_success_ignored() -> None:
    ev = classify_event(_row(
        kind="ci_completed", payload={"status": "success", "pr": 1},
    ))
    assert ev is None


# ---------------------------------------------------------------------------
# worker_completed / pr_merged
# ---------------------------------------------------------------------------


def test_worker_completed_normal() -> None:
    ev = classify_event(_row(
        kind="worker_completed",
        payload={"task_id": "issue-19", "worker": "worker-19"},
    ))
    assert ev is not None
    assert ev.kind == "worker_completed"
    assert ev.severity == "normal"


def test_pr_merged_normal() -> None:
    ev = classify_event(_row(
        kind="pr_merged", payload={"pr": 7, "task_id": "issue-7"},
    ))
    assert ev is not None
    assert ev.kind == "pr_merged"
    assert ev.severity == "normal"
    assert ev.pr == 7


# ---------------------------------------------------------------------------
# progress / unknown events
# ---------------------------------------------------------------------------


def test_progress_event_ignored() -> None:
    # The reader narrows the SELECT to relevant kinds, but if a stray
    # row makes it through the classifier must still ignore it.
    assert classify_event(_row(kind="heartbeat")) is None
    assert classify_event(_row(kind="anomaly_observed")) is None


# ---------------------------------------------------------------------------
# pending decisions
# ---------------------------------------------------------------------------


def test_stale_pending_decision_urgent() -> None:
    received = (_NOW - timedelta(minutes=20)).isoformat().replace(
        "+00:00", "Z",
    )
    entry = {
        "task_id": "stuck-task",
        "received_at": received,
        "raw_message": "should we split this PR?",
        "status": "pending",
    }
    ev = classify_pending(
        entry, _NOW, pending_decision_min=15, user_replied_min=15,
    )
    assert ev is not None
    assert ev.kind == "pending_decision"
    assert ev.severity == "urgent"
    assert ev.task_id == "stuck-task"
    assert ev.key == "pending:stuck-task:pending_decision"


def test_fresh_pending_decision_not_urgent() -> None:
    received = (_NOW - timedelta(minutes=5)).isoformat().replace(
        "+00:00", "Z",
    )
    entry = {
        "task_id": "fresh",
        "received_at": received,
        "raw_message": "?",
        "status": "pending",
    }
    assert classify_pending(
        entry, _NOW, pending_decision_min=15, user_replied_min=15,
    ) is None


def test_user_reply_not_forwarded_urgent() -> None:
    replied = (_NOW - timedelta(minutes=20)).isoformat().replace(
        "+00:00", "Z",
    )
    entry = {
        "task_id": "T2",
        "received_at": "2026-05-12T10:00:00Z",
        "raw_message": "?",
        "status": "escalated",
        "user_replied_at": replied,
    }
    ev = classify_pending(
        entry, _NOW, pending_decision_min=15, user_replied_min=15,
    )
    assert ev is not None
    assert ev.kind == "user_reply_not_forwarded"
    assert ev.severity == "urgent"
    assert ev.key == "pending:T2:user_reply_not_forwarded"


def test_user_reply_recent_no_event() -> None:
    replied = (_NOW - timedelta(minutes=5)).isoformat().replace(
        "+00:00", "Z",
    )
    entry = {
        "task_id": "T2",
        "received_at": "2026-05-12T10:00:00Z",
        "raw_message": "?",
        "status": "escalated",
        "user_replied_at": replied,
    }
    assert classify_pending(
        entry, _NOW, pending_decision_min=15, user_replied_min=15,
    ) is None


def test_resolved_pending_ignored() -> None:
    entry = {
        "task_id": "done",
        "received_at": "2026-04-01T00:00:00Z",  # very old
        "raw_message": "?",
        "status": "resolved",
        "resolution_kind": "to_worker",
    }
    assert classify_pending(
        entry, _NOW, pending_decision_min=15, user_replied_min=15,
    ) is None


def test_classify_all_combines_inputs() -> None:
    events = [
        _row(id=10, kind="worker_completed", payload={"task_id": "x"}),
        _row(id=11, kind="ci_completed", payload={"status": "failed", "pr": 1}),
    ]
    received = (_NOW - timedelta(minutes=30)).isoformat().replace(
        "+00:00", "Z",
    )
    pending = [{
        "task_id": "stuck",
        "received_at": received,
        "raw_message": "?",
        "status": "pending",
    }]
    out = classify_all(
        events, pending, _NOW, pending_decision_min=15, user_replied_min=15,
    )
    kinds = [ev.kind for ev in out]
    assert kinds == ["worker_completed", "ci_failed", "pending_decision"]


def test_event_default_title_uses_runtime_text() -> None:
    ev = classify_event(_row(
        kind="ci_completed",
        payload={"status": "failed", "pr": 99, "task_id": "x"},
    ))
    assert ev is not None
    # §6 fallback: when no template override, the classifier emits the
    # bundled English default text into title/body.
    assert ev.title == "CI failed"
    assert "99" in ev.body


def test_missing_id_returns_none() -> None:
    row = _row(kind="worker_completed")
    row.pop("id")
    assert classify_event(row) is None


def test_pending_missing_task_id_returns_none() -> None:
    entry = {
        "received_at": "2026-05-12T10:00:00Z",
        "raw_message": "?",
        "status": "pending",
    }
    assert classify_pending(
        entry, _NOW, pending_decision_min=15, user_replied_min=15,
    ) is None


# ---------------------------------------------------------------------------
# notify_map severity override (Issue #19 / §5 config schema)
# ---------------------------------------------------------------------------


def test_notify_map_overrides_severity_event() -> None:
    """A config ``notify`` override must reach the emitted AttentionEvent."""
    ev = classify_event(
        _row(kind="worker_completed", payload={"task_id": "t"}),
        notify_map={"worker_completed": "urgent"},
    )
    assert ev is not None
    assert ev.severity == "urgent"


def test_notify_map_overrides_severity_pending() -> None:
    received = (_NOW - timedelta(minutes=30)).isoformat().replace(
        "+00:00", "Z",
    )
    entry = {
        "task_id": "T",
        "received_at": received,
        "raw_message": "?",
        "status": "pending",
    }
    ev = classify_pending(
        entry, _NOW, pending_decision_min=15, user_replied_min=15,
        notify_map={"pending_decision": "normal"},
    )
    assert ev is not None
    assert ev.severity == "normal"


def test_notify_map_unknown_value_falls_back_to_default() -> None:
    """An invalid override is ignored — design defaults stand."""
    ev = classify_event(
        _row(kind="ci_completed", payload={"status": "failed", "pr": 1}),
        notify_map={"ci_failed": "loud"},  # type: ignore[dict-item]
    )
    assert ev is not None
    assert ev.severity == "urgent"  # design default


# ---------------------------------------------------------------------------
# Expanded notify_sent subkind coverage (round 2 codex feedback)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "subkind,expected_kind",
    [
        ("pane_silent", "pane_silent"),
        ("pane_crashed", "pane_crashed"),
        ("worker_stalled", "worker_stalled"),
        ("worker_not_reported", "worker_not_reported"),
        ("error", "worker_error"),
    ],
)
def test_notify_sent_production_subkinds_urgent(
    subkind: str, expected_kind: str,
) -> None:
    """AnomalyKind enum values + dispatcher's ``error`` tag must classify.

    Codex round 2 caught that the design's 3-row table did not match
    production. These are urgent because the human is the only
    recovery path for a stalled / crashed / silent worker.
    """
    ev = classify_event(_row(
        kind="notify_sent",
        payload={"kind": subkind, "worker": "w1", "task_id": "t1"},
    ))
    assert ev is not None
    assert ev.kind == expected_kind
    assert ev.severity == "urgent"
