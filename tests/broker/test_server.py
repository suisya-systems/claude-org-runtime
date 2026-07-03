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

from .conftest import FakeAdapter, MiniMcpClient


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


def _ops_with_cwd(b, cwd, agent_id="d", role="dispatcher"):
    """cwd を持つ ops-tier bind を作る (caller pane の cwd を模す)。"""
    tok = b.issue_token(agent_id, agent_id, role, cwd=cwd)
    b.register_local(tok)
    return b.get_bind(tok)


def test_spawn_relative_cwd_anchors_on_caller_cwd(tmp_path, fake_adapter):
    """Issue #61: relative cwd は caller pane の cwd を base に解決され、解決後の
    absolute が adapter.spawn と list_panes に伝わる (daemon base で再解決させない)。"""
    import os as _os

    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    fake_adapter.add_pane(active=True)
    base = _os.path.join(_os.sep, "root", "dogfood", "claude-org-ja")
    disp = _ops_with_cwd(b, base)
    out = dispatch_tool(b, disp, "spawn_claude_pane", {
        "direction": "vertical", "name": "disp-child", "cwd": ".dispatcher",
    })
    res = _text(out)
    expected = _os.path.normpath(_os.path.join(base, ".dispatcher"))
    # adapter は解決済み absolute を受け取る (relative を素通ししない)。
    assert fake_adapter.spawned[-1]["cwd"] == expected
    # 結果 dict / list_panes にも解決済み cwd が出る (cwd parity)。
    assert res["cwd"] == expected
    panes = _text(dispatch_tool(b, disp, "list_panes", {}))["panes"]
    rec = [p for p in panes if p["name"] == "disp-child"][0]
    assert rec["cwd"] == expected
    assert "dogfood" in rec["cwd"]  # 本 Issue: dogfood/ が落ちない


def test_spawn_relative_cwd_unknown_caller_rejected(tmp_path, fake_adapter):
    """Issue #61: caller cwd 不明 (cwd=None bind) + relative cwd は決定的に拒否。
    token も pane も作らない (黙って daemon base に落とさない)。"""
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    fake_adapter.add_pane(active=True)
    disp = _ops(b)  # cwd 無し (= 論理 root pane の cwd null を模す)
    before = len(b._binds)
    with pytest.raises(ToolArgError) as ei:
        dispatch_tool(b, disp, "spawn_claude_pane", {
            "direction": "vertical", "name": "orphan", "cwd": ".dispatcher",
        })
    assert "cwd_unanchored" in str(ei.value)
    assert len(b._binds) == before        # orphan token を作らない
    assert fake_adapter.spawned == []     # spawn にも到達しない


def test_spawn_absolute_cwd_unchanged_regardless_of_caller(tmp_path, fake_adapter):
    """Issue #61: absolute cwd は caller cwd に関わらず無変換で透過する。"""
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    fake_adapter.add_pane(active=True)
    disp = _ops_with_cwd(b, "/some/caller/base")
    out = dispatch_tool(b, disp, "spawn_claude_pane", {
        "direction": "vertical", "name": "abs-child", "cwd": "/repo",
    })
    assert _text(out)["cwd"] == "/repo"
    assert fake_adapter.spawned[-1]["cwd"] == "/repo"


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


@pytest.mark.parametrize("handle", ["%3", "w1:p2"])
def test_resolve_target_nonnumeric_managed_handle(tmp_path, fake_adapter, handle):
    """非数字 managed handle (tmux "%N" / Herdr "wN:pN") 直指定を解決する (Issue
    #100)。native handle 型 (str) を保って返す。"""
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    fake_adapter.add_pane(active=False, handle=handle)
    b._register_pane(handle, agent_id="a1", name=None, role="worker",
                     cwd=None, kind="claude", token=None)
    assert b.resolve_target(handle) == handle
    # 全桁数字契約は不変: '%3' は数字 id 3 とは別物で、混同しない。
    assert b.resolve_target("3") is None


def test_resolve_target_name_and_handle_do_not_collide(tmp_path, fake_adapter):
    """stable name ([A-Za-z0-9_-]) と非数字 handle (':' / '%' を含む) は文字集合が
    交わらないため、handle 直指定は name 解決を shadow しない — 同一 pane が
    name / handle 双方で addressable になる。"""
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    fake_adapter.add_pane(active=False, handle="%7")
    b._register_pane("%7", agent_id="a1", name="alpha", role="worker",
                     cwd=None, kind="claude", token=None)
    assert b.resolve_target("alpha") == "%7"   # stable name (既存の優先経路)
    assert b.resolve_target("%7") == "%7"       # 非数字 managed handle (追加経路)
    # 未登録の handle 風文字列は解決しない (誤解決しない)。
    assert b.resolve_target("%99") is None


