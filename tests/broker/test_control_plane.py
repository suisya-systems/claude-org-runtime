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
import signal
import threading
import time
import urllib.error
import urllib.request

import pytest

from claude_org_runtime.broker import cli as broker_cli
from claude_org_runtime.broker import sidecar
from claude_org_runtime.broker import store
from claude_org_runtime.broker.server import Broker
from claude_org_runtime.broker.surface import tools_for

from .conftest import MiniMcpClient


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


def test_admin_token_written_atomically_and_0600(tmp_path):
    # admin.token は temp → rename で atomic publish され、.tmp を残さない。
    import os
    import stat
    path = sidecar.write_admin_token(tmp_path, "SECRET-TOKEN")
    assert sidecar.read_admin_token(tmp_path) == "SECRET-TOKEN"
    assert not (tmp_path / (sidecar.ADMIN_TOKEN_NAME + ".tmp")).exists()
    # 0600 (POSIX のみ実効。Windows は read-only ビットのみで group/other を本当には
    # 落とせない既知制限のため、owner-read だけ確認する)。
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode & stat.S_IRUSR
    if os.name != "nt":
        assert mode & (stat.S_IRWXG | stat.S_IRWXO) == 0


def test_read_admin_token_empty_is_none(tmp_path):
    # 空ファイル (理論上の torn read / 外部 truncate) は公開済み token と誤認しない。
    (tmp_path / sidecar.ADMIN_TOKEN_NAME).write_text("", encoding="utf-8")
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
    # absolute cwd を渡すと as-is で bind に乗る (relative spawn の解決アンカー)。
    res = _admin_post(admin_broker, {"method": "mint_token",
                                     "params": {"role": "secretary",
                                                "cwd": str(tmp_path)}}, "ADMIN-SECRET")
    assert admin_broker.get_bind(res["token"]).cwd == str(tmp_path)


def test_admin_mint_token_absolutizes_relative_cwd(admin_broker):
    # relative cwd は daemon 起動 cwd 基準で絶対化される (Issue #61 が admin 経路で
    # 再発しないこと。Codex review Major)。
    import os
    res = _admin_post(admin_broker, {"method": "mint_token",
                                     "params": {"role": "secretary",
                                                "cwd": "rel/sub"}}, "ADMIN-SECRET")
    cwd = admin_broker.get_bind(res["token"]).cwd
    assert os.path.isabs(cwd)
    assert cwd == os.path.abspath("rel/sub")


def test_admin_mint_token_default_agent_id_is_unique(admin_broker):
    # 既定 (name 省略) で複数回 mint すると別 agent_id になる (固定名による
    # bind/queue 共有・配送先曖昧化を避ける。Codex review Major)。
    r1 = _admin_post(admin_broker, {"method": "mint_token",
                                    "params": {"role": "worker"}}, "ADMIN-SECRET")
    r2 = _admin_post(admin_broker, {"method": "mint_token",
                                    "params": {"role": "worker"}}, "ADMIN-SECRET")
    assert r1["agent_id"] != r2["agent_id"]
    assert r1["token"] != r2["token"]


def test_admin_mint_token_honors_explicit_name(admin_broker):
    # 明示 name 指定時はそれを agent_id に使う。
    res = _admin_post(admin_broker, {"method": "mint_token",
                                     "params": {"role": "secretary",
                                                "name": "org-up-secretary"}}, "ADMIN-SECRET")
    assert res["agent_id"] == "org-up-secretary"
    assert admin_broker.get_bind(res["token"]).name == "org-up-secretary"


