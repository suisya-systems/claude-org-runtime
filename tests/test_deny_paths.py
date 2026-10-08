"""Generator and ``sandbox doctor`` must resolve deny entries identically.

Both resolve through :func:`deny_paths.resolve_deny_entry`; this pins the
invariant end to end so a future caller-side shortcut (a second ``~/``
expansion, a ``startswith("/")`` check) shows up as a coverage diff.
"""

from __future__ import annotations

import os

import pytest

from claude_org_runtime.settings import deny_paths, generator, sandbox_doctor

# Every anchor form on both layers. ``os.path.abspath`` gives a native
# absolute path, i.e. a drive-letter one on Windows.
NATIVE_ABS = os.path.abspath("deny-target")
LAYER2 = [
    "Read(~/.aws/*)",
    "Edit(//etc/shadow)",
    "Write(~/x/**)",
    "Read(.env)",
    "Read(/project/rel)",
    "Read(**/credentials*)",
    "Bash(cat ~/.aws/config)",
    "not-a-rule",
]
LAYER3 = [
    "~/.ssh/**",
    "/etc/shadow",
    NATIVE_ABS,
    "/*",
    "secrets.env",
    "**/credentials*",
    {"anchor": "home", "path": ".aws/**"},
]


def _generator_paths(settings: dict, monkeypatch: pytest.MonkeyPatch) -> set:
    seen: set = set()

    def record(path: str, **_kw: object) -> None:
        seen.add(path)
        return None

    monkeypatch.setattr(generator, "_canonicalize_escaping_path", record)
    # Same two calls render_role_with_metadata makes.
    generator._canonicalize_sandbox_filesystem(settings.get("sandbox"))
    generator._canonicalize_deny(
        settings["permissions"]["deny"], deny_paths.PERMISSION_DENY_LAYER
    )
    return seen


@pytest.mark.parametrize("enabled", [True, False, None])
def test_generator_and_doctor_resolve_same_host_paths(
    enabled: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    # denyWrite carries one extra entry so dropping either key shows up.
    sandbox: dict = {
        "filesystem": {"denyRead": LAYER3, "denyWrite": [*LAYER3, "~/w/**"]}
    }
    if enabled is not None:
        sandbox["enabled"] = enabled
    settings = {"permissions": {"deny": LAYER2}, "sandbox": sandbox}

    doctor = {
        t.path for t in sandbox_doctor.collect_deny_targets(settings) if t.path
    }
    assert doctor == _generator_paths(settings, monkeypatch)

    home = os.path.expanduser("~")
    assert doctor == {
        f"{home}/.aws/*",
        "/etc/shadow",
        f"{home}/x/**",
        f"{home}/.ssh/**",
        f"{home}/w/**",
        NATIVE_ABS,
        # ``/etc/shadow`` and ``/*`` are absolute on POSIX and on Windows
        # (rooted, no drive) for the Python versions CI runs.
        "/*",
    }


def test_resolve_deny_entry_rejects_unknown_layer() -> None:
    with pytest.raises(ValueError):
        deny_paths.resolve_deny_entry("~/x", layer="sandbox.network.deny")