def test_org_down_closes_nonnumeric_handle_panes(tmp_path, fake_adapter):
    """org down 統合経路: launcher は list_panes の native handle id をそのまま
    close_pane に渡す。非数字 handle (tmux "%N") でも close できることを、実際に
    list_panes_view の id → close_pane_target を通して確認する (Issue #100 の実害)。
    """
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    for h in ("%3", "%4"):
        fake_adapter.add_pane(active=False, handle=h)
        b._register_pane(h, agent_id=f"a{h}", name=None, role="worker",
                         cwd=None, kind="claude", token=None)
    ids = [p["id"] for p in b.list_panes_view()]
    assert set(ids) == {"%3", "%4"}
    # launcher は str(pane.get("id")) を target に渡す (最後の 1 枚は last_pane 保護)。
    res = b.close_pane_target(str(ids[0]))
    assert res.get("isError") is not True
    assert _text(res)["closed"] == ids[0]
    assert ids[0] in fake_adapter.killed


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


def test_send_keys_full_vocabulary_on_capable_backend(tmp_path, fake_adapter):
    # FakeAdapter は full raw-key vocabulary を宣言する (tmux / Herdr 相当)。
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
    # かつて非対応だった Shift+Tab / 矢印 / Esc / Ctrl+A は full backend で送出される
    # (期待値反転)。canonical 化 (Shift+Tab -> backtab, Esc -> esc) も観測できる。
    out = dispatch_tool(
        b, disp, "send_keys",
        {"target": "focused", "keys": ["Shift+Tab", "Up", "Esc", "Ctrl+A"]},
    )
    assert _text(out)["ok"] is True
    screen = fake_adapter.get_text(h0)
    for marker in ("<backtab>", "<up>", "<esc>", "<ctrl+a>"):
        assert marker in screen


def test_send_keys_all_or_nothing_preflight_on_subset_backend(tmp_path):
    # WezTerm 相当の subset backend: Enter / Ctrl+C のみ emit 可能。
    fake = FakeAdapter()
    fake.supported_named_keys = frozenset({"enter", "ctrl+c"})
    b = Broker(state_dir=tmp_path, adapter=fake)
    h0 = fake.add_pane(active=True)
    disp = _ops(b)
    # 未対応キーが 1 つでも混じれば text を送る前に全体を拒否する (all-or-nothing)。
    out = dispatch_tool(
        b, disp, "send_keys",
        {"target": "focused", "text": "abc", "keys": ["Up"]},
    )
    assert out["isError"] is True
    assert "[key_unsupported]" in out["content"][0]["text"]
    # text すら送られていない (preflight が type_text より前で弾く)。
    assert fake.get_text(h0) == ""
    # subset 内 (Enter) は成功する。
    out = dispatch_tool(b, disp, "send_keys", {"target": "focused", "enter": True})
    assert _text(out)["ok"] is True


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


# ============================================ logical pane (root secretary, #57)
# 窓口 (人間駆動の root pane) を pane 登録簿に first-class な論理ペインとして載せ、
# list_panes 出現 / close_pane の [last_pane] 誤判定解消を固定する。

def _secretary_with_logical_pane(b):
    """登録済み secretary bind を作り、論理ペインとして pane 登録簿に載せる。"""
    tok = b.issue_token("manual-test", "manual-test", "secretary")
    b.register_local(tok)
    b.register_logical_pane(tok)
    return tok, b.get_bind(tok)


def test_register_logical_pane_appears_in_list_panes_and_suppresses_nudge(
    tmp_path, fake_adapter
):
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    tok, sec = _secretary_with_logical_pane(b)
    # bind.pane_id は None のまま — PTY ナッジを構造的に抑止 (人間は check_messages)。
    assert sec.pane_id is None
    # 実 adapter pane が 1 つも無くても、窓口が first-class entry として出る。
    panes = _text(dispatch_tool(b, sec, "list_panes", {}))["panes"]
    me = [p for p in panes if p["id"] == "manual-test"]
    assert len(me) == 1
    assert me[0]["role"] == "secretary"
    assert me[0]["name"] == "manual-test"
    assert me[0]["focused"] is False
    # 論理 handle は bind.name なので resolve_target も既存 name ブランチで引ける。
    assert b.resolve_target("manual-test") == "manual-test"


def test_secretary_logical_pane_lets_close_child_escape_last_pane(tmp_path, fake_adapter):
    """Issue #57 回帰: 窓口 (論理) + 子 1 つの状態で、子を [last_pane] 誤判定
    されずに閉じられる。事前 adapter pane を作らないので spawn 後の実ペインは
    子 1 つだけ — 論理ペインが数えられなければ close は [last_pane] になる。"""
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    tok, sec = _secretary_with_logical_pane(b)
    dispatch_tool(b, sec, "spawn_claude_pane", {"direction": "vertical", "name": "child"})
    assert len(fake_adapter.list_panes()) == 1   # 実ペインは子のみ
    out = dispatch_tool(b, sec, "close_pane", {"target": "child"})
    assert "isError" not in out, out
    res = _text(out)
    assert res["ok"] is True
    h = fake_adapter.spawned[-1]["handle"]
    assert res["closed"] == h
    assert h in fake_adapter.killed


