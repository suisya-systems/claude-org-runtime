# -*- coding: utf-8 -*-
"""org-broker サーバー本体 (orchestrator)。

設計 SoT: docs/design/renga-decoupling.md §4 (broker/adapter 設計)・§4.3
(ナッジ配達)。現行 canonical は本モジュール。歴史的 origin: claude-org-transport-lab
spike/broker.py (Phase 4/5 で MCP surface + allowlist guard + session 検証を確定) を
faithful port したもの。

:class:`Broker` は単一 stateful クラスで、token bind (:class:`~claude_org_runtime.
broker.tokens.TokenMixin`) と queue store (:class:`~claude_org_runtime.broker.
store.StoreMixin`) を mix-in し、自身は HTTP MCP サーバーの lifecycle と、
terminal adapter / スレッドに依存する nudge 配達を持つ。MCP tool の allowlist
分岐は :func:`claude_org_runtime.broker.surface.dispatch_tool` に委譲する。

並行性契約 (移植元の検証済みロジック、巻き戻さない):
- ``_lock`` は binds / queues を一括ガードする単一 Lock。
- nudge 起動は check-and-set をロック下で行い、同一宛先への並行 send_message で
  nudge worker が二重起動 (NUDGE_TEXT 二重注入) しないようにする。
- DELETE / session 失効は ``_journal`` を **ロック外** で呼ぶ (非再入 Lock の
  二重取得デッドロック回避)。
"""

from __future__ import annotations

import hmac
import itertools
import json
import secrets
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..terminal import (
    NUDGE_TEXT,
    PANE_LIVE_ALIVE,
    PANE_LIVE_GONE,
    PANE_LIVE_REUSED,
    PANE_LIVE_UNKNOWN,
    PaneId,
    TerminalAdapter,
    classify_pane_state,
    venv_pane_env,
    venv_pane_prep,
)
from . import sidecar, surface
from .store import ObserverLease, QueueRow, StoreMixin
from .surface import PROTOCOL_VERSIONS, SERVER_INFO, ToolArgError
from .tokens import AgentBind, TokenMixin


