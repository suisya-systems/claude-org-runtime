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
import secrets
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
# observed-session binding (Issue #129 問題 A): human launcher (org up) が **子プロセス
# env** に注入する非 replay 秘密。register 時に提示すると observed generation として
# claim できる。fork/resume は mcp-config を verbatim replay しても process env の本秘密は
# 継承しない (空になる) ため、daemon が generation bump を拒否して stand-down させる。
# mcp-config の DAEMON_URL/CRED/OWNER と違い、これは env だけに存在し config には載らない。
OBSERVER_SECRET = os.environ.get("ORG_BROKER_CHANNEL_OBSERVER", "")
# 明示 bg-hosted marker (Issue #129 問題 B / Phase 1): background-hosted host が明示的に
# セットした時だけ register/claim を抑止する。**heuristic 判定 (isatty / process tree 等)
# はしない**: foreground を bg と誤判定した瞬間に claim が止まり push 配信が停止する事故側に
# 倒れるため。不明時は foreground 扱いで claim 継続する (安全側 = 配信継続)。
BG_HOSTED = os.environ.get("ORG_BROKER_CHANNEL_BG_HOSTED", "").strip().lower() in (
    "1", "true", "yes", "on",
)

# session-scoped fencing (Issue #125): このプロセス固有の instance id。fork/resume は
# 同一 delivery cred (env) を replay するが、main() は新プロセスで毎回走るので instance id
# は新旧で必ず異なる (cred だけでは識別できない旧/新 sidecar を daemon が区別できる根拠)。
INSTANCE_ID = secrets.token_hex(8)
# register 成功で daemon から得た delivery generation。以後の poll/confirm に載せる。
# None = まだ register 成功していない (poll loop が register を再試行する)。
_gen_lock = threading.Lock()
_generation: int | None = None
# 起動時 register の同期試行回数 / 間隔 (Major #5: initialized 後・push loop 起動前に
# register を同期完了させる。transient な daemon 未起動のみ短くリトライする)。
_REGISTER_RETRIES = 3
_REGISTER_RETRY_DELAY = 0.5

_stdout_lock = threading.Lock()
_started = threading.Event()
# stand-down (Issue #129): register が ``suppressed_bg_hosted`` (Phase 1) / ``unobserved``
# (Phase 2) を返したらセットする。この sidecar は claim loop を起動せず沈黙する
# (何も claim/emit/confirm しない = background-hosted host や fork replay が message を
# claim->silent-drop で破壊するのを断つ)。stdin は読み続け MCP transport は生かす。
_stood_down = threading.Event()

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


# ------------------------------------------------------------- register (fence)
def _register_owner() -> int | None:
    """daemon へ ``/claim-owner`` を打ち owner の delivery generation を取得する。

    session-scoped fencing (Issue #125): これが owner の generation を +1 し、この
    instance を現世代の唯一の claimer として daemon に登録する。fork/resume で生まれた
    旧 sidecar は旧世代のまま残り、以後の poll/confirm を daemon が ``stale_sidecar`` で
    拒否する (二重 claim による沈黙喪失を断つ)。成功した generation を返し、transient
    失敗時は例外を送出する (呼び元がリトライ判断)。

    register には Issue #129 の 2 信号を任意で載せる: ``observer`` (非 replay 秘密、
    Phase 2 observed-session binding) と ``bg_hosted`` (明示 bg-hosted marker、Phase 1)。
    daemon が register/claim を抑止した場合 (``suppressed_bg_hosted`` / ``unobserved``)
    は :data:`_stood_down` をセットして ``None`` を返す (再試行せず claim もしない)。
    """
    global _generation
    payload: dict = {"instance_id": INSTANCE_ID}
    if OBSERVER_SECRET:
        payload["observer"] = OBSERVER_SECRET
    if BG_HOSTED:
        payload["bg_hosted"] = True
    res = _daemon_post("/claim-owner", payload)
    err = res.get("error")
    if err in ("suppressed_bg_hosted", "unobserved"):
        # Phase 1 (bg-hosted) / Phase 2 (fork replay で observer 秘密を持たない unobserved
        # sidecar)。この instance は claim しない = 沈黙 (message を破壊しない)。再試行も
        # しない (状態は transient ではないため retry で覆らない)。
        _stood_down.set()
        _log(f"delivery not claimed: {err} "
             f"(standing down; not entering claim loop)")
        return None
    gen = res.get("generation")
    if not res.get("ok") or gen is None:
        raise RuntimeError(f"claim-owner rejected: {res}")
    with _gen_lock:
        _generation = int(gen)
    _log(f"registered owner={OWNER} instance={INSTANCE_ID} generation={_generation}")
    return int(gen)


def _current_generation() -> int | None:
    with _gen_lock:
        return _generation