def test_close_only_child_without_logical_secretary_is_still_guarded(
    tmp_path, fake_adapter
):
    """対照: 論理ペイン未登録 (窓口なし) なら従来どおり [last_pane]。
    回帰の効果が『論理 pane を数える』ことに由来すると固定する。"""
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    disp = _ops(b)  # dispatcher、論理ペイン登録なし
    dispatch_tool(b, disp, "spawn_claude_pane", {"direction": "vertical", "name": "only"})
    assert len(fake_adapter.list_panes()) == 1
    out = dispatch_tool(b, disp, "close_pane", {"target": "only"})
    assert out["isError"] is True
    assert "[last_pane]" in out["content"][0]["text"]


def test_close_pane_rejects_logical_secretary(tmp_path, fake_adapter):
    """窓口自身を close_pane する操作は [logical_pane] で拒否する
    (存在しない adapter handle を kill しに行かせない)。"""
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    tok, sec = _secretary_with_logical_pane(b)
    # 子を 1 つ作り「最後の pane」条件を外す (last_pane と logical 拒否を分離)。
    dispatch_tool(b, sec, "spawn_claude_pane", {"direction": "vertical", "name": "child"})
    out = dispatch_tool(b, sec, "close_pane", {"target": "manual-test"})
    assert out["isError"] is True
    assert "[logical_pane]" in out["content"][0]["text"]
    # 論理ペインは登録簿に残り、bind も revoke されない。
    assert b._pane_meta.get("manual-test") is not None
    assert b.get_bind(tok) is not None
    assert "manual-test" not in fake_adapter.killed


def test_logical_pane_coexists_with_real_panes_in_list(tmp_path, fake_adapter):
    """論理ペインと spawn 済み実 adapter pane が list_panes に重複なく共存する
    (isolated-socket backend モデル: adapter は broker 管理 pane のみ見せる)。"""
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    tok, sec = _secretary_with_logical_pane(b)
    dispatch_tool(b, sec, "spawn_claude_pane", {"direction": "vertical", "name": "child"})
    panes = _text(dispatch_tool(b, sec, "list_panes", {}))["panes"]
    ids = [p["id"] for p in panes]
    assert "manual-test" in ids                       # 論理ペイン
    child_h = fake_adapter.spawned[-1]["handle"]
    assert child_h in ids                             # 実ペイン
    assert len(ids) == len(set(ids)) == 2             # 重複なし


def test_logical_pane_on_global_mux_backend_does_not_overpermit_close(tmp_path):
    """global-mux backend (wezterm, isolated_session=False) のシミュレーション:
    adapter が窓口の実 pane を匿名 (meta 無し) entry として返すケースを再現する。

    既知制限として list_panes は「匿名の実 pane」+「logical entry」の二重表示に
    なる (root 実 pane との相関は取れないため。実ペイン化はスコープ外)。重要なのは
    close_pane が over-permit しないこと: global-mux では論理ペインを last-pane
    計上しないため、未管理の実 pane (= broker の host pane 相当) を単独で閉じようと
    すると従来どおり [last_pane] で守られる。"""
    glob = FakeAdapter(isolated_session=False)
    b = Broker(state_dir=tmp_path, adapter=glob)
    root_real = glob.add_pane(active=True)   # 窓口の実 pane (匿名)
    tok, sec = _secretary_with_logical_pane(b)
    # 既知制限: 匿名実 pane と logical entry が二重に並ぶ。
    panes = _text(dispatch_tool(b, sec, "list_panes", {}))["panes"]
    ids = [p["id"] for p in panes]
    assert root_real in ids and "manual-test" in ids
    # 未管理 (broker 非 spawn) の実 pane を単独で閉じる → global-mux では論理を
    # 計上しないので [last_pane] で守られる (over-permit 退行が無いことの固定)。
    out = dispatch_tool(b, sec, "close_pane", {"target": str(root_real)})
    assert out["isError"] is True
    assert "[last_pane]" in out["content"][0]["text"]
    assert root_real not in glob.killed
    # 一方、子を足して 2 pane あれば、broker 管理の子は (実 pane 数だけで) 閉じられる。
    dispatch_tool(b, sec, "spawn_claude_pane", {"direction": "vertical", "name": "child"})
    child_h = glob.spawned[-1]["handle"]
    out = dispatch_tool(b, sec, "close_pane", {"target": "child"})
    assert "isError" not in out, out
    assert child_h in glob.killed


def test_logical_pane_on_global_mux_does_not_empty_when_root_pane_gone(tmp_path):
    """Codex review round 2 Major (残経路) 対応: global-mux で窓口の実 pane が
    out-of-band に消え、論理ペインだけが残った状態。

    この時 adapter.list_panes() は子 1 つだけを見せる。isolated_session=False の
    ため論理ペインを last-pane 計上せず、最後の実 pane (子) を閉じて mux を空に
    する over-permit を起こさない ([last_pane] で守る)。isolated backend なら
    同じ状況で窓口を +1 して閉じられる点と対照的 (= isolated_session で分岐する
    のが正しいモデルであることの固定)。"""
    glob = FakeAdapter(isolated_session=False)
    b = Broker(state_dir=tmp_path, adapter=glob)
    tok, sec = _secretary_with_logical_pane(b)
    # 子を 1 つ spawn (窓口の実 pane は最初から add していない = out-of-band 消失後を模す)。
    dispatch_tool(b, sec, "spawn_claude_pane", {"direction": "vertical", "name": "child"})
    assert len(glob.list_panes()) == 1   # 実ペインは子のみ (窓口の実 pane は不在)
    out = dispatch_tool(b, sec, "close_pane", {"target": "child"})
    assert out["isError"] is True
    assert "[last_pane]" in out["content"][0]["text"]
    child_h = glob.spawned[-1]["handle"]
    assert child_h not in glob.killed     # mux を空にしない


