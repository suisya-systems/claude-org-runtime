# -*- coding: utf-8 -*-
"""``claude-org-runtime broker send`` -- transport-neutral notify helper (Issue #93).

素の CLI subprocess (例: ja#590 ``tools/peer_notify.py`` の broker 経路) から
走行中 broker daemon の queue へ 1 通 enqueue するための薄い helper。MCP ツール
(``mcp__org-broker__send_message``) は Claude Code セッション内からしか呼べないため、
CLI からは daemon を発見し admin RPC で使い捨て token を mint して MCP send を 1 回
叩く、という最小の橋渡しを行う。

凍結 CLI 契約 (ja#590 が依存; 変更は窓口エスカレーション必須)::

    claude-org-runtime broker send --to <agent_id> --message <text>

    exit 0  = broker キューへ enqueue 成功 (配送)
    exit 非0 = 未配送 (broker sidecar 不在 / 認証失敗 / 宛先不在 / 到達不能 等)

**best-effort セマンティクス**: 例外は一切送出しない (内部の全失敗を握り潰し非0
return + stderr へ短い 1 行診断のみ)。broker sidecar 不在時は no-op で非0 return
する (renga 経路の ``RENGA_SOCKET`` 未設定 no-op と対称)。peer 通知は canonical な
イベント行の上の装飾であり、配送の前提条件ではない。

診断は cp932 コンソールでも壊れないよう ASCII のみで出す (`--message` 本文は
echo しない)。
"""

from __future__ import annotations

import argparse
import sys
import urllib.error

from . import sidecar
from .rpc import _McpClient, _admin_rpc

DEFAULT_STATE_DIR = ".state/broker"
# notify は best-effort な装飾で、呼び元 (ja#590) は subprocess 全体を ~5s で timeout
# する。各 RPC leg (mint / initialize / send / close) を **個別に** この上限で縛り、
# stale sidecar が dead port を指して connect が張り付く環境 (一部 Windows) でも 1 leg
# が早期に諦めて非0 を返せるようにする (制御面 org up/down の 10s より短い)。これは
# *per-RPC* の上限であり、複数 leg が同時に近上限まで stall する病的ケースでは累計が
# 呼び元の 5s を超えうるが、その場合は呼び元の subprocess timeout (TimeoutExpired ->
# 未配送扱い) が握る (delivered/undelivered のシグナルは保たれる)。localhost RPC は
# 通常 sub-100ms なので実運用ではこの上限に当たらない。
_RPC_TIMEOUT = 2.0
# enqueue 帰属に使う送信者 tier。messaging 面 (send_message) は全 tier 共通なので
# 最小権限の worker で足りる (surface._allowed_tools)。
_SENDER_ROLE = "worker"


def _fail(reason: str) -> int:
    """短い 1 行診断を stderr に出して非0 (未配送) を返す。

    ``reason`` は state_dir パス・broker 由来の error 文字列・``--to`` 値など外部由来の
    断片を含みうるため、cp932 コンソールでも壊れないよう **ASCII に正規化**してから
    出す (非 ASCII は ``\\xNN`` にエスケープ)。凍結契約「stderr は短い ASCII 診断のみ」を
    入力内容に依らず構造的に満たす。呼び元 (ja#590) は returncode のみで delivered を
    判定するため、stderr は人間のトラブルシュート用に留める。
    """
    safe = reason.encode("ascii", "backslashreplace").decode("ascii")
    print(f"broker send: {safe}", file=sys.stderr)
    return 1


def broker_send(args: argparse.Namespace) -> int:
    """``broker send`` 本体。best-effort: 例外を投げず未配送は非0 return。

    フロー: sidecar 発見 -> admin mint (使い捨て worker token) -> MCP initialize ->
    send_message -> DELETE で de-register。各段の失敗 (sidecar/token 不在・到達不能・
    宛先不在) は全て短い stderr 診断 + 非0 return に落とす。最後の総括 ``except`` で
    想定外の例外も握り潰し、CLI 境界から例外が漏れないことを構造的に保証する。
    """
    try:
        return _broker_send(args)
    except Exception as e:  # noqa: BLE001 - best-effort: 例外は CLI 境界で握り潰す
        return _fail(f"unexpected error ({type(e).__name__})")


