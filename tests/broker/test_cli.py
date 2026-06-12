# -*- coding: utf-8 -*-
"""Tests for the broker daemon CLI wiring.

大半は argument parser + top-level CLI 統合を検証する。``run`` 本体は serve
ループ (``time.sleep`` 無限待ち) でブロックするが、``--root-role`` の配線退行を
拾うため、末尾の 1 本だけ ``time.sleep`` を即時 KeyboardInterrupt に差し替えて
``run`` を実運用経路ごと回す。
"""

from __future__ import annotations

import os

import pytest

from claude_org_runtime.broker import cli as broker_cli
from claude_org_runtime.broker import surface
from claude_org_runtime.broker.server import Broker
from claude_org_runtime.broker.surface import tools_for
from claude_org_runtime.cli import build_parser as build_top_parser

# tier 別の期待公開面 (件数固定ではなく name 集合で比較し、catalog 増減に頑健)。
_MESSAGING_SURFACE = {"send_message", "check_messages", "list_peers", "set_summary"}
_FULL_SURFACE = {t["name"] for t in surface.TOOLS}  # secretary = 全面


def test_broker_parser_defaults():
    parser = broker_cli.build_parser()
    args = parser.parse_args(["serve"])
    assert args.state_dir == broker_cli.DEFAULT_STATE_DIR == ".state/broker"
    assert args.port == broker_cli.DEFAULT_PORT
    assert args.host == "127.0.0.1"
    assert args.no_nudge is False
    # 既定 root tier は worker (現行挙動不変)。
    assert args.root_role == broker_cli.DEFAULT_ROOT_ROLE == "worker"
    # --root-cwd 既定は None (run() が os.getcwd を充てる; Issue #61)。
    assert args.root_cwd is None
    assert args.func is broker_cli.run


def test_broker_parser_overrides():
    parser = broker_cli.build_parser()
    args = parser.parse_args(
        ["serve", "--port", "0", "--state-dir", "/tmp/q",
         "--backend", "tmux", "--no-nudge"]
    )
    assert args.port == 0
    assert args.state_dir == "/tmp/q"
    assert args.backend == "tmux"
    assert args.no_nudge is True


def test_broker_rejects_unknown_backend():
    parser = broker_cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["serve", "--backend", "screen"])


def test_broker_parses_root_role():
    parser = broker_cli.build_parser()
    for role in ("worker", "curator", "dispatcher", "secretary"):
        args = parser.parse_args(["serve", "--root-role", role])
        assert args.root_role == role


def test_broker_rejects_unknown_root_role():
    parser = broker_cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["serve", "--root-role", "admin"])


def test_broker_parses_root_cwd(tmp_path):
    # --root-cwd override が args に乗る (Issue #61 の relative spawn anchor)。
    parser = broker_cli.build_parser()
    args = parser.parse_args(["serve", "--root-cwd", str(tmp_path)])
    assert args.root_cwd == str(tmp_path)


def test_issue_root_token_carries_root_cwd(tmp_path):
    # root_cwd が bind.cwd に乗る (relative spawn の解決アンカー; Issue #61)。
    broker = Broker(state_dir=tmp_path, adapter=None)
    tok = broker_cli.issue_root_token(broker, "secretary", str(tmp_path))
    assert broker.get_bind(tok).cwd == str(tmp_path)


def test_top_level_cli_exposes_broker_serve():
    parser = build_top_parser()
    args = parser.parse_args(["broker", "serve", "--no-nudge"])
    assert args.func is broker_cli.run
    assert args.no_nudge is True


def test_top_level_cli_forwards_root_role():
    parser = build_top_parser()
    args = parser.parse_args(["broker", "serve", "--root-role", "secretary"])
    assert args.root_role == "secretary"


# --- end-to-end: --root-role → 発行 token の auth_role → tools/list 公開面 ---
# parser だけ / tools_for だけのテストでは run() が root_role を token に流す一行が
# 検証されない。issue_root_token を独立テスト可能に抽出して tier→公開面を端から端
# まで結び、さらに run() 本体の配線退行を最後の 1 本で直接拾う。

@pytest.mark.parametrize("role", ["worker", "curator", "dispatcher", "secretary"])
def test_issue_root_token_reflects_requested_tier(tmp_path, role):
    # 受理 4 tier すべてで auth_role が要求どおり反映される (root token は spawn 子
    # ではないため tier 上限切りは適用しない)。
    broker = Broker(state_dir=tmp_path, adapter=None)
    tok = broker_cli.issue_root_token(broker, role)
    bind = broker.get_bind(tok)
    assert bind.auth_role == role
    # CLI が流した auth_role が tools/list の公開面を駆動する。
    assert {t["name"] for t in tools_for(bind.auth_role)} == {
        t["name"] for t in tools_for(role)
    }


def test_issue_root_token_default_worker_is_messaging_surface(tmp_path):
    broker = Broker(state_dir=tmp_path, adapter=None)
    # 既定 (worker) で発行 → messaging 面のみ (現行挙動不変)。
    tok = broker_cli.issue_root_token(broker)
    bind = broker.get_bind(tok)
    assert bind.auth_role == "worker"
    assert {t["name"] for t in tools_for(bind.auth_role)} == _MESSAGING_SURFACE


