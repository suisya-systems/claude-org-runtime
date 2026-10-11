# -*- coding: utf-8 -*-
"""#131 暫定: push confirm 後に受領の兆候が無い行の pull 専用再通知。"""

from __future__ import annotations

import json

from claude_org_runtime.broker.server import Broker
from claude_org_runtime.broker.store import DELIVERED, UNDELIVERED


def _registered(b: Broker, agent_id: str):
    tok = b.issue_token(agent_id, agent_id, "worker")
    b.register_local(tok)
    return b.get_bind(tok)


def _push_delivered(b: Broker, src, dst_id: str):
    """src -> dst を push で emit+confirm し (行, sidecar 引数) を返す。"""
    dc = b.issue_delivery_cred(dst_id)
    gen = b.register_delivery_instance(dc, "i1")["generation"]
    b.enqueue(src, dst_id, "hello")
    rows = b.poll_claims(dc, gen, "i1")["rows"]
    assert len(rows) == 1
    assert b.confirm_delivered(dc, rows[0]["id"], rows[0]["epoch"], gen, "i1")["ok"]
    return b._rows[rows[0]["id"]], (dc, gen, "i1")


def _expire(row) -> None:
    if row.awaiting_receipt_since:   # 0 = 待ち解除済。触らない
        row.awaiting_receipt_since -= 3600


def _journal_events(b: Broker) -> list[dict]:
    path = b.state_dir / "queue.jsonl"
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]


def test_no_receipt_renotifies_via_pull_only_with_same_id(tmp_path):
    b = Broker(state_dir=tmp_path, adapter=None, renotify_seconds=60)
    nudged: list[str] = []
    b._trigger_nudge = lambda t: nudged.append(t.agent_id)
    src, dst = _registered(b, "src"), _registered(b, "dst")
    row, (dc, gen, iid) = _push_delivered(b, src, "dst")
    nudged.clear()
    assert row.state == DELIVERED

    # 期限前は何もしない
    assert b.poll_claims(dc, gen, iid)["rows"] == []
    assert row.state == DELIVERED

    _expire(row)
    # 期限後の poll が tick: push では再 claim せず、pull 専用で戻し再ナッジする
    assert b.poll_claims(dc, gen, iid)["rows"] == []
    assert row.state == UNDELIVERED and row.renotified
    assert nudged == ["dst"]
    assert any(e["event"] == "renotify_no_receipt" and e["id"] == row.id
               for e in _journal_events(b))

    # check_messages が同じ msg_id で再配達し、消化で確定する (再々通知しない)
    msgs = b.drain(dst)
    assert [(m["message"], m["msg_id"]) for m in msgs] == [("hello", row.id)]
    assert row.state == DELIVERED and not row.awaiting_receipt_since
    assert b.drain(dst) == []


def test_reply_to_sender_is_receipt(tmp_path):
    b = Broker(state_dir=tmp_path, adapter=None, renotify_seconds=60)
    src, dst = _registered(b, "src"), _registered(b, "dst")
    row, _ = _push_delivered(b, src, "dst")
    b.enqueue(dst, "src", "got it")
    _expire(row)  # 返信で待ちは解除済 (0 のまま)
    assert b.drain(dst) == []
    assert row.state == DELIVERED and not row.renotified


def test_send_to_third_party_is_not_receipt(tmp_path):
    b = Broker(state_dir=tmp_path, adapter=None, renotify_seconds=60)
    src, dst = _registered(b, "src"), _registered(b, "dst")
    _registered(b, "other")
    row, _ = _push_delivered(b, src, "dst")
    b.enqueue(dst, "other", "unrelated")
    _expire(row)
    assert [m["msg_id"] for m in b.drain(dst)] == [row.id]


def test_disabled_when_renotify_seconds_none(tmp_path):
    b = Broker(state_dir=tmp_path, adapter=None, renotify_seconds=None)
    src, dst = _registered(b, "src"), _registered(b, "dst")
    row, _ = _push_delivered(b, src, "dst")
    _expire(row)
    assert b.drain(dst) == []
    assert row.state == DELIVERED
