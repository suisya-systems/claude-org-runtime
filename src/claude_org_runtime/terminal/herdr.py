# -*- coding: utf-8 -*-
"""Herdr terminal adapter (第 3 の POSIX backend, minimal surface)。

設計 SoT: claude-org-transport-lab docs/design/herdr-adapter.md (merged PR #29)。
実測裏付け: 同 docs/reports/herdr-socket-spike.md (Herdr 0.7.1 / protocol 14)。
現行 canonical は本モジュール。WezTerm (Phase 1) / tmux (Phase 2) に続く第 3 の
``TerminalAdapter`` 実装で、broker / harness は同一の ``TerminalAdapter`` 面と
``make_adapter()`` ファクトリ経由でのみ Herdr backend に触る。

本タスクのスコープ (実装ガイダンス確定):
- 現行 ``TerminalAdapter`` Protocol (base.py) 準拠の最小面のみ:
  spawn / list_panes / pane_exists / get_text / type_text / send_enter /
  send_line / send_interrupt / kill_pane。
- 接続は **stdlib のみ** (Unix domain socket + newline-delimited JSON)。
- **POSIX / WSL 限定**。Windows named pipe は未対応 = instantiate 時に
  ``adapter_unavailable`` を明示 (設計書 §4.6 / 残存リスク)。

**スコープ外 (follow-up、設計書該当節を参照)**:
- events buffer の cursor / 30s cap / ``events_dropped`` 正規化 (設計書 §4.5)。
  Herdr ``events.subscribe`` は pane_id 必須・サイレントロスあり (spike 窓口補足4)
  のため、採用する場合は per-pane subscribe + polling reconcile が必須。broker
  surface (poll_events) 拡張を伴うため本 adapter では **events 系を一切使わない**
  (最小面は全て one-shot request/response で成立する)。
- full raw-key ``send_keys`` 語彙 (Shift+Tab / 矢印 / Home/End 等、設計書 §4.1)。
  broker surface (send_keys_to) 拡張が要る。本 adapter は Enter / Ctrl+C / literal
  text のみ (現行 broker 能力と同じ)。

分離 (isolated_session=True, 設計書 §3.4 / §4.2):
- 本 adapter は **専用 workspace を 1 つ確保**し、その workspace_id に属する pane
  のみを list / close する。既存 Herdr session の無関係 pane は workspace_id で
  厳格にフィルタして混入させない (spike: pane.list は workspace_id filter を持つ)。
- よって tmux (専用 socket) と同じく ``isolated_session=True``。broker の
  last-pane ガードが論理ペイン (窓口) を +1 計上してよい backend である
  (launcher._backend_is_isolated が ``_BACKEND_ADAPTER_CLASS`` 経由で読む)。

error code (設計書 §3.3 / §4.6): Herdr raw error を**透過せず**、adapter 出口で
Set D 語彙へ写像する (:class:`HerdrError` の ``code``)。socket 到達不能は
``adapter_unavailable`` に分離 (broker/MCP 不通の ``backend_unreachable`` は broker
層の責務で本 adapter は emit しない)。

import 時副作用なし: socket path 解決とバイナリ探索は **instantiate 時** に限定
(dataclass の default_factory / __post_init__)。tmux.py / wezterm.py の慣例に揃える。
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, ClassVar

from .base import NUDGE_TEXT, PaneRef  # noqa: F401  (NUDGE_TEXT 再利用)

# ---------------------------------------------------------------------------
# error code 正規化 (設計書 §3.3 の "adapter 出口コード" 列)
# ---------------------------------------------------------------------------

# adapter 出口コード (Set D 6.1 語彙 + runtime 拡張)。Herdr raw code を透過せず
# これらの正規化コードで :class:`HerdrError` を送出する。
CODE_PANE_NOT_FOUND = "pane_not_found"
CODE_PANE_VANISHED = "pane_vanished"
CODE_SPLIT_REFUSED = "split_refused"
CODE_LAST_PANE = "last_pane"
CODE_CWD_INVALID = "cwd_invalid"
CODE_NAME_IN_USE = "name_in_use"
CODE_INVALID_PARAMS = "invalid-params"
CODE_INTERNAL = "internal"
# 端末バックエンド (Herdr socket) 不通。broker は生存。Set D 6.1 外の runtime 拡張
# (renga-decoupling §5 が Set D 6.2 の "New codes MAY be added" 規定内で新設)。
# broker/MCP 不通の ``backend_unreachable`` とは別コード (§4.6 の 3/4 分離)。
CODE_ADAPTER_UNAVAILABLE = "adapter_unavailable"

# Herdr raw error.code → adapter 出口コード の写像表 (spike §4 実測語彙)。
# 未知 raw code は ``adapter_unavailable`` へ**写像しない** (§4.6: adapter 不通 vs
# broker 不通の分離を壊さないため)。未知は ``internal`` に落とし、呼出側は Set D
# 6.2 "未知コード non-fatal" で default-branch する。
_RAW_CODE_MAP = {
    "pane_not_found": CODE_PANE_NOT_FOUND,
    "workspace_not_found": CODE_PANE_NOT_FOUND,
    "invalid_request": CODE_INVALID_PARAMS,
    "invalid_params": CODE_INVALID_PARAMS,
    "invalid_key": CODE_INVALID_PARAMS,
    # Herdr の label は一意制約が無く衝突コードを発行しないが、異表記が来ても
    # 出口で name_in_use に正規化する (設計書 §3.3 命名注記)。
    "name_taken": CODE_NAME_IN_USE,
    "name_in_use": CODE_NAME_IN_USE,
}


class HerdrError(RuntimeError):
    """Herdr adapter が送出する正規化済みエラー。

    ``code`` は :data:`CODE_*` の adapter 出口コード。``raw`` は Herdr が返した
    生 error.code (socket 不通など Herdr 応答が無い場合は ``None``)。呼出側は
    ``code`` で分岐し、raw を透過しない (設計書 §4.6)。
    """

    def __init__(self, code: str, message: str, raw: str | None = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.raw = raw


# ---------------------------------------------------------------------------
# socket path / binary 解決 (instantiate 時のみ。import 時副作用なし)
# ---------------------------------------------------------------------------

# 既定セッション名 (spike §1.2: env / flag なしは default socket)。
_DEFAULT_SESSION = "default"


def _herdr_config_dir() -> str:
    """Herdr 設定ディレクトリ (``$XDG_CONFIG_HOME/herdr`` or ``~/.config/herdr``)。"""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = xdg if xdg else os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "herdr")


def resolve_socket_path(
    socket_path: str | None = None, session: str | None = None
) -> str:
    """Herdr Socket API の Unix domain socket パスを解決する。

    優先順位 (spike §1.2 実測 = 公式ドキュメント順):
    ``socket_path 引数`` > ``HERDR_SOCKET_PATH`` env > ``session 引数`` /
    ``HERDR_SESSION`` env > 既定セッション (``default``)。

    session 指定時は ``<config>/sessions/<session>/herdr.sock``、既定は
    ``<config>/herdr.sock`` (spike の default 解決先)。パス解決のみで到達性は
    見ない (到達性は request 時に ``adapter_unavailable`` として現れる)。
    """
    if socket_path:
        return socket_path
    env_sock = os.environ.get("HERDR_SOCKET_PATH")
    if env_sock:
        return env_sock
    sess = session or os.environ.get("HERDR_SESSION")
    cfg = _herdr_config_dir()
    if sess:
        return os.path.join(cfg, "sessions", sess, "herdr.sock")
    return os.path.join(cfg, "herdr.sock")


def find_herdr() -> str:
    """PATH 上の ``herdr`` バイナリ (診断用。adapter 本体は socket で話す)。

    tmux.py の ``find_tmux`` / wezterm.py の ``find_wezterm`` と命名を揃えた
    パリティ用。adapter の操作は Socket API 経由で、バイナリ実行はしないが、
    存在確認 / エラーメッセージ用に解決できるようにする。
    """
    exe = shutil.which("herdr")
    if exe:
        return exe
    raise FileNotFoundError("herdr not found in PATH")


# ---------------------------------------------------------------------------
# Socket transport (newline-delimited JSON, one-shot per request)
# ---------------------------------------------------------------------------

# spike §1.1 / §6: 通常リクエストは **1 接続 1 往復** でサーバがクローズする
# (パイプラインは 2 発目で BrokenPipe)。よって request ごとに接続を張り直す。
# 購読系 (events.subscribe) のみ接続維持だが、本 adapter は events を使わない。


@dataclass
class _HerdrClient:
    """Herdr Socket API の薄い JSON-lines クライアント (one-shot request)。"""

    socket_path: str
    timeout: float = 15.0
    _counter: "itertools.count[int]" = field(
        default_factory=lambda: itertools.count(1), repr=False
    )

    def request(self, method: str, params: dict | None = None) -> dict:
        """1 リクエスト送出 → 1 レスポンス受信 → 接続クローズ。

        成功時は response の ``result`` dict を返す。Herdr が ``error`` を返した
        場合は :class:`HerdrError` (正規化コード) を送出。socket 到達不能・
        BrokenPipe・タイムアウト・不正フレームは ``adapter_unavailable``。
        """
        req_id = f"c{next(self._counter)}"
        payload = json.dumps(
            {"id": req_id, "method": method, "params": params or {}}
        ) + "\n"
        raw = self._roundtrip(method, payload)
        try:
            resp = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            # 完全な 1 行を受信したが JSON でない = protocol/schema 崩れであって
            # socket 到達不能ではない (§4.6: adapter_unavailable は socket 不通確認
            # 時のみ)。internal に落とす (呼出側は Set D 6.2 で non-fatal 分岐)。
            raise HerdrError(
                CODE_INTERNAL,
                f"herdr {method}: unparseable response frame: {exc}",
            ) from exc
        if isinstance(resp, dict) and "error" in resp:
            err = resp.get("error") or {}
            raw_code = str(err.get("code", "")) if isinstance(err, dict) else ""
            msg = err.get("message", "") if isinstance(err, dict) else str(err)
            code = _RAW_CODE_MAP.get(raw_code, CODE_INTERNAL)
            raise HerdrError(code, f"herdr {method}: {msg}", raw=raw_code)
        result = resp.get("result") if isinstance(resp, dict) else None
        if not isinstance(result, dict):
            # 応答は届いたが 'result' が無い = schema/version 不一致 → internal。
            raise HerdrError(
                CODE_INTERNAL,
                f"herdr {method}: response missing 'result': {resp!r}",
            )
        return result

    def _roundtrip(self, method: str, payload: str) -> str:
        """socket を張り直して 1 行送り、改行までの 1 行を読んで返す。

        socket レベルの失敗 (到達不能 / BrokenPipe / タイムアウト / 途中クローズ)
        は全て ``adapter_unavailable`` に写像する (§4.6: 端末バックエンド不通)。
        """
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect(self.socket_path)
                sock.sendall(payload.encode("utf-8"))
                chunks: list[bytes] = []
                buf = b""
                while b"\n" not in buf:
                    data = sock.recv(65536)
                    if not data:
                        # サーバが応答行を返さずにクローズ (BrokenPipe 相当)。
                        break
                    chunks.append(data)
                    buf = b"".join(chunks)
        except (OSError, socket.timeout) as exc:
            raise HerdrError(
                CODE_ADAPTER_UNAVAILABLE,
                f"herdr {method}: socket unreachable at "
                f"{self.socket_path!r}: {exc}",
            ) from exc
        line = buf.split(b"\n", 1)[0]
        if not line:
            raise HerdrError(
                CODE_ADAPTER_UNAVAILABLE,
                f"herdr {method}: connection closed before a response line",
            )
        return line.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# HerdrAdapter (TerminalAdapter Protocol の第 3 実装)
# ---------------------------------------------------------------------------


@dataclass
class HerdrAdapter:
    """Herdr Socket API 背後の ``TerminalAdapter`` 実装 (POSIX 限定)。

    専用 workspace を 1 つ確保し (初回 spawn で lazily create)、その workspace の
    pane のみを list / close する (isolated_session=True)。全 request は one-shot
    JSON-lines で、events は使わない (最小面は request/response で成立)。
    """

    # 専用 workspace で無関係 pane を厳格フィルタするため isolated (設計書 §3.4)。
    # backend 固定の能力なので ClassVar (dataclass field にしない)。
    isolated_session: ClassVar[bool] = True

    socket_path: str = field(default_factory=resolve_socket_path)
    timeout: float = 15.0
    # workspace / agent の Herdr ラベル prefix。Herdr label は一意制約なし
    # (衝突検出は broker registry の責務、設計書 §4.2) なので表示補助に留まる。
    label_prefix: str = "claude-org"

    _client: _HerdrClient = field(init=False, repr=False)
    # 専用 workspace の識別子 (初回 spawn で確定)。未 spawn 時は None。
    _workspace_id: str | None = field(default=None, init=False)
    _tab_id: str | None = field(default=None, init=False)
    # workspace.create が同時生成する root shell pane。初回 agent 起動後に閉じる。
    _root_pane_id: str | None = field(default=None, init=False)
    _counter: "itertools.count[int]" = field(
        default_factory=lambda: itertools.count(1), init=False, repr=False
    )
    # workspace 確保 (check→create→bind) を直列化する lock。broker は
    # ThreadingHTTPServer 配下で spawn を並行に呼ぶため、これが無いと 2 つの
    # spawn が同時に workspace 未確定を見て二重に workspace.create しうる
    # (wezterm の _spawn_lock と同じ理由)。
    _spawn_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        # POSIX 限定 (設計書 §4.6): Windows named pipe は stdlib AF_UNIX で扱えず
        # 未対応。instantiate 時に adapter_unavailable を明示する (import 時副作用
        # は無し = このチェックは make_adapter/直接生成の時にだけ走る)。
        if os.name == "nt":
            raise HerdrError(
                CODE_ADAPTER_UNAVAILABLE,
                "Herdr adapter is POSIX-only; Windows named pipe transport is "
                "unsupported (design herdr-adapter.md §4.6 / 残存リスク)",
            )
        self._client = _HerdrClient(self.socket_path, self.timeout)

    # ------------------------------------------------------------------ util
    def _new_label(self) -> str:
        return f"{self.label_prefix}-{os.getpid()}-{next(self._counter)}"

    def _ensure_workspace(self, cwd: str | None) -> None:
        """初回 spawn で専用 workspace + tab + root pane を確保し bind する。

        _spawn_lock 下から呼ばれる。workspace.create は root shell pane を同時
        生成する (spike §項目1) ため、その pane_id を記録し、初回 agent 起動後に
        閉じて「managed agent pane のみの tab」に整える。
        """
        params: dict[str, Any] = {"label": f"{self.label_prefix}-{os.getpid()}"}
        if cwd:
            params["cwd"] = cwd
        res = self._client.request("workspace.create", params)
        ws = res.get("workspace") or {}
        root = res.get("root_pane") or {}
        self._workspace_id = ws.get("workspace_id")
        self._tab_id = ws.get("active_tab_id")
        self._root_pane_id = root.get("pane_id")
        if self._workspace_id is None or self._tab_id is None:
            # socket は応答したが期待フィールド欠落 = schema/version 不一致。
            # adapter_unavailable ではなく internal (§4.6)。
            raise HerdrError(
                CODE_INTERNAL,
                f"herdr workspace.create: missing workspace/tab id: {res!r}",
            )

    # ----------------------------------------------------------------- spawn
    def spawn(
        self,
        argv: list[str],
        cwd: str | None = None,
        new_window: bool = True,
    ) -> PaneRef:
        """argv を専用 workspace の単一 tab に起動し PaneRef を返す。

        - **cwd 前検証** (設計書 §4.6): cwd 指定時、存在・ディレクトリ性を
          layout mutation の**前に**検証し、不正なら socket を一切叩かず
          ``cwd_invalid`` (half-mutated state を作らない)。
        - 初回は workspace を確保 (``_ensure_workspace``)、以降は同 workspace/tab
          へ split して起動する (Set D 4.2 single-tab を維持)。``new_window`` は
          WezTerm/tmux 面との互換のため受けるが、Herdr では常に専用 workspace の
          単一 tab に置く (無関係 tab を作らない)。
        - identity (name/role/衝突検出) は broker registry の責務 (設計書 §4.2)。
          adapter は表示補助の label のみ渡す。
        """
        if cwd is not None and not os.path.isdir(cwd):
            raise HerdrError(
                CODE_CWD_INVALID,
                f"cwd {cwd!r} does not exist or is not a directory",
            )
        with self._spawn_lock:
            first = self._workspace_id is None
            if first:
                self._ensure_workspace(cwd)
            params: dict[str, Any] = {
                "name": self._new_label(),
                "argv": list(argv),
                "workspace": self._workspace_id,
                "tab": self._tab_id,
                # 単一 tab 内の追加 pane は下方向に分割する (placement の最終判断は
                # broker の choose_split が list_panes の cell geometry で行う。
                # ここは Herdr が要求する分割方向の既定値)。
                "split": "down",
            }
            if cwd:
                params["cwd"] = cwd
            res = self._client.request("agent.start", params)
            agent = res.get("agent") or {}
            pane_id = agent.get("pane_id")
            if pane_id is None:
                # socket は生きているのに期待フィールドが無い = schema/version
                # 不一致 (§4.6: adapter_unavailable は socket 到達不能確認時のみ)。
                raise HerdrError(
                    CODE_INTERNAL,
                    f"herdr agent.start: response missing pane_id: {res!r}",
                )
            # workspace.create が同時生成した root shell pane を後始末する。
            # 判定は ``first`` ではなく ``_root_pane_id`` の有無で行う: 初回 spawn
            # の agent.start が (transient に) 失敗すると workspace は確保済み
            # (次回 first=False) だが root pane が残る。``first`` gate だと二度と
            # 閉じられず leak するため、root pane が残っている限り「最初に agent
            # pane が立った spawn」で確実に閉じる (>=2 pane で last_pane にならない)。
            # cleanup なので失敗は無視。
            if self._root_pane_id is not None:
                try:
                    self._client.request(
                        "pane.close", {"pane_id": self._root_pane_id}
                    )
                except HerdrError:
                    pass
                self._root_pane_id = None
            return PaneRef(
                pane_id=pane_id,
                window_id=self._workspace_id,
                tab_id=self._tab_id,
            )

    # ------------------------------------------------------------------ list
    def list_panes(self) -> list[dict]:
        """専用 workspace の pane を geometry (cell 単位) 付きで列挙する。

        未 spawn (workspace 未確保) なら ``[]`` (tmux の「session 皆無」と同型)。
        pane.list を workspace_id で絞り、pane.layout の cell 単位 rect を pane_id
        で突き合わせて ``x/y/width/height`` を充填する (設計書 §4.3、spike 窓口
        補足2: Herdr geometry は端末セル単位)。返す dict は broker の
        ``list_panes_view`` が読む key (pane_id / x / y / width / height / active)
        を満たす。

        error 方針は tmux と同型: workspace 消失 (workspace_not_found) だけを空
        扱いにし、socket 不通 (adapter_unavailable) 等は例外のまま上げる
        (pane_exists が「backend 不通」を「pane 不在」と誤読しないため)。
        """
        if self._workspace_id is None:
            return []
        try:
            res = self._client.request(
                "pane.list", {"workspace_id": self._workspace_id}
            )
        except HerdrError as exc:
            # 専用 workspace が外部で閉じられた場合のみ空 (benign)。それ以外
            # (adapter_unavailable 等) は透過して上げる。
            if exc.code == CODE_PANE_NOT_FOUND and exc.raw == "workspace_not_found":
                # cached workspace state をクリアして次 spawn で再確保させる。
                # クリアしないと stale な workspace_id/tab_id が残り、次 spawn が
                # workspace.create を skip して消えた workspace へ agent.start し、
                # daemon 再起動まで回復不能になる (Codex P2)。
                self._workspace_id = None
                self._tab_id = None
                self._root_pane_id = None
                return []
            raise
        raw_panes = res.get("panes") or []
        # workspace_id が **厳密に一致** する pane のみ通す。isolated_session=True
        # の本 adapter は org down が list_panes の全 pane を broker 所有として
        # close しうるため、workspace_id 欠落/不一致の pane (unscoped / 旧 schema
        # 応答) を通すと無関係 pane の巻き添え close を招く (Codex P2)。pane.list は
        # server 側で workspace_id scope 済みだが、adapter 側でも厳格に再確認する。
        panes = [p for p in raw_panes if p.get("workspace_id") == self._workspace_id]
        geom, focused_id = self._layout_geometry(panes)
        out: list[dict] = []
        for p in panes:
            pid = p.get("pane_id")
            rect = geom.get(pid, {})
            out.append(
                {
                    "pane_id": pid,
                    "window_id": p.get("workspace_id", self._workspace_id),
                    "tab_id": p.get("tab_id", self._tab_id),
                    "x": int(rect.get("x", 0)),
                    "y": int(rect.get("y", 0)),
                    "width": int(rect.get("width", 0)),
                    "height": int(rect.get("height", 0)),
                    "active": bool(
                        p.get("active", focused_id is not None and pid == focused_id)
                    ),
                    "cwd": p.get("cwd") or p.get("foreground_cwd"),
                    "label": p.get("label"),
                    "agent_status": p.get("agent_status"),
                }
            )
        return out

    def _layout_geometry(
        self, panes: list[dict]
    ) -> tuple[dict[str, dict], str | None]:
        """pane.layout から pane_id → cell rect と focused_pane_id を得る。

        layout は tab 全体の pane rectangles を一度に返す (spike 窓口補足2) ので、
        任意の 1 pane を起点に 1 回だけ呼ぶ。取得不能 (単一 pane 等で失敗) の場合は
        空 geometry に degrade する (list_panes 自体は落とさない)。
        """
        if not panes:
            return {}, None
        anchor = panes[0].get("pane_id")
        if anchor is None:
            return {}, None
        try:
            res = self._client.request("pane.layout", {"pane_id": anchor})
        except HerdrError:
            return {}, None
        layout = res.get("layout") or res
        geom: dict[str, dict] = {}
        for entry in layout.get("panes", []) or []:
            pid = entry.get("pane_id")
            rect = entry.get("rect")
            if pid is not None and isinstance(rect, dict):
                geom[pid] = rect
        return geom, layout.get("focused_pane_id")

    def pane_exists(self, pane_id: str) -> bool:
        return any(p["pane_id"] == pane_id for p in self.list_panes())

    # -------------------------------------------------------------- get-text
    def get_text(self, pane_id: str, escapes: bool = False) -> str:
        """pane の画面テキストを取得する (grid scrape)。

        spike §項目3: ライブ画面は ``source=visible`` (``recent`` はスクロール
        アウト未発生時に空を返すため使わない)。``escapes=True`` は
        ``format=ansi`` で raw エスケープ込み、既定は ``format=text``。
        """
        res = self._client.request(
            "pane.read",
            {
                "pane_id": pane_id,
                "source": "visible",
                "format": "ansi" if escapes else "text",
            },
        )
        read = res.get("read") or {}
        return read.get("text", "")

    # ------------------------------------------------------------- send-text
    def type_text(self, pane_id: str, text: str) -> None:
        """未送信で入力欄に置く (submit しない)。

        Herdr ``pane.send_text`` はリテラル文字列を投入し Enter は付かない
        (spike §項目2)。複数行テキストの改行が TUI 入力欄で行ごとの submit に
        化けないかは Herdr 側の入力欄セマンティクス依存 (bracketed-paste flag は
        socket 面に無い)。ナッジ / 定型注入 (単一行) が主用途。
        """
        self._client.request("pane.send_text", {"pane_id": pane_id, "text": text})

    def send_enter(self, pane_id: str) -> None:
        """Enter 1 打 (承認プロンプト機械承認 / submit)。"""
        self._client.request("pane.send_keys", {"pane_id": pane_id, "keys": ["enter"]})

    def send_interrupt(self, pane_id: str) -> None:
        """Ctrl+C 1 打 (入力欄クリア / SIGINT)。spike §項目2 で実中断を確認済み。"""
        self._client.request(
            "pane.send_keys", {"pane_id": pane_id, "keys": ["ctrl+c"]}
        )

    def send_line(self, pane_id: str, text: str, settle: float = 0.15) -> None:
        """1 行送出 + Enter (ナッジ注入の正準形)。

        text を send_text で置き、settle 後に Enter を送る (literal 反映と Enter の
        競合を避ける小休止。tmux / wezterm の send_line と同型)。
        """
        self.type_text(pane_id, text)
        time.sleep(settle)
        self.send_enter(pane_id)

    # ------------------------------------------------------------------ kill
    def kill_pane(self, pane_id: str) -> None:
        """spawn した pane を閉じる。

        通常は ``pane.close`` で閉じる。ただし Herdr は tab に残る **最後の pane**
        の close を拒否しうる (tmux は最後の pane kill で session ごと消えるが、
        Herdr は明示の ``workspace.close`` が要る。設計書 §3.3: last_pane は Herdr
        明示コード無し)。本 adapter は初回 agent 起動後に root shell pane を閉じる
        ため「managed agent pane が 1 枚だけの workspace」が通常状態であり、その
        最後の 1 枚を close するケースは頻出する。

        ここで close 拒否を握り潰して黙って返すと、broker は「閉じた」と誤認して
        pane/token を unregister し成功報告する一方、TUI は生き続けて管理不能に
        なる (Codex P1)。よって close が拒否されたら、対象が専用 workspace に残る
        **唯一の pane** の場合に限り workspace ごと閉じて確実に reap する。既に
        消えている (pane_not_found) / socket 不通等の best-effort 断念は tmux
        ``kill_pane`` (check=False) 同様に握り潰す。
        """
        try:
            self._client.request("pane.close", {"pane_id": pane_id})
            return
        except HerdrError:
            pass
        # close 拒否。残存 pane を確認し、対象が唯一なら workspace ごと後始末する。
        try:
            remaining = [p["pane_id"] for p in self.list_panes()]
        except HerdrError:
            return  # backend 不通等: best-effort 断念 (これ以上は追わない)
        if remaining == [pane_id]:
            self.close_workspace()

    def close_workspace(self) -> None:
        """専用 workspace ごと後始末する (tmux ``kill_server`` 相当、best-effort)。

        本 adapter が確保した workspace の全 pane を一括で閉じる。冪等: 未確保 /
        既に消えている場合は何もしない。
        """
        if self._workspace_id is None:
            return
        try:
            self._client.request(
                "workspace.close", {"workspace_id": self._workspace_id}
            )
        except HerdrError:
            pass
        self._workspace_id = None
        self._tab_id = None
        self._root_pane_id = None
