"""Tests for ``claude_org_runtime.attention.notify``.

Covers §5 acceptance for ``--dry-run`` (no subprocess), backend
fallback (stdout + bell), and §6 acceptance for template override,
unknown placeholder fallback, truncation, and bilingual support.
"""

from __future__ import annotations

from io import StringIO

import pytest

from claude_org_runtime.attention.classifier import AttentionEvent
from claude_org_runtime.attention.config import AttentionConfig, Template
from claude_org_runtime.attention.notify import (
    FormattedNotification,
    notify,
    render_text,
)


def _event(**kwargs) -> AttentionEvent:
    defaults = dict(
        key="event:1",
        kind="ci_failed",
        severity="urgent",
        title="CI failed",
        body="PR #42 finished with failed.",
        source="state.db.events",
        task_id="t1",
        worker="w1",
        pr=42,
        status="failed",
        summary=None,
        created_at="2026-05-12T10:00:00Z",
    )
    defaults.update(kwargs)
    return AttentionEvent(**defaults)


# ---------------------------------------------------------------------------
# render_text: §6 template + truncation + unknown placeholder
# ---------------------------------------------------------------------------


def test_render_uses_runtime_default_when_no_template() -> None:
    title, body = render_text(_event(), AttentionConfig())
    assert title == "CI failed"
    assert body == "PR #42 finished with failed."


def test_render_applies_user_template() -> None:
    cfg = AttentionConfig(templates={
        "ci_failed": Template(
            title="CI Failed", body="PR #{pr} status={status}",
        ),
    })
    title, body = render_text(_event(), cfg)
    assert title == "CI Failed"
    assert body == "PR #42 status=failed"


def test_render_unknown_placeholder_falls_back(capsys) -> None:
    cfg = AttentionConfig(templates={
        "ci_failed": Template(
            title="CI Failed",
            body="PR #{pr} branch={branch}",  # `branch` not in allowlist
        ),
    })
    title, body = render_text(_event(), cfg)
    # Falls back to runtime default for BOTH title and body (we don't
    # render half a template — the warning + fallback is whole-event).
    assert title == "CI failed"
    assert body == "PR #42 finished with failed."
    err = capsys.readouterr().err
    assert "branch" in err
    assert "falling back" in err


def test_render_truncates_long_body() -> None:
    cfg = AttentionConfig(
        max_title_chars=10, max_body_chars=20,
        templates={"ci_failed": Template(
            title="A" * 80, body="B" * 80,
        )},
    )
    title, body = render_text(_event(), cfg)
    assert len(title) == 10
    assert title.endswith("…")
    assert len(body) == 20
    assert body.endswith("…")


def test_render_supports_japanese_template() -> None:
    """§6 acceptance: ja can supply Japanese text without surprises."""
    cfg = AttentionConfig(templates={
        "ci_failed": Template(
            title="CI が失敗しました",
            body="PR #{pr} が {status} で完了しました。",
        ),
    })
    title, body = render_text(_event(), cfg)
    assert title == "CI が失敗しました"
    assert body == "PR #42 が failed で完了しました。"


def test_render_summary_placeholder() -> None:
    cfg = AttentionConfig(templates={
        "pending_decision": Template(
            title="判断待ち",
            body="{task_id}: {summary}",
        ),
    })
    ev = _event(
        kind="pending_decision", severity="urgent",
        title="X", body="Y", source="pending_decisions",
        task_id="T", summary="should we ship?", pr=None, status=None,
        worker=None,
    )
    title, body = render_text(ev, cfg)
    assert title == "判断待ち"
    assert body == "T: should we ship?"


# ---------------------------------------------------------------------------
# notify: dispatch behavior
# ---------------------------------------------------------------------------


