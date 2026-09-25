"""``.env`` templates stay readable while real ``.env`` secrets stay denied.

The bundled worker templates deny ``Read(.env)`` / ``Read(.env.*)`` and carve
``generator.ENV_TEMPLATE_PATTERNS`` back out with ``Read(!<pattern>)`` rules.
https://code.claude.com/docs/en/permissions ("Read and Edit") defines the
semantics these tests model: a bare filename matches at any depth, and a
``!`` deny pattern un-denies what the relative rules listed before it in the
same file matched (an allow rule cannot, since deny is evaluated first).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from fnmatch import fnmatchcase
from pathlib import Path

import pytest

from claude_org_runtime.settings import generator

TEMPLATES = [
    ".env.example",
    ".env.live.example",
    ".env.production.example",
    "sub/.env.example",
    "sub/deep/.env.local.example",
]
SECRETS = [
    ".env",
    ".env.local",
    ".env.production",
    ".env.development.local",
    ".env.staging",
    ".env.example.bak",
    "sub/.env",
    "sub/.env.live",
    "sub/deep/.env.local",
]


def _read_denied(deny: list, path: str) -> bool:
    """Evaluate ``Read`` deny rules for a cwd-relative ``path``.

    Models only the rule shapes the templates use: relative bare-name
    patterns, optionally ``**/``-prefixed, which gitignore matches against
    the basename at any depth. Anchored (``~/``, ``//``, ``/``) rules never
    match a project-relative file and are skipped.
    """
    name = path.rsplit("/", 1)[-1]
    denied = False
    for rule in deny:
        if not (isinstance(rule, str) and rule.startswith("Read(") and rule.endswith(")")):
            continue
        spec = rule[len("Read(") : -1]
        negate = spec.startswith("!")
        spec = spec.lstrip("!")
        if spec.startswith("**/"):
            spec = spec[3:]
        if spec.startswith(("~/", "/")) or "/" in spec:
            continue
        if fnmatchcase(name, spec):
            denied = not negate
    return denied


def _roles_denying_env() -> dict[str, list]:
    schema = generator.load_schema()
    out = {}
    for role, body in schema["worker_roles"].items():
        if role.startswith("$"):
            continue
        rendered = generator.render_role(schema, role, "/w", "/org")
        deny = rendered.get("permissions", {}).get("deny", [])
        if "Read(.env.*)" in deny:
            out[role] = deny
    return out


def test_some_role_denies_env() -> None:
    # Guard against the parametrized tests below silently covering nothing.
    assert {"default", "claude-org-self-edit"} <= set(_roles_denying_env())


@pytest.mark.parametrize("role", sorted(_roles_denying_env()))
def test_carve_out_follows_env_deny_and_matches_constant(role: str) -> None:
    deny = _roles_denying_env()[role]
    start = deny.index("Read(.env.*)") + 1
    carve = [f"Read(!{p})" for p in generator.ENV_TEMPLATE_PATTERNS]
    # Order matters: a ``!`` rule only carves out rules listed before it.
    assert deny[start : start + len(carve)] == carve
    assert [r for r in deny if r.startswith("Read(!")] == carve


@pytest.mark.parametrize("role", sorted(_roles_denying_env()))
@pytest.mark.parametrize("path", TEMPLATES)
def test_templates_readable(role: str, path: str) -> None:
    assert not _read_denied(_roles_denying_env()[role], path)


@pytest.mark.parametrize("role", sorted(_roles_denying_env()))
@pytest.mark.parametrize("path", SECRETS)
def test_secrets_denied(role: str, path: str) -> None:
    assert _read_denied(_roles_denying_env()[role], path)


def test_model_needs_the_carve_out() -> None:
    # Without the ``!`` rules the templates are denied -- the reported bug.
    assert _read_denied(["Read(.env)", "Read(.env.*)"], ".env.example")


@pytest.mark.skipif(
    os.environ.get("CLAUDE_ORG_RUNTIME_LIVE_CLAUDE") != "1" or not shutil.which("claude"),
    reason="live Claude Code check; set CLAUDE_ORG_RUNTIME_LIVE_CLAUDE=1 with an authenticated `claude` on PATH",
)
def test_live_claude_read_tool(tmp_path: Path) -> None:
    deny = _roles_denying_env()["default"]
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"permissions": {"deny": deny}}), encoding="utf-8")
    proj = tmp_path / "proj"
    (proj / "sub").mkdir(parents=True)
    files = {
        ".env.example": "TPL_ROOT_7f3",
        "sub/.env.live.example": "TPL_SUB_7f3",
        ".env": "SEC_ROOT_7f3",
        ".env.production": "SEC_PROD_7f3",
        "sub/.env.local": "SEC_SUB_7f3",
    }
    for rel, token in files.items():
        (proj / rel).write_text(token + "\n", encoding="utf-8")
    prompt = (
        "Using ONLY the Read tool, read each of these files: "
        + ", ".join(files)
        + ". For each, print one line '<path>: <content>' or '<path>: DENIED'."
    )
    result = subprocess.run(
        ["claude", "-p", prompt, "--tools", "Read", "--setting-sources", "project",
         "--settings", str(settings), "--output-format", "text"],
        cwd=proj, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL,
    )
    out = result.stdout
    assert result.returncode == 0, result.stderr
    for rel, token in files.items():
        assert (token in out) == rel.endswith(".example"), (rel, out)
