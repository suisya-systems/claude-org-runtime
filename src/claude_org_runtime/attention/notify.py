"""Render and dispatch attention notifications.

Pipeline per event:

1. Pick a template (config override → bundled English default).
2. Detect unknown placeholders (outside §6 allowlist) and fall back to
   the bundled default if any are present — the watcher must not
   crash on a misspelled template per §6 acceptance criteria.
3. Truncate to ``max_title_chars`` / ``max_body_chars``.
4. Emit a stdout log line (always — including ``--dry-run``).
5. If desktop output is enabled and not dry-run, run the backend
   subprocess with a small timeout. If that fails / no backend is
   available, fall through to a terminal bell when sound applies.

Sub-process invocation:
* Never shells out via ``shell=True``.
* Times out at :data:`_SUBPROCESS_TIMEOUT_SEC` seconds.
* Strips control characters from title/body before composing arguments.
"""

from __future__ import annotations

import shutil
import string
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .classifier import AttentionEvent
from .config import ALLOWED_PLACEHOLDERS, AttentionConfig
from .platform import Backend, bell, detect_backend

_SUBPROCESS_TIMEOUT_SEC: float = 5.0


@dataclass(frozen=True)
class FormattedNotification:
    """Record of what was sent (returned by :func:`notify`).

    ``reached_user`` is True when at least one user-visible channel ran:
    either the desktop subprocess succeeded, the bell rang, or the
    runtime is intentionally in stdout-only mode (the log line is the
    notification). The CLI uses this to decide whether to record dedup
    state — a silently-failing desktop subprocess should retry on the
    next poll, but an stdout-only setup must not replay forever.
    """

    title: str
    body: str
    severity: str
    sound: bool
    backend: Backend
    desktop_dispatched: bool
    bell_dispatched: bool
    desktop_intended: bool

    @property
    def reached_user(self) -> bool:
        if self.desktop_dispatched or self.bell_dispatched:
            return True
        # No desktop attempt was made: this is the user's chosen
        # stdout-only or desktop-disabled mode and the log line is the
        # entire notification surface. Treat it as delivered.
        return not self.desktop_intended


def render_text(
    event: AttentionEvent, cfg: AttentionConfig,
) -> tuple[str, str]:
    """Return ``(title, body)`` after template + truncation."""
    template = cfg.templates.get(event.kind)
    title, body = event.title, event.body
    if template is not None:
        used = _placeholders(template.title) | _placeholders(template.body)
        unknown = used - ALLOWED_PLACEHOLDERS
        if unknown:
            print(
                f"warning: attention template[{event.kind!r}] uses unknown "
                f"placeholders {sorted(unknown)}; falling back to runtime "
                "default",
                file=sys.stderr,
            )
        else:
            try:
                title = _format_with_event(template.title, event)
                body = _format_with_event(template.body, event)
            except (ValueError, IndexError) as exc:
                print(
                    f"warning: attention template[{event.kind!r}] format "
                    f"failed ({exc}); falling back to runtime default",
                    file=sys.stderr,
                )
                title, body = event.title, event.body
    return (
        _truncate(title, cfg.max_title_chars),
        _truncate(body, cfg.max_body_chars),
    )


