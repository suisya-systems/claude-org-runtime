# -*- coding: utf-8 -*-
"""Unit tests for the stateless MCP surface (catalogue, tier gating, builders,
default-deny argv guards) — Issue C renga golden-shape compat."""

from __future__ import annotations

import json
import os

import pytest

from claude_org_runtime.broker import surface
from claude_org_runtime.broker.server import Broker
from claude_org_runtime.broker.surface import (
    ToolArgError,
    build_claude_argv,
    build_codex_argv,
    dispatch_tool,
    resolve_spawn_cwd,
    tools_for,
)

# renga golden shape: 移植 12 面 + spawn_codex_pane = 13 面 (drop-in 形差ゼロ)。
GOLDEN_SHAPE = {
    "send_message", "check_messages", "list_peers", "set_summary",
    "list_panes", "inspect_pane", "send_keys", "poll_events", "close_pane",
    "set_pane_identity", "spawn_claude_pane", "spawn_pane", "spawn_codex_pane",
}


def _bind(broker, agent_id, role="worker", register=True):
    token = broker.issue_token(agent_id, agent_id, role)
    if register:
        broker.register_local(token)
    return broker.get_bind(token)


# ----------------------------------------------------- spawn cwd 解決 (#61)
# renga 契約: absolute は as-is / relative は caller pane の cwd を base に。
# caller cwd 不明 + relative は決定的に拒否 (黙って daemon base に落とさない)。

def test_resolve_spawn_cwd_absolute_passes_through_unchanged():
    # absolute は無変換で透過 (normpath もしない; プラットフォーム差を作らない)。
    assert resolve_spawn_cwd("/repo", None) == "/repo"
    assert resolve_spawn_cwd("/repo", "/other/base") == "/repo"


def test_resolve_spawn_cwd_relative_anchors_on_caller_cwd():
    # relative + caller cwd 既知 → caller cwd を base に join した absolute。
    base = os.path.join(os.sep, "root", "dogfood", "claude-org-ja")
    resolved = resolve_spawn_cwd(".dispatcher", base)
    expected = os.path.normpath(os.path.join(base, ".dispatcher"))
    assert resolved == expected
    # 不変条件 (Windows/POSIX 双方で頑健): caller cwd が prefix で末尾に component。
    # (os.path.isabs は Windows の ntpath が drive 無し rooted を absolute と
    # 見なさない 3.13+ 挙動のため使わない。prefix/suffix 不変条件で代替する。)
    assert resolved.startswith(os.path.normpath(base))
    assert resolved.endswith(".dispatcher")
    # 本 Issue の核心: dogfood/ セグメントが落ちない。
    assert "dogfood" in resolved


def test_resolve_spawn_cwd_relative_unknown_caller_is_rejected():
    # relative + caller cwd 不明 (論理ペイン等) → 決定的に拒否 (ToolArgError)。
    # 黙って adapter (daemon base) で再解決させない (= 本 Issue の誤着地の根因)。
    with pytest.raises(ToolArgError) as ei:
        resolve_spawn_cwd(".dispatcher", None)
    assert "cwd_unanchored" in str(ei.value)


def test_resolve_spawn_cwd_none_inherits_caller_cwd():
    # cwd 省略 (None) → caller cwd を継承 (renga: 省略時は呼び元 cwd で起動)。
    assert resolve_spawn_cwd(None, "/root/base") == "/root/base"
    # caller cwd も不明なら None (adapter 既定に委ねる; relative ではないので拒否不要)。
    assert resolve_spawn_cwd(None, None) is None


# --------------------------------------------------------------- catalogue
def test_catalogue_is_renga_golden_shape():
    assert {t["name"] for t in surface.TOOLS} == GOLDEN_SHAPE


def test_default_protocol_is_first_listed():
    assert surface.PROTOCOL_VERSIONS[0] == "2025-06-18"


def test_tools_for_tier_scoping():
    messaging = {"send_message", "check_messages", "list_peers", "set_summary"}
    # worker / curator: messaging のみ。
    assert {t["name"] for t in tools_for("worker")} == messaging
    assert {t["name"] for t in tools_for("curator")} == messaging
    # dispatcher: messaging + pane 操作 (generic spawn_pane を除く)。
    disp = {t["name"] for t in tools_for("dispatcher")}
    assert "spawn_claude_pane" in disp and "spawn_codex_pane" in disp
    assert "list_panes" in disp and "send_keys" in disp
    assert "spawn_pane" not in disp
    # secretary: 全面 (generic spawn_pane を含む)。
    assert {t["name"] for t in tools_for("secretary")} == GOLDEN_SHAPE


