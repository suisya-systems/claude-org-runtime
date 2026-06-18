# -*- coding: utf-8 -*-
"""tool-less claude/channel sidecar (broker-native-roles.md §9.2 / §9.3 / §9.5)。

push 一次配送の per-session 配送トランスデューサ。**ツール宣言ゼロ**で
``experimental{claude/channel}`` のみを宣言する stdio MCP サーバー。org-broker
daemon を ~1s で claim->push し、受信を ``notifications/claude/channel`` でセッションへ
in-band 注入する (idle セッションも起こす)。現行 canonical は本モジュール。歴史的 origin:
claude-org-transport-lab spike/channel_sidecar.py (PR #24 merge 28a4cb2、tool-less
channel-only idle-wake が実機 PASS) の faithful port。spike の K1 env (``K1_*``) を runtime env
(``ORG_BROKER_CHANNEL_*``) へ rename し、daemon の delivery endpoint
(``/poll-claims`` / ``/confirm-delivered``) と queue row 形 (``{id, entry, epoch}``)
に合わせたもの。

なぜ tool-less が核心か (§9.5):
- このサーバーは check_messages を含む **いかなるツールも公開しない**ため、注入された
  セッションには「能動 poll する手段が存在しない」。本文がターンに現れたら、それは
  **push 以外にありえない** (idle-wake-via-push の反証可能性)。

trust 境界 (§9.4): sidecar には agent の full token ではなく **delivery-scoped
credential** のみを env で渡す。daemon 側で ``/poll-claims`` と
``/confirm-delivered``・``to_id == owner`` の行のみに制限される。

配達確定は emit の **後** (§9.3): ``/confirm-delivered`` は ``notifications/claude/
channel`` の emit (stdout flush) が成功した後にのみ行う。sidecar が emit 途中で死んでも
当該行は daemon 側 lease reaping で UNDELIVERED へ戻り、lost-message window が閉じる
(at-least-once + 冪等表示。重複は ``msg_id`` で受信側 dedup 可能)。

stdio transport: 改行区切り JSON-RPC (1 メッセージ 1 行、埋め込み改行なし)。
本ファイルは env 駆動 (CLI/argparse なし) で、``python -m
claude_org_runtime.broker.channel_sidecar`` として子 claude が起動する。文字列は
ASCII のみ (cp932 コンソール安全)。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import urllib.request

DAEMON_URL = os.environ.get("ORG_BROKER_CHANNEL_DAEMON_URL", "").rstrip("/")
DELIVERY_CRED = os.environ.get("ORG_BROKER_CHANNEL_CRED", "")
OWNER = os.environ.get("ORG_BROKER_CHANNEL_OWNER", "")
POLL_INTERVAL = float(os.environ.get("ORG_BROKER_CHANNEL_POLL_INTERVAL", "1.0"))
SOURCE_NAME = os.environ.get("ORG_BROKER_CHANNEL_SOURCE_NAME", "org-broker-channel")
LOG_PATH = os.environ.get("ORG_BROKER_CHANNEL_LOG", "")
# テスト専用 fault injection: "skip-confirm" = emit はするが confirm しない
# (emit と confirm の間で sidecar が死亡したケースの再現。lease reaping の回復を検証する)
FAULT = os.environ.get("ORG_BROKER_CHANNEL_FAULT", "")

_stdout_lock = threading.Lock()
_started = threading.Event()

# MCP protocolVersion negotiation (blind mirror を避ける)
_SUPPORTED_PROTO = frozenset((
    "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05",
))
_DEFAULT_PROTO = "2025-06-18"


def _log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    # stderr は claude が mcp-logs に拾う。ファイル指定があれば証跡用に併記。
    print(line, file=sys.stderr, flush=True)
    if LOG_PATH:
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass


def _write_message(obj: dict) -> None:
    """JSON-RPC メッセージを stdout へ 1 行で書く (改行区切り transport)。"""
    data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    with _stdout_lock:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


def _emit_channel(content: str, meta: dict) -> None:
    """claude/channel push 通知を emit。これが idle セッションを起こす in-band 注入。"""
    _write_message({
        "jsonrpc": "2.0",
        "method": "notifications/claude/channel",
        "params": {"content": content, "meta": meta},
    })


def _channel_payload(row: dict) -> tuple[str, dict]:
    """daemon の queue row (``{id, entry, epoch}``) を channel の (content, meta) へ。

    ``entry`` は broker のワイヤ形 ``{from_id, from_name, sent_at, message}``。
    ``content`` = 本文、``meta`` = 帰属 (from_id/from_name/sent_at) + dedup key
    (``msg_id`` = daemon 行 id)。msg_id は emit/confirm 残余 window や epoch flip での
    再配達を受信側が識別できる dedup key (at-least-once + 冪等表示の前提を実体化)。

    ``meta.sent_at`` は **必ず string 化**して載せる (#80)。store.enqueue は
    ``entry.sent_at`` を ``time.time()`` の **float** で打つが、host claude の
    ``notifications/claude/channel`` スキーマは ``sent_at`` を **string** で要求する。
    float のまま載せると host 側で ZodError になり、通知ごと STDIO で drop されて
    本文がセッションに注入されない (= push 一次配送の silent-drop)。entry 自体の
    数値 sent_at は pull 経路 (check_messages の tools/call result) では schema 対象外
    なので触らず、channel push に載せる射影だけを string 化する。None (欠落) は
    degenerate なので空文字にする (どちらも schema 上 valid string)。
    """
    entry = dict(row.get("entry") or {})
    content = entry.get("message", "")
    sent_at = entry.get("sent_at")
    meta = {
        "from_id": entry.get("from_id"),
        "from_name": entry.get("from_name"),
        "sent_at": "" if sent_at is None else str(sent_at),
        "msg_id": row["id"],
    }
    return content, meta


# ----------------------------------------------------------------- daemon I/O
def _daemon_post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        DAEMON_URL + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {DELIVERY_CRED}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read() or b"{}")


# ----------------------------------------------------------------- push loop
def _push_loop() -> None:
    """~1s で daemon を claim->emit->confirm する配送トランスデューサ (§9.3)。

    poll cadence そのものが daemon への heartbeat に相当する (daemon は最後の claim
    から sidecar の生存を推測できる)。配達確定 (/confirm-delivered) は emit が成功した
    *後* にのみ行う。sidecar が emit 途中で死んでも当該行は lease 失効で UNDELIVERED
    へ戻り (daemon 側 reaping)、lost-message window が閉じる。
    """
    _started.wait()   # client の initialized を待ってから配送開始
    _log(f"push loop start: daemon={DAEMON_URL} owner={OWNER} interval={POLL_INTERVAL}s")
    while True:
        try:
            res = _daemon_post("/poll-claims", {})
            rows = res.get("rows", [])
            for row in rows:
                content, meta = _channel_payload(row)
                _emit_channel(content, meta)
                _log(f"emitted row {row['id']} ({len(content)} bytes)")
                if FAULT == "skip-confirm":
                    _log(f"FAULT skip-confirm: not confirming {row['id']} (simulating death)")
                    continue
                # 配達確定は emit (stdout flush) の後にのみ。confirm 失敗時は再配達
                # されうるため結果を検査する。
                conf = _daemon_post("/confirm-delivered",
                                    {"id": row["id"], "epoch": row.get("epoch", -1)})
                if conf.get("ok"):
                    _log(f"confirmed row {row['id']}")
                else:
                    # 既に emit 済。stale_epoch (PUSH->PULL flip) 等で行は UNDELIVERED
                    # へ戻り pull/次 push で再配達されうる (msg_id で受信側 dedup 可能)。
                    # 沈黙喪失ではなく重複側に倒れる。
                    _log(f"WARN confirm not ok for {row['id']}: {conf} (may redeliver; dedup via msg_id)")
        except Exception as exc:    # daemon 一時停止等でクラッシュさせない
            _log(f"poll error: {exc}")
        time.sleep(POLL_INTERVAL)


# ----------------------------------------------------------------- JSON-RPC
def _handle(msg: dict) -> dict | None:
    method = msg.get("method")
    mid = msg.get("id")

    if method == "initialize":
        # tool-less: capabilities に experimental{claude/channel} のみ。tools を宣言
        # しない。protocolVersion は blind mirror せず、既知サポート版なら同調・未知なら
        # 既定へ negotiate。
        want = (msg.get("params") or {}).get("protocolVersion", _DEFAULT_PROTO)
        proto = want if want in _SUPPORTED_PROTO else _DEFAULT_PROTO
        _log(f"initialize (client={want} -> negotiated={proto}) -> declaring tool-less claude/channel")
        return {
            "jsonrpc": "2.0", "id": mid,
            "result": {
                "protocolVersion": proto,
                "capabilities": {"experimental": {"claude/channel": {}}},
                "serverInfo": {"name": SOURCE_NAME, "version": "0.1.0"},
                "instructions": (
                    "This is a tool-less push channel. Messages arrive as "
                    "<channel source=\"" + SOURCE_NAME + "\"> tags injected into your "
                    "turn. There is no tool to call; just act on the content."
                ),
            },
        }

    if method == "notifications/initialized":
        _started.set()        # client ready -> push loop 開始
        _log("client initialized -> push loop armed")
        return None           # 通知には応答しない

    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}

    # tool-less だが防御的に空で応答 (capability 未宣言なら通常 client は呼ばない)
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": []}}
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"resources": []}}
    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"prompts": []}}

    if mid is not None:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"method not found: {method}"}}
    return None   # 未知の通知は無視


def main() -> int:
    if not (DAEMON_URL and DELIVERY_CRED and OWNER):
        _log("FATAL: ORG_BROKER_CHANNEL_DAEMON_URL / ORG_BROKER_CHANNEL_CRED / "
             "ORG_BROKER_CHANNEL_OWNER must be set in env")
        return 2
    threading.Thread(target=_push_loop, daemon=True).start()
    _log(f"sidecar up: source={SOURCE_NAME}")
    for raw in sys.stdin.buffer:
        try:
            line = raw.decode("utf-8").strip()
        except UnicodeDecodeError:
            # 不正バイトの 1 行で transport を落とさない (channel を維持)
            _log("bad stdin bytes (skipped)")
            continue
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _log(f"bad json: {line[:120]}")
            continue
        resp = _handle(msg)
        if resp is not None:
            _write_message(resp)
    _log("stdin closed -> exit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
