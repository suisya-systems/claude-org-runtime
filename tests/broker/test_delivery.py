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


# ===================================================================== R4 store
def test_claim_then_confirm_lifecycle(tmp_path):
    b = Broker(state_dir=tmp_path, adapter=None)
    src, dst = _registered(b, "src"), _registered(b, "dst")
    dc = b.issue_delivery_cred("dst")
    b.enqueue(src, "dst", "hello")
    assert _row_states(b, "dst") == [UNDELIVERED]

    res = b.poll_claims(dc)
    assert len(res["rows"]) == 1 and res["epoch"] == 0
    rid = res["rows"][0]["id"]
    assert res["rows"][0]["entry"]["message"] == "hello"
    assert _row_states(b, "dst") == [CLAIMED]

    conf = b.confirm_delivered(dc, rid, res["epoch"])
    assert conf["ok"] is True
    assert _row_states(b, "dst") == [DELIVERED]
    # id 冪等: 二度目の confirm は idempotent。
    assert b.confirm_delivered(dc, rid, res["epoch"]) == {"ok": True, "idempotent": True}


def test_check_messages_respects_live_claim(tmp_path):
    """live な sidecar claim 中の行は check_messages が返さない (二重配達なし)。"""
    b = Broker(state_dir=tmp_path, adapter=None, lease_seconds=30.0)
    src, dst = _registered(b, "src"), _registered(b, "dst")
    b.enqueue(src, "dst", "m1")
    b.poll_claims(b.issue_delivery_cred("dst"))  # CLAIMED, lease 30s (まだ live)
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
    res = b.poll_claims(b.issue_delivery_cred("dst"))  # CLAIMED (sidecar 死亡で confirm せず)
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
    dc = b.issue_delivery_cred("dst")
    b.enqueue(src, "dst", "x")
    res = b.poll_claims(dc)
    rid = res["rows"][0]["id"]
    time.sleep(0.1)
    conf = b.confirm_delivered(dc, rid, res["epoch"])
    assert conf["ok"] is False and conf["error"] == "not_claimed"


def test_mode_epoch_fencing_rejects_stale_confirm(tmp_path):
    """flip で epoch が進み、旧 epoch の confirm は stale_epoch で拒否される。"""
    b = Broker(state_dir=tmp_path, adapter=None, lease_seconds=30.0)
    src, dst = _registered(b, "src"), _registered(b, "dst")
    dc = b.issue_delivery_cred("dst")
    b.enqueue(src, "dst", "x")
    res = b.poll_claims(dc)  # epoch 0, CLAIMED
    rid = res["rows"][0]["id"]
    flip = b.flip_mode("dst", PULL)  # epoch -> 1、CLAIMED -> UNDELIVERED
    assert flip["epoch"] == 1 and flip["mode"] == PULL
    assert _row_states(b, "dst") == [UNDELIVERED]
    conf = b.confirm_delivered(dc, rid, res["epoch"])  # epoch 0 (stale)
    assert conf["ok"] is False and conf["error"] == "stale_epoch" and conf["epoch"] == 1


def test_stale_confirm_does_not_strip_newer_claim(tmp_path):
    """Codex Major: stale epoch の confirm が新しい epoch の live claim を剥がさない。

    epoch 0 claim -> PULL -> PUSH (epoch 2) -> epoch 2 で再 claim。古い epoch 0 confirm が
    来ても epoch 2 の claim は無傷で、現 sidecar の epoch 2 confirm が成功する。
    """
    b = Broker(state_dir=tmp_path, adapter=None, lease_seconds=30.0)
    src, dst = _registered(b, "src"), _registered(b, "dst")
    dc = b.issue_delivery_cred("dst")
    b.enqueue(src, "dst", "x")
    first = b.poll_claims(dc)             # epoch 0, CLAIMED
    rid = first["rows"][0]["id"]
    b.flip_mode("dst", PULL)              # epoch 1, row -> UNDELIVERED
    b.flip_mode("dst", PUSH)             # epoch 2
    second = b.poll_claims(dc)            # epoch 2, 再 CLAIMED
    assert second["epoch"] == 2 and len(second["rows"]) == 1
    # 古い epoch 0 confirm: 拒否されるが epoch 2 の claim は剥がさない。
    stale = b.confirm_delivered(dc, rid, first["epoch"])
    assert stale["error"] == "stale_epoch"
    assert _row_states(b, "dst") == [CLAIMED]   # 新 claim 無傷
    # 現 sidecar の epoch 2 confirm は成功する (剥がされていない証拠)。
    assert b.confirm_delivered(dc, rid, second["epoch"])["ok"] is True


