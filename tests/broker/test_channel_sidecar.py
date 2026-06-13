# -*- coding: utf-8 -*-
"""channel sidecar (R3) の単体テスト — tool-less 宣言 + row->channel 変換。

設計 SoT: broker-native-roles.md §9.2 / §9.5。canonical 実装: transport-lab
spike/channel_sidecar.py の faithful port。実 claude を起こす idle-wake は K1 spike
(実機 PASS, PR #24) が証明済み。本テストは runtime port の純粋部分 (JSON-RPC handler /
queue row -> claude/channel payload 変換) を固定する。
"""

from __future__ import annotations

from claude_org_runtime.broker import channel_sidecar as cs


def test_initialize_is_tool_less_channel_only():
    """initialize は experimental{claude/channel} のみ宣言し tools を出さない (§9.5)。"""
    resp = cs._handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18"}})
    caps = resp["result"]["capabilities"]
    assert caps == {"experimental": {"claude/channel": {}}}
    assert "tools" not in caps  # tool-less = poll 手段が存在しない
    assert resp["result"]["protocolVersion"] == "2025-06-18"


def test_initialize_negotiates_unknown_protocol():
    resp = cs._handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "1999-01-01"}})
    assert resp["result"]["protocolVersion"] == cs._DEFAULT_PROTO


def test_tools_list_is_empty():
    resp = cs._handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert resp["result"]["tools"] == []


def test_notifications_initialized_returns_none():
    assert cs._handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_unknown_method_with_id_errors():
    resp = cs._handle({"jsonrpc": "2.0", "id": 9, "method": "frobnicate"})
    assert resp["error"]["code"] == -32601


def test_unknown_notification_ignored():
    assert cs._handle({"jsonrpc": "2.0", "method": "notifications/whatever"}) is None


def test_channel_payload_maps_entry_to_content_and_meta():
    """queue row {id, entry, epoch} -> (content, meta) 変換 (msg_id dedup key 含む)。"""
    row = {
        "id": "abc123",
        "epoch": 0,
        "entry": {
            "from_id": "dispatcher",
            "from_name": "dispatcher",
            "sent_at": 1781353457.69,
            "message": "DELEGATE: do the thing",
        },
    }
    content, meta = cs._channel_payload(row)
    assert content == "DELEGATE: do the thing"
    assert meta["from_id"] == "dispatcher"
    assert meta["from_name"] == "dispatcher"
    # #80: 数値 sent_at は string 化して載せる (host schema は string 必須)。
    assert meta["sent_at"] == "1781353457.69"
    assert isinstance(meta["sent_at"], str)
    assert meta["msg_id"] == "abc123"  # daemon 行 id = at-least-once dedup key


def test_channel_payload_tolerates_missing_entry_fields():
    content, meta = cs._channel_payload({"id": "x", "entry": {}})
    # 欠落 sent_at は degenerate なので空文字 (None を載せて schema 違反にしない)。
    assert content == "" and meta["msg_id"] == "x" and meta["from_id"] is None
    assert meta["sent_at"] == ""
