"""Issue #263: the cockpit pass names windows that were linked earlier."""

from __future__ import annotations

import pytest

from claude_org_runtime.terminal import tmux as tmux_mod
from claude_org_runtime.terminal.tmux import TmuxAdapter


@pytest.fixture
def adapter(monkeypatch: pytest.MonkeyPatch, fake_run) -> TmuxAdapter:
    monkeypatch.setattr(tmux_mod.subprocess, "run", fake_run)
    a = TmuxAdapter(exe="tmux")
    a._fake = fake_run  # type: ignore[attr-defined]
    return a


def _run(adapter, windows: list[str]) -> list[list[str]]:
    from claude_org_runtime.terminal import cockpit

    # has-session ok, source-file, list-windows, then _repair_home's list-windows.
    adapter._fake.queue((0, "", ""), (0, "", ""), (0, "\n".join(windows) + "\n", ""),
                        (0, "1\n", ""))
    cockpit.ensure_cockpit(adapter)
    return [c[3:] for c in adapter._fake.calls]


def _renamed(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if "rename-window" in c]


def test_already_linked_window_with_the_automatic_name_is_renamed_and_pinned(adapter) -> None:
    calls = _run(adapter, [
        "org\t@0\t\thome\toff",
        "org\t@3\tfix-1\tclaude\ton",
    ])
    renames = _renamed(calls)
    assert len(renames) == 1
    assert ["rename-window", "-t", "@3", "fix-1"] == renames[0][:4]
    assert ["set-option", "-w", "-t", "@3", "automatic-rename", "off"] == renames[0][-6:]
    assert not any(c[0] == "link-window" for c in calls)


def test_correctly_named_and_pinned_windows_are_left_alone(adapter) -> None:
    calls = _run(adapter, [
        "org\t@0\t\thome\toff",
        "org\t@3\tfix-1\tfix-1\toff",
    ])
    assert _renamed(calls) == []


def test_right_name_but_automatic_rename_still_on_is_pinned(adapter) -> None:
    calls = _run(adapter, [
        "org\t@3\tfix-1\tfix-1\ton",
    ])
    assert len(_renamed(calls)) == 1
