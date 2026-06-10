# -*- coding: utf-8 -*-
"""MCP protocol smoke tests for the ported org-broker.

Ported from ``claude-org-transport-lab`` ``spike/mcp_smoke_test.py`` (the
8 verified scenarios) into pytest: handshake, tools/list surface, auth,
messaging roundtrip + token-derived attribution, list_peers / set_summary,
unknown method / tool / invalid params, session validation, and DELETE
session revocation (the case that used to deadlock).
"""

from __future__ import annotations

import json

import pytest

from claude_org_runtime.broker.server import Broker
from claude_org_runtime.broker.surface import ToolArgError, dispatch_tool

from .conftest import MiniMcpClient


# --------------------------------------------------------------------- [1]
def test_handshake_registers_bind(broker, client_factory):
    a = MiniMcpClient(broker.url, broker.issue_token("agent-a", "agent-a", "worker"))
    init = a.rpc("initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "smoke", "version": "0"},
    })
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert a.session_id is not None
    a.notify("notifications/initialized")
    # AC-2-3: initialize 到達で bind が registered になる検知点。
    assert broker.find_registered("agent-a") is not None


def test_initialize_falls_back_to_default_protocol(broker):
    a = MiniMcpClient(broker.url, broker.issue_token("agent-a", "agent-a", "worker"))
    init = a.rpc("initialize", {"protocolVersion": "1999-01-01"})
    assert init["result"]["protocolVersion"] == "2025-06-18"  # PROTOCOL_VERSIONS[0]


# --------------------------------------------------------------------- [2]
def test_tools_list_is_worker_surface(client_factory):
    a = client_factory("agent-a")
    tl = a.rpc("tools/list")
    names = {t["name"] for t in tl["result"]["tools"]}
    assert names == {"send_message", "check_messages", "list_peers", "set_summary"}


# --------------------------------------------------------------------- [3]
def test_invalid_token_is_401(broker):
    bad = MiniMcpClient(broker.url, "wrong-token")
    resp = bad.rpc("initialize", {"protocolVersion": "2025-06-18"}, expect_status=401)
    assert "token_invalid" in resp["error"]["message"]


# --------------------------------------------------------------------- [4]
def test_messaging_roundtrip_and_token_attribution(client_factory):
    a = client_factory("agent-a")
    b = client_factory("agent-b")
    sent = a.call_tool("send_message",
                       {"to_id": "agent-b", "message": "こんにちは 🎌 multibyte test"})
    assert sent.get("ok") is True
    msgs = b.call_tool("check_messages")["messages"]
    assert len(msgs) == 1
    # 帰属は token 由来 (自己申告でない)。
    assert msgs[0]["from_id"] == "agent-a"
    assert msgs[0]["message"] == "こんにちは 🎌 multibyte test"
    # at-most-once drain: 2 回目は空。
    assert b.call_tool("check_messages")["messages"] == []


def test_send_to_unknown_peer_reports_not_found(client_factory):
    a = client_factory("agent-a")
    res = a.call_tool("send_message", {"to_id": "ghost", "message": "hi"})
    assert res["ok"] is False
    assert "peer_not_found" in res["error"]


# --------------------------------------------------------------------- [5]
def test_list_peers_and_set_summary(client_factory):
    a = client_factory("agent-a")
    client_factory("agent-b")
    a.call_tool("set_summary", {"summary": "smoke testing"})
    peers = a.call_tool("list_peers")["peers"]
    ids = {p["id"] for p in peers}
    assert ids == {"agent-a", "agent-b"}
    assert any(p["summary"] == "smoke testing" for p in peers)


# --------------------------------------------------------------------- [6]
def test_unknown_method_returns_jsonrpc_error(client_factory):
    a = client_factory("agent-a")
    um = a.rpc("nonexistent/method")
    assert um["error"]["code"] == -32601


def test_non_allowlisted_tool_is_iserror(client_factory):
    a = client_factory("agent-a")
    ut = a.rpc("tools/call", {"name": "spawn_agent", "arguments": {}})
    assert ut["result"].get("isError") is True