def test_dry_run_skips_subprocess_and_dedup_state(capsys) -> None:
    """§5 acceptance: ``--dry-run`` does not call notify subprocess."""
    calls: list[list[str]] = []

    def fake_runner(cmd: list[str]):
        calls.append(cmd)
        return None

    out = StringIO()
    result = notify(
        _event(), AttentionConfig(),
        dry_run=True, backend="linux", log_stream=out, runner=fake_runner,
    )
    assert calls == []
    assert result.desktop_dispatched is False
    assert result.bell_dispatched is False
    # stdout log line still emitted in dry-run.
    assert "URGENT" in out.getvalue()
    assert "ci_failed" in out.getvalue()


def test_dispatch_invokes_backend_runner() -> None:
    calls: list[list[str]] = []

    def fake_runner(cmd: list[str]):
        calls.append(cmd)
        return None

    result = notify(
        _event(), AttentionConfig(),
        backend="linux", log_stream=StringIO(), runner=fake_runner,
    )
    assert result.desktop_dispatched is True
    assert calls and calls[0][0] == "notify-send"
    assert "CI failed" in calls[0][1]  # title arg


def test_stdout_backend_no_subprocess_but_bells_on_urgent() -> None:
    """§5 acceptance: desktop backend missing → stdout + bell fallback."""
    out = StringIO()
    result = notify(
        _event(), AttentionConfig(),
        backend="stdout", log_stream=out,
        runner=lambda cmd: pytest.fail("runner should not run"),
    )
    assert result.desktop_dispatched is False
    assert result.bell_dispatched is True
    assert "URGENT" in out.getvalue()


def test_bell_rings_on_macos_desktop_success() -> None:
    """§5 ``macOS sound = afplay / else bell``: bell on urgent success."""
    result = notify(
        _event(), AttentionConfig(),  # urgent + sound=urgent-only
        backend="macos", log_stream=StringIO(),
        runner=lambda cmd: None,  # success
    )
    assert result.desktop_dispatched is True
    assert result.bell_dispatched is True


def test_bell_rings_on_linux_desktop_success() -> None:
    """§5 ``Linux sound = paplay / canberra / bell``: bell on urgent success."""
    result = notify(
        _event(), AttentionConfig(),
        backend="linux", log_stream=StringIO(),
        runner=lambda cmd: None,
    )
    assert result.desktop_dispatched is True
    assert result.bell_dispatched is True


def test_no_double_bell_on_windows_success() -> None:
    """Windows / WSL PowerShell command embeds beep — no terminal bell."""
    result = notify(
        _event(), AttentionConfig(),
        backend="windows", log_stream=StringIO(),
        runner=lambda cmd: None,
    )
    assert result.desktop_dispatched is True
    assert result.bell_dispatched is False


def test_normal_severity_no_bell_with_urgent_only() -> None:
    cfg = AttentionConfig()
    out = StringIO()
    result = notify(
        _event(severity="normal", kind="worker_completed"),
        cfg, backend="stdout", log_stream=out,
        runner=lambda cmd: pytest.fail("no runner expected"),
    )
    assert result.bell_dispatched is False


def test_sound_off_silent_even_on_urgent() -> None:
    cfg = AttentionConfig(sound="off")
    result = notify(
        _event(), cfg, backend="stdout", log_stream=StringIO(),
        runner=lambda cmd: pytest.fail("no runner expected"),
    )
    assert result.bell_dispatched is False


def test_runner_exception_falls_back_to_bell(capsys) -> None:
    def explode(cmd: list[str]):
        raise OSError("nope")

    out = StringIO()
    result = notify(
        _event(), AttentionConfig(),
        backend="linux", log_stream=out, runner=explode,
    )
    assert result.desktop_dispatched is False
    assert result.bell_dispatched is True
    err = capsys.readouterr().err
    assert "failed" in err


def test_runner_nonzero_returncode_falls_back_to_bell(capsys) -> None:
    """A failing notify-send (DBus missing, etc.) must NOT report success."""
    class FakeProc:
        returncode = 1

    def failing_runner(cmd: list[str]):
        return FakeProc()

    out = StringIO()
    result = notify(
        _event(), AttentionConfig(),
        backend="linux", log_stream=out, runner=failing_runner,
    )
    assert result.desktop_dispatched is False
    assert result.bell_dispatched is True
    err = capsys.readouterr().err
    assert "exited with code 1" in err


