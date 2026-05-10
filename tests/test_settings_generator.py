"""Tests for the settings generator port."""

from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stdout
from importlib.resources import files
from pathlib import Path

import pytest

from claude_org_runtime.settings import generator
from claude_org_runtime import cli as runtime_cli


def test_bundled_schema_loads() -> None:
    schema = generator.load_schema()
    assert isinstance(schema, dict)
    assert "worker_roles" in schema
    assert isinstance(schema["worker_roles"], dict)


def test_bundled_schema_is_valid_json_file() -> None:
    resource = files("claude_org_runtime.settings").joinpath(
        "role_configs_schema.json"
    )
    text = resource.read_text(encoding="utf-8")
    parsed = json.loads(text)
    assert parsed["version"] >= 1


def test_render_role_substitutes_placeholders() -> None:
    schema = {
        "worker_roles": {
            "demo": {
                "description": "ignored",
                "$comment": "ignored",
                "permissions": {
                    "allow": [
                        "Read({worker_dir}/**)",
                        "Bash(test {claude_org_path})",
                    ],
                },
                "hooks": {"on_stop": [{"path": "{claude_org_path}/hook.sh"}]},
            },
        },
    }
    out = generator.render_role(
        schema,
        role="demo",
        worker_dir="/tmp/wd",
        claude_org_path="/tmp/co",
    )
    assert "description" not in out and "$comment" not in out
    assert out["permissions"]["allow"][0] == "Read(/tmp/wd/**)"
    assert out["permissions"]["allow"][1] == "Bash(test /tmp/co)"
    assert out["hooks"]["on_stop"][0]["path"] == "/tmp/co/hook.sh"


def test_render_role_unknown_raises_keyerror() -> None:
    schema = {"worker_roles": {"a": {}, "$ignored": {}}}
    with pytest.raises(KeyError) as info:
        generator.render_role(
            schema, role="nope", worker_dir="/", claude_org_path="/",
        )
    assert "unknown worker role" in info.value.args[0]


def test_render_role_dollar_prefixed_not_addressable() -> None:
    schema = {"worker_roles": {"$special": {"x": 1}}}
    with pytest.raises(KeyError):
        generator.render_role(
            schema, role="$special", worker_dir="/", claude_org_path="/",
        )


def test_cli_writes_to_out_file(tmp_path: Path) -> None:
    out = tmp_path / "settings.local.json"
    rc = generator.main([
        "--role", "default",
        "--worker-dir", str(tmp_path / "wd"),
        "--claude-org-path", str(tmp_path / "co"),
        "--out", str(out),
    ])
    assert rc == 0
    parsed = json.loads(out.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)


def test_cli_unknown_role_returns_2(tmp_path: Path) -> None:
    rc = generator.main([
        "--role", "nope-not-a-role",
        "--worker-dir", str(tmp_path),
        "--claude-org-path", str(tmp_path),
    ])
    assert rc == 2


def test_cli_schema_override(tmp_path: Path) -> None:
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(
        json.dumps({"worker_roles": {"x": {"k": "{worker_dir}"}}}),
        encoding="utf-8",
    )
    out = tmp_path / "out.json"
    rc = generator.main([
        "--role", "x",
        "--worker-dir", "/wd",
        "--claude-org-path", "/co",
        "--schema", str(schema_path),
        "--out", str(out),
    ])
    assert rc == 0
    parsed = json.loads(out.read_text(encoding="utf-8"))
    assert parsed == {"k": "/wd"}


# ---------------------------------------------------------------------------
# Bundled schema sanity (the SoT shipped to consumers)
# ---------------------------------------------------------------------------


def test_bundled_schema_default_role_renders() -> None:
    """Render the canonical 'default' role with the bundled SoT schema."""
    out = generator.render_role(
        generator.load_schema(),
        role="default",
        worker_dir="C:/tmp/worker",
        claude_org_path="C:/tmp/claude-org",
    )
    # Output must be JSON-serializable and shaped like a settings.local.json
    text = json.dumps(out)
    # Placeholders must have been substituted (no leftover {worker_dir}).
    assert "{worker_dir}" not in text
    assert "{claude_org_path}" not in text


# ---------------------------------------------------------------------------
# Phase 3 case E: sandbox + WSL/realpath suppression
# ---------------------------------------------------------------------------


def _sandbox_role(
    *,
    enabled: bool = True,
    deny_read: list[str] | None = None,
    deny_write: list[str] | None = None,
    additional: list[str] | None = None,
    fail_if_unavailable: bool = False,
) -> dict:
    return {
        "permissions": {
            "deny": [
                "Bash(git push *)",
                "Read(.env)",
                "Read(**/credentials*)",
            ],
        },
        "sandbox": {
            "enabled": enabled,
            "filesystem": {
                "denyRead": list(deny_read or []),
                "denyWrite": list(deny_write or []),
                "additionalDirectories": list(additional or []),
            },
            "failIfUnavailable": fail_if_unavailable,
        },
    }


def test_render_sandbox_disabled_passes_through() -> None:
    """sandbox.enabled=false: structure is preserved untouched."""
    schema = {
        "worker_roles": {
            "demo": _sandbox_role(
                enabled=False,
                deny_read=["/mnt/c/Users/somebody/secrets.env"],
            ),
        },
    }
    result = generator.render_role_with_metadata(
        schema,
        role="demo",
        worker_dir="/home/u/work/wd",
        claude_org_path="/home/u/co",
        wsl_detector=lambda: True,
    )
    assert result.sandbox.enabled is False
    assert result.sandbox.suppressions == []
    fs = result.settings["sandbox"]["filesystem"]
    assert fs["denyRead"] == ["/mnt/c/Users/somebody/secrets.env"]


def test_render_role_without_sandbox_field() -> None:
    """Roles without a sandbox field render unchanged (backward compat)."""
    schema = {
        "worker_roles": {
            "demo": {"permissions": {"deny": ["Read(.env)"]}},
        },
    }
    result = generator.render_role_with_metadata(
        schema,
        role="demo",
        worker_dir="/home/u/work/wd",
        claude_org_path="/home/u/co",
        wsl_detector=lambda: False,
    )
    assert "sandbox" not in result.settings
    assert result.sandbox.enabled is False
    assert result.sandbox.suppressions == []


def test_non_wsl_no_suppression_when_paths_inside_root(tmp_path: Path) -> None:
    """Non-WSL Linux: deny entries that stay inside worker_dir don't fire."""
    worker_dir = str(tmp_path / "wd")
    os.makedirs(worker_dir, exist_ok=True)
    schema = {
        "worker_roles": {
            "demo": _sandbox_role(deny_read=["secrets.env", "subdir/private"]),
        },
    }
    result = generator.render_role_with_metadata(
        schema,
        role="demo",
        worker_dir=worker_dir,
        claude_org_path=str(tmp_path / "co"),
        wsl_detector=lambda: False,
    )
    assert result.sandbox.wsl_detected is False
    assert result.sandbox.enabled is True
    assert result.sandbox.suppressions == []
    fs = result.settings["sandbox"]["filesystem"]
    assert fs["denyRead"] == ["secrets.env", "subdir/private"]


def test_wsl_realpath_escape_suppresses_entry() -> None:
    """WSL: realpath of a worker_dir-relative entry escapes -> suppression."""
    worker_dir = "/home/u/work/wd"

    def fake_realpath(p: str) -> str:
        # The worker_dir itself is a host-cross symlink to /mnt/c/...
        if p == worker_dir or p.startswith(worker_dir + "/"):
            return p.replace(worker_dir, "/mnt/c/Users/u/work/wd", 1)
        return p

    schema = {
        "worker_roles": {
            "demo": _sandbox_role(
                deny_read=["secrets.env"],
                deny_write=["build/"],
                additional=[],
            ),
        },
    }
    result = generator.render_role_with_metadata(
        schema,
        role="demo",
        worker_dir=worker_dir,
        claude_org_path="/home/u/co",
        realpath_fn=fake_realpath,
        wsl_detector=lambda: True,
    )
    assert result.sandbox.wsl_detected is True
    suppressed_entries = {(s.layer, s.entry) for s in result.sandbox.suppressions}
    assert ("sandbox.filesystem.denyRead", "secrets.env") in suppressed_entries
    assert ("sandbox.filesystem.denyWrite", "build/") in suppressed_entries
    fs = result.settings["sandbox"]["filesystem"]
    assert fs["denyRead"] == []
    assert fs["denyWrite"] == []
    # Layer 2 permissions.deny is preserved untouched.
    deny = result.settings["permissions"]["deny"]
    assert "Read(.env)" in deny
    assert "Read(**/credentials*)" in deny


def test_wsl_realpath_inside_additional_directories_no_suppression() -> None:
    """WSL but realpath stays inside additionalDirectories -> no suppression."""
    worker_dir = "/home/u/work/wd"

    def fake_realpath(p: str) -> str:
        if p == worker_dir or p.startswith(worker_dir + "/"):
            return p.replace(worker_dir, "/mnt/c/Users/u/work/wd", 1)
        if p == "/mnt/c" or p.startswith("/mnt/c/"):
            return p
        return p

    schema = {
        "worker_roles": {
            "demo": _sandbox_role(
                deny_read=["secrets.env"],
                additional=["/mnt/c"],
            ),
        },
    }
    result = generator.render_role_with_metadata(
        schema,
        role="demo",
        worker_dir=worker_dir,
        claude_org_path="/home/u/co",
        realpath_fn=fake_realpath,
        wsl_detector=lambda: True,
    )
    assert result.sandbox.suppressions == []
    fs = result.settings["sandbox"]["filesystem"]
    assert fs["denyRead"] == ["secrets.env"]