class Broker(TokenMixin, StoreMixin):
    """localhost HTTP MCP サーバー + queue store + ナッジ配達。"""

    def __init__(
        self,
        state_dir: str | Path,
        adapter: TerminalAdapter | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
        nudge_defer_interval: float = 2.0,
        nudge_defer_max_tries: int = 30,
        admin_token: str | None = None,
        lease_seconds: float = 30.0,
        observer_lease_seconds: float = 90.0,
        reclaim_warn_threshold: int = 3,
        respawn_burst_window: float = 10.0,
        respawn_burst_threshold: int = 5,
        root_cwd: str | None = None,
    ):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        # root workspace cwd (serve の --root-cwd)。spawn 時の venv 探索フォールバック
        # 基準に使う (Issue #130): pane 自身の cwd/.venv を優先し、無ければ
        # root_cwd/.venv に落とす。worker worktree に .venv が無い通常形をここで拾う。
        # state_dir は broker state の場所であり作業 repo ではないので探索基準にしない。
        self.root_cwd = root_cwd
        self.adapter = adapter
        self.host = host
        self.port = port
        self.nudge_defer_interval = nudge_defer_interval
        self.nudge_defer_max_tries = nudge_defer_max_tries
        # push 一次配送 (§9.3): claim lease の長さと flapping 印字閾値。lease は
        # worst-case emit+confirm 往復より保守的に取る (sidecar 死亡時の reap 遅延と
        # の trade-off)。reclaim_warn_threshold 超で reclaim された行は印字する。
        self.lease_seconds = lease_seconds
        # observed-session binding (Issue #129 問題 A): observer lease の TTL。observed
        # sidecar の register/poll heartbeat (~POLL_INTERVAL 毎) が renew するため、
        # session 継続中は失効せず、poll が止まった dead session のみ TTL 経過で解放する。
        self.observer_lease_seconds = observer_lease_seconds
        self.reclaim_warn_threshold = reclaim_warn_threshold
        # admin HTTP RPC (token mint / graceful shutdown) の認証 token。None なら
        # admin 面は無効 (/admin は 404)。serve が生成し sidecar 0600 に書く。既存
        # の per-agent bearer token (bind 表) とは別系統の認証 (Codex review
        # Blocker: 走行中 daemon への admin 経路 / Major: 認証付き)。
        self.admin_token = admin_token

        self._lock = threading.Lock()
        self._binds: dict[str, AgentBind] = {}        # token -> bind
        # push 一次配送の三状態ライフサイクル (§9.3)。row id -> QueueRow。
        # 旧 agent_id 別 inbox (``_queues``) を行モデルへ置換 (.state/broker schema
        # 改訂)。配送解決は row.to_id で行う。
        self._rows: dict[str, QueueRow] = {}          # row id -> QueueRow
        self._delivery_modes: dict[str, str] = {}     # agent_id -> PUSH/PULL (既定 PUSH)
        self._epochs: dict[str, int] = {}             # agent_id -> mode-epoch (既定 0)
        # session-scoped delivery fencing (Issue #125)。channel sidecar は起動時に
        # register して owner の generation を +1 し自分を現世代に登録する。以後の
        # poll/confirm は現世代のみ許可 (fork replay で同一 cred の二重 sidecar を fence)。
        # _lock で守る (mixin の暗黙属性にせず __init__ で明示確立 — Codex review Major)。
        self._delivery_generations: dict[str, int] = {}   # owner -> current generation (既定 0)
        self._delivery_instances: dict[str, str] = {}     # owner -> current-gen instance id
        self._delivery_poll_seen: dict[str, dict[str, float]] = {}  # owner -> {instance: ts}
        self._duplicate_emit_at: dict[tuple[str, str, str], float] = {}  # dup emit cooldown
        # observed-session binding (Issue #129 問題 A)。owner -> ObserverLease。human
        # launcher が assert_observer で束ね、非 replay 秘密を提示できる sidecar のみ
        # generation を bump できる (fork replay の takeover を断つ)。_lock で守る。
        self._observer_leases: dict[str, ObserverLease] = {}
        self._nudge_threads: dict[str, threading.Thread] = {}
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

        # broker が spawn した pane の登録簿 (token の有無に依らない)。
        # key = str(native handle), value = {handle, name, role, cwd, kind,
        # agent_id, token}。list_panes / resolve_target / set_pane_identity の
        # org メタ (name/role/cwd) の単一の出所。token pane は bind 表にも載るが、
        # generic spawn_pane (token 非注入) はここにのみ載る。
        # _pane_meta / _reserved_names は ``_lock`` (binds/queues と同一の単一
        # Lock) で守る。_lock 下では adapter I/O / _journal / _emit_event を
        # 呼ばない (既存のデッドロック回避契約を pane registry にも適用する)。
        self._pane_meta: dict[str, dict] = {}
        self._reserved_names: set[str] = set()  # spawn in-flight の name 予約
        self._pane_counter = itertools.count(1)
        # 同名連続 spawn の burst dampener (Issue #109 真因D 防御)。name -> 直近で
        # 受理した spawn の timestamp 列 (window 外は都度 prune)。誤 reap 修正
        # (真因B) で false-reap ループ自体は断つが、launcher 側リトライと reap の
        # 相互増幅で同名 pane を短時間に量産する経路への追加防御として、window 内
        # 受理数が threshold 以上なら次の同名 spawn を拒否する。リトライ上限 /
        # バックオフの本体は launcher 側責務 (本タスクスコープ外、Issue #109 に memo)。
        # 既定は緩め (10s に 5 回) で人手の通常 spawn / close→respawn は素通りする。
        self._spawn_history: dict[str, list[float]] = {}
        self.respawn_burst_window = respawn_burst_window
        self.respawn_burst_threshold = respawn_burst_threshold

        # poll_events 用 lifecycle イベント ring (cursor = list index)。
        # 専用 Condition を使い、_lock の binds/queues 契約と絡めない。
        self._events: list[dict] = []
        self._events_cv = threading.Condition()

        # graceful shutdown 用シグナル。admin RPC (shutdown) / KeyboardInterrupt の
        # どちらでも run() の wait_for_shutdown を解除し、run() が唯一の stop()
        # 呼出元として後始末する。Windows で SIGINT に依存しない停止経路
        # (Codex review Blocker 2 対応)。
        self._shutdown_event = threading.Event()
        # broker_stopped を二重に journal しないための one-shot ガード。run() の
        # finally が唯一の stop() 呼出元という契約の保険 (down のオフセット
        # スライスで broker_stopped が厳密に 1 回であることを保証する)。
        self._stopped = False

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        broker = self

        class Handler(_McpHandler):
            pass

        class QuietServer(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                # クライアント側切断 (WinError 10054 等) はログ汚染しない
                import sys as _sys
                exc = _sys.exception()
                if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                                    BrokenPipeError, TimeoutError)):
                    return
                super().handle_error(request, client_address)

        Handler.broker = broker
        self._server = QuietServer((self.host, self.port), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="broker-http", daemon=True
        )
        self._thread.start()
        self._journal("broker_started", host=self.host, port=self.port)

    def stop(self) -> None:
        """HTTP サーバーを停止し journal に ``broker_stopped`` を残す (冪等)。

        ``_stopped`` ガードで ``broker_stopped`` の journal は **厳密に 1 回**。
        run() の finally が唯一の呼出元だが、二重呼び (admin teardown 等) でも
        down のオフセットスライスが複数 ``broker_stopped`` を拾わないようにする。
        """
        if self._stopped:
            return
        self._stopped = True
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        self._journal("broker_stopped")

    def request_shutdown(self) -> None:
        """走行中 daemon に graceful shutdown を要求する (admin RPC の実体)。

        ``_shutdown_event`` を立てるだけで、実際の停止 (stop() + sidecar 削除) は
        :meth:`wait_for_shutdown` を待つ run() 側が行う。HTTP ハンドラスレッドから
        ``ThreadingHTTPServer.shutdown()`` を直接呼ぶデッドロックを避けるための分離
        (シグナルに依存しない停止 = Windows 要件)。"""
        self._shutdown_event.set()

    def wait_for_shutdown(self, timeout: float | None = None) -> bool:
        """shutdown 要求 (admin RPC) があるまでブロックする。run() の前景ループ。

        返り値は :meth:`threading.Event.wait` 準拠 (要求済み=True / timeout=False)。
        serve は前景 debug primitive のまま (既存挙動不変) で、ここがその待機点を
        ``time.sleep`` ループから差し替えたもの。KeyboardInterrupt でも抜ける。"""
        return self._shutdown_event.wait(timeout)

    # --------------------------------------------------------------- admin RPC
    def admin_mint_token(self, params: dict) -> dict:
        """走行中 daemon に対し新規 root token を発行する (admin RPC の実体)。

        Codex review Blocker 1 対応: serve は起動時に token を stdout へ 1 回出す
        だけで、走行中 daemon への token 発行経路が無かった。本メソッドが tier
        (``role`` = auth_role) 指定の token を mint する。root token と同様、これは
        spawn 子ではないため tier 上限切り (``capped_auth_role``) は適用せず、要求
        どおりの tier で bind する (= secretary 等を直接発行できる)。返り値に
        ``--mcp-config`` を含め、呼び元 (org up / タスク 2) がそのまま使える。

        ``role`` は :data:`surface.ROOT_ROLE_CHOICES` の集合で検証する (CLI の
        ``--root-role`` と同じ canonical 集合)。``cwd`` は relative spawn の解決
        アンカー (Issue #61) として bind に持たせる (任意)。

        ``channel`` (任意、既定 False): True なら mint した token の ``mcp_config`` に
        push 一次配送の channel sidecar (``org-broker-channel``, OWNER=この agent) を
        積み delivery-scoped credential を発行する (spawn_claude の子経路ミラー)。
        secretary(窓口) 起動経路がこれで dev-channel sidecar を持ち push が届く。

        ``observer`` (任意、既定 False): observed-session binding (Issue #129 問題 A) を
        有効にする。``channel`` を要し、True の時だけ observer lease を assert して
        ``observer_secret`` を返す。呼び元はこの秘密を子プロセス env へ handoff する契約
        (org up 経路)。指定しない channel mint は従来の last-register-wins のまま。
        """
        role = params.get("role", "worker")
        if role not in surface.ROOT_ROLE_CHOICES:
            return {
                "ok": False,
                "error": (
                    f"[invalid_role] {role!r} not in "
                    f"{surface.ROOT_ROLE_CHOICES}"
                ),
            }
        cwd = params.get("cwd")
        if cwd is not None:
            if not isinstance(cwd, str):
                return {"ok": False, "error": "[invalid_cwd] cwd must be a string"}
            # relative cwd は daemon 起動 cwd 基準で絶対化する。serve の --root-cwd と
            # 同じ正規化 (相対のまま bind に積むと relative spawn の解決アンカーが
            # 相対になり Issue #61 が admin 経路で再発する。Codex review Major)。
            cwd = sidecar.absolutize(cwd)
        name = params.get("name")
        if name is not None and not isinstance(name, str):
            return {"ok": False, "error": "[invalid_name] name must be a string"}
        # channel は厳密に bool。truthy 文字列/数値で credential 発行が誤発火しない
        # ように、非 bool は [invalid_params] で拒否する (cwd/name と同じ検証方針)。
        channel = params.get("channel", False)
        if not isinstance(channel, bool):
            return {"ok": False, "error": "[invalid_params] channel must be a boolean"}
        # observer (任意、既定 False): observed-session binding を有効にする (Issue #129
        # 問題 A)。**明示 opt-in の時だけ** observer lease を assert し秘密を返す。これは
        # 呼び元が秘密を子プロセス env (ORG_BROKER_CHANNEL_OBSERVER) へ handoff する契約と
        # ペアで、org up の human-facing 経路だけが指定する。channel だけの mint (secret
        # handoff を持たない admin caller が mcp_config だけで起動する) に lease を張ると、
        # その sidecar が秘密を提示できず unobserved で stand-down し push が止まるため、
        # ここは opt-in に閉じる (Codex review P2)。observer は channel を要する。
        observer = params.get("observer", False)
        if not isinstance(observer, bool):
            return {"ok": False, "error": "[invalid_params] observer must be a boolean"}
        if observer and not channel:
            return {"ok": False,
                    "error": "[invalid_params] observer requires channel"}
        # 既定 agent_id は毎回一意にする: 固定名だと複数回 mint した token が同一
        # agent として bind/queue を共有し配送先が曖昧化する (agent_id 基準の
        # 配送/排出。Codex review Major)。明示 name 指定時はそれを agent_id に使うが、
        # その場合も unique=True で重複 (root token や別 mint との衝突) を原子的に
        # 拒否する (Codex review round 2 Major: 明示 name の重複未防御)。
        agent_id = name or f"admin-{secrets.token_hex(4)}"
        try:
            token = self.issue_token(
                agent_id, agent_id, role, cwd=cwd, auth_role=role, unique=True,
            )
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        mcp_config = self.mcp_config_for(token)
        # channel 配線 (push 一次配送 §9.5): ``channel`` 要求時のみ org-broker-channel
        # sidecar (OWNER=この agent) を mcp_config に積み delivery-scoped credential を
        # 発行する。secretary(窓口) mint 経路が子 (spawn_claude) と同じ channel sidecar を
        # 持ち、root Claude Code にも push 一次が届くようにする (本タスクの本丸)。
        # control-plane の probe / down ctrl token は channel を要求しないため、使い捨て
        # token に未使用 delivery cred を leak させない。
        observer_secret: str | None = None
        if channel:
            delivery_cred = self.issue_delivery_cred(agent_id)
            mcp_config["mcpServers"]["org-broker-channel"] = (
                self.channel_server_config(delivery_cred, agent_id)
            )
            # observed-session binding (Issue #129 問題 A): observer 明示 opt-in の時だけ
            # lease を assert し秘密を返す (= human-facing observed session の起動)。呼び元
            # (org up) はこれを **mcp-config ではなく子プロセス env** に注入する
            # (assert_observer の非 replay 契約)。この session の sidecar だけが秘密を
            # 提示でき generation を bump できる。fork replay は mcp_config (delivery
            # cred 込み) を継承しても process env の秘密を持たないため takeover 不可。
            # 秘密は mcp_config に **入れない** (persisted secretary-mcp.json は replay
            # 面なので、そこへ入れると fork が復元でき束縛が破れる)。observer を指定しない
            # channel mint は従来の last-register-wins のまま (secret handoff 不要)。
            if observer:
                observer_secret = self.assert_observer(agent_id)
        return {
            "ok": True,
            "token": token,
            "agent_id": agent_id,
            "name": agent_id,
            "role": role,
            "mcp_config": mcp_config,
            "observer_secret": observer_secret,
        }

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/mcp"

    @property
    def base_url(self) -> str:
        """``/mcp`` を含まない daemon の base URL (channel sidecar の delivery 経路)。

        channel sidecar はここに ``/poll-claims`` / ``/confirm-delivered`` を付けて
        叩く (MCP 面とは別系統の delivery endpoint)。"""
        return f"http://{self.host}:{self.port}"

    @property
    def admin_url(self) -> str:
        return f"http://{self.host}:{self.port}/admin"

    # ----------------------------------------------------------------- nudge
    def _trigger_nudge(self, target: AgentBind) -> None:
        """ナッジ配達 (設計書 §4.3)。定型 1 行のみ PTY 経由、本文は通さない。

        注入前に get-text で入力欄静止を確認し、静止していなければ
        defer + 再試行する (確定事項 (1) の静止確認)。
        重複ナッジは冪等 (キュー消費は check_messages 側で一度きり)。
        """
        if self.adapter is None or target.pane_id is None:
            return
        key = target.agent_id
        # check-and-set はロック下で行う: ThreadingHTTPServer 配下で同一宛先へ
        # 並行 send_message された場合の nudge worker 二重起動 (= NUDGE_TEXT
        # 二重注入) を防ぐ (codex review round 3 Major 対応)
        with self._lock:
            existing = self._nudge_threads.get(key)
            if existing and existing.is_alive():
                return  # 配達スレッドが既に走っている (冪等性)
            t = threading.Thread(
                target=self._nudge_worker, args=(target,),
                name=f"nudge-{key}", daemon=True,
            )
            self._nudge_threads[key] = t
        t.start()

    def _nudge_worker(self, target: AgentBind) -> None:
        pane_id = target.pane_id
        assert pane_id is not None and self.adapter is not None
        from .store import CLAIMED, UNDELIVERED
        for attempt in range(1, self.nudge_defer_max_tries + 1):
            with self._lock:
                # UNDELIVERED または (まだ生きている) CLAIMED 行が残っていれば pending。
                # push 経路が claim 中でも fallback nudge は冪等 (claim-respecting
                # check_messages が二重配達を防ぐ)。drain/confirm 済なら再ナッジ不要。
                pending = any(
                    r.to_id == target.agent_id and r.state in (UNDELIVERED, CLAIMED)
                    for r in self._rows.values()
                )
            if not pending:
                return  # 配達前に drain 済み (再ナッジ不要)
            try:
                state = classify_pane_state(self.adapter.get_text(pane_id))
            except Exception as e:  # adapter 不通は nudge_failed 相当
                self._journal(
                    "nudge_failed", agent_id=target.agent_id, error=str(e)
                )
                return
            if state == "idle":
                self.adapter.send_line(pane_id, NUDGE_TEXT)
                self._journal(
                    "nudge_sent",
                    agent_id=target.agent_id,
                    pane_id=pane_id,
                    attempt=attempt,
                )
                return
            self._journal(
                "nudge_deferred",
                agent_id=target.agent_id,
                pane_id=pane_id,
                state=state,
                attempt=attempt,
            )
            time.sleep(self.nudge_defer_interval)
        self._journal(
            "nudge_failed",
            agent_id=target.agent_id,
            pane_id=pane_id,
            error="defer retries exhausted",
        )

    # ------------------------------------------------------------- MCP tools
    def call_tool(self, bind: AgentBind, name: str, args: dict) -> dict:
        """ツール実行。allowlist 分岐は surface.dispatch_tool に委譲する。
        引数不正は ToolArgError (handler 側で -32602 に変換)。"""
        return surface.dispatch_tool(self, bind, name, args)

    # ---------------------------------------------------------- pane: 解決
    def resolve_target(self, target: str) -> "PaneId | None":
        """pane addressing を native handle に解決する (§3.3-2)。

        三系統 (renga と同契約): 全桁数字 → handle / 非数字 str → stable name /
        'focused' → 現在フォーカス pane。解決不能なら None。

        加えて、非数字の *managed handle* 直指定 (tmux ``"%N"`` / Herdr
        ``"wN:pN"``) も stable name の後段フォールバックとして解決する。``org
        down`` の launcher が ``list_panes`` の id (= native handle) を
        ``close_pane`` に渡す経路で、この id が非数字だと従来は name にも一致せず
        ``[pane_not_found]`` になっていた (Issue #100)。stable name は許可文字集合
        ``[A-Za-z0-9_-]`` に閉じ、非数字 handle は ``:`` / ``%`` を必ず含むため
        両者の文字集合は交わらず、この追加解決は name 解決を shadow しない
        (安全性はテストで固定する)。auth tier は resolve_target では判定せず
        caller の bind (``auth_role``) 側で切るため、解決経路の追加は tier 境界を
        変えない。
        """
        if self.adapter is None:
            return None
        # adapter I/O は lock 外で先に済ませる (lock 下で I/O しない契約)。
        panes = self._adapter_panes()
        # registry を信じる入口の opportunistic reap: adapter snapshot に無い自己終了
        # managed pane をここで掃除し、表示 (list_panes_view) と解決・予約の非対称を
        # 埋める。取得済み snapshot を渡して二重 list_panes を避ける。解決ロジック自体
        # (下の三系統マッチ) は不変で、入口で共通 cleanup helper を呼ぶ形に留める。
        self._reap_stale_managed_panes(panes)
        if target == "focused":
            for p in panes:
                if p.get("active"):
                    return p.get("pane_id")
            return None
        if surface._ALL_DIGITS.match(target):
            # 全桁数字は常に handle (renga 契約)。native 型を保って返す。
            with self._lock:
                meta = self._pane_meta.get(target)
            if meta is not None:
                return meta["handle"]
            for p in panes:
                if str(p.get("pane_id")) == target:
                    return p.get("pane_id")
            return None
        # 非数字 str → stable name 一致 (broker が知る pane の name)。
        with self._lock:
            for meta in self._pane_meta.values():
                if meta.get("name") == target:
                    return meta["handle"]
            for b in self._binds.values():
                if not b.revoked and b.name == target and b.pane_id is not None:
                    return b.pane_id
            # name に一致しなければ、非数字 managed handle の直指定として引く
            # (org down の list_panes → close_pane 経路。docstring 参照)。
            meta = self._pane_meta.get(target)
            if meta is not None:
                return meta["handle"]
        # 登録簿 (_pane_meta) に無い adapter pane も native handle で解決する
        # (全桁数字ブランチと対称。adapter I/O は lock 外で済ませた panes を使う)。
        for p in panes:
            if str(p.get("pane_id")) == target:
                return p.get("pane_id")
        return None

    def _adapter_panes(self) -> list[dict]:
        """adapter.list_panes() の安全ラッパ (adapter 無しは空)。"""
        if self.adapter is None:
            return []
        return self.adapter.list_panes()

    def _meta_for(self, handle: "PaneId") -> dict | None:
        return self._pane_meta.get(str(handle))

    # ---------------------------------------------------------- pane: 出力面
    def list_panes_view(self) -> list[dict]:
        """renga golden shape の list_panes 出力 (id/name/role/focused/x/y/w/h/cwd)。

        geometry / focused は adapter (native: pane_id/left/top/width/height/
        active) から、name/role/cwd/kind は broker の pane 登録簿から取る
        (cwd は tmux capture に無いため bind/登録簿が唯一の出所 — §3.3-4)。
        receive_mode は push 一次の定数 (D2: §9.6 push primary / pull fallback)。
        """
        panes = self._adapter_panes()
        with self._lock:  # _pane_meta の一貫スナップショット (iteration 中 mutation 回避)
            meta_snapshot = {k: dict(v) for k, v in self._pane_meta.items()}
        out: list[dict] = []
        for p in panes:
            handle = p.get("pane_id")
            meta = meta_snapshot.get(str(handle), {})
            out.append({
                "id": handle,
                "name": meta.get("name"),
                "role": meta.get("role"),
                "focused": bool(p.get("active", False)),
                "x": p.get("left", p.get("x", 0)),
                "y": p.get("top", p.get("y", 0)),
                "w": p.get("width", 0),
                "h": p.get("height", 0),
                "cwd": meta.get("cwd"),
                "kind": meta.get("kind"),
                "receive_mode": surface.RECEIVE_MODE,
            })
        # 論理ペイン (人間駆動の窓口) は実 adapter pane を持たないため上の loop に
        # 出ない。``logical=True`` かつ adapter に存在しない登録簿エントリを
        # first-class entry として補う (geometry/focused は実体なしの既定値)。
        # 条件を「logical かつ非 adapter」に限定するのは、adapter が out-of-band で
        # 閉じた pane の stale meta を resurface させないため。
        #
        # 既知制限 (global-mux backend, wezterm): wezterm `cli list` は dedicated
        # socket 分離が無く、窓口自身の実 pane も匿名 (name/role=None) entry として
        # 返す。よって wezterm では窓口が「匿名の実 pane」+「ここで補う logical
        # entry」の二重で並ぶ。dedup には窓口の実 pane_id との相関 (= 実ペイン化、
        # 本 Issue のスコープ外) が要るため、二重表示は許容する。tmux
        # (isolated socket) では adapter に窓口が出ないため logical entry が唯一の
        # 出所で、二重化は起きない。
        adapter_handles = {str(p.get("pane_id")) for p in panes}
        for hk, meta in meta_snapshot.items():
            if hk in adapter_handles or not meta.get("logical"):
                continue
            out.append({
                "id": meta.get("handle"),
                "name": meta.get("name"),
                "role": meta.get("role"),
                "focused": False,
                "x": 0, "y": 0, "w": 0, "h": 0,
                "cwd": meta.get("cwd"),
                "kind": meta.get("kind"),
                "receive_mode": surface.RECEIVE_MODE,
            })
        return out

    def inspect_pane_view(
        self, target: str, lines: int | None, fmt: str, include_cursor: bool
    ) -> dict:
        """pane の画面 scrape (grid scrape)。renga inspect_pane と同形の結果。"""
        if self.adapter is None:
            return _err("[no_backend] no terminal adapter configured")
        handle = self.resolve_target(target)
        if handle is None:
            return _err(f"[pane_not_found] no pane for target {target!r}")
        screen = self.adapter.get_text(handle)
        rows = screen.splitlines()
        if lines is not None:
            rows = rows[-lines:]
        payload: dict = {"target": target}
        if include_cursor:
            # cursor 位置は adapter list_panes の cursor_x/cursor_y から best-effort。
            cur = None
            for p in self._adapter_panes():
                if p.get("pane_id") == handle:
                    cur = {
                        "visible": True,
                        "row": p.get("cursor_y", 0),
                        "col": p.get("cursor_x", 0),
                    }
                    break
            payload["cursor"] = cur
        if fmt == "grid":
            grid = [{"row": i, "text": r} for i, r in enumerate(rows)]
            payload["grid"] = grid
            text = json.dumps(grid, ensure_ascii=False)
        else:
            text = "\n".join(rows)
            payload["text"] = text
        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": payload,
        }

    def send_keys_to(
        self, target: str, text: str | None, keys: list[str], enter: bool
    ) -> dict:
        """raw PTY 打鍵 (renga send_keys 同形)。

        ``keys`` は surface で **canonical** 形へ正規化済み
        (:func:`~claude_org_runtime.terminal.keys.normalize_key`)。実打鍵可能な
        canonical 部分集合は backend adapter の ``supported_named_keys`` が宣言し
        (tmux は full、Herdr は subset、WezTerm は Enter / Ctrl+C の最小 subset)、
        text 送信の**前に**送信予定の全キーを preflight する。未対応が 1 つでもあれば全体を
        ``[key_unsupported]`` で拒否する (all-or-nothing: 途中まで打鍵して画面を
        壊さない、Issue #108 確定事項 (1))。順序は text -> keys -> enter を維持する。
        """
        if self.adapter is None:
            return _err("[no_backend] no terminal adapter configured")
        handle = self.resolve_target(target)
        if handle is None:
            return _err(f"[pane_not_found] no pane for target {target!r}")
        seq = list(keys) + (["enter"] if enter else [])
        # adapter が emit 可能な canonical だけを許す。未対応キーがあれば **text を
        # 送る前に**全体を弾く (部分実行で画面を壊さない all-or-nothing preflight)。
        supported = getattr(self.adapter, "supported_named_keys", frozenset())
        unsupported = [k for k in seq if k not in supported]
        if unsupported:
            return _err(
                f"[key_unsupported] canonical keys {unsupported!r} are not emittable "
                f"by the current terminal adapter (supported: {sorted(supported)})"
            )
        if text:
            self.adapter.type_text(handle, text)
        if seq:
            self.adapter.send_named_keys(handle, seq)
        return _ok({"ok": True, "target": target})

    # ---------------------------------------------------- pane: 共通 cleanup / reap
    def _cleanup_pane(self, handle: "PaneId") -> tuple[str | None, bool]:
        """pane の bookkeeping を掃除する — close_pane と自己終了 reap の共通経路。

        明示 close (:meth:`close_pane_target`) と入口 opportunistic reap
        (:meth:`_reap_stale_managed_panes`) の両方から呼ぶ単一 helper。**adapter I/O
        (kill_pane) は呼ばない**: close は呼び元が明示 kill し、reap 対象は既に消えた
        pane なので kill 不要。掃除内容は切戻し §5.5 と同一:

          1. ``_pane_meta`` から pop (org メタの単一出所を落とす)
          2. bind の full revoke (``revoked=True`` / ``registered=False``) —
             spawn 直後の ``issue_token(unique=True)`` が未 revoke bind を見て
             ``[name_taken]`` を返す幽霊 binding を断つ (本 Issue の本丸)
          3. delivery-scoped credential の revoke
          4. delivery_mode / epoch のリセット
          5. 未配達 queue 行の破棄 (revoked bind は uniqueness から外れ同名 re-spawn
             できるため、残すとクロスセッション誤配送になる)

        lock 規律: meta pop と bind revoke は ``_lock`` 下で原子的に、自前で ``_lock``
        を取る delivery 掃除 (revoke/reset/discard) は lock 解放後に呼ぶ (非再入 Lock
        の二重取得回避 — 既存 close 経路と同じ順序)。``pane_exited`` event 発行と
        journal は経路別 (pane_closed / pane_reaped) に呼び元が行う。

        返り値 ``(agent_id, found)``: ``found=False`` は既に掃除済み (並行 close/reap
        に先を越された二重掃除) を表す。
        """
        with self._lock:
            meta = self._pane_meta.pop(str(handle), None)
            agent_id = meta.get("agent_id") if meta else None
            tok = meta.get("token") if meta else None
            if tok and tok in self._binds:
                b = self._binds[tok]
                b.revoked = True
                b.registered = False
        # delivery 掃除は **token-backed pane** に限る (tok が真の時のみ)。generic
        # spawn_pane は token=None で登録され、channel sidecar も delivery cred も queue
        # 行も持たない。その meta agent_id は bind-only の別 live agent (admin_mint_token
        # で mint された同名 channel agent 等) と衝突しうる (名前空間が非交差) ため、
        # token 無し pane の reap/close で無関係な live agent の delivery state
        # (cred / mode / 未配達行) を巻き込むと誤って配送を壊す (Codex review P2)。
        # token 有り pane では agent_id == token の owner なので従来どおり full 掃除する。
        if tok and agent_id:
            self.revoke_delivery_creds(agent_id)
            self.reset_delivery_state(agent_id)
            # 未配達行も破棄する: revoked bind は uniqueness から除外され同名 re-spawn が
            # 可能なため、残すと旧セッション宛の行を新しい同名 agent が drain/claim する
            # クロスセッション誤配送になる (Codex review Major / 切戻し §5.5(5))。
            self.discard_agent_rows(agent_id)
        return agent_id, meta is not None

    def _reap_stale_managed_panes(self, panes: list[dict] | None = None) -> None:
        """adapter snapshot に無い broker 管理 pane (自己終了) を掃除する。

        registry を信じる入口 (:meth:`resolve_target` / :meth:`_reserve_name`、後者
        経由で spawn 群 / 前者経由で set_pane_identity・close・inspect・send_keys) から
        共有で呼ぶ opportunistic reap。``list_panes_view`` は既に「adapter snapshot に
        いる pane のみ live」と判定しているが (表示面)、予約・解決の入口はそれを見て
        いなかった — その非対称 (表示と予約の不一致) が幽霊 binding の根本。ここで両者を
        揃える。

        **決定的 pane 単位モデル (Issue #109 真因B)**: 「snapshot に現れない」を即
        「物理的に消えた」と等値せず、pane 単位の liveness で判定する。``_pane_meta``
        の非 logical entry を snapshot と突き合わせ:
          - live 集合にいる → ``last_seen_at`` 更新・欠落状態クリア。
          - live 集合にいない → 連続欠落として ``missing_count`` を積み上げ、
            **age (= now - spawned_at) が ``reap_min_age_seconds`` 超**かつ
            **``missing_count`` が ``reap_min_missing_snapshots`` 以上**の時だけ
            reap 候補にする。閾値は adapter の ClassVar から backend-aware に読む
            (getattr フォールバックで既定 backend は 0.0/1 = 従来の即時 reap)。
        これで Herdr の eventually consistent snapshot (boot 中・ラグで生 pane が一時
        欠落) による誤 reap を防ぐ。**logical pane (human-driven の窓口) は reap 対象外**
        — adapter snapshot に永遠に出ないため、reap すると窓口が消える。

        **物理 close 検証 (真因A)**: reap 候補を bookkeeping 削除する前に fresh
        ``pane_exists`` で再確認する。(a) 確認が backend 不通で取れなければこのラウンドは
        skip し次回に委ねる (backend blip 中の誤 reap 回避)。(b) 依然残存していれば
        eventually consistent snapshot が欠落させていただけで pane は生存しているので、
        物理 close (:meth:`kill_pane_detailed` があれば詳細、無ければ ``kill_pane`` +
        ``pane_exists`` 後確認) を試み、その成否を journal する — reap 経路が物理 close を
        呼ばない旧設計が孤児 TUI を残していた真因の是正。(c) 不在なら従来どおり bookkeeping
        のみ掃除。

        adapter I/O (list_panes / pane_exists / kill) は lock 外。呼び元が既に snapshot
        を持つ場合は ``panes`` で渡して二重 list_panes を避ける (resolve_target の hot
        path 用)。reap した pane には ``pane_exited`` event を発行し ``pane_reaped`` を
        journal する (明示 close は ``pane_closed``。dispatcher の poll_events(pane_exited)
        依存に合わせ event type は close と揃え、journal 語彙のみ検知経路で区別する)。
        """
        if self.adapter is None:
            return
        if panes is None:
            panes = self._adapter_panes()
        live = {str(p.get("pane_id")) for p in panes}
        now = time.time()
        min_age = float(getattr(self.adapter, "reap_min_age_seconds", 0.0))
        min_missing = int(getattr(self.adapter, "reap_min_missing_snapshots", 1))
        min_missing_secs = float(getattr(self.adapter, "reap_min_missing_seconds", 0.0))
        candidates: list["PaneId"] = []
        with self._lock:  # liveness 更新と候補抽出のみ lock 下 (I/O は下で lock 外)
            for hk, m in self._pane_meta.items():
                if m.get("logical"):
                    continue
                if hk in live:
                    # 目撃: 連続欠落をリセット (age だけでなく「連続」性を担保)。
                    m["last_seen_at"] = now
                    m["missing_since"] = None
                    m["missing_count"] = 0
                    continue
                # snapshot に不在: 連続欠落を積む (物理消滅とは即断しない)。
                m["missing_count"] = int(m.get("missing_count", 0)) + 1
                if m.get("missing_since") is None:
                    m["missing_since"] = now
                # age は spawn からの経過。既定 backend (min_age=0.0) は clock 分解能に
                # 依らず即 reap する必要があるため境界を含める (>=)。Herdr (min_age>0)
                # では boot 中の一時欠落を保護する実効差は無視できる。
                age = now - float(m.get("spawned_at", now))
                # missing_count は reap **呼び出し回数**を数える (poll cadence 非依存)。
                # broker は ThreadingHTTPServer 配下で resolve_target / _reserve_name を
                # 並行に呼ぶため、単一の snapshot ラグ窓の間に複数スレッドが立て続けに
                # missing_count を積むと、count だけでは「連続 snapshot 欠落」の意図を
                # 満たさず生 pane を誤判定しうる (adversarial review Major)。そこで
                # missing_since を使った **実時間ゲート** (now - missing_since >=
                # reap_min_missing_seconds) を併用し、cadence 非依存にする: 何回呼ばれても
                # 実時間が経たない限り閾値を超えない。既定 backend (=0.0) は初回欠落で
                # 即成立し従来の即時 reap を保つ。Herdr (>0) は連続欠落が実時間で継続した
                # 時のみ reap する (単一ラグ窓での bursty 誤 reap を構造的に断つ)。
                missing_secs = now - float(m.get("missing_since", now))
                if (
                    age >= min_age
                    and m["missing_count"] >= min_missing
                    and missing_secs >= min_missing_secs
                ):
                    candidates.append(m["handle"])
        for handle in candidates:
            meta = self._meta_for(handle) or {}
            # 権威 liveness (Fix-D, 真因A/B): adapter が workspace 非依存の
            # pane.get liveness を持つ backend (herdr) では、snapshot 欠落から
            # 「消えた」を即断せず、pane_id を直接引いて terminal_id 照合で生死を
            # 権威判定してから bookkeeping を落とす。list_panes/pane_exists 由来の
            # stale snapshot に依存した「盲目的 pane.close して close 成否で判定」の
            # 反転論理 (生 pane を close して生きていた証拠を得る) を廃する。
            verdict = self._authoritative_liveness(handle, meta.get("terminal_id"))
            if verdict is not None:
                if verdict in (PANE_LIVE_ALIVE, PANE_LIVE_UNKNOWN):
                    # ALIVE: 我々の pane が生存 (placement 崩れ / snapshot lag で欠落した
                    # だけ)。誤 reap 禁止。UNKNOWN: backend 不通で判定不能。いずれも
                    # bookkeeping を保持し defer し、次ラウンドに委ねる。
                    self._journal(
                        "pane_reap_deferred", pane_id=handle,
                        agent_id=meta.get("agent_id"),
                        kill={"closed_via": "live_" + verdict, "still_present": None},
                    )
                    continue
                # GONE: 権威的に消滅 (close 不要)。REUSED: pane_id が別 pane に再利用され
                # 我々の pane は消滅。いずれも bookkeeping を掃除するが、**物理 close は
                # 発行しない**: GONE は既に不在、REUSED はその pane_id が今や無関係 pane で
                # close すると巻き添える (isolation 保護)。
                closed_via = (
                    "already_gone_verified" if verdict == PANE_LIVE_GONE
                    else "id_reused_skip_close"
                )
                agent_id, found = self._cleanup_pane(handle)
                if not found:
                    continue  # 並行 close/reap が先に掃除した
                self._emit_event(
                    {"type": "pane_exited", "pane_id": handle, "agent_id": agent_id}
                )
                self._journal(
                    "pane_reaped", pane_id=handle, agent_id=agent_id,
                    kill={"closed_via": closed_via, "still_present": False},
                )
                continue
            # ---- 従来経路 (tmux/wezterm: 権威 liveness を持たない backend) ----
            # 物理 close 検証 (真因A): bookkeeping を落とす前に **必ず物理 close を
            # 発行**する。list_panes 由来の pane_exists を「消えていそうか」の事前 probe に
            # 使うと、生きているのに snapshot から欠落した pane を「不在」と誤読し、close を
            # 発行せず bookkeeping だけ落として生 TUI を孤児化しうる (Codex round2 P2)。
            # よって事前判定はせず常に close する: 既に消えていれば no-op (already_gone)、
            # snapshot が欠落させていただけで生存していれば実際に閉じる (idempotent)。
            kill_result = self._physically_close_reaped(handle)
            # bookkeeping を落とすのは **close が有効だった (= pane を消せた / 既に消えて
            # いた) と分かった時だけ**。close が拒否 / backend 不通なら pane が生きたまま
            # 残りうるので meta/token を保持し次ラウンドで再 close を試みる (defer)。判定は
            # stale な post-close probe (still_present) ではなく close 経路 (closed_via) で
            # 行う — 「close を発行して受理された」事実に基づく (Codex round2 P2)。
            if not self._reap_close_effective(kill_result):
                self._journal(
                    "pane_reap_deferred", pane_id=handle,
                    agent_id=meta.get("agent_id"),
                    kill=kill_result,
                )
                continue
            agent_id, found = self._cleanup_pane(handle)
            if not found:
                continue  # 並行 close/reap が先に掃除した
            self._emit_event(
                {"type": "pane_exited", "pane_id": handle, "agent_id": agent_id}
            )
            self._journal(
                "pane_reaped", pane_id=handle, agent_id=agent_id, kill=kill_result,
            )

    # close が「有効だった (pane を消せた or 既に消えていた)」とみなす closed_via。
    # これ以外 ("refused"=close 拒否で生存 / "list_failed"・"error"=backend 不通で
    # 未確認) は pane が残りうるため reap を defer する (bookkeeping を落とさない)。
    _REAP_CLOSE_EFFECTIVE = frozenset(
        {"pane.close", "workspace.close", "already_gone", "kill_pane"}
    )

    def _reap_close_effective(self, kill_result: dict) -> bool:
        """物理 close が pane を消せた/既に不在だったと判断できるか (真因A / P2)。"""
        return kill_result.get("closed_via") in self._REAP_CLOSE_EFFECTIVE

    def _authoritative_liveness(
        self, handle: "PaneId", terminal_id: "PaneId | None"
    ) -> str | None:
        """adapter が workspace 非依存の権威 liveness を持てば verdict を返す (Fix-D)。

        持てば :data:`PANE_LIVE_ALIVE` / :data:`PANE_LIVE_REUSED` /
        :data:`PANE_LIVE_GONE` / :data:`PANE_LIVE_UNKNOWN` のいずれか、持たなければ
        ``None`` (呼び元は ``None`` で従来の物理 close 検証経路にフォールバックする)。

        真因A/B: ``list_panes`` / ``pane_exists`` は自 workspace filter 越しの liveness で、
        placement バグや workspace 消失で生 pane を構造的に欠落させ、それを reaper が
        「消えた」と誤読して生 pane を close (= 誤 reap) していた。``pane.get`` のような
        workspace 非依存の直接 probe を持つ backend (herdr) では、これを reap 決定の権威に
        する。best-effort: adapter が例外を上げたら :data:`PANE_LIVE_UNKNOWN` に倒して
        defer させる (判定不能時に誤 reap しない安全側)。
        """
        fn = getattr(self.adapter, "pane_liveness", None)
        if not callable(fn):
            return None
        try:
            verdict = fn(handle, terminal_id)
        except Exception:  # noqa: BLE001 - best-effort; 判定不能は defer 側に倒す
            return PANE_LIVE_UNKNOWN
        # adapter が想定外の値を返しても安全側 (UNKNOWN=defer) に正規化する。
        if verdict in (
            PANE_LIVE_ALIVE, PANE_LIVE_REUSED, PANE_LIVE_GONE, PANE_LIVE_UNKNOWN
        ):
            return verdict
        return PANE_LIVE_UNKNOWN

    def _physically_close_reaped(self, handle: "PaneId") -> dict:
        """reap 候補に物理 close を発行し、close 経路と残存を返す (真因A)。

        adapter が :meth:`kill_pane_detailed` を持てば close 経路 (``closed_via``) /
        残存 (``still_present``) の詳細を、無ければ ``kill_pane`` + close 後
        ``pane_exists`` で最小の可視化を返す。close が有効だったかの判定は呼び元が
        ``closed_via`` (:meth:`_reap_close_effective`) で行う。best-effort (孤児化回避が
        目的) なので adapter 例外は握り潰し ``closed_via="error"`` に落とす (= 呼び元は
        defer する)。``still_present`` は journal 補助 (list-backed backend では stale)。
        """
        detailed = getattr(self.adapter, "kill_pane_detailed", None)
        if callable(detailed):
            try:
                return detailed(handle)
            except Exception as exc:  # noqa: BLE001 - best-effort reap kill
                return {"closed_via": "error", "still_present": None,
                        "error": str(exc)}
        result: dict = {"closed_via": None, "still_present": None}
        try:
            self.adapter.kill_pane(handle)
            result["closed_via"] = "kill_pane"
        except Exception as exc:  # noqa: BLE001 - best-effort reap kill
            result["closed_via"] = "error"
            result["error"] = str(exc)
        try:
            result["still_present"] = bool(self.adapter.pane_exists(handle))
        except Exception:  # noqa: BLE001 - 後確認も best-effort
            result["still_present"] = None
        return result

    def close_pane_target(self, target: str) -> dict:
        """pane を閉じる (renga close_pane 同形)。token を revoke しイベントを emit。"""
        if self.adapter is None:
            return _err("[no_backend] no terminal adapter configured")
        handle = self.resolve_target(target)
        if handle is None:
            return _err(f"[pane_not_found] no pane for target {target!r}")
        # adapter I/O は lock 外で先に済ませる (lock 下で I/O しない契約)。
        adapter_panes = self._adapter_panes()
        with self._lock:
            target_meta = self._pane_meta.get(str(handle))
            is_logical = bool(target_meta and target_meta.get("logical"))
            logical_count = sum(
                1 for m in self._pane_meta.values() if m.get("logical")
            )
        # 論理ペイン (人間駆動の窓口) は bookkeeping 専用で、実 adapter pane を
        # 持たない。adapter.kill_pane を呼べば存在しない handle を kill しようと
        # して backend がエラーになるため、窓口自身を閉じる操作は構造的に拒否する。
        if is_logical:
            return _err(
                "[logical_pane] cannot close a human-driven logical pane "
                "(the root secretary is a bookkeeping entry, not an adapter pane)"
            )
        # 最後の 1 pane は閉じない (renga: last_pane)。論理ペイン (窓口) も tab を
        # 非空に保つ実体として数えるが、それは **(a) backend が isolated-session で
        # 窓口を adapter の外に隠し、かつ (b) 閉じる対象が broker 管理 pane** の時に
        # 限る (= 窓口が adapter から見えない backend で、自分が spawn した pane を
        # 閉じる時だけ窓口を +1)。
        #   - isolated-socket backend (tmux, -L claude-org-broker, isolated_session=
        #     True): adapter.list_panes() に窓口が出ないため、子 1 つだけでも窓口を
        #     +1 して [last_pane] 誤判定を防ぐ (= Issue #57 の本丸)。窓口は別 socket
        #     に常駐するので +1 は常に妥当 (stale にならない)。
        #   - global-mux backend (wezterm, cli list, isolated_session=False): 窓口の
        #     実 pane は (在れば) 既に adapter pane として数えられ、無ければ +1 は
        #     stale。どちらでも論理ペインを計上せず実 pane 数のみで [last_pane] を
        #     守る。これで host pane の over-permit (窓口可視時) も、窓口が out-of-band
        #     で消えた後に最後の実 pane を空にする over-permit も両方防ぐ
        #     (Codex review round 2 Major 対応)。なお global-mux では adapter が窓口
        #     を実 pane として見せるため、本来 [last_pane] 誤判定 (Issue #57) 自体が
        #     起きない。
        adapter_isolated = getattr(self.adapter, "isolated_session", False)
        target_managed = target_meta is not None
        count_logical = adapter_isolated and target_managed
        effective_count = len(adapter_panes) + (logical_count if count_logical else 0)
        if effective_count <= 1:
            return _err("[last_pane] cannot close the last pane of the only tab")
        self.adapter.kill_pane(handle)
        # bookkeeping 掃除は自己終了 reap と共通の helper に寄せる (meta pop / token
        # full revoke / delivery cred revoke / delivery state reset / 未配達行破棄)。
        # close は明示 kill 済みなので found に関係なく pane_exited を emit し
        # pane_closed を journal する (reap 経路は pane_reaped で区別する)。
        agent_id, _found = self._cleanup_pane(handle)
        self._emit_event({"type": "pane_exited", "pane_id": handle, "agent_id": agent_id})
        self._journal("pane_closed", pane_id=handle, agent_id=agent_id)
        return _ok({"ok": True, "closed": handle})

    def set_pane_identity(
        self,
        target: str,
        has_name: bool,
        new_name: str | None,
        has_role: bool,
        new_role: str | None,
    ) -> dict:
        """pane の表示 name / role を three-state で更新する (§3.3-5)。

        omit=据置 / null=クリア / str=設定。**auth tier (auth_role) は不変**で、
        ここでは触らない (set_pane_identity 経由の権限昇格を構造的に断つ)。
        name 衝突 (他 pane と同名) は -32602。
        """
        if self.adapter is None:
            return _err("[no_backend] no terminal adapter configured")
        # resolve_target は内部で _lock を取るため lock 外で先に呼ぶ (非再入)。
        # resolve_target が入口 opportunistic reap を兼ねるため、自己終了した managed
        # pane はここで meta が落ち [pane_not_found] になる (死んだ pane に identity を
        # 設定できない = 正しい)。set_pane_identity 自身での明示 reap は不要。
        handle = self.resolve_target(target)
        if handle is None:
            return _err(f"[pane_not_found] no pane for target {target!r}")
        collision: str | None = None
        record: dict | None = None
        with self._lock:
            meta = self._pane_meta.get(str(handle))
            if meta is None:
                return _err(
                    f"[pane_not_found] target {target!r} is not a broker-managed "
                    "pane (identity lives in the broker pane registry)"
                )
            # 衝突検査 (renga: name は tab 内一意)。自分自身 / in-flight 予約も除外せず見る。
            if has_name and new_name is not None:
                taken = new_name in self._reserved_names or any(
                    h != str(handle) and m.get("name") == new_name
                    for h, m in self._pane_meta.items()
                )
                if taken:
                    collision = new_name
            if collision is None:
                if has_name:
                    meta["name"] = new_name
                if has_role:
                    meta["role"] = new_role
                tok = meta.get("token")
                if tok and tok in self._binds:
                    b = self._binds[tok]
                    if has_name:
                        # null クリアは bind 側 name も落とす (旧名で解決され続けない)。
                        b.name = new_name if new_name is not None else ""
                    if has_role:
                        b.role = new_role if new_role is not None else ""
                record = {
                    "id": handle, "name": meta.get("name"),
                    "role": meta.get("role"), "cwd": meta.get("cwd"),
                }
        if collision is not None:
            raise ToolArgError(f"name {collision!r} collides with another pane")
        self._journal("pane_identity_set", pane_id=handle,
                      name=record["name"], role=record["role"])
        return _ok(record)

    # ---------------------------------------------------------- pane: spawn
    def _gen_agent_id(self, prefix: str) -> str:
        return f"{prefix}-{next(self._pane_counter)}"

    def _register_pane(
        self, handle: "PaneId", agent_id: str, name: str | None,
        role: str | None, cwd: str | None, kind: str | None, token: str | None,
        terminal_id: "PaneId | None" = None,
    ) -> None:
        now = time.time()
        with self._lock:
            self._pane_meta[str(handle)] = {
                "handle": handle, "agent_id": agent_id, "name": name,
                "role": role, "cwd": cwd, "kind": kind, "token": token,
                # workspace 非依存 liveness (Fix-D) の id 再利用ガードに使う backend の
                # 安定プロセス identity (Herdr terminal_id)。pane_id は移送/再利用で
                # 変わりうるが terminal_id はプロセスに紐づき不変。持たない backend
                # (tmux/wezterm) は None (ガードは効かないが従来経路は不変)。
                "terminal_id": terminal_id,
                # 決定的 reap モデルの pane 単位 liveness (真因B)。spawn 時刻・最終
                # 目撃時刻・連続欠落の起点/回数。_reap_stale_managed_panes が snapshot
                # ごとに更新し、age + 連続欠落の閾値超過でのみ reap 対象にする。
                "spawned_at": now, "last_seen_at": now,
                "missing_since": None, "missing_count": 0,
            }
            self._reserved_names.discard(name)  # 予約を確定 meta へ昇格

    def register_logical_pane(self, token: str) -> dict:
        """human 駆動の root pane (窓口/secretary) を pane 登録簿に first-class な
        論理ペインとして載せる (Issue #57 の bootstrap gap 解消)。

        窓口は人間が直接駆動するため、broker が send_keys/spawn で『駆動』する実
        adapter pane を持たない。だが pane 登録簿にも close_pane の last-pane
        カウントにも乗らないと、窓口が子を 1 つ spawn した瞬間その子が『唯一の
        pane』と誤判定され、close_pane が [last_pane] で子を閉じられない
        (= Issue #57)。さらに list_panes にも窓口が出ず balanced-split の
        アンカーも無い。

        本メソッドは ``bind.pane_id`` を **None のまま据え置く** (= :meth:
        `_trigger_nudge` が pane_id None で early-return するため、人間駆動の窓口
        に PTY ナッジが注入されない。人間は check_messages で読む) ことで実ペイン
        駆動を構造的に避けつつ、pane 登録簿 (``_pane_meta``) に ``logical=True``
        entry として載せる。これで (1) list_panes に窓口が first-class entry と
        して現れ、(2) close_pane の last-pane カウントに数えられ子を誤判定なく
        閉じられる。

        handle は ``bind.name`` (非数字 str) を充てる: 実 adapter handle
        (WezTerm=int / tmux="%N") と衝突せず、``resolve_target`` の既存 name
        ブランチでそのまま解決できる (セキュリティ重要な共有解決関数を変更せずに
        id/name 両系統で addressable にする)。
        """
        bind = self.get_bind(token)
        if bind is None:
            raise ValueError("cannot register logical pane: unknown or revoked token")
        handle = bind.name or bind.agent_id
        with self._lock:
            self._pane_meta[str(handle)] = {
                "handle": handle, "agent_id": bind.agent_id, "name": bind.name,
                "role": bind.role, "cwd": bind.cwd, "kind": bind.kind,
                "token": token, "logical": True,
            }
        self._journal("logical_pane_registered", agent_id=bind.agent_id, pane_id=handle)
        return {
            "id": handle, "agent_id": bind.agent_id, "name": bind.name,
            "role": bind.role, "logical": True,
        }

    def _reserve_name(self, name: str | None) -> str | None:
        """name を予約する (collision なら error 文字列)。spawn の I/O をまたいだ
        TOCTOU で重複 name が通るのを防ぐ (in-flight 予約を含めて検査)。"""
        if name is None:
            return None
        # 予約前に自己終了 managed pane を reap する: これをしないと spawn 直後の
        # issue_token(unique=True) が (reap 前の) 未 revoke bind を見て再び
        # [name_taken] を返し、幽霊 binding で同名 re-spawn が永久に塞がる (本 Issue
        # の中核。resolve_target 経由の split target 解決でも既に reap 済みだが、
        # name=None spawn 等で resolve を経ない経路の保険として入口で明示 reap する)。
        self._reap_stale_managed_panes()
        now = time.time()
        with self._lock:
            # 同名連続 spawn の burst dampener (Issue #109 真因D)。window 外を prune
            # した上で、受理済み spawn が threshold 以上なら次を拒否する。false-reap
            # ループ自体は真因B 修正で断つが、launcher リトライ x reap の相互増幅で
            # 同名 pane を短時間量産する経路への追加防御 (バックオフ本体は launcher
            # 側責務、Issue #109 に memo)。既定は緩めで通常 spawn は素通りする。
            #
            # まず全 name の window 外 timestamp を掃除し、空になった entry を落とす。
            # これをしないと一度きり spawn された uniq name の entry が永久に残り、
            # 長命 broker で _spawn_history が単調増加する (adversarial review Minor)。
            # 掃除対象は小さい dict (最近 spawn した name 群) なので毎回でも軽い。
            for k in list(self._spawn_history):
                kept = [t for t in self._spawn_history[k] if now - t < self.respawn_burst_window]
                if kept:
                    self._spawn_history[k] = kept
                else:
                    del self._spawn_history[k]
            hist = self._spawn_history.get(name, [])
            if len(hist) >= self.respawn_burst_threshold:
                return (
                    f"[respawn_flood] pane name {name!r} spawned "
                    f"{len(hist)} times within {self.respawn_burst_window:g}s "
                    "(retry storm dampener; back off before respawning)"
                )
            taken = name in self._reserved_names or any(
                m.get("name") == name for m in self._pane_meta.values()
            )
            if taken:
                return f"[name_taken] pane name {name!r} already in use"
            self._reserved_names.add(name)
            # 受理を burst 履歴に記録 (空 entry は上の sweep で落ちるため非空で保存)。
            self._spawn_history[name] = hist + [now]
        return None

    def _release_name(self, name: str | None) -> None:
        """予約失敗/spawn 失敗のロールバック。予約解放に加え、_reserve_name が
        受理時に積んだ burst 履歴の直近 1 件も戻す (Codex P3)。

        _release_name は失敗経路 (issue_token 衝突 / adapter.spawn 例外等) のみから
        呼ばれる。成功時は _register_pane が予約を discard し burst 記録は残す (実際に
        pane が立った spawn だけを数える)。同名予約は _reserved_names で直列化される
        ため、この name の burst 履歴末尾は必ず本 in-flight 予約が積んだ timestamp で
        あり、pop で正しく取り消せる (失敗した予約を respawn_flood に数えない)。"""
        if name is None:
            return
        with self._lock:
            self._reserved_names.discard(name)
            hist = self._spawn_history.get(name)
            if hist:
                hist.pop()  # 直近 (この失敗した予約) の記録を戻す
                if not hist:
                    del self._spawn_history[name]

    def _revoke_token(self, token: str | None) -> None:
        """spawn 途中失敗時の発行済み token の掃除 (部分 spawn のロールバック)。

        ``adapter.spawn`` 失敗時、token は既に発行済みだが pane に bind されない。
        未登録のまま放置すると benign だが残存するため revoke して掃除する。"""
        if not token:
            return
        with self._lock:
            b = self._binds.get(token)
            if b is not None:
                b.revoked = True

    def _resolve_split_target(self, target: str) -> tuple["PaneId | None", dict | None]:
        """spawn 対象 pane を解決・検証する (Major 対応)。

        renga は target pane を split する契約。adapter は方向 split を持たない
        (§4.7 Phase 4) ため実 spawn は new window になるが、**明示 target の
        誤指定は検出する**: 解決不能かつ 'focused' 既定でなければ pane_not_found。
        'focused' 既定は broker が caller pane を把握していない場合があるため
        best-effort で通す。返り値は (resolved_handle, error_result)。
        """
        handle = self.resolve_target(target)
        if handle is None and target != "focused":
            return None, _err(f"[pane_not_found] no pane for split target {target!r}")
        return handle, None

    def _adapter_spawn(
        self, argv: list[str], cwd: str | None,
        role: str | None, project: str | None,
        kind: str | None = None,
    ):
        """adapter.spawn を backend の能力に応じて呼ぶ (Issue #110 §6.2 Layer C)。

        ``supports_space_layout`` な backend (Herdr) には role/project から算出した
        :class:`SpaceDescriptor` を ``space=`` で渡し、持たない backend (tmux/wezterm) には
        ``space`` を渡さず従来の flat spawn を呼ぶ (完全不変)。分岐は ``getattr`` で読む
        (``isolated_session`` 等と同じ能力フラグ規約)。

        全 spawn 経路 (claude / codex / generic) で pane プロセスへ
        ``ORG_BROKER_STATE_DIR`` (この daemon の state dir 絶対パス) を注入する
        (Issue #122)。pane 内で走る CLI subprocess (例 ``broker send`` を叩く ja
        ``peer_notify``) が、非既定 ``--state-dir`` で起動された daemon の queue を
        発見できるようにするため。値は daemon 自身の state dir なので backend を問わず
        単一の出所から与える。

        さらに workspace virtualenv を pane に継承させる (Issue #130)。pane cwd/.venv
        優先・無ければ root_cwd/.venv フォールバックで ``.venv`` を探し、見つかれば
        ``VIRTUAL_ENV`` を env dict に足し、``PATH`` は POSIX では argv を
        post-profile login-shell wrapper に包んで prepend する
        (:func:`~claude_org_runtime.terminal.base.venv_pane_prep`)。``.venv`` が無ければ
        完全 no-op (argv/env 不変)。PATH を env dict に直に載せないのは、login shell の
        profile 初期化が ``-e`` 相当で渡した PATH を後から再構築して ``.venv/bin`` を
        消すため (Blocker 2)。

        ``venv_path_via_pane_env`` を宣言する backend (Herdr, Issue #151) では argv を
        **書き換えず** ``VIRTUAL_ENV`` のみ渡す。herdr 0.7.5 の ``agent.start`` は
        ``argv`` を受け取らなくなり login-shell wrapper の運搬経路が消えたため、
        ``PATH`` prepend は adapter が pane 生成後 (profile 初期化完了後) に打ち込む。

        ``kind`` (Issue #151) は broker が知っている意味的な種別
        (``"claude"`` / ``"codex"`` / generic は None)。``supports_agent_kind`` な
        backend にのみ渡す。**argv[0] からの推測はしない** (venv wrapper 経路では
        argv[0] がシェルに、generic spawn では任意コマンドになり破綻するため)。
        """
        env = {"ORG_BROKER_STATE_DIR": sidecar.absolutize(self.state_dir)}
        if getattr(self.adapter, "venv_path_via_pane_env", False):
            env.update(venv_pane_env(cwd, self.root_cwd))
        else:
            argv, venv_env = venv_pane_prep(argv, cwd, self.root_cwd)
            env.update(venv_env)
        extra: dict = {}
        if getattr(self.adapter, "supports_agent_kind", False):
            extra["kind"] = kind
        if getattr(self.adapter, "supports_space_layout", False):
            space = surface.space_descriptor_for(role, project)
            return self.adapter.spawn(
                argv, cwd=cwd, new_window=True, space=space, env=env, **extra
            )
        return self.adapter.spawn(argv, cwd=cwd, new_window=True, env=env, **extra)

    def spawn_claude(
        self, caller: AgentBind, direction: str, target: str, name: str | None,
        role: str | None, model: str | None, permission_mode: str | None,
        extra: list[str], cwd: str | None, project: str | None = None,
    ) -> dict:
        """spawn_claude_pane: 対話 TUI claude を broker MCP 接続で起動する。

        agent_id は name から導出 (無ければ生成)。argv は broker が構造化ビルダーで
        組み (default-deny guard 込み)、--mcp-config で token を注入する。子 token の
        権限 tier (auth_role) は表示 role の自己申告ではなく **caller tier で上限を
        切った** tier にする (Blocker: spawn 時 tier 昇格の阻止)。adapter は方向
        split を持たない (§4.7 Phase 4) ため direction / target は受理して
        記録・検証し、実 spawn は adapter.spawn (new window) で行う (本段の既知挙動)。
        """
        if self.adapter is None:
            return _err("[no_backend] no terminal adapter configured")
        split_handle, terr = self._resolve_split_target(target)
        if terr is not None:
            return terr
        # token 発行前に caller 由来 argv を pre-validate (orphan token を作らない)。
        surface.build_claude_argv(
            mcp_config_json="{}", model=model,
            permission_mode=permission_mode, extra_args=extra,
        )
        if (err := self._reserve_name(name)) is not None:
            return _err(err)
        token: str | None = None  # generic spawn では None のまま
        delivery_cred: str | None = None  # channel sidecar 用 (§9.4)。失敗時 revoke
        try:
            auth_role = surface.capped_auth_role(role, caller.auth_role)
            agent_id = name or self._gen_agent_id("claude")
            try:
                token = self.issue_token(
                    agent_id, name or agent_id, role or "", cwd=cwd, kind="claude",
                    auth_role=auth_role, unique=True,
                )
            except ValueError as e:
                # agent_id/name が既存 active bind と衝突する場合は **delivery cred を
                # 発行する前に** 原子的に拒否する。queue / delivery 解決は agent_id を
                # キーにするため、衝突した子の channel sidecar が被害 agent の queue を
                # claim->confirm して横取り + 沈黙喪失する (cross-agent 配送横取り)。
                # _reserve_name は _pane_meta 名前空間しか見ず、pane を持たない
                # bind-only agent (admin_mint_token で mint された secretary/dispatcher
                # 等) を取りこぼすため、admin_mint_token と同じ unique=True 防御を
                # spawn 経路へ拡張する。予約名は解放してから返す。
                self._release_name(name)
                return _err(str(e))
            # push 一次配送の spawn 儀式 (§9.5): full token の --mcp-config (daemon)
            # に加えて、(a) channel sidecar を stdio MCP として同 config に積み、
            # (b) delivery-scoped credential を sidecar env に注入し、(c) dev-channel
            # flag で sidecar を load させる。delivery cred は full token とは別物
            # (least-privilege)。子 claude が sidecar を subprocess として起こす
            # ため broker は sidecar を直接 spawn しない (pane kill で道連れに落ちる)。
            delivery_cred = self.issue_delivery_cred(agent_id)
            mcp_config = self.mcp_config_for(token)
            mcp_config["mcpServers"]["org-broker-channel"] = (
                self.channel_server_config(delivery_cred, agent_id)
            )
            argv = surface.build_claude_argv(
                mcp_config_json=json.dumps(mcp_config),
                model=model, permission_mode=permission_mode, extra_args=extra,
                channel_server="org-broker-channel",
            )
            ref = self._adapter_spawn(argv, cwd, role, project, kind="claude")
        except BaseException:
            # 失敗時のみ予約を解放し、発行済み token / delivery cred があれば掃除する。
            # 成功時は予約を保持したまま _register_pane が _lock 下で meta 登録と予約
            # discard を原子的に行う (spawn 成功後〜meta 登録前に同名 spawn が
            # 再予約できる窓を作らない)。token は generic spawn では None。
            self._release_name(name)
            self._revoke_token(token)
            self._revoke_token(delivery_cred)
            raise
        self.bind_pane(token, ref.pane_id)
        self._register_pane(ref.pane_id, agent_id, name, role, cwd, "claude", token,
                            terminal_id=getattr(ref, "terminal_id", None))
        self._emit_event({
            "type": "pane_started", "pane_id": ref.pane_id, "agent_id": agent_id,
        })
        self._journal("pane_spawned", kind="claude", agent_id=agent_id,
                      pane_id=ref.pane_id)
        return _ok({
            "id": ref.pane_id, "agent_id": agent_id, "name": name, "role": role,
            "direction": direction, "split_target": split_handle, "cwd": cwd,
        })

    def spawn_codex(
        self, caller: AgentBind, direction: str, target: str, name: str | None,
        role: str | None, extra: list[str], cwd: str | None,
        project: str | None = None,
    ) -> dict:
        """spawn_codex_pane: 対話 TUI codex を起動する (§3.3-6)。

        課金中立 default-deny guard で対話 TUI に構造的限定する (exec / review /
        *-server 等の非対話サブコマンドを拒否)。子 token の auth_role は caller tier
        上限で切る (Blocker 対応)。codex の MCP 自動登録は renga の
        RENGA_PEER_CLIENT_KIND env 注入に相当する env を adapter.spawn が持たない
        ため本段では行わない (token は bind/帰属簿のため発行・記録のみ。env 注入は
        Phase 4 / full backend adapter。既知制限)。
        """
        if self.adapter is None:
            return _err("[no_backend] no terminal adapter configured")
        split_handle, terr = self._resolve_split_target(target)
        if terr is not None:
            return terr
        # token 発行前に default-deny guard を通す (orphan token を作らない)。
        argv = surface.build_codex_argv(extra_args=extra)
        if (err := self._reserve_name(name)) is not None:
            return _err(err)
        token: str | None = None  # generic spawn では None のまま
        try:
            auth_role = surface.capped_auth_role(role, caller.auth_role)
            agent_id = name or self._gen_agent_id("codex")
            try:
                token = self.issue_token(
                    agent_id, name or agent_id, role or "", cwd=cwd, kind="codex",
                    auth_role=auth_role, unique=True,
                )
            except ValueError as e:
                # spawn_claude と同じ agent_id 衝突防御 (unique=True)。codex は channel
                # sidecar を持たない (pull peer) が、agent_id 共有は check_messages 経由の
                # queue 共有・誤配送 (spike broker.py L458 のハザード) を招くため同様に
                # 原子的に拒否する。予約名は解放してから返す。
                self._release_name(name)
                return _err(str(e))
            ref = self._adapter_spawn(argv, cwd, role, project, kind="codex")
        except BaseException:
            # 失敗時のみ予約を解放し、発行済み token があれば掃除する。成功時は
            # 予約を保持したまま _register_pane が _lock 下で meta 登録と予約
            # discard を原子的に行う (spawn 成功後〜meta 登録前に同名 spawn が
            # 再予約できる窓を作らない)。token は generic spawn では None。
            self._release_name(name)
            self._revoke_token(token)
            raise
        self.bind_pane(token, ref.pane_id)
        self._register_pane(ref.pane_id, agent_id, name, role, cwd, "codex", token,
                            terminal_id=getattr(ref, "terminal_id", None))
        self._emit_event({
            "type": "pane_started", "pane_id": ref.pane_id, "agent_id": agent_id,
        })
        self._journal("pane_spawned", kind="codex", agent_id=agent_id,
                      pane_id=ref.pane_id)
        return _ok({
            "id": ref.pane_id, "agent_id": agent_id, "name": name, "role": role,
            "direction": direction, "split_target": split_handle, "cwd": cwd,
        })

    def spawn_generic(
        self, direction: str, target: str, name: str | None, role: str | None,
        command: str | None, cwd: str | None, project: str | None = None,
    ) -> dict:
        """spawn_pane (generic, secretary tier): 任意コマンドを起動する。

        token を注入しない非 org spawn 経路 (attention watcher 用, §3.3-3)。
        bind は作らない (peer にならない・tier を持たない) が、name/role/cwd は
        pane 登録簿に残す (list_panes に出すため)。command 無しは shell のみ起動。
        """
        if self.adapter is None:
            return _err("[no_backend] no terminal adapter configured")
        split_handle, terr = self._resolve_split_target(target)
        if terr is not None:
            return terr
        if (err := self._reserve_name(name)) is not None:
            return _err(err)
        token: str | None = None  # generic spawn では None のまま
        try:
            argv = ["sh", "-c", command] if command else ["sh"]
            ref = self._adapter_spawn(argv, cwd, role, project, kind=None)  # generic: agent ではない
        except BaseException:
            # 失敗時のみ予約を解放し、発行済み token があれば掃除する。成功時は
            # 予約を保持したまま _register_pane が _lock 下で meta 登録と予約
            # discard を原子的に行う (spawn 成功後〜meta 登録前に同名 spawn が
            # 再予約できる窓を作らない)。token は generic spawn では None。
            self._release_name(name)
            self._revoke_token(token)
            raise
        agent_id = name or self._gen_agent_id("pane")
        self._register_pane(ref.pane_id, agent_id, name, role, cwd, None, None,
                            terminal_id=getattr(ref, "terminal_id", None))
        self._emit_event({"type": "pane_started", "pane_id": ref.pane_id})
        self._journal("pane_spawned", kind="generic", pane_id=ref.pane_id)
        return _ok({
            "id": ref.pane_id, "name": name, "role": role,
            "direction": direction, "split_target": split_handle, "cwd": cwd,
        })

    # ---------------------------------------------------------- pane: events
    def _emit_event(self, ev: dict) -> None:
        ev = {"ts": time.time(), **ev}
        with self._events_cv:
            self._events.append(ev)
            self._events_cv.notify_all()

    def poll_events(
        self, since: str | None, timeout_ms: int, types: list[str] | None
    ) -> dict:
        """cursor-based long-poll (renga poll_events 同形)。

        初回 (since 省略) は「今以降」から開始 (履歴 replay なし)。新規イベントが
        来るまで最大 timeout_ms (30000 cap) ブロックする。types フィルタは返却を
        絞るが long-poll は延長しない (非該当イベントで早期 return + cursor 前進)。
        """
        cap_ms = min(max(timeout_ms, 0), 30000)
        deadline = time.monotonic() + cap_ms / 1000.0
        with self._events_cv:
            if since is None:
                cursor = len(self._events)
            else:
                try:
                    cursor = int(since)
                except (TypeError, ValueError):
                    cursor = 0
                cursor = max(0, min(cursor, len(self._events)))
            while len(self._events) == cursor:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._events_cv.wait(remaining)
            new = self._events[cursor:]
            end = len(self._events)
        if types:
            tset = set(types)
            evs = [e for e in new if e.get("type") in tset]
        else:
            evs = list(new)
        return {"next_since": str(end), "events": evs}