def _broker_send(args: argparse.Namespace) -> int:
    to_id = args.to
    message = args.message
    state_dir = sidecar.absolutize(args.state_dir)

    # --- sidecar 発見 (broker daemon 不在は no-op 非0。renga の RENGA_SOCKET 未設定と対称)
    sc = sidecar.read_sidecar(state_dir)
    if sc is None:
        return _fail(f"no broker daemon (sidecar absent under {state_dir!r})")
    admin_token = sidecar.read_admin_token(state_dir)
    if admin_token is None:
        return _fail(f"broker daemon admin token absent under {state_dir!r}")
    host, port = sc["host"], sc["port"]

    # --- 使い捨て worker token を mint (admin RPC)。到達不能 = stale sidecar -> 未配送
    try:
        minted = _admin_rpc(
            host, port, admin_token, "mint_token",
            {"role": _SENDER_ROLE}, timeout=_RPC_TIMEOUT,
        )
    except urllib.error.URLError:
        return _fail(f"broker daemon unreachable at {host}:{port}")
    if not (minted and minted.get("ok")):
        err = (minted or {}).get("error", "no response")
        return _fail(f"admin mint_token failed: {err}")
    token = minted.get("token")
    if not isinstance(token, str) or not token:
        # ok=True なら token は必ず載る (server.admin_mint_token) が、応答が壊れていても
        # KeyError で落とさず未配送に落とす (best-effort)。
        return _fail("admin mint_token returned no token")

    # --- MCP send_message (initialize -> tools/call)。送信後は DELETE で de-register
    client = _McpClient(host, port, token, timeout=_RPC_TIMEOUT)
    try:
        client.initialize()
        result = client.send_message(to_id, message)
    except urllib.error.URLError:
        return _fail(f"broker MCP surface unreachable at {host}:{port}")
    finally:
        # 使い捨て token を de-register (idle な登録を残さない)。cleanup の失敗は
        # **配送結果 (exit code) を上書きしない**: enqueue 成功後に close() が例外を
        # 出しても catch-all に落として未配送 exit に反転させてはならない (凍結契約
        # exit 0 = enqueue 成功)。rpc._McpClient.close は best-effort で全例外を握るが、
        # 境界でも二重に保証する。
        try:
            client.close()
        except Exception:  # noqa: BLE001 - cleanup は配送結果を上書きしない
            pass

    if result.get("ok") is True:
        # 配送 (enqueue) 成功。stdout は静かに保つ (CLI は returncode が唯一の契約)。
        return 0
    err = result.get("error", "send_message returned not-ok")
    return _fail(f"undelivered: {err}")


def add_subparsers(subparsers: argparse._SubParsersAction) -> None:
    """``claude-org-runtime broker ...`` に ``send`` を生やす (Issue #93)。"""
    send_p = subparsers.add_parser(
        "send",
        help=(
            "Enqueue one message to another agent via a running broker daemon "
            "(best-effort; exit 0 = enqueued, non-0 = undelivered)."
        ),
    )
    send_p.add_argument(
        "--to", required=True, metavar="AGENT_ID",
        help="Recipient agent id or name (resolved by the broker queue).",
    )
    send_p.add_argument(
        "--message", required=True, metavar="TEXT",
        help="Message text to enqueue.",
    )
    send_p.add_argument(
        "--state-dir", default=DEFAULT_STATE_DIR,
        help=(
            "broker daemon state dir to discover the sidecar. "
            f"Default: {DEFAULT_STATE_DIR} (relative to CWD)."
        ),
    )
    send_p.set_defaults(func=broker_send)
