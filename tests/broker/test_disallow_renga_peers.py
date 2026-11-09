"""#76 の防御を --strict-mcp-config ではなく --disallowedTools で行う受け入れテスト。

strict は claude.ai コネクタ / プラグイン / ユーザー設定の MCP まで落としていた。
防ぎたいのは ambient renga-peers の同名ツールへの誤ルートだけなので、その 14 ツールを
完全修飾名で止め、他の MCP は load されるままにする。
"""
from __future__ import annotations

import json

from claude_org_runtime.broker import launcher
from claude_org_runtime.broker.server import Broker
from claude_org_runtime.broker.surface import build_claude_argv, dispatch_tool

RENGA_TOOLS = {
    f"mcp__renga-peers__{t}" for t in (
        "set_summary", "list_peers", "send_message", "check_messages", "list_panes",
        "spawn_pane", "close_pane", "focus_pane", "new_tab", "inspect_pane",
        "poll_events", "send_keys", "spawn_claude_pane", "set_pane_identity",
    )
}


def _denied(argv):
    assert argv.count("--disallowedTools") == 1
    return set(argv[argv.index("--disallowedTools") + 1].split(","))


def test_builder_blocks_renga_peers_tools_without_strict():
    argv = build_claude_argv(mcp_config_json="{}")
    assert "--strict-mcp-config" not in argv
    assert _denied(argv) == RENGA_TOOLS


def test_builder_does_not_inject_strict_with_channel_or_resume():
    argv = build_claude_argv(mcp_config_json="{}", channel_server="org-broker-channel",
                             continue_session=True)
    assert "--strict-mcp-config" not in argv
    assert _denied(argv) == RENGA_TOOLS


def test_caller_cannot_weaken_the_denylist():
    import pytest
    from claude_org_runtime.broker.surface import ToolArgError
    with pytest.raises(ToolArgError):
        build_claude_argv(mcp_config_json="{}", extra_args=["--disallowedTools", "x"])


def test_up_argv_blocks_renga_peers_too():
    argv = launcher.build_up_argv({"mcpServers": {"org-broker": {}}})
    assert "--strict-mcp-config" not in argv
    assert _denied(argv) == RENGA_TOOLS


def test_spawn_claude_pane_blocks_renga_peers(tmp_path, fake_adapter):
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    fake_adapter.add_pane(active=True)
    tok = b.issue_token("d", "d", "dispatcher")
    b.register_local(tok)
    dispatch_tool(b, b.get_bind(tok), "spawn_claude_pane", {
        "direction": "vertical", "name": "w1", "role": "worker", "cwd": "/repo",
    })
    argv = fake_adapter.spawned[-1]["argv"]
    assert "--strict-mcp-config" not in argv
    assert _denied(argv) == RENGA_TOOLS
    cfg = json.loads(argv[argv.index("--mcp-config") + 1])
    assert "org-broker" in cfg["mcpServers"]
