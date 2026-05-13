"""Tests for ``claude_org_runtime.attention.config``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_org_runtime.attention.config import (
    DEFAULT_NOTIFY,
    AttentionConfig,
    Template,
    load_config,
)


def test_defaults_match_design_doc() -> None:
    cfg = AttentionConfig()
    assert cfg.desktop is True
    assert cfg.sound == "urgent-only"
    assert cfg.cooldown_sec == 300
    assert cfg.poll_interval_sec == 10
    assert cfg.pending_decision_min == 15
    # Issue #26 Part A TTL ladder: 24h demote → 7d drop.
    assert cfg.pending_decision_max == 1440
    assert cfg.pending_decision_drop == 10080
    assert cfg.user_replied_min == 15
    assert cfg.max_title_chars == 80
    assert cfg.max_body_chars == 240
    # Issue #26 round-4: ``notify`` is sparse — empty unless the user
    # provided overrides. The merged severity table lives in
    # :data:`DEFAULT_NOTIFY` and is consulted by the classifier when
    # ``cfg.notify`` has no entry for a kind.
    assert cfg.notify == {}
    assert cfg.templates == {}


def test_default_notify_severity_part_b_rebalance() -> None:
    """Issue #26 Part B: only the human-only-recovery kinds stay urgent."""
    assert DEFAULT_NOTIFY["approval_blocked"] == "urgent"
    assert DEFAULT_NOTIFY["pending_decision"] == "urgent"
    assert DEFAULT_NOTIFY["user_reply_not_forwarded"] == "urgent"
    assert DEFAULT_NOTIFY["ci_failed"] == "urgent"
    assert DEFAULT_NOTIFY["pane_crashed"] == "urgent"
    # Demoted to normal in this PR.
    for demoted in (
        "relay_gap_suspected",
        "silent_worker_output",
        "pane_silent",
        "worker_stalled",
        "worker_not_reported",
        "worker_error",
    ):
        assert DEFAULT_NOTIFY[demoted] == "normal", demoted
    # Already-normal kinds unchanged.
    assert DEFAULT_NOTIFY["worker_completed"] == "normal"
    assert DEFAULT_NOTIFY["pr_merged"] == "normal"


def test_load_missing_file_returns_defaults(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "missing.json")
    assert cfg == AttentionConfig()


def test_load_none_returns_defaults() -> None:
    assert load_config(None) == AttentionConfig()


def test_load_full_config(tmp_path: Path) -> None:
    path = tmp_path / "attention.json"
    path.write_text(
        json.dumps({
            "desktop": False,
            "sound": "off",
            "cooldown_sec": 60,
            "poll_interval_sec": 5,
            "pending_decision_min": 20,
            "user_replied_min": 7,
            "max_title_chars": 40,
            "max_body_chars": 100,
            "notify": {"worker_completed": "urgent"},
            "templates": {
                "ci_failed": {
                    "title": "CI Failed",
                    "body": "PR #{pr} status={status}",
                },
            },
        }),
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.desktop is False
    assert cfg.sound == "off"
    assert cfg.cooldown_sec == 60
    assert cfg.poll_interval_sec == 5
    assert cfg.pending_decision_min == 20
    assert cfg.user_replied_min == 7
    assert cfg.max_title_chars == 40
    assert cfg.max_body_chars == 100
    assert cfg.notify["worker_completed"] == "urgent"
    # Issue #26 round-4: cfg.notify is sparse — only entries explicitly
    # set in the config JSON live here. Unset keys are resolved against
    # DEFAULT_NOTIFY by the classifier, which is what makes the TTL
    # demote path work on the CLI route.
    assert "approval_blocked" not in cfg.notify
    assert cfg.templates["ci_failed"] == Template(
        title="CI Failed", body="PR #{pr} status={status}",
    )


def test_invalid_sound_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"sound": "noisy"}), encoding="utf-8")
    with pytest.raises(ValueError, match="config.sound"):
        load_config(path)


def test_invalid_notify_severity_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps({"notify": {"ci_failed": "panic"}}), encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must be 'urgent' or 'normal'"):
        load_config(path)


def test_negative_int_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"cooldown_sec": -1}), encoding="utf-8")
    with pytest.raises(ValueError, match="non-negative"):
        load_config(path)


def test_non_int_for_int_field_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps({"max_title_chars": "lots"}), encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must be an integer"):
        load_config(path)


def test_bool_not_accepted_as_int(tmp_path: Path) -> None:
    # bool is a subclass of int in Python — guard against that here so a
    # ja config can't accidentally pass `True` as a cooldown.
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"cooldown_sec": True}), encoding="utf-8")
    with pytest.raises(ValueError, match="must be an integer"):
        load_config(path)


