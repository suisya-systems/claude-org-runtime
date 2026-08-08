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
from typing import Any, Optional

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
# not re-read it whole on every poll. Instead the tail is walked backwards
# one chunk at a time and the walk stops at the first line older than the
# freshness window — so the amount read follows the window the operator
# configured, not an unrelated byte constant. On a quiet daemon that is a
# single chunk; on a busy one it grows only as far as the window reaches.
BROKER_JOURNAL_CHUNK_BYTES = 64 * 1024

# Safety bound on that walk: a journal whose lines all lack a usable ``ts``
# (or a clock that jumped backwards) would otherwise drag the scan to the
# top of an unbounded file on every poll. Hitting this cap is reported
# rather than silently truncating the window.
BROKER_JOURNAL_MAX_SCAN_BYTES = 8 * 1024 * 1024


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
    chunk_bytes: int = BROKER_JOURNAL_CHUNK_BYTES,
    max_scan_bytes: int = BROKER_JOURNAL_MAX_SCAN_BYTES,
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

    Only the journal tail is read, walked backwards in ``chunk_bytes``
    steps until a line older than the window turns up (every journal line
    carries a ``ts``, so any line can end the walk). The bytes read
    therefore follow the configured window rather than a fixed constant —
    raising ``duplicate_sidecar_window_sec`` widens the scan to match, and
    a busy daemon cannot push a still-live incident out of view. The
    ``max_scan_bytes`` cap only guards the pathological case (no usable
    timestamps at all, or a clock that jumped backwards) and says so on
    stderr rather than truncating the window silently.

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
    cutoff = now_epoch - window_sec
    try:
        lines, capped = _tail_lines_back_to(
            p, cutoff,
            chunk_bytes=max(1, chunk_bytes),
            max_scan_bytes=max(1, max_scan_bytes),
        )
    except OSError as exc:
        print(
            f"warning: cannot read broker journal {p}: {exc}; "
            "treating as no duplicate-sidecar signals",
            file=sys.stderr,
        )
        return []
    if capped:
        print(
            f"warning: broker journal {p} scanned back "
            f"{max_scan_bytes} bytes without reaching the "
            f"{window_sec}s freshness window; older duplicate-sidecar "
            "signals inside the window may be missing",
            file=sys.stderr,
        )
    out: list[dict[str, Any]] = []
    for line in lines:
        rec = _journal_record(line)
        if rec is None or rec.get("event") != DUPLICATE_SIDECAR_EVENT:
            continue
        ts = _journal_ts(rec)
        if ts is None or ts < cutoff:
            continue
        out.append({
            "ts": ts,
            "owner": rec.get("owner"),
            "instances": rec.get("instances"),
        })
    return out


def _tail_lines_back_to(
    path: Path,
    cutoff: float,
    *,
    chunk_bytes: int,
    max_scan_bytes: int,
) -> tuple[list[str], bool]:
    """Read whole journal lines back to the first one older than ``cutoff``.

    Returns ``(lines, capped)`` where ``capped`` is True when the walk
    stopped on ``max_scan_bytes`` rather than on an old-enough line or the
    top of the file — i.e. the caller cannot assume the window is fully
    covered.
    """
    with path.open("rb") as f:
        size = f.seek(0, os.SEEK_END)
        pos = size
        buf = b""
        capped = False
        while pos > 0:
            if size - pos >= max_scan_bytes:
                capped = True
                break
            start = max(0, pos - chunk_bytes)
            f.seek(start)
            buf = f.read(pos - start) + buf
            pos = start
            if _spans_cutoff(buf, cutoff, at_file_start=pos == 0):
                break
    # ``errors="replace"`` keeps a chunk boundary landing mid-codepoint
    # (or any single corrupt byte) from discarding the whole read; the
    # damaged first line is dropped whenever we did not reach byte 0.
    lines = buf.decode("utf-8", "replace").splitlines()
    if pos > 0 and lines:
        lines = lines[1:]
    return lines, capped


def _spans_cutoff(buf: bytes, cutoff: float, *, at_file_start: bool) -> bool:
    """True when the oldest complete line in ``buf`` predates ``cutoff``.

    The journal is a single daemon appending in time order, so the first
    complete line is the oldest one held; once it is older than the
    cutoff, everything inside the window is already in ``buf``. Lines
    without a usable ``ts`` (corrupt, or a schema the daemon has not
    written since) simply do not end the walk.
    """
    lines = buf.decode("utf-8", "replace").splitlines()
    if not at_file_start and lines:
        lines = lines[1:]
    for line in lines:
        rec = _journal_record(line)
        if rec is None:
            continue
        ts = _journal_ts(rec)
        if ts is not None:
            return ts < cutoff
    return False


def _journal_record(line: str) -> Optional[dict[str, Any]]:
    """Parse one journal line into a dict (``None`` if unusable)."""
    line = line.strip()
    if not line:
        return None
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    return rec if isinstance(rec, dict) else None


def _journal_ts(rec: dict[str, Any]) -> Optional[float]:
    """Return the record's epoch ``ts`` as a finite float (or ``None``).

    ``bool`` is an ``int`` subclass, so it is excluded explicitly, and
    ``json.loads`` accepts ``NaN`` / ``Infinity`` — a non-finite ts would
    never age out of the window and would re-alert forever.
    """
    ts = rec.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    ts = float(ts)
    return ts if math.isfinite(ts) else None


def _safe_payload(raw: Any) -> dict[str, Any]:
    """Coerce ``events.payload_json`` to a plain dict (or empty)."""
    if raw is None or raw == "":
        return {}
    try:
        v = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}
