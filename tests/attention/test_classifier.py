"""Tests for ``claude_org_runtime.attention.classifier``.

Covers every §5 classification row plus the §6 "default title/body
reflects the AttentionEvent fields" baseline.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from claude_org_runtime.attention.classifier import (
    AttentionEvent,
    _NOTIFY_SUBKIND_TO_KIND,
    _default_text,
    _iso_from_epoch,
    classify_all,
    classify_broker_duplicates,
    classify_duplicate_sidecar,
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


def test_notify_sent_relay_gap_normal() -> None:
    """Issue #26 Part B: anomaly-detector signals ride at ``normal``."""
    ev = classify_event(_row(
        kind="notify_sent",
        payload={"kind": "relay_gap_suspected", "task_id": "T1"},
    ))
    assert ev is not None
    assert ev.kind == "relay_gap_suspected"
    assert ev.severity == "normal"


def test_notify_sent_silent_worker_normal() -> None:
    """Issue #26 Part B: best-effort relay signal demoted to ``normal``."""
    ev = classify_event(_row(
        kind="notify_sent",
        payload={"kind": "pane_output_without_peer_msg", "worker": "wkr"},
    ))
    assert ev is not None
    assert ev.kind == "silent_worker_output"
    assert ev.severity == "normal"


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


# ---------------------------------------------------------------------------
# Issue #26 Part A: pending_decision TTL ladder (min / max / drop)
# ---------------------------------------------------------------------------


def _pending(received_ago_min: float) -> dict:
    """Helper: a pending entry whose ``received_at`` is N minutes ago."""
    received = (_NOW - timedelta(minutes=received_ago_min)).isoformat().replace(
        "+00:00", "Z",
    )
    return {
        "task_id": "ttl-task",
        "received_at": received,
        "raw_message": "should we ship?",
        "status": "pending",
    }


def _user_replied(replied_ago_min: float) -> dict:
    """Helper: an escalated entry whose ``user_replied_at`` is N minutes ago."""
    replied = (_NOW - timedelta(minutes=replied_ago_min)).isoformat().replace(
        "+00:00", "Z",
    )
    return {
        "task_id": "ttl-reply",
        "received_at": "2026-05-01T00:00:00Z",
        "raw_message": "go ahead",
        "status": "escalated",
        "user_replied_at": replied,
    }


def test_pending_decision_ttl_below_min_no_event() -> None:
    """age < pending_decision_min → no event (entry is still fresh)."""
    ev = classify_pending(
        _pending(received_ago_min=5), _NOW,
        pending_decision_min=15, user_replied_min=15,
        pending_decision_max=1440, pending_decision_drop=10080,
    )
    assert ev is None


def test_pending_decision_ttl_min_to_max_urgent() -> None:
    """min ≤ age < max → urgent (escalate)."""
    # 60 min ≥ 15 (min) but well below 1440 (max).
    ev = classify_pending(
        _pending(received_ago_min=60), _NOW,
        pending_decision_min=15, user_replied_min=15,
        pending_decision_max=1440, pending_decision_drop=10080,
    )
    assert ev is not None
    assert ev.kind == "pending_decision"
    assert ev.severity == "urgent"


def test_pending_decision_ttl_max_to_drop_demoted_to_normal() -> None:
    """max ≤ age < drop → severity demoted from urgent to normal."""
    # 1500 min (25h) > 1440 (max) but < 10080 (drop).
    ev = classify_pending(
        _pending(received_ago_min=1500), _NOW,
        pending_decision_min=15, user_replied_min=15,
        pending_decision_max=1440, pending_decision_drop=10080,
    )
    assert ev is not None
    assert ev.kind == "pending_decision"
    assert ev.severity == "normal"