# ================================================ self-termination reap (#103)
# broker が spawn した pane が自己終了 (プロセス自死) した際、registry を信じる入口
# (_reserve_name / resolve_target) で adapter snapshot による opportunistic reap を
# 行い、name binding / token / delivery cred / delivery state / 未配達行を close_pane
# と共通の helper で掃除することを固定する。幽霊 binding ([name_taken]) の除去が本丸。

def _managed_meta(b, handle):
    return b._meta_for(handle)


def test_self_terminated_pane_reaped_on_respawn_frees_name(tmp_path, fake_adapter):
    """核心 (#103): 自己終了した managed pane の name は次の同名 spawn で解放される。

    reap 前は issue_token(unique=True) が未 revoke bind を見て [name_taken] を返し、
    幽霊 binding で同名 re-spawn が永久に塞がる。入口 reap でこれを断つ。"""
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    fake_adapter.add_pane(active=True)   # host pane (reap 対象外)
    disp = _ops(b)
    dispatch_tool(b, disp, "spawn_claude_pane", {"direction": "vertical", "name": "w"})
    h1 = fake_adapter.spawned[-1]["handle"]
    tok1 = b._meta_for(h1)["token"]
    # pane が自己終了する (broker は kill_pane していない = self.killed に載らない)。
    fake_adapter.terminate(h1)
    assert h1 not in fake_adapter.killed
    # 同名で再 spawn: 入口 (_reserve_name / split target 解決) の reap で幽霊 binding が
    # 掃除され、[name_taken] にならず成功する。
    out = dispatch_tool(b, disp, "spawn_claude_pane", {"direction": "vertical", "name": "w"})
    assert "isError" not in out or out.get("isError") is not True
    res = _text(out)
    assert res["agent_id"] == "w"
    h2 = fake_adapter.spawned[-1]["handle"]
    assert h2 != h1
    # 旧 binding は revoke され、旧 meta は落ちている。新 meta は生きている。
    assert b._binds[tok1].revoked is True
    assert b._meta_for(h1) is None
    assert b._meta_for(h2) is not None


def test_reap_full_cleanup_via_resolve_entry(tmp_path, fake_adapter):
    """入口 reap は close_pane と同じ full cleanup を行う: meta pop / token revoke /
    delivery cred revoke / delivery state reset / 未配達行 discard。"""
    from claude_org_runtime.broker.store import QueueRow

    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    fake_adapter.add_pane(active=True)   # focused host pane
    disp = _ops(b)
    dispatch_tool(b, disp, "spawn_claude_pane", {"direction": "vertical", "name": "w"})
    h = fake_adapter.spawned[-1]["handle"]
    agent_id = b._meta_for(h)["agent_id"]
    tok = b._meta_for(h)["token"]
    # channel sidecar の delivery cred + 未配達 row + PULL flip 済み delivery state を模す。
    cred = b.issue_delivery_cred(agent_id)
    b._rows["r1"] = QueueRow(id="r1", to_id=agent_id, entry={"message": "hi"})
    b._delivery_modes[agent_id] = "PULL"
    b._epochs[agent_id] = 3
    # pane 自己終了 -> 入口 (resolve_target) が opportunistic reap する。
    fake_adapter.terminate(h)
    assert b.resolve_target("focused") is not None   # host pane 解決 = reap を駆動
    assert b._meta_for(h) is None                    # meta pop
    assert b._binds[tok].revoked is True             # token full revoke
    assert b.get_bind(cred) is None                  # delivery cred revoke
    assert "r1" not in b._rows                        # 未配達行 discard
    assert agent_id not in b._delivery_modes          # delivery state reset
    assert agent_id not in b._epochs


def test_reap_emits_pane_exited_and_journals_pane_reaped(tmp_path, fake_adapter):
    """reap は close と同じ pane_exited event を emit しつつ、journal は pane_reaped で
    区別する (dispatcher の poll_events(pane_exited) 依存に合わせ event type は統一、
    検知経路は journal 語彙で分離)。"""
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    fake_adapter.add_pane(active=True)
    disp = _ops(b)
    dispatch_tool(b, disp, "spawn_claude_pane", {"direction": "vertical", "name": "w"})
    h = fake_adapter.spawned[-1]["handle"]
    agent_id = b._meta_for(h)["agent_id"]
    base = b.poll_events(None, 0, None)["next_since"]
    fake_adapter.terminate(h)
    # 入口 (_reserve_name) 経由で reap を駆動する別 spawn。
    dispatch_tool(b, disp, "spawn_claude_pane", {"direction": "vertical", "name": "other"})
    evs = b.poll_events(base, 0, ["pane_exited"])["events"]
    exited = [e for e in evs if e.get("pane_id") == h]
    assert len(exited) == 1
    assert exited[0]["agent_id"] == agent_id
    # journal は pane_reaped (pane_closed ではない)。
    path = b.state_dir / "queue.jsonl"
    events = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]
    reaped = [e for e in events if e["event"] == "pane_reaped" and e.get("pane_id") == h]
    assert len(reaped) == 1
    assert reaped[0]["agent_id"] == agent_id
    assert not any(e["event"] == "pane_closed" and e.get("pane_id") == h for e in events)