def test_devcontainer_workspaces_symlink_suppression() -> None:
    """Devcontainer-like /workspaces symlink case: realpath escapes."""
    worker_dir = "/home/u/wd"

    def fake_realpath(p: str) -> str:
        if p == worker_dir or p.startswith(worker_dir + "/"):
            return p.replace(worker_dir, "/workspaces/repo", 1)
        return p

    schema = {
        "worker_roles": {
            "demo": _sandbox_role(deny_read=["secrets.env"], additional=[]),
        },
    }
    result = generator.render_role_with_metadata(
        schema,
        role="demo",
        worker_dir=worker_dir,
        claude_org_path="/home/u/co",
        realpath_fn=fake_realpath,
        wsl_detector=lambda: False,
    )
    assert len(result.sandbox.suppressions) == 1
    s = result.sandbox.suppressions[0]
    assert s.entry == "secrets.env"
    assert s.realpath.startswith("/workspaces/")
    assert "escapes sandbox read roots" in s.reason


def test_relative_pure_glob_kept_when_worker_dir_reachable(tmp_path: Path) -> None:
    """Relative pure-glob patterns survive when worker_dir is reachable."""
    worker_dir = str(tmp_path / "wd")
    os.makedirs(worker_dir, exist_ok=True)
    schema = {
        "worker_roles": {
            "demo": _sandbox_role(
                deny_read=["**/credentials*", "*.pem"],
                additional=[],
            ),
        },
    }
    result = generator.render_role_with_metadata(
        schema,
        role="demo",
        worker_dir=worker_dir,
        claude_org_path=str(tmp_path / "co"),
        wsl_detector=lambda: False,
    )
    assert result.sandbox.suppressions == []
    fs = result.settings["sandbox"]["filesystem"]
    assert fs["denyRead"] == ["**/credentials*", "*.pem"]


def test_relative_pure_glob_suppressed_when_worker_dir_escapes() -> None:
    """Relative pure-glob (anchored at worker_dir) escapes -> suppressed."""
    worker_dir = "/home/u/wd"

    def fake_realpath(p: str) -> str:
        if p == worker_dir or p.startswith(worker_dir + "/"):
            return p.replace(worker_dir, "/mnt/c/Users/u/wd", 1)
        return p

    schema = {
        "worker_roles": {
            "demo": _sandbox_role(
                deny_read=["**/credentials*", "*.pem"],
                additional=[],
            ),
        },
    }
    result = generator.render_role_with_metadata(
        schema,
        role="demo",
        worker_dir=worker_dir,
        claude_org_path="/home/u/co",
        realpath_fn=fake_realpath,
        wsl_detector=lambda: True,
    )
    suppressed_entries = {
        (s.layer, s.entry) for s in result.sandbox.suppressions
    }
    assert (
        "sandbox.filesystem.denyRead",
        "**/credentials*",
    ) in suppressed_entries
    assert (
        "sandbox.filesystem.denyRead",
        "*.pem",
    ) in suppressed_entries
    fs = result.settings["sandbox"]["filesystem"]
    assert fs["denyRead"] == []
    # The reason mentions worker_dir, so operators can see why a glob
    # without an anchored prefix was dropped.
    reasons = {s.reason for s in result.sandbox.suppressions}
    assert any("worker_dir" in r for r in reasons)


def test_absolute_pure_glob_kept_unchanged() -> None:
    """Absolute pure-glob patterns (e.g. ``/*``) are kept unchanged."""
    worker_dir = "/home/u/wd"

    def fake_realpath(p: str) -> str:
        if p == worker_dir or p.startswith(worker_dir + "/"):
            return p.replace(worker_dir, "/mnt/c/Users/u/wd", 1)
        return p

    schema = {
        "worker_roles": {
            "demo": _sandbox_role(
                deny_read=["/*"],
                additional=[],
            ),
        },
    }
    result = generator.render_role_with_metadata(
        schema,
        role="demo",
        worker_dir=worker_dir,
        claude_org_path="/home/u/co",
        realpath_fn=fake_realpath,
        wsl_detector=lambda: True,
    )
    assert result.sandbox.suppressions == []
    fs = result.settings["sandbox"]["filesystem"]
    assert fs["denyRead"] == ["/*"]


def test_wsl_marker_detected_from_proc_files(tmp_path: Path) -> None:
    """_detect_wsl reads the kernel marker from the supplied probe files."""
    proc_version = tmp_path / "version"
    osrelease = tmp_path / "osrelease"
    proc_version.write_text("Linux x.y.z (gcc) #1 SMP\n", encoding="utf-8")
    osrelease.write_text("5.15.123-microsoft-standard-WSL2\n", encoding="utf-8")
    assert generator._detect_wsl((str(proc_version), str(osrelease))) is True


def test_wsl_marker_not_present(tmp_path: Path) -> None:
    proc_version = tmp_path / "version"
    osrelease = tmp_path / "osrelease"
    proc_version.write_text("Linux 6.6.0-1-amd64 #1 SMP\n", encoding="utf-8")
    osrelease.write_text("6.6.0-1-amd64\n", encoding="utf-8")
    assert generator._detect_wsl((str(proc_version), str(osrelease))) is False


def test_wsl1_detected_from_microsoft_in_proc_version(tmp_path: Path) -> None:
    """WSL1's /proc/version uses ``Microsoft`` (capital M) without ``WSL``."""
    proc_version = tmp_path / "version"
    osrelease = tmp_path / "osrelease"
    proc_version.write_text(
        "Linux version 4.4.0-19041-Microsoft (Microsoft@Microsoft.com)\n",
        encoding="utf-8",
    )
    osrelease.write_text("4.4.0-19041-Microsoft\n", encoding="utf-8")
    assert generator._detect_wsl((str(proc_version), str(osrelease))) is True


def test_wsl_detected_from_wsl_token_in_proc_version_only(tmp_path: Path) -> None:
    """``WSL`` token in /proc/version is sufficient even if osrelease lacks it."""
    proc_version = tmp_path / "version"
    osrelease = tmp_path / "osrelease"
    proc_version.write_text(
        "Linux version 5.15.0-microsoft-standard-WSL2 (root@host)\n",
        encoding="utf-8",
    )
    osrelease.write_text("6.6.0-1-amd64\n", encoding="utf-8")
    assert generator._detect_wsl((str(proc_version), str(osrelease))) is True


def test_wsl_detected_when_only_proc_version_available(tmp_path: Path) -> None:
    """A missing osrelease file does not block detection from /proc/version."""
    proc_version = tmp_path / "version"
    proc_version.write_text(
        "Linux version 5.15.0-microsoft-standard-WSL2\n", encoding="utf-8"
    )
    missing = tmp_path / "does-not-exist"
    assert generator._detect_wsl((str(proc_version), str(missing))) is True


def test_settings_show_explain_text_includes_suppressions(tmp_path: Path) -> None:
    """`settings show --explain` text output surfaces suppression reasons."""
    worker_dir = str(tmp_path / "wd")
    os.makedirs(worker_dir, exist_ok=True)
    co = str(tmp_path / "co")
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(
        json.dumps(
            {
                "worker_roles": {
                    "demo": _sandbox_role(
                        deny_read=["secrets.env"],
                        additional=[],
                    ),
                }
            }
        ),
        encoding="utf-8",
    )

    parser = runtime_cli.build_parser()
    args = parser.parse_args(
        [
            "settings",
            "show",
            "--role",
            "demo",
            "--worker-dir",
            worker_dir,
            "--claude-org-path",
            co,
            "--schema",
            str(schema_path),
            "--explain",
        ]
    )
    # Force escape via monkeyed realpath would require injecting into the
    # CLI; here we just assert the explain section is present even when
    # there is no suppression.
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = args.func(args)
    assert rc == 0
    out = buf.getvalue()
    assert "suppressions" in out
    assert "wsl_detected" in out
    assert "sandbox.enabled: True" in out
    assert "permissions.deny" in out


