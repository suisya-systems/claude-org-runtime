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

from claude_org_runtime.broker import sidecar, store
from claude_org_runtime.broker.server import Broker
from claude_org_runtime.broker.store import CLAIMED, DELIVERED, PULL, PUSH, UNDELIVERED
from claude_org_runtime.broker.surface import dispatch_tool
from claude_org_runtime.terminal import base as terminal_base

from . import conftest
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


def _make_venv(root, *, windows=None):
    """Create a fake ``.venv`` under ``root`` (Issue #130), layout matching the
    host ``os.name`` by default: ``Scripts/python.exe`` under nt, else
    ``bin/python``. Using the host layout (not a forced os.name) keeps these
    tests off the pathlib pitfall -- forcing os.name would make ``Broker``'s
    ``pathlib.Path`` build a ``PosixPath`` on Windows and raise (Codex P1)."""
    if windows is None:
        windows = os.name == "nt"
    if windows:
        (root / ".venv" / "Scripts").mkdir(parents=True)
        (root / ".venv" / "Scripts" / "python.exe").write_text("")
    else:
        (root / ".venv" / "bin").mkdir(parents=True)
        (root / ".venv" / "bin" / "python").write_text("")
    return root / ".venv"


def _assert_pane_venv_activated(sp, venv):
    """Assert the pane env activated ``venv`` for the running host's branch
    (platform-adaptive; no os.name forcing). Path expectations derive from
    ``terminal_base.venv_bin_dir`` so they are separator-safe on both OSes."""
    assert sp["env"]["VIRTUAL_ENV"] == str(venv)
    bin_dir = terminal_base.venv_bin_dir(str(venv))
    if os.name == "nt":
        # Windows: PATH rides the env dict as "<Scripts>;%PATH%" (wezterm's cmd
        # `set` expands %PATH%); argv is not wrapped.
        assert sp["env"]["PATH"] == f"{bin_dir};%PATH%"
    else:
        # POSIX: PATH prepend rides the post-profile login-shell wrapper argv.
        assert sp["argv"][1] == "-lc"
        assert "export PATH=" in sp["argv"][2]
        assert bin_dir in sp["argv"][2]


def test_broker_stores_root_cwd(tmp_path):
    """Broker が --root-cwd を保持する (Issue #130 の venv 探索フォールバック基準)。"""
    b = Broker(state_dir=tmp_path / "sd", adapter=None, root_cwd="/abs/root")
    assert b.root_cwd == "/abs/root"


def test_adapter_spawn_activates_pane_cwd_venv(tmp_path, fake_adapter):
    """Issue #130: pane cwd/.venv があれば venv を活性化する。POSIX は argv を
    login-shell wrapper に包み PATH prepend、Windows は env dict %PATH%。#122 の env も残る。"""
    venv = _make_venv(tmp_path)
    b = Broker(state_dir=tmp_path / "sd", adapter=fake_adapter)
    b._adapter_spawn(["claude", "--flag"], str(tmp_path), "worker", None)
    sp = fake_adapter.spawned[-1]
    _assert_pane_venv_activated(sp, venv)
    # #122 の ORG_BROKER_STATE_DIR は env dict にそのまま残る (退行なし)。
    assert sp["env"]["ORG_BROKER_STATE_DIR"] == sidecar.absolutize(tmp_path / "sd")
    # 元の claude argv は保たれる (Windows: そのまま / POSIX: wrapper 末尾)。
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
    _assert_pane_venv_activated(sp, venv)


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
    channel/mcp-config flag を保ったまま活性化する。"""
    venv = _make_venv(tmp_path)
    b = Broker(state_dir=tmp_path / "sd", adapter=fake_adapter)
    fake_adapter.add_pane(active=True)
    disp = _ops(b)
    dispatch_tool(b, disp, "spawn_claude_pane", {
        "direction": "vertical", "name": "worker-foo", "cwd": str(tmp_path),
    })
    sp = fake_adapter.spawned[-1]
    assert sp["env"]["VIRTUAL_ENV"] == str(venv)
    # 元の claude argv (channel/mcp-config) は保たれる (POSIX は wrapper 末尾)。
    assert "--mcp-config" in sp["argv"]
    assert "--dangerously-load-development-channels" in sp["argv"]
    if os.name != "nt":
        assert sp["argv"][1] == "-lc"


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
    秘密無し / 不一致の register (fork replay 相当) は拒否し generation 不変 (observed
    live session の takeover を断つ)。

    Issue #169: 拒否コードは 2 種類に分かれる — 秘密未提示は非 latch の
    ``observer_pending``、不一致提示 (= supersede された) は latch する ``unobserved``。
    """
    b = Broker(state_dir=tmp_path, adapter=None)
    _registered(b, "sec")
    secret = b.assert_observer("sec")
    dc = b.issue_delivery_cred("sec")
    # observed sidecar: 正しい秘密 -> generation 1。
    reg = b.register_delivery_instance(dc, "obs", observer=secret)
    assert reg["ok"] is True and reg["generation"] == 1
    assert b._delivery_generations["sec"] == 1
    # fork replay: 秘密無し -> observer_pending、generation は 1 のまま、現世代 instance
    # も不変 (fence は Issue #129 のまま効いている)。
    forked = b.register_delivery_instance(dc, "fork", observer=None)
    assert forked["ok"] is False and forked["error"] == "observer_pending"
    assert b._delivery_generations["sec"] == 1
    assert b._delivery_instances["sec"] == "obs"
    # 間違った秘密は「かつて秘密を持っていた = supersede された」なので latch 側。
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
    # fork が秘密無しで register (observer_pending) — generation を奪えない。
    assert (b.register_delivery_instance(dc, "fork", observer=None)["error"]
            == "observer_pending")
    # observed sidecar は現世代のまま claim できる (message 喪失しない)。
    res = b.poll_claims(dc, gen, "obs")
    assert [r["entry"]["message"] for r in res["rows"]] == ["human-facing-message"]
    # fork は (奪えていないので現世代番号 gen を replay しても) instance 照合で拒否される。
    assert b.poll_claims(dc, gen, "fork")["error"] == "stale_sidecar"