def test_live_managed_pane_not_reaped(tmp_path, fake_adapter):
    """生きている managed pane は入口 reap で掃除されない (false-positive reap 回避)。"""
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    fake_adapter.add_pane(active=True)
    disp = _ops(b)
    dispatch_tool(b, disp, "spawn_claude_pane", {"direction": "vertical", "name": "live"})
    h = fake_adapter.spawned[-1]["handle"]
    tok = b._meta_for(h)["token"]
    # reap を何度か駆動しても生存 pane は無傷。
    b.resolve_target("focused")
    dispatch_tool(b, disp, "spawn_claude_pane", {"direction": "vertical", "name": "sib"})
    assert b._meta_for(h) is not None
    assert b._binds[tok].revoked is False
    assert h not in fake_adapter.killed


def test_logical_pane_is_not_reaped(tmp_path, fake_adapter):
    """logical pane (human-driven 窓口) は adapter 実体を持たないため reap 対象外。

    adapter snapshot に永遠に出ないので、reap すると窓口が消える。除外を固定する。"""
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    tok, sec = _secretary_with_logical_pane(b)
    # 子を spawn (= _reserve_name で reap 駆動)。窓口の logical meta は残る。
    dispatch_tool(b, sec, "spawn_claude_pane", {"direction": "vertical", "name": "child"})
    assert b._pane_meta.get("manual-test") is not None
    assert b._binds[tok].revoked is False
    # resolve_target 経由でも reap されない。
    assert b.resolve_target("manual-test") == "manual-test"
    assert b._pane_meta.get("manual-test") is not None


def test_close_pane_still_full_cleanup_after_helper_refactor(tmp_path, fake_adapter):
    """close_pane を共通 helper に寄せた後も従来どおり full cleanup + pane_closed
    journal を行う (reap への切り出しで close 経路を退行させない)。"""
    from claude_org_runtime.broker.store import QueueRow

    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    fake_adapter.add_pane(active=True)
    disp = _ops(b)
    dispatch_tool(b, disp, "spawn_claude_pane", {"direction": "vertical", "name": "w"})
    h = fake_adapter.spawned[-1]["handle"]
    agent_id = b._meta_for(h)["agent_id"]
    tok = b._meta_for(h)["token"]
    cred = b.issue_delivery_cred(agent_id)
    b._rows["r1"] = QueueRow(id="r1", to_id=agent_id, entry={"message": "hi"})
    out = dispatch_tool(b, disp, "close_pane", {"target": "w"})
    assert _text(out)["closed"] == h
    assert h in fake_adapter.killed          # 明示 close は kill する (reap と違う点)
    assert b._binds[tok].revoked is True
    assert b._meta_for(h) is None
    assert b.get_bind(cred) is None
    assert "r1" not in b._rows
    path = b.state_dir / "queue.jsonl"
    events = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]
    assert any(e["event"] == "pane_closed" and e.get("pane_id") == h for e in events)
    assert not any(e["event"] == "pane_reaped" and e.get("pane_id") == h for e in events)


def test_reap_of_tokenless_generic_pane_spares_live_namesake_delivery(tmp_path):
    """generic spawn_pane (token=None) の自己終了 reap は、同名の bind-only live
    agent (admin-mint された channel agent 等) の delivery state を巻き込まない。

    generic pane は channel sidecar / delivery cred / queue 行を持たず、その meta
    agent_id は別 live agent と名前空間非交差で衝突しうる。token 無し pane の掃除で
    無関係 agent の配送を壊さないことを固定する (Codex review P2)。"""
    from claude_org_runtime.broker.store import QueueRow

    fake_adapter = FakeAdapter()
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    fake_adapter.add_pane(active=True)   # focused host pane
    sec = _ops(b, agent_id="sec", role="secretary")
    # live な bind-only channel agent "foo" (delivery cred + 未配達行 + PULL state)。
    b.issue_token("foo", "foo", "worker")
    cred = b.issue_delivery_cred("foo")
    b._rows["r1"] = QueueRow(id="r1", to_id="foo", entry={"message": "keep me"})
    b._delivery_modes["foo"] = "PULL"
    # 同名の generic pane を spawn (token=None、名前空間は _pane_meta 側のみ)。
    dispatch_tool(b, sec, "spawn_pane", {"direction": "vertical", "name": "foo"})
    h = fake_adapter.spawned[-1]["handle"]
    assert b._meta_for(h)["token"] is None
    # generic pane が自己終了 -> 入口 reap。
    fake_adapter.terminate(h)
    assert b.resolve_target("focused") is not None   # reap を駆動
    assert b._meta_for(h) is None                    # 死んだ generic pane の meta は落ちる
    # だが同名 live agent "foo" の delivery state は無傷。
    assert b.get_bind(cred) is not None              # delivery cred は revoke されない
    assert "r1" in b._rows                            # 未配達行は残る
    assert b._delivery_modes.get("foo") == "PULL"    # delivery state は維持


