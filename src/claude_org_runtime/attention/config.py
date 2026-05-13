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
    # Issue #28: secretary paused for a user-bound decision — "human is the
    # sole recovery path" tier, so it joins approval_blocked /
    # pending_decision at ``urgent`` by default.
    "secretary_awaiting_user": "urgent",
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
    # Sparse map of *explicit user overrides* only (Issue #26 round-4
    # fix). Pre-#26 this was pre-filled with every ``DEFAULT_NOTIFY``
    # entry, which caused the TTL demote check in
    # :func:`classifier._severity_for` to treat every default as an
    # operator override — defeating the ``max ≤ age < drop`` demote
    # path entirely on the CLI route. Keeping the dict sparse lets the
    # classifier fall back to ``DEFAULT_NOTIFY`` for unset keys and
    # apply demote there, while honoring genuine user overrides.
    notify: dict[str, Severity] = field(default_factory=dict)
    templates: dict[str, Template] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Validate the TTL ladder once at construction so a malformed
        # default-built config (e.g. test scaffolding that overrides
        # only one threshold) trips immediately rather than producing
        # silently wrong classifications downstream. The ladder must
        # admit a real "urgent" window for BOTH the pending_decision
        # path (clock at received_at) and the user_reply_not_forwarded
        # path (clock at user_replied_at), so ``max`` has to exceed
        # both lower bounds.
        if self.pending_decision_max <= self.pending_decision_min:
            raise ValueError(
                "config.pending_decision_max must be greater than "
                "pending_decision_min "
                f"({self.pending_decision_max} <= {self.pending_decision_min})"
            )
        if self.pending_decision_max <= self.user_replied_min:
            raise ValueError(
                "config.pending_decision_max must be greater than "
                "user_replied_min "
                f"({self.pending_decision_max} <= {self.user_replied_min})"
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
        # Issue #26 round-4: keep this dict sparse so the classifier
        # can distinguish "operator pinned this severity" from "the
        # design default happens to be urgent". Pre-filling the dict
        # with DEFAULT_NOTIFY would mask the difference and break the
        # TTL demote path on the CLI route.
        notify: dict[str, Severity] = {}
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

    # Issue #26 backward-compat: a pre-#26 user config that only
    # raised ``pending_decision_min`` or ``user_replied_min`` above the
    # new default ``max`` (1440) — or only raised ``max`` above the new
    # default ``drop`` (10080) — used to load fine. Validation now
    # requires both ``min`` and ``user_replied_min`` to be below ``max``
    # below ``drop``, so auto-scale any missing knob upward to keep
    # legacy configs loading. Explicit user values for ``max`` / ``drop``
    # always win, and the dataclass validator still rejects any
    # inversion they introduce.
    _fields = AttentionConfig.__dataclass_fields__
    _default_max = _fields["pending_decision_max"].default
    _default_drop = _fields["pending_decision_drop"].default
    _default_user_replied_min = _fields["user_replied_min"].default
    _default_pending_decision_min = _fields["pending_decision_min"].default
    if "pending_decision_max" not in raw:
        # Both ladder paths share the same ``max`` threshold, so the
        # auto-scaled value has to clear whichever lower bound is
        # larger. ``+1`` keeps validation happy without inventing a
        # specific policy multiplier.
        effective_min_floor = max(
            kwargs.get("pending_decision_min", _default_pending_decision_min),
            kwargs.get("user_replied_min", _default_user_replied_min),
        )
        if effective_min_floor >= _default_max:
            kwargs["pending_decision_max"] = effective_min_floor + 1
    effective_max = kwargs.get("pending_decision_max", _default_max)
    if (
        "pending_decision_drop" not in raw
        and effective_max >= _default_drop
    ):
        kwargs["pending_decision_drop"] = effective_max + 1

    return AttentionConfig(**kwargs)