def test_admin_mint_token_rejects_duplicate_explicit_name(admin_broker):
    # 同一 explicit name で再 mint すると拒否される (queue 共有・誤配送を防ぐ。
    # Codex review round 2 Major: 明示 name の重複防御)。
    r1 = _admin_post(admin_broker, {"method": "mint_token",
                                    "params": {"role": "worker", "name": "dup"}}, "ADMIN-SECRET")
    assert r1["ok"] is True
    r2 = _admin_post(admin_broker, {"method": "mint_token",
                                    "params": {"role": "worker", "name": "dup"}}, "ADMIN-SECRET",
                     expect_status=400)
    assert r2["ok"] is False
    assert "name_taken" in r2["error"]


def test_admin_mint_token_channel_wires_sidecar(admin_broker):
    # channel=True で mint した secretary の mcp_config に org-broker-channel sidecar が
    # 積まれ (OWNER=この agent)、delivery-scoped credential が発行される。これが
    # secretary(窓口) 起動経路の push 一次配送 channel 配線 (本タスクの本丸)。
    res = _admin_post(admin_broker, {"method": "mint_token",
                                     "params": {"role": "secretary",
                                                "name": "secretary",
                                                "channel": True}}, "ADMIN-SECRET")
    assert res["ok"] is True
    servers = res["mcp_config"]["mcpServers"]
    # full token 用の org-broker と channel sidecar の両方が載る。
    assert "org-broker" in servers
    assert "org-broker-channel" in servers
    chan = servers["org-broker-channel"]
    env = chan["env"]
    assert env["ORG_BROKER_CHANNEL_OWNER"] == "secretary"
    # delivery cred は full token とは別物 (least-privilege)。
    delivery_cred = env["ORG_BROKER_CHANNEL_CRED"]
    assert delivery_cred != res["token"]
    dbind = admin_broker.get_bind(delivery_cred)
    assert dbind is not None and dbind.scope == "delivery"
    assert dbind.agent_id == "secretary"


def test_admin_mint_token_without_channel_has_no_sidecar(admin_broker):
    # channel 既定 (省略) では org-broker-channel を積まず、delivery cred も leak
    # しない (control-plane の probe / down ctrl token がこの経路)。
    res = _admin_post(admin_broker, {"method": "mint_token",
                                     "params": {"role": "secretary"}}, "ADMIN-SECRET")
    assert "org-broker-channel" not in res["mcp_config"]["mcpServers"]
    # delivery-scoped bind が新たに発行されていない。
    assert not any(b.scope == "delivery" for b in admin_broker._binds.values())


def test_admin_mint_token_rejects_non_bool_channel(admin_broker):
    # channel は厳密 bool。truthy 文字列で credential 発行が誤発火しないよう
    # 非 bool は [invalid_params] で拒否する。
    res = _admin_post(admin_broker, {"method": "mint_token",
                                     "params": {"role": "secretary",
                                                "channel": "true"}}, "ADMIN-SECRET",
                      expect_status=400)
    assert res["ok"] is False
    assert "invalid_params" in res["error"]
    # 拒否時は delivery cred を発行しない。
    assert not any(b.scope == "delivery" for b in admin_broker._binds.values())


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


# ====================================================== admin: flip_mode (R4)
def test_admin_flip_mode_advances_epoch(admin_broker):
    """admin flip_mode RPC が per-agent delivery_mode を反転し epoch を進める (§9.3)。"""
    res = _admin_post(admin_broker, {"method": "flip_mode",
                                     "params": {"owner": "w", "mode": "PULL"}},
                      "ADMIN-SECRET")
    assert res["ok"] is True and res["mode"] == "PULL" and res["epoch"] == 1
    # 同 mode への再 flip は no-op (epoch 据置)。
    res2 = _admin_post(admin_broker, {"method": "flip_mode",
                                      "params": {"owner": "w", "mode": "PULL"}},
                       "ADMIN-SECRET")
    assert res2["epoch"] == 1


def test_admin_flip_mode_rejects_bad_params(admin_broker):
    res = _admin_post(admin_broker, {"method": "flip_mode", "params": {"owner": "w"}},
                      "ADMIN-SECRET", expect_status=400)
    assert "invalid_params" in res["error"]