def test_no_observer_lease_keeps_last_register_wins(tmp_path):
    """lease 未 assert の owner は従来の last-register-wins が不変。

    observer 束縛の無い owner の push 配信を回帰させないことの回帰ガード。Issue #165 で
    lease を張る経路は増えたが、**張っていない owner** (admin mint の channel token で
    起動した caller 等、秘密の handoff を持たない呼び元) は従来どおり generation を
    bump して claim できなければならない (child push を壊さない、が #165 の制約)。
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
    # armed lease は失効していない: 秘密無し fork は依然 refuse される。
    assert (b.register_delivery_instance(dc, "fork", observer=None)["error"]
            == "observer_pending")
    # 秘密を持つ observed sidecar は register できる (保護が失われていない)。
    assert b.register_delivery_instance(dc, "obs", observer=secret)["ok"] is True
    # register で activate されるので、以後は TTL 計時が始まる。
    dumped = b.delivery_dump()["observers"]["sec"]
    assert dumped["state"] == "active" and isinstance(dumped["expires_at"], float)


def test_observer_lease_stays_fenced_after_the_heartbeat_stops(tmp_path):
    """**この挙動は Issue #169 で意図的に変えた。元に戻さないこと。**

    以前ここは「poll が止まって TTL 経過 -> lease 失効 -> 秘密無し register が通る」を
    固定していた (dead session の stale lease が将来の register を塞がないように)。
    その扉は塞いだ。理由:

    - **heartbeat の停止は死亡の証拠にならない**。pane の Ctrl+Z (SIGTSTP はプロセス
      グループ全体に効く)、ラップトップ suspend、MCP サーバー再起動の長期化、NTP に
      よる wall-clock ステップでも 90 秒の空白は開く。
    - **現職は生涯 1 回しか register しない** (generation war 防止の既存設計) のに対し、
      fork は 1 秒ごとに register を叩き続ける。だから扉が一度開くと、そこに居るのは
      常に fork だけで、現職は二度と取り返せない。

    = TTL 失効を扉にすると、それは実質「fork 専用の扉」になる。扉は **外部の行為**
    (pane の close/reap、再 spawn の re-assert、adopt #166) だけが開ける。止まっていた
    現職が戻ってくれば lease は active に戻る。
    """
    # 時間の刻みについて: 「renew されているから active のまま」は **上限側** の主張
    # なので、遅い CI runner (Windows) でスケジューリングが遅れると偽陽性で落ちる。
    # 1 回の poll 間隔 (0.2) を TTL (1.0) の 1/5 に取って余裕を持たせつつ、合計経過
    # (0.8) は TTL を超えるようにして「renew が効いている」ことは依然として証明する。
    b = Broker(state_dir=tmp_path, adapter=None, lease_seconds=30.0,
               observer_lease_seconds=1.0)
    _registered(b, "sec")
    secret = b.assert_observer("sec")
    dc = b.issue_delivery_cred("sec")
    gen = b.register_delivery_instance(dc, "obs", observer=secret)["generation"]
    # poll が renew するので、TTL 超の合計時間でも lease は active のまま。
    for _ in range(4):
        time.sleep(0.2)
        b.poll_claims(dc, gen, "obs")
    assert b.delivery_dump()["observers"]["sec"]["state"] == "active"
    # poll を止めて TTL 経過 -> stale。**fence は維持される** (こちらは下限側の主張
    # なので、遅れて到達しても結論は変わらない)。
    time.sleep(1.2)
    assert b.delivery_dump()["observers"]["sec"]["state"] == "stale"
    assert (b.register_delivery_instance(dc, "fork", observer=None)["error"]
            == "observer_pending")
    assert b._delivery_instances["sec"] == "obs"        # 世代交代していない
    # 止まっていた現職が戻れば active に戻る (suspend からの復帰など)。
    b.poll_claims(dc, gen, "obs")
    assert b.delivery_dump()["observers"]["sec"]["state"] == "active"
    # 秘密を持つ本人は stale の間も register できる (pane 内で sidecar が再起動した等)。
    time.sleep(1.2)
    assert b.register_delivery_instance(dc, "obs2", observer=secret)["ok"] is True


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


# ============================== Issue #165 observer lease on the spawn path
def _pane_env(fake_adapter) -> dict:
    return fake_adapter.spawned[-1]["env"]


def test_spawn_claude_asserts_observer_lease_and_hands_secret_via_pane_env(
    tmp_path, fake_adapter,
):
    """spawn_claude が observer lease を張り、秘密を **pane プロセス env** で子へ渡す。

    #165 の本体。以前この経路は delivery cred と channel sidecar を配りながら lease を
    張らず、dispatcher / 全 worker が last-register-wins に落ちていた (§4.1)。秘密は
    mcp-config に **載せない** — mcp-config は fork が verbatim replay する面そのもので、
    そこへ載せた瞬間に「fork が replay できない信号」という lease の存在理由が消える。
    """
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    fake_adapter.add_pane(active=True)
    disp = _ops(b)
    dispatch_tool(b, disp, "spawn_claude_pane",
                  {"direction": "vertical", "name": "worker-foo", "cwd": "/repo"})
    # lease が張られ、armed (expires_at=None = 失効しない) で置かれている。
    lease = b._observer_leases["worker-foo"]
    assert lease.expires_at is None
    # 秘密は pane プロセス env に載る。
    secret = _pane_env(fake_adapter)["ORG_BROKER_CHANNEL_OBSERVER"]
    assert secret == lease.secret and secret
    # **mcp-config にも argv にも載らない** (fork の replay 面に秘密を置かない)。
    argv = fake_adapter.spawned[-1]["argv"]
    assert secret not in json.dumps(argv)
    cfg = json.loads(argv[argv.index("--mcp-config") + 1])
    assert secret not in json.dumps(cfg)
    # broker 所有の env キーは呼び元の env_extra に潰されない。
    assert _pane_env(fake_adapter)["ORG_BROKER_STATE_DIR"]


def test_spawn_claude_lease_fences_fork_but_not_the_spawned_session(
    tmp_path, fake_adapter,
):
    """acceptance (#165): fork の register は generation を奪えず、original は push を
    受け取り続ける。lease 未 assert の owner は従来どおり (別テストで固定)。"""
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    fake_adapter.add_pane(active=True)
    disp = _ops(b)
    dispatch_tool(b, disp, "spawn_claude_pane",
                  {"direction": "vertical", "name": "w", "cwd": "/repo"})
    secret = _pane_env(fake_adapter)["ORG_BROKER_CHANNEL_OBSERVER"]
    dc = [t for t, bd in b._binds.items()
          if bd.agent_id == "w" and bd.scope == "delivery"][0]
    b.register_local([t for t, bd in b._binds.items()
                      if bd.agent_id == "w" and bd.scope == "full"][0])
    # spawn された session の sidecar (env の秘密を提示できる) は register できる。
    gen = b.register_delivery_instance(dc, "orig", observer=secret)["generation"]
    b.enqueue(disp, "w", "for-the-live-session")
    # mcp-config を replay した fork は秘密を持てないので generation を奪えない。
    forked = b.register_delivery_instance(dc, "fork", observer=None)
    assert forked["error"] == "observer_pending"
    assert b._delivery_instances["w"] == "orig"
    # original は fork イベントを跨いで push を受け取り続ける。
    rows = b.poll_claims(dc, gen, "orig")["rows"]
    assert [r["entry"]["message"] for r in rows] == ["for-the-live-session"]
    assert b.poll_claims(dc, gen, "fork")["error"] == "stale_sidecar"


def test_spawn_claude_lease_armed_survives_a_session_slower_than_the_ttl(
    tmp_path, fake_adapter,
):
    """acceptance (#165): 起動が TTL より遅い session が TTL で fence されない。

    2 相ライフサイクル (armed = 失効しない -> 初回 observed register で activate ->
    poll heartbeat で renew) を spawn 経路で固定する。段1 folder-trust プロンプトの
    放置等で初回 register が TTL を大きく超えても、秘密を持つ session は登録できる。
    """
    b = Broker(state_dir=tmp_path, adapter=fake_adapter,
               observer_lease_seconds=0.1)
    fake_adapter.add_pane(active=True)
    disp = _ops(b)
    dispatch_tool(b, disp, "spawn_claude_pane",
                  {"direction": "vertical", "name": "slow", "cwd": "/repo"})
    secret = _pane_env(fake_adapter)["ORG_BROKER_CHANNEL_OBSERVER"]
    dc = [t for t, bd in b._binds.items()
          if bd.agent_id == "slow" and bd.scope == "delivery"][0]
    time.sleep(0.25)          # TTL(0.1) の 2 倍以上の起動遅延
    # armed lease は失効していないので fork はまだ弾かれる。
    assert (b.register_delivery_instance(dc, "fork", observer=None)["error"]
            == "observer_pending")
    # 遅れて起きた本人は登録でき、そこで初めて TTL 計時が始まる (activate)。
    assert b.register_delivery_instance(dc, "slow-obs", observer=secret)["ok"] is True
    assert isinstance(b._observer_leases["slow"].expires_at, float)


def test_spawn_failure_clears_observer_lease(tmp_path):
    """spawn (adapter) 失敗時に observer lease も巻き戻す (誰も提示できない armed lease を
    owner に残さない)。delivery cred の失敗時 revoke と同型。"""
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
    assert "w" not in b._observer_leases


def test_name_collision_spawn_cannot_rotate_a_live_agents_lease(tmp_path, fake_adapter):
    """``assert_observer`` は ``issue_token(unique=True)`` の **後** で呼ぶ。順序が
    load-bearing になった (Issue #165 + #169)。

    先に rotate してしまうと、``spawn_claude_pane(name="<live agent>")`` を投げるだけで
    被害 agent の lease が回り、被害者自身の sidecar が「かつての秘密」を提示する側に
    なる = latch する拒否 (``unobserved``) を受けて **恒久的に mute** される。権限の
    要らない、他人のセッションを黙らせる操作になってしまう。
    """
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    fake_adapter.add_pane(active=True)
    victim = b.issue_token("secretary", "secretary", "secretary")
    b.register_local(victim)
    live_secret = b.assert_observer("secretary")     # 稼働中の observed session
    disp = _ops(b)
    out = dispatch_tool(b, disp, "spawn_claude_pane",
                        {"direction": "vertical", "name": "secretary", "cwd": "/repo"})
    assert out.get("isError") and "name_taken" in out["content"][0]["text"]
    # 被害者の lease は回っていない = 被害者の sidecar は今までどおり register できる。
    assert b._observer_leases["secretary"].secret == live_secret
    dc = b.issue_delivery_cred("secretary")
    assert b.register_delivery_instance(dc, "victim", observer=live_secret)["ok"] is True


def test_spawn_failure_rollback_does_not_clear_someone_elses_lease(tmp_path):
    """巻き戻しは **自分が張った lease だけ** を落とす (compare-and-delete)。

    失敗経路では name 予約と token が先に解放されるので、その隙に同名で別 caller が
    新しい lease を張れる。無条件 pop だと、その新 lease を消してしまう (新 session は
    mute されないが fork 保護だけが黙って外れる = 気付けない劣化)。
    """
    b = Broker(state_dir=tmp_path, adapter=None)
    mine = b.assert_observer("w")
    theirs = b.assert_observer("w")           # 別 caller が rotate した (= 自分のは失効)
    assert b.clear_observer("w", mine) is False
    assert b._observer_leases["w"].secret == theirs
    assert b.clear_observer("w", theirs) is True
    assert "w" not in b._observer_leases


def test_spawn_failure_error_and_journal_do_not_leak_the_observer_secret(tmp_path):
    """adapter は起動失敗時に引数列をそのまま例外文へ載せる。その文字列は呼び元への
    tools/call エラーになり、traceback ごと queue.jsonl にも残る (このファイルは
    admin.token と違い 0600 ではない)。秘密が両方から伏せられていること。"""
    class LeakyAdapter(FakeAdapter):
        def spawn(self, argv, cwd=None, new_window=True, space=None, env=None):
            # tmux / wezterm と同型: 引数列を例外文へ載せる。
            leak = " ".join(f"{k}={v}" for k, v in (env or {}).items())
            raise RuntimeError(f"spawn failed: -e {leak}")

    adapter = LeakyAdapter()
    b = Broker(state_dir=tmp_path, adapter=adapter)
    adapter.add_pane(active=True)
    disp = _ops(b)
    with pytest.raises(RuntimeError) as excinfo:
        dispatch_tool(b, disp, "spawn_claude_pane",
                      {"direction": "vertical", "name": "w", "cwd": "/repo"})
    raw = str(excinfo.value)
    secret = raw.split("ORG_BROKER_CHANNEL_OBSERVER=")[1].split()[0]
    # 巻き戻しで lease は消えている = 値一致だけの scrub では捕まらない状況を再現する。
    assert "w" not in b._observer_leases
    scrubbed = b.scrub_secrets(raw)
    assert secret not in scrubbed
    assert "[REDACTED_OBSERVER_SECRET]" in scrubbed
    # 他の診断情報は残る (読めない診断にしない)。
    assert "spawn failed" in scrubbed


def test_scrub_secrets_redacts_live_secret_without_the_env_prefix(tmp_path):
    """前置の無い剥き出しの値も、live な lease と一致すれば伏せる。"""
    b = Broker(state_dir=tmp_path, adapter=None)
    secret = b.assert_observer("sec")
    assert secret not in b.scrub_secrets(f"boom while starting: {secret}")
    # JSON 形 (herdr の env params) も代入形として伏せる。
    payload = '{"env": {"ORG_BROKER_CHANNEL_OBSERVER": "abc123_-XY"}}'
    assert "abc123_-XY" not in b.scrub_secrets(payload)


def test_spawn_codex_and_generic_do_not_assert_a_lease(tmp_path, fake_adapter):
    """channel sidecar を持たない spawn 経路は lease を張らない (張ると誰も秘密を提示
    できず、その owner の register が恒久的に refuse される)。"""
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    fake_adapter.add_pane(active=True)
    disp = _ops(b)
    dispatch_tool(b, disp, "spawn_codex_pane",
                  {"direction": "vertical", "name": "cx", "cwd": "/repo"})
    dispatch_tool(b, disp, "spawn_pane",
                  {"direction": "vertical", "command": "top", "cwd": "/repo"})
    assert b._observer_leases == {}


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


# ============================== Issue #169 recoverable stand-down
def _journal_events(b, event: str) -> list[dict]:
    path = b.state_dir / "queue.jsonl"
    if not path.exists():
        return []
    return [r for r in (json.loads(l) for l in path.read_text(encoding="utf-8")
                        .splitlines() if l.strip())
            if r["event"] == event]


def test_superseded_instance_cannot_win_the_claim_back_by_retrying(tmp_path):
    """acceptance (#169): supersede された instance は再試行だけでは claim を取り戻せない。

    latch が存在する理由そのもの。rotate で置き換えられた session が古い秘密を提示し
    続けても、generation は現職のまま動かない。
    """
    b = Broker(state_dir=tmp_path, adapter=None)
    _registered(b, "sec")
    s1 = b.assert_observer("sec")
    dc = b.issue_delivery_cred("sec")
    assert b.register_delivery_instance(dc, "old", observer=s1)["ok"] is True
    s2 = b.assert_observer("sec")             # 新 session が lease を rotate
    assert b.register_delivery_instance(dc, "new", observer=s2)["generation"] == 2
    # 旧 session が粘る: 何度再試行しても latch する拒否のままで generation は不変。
    for _ in range(5):
        res = b.register_delivery_instance(dc, "old", observer=s1)
        assert res["error"] == "unobserved"     # LATCHING_REFUSALS
    assert b._delivery_generations["sec"] == 2
    assert b._delivery_instances["sec"] == "new"


def test_pending_instance_recovers_when_the_pane_actually_dies(tmp_path):
    """acceptance (#169): stand-down した session が **再起動なしで** 回復する。

    ただし回復の条件は「粘ったから」でも「時間が経ったから」でもなく、**外部が
    現職の消滅を宣言したから** — ここでは pane の close/reap
    (:meth:`reset_delivery_state`)、つまり broker が実際に観測した死。
    """
    b = Broker(state_dir=tmp_path, adapter=None, lease_seconds=30.0,
               observer_lease_seconds=0.2)
    _registered(b, "sec")
    secret = b.assert_observer("sec")
    dc = b.issue_delivery_cred("sec")
    gen = b.register_delivery_instance(dc, "obs", observer=secret)["generation"]
    # 現職が poll していても、止まっていても、秘密無しの再試行は通らない。
    for _ in range(3):
        b.poll_claims(dc, gen, "obs")
        assert (b.register_delivery_instance(dc, "manual", observer=None)["error"]
                == "observer_pending")
    time.sleep(0.3)                                    # heartbeat 途絶 (stale) でも同じ
    assert (b.register_delivery_instance(dc, "manual", observer=None)["error"]
            == "observer_pending")
    # pane が実際に閉じた (close_pane / reap) -> lease は落ちる -> 同じ再試行が通る。
    # 再試行を続けていた sidecar は **プロセス再起動なしで** 配送に復帰する。
    b.reset_delivery_state("sec")
    assert b.register_delivery_instance(dc, "manual", observer=None)["ok"] is True


def test_stale_lease_is_released_when_the_pane_died_out_of_band(tmp_path, fake_adapter):
    """sticky lease の主要な解除経路 (pane の close/reap) は **opportunistic** で、
    ``resolve_target`` / ``_reserve_name`` を通る入口からしか走らない。``/claim-owner``
    は store にしか届かないので、pane が broker の外で死ぬ (crash / 端末ごと閉じる) と
    誰も reap を叩かず、再試行し続ける正統な sidecar が恒久的に push を受け取れない。

    そこで stale lease を理由に拒否した時だけ liveness probe (opportunistic reap) を
    走らせる。**TTL に戻したのではない**: 材料は adapter が答える pane の生死。

    reap 後にこの cred が受け取るのは ``unauthorized`` (``ok`` ではない) が正しい:
    reap は lease を落とすと同時に delivery cred を revoke し未配達行を purge する
    = broker はその agent を「葬った」。死んだ pane の設定を replay した session へ
    配送を再開させるのは、その purge が防いでいる cross-session 誤配送そのものになる。
    ここで直るのは「stale lease が pane より長生きして bookkeeping が残り続ける」方で、
    同 agent_id の **新しい spawn** は綺麗な状態から始められる。
    """
    b = Broker(state_dir=tmp_path, adapter=fake_adapter, lease_seconds=0.0,
               observer_lease_seconds=0.1)
    fake_adapter.add_pane(active=True)
    disp = _ops(b)
    out = _text(dispatch_tool(b, disp, "spawn_claude_pane",
                              {"direction": "vertical", "name": "w", "cwd": "/repo"}))
    secret = _pane_env(fake_adapter)["ORG_BROKER_CHANNEL_OBSERVER"]
    dc = [t for t, bd in b._binds.items()
          if bd.agent_id == "w" and bd.scope == "delivery"][0]
    b.register_delivery_instance(dc, "orig", observer=secret)
    time.sleep(0.15)                                  # heartbeat 途絶 -> stale
    # pane が **生きている** 間は stale でも fence を維持する (Ctrl+Z / suspend 相当)。
    assert (b.register_delivery_instance(dc, "manual", observer=None)["error"]
            == "observer_pending")
    assert "w" in b._observer_leases
    # pane が broker の外で消えた (crash)。close_pane は呼ばれていない。
    fake_adapter.kill_pane(out["id"])
    # 次の再試行が liveness probe を走らせ、reap が pane の bookkeeping を掃除する。
    # stale lease は pane より長生きしない。
    assert (b.register_delivery_instance(dc, "manual", observer=None)["error"]
            == "unauthorized")
    assert "w" not in b._observer_leases
    assert str(out["id"]) not in b._pane_meta
    # 同 agent_id の新しい spawn は綺麗な状態から始まり、自分の lease を張れる。
    fake_adapter.add_pane(active=True)
    dispatch_tool(b, disp, "spawn_claude_pane",
                  {"direction": "vertical", "name": "w", "cwd": "/repo"})
    fresh = _pane_env(fake_adapter)["ORG_BROKER_CHANNEL_OBSERVER"]
    assert fresh != secret
    assert b._observer_leases["w"].secret == fresh


def test_spawn_lease_expires_if_it_is_never_activated(tmp_path, fake_adapter):
    """acceptance (#165 の安全弁): 秘密が子へ届かない環境で恒久無音にならない。

    spawn 経路の lease は活性化期限つき。期限内に一度も observed register が来なければ
    (= 誰も秘密を提示できていない = 守るべき現職が存在しない)、lease を落として今日の
    last-register-wins に戻す。保護が外れる瞬間なので journal に必ず残す。
    """
    b = Broker(state_dir=tmp_path, adapter=fake_adapter, observer_arming_seconds=0.1)
    fake_adapter.add_pane(active=True)
    disp = _ops(b)
    dispatch_tool(b, disp, "spawn_claude_pane",
                  {"direction": "vertical", "name": "w", "cwd": "/repo"})
    dc = [t for t, bd in b._binds.items()
          if bd.agent_id == "w" and bd.scope == "delivery"][0]
    # 期限内は armed のまま fence する (秘密を持たない register は通らない)。
    assert (b.register_delivery_instance(dc, "no-secret")["error"]
            == "observer_pending")
    assert b.delivery_dump()["observers"]["w"]["state"] == "armed"
    time.sleep(0.15)
    # 期限切れ: lease が落ちて今日の挙動 (last-register-wins) に戻る。
    assert b.register_delivery_instance(dc, "no-secret")["ok"] is True
    assert "w" not in b._observer_leases
    assert _journal_events(b, "observer_arming_expired")[0]["owner"] == "w"


def test_secretary_path_lease_never_expires_while_armed(tmp_path):
    """launcher / secretary 経路は **無期限 armed** のまま (段1 folder-trust は人間の
    承認待ちで、分どころか時間オーダーで放置されうる)。活性化期限は spawn 経路だけ。"""
    b = Broker(state_dir=tmp_path, adapter=None, observer_arming_seconds=0.1)
    _registered(b, "sec")
    secret = b.assert_observer("sec")          # arming_seconds を渡さない = 無期限
    dc = b.issue_delivery_cred("sec")
    time.sleep(0.15)
    assert (b.register_delivery_instance(dc, "fork", observer=None)["error"]
            == "observer_pending")
    assert b.register_delivery_instance(dc, "obs", observer=secret)["ok"] is True


def test_poll_does_not_activate_an_armed_lease(tmp_path):
    """armed の activate は「秘密を提示した register」の専権。

    poll で activate できてしまうと、秘密を一度も提示していない instance が lease を
    活性化でき (docs §7 項目5)、活性化期限 = 「誰も提示できていないなら今日の挙動へ
    戻す」という #165 の安全弁が黙って無効化される。
    """
    b = Broker(state_dir=tmp_path, adapter=None, observer_arming_seconds=30.0)
    _registered(b, "w")
    dc = b.issue_delivery_cred("w")
    gen = b.register_delivery_instance(dc, "i1")["generation"]   # lease 前に register
    b.assert_observer("w", arming_seconds=30.0)                  # 後から lease を張る
    b.poll_claims(dc, gen, "i1")                                 # 現世代 instance の poll
    assert b._observer_leases["w"].expires_at is None            # armed のまま
    assert b.delivery_dump()["observers"]["w"]["state"] == "armed"


def test_fenced_instance_poll_does_not_renew_the_incumbent_lease(tmp_path):
    """§8.1「『直近に poll した誰か』ではなく『現世代 instance』を見る」の固定。

    fence された instance は stale_sidecar を受けたあとも poll を続ける。その poll が
    lease を延命できてしまうと、現職が死んでも lease が生き続け回復が塞がる (逆に、
    fenced な instance が自分で自分の道を開けるようにもなる)。renew は現世代 instance の
    poll だけが打つ。
    """
    b = Broker(state_dir=tmp_path, adapter=None, observer_lease_seconds=30.0)
    _registered(b, "sec")
    secret = b.assert_observer("sec")
    dc = b.issue_delivery_cred("sec")
    gen = b.register_delivery_instance(dc, "obs", observer=secret)["generation"]
    before = b._observer_leases["sec"].expires_at
    assert isinstance(before, float)
    # fence された instance の poll: 拒否され、lease の失効時刻を動かさない。
    assert b.poll_claims(dc, gen, "fork")["error"] == "stale_sidecar"
    assert b._observer_leases["sec"].expires_at == before
    # 現世代 instance の poll は renew する。**時計の分解能に依存させない**: Windows の
    # ``time.time()`` は ~15.6ms 刻みなので、連続 2 回の呼び出しが同値を返しうる
    # (renew されていても ``> before`` が偽になる)。期限を明らかに過去へ倒してから
    # poll し、TTL 分先へ飛ぶことを見る。stale からの復帰 (止まっていた現職が戻る
    # ケース) も同時に踏む。
    b._observer_leases["sec"].expires_at = 1.0
    b.poll_claims(dc, gen, "obs")
    assert b._observer_leases["sec"].expires_at > time.time() + 25.0


def test_delivery_dump_exposes_standdowns_and_clears_them_on_register(tmp_path):
    """acceptance (#169): stood-down 状態がプロセスの外から観測できる。

    sidecar 側の ``_stood_down`` は子プロセス内の Event で外から見えないため、daemon が
    「どの owner の どの instance が・なぜ・いつから claim していないか」を持つ。
    """
    b = Broker(state_dir=tmp_path, adapter=None)
    _registered(b, "sec")
    secret = b.assert_observer("sec")
    dc = b.issue_delivery_cred("sec")
    b.register_delivery_instance(dc, "obs", observer=secret)
    # 非 latch の拒否: 再試行中であることが count / since で読める。
    b.register_delivery_instance(dc, "manual", observer=None)
    b.register_delivery_instance(dc, "manual", observer=None)
    rec = b.delivery_dump()["standdowns"]["sec"]["manual"]
    assert rec["reason"] == "observer_pending"
    assert rec["latched"] is False and rec["count"] == 2
    assert rec["last"] >= rec["since"]
    # latch する拒否は別 instance の枠に latched=True で残る (互いを潰さない)。
    b.register_delivery_instance(dc, "old", observer="stale")
    per_owner = b.delivery_dump()["standdowns"]["sec"]
    assert per_owner["old"]["reason"] == "unobserved"
    assert per_owner["old"]["latched"] is True
    assert per_owner["manual"]["count"] == 2      # 上書きされていない
    # register が通った instance の記録だけ消える (他 instance の mute は残す —
    # takeover の瞬間に観測面を白紙に戻さない)。
    b.reset_delivery_state("sec")
    b.register_delivery_instance(dc, "manual")
    assert b.delivery_dump()["standdowns"] == {}


def test_standdown_records_survive_two_claimants_without_overwriting(tmp_path):
    """2 つの instance が交互に再試行しても互いの記録を潰さない。

    owner に 1 枠しかないと ``since`` が毎秒 now に戻り、「1 時間黙っている pane」が
    「0 秒前から」に見える。latch した正統 instance の記録が、粘っている fork に
    消されることもある (一番残すべき 1 行が消える)。
    """
    b = Broker(state_dir=tmp_path, adapter=None)
    _registered(b, "sec")
    b.assert_observer("sec")
    dc = b.issue_delivery_cred("sec")
    for _ in range(5):
        b.register_delivery_instance(dc, "fork-a", observer=None)
        b.register_delivery_instance(dc, "fork-b", observer=None)
    per_owner = b.delivery_dump()["standdowns"]["sec"]
    assert set(per_owner) == {"fork-a", "fork-b"}
    assert per_owner["fork-a"]["count"] == 5 and per_owner["fork-b"]["count"] == 5
    assert all(r["last"] >= r["since"] for r in per_owner.values())


def test_standdown_records_are_bounded_and_keep_the_latched_ones(tmp_path):
    """記録は owner あたり上限付き。溢れたら **latch していない古い記録から** 捨てる
    (latch = そのプロセスが二度と claim しないという、一番残す価値のある事実)。"""
    b = Broker(state_dir=tmp_path, adapter=None)
    _registered(b, "sec")
    b.assert_observer("sec")
    dc = b.issue_delivery_cred("sec")
    b.register_delivery_instance(dc, "superseded", observer="stale")   # latched
    for i in range(store._STANDDOWN_MAX_PER_OWNER + 4):
        b.register_delivery_instance(dc, f"fork-{i}", observer=None)
    per_owner = b.delivery_dump()["standdowns"]["sec"]
    assert len(per_owner) <= store._STANDDOWN_MAX_PER_OWNER
    assert "superseded" in per_owner        # latch した記録は生き残る


def test_fenced_poll_is_recorded_as_a_standdown(tmp_path):
    """黙っている sidecar の多数派は register 拒否ではなく **poll の fence** (世代交代
    された instance)。それが観測面から抜けていると「なぜ静かなのか」に答えられない。"""
    b = Broker(state_dir=tmp_path, adapter=None)
    _registered(b, "w")
    dc = b.issue_delivery_cred("w")
    gen = b.register_delivery_instance(dc, "i1")["generation"]
    b.register_delivery_instance(dc, "i2")          # i1 を世代交代させる
    assert b.poll_claims(dc, gen, "i1")["error"] == "stale_sidecar"
    rec = b.delivery_dump()["standdowns"]["w"]["i1"]
    assert rec["reason"] == "stale_sidecar" and rec["latched"] is False
    assert _journal_events(b, "delivery_poll_fenced")[0]["instance"] == "i1"


def test_repeated_pending_refusals_do_not_grow_the_journal(tmp_path):
    """非 latch の拒否は poll cadence で繰り返されるので、毎回 journal すると
    queue.jsonl が毎秒太る。同一 (instance, reason) の再試行は 1 行に畳む。"""
    b = Broker(state_dir=tmp_path, adapter=None)
    _registered(b, "sec")
    b.assert_observer("sec")
    dc = b.issue_delivery_cred("sec")
    for _ in range(20):
        b.register_delivery_instance(dc, "manual", observer=None)
    assert len(_journal_events(b, "delivery_register_unobserved")) == 1
    # 継続状態は journal ではなく dump 側が持つ。
    assert b.delivery_dump()["standdowns"]["sec"]["manual"]["count"] == 20


def test_reset_delivery_state_clears_standdowns(tmp_path):
    """close_pane 相当の reset で stand-down 記録も消える (同名 respawn の誤読を防ぐ)。"""
    b = Broker(state_dir=tmp_path, adapter=None)
    _registered(b, "sec")
    b.assert_observer("sec")
    dc = b.issue_delivery_cred("sec")
    b.register_delivery_instance(dc, "manual", observer=None)
    assert "sec" in b._delivery_standdowns
    b.reset_delivery_state("sec")
    assert b._delivery_standdowns == {}


# ============================== Issue #166 explicit adopt / handover (store)
def test_adopt_fences_the_incumbent_sidecar_immediately(tmp_path):
    """adopt が返った瞬間に旧 sidecar は claim 権を失う (rotate だけでは handover に
    ならない)。

    :meth:`poll_claims` は observer 秘密を **再検証しない**。秘密を差し替えるだけだと、
    旧 session は adopting sidecar が register するまで claim/confirm を続けられる =
    引き継いだつもりの会話が旧 pane に届き続ける。generation の bump を rotate と同一
    lock スコープに置くことで、handover 境界を RPC の成功ではなく fence にする。
    """
    b = Broker(state_dir=tmp_path, adapter=None, lease_seconds=30.0)
    src, _dst = _registered(b, "src"), _registered(b, "dst")
    dc, gen, iid = _sidecar(b, "dst")
    res = b.adopt_delivery("dst")
    assert res["ok"] is True and res["generation"] == gen + 1
    b.enqueue(src, "dst", "must-not-reach-the-old-session")
    fenced = b.poll_claims(dc, gen, iid)
    assert fenced["error"] == "stale_sidecar" and fenced["rows"] == []
    assert _row_states(b, "dst") == [UNDELIVERED]   # 旧 sidecar は claim していない


def test_adopt_clears_the_registered_instance_so_nobody_can_deliver(tmp_path):
    """adopt 後 register までの窓では **どの instance も** 配達できない。

    generation を進めるだけだと、旧 sidecar は ``stale_sidecar`` 応答で知った新世代番号を
    自分の instance_id で replay して配達を続けられる (既存の replay 攻撃面)。現世代
    instance を空にして初めて「誰も claim できない窓」= 引き継ぎ境界になる。
    """
    b = Broker(state_dir=tmp_path, adapter=None)
    _registered(b, "dst")
    dc, _gen, iid = _sidecar(b, "dst")
    res = b.adopt_delivery("dst")
    assert "dst" not in b._delivery_instances
    for instance in (iid, "brand-new-instance"):
        got = b.poll_claims(dc, res["generation"], instance)
        assert got["error"] == "stale_sidecar" and got["rows"] == []


def test_message_enqueued_during_the_adopt_window_is_held_not_lost(tmp_path):
    """fence されている間に届いた message は **保持** され、adopting sidecar の register
    後に配達される。

    adopt は「誰も配達できない窓」を意図的に開けるので、その窓に落ちた message が捨て
    られる (または旧 pane へ流れる) と、handover のたびに人間の依頼が静かに消える。
    """
    b = Broker(state_dir=tmp_path, adapter=None, lease_seconds=30.0)
    src, _dst = _registered(b, "src"), _registered(b, "dst")
    dc, _gen, iid = _sidecar(b, "dst")
    res = b.adopt_delivery("dst")
    b.enqueue(src, "dst", "for-the-adopting-session")
    assert _row_states(b, "dst") == [UNDELIVERED]
    assert b.poll_claims(dc, res["generation"], iid)["rows"] == []
    reg = b.register_delivery_instance(dc, "adopting",
                                       observer=res["observer_secret"])
    rows = b.poll_claims(dc, reg["generation"], "adopting")["rows"]
    assert [r["entry"]["message"] for r in rows] == ["for-the-adopting-session"]


def test_adopting_register_with_the_new_secret_completes_the_adoption(tmp_path):
    """adopt の完了条件は「秘密の発行に成功した」ではなく「adopting instance が現世代の
    claimer として登録された」こと。

    RPC 成功を完了にすると、起動に失敗した adopt が成功として記録され、誰も配達しない
    owner が沈黙のまま残る。完了は journal からも読めなければならない (失敗と完了を
    後から区別できないと、無音の原因を特定できない)。
    """
    b = Broker(state_dir=tmp_path, adapter=None)
    _registered(b, "dst")
    dc, _gen, _iid = _sidecar(b, "dst")
    res = b.adopt_delivery("dst")
    reg = b.register_delivery_instance(dc, "adopting",
                                       observer=res["observer_secret"])
    assert reg["ok"] is True and reg["generation"] == res["generation"] + 1
    assert b._pending_adoptions == {}
    done = _journal_events(b, "delivery_adopt_completed")
    assert len(done) == 1
    assert done[0]["adoption_id"] == res["adoption_id"] and done[0]["owner"] == "dst"
    assert done[0]["instance"] == "adopting"
    # 「どの register が adopt を締めたか」は register 行からも読める。
    assert [r["adopted"] for r in _journal_events(
        b, "delivery_generation_registered")] == [False, True]


def test_pre_adopt_secret_cannot_register_after_the_adopt(tmp_path):
    """adopt 前の秘密を持つ sidecar は latch する ``unobserved`` で拒否される。

    ここが非 latch (``observer_pending``) だと、handover 元の session が 1 秒ごとに
    register を叩き続け、adopting session が register する前の窓を掠め取りうる。
    adopt は「かつて秘密を持っていた側」を確実に降ろす操作でなければならない。
    """
    b = Broker(state_dir=tmp_path, adapter=None)
    _registered(b, "dst")
    old_secret = b.assert_observer("dst")
    dc = b.issue_delivery_cred("dst")
    b.register_delivery_instance(dc, "old", observer=old_secret)
    res = b.adopt_delivery("dst")
    before = b._delivery_generations["dst"]
    for _ in range(3):
        refused = b.register_delivery_instance(dc, "old-retry", observer=old_secret)
        assert refused["error"] == "unobserved"    # LATCHING_REFUSALS
    assert b._delivery_generations["dst"] == before      # generation を奪えない
    assert b._pending_adoptions["dst"].adoption_id == res["adoption_id"]


def test_adopt_requeue_returns_in_flight_rows_and_leaves_other_owners_alone(tmp_path):
    """``in_flight="requeue"`` は当該 owner の in-flight ``CLAIMED`` だけを差し戻す。

    旧 host が emit 済かは daemon には分からないので、requeue は「沈黙より重複」を選ぶ
    既定である。件数を応答に載せるのはその帰結を operator が後から追えるようにするため。
    owner で絞り込みが漏れると、無関係な agent の live claim を剥がして不要な再配送
    (= 別 pane での重複 action) を撒く。
    """
    b = Broker(state_dir=tmp_path, adapter=None, lease_seconds=300.0)
    src = _registered(b, "src")
    _registered(b, "dst")
    _registered(b, "other")
    dc, gen, iid = _sidecar(b, "dst")
    oc, ogen, oiid = _sidecar(b, "other", instance="io")
    b.enqueue(src, "dst", "m1")
    b.enqueue(src, "dst", "m2")
    b.enqueue(src, "other", "not-yours")
    b.poll_claims(dc, gen, iid)
    b.poll_claims(oc, ogen, oiid)
    assert _row_states(b, "dst") == [CLAIMED, CLAIMED]
    res = b.adopt_delivery("dst", in_flight="requeue")
    assert res["in_flight_policy"] == "requeue" and res["in_flight_rows"] == 2
    assert _row_states(b, "dst") == [UNDELIVERED, UNDELIVERED]
    assert _row_states(b, "other") == [CLAIMED]      # 他 owner の claim は無傷


def test_adopt_drop_policy_retires_the_in_flight_rows(tmp_path):
    """``in_flight="drop"`` は in-flight 行を ``DELIVERED`` にして **二度と配らない**。

    at-most-once が要る運用 (旧 host が既に action 済) 向けの選択肢。ここで行が生き残る
    と、drop を選んだ operator が意図した「重複 action を断つ」保証が pull 経路
    (check_messages) 側から破れる。
    """
    b = Broker(state_dir=tmp_path, adapter=None, lease_seconds=300.0)
    src, dst = _registered(b, "src"), _registered(b, "dst")
    dc, gen, iid = _sidecar(b, "dst")
    b.enqueue(src, "dst", "m1")
    b.enqueue(src, "dst", "m2")
    b.poll_claims(dc, gen, iid)
    res = b.adopt_delivery("dst", in_flight="drop")
    assert res["in_flight_policy"] == "drop" and res["in_flight_rows"] == 2
    assert _row_states(b, "dst") == [DELIVERED, DELIVERED]
    assert b.drain(dst) == []       # pull 経路からも戻ってこない


def test_adopt_started_journal_records_the_in_flight_choice(tmp_path):
    """選んだ policy と件数を ``delivery_adopt_started`` に残す。

    応答は CLI の標準出力に流れて消えるが、「あの handover で会話末尾を捨てたのか
    重複させたのか」は事後に必ず問われる。journal に残らないと、静かに片方へ倒れていた
    ことを後から証明できない。
    """
    b = Broker(state_dir=tmp_path, adapter=None, lease_seconds=300.0)
    src, _dst = _registered(b, "src"), _registered(b, "dst")
    dc, gen, iid = _sidecar(b, "dst")
    b.enqueue(src, "dst", "in-flight")
    b.poll_claims(dc, gen, iid)
    res = b.adopt_delivery("dst", in_flight="drop")
    started = _journal_events(b, "delivery_adopt_started")
    assert len(started) == 1
    assert started[0]["owner"] == "dst"
    assert started[0]["adoption_id"] == res["adoption_id"]
    assert started[0]["generation"] == res["generation"]
    assert started[0]["in_flight_policy"] == "drop"
    assert started[0]["in_flight_rows"] == 1
    assert started[0]["forced"] is False


def test_old_sidecar_confirm_after_adopt_is_fenced_not_idempotent(tmp_path):
    """adopt 後の旧 sidecar の confirm は ``stale_sidecar``。**冪等成功にしない**。

    ``drop`` policy は行を ``DELIVERED`` にするので、generation 照合を confirm の冪等
    分岐より後ろに置くと旧 sidecar は ``{"ok": True, "idempotent": True}`` を受け取る。
    旧 host は「自分が届けた」と信じて何事もなく次を待ち、adopt が起きたことに永久に
    気付かない (二重 host が観測面から消える)。
    """
    b = Broker(state_dir=tmp_path, adapter=None, lease_seconds=300.0)
    src, _dst = _registered(b, "src"), _registered(b, "dst")
    dc, gen, iid = _sidecar(b, "dst")
    b.enqueue(src, "dst", "x")
    claimed = b.poll_claims(dc, gen, iid)
    rid = claimed["rows"][0]["id"]
    b.adopt_delivery("dst", in_flight="drop")
    conf = b.confirm_delivered(dc, rid, claimed["epoch"], gen, iid)
    assert conf["ok"] is False and conf["error"] == "stale_sidecar"
    assert "idempotent" not in conf


def test_second_adopt_needs_force_and_force_supersedes_the_first(tmp_path):
    """並行 adopt を **暗黙の敗北にしない** (last-rotate-wins をそのまま晒さない)。

    rotate は last-rotate-wins なので、2 回目を黙って通すと 1 回目の CLI は「成功」を
    受け取った後で既に無効な秘密を持つ session を起動し、その session は ``unobserved``
    で恒久沈黙する。既定は拒否し、上書きは ``force`` という明示の選択にする。
    """
    b = Broker(state_dir=tmp_path, adapter=None)
    _registered(b, "dst")
    dc = b.issue_delivery_cred("dst")
    first = b.adopt_delivery("dst")
    busy = b.adopt_delivery("dst")
    assert busy["ok"] is False and "[adopt_in_flight]" in busy["error"]
    assert busy["adoption_id"] == first["adoption_id"]
    # 拒否は先行 adopt を一切動かさない (拒否のついでに秘密が回ると最悪)。
    assert b._pending_adoptions["dst"].adoption_id == first["adoption_id"]
    assert b._observer_leases["dst"].secret == first["observer_secret"]
    second = b.adopt_delivery("dst", force=True)
    assert second["ok"] is True and second["adoption_id"] != first["adoption_id"]
    sup = _journal_events(b, "delivery_adopt_superseded")
    assert len(sup) == 1 and sup[0]["adoption_id"] == first["adoption_id"]
    # 先行 adopt が起動した session は latch する拒否を受ける (沈黙の原因が journal に
    # 残っているので診断できる)。
    assert b.register_delivery_instance(
        dc, "first-session", observer=first["observer_secret"])["error"] == "unobserved"


def test_adopt_unknown_owner_installs_no_lease(tmp_path):
    """存在しない owner への adopt は拒否し、**lease を残さない**。

    typo を成功にすると、operator は handover したつもりで、実際には誰も居ない名前に
    armed lease を張って終わる。その lease は失効しないので、後日その名前で起動した
    session が ``observer_pending`` で永久に claim できなくなる。
    """
    b = Broker(state_dir=tmp_path, adapter=None)
    res = b.adopt_delivery("typo-worker")
    assert res["ok"] is False and "[unknown_owner]" in res["error"]
    assert "typo-worker" not in b._observer_leases
    assert b._pending_adoptions == {}
    # その名前で後から起動する session を塞いでいない。
    _registered(b, "typo-worker")
    dc = b.issue_delivery_cred("typo-worker")
    assert b.register_delivery_instance(dc, "i1")["ok"] is True


def test_adopt_owner_without_delivery_credential_is_refused(tmp_path):
    """delivery cred を持たない owner の adopt は拒否する (lease も張らない)。

    adopting session の channel sidecar はこの cred で ``/claim-owner`` を叩くので、
    無ければ handover しても配送は始まらない。「成功したのに何も起きない」を返すより、
    mint / spawn からやり直せと loud に落とす方が回復が早い。
    """
    b = Broker(state_dir=tmp_path, adapter=None)
    _registered(b, "pull-only")
    res = b.adopt_delivery("pull-only")
    assert res["ok"] is False and "[no_delivery_credential]" in res["error"]
    assert "pull-only" not in b._observer_leases
    assert b._pending_adoptions == {}


def test_adopt_rejects_invalid_in_flight_and_arming_seconds(tmp_path):
    """不正な policy / 期限は **副作用ゼロで** 弾く。

    検証が fence の後ろに落ちると、引数を打ち間違えただけで旧 sidecar が降ろされ、
    誰も配達しない owner が残る (操作は失敗したのに配送だけ止まる、最も不可解な壊れ方)。
    """
    b = Broker(state_dir=tmp_path, adapter=None)
    _registered(b, "dst")
    b.issue_delivery_cred("dst")
    bad = b.adopt_delivery("dst", in_flight="purge")
    assert bad["ok"] is False and "[invalid_in_flight]" in bad["error"]
    for value in (0, -1.0, "soon"):
        got = b.adopt_delivery("dst", arming_seconds=value)
        assert got["ok"] is False and "[invalid_arming_seconds]" in got["error"]
    assert b._pending_adoptions == {} and "dst" not in b._observer_leases
    assert "dst" not in b._delivery_generations


def test_adopt_expires_when_the_adopting_session_never_registers(tmp_path):
    """期限内に adopting register が来なければ adopt は **失敗** で、必ず journal に残る。

    adopt は旧 sidecar をその場で降ろすので、起動に失敗すると誰も配達しない owner が
    残る。この失敗が沈黙すると「秘密は出したので成功」という最悪の記録だけが残り、
    attention watcher が operator へ能動通知する材料も無くなる。
    """
    b = Broker(state_dir=tmp_path, adapter=None, adopt_arming_seconds=0.05)
    _registered(b, "dst")
    b.issue_delivery_cred("dst")
    res = b.adopt_delivery("dst")
    assert res["arming_seconds"] == 0.05
    time.sleep(0.1)                       # 期限を **過ぎた** ことだけを主張する
    b.adopt_status("dst")                 # sweep 入口
    expired = _journal_events(b, "delivery_adopt_expired")
    assert len(expired) == 1
    assert expired[0]["owner"] == "dst"
    assert expired[0]["adoption_id"] == res["adoption_id"]
    assert expired[0]["lease_dropped"] is True
    assert b._pending_adoptions == {} and "dst" not in b._observer_leases
    assert b.adopt_status("dst")["pending"] is None


def test_expired_adopt_restores_the_previous_sidecars_delivery_path(tmp_path):
    """**回帰**: 期限切れは lease を落とすだけでは足りない。fence 前の
    ``(generation, instance)`` を戻して、旧 sidecar の配達を実際に復活させる。

    fence された旧 sidecar は ``stale_sidecar`` で latch せず poll を続けるだけで、
    **register し直さない** (一度成功した instance は再 register 経路を通らない)。復帰が
    無いと「lease は外れたが claimer が 1 つも居ない」owner が pane を閉じるまで残る =
    arming deadline が防ぐはずだった恒久無音そのものになる。journal ではなく
    ``poll_claims`` が通ることで固定する (ここは fenced sidecar の poll 自身が sweep 入口
    でもある)。
    """
    b = Broker(state_dir=tmp_path, adapter=None, lease_seconds=30.0,
               adopt_arming_seconds=0.05)
    src, _dst = _registered(b, "src"), _registered(b, "dst")
    dc, gen, iid = _sidecar(b, "dst")
    b.adopt_delivery("dst")
    b.enqueue(src, "dst", "back-to-the-incumbent")
    time.sleep(0.1)
    rows = b.poll_claims(dc, gen, iid)["rows"]
    assert [r["entry"]["message"] for r in rows] == ["back-to-the-incumbent"]
    assert b._delivery_generations["dst"] == gen
    assert b._delivery_instances["dst"] == iid
    expired = _journal_events(b, "delivery_adopt_expired")
    assert len(expired) == 1
    assert expired[0]["restored"] is True
    assert expired[0]["restored_generation"] == gen


def test_closing_the_superseded_pane_does_not_kill_the_adopted_session(
    tmp_path, fake_adapter,
):
    """**回帰**: adopt 済み owner の旧 pane を閉じても、adopting session の資格情報と
    未配達行は道連れにならない。

    ``_cleanup_pane`` は pane を「その owner の唯一の実体」とみなし、close / reap で
    token を revoke し delivery cred を revoke し delivery state を reset し未配達行を
    捨てる。adopt は所有権を別プロセスへ移すので、この前提は adopt の瞬間に偽になる。
    しかも ``org adopt`` は operator に「旧 pane は都合のよい時に閉じてよい」と案内する
    = 案内どおりに操作した瞬間に、たった今引き継いだ session が MCP も配達も失う。
    pane meta には切り離し印だけを付け、pane 自体は今までどおり畳めるようにする。
    """
    b = Broker(state_dir=tmp_path, adapter=fake_adapter, lease_seconds=30.0)
    src = _registered(b, "src")
    fake_adapter.add_pane(active=True)
    disp = _ops(b)
    out = _text(dispatch_tool(b, disp, "spawn_claude_pane",
                              {"direction": "vertical", "name": "w", "cwd": "/repo"}))
    owner_token = [t for t, bd in b._binds.items()
                   if bd.agent_id == "w" and bd.scope == "full"][0]
    b.register_local(owner_token)   # 配送先になるには registered が要る
    res = b.adopt_delivery("w")
    assert res["ok"] is True and res["detached_panes"] == [str(out["id"])]
    assert b.enqueue(src, "w", "must-survive-closing-the-husk")["ok"] is True

    closed = b.close_pane_target(str(out["id"]))   # 案内どおり抜け殻を閉じる
    assert closed.get("ok") is not False
    assert str(out["id"]) not in b._pane_meta      # pane は畳めている

    # adopting session が使う資格情報と queue は生きている。
    assert b._binds[owner_token].revoked is False
    live_creds = [t for t, bd in b._binds.items()
                  if bd.agent_id == "w" and bd.scope == "delivery" and not bd.revoked]
    assert live_creds, "delivery cred was revoked with the superseded pane"
    assert _row_states(b, "w") == [UNDELIVERED]
    # そして adopt 済みの秘密で register すれば実際に配達が始まる。
    reg = b.register_delivery_instance(
        live_creds[0], "adopted", observer=res["observer_secret"])
    assert reg["ok"] is True
    rows = b.poll_claims(live_creds[0], reg["generation"], "adopted")["rows"]
    assert [r["entry"]["message"] for r in rows] == ["must-survive-closing-the-husk"]


def test_forced_adopt_expiry_restores_the_original_incumbent_not_the_fence(tmp_path):
    """**回帰**: ``force`` で先行 adopt を supersede した時、復帰先は先行 adopt が
    fence した **後** の中間状態ではなく、最初に fence する前の現職でなければならない。

    素直に「今の generation / instance」を控えると、それは先行 adopt が既に bump し
    instance を空にした後の値になる。その状態を「原状」として復帰すると、instance が
    空のままの generation が入り、元の sidecar は stale のまま = 復帰したつもりで owner は
    無音のまま残る。adopt を 2 回続けて失敗させるという、最も慌てている時に踏む経路。
    """
    b = Broker(state_dir=tmp_path, adapter=None, lease_seconds=30.0,
               adopt_arming_seconds=0.05)
    src, _dst = _registered(b, "src"), _registered(b, "dst")
    dc, gen, iid = _sidecar(b, "dst")
    b.adopt_delivery("dst")                       # 1 回目: 現職を fence
    b.adopt_delivery("dst", force=True)           # 2 回目: 1 回目を supersede
    b.enqueue(src, "dst", "back-to-the-original-incumbent")
    time.sleep(0.1)
    rows = b.poll_claims(dc, gen, iid)["rows"]    # 元の現職が復活していること
    assert [r["entry"]["message"] for r in rows] == ["back-to-the-original-incumbent"]
    assert b._delivery_generations["dst"] == gen
    assert b._delivery_instances["dst"] == iid


def test_expired_adopt_does_not_clobber_a_lease_installed_after_the_deadline(tmp_path):
    """compare-and-delete: 期限切れが落とすのは **自分が張った lease だけ**。

    期限後に別経路 (再 spawn の re-assert / 後続 adopt) が張り直した lease を巻き添えに
    すると、今まさに有効な fork 保護が黙って外れる。新しい session は mute されないので
    誰も気付かないまま、replay した fork が takeover できる状態に戻る。
    """
    b = Broker(state_dir=tmp_path, adapter=None, adopt_arming_seconds=0.05)
    _registered(b, "dst")
    dc = b.issue_delivery_cred("dst")
    b.adopt_delivery("dst")
    time.sleep(0.1)
    fresh = b.assert_observer("dst")      # 期限後に張り直された別 caller の lease
    b.adopt_status("dst")                 # sweep 入口
    assert b._pending_adoptions == {}
    assert b._observer_leases["dst"].secret == fresh
    assert _journal_events(b, "delivery_adopt_expired")[0]["lease_dropped"] is False
    assert b.register_delivery_instance(dc, "fresh-obs", observer=fresh)["ok"] is True


def test_expired_adopt_does_not_restore_the_fence_after_someone_registered(tmp_path):
    """compare-and-restore: 期限切れが **現に配達している sidecar を蹴らない**。

    adopt を経ずに再 spawn した session が先に register していると、無条件の原状復帰は
    現世代 instance を旧 instance へ差し替えてしまい、生きている session を無音にする。
    復帰は「現世代が adopt の張った世代のままで、かつ誰も register していない」時だけ。
    """
    b = Broker(state_dir=tmp_path, adapter=None, lease_seconds=30.0)
    src, _dst = _registered(b, "src"), _registered(b, "dst")
    dc, _gen, _iid = _sidecar(b, "dst")
    b.adopt_delivery("dst")
    # adopt の秘密ではなく、再 spawn の re-assert で立った別 session が先に register する。
    respawn = b.assert_observer("dst")
    reg = b.register_delivery_instance(dc, "respawned", observer=respawn)
    assert "dst" in b._pending_adoptions          # adoption_id が違うので締まっていない
    # 期限だけを過去へ倒す (sleep で待つ形にすると「register は期限前に入る」という
    # 上限側の仮定が要り、遅い runner で崩れる。ここで見たいのは時間の経過ではなく
    # sweep の compare-and-restore)。
    b._pending_adoptions["dst"].armed_until = 0.0
    b.enqueue(src, "dst", "for-the-live-session")   # lock 外の sweep 入口
    expired = _journal_events(b, "delivery_adopt_expired")
    assert len(expired) == 1
    assert expired[0]["restored"] is False and expired[0]["restored_generation"] is None
    assert expired[0]["lease_dropped"] is False
    assert b._observer_leases["dst"].secret == respawn
    assert b._delivery_instances["dst"] == "respawned"
    rows = b.poll_claims(dc, reg["generation"], "respawned")["rows"]
    assert [r["entry"]["message"] for r in rows] == ["for-the-live-session"]


def test_an_expired_adoption_does_not_block_the_next_adopt(tmp_path):
    """失敗した adopt が owner を人質に取らない。

    sweep が adopt 入口の先頭に無いと、1 回目が失敗した owner は期限の記録が残ったまま
    ``[adopt_in_flight]`` を返し続け、operator は「既に死んでいる adopt」に対して
    ``force`` を強要される (force は先行 session を降ろす意味を持つので、意味論が濁る)。
    """
    b = Broker(state_dir=tmp_path, adapter=None, adopt_arming_seconds=0.05)
    _registered(b, "dst")
    b.issue_delivery_cred("dst")
    first = b.adopt_delivery("dst")
    time.sleep(0.1)
    second = b.adopt_delivery("dst")
    assert second["ok"] is True and second["adoption_id"] != first["adoption_id"]
    assert _journal_events(b, "delivery_adopt_expired")[0]["adoption_id"] == \
        first["adoption_id"]
    # 期限切れ経路であって force の supersede ではない。
    assert _journal_events(b, "delivery_adopt_superseded") == []


def test_expired_adopt_is_swept_by_check_messages_when_nothing_polls(tmp_path):
    """poll 入口だけに sweep を置かない。

    adopt の主用途は「session が死んだ owner の引き継ぎ」で、そこでは poll する sidecar
    が 1 つも居ない。まさに通知したいケース (adopt したが起動に失敗した dead owner) だけ
    が永久に検知されないのを防ぐため、pull 経路 (check_messages) も sweep 入口になる。
    """
    b = Broker(state_dir=tmp_path, adapter=None, adopt_arming_seconds=0.05)
    src, dst = _registered(b, "src"), _registered(b, "dst")
    b.issue_delivery_cred("dst")
    res = b.adopt_delivery("dst")
    b.enqueue(src, "dst", "someone-is-waiting")
    time.sleep(0.1)
    assert [m["message"] for m in b.drain(dst)] == ["someone-is-waiting"]
    expired = _journal_events(b, "delivery_adopt_expired")
    assert len(expired) == 1 and expired[0]["adoption_id"] == res["adoption_id"]


def test_adopt_arms_an_owner_that_never_registered_a_sidecar(tmp_path):
    """generation も lease も無い owner で adopt が成立する。

    adopt の主用途は「session が死んだ / 手起動で resume した」owner の引き継ぎであり、
    そこでは register 済 (= generation あり) でも observed (= lease あり) でもない。
    既存状態を前提にすると、本来の用途がまるごと弾かれる。
    """
    b = Broker(state_dir=tmp_path, adapter=None)
    _registered(b, "dst")
    dc = b.issue_delivery_cred("dst")
    assert "dst" not in b._delivery_generations and "dst" not in b._observer_leases
    res = b.adopt_delivery("dst")
    assert res["ok"] is True and res["generation"] == 1
    lease = b._observer_leases["dst"]
    assert lease.secret == res["observer_secret"]
    # armed (TTL では失効しない) + 有限の活性化期限、という adopt 経路の相。
    assert lease.expires_at is None and isinstance(lease.arming_until, float)
    assert b._pending_adoptions["dst"].adoption_id == res["adoption_id"]
    # 秘密を渡された adopting session だけが claimer になれる。
    assert b.register_delivery_instance(
        dc, "adopting", observer=res["observer_secret"])["ok"] is True


def test_reset_delivery_state_cancels_a_pending_adoption(tmp_path):
    """pane が閉じたら進行中 adopt は cancel する (黙って消さない)。

    残すと期限まで後続 adopt が ``[adopt_in_flight]`` で塞がれ、期限後には「もう居ない
    owner の adopt が失敗した」という誤解を招く通知が出る。cancel を journal に残すのは、
    ``delivery_adopt_expired`` が来ないことの説明を残すため。
    """
    b = Broker(state_dir=tmp_path, adapter=None)
    _registered(b, "dst")
    b.issue_delivery_cred("dst")
    res = b.adopt_delivery("dst")
    b.reset_delivery_state("dst")
    assert b._pending_adoptions == {}
    cancelled = _journal_events(b, "delivery_adopt_cancelled")
    assert len(cancelled) == 1
    assert cancelled[0]["owner"] == "dst"
    assert cancelled[0]["adoption_id"] == res["adoption_id"]
    assert cancelled[0]["reason"] == "delivery_reset"
    # cancel 済なので次の adopt は force 無しで通る。
    assert b.adopt_delivery("dst")["ok"] is True


def test_delivery_dump_exposes_the_pending_adoption_without_the_secret(tmp_path):
    """「誰も配達していない窓」を dump 単独で説明できるようにする (秘密は載せない)。

    adopt〜register の間は generations / instances だけ見ても「instance が消えている」
    としか読めず、事故と区別できない。一方 dump は admin 診断で人手に渡り貼り付けられる
    面なので、ここに秘密が載ると handover を横取りできる材料が配られる。
    """
    b = Broker(state_dir=tmp_path, adapter=None)
    _registered(b, "dst")
    b.issue_delivery_cred("dst")
    res = b.adopt_delivery("dst", in_flight="drop")
    dump = b.delivery_dump()
    rec = dump["adoptions"]["dst"]
    assert rec["adoption_id"] == res["adoption_id"]
    assert rec["in_flight_policy"] == "drop" and rec["in_flight_rows"] == 0
    assert rec["fenced_generation"] == res["generation"]
    assert res["observer_secret"] not in json.dumps(dump)


def test_adopt_never_writes_the_observer_secret_to_the_journal(tmp_path):
    """queue.jsonl は ``admin.token`` と違い 0600 ではない。

    adopt の秘密がここに落ちると、ファイルを読めるだけで配達所有権を横取りできる =
    「fork が replay できない信号」という lease の存在理由が消える。adopt / 完了 /
    status のどの経路でも書かないことを、実ファイルの本文で固定する。
    """
    b = Broker(state_dir=tmp_path, adapter=None, lease_seconds=300.0)
    src, _dst = _registered(b, "src"), _registered(b, "dst")
    dc, gen, iid = _sidecar(b, "dst")
    b.enqueue(src, "dst", "x")
    b.poll_claims(dc, gen, iid)
    res = b.adopt_delivery("dst")
    b.register_delivery_instance(dc, "adopting", observer=res["observer_secret"])
    b.adopt_status("dst")
    text = (b.state_dir / "queue.jsonl").read_text(encoding="utf-8")
    assert res["observer_secret"] not in text
    assert "delivery_adopt_completed" in text     # vacuous pass でないことの担保


def test_scrub_secrets_redacts_a_pending_adoptions_secret_after_a_rotate(tmp_path):
    """lease が後続 rotate で差し替わった **後** も、進行中 adopt の秘密は伏せる。

    adopt が起こした session の spawn 失敗例外は、その秘密を env 代入形ではなく剥き出しの
    値として運びうる (adapter 実装依存)。live な lease の一致だけを見ていると、rotate した
    瞬間からその値が診断文字列と queue.jsonl へ素通りする。
    """
    b = Broker(state_dir=tmp_path, adapter=None)
    _registered(b, "dst")
    b.issue_delivery_cred("dst")
    res = b.adopt_delivery("dst")
    b.assert_observer("dst")      # lease は別秘密へ rotate (pending はまだ生きている)
    assert b._observer_leases["dst"].secret != res["observer_secret"]
    scrubbed = b.scrub_secrets(
        f"spawn failed while adopting: {res['observer_secret']}")
    assert res["observer_secret"] not in scrubbed
    assert "[REDACTED_OBSERVER_SECRET]" in scrubbed
    assert "spawn failed while adopting" in scrubbed   # 診断は読めるまま


def test_adopt_arming_seconds_defaults_to_the_broker_tunable(tmp_path):
    """既定の活性化期限は ``Broker(adopt_arming_seconds=...)`` から取る。

    モジュール定数 (300s) を直接読むと tunable が黙って効かなくなり、期限まわりのテストは
    「300 秒待たないので何も起きない」= vacuous pass に化ける (壊れているのに緑になる)。
    """
    b = Broker(state_dir=tmp_path, adapter=None, adopt_arming_seconds=1.5)
    _registered(b, "dst")
    b.issue_delivery_cred("dst")
    res = b.adopt_delivery("dst")
    assert res["arming_seconds"] == 1.5
    assert store.DEFAULT_ADOPT_ARMING_SECONDS != 1.5   # 定数の素通しではない
    # 期限も tunable 由来 (定数由来なら now+300 になり、この上限を必ず超える)。
    assert b._pending_adoptions["dst"].armed_until <= time.time() + 1.5


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
    # 秘密無し (fork replay) は非 latch の observer_pending (Issue #169)。
    st, body = _post(broker.base_url + "/claim-owner", delivery, {"instance_id": "fork"})
    assert st == 200 and body["error"] == "observer_pending"
    # 秘密を提示したが不一致 (= supersede された) は latch する unobserved。
    st, body = _post(broker.base_url + "/claim-owner", delivery,
                     {"instance_id": "old", "observer": "stale-secret"})
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


def _start_sidecar(env: dict) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-m", "claude_org_runtime.broker.channel_sidecar"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=env,
    )
    proc.stdin.write(
        (json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-06-18"}}) + "\n").encode()
    )
    proc.stdin.write(
        (json.dumps({"jsonrpc": "2.0",
                     "method": "notifications/initialized"}) + "\n").encode()
    )
    proc.stdin.flush()
    return proc


def _await(predicate, timeout: float = 15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = predicate()
        if got:
            return got
        time.sleep(0.1)
    return None


def test_spawn_lease_end_to_end_over_the_wire(tmp_path, fake_adapter):
    """#165 + #169 を **別プロセス + 実 HTTP** で結線する。

    子 claude は起こせないので、その手前まで忠実に組む: spawn_claude が組み立てた
    (a) mcp-config の channel env ブロック (fork が verbatim replay できる面) と
    (b) pane プロセス env (fork が継承できない面) を、実際の子と同じ形で合成して
    sidecar subprocess に渡す。

    - mcp-config だけを持つ sidecar (= fork replay) は claim できず、**latch もせず**
      再試行し続ける (再試行は daemon 側の count が増えることで外から観測できる)。
    - pane env の秘密も持つ sidecar (= spawn された本人) は register して配達する。
    """
    b = Broker(state_dir=tmp_path / "broker", adapter=fake_adapter, port=0,
               lease_seconds=30.0)
    b.start()
    forked = legit = None
    try:
        fake_adapter.add_pane(active=True)
        disp = _ops(b)
        dispatch_tool(b, disp, "spawn_claude_pane",
                      {"direction": "vertical", "name": "w", "cwd": "/repo"})
        argv = fake_adapter.spawned[-1]["argv"]
        cfg = json.loads(argv[argv.index("--mcp-config") + 1])
        replayable = cfg["mcpServers"]["org-broker-channel"]["env"]
        pane_env = fake_adapter.spawned[-1]["env"]
        assert "ORG_BROKER_CHANNEL_OBSERVER" not in replayable   # 秘密は replay 面に無い

        b.register_local([t for t, bd in b._binds.items()
                          if bd.agent_id == "w" and bd.scope == "full"][0])
        src = b.issue_token("src", "src", "worker")
        b.register_local(src)
        b.enqueue(b.get_bind(src), "w", "for-the-live-session")

        base = {**os.environ, **replayable,
                "ORG_BROKER_CHANNEL_POLL_INTERVAL": "0.2",
                "PYTHONPATH": os.pathsep.join(sys.path)}

        # (1) fork replay: mcp-config だけを replay した sidecar。
        forked = _start_sidecar(dict(base))
        rec = _await(lambda: b.delivery_dump()["standdowns"].get("w"))
        assert rec is not None, "fork's refusal was not recorded"
        inst, first = next(iter(rec.items()))
        assert first["reason"] == "observer_pending" and first["latched"] is False
        # **latch していない**ことを外から観測する: 再試行のたび count が増える。
        assert _await(lambda:
                      b.delivery_dump()["standdowns"]["w"][inst]["count"] >= 3), \
            "fork stopped retrying (it latched) instead of staying recoverable"
        # 当然、行は claim されず残っている (fork は message を破壊しない)。
        assert _row_states(b, "w") == [UNDELIVERED]

        # (2) spawn された本人: pane env の秘密を持つので register して配達できる。
        legit = _start_sidecar({**base,
                                "ORG_BROKER_CHANNEL_OBSERVER":
                                    pane_env["ORG_BROKER_CHANNEL_OBSERVER"]})
        assert _await(lambda: _row_states(b, "w") == [DELIVERED]), \
            "the spawned session's sidecar never delivered the row"
        # fork は今も claim していない (takeover は起きていない)。
        assert b._delivery_instances["w"] != inst
    finally:
        for proc in (forked, legit):
            if proc is None:
                continue
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        b.stop()


# ---------------------------------------------------------------------------
# Issue #151 A: 意味的 kind の受け渡しと venv 継承方式の backend 分岐
# ---------------------------------------------------------------------------

def _kind_of_last_spawn(adapter):
    return adapter.spawned[-1]["kind"]


def test_spawn_threads_semantic_kind_to_capable_backend(tmp_path):
    """broker が知っている種別を spawn(kind=) で明示的に渡す (Codex Blocker 2)。

    herdr 0.7.5 の agent.start は kind が必須で実行ファイルをこれで決める。
    argv[0] からの推測は venv wrapper 経路 (argv[0] がシェルになる) や generic
    spawn (任意コマンド) で破綻するため、broker 側の意味的な種別が唯一の出所。
    """
    adapter = FakeAdapter(supports_agent_kind=True)
    b = Broker(state_dir=tmp_path / "sd", adapter=adapter)
    adapter.add_pane(active=True)
    disp = _ops(b)

    dispatch_tool(b, disp, "spawn_claude_pane",
                  {"direction": "vertical", "name": "w-claude"})
    assert _kind_of_last_spawn(adapter) == "claude"

    dispatch_tool(b, disp, "spawn_codex_pane",
                  {"direction": "vertical", "name": "w-codex"})
    assert _kind_of_last_spawn(adapter) == "codex"

    sec = _ops(b, "s2", "secretary")
    dispatch_tool(b, sec, "spawn_pane",
                  {"direction": "horizontal", "command": "watch ls", "name": "w-gen"})
    # generic は agent ではないので None (「渡されなかった」ではなく明示的に None)。
    assert _kind_of_last_spawn(adapter) is None


def test_spawn_omits_kind_for_backends_without_the_capability(tmp_path):
    """tmux / wezterm の spawn シグネチャは不変に保つ (kind を渡さない)。"""
    adapter = FakeAdapter()  # supports_agent_kind=False (既定)
    b = Broker(state_dir=tmp_path / "sd", adapter=adapter)
    adapter.add_pane(active=True)
    dispatch_tool(b, _ops(b), "spawn_claude_pane",
                  {"direction": "vertical", "name": "w"})
    assert _kind_of_last_spawn(adapter) is conftest._UNSET


def test_venv_pane_env_backend_gets_virtual_env_without_argv_rewrite(tmp_path):
    """``venv_path_via_pane_env`` な backend では argv を書き換えない (#151 A)。

    herdr 0.7.5 は agent.start から argv が消えるため login-shell wrapper を運べない。
    broker は VIRTUAL_ENV だけ env に載せ、PATH prepend は adapter が pane 生成後
    (profile 初期化完了後) に打ち込む契約にする。**PATH を env dict に載せない**のは
    Issue #130 Blocker 2 (profile が env 経由 PATH を再構築して .venv/bin を消す)
    をここでも踏まないため。
    """
    venv = _make_venv(tmp_path)
    adapter = FakeAdapter(venv_path_via_pane_env=True)
    b = Broker(state_dir=tmp_path / "sd", adapter=adapter, root_cwd=str(tmp_path))
    adapter.add_pane(active=True)
    dispatch_tool(b, _ops(b), "spawn_claude_pane",
                  {"direction": "vertical", "name": "w", "cwd": str(tmp_path)})
    sp = adapter.spawned[-1]
    assert sp["env"]["VIRTUAL_ENV"] == str(venv)
    assert "PATH" not in sp["env"]          # profile に潰されるので載せない
    assert sp["argv"][0] == "claude"        # wrapper で包まない


def test_venv_wrapper_backend_is_unchanged(tmp_path):
    """既定 backend (tmux/wezterm/herdr legacy) は従来の wrapper 方式のまま。"""
    venv = _make_venv(tmp_path)
    adapter = FakeAdapter()  # venv_path_via_pane_env=False
    b = Broker(state_dir=tmp_path / "sd", adapter=adapter, root_cwd=str(tmp_path))
    adapter.add_pane(active=True)
    dispatch_tool(b, _ops(b), "spawn_claude_pane",
                  {"direction": "vertical", "name": "w", "cwd": str(tmp_path)})
    _assert_pane_venv_activated(adapter.spawned[-1], venv)
