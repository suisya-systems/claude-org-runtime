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


def test_notifications_initialized_registers_before_arming(monkeypatch):
    """Issue #125 Major #5: initialized は register を **同期** 完了させてから
    _started (push loop) を arm する。返り値は None (通知には応答しない)。"""
    calls: list[bool] = []

    def _fake_register() -> bool:
        # _started はまだ立っていない (register が先) ことを確認する。
        assert not cs._started.is_set()
        calls.append(True)
        return True

    monkeypatch.setattr(cs, "_register_with_retries", _fake_register)
    cs._started.clear()
    try:
        assert cs._handle({"jsonrpc": "2.0",
                           "method": "notifications/initialized"}) is None
        assert calls == [True]          # register を試みた
        assert cs._started.is_set()     # その後 push loop を arm した
    finally:
        cs._started.clear()


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


# ===================================================== Issue #129 stand-down guard
def _reset_sidecar_state():
    cs._stood_down.clear()
    cs._started.clear()
    with cs._gen_lock:
        cs._generation = None


def test_register_owner_includes_observer_and_bg_signals(monkeypatch):
    """register payload に observer 秘密 (Phase 2) を載せる。BG_HOSTED False の間は
    bg_hosted を載せない (明示 marker のみ suppress を発火させる)。"""
    captured = {}
    monkeypatch.setattr(cs, "_daemon_post",
                        lambda path, payload: captured.update(path=path, payload=payload)
                        or {"ok": True, "generation": 1})
    monkeypatch.setattr(cs, "OBSERVER_SECRET", "sekret")
    monkeypatch.setattr(cs, "BG_HOSTED", False)
    _reset_sidecar_state()
    try:
        assert cs._register_owner() == 1
        assert captured["path"] == "/claim-owner"
        assert captured["payload"]["observer"] == "sekret"
        assert "bg_hosted" not in captured["payload"]
    finally:
        _reset_sidecar_state()


def test_register_owner_stands_down_on_unobserved(monkeypatch):
    """daemon の unobserved (fork replay で observer 秘密を持たない) は stand-down させ、
    None を返す (claim しない = message を破壊しない)。"""
    monkeypatch.setattr(cs, "_daemon_post",
                        lambda path, payload: {"ok": False, "error": "unobserved"})
    _reset_sidecar_state()
    try:
        assert cs._register_owner() is None
        assert cs._stood_down.is_set()
        assert cs._current_generation() is None
    finally:
        _reset_sidecar_state()


def test_register_owner_stands_down_on_bg_suppress(monkeypatch):
    """明示 bg_hosted marker: payload に bg_hosted=True を載せ、suppressed_bg_hosted で
    stand-down する (daemon に marker を伝えて観測性を残しつつ claim しない)。"""
    posted = []
    monkeypatch.setattr(cs, "BG_HOSTED", True)
    monkeypatch.setattr(cs, "_daemon_post",
                        lambda path, payload: posted.append(payload)
                        or {"ok": False, "error": "suppressed_bg_hosted"})
    _reset_sidecar_state()
    try:
        assert cs._register_owner() is None
        assert cs._stood_down.is_set()
        assert posted[0]["bg_hosted"] is True
    finally:
        _reset_sidecar_state()


def test_register_with_retries_no_retry_on_stand_down(monkeypatch):
    """stand-down は transient ではないので再試行しない (即 False、1 回のみ post)。"""
    calls = []
    monkeypatch.setattr(cs, "_daemon_post",
                        lambda path, payload: calls.append(1)
                        or {"ok": False, "error": "unobserved"})
    _reset_sidecar_state()
    try:
        assert cs._register_with_retries() is False
        assert cs._stood_down.is_set()
        assert len(calls) == 1
    finally:
        _reset_sidecar_state()


def test_initialized_stands_down_without_registering(monkeypatch):
    """stand-down 済なら重複 initialized でも再 register しない (suppress / 沈黙を保つ)。"""
    def _boom() -> bool:
        raise AssertionError("must not register while stood down")

    monkeypatch.setattr(cs, "_register_with_retries", _boom)
    _reset_sidecar_state()
    cs._stood_down.set()
    try:
        assert cs._handle({"jsonrpc": "2.0",
                           "method": "notifications/initialized"}) is None
        assert cs._started.is_set()   # push loop は arm するが中で stand-down して抜ける
    finally:
        _reset_sidecar_state()