def test_admin_delivery_dump(admin_broker):
    """delivery_dump RPC が配送ライフサイクルの横断スナップショットを返す (admin scope)。"""
    res = _admin_post(admin_broker, {"method": "delivery_dump"}, "ADMIN-SECRET")
    assert res["ok"] is True and "by_state" in res and "modes" in res


# ======================================================== admin: adopt (#166)
def _mint_channel_owner(broker: Broker, name: str) -> tuple[str, str]:
    """channel 付きの owner を 1 体 mint し ``(full token, delivery cred)`` を返す。

    adopt は既存 owner の配達所有権を移す操作なので、full bind と delivery
    credential を **両方** 持つ owner が前提になる (片方でも欠ければ store が弾く)。
    """
    res = _admin_post(broker, {"method": "mint_token",
                               "params": {"role": "secretary", "name": name,
                                          "channel": True}}, "ADMIN-SECRET")
    assert res["ok"] is True
    env = res["mcp_config"]["mcpServers"]["org-broker-channel"]["env"]
    return res["token"], env["ORG_BROKER_CHANNEL_CRED"]


def _adopt(broker: Broker, params: dict, token: str | None = "ADMIN-SECRET",
           expect_status: int = 200):
    """adopt_delivery admin RPC を 1 回叩く。"""
    return _admin_post(broker, {"method": "adopt_delivery", "params": params},
                       token, expect_status=expect_status)


def _adopt_status(broker: Broker, params: dict, token: str | None = "ADMIN-SECRET",
                  expect_status: int = 200):
    """adopt_status admin RPC を 1 回叩く。"""
    return _admin_post(broker, {"method": "adopt_status", "params": params},
                       token, expect_status=expect_status)


def _fence_state(broker: Broker, owner: str) -> tuple[dict, int]:
    """「rotate が起きたか」を判定する 2 値 (進行中 adopt, 現 generation) を取る。

    拒否されたはずの adopt が副作用だけ残していないことを、拒否系テストが
    この 1 組で確認する。
    """
    dump = broker.delivery_dump()
    return dump["adoptions"], dump["generations"].get(owner, 0)


def test_admin_adopt_delivery_returns_handover_payload(admin_broker):
    """adopt_delivery が adopting session を起動できる完全な handover 応答を返す。

    ここが欠けると operator は「rotate は起きたが何を持って claude を起動すれば
    よいか分からない」状態に置かれる。adopt は応答を返す時点で現職を fence 済み
    なので、その窓は「誰も配達しない owner」を arming 期限まで放置することになる。
    """
    _mint_channel_owner(admin_broker, "sec")
    before = time.time()
    res = _adopt(admin_broker, {"owner": "sec"})
    assert res["ok"] is True
    assert res["owner"] == "sec"
    assert isinstance(res["adoption_id"], str) and res["adoption_id"]
    assert isinstance(res["observer_secret"], str) and res["observer_secret"]
    # fence は RPC の内側で完了している (generation が進む)。
    assert res["generation"] == 1
    assert res["in_flight_policy"] == "requeue"      # 既定 policy
    assert res["in_flight_rows"] == 0                # CLAIMED 行なし
    # 有限の活性化期限が入る (**下限のみ**主張: 呼び出し前時刻 + arming 以上)。
    assert res["armed_until"] >= before + res["arming_seconds"]
    servers = res["mcp_config"]["mcpServers"]
    # adopting session は full token の http server と channel sidecar の両方を要る。
    assert servers["org-broker"]["type"] == "http"
    assert servers["org-broker-channel"]["env"]["ORG_BROKER_CHANNEL_OWNER"] == "sec"


