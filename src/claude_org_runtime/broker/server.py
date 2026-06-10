# -*- coding: utf-8 -*-
"""org-broker サーバー本体 (orchestrator)。

設計 SoT: docs/design/renga-decoupling.md §4 (broker/adapter 設計)・§4.3
(ナッジ配達)。canonical 実装: claude-org-transport-lab spike/broker.py
(Phase 4/5 で確定した MCP surface + allowlist guard + session 検証) の
faithful port。

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

import itertools
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..terminal import (
    NUDGE_TEXT,
    PaneId,
    TerminalAdapter,
    classify_pane_state,
)
from . import surface
from .store import StoreMixin
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
    ):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.adapter = adapter
        self.host = host
        self.port = port
        self.nudge_defer_interval = nudge_defer_interval
        self.nudge_defer_max_tries = nudge_defer_max_tries

        self._lock = threading.Lock()
        self._binds: dict[str, AgentBind] = {}        # token -> bind
        self._queues: dict[str, list[dict]] = {}      # agent_id -> messages
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

        # poll_events 用 lifecycle イベント ring (cursor = list index)。
        # 専用 Condition を使い、_lock の binds/queues 契約と絡めない。
        self._events: list[dict] = []
        self._events_cv = threading.Condition()

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
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        self._journal("broker_stopped")

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/mcp"

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
        for attempt in range(1, self.nudge_defer_max_tries + 1):
            with self._lock:
                pending = bool(self._queues.get(target.agent_id))
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
        """
        if self.adapter is None:
            return None
        # adapter I/O は lock 外で先に済ませる (lock 下で I/O しない契約)。
        panes = self._adapter_panes()
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
        receive_mode は全 pull 統一の定数 (Set D amendment)。
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

        語彙検証は surface 側 (-32602)。実打鍵は現 adapter 能力 (literal text /
        Enter / Ctrl+C) に限定される。それ以外の有効キー (Tab / Shift+Tab /
        矢印等) は backend adapter が emit 面を持たないため
        ``[key_unsupported]`` を返す (§4.7 Phase 4 で full backend adapter 化。
        既知制限)。
        """
        if self.adapter is None:
            return _err("[no_backend] no terminal adapter configured")
        handle = self.resolve_target(target)
        if handle is None:
            return _err(f"[pane_not_found] no pane for target {target!r}")
        seq = list(keys) + (["enter"] if enter else [])
        # adapter で emit 可能なキーだけを許す。未対応キーがあれば先に弾く
        # (部分実行で画面を壊さない)。
        unsupported = [
            k for k in seq if k.lower() not in ("enter", "return", "ctrl+c")
        ]
        if unsupported:
            return _err(
                f"[key_unsupported] keys {unsupported!r} are not emittable by the "
                "current terminal adapter (only Enter / Ctrl+C / literal text; "
                "full raw-key vocabulary is Phase 4 / full backend adapter)"
            )
        if text:
            self.adapter.type_text(handle, text)
        for k in seq:
            kl = k.lower()
            if kl in ("enter", "return"):
                self.adapter.send_enter(handle)
            elif kl == "ctrl+c":
                self.adapter.send_interrupt(handle)
        return _ok({"ok": True, "target": target})

    def close_pane_target(self, target: str) -> dict:
        """pane を閉じる (renga close_pane 同形)。token を revoke しイベントを emit。"""
        if self.adapter is None:
            return _err("[no_backend] no terminal adapter configured")
        handle = self.resolve_target(target)
        if handle is None:
            return _err(f"[pane_not_found] no pane for target {target!r}")
        # 最後の 1 pane は閉じない (renga: last_pane)。
        if len(self._adapter_panes()) <= 1:
            return _err("[last_pane] cannot close the last pane of the only tab")
        self.adapter.kill_pane(handle)
        # registry の pop と token revoke を 1 ロックスコープで原子的に行う。
        with self._lock:
            meta = self._pane_meta.pop(str(handle), None)
            agent_id = meta.get("agent_id") if meta else None
            tok = meta.get("token") if meta else None
            if tok and tok in self._binds:
                b = self._binds[tok]
                b.revoked = True
                b.registered = False
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
    ) -> None:
        with self._lock:
            self._pane_meta[str(handle)] = {
                "handle": handle, "agent_id": agent_id, "name": name,
                "role": role, "cwd": cwd, "kind": kind, "token": token,
            }
            self._reserved_names.discard(name)  # 予約を確定 meta へ昇格

    def _reserve_name(self, name: str | None) -> str | None:
        """name を予約する (collision なら error 文字列)。spawn の I/O をまたいだ
        TOCTOU で重複 name が通るのを防ぐ (in-flight 予約を含めて検査)。"""
        if name is None:
            return None
        with self._lock:
            taken = name in self._reserved_names or any(
                m.get("name") == name for m in self._pane_meta.values()
            )
            if taken:
                return f"[name_taken] pane name {name!r} already in use"
            self._reserved_names.add(name)
        return None

    def _release_name(self, name: str | None) -> None:
        if name is None:
            return
        with self._lock:
            self._reserved_names.discard(name)

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

    def spawn_claude(
        self, caller: AgentBind, direction: str, target: str, name: str | None,
        role: str | None, model: str | None, permission_mode: str | None,
        extra: list[str], cwd: str | None,
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
        try:
            auth_role = surface.capped_auth_role(role, caller.auth_role)
            agent_id = name or self._gen_agent_id("claude")
            token = self.issue_token(
                agent_id, name or agent_id, role or "", cwd=cwd, kind="claude",
                auth_role=auth_role,
            )
            argv = surface.build_claude_argv(
                mcp_config_json=json.dumps(self.mcp_config_for(token)),
                model=model, permission_mode=permission_mode, extra_args=extra,
            )
            ref = self.adapter.spawn(argv, cwd=cwd, new_window=True)
        finally:
            self._release_name(name)
        self.bind_pane(token, ref.pane_id)
        self._register_pane(ref.pane_id, agent_id, name, role, cwd, "claude", token)
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
        try:
            auth_role = surface.capped_auth_role(role, caller.auth_role)
            agent_id = name or self._gen_agent_id("codex")
            token = self.issue_token(
                agent_id, name or agent_id, role or "", cwd=cwd, kind="codex",
                auth_role=auth_role,
            )
            ref = self.adapter.spawn(argv, cwd=cwd, new_window=True)
        finally:
            self._release_name(name)
        self.bind_pane(token, ref.pane_id)
        self._register_pane(ref.pane_id, agent_id, name, role, cwd, "codex", token)
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
        command: str | None, cwd: str | None,
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
        try:
            argv = ["sh", "-c", command] if command else ["sh"]
            ref = self.adapter.spawn(argv, cwd=cwd, new_window=True)
        finally:
            self._release_name(name)
        agent_id = name or self._gen_agent_id("pane")
        self._register_pane(ref.pane_id, agent_id, name, role, cwd, None, None)
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


class _McpHandler(BaseHTTPRequestHandler):
    """MCP streamable-HTTP (JSON-RPC over POST, application/json 応答)。"""

    broker: Broker  # start() 時に注入
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 標準 stderr ログ抑止
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

    def do_POST(self):
        if self.path.rstrip("/") != "/mcp":
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
