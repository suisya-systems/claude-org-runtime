"""Unit tests for the scrub_fixture script."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.scrub import scrub_fixture as sf


def test_url_is_redacted() -> None:
    assert sf.scrub_text("see https://example.org/foo?x=1") == (
        "see https://example.com/REDACTED"
    )


def test_email_is_redacted() -> None:
    assert sf.scrub_text("contact alice.smith+work@example.org") == (
        "contact redacted@example.com"
    )


@pytest.mark.parametrize(
    "secret",
    [
        "ghp_" + "a" * 36,
        "github_pat_" + "B" * 24,
        "gho_" + "c" * 36,
        "ghs_" + "d" * 36,
        "ghu_" + "e" * 36,
        "ghr_" + "f" * 36,
        "sk-" + "x" * 40,
        "sk-proj-" + "y" * 40,
        "AKIA" + "Z" * 16,
        "ASIA" + "Y" * 16,
        "xoxb-" + "1" * 20,
    ],
)
def test_api_keys_are_redacted(secret: str) -> None:
    out = sf.scrub_text(f"token={secret} end")
    assert secret not in out
    assert sf.KEY_REPLACEMENT in out


def test_preserved_fields_are_untouched() -> None:
    record = {
        "task_id": "T-123",
        "event": "https://internal.example/start",  # would normally be scrubbed
        "ts": "2026-05-02T10:00:00Z",
        "pane_id": "pane-https://x",
        "pane_name": "alice@team.example",
        "status": "ACTIVE",
        "state": "https://internal.example/state",
        "note": "short note",
    }
    line = json.dumps(record)
    scrubbed = json.loads(sf.scrub_jsonl(line).strip())
    for field in (
        "task_id",
        "event",
        "ts",
        "pane_id",
        "pane_name",
        "status",
        "state",
    ):
        assert scrubbed[field] == record[field], field


def test_short_note_keeps_text_but_scrubs_pii() -> None:
    record = {"task_id": "T-1", "note": "ping bob@example.com"}
    scrubbed = json.loads(sf.scrub_jsonl(json.dumps(record)).strip())
    assert scrubbed["note"] == "ping redacted@example.com"
    assert scrubbed["task_id"] == "T-1"


def test_long_note_is_replaced_wholesale() -> None:
    long_note = "x" * sf.NOTE_REDACT_THRESHOLD
    record = {"task_id": "T-2", "note": long_note}
    scrubbed = json.loads(sf.scrub_jsonl(json.dumps(record)).strip())
    assert scrubbed["note"] == sf.NOTE_REPLACEMENT


def test_jsonl_preserves_one_record_per_line() -> None:
    src = (
        json.dumps({"task_id": "A", "note": "ok"})
        + "\n"
        + json.dumps({"task_id": "B", "note": "ok"})
        + "\n"
    )
    out = sf.scrub_jsonl(src)
    assert out.count("\n") == 2
    assert all(json.loads(l)["task_id"] in {"A", "B"} for l in out.splitlines())


def test_session_narrative_block_is_replaced() -> None:
    md = (
        "# Org State\n\n"
        "## 2026-05-02 セッション #7 主要成果\n\n"
        "alice@example.com で長文の振り返り。https://wiki/internal を参照。\n"
        "- bullet one\n- bullet two\n\n"
        "## Notes\n\nresidual paragraph with bob@example.com.\n"
    )
    out = sf.scrub_markdown(md)
    assert sf.SESSION_REPLACEMENT in out
    assert "alice@example.com" not in out
    assert "wiki/internal" not in out
    # The non-session H2 block stays, but its inline PII is still scrubbed.
    assert "## Notes" in out
    assert "redacted@example.com" in out


def test_synthetic_fixture_round_trip() -> None:
    root = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic"
    src = (root / "scrub_input_sample.jsonl").read_text(encoding="utf-8")
    expected = (root / "expected_output.jsonl").read_text(encoding="utf-8")
    assert sf.scrub_jsonl(src) == expected


def test_main_writes_output_and_diff(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = tmp_path / "in.jsonl"
    dst = tmp_path / "out.jsonl"
    src.write_text(
        json.dumps({"task_id": "T", "note": "see https://x.example/y"}) + "\n",
        encoding="utf-8",
    )
    rc = sf.main(["--in", str(src), "--out", str(dst), "--diff"])
    assert rc == 0
    written = dst.read_text(encoding="utf-8")
    assert "https://x.example" not in written
    assert sf.URL_REPLACEMENT in written
    captured = capsys.readouterr().out
    assert "before" in captured and "after" in captured