# ---------------------------------------------------------------------------
# reached_user contract (round 3 codex Major)
# ---------------------------------------------------------------------------


def test_reached_user_true_for_stdout_only_mode() -> None:
    """No desktop attempt + no bell still counts as reached when intentional."""
    result = notify(
        _event(severity="normal", kind="worker_completed"),
        AttentionConfig(),
        backend="stdout", log_stream=StringIO(),
    )
    assert result.desktop_intended is False
    assert result.desktop_dispatched is False
    assert result.bell_dispatched is False
    assert result.reached_user is True


def test_reached_user_false_when_desktop_failed_and_silent() -> None:
    """sound=off + backend failure → did not reach the user → retry."""
    class FailingProc:
        returncode = 1

    cfg = AttentionConfig(sound="off")
    result = notify(
        _event(severity="normal", kind="worker_completed"),
        cfg, backend="linux", log_stream=StringIO(),
        runner=lambda _: FailingProc(),
    )
    assert result.desktop_intended is True
    assert result.desktop_dispatched is False
    assert result.bell_dispatched is False
    assert result.reached_user is False


def test_reached_user_true_when_bell_rang_after_desktop_failed() -> None:
    class FailingProc:
        returncode = 1

    result = notify(
        _event(),  # urgent
        AttentionConfig(),  # sound=urgent-only
        backend="linux", log_stream=StringIO(),
        runner=lambda _: FailingProc(),
    )
    assert result.bell_dispatched is True
    assert result.reached_user is True


def test_reached_user_true_when_desktop_disabled_only() -> None:
    """``desktop=False`` is an intentional config — log alone is delivery."""
    cfg = AttentionConfig(desktop=False, sound="off")
    result = notify(
        _event(severity="normal", kind="worker_completed"),
        cfg, backend="linux", log_stream=StringIO(),
        runner=lambda _: pytest.fail("desktop=False, runner must not run"),
    )
    assert result.desktop_intended is False
    assert result.reached_user is True


# ---------------------------------------------------------------------------
# Windows / WSL beep gating (round 3 codex Major)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend", ["windows", "wsl"])
def test_powershell_subprocess_skipped_when_sound_off(backend) -> None:
    """Win/WSL with sound off: the subprocess would be invisible to the
    user (Write-Host into a captured stream), so we skip it entirely
    and treat delivery as intentional stdout-only mode.
    """
    calls: list[list[str]] = []
    cfg = AttentionConfig(sound="off")
    result = notify(
        _event(), cfg, backend=backend, log_stream=StringIO(),
        runner=lambda cmd: calls.append(cmd),
    )
    assert calls == []
    assert result.desktop_intended is False
    assert result.reached_user is True


@pytest.mark.parametrize("backend", ["windows", "wsl"])
def test_powershell_beep_present_when_sound_urgent(backend) -> None:
    calls: list[list[str]] = []
    cfg = AttentionConfig()  # sound="urgent-only", event is urgent
    notify(
        _event(), cfg, backend=backend, log_stream=StringIO(),
        runner=lambda cmd: calls.append(cmd),
    )
    assert calls
    joined = " ".join(calls[0])
    assert "console]::beep" in joined


def test_desktop_disabled_still_bells_on_urgent() -> None:
    cfg = AttentionConfig(desktop=False)
    out = StringIO()
    result = notify(
        _event(), cfg, backend="linux", log_stream=out,
        runner=lambda cmd: pytest.fail("desktop disabled, no runner"),
    )
    assert result.desktop_dispatched is False
    assert result.bell_dispatched is True


def test_control_chars_stripped_from_command() -> None:
    calls: list[list[str]] = []

    def capture(cmd: list[str]):
        calls.append(cmd)

    ev = _event(title="ok\x07evil", body="hi\nthere")
    notify(ev, AttentionConfig(), backend="linux",
           log_stream=StringIO(), runner=capture)
    assert calls
    title_arg, body_arg = calls[0][1], calls[0][2]
    assert "\x07" not in title_arg
    assert "\n" not in body_arg