def test_admin_adopt_delivery_does_not_leak_internal_keys(admin_broker):
    """内部フィールド (``_owner_token`` / ``_delivery_cred``) がワイヤに出ない。

    store は「検証と同一 lock スコープで取った資格情報」をこの 2 キーで返す。
    server が pop し忘れると、admin 応答をそのまま端末やログに貼る運用で owner の
    full token が mcp_config の外へもう 1 部流れる (露出面が黙って増える)。
    """
    _mint_channel_owner(admin_broker, "sec")
    res = _adopt(admin_broker, {"owner": "sec"})
    assert "_owner_token" not in res
    assert "_delivery_cred" not in res


def test_admin_adopt_delivery_rekeys_the_bind_instead_of_minting_a_second(
    admin_broker,
):
    """adopt は bind を **付け替える**: 新規 mint もせず、旧 token も残さない。

    mint し直すと同一 agent_id に bind が 2 本並び、配送先解決と観測が曖昧化する。
    かといって旧 token をそのまま使い回すと、旧プロセスと adopting プロセスが 1 つの
    bind (= 1 つの ``session_id``) を共有し、双方の initialize が互いの session を
    上書きして両方 ``[session_invalid]`` に落ちうる (decision note §4.5 の session
    steal と同じ形)。鍵だけ差し替えることで「bind は 1 本のまま、旧プロセスの MCP
    面は閉じる」を同時に満たす。
    """
    token, cred = _mint_channel_owner(admin_broker, "sec")
    bind_before = admin_broker.get_bind(token)
    res = _adopt(admin_broker, {"owner": "sec"})
    servers = res["mcp_config"]["mcpServers"]
    adopted = servers["org-broker"]["headers"]["Authorization"].removeprefix("Bearer ")
    # full token は付け替わり、旧 token はもう解決しない (旧プロセスは締め出される)。
    assert adopted != token
    assert admin_broker.get_bind(token) is None
    # bind オブジェクトは同一 (registered / cwd / role を引き継いでいる)。
    assert admin_broker.get_bind(adopted) is bind_before
    assert admin_broker.get_bind(adopted).session_id is None
    # bind は 1 本のまま (mint していない)。delivery cred は使い回す。
    assert len([b for b in admin_broker._binds.values()
                if b.agent_id == "sec" and b.scope == "full"]) == 1
    assert servers["org-broker-channel"]["env"]["ORG_BROKER_CHANNEL_CRED"] == cred


def test_admin_adopt_delivery_keeps_observer_secret_out_of_mcp_config(admin_broker):
    """observer 秘密は top-level のみに現れ、mcp_config には一切載らない。

    mcp_config は fork/resume で replay される面である。そこへ秘密が混じると
    replay した session が現職を詐称でき、「replay されない process env でだけ
    正統性を見分ける」という lease の存在根拠がまるごと無効になる。
    """
    _mint_channel_owner(admin_broker, "sec")
    res = _adopt(admin_broker, {"owner": "sec"})
    secret = res["observer_secret"]
    assert secret and secret not in json.dumps(res["mcp_config"])


@pytest.mark.parametrize("policy", ["requeue", "drop"])
def test_admin_adopt_delivery_echoes_in_flight_policy(admin_broker, policy):
    """要求した in-flight policy と適用件数が応答に残る。

    policy が応答に残らないと、operator は「drop したつもりが requeue された」を
    事後に確認できず、二重配達 / 取りこぼしの原因を辿れない。
    """
    _mint_channel_owner(admin_broker, "sec")
    res = _adopt(admin_broker, {"owner": "sec", "in_flight": policy})
    assert res["in_flight_policy"] == policy
    assert res["in_flight_rows"] == 0


def test_admin_adopt_delivery_arming_default_follows_daemon_tunable(tmp_path):
    """``arming_seconds`` 省略時の既定は Broker の tunable を反映する。

    モジュール定数 (300s) を直接読む実装に戻ると ``Broker(adopt_arming_seconds=...)``
    が黙って効かなくなり、期限まわりの検証が 300 秒待ちの vacuous pass に化ける。
    """
    b = Broker(state_dir=tmp_path / "broker", adapter=None, port=0,
               admin_token="ADMIN-SECRET", adopt_arming_seconds=1234.0)
    b.start()
    try:
        _mint_channel_owner(b, "sec")
        res = _adopt(b, {"owner": "sec"})
        assert res["arming_seconds"] == 1234.0
    finally:
        b.stop()