def _ok(result: dict) -> dict:
    """tools/call 成功結果 (JSON テキスト 1 content)。"""
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}


def _err(text: str) -> dict:
    """tools/call エラー結果 (isError)。renga の構造化エラーコードに倣う。"""
    return {"content": [{"type": "text", "text": text}], "isError": True}


def _tool_name_of(params: object) -> str:
    """tools/call params から tool 名を **決して例外を出さずに** 取り出す。

    ``params`` は生のリクエスト由来なので dict とは限らない (``"params": "x"`` の
    ような不正な JSON-RPC でも到達する)。診断経路の中で ``AttributeError`` を出すと、
    C-1 が防ごうとしている当のもの (応答を書かないままの socket close) を診断コード
    自身が再現してしまう。
    """
    if isinstance(params, dict):
        name = params.get("name")
        if isinstance(name, str) and name:
            return name
    return "?"


def _tool_error_message(params: object, exc: BaseException) -> str:
    """想定外例外を **診断可能な 1 行**へ落とす (Issue #151 C-1)。

    tool 名と例外クラス名 + str を載せる。``HerdrError`` 等の構造化例外は str が
    既に ``[code] message`` 形なので、そのまま原因コード (``adapter_unavailable`` /
    ``invalid-params`` 等) がクライアント側に出る。

    **引数は載せない**: tools/call の arguments には token / cred 等の秘匿値が
    載りうるため (scrub-policy)。詳細な traceback は journal 側 (daemon ローカル)
    にのみ残す (:meth:`_McpHandler._journal_tool_failure`)。
    """
    name = _tool_name_of(params)
    return f"[tool_failed] {name}: {type(exc).__name__}: {exc}"


