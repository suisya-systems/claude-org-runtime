# -*- coding: utf-8 -*-
"""workspace レイアウトの broker 面テスト (Issue #110 §6.2 Layer B/C)。

- ``surface.space_descriptor_for`` の role/project → SpaceDescriptor 写像。
- spawn 系が ``supports_space_layout`` backend にだけ SpaceDescriptor を渡し、flat
  backend には渡さない (完全不変) こと。
"""

from __future__ import annotations

import json

from claude_org_runtime.broker.server import Broker
from claude_org_runtime.broker.surface import (
    dispatch_tool,
    is_control_role,
    space_descriptor_for,
)
from claude_org_runtime.terminal.base import (
    SPACE_CONTROL,
    SPACE_UNASSIGNED,
)

from .conftest import FakeAdapter


def _ops(b, agent_id="d", role="dispatcher", cwd=None):
    tok = b.issue_token(agent_id, agent_id, role, cwd=cwd)
    b.register_local(tok)
    return b.get_bind(tok)


def _text(out):
    return json.loads(out["content"][0]["text"])


# --------------------------------------------------------------- pure mapping

def test_is_control_role() -> None:
    assert is_control_role("secretary")
    assert is_control_role("dispatcher")
    assert is_control_role("watcher")
    assert is_control_role("pr-watch")        # watcher 変種 ("watch" を含む)
    assert is_control_role("attention")
    assert not is_control_role("worker")
    assert not is_control_role(None)
    assert not is_control_role("")


def test_space_descriptor_for_control_roles() -> None:
    for role in ("secretary", "dispatcher", "watcher", "pr-watch"):
        d = space_descriptor_for(role, project=None)
        assert d.space_key == SPACE_CONTROL


def test_space_descriptor_for_worker_with_project() -> None:
    d = space_descriptor_for("worker", project="transport-lab")
    assert d.space_key == "project:transport-lab"


def test_space_descriptor_for_worker_without_project_degrades_to_unassigned() -> None:
    d = space_descriptor_for("worker", project=None)
    assert d.space_key == SPACE_UNASSIGNED


def test_space_descriptor_for_projectless_control_prefers_control() -> None:
    # control role wins even if a project is (redundantly) supplied.
    d = space_descriptor_for("dispatcher", project="x")
    assert d.space_key == SPACE_CONTROL


# ------------------------------------------------- spawn routing (Layer C)

def test_spawn_claude_passes_space_to_layout_backend(tmp_path) -> None:
    adapter = FakeAdapter(supports_space_layout=True)
    b = Broker(state_dir=tmp_path, adapter=adapter)
    adapter.add_pane(active=True)
    disp = _ops(b)
    dispatch_tool(b, disp, "spawn_claude_pane", {
        "direction": "vertical", "name": "worker-foo", "role": "worker",
        "project": "transport-lab", "cwd": "/repo",
    })
    space = adapter.spawned[-1]["space"]
    assert space is not None
    assert space.space_key == "project:transport-lab"


def test_spawn_claude_control_role_routes_to_control_space(tmp_path) -> None:
    adapter = FakeAdapter(supports_space_layout=True)
    b = Broker(state_dir=tmp_path, adapter=adapter)
    adapter.add_pane(active=True)
    disp = _ops(b)
    dispatch_tool(b, disp, "spawn_claude_pane", {
        "direction": "vertical", "name": "disp-child", "role": "dispatcher",
        "cwd": "/repo",
    })
    assert adapter.spawned[-1]["space"].space_key == SPACE_CONTROL


def test_spawn_claude_worker_without_project_uses_unassigned(tmp_path) -> None:
    adapter = FakeAdapter(supports_space_layout=True)
    b = Broker(state_dir=tmp_path, adapter=adapter)
    adapter.add_pane(active=True)
    disp = _ops(b)
    dispatch_tool(b, disp, "spawn_claude_pane", {
        "direction": "vertical", "name": "worker-bar", "role": "worker",
        "cwd": "/repo",
    })
    assert adapter.spawned[-1]["space"].space_key == SPACE_UNASSIGNED


def test_spawn_claude_flat_backend_receives_no_space(tmp_path) -> None:
    # A backend without supports_space_layout (tmux/wezterm-style) must receive the
    # legacy flat spawn — space stays None (完全不変, §6.2).
    adapter = FakeAdapter()  # supports_space_layout absent
    b = Broker(state_dir=tmp_path, adapter=adapter)
    adapter.add_pane(active=True)
    disp = _ops(b)
    dispatch_tool(b, disp, "spawn_claude_pane", {
        "direction": "vertical", "name": "worker-foo", "role": "worker",
        "project": "transport-lab", "cwd": "/repo",
    })
    assert adapter.spawned[-1]["space"] is None


def test_spawn_codex_and_generic_pass_space_to_layout_backend(tmp_path) -> None:
    adapter = FakeAdapter(supports_space_layout=True)
    b = Broker(state_dir=tmp_path, adapter=adapter)
    adapter.add_pane(active=True)
    disp = _ops(b)
    dispatch_tool(b, disp, "spawn_codex_pane", {
        "direction": "vertical", "name": "codex-w", "role": "worker",
        "project": "p1", "cwd": "/repo",
    })
    assert adapter.spawned[-1]["space"].space_key == "project:p1"
    # generic spawn_pane (secretary tier, attention watcher) -> control if watcher role.
    sec = _ops(b, agent_id="s", role="secretary")
    dispatch_tool(b, sec, "spawn_pane", {
        "direction": "vertical", "name": "attn", "role": "attention",
        "command": "sh", "cwd": "/repo",
    })
    assert adapter.spawned[-1]["space"].space_key == SPACE_CONTROL