def test_pending_decision_ttl_above_drop_suppressed_for_notify() -> None:
    """age ≥ pending_decision_drop → still emitted but ``suppressed=True``.

    The classifier surfaces the row so ``attention scan --json`` can
    list it for triage; the dispatcher in cli.py is what skips routing
    it to ``notify``.
    """
    ev = classify_pending(
        _pending(received_ago_min=11000), _NOW,
        pending_decision_min=15, user_replied_min=15,
        pending_decision_max=1440, pending_decision_drop=10080,
    )
    assert ev is not None
    assert ev.kind == "pending_decision"
    assert ev.suppressed is True
    # Dropped rows are de-escalated severity-wise; the ``suppressed``
    # marker is the real signal to consumers.
    assert ev.severity == "normal"
    payload = ev.to_dict()
    assert payload["suppressed"] is True


def test_pending_decision_demotion_respects_notify_map_override() -> None:
    """An explicit ``notify_map`` override still wins over demotion.

    Ops can pin ``"urgent"`` on a long-lived event class via config;
    the TTL ladder should not silently override that intent.
    """
    ev = classify_pending(
        _pending(received_ago_min=1500), _NOW,
        pending_decision_min=15, user_replied_min=15,
        pending_decision_max=1440, pending_decision_drop=10080,
        notify_map={"pending_decision": "urgent"},
    )
    assert ev is not None
    assert ev.severity == "urgent"


def test_user_reply_not_forwarded_ttl_below_min_no_event() -> None:
    ev = classify_pending(
        _user_replied(replied_ago_min=5), _NOW,
        pending_decision_min=15, user_replied_min=15,
        pending_decision_max=1440, pending_decision_drop=10080,
    )
    assert ev is None


def test_user_reply_not_forwarded_ttl_min_to_max_urgent() -> None:
    ev = classify_pending(
        _user_replied(replied_ago_min=60), _NOW,
        pending_decision_min=15, user_replied_min=15,
        pending_decision_max=1440, pending_decision_drop=10080,
    )
    assert ev is not None
    assert ev.kind == "user_reply_not_forwarded"
    assert ev.severity == "urgent"


def test_user_reply_not_forwarded_ttl_max_to_drop_demoted_to_normal() -> None:
    ev = classify_pending(
        _user_replied(replied_ago_min=1500), _NOW,
        pending_decision_min=15, user_replied_min=15,
        pending_decision_max=1440, pending_decision_drop=10080,
    )
    assert ev is not None
    assert ev.kind == "user_reply_not_forwarded"
    assert ev.severity == "normal"


def test_user_reply_not_forwarded_ttl_above_drop_suppressed_for_notify() -> None:
    ev = classify_pending(
        _user_replied(replied_ago_min=11000), _NOW,
        pending_decision_min=15, user_replied_min=15,
        pending_decision_max=1440, pending_decision_drop=10080,
    )
    assert ev is not None
    assert ev.kind == "user_reply_not_forwarded"
    assert ev.suppressed is True
    assert ev.severity == "normal"


def test_user_reply_not_forwarded_skipped_when_resolution_is_to_worker() -> None:
    """Once the secretary has forwarded the reply, the alert must clear.

    Even if ``status`` lingers at ``escalated`` and ``user_replied_at``
    is old, an explicit ``resolution_kind == 'to_worker'`` marker means
    the gap closed and the urgent classification no longer applies.
    """
    entry = _user_replied(replied_ago_min=60)
    entry["resolution_kind"] = "to_worker"
    assert classify_pending(
        entry, _NOW,
        pending_decision_min=15, user_replied_min=15,
        pending_decision_max=1440, pending_decision_drop=10080,
    ) is None


def test_user_reply_not_forwarded_fires_for_other_resolution_kinds() -> None:
    """Non-``to_worker`` resolution_kind values still fire the alert.

    Only ``to_worker`` indicates the relay actually completed; any
    other value (or a missing field) leaves the gap open.
    """
    entry = _user_replied(replied_ago_min=60)
    entry["resolution_kind"] = "answered"
    ev = classify_pending(
        entry, _NOW,
        pending_decision_min=15, user_replied_min=15,
        pending_decision_max=1440, pending_decision_drop=10080,
    )
    assert ev is not None
    assert ev.kind == "user_reply_not_forwarded"


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


