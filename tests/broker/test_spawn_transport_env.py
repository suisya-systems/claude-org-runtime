"""Every pane the broker daemon spawns is told it runs on the broker transport.

Without an explicit ORG_TRANSPORT in the pane env, panes inherit whatever the tmux
server's global environment holds (e.g. ORG_TRANSPORT=renga from a login shell),
so helpers resolving the transport from env inside broker panes address renga.
"""
from __future__ import annotations

import pytest

from claude_org_runtime.broker.server import Broker
from claude_org_runtime.broker.surface import dispatch_tool

from .conftest import FakeAdapter


def _ops(b: Broker, agent_id="d", role="dispatcher"):
    tok = b.issue_token(agent_id, agent_id, role)
    b.register_local(tok)
    return b.get_bind(tok)


@pytest.mark.parametrize("space_layout", [False, True])
@pytest.mark.parametrize("tool,args,role", [
    ("spawn_claude_pane", {"direction": "vertical", "name": "w", "cwd": "/repo"},
     "dispatcher"),
    ("spawn_codex_pane", {"direction": "vertical", "name": "cx", "cwd": "/repo"},
     "dispatcher"),
    ("spawn_pane", {"direction": "vertical", "command": "top", "name": "g"},
     "secretary"),
])
def test_spawned_pane_env_has_broker_transport(
    tmp_path, monkeypatch, tool, args, role, space_layout,
):
    monkeypatch.setenv("ORG_TRANSPORT", "renga")  # the leak source on the host
    adapter = FakeAdapter(supports_space_layout=space_layout)
    b = Broker(state_dir=tmp_path / "sd", adapter=adapter)
    adapter.add_pane(active=True)
    dispatch_tool(b, _ops(b, "op", role), tool, args)
    assert adapter.spawned, "spawn did not reach the adapter"
    assert adapter.spawned[-1]["env"].get("ORG_TRANSPORT") == "broker"


def test_env_extra_cannot_override_broker_transport(tmp_path):
    adapter = FakeAdapter()
    b = Broker(state_dir=tmp_path / "sd", adapter=adapter)
    adapter.add_pane(active=True)
    b._adapter_spawn(["sh"], None, None, None,
                     env_extra={"ORG_TRANSPORT": "renga", "X": "1"})
    env = adapter.spawned[-1]["env"]
    assert env["ORG_TRANSPORT"] == "broker"
    assert env["X"] == "1"
