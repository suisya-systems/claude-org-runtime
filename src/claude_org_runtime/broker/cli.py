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
import os
import secrets
import time
from pathlib import Path

from ..terminal import VALID_BACKENDS, default_backend, make_adapter
from . import sidecar
from .server import Broker
from .surface import ROOT_ROLE_CHOICES

DEFAULT_STATE_DIR = ".state/broker"
DEFAULT_PORT = 48720

# root agent (手動検証用 token) を bind する権限 tier。tools/list の公開面は
# token の auth_role で構造的に絞られる (surface.tools_for)。既定 worker は
# 現行挙動 (messaging 4 面) を不変に保つ。secretary 起動で 13 面全面になる。
# 受理集合は surface.ROOT_ROLE_CHOICES を canonical な単一の出所として共有する
# (admin RPC の mint_token と同じ集合で検証する)。
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
    parser.add_argument(
        "--root-cwd", default=None,
        help=(
            "root pane (人間駆動の窓口/secretary) の cwd を bind に持たせる "
            "(Issue #61)。spawn_* の relative cwd はこの cwd を base に解決される "
            "(absolute は as-is)。**省略時は daemon の起動 cwd (os.getcwd) を充てる**: "
            "本デーモンは session root から起動する運用契約のため、その起動ディレクトリ "
            "が relative spawn の解決アンカーになる。運用上 session root 以外から "
            "起動する場合は本フラグで明示せよ (= 決定的な解決 base。黙って間違った base "
            "に落とさないための文書化済み既定)。relative を渡しても daemon 起動 cwd を "
            "基準に **absolute 化** して bind に持たせる (解決アンカーは常に absolute)。"
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


def issue_root_token(
    broker: Broker,
    root_role: str = DEFAULT_ROOT_ROLE,
    root_cwd: str | None = None,
) -> str:
    """手動検証用 root token を 1 本発行する (spike __main__ 同等)。

    ``root_role`` は表示 role 兼 **権限 tier (auth_role)**。root token は spawn
    子ではないため tier 上限切り (``capped_auth_role``) は適用せず、要求どおりの
    tier で bind する。tools/list の公開面はこの ``auth_role`` で構造的に絞られる
    (既定 worker = messaging 4 面で現行挙動不変; secretary で全 13 面)。

    ``root_cwd`` は root pane の cwd (Issue #61)。これを bind に持たせることで、
    人間駆動の窓口が ``spawn_claude_pane(cwd=".dispatcher")`` のような **relative
    cwd** を投げたとき、broker が **この cwd を base に** absolute 解決できる
    (cwd null だと解決アンカーが無く relative spawn が拒否される / 誤 base に
    落ちる、が本 Issue の根因)。``run`` がブロックする serve ループに入る前の
    この一行を独立テスト可能にするための抽出。
    """
    return broker.issue_token(
        "manual-test", "manual-test", root_role,
        cwd=root_cwd, auth_role=root_role,
    )


def run(args: argparse.Namespace) -> int:
    # state-dir は入口で絶対化する (sidecar / journal の単一の絶対パス。Windows
    # isabs の罠を避けるため posixpath 併用。Codex review Minor / #61 の先例)。
    state_dir = sidecar.absolutize(args.state_dir)
    # backend は **解決済み** 名を記録する: --backend 省略時は default_backend()、
    # --no-nudge (adapter 無し) は None。健全性判定 (タスク 2) が「同 backend」を
    # 照合できるよう要求値ではなく実値を sidecar に残す (Codex review Major)。
    if args.no_nudge:
        adapter = None
        backend_name = None
    else:
        backend_name = args.backend or default_backend()
        adapter = make_adapter(backend_name)
    # admin HTTP RPC (token mint / graceful shutdown) の認証 token。root token とは
    # 別系統で生成し sidecar に 0600 で書く (平文 journal 禁止。Codex review)。
    admin_token = secrets.token_urlsafe(32)
    broker = Broker(
        state_dir=state_dir,
        adapter=adapter,
        host=args.host,
        port=args.port,
        admin_token=admin_token,
    )
    # run スライスの起点: この run の開始前の journal バイト長 (broker.start が
    # broker_started を append する前に取る)。down はこのオフセット以降だけを見て
    # broker_stopped を確認する = 全履歴 grep の偽陽性回避 (Codex review Major)。
    journal_offset = sidecar.journal_offset(state_dir)
    started_at = time.time()
    broker.start()
    # daemon sidecar を公開する (発見用メタ + admin token)。停止時に finally で削除。
    sidecar.write_sidecar(
        state_dir,
        pid=os.getpid(),
        host=args.host,
        port=broker.port,
        backend=backend_name,
        started_at=started_at,
        journal_offset=journal_offset,
    )
    sidecar.write_admin_token(state_dir, admin_token)
    print(f"org-broker listening on {broker.url}")
    print(f"admin RPC: {broker.admin_url} (token in {state_dir}/{sidecar.ADMIN_TOKEN_NAME})")
    print(f"daemon sidecar: {state_dir}/{sidecar.SIDECAR_NAME} (backend={backend_name})")
    print(f"queue store: {Path(state_dir) / 'queue.jsonl'}")
    # 手動検証用の token を 1 本発行して mcp-config を表示する (spike __main__ と同等)。
    # root_cwd 省略時は daemon 起動 cwd (os.getcwd) を anchor に充てる (Issue #61。
    # 運用契約: 本デーモンは session root から起動する。help 参照)。明示指定が
    # relative の場合も **absolute 化** する: root の cwd が relative のままだと、
    # 子 spawn の relative cwd 解決アンカーが relative になり、resolve_spawn_cwd の
    # join 結果も relative → adapter (daemon base) で再解決され Issue #61 が再発する。
    # CLI 境界で absolute に固定して解決アンカーを決定的にする (codex review Major)。
    root_cwd = os.path.abspath(args.root_cwd) if args.root_cwd is not None else os.getcwd()
    tok = issue_root_token(broker, args.root_role, root_cwd)
    print(f"manual test token ({args.root_role}):", tok)
    print(f"root pane cwd (relative spawn anchor): {root_cwd}")
    print("mcp-config:", json.dumps(broker.mcp_config_for(tok)))
    # root pane (人間駆動の窓口) を pane 登録簿に論理ペインとして載せる (Issue #57)。
    # bind.pane_id は None のままなので PTY ナッジは飛ばない (人間は check_messages
    # で読む)。これで list_panes に窓口が出て、子を 1 つ spawn した状態でも
    # close_pane が [last_pane] 誤判定せず子を閉じられる。
    root_pane = broker.register_logical_pane(tok)
    print(f"root pane registered (logical, id={root_pane['id']}, role={args.root_role})")
    # 前景で shutdown 要求まで待つ。要求は (a) admin RPC (shutdown) または
    # (b) KeyboardInterrupt の二経路。シグナル (SIGINT) に依存しない停止経路を
    # admin RPC で提供するのが Blocker 2 (Windows 要件)。serve 自体は前景 debug
    # primitive のまま (既存挙動不変)。run() が **唯一の stop() 呼出元** で、
    # broker_stopped を journal に 1 回残し sidecar を削除する。
    try:
        broker.wait_for_shutdown()
    except KeyboardInterrupt:
        pass
    finally:
        broker.stop()
        sidecar.remove_sidecar(state_dir)
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