def test_missing_args_is_invalid_params(client_factory):
    a = client_factory("agent-a")
    ip = a.rpc("tools/call", {"name": "send_message", "arguments": {}})
    assert ip["error"]["code"] == -32602


# --------------------------------------------------------------------- [7]
def test_call_before_initialize_is_404(broker):
    c = MiniMcpClient(broker.url, broker.issue_token("agent-c", "agent-c", "worker"))
    resp = c.rpc("tools/list", expect_status=404)
    assert "session_invalid" in resp["error"]["message"]
    c.rpc("initialize", {"protocolVersion": "2025-06-18"})
    c.notify("notifications/initialized")
    assert "result" in c.rpc("tools/list")
    # session 不一致は 404。
    c.session_id = "bogus-session"
    resp = c.rpc("tools/list", expect_status=404)
    assert "session_invalid" in resp["error"]["message"]


# --------------------------------------------------------------------- [8]
def test_delete_revokes_session(broker, client_factory):
    b = client_factory("agent-b")
    good_sid = b.session_id
    # 不一致 DELETE は失効させず 404。
    b.session_id = "bogus-session"
    b.delete(expect_status=404)
    # 正規 session DELETE は 200 (旧実装はここでデッドロックしていた)。
    b.session_id = good_sid
    b.delete(expect_status=200)
    resp = b.rpc("tools/list", expect_status=404)
    assert "session_invalid" in resp["error"]["message"]
    # 再 initialize で復帰。
    b.rpc("initialize", {"protocolVersion": "2025-06-18"})
    assert "result" in b.rpc("tools/list")


def test_delete_drops_registration_for_delivery(broker, client_factory):
    # DELETE 後の bind は list_peers / 配送先から外れる (round 3 Major)。
    a = client_factory("agent-a")
    b = client_factory("agent-b")
    b.delete(expect_status=200)
    res = a.call_tool("send_message", {"to_id": "agent-b", "message": "after delete"})
    assert res["ok"] is False
    assert "peer_not_found" in res["error"]


# --------------------------------------------------------------------- journal
def test_queue_journal_written_to_state_dir(broker, client_factory):
    a = client_factory("agent-a")
    b = client_factory("agent-b")
    a.call_tool("send_message", {"to_id": "agent-b", "message": "x"})
    b.call_tool("check_messages")
    path = broker.state_dir / "queue.jsonl"
    assert path.exists()
    events = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]
    kinds = {e["event"] for e in events}
    assert {"broker_started", "token_issued", "agent_registered",
            "message_enqueued", "queue_drained"} <= kinds
    # ts は epoch float (broker_queue_event schema と整合)。
    assert all(isinstance(e["ts"], float) for e in events)


# ===================================================================== pane ops
# Pane-control surface (Issue C) を FakeAdapter 上で dispatch_tool 直叩きで検証する。
# HTTP は messaging テストで網羅済みなので、ここはロジック面に集中する。

def _ops(b, agent_id="d", role="dispatcher"):
    """登録済みの ops-tier bind を作る。"""
    tok = b.issue_token(agent_id, agent_id, role)
    b.register_local(tok)
    return b.get_bind(tok)


def _text(out):
    return json.loads(out["content"][0]["text"])


def test_spawn_claude_builds_interactive_argv_and_registers(tmp_path, fake_adapter):
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    fake_adapter.add_pane(active=True)
    disp = _ops(b)
    out = dispatch_tool(b, disp, "spawn_claude_pane", {
        "direction": "vertical", "name": "worker-foo", "role": "worker",
        "model": "opus", "permission_mode": "acceptEdits", "cwd": "/repo",
    })
    res = _text(out)
    assert res["agent_id"] == "worker-foo"
    spawned = fake_adapter.spawned[-1]
    argv = spawned["argv"]
    assert argv[0] == "claude" and "--mcp-config" in argv
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert spawned["cwd"] == "/repo"
    # broker が注入した mcp-config は token bearer を含む (帰属の根拠)。
    cfg = json.loads(argv[argv.index("--mcp-config") + 1])
    assert "Authorization" in cfg["mcpServers"]["org-broker"]["headers"]
    # list_panes に cwd/name/role/kind が出る (cwd parity, §3.3-4)。
    panes = _text(dispatch_tool(b, disp, "list_panes", {}))["panes"]
    rec = [p for p in panes if p["name"] == "worker-foo"][0]
    assert rec["cwd"] == "/repo" and rec["role"] == "worker" and rec["kind"] == "claude"


