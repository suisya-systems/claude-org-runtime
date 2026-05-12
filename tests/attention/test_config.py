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
    assert cfg.user_replied_min == 15
    assert cfg.max_title_chars == 80
    assert cfg.max_body_chars == 240
    assert cfg.notify == DEFAULT_NOTIFY
    assert cfg.templates == {}


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
    # Defaults preserved for keys not overridden.
    assert cfg.notify["approval_blocked"] == "urgent"
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