def test_admin_adopt_delivery_honors_explicit_arming_seconds(admin_broker):
    """明示された ``arming_seconds`` は float に正規化して受理される。

    int を弾いたり無視したりすると、operator が短い期限を選んでも既定の長さで
    armed のままになり、失敗した adopt の検知 (``delivery_adopt_expired``) が
    意図した時刻に発火しない。
    """
    _mint_channel_owner(admin_broker, "sec")
    res = _adopt(admin_broker, {"owner": "sec", "arming_seconds": 42})
    assert res["arming_seconds"] == 42.0


@pytest.mark.parametrize("bad_token", [None, "WRONG"])
def test_admin_adopt_delivery_requires_admin_token(admin_broker, bad_token):
    """admin bearer 無し / 不一致の adopt は 401 で、**何も rotate しない**。

    adopt は現職を無条件に fence する操作なので、認証に失敗した要求が副作用だけ
    残すと、拒否されたはずの呼び出しが owner を無音にできてしまう (fence は
    巻き戻らない — 旧 sidecar は二度と register し直さない)。
    """
    _mint_channel_owner(admin_broker, "sec")
    res = _adopt(admin_broker, {"owner": "sec"}, token=bad_token,
                 expect_status=401)
    assert "admin_unauthorized" in res["error"]
    assert _fence_state(admin_broker, "sec") == ({}, 0)


def test_admin_adopt_delivery_rejects_agent_and_delivery_credentials(admin_broker):
    """agent の full token でも delivery credential でも adopt には到達できない。

    adopt は observer lease より強い操作 (現職を無条件に fence できる) である。
    lease より弱い主体 — とりわけ乗っ取り側 sidecar が持つ delivery cred — から
    到達できると、fork した session が所有権を自分へ奪う経路になる。
    """
    token, cred = _mint_channel_owner(admin_broker, "sec")
    for bearer in (token, cred):
        res = _adopt(admin_broker, {"owner": "sec"}, token=bearer,
                     expect_status=401)
        assert "admin_unauthorized" in res["error"]
    assert _fence_state(admin_broker, "sec") == ({}, 0)


def test_adopt_is_absent_from_the_mcp_tool_surface(admin_broker):
    """最上位 tier (secretary) の tools/list にも adopt 系ツールは現れない。

    ツール面に出ると、エージェント自身が自分や他人の配達所有権を奪えることになる。
    admin token (0600 の ``admin.token``) を持つ operator だけの操作である、という
    認可境界を tier 最上位の実 tools/list で固定する。
    """
    res = _admin_post(admin_broker, {"method": "mint_token",
                                     "params": {"role": "secretary"}},
                      "ADMIN-SECRET")
    c = MiniMcpClient(admin_broker.url, res["token"])
    c.rpc("initialize", {"protocolVersion": "2025-06-18"})
    c.notify("notifications/initialized")
    names = {t["name"] for t in c.rpc("tools/list")["result"]["tools"]}
    assert [n for n in names if "adopt" in n] == []
    # catalogue 側 (tier フィルタの入力) にもそもそも存在しない。
    assert [t["name"] for t in tools_for("secretary") if "adopt" in t["name"]] == []