def _register_with_retries() -> bool:
    """register を短くリトライする (transient な daemon 未起動のみ想定)。成功で True。

    **register は生涯 1 回だけ成功させる** のが fencing の要: 一度でも成功したら
    二度と register し直さない (再 register は generation を再度上げ、他の live sidecar を
    fence して generation war を起こす)。よって呼び元は ``_current_generation() is None``
    の間だけ本関数を呼ぶ。
    """
    for attempt in range(1, _REGISTER_RETRIES + 1):
        try:
            gen = _register_owner()
        except Exception as exc:
            _log(f"register attempt {attempt}/{_REGISTER_RETRIES} failed: {exc}")
            if attempt < _REGISTER_RETRIES:
                time.sleep(_REGISTER_RETRY_DELAY)
            continue
        # daemon が register/claim を抑止した (Issue #129 stand-down)。transient では
        # ないので再試行しない。呼び元は claim loop を起動しない。
        if _stood_down.is_set():
            return False
        if gen is not None:
            return True
        # gen None かつ非 stand-down は想定外。失敗として扱い再試行しない。
        return False
    return False


# ----------------------------------------------------------------- push loop
def _push_loop() -> None:
    """~1s で daemon を claim->emit->confirm する配送トランスデューサ (§9.3)。

    poll cadence そのものが daemon への heartbeat に相当する (daemon は最後の claim
    から sidecar の生存を推測できる)。配達確定 (/confirm-delivered) は emit が成功した
    *後* にのみ行う。sidecar が emit 途中で死んでも当該行は lease 失効で UNDELIVERED
    へ戻り (daemon 側 reaping)、lost-message window が閉じる。

    poll/confirm には register で得た ``generation`` と自身の ``instance_id`` を必ず
    載せる (Issue #125 session fencing)。旧世代の sidecar は ``stale_sidecar`` を受け、
    claim せず静かに待機する (再 register はしない — 上記の通り generation war を招く)。
    """
    _started.wait()   # client の initialized を待ってから配送開始
    if _stood_down.is_set():
        # Issue #129: register で抑止された (bg-hosted / unobserved fork)。claim loop を
        # 起動せず沈黙する (message を claim->破壊しない)。stdin loop は生きたまま。
        _log("standing down (delivery suppressed at register); claim loop not started")
        return
    _log(f"push loop start: daemon={DAEMON_URL} owner={OWNER} "
         f"instance={INSTANCE_ID} interval={POLL_INTERVAL}s")
    while True:
        if _stood_down.is_set():
            _log("standing down; exiting claim loop")
            return
        try:
            gen = _current_generation()
            if gen is None:
                # 起動時 register が transient に失敗した場合のみここで再試行する
                # (まだ一度も成功していない間だけ = generation war を起こさない)。
                if not _register_with_retries():
                    if _stood_down.is_set():
                        _log("standing down after register; exiting claim loop")
                        return
                    time.sleep(POLL_INTERVAL)
                    continue
                gen = _current_generation()
            res = _daemon_post("/poll-claims",
                               {"generation": gen, "instance_id": INSTANCE_ID})
            if res.get("error") == "stale_sidecar":
                # 新しい session の sidecar に世代交代された (fork 元 / 旧 session)。
                # 再 register せず待機する (この instance は superseded。session が
                # 終われば自然に消える)。沈黙喪失ではなく単に claim しない。
                _log(f"stale_sidecar: superseded (current gen="
                     f"{res.get('generation')}); standing down")
                time.sleep(POLL_INTERVAL)
                continue
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
                                    {"id": row["id"], "epoch": row.get("epoch", -1),
                                     "generation": gen, "instance_id": INSTANCE_ID})
                if conf.get("ok"):
                    _log(f"confirmed row {row['id']}")
                else:
                    # 既に emit 済。stale_epoch (PUSH->PULL flip) / stale_sidecar 等で行は
                    # UNDELIVERED へ戻り pull/次 push で再配達されうる (msg_id で受信側
                    # dedup 可能)。沈黙喪失ではなく重複側に倒れる。
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
        # session fencing (Issue #125 Major #5): register を **push loop 起動前** に
        # 同期完了させる。先に _started を立てると未登録 generation で poll する起動
        # race が残る。transient 失敗時は push loop 側が (未登録の間だけ) 再試行する。
        # register は **成功後は二度と行わない** (再 register は generation を上げ、
        # 自分の in-flight claim を差し戻して不要な再配達を招く)。conformant client は
        # initialized を 1 回だけ送るが、重複通知でも再 register しないよう guard する。
        if _stood_down.is_set():
            # Issue #129: 既に抑止済 (bg-hosted / unobserved)。重複 initialized でも
            # 再 register せず沈黙を保つ (daemon への suppress 再 journal も避ける)。
            _log("client re-initialized (stood down); not registering")
        elif _current_generation() is not None:
            _log(f"client re-initialized (already registered "
                 f"gen={_current_generation()}); not re-registering")
        elif _register_with_retries():
            _log("client initialized -> registered -> push loop armed")
        else:
            _log("client initialized -> register deferred (daemon unreachable); "
                 "push loop will retry")
        _started.set()        # client ready -> push loop 開始
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
