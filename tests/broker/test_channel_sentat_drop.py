# -*- coding: utf-8 -*-
"""Regression: broker channel push silent-drop via numeric meta.sent_at (#80).

#75 (push 一次配送) 移植回帰の決定論的 silent-drop。store.enqueue は
``entry.sent_at`` を ``time.time()`` の **float** で打つ。channel sidecar の
``_channel_payload`` がその数値をそのまま ``meta.sent_at`` へ載せて
``notifications/claude/channel`` で emit すると、host claude の channel スキーマは
``sent_at`` を **string** で要求するため ZodError になり、通知ごと STDIO で drop
されて本文がセッションへ注入されない (= push 一次の本文喪失)。さらに
``/confirm-delivered`` が host-accept を見ず emit (stdout flush) 成功で確定するため
queue 行も DELIVERED 化して drain 済みになり、pull フォールバックも空振りする
(#80 の二層原因のうち本テストは layer 1 = 型不一致を固定する)。

**テストの限界 (誠実な明記)**: 実 Claude (host) の Zod 検証を unit test では
走らせられない。本テストは host の ``notifications/claude/channel`` スキーマのうち
本 Issue の争点である「``meta.sent_at`` は string でなければ ZodError -> STDIO drop」
という制約を最小モデル (:func:`_host_accepts_channel`) で決定論的に再現し、
**修正前 (float をそのまま載せる) は drop / 修正後 (string 化) は accept** を
fail-before / pass-after で証明する。#76 (test_nudge_misroute.py) の様式に倣う。
"""

from __future__ import annotations

from claude_org_runtime.broker import channel_sidecar as cs
from claude_org_runtime.broker.server import Broker


# --------------------------------------------------------------------------- host model
def _host_accepts_channel(params: dict) -> bool:
    """host claude の ``notifications/claude/channel`` 受理可否を最小モデル化する。

    争点の制約のみ: ``params.content`` は string、``params.meta.sent_at`` は
    **string** でなければならない。満たさなければ host 側で ZodError になり通知が
    STDIO で drop される (= 本文がセッションへ注入されない silent-drop)。数値
    sent_at がこの境界で弾かれることを決定的に判定する (検証不能な host 内部挙動は
    焼き込まず、string-required の 1 点だけを判定基準にする)。
    """
    if not isinstance(params, dict):
        return False
    if not isinstance(params.get("content"), str):
        return False
    meta = params.get("meta")
    if not isinstance(meta, dict):
        return False
    return isinstance(meta.get("sent_at"), str)


def _emit_params(content: str, meta: dict) -> dict:
    """_emit_channel が stdout へ書く JSON-RPC notification の params 部。"""
    return {"content": content, "meta": meta}


# --------------------------------------------------------------------------- (a)
def test_numeric_sent_at_would_be_dropped_by_host():
    """誤配再現の対照: pre-fix 形 (float sent_at を直載せ) は host schema に弾かれる。

    修正前の ``_channel_payload`` は ``entry.get("sent_at")`` (float) をそのまま
    ``meta.sent_at`` に載せていた。これを host が受けると ZodError -> STDIO drop で
    本文喪失する。string 化がこれを解消することの裏付け。
    """
    entry = {
        "from_id": "dispatcher", "from_name": "dispatcher",
        "sent_at": 1781353457.69, "message": "DELEGATE: do the thing",
    }
    pre_fix_meta = {  # 修正前の射影 (数値をそのまま載せる)
        "from_id": entry["from_id"], "from_name": entry["from_name"],
        "sent_at": entry["sent_at"], "msg_id": "row-1",
    }
    params = _emit_params(entry["message"], pre_fix_meta)
    assert not _host_accepts_channel(params)  # ZodError -> drop (silent-drop)


def test_channel_payload_sent_at_is_string_and_host_accepts():
    """修正 (a): ``_channel_payload`` は数値 sent_at を string 化して host に通す。

    fail-before: float のまま -> host が drop。pass-after: string 化 -> host が accept
    し本文が worker に届く。値も厳密に検査する (型だけでなく内容が壊れていないこと)。
    """
    row = {
        "id": "row-1", "epoch": 0,
        "entry": {
            "from_id": "dispatcher", "from_name": "dispatcher",
            "sent_at": 1781353457.69, "message": "DELEGATE: do the thing",
        },
    }
    content, meta = cs._channel_payload(row)
    assert meta["sent_at"] == "1781353457.69"   # 値が壊れていない
    assert isinstance(meta["sent_at"], str)
    assert _host_accepts_channel(_emit_params(content, meta))  # accept -> 本文注入


def test_enqueue_to_channel_payload_end_to_end_survives_host(tmp_path):
    """real store の数値 sent_at -> claim -> _channel_payload -> host accept を結線。

    store.enqueue が打つ実 float sent_at (``time.time()``) が、claim した row を
    ``_channel_payload`` に通すと string 化されて host schema を通過する end-to-end。
    enqueue/poll_claims は実ロジック (モックしない) で、修正の射影だけが境界を直す
    ことを示す。
    """
    b = Broker(state_dir=tmp_path, adapter=None)
    src = b.issue_token("src", "src", "worker")
    b.register_local(src)
    dst = b.issue_token("dst", "dst", "worker")
    b.register_local(dst)
    b.enqueue(b.get_bind(src), "dst", "hello-over-channel")

    res = b.poll_claims(b.issue_delivery_cred("dst"))
    row = res["rows"][0]
    # store が打つ sent_at は float (= 本 Issue の混入源)。
    assert isinstance(row["entry"]["sent_at"], float)

    content, meta = cs._channel_payload(row)
    assert content == "hello-over-channel"
    assert isinstance(meta["sent_at"], str)
    assert _host_accepts_channel(_emit_params(content, meta))
