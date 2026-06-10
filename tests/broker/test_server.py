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