def test_malformed_received_at_treated_as_stale() -> None:
    """Round 4 codex Minor: garbled timestamps must fire alerts, not hide them."""
    entry = {
        "task_id": "garbled",
        "received_at": "not-a-real-timestamp",
        "raw_message": "?",
        "status": "pending",
    }
    ev = classify_pending(
        entry, _NOW, pending_decision_min=15, user_replied_min=15,
    )
    assert ev is not None
    assert ev.kind == "pending_decision"
    assert ev.severity == "urgent"


def test_missing_received_at_treated_as_stale() -> None:
    entry = {
        "task_id": "no-ts",
        "raw_message": "?",
        "status": "pending",
    }
    ev = classify_pending(
        entry, _NOW, pending_decision_min=15, user_replied_min=15,
    )
    assert ev is not None
    assert ev.kind == "pending_decision"


@pytest.mark.parametrize(
    "subkind,expected_kind,expected_severity",
    [
        # Issue #26 Part B: only ``pane_crashed`` keeps ``urgent`` —
        # the others are best-effort anomaly signals that often
        # self-resolve, so they ride at ``normal`` to avoid alert fatigue.
        ("pane_silent", "pane_silent", "normal"),
        ("pane_crashed", "pane_crashed", "urgent"),
        ("worker_stalled", "worker_stalled", "normal"),
        ("worker_not_reported", "worker_not_reported", "normal"),
        ("error", "worker_error", "normal"),
    ],
)
def test_notify_sent_production_subkinds_severity(
    subkind: str, expected_kind: str, expected_severity: str,
) -> None:
    """AnomalyKind enum values + dispatcher's ``error`` tag must classify.

    Codex round 2 originally caught that the design's 3-row table did
    not match production. Issue #26 Part B then rebalanced severity:
    a crashed pane is the only one a human has to look at right now;
    silent / stalled / not-reported / generic-error are softer signals.
    """
    ev = classify_event(_row(
        kind="notify_sent",
        payload={"kind": subkind, "worker": "w1", "task_id": "t1"},
    ))
    assert ev is not None
    assert ev.kind == expected_kind
    assert ev.severity == expected_severity


# ---------------------------------------------------------------------------
# Issue #28: secretary_awaiting_user (notify_sent subkind 'awaiting_user')
# ---------------------------------------------------------------------------


def test_notify_subkind_table_includes_awaiting_user() -> None:
    """Issue #28 runtime: 'awaiting_user' → 'secretary_awaiting_user'."""
    assert _NOTIFY_SUBKIND_TO_KIND["awaiting_user"] == "secretary_awaiting_user"


def test_notify_sent_awaiting_user_classifies_urgent() -> None:
    """notify_sent payload kind='awaiting_user' must surface as urgent.

    Issue #28: the secretary paused for the user — "human is the sole
    recovery path", so the kind joins approval_blocked / pending_decision
    in the urgent tier by default.
    """
    ev = classify_event(_row(
        kind="notify_sent",
        payload={
            "kind": "awaiting_user",
            "task_id": "issue-28",
            "worker": "secretary",
        },
    ))
    assert ev is not None
    assert ev.kind == "secretary_awaiting_user"
    assert ev.severity == "urgent"
    assert ev.task_id == "issue-28"


def test_secretary_awaiting_user_default_text_non_empty() -> None:
    """_default_text must return real strings (ja templates override UX)."""
    title, body = _default_text(
        "secretary_awaiting_user", task_id="issue-28",
    )
    assert title
    assert body
    # task_id must reach the body — it's the only identifying field on a
    # paused-secretary notification, and ja templates lean on it.
    assert "issue-28" in body


# ---------------------------------------------------------------------------
# duplicate_sidecar — broker journal consumer (Issue #167)
# ---------------------------------------------------------------------------