def test_spawn_orphan_token_not_created_on_bad_args(tmp_path, fake_adapter):
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    fake_adapter.add_pane(active=True)
    disp = _ops(b)
    before = len(b._binds)
    with pytest.raises(ToolArgError):
        dispatch_tool(b, disp, "spawn_claude_pane",
                      {"direction": "vertical", "args": ["-p"]})  # headless
    assert len(b._binds) == before  # pre-validate で token を作っていない
    assert fake_adapter.spawned == []


def test_resolve_target_three_ways(tmp_path, fake_adapter):
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    h0 = fake_adapter.add_pane(active=True)
    disp = _ops(b)
    dispatch_tool(b, disp, "spawn_claude_pane", {"direction": "vertical", "name": "alpha"})
    h1 = fake_adapter.spawned[-1]["handle"]
    assert b.resolve_target("alpha") == h1        # stable name
    assert b.resolve_target(str(h1)) == h1        # 全桁数字 → handle
    assert b.resolve_target("focused") == h0      # focused
    assert b.resolve_target("nope") is None


def test_spawn_codex_via_dispatch_rejects_exec_but_allows_tui(tmp_path, fake_adapter):
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    fake_adapter.add_pane(active=True)
    sec = _ops(b, "s", "secretary")
    with pytest.raises(ToolArgError):
        dispatch_tool(b, sec, "spawn_codex_pane",
                      {"direction": "vertical", "args": ["exec", "ls"]})
    # 拒否時に orphan token / spawn を残さない。
    assert fake_adapter.spawned == []
    out = dispatch_tool(b, sec, "spawn_codex_pane", {"direction": "vertical", "name": "cdx"})
    assert _text(out)["agent_id"] == "cdx"
    assert fake_adapter.spawned[-1]["argv"][0] == "codex"


def test_spawn_generic_secretary_only_no_token(tmp_path, fake_adapter):
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    fake_adapter.add_pane(active=True)
    sec = _ops(b, "s", "secretary")
    out = dispatch_tool(b, sec, "spawn_pane",
                        {"direction": "horizontal", "command": "watch ls", "name": "watcher"})
    assert _text(out)["name"] == "watcher"
    h = fake_adapter.spawned[-1]["handle"]
    assert b._meta_for(h)["token"] is None        # token 非注入 (非 org spawn)
    assert "watch ls" in fake_adapter.spawned[-1]["argv"]


def test_set_pane_identity_three_state_keeps_auth_role(tmp_path, fake_adapter):
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    fake_adapter.add_pane(active=True)
    disp = _ops(b)
    dispatch_tool(b, disp, "spawn_claude_pane",
                  {"direction": "vertical", "name": "w1", "role": "worker"})
    h = fake_adapter.spawned[-1]["handle"]
    tok = b._meta_for(h)["token"]
    # str=設定
    out = dispatch_tool(b, disp, "set_pane_identity", {"target": "w1", "role": "reviewer"})
    assert _text(out)["role"] == "reviewer"
    # auth tier (auth_role) は不変 — 表示 role 変更で権限昇格しない (§3.3-5)。
    assert b._binds[tok].auth_role == "worker"
    assert b._binds[tok].role == "reviewer"
    # null=クリア
    out = dispatch_tool(b, disp, "set_pane_identity", {"target": "w1", "role": None})
    assert _text(out)["role"] is None
    # omit=据置 — name は触っていないので w1 のまま (まだ name で引ける)。
    assert b.resolve_target("w1") == h


def test_set_pane_identity_name_collision_is_invalid_params(tmp_path, fake_adapter):
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    fake_adapter.add_pane(active=True)
    disp = _ops(b)
    dispatch_tool(b, disp, "spawn_claude_pane", {"direction": "vertical", "name": "aa"})
    dispatch_tool(b, disp, "spawn_claude_pane", {"direction": "vertical", "name": "bb"})
    with pytest.raises(ToolArgError):
        dispatch_tool(b, disp, "set_pane_identity", {"target": "bb", "name": "aa"})


