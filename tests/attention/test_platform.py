"""Tests for ``claude_org_runtime.attention.platform``."""

from __future__ import annotations

from io import StringIO
from typing import Optional

from claude_org_runtime.attention.platform import (
    bell,
    detect_backend,
    detect_wsl,
)


def _which_factory(found: set[str]):
    def _which(name: str) -> Optional[str]:
        return f"/usr/bin/{name}" if name in found else None
    return _which


def test_macos_with_osascript() -> None:
    assert detect_backend(
        system="Darwin", is_wsl=False, which=_which_factory({"osascript"}),
    ) == "macos"


def test_macos_without_osascript_falls_back() -> None:
    assert detect_backend(
        system="Darwin", is_wsl=False, which=_which_factory(set()),
    ) == "stdout"


def test_linux_with_notify_send() -> None:
    assert detect_backend(
        system="Linux", is_wsl=False, which=_which_factory({"notify-send"}),
    ) == "linux"


def test_linux_without_notify_send_falls_back() -> None:
    assert detect_backend(
        system="Linux", is_wsl=False, which=_which_factory(set()),
    ) == "stdout"


def test_wsl_with_powershell() -> None:
    assert detect_backend(
        system="Linux", is_wsl=True, which=_which_factory({"powershell.exe"}),
    ) == "wsl"


def test_wsl_without_powershell_falls_back() -> None:
    assert detect_backend(
        system="Linux", is_wsl=True, which=_which_factory(set()),
    ) == "stdout"


def test_wsl_prefers_wsl_notify_send_when_present() -> None:
    """Issue #25: prefer wsl-notify-send.exe (real toast) over Write-Host."""
    assert detect_backend(
        system="Linux", is_wsl=True,
        which=_which_factory({"wsl-notify-send.exe", "powershell.exe"}),
    ) == "wsl-notify-send"


def test_wsl_falls_back_to_write_host_without_wsl_notify_send() -> None:
    """No wsl-notify-send.exe + powershell.exe present → legacy ``wsl`` path."""
    assert detect_backend(
        system="Linux", is_wsl=True, which=_which_factory({"powershell.exe"}),
    ) == "wsl"


def test_windows_with_powershell() -> None:
    assert detect_backend(
        system="Windows", is_wsl=False,
        which=_which_factory({"powershell.exe"}),
    ) == "windows"


def test_windows_without_powershell_falls_back() -> None:
    assert detect_backend(
        system="Windows", is_wsl=False, which=_which_factory(set()),
    ) == "stdout"


def test_unknown_system_falls_back() -> None:
    assert detect_backend(
        system="Haiku", is_wsl=False, which=_which_factory({"anything"}),
    ) == "stdout"


def test_detect_wsl_does_not_raise(monkeypatch) -> None:
    # Make sure both /proc/version path and env-var path are exercised
    # without crashing the test runner. We do not assert the return
    # value because the test environment differs across CI hosts.
    monkeypatch.delenv("WSL_DISTRO_NAME", raising=False)
    monkeypatch.delenv("WSL_INTEROP", raising=False)
    detect_wsl()


def test_bell_writes_alert_byte() -> None:
    buf = StringIO()
    bell(stream=buf)
    assert buf.getvalue() == "\a"


def test_bell_swallows_closed_stream() -> None:
    class Broken:
        def write(self, _: str) -> int:
            raise OSError("closed")

        def flush(self) -> None:
            raise OSError("closed")
    # Must not raise.
    bell(stream=Broken())
