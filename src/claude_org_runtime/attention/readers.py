"""Read-only loaders for the attention watcher.

The classifier is pure; this module is the only place that touches the
filesystem and SQLite. Each loader tolerates missing files and returns
an empty list — first-start environments (no ``state.db``, no
``pending_decisions.json``) must not crash the watcher per §11.5.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

# Event ``kind`` column values relevant to attention classification.
# Narrowing the SELECT keeps `scan` cheap on busy DBs (events grows
# unbounded) and gives the unit tests a fixed surface to assert.
RELEVANT_EVENT_KINDS: tuple[str, ...] = (
    "notify_sent",
    "ci_completed",
    "worker_completed",
    "pr_merged",
)

# Broker journal (Issue #167). ``.state/broker/queue.jsonl`` is the
# org-broker's append-only journal; ``duplicate_sidecar_detected`` is the
# line the daemon writes when two distinct sidecar instances poll for the
# same owner inside one lease window (``broker/store.py``
# ``_note_poll_locked``). Field shape: ``{ts, event, owner, instances}``.
BROKER_JOURNAL_NAME = "queue.jsonl"
DUPLICATE_SIDECAR_EVENT = "duplicate_sidecar_detected"

# The journal is append-only and never rotated, so a running watcher must
# not re-read it whole on every poll. Only the tail is parsed: 256 KiB is
# ~1-2k journal lines, far more than one detection window's worth of
# traffic even on a busy daemon.
BROKER_JOURNAL_TAIL_BYTES = 256 * 1024


def read_events(state_db_path: Path) -> list[dict[str, Any]]:
    """Return rows from ``events`` that may produce attention events.

    Returns ``[]`` for any read error — missing file, missing
    ``events`` table, non-SQLite file, corrupt page, or query-time
    SQLite errors. A long-running ``watch`` must not crash because of
    a transient DB issue; we log a one-line warning and let the next
    poll retry.
    """
    p = Path(state_db_path)
    if not p.exists():
        return []
    uri = f"file:{p.as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        print(
            f"warning: cannot open state DB {p}: {exc}; "
            "treating as no events",
            file=sys.stderr,
        )
        return []
    try:
        conn.row_factory = sqlite3.Row
        try:
            has_events = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='events'"
            ).fetchone()
        except sqlite3.Error as exc:
            print(
                f"warning: state DB {p} is unreadable ({exc}); "
                "treating as no events",
                file=sys.stderr,
            )
            return []
        if has_events is None:
            return []
        placeholders = ",".join("?" * len(RELEVANT_EVENT_KINDS))
        try:
            cur = conn.execute(
                f"SELECT id, occurred_at, actor, kind, payload_json "
                f"FROM events WHERE kind IN ({placeholders}) "
                f"ORDER BY id ASC",
                RELEVANT_EVENT_KINDS,
            )
        except sqlite3.Error as exc:
            print(
                f"warning: state DB events query failed ({exc}); "
                "treating as no events",
                file=sys.stderr,
            )
            return []
        out: list[dict[str, Any]] = []
        for r in cur:
            out.append({
                "id": r["id"],
                "occurred_at": r["occurred_at"],
                "actor": r["actor"],
                "kind": r["kind"],
                "payload": _safe_payload(r["payload_json"]),
            })
        return out
    finally:
        conn.close()


def read_pending_decisions(pending_path: Path) -> list[dict[str, Any]]:
    """Return entries from ``pending_decisions.json`` (or ``[]`` if absent).

    Tolerates malformed JSON: a corrupt register must not crash the
    watcher (the register is owned by the Secretary pane, not the
    watcher, and may briefly be inconsistent while being rewritten).
    """
    p = Path(pending_path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [d for d in data if isinstance(d, dict)]


def read_broker_duplicates(
    broker_state_dir: Path,
    *,
    now_epoch: float,
    window_sec: float,
    tail_bytes: int = BROKER_JOURNAL_TAIL_BYTES,
) -> list[dict[str, Any]]:
    """Return recent ``duplicate_sidecar_detected`` lines from the broker journal.

    Issue #167: the daemon already detects the double-claimer condition
    but nothing consumed the signal, so an operator learned about a
    double sidecar only by noticing that reports had stopped arriving.
    This is the read half of the consumer; :func:`classifier.
    classify_broker_duplicates` turns the rows into notifications.

    Each returned row is ``{"ts": float, "owner": Any, "instances": Any}``
    — the raw journal fields, normalized only in that ``ts`` is a usable
    float. Rows older than ``window_sec`` are dropped: the store re-emits
    per instance pair once per lease window (30s by default) for as long
    as the condition lasts, so a short window is what makes the alert
    mean "this is happening now" rather than "this happened once".

    Missing file / unreadable journal / malformed lines all degrade to
    "no duplicates" with at most a one-line warning, matching the other
    loaders here: a long-running ``watch`` must not crash on a transient
    filesystem problem.

    A line whose ``ts`` is missing, non-numeric, or non-finite is
    **skipped** rather than treated as fresh. That is the opposite of the
    :func:`classifier._minutes_since` posture (malformed timestamp →
    alert) and deliberately so: this signal repeats on its own while the
    condition holds, so a dropped line costs at most one lease window of
    delay, whereas an undateable line sitting in the tail would re-alert
    every cooldown until the journal grew past it.
    """
    p = Path(broker_state_dir) / BROKER_JOURNAL_NAME
    if not p.exists():
        return []
    try:
        with p.open("rb") as f:
            size = f.seek(0, os.SEEK_END)
            start = max(0, size - max(0, tail_bytes))
            f.seek(start)
            raw = f.read()
    except OSError as exc:
        print(
            f"warning: cannot read broker journal {p}: {exc}; "
            "treating as no duplicate-sidecar signals",
            file=sys.stderr,
        )
        return []
    # ``errors="replace"`` keeps a tail cut mid-codepoint (or any single
    # corrupt byte) from discarding the whole read; the damaged first
    # line is dropped below whenever we did not start at byte 0.
    lines = raw.decode("utf-8", "replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]
    out: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        if rec.get("event") != DUPLICATE_SIDECAR_EVENT:
            continue
        ts = rec.get("ts")
        if isinstance(ts, bool) or not isinstance(ts, (int, float)):
            continue
        ts = float(ts)
        # json.loads accepts NaN / Infinity; a non-finite ts would never
        # age out of the window and would re-alert forever.
        if not math.isfinite(ts):
            continue
        if now_epoch - ts > window_sec:
            continue
        out.append({
            "ts": ts,
            "owner": rec.get("owner"),
            "instances": rec.get("instances"),
        })
    return out


def _safe_payload(raw: Any) -> dict[str, Any]:
    """Coerce ``events.payload_json`` to a plain dict (or empty)."""
    if raw is None or raw == "":
        return {}
    try:
        v = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}