@pytest.mark.parametrize("params,code", [
    ({}, "invalid_params"),                                  # owner 欠落
    ({"owner": 7}, "invalid_params"),                         # 非文字列 owner
    ({"owner": ""}, "invalid_params"),                        # 空 owner
    ({"owner": "sec", "force": "true"}, "invalid_params"),    # truthy 文字列
    ({"owner": "sec", "in_flight": 1}, "invalid_params"),     # 非文字列 policy
    ({"owner": "sec", "in_flight": "nuke"}, "invalid_in_flight"),
    ({"owner": "sec", "arming_seconds": "30"}, "invalid_params"),
    ({"owner": "sec", "arming_seconds": True}, "invalid_params"),  # bool は数値でない
    ({"owner": "sec", "arming_seconds": 0}, "invalid_arming_seconds"),
])
def test_admin_adopt_delivery_rejects_bad_params(admin_broker, params, code):
    """壊れたパラメータは分類コード付き 400 で拒否され、**rotate は起きない**。

    truthy 文字列を bool と見なすような緩い受理は、operator が意図しない force
    supersede を踏む経路そのものになる。fence は巻き戻せないので、こうした要求は
    副作用を出す前に入口で落とし切る必要がある。
    """
    _mint_channel_owner(admin_broker, "sec")
    res = _adopt(admin_broker, params, expect_status=400)
    assert res["ok"] is False
    assert res["error"].startswith(f"[{code}]")
    assert _fence_state(admin_broker, "sec") == ({}, 0)


def test_admin_adopt_delivery_unknown_owner_is_400(admin_broker):
    """存在しない owner への adopt を成功にしない。

    typo を成功にすると、operator は handover したつもりで、実際には誰も居ない
    owner に lease を張って終わる (= 将来その名前で起動する session を
    ``observer_pending`` で塞ぐ) — 失敗が沈黙する最悪の形になる。
    """
    res = _adopt(admin_broker, {"owner": "ghost"}, expect_status=400)
    assert res["ok"] is False
    assert res["error"].startswith("[unknown_owner]")
    assert _fence_state(admin_broker, "ghost") == ({}, 0)


def test_admin_adopt_delivery_without_delivery_credential_is_400(admin_broker):
    """delivery credential を持たない owner の adopt は拒否される。

    adopting session の channel sidecar はその cred で ``/claim-owner`` を叩く。
    無いまま「成功」を返すと fence だけ済んで配達が二度と始まらない。
    """
    _admin_post(admin_broker, {"method": "mint_token",
                               "params": {"role": "secretary", "name": "nochan"}},
                "ADMIN-SECRET")
    res = _adopt(admin_broker, {"owner": "nochan"}, expect_status=400)
    assert res["ok"] is False
    assert res["error"].startswith("[no_delivery_credential]")
    assert _fence_state(admin_broker, "nochan") == ({}, 0)


def test_admin_adopt_delivery_conflict_requires_force(admin_broker):
    """期限内の未完了 adopt がある間、2 本目は force 無しでは拒否される。

    rotate は last-rotate-wins なので、黙って許すと先行 CLI は「成功」を受け取った
    後で **既に無効な秘密**を持つ session を起動し、その session は unobserved で
    恒久に沈黙する。競合を暗黙の敗北ではなく明示の選択にする。
    """
    _mint_channel_owner(admin_broker, "sec")
    first = _adopt(admin_broker, {"owner": "sec"})
    clash = _adopt(admin_broker, {"owner": "sec"}, expect_status=400)
    assert clash["ok"] is False
    assert clash["error"].startswith("[adopt_in_flight]")
    assert clash["adoption_id"] == first["adoption_id"]   # 現職の adopt を名指す
    forced = _adopt(admin_broker, {"owner": "sec", "force": True})
    assert forced["ok"] is True
    assert forced["adoption_id"] != first["adoption_id"]
    assert forced["observer_secret"] != first["observer_secret"]