def test_close_pane_revokes_token_and_emits_event(tmp_path, fake_adapter):
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    fake_adapter.add_pane(active=True)   # keep pane count > 1
    disp = _ops(b)
    dispatch_tool(b, disp, "spawn_claude_pane", {"direction": "vertical", "name": "w"})
    h = fake_adapter.spawned[-1]["handle"]
    tok = b._meta_for(h)["token"]
    out = dispatch_tool(b, disp, "close_pane", {"target": "w"})
    assert _text(out)["closed"] == h
    assert h in fake_adapter.killed
    assert b._binds[tok].revoked is True
    assert b._meta_for(h) is None


def test_close_last_pane_is_guarded(tmp_path, fake_adapter):
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    fake_adapter.add_pane(active=True)
    disp = _ops(b)
    out = dispatch_tool(b, disp, "close_pane", {"target": "focused"})
    assert out["isError"] is True
    assert "[last_pane]" in out["content"][0]["text"]


def test_poll_events_baseline_then_emit_and_filter(tmp_path, fake_adapter):
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    fake_adapter.add_pane(active=True)
    disp = _ops(b)
    # 初回は「今以降」: 履歴 replay なし。timeout 0 で即 return。
    first = b.poll_events(None, 0, None)
    assert first["events"] == []
    cur = first["next_since"]
    dispatch_tool(b, disp, "spawn_claude_pane", {"direction": "vertical", "name": "w"})
    drained = b.poll_events(cur, 0, None)
    assert any(e["type"] == "pane_started" for e in drained["events"])
    # types フィルタは返却を絞るが cursor は前進する。
    filtered = b.poll_events(cur, 0, ["pane_exited"])
    assert filtered["events"] == []


def test_send_keys_enter_supported_others_flagged(tmp_path, fake_adapter):
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    h0 = fake_adapter.add_pane(active=True)
    disp = _ops(b)
    out = dispatch_tool(b, disp, "send_keys",
                        {"target": "focused", "text": "y", "enter": True})
    assert _text(out)["ok"] is True
    assert "y" in fake_adapter.get_text(h0)
    # 未知キー名は -32602 (renga vocab parity)。
    with pytest.raises(ToolArgError):
        dispatch_tool(b, disp, "send_keys", {"target": "focused", "keys": ["Hyper+Z"]})
    # 有効だが現 adapter 非対応キー (Shift+Tab) は既知制限として明示エラー。
    out = dispatch_tool(b, disp, "send_keys", {"target": "focused", "keys": ["Shift+Tab"]})
    assert out["isError"] is True
    assert "[key_unsupported]" in out["content"][0]["text"]


def test_inspect_pane_text_and_grid(tmp_path, fake_adapter):
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    h0 = fake_adapter.add_pane(active=True)
    fake_adapter._screens[h0] = "line1\nline2\nline3"
    disp = _ops(b)
    out = dispatch_tool(b, disp, "inspect_pane", {"target": "focused", "lines": 2})
    assert out["structuredContent"]["text"] == "line2\nline3"
    out = dispatch_tool(b, disp, "inspect_pane", {"target": "focused", "format": "grid"})
    assert out["structuredContent"]["grid"][0]["text"] == "line1"


def test_spawn_requires_backend(tmp_path):
    b = Broker(state_dir=tmp_path, adapter=None)
    disp = _ops(b)
    out = dispatch_tool(b, disp, "spawn_claude_pane", {"direction": "vertical"})
    assert out["isError"] is True
    assert "[no_backend]" in out["content"][0]["text"]


