# -*- coding: utf-8 -*-
"""Tests for the broker daemon CLI wiring (parser construction only).

``run`` blocks in a serve loop, so these exercise the argument parser and
the top-level CLI integration rather than actually starting the daemon.
"""

from __future__ import annotations

import pytest

from claude_org_runtime.broker import cli as broker_cli
from claude_org_runtime.broker.server import Broker
from claude_org_runtime.broker.surface import tools_for
from claude_org_runtime.cli import build_parser as build_top_parser

# messaging-only tier の公開面数 (worker / curator)。secretary は全 13 面。
_MESSAGING_SURFACE_COUNT = 4
_SECRETARY_SURFACE_COUNT = 13


def test_broker_parser_defaults():
    parser = broker_cli.build_parser()
    args = parser.parse_args(["serve"])
    assert args.state_dir == broker_cli.DEFAULT_STATE_DIR == ".state/broker"
    assert args.port == broker_cli.DEFAULT_PORT
    assert args.host == "127.0.0.1"
    assert args.no_nudge is False
    # 既定 root tier は worker (現行挙動不変)。
    assert args.root_role == broker_cli.DEFAULT_ROOT_ROLE == "worker"
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


def test_top_level_cli_exposes_broker_serve():
    parser = build_top_parser()
    args = parser.parse_args(["broker", "serve", "--no-nudge"])
    assert args.func is broker_cli.run
    assert args.no_nudge is True


def test_top_level_cli_forwards_root_role():
    parser = build_top_parser()
    args = parser.parse_args(["broker", "serve", "--root-role", "secretary"])
    assert args.root_role == "secretary"


# --- end-to-end: --root-role → 発行 token の auth_role → tools/list 公開面数 ---
# parser だけ / tools_for だけのテストでは run() が root_role を token に流す一行が
# 検証されない。issue_root_token を独立テスト可能に抽出し、tier→面数を端から端まで
# 結ぶ (run() の serve ループはブロックするため helper を直接叩く)。

def test_issue_root_token_default_worker_is_messaging_surface(tmp_path):
    broker = Broker(state_dir=tmp_path, adapter=None)
    # 既定 (worker) で発行 → messaging 4 面 (現行挙動不変)。
    tok = broker_cli.issue_root_token(broker)
    bind = broker.get_bind(tok)
    assert bind.auth_role == "worker"
    assert len(tools_for(bind.auth_role)) == _MESSAGING_SURFACE_COUNT


def test_issue_root_token_secretary_is_full_surface(tmp_path):
    broker = Broker(state_dir=tmp_path, adapter=None)
    # secretary tier で発行 → 全 13 面 (Issue #53 受入)。
    tok = broker_cli.issue_root_token(broker, "secretary")
    bind = broker.get_bind(tok)
    assert bind.auth_role == "secretary"
    assert len(tools_for(bind.auth_role)) == _SECRETARY_SURFACE_COUNT
