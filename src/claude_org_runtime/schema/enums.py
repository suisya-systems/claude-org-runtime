"""String-mixin Enums for ``.state/`` schema.

Values are taken from the 2026-05-02 measurement of two parallel journals
(``.dispatcher/.state/journal.jsonl`` and ``.state/journal.jsonl``) in
claude-org-ja. The ``str`` mixin keeps JSON serialisation trivial: a member
serialises as its raw string value via ``json.dumps`` without a custom
encoder.

Canonical-name choices (each drift axis collapses to one canonical token):

- ``task_id`` over ``worker``: ``task_id`` is a slug describing *what* is
  being done, while ``worker`` was an opaque per-instance identifier; the
  slug survives re-dispatch and is more structural. The migrate script
  prefers an explicit ``task`` slug when present and only falls back to
  ``worker`` for legacy events that have no slug field, deferring the
  final normalisation to a downstream secretary pass.
- ``pane_id`` (numeric int) AND ``pane_name`` (stable string) are kept
  separate because they play different roles -- the int is renga's mutable
  pane handle, the string is the human-stable label. The legacy ``pane`` key
  is folded into whichever of the two its value's type matches.
- ``worker_dir`` over ``dir``: ``dir`` is too generic; ``worker_dir``
  documents that the path is the worker's worktree.
"""

from __future__ import annotations

from enum import Enum


class WorkerStatus(str, Enum):
    """Lifecycle status of a worker entry in ``org-state.md``.

    Values are derived from observed status strings in the Worker Directory
    Registry table across the claude-org-ja history. ``BLOCKED`` covers
    workers waiting on an external dependency; ``REVIEW`` covers PR/code
    review hand-off.
    """

    IN_USE = "in_use"
    AVAILABLE = "available"
    COMPLETED = "completed"
    SUSPENDED = "suspended"
    MERGED = "merged"
    REVIEW = "review"
    BLOCKED = "blocked"


class JournalEventType(str, Enum):
    """Event kind for a single ``journal.jsonl`` line.

    The first block enumerates the *stable-frequent* events common to both
    journals (~80% of all events as of 2026-05-02). The second block lists
    every other event observed at least once in either journal during the
    measurement window. ``MISC`` is a catch-all for events the migrate
    script could not map to a known type -- the original event string is
    preserved on the :class:`JournalEvent` via the ``original_event`` field.
    """

    # Stable-frequent (top 5 + closely-related)
    WORKER_SPAWNED = "worker_spawned"
    PANE_CLOSED = "pane_closed"
    WORKER_CLOSED = "worker_closed"
    SUSPEND = "suspend"
    NOTIFY_SENT = "notify_sent"

    # Other observed events (dispatcher journal)
    WORKER_PANE_CLOSED = "worker_pane_closed"
    WORKER_COMPLETED = "worker_completed"
    ANOMALY_OBSERVED = "anomaly_observed"
    FOREMAN_SHUTDOWN = "foreman_shutdown"
    APPROVAL_APPLIED = "approval_applied"

    # Other observed events (state journal)
    PR_MERGED = "pr_merged"
    WORKER_REVIEW = "worker_review"
    DELEGATE_SENT = "delegate_sent"
    PLAN_APPROVED = "plan_approved"
    TASK_COMPLETED = "task_completed"
    RESUME = "resume"
    ISSUE_CLOSED = "issue_closed"
    PR_OPENED = "pr_opened"
    ISSUE_FILED = "issue_filed"
    ISSUES_FILED = "issues_filed"
    WORKER_REPORT_FORWARDED = "worker_report_forwarded"
    WORKTREE_REMOVED = "worktree_removed"
    PRE_HISTORY_RESET_SNAPSHOT = "pre_history_reset_snapshot"
    DESIGN_APPROVED = "design_approved"
    PRS_OPENED = "prs_opened"
    PRS_MERGED = "prs_merged"
    DELEGATE_RESUME = "delegate_resume"
    PLAN_DELIVERED = "plan_delivered"
    PRS_PUSHED = "prs_pushed"
    DELEGATE_RESUME_R2 = "delegate_resume_r2"
    PLAN_APPROVED_AND_PREP_DISPATCHED = "plan_approved_and_prep_dispatched"
    FIX_PUSHED = "fix_pushed"
    PREP_DELIVERED = "prep_delivered"
    WORKER_REPORTED = "worker_reported"
    ISSUES_SWEPT = "issues_swept"
    DRIFT_REAUDIT = "drift_reaudit"
    PHASE_D_SNAPSHOT = "phase_d_snapshot"
    PHASE_D_FORCE_PUSH = "phase_d_force_push"
    PHASE_D_COMPLETE = "phase_d_complete"

    # Catch-all for unknown / one-off event names
    MISC = "misc"


class AnomalyKind(str, Enum):
    """Variant tag for ``anomaly_observed`` / ``notify_sent`` payloads.

    The 2026-05-02 measurement observed ``kind`` as a free-text tag whose
    enumeration was not exhaustively recorded; the values below cover the
    categories actually used by the dispatcher's anomaly detector. Unknown
    or future variants should serialise as :data:`UNKNOWN` while the
    original tag is preserved in :attr:`JournalEvent.extra`.
    """

    PANE_SILENT = "pane_silent"
    PANE_CRASHED = "pane_crashed"
    WORKER_STALLED = "worker_stalled"
    WORKER_NOT_REPORTED = "worker_not_reported"
    UNKNOWN = "unknown"