# ============================ deterministic pane-unit reap model (Issue #109)
# Herdr の eventually consistent snapshot (boot 中 / ラグで生 pane が一時欠落) が、
# 「snapshot に現れない = 物理消滅」と即断する旧 reap で誤 reap され、_cleanup_pane が
# 物理 close を呼ばない設計と相まって孤児 TUI を残す複合 Blocker を、pane 単位の
# 決定的モデル (age + 連続欠落) と物理 close 検証で断つことを固定する。


def test_transient_snapshot_miss_does_not_reap_live_pane(tmp_path):
    """真因B: eventually consistent snapshot から一時的に欠落した生 pane は reap
    されない (>= min_missing 連続欠落が要る)。snapshot 復帰で欠落 streak はリセット。"""
    adapter = FakeAdapter(reap_min_missing_snapshots=2)
    b = Broker(state_dir=tmp_path, adapter=adapter)
    adapter.add_pane(active=True)   # host pane
    disp = _ops(b)
    dispatch_tool(b, disp, "spawn_claude_pane", {"direction": "vertical", "name": "w"})
    h = adapter.spawned[-1]["handle"]
    tok = b._meta_for(h)["token"]
    # snapshot から一時消失 (物理的にはまだ生存 = pane_exists True)。
    adapter.desync_hide(h)
    b.resolve_target("focused")     # 欠落 1 回目 -> 閾値未満で生存
    assert b._meta_for(h) is not None
    assert b._binds[tok].revoked is False
    assert h not in adapter.killed
    assert b._meta_for(h)["missing_count"] == 1
    # snapshot が追いつく -> 欠落 streak はリセット (連続性を担保)。
    adapter.desync_show(h)
    b.resolve_target("focused")
    assert b._meta_for(h)["missing_count"] == 0
    assert b._meta_for(h)["missing_since"] is None


def test_young_pane_not_reaped_despite_misses(tmp_path):
    """真因B (age gate): spawn 直後の若い pane は連続欠落しても age 未達で reap されない
    (boot 中の一時欠落保護)。min_missing=1 でも age が守る。"""
    adapter = FakeAdapter(reap_min_age_seconds=30.0, reap_min_missing_snapshots=1)
    b = Broker(state_dir=tmp_path, adapter=adapter)
    adapter.add_pane(active=True)
    disp = _ops(b)
    dispatch_tool(b, disp, "spawn_claude_pane", {"direction": "vertical", "name": "boot"})
    h = adapter.spawned[-1]["handle"]
    adapter.desync_hide(h)
    for _ in range(5):
        b.resolve_target("focused")
    assert b._meta_for(h) is not None            # 若すぎて reap されない
    assert b._meta_for(h)["missing_count"] == 5  # 欠落は数えているが age が未達


def test_pane_reaped_after_age_and_consecutive_miss(tmp_path):
    """真因B: age 超過 かつ min_missing 連続欠落を満たすと reap される (決定的モデル)。"""
    adapter = FakeAdapter(reap_min_age_seconds=5.0, reap_min_missing_snapshots=2)
    b = Broker(state_dir=tmp_path, adapter=adapter)
    adapter.add_pane(active=True)   # host pane
    disp = _ops(b)
    dispatch_tool(b, disp, "spawn_claude_pane", {"direction": "vertical", "name": "w"})
    h = adapter.spawned[-1]["handle"]
    tok = b._meta_for(h)["token"]
    # age gate を満たすよう spawn 時刻を過去へ (100s 前に spawn した扱い)。
    b._pane_meta[str(h)]["spawned_at"] -= 100
    adapter.terminate(h)            # pane 自己終了 (物理消滅)
    b.resolve_target("focused")     # 欠落 1 回目: 閾値未満 -> 生存
    assert b._meta_for(h) is not None
    assert b._meta_for(h)["missing_count"] == 1
    b.resolve_target("focused")     # 欠落 2 回目: age + 連続欠落成立 -> reap
    assert b._meta_for(h) is None
    assert b._binds[tok].revoked is True
    assert h not in adapter.killed  # 物理消滅済みなので kill は呼ばない


