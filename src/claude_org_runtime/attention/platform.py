"""OS-specific notification backend probes.

Backend selection is a pure function of (system, wsl?, which()) so the
unit tests can drive every branch without a real OS check. Per §5:

* macOS  → ``osascript display notification``
* Linux  → ``notify-send``
* WSL    → ``wsl-notify-send.exe`` (Windows toast) when installed,
  else ``powershell.exe`` Write-Host fallback
* Windows → ``wsl-notify-send.exe`` (Windows toast) when installed,
  else PowerShell beep + Write-Host fallback
* anything else / missing binary → ``"stdout"`` (caller bells + logs)
"""

from __future__ import annotations

import os
import platform as _stdlib_platform
import shutil
import sys
from typing import Callable, Literal, Optional

Backend = Literal[
    "macos", "linux", "windows", "wsl", "wsl-notify-send", "stdout",
]


def detect_backend(
    *,
    system: Optional[str] = None,
    is_wsl: Optional[bool] = None,
    which: Optional[Callable[[str], Optional[str]]] = None,
) -> Backend:
    """Pick the active backend. Arguments are test overrides.

    ``system`` defaults to :func:`platform.system`. ``is_wsl`` defaults
    to a best-effort WSL probe (env vars + ``/proc/version``).
    ``which`` defaults to :func:`shutil.which`; tests pass a stub.
    """
    sys_name = system if system is not None else _stdlib_platform.system()
    wsl = is_wsl if is_wsl is not None else detect_wsl()
    which_fn = which or shutil.which

    if sys_name == "Darwin":
        return "macos" if which_fn("osascript") else "stdout"

    if sys_name == "Linux":
        if wsl:
            # Prefer wsl-notify-send.exe: it surfaces a real Windows
            # toast notification, while the legacy Write-Host path only
            # writes to a captured PowerShell stdout that the user never
            # sees (Issue #25). Fall back when the binary is not on PATH
            # so users without the optional install keep the existing
            # beep-via-powershell behavior bit-for-bit.
            if which_fn("wsl-notify-send.exe"):
                return "wsl-notify-send"
            return "wsl" if which_fn("powershell.exe") else "stdout"
        return "linux" if which_fn("notify-send") else "stdout"

    if sys_name == "Windows":
        if which_fn("wsl-notify-send.exe"):
            return "wsl-notify-send"
        if which_fn("powershell.exe") or which_fn("powershell"):
            return "windows"
        return "stdout"

    return "stdout"


def detect_wsl() -> bool:
    """Best-effort WSL detection.

    The env vars (``WSL_DISTRO_NAME`` / ``WSL_INTEROP``) are set on
    WSL 1/2 since Windows 10 21H2. ``/proc/version`` is a fallback for
    minimal images that scrub the env. Both probes are read-only.
    """
    if "WSL_DISTRO_NAME" in os.environ or "WSL_INTEROP" in os.environ:
        return True
    try:
        with open("/proc/version", "r", encoding="utf-8") as f:
            content = f.read().lower()
    except OSError:
        return False
    return "microsoft" in content or "wsl" in content


def bell(stream=None) -> None:
    """Emit a BEL (``\\a``) — defaults to stderr to keep stdout clean."""
    if stream is None:
        stream = sys.stderr
    try:
        stream.write("\a")
        stream.flush()
    except (OSError, ValueError):
        # Closed pipes / non-tty streams must not crash the watcher.
        pass
