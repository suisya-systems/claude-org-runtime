"""Workers can build images but not push them to a registry (#149).

Worker templates allow ``docker build`` / ``docker buildx build`` and run them
outside the sandbox, where the host's ~/.docker credentials are readable. The
worker ``--settings`` overlay carries deny rules for the registry-write forms.
The offline tests model Claude Code's Bash rule matching as documented at
https://code.claude.com/docs/en/permissions: deny is evaluated first, ``*``
matches any characters (spaces included), and the legacy ``:*`` suffix is a
prefix match.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from claude_org_runtime.dispatcher.runner import WORKER_SANDBOX_SETTINGS
from claude_org_runtime.settings import generator

DENY = WORKER_SANDBOX_SETTINGS["permissions"]["deny"]
ALLOW = generator.load_schema()["worker_roles"]["default"]["permissions"]["allow"]

PUSHES = [
    "docker push reg.example/app:1",
    "docker image push reg.example/app:1",
    "docker manifest push reg.example/app:1",
    "docker compose push",
    "docker compose -f c.yml push web",
    "docker --config /tmp/c push reg.example/app:1",
    "docker build --push -t reg.example/app:1 .",
    "docker buildx build --push -t reg.example/app:1 .",
    "docker buildx build -t reg.example/app:1 . --push",
    "docker buildx build --push=true -t reg.example/app:1 .",
    'docker buildx build "--push" -t reg.example/app:1 .',
    "docker buildx build --output type=registry -t reg.example/app:1 .",
    "docker buildx build --output=type=registry,name=reg.example/app:1 .",
    "docker buildx build -o type=registry -t reg.example/app:1 .",
    "docker buildx build -o type=image,name=reg.example/app:1,push=true .",
    "docker buildx build --cache-to type=registry,ref=reg.example/app:cache .",
    "docker buildx bake --push",
    "docker buildx imagetools create -t reg.example/app:2 reg.example/app:1",
]

LOCAL_BUILDS = [
    "docker build -t app:dev .",
    "docker build --pull -t app:dev .",
    "docker buildx build --load -t app:dev .",
    "docker buildx build -t pusher:dev .",
    "docker buildx build --output type=docker -t app:dev .",
    "docker buildx build -o type=local,dest=out .",
    "docker buildx build --build-arg BASE=reg.example/private/base:1 -t app:dev .",
    "docker images",
    "docker image inspect app:dev",
]


def _matches(rule: str, command: str) -> bool:
    spec = rule[len("Bash(") : -1]
    if spec.endswith(":*"):
        return command == spec[:-2] or command.startswith(spec[:-2] + " ")
    pattern = ".*".join(re.escape(part) for part in spec.split("*"))
    return re.fullmatch(pattern, command, re.DOTALL) is not None


@pytest.mark.parametrize("command", PUSHES)
def test_registry_writes_denied(command: str) -> None:
    assert any(_matches(rule, command) for rule in DENY), command


@pytest.mark.parametrize("command", LOCAL_BUILDS)
def test_local_builds_still_allowed(command: str) -> None:
    assert not any(_matches(rule, command) for rule in DENY), command
    assert any(_matches(rule, command) for rule in ALLOW), command


def test_push_was_auto_allowed_before() -> None:
    # The hole: the allow set alone approves a build that pushes.
    assert any(_matches(r, "docker buildx build --push -t r/a:1 .") for r in ALLOW)


@pytest.mark.skipif(
    os.environ.get("CLAUDE_ORG_RUNTIME_LIVE_CLAUDE") != "1" or not shutil.which("claude"),
    reason="live Claude Code check; set CLAUDE_ORG_RUNTIME_LIVE_CLAUDE=1 with an authenticated `claude` on PATH",
)
@pytest.mark.skipif(os.name == "nt", reason="the fake docker is a POSIX shell script")
def test_live_claude_bash_tool(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    fake = bin_dir / "docker"
    fake.write_text(f'#!/bin/sh\necho "$*" >> "{log}"\n', encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({"permissions": {"allow": ALLOW, "deny": DENY}}), encoding="utf-8"
    )
    commands = [
        "docker build -t app:dev .",
        "docker buildx build --push -t reg.example/app:1 .",
        "docker buildx build -o type=registry -t reg.example/app:1 .",
        "docker buildx build --load -t app:dev .",
    ]
    prompt = (
        "Run each of these shell commands with the Bash tool, one call per "
        "command, exactly as written, even if one is refused: "
        + " ; ".join(f"`{c}`" for c in commands)
    )
    result = subprocess.run(
        ["claude", "-p", prompt, "--tools", "Bash", "--setting-sources", "project",
         "--settings", str(settings), "--output-format", "text"],
        cwd=tmp_path, capture_output=True, text=True, timeout=300,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )
    assert result.returncode == 0, result.stderr
    ran = log.read_text(encoding="utf-8") if log.exists() else ""
    assert "build -t app:dev ." in ran, (ran, result.stdout)
    assert "--load" in ran, (ran, result.stdout)
    assert "--push" not in ran and "type=registry" not in ran, ran