def notify(
    event: AttentionEvent,
    cfg: AttentionConfig,
    *,
    dry_run: bool = False,
    backend: Optional[Backend] = None,
    log_stream=None,
    runner: Optional[Callable[[list[str]], Any]] = None,
) -> FormattedNotification:
    """Emit one attention notification (and stdout log).

    ``dry_run=True`` keeps the stdout log line but skips both the OS
    subprocess and the terminal bell (the latter so unit tests stay
    silent). ``backend`` overrides auto-detection. ``runner`` overrides
    :func:`subprocess.run`; tests use this to capture the command
    instead of executing it.
    """
    log_stream = log_stream if log_stream is not None else sys.stdout
    title, body = render_text(event, cfg)
    chosen = backend if backend is not None else detect_backend()
    play_sound = _should_play_sound(cfg.sound, event.severity)
    desktop_intended = bool(cfg.desktop) and chosen != "stdout"

    # Windows / WSL backends signal exclusively through the embedded
    # ``[console]::beep`` — the ``Write-Host`` output goes to a
    # captured PowerShell stdout we discard, so it is invisible to the
    # user. With sound suppressed the subprocess would dispatch
    # successfully yet deliver nothing, so downgrade to intentional
    # stdout-only delivery instead of pretending it worked.
    if chosen in ("windows", "wsl") and not play_sound:
        desktop_intended = False

    log_stream.write(
        f"[attention] {event.severity.upper()} {event.kind} "
        f"key={event.key} task={event.task_id or '-'} :: {title}\n"
    )
    log_stream.flush()

    desktop_dispatched = False
    bell_dispatched = False
    if dry_run:
        return FormattedNotification(
            title=title, body=body, severity=event.severity,
            sound=play_sound, backend=chosen,
            desktop_dispatched=False, bell_dispatched=False,
            desktop_intended=desktop_intended,
        )

    if desktop_intended:
        desktop_dispatched = _dispatch_desktop(
            chosen, title, body, play_sound=play_sound, runner=runner,
        )

    # Bell semantics per §5:
    #   - macOS / Linux: notification is visual-only; ring the bell as
    #     the audio channel (afplay / paplay would be a richer choice
    #     but those would each need an opt-in dep — bell stays generic).
    #   - Windows / WSL: ``[console]::beep`` is already inside the
    #     PowerShell command, so adding a bell here would double up.
    #   - desktop disabled / dispatch failed / stdout-only: bell is
    #     the only audio surface left.
    if play_sound and chosen not in ("windows", "wsl"):
        bell()
        bell_dispatched = True

    return FormattedNotification(
        title=title, body=body, severity=event.severity,
        sound=play_sound, backend=chosen,
        desktop_dispatched=desktop_dispatched,
        bell_dispatched=bell_dispatched,
        desktop_intended=desktop_intended,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _dispatch_desktop(
    backend: Backend, title: str, body: str,
    *, play_sound: bool, runner=None,
) -> bool:
    """Run the backend subprocess; return True only on a clean exit.

    ``run_fn`` is ``subprocess.run`` with ``check=False`` so a failing
    backend (e.g. ``notify-send`` with no DBus) does not raise; we
    inspect ``returncode`` explicitly. A non-zero exit demotes the
    return to ``False`` so :func:`notify` falls back to a bell — and,
    crucially, the caller does not mark the event as dedup'd, so the
    next poll re-attempts the notification. ``play_sound`` is threaded
    in so the Windows / WSL PowerShell command can conditionally
    include the ``[console]::beep`` — otherwise ``cfg.sound='off'``
    would still ring a beep on those platforms.
    """
    cmd = _backend_command(backend, title, body, play_sound=play_sound)
    if cmd is None:
        return False
    run_fn = runner or _safe_subprocess_run
    try:
        result = run_fn(cmd)
    except (OSError, subprocess.SubprocessError) as exc:
        print(
            f"warning: desktop notification via {backend!r} failed: {exc}",
            file=sys.stderr,
        )
        return False
    returncode = getattr(result, "returncode", 0)
    if returncode and returncode != 0:
        print(
            f"warning: desktop notification via {backend!r} exited "
            f"with code {returncode}",
            file=sys.stderr,
        )
        return False
    return True


def _safe_subprocess_run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        timeout=_SUBPROCESS_TIMEOUT_SEC,
        check=False,
        capture_output=True,
    )


def _backend_command(
    backend: Backend, title: str, body: str,
    *, play_sound: bool,
) -> Optional[list[str]]:
    safe_title = _strip_control(title)
    safe_body = _strip_control(body)
    if backend == "macos":
        script = (
            f"display notification {_apple_quote(safe_body)} "
            f"with title {_apple_quote(safe_title)}"
        )
        return ["osascript", "-e", script]
    if backend == "linux":
        return ["notify-send", safe_title, safe_body]
    if backend in ("windows", "wsl"):
        # Honour ``cfg.sound`` on the PowerShell path: include the
        # beep iff the caller asked for sound. Without this guard
        # ``sound="off"`` users still hear a beep on Windows / WSL.
        beep = "; [console]::beep(800,200)" if play_sound else ""
        message = (
            f"Write-Host '{_ps_quote(safe_title)}: "
            f"{_ps_quote(safe_body)}'{beep}"
        )
        if backend == "windows":
            ps = shutil.which("powershell.exe") or "powershell"
            return [ps, "-NoProfile", "-Command", message]
        return ["powershell.exe", "-NoProfile", "-Command", message]
    return None


def _should_play_sound(sound_mode: str, severity: str) -> bool:
    if sound_mode == "off":
        return False
    if sound_mode == "urgent-only":
        return severity == "urgent"
    return True  # "all"


def _truncate(s: str, limit: int) -> str:
    if limit <= 0 or len(s) <= limit:
        return s
    if limit == 1:
        return s[:1]
    return s[: limit - 1] + "…"


def _strip_control(s: str) -> str:
    """Drop ASCII C0 control bytes and DEL (\\x00-\\x1f, \\x7f)."""
    return "".join(ch for ch in s if 0x20 <= ord(ch) < 0x7F or ord(ch) > 0x7F)


def _apple_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _ps_quote(s: str) -> str:
    return s.replace("'", "''")


def _placeholders(template: str) -> set[str]:
    """Return the set of named placeholders referenced by ``template``.

    Uses :class:`string.Formatter` so ``{key:format-spec}`` and
    ``{key!conv}`` forms are recognised, not just ``{key}``.
    """
    out: set[str] = set()
    formatter = string.Formatter()
    try:
        for _, field_name, _, _ in formatter.parse(template):
            if not field_name:
                continue
            # Strip ``.attr`` / ``[idx]`` lookups — we only allow the
            # top-level name through.
            head = field_name.split(".")[0].split("[")[0]
            if head:
                out.add(head)
    except ValueError:
        # An unparsable template — treat every placeholder as unknown.
        # The caller will trigger the fallback path.
        out.add("__invalid__")
    return out


def _format_with_event(template: str, event: AttentionEvent) -> str:
    values = {
        "task_id": event.task_id or "",
        "worker": event.worker or "",
        "kind": event.kind,
        "status": event.status or "",
        "pr": "" if event.pr is None else str(event.pr),
        "summary": event.summary or "",
    }
    return template.format_map(values)
