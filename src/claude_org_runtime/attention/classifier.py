"""Pure classifier: events / pending → :class:`AttentionEvent`.

No I/O, no subprocesses. Given the rows returned by
:mod:`readers`, this module produces a deterministic list of
:class:`AttentionEvent` records that downstream code (dedup, notify)
can consume. The classification table is the §5 design doc verbatim.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Literal, Mapping, Optional

from .config import DEFAULT_NOTIFY

Severity = Literal["urgent", "normal"]


# ``events.kind='notify_sent'`` carries a payload ``kind`` that names the
# specific notification subtype. The table maps those subtypes to the
# attention-layer kind used downstream. Covers the 3 design-doc subkinds
# (§5) plus the broader vocabulary actually emitted in production: the
# ``AnomalyKind`` enum (``pane_silent`` / ``pane_crashed`` / ``worker_stalled``
# / ``worker_not_reported``) and the freeform ``error`` tag used by the
# dispatcher prompt (``prompts/templates/dispatcher.md:410``). Any unknown
# subtype is intentionally ignored so duplicate/progress-only ``notify_sent``
# rows do not produce attention spam (design §5 "通知しないもの").
_NOTIFY_SUBKIND_TO_KIND: dict[str, str] = {
    "approval_blocked": "approval_blocked",
    "relay_gap_suspected": "relay_gap_suspected",
    "pane_output_without_peer_msg": "silent_worker_output",
    "pane_silent": "pane_silent",
    "pane_crashed": "pane_crashed",
    "worker_stalled": "worker_stalled",
    "worker_not_reported": "worker_not_reported",
    "error": "worker_error",
}

# CI run statuses that classify as a failure. Mirrors §5 column 2.
_CI_FAIL_STATUSES: frozenset[str] = frozenset(
    {"failed", "canceled", "incomplete"}
)


@dataclass(frozen=True)
class AttentionEvent:
    """One normalized attention record.

    ``key`` is the stable dedup identity (``event:<events.id>`` or
    ``pending:<task_id>:<kind>``). The text fields ``title`` / ``body``
    hold the runtime-default English copy; :func:`notify.render_text`
    overlays user-supplied templates from :class:`AttentionConfig`
    before dispatch.

    ``suppressed`` is the Issue #26 Part A "age ≥ drop" marker: the
    classifier still emits the record so triage tools (``attention
    scan --json``) can list it, but the dispatcher in :mod:`cli` must
    NOT route it to ``notify`` — no desktop notification, no bell, no
    dedup-state update.
    """

    key: str
    kind: str
    severity: Severity
    title: str
    body: str
    source: str
    task_id: Optional[str] = None
    worker: Optional[str] = None
    pr: Optional[int] = None
    status: Optional[str] = None
    summary: Optional[str] = None
    created_at: Optional[str] = None
    suppressed: bool = False

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "key": self.key,
            "kind": self.kind,
            "severity": self.severity,
            "title": self.title,
            "body": self.body,
            "source": self.source,
        }
        for f in ("task_id", "worker", "pr", "status", "summary", "created_at"):
            v = getattr(self, f)
            if v is not None:
                out[f] = v
        if self.suppressed:
            out["suppressed"] = True
        return out


def classify_event(
    row: dict[str, Any],
    notify_map: Optional[Mapping[str, str]] = None,
) -> Optional[AttentionEvent]:
    """Map one ``events`` row to an :class:`AttentionEvent` or ``None``.

    Returns ``None`` for rows that should not produce a notification
    (e.g. ``ci_completed status=success``, unrecognized
    ``notify_sent.kind``). ``notify_map`` overrides the §5 default
    severity-per-kind table; missing keys fall back to the default.
    """
    kind = row.get("kind")
    payload = row.get("payload") or {}
    event_id = row.get("id")
    if event_id is None:
        return None
    key = f"event:{event_id}"
    task_id = _str_or_none(payload.get("task_id") or payload.get("task"))
    worker = _str_or_none(payload.get("worker") or row.get("actor"))
    pr = _coerce_int(payload.get("pr"))
    occurred_at = row.get("occurred_at")

    if kind == "notify_sent":
        sub = str(payload.get("kind") or "")
        a_kind = _NOTIFY_SUBKIND_TO_KIND.get(sub)
        if a_kind is None:
            return None
        title, body = _default_text(
            a_kind, task_id=task_id, worker=worker, pr=pr,
        )
        return AttentionEvent(
            key=key, kind=a_kind, severity=_severity_for(a_kind, notify_map),
            title=title, body=body, source="state.db.events",
            task_id=task_id, worker=worker, pr=pr,
            created_at=occurred_at,
        )

    if kind == "ci_completed":
        status = str(payload.get("status") or "")
        if status not in _CI_FAIL_STATUSES:
            return None
        title, body = _default_text(
            "ci_failed", task_id=task_id, worker=worker, pr=pr,
            status=status,
        )
        return AttentionEvent(
            key=key, kind="ci_failed",
            severity=_severity_for("ci_failed", notify_map),
            title=title, body=body, source="state.db.events",
            task_id=task_id, worker=worker, pr=pr, status=status,
            created_at=occurred_at,
        )

    if kind == "worker_completed":
        title, body = _default_text(
            "worker_completed", task_id=task_id, worker=worker, pr=pr,
        )
        return AttentionEvent(
            key=key, kind="worker_completed",
            severity=_severity_for("worker_completed", notify_map),
            title=title, body=body, source="state.db.events",
            task_id=task_id, worker=worker, pr=pr,
            created_at=occurred_at,
        )

    if kind == "pr_merged":
        title, body = _default_text(
            "pr_merged", task_id=task_id, worker=worker, pr=pr,
        )
        return AttentionEvent(
            key=key, kind="pr_merged",
            severity=_severity_for("pr_merged", notify_map),
            title=title, body=body, source="state.db.events",
            task_id=task_id, worker=worker, pr=pr,
            created_at=occurred_at,
        )

    return None


def classify_pending(
    entry: dict[str, Any],
    now: datetime,
    pending_decision_min: int,
    user_replied_min: int,
    notify_map: Optional[Mapping[str, str]] = None,
    *,
    pending_decision_max: int = 1440,
    pending_decision_drop: int = 10080,
) -> Optional[AttentionEvent]:
    """Map a ``pending_decisions.json`` entry to an :class:`AttentionEvent`.

    Two attention paths:

    * ``status=='pending'`` → ``pending_decision`` with the Issue #26
      Part A TTL ladder (clock starts at ``received_at``):

      - ``age < pending_decision_min`` → no event.
      - ``min ≤ age < pending_decision_max`` → urgent (escalate).
      - ``max ≤ age < pending_decision_drop`` → severity demoted to
        ``normal`` (the design-default ``urgent`` becomes ``normal``; an
        explicit ``notify_map`` override still wins so ops can pin it).
      - ``age ≥ pending_decision_drop`` → ``None`` (suppressed entirely
        from notify; ``attention scan --json`` still surfaces the row
        because it re-reads the raw input).

    * ``status=='escalated'`` (Secretary told the user) but the
      ``user_replied_at`` mark predates any ``to_worker`` resolution →
      ``user_reply_not_forwarded``. The same TTL ladder applies, with
      ``user_replied_at`` as the clock — i.e. "how long has the
      secretary failed to forward the user's reply to the worker".
    """
    status = entry.get("status")
    task_id = _str_or_none(entry.get("task_id"))
    raw_message = entry.get("raw_message")
    received_at = entry.get("received_at")
    user_replied_at = entry.get("user_replied_at")
    if not task_id:
        return None

    if status == "pending":
        age = _minutes_since(received_at, now)
        ladder = _ttl_ladder(
            age,
            pending_decision_min,
            pending_decision_max,
            pending_decision_drop,
        )
        if ladder is not None:
            title, body = _default_text(
                "pending_decision", task_id=task_id,
            )
            return AttentionEvent(
                key=f"pending:{task_id}:pending_decision",
                kind="pending_decision",
                severity=_severity_for(
                    "pending_decision", notify_map,
                    demote=ladder in ("demote", "drop"),
                ),
                title=title, body=body,
                source="pending_decisions",
                task_id=task_id,
                summary=_short_summary(raw_message),
                created_at=received_at,
                suppressed=ladder == "drop",
            )

    if (
        status == "escalated"
        and user_replied_at
        and entry.get("resolution_kind") != "to_worker"
    ):
        age = _minutes_since(user_replied_at, now)
        ladder = _ttl_ladder(
            age,
            user_replied_min,
            pending_decision_max,
            pending_decision_drop,
        )
        if ladder is not None:
            title, body = _default_text(
                "user_reply_not_forwarded", task_id=task_id,
            )
            return AttentionEvent(
                key=f"pending:{task_id}:user_reply_not_forwarded",
                kind="user_reply_not_forwarded",
                severity=_severity_for(
                    "user_reply_not_forwarded", notify_map,
                    demote=ladder in ("demote", "drop"),
                ),
                title=title, body=body,
                source="pending_decisions",
                task_id=task_id,
                summary=_short_summary(raw_message),
                created_at=user_replied_at,
                suppressed=ladder == "drop",
            )

    return None


def classify_all(
    events: Iterable[dict[str, Any]],
    pending: Iterable[dict[str, Any]],
    now: datetime,
    pending_decision_min: int,
    user_replied_min: int,
    notify_map: Optional[Mapping[str, str]] = None,
    *,
    pending_decision_max: int = 1440,
    pending_decision_drop: int = 10080,
) -> list[AttentionEvent]:
    """Classify both inputs in order: DB events first, then pending.

    The ``pending_decision_max`` / ``pending_decision_drop`` defaults
    mirror :class:`AttentionConfig` so test callers that pre-date
    Issue #26 keep working without passing the new ladder thresholds.
    """
    out: list[AttentionEvent] = []
    for row in events:
        ev = classify_event(row, notify_map=notify_map)
        if ev is not None:
            out.append(ev)
    for entry in pending:
        ev = classify_pending(
            entry, now, pending_decision_min, user_replied_min,
            notify_map=notify_map,
            pending_decision_max=pending_decision_max,
            pending_decision_drop=pending_decision_drop,
        )
        if ev is not None:
            out.append(ev)
    return out


def _severity_for(
    kind: str,
    notify_map: Optional[Mapping[str, str]],
    *,
    demote: bool = False,
) -> Severity:
    """Resolve severity for ``kind`` via override map then design default.

    ``demote=True`` is the Issue #26 Part A "max ≤ age < drop" tier:
    a pending event the design defaults to ``urgent`` becomes
    ``normal`` so it still surfaces but no longer wakes the operator.
    An explicit ``notify_map`` override always wins over both the
    default and the demotion — ops can pin ``"urgent"`` on a
    long-running event class if they want the loud behavior back.
    """
    if notify_map is not None and kind in notify_map:
        sev = notify_map[kind]
        if sev in ("urgent", "normal"):
            return sev  # type: ignore[return-value]
    default = DEFAULT_NOTIFY.get(kind, "normal")
    if demote and default == "urgent":
        return "normal"
    return default  # type: ignore[return-value]


def _ttl_ladder(
    age_minutes: float,
    min_minutes: int,
    max_minutes: int,
    drop_minutes: int,
) -> Optional[Literal["urgent", "demote", "drop"]]:
    """Issue #26 Part A TTL tier for a pending-style row.

    Returns ``"urgent"`` for the standard ``min..max`` window,
    ``"demote"`` when the row has aged past ``max`` but not yet past
    ``drop`` (keep notifying but at ``normal``), ``"drop"`` when the
    row has aged past ``drop`` (notify is suppressed but the row must
    still surface in ``attention scan --json`` for triage), and
    ``None`` only when the row is still fresher than ``min``.

    An infinite ``age_minutes`` (the malformed/missing-timestamp
    sentinel returned by :func:`_minutes_since`) short-circuits to
    ``"urgent"`` so a garbled ``received_at`` never silently falls
    into the ``drop`` bucket and disappears — the existing false-
    positive posture for relay-gap detection wins over silence.
    """
    if math.isinf(age_minutes):
        return "urgent"
    if age_minutes < min_minutes:
        return None
    if age_minutes >= drop_minutes:
        return "drop"
    if age_minutes >= max_minutes:
        return "demote"
    return "urgent"


# ---------------------------------------------------------------------------
# Runtime-default English text (overridable by :class:`Template`)
# ---------------------------------------------------------------------------


_DEFAULT_TEMPLATES: dict[str, tuple[str, str]] = {
    "approval_blocked": (
        "Worker approval required",
        "{worker} is waiting for approval.",
    ),
    "relay_gap_suspected": (
        "Secretary relay gap suspected",
        "Relay gap detected for {task_id}.",
    ),
    "silent_worker_output": (
        "Silent worker output",
        "{worker} produced output without a peer message.",
    ),
    "ci_failed": (
        "CI failed",
        "PR #{pr} finished with {status}.",
    ),
    "worker_completed": (
        "Worker completed",
        "{worker} finished task {task_id}.",
    ),
    "pr_merged": (
        "PR merged",
        "PR #{pr} merged ({task_id}).",
    ),
    "pending_decision": (
        "Pending decision",
        "{task_id} is waiting for human judgment.",
    ),
    "user_reply_not_forwarded": (
        "User reply not forwarded",
        "{task_id}: user reply has not been relayed to the worker.",
    ),
    "pane_silent": (
        "Worker pane silent",
        "{worker} pane has gone silent.",
    ),
    "pane_crashed": (
        "Worker pane crashed",
        "{worker} pane crashed unexpectedly.",
    ),
    "worker_stalled": (
        "Worker stalled",
        "{worker} appears stalled (no progress).",
    ),
    "worker_not_reported": (
        "Worker not reported",
        "{worker} has not reported back to the secretary.",
    ),
    "worker_error": (
        "Worker error",
        "{worker} reported an error.",
    ),
}


def _default_text(
    kind: str,
    *,
    task_id: Any = None,
    worker: Any = None,
    pr: Any = None,
    status: Any = None,
) -> tuple[str, str]:
    title_fmt, body_fmt = _DEFAULT_TEMPLATES.get(
        kind, ("Attention", "{kind} event"),
    )
    values = {
        "task_id": _str_or_unknown(task_id),
        "worker": _str_or_unknown(worker),
        "pr": _str_or_unknown(pr),
        "status": _str_or_unknown(status),
        "kind": kind,
        "summary": "",
    }
    return title_fmt.format_map(values), body_fmt.format_map(values)


def _str_or_unknown(v: Any) -> str:
    if v is None or v == "":
        return "unknown"
    return str(v)


def _str_or_none(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _coerce_int(v: Any) -> Optional[int]:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _minutes_since(iso_ts: Any, now: datetime) -> float:
    """Minutes between ``iso_ts`` and ``now``.

    Malformed / missing timestamps return a large positive number so
    the surrounding classifier sees the entry as "older than threshold"
    and fires the urgent alert — false-positives are preferable to
    false-negatives for a relay-gap watcher (matches the ja-side
    ``pending_decisions.py`` posture which also surfaces malformed
    timestamps as stale).
    """
    if not iso_ts or not isinstance(iso_ts, str):
        return float("inf")
    parsed = _parse_iso(iso_ts)
    if parsed is None:
        return float("inf")
    return (now - parsed).total_seconds() / 60.0


def _parse_iso(s: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp; accept trailing ``Z``."""
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _short_summary(s: Any, limit: int = 120) -> Optional[str]:
    if s is None:
        return None
    text = str(s).strip()
    if not text:
        return None
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text