def test_spawn_child_auth_role_capped_by_caller_tier(tmp_path, fake_adapter):
    """Blocker 対応: 表示 role の自己申告で tier を昇格できない (caller tier 上限)。"""
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    fake_adapter.add_pane(active=True)
    disp = _ops(b, "d", "dispatcher")
    # dispatcher が role="secretary" を申告 → auth_role は dispatcher 止まり。
    dispatch_tool(b, disp, "spawn_claude_pane",
                  {"direction": "vertical", "name": "x", "role": "secretary"})
    tok = b._meta_for(fake_adapter.spawned[-1]["handle"])["token"]
    assert b._binds[tok].auth_role == "dispatcher"   # 昇格していない
    assert b._binds[tok].role == "secretary"          # 表示は要求どおり
    # role 未指定は messaging tier (worker)。
    dispatch_tool(b, disp, "spawn_claude_pane", {"direction": "vertical", "name": "y"})
    tok2 = b._meta_for(fake_adapter.spawned[-1]["handle"])["token"]
    assert b._binds[tok2].auth_role == "worker"
    # secretary は dispatcher tier を子に渡せる。
    sec = _ops(b, "s", "secretary")
    dispatch_tool(b, sec, "spawn_claude_pane",
                  {"direction": "vertical", "name": "z", "role": "dispatcher"})
    tok3 = b._meta_for(fake_adapter.spawned[-1]["handle"])["token"]
    assert b._binds[tok3].auth_role == "dispatcher"


def test_spawn_rejects_unknown_explicit_target(tmp_path, fake_adapter):
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    fake_adapter.add_pane(active=True)
    disp = _ops(b)
    out = dispatch_tool(b, disp, "spawn_claude_pane",
                        {"direction": "vertical", "target": "ghost"})
    assert out["isError"] is True
    assert "[pane_not_found]" in out["content"][0]["text"]
    assert fake_adapter.spawned == []   # 解決前に弾く (orphan を作らない)


def test_set_pane_identity_null_name_clears_bind(tmp_path, fake_adapter):
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    fake_adapter.add_pane(active=True)
    disp = _ops(b)
    dispatch_tool(b, disp, "spawn_claude_pane", {"direction": "vertical", "name": "w"})
    tok = b._meta_for(fake_adapter.spawned[-1]["handle"])["token"]
    dispatch_tool(b, disp, "set_pane_identity", {"target": "w", "name": None})
    assert b._binds[tok].name == ""         # bind 側 name もクリア (Minor 対応)
    assert b.resolve_target("w") is None     # 旧名で解決され続けない


def test_spawn_name_reservation_promotes_to_meta(tmp_path, fake_adapter):
    """予約は spawn 成功後 _register_pane が meta へ確定昇格し、予約集合に残さない。
    確定後の同名 spawn は name_taken (in-flight 窓も meta も両方で重複を弾く)。"""
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    fake_adapter.add_pane(active=True)
    disp = _ops(b)
    dispatch_tool(b, disp, "spawn_claude_pane", {"direction": "vertical", "name": "dup"})
    assert "dup" not in b._reserved_names          # 予約は meta へ昇格済み
    out = dispatch_tool(b, disp, "spawn_claude_pane", {"direction": "vertical", "name": "dup"})
    assert out["isError"] is True
    assert "[name_taken]" in out["content"][0]["text"]


def test_spawn_failure_releases_name_reservation(tmp_path, fake_adapter):
    """spawn (adapter I/O) 失敗時は except 経路で予約を解放し、同名を再利用できる。"""
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    fake_adapter.add_pane(active=True)
    disp = _ops(b)
    orig = fake_adapter.spawn

    def boom(*a, **k):
        raise RuntimeError("adapter spawn failed")

    fake_adapter.spawn = boom
    with pytest.raises(RuntimeError):
        dispatch_tool(b, disp, "spawn_claude_pane", {"direction": "vertical", "name": "r"})
    assert "r" not in b._reserved_names             # 失敗時に解放されている
    # 発行済み token も revoke され配送対象に残らない (部分 spawn のロールバック)。
    assert all(bd.revoked for bd in b._binds.values() if bd.agent_id == "r")
    fake_adapter.spawn = orig
    out = dispatch_tool(b, disp, "spawn_claude_pane", {"direction": "vertical", "name": "r"})
    assert _text(out)["agent_id"] == "r"            # 同名で再 spawn 可能


def test_spawn_target_must_be_string(tmp_path, fake_adapter):
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    fake_adapter.add_pane(active=True)
    disp = _ops(b)
    with pytest.raises(ToolArgError):
        dispatch_tool(b, disp, "spawn_claude_pane", {"direction": "vertical", "target": 123})