def test_admin_adopt_status_reports_pending_without_secret(admin_broker):
    """adopt_status は進行中 adopt を報告するが、秘密は決して返さない。

    ``org adopt`` は exec 直前の preflight にこれを使う。ここで秘密を返すと
    「起動せずに現職の秘密を読む」経路になり、非 replay 秘密を 1 プロセスの env に
    閉じ込める契約が崩れる。
    """
    _mint_channel_owner(admin_broker, "sec")
    started = _adopt(admin_broker, {"owner": "sec", "in_flight": "drop"})
    res = _adopt_status(admin_broker, {"owner": "sec"})
    assert res["ok"] is True and res["owner"] == "sec"
    assert res["generation"] == started["generation"]
    assert res["instance_id"] is None                  # fence 済 (claimer 不在)
    assert res["observer_state"] == store.OBSERVER_ARMED
    pending = res["pending"]
    assert pending["adoption_id"] == started["adoption_id"]
    assert pending["in_flight_policy"] == "drop"
    assert pending["in_flight_rows"] == 0
    assert pending["fenced_generation"] == started["generation"]
    # 残 arming は経過に依らず 0 以上・要求値以下 (時刻に依存しない不変量)。
    assert 0.0 <= pending["armed_seconds_remaining"] <= started["arming_seconds"]
    assert started["observer_secret"] not in json.dumps(res)


def test_admin_adopt_status_pending_is_none_when_idle(admin_broker):
    """進行中 adopt が無い owner では ``pending`` が None になる。

    決着済み adoption の残骸を返し続けると、``org adopt`` の preflight がそれを
    現職と誤認し、既に無効な秘密のまま session を起動してしまう。
    """
    _mint_channel_owner(admin_broker, "sec")
    res = _adopt_status(admin_broker, {"owner": "sec"})
    assert res["ok"] is True
    assert res["pending"] is None
    assert res["generation"] == 0 and res["instance_id"] is None
    assert res["observer_state"] == store.OBSERVER_NONE


@pytest.mark.parametrize("params", [{}, {"owner": 7}, {"owner": ""}])
def test_admin_adopt_status_rejects_bad_owner(admin_broker, params):
    """owner が欠落 / 非文字列 / 空の status 要求は 400 で拒否される。

    非文字列 owner を素通しすると dict lookup が黙って miss し、「pending なし」を
    正常応答として返す — preflight が空振りしていることに気付けなくなる。
    """
    res = _adopt_status(admin_broker, params, expect_status=400)
    assert res["ok"] is False
    assert "invalid_params" in res["error"]


def test_admin_adopt_delivery_exception_is_rendered_as_400(admin_broker, monkeypatch):
    """ハンドラ内の例外は 400 ``[adopt_failed]`` 本文になり、無言で接続を切らない。

    ``/admin`` には catch-all が無い (tools/call の except は ``/mcp`` 専用) ため、
    ここで例外が漏れると応答を書かないままソケットが閉じ、CLI からは「daemon 不到達」と
    区別できない — adopt は現職を fence した **後** に落ちうる操作なので、その
    取り違えは最悪の診断ミスになる。例外文は scrub_secrets を通す。
    """
    _mint_channel_owner(admin_broker, "sec")
    live = _adopt(admin_broker, {"owner": "sec"})["observer_secret"]

    def _boom(*args, **kwargs):
        raise RuntimeError(f"backend exploded with {live}")

    monkeypatch.setattr(admin_broker, "adopt_delivery", _boom)
    res = _adopt(admin_broker, {"owner": "sec", "force": True}, expect_status=400)
    assert res["ok"] is False
    assert res["error"].startswith("[adopt_failed] RuntimeError")
    assert live not in res["error"]
    assert "REDACTED_OBSERVER_SECRET" in res["error"]


def test_admin_adopt_status_exception_is_rendered_as_400(admin_broker, monkeypatch):
    """status 側の例外も 400 ``[adopt_status_failed]`` 本文で返る。

    preflight が「例外で切断」と「daemon 不到達」を区別できないと、``org adopt`` は
    起動を止めるべき場面 (自分の adoption が既に負けている) を通してしまう。
    """
    def _boom(*args, **kwargs):
        raise RuntimeError("status exploded")

    monkeypatch.setattr(admin_broker, "adopt_status", _boom)
    res = _adopt_status(admin_broker, {"owner": "sec"}, expect_status=400)
    assert res["ok"] is False
    assert res["error"].startswith("[adopt_status_failed] RuntimeError")


