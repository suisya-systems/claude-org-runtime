# -*- coding: utf-8 -*-
"""org-broker daemon CLI entry.

``claude-org-runtime broker serve`` (top-level CLI 経由) と
``python -m claude_org_runtime.broker`` (__main__) の双方から使う。
canonical 実装: claude-org-transport-lab spike/broker.py の ``__main__``
ブロックを faithful port し、queue 書込先を ``.state/broker/`` に化したもの。

state-dir の既定は ``.state/broker`` (CWD 相対)。spike は自己完結のため
``spike/broker-state/`` を既定にしていたが、runtime では設計上の本番置き場
``.state/broker/`` を既定にする (本フェーズでは flag 既定 renga で不活性、
runtime 内部テストのみで使用)。
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from ..terminal import VALID_BACKENDS, make_adapter
from .server import Broker

DEFAULT_STATE_DIR = ".state/broker"
DEFAULT_PORT = 48720

# root agent (手動検証用 token) を bind する権限 tier。tools/list の公開面は
# token の auth_role で構造的に絞られる (surface.tools_for)。既定 worker は
# 現行挙動 (messaging 4 面) を不変に保つ。secretary 起動で 13 面全面になる。
ROOT_ROLE_CHOICES = ("worker", "curator", "dispatcher", "secretary")
DEFAULT_ROOT_ROLE = "worker"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help=f"localhost bind port (default: {DEFAULT_PORT}; 0 = ephemeral).",
    )
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="bind host (default: 127.0.0.1; localhost only by design).",
    )
    parser.add_argument(
        "--state-dir", default=DEFAULT_STATE_DIR,
        help=(
            "queue.jsonl 書込先 (CWD 相対の既定 .state/broker)。"
        ),
    )
    parser.add_argument(
        "--backend", choices=VALID_BACKENDS, default=None,
        help=(
            "terminal backend (省略時は OS から自動選択: POSIX=tmux / "
            "Windows=wezterm)。--no-nudge 指定時は無視される。"
        ),
    )
    parser.add_argument(
        "--no-nudge", action="store_true",
        help="terminal adapter を生成せずナッジ配達を無効化する (queue のみ)。",
    )
    parser.add_argument(
        "--root-role", choices=ROOT_ROLE_CHOICES, default=DEFAULT_ROOT_ROLE,
        help=(
            "手動検証用 root token を bind する権限 tier (auth_role)。tools/list の "
            f"公開面はこの tier で構造的に絞られる (既定 {DEFAULT_ROOT_ROLE} = "
            "messaging 4 面で現行挙動不変; secretary で全 13 面)。"
        ),
    )


def add_subparsers(subparsers: argparse._SubParsersAction) -> None:
    """top-level CLI (``claude-org-runtime broker ...``) に serve を生やす。"""
    serve_p = subparsers.add_parser(
        "serve",
        help="org-broker daemon を localhost で起動する (Ctrl+C で停止)。",
    )
    add_arguments(serve_p)
    serve_p.set_defaults(func=run)


def issue_root_token(broker: Broker, root_role: str = DEFAULT_ROOT_ROLE) -> str:
    """手動検証用 root token を 1 本発行する (spike __main__ 同等)。

    ``root_role`` は表示 role 兼 **権限 tier (auth_role)**。root token は spawn
    子ではないため tier 上限切り (``capped_auth_role``) は適用せず、要求どおりの
    tier で bind する。tools/list の公開面はこの ``auth_role`` で構造的に絞られる
    (既定 worker = messaging 4 面で現行挙動不変; secretary で全 13 面)。``run``
    がブロックする serve ループに入る前のこの一行を独立テスト可能にするための抽出。
    """
    return broker.issue_token(
        "manual-test", "manual-test", root_role, auth_role=root_role
    )


def run(args: argparse.Namespace) -> int:
    adapter = None if args.no_nudge else make_adapter(args.backend)
    broker = Broker(
        state_dir=args.state_dir,
        adapter=adapter,
        host=args.host,
        port=args.port,
    )
    broker.start()
    print(f"org-broker listening on {broker.url}")
    print(f"queue store: {Path(args.state_dir).resolve() / 'queue.jsonl'}")
    # 手動検証用の token を 1 本発行して mcp-config を表示する (spike __main__ と同等)。
    tok = issue_root_token(broker, args.root_role)
    print(f"manual test token ({args.root_role}):", tok)
    print("mcp-config:", json.dumps(broker.mcp_config_for(tok)))
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        broker.stop()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-org-runtime-broker",
        description="org-broker daemon (localhost MCP server + queue store + nudge).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    add_subparsers(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)
