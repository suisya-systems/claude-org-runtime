"""``claude-org-runtime attention scan/watch`` implementation.

Mounted into the top-level CLI by :mod:`claude_org_runtime.cli`. Also
runnable as ``python -m claude_org_runtime.attention.cli`` for direct
testing.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .classifier import AttentionEvent, classify_all
from .config import AttentionConfig, load_config
from .dedup import (
    DedupState, load_state, record_notified, save_state, should_notify,
)
from .notify import notify as run_notify
from .readers import read_events, read_pending_decisions


def _state_paths(state_dir: Path) -> tuple[Path, Path, Path]:
    """Return ``(state.db, pending_decisions.json, attention_notified.json)``."""
    return (
        state_dir / "state.db",
        state_dir / "pending_decisions.json",
        state_dir / "attention_notified.json",
    )


def _scan_once(
    state_dir: Path,
    cfg: AttentionConfig,
    *,
    now: datetime,
    dry_run: bool,
    backend: Optional[str] = None,
    emit_json: bool = False,
    log_stream=None,
) -> list[AttentionEvent]:
    """One classification + dispatch cycle. Returns the events notified."""
    db_path, pending_path, dedup_path = _state_paths(state_dir)
    events = read_events(db_path)
    pending = read_pending_decisions(pending_path)
    classified = classify_all(
        events, pending, now,
        cfg.pending_decision_min, cfg.user_replied_min,
        notify_map=cfg.notify,
    )
    state: DedupState = load_state(dedup_path)
    notified: list[AttentionEvent] = []
    notified_payloads: list[dict] = []
    # When ``--json`` is requested the caller wants a machine-readable
    # stdout payload; sending the human log lines to stderr keeps the
    # stdout stream pure JSON for the §8 ja-side golden test.
    effective_log = log_stream
    if emit_json and effective_log is None:
        effective_log = sys.stderr
    for ev in classified:
        if not should_notify(
            state, ev.key,
            source=ev.source,
            cooldown_sec=cfg.cooldown_sec,
            now=now,
        ):
            continue
        notified.append(ev)
        formatted = run_notify(
            ev, cfg, dry_run=dry_run, backend=backend,
            log_stream=effective_log,
        )
        # Emit the rendered title/body in --json so the payload reflects
        # what was actually sent (post-template, post-truncation).
        payload = ev.to_dict()
        payload["title"] = formatted.title
        payload["body"] = formatted.body
        notified_payloads.append(payload)
        if not dry_run:
            record_notified(state, ev.key, source=ev.source, now=now)
    if not dry_run and notified:
        save_state(dedup_path, state)
    if emit_json:
        json.dump(
            notified_payloads,
            sys.stdout, indent=2, ensure_ascii=False,
        )
        sys.stdout.write("\n")
    return notified


def cmd_attention_scan(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    cfg = load_config(Path(args.config) if args.config else None)
    _scan_once(
        state_dir, cfg,
        now=datetime.now(timezone.utc),
        dry_run=bool(args.dry_run),
        emit_json=bool(args.json),
    )
    return 0


def cmd_attention_watch(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir).resolve()
    cfg = load_config(Path(args.config) if args.config else None)
    interval = max(1, int(cfg.poll_interval_sec))
    max_iterations: Optional[int] = getattr(args, "max_iterations", None)
    count = 0
    try:
        while True:
            _scan_once(
                state_dir, cfg,
                now=datetime.now(timezone.utc),
                dry_run=False,
            )
            count += 1
            if max_iterations is not None and count >= max_iterations:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        print("attention watch interrupted", file=sys.stderr)
    return 0


def add_subparsers(
    sub: "argparse._SubParsersAction[argparse.ArgumentParser]",
) -> None:
    """Mount ``scan`` and ``watch`` under the caller's ``attention`` subparser."""
    scan_p = sub.add_parser(
        "scan",
        help="One-shot scan of .state for attention events",
    )
    scan_p.add_argument(
        "--state-dir", default=".state",
        help="state directory root (default: .state)",
    )
    scan_p.add_argument(
        "--config", default=None,
        help="path to attention config JSON (optional)",
    )
    scan_p.add_argument(
        "--dry-run", action="store_true",
        help=(
            "classify and log, but never invoke an OS notification "
            "subprocess or update dedup state"
        ),
    )
    scan_p.add_argument(
        "--json", action="store_true",
        help="emit notified events to stdout as JSON",
    )
    scan_p.set_defaults(func=cmd_attention_scan)

    watch_p = sub.add_parser(
        "watch",
        help="Long-running poll of .state for attention events",
    )
    watch_p.add_argument(
        "--state-dir", default=".state",
        help="state directory root (default: .state)",
    )
    watch_p.add_argument(
        "--config", default=None,
        help="path to attention config JSON (optional)",
    )
    watch_p.add_argument(
        "--max-iterations", type=int, default=None,
        help=argparse.SUPPRESS,
    )
    watch_p.set_defaults(func=cmd_attention_watch)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-org-runtime-attention",
        description="Attention scan/watch CLI",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    add_subparsers(sub)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
