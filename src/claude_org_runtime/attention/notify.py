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
    """Record of what was sent (returned by :func:`notify`)."""

    title: str
    body: str
    severity: str
    sound: bool
    backend: Backend
    desktop_dispatched: bool
    bell_dispatched: bool


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
        )

    if cfg.desktop and chosen != "stdout":
        desktop_dispatched = _dispatch_desktop(
            chosen, title, body, runner=runner,
        )

    # Bell fallback applies when sound is wanted and either:
    #   (a) no desktop backend was usable, OR
    #   (b) the subprocess failed, OR
    #   (c) desktop was disabled outright.
    if play_sound and (not desktop_dispatched or not cfg.desktop):
        bell()
        bell_dispatched = True

    return FormattedNotification(
        title=title, body=body, severity=event.severity,
        sound=play_sound, backend=chosen,
        desktop_dispatched=desktop_dispatched,
        bell_dispatched=bell_dispatched,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _dispatch_desktop(
    backend: Backend, title: str, body: str,
    *, runner=None,
) -> bool:
    """Run the backend subprocess; return True only on a clean exit.

    ``run_fn`` is ``subprocess.run`` with ``check=False`` so a failing
    backend (e.g. ``notify-send`` with no DBus) does not raise; we
    inspect ``returncode`` explicitly. A non-zero exit demotes the
    return to ``False`` so :func:`notify` falls back to a bell — and,
    crucially, the caller does not mark the event as dedup'd, so the
    next poll re-attempts the notification.
    """
    cmd = _backend_command(backend, title, body)
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
    if backend == "windows":
        ps = shutil.which("powershell.exe") or "powershell"
        message = (
            f"Write-Host '{_ps_quote(safe_title)}: "
            f"{_ps_quote(safe_body)}'; [console]::beep(800,200)"
        )
        return [ps, "-NoProfile", "-Command", message]
    if backend == "wsl":
        message = (
            f"Write-Host '{_ps_quote(safe_title)}: "
            f"{_ps_quote(safe_body)}'; [console]::beep(800,200)"
        )
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
    """Strip ASCII control bytes except SPACE."""
    return "".join(ch for ch in s if ord(ch) >= 0x20 or ch == " ")


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
