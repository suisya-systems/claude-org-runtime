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


def test_pure_glob_entry_is_not_suppressed() -> None:
    """Patterns whose first segment is a glob have no anchored realpath -> kept."""
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
    assert result.sandbox.suppressions == []
    fs = result.settings["sandbox"]["filesystem"]
    assert fs["denyRead"] == ["**/credentials*", "*.pem"]


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