def test_template_must_be_object(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps({"templates": {"ci_failed": "string"}}), encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must be a JSON object"):
        load_config(path)


def test_template_title_body_must_be_strings(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps({
            "templates": {"ci_failed": {"title": 5, "body": "ok"}},
        }),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must have string"):
        load_config(path)


def test_top_level_must_be_object(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps([1, 2]), encoding="utf-8")
    with pytest.raises(ValueError, match="must be a JSON object"):
        load_config(path)


# ---------------------------------------------------------------------------
# Issue #26 Part A: TTL ladder config (pending_decision_max / _drop)
# ---------------------------------------------------------------------------


def test_load_pending_decision_max_and_drop(tmp_path: Path) -> None:
    """New TTL knobs round-trip from JSON into AttentionConfig."""
    path = tmp_path / "attention.json"
    path.write_text(
        json.dumps({
            "pending_decision_min": 5,
            "pending_decision_max": 60,
            "pending_decision_drop": 600,
        }),
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.pending_decision_min == 5
    assert cfg.pending_decision_max == 60
    assert cfg.pending_decision_drop == 600


def test_backward_compat_missing_ttl_keys_fills_defaults(tmp_path: Path) -> None:
    """Pre-Issue-#26 user configs (no TTL keys) keep working with defaults."""
    path = tmp_path / "attention.json"
    path.write_text(
        json.dumps({"pending_decision_min": 30}),  # no max/drop
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.pending_decision_min == 30
    assert cfg.pending_decision_max == 1440  # default fallback
    assert cfg.pending_decision_drop == 10080  # default fallback


def test_pending_decision_max_must_exceed_min() -> None:
    with pytest.raises(
        ValueError, match="pending_decision_max must be greater than"
    ):
        AttentionConfig(
            pending_decision_min=100, pending_decision_max=100,
        )


def test_pending_decision_max_must_strictly_exceed_min() -> None:
    """``max == min`` is rejected — needs a real demotion window."""
    with pytest.raises(
        ValueError, match="pending_decision_max must be greater than"
    ):
        AttentionConfig(
            pending_decision_min=200, pending_decision_max=150,
        )


def test_pending_decision_drop_must_exceed_max() -> None:
    with pytest.raises(
        ValueError, match="pending_decision_drop must be greater than"
    ):
        AttentionConfig(
            pending_decision_min=10,
            pending_decision_max=100,
            pending_decision_drop=100,
        )


def test_pending_decision_drop_below_max_rejected() -> None:
    with pytest.raises(
        ValueError, match="pending_decision_drop must be greater than"
    ):
        AttentionConfig(
            pending_decision_min=10,
            pending_decision_max=100,
            pending_decision_drop=50,
        )


def test_pending_decision_max_must_exceed_user_replied_min() -> None:
    """user_reply_not_forwarded shares the ``max`` ceiling.

    If a user pins ``user_replied_min`` ≥ ``pending_decision_max``,
    the user_reply ladder never produces an urgent tier — the first
    eligible age is already past ``max``. Validate so the
    misconfiguration trips at construction instead of silently
    suppressing all relay-gap alerts.
    """
    with pytest.raises(
        ValueError,
        match="pending_decision_max must be greater than user_replied_min",
    ):
        AttentionConfig(
            pending_decision_min=10,
            user_replied_min=2000,
            pending_decision_max=1440,
        )


def test_backward_compat_user_replied_min_above_default_max_auto_scales(
    tmp_path: Path,
) -> None:
    """Same backward-compat as ``pending_decision_min``, but for
    ``user_replied_min`` — a legacy "alert me after a day" config
    should still load.
    """
    path = tmp_path / "legacy.json"
    path.write_text(
        json.dumps({"user_replied_min": 2880}),  # 48h, > default max
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.user_replied_min == 2880
    assert cfg.pending_decision_max > 2880
    assert cfg.pending_decision_drop > cfg.pending_decision_max


def test_load_config_propagates_ttl_validation(tmp_path: Path) -> None:
    """An invalid TTL ladder in JSON surfaces ValueError via load_config."""
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps({
            "pending_decision_min": 10,
            "pending_decision_max": 5,
        }),
        encoding="utf-8",
    )
    with pytest.raises(
        ValueError, match="pending_decision_max must be greater than"
    ):
        load_config(path)


def test_negative_pending_decision_max_rejected(tmp_path: Path) -> None:
    """non-negative guard applies to the new knobs too."""
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps({"pending_decision_max": -1}), encoding="utf-8",
    )
    with pytest.raises(ValueError, match="non-negative"):
        load_config(path)


def test_backward_compat_min_above_default_max_auto_scales(
    tmp_path: Path,
) -> None:
    """A legacy config with min > default_max (1440) must still load.

    Before Issue #26 there was no ``max`` / ``drop`` knob, so a user
    config that set ``pending_decision_min`` arbitrarily high (e.g. a
    silenced "alert me after 2 days" setup) would still work. The
    new validation would otherwise reject it because the default max
    (1440) would be ≤ the user min. The loader auto-scales the
    missing ``max`` / ``drop`` knobs upward so the legacy config keeps
    loading.
    """
    path = tmp_path / "legacy.json"
    path.write_text(
        json.dumps({"pending_decision_min": 2880}),  # 48h, > default max
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.pending_decision_min == 2880
    assert cfg.pending_decision_max > 2880
    assert cfg.pending_decision_drop > cfg.pending_decision_max


def test_backward_compat_explicit_max_above_default_drop_auto_scales(
    tmp_path: Path,
) -> None:
    """If a user pins ``max`` above the default ``drop``, drop scales too."""
    path = tmp_path / "weird.json"
    path.write_text(
        json.dumps({
            "pending_decision_min": 60,
            "pending_decision_max": 20000,  # > default drop (10080)
        }),
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.pending_decision_max == 20000
    assert cfg.pending_decision_drop > 20000


def test_explicit_inversion_in_config_still_rejected(tmp_path: Path) -> None:
    """Backward-compat auto-scale must not mask an explicit inversion."""
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps({
            "pending_decision_min": 50,
            "pending_decision_max": 30,  # explicitly < min
        }),
        encoding="utf-8",
    )
    with pytest.raises(
        ValueError, match="pending_decision_max must be greater than"
    ):
        load_config(path)