def test_spawn_pane_schemas_match_renga_required_fields():
    by_name = {t["name"]: t for t in surface.TOOLS}
    claude = by_name["spawn_claude_pane"]["inputSchema"]
    assert claude["required"] == ["direction"]
    assert set(claude["properties"]) >= {
        "direction", "target", "name", "role", "model", "permission_mode",
        "args", "cwd",
    }
    codex = by_name["spawn_codex_pane"]["inputSchema"]
    assert codex["required"] == ["direction"]
    assert set(codex["properties"]) >= {
        "direction", "target", "name", "role", "args", "cwd",
    }
    # codex は model/permission_mode を持たない (renga と同形)。
    assert "model" not in codex["properties"]


# --------------------------------------------------------------- tier gating
def test_worker_cannot_reach_pane_ops(tmp_path):
    b = Broker(state_dir=tmp_path, adapter=None)
    worker = _bind(b, "w", role="worker")
    out = dispatch_tool(b, worker, "send_keys", {"target": "1", "text": "y"})
    assert out["isError"] is True
    assert "[tool_not_authorized]" in out["content"][0]["text"]


def test_dispatcher_cannot_reach_generic_spawn_pane(tmp_path):
    b = Broker(state_dir=tmp_path, adapter=None)
    disp = _bind(b, "d", role="dispatcher")
    out = dispatch_tool(b, disp, "spawn_pane", {"direction": "vertical"})
    assert out["isError"] is True
    assert "[tool_not_authorized]" in out["content"][0]["text"]


def test_unknown_tool_is_iserror(tmp_path):
    b = Broker(state_dir=tmp_path, adapter=None)
    disp = _bind(b, "d", role="dispatcher")
    out = dispatch_tool(b, disp, "spawn_agent", {})
    assert out["isError"] is True
    assert "[unknown_tool]" in out["content"][0]["text"]


# --------------------------------------------------------------- messaging
def test_dispatch_send_message_validates_types(tmp_path):
    b = Broker(state_dir=tmp_path, adapter=None)
    src = _bind(b, "src")
    with pytest.raises(ToolArgError):
        dispatch_tool(b, src, "send_message", {"to_id": "x"})


def test_dispatch_set_summary_validates_type(tmp_path):
    b = Broker(state_dir=tmp_path, adapter=None)
    src = _bind(b, "src")
    with pytest.raises(ToolArgError):
        dispatch_tool(b, src, "set_summary", {"summary": 123})


def test_list_peers_includes_cwd_and_receive_mode(tmp_path):
    b = Broker(state_dir=tmp_path, adapter=None)
    # cwd を保持した bind を作る。
    tok = b.issue_token("worker-x", "worker-x", "worker", cwd="/tmp/x")
    b.register_local(tok)
    src = _bind(b, "src")
    out = dispatch_tool(b, src, "list_peers", {})
    peers = {p["id"]: p for p in json.loads(out["content"][0]["text"])["peers"]}
    assert peers["worker-x"]["cwd"] == "/tmp/x"
    # D2: broker は push 一次 (channel sidecar)。報告 receive_mode は "push"。
    assert peers["worker-x"]["receive_mode"] == "push"


# --------------------------------------------------------------- claude builder
def test_build_claude_argv_injects_mcp_config_and_structured_fields():
    argv = build_claude_argv(
        mcp_config_json='{"mcpServers":{}}', model="opus",
        permission_mode="acceptEdits", extra_args=["--add-dir", "/repo"],
    )
    assert argv[0] == "claude"
    assert "--mcp-config" in argv
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert "--add-dir" in argv


def test_build_claude_argv_channel_server_emits_one_dev_channel():
    """D2 / §9.7: channel_server を渡したときだけ dev-channel を **1 つ** emit する。

    push 一次配送 (broker 枝) の spawn は channel sidecar の dev-channel を 1 本だけ
    注入し、それ以外の caller (renga 枝の build_up_argv 等、channel_server 無し) は
    **第二の dev-channel を一切 emit しない** = launcher argv の bit 等価 (§9.7)。
    """
    # channel_server 無し: dev-channel flag はゼロ (renga/no-channel caller 経路)。
    plain = build_claude_argv(mcp_config_json="{}")
    assert "--dangerously-load-development-channels" not in plain
    # channel_server 有り: ちょうど 1 本、指定 server を指す。
    withch = build_claude_argv(mcp_config_json="{}", channel_server="org-broker-channel")
    assert withch.count("--dangerously-load-development-channels") == 1
    idx = withch.index("--dangerously-load-development-channels")
    assert withch[idx + 1] == "server:org-broker-channel"


