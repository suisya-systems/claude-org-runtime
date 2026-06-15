# -*- coding: utf-8 -*-
"""``claude-org-runtime broker send`` notify helper のテスト (Issue #93)。

helper は走行中 daemon への薄い橋渡し (sidecar 発見 -> admin mint -> MCP send) で、
**best-effort** (例外を投げず未配送は非0 return) が中心契約。ここでは:

- 配送成功: 登録済み宛先へ enqueue され exit 0、queue に行が積まれる。
- 未配送の各経路 (sidecar 不在 / admin.token 不在 / daemon 到達不能 / 宛先不在) が
  例外を投げず非0 を返す。
- CLI 配線 (parser / top-level CLI 統合) と ASCII-only help。

走行中 daemon は本物の :class:`Broker` を ephemeral port で起動し sidecar を
ディスクに書いて helper に発見させる (test_launcher の live_daemon と同方針)。
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import pytest

from claude_org_runtime.broker import cli as broker_cli
from claude_org_runtime.broker import notify, sidecar
from claude_org_runtime.broker.server import Broker
from claude_org_runtime.cli import main as top_main
from claude_org_runtime.terminal import default_backend


# --------------------------------------------------------------------- helpers
def _send_args(state_dir, *, to="alice", message="hello"):
    return argparse.Namespace(to=to, message=message, state_dir=str(state_dir))


def _write_sidecar(state_dir, host, port, admin_token="ADMIN-SECRET"):
    sidecar.write_sidecar(
        state_dir, pid=os.getpid(), host=host, port=port,
        backend=default_backend(), started_at=time.time(), journal_offset=0,
    )
    sidecar.write_admin_token(state_dir, admin_token)


@pytest.fixture
def live_daemon(tmp_path):
    """本物の started Broker (admin token 付き) + ディスク上の sidecar。"""
    state_dir = str(tmp_path / "broker")
    b = Broker(state_dir=state_dir, adapter=None, port=0, admin_token="ADMIN-SECRET")
    b.start()
    _write_sidecar(state_dir, b.host, b.port)
    try:
        yield b, state_dir
    finally:
        b.stop()


def _register_recipient(broker, agent_id="alice"):
    """配送先になる登録済み bind を 1 つ作る (MCP を介さない server-side 登録)。"""
    tok = broker.issue_token(agent_id, agent_id, "worker")
    broker.register_local(tok)
    return tok


# ===================================================================== delivery
def test_send_delivers_to_registered_recipient(live_daemon):
    b, state_dir = live_daemon
    recip_tok = _register_recipient(b, "alice")

    rc = notify.broker_send(_send_args(state_dir, to="alice", message="ping"))
    assert rc == 0

    # 実際に queue へ積まれていることを drain で確認する。
    drained = b.drain(b.get_bind(recip_tok))
    assert [m["message"] for m in drained] == ["ping"]


def test_send_delivers_unicode_body(live_daemon):
    # cp932 制約は stderr/help 限定で payload には及ばない。非 ASCII 本文が
    # byte-for-byte で queue に届くことを守る (payload を ASCII 化する退行の検出)。
    b, state_dir = live_daemon
    recip_tok = _register_recipient(b, "alice")
    body = "日本語 メッセージ body"
    rc = notify.broker_send(_send_args(state_dir, to="alice", message=body))
    assert rc == 0
    drained = b.drain(b.get_bind(recip_tok))
    assert [m["message"] for m in drained] == [body]


def test_send_close_failure_does_not_invert_success(live_daemon, monkeypatch):
    # Blocker 回帰: enqueue 成功後に de-register (close) が非 URLError (TimeoutError 等)
    # を投げても、cleanup 失敗が配送結果 (exit 0) を上書きしてはならない。
    b, state_dir = live_daemon
    recip_tok = _register_recipient(b, "alice")

    def boom(self):
        raise TimeoutError("cleanup blew up")

    monkeypatch.setattr(notify._McpClient, "close", boom)
    rc = notify.broker_send(_send_args(state_dir, to="alice", message="ping"))
    assert rc == 0
    assert [m["message"] for m in b.drain(b.get_bind(recip_tok))] == ["ping"]


def test_send_diagnostic_is_ascii_even_with_unicode_path(tmp_path, capsys):
    # Major 回帰: state_dir 等 外部由来の非 ASCII 断片が stderr に混じっても、診断は
    # ASCII (cp932 安全) に正規化される。
    nonascii_dir = tmp_path / "ブローカー"  # 非 ASCII パス
    rc = notify.broker_send(_send_args(nonascii_dir, to="alice", message="x"))
    assert rc != 0
    err = capsys.readouterr().err
    assert err.strip()  # 何か診断は出ている
    err.encode("ascii")  # 非 ASCII が残れば例外
    err.encode("cp932")


def test_send_unknown_recipient_is_undelivered(live_daemon):
    b, state_dir = live_daemon
    rc = notify.broker_send(_send_args(state_dir, to="nobody", message="ping"))
    assert rc != 0


def test_send_does_not_leak_registered_sender(live_daemon):
    """送信に使う使い捨て token は送信後 DELETE で de-register される。"""
    b, state_dir = live_daemon
    _register_recipient(b, "alice")
    notify.broker_send(_send_args(state_dir, to="alice", message="ping"))
    # list_peers 相当 = registered な full bind のみ。送信者 (admin-*) は残さない。
    registered = [
        bind.agent_id for bind in b._binds.values()
        if bind.registered and not bind.revoked
    ]
    assert not any(aid.startswith("admin-") for aid in registered)


# ============================================================= undelivered paths
def test_send_no_sidecar_is_noop_nonzero(tmp_path):
    # broker sidecar 不在 = daemon 不在。renga の RENGA_SOCKET 未設定 no-op と対称。
    rc = notify.broker_send(_send_args(tmp_path / "absent"))
    assert rc != 0


def test_send_missing_admin_token_is_nonzero(tmp_path):
    state_dir = str(tmp_path / "broker")
    # daemon.json はあるが admin.token が無い (= admin RPC で mint できない)。
    sidecar.write_sidecar(
        state_dir, pid=os.getpid(), host="127.0.0.1", port=1,
        backend=default_backend(), started_at=time.time(), journal_offset=0,
    )
    rc = notify.broker_send(_send_args(state_dir))
    assert rc != 0


def test_send_rejected_admin_token_is_undelivered(tmp_path):
    # admin.token はあるが daemon の admin_token と不一致 (= 認証失敗)。admin RPC は
    # 401 {"ok": False} を返し、mint-not-ok 経路で未配送に落ちる (契約の 'auth fail')。
    state_dir = str(tmp_path / "broker")
    b = Broker(state_dir=state_dir, adapter=None, port=0, admin_token="REAL-SECRET")
    b.start()
    _write_sidecar(state_dir, b.host, b.port, admin_token="WRONG-SECRET")
    try:
        rc = notify.broker_send(_send_args(state_dir, to="alice", message="ping"))
        assert rc != 0
    finally:
        b.stop()


def test_send_malformed_sidecar_is_caught_no_raise(tmp_path, capsys):
    # best-effort の要 = broker_send の catch-all。daemon.json が valid JSON だが
    # host/port を欠くと sc["host"] が KeyError を投げる。read_sidecar は壊れた JSON を
    # None にするため、この欠キーこそ catch-all の唯一の現実的 trigger。CLI 境界から
    # 例外が漏れず、短い ASCII 1 行診断 (traceback 無し) で非0 を返すことを守る。
    state_dir = Path(tmp_path / "broker")
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / sidecar.SIDECAR_NAME).write_text(
        json.dumps({"backend": None}), encoding="utf-8"  # host/port 欠落
    )
    (state_dir / sidecar.ADMIN_TOKEN_NAME).write_text("ADMIN", encoding="utf-8")

    rc = notify.broker_send(_send_args(state_dir, to="alice", message="ping"))
    assert rc != 0
    err = capsys.readouterr().err
    assert "unexpected error" in err
    assert "Traceback" not in err
    err.encode("cp932")  # 診断は cp932 安全 (ASCII のみ)
    err.encode("ascii")


def test_send_unreachable_daemon_is_nonzero_no_raise(tmp_path):
    # 停止した daemon の port を指す stale sidecar。admin RPC は URLError。
    state_dir = str(tmp_path / "broker")
    b = Broker(state_dir=state_dir, adapter=None, port=0, admin_token="ADMIN-SECRET")
    b.start()
    host, port = b.host, b.port
    _write_sidecar(state_dir, host, port)
    b.stop()  # daemon を止めてから送る = 到達不能
    rc = notify.broker_send(_send_args(state_dir, to="alice", message="ping"))
    assert rc != 0


def test_top_level_cli_routes_to_broker_send(tmp_path):
    # claude-org-runtime broker send ... が broker_send に到達し int を返す。
    rc = top_main([
        "broker", "send", "--to", "alice", "--message", "hi",
        "--state-dir", str(tmp_path / "absent"),
    ])
    assert rc != 0  # daemon 不在 = 未配送。例外は漏れない。


# ====================================================================== wiring
def test_send_parser_defaults_and_required():
    parser = broker_cli.build_parser()
    args = parser.parse_args(["send", "--to", "x", "--message", "y"])
    assert args.to == "x"
    assert args.message == "y"
    assert args.state_dir == notify.DEFAULT_STATE_DIR == ".state/broker"
    assert args.func is notify.broker_send


def test_send_requires_to_and_message():
    parser = broker_cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["send", "--to", "x"])  # --message 欠落
    with pytest.raises(SystemExit):
        parser.parse_args(["send", "--message", "y"])  # --to 欠落


def test_send_help_is_ascii_only():
    # cp932 コンソールで --help が UnicodeEncodeError にならないこと。
    parser = broker_cli.build_parser()
    send_action = next(
        a for a in parser._subparsers._group_actions[0].choices.values()
        if a.prog.endswith("send")
    )
    help_text = send_action.format_help()
    help_text.encode("cp932")  # 非 ASCII (em-dash 等) を含めば例外
    help_text.encode("ascii")