@pytest.mark.parametrize("method", ["adopt", "adopt_delivery_v2", "delivery_adopt"])
def test_admin_near_adopt_method_names_are_unknown(admin_broker, method):
    """adopt に似た未知メソッドは ``[unknown_admin_method]`` のまま 400 で落ちる。

    新設の分岐が接頭辞一致のような緩い判定に化けると、綴り違いの要求が本物の
    adopt 分岐に落ち、operator が意図しない owner の fence を踏む。
    """
    res = _admin_post(admin_broker, {"method": method}, "ADMIN-SECRET",
                      expect_status=400)
    assert "unknown_admin_method" in res["error"]


def test_adopt_hidden_when_no_admin_token(broker):
    """admin token 未設定の daemon では adopt 経路ごと 404 で隠れ、副作用も残らない。

    ``/admin`` は 404 gate が先に立つ契約。新分岐がその手前で評価されるようになると、
    admin 面を持たない daemon (内部起動 / テスト用) でも fence だけが実行される。
    """
    broker.issue_token("sec", "sec", "secretary")
    broker.issue_delivery_cred("sec")
    assert _adopt(broker, {"owner": "sec"}, token="anything",
                  expect_status=404) is None
    assert _fence_state(broker, "sec") == ({}, 0)


# ============================================================= #74 SIGTERM
def test_sigterm_handler_requests_shutdown(tmp_path):
    """Closes #74: SIGTERM ハンドラが request_shutdown を呼び graceful 停止を起こす。

    ハンドラ自体は shutdown event を立てるだけで、broker_stopped の emit は run() の
    finally → stop() に集約される (test_admin_shutdown_clean_stop_via_run と同経路)。
    ここではシグナル配線 (SIGTERM -> request_shutdown) を main thread で直接検証する
    (run() を別スレッドで回すと signal.signal が登録できないため、配線は単体で固定)。
    """
    sig = getattr(signal, "SIGTERM", None)
    if sig is None:  # pragma: no cover - 全対応プラットフォームに SIGTERM はある
        pytest.skip("SIGTERM unavailable on this platform")
    b = Broker(state_dir=tmp_path / "broker", adapter=None, port=0)
    previous = signal.getsignal(sig)
    try:
        broker_cli._install_signal_handlers(b)
        handler = signal.getsignal(sig)
        assert callable(handler) and handler is not previous
        assert not b._shutdown_event.is_set()
        handler(sig, None)  # シグナル受信を模す
        assert b._shutdown_event.is_set()
        # wait_for_shutdown が即座に解除される (run() の前景ループが抜ける)。
        assert b.wait_for_shutdown(timeout=1.0) is True
    finally:
        signal.signal(sig, previous)  # グローバル signal 状態を復元


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


# ============================================== pid liveness helper (Issue #122)
def test_pid_alive_true_for_self():
    """本プロセスの pid は生存中と判定される。"""
    import os

    assert sidecar.pid_alive(os.getpid()) is True


def test_pid_alive_false_for_reaped_child():
    """終了済み子プロセスの pid は非生存と判定される (broker send の stale hint 根拠)。"""
    import subprocess
    import sys

    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    # reaped child -> no such live process. (pid reuse within this window is
    # vanishingly unlikely on a test box.)
    assert sidecar.pid_alive(p.pid) is False


@pytest.mark.parametrize("bad", [0, -1, "123", None])
def test_pid_alive_false_for_nonpositive_or_nonint(bad):
    """0 / 負値 / 非 int は非生存 (壊れた sidecar pid を alive と誤認しない)。"""
    assert sidecar.pid_alive(bad) is False
