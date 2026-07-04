# -*- coding: utf-8 -*-
"""push 一次配送 (R3/R4) のライフサイクル + trust 境界テスト。

設計 SoT: broker-native-roles.md §9.3 (三状態) / §9.4 (delivery-scoped token) /
§9.5 (spawn 儀式) / §5.5 (切戻し第 6 ステップ)。canonical 実装: transport-lab
spike/k1_daemon.py (PR #24 merge 28a4cb2 で idle-wake 実機 PASS) のライフサイクル
不変条件を runtime store + delivery endpoint で固定する。

被覆 (full 受入):
- claim-then-confirm: UNDELIVERED -> CLAIMED -> DELIVERED、id 冪等。
- claim-respecting check_messages: live claim を二重配達しない / 並行ドレインしない。
- lease-reap recovery: sidecar 死亡 (confirm せず) でも message を喪失せず再配達。
- mode-epoch fencing: flip 後の stale epoch confirm を拒否し行を再 eligible 化。
- claim-issuance ゲート: PULL mode で poll_claims を拒否 (check_messages は不変)。
- delivery-scoped credential: /mcp 拒否 / endpoint は owner 行のみ / full token 遮断。
- spawn 儀式: dev-channel flag + channel server config + delivery cred 発行。
- 切戻し: close_pane が delivery cred revoke + delivery_mode reset。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from claude_org_runtime.broker import sidecar
from claude_org_runtime.broker.server import Broker
from claude_org_runtime.broker.store import CLAIMED, DELIVERED, PULL, PUSH, UNDELIVERED
from claude_org_runtime.broker.surface import dispatch_tool

from .conftest import FakeAdapter


# --------------------------------------------------------------------- helpers
def _registered(b: Broker, agent_id: str, pane_id=None):
    tok = b.issue_token(agent_id, agent_id, "worker", pane_id=pane_id)
    b.register_local(tok)
    return b.get_bind(tok)


def _ops(b: Broker, agent_id="d", role="dispatcher"):
    tok = b.issue_token(agent_id, agent_id, role)
    b.register_local(tok)
    return b.get_bind(tok)


def _text(out):
    return json.loads(out["content"][0]["text"])


def _row_states(b: Broker, to_id: str) -> list[str]:
    return [r.state for r in b._rows.values() if r.to_id == to_id]


def _sidecar(b: Broker, owner: str, instance: str = "i1"):
    """delivery cred を発行し 1 つの sidecar instance を register する (Issue #125)。

    session-scoped fencing で poll/confirm は register 済 generation を要求するため、
    テストはまず register してから (cred, generation, instance) を得る。
    """
    dc = b.issue_delivery_cred(owner)
    reg = b.register_delivery_instance(dc, instance)
    return dc, reg["generation"], instance


# ===================================================================== R4 store
def test_claim_then_confirm_lifecycle(tmp_path):
    b = Broker(state_dir=tmp_path, adapter=None)
    src, dst = _registered(b, "src"), _registered(b, "dst")
    dc, gen, iid = _sidecar(b, "dst")
    b.enqueue(src, "dst", "hello")
    assert _row_states(b, "dst") == [UNDELIVERED]

    res = b.poll_claims(dc, gen, iid)
    assert len(res["rows"]) == 1 and res["epoch"] == 0
    rid = res["rows"][0]["id"]
    assert res["rows"][0]["entry"]["message"] == "hello"
    assert _row_states(b, "dst") == [CLAIMED]

    conf = b.confirm_delivered(dc, rid, res["epoch"], gen, iid)
    assert conf["ok"] is True
    assert _row_states(b, "dst") == [DELIVERED]
    # id 冪等: 二度目の confirm は idempotent。
    assert b.confirm_delivered(dc, rid, res["epoch"], gen, iid) == {"ok": True, "idempotent": True}


def test_check_messages_respects_live_claim(tmp_path):
    """live な sidecar claim 中の行は check_messages が返さない (二重配達なし)。"""
    b = Broker(state_dir=tmp_path, adapter=None, lease_seconds=30.0)
    src, dst = _registered(b, "src"), _registered(b, "dst")
    b.enqueue(src, "dst", "m1")
    dc, gen, iid = _sidecar(b, "dst")
    b.poll_claims(dc, gen, iid)  # CLAIMED, lease 30s (まだ live)
    # check_messages は live claim を見送る (空)。
    assert b.drain(dst) == []
    assert _row_states(b, "dst") == [CLAIMED]


def test_check_messages_drains_unclaimed(tmp_path):
    b = Broker(state_dir=tmp_path, adapter=None)
    src, dst = _registered(b, "src"), _registered(b, "dst")
    b.enqueue(src, "dst", "m1")
    b.enqueue(src, "dst", "m2")
    msgs = b.drain(dst)
    assert [m["message"] for m in msgs] == ["m1", "m2"]
    assert _row_states(b, "dst") == [DELIVERED, DELIVERED]
    assert b.drain(dst) == []  # at-most-once on DELIVERED


def test_lease_reap_recovers_dead_sidecar(tmp_path):
    """confirm されないまま lease 失効した行は再 eligible 化し喪失しない (§9.3)。"""
    b = Broker(state_dir=tmp_path, adapter=None, lease_seconds=0.05)
    src, dst = _registered(b, "src"), _registered(b, "dst")
    b.enqueue(src, "dst", "survive-me")
    dc, gen, iid = _sidecar(b, "dst")
    res = b.poll_claims(dc, gen, iid)  # CLAIMED (sidecar 死亡で confirm せず)
    assert _row_states(b, "dst") == [CLAIMED]
    time.sleep(0.1)  # lease 失効を待つ
    # check_messages (pull fallback) が reap して再配達する = 喪失しない。
    msgs = b.drain(dst)
    assert [m["message"] for m in msgs] == ["survive-me"]
    # reclaim_count が増えている。
    row = next(iter(b._rows.values()))
    assert row.reclaim_count == 1


def test_confirm_after_lease_expiry_rejected(tmp_path):
    """lease 失効後の confirm は not_claimed で拒否 (reap で UNDELIVERED へ戻る)。"""
    b = Broker(state_dir=tmp_path, adapter=None, lease_seconds=0.05)
    src, dst = _registered(b, "src"), _registered(b, "dst")
    dc, gen, iid = _sidecar(b, "dst")
    b.enqueue(src, "dst", "x")
    res = b.poll_claims(dc, gen, iid)
    rid = res["rows"][0]["id"]
    time.sleep(0.1)
    conf = b.confirm_delivered(dc, rid, res["epoch"], gen, iid)
    assert conf["ok"] is False and conf["error"] == "not_claimed"


def test_mode_epoch_fencing_rejects_stale_confirm(tmp_path):
    """flip で epoch が進み、旧 epoch の confirm は stale_epoch で拒否される。"""
    b = Broker(state_dir=tmp_path, adapter=None, lease_seconds=30.0)
    src, dst = _registered(b, "src"), _registered(b, "dst")
    dc, gen, iid = _sidecar(b, "dst")
    b.enqueue(src, "dst", "x")
    res = b.poll_claims(dc, gen, iid)  # epoch 0, CLAIMED
    rid = res["rows"][0]["id"]
    flip = b.flip_mode("dst", PULL)  # epoch -> 1、CLAIMED -> UNDELIVERED
    assert flip["epoch"] == 1 and flip["mode"] == PULL
    assert _row_states(b, "dst") == [UNDELIVERED]
    conf = b.confirm_delivered(dc, rid, res["epoch"], gen, iid)  # epoch 0 (stale)
    assert conf["ok"] is False and conf["error"] == "stale_epoch" and conf["epoch"] == 1


def test_stale_confirm_does_not_strip_newer_claim(tmp_path):
    """Codex Major: stale epoch の confirm が新しい epoch の live claim を剥がさない。

    epoch 0 claim -> PULL -> PUSH (epoch 2) -> epoch 2 で再 claim。古い epoch 0 confirm が
    来ても epoch 2 の claim は無傷で、現 sidecar の epoch 2 confirm が成功する。
    """
    b = Broker(state_dir=tmp_path, adapter=None, lease_seconds=30.0)
    src, dst = _registered(b, "src"), _registered(b, "dst")
    dc, gen, iid = _sidecar(b, "dst")
    b.enqueue(src, "dst", "x")
    first = b.poll_claims(dc, gen, iid)  # epoch 0, CLAIMED
    rid = first["rows"][0]["id"]
    b.flip_mode("dst", PULL)              # epoch 1, row -> UNDELIVERED
    b.flip_mode("dst", PUSH)             # epoch 2
    second = b.poll_claims(dc, gen, iid)  # epoch 2, 再 CLAIMED
    assert second["epoch"] == 2 and len(second["rows"]) == 1
    # 古い epoch 0 confirm: 拒否されるが epoch 2 の claim は剥がさない。
    stale = b.confirm_delivered(dc, rid, first["epoch"], gen, iid)
    assert stale["error"] == "stale_epoch"
    assert _row_states(b, "dst") == [CLAIMED]   # 新 claim 無傷
    # 現 sidecar の epoch 2 confirm は成功する (剥がされていない証拠)。
    assert b.confirm_delivered(dc, rid, second["epoch"], gen, iid)["ok"] is True


def test_pull_mode_disables_claim_issuance(tmp_path):
    """PULL mode は poll_claims を拒否するが check_messages は不変 (§9.3)。"""
    b = Broker(state_dir=tmp_path, adapter=None)
    src, dst = _registered(b, "src"), _registered(b, "dst")
    b.flip_mode("dst", PULL)
    b.enqueue(src, "dst", "m1")
    dc, gen, iid = _sidecar(b, "dst")
    res = b.poll_claims(dc, gen, iid)
    assert res["error"] == "push_disabled" and res["rows"] == []
    # check_messages は mode に依らず claim-respecting drain (フォールバック健在)。
    assert [m["message"] for m in b.drain(dst)] == ["m1"]


def test_poll_claims_gated_on_registered_owner(tmp_path):
    """Codex Major: 未登録 (initialize 前 / DELETE 後) の owner には claim を発行しない。

    死にかけ session への emit->confirm で DELIVERED-but-lost になる窓を閉じる。行は
    UNDELIVERED のまま残り、registered に戻れば claim され、check_messages でも拾える。
    """
    b = Broker(state_dir=tmp_path, adapter=None)
    # full token は発行するが register しない (= initialize 前 / DELETE 後を模す)。
    full = b.issue_token("dst", "dst", "worker")
    dc, gen, iid = _sidecar(b, "dst")   # delivery sidecar は register 済 (generation live)
    src = _registered(b, "src")
    # registered な src 経由で enqueue (宛先解決のため dst を一時 register して戻す)。
    b.register_local(full)
    b.enqueue(src, "dst", "do-not-lose-me")
    # ここで dst が DELETE された状況を模す (registered=False)。
    b.get_bind(full).registered = False
    res = b.poll_claims(dc, gen, iid)
    assert res["error"] == "owner_unregistered" and res["rows"] == []
    assert _row_states(b, "dst") == [UNDELIVERED]   # 行は残る (喪失しない)
    # re-initialize (registered に戻る) で claim 可能になる。
    b.get_bind(full).registered = True
    res2 = b.poll_claims(dc, gen, iid)
    assert [r["entry"]["message"] for r in res2["rows"]] == ["do-not-lose-me"]


def test_poll_claims_only_returns_owner_rows(tmp_path):
    b = Broker(state_dir=tmp_path, adapter=None)
    src = _registered(b, "src")
    _registered(b, "dst")
    _registered(b, "dst2")
    b.enqueue(src, "dst", "for-dst")
    b.enqueue(src, "dst2", "for-dst2")
    dc, gen, iid = _sidecar(b, "dst")
    res = b.poll_claims(dc, gen, iid)
    assert [r["entry"]["message"] for r in res["rows"]] == ["for-dst"]


def test_confirm_not_owner_rejected(tmp_path):
    b = Broker(state_dir=tmp_path, adapter=None)
    src = _registered(b, "src")
    _registered(b, "dst")
    _registered(b, "other")
    b.enqueue(src, "dst", "x")
    dc, gen, iid = _sidecar(b, "dst")
    res = b.poll_claims(dc, gen, iid)
    rid = res["rows"][0]["id"]
    # 別 owner の cred は他人宛の行を confirm できない (owner=cred.agent_id で判定)。
    # not_owner は generation fence より前に効く (別 owner なので generation は無関係)。
    other_cred, other_gen, other_iid = _sidecar(b, "other", instance="io")
    assert b.confirm_delivered(
        other_cred, rid, res["epoch"], other_gen, other_iid)["error"] == "not_owner"


def test_revoked_delivery_cred_cannot_claim_or_confirm(tmp_path):
    """Codex Major (revocation fence): revoke 済 delivery cred は claim/confirm 不可。

    owner の full bind が registered でも、cred 自体が revoke 済なら poll_claims /
    confirm_delivered は unauthorized を返し行に触れない (owner だけで claim できた
    TOCTOU を、token を _lock 下で再検証することで原子的 fence にする)。
    """
    b = Broker(state_dir=tmp_path, adapter=None)
    src, dst = _registered(b, "src"), _registered(b, "dst")
    dc = b.issue_delivery_cred("dst")
    b.enqueue(src, "dst", "x")
    b.revoke_delivery_creds("dst")  # close_pane の revoke_delivery_creds 相当
    # revoked cred は owner が None に解決され unauthorized (generation fence より前)。
    res = b.poll_claims(dc, 1, "i1")
    assert res["error"] == "unauthorized" and res["rows"] == []
    assert _row_states(b, "dst") == [UNDELIVERED]   # revoked cred では claim されない
    assert b.confirm_delivered(dc, "anyid", 0, 1, "i1")["error"] == "unauthorized"
    # 完全に未知の token も同様。
    assert b.poll_claims("bogus-token", 1, "i1")["error"] == "unauthorized"


def test_flip_mode_invalid(tmp_path):
    b = Broker(state_dir=tmp_path, adapter=None)
    res = b.flip_mode("dst", "SHOVE")
    assert res["ok"] is False and "invalid_mode" in res["error"]


# ================================================ Issue #125 session fencing
def test_register_bumps_generation_monotonically(tmp_path):
    """register ごとに generation が単調 +1 する (daemon 再起動なしで増加)。"""
    b = Broker(state_dir=tmp_path, adapter=None)
    _registered(b, "dst")
    dc = b.issue_delivery_cred("dst")
    assert b.register_delivery_instance(dc, "i1")["generation"] == 1
    assert b.register_delivery_instance(dc, "i2")["generation"] == 2
    # 別 owner は独立した世代空間を持つ。
    _registered(b, "other")
    oc = b.issue_delivery_cred("other")
    assert b.register_delivery_instance(oc, "io")["generation"] == 1


def test_register_requires_delivery_scope(tmp_path):
    """Issue #125 Major #4: register は delivery cred のみ。full/revoked/bogus token は
    unauthorized で拒否し、他 owner の generation を bump できない (横取り fence 防御)。"""
    b = Broker(state_dir=tmp_path, adapter=None)
    _registered(b, "dst")
    # full-scope token は register できない (scope != delivery -> owner None)。
    full = b.issue_token("dst", "dst", "worker")
    assert b.register_delivery_instance(full, "i1") == {"ok": False, "error": "unauthorized"}
    # 完全に未知の token も同様。
    assert b.register_delivery_instance("bogus", "i1")["error"] == "unauthorized"
    # revoke 済 delivery cred も register できない。
    dc = b.issue_delivery_cred("dst")
    b.revoke_delivery_creds("dst")
    assert b.register_delivery_instance(dc, "i1")["error"] == "unauthorized"
    # どの拒否経路でも generation は bump されない (他 owner の fence を乗っ取れない)。
    assert "dst" not in b._delivery_generations


def test_claim_owner_rejects_full_token_over_http(broker):
    """Issue #125 Major #4: /claim-owner は delivery scope bearer のみ (full token は 401)。"""
    full = broker.issue_token("agent", "agent", "worker")
    status, _ = _post(broker.base_url + "/claim-owner", full, {"instance_id": "i1"})
    assert status == 401
    # delivery cred は通る。
    delivery = broker.issue_delivery_cred("agent")
    status, body = _post(broker.base_url + "/claim-owner", delivery, {"instance_id": "i1"})
    assert status == 200 and body["ok"] is True and body["generation"] == 1


def test_old_generation_poll_rejected(tmp_path):
    """Issue #125: fork 元 (旧 generation) の sidecar poll は stale_sidecar で拒否。"""
    b = Broker(state_dir=tmp_path, adapter=None, lease_seconds=30.0)
    src, dst = _registered(b, "src"), _registered(b, "dst")
    # 二重 sidecar: 同一 cred (fork replay) を別 instance で 2 度 register。
    dc = b.issue_delivery_cred("dst")
    reg_old = b.register_delivery_instance(dc, "old-inst")   # generation 1
    reg_new = b.register_delivery_instance(dc, "new-inst")   # generation 2 (現世代)
    b.enqueue(src, "dst", "for-current-session")
    # 旧世代 sidecar の poll は claim を発行しない (fence)。
    res_old = b.poll_claims(dc, reg_old["generation"], "old-inst")
    assert res_old["error"] == "stale_sidecar" and res_old["generation"] == 2
    assert res_old["rows"] == []
    assert _row_states(b, "dst") == [UNDELIVERED]   # 旧 sidecar は claim していない
    # 現世代 sidecar だけが claim できる (二重 claim による消失が消える)。
    res_new = b.poll_claims(dc, reg_new["generation"], "new-inst")
    assert [r["entry"]["message"] for r in res_new["rows"]] == ["for-current-session"]


def test_old_generation_confirm_rejected(tmp_path):
    """Issue #125 Blocker #2: 旧 generation が register 前に claim した行を後から
    confirm できない。旧 claim は現世代へ再 eligible 化され現 sidecar が届ける。"""
    b = Broker(state_dir=tmp_path, adapter=None, lease_seconds=30.0)
    src, dst = _registered(b, "src"), _registered(b, "dst")
    dc = b.issue_delivery_cred("dst")
    reg_old = b.register_delivery_instance(dc, "old-inst")   # generation 1
    b.enqueue(src, "dst", "x")
    # 旧 sidecar が (新 sidecar 登場前に) claim する。
    claimed = b.poll_claims(dc, reg_old["generation"], "old-inst")
    rid = claimed["rows"][0]["id"]
    assert _row_states(b, "dst") == [CLAIMED]
    # 新 sidecar が register -> generation 2、旧 CLAIMED 行は UNDELIVERED へ差し戻し。
    reg_new = b.register_delivery_instance(dc, "new-inst")
    assert reg_new["generation"] == 2
    assert _row_states(b, "dst") == [UNDELIVERED]   # 旧 claim を待たず即差し戻し
    # 旧 sidecar が後から confirm しても拒否される (lost にならない)。
    conf = b.confirm_delivered(dc, rid, claimed["epoch"], reg_old["generation"], "old-inst")
    assert conf["ok"] is False and conf["error"] == "stale_sidecar" and conf["generation"] == 2
    assert _row_states(b, "dst") == [UNDELIVERED]   # 依然 UNDELIVERED (現 sidecar 用)
    # 現世代 sidecar が claim -> confirm すると DELIVERED になる。
    c2 = b.poll_claims(dc, reg_new["generation"], "new-inst")
    assert c2["rows"][0]["id"] == rid
    assert b.confirm_delivered(dc, rid, c2["epoch"], reg_new["generation"], "new-inst")["ok"]
    assert _row_states(b, "dst") == [DELIVERED]


def test_stale_instance_cannot_replay_current_generation(tmp_path):
    """Codex review P2: stale sidecar は stale_sidecar 応答で現世代番号を知りうるが、
    その番号を自分の instance_id で replay しても daemon が instance を照合して拒否する
    (真に daemon 側で単一 claimer を強制)。現 instance の live claim も剥がさない。"""
    b = Broker(state_dir=tmp_path, adapter=None, lease_seconds=30.0)
    src, dst = _registered(b, "src"), _registered(b, "dst")
    dc = b.issue_delivery_cred("dst")
    b.register_delivery_instance(dc, "old")            # generation 1
    reg_new = b.register_delivery_instance(dc, "new")  # generation 2 (現世代 instance=new)
    cur_gen = reg_new["generation"]
    b.enqueue(src, "dst", "x")
    # 現世代 sidecar が claim (row は new の live claim)。
    claimed = b.poll_claims(dc, cur_gen, "new")
    rid = claimed["rows"][0]["id"]
    assert _row_states(b, "dst") == [CLAIMED]
    # stale (old) が漏れた現世代番号 2 を自分の instance_id で replay -> それでも拒否。
    replay = b.poll_claims(dc, cur_gen, "old")
    assert replay["error"] == "stale_sidecar" and replay["rows"] == []
    assert _row_states(b, "dst") == [CLAIMED]   # new の claim は無傷
    # stale が現世代番号で confirm を試みても拒否し、new の claim を剥がさない。
    conf = b.confirm_delivered(dc, rid, claimed["epoch"], cur_gen, "old")
    assert conf["error"] == "stale_sidecar"
    assert _row_states(b, "dst") == [CLAIMED]   # 依然 new の live claim
    # 現世代 (new) の confirm は成功する (剥がされていない証拠)。
    assert b.confirm_delivered(dc, rid, claimed["epoch"], cur_gen, "new")["ok"] is True
    assert _row_states(b, "dst") == [DELIVERED]


def test_register_requeues_old_generation_claim(tmp_path):
    """Issue #125 Blocker #3: 新 generation register で旧 CLAIMED を UNDELIVERED へ即戻す
    (lease 失効を待たない)。"""
    b = Broker(state_dir=tmp_path, adapter=None, lease_seconds=300.0)  # lease は長い
    src, dst = _registered(b, "src"), _registered(b, "dst")
    dc = b.issue_delivery_cred("dst")
    reg_old = b.register_delivery_instance(dc, "old")
    b.enqueue(src, "dst", "m")
    b.poll_claims(dc, reg_old["generation"], "old")
    assert _row_states(b, "dst") == [CLAIMED]
    # 長い lease でも register 時に即 requeue される (fence が lease 失効遅延を作らない)。
    b.register_delivery_instance(dc, "new")
    assert _row_states(b, "dst") == [UNDELIVERED]


def test_duplicate_sidecar_detected_journaled(tmp_path):
    """Issue #125 Major #5: 同一 owner を複数 instance が lease window 内に poll したら
    duplicate_sidecar_detected を journal する (pair ごと初回のみ、毎 poll スパムなし)。"""
    b = Broker(state_dir=tmp_path, adapter=None, lease_seconds=30.0)
    _registered(b, "dst")
    dc = b.issue_delivery_cred("dst")
    reg1 = b.register_delivery_instance(dc, "inst-a")
    reg2 = b.register_delivery_instance(dc, "inst-b")   # 現世代
    # 両 instance が poll する (旧 inst-a は stale だが記録はされる)。
    b.poll_claims(dc, reg1["generation"], "inst-a")
    b.poll_claims(dc, reg2["generation"], "inst-b")
    b.poll_claims(dc, reg1["generation"], "inst-a")   # 再度 (cooldown で追加 emit なし)

    lines = (b.state_dir / "queue.jsonl").read_text(encoding="utf-8").splitlines()
    dups = [json.loads(x) for x in lines
            if json.loads(x)["event"] == "duplicate_sidecar_detected"]
    assert len(dups) == 1   # pair {inst-a, inst-b} は 1 回だけ
    assert set(dups[0]["instances"]) == {"inst-a", "inst-b"}
    assert dups[0]["owner"] == "dst"


def test_single_sidecar_never_flags_duplicate(tmp_path):
    """Issue #125: 正常系 (単一 instance が繰り返し poll) は duplicate を一切出さない
    (false-positive しない = 通常配備で毎 poll スパムしない)。"""
    b = Broker(state_dir=tmp_path, adapter=None, lease_seconds=30.0)
    _registered(b, "dst")
    dc = b.issue_delivery_cred("dst")
    reg = b.register_delivery_instance(dc, "solo")
    for _ in range(3):
        b.poll_claims(dc, reg["generation"], "solo")
    lines = (b.state_dir / "queue.jsonl").read_text(encoding="utf-8").splitlines()
    dups = [x for x in lines if json.loads(x)["event"] == "duplicate_sidecar_detected"]
    assert dups == []


def test_duplicate_detection_cooldown_reemit_and_distinct_pairs(tmp_path):
    """Issue #125 Major #5/#10: duplicate 検知は (a) cooldown 内は再 emit しない
    (anti-spam) (b) cooldown 経過後は再 emit する (持続的二重の liveness シグナル)
    (c) distinct instance pair ごとに別 emit する。

    ``_note_poll_locked`` を制御した ``now`` で直接呼び (単一スレッド・純ロジック)、
    時間依存の flakiness なしに両半分を固定する。
    """
    b = Broker(state_dir=tmp_path, adapter=None, lease_seconds=10.0)
    W = b.lease_seconds
    T = 1000.0   # 非ゼロ基準 (emit cooldown の既定 0.0 と衝突させない)
    # t=T: a -> b。pair {a,b} を 1 回 emit。
    assert b._note_poll_locked("dst", "a", T) == []
    j = b._note_poll_locked("dst", "b", T)
    assert [e[0] for e in j] == ["duplicate_sidecar_detected"]
    assert set(j[0][1]["instances"]) == {"a", "b"}
    # cooldown 内 (< W) の再 poll は追加 emit しない (anti-spam)。
    assert b._note_poll_locked("dst", "a", T + 1.0) == []
    # 両 instance を window 内に保ちつつ cooldown をまたぐ。
    assert b._note_poll_locked("dst", "b", T + 9.0) == []      # b alive、まだ cooldown 内
    reemit = b._note_poll_locked("dst", "a", T + W + 1.0)      # cooldown 経過 -> 再 emit
    assert [e[0] for e in reemit] == ["duplicate_sidecar_detected"]
    assert set(reemit[0][1]["instances"]) == {"a", "b"}
    # distinct pair: 新 instance c は {a,c} / {b,c} を別々に emit する。
    c = b._note_poll_locked("dst", "c", T + W + 1.0)
    pairs = {tuple(sorted(e[1]["instances"])) for e in c}
    assert pairs == {("a", "c"), ("b", "c")}


def test_delivery_dump_exposes_generation_and_instance(tmp_path):
    """Issue #125 Minor #9: delivery_dump に owner ごとの現世代と active instance。"""
    b = Broker(state_dir=tmp_path, adapter=None)
    _registered(b, "dst")
    dc = b.issue_delivery_cred("dst")
    b.register_delivery_instance(dc, "inst-x")
    dump = b.delivery_dump()
    assert dump["generations"]["dst"] == 1
    assert dump["instances"]["dst"] == "inst-x"


def test_reset_delivery_state_clears_fencing(tmp_path):
    """Issue #125 Major #8: reset で generation/instance/duplicate tracking も消える
    (同名 respawn 後の誤 fence / 誤 duplicate を防ぐ)。"""
    b = Broker(state_dir=tmp_path, adapter=None)
    _registered(b, "dst")
    dc = b.issue_delivery_cred("dst")
    b.register_delivery_instance(dc, "inst-x")
    assert "dst" in b._delivery_generations
    b.reset_delivery_state("dst")
    assert "dst" not in b._delivery_generations
    assert "dst" not in b._delivery_instances
    assert "dst" not in b._delivery_poll_seen
    # 同名 respawn は generation 1 から再開する (旧世代を継承しない)。
    dc2 = b.issue_delivery_cred("dst")
    assert b.register_delivery_instance(dc2, "inst-y")["generation"] == 1


def test_double_sidecar_over_http_only_current_claims(broker):
    """Issue #125 Blocker #7: 同一 delivery cred + 異なる instance の二重 sidecar を
    HTTP 経由で再現し、現世代のみ claim すること (旧世代は stale_sidecar) を固定する。"""
    src = broker.issue_token("src", "src", "worker")
    broker.register_local(src)
    dst = broker.issue_token("dst", "dst", "worker")
    broker.register_local(dst)
    delivery = broker.issue_delivery_cred("dst")   # fork replay で共有される単一 cred

    # 旧 session の sidecar (instance old) が register。
    _, reg_old = _post(broker.base_url + "/claim-owner", delivery, {"instance_id": "old"})
    # fork/resume で立った新 sidecar (instance new) が register -> 世代交代。
    _, reg_new = _post(broker.base_url + "/claim-owner", delivery, {"instance_id": "new"})
    assert reg_old["generation"] == 1 and reg_new["generation"] == 2

    broker.enqueue(broker.get_bind(src), "dst", "human-facing-message")

    # 旧 sidecar の poll は stale_sidecar (claim しない = 二重 claim 消失が起きない)。
    st_old, body_old = _post(broker.base_url + "/poll-claims", delivery,
                             {"generation": 1, "instance_id": "old"})
    assert st_old == 200 and body_old["error"] == "stale_sidecar"
    assert body_old["rows"] == []

    # 現世代 sidecar だけが claim できる。
    st_new, body_new = _post(broker.base_url + "/poll-claims", delivery,
                             {"generation": 2, "instance_id": "new"})
    assert st_new == 200 and len(body_new["rows"]) == 1
    row = body_new["rows"][0]
    assert row["entry"]["message"] == "human-facing-message"

    # 旧 sidecar が後から confirm しても拒否 (row は現世代のもの)。
    st_c, conf = _post(broker.base_url + "/confirm-delivered", delivery,
                       {"id": row["id"], "epoch": row["epoch"],
                        "generation": 1, "instance_id": "old"})
    assert st_c == 200 and conf["error"] == "stale_sidecar"
    # 現世代 confirm は成功。
    _, conf2 = _post(broker.base_url + "/confirm-delivered", delivery,
                     {"id": row["id"], "epoch": row["epoch"],
                      "generation": 2, "instance_id": "new"})
    assert conf2["ok"] is True


# ============================================================ R4 HTTP endpoints
def _post(url: str, token: str, payload: dict):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        body = e.read()
        return e.code, (json.loads(body) if body else {})


def test_delivery_endpoints_require_delivery_scope(broker):
    """/poll-claims・/confirm-delivered は delivery cred のみ。full token は 401。"""
    full = broker.issue_token("agent", "agent", "worker")
    delivery = broker.issue_delivery_cred("agent")
    # full token は delivery endpoint に入れない (least-privilege の双方向遮断)。
    status, _ = _post(broker.base_url + "/poll-claims", full,
                      {"generation": 1, "instance_id": "i1"})
    assert status == 401
    # delivery cred は register して現世代で poll できる。
    status, reg = _post(broker.base_url + "/claim-owner", delivery, {"instance_id": "i1"})
    assert status == 200 and reg["ok"] is True and reg["generation"] == 1
    status, body = _post(broker.base_url + "/poll-claims", delivery,
                        {"generation": reg["generation"], "instance_id": "i1"})
    assert status == 200 and body["rows"] == []


def test_delivery_cred_cannot_use_mcp_surface(broker):
    """delivery-scoped credential は /mcp (initialize/tools) を構造的に使えない。"""
    delivery = broker.issue_delivery_cred("agent")
    req = urllib.request.Request(
        broker.url,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": "2025-06-18"}}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {delivery}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
    except urllib.error.HTTPError as e:
        status = e.code
    assert status == 403  # scope_forbidden


def test_delivery_endpoint_roundtrip_over_http(broker):
    """enqueue -> /poll-claims -> /confirm-delivered を HTTP 越しに往復する。"""
    src = broker.issue_token("src", "src", "worker")
    broker.register_local(src)
    dst = broker.issue_token("dst", "dst", "worker")
    broker.register_local(dst)
    broker.enqueue(broker.get_bind(src), "dst", "wire-hello")
    delivery = broker.issue_delivery_cred("dst")

    status, reg = _post(broker.base_url + "/claim-owner", delivery, {"instance_id": "i1"})
    assert status == 200 and reg["ok"] is True
    gen = reg["generation"]

    status, body = _post(broker.base_url + "/poll-claims", delivery,
                        {"generation": gen, "instance_id": "i1"})
    assert status == 200 and len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["entry"]["message"] == "wire-hello"

    status, conf = _post(broker.base_url + "/confirm-delivered", delivery,
                         {"id": row["id"], "epoch": row["epoch"],
                          "generation": gen, "instance_id": "i1"})
    assert status == 200 and conf["ok"] is True


def test_confirm_invalid_id_400(broker):
    delivery = broker.issue_delivery_cred("dst")
    status, body = _post(broker.base_url + "/confirm-delivered", delivery,
                         {"id": 123, "epoch": 0, "generation": 1, "instance_id": "i1"})
    assert status == 400 and "invalid_id" in body["error"]


# ================================================================ R3 spawn 儀式
def test_spawn_claude_injects_broker_state_dir_env(tmp_path, fake_adapter):
    """spawn_claude_pane が pane 親環境へ ORG_BROKER_STATE_DIR(絶対) を注入する (#122)。

    pane 内で走る CLI subprocess (broker send を叩く ja peer_notify) が、非既定
    --state-dir daemon の queue を発見できるようにするための本丸。channel sidecar 用の
    mcp_config env とは別物 (これは actual pane env = fake_adapter.spawned[-1]['env'])。
    """
    b = Broker(state_dir=tmp_path / "sd", adapter=fake_adapter)
    fake_adapter.add_pane(active=True)
    disp = _ops(b)
    dispatch_tool(b, disp, "spawn_claude_pane", {
        "direction": "vertical", "name": "worker-foo", "cwd": "/repo",
    })
    env = fake_adapter.spawned[-1]["env"]
    assert env["ORG_BROKER_STATE_DIR"] == sidecar.absolutize(tmp_path / "sd")
    assert sidecar.is_absolute(env["ORG_BROKER_STATE_DIR"])


def test_spawn_generic_injects_broker_state_dir_env(tmp_path, fake_adapter):
    """spawn_pane (generic, secretary tier) も同じ ORG_BROKER_STATE_DIR を注入する (#122)。"""
    b = Broker(state_dir=tmp_path / "sd", adapter=fake_adapter)
    fake_adapter.add_pane(active=True)
    sec = _ops(b, "s", "secretary")
    dispatch_tool(b, sec, "spawn_pane",
                  {"direction": "horizontal", "command": "watch ls", "name": "w"})
    env = fake_adapter.spawned[-1]["env"]
    assert env["ORG_BROKER_STATE_DIR"] == sidecar.absolutize(tmp_path / "sd")


def test_spawn_injects_broker_state_dir_on_space_layout_branch(tmp_path):
    """space-layout backend (Herdr 経路) の spawn 分岐でも env が注入される (#122)。

    _adapter_spawn には flat 分岐と space 分岐があり、supports_space_layout=True の
    backend (Herdr) は space 分岐を通る。この分岐の env=env が将来落ちると Herdr の
    #122 が silently 再発するため、space 分岐の env 注入を回帰で固定する。
    """
    adapter = FakeAdapter(supports_space_layout=True)
    b = Broker(state_dir=tmp_path / "sd", adapter=adapter)
    adapter.add_pane(active=True)
    disp = _ops(b)
    dispatch_tool(b, disp, "spawn_claude_pane",
                  {"direction": "vertical", "name": "worker-foo", "cwd": "/repo"})
    spawned = adapter.spawned[-1]
    # took the space branch (space descriptor present) AND still got env.
    assert spawned["space"] is not None
    assert spawned["env"]["ORG_BROKER_STATE_DIR"] == sidecar.absolutize(tmp_path / "sd")


def _make_venv(root):
    """Create a fake POSIX ``.venv`` (bin/python) under ``root`` (Issue #130)."""
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "python").write_text("")
    return root / ".venv"


def test_broker_stores_root_cwd(tmp_path):
    """Broker が --root-cwd を保持する (Issue #130 の venv 探索フォールバック基準)。"""
    b = Broker(state_dir=tmp_path / "sd", adapter=None, root_cwd="/abs/root")
    assert b.root_cwd == "/abs/root"


def test_adapter_spawn_activates_pane_cwd_venv(tmp_path, fake_adapter):
    """Issue #130: pane cwd/.venv があれば argv を login-shell wrapper に包み PATH を
    prepend し、VIRTUAL_ENV を env dict に注入する (POSIX host)。#122 の env も残る。"""
    venv = _make_venv(tmp_path)
    b = Broker(state_dir=tmp_path / "sd", adapter=fake_adapter)
    b._adapter_spawn(["claude", "--flag"], str(tmp_path), "worker", None)
    sp = fake_adapter.spawned[-1]
    assert sp["env"]["VIRTUAL_ENV"] == str(venv)
    # #122 の ORG_BROKER_STATE_DIR は env dict にそのまま残る (退行なし)。
    assert sp["env"]["ORG_BROKER_STATE_DIR"] == sidecar.absolutize(tmp_path / "sd")
    # argv は post-profile login-shell wrapper に包まれ、PATH prepend を後段で効かせる。
    assert sp["argv"][1] == "-lc"
    assert f'export PATH={venv}/bin:"$PATH"' in sp["argv"][2]
    assert sp["argv"][-2:] == ["claude", "--flag"]


def test_adapter_spawn_falls_back_to_root_cwd_venv(tmp_path, fake_adapter):
    """Issue #130 Major: worker worktree に .venv が無く root_cwd にある通常形で、
    root_cwd/.venv にフォールバックして活性化する。state_dir は探索基準にしない。"""
    worker = tmp_path / "worker"; worker.mkdir()
    root = tmp_path / "root"; root.mkdir()
    venv = _make_venv(root)
    b = Broker(state_dir=tmp_path / "sd", adapter=fake_adapter, root_cwd=str(root))
    b._adapter_spawn(["claude"], str(worker), "worker", None)
    sp = fake_adapter.spawned[-1]
    assert sp["env"]["VIRTUAL_ENV"] == str(venv)
    assert f'export PATH={venv}/bin:"$PATH"' in sp["argv"][2]


def test_adapter_spawn_noop_without_venv(tmp_path, fake_adapter):
    """Issue #130 Minor: .venv がどこにも無ければ完全 no-op (argv 不変 / VIRTUAL_ENV なし)。"""
    worker = tmp_path / "worker"; worker.mkdir()
    b = Broker(state_dir=tmp_path / "sd", adapter=fake_adapter, root_cwd=str(tmp_path))
    b._adapter_spawn(["claude", "--flag"], str(worker), "worker", None)
    sp = fake_adapter.spawned[-1]
    assert sp["argv"] == ["claude", "--flag"]
    assert "VIRTUAL_ENV" not in sp["env"]
    assert sp["env"]["ORG_BROKER_STATE_DIR"] == sidecar.absolutize(tmp_path / "sd")


def test_spawn_claude_pane_activates_venv_end_to_end(tmp_path, fake_adapter):
    """Issue #130: spawn_claude_pane ツール経路 (cwd 解決込み) でも venv を継承し、
    channel/mcp-config flag を保ったまま argv が wrapper に包まれる。"""
    venv = _make_venv(tmp_path)
    b = Broker(state_dir=tmp_path / "sd", adapter=fake_adapter)
    fake_adapter.add_pane(active=True)
    disp = _ops(b)
    dispatch_tool(b, disp, "spawn_claude_pane", {
        "direction": "vertical", "name": "worker-foo", "cwd": str(tmp_path),
    })
    sp = fake_adapter.spawned[-1]
    assert sp["env"]["VIRTUAL_ENV"] == str(venv)
    assert sp["argv"][1] == "-lc"
    # 元の claude argv (channel/mcp-config) は wrapper の末尾にそのまま残る。
    assert "--mcp-config" in sp["argv"]
    assert "--dangerously-load-development-channels" in sp["argv"]


def test_spawn_claude_injects_channel_sidecar_and_dev_channel(tmp_path, fake_adapter):
    """spawn_claude が channel sidecar + dev-channel flag + delivery cred を仕込む。"""
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    fake_adapter.add_pane(active=True)
    disp = _ops(b)
    out = dispatch_tool(b, disp, "spawn_claude_pane", {
        "direction": "vertical", "name": "worker-foo", "cwd": "/repo",
    })
    assert _text(out)["agent_id"] == "worker-foo"
    argv = fake_adapter.spawned[-1]["argv"]
    # dev-channel flag (3-3b 機械承認の再導入) が channel sidecar を指す。
    assert "--dangerously-load-development-channels" in argv
    assert argv[argv.index("--dangerously-load-development-channels") + 1] == \
        "server:org-broker-channel"
    # mcp-config に daemon (org-broker) と channel (org-broker-channel) の両方。
    cfg = json.loads(argv[argv.index("--mcp-config") + 1])
    servers = cfg["mcpServers"]
    assert "org-broker" in servers and "org-broker-channel" in servers
    ch = servers["org-broker-channel"]
    assert ch["args"] == ["-m", "claude_org_runtime.broker.channel_sidecar"]
    assert ch["env"]["ORG_BROKER_CHANNEL_OWNER"] == "worker-foo"
    assert ch["env"]["ORG_BROKER_CHANNEL_DAEMON_URL"] == b.base_url
    # delivery cred が発行され、その token が sidecar env に載っている。
    cred = ch["env"]["ORG_BROKER_CHANNEL_CRED"]
    cred_bind = b.get_bind(cred)
    assert cred_bind is not None and cred_bind.scope == "delivery"
    assert cred_bind.agent_id == "worker-foo" and cred_bind.registered is False


def test_delivery_cred_not_in_list_peers(tmp_path, fake_adapter):
    """delivery cred は registered=False で list_peers / 配送先に現れない。"""
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    fake_adapter.add_pane(active=True)
    disp = _ops(b)
    dispatch_tool(b, disp, "spawn_claude_pane",
                  {"direction": "vertical", "name": "w", "cwd": "/repo"})
    peers = _text(dispatch_tool(b, disp, "list_peers", {}))["peers"]
    # spawn された worker 自体は (register 前なので) peer に出ない; delivery cred も出ない。
    assert all(p["id"] != "" for p in peers)
    # delivery cred bind は存在するが registered=False。
    creds = [bd for bd in b._binds.values() if bd.scope == "delivery"]
    assert len(creds) == 1 and creds[0].registered is False


def test_close_pane_revokes_delivery_cred_and_resets_mode(tmp_path, fake_adapter):
    """切戻し §5.5 第 6: close_pane が delivery cred revoke + delivery_mode reset。"""
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    fake_adapter.add_pane(active=True)
    disp = _ops(b)
    out = _text(dispatch_tool(b, disp, "spawn_claude_pane",
                              {"direction": "vertical", "name": "w", "cwd": "/repo"}))
    pane_id = out["id"]
    # 配送状態を作る (mode flip)。
    b.flip_mode("w", PULL)
    assert "w" in b._delivery_modes
    cred = [bd for bd in b._binds.values() if bd.scope == "delivery"][0]
    assert cred.revoked is False
    # close_pane で reap。
    dispatch_tool(b, disp, "close_pane", {"target": str(pane_id)})
    assert cred.revoked is True               # delivery cred revoke
    assert "w" not in b._delivery_modes       # delivery_mode reset
    assert "w" not in b._epochs


def test_close_pane_purges_undelivered_rows(tmp_path, fake_adapter):
    """Codex Major: close_pane が未配達行を purge し、同名 re-spawn への誤配送を断つ。"""
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    fake_adapter.add_pane(active=True)
    disp = _ops(b)
    out = _text(dispatch_tool(b, disp, "spawn_claude_pane",
                              {"direction": "vertical", "name": "w", "cwd": "/repo"}))
    pane_id = out["id"]
    # spawn 直後は未 register なので enqueue 解決のため register_local しておく。
    b.register_local([t for t, bd in b._binds.items()
                      if bd.agent_id == "w" and bd.scope == "full"][0])
    b.enqueue(disp, "w", "stale-secret")
    assert _row_states(b, "w") == [UNDELIVERED]
    dispatch_tool(b, disp, "close_pane", {"target": str(pane_id)})
    # 旧セッション宛の行は消える (同名 re-spawn が拾えない)。
    assert _row_states(b, "w") == []


def test_spawn_failure_revokes_delivery_cred(tmp_path):
    """spawn (adapter) 失敗時に発行済み delivery cred も掃除される (orphan なし)。"""
    class BoomAdapter(FakeAdapter):
        def spawn(self, argv, cwd=None, new_window=True, space=None, env=None):
            raise RuntimeError("boom")

    adapter = BoomAdapter()
    b = Broker(state_dir=tmp_path, adapter=adapter)
    adapter.add_pane(active=True)
    disp = _ops(b)
    with pytest.raises(RuntimeError):
        dispatch_tool(b, disp, "spawn_claude_pane",
                      {"direction": "vertical", "name": "w", "cwd": "/repo"})
    # full token も delivery cred も revoke 済 (active な bind が残らない)。
    live = [bd for bd in b._binds.values() if not bd.revoked and bd.agent_id == "w"]
    assert live == []


def test_spawn_rejects_collision_with_bind_only_agent(tmp_path, fake_adapter):
    """cross-agent 配送横取りの防御: 既存 active bind (pane を持たない bind-only agent =
    admin mint された secretary 等) と agent_id 衝突する spawn は拒否され、被害 agent の
    agent_id を owner とする delivery cred を一切 mint しない (unique=True 防御)。"""
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    fake_adapter.add_pane(active=True)
    # admin mint 相当: pane を持たない registered な bind-only agent "secretary"。
    victim = b.issue_token("secretary", "secretary", "secretary")
    b.register_local(victim)
    b.enqueue(b.get_bind(victim), "secretary", "secret-for-the-real-secretary")
    disp = _ops(b)
    out = dispatch_tool(b, disp, "spawn_claude_pane",
                        {"direction": "vertical", "name": "secretary", "cwd": "/repo"})
    # 衝突は name_taken で拒否される。
    assert out.get("isError") and "name_taken" in out["content"][0]["text"]
    # 被害 agent_id を owner とする delivery cred は存在しない (横取り経路が開かない)。
    creds = [bd for bd in b._binds.values()
             if bd.scope == "delivery" and not bd.revoked]
    assert creds == []
    # 被害者の queue は無傷 (本人の check_messages で読める)。
    assert [m["message"] for m in b.drain(b.get_bind(victim))] == \
        ["secret-for-the-real-secretary"]
    # spawn 自体に到達していない (adapter.spawn 未呼出)。
    assert fake_adapter.spawned == []


# ============================== Issue #129 observed-session binding (問題 A)
def test_observer_lease_gates_generation_bump(tmp_path):
    """assert_observer 済 owner は、秘密を提示する sidecar だけが generation を bump できる。
    秘密無し / 不一致の register (fork replay 相当) は ``unobserved`` で拒否し generation
    不変 (observed live session の takeover を断つ)。"""
    b = Broker(state_dir=tmp_path, adapter=None)
    _registered(b, "sec")
    secret = b.assert_observer("sec")
    dc = b.issue_delivery_cred("sec")
    # observed sidecar: 正しい秘密 -> generation 1。
    reg = b.register_delivery_instance(dc, "obs", observer=secret)
    assert reg["ok"] is True and reg["generation"] == 1
    assert b._delivery_generations["sec"] == 1
    # fork replay: 秘密無し -> unobserved、generation は 1 のまま、現世代 instance も不変。
    forked = b.register_delivery_instance(dc, "fork", observer=None)
    assert forked["ok"] is False and forked["error"] == "unobserved"
    assert b._delivery_generations["sec"] == 1
    assert b._delivery_instances["sec"] == "obs"
    # 間違った秘密でも同様に拒否する。
    wrong = b.register_delivery_instance(dc, "fork2", observer="not-the-secret")
    assert wrong["error"] == "unobserved" and b._delivery_generations["sec"] == 1


def test_observer_fork_cannot_take_over_delivery(tmp_path):
    """問題 A の核心: observed session が claim 中に fork が register を試みても generation を
    奪えず、observed sidecar が message を届け続ける (二重 claim による沈黙喪失が起きない)。"""
    b = Broker(state_dir=tmp_path, adapter=None, lease_seconds=30.0)
    src, sec = _registered(b, "src"), _registered(b, "sec")
    secret = b.assert_observer("sec")
    dc = b.issue_delivery_cred("sec")
    gen = b.register_delivery_instance(dc, "obs", observer=secret)["generation"]
    b.enqueue(src, "sec", "human-facing-message")
    # fork が秘密無しで register (unobserved) — generation を奪えない。
    assert b.register_delivery_instance(dc, "fork", observer=None)["error"] == "unobserved"
    # observed sidecar は現世代のまま claim できる (message 喪失しない)。
    res = b.poll_claims(dc, gen, "obs")
    assert [r["entry"]["message"] for r in res["rows"]] == ["human-facing-message"]
    # fork は (奪えていないので現世代番号 gen を replay しても) instance 照合で拒否される。
    assert b.poll_claims(dc, gen, "fork")["error"] == "stale_sidecar"


def test_no_observer_lease_keeps_last_register_wins(tmp_path):
    """lease 未 assert の owner (子 pane 等) は従来の last-register-wins が不変。

    Phase 2 が observer 束縛の無い owner の push 配信を回帰させないことの回帰ガード
    (observer lease は org up secretary 経路だけが assert し、spawn_claude 子は assert
    しない = 従来どおり generation を bump して claim できる)。
    """
    b = Broker(state_dir=tmp_path, adapter=None)
    _registered(b, "w")
    dc = b.issue_delivery_cred("w")
    assert b.register_delivery_instance(dc, "i1")["generation"] == 1
    assert b.register_delivery_instance(dc, "i2")["generation"] == 2   # bump 継続


def test_observer_lease_armed_survives_slow_startup(tmp_path):
    """Codex P2: assert から初回 register までの起動遅延が TTL を超えても、armed lease は
    失効しない (register 前に wall-clock で失効すると fork/replay 保護が黙って外れる)。
    初回 observed register まで fork は unobserved で弾かれ続ける。"""
    b = Broker(state_dir=tmp_path, adapter=None, observer_lease_seconds=0.1)
    _registered(b, "sec")
    secret = b.assert_observer("sec")
    dc = b.issue_delivery_cred("sec")
    time.sleep(0.2)   # TTL(0.1) を超える起動遅延 (段1 folder-trust 放置等)
    # armed lease は失効していない: 秘密無し fork は依然 unobserved。
    assert b.register_delivery_instance(dc, "fork", observer=None)["error"] == "unobserved"
    # 秘密を持つ observed sidecar は register できる (保護が失われていない)。
    assert b.register_delivery_instance(dc, "obs", observer=secret)["ok"] is True
    # register で activate されるので、以後は TTL 計時が始まる (dump に失効時刻が入る)。
    assert isinstance(b.delivery_dump()["observers"]["sec"], float)


def test_observer_lease_renewed_by_poll_and_expires(tmp_path):
    """observer lease は現世代 poll heartbeat で renew し、poll が止まると TTL 経過で失効する
    (dead observed session の stale lease が将来の register を永久に塞がない)。"""
    b = Broker(state_dir=tmp_path, adapter=None, lease_seconds=30.0,
               observer_lease_seconds=0.2)
    _registered(b, "sec")
    secret = b.assert_observer("sec")
    dc = b.issue_delivery_cred("sec")
    gen = b.register_delivery_instance(dc, "obs", observer=secret)["generation"]
    # poll が renew するので、TTL 超の合計時間でも lease は生き続ける。
    for _ in range(4):
        time.sleep(0.1)
        b.poll_claims(dc, gen, "obs")
    assert "sec" in b.delivery_dump()["observers"]      # まだ束縛されている
    # poll を止めて TTL 経過 -> 失効。以後は last-register-wins に戻り、秘密無し register が
    # 通る (dead session を lease が塞がない)。
    time.sleep(0.3)
    assert b.register_delivery_instance(dc, "recover", observer=None)["ok"] is True


def test_reset_delivery_state_clears_observer_lease(tmp_path):
    """close_pane 相当の reset で observer lease も消える (同名 respawn の誤束縛を防ぐ)。"""
    b = Broker(state_dir=tmp_path, adapter=None)
    _registered(b, "sec")
    b.assert_observer("sec")
    assert "sec" in b._observer_leases
    b.reset_delivery_state("sec")
    assert "sec" not in b._observer_leases


def test_assert_observer_rotates_secret(tmp_path):
    """assert_observer は呼ぶたびに秘密を rotate する (新 launcher が旧 session を supersede)。
    旧秘密は以後 unobserved になり、新秘密だけが generation を bump できる。"""
    b = Broker(state_dir=tmp_path, adapter=None)
    _registered(b, "sec")
    s1 = b.assert_observer("sec")
    s2 = b.assert_observer("sec")
    assert s1 != s2
    dc = b.issue_delivery_cred("sec")
    assert b.register_delivery_instance(dc, "old", observer=s1)["error"] == "unobserved"
    assert b.register_delivery_instance(dc, "new", observer=s2)["ok"] is True


# ============================== Issue #129 bg-hosted suppress guard (問題 B / Phase 1)
def test_bg_hosted_marker_suppresses_register(tmp_path):
    """Phase 1: 明示 bg_hosted marker の register は generation を bump せず claim も許さず、
    ``delivery_suppressed_bg_hosted`` を journal する (heuristic ではなく明示 marker のみ)。"""
    b = Broker(state_dir=tmp_path, adapter=None)
    _registered(b, "sec")
    dc = b.issue_delivery_cred("sec")
    res = b.register_delivery_instance(dc, "bg", bg_hosted=True)
    assert res["ok"] is False and res["error"] == "suppressed_bg_hosted"
    assert "sec" not in b._delivery_generations   # generation 不変 (claim 権を得ない)
    events = [json.loads(x)["event"]
              for x in (b.state_dir / "queue.jsonl").read_text(encoding="utf-8").splitlines()]
    assert "delivery_suppressed_bg_hosted" in events


def test_bg_hosted_suppress_does_not_regress_normal_register(tmp_path):
    """bg_hosted 未指定 (既定 False) の register は従来どおり generation を bump する
    (suppress は明示 marker がある時だけ = 不明時は foreground 扱いで claim 継続)。"""
    b = Broker(state_dir=tmp_path, adapter=None)
    _registered(b, "sec")
    dc = b.issue_delivery_cred("sec")
    assert b.register_delivery_instance(dc, "fg")["generation"] == 1


# ============================== Issue #129 admin mint observer wiring
def test_admin_mint_observer_optin_asserts_lease_and_returns_secret(tmp_path):
    """observer=True の channel mint だけが observer lease を assert し秘密を返す。秘密は
    mcp_config に載らない (非 replay 信号 = 子プロセス env handoff とペア)。"""
    b = Broker(state_dir=tmp_path, adapter=None)
    res = b.admin_mint_token({"role": "secretary", "name": "sec",
                              "channel": True, "observer": True})
    assert res["ok"] is True
    secret = res["observer_secret"]
    assert secret and isinstance(secret, str)
    assert "sec" in b._observer_leases
    assert secret not in json.dumps(res["mcp_config"])   # persisted 面に秘密を残さない


def test_admin_mint_channel_without_observer_does_not_bind(tmp_path):
    """Codex P2: observer opt-in の無い channel mint は lease を張らず秘密も返さない。

    secret handoff を持たない admin caller が mcp_config だけで起動しても sidecar が
    unobserved で止まらない (従来の last-register-wins のまま)。"""
    b = Broker(state_dir=tmp_path, adapter=None)
    res = b.admin_mint_token({"role": "secretary", "name": "sec", "channel": True})
    assert res["ok"] is True
    assert res["observer_secret"] is None
    assert "sec" not in b._observer_leases
    # その sidecar は observer 無しでも register して generation を bump できる (配信継続)。
    cred = res["mcp_config"]["mcpServers"]["org-broker-channel"]["env"][
        "ORG_BROKER_CHANNEL_CRED"]
    assert b.register_delivery_instance(cred, "i1")["generation"] == 1


def test_admin_mint_observer_requires_channel(tmp_path):
    """observer=True を channel 無しで要求したら [invalid_params] で拒否する
    (観測束縛は delivery cred を要するため、無意味な組合せを loud に落とす)。"""
    b = Broker(state_dir=tmp_path, adapter=None)
    res = b.admin_mint_token({"role": "secretary", "name": "sec", "observer": True})
    assert res["ok"] is False and "observer requires channel" in res["error"]


def test_admin_mint_channel_not_requested_has_no_observer_secret(tmp_path):
    """channel 非要求 mint は observer_secret=None (delivery cred も lease も無し)。"""
    b = Broker(state_dir=tmp_path, adapter=None)
    res = b.admin_mint_token({"role": "secretary", "name": "sec2"})
    assert res["observer_secret"] is None and "sec2" not in b._observer_leases


# ============================== Issue #129 HTTP wire (observer / bg_hosted)
def test_claim_owner_observer_and_bg_over_http(broker):
    """/claim-owner が observer 秘密 (Phase 2) と bg_hosted marker (Phase 1) を配線する。"""
    broker.register_local(broker.issue_token("sec", "sec", "secretary"))
    secret = broker.assert_observer("sec")
    delivery = broker.issue_delivery_cred("sec")
    # observed 秘密ありは generation を bump する。
    st, body = _post(broker.base_url + "/claim-owner", delivery,
                     {"instance_id": "obs", "observer": secret})
    assert st == 200 and body["ok"] is True and body["generation"] == 1
    # 秘密無し (fork replay) は unobserved。
    st, body = _post(broker.base_url + "/claim-owner", delivery, {"instance_id": "fork"})
    assert st == 200 and body["error"] == "unobserved"
    # bg_hosted marker は suppress。
    st, body = _post(broker.base_url + "/claim-owner", delivery,
                     {"instance_id": "bg", "bg_hosted": True})
    assert st == 200 and body["error"] == "suppressed_bg_hosted"


def test_claim_owner_rejects_bad_observer_and_bg_types(broker):
    """observer は文字列、bg_hosted は bool を要求する (truthy 文字列で誤発火しない)。"""
    delivery = broker.issue_delivery_cred("sec")
    st, body = _post(broker.base_url + "/claim-owner", delivery,
                     {"instance_id": "i", "observer": 123})
    assert st == 400 and "invalid_observer" in body["error"]
    st, body = _post(broker.base_url + "/claim-owner", delivery,
                     {"instance_id": "i", "bg_hosted": "yes"})
    assert st == 400 and "invalid_bg_hosted" in body["error"]


# ============================ R3<->R4 cross-process integration (real sidecar)
def test_sidecar_subprocess_claims_emits_and_confirms(tmp_path):
    """実 channel sidecar を subprocess で起こし、poll->emit->confirm の往復を検証。

    実 claude を起こす idle-wake 自体は K1 spike (実機 PASS) が証明済み。本テストは
    runtime の R3 sidecar <-> R4 daemon endpoint を **別プロセス + 実 HTTP** で結線
    して、(a) sidecar が daemon から claim し、(b) ``notifications/claude/channel`` を
    stdout に emit し、(c) ``/confirm-delivered`` で daemon 側が DELIVERED 化する
    ことを end-to-end で固定する (confirm-only-after-emit の実証)。
    """
    b = Broker(state_dir=tmp_path / "broker", adapter=None, port=0, lease_seconds=30.0)
    b.start()
    try:
        src = b.issue_token("src", "src", "worker")
        b.register_local(src)
        dst = b.issue_token("dst", "dst", "worker")
        b.register_local(dst)
        b.enqueue(b.get_bind(src), "dst", "push-over-the-wire")
        delivery = b.issue_delivery_cred("dst")

        env = {
            **os.environ,
            "ORG_BROKER_CHANNEL_DAEMON_URL": b.base_url,
            "ORG_BROKER_CHANNEL_CRED": delivery,
            "ORG_BROKER_CHANNEL_OWNER": "dst",
            "ORG_BROKER_CHANNEL_POLL_INTERVAL": "0.2",
            "PYTHONPATH": os.pathsep.join(sys.path),
        }
        proc = subprocess.Popen(
            [sys.executable, "-m", "claude_org_runtime.broker.channel_sidecar"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env,
        )
        try:
            # MCP handshake: initialize -> initialized (push loop が起動する)。
            proc.stdin.write(
                (json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {"protocolVersion": "2025-06-18"}}) + "\n").encode()
            )
            proc.stdin.write(
                (json.dumps({"jsonrpc": "2.0",
                             "method": "notifications/initialized"}) + "\n").encode()
            )
            proc.stdin.flush()

            # stdout を別スレッドで読み、channel notification を待つ (deadline 付き)。
            found: dict = {}

            def _reader():
                for raw in proc.stdout:
                    try:
                        msg = json.loads(raw.decode("utf-8").strip())
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if msg.get("method") == "notifications/claude/channel":
                        found["msg"] = msg
                        return

            rt = threading.Thread(target=_reader, daemon=True)
            rt.start()
            rt.join(timeout=15.0)

            assert "msg" in found, "sidecar never emitted notifications/claude/channel"
            params = found["msg"]["params"]
            assert params["content"] == "push-over-the-wire"
            assert params["meta"]["from_id"] == "src"
            assert "msg_id" in params["meta"]
            # #80: emit/wire 境界で sent_at が string であること (host schema は string
            # 必須。float のままだと ZodError -> STDIO drop で本文喪失する)。
            assert isinstance(params["meta"]["sent_at"], str)

            # daemon 側で confirm が届き DELIVERED になるまで待つ (emit の後に confirm)。
            deadline = time.time() + 10
            while time.time() < deadline:
                states = _row_states(b, "dst")
                if states == [DELIVERED]:
                    break
                time.sleep(0.1)
            assert _row_states(b, "dst") == [DELIVERED]
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    finally:
        b.stop()
