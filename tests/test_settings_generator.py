"""Tests for the settings generator port."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

import pytest

from claude_org_runtime.settings import generator


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