class _McpHandler(BaseHTTPRequestHandler):
    """MCP streamable-HTTP (JSON-RPC over POST, application/json 応答)。"""

    broker: Broker  # start() 時に注入
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 標準 stderr ログ抑止
        pass

    def _journal_tool_failure(
        self, bind: AgentBind, params: object, exc: BaseException
    ) -> None:
        """tools/call の想定外例外を journal へ残す (Issue #151 C-1 の事後診断面)。

        クライアントへ返す 1 行 (:func:`_tool_error_message`) では原因の特定に
        足りないため、traceback を daemon ローカルの queue.jsonl にのみ残す。
        journal 書込み自体の失敗 (disk full 等) で例外が再び do_POST を貫通しては
        本末転倒なので、ここは握り潰す。
        """
        try:
            self.broker._journal(
                "tool_call_failed",
                agent_id=bind.agent_id,
                tool=_tool_name_of(params),
                error=f"{type(exc).__name__}: {exc}",
                traceback="".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)
                ),
            )
        except Exception:  # noqa: BLE001 - 診断のための best-effort
            pass

    def _send_json(self, status: int, payload: dict | None, session_id: str | None = None):
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        if body:
            self.send_header("Content-Type", "application/json")
        if session_id:
            self.send_header("Mcp-Session-Id", session_id)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):  # SSE ストリームは提供しない (POST 応答のみで完結)
        self._send_json(405, None)

    def do_DELETE(self):
        """セッション終了: 当該 bind の session を失効させる。

        POST 側と対称に、session 不一致 / 欠落は 404 で拒否する
        (codex review round 2 Major 対応)。_journal はロック外で呼ぶ
        (非再入 Lock の二重取得デッドロック回避。同 round Blocker 対応)。
        """
        auth = self.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
        bind = self.broker.get_bind(token)
        if bind is None:
            self._send_json(401, None)
            return
        sid = self.headers.get("Mcp-Session-Id")
        closed = False
        with self.broker._lock:
            if bind.session_id is not None and sid == bind.session_id:
                bind.session_id = None
                # 登録も落とす: 切断済み client を list_peers / 配送先に
                # 残さない (codex review round 3 Major 対応)
                bind.registered = False
                closed = True
        if not closed:
            self._send_json(404, None)
            return
        self.broker._journal("session_closed", agent_id=bind.agent_id)
        self._send_json(200, None)

    def _handle_admin(self):
        """admin HTTP RPC (token mint / graceful shutdown)。

        per-agent bearer token (bind 表) とは別系統で、``broker.admin_token`` の
        bearer を要求する (定数時間比較)。admin token 未設定なら経路ごと隠す (404)。
        body は ``{"method": ..., "params": {...}}`` の小さな JSON-RPC 風。
        Codex review: admin 経路は認証付き / シグナル非依存の HTTP RPC。
        """
        broker = self.broker
        # admin 面が無効 (serve が admin token を設定していない / 内部テスト用
        # broker)。経路の存在自体を隠すため 404。
        if broker.admin_token is None:
            self._send_json(404, None)
            return
        auth = self.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
        # 定数時間比較 (token 長/内容のタイミングリークを避ける)。空 token も弾く。
        if not token or not hmac.compare_digest(token, broker.admin_token):
            self._send_json(
                401, {"ok": False, "error": "[admin_unauthorized] invalid admin token"}
            )
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"ok": False, "error": "[parse_error] invalid json body"})
            return
        method = req.get("method", "")
        params = req.get("params") or {}
        if not isinstance(params, dict):
            self._send_json(400, {"ok": False, "error": "[invalid_params] params must be object"})
            return
        if method == "mint_token":
            result = broker.admin_mint_token(params)
            self._send_json(200 if result.get("ok") else 400, result)
        elif method == "flip_mode":
            # per-agent delivery_mode の PUSH<->PULL flip (§9.3 mode-epoch fencing)。
            # owner (= 対象 agent_id) と mode を取り、横断状態を触るため admin scope。
            owner = params.get("owner")
            mode = params.get("mode")
            if not isinstance(owner, str) or not isinstance(mode, str):
                self._send_json(400, {
                    "ok": False,
                    "error": "[invalid_params] flip_mode requires string owner and mode",
                })
            else:
                result = broker.flip_mode(owner, mode)
                self._send_json(200 if result.get("ok") else 400, result)
        elif method == "delivery_dump":
            # 配送ライフサイクルの横断スナップショット (owner/state を晒すため admin)。
            self._send_json(200, {"ok": True, **broker.delivery_dump()})
        elif method == "shutdown":
            # 応答を先に返してから shutdown を要求する: クライアントは ack を受け
            # 取れ、実際の停止 (server.shutdown + sidecar 削除) は run() 側が行う
            # (ハンドラスレッドから ThreadingHTTPServer.shutdown を直接呼ぶ
            # デッドロックを避ける)。
            self._send_json(200, {"ok": True, "shutting_down": True})
            broker.request_shutdown()
        else:
            self._send_json(
                400, {"ok": False, "error": f"[unknown_admin_method] {method!r}"}
            )

    def _handle_delivery(self, path: str):
        """delivery endpoint (``/claim-owner`` / ``/poll-claims`` / ``/confirm-delivered``)。

        §9.3 / §9.4 / Issue #125。channel sidecar 専用。**delivery-scoped credential**
        (``scope == "delivery"``) の bearer のみ受理し、operate できるのは
        ``to_id == bind.agent_id (= owner)`` の行に限る (store 側が owner で構造的に絞る)。
        full token (agent / admin) はこの経路を使えない (least-privilege の双方向遮断)。
        body: ``{"instance_id"}`` (claim-owner) / ``{"generation", "instance_id"}``
        (poll) / ``{"id", "epoch", "generation", "instance_id"}`` (confirm)。素の HTTP RPC。

        owner は store 側が token から **_lock 下で**再解決+検証する (revoke を
        register/claim/confirm に対する原子的 fence にするため owner ではなく token を渡す。
        ここでの get_bind は早期 401 の cheap gate)。``generation`` / ``instance_id`` は
        session-scoped fencing 値 (Issue #125 Blocker #1): register 応答の generation を
        sidecar が poll/confirm に載せ、daemon は現世代のみ許可する。
        """
        broker = self.broker
        auth = self.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
        bind = broker.get_bind(token)
        if bind is None or bind.scope != "delivery":
            self._send_json(401, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"error": "[parse_error] invalid json body"})
            return
        if not isinstance(req, dict):
            self._send_json(400, {"error": "[invalid_body] body must be a json object"})
            return
        instance_id = req.get("instance_id")
        if not isinstance(instance_id, str) or not instance_id:
            self._send_json(400, {"ok": False, "error": "[invalid_instance] "
                                  "instance_id must be a non-empty string"})
            return
        if path == "/claim-owner":
            # sidecar register: owner の delivery generation を +1 し現世代に登録する。
            # Issue #129: observer 秘密 (非 replay 信号、Phase 2) と bg_hosted marker
            # (Phase 1) を任意で受ける。observer は文字列、bg_hosted は厳密に bool の時
            # のみ True 扱い (truthy 文字列/数値で suppress が誤発火しないよう防御)。
            observer = req.get("observer")
            if observer is not None and not isinstance(observer, str):
                self._send_json(400, {"ok": False, "error": "[invalid_observer] "
                                      "observer must be a string"})
                return
            bg_hosted = req.get("bg_hosted", False)
            if not isinstance(bg_hosted, bool):
                self._send_json(400, {"ok": False, "error": "[invalid_bg_hosted] "
                                      "bg_hosted must be a boolean"})
                return
            self._send_json(200, broker.register_delivery_instance(
                token, instance_id, observer=observer, bg_hosted=bg_hosted))
            return
        # poll / confirm は generation を必須にする (fence の根拠)。
        try:
            generation = int(req["generation"])
        except (KeyError, TypeError, ValueError):
            self._send_json(400, {"ok": False, "error": "[invalid_generation] "
                                  "generation must be an int"})
            return
        if path == "/poll-claims":
            self._send_json(200, broker.poll_claims(token, generation, instance_id))
            return
        # /confirm-delivered
        rid = req.get("id")
        if not isinstance(rid, str):
            self._send_json(400, {"ok": False, "error": "[invalid_id] id must be a string"})
            return
        try:
            epoch = int(req.get("epoch", -1))
        except (TypeError, ValueError):
            self._send_json(400, {"ok": False, "error": "[invalid_epoch] epoch must be an int"})
            return
        self._send_json(200, broker.confirm_delivered(
            token, rid, epoch, generation, instance_id))

    def do_POST(self):
        path = self.path.rstrip("/")
        if path == "/admin":
            self._handle_admin()
            return
        if path in ("/claim-owner", "/poll-claims", "/confirm-delivered"):
            self._handle_delivery(path)
            return
        if path != "/mcp":
            self._send_json(404, None)
            return
        # --- 認証 (per-agent token, 設計書 §4.4) -------------------------
        auth = self.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
        bind = self.broker.get_bind(token)
        if bind is None:
            self._send_json(
                401,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32001, "message": "[token_invalid] unauthorized"},
                },
            )
            return
        # delivery-scoped credential は MCP ツール面を**構造的に**持たない (§9.4
        # least-privilege)。/mcp (initialize / tools/*) は full scope のみ。delivery
        # cred は /poll-claims / /confirm-delivered からのみ daemon を操作できる。
        if bind.scope != "full":
            self._send_json(
                403,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32001,
                        "message": "[scope_forbidden] delivery-scoped credential "
                        "cannot use the MCP tool surface",
                    },
                },
            )
            return

        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(
                400,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "parse error"},
                },
            )
            return

        method = req.get("method", "")
        req_id = req.get("id")

        # --- セッション検証 (initialize 以外は Mcp-Session-Id 必須) -------
        # codex review Major 対応: bearer token のみで操作可能だと
        # initialize 前 / DELETE 後の stale client を排除できない。
        # 不一致は 404 (MCP spec: クライアントは再 initialize する)。
        if method != "initialize":
            sid = self.headers.get("Mcp-Session-Id")
            with self.broker._lock:
                expected = bind.session_id
            if expected is None or sid != expected:
                self._send_json(
                    404,
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {
                            "code": -32001,
                            "message": "[session_invalid] initialize first",
                        },
                    },
                )
                return

        # --- notification (id なし) は 202 で受理 ------------------------
        if req_id is None:
            if method == "notifications/initialized":
                pass  # 登録自体は initialize 時に済んでいる
            self._send_json(202, None)
            return

        if method == "initialize":
            client_pv = (req.get("params") or {}).get("protocolVersion", "")
            pv = client_pv if client_pv in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            session_id = secrets.token_hex(16)
            with self.broker._lock:
                bind.registered = True
                bind.registered_at = time.time()
                bind.session_id = session_id
            self.broker._journal(
                "agent_registered", agent_id=bind.agent_id, role=bind.role
            )
            self._send_json(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": pv,
                        "capabilities": {"tools": {}},
                        "serverInfo": SERVER_INFO,
                    },
                },
                session_id=session_id,
            )
        elif method == "tools/list":
            # tier-scoped catalogue (§4.2): 公開面は bind の不変 auth_role で
            # 構造的に絞る (worker/curator=messaging / dispatcher=+pane操作 /
            # secretary=+generic spawn_pane)。
            self._send_json(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"tools": surface.tools_for(bind.auth_role)},
                },
            )
        elif method == "tools/call":
            params = req.get("params") or {}
            try:
                result = self.broker.call_tool(
                    bind, params.get("name", ""), params.get("arguments") or {}
                )
            except ToolArgError as e:
                self._send_json(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32602, "message": f"invalid params: {e}"},
                    },
                )
                return
            except Exception as e:  # noqa: BLE001 - 意図的な最後の関門
                # 想定外例外を **必ず JSON-RPC error として返す** (Issue #151 C-1)。
                # これが無いと例外が do_POST を貫通してハンドラスレッドが応答を
                # 書かないまま終了し、クライアントには「The socket connection was
                # closed unexpectedly」しか届かない = 無診断 (Issue #151 の主因:
                # herdr adapter の agent.start が protocol 不一致で HerdrError を
                # 投げた時、spawn_claude の re-raise がここを素通りしていた)。
                #
                # ``BaseException`` は捕らない: KeyboardInterrupt / SystemExit を
                # 握り潰すと daemon の停止経路を壊す。
                self._journal_tool_failure(bind, params, e)
                self._send_json(
                    200,
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {
                            "code": -32603,
                            "message": _tool_error_message(params, e),
                        },
                    },
                )
                return
            self._send_json(
                200, {"jsonrpc": "2.0", "id": req_id, "result": result}
            )
        elif method == "ping":
            self._send_json(200, {"jsonrpc": "2.0", "id": req_id, "result": {}})
        else:
            self._send_json(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"method not found: {method}"},
                },
            )