def _dup_row(ts: float = 1000.0, owner="sec", instances=("b1", "a2")) -> dict:
    return {"ts": ts, "owner": owner, "instances": list(instances)}


def test_duplicate_sidecar_names_owner_and_both_instances() -> None:
    """Acceptance: the signal identifies which sessions are competing."""
    ev = classify_duplicate_sidecar(_dup_row())
    assert ev.kind == "duplicate_sidecar"
    assert ev.severity == "urgent"
    assert ev.worker == "sec"
    # Sorted so the key does not depend on journal write order.
    assert ev.summary == "a2, b1"
    assert "sec" in ev.body
    assert "a2, b1" in ev.body
    assert ev.title == "Duplicate channel sidecar"


def test_duplicate_sidecar_is_cooldown_gated_not_write_once() -> None:
    """Source must stay out of the ``state.db.events`` dedup namespace.

    ``dedup.should_notify`` records ``state.db.events`` keys forever; a
    live double sidecar has to keep re-alerting on the cooldown cadence.
    """
    ev = classify_duplicate_sidecar(_dup_row())
    assert ev.source == "broker.queue.jsonl"


def test_duplicate_sidecar_key_is_per_contesting_pair() -> None:
    same = classify_duplicate_sidecar(_dup_row(instances=("a", "b")))
    reordered = classify_duplicate_sidecar(_dup_row(instances=("b", "a")))
    other_pair = classify_duplicate_sidecar(_dup_row(instances=("a", "c")))
    other_owner = classify_duplicate_sidecar(
        _dup_row(owner="w1", instances=("a", "b")),
    )
    assert same.key == reordered.key
    assert other_pair.key != same.key
    assert other_owner.key != same.key


def test_duplicate_sidecar_ts_becomes_iso_created_at() -> None:
    ev = classify_duplicate_sidecar(_dup_row(ts=1767225600.0))
    assert ev.created_at == "2026-01-01T00:00:00Z"


def test_duplicate_sidecar_malformed_fields_still_notify() -> None:
    """A garbled field is not a reason to stay silent about a live pair."""
    ev = classify_duplicate_sidecar({"ts": 1.0, "instances": "not-a-list"})
    assert ev.kind == "duplicate_sidecar"
    assert ev.worker is None
    assert ev.summary is None
    assert "unknown" in ev.body
    assert ev.key == "broker:duplicate_sidecar:unknown:unknown"


def test_duplicate_sidecar_severity_override_applies() -> None:
    ev = classify_duplicate_sidecar(
        _dup_row(), notify_map={"duplicate_sidecar": "normal"},
    )
    assert ev.severity == "normal"


def test_classify_broker_duplicates_collapses_repeats_per_pair() -> None:
    """The store re-journals a live pair once per lease window."""
    out = classify_broker_duplicates([
        _dup_row(ts=1000.0, instances=("a", "b")),
        _dup_row(ts=1030.0, instances=("a", "b")),
        _dup_row(ts=1060.0, instances=("a", "b")),
        _dup_row(ts=1010.0, instances=("a", "c")),
    ])
    assert len(out) == 2
    by_summary = {ev.summary: ev for ev in out}
    # Newest row wins for the repeated pair.
    assert by_summary["a, b"].created_at == _iso_from_epoch(1060.0)
    assert set(by_summary) == {"a, b", "a, c"}


def test_classify_all_appends_broker_duplicates() -> None:
    out = classify_all(
        [], [], _NOW, pending_decision_min=15, user_replied_min=15,
        broker_duplicates=[_dup_row()],
    )
    assert [ev.kind for ev in out] == ["duplicate_sidecar"]


def test_classify_all_without_broker_duplicates_is_unchanged() -> None:
    out = classify_all(
        [_row(id=1, kind="worker_completed", payload={"task_id": "x"})],
        [], _NOW, pending_decision_min=15, user_replied_min=15,
    )
    assert [ev.kind for ev in out] == ["worker_completed"]
