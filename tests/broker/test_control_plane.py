# -*- coding: utf-8 -*-
"""broker daemon 制御面のテスト (runtime#63 タスク 1)。

Codex design review が org up/down launcher の前提として要求した 3 つの土台を
検証する:

1. daemon sidecar 契約 — serve が ``daemon.json`` (pid/host/port/state_dir(絶対)/
   backend/started_at/journal_offset) と ``admin.token`` (0600) を書き、停止時に
   削除する。
2. 管理面 — 走行中 daemon への admin HTTP RPC: 新規 root token の mint (tier 指定可)
   と graceful shutdown。admin 認証なしアクセスは拒否される。
3. shutdown は stop() 経由で journal に ``broker_stopped`` を残し、down は
   journal_offset スライスでそれを検証する (全履歴 grep の偽陽性回避)。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from claude_org_runtime.broker import cli as broker_cli
from claude_org_runtime.broker import sidecar
from claude_org_runtime.broker.server import Broker
from claude_org_runtime.broker.surface import tools_for


# --------------------------------------------------------------------- helpers
def _admin_post(broker: Broker, body: dict | None, token: str | None,
                expect_status: int = 200):
    """admin HTTP RPC を 1 回叩く小さなクライアント。"""
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        broker.admin_url,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
            payload = resp.read()
    except urllib.error.HTTPError as e:
        status = e.code
        payload = e.read()
    assert status == expect_status, f"status {status} != {expect_status}: {payload!r}"
    return json.loads(payload) if payload else None


@pytest.fixture
def admin_broker(tmp_path):
    """admin token を持つ started broker (adapter=None)。"""
    b = Broker(state_dir=tmp_path / "broker", adapter=None, port=0,
               admin_token="ADMIN-SECRET")
    b.start()
    try:
        yield b
    finally:
        b.stop()


# ===================================================================== sidecar
def test_sidecar_roundtrip_and_fields(tmp_path):
    # write → read で全契約フィールドが往復し、state_dir が絶対化される。
    sidecar.write_sidecar(
        tmp_path, pid=4321, host="127.0.0.1", port=48720, backend="tmux",
        started_at=1781234567.0, journal_offset=128,
    )
    data = sidecar.read_sidecar(tmp_path)
    assert data["pid"] == 4321
    assert data["host"] == "127.0.0.1"
    assert data["port"] == 48720
    assert data["backend"] == "tmux"
    assert data["started_at"] == 1781234567.0
    assert data["journal_offset"] == 128
    # state_dir は絶対パスで記録される (Codex review Minor: 入口で絶対化)。
    assert data["state_dir"] == sidecar.absolutize(tmp_path)
    import os
    assert os.path.isabs(data["state_dir"])


def test_sidecar_backend_none_for_no_nudge(tmp_path):
    # --no-nudge (adapter 無し) は backend=None を記録する (健全性判定が照合可)。
    sidecar.write_sidecar(
        tmp_path, pid=1, host="127.0.0.1", port=0, backend=None,
        started_at=0.0, journal_offset=0,
    )
    assert sidecar.read_sidecar(tmp_path)["backend"] is None


def test_remove_sidecar_is_idempotent(tmp_path):
    sidecar.write_sidecar(
        tmp_path, pid=1, host="127.0.0.1", port=0, backend="tmux",
        started_at=0.0, journal_offset=0,
    )
    sidecar.write_admin_token(tmp_path, "tok")
    assert sidecar.read_sidecar(tmp_path) is not None
    assert sidecar.read_admin_token(tmp_path) == "tok"
    sidecar.remove_sidecar(tmp_path)
    sidecar.remove_sidecar(tmp_path)  # 二度目も例外なし (冪等)
    assert sidecar.read_sidecar(tmp_path) is None
    assert sidecar.read_admin_token(tmp_path) is None


def test_read_journal_since_avoids_prior_run_false_positive(tmp_path):
    """journal_offset スライスが過去 run の broker_stopped を拾わないことを検証。

    Codex review Major の核心: 全履歴 grep は過去 run の残留で偽陽性になる。
    偽の過去 broker_stopped を 1 行書いてからオフセットを取り、当該 run の
    broker_stopped を append する。スライスは当該 run の 1 件のみを返すべき
    (素朴な grep なら 2 件マッチして偽陽性になる)。
    """
    jpath = tmp_path / sidecar.JOURNAL_NAME
    # 過去 run の残留 (偽の broker_stopped + 無関係イベント)。
    jpath.write_text(
        json.dumps({"ts": 1.0, "event": "broker_stopped"}) + "\n"
        + json.dumps({"ts": 2.0, "event": "message_enqueued"}) + "\n",
        encoding="utf-8",
    )
    offset = sidecar.journal_offset(tmp_path)  # この run の起点
    # 当該 run の追記 (started → stopped)。
    with jpath.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": 3.0, "event": "broker_started"}) + "\n")
        f.write(json.dumps({"ts": 4.0, "event": "broker_stopped"}) + "\n")

    sliced = sidecar.read_journal_since(tmp_path, offset)
    stopped = [e for e in sliced if e["event"] == "broker_stopped"]
    assert len(stopped) == 1                 # 当該 run の 1 件のみ
    assert stopped[0]["ts"] == 4.0           # 過去 run (ts=1.0) ではない
    # 素朴な全履歴 grep なら 2 件マッチする (= 回避できていることの対比)。
    whole = sidecar.read_journal_since(tmp_path, 0)
    assert len([e for e in whole if e["event"] == "broker_stopped"]) == 2


# =============================================================== admin: mint
@pytest.mark.parametrize("role", ["worker", "curator", "dispatcher", "secretary"])
def test_admin_mint_token_reflects_tier(admin_broker, role):
    # admin RPC で mint した token の auth_role が要求 tier どおりで、tools/list の
    # 公開面を駆動する (Codex review Blocker 1: 走行中 daemon への token mint 経路)。
    res = _admin_post(admin_broker, {"method": "mint_token",
                                     "params": {"role": role}}, "ADMIN-SECRET")
    assert res["ok"] is True
    assert res["role"] == role
    bind = admin_broker.get_bind(res["token"])
    assert bind is not None
    assert bind.auth_role == role
    # mint した token の公開面が tier どおり。
    assert {t["name"] for t in tools_for(bind.auth_role)} == {
        t["name"] for t in tools_for(role)
    }
    # mcp-config に同 token が埋まり、そのまま使える。
    hdr = res["mcp_config"]["mcpServers"]["org-broker"]["headers"]["Authorization"]
    assert hdr == f"Bearer {res['token']}"


def test_admin_mint_token_secretary_is_full_surface(admin_broker):
    res = _admin_post(admin_broker, {"method": "mint_token",
                                     "params": {"role": "secretary"}}, "ADMIN-SECRET")
    bind = admin_broker.get_bind(res["token"])
    assert len({t["name"] for t in tools_for(bind.auth_role)}) == 13


def test_admin_mint_token_carries_cwd(admin_broker, tmp_path):
    # cwd を渡すと bind に乗る (relative spawn の解決アンカー; Issue #61 parity)。
    res = _admin_post(admin_broker, {"method": "mint_token",
                                     "params": {"role": "secretary",
                                                "cwd": str(tmp_path)}}, "ADMIN-SECRET")
    assert admin_broker.get_bind(res["token"]).cwd == str(tmp_path)


def test_admin_mint_token_rejects_unknown_role(admin_broker):
    res = _admin_post(admin_broker, {"method": "mint_token",
                                     "params": {"role": "admin"}}, "ADMIN-SECRET",
                      expect_status=400)
    assert res["ok"] is False
    assert "invalid_role" in res["error"]


# =============================================================== admin: auth
def test_admin_rejects_missing_token(admin_broker):
    # 認証なしアクセスは 401 で拒否される (Codex review Major: admin 認証付き)。
    res = _admin_post(admin_broker, {"method": "mint_token", "params": {}},
                      token=None, expect_status=401)
    assert "admin_unauthorized" in res["error"]


def test_admin_rejects_wrong_token(admin_broker):
    res = _admin_post(admin_broker, {"method": "mint_token", "params": {}},
                      token="WRONG", expect_status=401)
    assert "admin_unauthorized" in res["error"]


def test_admin_disabled_when_no_admin_token(broker):
    # admin_token 未設定 (内部テスト用 broker) は admin 経路ごと 404 で隠す。
    res = _admin_post(broker, {"method": "shutdown"}, token="anything",
                      expect_status=404)
    assert res is None


def test_admin_unknown_method_rejected(admin_broker):
    res = _admin_post(admin_broker, {"method": "frobnicate"}, "ADMIN-SECRET",
                      expect_status=400)
    assert "unknown_admin_method" in res["error"]


# ============================================================ admin: shutdown
def test_admin_shutdown_clean_stop_via_run(tmp_path, monkeypatch):
    """admin shutdown RPC が clean stop を起こし、journal_offset スライスで
    broker_stopped が厳密に 1 回確認でき、sidecar が削除されることを end-to-end で
    検証する (Codex review Blocker 2 / Major)。

    run() を daemon スレッドで起動し、sidecar から admin token を読んで shutdown を
    叩く。run() は wait_for_shutdown → finally で stop() + sidecar 削除に進む。
    """
    state_dir = str(tmp_path / "broker")
    args = broker_cli.build_parser().parse_args(
        ["serve", "--port", "0", "--no-nudge", "--state-dir", state_dir]
    )
    rc_box: dict = {}

    def _run():
        rc_box["rc"] = broker_cli.run(args)

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    # sidecar が公開されるまで待つ (port / admin token を取得)。
    deadline = time.time() + 10
    data = None
    while time.time() < deadline:
        data = sidecar.read_sidecar(state_dir)
        admin_token = sidecar.read_admin_token(state_dir)
        if data is not None and admin_token is not None:
            break
        time.sleep(0.02)
    assert data is not None, "sidecar was never published"
    assert admin_token is not None
    # sidecar 契約フィールド (run() 経由の実値)。
    assert isinstance(data["port"], int) and data["port"] > 0
    assert data["backend"] is None              # --no-nudge
    assert isinstance(data["journal_offset"], int)
    offset = data["journal_offset"]

    # admin shutdown を叩く。応答 ack を受けてから run() が停止に進む。
    url = f"http://{data['host']}:{data['port']}/admin"
    req = urllib.request.Request(
        url, data=json.dumps({"method": "shutdown"}).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {admin_token}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        ack = json.loads(resp.read())
    assert ack["ok"] is True and ack["shutting_down"] is True

    t.join(timeout=10)
    assert not t.is_alive(), "run() did not return after shutdown RPC"
    assert rc_box["rc"] == 0

    # journal_offset スライスで broker_stopped が厳密に 1 回 (全履歴 grep 不要)。
    sliced = sidecar.read_journal_since(state_dir, offset)
    stopped = [e for e in sliced if e["event"] == "broker_stopped"]
    assert len(stopped) == 1
    # broker_started もこの run のスライスに含まれる (offset は start 前に取得)。
    assert any(e["event"] == "broker_started" for e in sliced)

    # sidecar (daemon.json + admin.token) は停止時に削除される。
    assert sidecar.read_sidecar(state_dir) is None
    assert sidecar.read_admin_token(state_dir) is None