def test_reap_requires_wall_time_missing_not_just_call_count(tmp_path):
    """真因B の cadence 非依存ゲート (adversarial review Major): reap は missing_count
    だけでなく missing_since からの**実時間**経過も要求する。request-driven に何度
    reap を回しても、単一ラグ窓 (実時間が進まない) では生 pane を reap しない。"""
    # missing_count は 1 回で満たすが、実時間ゲート (100s) は満たさない設定。
    adapter = FakeAdapter(
        reap_min_missing_snapshots=1, reap_min_missing_seconds=100.0,
    )
    b = Broker(state_dir=tmp_path, adapter=adapter)
    adapter.add_pane(active=True)   # host pane
    disp = _ops(b)
    dispatch_tool(b, disp, "spawn_claude_pane", {"direction": "vertical", "name": "w"})
    h = adapter.spawned[-1]["handle"]
    tok = b._meta_for(h)["token"]
    b._pane_meta[str(h)]["spawned_at"] -= 100   # age gate は満たす
    adapter.desync_hide(h)          # snapshot から欠落 (物理的には生存)
    # 立て続けに reap を駆動 (単一ラグ窓を模す: 実時間はほぼ進まない)。
    for _ in range(10):
        b.resolve_target("focused")
    assert b._meta_for(h) is not None       # 実時間ゲート未達で reap されない
    assert b._binds[tok].revoked is False
    assert h not in adapter.killed          # 生 pane を物理 kill しない (Major 回帰防止)
    assert b._meta_for(h)["missing_count"] >= 10  # 呼び出しは数えているが時間が未達
    # missing_since を過去へずらし実時間経過を満たすと reap される。
    b._pane_meta[str(h)]["missing_since"] -= 200
    b.resolve_target("focused")
    assert b._meta_for(h) is None


def test_reap_physically_closes_residual_pane_and_journals(tmp_path):
    """真因A: 物理残存する reap 候補 (snapshot ラグで欠落したが実は生存) は bookkeeping
    削除前に物理 close され、close 経路 / 残存が pane_reaped に journal される。"""
    adapter = FakeAdapter(detailed_kill=True)   # 既定閾値 (0.0 / 1) で即候補化
    b = Broker(state_dir=tmp_path, adapter=adapter)
    adapter.add_pane(active=True)   # host pane
    disp = _ops(b)
    dispatch_tool(b, disp, "spawn_claude_pane", {"direction": "vertical", "name": "w"})
    h = adapter.spawned[-1]["handle"]
    agent_id = b._meta_for(h)["agent_id"]
    # snapshot からは消えるが物理的には生存 (eventually consistent)。
    adapter.desync_hide(h)
    b.resolve_target("focused")     # reap 駆動
    # 事前 probe に頼らず常に物理 close を発行する -> 生存していれば実際に閉じる。
    assert h in adapter.killed
    assert b._meta_for(h) is None   # close 有効なので bookkeeping も掃除される
    path = b.state_dir / "queue.jsonl"
    events = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]
    reaped = [e for e in events if e["event"] == "pane_reaped" and e.get("pane_id") == h]
    assert len(reaped) == 1
    assert reaped[0]["agent_id"] == agent_id
    assert reaped[0]["kill"]["closed_via"] == "pane.close"
    assert reaped[0]["kill"]["still_present"] is False


def test_reap_defers_when_physical_close_fails_to_remove_live_pane(tmp_path):
    """Codex P2: 物理 close が生 pane を消せなかった場合 (close 拒否 / kill 失敗)、
    bookkeeping を落とさず保持し次ラウンドに委ねる — 誤 reap で生 TUI を unmanaged
    孤児化させない (本 patch が断とうとする状態を再生しない)。"""
    adapter = FakeAdapter(detailed_kill=True, kill_ineffective=True)
    b = Broker(state_dir=tmp_path, adapter=adapter)
    adapter.add_pane(active=True)
    disp = _ops(b)
    dispatch_tool(b, disp, "spawn_claude_pane", {"direction": "vertical", "name": "w"})
    h = adapter.spawned[-1]["handle"]
    tok = b._meta_for(h)["token"]
    adapter.desync_hide(h)          # snapshot 欠落だが物理生存
    b.resolve_target("focused")     # reap 駆動 -> close は refused (still_present True)
    # close が消せなかったので bookkeeping は保持される (defer)。
    assert b._meta_for(h) is not None
    assert b._binds[tok].revoked is False
    path = b.state_dir / "queue.jsonl"
    events = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]
    assert any(e["event"] == "pane_reap_deferred" and e.get("pane_id") == h for e in events)
    assert not any(e["event"] == "pane_reaped" and e.get("pane_id") == h for e in events)
    # close が効くようになれば次ラウンドで通常どおり reap される。
    adapter._kill_ineffective = False
    b.resolve_target("focused")
    assert b._meta_for(h) is None
    assert b._binds[tok].revoked is True


def test_failed_spawn_does_not_count_toward_respawn_flood(tmp_path):
    """Codex P3: 予約後に spawn が失敗した試行は burst 履歴に残さない — 実際に pane が
    立たなかった失敗の連続で [respawn_flood] を誤発火しない。bind-only 同名衝突で
    issue_token(unique=True) が失敗する経路で固定する。"""
    adapter = FakeAdapter()
    b = Broker(
        state_dir=tmp_path, adapter=adapter,
        respawn_burst_threshold=2, respawn_burst_window=100.0,
    )
    adapter.add_pane(active=True)
    disp = _ops(b)
    # 同名の bind-only agent を先に mint -> spawn_claude の issue_token が衝突で失敗。
    b.issue_token("dup", "dup", "worker")
    for _ in range(5):
        out = dispatch_tool(b, disp, "spawn_claude_pane",
                            {"direction": "vertical", "name": "dup"})
        assert out["isError"] is True
        # threshold(2) を超えても respawn_flood にはならない (失敗は数えない)。
        assert "[respawn_flood]" not in out["content"][0]["text"]
    # burst 履歴は失敗ぶんを溜めていない (rollback されている)。
    assert b._spawn_history.get("dup", []) == []


