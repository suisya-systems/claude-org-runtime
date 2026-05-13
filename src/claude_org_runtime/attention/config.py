"""Attention notification config (Issue #19 + #20).

Carries both the scan/watch knobs from §5 (``cooldown_sec``,
``pending_decision_min`` …) and the locale/template overrides from §6
(``templates``). One loader, one dataclass — keeps the JSON shape
flat for ja-side default configs and keeps the watcher from juggling
two parallel config objects.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

Severity = Literal["urgent", "normal"]
SoundMode = Literal["off", "urgent-only", "all"]

# Default severity per attention kind. ja config may override individual
# entries; unknown kinds inherit ``normal`` unless overridden.
#
# Issue #26 Part B rebalance: only "human is the sole recovery path"
# events stay ``urgent`` (approval_blocked / pending_decision /
# user_reply_not_forwarded / ci_failed / pane_crashed). The anomaly-
# detector kinds (relay_gap_suspected / silent_worker_output /
# pane_silent / worker_stalled / worker_not_reported / worker_error)
# are best-effort signals that often self-resolve, so they ride at
# ``normal`` to avoid alert fatigue.
DEFAULT_NOTIFY: dict[str, Severity] = {
    "approval_blocked": "urgent",
    "relay_gap_suspected": "normal",
    "silent_worker_output": "normal",
    "ci_failed": "urgent",
    "pending_decision": "urgent",
    "user_reply_not_forwarded": "urgent",
    "pane_silent": "normal",
    "pane_crashed": "urgent",
    "worker_stalled": "normal",
    "worker_not_reported": "normal",
    "worker_error": "normal",
    "worker_completed": "normal",
    "pr_merged": "normal",
}

# Placeholder allowlist from design §6. Anything outside this set
# triggers a warning + fallback to the runtime default template (the
# watcher must never crash on a misspelled template).
ALLOWED_PLACEHOLDERS: frozenset[str] = frozenset(
    {"task_id", "worker", "kind", "status", "pr", "summary"}
)

_VALID_SOUND_MODES: frozenset[str] = frozenset({"off", "urgent-only", "all"})


@dataclass(frozen=True)
class Template:
    """One ``{title, body}`` template pair for a given attention kind."""

    title: str
    body: str


@dataclass(frozen=True)
class AttentionConfig:
    """All knobs the attention watcher reads.

    The defaults match the §5 reference JSON. ``templates`` is empty by
    default — when no override is present, :func:`notify.render_text`
    falls back to the bundled English defaults attached to each
    :class:`AttentionEvent` by the classifier.
    """

    desktop: bool = True
    sound: SoundMode = "urgent-only"
    cooldown_sec: int = 300
    poll_interval_sec: int = 10
    pending_decision_min: int = 15
    # Issue #26 Part A TTL ladder for urgent pending_decisions:
    # min ≤ age < max → urgent (escalate); max ≤ age < drop → demote to
    # normal (still notify); age ≥ drop → suppress entirely from notify
    # but ``attention scan --json`` still surfaces the row so an operator
    # can run a triage report. Same ladder applies to
    # ``user_reply_not_forwarded`` (clock starts at ``user_replied_at``).
    pending_decision_max: int = 1440  # 24h
    pending_decision_drop: int = 10080  # 7d
    user_replied_min: int = 15
    max_title_chars: int = 80
    max_body_chars: int = 240
    notify: dict[str, Severity] = field(
        default_factory=lambda: dict(DEFAULT_NOTIFY)
    )
    templates: dict[str, Template] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Validate the TTL ladder once at construction so a malformed
        # default-built config (e.g. test scaffolding that overrides
        # only one threshold) trips immediately rather than producing
        # silently wrong classifications downstream.
        if self.pending_decision_max <= self.pending_decision_min:
            raise ValueError(
                "config.pending_decision_max must be greater than "
                "pending_decision_min "
                f"({self.pending_decision_max} <= {self.pending_decision_min})"
            )
        if self.pending_decision_drop <= self.pending_decision_max:
            raise ValueError(
                "config.pending_decision_drop must be greater than "
                "pending_decision_max "
                f"({self.pending_decision_drop} <= {self.pending_decision_max})"
            )


def load_config(path: Path | None) -> AttentionConfig:
    """Load ``AttentionConfig`` from a JSON file (or return defaults).

    Missing file → defaults. Malformed JSON or wrong shape → raises
    :class:`ValueError` so the CLI surfaces a clear error before the
    watcher ever runs. ``templates`` placeholders are not validated
    here — that lives in :func:`notify.render_text` because validation
    happens once per event, not once at config-load time, and a typo
    must not block the watcher from starting.
    """
    if path is None:
        return AttentionConfig()
    p = Path(path)
    if not p.exists():
        return AttentionConfig()
    raw = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(
            f"attention config {str(p)!r} must be a JSON object"
        )

    kwargs: dict[str, Any] = {}

    for key in (
        "cooldown_sec", "poll_interval_sec",
        "pending_decision_min",
        "pending_decision_max", "pending_decision_drop",
        "user_replied_min",
        "max_title_chars", "max_body_chars",
    ):
        if key in raw:
            value = raw[key]
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(
                    f"config.{key} must be an integer, got "
                    f"{type(value).__name__}"
                )
            if value < 0:
                raise ValueError(f"config.{key} must be non-negative")
            kwargs[key] = value

    if "desktop" in raw:
        if not isinstance(raw["desktop"], bool):
            raise ValueError("config.desktop must be a boolean")
        kwargs["desktop"] = raw["desktop"]

    if "sound" in raw:
        if raw["sound"] not in _VALID_SOUND_MODES:
            raise ValueError(
                f"config.sound must be one of {sorted(_VALID_SOUND_MODES)}, "
                f"got {raw['sound']!r}"
            )
        kwargs["sound"] = raw["sound"]

    if "notify" in raw:
        if not isinstance(raw["notify"], dict):
            raise ValueError("config.notify must be a JSON object")
        notify = dict(DEFAULT_NOTIFY)
        for k, v in raw["notify"].items():
            if v not in ("urgent", "normal"):
                raise ValueError(
                    f"config.notify[{k!r}] must be 'urgent' or 'normal', "
                    f"got {v!r}"
                )
            notify[k] = v
        kwargs["notify"] = notify

    if "templates" in raw:
        if not isinstance(raw["templates"], dict):
            raise ValueError("config.templates must be a JSON object")
        templates: dict[str, Template] = {}
        for kind, tmpl in raw["templates"].items():
            if not isinstance(tmpl, dict):
                raise ValueError(
                    f"config.templates[{kind!r}] must be a JSON object"
                )
            title = tmpl.get("title", "")
            body = tmpl.get("body", "")
            if not isinstance(title, str) or not isinstance(body, str):
                raise ValueError(
                    f"config.templates[{kind!r}] must have string "
                    "'title' and 'body' fields"
                )
            templates[kind] = Template(title=title, body=body)
        kwargs["templates"] = templates

    return AttentionConfig(**kwargs)