def test_build_claude_argv_rejects_caller_dev_channel():
    """caller は args[] で第二/別の dev-channel を持ち込めない (単一注入経路, §9.5)。"""
    with pytest.raises(ToolArgError):
        build_claude_argv(
            mcp_config_json="{}",
            extra_args=["--dangerously-load-development-channels", "server:evil"],
        )


def test_build_claude_argv_rejects_reserved_args():
    for bad in (["--model", "opus"], ["--permission-mode", "x"], ["--mcp-config", "{}"]):
        with pytest.raises(ToolArgError):
            build_claude_argv(mcp_config_json="{}", extra_args=bad)


def test_build_claude_argv_rejects_headless_flags():
    for bad in (["-p"], ["--print"], ["--output-format", "json"], ["--headless"]):
        with pytest.raises(ToolArgError):
            build_claude_argv(mcp_config_json="{}", extra_args=bad)


def test_build_claude_argv_rejects_subcommand_and_dashdash():
    with pytest.raises(ToolArgError):
        build_claude_argv(mcp_config_json="{}", extra_args=["mcp"])  # bare positional
    with pytest.raises(ToolArgError):
        build_claude_argv(mcp_config_json="{}", extra_args=["--"])


# ---------------------------------------------- codex builder (MANDATORY guard)
def test_build_codex_argv_allows_interactive_flags():
    assert build_codex_argv(extra_args=[]) == ["codex"]
    assert build_codex_argv(extra_args=["-m", "gpt-5"]) == ["codex", "-m", "gpt-5"]
    assert build_codex_argv(extra_args=["--model", "o3", "--search"]) == [
        "codex", "--model", "o3", "--search",
    ]
    # --flag=value 形も許す。
    assert build_codex_argv(extra_args=["--config=key=val"]) == [
        "codex", "--config=key=val",
    ]


@pytest.mark.parametrize("subcommand", [
    "exec", "review", "mcp-server", "app-server", "exec-server",
    "apply", "sandbox", "completion", "login", "resume",
])
def test_build_codex_argv_rejects_non_interactive_subcommands(subcommand):
    """§8 Issue C 完了基準: codex spawn は対話 TUI に構造的限定され、exec /
    review / *-server 等の非対話サブコマンドを default-deny で拒否する。"""
    with pytest.raises(ToolArgError):
        build_codex_argv(extra_args=[subcommand])
    # flag の後ろに置いてもバイパスできない (blacklist 後追いの取り逃しを防ぐ)。
    with pytest.raises(ToolArgError):
        build_codex_argv(extra_args=["--model", "o3", subcommand])


def test_build_codex_argv_rejects_dashdash_and_unknown_and_positional():
    with pytest.raises(ToolArgError):
        build_codex_argv(extra_args=["--"])
    with pytest.raises(ToolArgError):
        build_codex_argv(extra_args=["--definitely-not-a-flag"])
    with pytest.raises(ToolArgError):
        build_codex_argv(extra_args=["write me a poem"])  # bare positional prompt


def test_build_codex_argv_rejects_exec_after_dashdash_bypass():
    # `codex -- exec` / `codex --model o3 -- exec` のような `--` 経由バイパスも拒否。
    with pytest.raises(ToolArgError):
        build_codex_argv(extra_args=["--model", "o3", "--", "exec"])


def test_codex_basename_guard_rejects_non_codex_argv0():
    from claude_org_runtime.broker.surface import _guard_interactive_codex_argv
    with pytest.raises(ToolArgError):
        _guard_interactive_codex_argv(["python", "agent.py"])
    # 絶対パスの codex は basename 判定で通す (false-reject しない)。
    _guard_interactive_codex_argv(["/usr/local/bin/codex", "--search"])


# --------------------------------------------------------------- name validation
def test_set_pane_identity_validates_name(tmp_path, fake_adapter):
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    fake_adapter.add_pane(active=True)
    disp = _bind(b, "d", role="dispatcher")
    for bad in ("", "123", "bad name", "bad/name"):
        with pytest.raises(ToolArgError):
            dispatch_tool(b, disp, "set_pane_identity",
                          {"target": "focused", "name": bad})