def test_issue_root_token_dispatcher_gains_pane_ops(tmp_path):
    # 中間 tier dispatcher: messaging + pane 操作。generic spawn_pane は不可
    # (secretary 専用)。messaging-only に潰れる回帰を拾う。
    broker = Broker(state_dir=tmp_path, adapter=None)
    tok = broker_cli.issue_root_token(broker, "dispatcher")
    names = {t["name"] for t in tools_for(broker.get_bind(tok).auth_role)}
    assert _MESSAGING_SURFACE < names           # messaging を真に含む
    assert {"list_panes", "send_keys", "spawn_claude_pane"} <= names
    assert "spawn_pane" not in names


def test_issue_root_token_secretary_is_full_surface(tmp_path):
    broker = Broker(state_dir=tmp_path, adapter=None)
    # secretary tier で発行 → 全面 (Issue #53 受入: 13 面)。
    tok = broker_cli.issue_root_token(broker, "secretary")
    bind = broker.get_bind(tok)
    assert bind.auth_role == "secretary"
    assert {t["name"] for t in tools_for(bind.auth_role)} == _FULL_SURFACE
    assert len(_FULL_SURFACE) == 13  # golden shape の面数を明示確認


def test_run_wires_root_role_into_issued_token(tmp_path, monkeypatch):
    """run() 実運用経路が args.root_role を token の auth_role まで流すことを検証。

    serve ループ (time.sleep 無限待ち) を即時 KeyboardInterrupt に差し替えて run()
    を最後まで回す。helper 直叩きでは run() が ``issue_root_token(broker)`` に
    退行しても素通りするため、この配線は run() 経由で押さえる必要がある。
    """
    captured = {}
    real_issue = broker_cli.issue_root_token

    def spy(broker, root_role=broker_cli.DEFAULT_ROOT_ROLE, root_cwd=None):
        tok = real_issue(broker, root_role, root_cwd)
        captured["auth_role"] = broker.get_bind(tok).auth_role
        captured["cwd"] = broker.get_bind(tok).cwd
        return tok

    def boom(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(broker_cli, "issue_root_token", spy)
    monkeypatch.setattr(broker_cli.time, "sleep", boom)

    parser = broker_cli.build_parser()
    args = parser.parse_args(
        ["serve", "--port", "0", "--no-nudge",
         "--state-dir", str(tmp_path), "--root-role", "dispatcher"]
    )
    rc = broker_cli.run(args)
    assert rc == 0
    assert captured["auth_role"] == "dispatcher"
    # --root-cwd 省略 → daemon 起動 cwd (os.getcwd) を anchor に充てる (Issue #61)。
    assert captured["cwd"] == os.getcwd()


def test_run_wires_explicit_root_cwd_into_bind(tmp_path, monkeypatch):
    """run() 実運用経路が明示 --root-cwd を bind.cwd まで流し、relative を absolute
    化することを検証 (codex review Minor / Major)。helper 直叩きや parser 単体では
    run() が args.root_cwd を無視/相対のまま流す退行を拾えないため run() 経由で押さえる。
    """
    captured = {}
    real_issue = broker_cli.issue_root_token

    def spy(broker, root_role=broker_cli.DEFAULT_ROOT_ROLE, root_cwd=None):
        tok = real_issue(broker, root_role, root_cwd)
        captured["cwd"] = broker.get_bind(tok).cwd
        return tok

    def boom(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(broker_cli, "issue_root_token", spy)
    monkeypatch.setattr(broker_cli.time, "sleep", boom)

    # relative な --root-cwd を渡し、run() が absolute 化して bind に載せることを確認。
    parser = broker_cli.build_parser()
    args = parser.parse_args(
        ["serve", "--port", "0", "--no-nudge",
         "--state-dir", str(tmp_path), "--root-role", "secretary",
         "--root-cwd", "rel/sub"]
    )
    rc = broker_cli.run(args)
    assert rc == 0
    # relative → daemon 起動 cwd 基準で absolute 化 (解決アンカーは常に absolute)。
    assert captured["cwd"] == os.path.abspath("rel/sub")
    assert os.path.isabs(captured["cwd"])


def test_run_registers_root_logical_pane(tmp_path, monkeypatch):
    """run() 実運用経路が root token を pane 登録簿に論理ペインとして載せることを
    検証 (Issue #57)。serve ループを即時 KeyboardInterrupt に差し替えて run() を
    最後まで回し、register_logical_pane の戻りを spy で捕捉する。"""
    captured = {}
    real_register = Broker.register_logical_pane

    def spy(self, token):
        res = real_register(self, token)
        captured["pane"] = res
        captured["in_meta"] = str(res["id"]) in self._pane_meta
        return res

    def boom(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(Broker, "register_logical_pane", spy)
    monkeypatch.setattr(broker_cli.time, "sleep", boom)

    parser = broker_cli.build_parser()
    args = parser.parse_args(
        ["serve", "--port", "0", "--no-nudge",
         "--state-dir", str(tmp_path), "--root-role", "secretary"]
    )
    rc = broker_cli.run(args)
    assert rc == 0
    assert captured["pane"]["logical"] is True
    assert captured["pane"]["role"] == "secretary"
    assert captured["in_meta"] is True