def test_settings_show_explain_json_payload(tmp_path: Path) -> None:
    """`settings show --explain --json` emits a structured payload."""
    worker_dir = str(tmp_path / "wd")
    os.makedirs(worker_dir, exist_ok=True)
    co = str(tmp_path / "co")
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(
        json.dumps(
            {
                "worker_roles": {
                    "demo": _sandbox_role(
                        deny_read=["secrets.env"],
                        additional=[],
                    ),
                }
            }
        ),
        encoding="utf-8",
    )
    parser = runtime_cli.build_parser()
    args = parser.parse_args(
        [
            "settings",
            "show",
            "--role",
            "demo",
            "--worker-dir",
            worker_dir,
            "--claude-org-path",
            co,
            "--schema",
            str(schema_path),
            "--explain",
            "--json",
        ]
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = args.func(args)
    assert rc == 0
    payload = json.loads(buf.getvalue())
    assert payload["role"] == "demo"
    assert "settings" in payload and "sandbox" in payload
    assert "suppressions" in payload["sandbox"]
    assert "sandbox_read_roots" in payload["sandbox"]


def test_settings_show_without_explain_omits_metadata(tmp_path: Path) -> None:
    """Bare `settings show --json` does not include suppression metadata."""
    worker_dir = str(tmp_path / "wd")
    os.makedirs(worker_dir, exist_ok=True)
    co = str(tmp_path / "co")
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(
        json.dumps(
            {
                "worker_roles": {
                    "demo": _sandbox_role(deny_read=["secrets.env"]),
                }
            }
        ),
        encoding="utf-8",
    )
    parser = runtime_cli.build_parser()
    args = parser.parse_args(
        [
            "settings",
            "show",
            "--role",
            "demo",
            "--worker-dir",
            worker_dir,
            "--claude-org-path",
            co,
            "--schema",
            str(schema_path),
            "--json",
        ]
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = args.func(args)
    assert rc == 0
    payload = json.loads(buf.getvalue())
    assert "sandbox" not in payload
    assert payload["settings"]["sandbox"]["enabled"] is True


def _make_render_result_with_suppressions() -> generator.RenderResult:
    """Hand-built RenderResult exercising the suppression rendering path."""
    settings = {
        "permissions": {
            "deny": [
                "Bash(git push *)",
                "Read(.env)",
                "Read(**/credentials*)",
            ],
        },
        "sandbox": {
            "enabled": True,
            "filesystem": {
                "denyRead": [],
                "denyWrite": [],
                "additionalDirectories": [],
            },
            "failIfUnavailable": False,
        },
    }
    sandbox_meta = generator.SandboxMetadata(
        enabled=True,
        wsl_detected=True,
        sandbox_read_roots=("/home/u/wd",),
        suppressions=[
            generator.SandboxSuppression(
                layer="sandbox.filesystem.denyRead",
                entry="secrets.env",
                reason="realpath escapes sandbox read roots",
                realpath="/mnt/c/Users/u/wd/secrets.env",
                sandbox_read_roots=("/home/u/wd",),
            ),
            generator.SandboxSuppression(
                layer="sandbox.filesystem.denyWrite",
                entry="*.pem",
                reason=(
                    "worker_dir realpath escapes sandbox read roots "
                    "(anchored relative pattern)"
                ),
                realpath="/mnt/c/Users/u/wd",
                sandbox_read_roots=("/home/u/wd",),
            ),
        ],
    )
    return generator.RenderResult(settings=settings, sandbox=sandbox_meta)


def test_format_show_output_text_includes_suppression_reasons() -> None:
    """Text --explain output renders every suppressed entry's reason."""
    result = _make_render_result_with_suppressions()
    text = generator._format_show_output(
        result, "demo", explain=True, as_json=False,
    )
    assert "wsl_detected: True" in text
    assert "sandbox_read_roots (1):" in text
    assert "  - /home/u/wd" in text
    assert "suppressions (2):" in text
    assert "sandbox.filesystem.denyRead" in text
    assert "secrets.env" in text
    assert "realpath escapes sandbox read roots" in text
    assert "sandbox.filesystem.denyWrite" in text
    assert "*.pem" in text
    assert "worker_dir realpath escapes" in text
    # Layer 2 deny is preserved in the output.
    assert "Read(.env)" in text
    assert "Read(**/credentials*)" in text


def test_format_show_output_json_payload_carries_suppressions() -> None:
    """JSON --explain payload carries structured suppression entries."""
    result = _make_render_result_with_suppressions()
    text = generator._format_show_output(
        result, "demo", explain=True, as_json=True,
    )
    payload = json.loads(text)
    assert payload["role"] == "demo"
    assert payload["sandbox"]["wsl_detected"] is True
    suppressions = payload["sandbox"]["suppressions"]
    assert len(suppressions) == 2
    layers = {s["layer"] for s in suppressions}
    assert layers == {
        "sandbox.filesystem.denyRead",
        "sandbox.filesystem.denyWrite",
    }
    entries = {s["entry"] for s in suppressions}
    assert entries == {"secrets.env", "*.pem"}
    # Each suppression carries the realpath that triggered the escape
    # and the sandbox_read_roots context.
    for s in suppressions:
        assert "realpath" in s and s["realpath"]
        assert s["sandbox_read_roots"] == ["/home/u/wd"]
    # Layer 2 deny is preserved in the rendered settings.
    deny = payload["settings"]["permissions"]["deny"]
    assert "Read(.env)" in deny


def test_format_show_output_text_without_explain_omits_metadata() -> None:
    """Without --explain the text output skips suppression sections."""
    result = _make_render_result_with_suppressions()
    text = generator._format_show_output(
        result, "demo", explain=False, as_json=False,
    )
    assert "wsl_detected" not in text
    assert "suppressions" not in text
    # Settings sections still render.
    assert "permissions.deny" in text
    assert "sandbox.enabled: True" in text


def test_render_role_dict_api_still_returns_dict() -> None:
    """The ``render_role`` shim still returns just the rendered dict."""
    schema = {
        "worker_roles": {
            "demo": _sandbox_role(deny_read=["secrets.env"]),
        },
    }
    out = generator.render_role(
        schema,
        role="demo",
        worker_dir="/tmp/wd",
        claude_org_path="/tmp/co",
    )
    assert isinstance(out, dict)
    assert "sandbox" in out


# ---------------------------------------------------------------------------
# Phase 3 case E §5.2(b): ``$comment`` suppression metadata emission
# (sandbox-launcher-contract.md §2.1 conditionally-required field).
# ---------------------------------------------------------------------------


def test_comment_emitted_with_wsl_platform_when_suppressed() -> None:
    """WSL escape suppression emits ``$comment`` with ``platform=wsl``.

    Mirrors the typical production WSL layout per
    ``phase3-bootstrap-policy-design.md`` §1: ``worker_dir`` lives on
    the Linux side (``/home/<u>/work/wd``) and is NOT a host-cross
    symlink, while only ``~/.aws`` / ``~/.ssh`` resolve into
    ``/mnt/c/...``. Layer 3 ``denyRead`` / ``denyWrite`` entries on
    those home-anchored paths are what need suppression. Per the
    schema's ``worker_roles.$comment_sandbox_anchor``, home-anchored
    entries SHOULD be authored as the structured form
    (``{anchor: 'home', path: '.aws/.env'}``); legacy raw ``~/...``
    strings stay worker_dir-anchored for backward compat.
    """
    worker_dir = "/home/u/work/wd"
    home = "/home/u"

    def fake_realpath(p: str) -> str:
        # Only ~/.aws and ~/.ssh escape to /mnt/c/...; worker_dir is
        # NOT a symlink (faithfully reproduces the WSL fragility).
        if p == f"{home}/.aws" or p.startswith(f"{home}/.aws/"):
            return p.replace(f"{home}/.aws", "/mnt/c/Users/u/.aws", 1)
        if p == f"{home}/.ssh" or p.startswith(f"{home}/.ssh/"):
            return p.replace(f"{home}/.ssh", "/mnt/c/Users/u/.ssh", 1)
        return p

    schema = {
        "worker_roles": {
            "demo": _sandbox_role(
                deny_read=[
                    {
                        "anchor": "home",
                        "path": ".aws/.env",
                        "suppressOnSymlinkEscape": True,
                    },
                    {
                        "anchor": "home",
                        "path": ".ssh/id_rsa",
                        "suppressOnSymlinkEscape": True,
                    },
                ],
                deny_write=[
                    {
                        "anchor": "home",
                        "path": ".aws/credentials",
                        "suppressOnSymlinkEscape": True,
                    },
                ],
                additional=[],
            ),
        },
    }
    # Force HOME so _anchor_base_path("home") resolves to the same
    # /home/u that fake_realpath is rewriting.
    import os as _os

    saved_home = _os.environ.get("HOME")
    _os.environ["HOME"] = home
    try:
        result = generator.render_role_with_metadata(
            schema,
            role="demo",
            worker_dir=worker_dir,
            claude_org_path="/home/u/co",
            realpath_fn=fake_realpath,
            wsl_detector=lambda: True,
        )
    finally:
        if saved_home is None:
            _os.environ.pop("HOME", None)
        else:
            _os.environ["HOME"] = saved_home

    comment = result.settings.get("$comment")
    assert isinstance(comment, str)
    # Fixed prefix per sandbox-launcher-contract.md §2.1 (machine-parseable
    # anchor for the launcher's /sandbox status display).
    assert comment.startswith(
        "platform=wsl, layer-3 entries suppressed: ["
    )
    assert comment.endswith("]")
    # Each suppressed structured entry surfaces as ``home:<path>`` so
    # the launcher can render the operator's authored form.
    assert "home:.aws/.env" in comment
    assert "home:.ssh/id_rsa" in comment
    assert "home:.aws/credentials" in comment
    # Layer 3 was actually emptied (3 → 0 dropped entries).
    fs = result.settings["sandbox"]["filesystem"]
    assert fs["denyRead"] == []
    assert fs["denyWrite"] == []


def test_comment_uses_linux_platform_when_not_wsl() -> None:
    """Non-WSL escape (devcontainer-style) emits ``platform=linux`` comment."""
    worker_dir = "/home/u/wd"

    def fake_realpath(p: str) -> str:
        if p == worker_dir or p.startswith(worker_dir + "/"):
            return p.replace(worker_dir, "/workspaces/repo", 1)
        return p

    schema = {
        "worker_roles": {
            "demo": _sandbox_role(deny_read=["secrets.env"], additional=[]),
        },
    }
    result = generator.render_role_with_metadata(
        schema,
        role="demo",
        worker_dir=worker_dir,
        claude_org_path="/home/u/co",
        realpath_fn=fake_realpath,
        wsl_detector=lambda: False,
    )
    comment = result.settings.get("$comment")
    assert isinstance(comment, str)
    assert comment.startswith(
        "platform=linux, layer-3 entries suppressed: ["
    )
    assert "secrets.env" in comment


def test_comment_absent_when_no_suppressions(tmp_path: Path) -> None:
    """No suppression -> no ``$comment`` field (avoids stale metadata)."""
    worker_dir = str(tmp_path / "wd")
    os.makedirs(worker_dir, exist_ok=True)
    schema = {
        "worker_roles": {
            "demo": _sandbox_role(deny_read=["secrets.env"], additional=[]),
        },
    }
    result = generator.render_role_with_metadata(
        schema,
        role="demo",
        worker_dir=worker_dir,
        claude_org_path=str(tmp_path / "co"),
        wsl_detector=lambda: False,
    )
    assert result.sandbox.suppressions == []
    assert "$comment" not in result.settings


def test_comment_absent_when_sandbox_disabled() -> None:
    """sandbox.enabled=false short-circuits before any suppression / comment."""
    schema = {
        "worker_roles": {
            "demo": _sandbox_role(
                enabled=False,
                deny_read=["/mnt/c/Users/somebody/secrets.env"],
            ),
        },
    }
    result = generator.render_role_with_metadata(
        schema,
        role="demo",
        worker_dir="/home/u/work/wd",
        claude_org_path="/home/u/co",
        wsl_detector=lambda: True,
    )
    assert "$comment" not in result.settings


def test_comment_renders_structured_entry_with_anchor_prefix() -> None:
    """Structured entries surface as ``<anchor>:<path>`` in the comment list."""
    worker_dir = "/home/u/work/wd"

    def fake_realpath(p: str) -> str:
        # ``home`` resolves to a /mnt/c/... escape so the structured entry
        # gets suppressed and the comment list has to render it.
        if p in ("/home/u", "/home/u/"):
            return "/mnt/c/Users/u"
        if p.startswith("/home/u/"):
            return p.replace("/home/u", "/mnt/c/Users/u", 1)
        return p

    structured_entry = {
        "anchor": "home",
        "path": ".aws/.env",
        "suppressOnSymlinkEscape": True,
    }
    abs_entry = {
        "anchor": "absolute",
        "path": "/mnt/c/Users/u/Windows/secret",
        "suppressOnSymlinkEscape": True,
    }
    schema = {
        "worker_roles": {
            "demo": _sandbox_role(
                deny_read=[structured_entry, abs_entry], additional=[],
            ),
        },
    }
    result = generator.render_role_with_metadata(
        schema,
        role="demo",
        worker_dir=worker_dir,
        claude_org_path="/home/u/co",
        realpath_fn=fake_realpath,
        wsl_detector=lambda: True,
    )
    comment = result.settings["$comment"]
    # ``home``-anchored structured entry: rendered as ``home:<path>`` so
    # the operator can disambiguate from a literal worker_dir-anchored
    # entry on the launcher's status display.
    assert "home:.aws/.env" in comment
    # ``absolute`` anchor: the path is already self-explanatory, so the
    # ``absolute:`` prefix is omitted.
    assert "/mnt/c/Users/u/Windows/secret" in comment
    assert "absolute:" not in comment


def test_layer_2_permissions_deny_preserved_alongside_comment() -> None:
    """``permissions.deny`` survives Layer 3 suppression -- Layer 2 invariant."""
    worker_dir = "/home/u/work/wd"

    def fake_realpath(p: str) -> str:
        if p == worker_dir or p.startswith(worker_dir + "/"):
            return p.replace(worker_dir, "/mnt/c/Users/u/work/wd", 1)
        return p

    schema = {
        "worker_roles": {
            "demo": _sandbox_role(deny_read=["secrets.env"], additional=[]),
        },
    }
    result = generator.render_role_with_metadata(
        schema,
        role="demo",
        worker_dir=worker_dir,
        claude_org_path="/home/u/co",
        realpath_fn=fake_realpath,
        wsl_detector=lambda: True,
    )
    deny = result.settings["permissions"]["deny"]
    # Layer 2 deny rules are emitted untouched even when Layer 3 entries
    # are suppressed -- per phase3-bootstrap-policy-design.md §5.2(b).
    assert "Read(.env)" in deny
    assert "Read(**/credentials*)" in deny
    assert "$comment" in result.settings


def test_settings_show_text_surfaces_comment(tmp_path: Path) -> None:
    """Bare (no --explain) text show surfaces the runtime ``$comment`` line."""
    worker_dir = "/home/u/work/wd"

    def fake_realpath(p: str) -> str:
        if p == worker_dir or p.startswith(worker_dir + "/"):
            return p.replace(worker_dir, "/mnt/c/Users/u/work/wd", 1)
        return p

    schema = {
        "worker_roles": {
            "demo": _sandbox_role(deny_read=["secrets.env"], additional=[]),
        },
    }
    result = generator.render_role_with_metadata(
        schema,
        role="demo",
        worker_dir=worker_dir,
        claude_org_path="/home/u/co",
        realpath_fn=fake_realpath,
        wsl_detector=lambda: True,
    )
    text = generator._format_show_output(
        result, "demo", explain=False, as_json=False,
    )
    assert "$comment: platform=wsl, layer-3 entries suppressed: [" in text
    # Without --explain we still avoid the per-entry suppression block.
    assert "suppressions (" not in text


# ---------------------------------------------------------------------------
# Phase 1 (Refs `claude-org-ja#378`): structured anchor + role_kind + Pattern B
# ---------------------------------------------------------------------------


def _structured(
    anchor: str,
    path: str,
    *,
    suppress: bool = True,
) -> dict:
    return {
        "anchor": anchor,
        "path": path,
        "suppressOnSymlinkEscape": suppress,
    }


def test_structured_entry_round_trip_preserved_in_kept_output(tmp_path: Path) -> None:
    """Structured entries that survive suppression round-trip unchanged."""
    worker_dir = str(tmp_path / "wd")
    os.makedirs(worker_dir, exist_ok=True)
    entry = _structured("worker_dir", "secrets.env")
    schema = {
        "worker_roles": {
            "demo": _sandbox_role(deny_read=[entry]),
        },
    }
    result = generator.render_role_with_metadata(
        schema,
        role="demo",
        worker_dir=worker_dir,
        claude_org_path=str(tmp_path / "co"),
        wsl_detector=lambda: False,
    )
    assert result.sandbox.suppressions == []
    assert result.settings["sandbox"]["filesystem"]["denyRead"] == [entry]


def test_legacy_string_and_structured_entry_coexist(tmp_path: Path) -> None:
    """Mixed legacy + structured entries both render correctly."""
    worker_dir = str(tmp_path / "wd")
    os.makedirs(worker_dir, exist_ok=True)
    schema = {
        "worker_roles": {
            "demo": _sandbox_role(
                deny_read=[
                    "secrets.env",
                    _structured("worker_dir", "private.key"),
                ],
            ),
        },
    }
    result = generator.render_role_with_metadata(
        schema,
        role="demo",
        worker_dir=worker_dir,
        claude_org_path=str(tmp_path / "co"),
        wsl_detector=lambda: False,
    )
    assert result.sandbox.suppressions == []
    kept = result.settings["sandbox"]["filesystem"]["denyRead"]
    assert kept[0] == "secrets.env"
    assert kept[1]["anchor"] == "worker_dir"
    assert kept[1]["path"] == "private.key"


def test_home_anchor_realpath_evaluated_against_home() -> None:
    """anchor=home resolves the entry against /home/<user>, not worker_dir."""
    worker_dir = "/home/u/work/wd"
    home = os.path.expanduser("~")

    captured: list[str] = []

    def fake_realpath(p: str) -> str:
        captured.append(p)
        return p

    schema = {
        "worker_roles": {
            "demo": _sandbox_role(
                deny_read=[_structured("home", ".aws/credentials")],
                additional=[],
            ),
        },
    }
    result = generator.render_role_with_metadata(
        schema,
        role="demo",
        worker_dir=worker_dir,
        claude_org_path="/home/u/co",
        realpath_fn=fake_realpath,
        wsl_detector=lambda: True,
    )
    # /home/u (or whatever home is) is not in read_roots -> suppressed.
    assert len(result.sandbox.suppressions) == 1
    suppression = result.sandbox.suppressions[0]
    expected_target = os.path.join(home, ".aws/credentials")
    assert expected_target in captured
    assert suppression.realpath == expected_target
    # Layer 2 untouched.
    assert "Read(.env)" in result.settings["permissions"]["deny"]


def test_home_anchor_kept_when_home_is_in_additional_directories() -> None:
    """anchor=home is reachable when /home/<user> is a sandbox read root."""
    worker_dir = "/home/u/work/wd"
    home = os.path.expanduser("~")
    schema = {
        "worker_roles": {
            "demo": _sandbox_role(
                deny_read=[_structured("home", ".aws/credentials")],
                additional=[home],
            ),
        },
    }
    result = generator.render_role_with_metadata(
        schema,
        role="demo",
        worker_dir=worker_dir,
        claude_org_path="/home/u/co",
        realpath_fn=lambda p: p,
        wsl_detector=lambda: False,
    )
    assert result.sandbox.suppressions == []


def test_absolute_anchor_literal_path_round_trip() -> None:
    """anchor=absolute treats the path literally, no anchor base join."""
    worker_dir = "/home/u/wd"
    schema = {
        "worker_roles": {
            "demo": _sandbox_role(
                deny_read=[_structured("absolute", "/etc/shadow")],
                additional=["/etc"],
            ),
        },
    }
    result = generator.render_role_with_metadata(
        schema,
        role="demo",
        worker_dir=worker_dir,
        claude_org_path="/home/u/co",
        realpath_fn=lambda p: p,
        wsl_detector=lambda: False,
    )
    # /etc/shadow is inside additionalDirectories=/etc -> kept.
    assert result.sandbox.suppressions == []
    assert (
        result.settings["sandbox"]["filesystem"]["denyRead"][0]["path"]
        == "/etc/shadow"
    )


def test_claude_org_path_anchor_resolves_against_claude_org_path() -> None:
    """anchor=claude_org_path uses ctx.claude_org_path as the base."""
    worker_dir = "/home/u/wd"
    co = "/home/u/claude-org"
    schema = {
        "worker_roles": {
            "demo": _sandbox_role(
                deny_read=[_structured("claude_org_path", "secrets/api.key")],
                additional=[co],
            ),
        },
    }
    result = generator.render_role_with_metadata(
        schema,
        role="demo",
        worker_dir=worker_dir,
        claude_org_path=co,
        realpath_fn=lambda p: p,
        wsl_detector=lambda: False,
    )
    # claude_org_path is in additionalDirectories -> kept.
    assert result.sandbox.suppressions == []


def test_suppress_on_symlink_escape_false_keeps_entry() -> None:
    """suppressOnSymlinkEscape=false keeps the entry even when realpath escapes."""
    worker_dir = "/home/u/work/wd"

    def fake_realpath(p: str) -> str:
        if p == worker_dir or p.startswith(worker_dir + "/"):
            return p.replace(worker_dir, "/mnt/c/Users/u/work/wd", 1)
        return p

    schema = {
        "worker_roles": {
            "demo": _sandbox_role(
                deny_read=[
                    _structured("worker_dir", "secrets.env", suppress=False),
                ],
            ),
        },
    }
    result = generator.render_role_with_metadata(
        schema,
        role="demo",
        worker_dir=worker_dir,
        claude_org_path="/home/u/co",
        realpath_fn=fake_realpath,
        wsl_detector=lambda: True,
    )
    assert result.sandbox.suppressions == []
    kept = result.settings["sandbox"]["filesystem"]["denyRead"][0]
    assert kept["suppressOnSymlinkEscape"] is False


def test_pattern_b_substitution_in_entry_path_and_additional_directories() -> None:
    """Pattern B placeholders are substituted in entry paths + additionalDirectories."""
    worker_dir = "/home/u/work/wd"
    base_clone = "/home/u/base"
    schema = {
        "worker_roles": {
            "demo": _sandbox_role(
                deny_read=[
                    _structured("absolute", "{base_clone}/.git/config"),
                ],
                additional=["{base_clone}/.git"],
            ),
        },
    }
    result = generator.render_role_with_metadata(
        schema,
        role="demo",
        worker_dir=worker_dir,
        claude_org_path="/home/u/co",
        base_clone=base_clone,
        task_id="demo-task",
        branch_ref="feat/x",
        pattern="B",
        realpath_fn=lambda p: p,
        wsl_detector=lambda: False,
    )
    fs = result.settings["sandbox"]["filesystem"]
    # additionalDirectories were substituted.
    assert fs["additionalDirectories"] == [f"{base_clone}/.git"]
    # The rendered entry carries the substituted path -- the bwrap
    # launcher consumes the rendered settings.local.json directly so
    # concrete paths (not templates) must appear in the output.
    assert fs["denyRead"][0]["path"] == f"{base_clone}/.git/config"
    # Reachability evaluation used the substituted path, so the entry
    # is kept (it is inside the substituted additionalDirectory).
    assert result.sandbox.suppressions == []


def test_pattern_b_placeholders_in_legacy_string_entries() -> None:
    """Legacy string entries also see Pattern B substitution before realpath."""
    worker_dir = "/home/u/work/wd"
    base_clone = "/home/u/base"

    def fake_realpath(p: str) -> str:
        return p

    schema = {
        "worker_roles": {
            "demo": _sandbox_role(
                deny_read=["{base_clone}/.git/HEAD"],
                additional=[],
            ),
        },
    }
    result = generator.render_role_with_metadata(
        schema,
        role="demo",
        worker_dir=worker_dir,
        claude_org_path="/home/u/co",
        base_clone=base_clone,
        realpath_fn=fake_realpath,
        wsl_detector=lambda: False,
    )
    # `{base_clone}/...` resolves outside worker_dir read root -> suppressed.
    assert len(result.sandbox.suppressions) == 1
    s = result.sandbox.suppressions[0]
    assert s.realpath == f"{base_clone}/.git/HEAD"


def test_render_org_role_with_role_kind_org() -> None:
    """role_kind='org' looks up the role under schema['roles']."""
    schema = {
        "roles": {
            "secretary": {
                "description": "Secretary",
                "settings_paths": [".claude/settings.local.json"],
                "sandbox": {
                    "enabled": True,
                    "filesystem": {
                        "denyRead": [_structured("home", ".ssh/id_rsa")],
                        "denyWrite": [],
                        "additionalDirectories": [],
                    },
                    "failIfUnavailable": False,
                },
            },
            "$comment_irrelevant": "ignored",
        },
        "worker_roles": {},
    }
    result = generator.render_role_with_metadata(
        schema,
        role="secretary",
        role_kind="org",
        worker_dir="/home/u/wd",
        claude_org_path="/home/u/co",
        realpath_fn=lambda p: p,
        wsl_detector=lambda: False,
    )
    # description is dropped (metadata).
    assert "description" not in result.settings
    # settings_paths is preserved (the renderer doesn't filter org-role
    # specific fields).
    assert result.settings["settings_paths"] == [
        ".claude/settings.local.json"
    ]
    # Sandbox suppression ran: home anchor is outside worker_dir.
    assert len(result.sandbox.suppressions) == 1
    assert result.sandbox.suppressions[0].layer == "sandbox.filesystem.denyRead"


def test_render_org_role_unknown_role_raises() -> None:
    """role_kind='org' for an unknown role surfaces an org-role-flavored error."""
    schema = {"roles": {"secretary": {}}, "worker_roles": {}}
    with pytest.raises(KeyError) as info:
        generator.render_role_with_metadata(
            schema,
            role="nope",
            role_kind="org",
            worker_dir="/wd",
            claude_org_path="/co",
        )
    assert "unknown org role" in info.value.args[0]


def test_render_role_kind_invalid_raises_valueerror() -> None:
    """An unknown role_kind is rejected up-front."""
    schema = {"worker_roles": {"demo": {}}}
    with pytest.raises(ValueError) as info:
        generator.render_role_with_metadata(
            schema,
            role="demo",
            role_kind="bogus",
            worker_dir="/wd",
            claude_org_path="/co",
        )
    assert "unknown role_kind" in str(info.value)


def test_unknown_anchor_in_structured_entry_passes_through(tmp_path: Path) -> None:
    """A structured entry with an invalid anchor is kept untouched."""
    worker_dir = str(tmp_path / "wd")
    os.makedirs(worker_dir, exist_ok=True)
    weird = {"anchor": "moon", "path": "x", "suppressOnSymlinkEscape": True}
    schema = {
        "worker_roles": {
            "demo": _sandbox_role(deny_read=[weird]),
        },
    }
    result = generator.render_role_with_metadata(
        schema,
        role="demo",
        worker_dir=worker_dir,
        claude_org_path=str(tmp_path / "co"),
        wsl_detector=lambda: False,
    )
    assert result.sandbox.suppressions == []
    assert result.settings["sandbox"]["filesystem"]["denyRead"] == [weird]


def test_normalize_sandbox_entry_legacy_absolute_string() -> None:
    """Legacy absolute string -> anchor=absolute, suppress=True."""
    norm = generator._normalize_sandbox_entry("/etc/shadow")
    assert norm is not None
    assert norm.anchor == "absolute"
    assert norm.path == "/etc/shadow"
    assert norm.suppress_on_symlink_escape is True


def test_normalize_sandbox_entry_legacy_relative_string() -> None:
    """Legacy relative string -> anchor=worker_dir, suppress=True."""
    norm = generator._normalize_sandbox_entry("secrets.env")
    assert norm is not None
    assert norm.anchor == "worker_dir"
    assert norm.path == "secrets.env"
    assert norm.suppress_on_symlink_escape is True


def test_normalize_sandbox_entry_structured_defaults_suppress_true() -> None:
    """Structured entry without suppressOnSymlinkEscape defaults to True."""
    norm = generator._normalize_sandbox_entry(
        {"anchor": "home", "path": ".aws/credentials"}
    )
    assert norm is not None
    assert norm.anchor == "home"
    assert norm.suppress_on_symlink_escape is True


def test_format_show_output_text_handles_structured_entry() -> None:
    """Text rendering of a structured entry produces a readable line."""
    settings = {
        "permissions": {"deny": []},
        "sandbox": {
            "enabled": True,
            "filesystem": {
                "denyRead": [_structured("home", ".aws/credentials")],
                "denyWrite": [],
                "additionalDirectories": [],
            },
            "failIfUnavailable": False,
        },
    }
    result = generator.RenderResult(
        settings=settings, sandbox=generator.SandboxMetadata(),
    )
    text = generator._format_show_output(
        result, "demo", explain=False, as_json=False,
    )
    assert "sandbox.filesystem.denyRead (1):" in text
    # The structured entry is rendered via its repr / dict form.
    assert "anchor" in text
    assert ".aws/credentials" in text


def test_render_role_legacy_signature_unchanged(tmp_path: Path) -> None:
    """The pre-Phase-1 positional signature still works."""
    schema = {
        "worker_roles": {
            "demo": _sandbox_role(deny_read=["secrets.env"]),
        },
    }
    out = generator.render_role(
        schema, "demo", str(tmp_path / "wd"), str(tmp_path / "co"),
    )
    assert isinstance(out, dict)


# ---------------------------------------------------------------------------
# Phase 1 round-1 Codex fixes: strict bool, malformed absolute,
# additionalDirectories preservation, CLI plumbing.
# ---------------------------------------------------------------------------


def test_suppress_on_symlink_escape_non_bool_passes_through(tmp_path: Path) -> None:
    """A structured entry whose suppressOnSymlinkEscape is not a bool is kept-as-is.

    ``bool('false') == True`` would silently flip the operator's
    intent; the safer behaviour is to surface the malformed entry by
    leaving it in the rendered output untouched.
    """
    worker_dir = "/home/u/work/wd"

    def fake_realpath(p: str) -> str:
        if p == worker_dir or p.startswith(worker_dir + "/"):
            return p.replace(worker_dir, "/mnt/c/Users/u/work/wd", 1)
        return p

    bad = {
        "anchor": "worker_dir",
        "path": "secrets.env",
        "suppressOnSymlinkEscape": "false",
    }
    schema = {
        "worker_roles": {
            "demo": _sandbox_role(deny_read=[bad]),
        },
    }
    result = generator.render_role_with_metadata(
        schema,
        role="demo",
        worker_dir=worker_dir,
        claude_org_path="/home/u/co",
        realpath_fn=fake_realpath,
        wsl_detector=lambda: True,
    )
    assert result.sandbox.suppressions == []
    assert result.settings["sandbox"]["filesystem"]["denyRead"] == [bad]


def test_normalize_sandbox_entry_non_bool_suppress_returns_none() -> None:
    """``_normalize_sandbox_entry`` rejects non-bool suppress flags."""
    assert (
        generator._normalize_sandbox_entry(
            {
                "anchor": "worker_dir",
                "path": "x",
                "suppressOnSymlinkEscape": "true",
            }
        )
        is None
    )
    assert (
        generator._normalize_sandbox_entry(
            {
                "anchor": "worker_dir",
                "path": "x",
                "suppressOnSymlinkEscape": 1,
            }
        )
        is None
    )


def test_absolute_anchor_with_relative_path_kept_as_is() -> None:
    """anchor=absolute with a relative path is malformed -> keep-as-is.

    Resolving the relative path against ``CWD`` would produce
    surprising suppressions; the launcher / drift CI is the right
    place to surface the operator error.
    """
    worker_dir = "/home/u/wd"
    bad = {
        "anchor": "absolute",
        "path": "etc/shadow",  # missing leading "/"
        "suppressOnSymlinkEscape": True,
    }
    schema = {
        "worker_roles": {
            "demo": _sandbox_role(deny_read=[bad], additional=[]),
        },
    }
    result = generator.render_role_with_metadata(
        schema,
        role="demo",
        worker_dir=worker_dir,
        claude_org_path="/home/u/co",
        realpath_fn=lambda p: p,
        wsl_detector=lambda: False,
    )
    assert result.sandbox.suppressions == []
    assert result.settings["sandbox"]["filesystem"]["denyRead"] == [bad]


def test_additional_directories_absent_stays_absent(tmp_path: Path) -> None:
    """When the original sandbox lacks additionalDirectories, render keeps it absent."""
    worker_dir = str(tmp_path / "wd")
    os.makedirs(worker_dir, exist_ok=True)
    schema = {
        "worker_roles": {
            "demo": {
                "permissions": {"deny": []},
                "sandbox": {
                    "enabled": True,
                    "filesystem": {
                        "denyRead": ["secrets.env"],
                        "denyWrite": [],
                    },
                    "failIfUnavailable": False,
                },
            },
        },
    }
    result = generator.render_role_with_metadata(
        schema,
        role="demo",
        worker_dir=worker_dir,
        claude_org_path=str(tmp_path / "co"),
        wsl_detector=lambda: False,
    )
    fs = result.settings["sandbox"]["filesystem"]
    assert "additionalDirectories" not in fs
    assert fs["denyRead"] == ["secrets.env"]


def test_cli_generate_role_kind_org_rejected(tmp_path: Path, capsys) -> None:
    """`settings generate --role-kind org` is rejected with a helpful message.

    Org settings.local.json files (secretary / dispatcher / curator)
    are hand-maintained; the org-side `roles[*]` schema entries are
    audit constraints, not a renderable settings template. Use
    `settings show --role-kind org` for inspection (sandbox
    suppression, etc.).
    """
    rc = generator.main(
        [
            "--role",
            "secretary",
            "--role-kind",
            "org",
            "--worker-dir",
            str(tmp_path / "wd"),
            "--claude-org-path",
            str(tmp_path / "co"),
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "settings generate" in err
    assert "--role-kind org" in err
    assert "settings show" in err


def test_cli_pattern_b_context_substitutes_placeholders(tmp_path: Path) -> None:
    """`settings generate --base-clone ...` substitutes Pattern B placeholders."""
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(
        json.dumps(
            {
                "worker_roles": {
                    "demo": {
                        "permissions": {
                            "allow": ["Bash(test {base_clone})"],
                        },
                        "env": {
                            "BASE": "{base_clone}",
                            "TASK": "{task_id}",
                            "BRANCH": "{branch_ref}",
                        },
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    out = tmp_path / "out.json"
    rc = generator.main(
        [
            "--role",
            "demo",
            "--worker-dir",
            "/wd",
            "--claude-org-path",
            "/co",
            "--base-clone",
            "/tmp/base",
            "--task-id",
            "task-123",
            "--branch-ref",
            "feat/x",
            "--pattern",
            "B",
            "--schema",
            str(schema_path),
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    parsed = json.loads(out.read_text(encoding="utf-8"))
    assert parsed["permissions"]["allow"][0] == "Bash(test /tmp/base)"
    assert parsed["env"] == {
        "BASE": "/tmp/base",
        "TASK": "task-123",
        "BRANCH": "feat/x",
    }


def test_cli_role_kind_invalid_argparse_rejects(tmp_path: Path) -> None:
    """`--role-kind bogus` is rejected by argparse (non-zero exit)."""
    with pytest.raises(SystemExit) as info:
        generator.main(
            [
                "--role",
                "demo",
                "--role-kind",
                "bogus",
                "--worker-dir",
                "/wd",
                "--claude-org-path",
                "/co",
            ]
        )
    assert info.value.code != 0


def test_cli_show_unknown_org_role_returns_2(tmp_path: Path) -> None:
    """`settings show --role-kind org` with an unknown role returns rc=2."""
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(
        json.dumps({"roles": {"secretary": {}}, "worker_roles": {}}),
        encoding="utf-8",
    )
    parser = runtime_cli.build_parser()
    args = parser.parse_args(
        [
            "settings",
            "show",
            "--role",
            "nope",
            "--role-kind",
            "org",
            "--worker-dir",
            "/wd",
            "--claude-org-path",
            "/co",
            "--schema",
            str(schema_path),
        ]
    )
    rc = args.func(args)
    assert rc == 2


def test_cli_show_role_kind_org_with_explain(tmp_path: Path) -> None:
    """`settings show --role-kind org --explain` surfaces sandbox suppression."""
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(
        json.dumps(
            {
                "roles": {
                    "secretary": {
                        "sandbox": {
                            "enabled": True,
                            "filesystem": {
                                "denyRead": [
                                    {
                                        "anchor": "absolute",
                                        "path": "/etc/shadow",
                                        "suppressOnSymlinkEscape": True,
                                    }
                                ],
                                "denyWrite": [],
                                "additionalDirectories": [],
                            },
                            "failIfUnavailable": False,
                        }
                    }
                },
                "worker_roles": {},
            }
        ),
        encoding="utf-8",
    )
    parser = runtime_cli.build_parser()
    args = parser.parse_args(
        [
            "settings",
            "show",
            "--role",
            "secretary",
            "--role-kind",
            "org",
            "--worker-dir",
            str(tmp_path / "wd"),
            "--claude-org-path",
            str(tmp_path / "co"),
            "--schema",
            str(schema_path),
            "--explain",
            "--json",
        ]
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = args.func(args)
    assert rc == 0
    payload = json.loads(buf.getvalue())
    assert payload["role"] == "secretary"
    # /etc/shadow escapes the worker_dir read root -> suppressed.
    suppressions = payload["sandbox"]["suppressions"]
    assert len(suppressions) == 1
    assert suppressions[0]["entry"]["path"] == "/etc/shadow"


# ---------------------------------------------------------------------------
# Phase 1 (Refs `claude-org-runtime#13`): sandbox_by_pattern + base_clone anchor
# ---------------------------------------------------------------------------


def _pattern_sandbox(
    *,
    deny_read: list | None = None,
    deny_write: list | None = None,
    additional: list[str] | None = None,
) -> dict:
    """Compact builder for a single sandbox_by_pattern entry."""
    return {
        "enabled": True,
        "filesystem": {
            "denyRead": list(deny_read or []),
            "denyWrite": list(deny_write or []),
            "additionalDirectories": list(additional or []),
        },
        "failIfUnavailable": False,
    }


def _pattern_role(*, sandbox_by_pattern: dict, sandbox: dict | None = None) -> dict:
    """Compact builder for a worker role exercising sandbox_by_pattern."""
    body: dict = {
        "permissions": {
            "deny": [
                "Bash(git push *)",
                "Read(.env)",
            ],
        },
        "sandbox_by_pattern": sandbox_by_pattern,
    }
    if sandbox is not None:
        body["sandbox"] = sandbox
    return body


def test_pattern_a_renders_pattern_a_sandbox(tmp_path: Path) -> None:
    """--pattern A selects sandbox_by_pattern.A as the rendered sandbox."""
    worker_dir = str(tmp_path / "wd")
    os.makedirs(worker_dir, exist_ok=True)
    schema = {
        "worker_roles": {
            "demo": _pattern_role(
                sandbox_by_pattern={
                    "A": _pattern_sandbox(
                        deny_read=["secrets.env"],
                        additional=["{worker_dir}"],
                    ),
                    "B": _pattern_sandbox(
                        deny_read=[
                            _structured("base_clone", ".git/config"),
                        ],
                        additional=[
                            "{worker_dir}",
                            "{base_clone}/.git/worktrees/{task_id}",
                        ],
                    ),
                },
            ),
        },
    }
    result = generator.render_role_with_metadata(
        schema,
        role="demo",
        worker_dir=worker_dir,
        claude_org_path=str(tmp_path / "co"),
        pattern="A",
        wsl_detector=lambda: False,
    )
    fs = result.settings["sandbox"]["filesystem"]
    assert fs["denyRead"] == ["secrets.env"]
    # additionalDirectories was substituted with the concrete worker_dir;
    # base_clone-flavored entries from sandbox_by_pattern.B did NOT leak in.
    assert fs["additionalDirectories"] == [worker_dir]
    # sandbox_by_pattern itself never appears in the rendered output.
    assert "sandbox_by_pattern" not in result.settings


def test_pattern_b_renders_pattern_b_sandbox_with_base_clone() -> None:
    """--pattern B selects sandbox_by_pattern.B and resolves base_clone anchors."""
    worker_dir = "/home/u/work/proj/.worktrees/task-42"
    base_clone = "/home/u/work/proj"
    schema = {
        "worker_roles": {
            "demo": _pattern_role(
                sandbox_by_pattern={
                    "A": _pattern_sandbox(
                        additional=["{worker_dir}"],
                    ),
                    "B": _pattern_sandbox(
                        deny_read=[
                            _structured("base_clone", ".git/HEAD"),
                        ],
                        deny_write=[
                            _structured("base_clone", ".git/config"),
                        ],
                        additional=[
                            "{worker_dir}",
                            "{base_clone}/.git/worktrees/{task_id}",
                            "{base_clone}/.git/objects",
                        ],
                    ),
                },
            ),
        },
    }
    result = generator.render_role_with_metadata(
        schema,
        role="demo",
        worker_dir=worker_dir,
        claude_org_path="/home/u/co",
        base_clone=base_clone,
        task_id="task-42",
        branch_ref="feat/x",
        pattern="B",
        realpath_fn=lambda p: p,
        wsl_detector=lambda: False,
    )
    fs = result.settings["sandbox"]["filesystem"]
    # additionalDirectories was substituted with concrete paths.
    assert fs["additionalDirectories"] == [
        worker_dir,
        f"{base_clone}/.git/worktrees/task-42",
        f"{base_clone}/.git/objects",
    ]
    # base_clone anchor entries survive when their realpath is inside one
    # of the additionalDirectories (here .git/HEAD is not inside .git/objects
    # nor .git/worktrees/task-42 -> suppressed).
    assert any(
        s.layer == "sandbox.filesystem.denyRead"
        and isinstance(s.entry, dict)
        and s.entry["anchor"] == "base_clone"
        and s.entry["path"] == ".git/HEAD"
        for s in result.sandbox.suppressions
    )


def test_pattern_c_renders_pattern_c_sandbox(tmp_path: Path) -> None:
    """--pattern C selects sandbox_by_pattern.C surface (ephemeral)."""
    worker_dir = str(tmp_path / "ephemeral-task")
    os.makedirs(worker_dir, exist_ok=True)
    schema = {
        "worker_roles": {
            "demo": _pattern_role(
                sandbox_by_pattern={
                    "A": _pattern_sandbox(additional=["{worker_dir}"]),
                    "C": _pattern_sandbox(
                        deny_read=["secrets.env"],
                        additional=["{worker_dir}"],
                    ),
                },
            ),
        },
    }
    result = generator.render_role_with_metadata(
        schema,
        role="demo",
        worker_dir=worker_dir,
        claude_org_path=str(tmp_path / "co"),
        pattern="C",
        wsl_detector=lambda: False,
    )
    fs = result.settings["sandbox"]["filesystem"]
    assert fs["denyRead"] == ["secrets.env"]
    assert fs["additionalDirectories"] == [worker_dir]


def test_sandbox_by_pattern_requires_pattern() -> None:
    """A worker role with sandbox_by_pattern errors when --pattern is missing."""
    schema = {
        "worker_roles": {
            "demo": _pattern_role(
                sandbox_by_pattern={"A": _pattern_sandbox()},
            ),
        },
    }
    with pytest.raises(ValueError) as info:
        generator.render_role_with_metadata(
            schema,
            role="demo",
            worker_dir="/wd",
            claude_org_path="/co",
        )
    msg = str(info.value)
    assert "sandbox_by_pattern" in msg
    assert "--pattern" in msg


def test_sandbox_by_pattern_unknown_pattern_key_rejected() -> None:
    """Unknown pattern keys (e.g. 'D') in sandbox_by_pattern are rejected."""
    schema = {
        "worker_roles": {
            "demo": _pattern_role(
                sandbox_by_pattern={
                    "A": _pattern_sandbox(),
                    "D": _pattern_sandbox(),
                },
            ),
        },
    }
    with pytest.raises(ValueError) as info:
        generator.render_role_with_metadata(
            schema,
            role="demo",
            worker_dir="/wd",
            claude_org_path="/co",
            pattern="A",
        )
    assert "unknown pattern keys" in str(info.value)


def test_sandbox_by_pattern_missing_selected_pattern_rejected() -> None:
    """Selecting a pattern not defined on the role surfaces an error."""
    schema = {
        "worker_roles": {
            "demo": _pattern_role(
                sandbox_by_pattern={"A": _pattern_sandbox()},
            ),
        },
    }
    with pytest.raises(ValueError) as info:
        generator.render_role_with_metadata(
            schema,
            role="demo",
            worker_dir="/wd",
            claude_org_path="/co",
            pattern="B",
        )
    msg = str(info.value)
    assert "no entry for pattern 'B'" in msg
    assert "['A']" in msg


def test_worker_role_sandbox_and_sandbox_by_pattern_mutually_exclusive() -> None:
    """worker_roles[<role>] cannot declare both 'sandbox' and 'sandbox_by_pattern'."""
    schema = {
        "worker_roles": {
            "demo": _pattern_role(
                sandbox_by_pattern={"A": _pattern_sandbox()},
                sandbox=_pattern_sandbox(deny_read=["legacy.env"]),
            ),
        },
    }
    with pytest.raises(ValueError) as info:
        generator.render_role_with_metadata(
            schema,
            role="demo",
            worker_dir="/wd",
            claude_org_path="/co",
            pattern="A",
        )
    msg = str(info.value)
    assert "mutually exclusive" in msg
    assert "sandbox_by_pattern" in msg


def test_org_role_sandbox_by_pattern_rejected() -> None:
    """Org roles (roles[*]) may not declare sandbox_by_pattern."""
    schema = {
        "roles": {
            "secretary": {
                "sandbox_by_pattern": {"A": _pattern_sandbox()},
            }
        },
        "worker_roles": {},
    }
    with pytest.raises(ValueError) as info:
        generator.render_role_with_metadata(
            schema,
            role="secretary",
            role_kind="org",
            worker_dir="/wd",
            claude_org_path="/co",
            pattern="A",
        )
    msg = str(info.value)
    assert "reserved for worker roles" in msg


def test_sandbox_by_pattern_must_be_dict() -> None:
    """sandbox_by_pattern: <non-dict> is rejected up-front."""
    schema = {
        "worker_roles": {
            "demo": {
                "permissions": {"deny": []},
                "sandbox_by_pattern": ["A", "B"],
            }
        }
    }
    with pytest.raises(ValueError) as info:
        generator.render_role_with_metadata(
            schema,
            role="demo",
            worker_dir="/wd",
            claude_org_path="/co",
            pattern="A",
        )
    assert "must be a dict" in str(info.value)


def test_sandbox_by_pattern_null_value_rejected() -> None:
    """``sandbox_by_pattern: null`` (key present, value None) is rejected.

    Key-presence (not value-truthiness) drives the routing so a worker
    role declaring ``{ sandbox: ..., sandbox_by_pattern: null }`` does
    not silently fall through to the legacy single-``sandbox`` path
    (which would smuggle in the wrong Pattern A/B/C surface).
    """
    schema = {
        "worker_roles": {
            "demo": {
                "permissions": {"deny": []},
                "sandbox_by_pattern": None,
                "sandbox": _pattern_sandbox(deny_read=["legacy.env"]),
            }
        }
    }
    with pytest.raises(ValueError) as info:
        generator.render_role_with_metadata(
            schema,
            role="demo",
            worker_dir="/wd",
            claude_org_path="/co",
            pattern="A",
        )
    msg = str(info.value)
    # Mutual-exclusivity catches this ahead of the dict-shape check.
    assert "mutually exclusive" in msg


def test_sandbox_by_pattern_null_value_alone_rejected() -> None:
    """``sandbox_by_pattern: null`` without sandbox still fails (not a dict)."""
    schema = {
        "worker_roles": {
            "demo": {
                "permissions": {"deny": []},
                "sandbox_by_pattern": None,
            }
        }
    }
    with pytest.raises(ValueError) as info:
        generator.render_role_with_metadata(
            schema,
            role="demo",
            worker_dir="/wd",
            claude_org_path="/co",
            pattern="A",
        )
    assert "must be a dict" in str(info.value)


def test_pattern_b_placeholder_without_base_clone_rejected() -> None:
    """Pattern B sandbox referencing {base_clone} without --base-clone errors out.

    Without the matching context, ``_substitute`` would leave a literal
    ``{base_clone}`` in the rendered sandbox; the bwrap launcher
    consumes ``sandbox.filesystem.additionalDirectories`` as concrete
    paths, so the misconfiguration must be caught at render time.
    """
    schema = {
        "worker_roles": {
            "demo": _pattern_role(
                sandbox_by_pattern={
                    "B": _pattern_sandbox(
                        additional=[
                            "{worker_dir}",
                            "{base_clone}/.git/worktrees/{task_id}",
                        ],
                    ),
                },
            ),
        },
    }
    with pytest.raises(ValueError) as info:
        generator.render_role_with_metadata(
            schema,
            role="demo",
            worker_dir="/wd",
            claude_org_path="/co",
            pattern="B",
        )
    msg = str(info.value)
    assert "{base_clone}" in msg
    assert "--base-clone" in msg
    assert "--pattern B" in msg


def test_pattern_b_placeholder_without_task_id_rejected() -> None:
    """{task_id} without --task-id is also caught (independent of base_clone)."""
    schema = {
        "worker_roles": {
            "demo": _pattern_role(
                sandbox_by_pattern={
                    "B": _pattern_sandbox(
                        additional=[
                            "{worker_dir}",
                            "{base_clone}/.git/worktrees/{task_id}",
                        ],
                    ),
                },
            ),
        },
    }
    with pytest.raises(ValueError) as info:
        generator.render_role_with_metadata(
            schema,
            role="demo",
            worker_dir="/wd",
            claude_org_path="/co",
            base_clone="/home/u/proj",  # task_id intentionally omitted
            pattern="B",
        )
    msg = str(info.value)
    # base_clone resolved, but task_id is left dangling.
    assert "{task_id}" in msg
    assert "--task-id" in msg


def test_pattern_b_placeholder_in_deny_read_string_rejected() -> None:
    """An unresolved {base_clone} on a legacy-string deny entry is also caught."""
    schema = {
        "worker_roles": {
            "demo": _pattern_role(
                sandbox_by_pattern={
                    "B": _pattern_sandbox(
                        deny_read=["{base_clone}/.git/HEAD"],
                        additional=["{worker_dir}"],
                    ),
                },
            ),
        },
    }
    with pytest.raises(ValueError) as info:
        generator.render_role_with_metadata(
            schema,
            role="demo",
            worker_dir="/wd",
            claude_org_path="/co",
            pattern="B",
        )
    assert "{base_clone}" in str(info.value)


def test_org_role_sandbox_by_pattern_null_rejected() -> None:
    """``sandbox_by_pattern: null`` on an org role is also misconfiguration."""
    schema = {
        "roles": {
            "secretary": {"sandbox_by_pattern": None},
        },
        "worker_roles": {},
    }
    with pytest.raises(ValueError) as info:
        generator.render_role_with_metadata(
            schema,
            role="secretary",
            role_kind="org",
            worker_dir="/wd",
            claude_org_path="/co",
        )
    assert "reserved for worker roles" in str(info.value)


def test_legacy_sandbox_unaffected_by_pattern_flag(tmp_path: Path) -> None:
    """Roles using the legacy single 'sandbox' ignore --pattern."""
    worker_dir = str(tmp_path / "wd")
    os.makedirs(worker_dir, exist_ok=True)
    schema = {
        "worker_roles": {
            "demo": _sandbox_role(deny_read=["secrets.env"]),
        }
    }
    result = generator.render_role_with_metadata(
        schema,
        role="demo",
        worker_dir=worker_dir,
        claude_org_path=str(tmp_path / "co"),
        pattern="B",
        wsl_detector=lambda: False,
    )
    # Pre-Phase-1 behavior preserved: --pattern is informational and the
    # legacy single sandbox renders unchanged.
    assert result.settings["sandbox"]["filesystem"]["denyRead"] == [
        "secrets.env"
    ]


def test_base_clone_anchor_resolves_to_ctx_base_clone() -> None:
    """anchor='base_clone' joins entry.path against ctx.base_clone."""
    worker_dir = "/home/u/work/proj/.worktrees/task-1"
    base_clone = "/home/u/work/proj"
    captured: list[str] = []

    def fake_realpath(p: str) -> str:
        captured.append(p)
        return p

    schema = {
        "worker_roles": {
            "demo": _sandbox_role(
                deny_read=[_structured("base_clone", ".git/config")],
                additional=[],
            ),
        },
    }
    result = generator.render_role_with_metadata(
        schema,
        role="demo",
        worker_dir=worker_dir,
        claude_org_path="/home/u/co",
        base_clone=base_clone,
        realpath_fn=fake_realpath,
        wsl_detector=lambda: False,
    )
    # The anchor base path (base_clone) is realpath'd, then .git/config
    # is joined onto it for the reachability check. ``os.path.join`` is
    # platform-aware so the joined path uses backslashes on Windows --
    # match that here instead of hard-coding a POSIX separator (the
    # existing home-anchor test uses the same pattern).
    expected_joined = os.path.join(base_clone, ".git/config")
    assert any(p == base_clone for p in captured)
    assert any(p == expected_joined for p in captured)
    # base_clone is outside worker_dir read root -> suppressed.
    assert len(result.sandbox.suppressions) == 1


def test_base_clone_anchor_without_base_clone_context_raises() -> None:
    """anchor='base_clone' without --base-clone surfaces a usable error."""
    schema = {
        "worker_roles": {
            "demo": _sandbox_role(
                deny_read=[_structured("base_clone", ".git/HEAD")],
                additional=[],
            ),
        },
    }
    with pytest.raises(ValueError) as info:
        generator.render_role_with_metadata(
            schema,
            role="demo",
            worker_dir="/home/u/wd",
            claude_org_path="/home/u/co",
            wsl_detector=lambda: False,
        )
    msg = str(info.value)
    assert "anchor='base_clone'" in msg
    assert "--base-clone" in msg


def test_normalize_sandbox_entry_accepts_base_clone_anchor() -> None:
    """_normalize_sandbox_entry accepts the new base_clone anchor."""
    norm = generator._normalize_sandbox_entry(
        {"anchor": "base_clone", "path": ".git/objects"}
    )
    assert norm is not None
    assert norm.anchor == "base_clone"
    assert norm.path == ".git/objects"


def test_cli_pattern_choices_rejects_typo(tmp_path: Path) -> None:
    """--pattern choices=A|B|C rejects free-form values like 'b'."""
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(
        json.dumps({"worker_roles": {"demo": {"permissions": {"deny": []}}}}),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit) as info:
        generator.main(
            [
                "--role",
                "demo",
                "--worker-dir",
                "/wd",
                "--claude-org-path",
                "/co",
                "--pattern",
                "b",  # lowercase typo -- must be rejected
                "--schema",
                str(schema_path),
            ]
        )
    assert info.value.code != 0


def test_cli_pattern_b_sandbox_by_pattern_renders(tmp_path: Path) -> None:
    """End-to-end: settings generate --pattern B writes Pattern B sandbox."""
    schema_path = tmp_path / "schema.json"
    schema_payload = {
        "worker_roles": {
            "demo": {
                "permissions": {"deny": []},
                "sandbox_by_pattern": {
                    "A": {
                        "enabled": True,
                        "filesystem": {
                            "denyRead": ["secrets.env"],
                            "denyWrite": [],
                            "additionalDirectories": ["{worker_dir}"],
                        },
                        "failIfUnavailable": False,
                    },
                    "B": {
                        "enabled": True,
                        "filesystem": {
                            "denyRead": [],
                            "denyWrite": [],
                            "additionalDirectories": [
                                "{worker_dir}",
                                "{base_clone}/.git/worktrees/{task_id}",
                            ],
                        },
                        "failIfUnavailable": False,
                    },
                },
            }
        }
    }
    schema_path.write_text(json.dumps(schema_payload), encoding="utf-8")
    out = tmp_path / "out.json"
    rc = generator.main(
        [
            "--role",
            "demo",
            "--worker-dir",
            "/home/u/proj/.worktrees/task-1",
            "--claude-org-path",
            "/home/u/co",
            "--base-clone",
            "/home/u/proj",
            "--task-id",
            "task-1",
            "--branch-ref",
            "feat/x",
            "--pattern",
            "B",
            "--schema",
            str(schema_path),
            "--out",
            str(out),
        ]
    )
    assert rc == 0
    parsed = json.loads(out.read_text(encoding="utf-8"))
    assert "sandbox_by_pattern" not in parsed
    fs = parsed["sandbox"]["filesystem"]
    assert fs["additionalDirectories"] == [
        "/home/u/proj/.worktrees/task-1",
        "/home/u/proj/.git/worktrees/task-1",
    ]


def test_cli_pattern_required_when_sandbox_by_pattern_present(tmp_path: Path) -> None:
    """settings generate without --pattern errors out when role uses sandbox_by_pattern."""
    schema_path = tmp_path / "schema.json"
    schema_path.write_text(
        json.dumps(
            {
                "worker_roles": {
                    "demo": {
                        "permissions": {"deny": []},
                        "sandbox_by_pattern": {
                            "A": {"enabled": True, "filesystem": {}},
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    rc = generator.main(
        [
            "--role",
            "demo",
            "--worker-dir",
            "/wd",
            "--claude-org-path",
            "/co",
            "--schema",
            str(schema_path),
        ]
    )
    assert rc == 2