def test_pull_mode_disables_claim_issuance(tmp_path):
    """PULL mode は poll_claims を拒否するが check_messages は不変 (§9.3)。"""
    b = Broker(state_dir=tmp_path, adapter=None)
    src, dst = _registered(b, "src"), _registered(b, "dst")
    b.flip_mode("dst", PULL)
    b.enqueue(src, "dst", "m1")
    res = b.poll_claims(b.issue_delivery_cred("dst"))
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
    dc = b.issue_delivery_cred("dst")
    src = _registered(b, "src")
    # registered な src 経由で enqueue (宛先解決のため dst を一時 register して戻す)。
    b.register_local(full)
    b.enqueue(src, "dst", "do-not-lose-me")
    # ここで dst が DELETE された状況を模す (registered=False)。
    b.get_bind(full).registered = False
    res = b.poll_claims(dc)
    assert res["error"] == "owner_unregistered" and res["rows"] == []
    assert _row_states(b, "dst") == [UNDELIVERED]   # 行は残る (喪失しない)
    # re-initialize (registered に戻る) で claim 可能になる。
    b.get_bind(full).registered = True
    res2 = b.poll_claims(dc)
    assert [r["entry"]["message"] for r in res2["rows"]] == ["do-not-lose-me"]


def test_poll_claims_only_returns_owner_rows(tmp_path):
    b = Broker(state_dir=tmp_path, adapter=None)
    src = _registered(b, "src")
    _registered(b, "dst")
    _registered(b, "dst2")
    b.enqueue(src, "dst", "for-dst")
    b.enqueue(src, "dst2", "for-dst2")
    res = b.poll_claims(b.issue_delivery_cred("dst"))
    assert [r["entry"]["message"] for r in res["rows"]] == ["for-dst"]


def test_confirm_not_owner_rejected(tmp_path):
    b = Broker(state_dir=tmp_path, adapter=None)
    src = _registered(b, "src")
    _registered(b, "dst")
    _registered(b, "other")
    b.enqueue(src, "dst", "x")
    res = b.poll_claims(b.issue_delivery_cred("dst"))
    rid = res["rows"][0]["id"]
    # 別 owner の cred は他人宛の行を confirm できない (owner=cred.agent_id で判定)。
    other_cred = b.issue_delivery_cred("other")
    assert b.confirm_delivered(other_cred, rid, res["epoch"])["error"] == "not_owner"


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
    res = b.poll_claims(dc)
    assert res["error"] == "unauthorized" and res["rows"] == []
    assert _row_states(b, "dst") == [UNDELIVERED]   # revoked cred では claim されない
    assert b.confirm_delivered(dc, "anyid", 0)["error"] == "unauthorized"
    # 完全に未知の token も同様。
    assert b.poll_claims("bogus-token")["error"] == "unauthorized"


def test_flip_mode_invalid(tmp_path):
    b = Broker(state_dir=tmp_path, adapter=None)
    res = b.flip_mode("dst", "SHOVE")
    assert res["ok"] is False and "invalid_mode" in res["error"]


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
    status, _ = _post(broker.base_url + "/poll-claims", full, {})
    assert status == 401
    # delivery cred は通る。
    status, body = _post(broker.base_url + "/poll-claims", delivery, {})
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

    status, body = _post(broker.base_url + "/poll-claims", delivery, {})
    assert status == 200 and len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["entry"]["message"] == "wire-hello"

    status, conf = _post(broker.base_url + "/confirm-delivered", delivery,
                         {"id": row["id"], "epoch": row["epoch"]})
    assert status == 200 and conf["ok"] is True


def test_confirm_invalid_id_400(broker):
    delivery = broker.issue_delivery_cred("dst")
    status, body = _post(broker.base_url + "/confirm-delivered", delivery,
                         {"id": 123, "epoch": 0})
    assert status == 400 and "invalid_id" in body["error"]


# ================================================================ R3 spawn 儀式
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
        def spawn(self, argv, cwd=None, new_window=True):
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