def test_reap_residual_uses_kill_pane_fallback_without_detailed(tmp_path):
    """kill_pane_detailed を持たない adapter でも、物理残存する reap 候補は kill_pane +
    close 後 pane_exists で最小可視化しつつ確実に物理 close する (fallback 経路)。"""
    adapter = FakeAdapter()   # detailed_kill=False -> kill_pane_detailed 無し
    assert not hasattr(adapter, "kill_pane_detailed")
    b = Broker(state_dir=tmp_path, adapter=adapter)
    adapter.add_pane(active=True)
    disp = _ops(b)
    dispatch_tool(b, disp, "spawn_claude_pane", {"direction": "vertical", "name": "w"})
    h = adapter.spawned[-1]["handle"]
    adapter.desync_hide(h)
    b.resolve_target("focused")
    assert h in adapter.killed
    assert b._meta_for(h) is None
    path = b.state_dir / "queue.jsonl"
    events = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]
    reaped = [e for e in events if e["event"] == "pane_reaped" and e.get("pane_id") == h]
    assert reaped[0]["kill"]["closed_via"] == "kill_pane"
    assert reaped[0]["kill"]["still_present"] is False


def test_reap_deferred_when_physical_close_backend_unreachable(tmp_path):
    """真因A の安全側 (Codex round2 P2): 物理 close が backend 不通で発行できない
    ラウンドは reap を見送る (bookkeeping を落とさない) — 消せたか未確認のまま meta を
    落とすと生 TUI を孤児化しうる。欠落状態は次ラウンドへ持ち越す。

    判定は list-backed で stale になりうる pane_exists ではなく close 経路で行うので、
    ここでは close (kill_pane) 自体を backend 不通にする。"""
    adapter = FakeAdapter()
    b = Broker(state_dir=tmp_path, adapter=adapter)
    adapter.add_pane(active=True)
    disp = _ops(b)
    dispatch_tool(b, disp, "spawn_claude_pane", {"direction": "vertical", "name": "w"})
    h = adapter.spawned[-1]["handle"]
    tok = b._meta_for(h)["token"]
    adapter.desync_hide(h)          # snapshot から欠落 (候補化)
    # 物理 close (kill_pane) を backend 不通 (例外) にする -> closed_via="error" で defer。
    orig_kill = adapter.kill_pane
    adapter.kill_pane = lambda pid: (_ for _ in ()).throw(RuntimeError("socket down"))
    b.resolve_target("focused")
    assert b._meta_for(h) is not None       # 消せたか未確認 -> 誤 reap しない
    assert b._binds[tok].revoked is False
    # backend 復帰後は close が有効になり reap される (孤児化を残さない)。
    adapter.kill_pane = orig_kill
    b.resolve_target("focused")
    assert b._meta_for(h) is None
    assert h in adapter.killed


# ================================ same-name respawn burst dampener (真因D)


def test_respawn_burst_is_dampened(tmp_path):
    """真因D: window 内で threshold 回を超える同名 spawn は [respawn_flood] で拒否する
    (launcher リトライ x reap の相互増幅による同名孤児量産への追加防御)。"""
    adapter = FakeAdapter()
    b = Broker(
        state_dir=tmp_path, adapter=adapter,
        respawn_burst_threshold=3, respawn_burst_window=100.0,
    )
    adapter.add_pane(active=True)
    disp = _ops(b)
    # threshold 回まで受理 (毎回 terminate で name 解放 -> 次 spawn 前に reap で再取得可)。
    for _ in range(3):
        out = dispatch_tool(b, disp, "spawn_claude_pane",
                            {"direction": "vertical", "name": "w"})
        assert out.get("isError") is not True
        adapter.terminate(adapter.spawned[-1]["handle"])
    # threshold+1 回目 (window 内) は拒否。
    out = dispatch_tool(b, disp, "spawn_claude_pane",
                        {"direction": "vertical", "name": "w"})
    assert out["isError"] is True
    assert "[respawn_flood]" in out["content"][0]["text"]


def test_respawn_burst_does_not_block_distinct_names(tmp_path):
    """burst dampener は name 単位。別名の spawn は同一 window でも影響を受けない。"""
    adapter = FakeAdapter()
    b = Broker(
        state_dir=tmp_path, adapter=adapter,
        respawn_burst_threshold=2, respawn_burst_window=100.0,
    )
    adapter.add_pane(active=True)
    disp = _ops(b)
    for i in range(5):
        out = dispatch_tool(b, disp, "spawn_claude_pane",
                            {"direction": "vertical", "name": f"w{i}"})
        assert out.get("isError") is not True
