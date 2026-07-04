# -*- coding: utf-8 -*-
"""Herdr terminal adapter (第 3 の POSIX backend, minimal surface)。

設計 SoT: claude-org-transport-lab docs/design/herdr-adapter.md (merged PR #29) +
docs/design/herdr-workspace-layout.md (merged PR #31, Issue #110 = workspace
レイアウトポリシー)。実測裏付け: docs/reports/herdr-socket-spike.md (Herdr 0.7.1 /
protocol 14) + investigation/LAYOUT_PROBE_FINDINGS.md (probe 6 配置決定性)。

現行 canonical は本モジュール。WezTerm (Phase 1) / tmux (Phase 2) に続く第 3 の
``TerminalAdapter`` 実装で、broker / harness は同一の ``TerminalAdapter`` 面と
``make_adapter()`` ファクトリ経由でのみ Herdr backend に触る。

本タスクのスコープ (最小面 + workspace レイアウト):
- 最小面: spawn / list_panes / pane_exists / get_text / type_text / send_enter /
  send_line / send_interrupt / kill_pane。
- workspace レイアウト (Issue #110): 単一専用 workspace から **org 所有 workspace 集合**
  へ拡張。control 面 1 スペース + ワーカーはプロジェクト単位スペース。詳細は下記
  「workspace レイアウト」節。
- 接続は **stdlib のみ** (Unix domain socket + newline-delimited JSON)。
- **POSIX / WSL 限定**。Windows named pipe は未対応 = instantiate 時に
  ``adapter_unavailable`` を明示 (設計書 §4.6 / 残存リスク)。

**スコープ外 (follow-up)**:
- events buffer / cursor 正規化 (設計書 §4.5)。本 adapter は events を一切使わない
  (最小面は全て one-shot request/response、監視は poll ベース、Issue #110 §9)。
- Set D Surface 4.2 (single-tab MUST) / Surface 1 (spawn の space パラメータ) の契約
  ratify は本体取り込みスコープ (別 PR)。本 adapter は flag 実装のみ (Issue #110 §10)。
- ja 側 delegate 配管 (project-slug の brief からのリレー)。

分離と isolation 境界 (Issue #110 §4、**BLOCKER 級不変条件**):
- 本 adapter は **org 所有 workspace 集合**を確保し、その workspace の pane のみを
  list / close する (isolated_session=True)。集合は **2 つに分離**する:
  - **close-authority owned set** (:attr:`_spaces`): adapter が自ら ``workspace.create``
    し、かつラベルが自 org / 現世代に前方一致する workspace のみ。``workspace.close``
    を発行してよいのはこの集合に限る (self-ownership ゲート)。
  - **liveness-tracking set** (:attr:`_owned_panes` の実配置): 各 pane が実際に居る
    workspace への追跡。verify+rebind (Fix-C) が実配置を記録するが、foreign (人間 /
    他 org) workspace は **決して close-authority へ加えない** (§7.3)。
- list_panes の一次フィルタは **``pane_id ∈ 自 registry``** (:attr:`_owned_panes`)。
  owned workspace ではさらに adapter-managed tab_id を要求する (per-workspace
  single-tab 不変条件 = Surface 4.2 の tab 分離を workspace 単位で維持)。

配置戦略 C (spawn-then-move、Issue #114 Fix-C / probe 6): Herdr 0.7.1 の ``agent.start``
は ``workspace`` / ``tab`` を無視し focused workspace へ相乗り配置する (probe 6a)。よって
spawn 後に ``pane.get`` で実着地を検証し、狙った space の workspace とずれていれば
``pane.move`` で space の tab へ移送する (probe 6c: cross-workspace move 可、pane_id は
変わるが terminal_id は保存)。移送先 tab は明示なので focused 非依存で決定的。

error code (設計書 §3.3 / §4.6): Herdr raw error を透過せず adapter 出口で Set D 語彙へ
写像する。socket 到達不能は ``adapter_unavailable`` に分離。

import 時副作用なし: socket path 解決とバイナリ探索は instantiate 時に限定。
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import socket
import threading
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar

from .base import (  # noqa: F401  (NUDGE_TEXT / space 定数の再利用)
    NUDGE_TEXT,
    PANE_LIVE_ALIVE,
    PANE_LIVE_GONE,
    PANE_LIVE_REUSED,
    PANE_LIVE_UNKNOWN,
    SPACE_CONTROL,
    PaneRef,
    SpaceDescriptor,
    backend_unavailable_reason,
)
from .keys import CTRL_LETTERS

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
# canonical キー -> Herdr pane.send_keys token の明示マップ (Issue #108)
# ---------------------------------------------------------------------------
#
# canonical -> Herdr token。Herdr 0.7.1 (protocol 14) の実 server に対して各 token を
# 個別に打鍵して**実測した accept/reject** に基づく (tmp probe / E2E)。既存
# send_enter / send_interrupt が ``["enter"]`` / ``["ctrl+c"]`` を送っている
# (spike §項目2) のと整合。adapter は canonical のみを受け取り (正規化は broker/surface
# 側)、ここで token 化する。CI は fake socket で送出 token を pin し、実 binary がある
# 環境では E2E で裏取りする (Herdr 語彙が版で変わったら本表のみ直せば済む単一箇所)。
#
# **重要 (実測)**: Herdr 0.7.1 の send-keys 語彙は **full ではない**。
# delete / home / end / pageup / pagedown は ``invalid_key`` で拒否されるため
# **本表から意図的に除外**する (canonical だが Herdr では emit 不能)。これらを含む
# send_keys は broker preflight が Herdr backend で ``[key_unsupported]`` を返す。
# backtab は Herdr では ``shift+tab`` token、esc は ``escape`` token で送る。
# ctrl+a..z は 26 個すべて accept される (実測)。
_HERDR_KEY_MAP: dict[str, str] = {
    "enter": "enter",
    "tab": "tab",
    "space": "space",
    "esc": "escape",
    "backspace": "backspace",
    "backtab": "shift+tab",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    **{f"ctrl+{c}": f"ctrl+{c}" for c in CTRL_LETTERS},
}


# ---------------------------------------------------------------------------
# workspace レイアウト: 世代識別 + space 状態機械 (Issue #110 §4.2 / §5)
# ---------------------------------------------------------------------------

# workspace 単位の状態 (§4.2)。close-authority owned set と liveness-tracking の
# 双方のメンバに付く。SWEPT / workspace.close は owned set メンバにのみ適用する。
WS_LIVE = "LIVE"          # 正常
WS_DEGRADED = "DEGRADED"  # workspace_not_found / socket blip で list ソース一時喪失
WS_SWEPT = "SWEPT"        # adapter が意図的に掃除した (正当に消えた)
WS_GONE = "GONE"          # 恒久的に消えたと確定 (workspace.list に不在)

# state dir 内の世代識別ファイル (§5.2)。org_instance_id は初回 org up で生成・永続、
# generation は daemon boot ごとに単調増加 (write-ahead)。
_ORG_INSTANCE_FILE = "herdr_org_instance"
_GENERATION_FILE = "herdr_generation"
_SWEEP_LOCK_FILE = "herdr_sweep.lock"


def _atomic_write(path: str, content: str, *, fsync: bool = False) -> None:
    """tmp へ書いて rename する原子的書き込み (fsync=True で write-ahead 保証)。

    fsync=True は tmp ファイル本体に加えて **親ディレクトリも fsync** する — さもないと
    ``os.replace`` の rename エントリが crash 前にディスクへ落ちず、generation の
    write-ahead 順序 (§5.2) が電源断で崩れうる (新 boot が同 generation を読み直す)。
    """
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
        if fsync:
            f.flush()
            os.fsync(f.fileno())
    os.replace(tmp, path)
    if fsync:
        dir_fd = os.open(os.path.dirname(path) or ".", os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)


def _read_or_create_org_instance_id(state_dir: str) -> str:
    """org インスタンスの衝突耐性 id を読む / 無ければ生成・永続する (§5.2)。

    ≥128-bit のエントロピー (UUID) で衝突耐性を担保する — sweep は前方一致で自 org
    workspace を選ぶため、2 org が同 id を引くと cross-org 汚染になる (§5.2)。
    """
    path = os.path.join(state_dir, _ORG_INSTANCE_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            val = f.read().strip()
        if val:
            return val
    except OSError:
        pass
    val = uuid.uuid4().hex  # 128-bit
    try:
        _atomic_write(path, val)
    except OSError:
        pass
    return val


def _bump_generation(state_dir: str) -> int:
    """daemon boot ごとの単調増加 generation を返す (§5.2)。

    **write-ahead**: increment 後の値を **``workspace.create`` を 1 つでも発行する前に**
    fsync 永続化する。さもないと「gN に increment → gN で workspace 作成 → 永続前 crash」
    で次 boot が再び gN を読み、sweep (gen < current のみ対象) が死んだ gN を回収できず、
    かつ adopt が死 daemon の gN を live として取り込む世代内孤児を生む (§5.2)。
    """
    path = os.path.join(state_dir, _GENERATION_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            cur = int(f.read().strip() or "0")
    except (OSError, ValueError):
        cur = 0
    nxt = cur + 1
    _atomic_write(path, str(nxt), fsync=True)
    return nxt


def _pid_alive(pid: int) -> bool:
    """pid が生存しているか (os.kill(pid, 0))。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在するが別 uid
    except OSError:
        return False
    return True


def _parse_label(prefix_oid: str, label: str) -> tuple[int | None, str | None]:
    """自 org ラベル ``{prefix}/{oid}/g{gen}/{space_key}`` から (gen, space_key) を取る。

    ``prefix_oid`` は末尾スラッシュ付きの ``{label_prefix}/{org_instance_id}/``。
    前方一致しない (別 org / prefix 無し) は ``(None, None)`` = 絶対に触れない (§5.3)。
    """
    if not label.startswith(prefix_oid):
        return None, None
    rest = label[len(prefix_oid):]
    gpart, sep, space_key = rest.partition("/")
    if not sep or not gpart.startswith("g"):
        return None, None
    try:
        gen = int(gpart[1:])
    except ValueError:
        return None, None
    return gen, space_key


@dataclass
class _Space:
    """close-authority owned set のメンバ = 自作成 workspace + 単一 adapter-managed tab。

    space = (workspace_id, その adapter-managed tab_id) の組 (§2 の per-workspace
    single-tab 不変条件)。adapter はこの tab_id を記録し、list / close / addressing を
    workspace_id **かつ** tab_id で絞る。
    """

    space_key: str
    workspace_id: str
    tab_id: str
    label: str
    root_pane_id: str | None = None
    state: str = WS_LIVE
    created_at: float = 0.0
    # §4.2 の DEGRADED 有界脱出用: 連続喪失の回数 / 実時間。
    missing_since: float | None = None
    missing_count: int = 0


@dataclass
class _PaneRecord:
    """adapter registry のエントリ (liveness-tracking の実配置ポインタ)。

    pane_id → 実際に居る (space_key, workspace_id, tab_id)。owned pane は常に owned
    workspace に居る (戦略 C が移送するため) が、workspace_id / tab_id を明示保持して
    list_panes の一次フィルタ (registry) + owned-tab フィルタに使う。
    """

    pane_id: str
    space_key: str
    workspace_id: str
    tab_id: str | None
    terminal_id: str | None = None
    spawned_at: float = 0.0


# ---------------------------------------------------------------------------
# HerdrAdapter (TerminalAdapter Protocol の第 3 実装)
# ---------------------------------------------------------------------------


@dataclass
class HerdrAdapter:
    """Herdr Socket API 背後の ``TerminalAdapter`` 実装 (POSIX 限定)。

    org 所有 workspace 集合を確保し (space ごとに lazy create)、その集合の pane のみを
    list / close する (isolated_session=True)。全 request は one-shot JSON-lines で、
    events は使わない。配置は戦略 C (spawn-then-move、Issue #114 Fix-C / probe 6)。
    """

    # 専用 workspace 集合で無関係 pane を厳格フィルタするため isolated (設計書 §3.4)。
    isolated_session: ClassVar[bool] = True

    # workspace レイアウトポリシー (Issue #110) を持つ backend であることの宣言。
    # broker は getattr でこれを読み、True の時だけ role/project から SpaceDescriptor を
    # 算出して spawn(space=) へ渡し、空スペース掃除等のレイアウト挙動を有効化する。
    # tmux/wezterm は本フラグを持たない (getattr フォールバック False で flat 不変)。
    supports_space_layout: ClassVar[bool] = True

    # Herdr pane.send_keys が emit 可能な canonical 部分集合 (= _HERDR_KEY_MAP の key)。
    supported_named_keys: ClassVar[frozenset[str]] = frozenset(_HERDR_KEY_MAP)

    # opportunistic reap の backend-aware 閾値 (broker が getattr で読む)。Herdr の
    # pane.list は eventually consistent で生 pane が一時欠落しうるため保守側に振る。
    reap_min_age_seconds: ClassVar[float] = 12.0
    reap_min_missing_snapshots: ClassVar[int] = 3
    reap_min_missing_seconds: ClassVar[float] = 6.0

    # 空プロジェクトスペースの掃除 grace (§4.3 の in-flight / grace ガード)。boot latency
    # 窓での「一瞬空」を掃除と誤認しないための最小 age。in-flight カウンタが主ガードで、
    # これは補助 (create 直後の born-empty を racing spawn が埋める窓を跨ぐ)。
    space_sweep_grace_seconds: ClassVar[float] = 8.0

    # DEGRADED → GONE の有界脱出しきい値 (§4.2)。連続喪失がこの回数 / 秒を超えたら
    # workspace.list 突き合わせで GONE 判定を強制する。
    degraded_max_misses: ClassVar[int] = 3
    degraded_max_seconds: ClassVar[float] = 6.0

    socket_path: str = field(default_factory=resolve_socket_path)
    timeout: float = 15.0
    # workspace / agent の Herdr ラベル prefix。Herdr label は一意制約なしなので
    # owned set の権威は _spaces 写像であり、ラベルは discovery / 人間可読の補助。
    label_prefix: str = "claude-org"
    # 世代識別の永続先 (broker state dir、§5.2)。None なら ephemeral (テスト / standalone)
    # で org_instance_id を都度生成し generation=0、起動時 sweep は行わない。
    state_dir: str | None = None
    # 明示指定用 (通常は state_dir から解決)。None なら __post_init__ で解決する。
    org_instance_id: str | None = None
    generation: int | None = None

    _client: _HerdrClient = field(init=False, repr=False)
    # close-authority owned set: space_key -> _Space (§4.1)。値の workspace_id 集合が
    # 「workspace.close を発行してよい」集合。自作成 + 自ラベル一致でのみ成長する。
    _spaces: dict[str, _Space] = field(default_factory=dict, init=False, repr=False)
    # adapter registry (liveness-tracking の実配置): pane_id -> _PaneRecord。list_panes の
    # 一次フィルタ。GONE/REUSED / close で除去する。
    _owned_panes: dict[str, _PaneRecord] = field(
        default_factory=dict, init=False, repr=False
    )
    # SWEPT にしたが workspace.close が失敗した workspace の再試行集合 (§4.3)。
    # **workspace_id -> _Space** (owned set の外)。live-list からは除外し
    # _retry_pending_sweep が workspace.close を再試行する。
    _pending_sweep: dict[str, _Space] = field(
        default_factory=dict, init=False, repr=False
    )
    # space_key -> in-flight spawn 数 (§4.3 の掃除抑止)。spawn が create〜pane 記録の
    # 窓で加算し、掃除はこれが >0 の space をスキップする。
    _spawn_inflight: dict[str, int] = field(
        default_factory=dict, init=False, repr=False
    )
    _counter: "itertools.count[int]" = field(
        default_factory=lambda: itertools.count(1), init=False, repr=False
    )
    # workspace 確保 (check→create→bind) + agent 配置を直列化する lock。二重
    # workspace.create を防ぎ (dedup)、spawn 群を直列化する (wezterm の _spawn_lock 同型)。
    _spawn_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False, compare=False
    )
    # registry dict (_spaces / _owned_panes / _pending_sweep / _spawn_inflight) の
    # in-memory 更新を守る lock。**この lock 下では socket I/O を呼ばない** (broker と
    # 同じデッドロック回避契約)。
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        # POSIX / WSL only (design herdr-adapter.md §4.6): native Windows lacks
        # the stdlib AF_UNIX socket this adapter needs. Reason string comes from
        # the shared SoT helper so the direct 'broker serve --backend herdr' path
        # and the org up launcher fail-fast never drift and stay ASCII (cp932).
        reason = backend_unavailable_reason("herdr")
        if reason:
            raise HerdrError(CODE_ADAPTER_UNAVAILABLE, reason)
        self._client = _HerdrClient(self.socket_path, self.timeout)
        # 世代識別の解決 (§5.2)。state_dir があれば永続 org_instance_id + boot ごとの
        # 単調 generation (write-ahead)。無ければ ephemeral (テスト / standalone)。
        # state_dir は Broker より先に adapter が構築されうる (cli.py: make_adapter →
        # Broker の順) ため、ここで確実に作る。さもないと _bump_generation の write-ahead
        # が FileNotFoundError で落ち、初回 Herdr daemon が起動できない (Codex P1)。
        if self.state_dir:
            try:
                os.makedirs(self.state_dir, exist_ok=True)
            except OSError:
                pass
        if self.org_instance_id is None:
            if self.state_dir:
                self.org_instance_id = _read_or_create_org_instance_id(self.state_dir)
            else:
                self.org_instance_id = uuid.uuid4().hex
        if self.generation is None:
            self.generation = _bump_generation(self.state_dir) if self.state_dir else 0
        # 起動時 stale 掃除 (§5.3)。state_dir がある実 daemon boot でのみ。socket 不通 /
        # 未起動は best-effort でスキップ (degrade、boot を落とさない)。
        if self.state_dir:
            try:
                self._startup_sweep()
            except HerdrError:
                pass

    # -------------------------------------------------- back-compat accessors
    # 旧 single-workspace API (テスト / 後方互換)。control スペース (無ければ唯一の
    # スペース) を指す read-only アクセサ。新経路は _spaces / _owned_panes を使う。
    def _primary_space(self) -> _Space | None:
        sp = self._spaces.get(SPACE_CONTROL)
        if sp is not None:
            return sp
        if len(self._spaces) == 1:
            return next(iter(self._spaces.values()))
        return None

    @property
    def _workspace_id(self) -> str | None:
        sp = self._primary_space()
        return sp.workspace_id if sp else None

    @property
    def _tab_id(self) -> str | None:
        sp = self._primary_space()
        return sp.tab_id if sp else None

    @property
    def _root_pane_id(self) -> str | None:
        sp = self._primary_space()
        return sp.root_pane_id if sp else None

    # ------------------------------------------------------------------ util
    def _agent_name(self, space_key: str) -> str:
        """agent.start の表示 name (世代 / space が読めるラベル)。"""
        return (
            f"{self.label_prefix}/{self.org_instance_id}/g{self.generation}/"
            f"{space_key}/a{next(self._counter)}"
        )

    def _space_label(self, space_key: str) -> str:
        """workspace ラベル ``{prefix}/{org_instance_id}/g{gen}/{space_key}`` (§5.2)。"""
        return (
            f"{self.label_prefix}/{self.org_instance_id}/g{self.generation}/{space_key}"
        )

    @staticmethod
    def _split_for(space_key: str, space: SpaceDescriptor | None) -> str:
        """per-space の既定分割方向 (Issue #110 §8)。

        control / project とも既定は上下 (Herdr ``down``)。SpaceDescriptor が
        split_direction を明示すればそれを使う (設定可能化)。§2 の語彙対応厳守
        (claude-org「上下」= Herdr ``down`` / 「左右」= ``right``)。
        """
        if space is not None and space.split_direction:
            return space.split_direction
        return "down"

    @staticmethod
    def _space_key_of(space: SpaceDescriptor | None) -> str:
        """SpaceDescriptor から space_key を取る。None は control に既定 (単一制御面)。

        space を渡さない呼出 (テスト / 非レイアウト経路) は control スペースに集約する
        = 現行の単一 workspace 挙動と後方互換 (全 pane が 1 スペースに載る)。
        """
        if space is not None and space.space_key:
            return space.space_key
        return SPACE_CONTROL

    # ----------------------------------------------------- space lazy 作成
    def _create_space(self, space_key: str, cwd: str | None) -> _Space:
        """workspace + tab + root pane を確保し _spaces へ登録する (_spawn_lock 下)。

        workspace.create は root shell pane を同時生成する (spike §項目1)。その pane_id
        を _Space に記録し、初回 agent 配置後 (実配置検証後) に閉じる (§7.4)。
        """
        params: dict[str, Any] = {"label": self._space_label(space_key)}
        if cwd:
            params["cwd"] = cwd
        res = self._client.request("workspace.create", params)
        ws = res.get("workspace") or {}
        root = res.get("root_pane") or {}
        workspace_id = ws.get("workspace_id")
        tab_id = ws.get("active_tab_id")
        if workspace_id is None or tab_id is None:
            raise HerdrError(
                CODE_INTERNAL,
                f"herdr workspace.create: missing workspace/tab id: {res!r}",
            )
        space = _Space(
            space_key=space_key,
            workspace_id=workspace_id,
            tab_id=tab_id,
            label=params["label"],
            root_pane_id=root.get("pane_id"),
            created_at=time.time(),
        )
        with self._lock:
            self._spaces[space_key] = space
        return space

    def _ensure_space(self, space_key: str, cwd: str | None) -> tuple[_Space, bool]:
        """space_key の workspace を get-or-create する (_spawn_lock 下)。

        既存を再利用する前に **workspace.list で実在確認**する。LIVE でも、worker が
        kill_pane 外で自己終了 → Herdr が workspace を auto-close → poll/liveness が sweep する
        前に respawn した場合、キャッシュは LIVE のまま死んだ workspace/tab を指し、その tab へ
        の pane.move が失敗する (Codex P2)。存在すれば再利用 (DEGRADED は LIVE へ復帰)、消えて
        いれば GONE として捨てて新規作成する (§4.2: workspace_not_found 単発で eager recreate
        せず、実在確認で GONE 確定した時のみ作り直す — 孤児増殖アームを開かない)。
        返り値 ``(space, created_now)``。
        """
        with self._lock:
            sp = self._spaces.get(space_key)
        if sp is not None and sp.state in (WS_LIVE, WS_DEGRADED):
            if self._workspace_present(sp.workspace_id):
                if sp.state == WS_DEGRADED:
                    with self._lock:
                        sp.state = WS_LIVE
                        sp.missing_since = None
                        sp.missing_count = 0
                return sp, False
            # 実在せず = auto-close / 消滅確定。owned set から外し pane を解放して新規作成。
            self._drop_space(space_key, WS_GONE)
        return self._create_space(space_key, cwd), True

    def _workspace_present(self, workspace_id: str) -> bool:
        """workspace.list に workspace_id が居るか (best-effort、判定不能は保守的 True)。"""
        try:
            res = self._client.request("workspace.list", {})
        except HerdrError:
            return True  # 判定不能: 消えたと断じない (安全側 = defer)
        return any(
            w.get("workspace_id") == workspace_id
            for w in (res.get("workspaces") or [])
        )

    def _drop_space(self, space_key: str, state: str) -> None:
        """owned set から space を外し、その space の owned pane registry を掃除する。

        GONE (恒久喪失) / SWEPT (掃除完了) 確定時のみ呼ぶ。close-authority から外れるので
        以後 workspace.close の対象にならない。その pane は broker が pane_liveness で
        GONE 判定して reap する。
        """
        with self._lock:
            sp = self._spaces.pop(space_key, None)
            if sp is not None:
                sp.state = state
            gone_ws = sp.workspace_id if sp else None
            if gone_ws is not None:
                for pid in [
                    p for p, r in self._owned_panes.items()
                    if r.workspace_id == gone_ws
                ]:
                    self._owned_panes.pop(pid, None)

    # ----------------------------------------------------------------- spawn
    def spawn(
        self,
        argv: list[str],
        cwd: str | None = None,
        new_window: bool = True,
        space: SpaceDescriptor | None = None,
        env: dict[str, str] | None = None,
    ) -> PaneRef:
        """argv を space の workspace/tab に起動し PaneRef を返す (戦略 C)。

        - **cwd 前検証** (設計書 §4.6): 不正なら socket を一切叩かず ``cwd_invalid``。
        - space (Issue #110 §6.2 Layer C): 配置先スペース。None は control スペースへ
          集約 (後方互換)。space の workspace を lazy 確保し、``agent.start`` 後に実着地を
          ``pane.get`` で検証、狙った workspace とずれていれば ``pane.move`` で space の
          tab へ移送する (probe 6a: agent.start は focused に相乗り / 6c: move 可)。
        - ``new_window`` は tmux/wezterm 面との互換のため受けるが Herdr では space の
          workspace/tab に置く。
        - ``env`` (Issue #122): pane プロセスへ追加注入する環境変数。Herdr protocol は
          ``agent.start`` の ``env`` param で任意 env 注入をサポートする (socket spike
          実測、knowledge/curated/herdr.md)。Herdr が自動注入する ``HERDR_*`` の上に
          重なる。空 / None なら param を付けない (従来挙動)。
        """
        if cwd is not None and not os.path.isdir(cwd):
            raise HerdrError(
                CODE_CWD_INVALID,
                f"cwd {cwd!r} does not exist or is not a directory",
            )
        space_key = self._space_key_of(space)
        split = self._split_for(space_key, space)
        with self._spawn_lock:
            sp, created_now = self._ensure_space(space_key, cwd)
            with self._lock:
                self._spawn_inflight[space_key] = (
                    self._spawn_inflight.get(space_key, 0) + 1
                )
            failed = False
            try:
                params: dict[str, Any] = {
                    "name": self._agent_name(space_key),
                    "argv": list(argv),
                    "workspace": sp.workspace_id,
                    "tab": sp.tab_id,
                    "split": split,
                }
                if cwd:
                    params["cwd"] = cwd
                if env:
                    params["env"] = dict(env)
                res = self._client.request("agent.start", params)
                agent = res.get("agent") or {}
                pane_id = agent.get("pane_id")
                if pane_id is None:
                    raise HerdrError(
                        CODE_INTERNAL,
                        f"herdr agent.start: response missing pane_id: {res!r}",
                    )
                # placement 補正 (Fix-C)。root cleanup の**前**に行う (先に root を閉じると
                # 相乗り先が空になった space workspace が auto-close し移送先 tab が消える)。
                # per-space split (§8) は agent.start では無視されるので pane.move へ渡す。
                pane_id, terminal_id = self._reconcile_placement(
                    agent, pane_id, sp, split
                )
                # 実配置を registry に記録 (liveness-tracking)。移送済みなので owned
                # workspace/tab に居る。
                with self._lock:
                    self._owned_panes[str(pane_id)] = _PaneRecord(
                        pane_id=str(pane_id),
                        space_key=space_key,
                        workspace_id=sp.workspace_id,
                        tab_id=sp.tab_id,
                        terminal_id=terminal_id,
                        spawned_at=time.time(),
                    )
                # workspace.create が同時生成した root shell pane を後始末する (§7.4)。
                # ここに来た時点で agent は space.workspace_id に居る (直着地 or 移送済み)
                # ので、root を閉じても space は auto-close しない (実配置検証ゲート充足)。
                # 判定は _root_pane 有無で行う (transient 失敗で root が残っても次 spawn で
                # 確実に閉じる)。cleanup なので失敗は無視。
                if sp.root_pane_id is not None:
                    try:
                        self._client.request(
                            "pane.close", {"pane_id": sp.root_pane_id}
                        )
                    except HerdrError:
                        pass
                    sp.root_pane_id = None
                return PaneRef(
                    pane_id=pane_id,
                    window_id=sp.workspace_id,
                    tab_id=sp.tab_id,
                    terminal_id=terminal_id,
                )
            except BaseException:
                failed = True
                raise
            finally:
                with self._lock:
                    n = self._spawn_inflight.get(space_key, 0) - 1
                    if n <= 0:
                        self._spawn_inflight.pop(space_key, None)
                    else:
                        self._spawn_inflight[space_key] = n
                # spawn 失敗で、この呼出が作った space が born-empty (root だけ / agent が
                # foreign に流れて自 workspace が空) なら掃除する (§7.4 の misplaced-W)。
                # in-flight は上で減算済み・_spawn_lock 保持中なので _locked=True で直接呼ぶ。
                if failed and created_now:
                    try:
                        self._sweep_if_empty(
                            space_key, immediate=True, _locked=True
                        )
                    except HerdrError:
                        pass

    # ------------------------------------------------------- placement (Fix-C)
    @staticmethod
    def _workspace_of(pane_id: Any) -> Any:
        """pane_id (``wN:pM``) から workspace prefix (``wN``) を取り出す。"""
        if isinstance(pane_id, str) and ":" in pane_id:
            return pane_id.split(":", 1)[0]
        return pane_id

    def _reconcile_placement(
        self, agent: dict, pane_id: Any, space: _Space, split: str = "down"
    ) -> tuple[Any, Any]:
        """agent.start の実着地を検証し、space の workspace とずれていれば移送する (Fix-C)。

        Herdr 0.7.1 は ``agent.start`` の ``workspace`` / ``tab`` を無視し focused
        workspace へ相乗り配置する (probe 6a)。本 helper は着地 workspace を応答から検証
        (別 RPC 不要) し、space の workspace と DIVERGED した時**のみ** ``pane.move`` で
        space の tab へ移送する (probe 6c: cross-workspace move 可、pane_id は変わるが
        terminal_id 保存)。移送先 tab は明示なので focused 非依存で決定的。

        ``split`` は per-space 分割方向 (§8)。戦略 C では agent.start の split は無視され
        (probe 6a/6d) 実配置は本 pane.move が行うため、per-space 方向はここに渡して効かせる。

        **self-ownership ゲート (§7.3、最重要不変条件)**: 着地先 (foreign = 人間 / 他 org
        かもしれない) を close-authority owned set に**決して加えない**。移送で自 space へ
        引き取り、失敗したら取り残し pane を best-effort close して元エラーを透過し spawn を
        失敗させる (isolation を破った PaneRef を決して返さない、foreign workspace は
        決して close しない)。

        冪等性: 将来 Herdr が workspace param を honor し直着地したら ``landed == space`` で
        move しない (二重移送を防ぐ)。返り値 ``(pane_id, terminal_id)``: 移送時は post-move
        の id、しない時は着地時の id。
        """
        terminal_id = agent.get("terminal_id")
        landed_ws = agent.get("workspace_id") or self._workspace_of(pane_id)
        if landed_ws == space.workspace_id:
            return pane_id, terminal_id
        # DIVERGED: focused workspace (foreign) に相乗り。self space の tab へ移送。
        try:
            res = self._client.request(
                "pane.move",
                {
                    "pane_id": pane_id,
                    "destination": {
                        "type": "tab",
                        "tab_id": space.tab_id,
                        "split": split,
                    },
                },
            )
        except HerdrError:
            # 移送失敗: 取り残し pane を best-effort close (foreign ws の孤児を残さない、
            # かつ foreign workspace は決して close しない) してから元エラーを透過する。
            try:
                self._client.request("pane.close", {"pane_id": pane_id})
            except HerdrError:
                pass
            raise
        moved = (res.get("move_result") or {}).get("pane") or {}
        new_pane_id = moved.get("pane_id")
        if new_pane_id is None:
            raise HerdrError(
                CODE_INTERNAL,
                f"herdr pane.move: response missing post-move pane_id: {res!r}",
            )
        return new_pane_id, moved.get("terminal_id", terminal_id)

    # ------------------------------------------------------------------ list
    def list_panes(self) -> list[dict]:
        """org 所有 workspace 集合の owned pane を geometry 付きで列挙する (§4.1)。

        liveness-tracking set の各 workspace を ``pane.list {workspace_id}`` で問い合わせて
        union し、**一次フィルタ ``pane_id ∈ 自 registry``** を課す。owned workspace では
        さらに adapter-managed tab_id を要求する (per-workspace single-tab 不変条件)。
        ある workspace が DEGRADED (``workspace_not_found``) でも他 workspace の pane を
        空にしない (§4.2)。socket 不通 (adapter_unavailable) は例外のまま上げる。
        """
        # 問い合わせ対象 workspace とフィルタ材料を lock 下でスナップショット (I/O は lock 外)。
        with self._lock:
            owned_ws = {
                s.workspace_id: s
                for s in self._spaces.values()
                if s.state in (WS_LIVE, WS_DEGRADED)
            }
            # registry の実配置 workspace も含める (foreign 着地の追跡先。戦略 C の steady
            # state では owned と一致するが、防御的に union を取る)。
            query_ws = set(owned_ws)
            pane_ws = {
                r.workspace_id for r in self._owned_panes.values()
            }
            query_ws |= pane_ws
            registry = dict(self._owned_panes)
        if not query_ws:
            # owned space も pane も残っていなくても、pending-sweep (close 失敗で owned 外へ
            # 退避した空 workspace) は世代内で回収する必要がある (Codex P2: single-project
            # セッションで最後の space が空 + close 失敗だと query_ws が空になり、この early
            # return が retry をスキップして次 boot まで孤児が残る)。
            self._retry_pending_sweep()
            return []
        collected: list[dict] = []
        degraded_now: list[str] = []
        recovered: list[str] = []
        for wid in query_ws:
            try:
                res = self._client.request("pane.list", {"workspace_id": wid})
            except HerdrError as exc:
                if (
                    exc.code == CODE_PANE_NOT_FOUND
                    and exc.raw == "workspace_not_found"
                ):
                    # 単一 workspace の一時喪失。DEGRADED にするが集合から clear しない
                    # (§4.2: 現行 clear 挙動を supersede、自動 recreate しない)。他 workspace
                    # の pane 集合は空にしない。
                    if wid in owned_ws:
                        degraded_now.append(wid)
                    continue
                raise  # adapter_unavailable 等は透過 (pane_exists が誤読しないため)
            recovered.append(wid)
            for p in res.get("panes") or []:
                collected.append(p)
        # フィルタ: 一次ゲート = registry pane_id。owned workspace は adapter-managed tab を
        # 追加要求 (foreign は tab フィルタを課さない — §4.1)。
        filtered: list[dict] = []
        for p in collected:
            pid = str(p.get("pane_id"))
            rec = registry.get(pid)
            if rec is None:
                continue  # 一次ゲート: 自 registry 外は通さない
            p_ws = p.get("workspace_id")
            sp = owned_ws.get(p_ws)
            if sp is not None and sp.tab_id:
                # owned workspace で adapter-managed tab が判明している時のみ、その tab の
                # pane に絞る (single-tab 不変条件)。tab_id 未判明の space (§5.3 step 4 で
                # tab を確定できず adopt した稀な回復ケース) は tab フィルタを課さない —
                # さもないと adopt した全 pane を弾いて false-missing になる。
                if p.get("tab_id") is not None and p.get("tab_id") != sp.tab_id:
                    continue
            filtered.append(p)
        # workspace 単位状態の更新 (lock 下、I/O なし)。
        self._update_ws_states(degraded_now, recovered)
        # geometry は workspace (= tab) 単位。workspace ごとに anchor を 1 つ選んで layout。
        out = self._build_pane_view(filtered)
        # DEGRADED の有界脱出 (§4.2): しきい値超で workspace.list 突き合わせ → GONE 収束。
        self._escape_degraded()
        # 空プロジェクトスペースの掃除 retry (§4.3): grace で見送った空スペースを回収する。
        self._sweep_empty_project_spaces()
        # close 失敗で pending に退避した空 workspace の workspace.close 再試行 (§4.3)。
        self._retry_pending_sweep()
        return out

    def _update_ws_states(
        self, degraded: list[str], recovered: list[str]
    ) -> None:
        now = time.time()
        with self._lock:
            by_ws = {s.workspace_id: s for s in self._spaces.values()}
            for wid in recovered:
                sp = by_ws.get(wid)
                if sp is not None and sp.state == WS_DEGRADED:
                    sp.state = WS_LIVE
                    sp.missing_since = None
                    sp.missing_count = 0
            for wid in degraded:
                sp = by_ws.get(wid)
                if sp is None:
                    continue
                sp.state = WS_DEGRADED
                sp.missing_count += 1
                if sp.missing_since is None:
                    sp.missing_since = now

    def _escape_degraded(self) -> None:
        """DEGRADED がしきい値超なら workspace.list で GONE を確定させる (§4.2)。

        放置すると「恒久的に消えた workspace の pane を永久 defer する false-liveness leak」
        (§4.2)。workspace.list に現れない DEGRADED workspace は GONE として集合から外し、
        その pane を broker の reap へ解放する (pane_liveness が pane.get で GONE を返す)。
        """
        now = time.time()
        with self._lock:
            suspects = [
                s for s in self._spaces.values()
                if s.state == WS_DEGRADED
                and (
                    s.missing_count >= self.degraded_max_misses
                    or (
                        s.missing_since is not None
                        and now - s.missing_since >= self.degraded_max_seconds
                    )
                )
            ]
        if not suspects:
            return
        try:
            res = self._client.request("workspace.list", {})
        except HerdrError:
            return  # 判定不能: 次ラウンドに委ねる (安全側)
        present = {w.get("workspace_id") for w in (res.get("workspaces") or [])}
        for sp in suspects:
            if sp.workspace_id not in present:
                # 恒久喪失確定 → GONE。集合から外し pane を解放する。
                self._drop_space(sp.space_key, WS_GONE)

    def _build_pane_view(self, panes: list[dict]) -> list[dict]:
        """フィルタ済み pane 群を broker の list_panes_view が読む dict へ整形する。

        geometry は workspace (= tab) 単位なので workspace ごとに anchor を選んで
        pane.layout を 1 回引く。返す dict は現行同様の key + workspace_id / tab_id。
        """
        by_ws: dict[Any, list[dict]] = {}
        for p in panes:
            by_ws.setdefault(p.get("workspace_id"), []).append(p)
        out: list[dict] = []
        for wid, group in by_ws.items():
            geom, focused_id = self._layout_geometry(group)
            for p in group:
                pid = p.get("pane_id")
                rect = geom.get(pid, {})
                out.append(
                    {
                        "pane_id": pid,
                        "window_id": p.get("workspace_id", wid),
                        "tab_id": p.get("tab_id"),
                        "x": int(rect.get("x", 0)),
                        "y": int(rect.get("y", 0)),
                        "width": int(rect.get("width", 0)),
                        "height": int(rect.get("height", 0)),
                        "active": bool(
                            p.get(
                                "active",
                                focused_id is not None and pid == focused_id,
                            )
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
        """pane.layout から pane_id → cell rect と focused_pane_id を得る (単一 tab 分)。

        layout は tab 全体の pane rectangles を一度に返す (spike 窓口補足2)。取得不能は
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

    def pane_liveness(
        self, pane_id: str, terminal_id: str | None = None
    ) -> str:
        """workspace 非依存に pane の生存を **権威判定** する (Issue #114 Fix-D)。

        :meth:`list_panes` / :meth:`pane_exists` は owned workspace filter 越しの liveness
        で、placement バグや workspace 消失で生 pane を構造的に欠落させる。本メソッドは
        workspace を指定しない ``pane.get(pane_id)`` で pane の実在を直接引き、spawn 時に
        記録した terminal_id と照合する:
          - present かつ terminal_id 一致 -> :data:`PANE_LIVE_ALIVE`。
          - present だが terminal_id 不一致 -> :data:`PANE_LIVE_REUSED` (id 再利用。**その
            pane_id を close してはならない**)。
          - ``pane_not_found`` -> :data:`PANE_LIVE_GONE` (権威的に消滅)。
          - socket 不通等 -> :data:`PANE_LIVE_UNKNOWN` (判定不能、reaper は defer)。

        **GONE** は registry から除去し、その space が空になったプロジェクトスペースなら掃除する
        (§4.3 の poll 経路 ephemeral cleanup)。**REUSED** は registry entry を落とすが **掃除は
        しない** — その pane_id は別プロセスの pane を指すため workspace.close で巻き添える
        (Codex P2、REUSED の「触るな」不変条件)。
        """
        try:
            res = self._client.request("pane.get", {"pane_id": pane_id})
        except HerdrError as exc:
            if exc.code == CODE_PANE_NOT_FOUND:
                self._forget_pane(pane_id)
                return PANE_LIVE_GONE
            return PANE_LIVE_UNKNOWN
        pane = res.get("pane") or {}
        got_tid = pane.get("terminal_id")
        if terminal_id is not None and got_tid is not None and got_tid != terminal_id:
            # REUSED: pane_id は今や別プロセスの pane を指す。bookkeeping だけ落とし、
            # workspace は掃除しない (sweep=False) — workspace.close するとその再利用先 pane を
            # 巻き添えに殺す = REUSED の「触るな」不変条件 / isolation 違反 (Codex P2)。
            self._forget_pane(pane_id, sweep=False)
            return PANE_LIVE_REUSED
        return PANE_LIVE_ALIVE

    def _forget_pane(self, pane_id: str, *, sweep: bool = True) -> None:
        """registry から pane を除去し、``sweep`` なら空プロジェクトスペースを即掃除する。

        pane が実際に除去された = space が真に空で workspace は auto-close 済みのことが多い
        ので grace を飛ばす (immediate、Codex P2: grace で LIVE 残置すると respawn が死んだ
        workspace を再利用する)。``sweep=False`` は REUSED 経路用 — pane_id は別 pane に再利用
        されており workspace.close で巻き添え close しないため掃除を抑止する。
        """
        with self._lock:
            rec = self._owned_panes.pop(str(pane_id), None)
        if rec is not None and sweep:
            self._sweep_if_empty(rec.space_key, immediate=True)

    # -------------------------------------------------------------- get-text
    def get_text(self, pane_id: str, escapes: bool = False) -> str:
        """pane の画面テキストを取得する (grid scrape)。

        spike §項目3: ライブ画面は ``source=visible``。``escapes=True`` は ``format=ansi``。
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
        """未送信で入力欄に置く (submit しない)。"""
        self._client.request("pane.send_text", {"pane_id": pane_id, "text": text})

    def send_enter(self, pane_id: str) -> None:
        """Enter 1 打 (承認プロンプト機械承認 / submit)。"""
        self._client.request("pane.send_keys", {"pane_id": pane_id, "keys": ["enter"]})

    def send_interrupt(self, pane_id: str) -> None:
        """Ctrl+C 1 打 (入力欄クリア / SIGINT)。spike §項目2 で実中断を確認済み。"""
        self._client.request(
            "pane.send_keys", {"pane_id": pane_id, "keys": ["ctrl+c"]}
        )

    def send_named_keys(self, pane_id: str, keys: Sequence[str]) -> None:
        """canonical キー列を Herdr token へ写像し 1 回の pane.send_keys で batch 送出。"""
        if not keys:
            return
        tokens = [_HERDR_KEY_MAP[k] for k in keys]  # 未知 canonical は KeyError
        self._client.request("pane.send_keys", {"pane_id": pane_id, "keys": tokens})

    def send_line(self, pane_id: str, text: str, settle: float = 0.15) -> None:
        """1 行送出 + Enter (ナッジ注入の正準形)。"""
        self.type_text(pane_id, text)
        time.sleep(settle)
        self.send_enter(pane_id)

    # ------------------------------------------------------------------ kill
    def kill_pane(self, pane_id: str) -> None:
        """spawn した pane を閉じる (fire-and-forget)。

        通常は ``pane.close``。tab に残る **唯一の pane** の close 拒否時は、その pane が
        居る workspace ごと閉じて確実に reap する (§4.1: close-authority owned set の
        メンバのみ)。既に消えている / socket 不通等の best-effort 断念は握り潰す。
        """
        self._close_pane(pane_id)

    def kill_pane_detailed(self, pane_id: str) -> dict:
        """:meth:`kill_pane` と同じ close を行い、経路と残存を dict で返す。

        broker の reap 物理 close 検証が journal できるようにするための拡張。close 後に
        ``pane_exists`` で残存を再確認して ``still_present`` に載せる。
        """
        result = self._close_pane(pane_id)
        try:
            result["still_present"] = self.pane_exists(pane_id)
        except HerdrError:
            result["still_present"] = None
        return result

    def _close_pane(self, pane_id: str) -> dict:
        """pane.close (+ 自 workspace 内で唯一 pane なら workspace.close fallback) を実行。

        :meth:`kill_pane` / :meth:`kill_pane_detailed` の共通コア。sole-pane 判定は
        **その pane が居る workspace 内**で行う (multi-workspace では他 workspace の pane を
        含めた全体 remaining では sole 判定が成立しないため、§4.1 の集合化に合わせる)。
        close 成功 / workspace.close fallback で registry から除去する。
        """
        result: dict[str, Any] = {"closed_via": None, "still_present": None, "raw": None}
        target_ws = self._workspace_for_pane(pane_id)
        try:
            self._client.request("pane.close", {"pane_id": pane_id})
            result["closed_via"] = "pane.close"
            self._forget_pane(pane_id)
        except HerdrError as exc:
            result["raw"] = exc.raw or exc.code
            # pane.close が「不在」を返したなら pane は既に消滅と確定 (close error が権威)。
            if exc.code == CODE_PANE_NOT_FOUND:
                result["closed_via"] = "already_gone"
                self._forget_pane(pane_id)
                return result
            # それ以外の close 拒否 (single_pane 等) は「pane が存在する」証拠。sole-pane を
            # **自 workspace 内で** 確証できた時のみ workspace ごと閉じる。lag で空 / 他 pane
            # ありの時は already_gone と断じず refused にして呼び元 (broker) が defer する。
            #
            # sole-pane 判定は **raw pane.list (registry フィルタなし)** で行う: workspace.close
            # は workspace 内の全 pane を巻き込むため、非 registry pane (別 managed pane /
            # 外部 pane / 未掃除 root) が 1 つでも居れば close してはならない。registry
            # フィルタ済みの list_panes を使うと非 registry pane を見落として巻き添え close
            # する (§4.1)。target_ws 不明時は sole を確証できないので refused。
            if target_ws is None:
                result["closed_via"] = "refused"
                return result
            # sole-pane 判定〜workspace.close を **``_spawn_lock`` 下**で行い、判定と close の
            # 間に同 workspace へ spawn が pane を足す race を断つ (§4.3、_sweep_if_empty と同じ
            # 規律)。_close_pane は kill 経路 (spawn_lock 非保持) からのみ呼ばれるので安全に取れる。
            with self._spawn_lock:
                try:
                    res = self._client.request(
                        "pane.list", {"workspace_id": target_ws}
                    )
                except HerdrError:
                    result["closed_via"] = "list_failed"
                    return result
                remaining = [
                    p.get("pane_id")
                    for p in (res.get("panes") or [])
                    if p.get("workspace_id") in (target_ws, None)
                ]
                if remaining == [pane_id]:
                    if self.close_workspace(target_ws):
                        result["closed_via"] = "workspace.close"
                    else:
                        result["closed_via"] = "workspace_close_failed"
                else:
                    result["closed_via"] = "refused"
        return result

    def _workspace_for_pane(self, pane_id: str) -> Any:
        """pane_id の所属 workspace を registry から引く (無ければ prefix から推定)。"""
        with self._lock:
            rec = self._owned_panes.get(str(pane_id))
        if rec is not None:
            return rec.workspace_id
        return self._workspace_of(pane_id)

    # ------------------------------------------------------- space cleanup
    def _sweep_if_empty(
        self, space_key: str, *, immediate: bool = False, _locked: bool = False
    ) -> None:
        """プロジェクトスペースが空になったら workspace.close で掃除する (§4.3)。

        control スペースは org ライフタイムと同寿命なので掃除しない (§4.3、一時的に空でも
        残す)。in-flight spawn がある / まだ owned pane が居るスペースはスキップ (§4.3 の
        in-flight ガード)。

        ``immediate=True`` は grace を飛ばして即掃除する。**pane が実際に除去された経路**
        (`_forget_pane` = org 主導 close / self-exit、§4.3 空検知 (a)(b)) と spawn 失敗の
        born-empty (§7.4) で使う: これらは space が真に空で、かつ最後の pane 退出で Herdr が
        workspace を auto-close 済みのことが多い。grace で LIVE のまま残すと、直後の同 slug
        respawn が `_ensure_space` で **死んだ workspace/tab を LIVE として再利用**し move/start
        が失敗する (Codex P2)。grace は periodic な `_sweep_empty_project_spaces` の安全網でのみ
        効かせ、pane 除去起点の即時掃除では飛ばす。

        掃除と spawn の race を断つため **``_spawn_lock`` 下で判定〜close** する
        (``_locked=False`` の呼出は lock を取得する)。spawn の失敗経路は既に
        ``_spawn_lock`` を保持しているので ``_locked=True`` で直接呼ぶ。
        """
        if space_key == SPACE_CONTROL:
            return
        if not _locked:
            # spawn と相互排他にして「掃除中に同 space へ spawn が pane を足す」race を断つ。
            with self._spawn_lock:
                self._sweep_if_empty(
                    space_key, immediate=immediate, _locked=True
                )
            return
        with self._lock:
            sp = self._spaces.get(space_key)
            if sp is None:
                return
            if self._spawn_inflight.get(space_key, 0) > 0:
                return  # spawn in-flight: 掃除抑止
            has_pane = any(
                r.space_key == space_key for r in self._owned_panes.values()
            )
            if has_pane:
                return
            if not immediate:
                if time.time() - sp.created_at < self.space_sweep_grace_seconds:
                    return  # grace 未達: _sweep_empty_project_spaces の retry で後回収
        # workspace.close (lock 外 I/O)。成功で SWEPT、失敗は pending_sweep で再試行。
        self._sweep_space(space_key)

    def _sweep_empty_project_spaces(self) -> None:
        """空になったプロジェクトスペースを掃除する (list_panes からの retry、§4.3)。

        grace で即掃除が見送られた空スペースを次 poll で回収する (leak 防止)。control は
        除外 (org ライフタイムと同寿命)。
        """
        with self._lock:
            candidates = [
                k for k, s in self._spaces.items()
                if k != SPACE_CONTROL
                and self._spawn_inflight.get(k, 0) == 0
                and not any(
                    r.space_key == k for r in self._owned_panes.values()
                )
            ]
        for key in candidates:
            self._sweep_if_empty(key)

    def _own_pane_ids(self, sp: _Space) -> set[str]:
        """space が閉じてよい **自分の pane_id 集合** (space の root pane + 自 registry pane)。

        workspace.close の占有判定で「自分の pane」を除外するために使う (§7.4 の born-empty:
        spawn 失敗で workspace.create の root pane だけが残るケースを foreign と誤判定しないため、
        Codex 確認ラウンド P2)。root pane は自作成 workspace の付属物で workspace ごと閉じてよい。
        """
        ids: set[str] = set()
        if sp.root_pane_id is not None:
            ids.add(str(sp.root_pane_id))
        with self._lock:
            ids.update(
                str(p) for p, r in self._owned_panes.items()
                if r.workspace_id == sp.workspace_id
            )
        return ids

    def _close_workspace_if_empty(
        self, workspace_id: str, own_pane_ids: "frozenset[str] | set[str]" = frozenset()
    ) -> str:
        """workspace が **非 owned pane 皆無**の時**だけ** workspace.close する (§4.1 isolation の要)。

        workspace.close は workspace 内の全 pane を巻き込むため、**非 owned pane** (REUSED で
        pane_id を引き継いだ別プロセス / 人間 / 外部が move-in した pane) が 1 つでも居る間は
        絶対に close しない (巻き添え kill = isolation 違反、Codex P1/P2)。ただし ``own_pane_ids``
        (= space の root pane + 自 registry pane) は「自分のもの」で workspace ごと閉じてよいので
        **占有に数えない** (Codex 確認ラウンド P2: born-empty の自 root pane を foreign と誤判定して
        leak させない)。sweep も pending 再試行もこの単一関門を通す。返り値:
          - ``"closed"``    非 owned pane 皆無で workspace.close 成功。
          - ``"gone"``      pane.list / workspace.close が not_found = 既に auto-close 済み。
          - ``"occupied"``  非 owned pane が居る → close せず (relinquish/defer 判断は呼び元)。
          - ``"close_failed"`` 空だが workspace.close が失敗 (transient/refused) → 再試行対象。
          - ``"unknown"``   pane.list が backend 不通等で確認不能 → defer (再試行)。
        """
        try:
            res = self._client.request("pane.list", {"workspace_id": workspace_id})
        except HerdrError as exc:
            if exc.code == CODE_PANE_NOT_FOUND:
                return "gone"
            return "unknown"
        # 自分の pane (root + 自 registry) は占有に数えない。非 owned pane が 1 つでも居る時のみ occupied。
        foreign = [
            p for p in (res.get("panes") or [])
            if str(p.get("pane_id")) not in own_pane_ids
        ]
        if foreign:
            return "occupied"
        try:
            self._client.request(
                "workspace.close", {"workspace_id": workspace_id}
            )
        except HerdrError as exc:
            if exc.code == CODE_PANE_NOT_FOUND:
                return "gone"
            return "close_failed"
        return "closed"

    def _sweep_space(self, space_key: str) -> None:
        with self._lock:
            sp = self._spaces.get(space_key)
        if sp is None:
            return
        status = self._close_workspace_if_empty(
            sp.workspace_id, self._own_pane_ids(sp)
        )
        if status in ("closed", "gone"):
            self._drop_space(space_key, WS_SWEPT)
        elif status in ("occupied", "close_failed"):
            # **owned set から外して** pending_sweep (workspace_id キー) に退避し再試行する (§4.3)。
            #  - close_failed: 空だが workspace.close が transient 失敗 → 再試行で回収。
            #  - occupied: pane.list が非空。「非 owned pane (reused/foreign)」か「eventually
            #    consistent な pane.list が直前に閉じた自 pane をまだ見せている lag」かを区別
            #    できない (Herdr pane.list は eventual consistent、§真因B)。permanently relinquish
            #    すると lag の場合に snapshot 追いつき後に閉じ損ねて leak する (Codex P2)。よって
            #    defer し、空を確証できるまで再試行する — foreign なら閉じないまま defer が続き
            #    isolation を保つ (workspace.close は _close_workspace_if_empty が空を確証した時のみ)。
            # owned set に SWEPT のまま残すと同一 space_key への re-spawn が _ensure_space でこの
            # エントリを上書きし世代内孤児になる (adversarial review MAJOR) ため必ず外す。
            with self._lock:
                sp2 = self._spaces.pop(space_key, None)
                if sp2 is not None:
                    sp2.state = WS_SWEPT
                    self._pending_sweep[sp2.workspace_id] = sp2
                    for pid in [
                        p for p, r in self._owned_panes.items()
                        if r.workspace_id == sp2.workspace_id
                    ]:
                        self._owned_panes.pop(pid, None)
        # status == "unknown": defer (何もしない、次ラウンドで再試行)。

    def _retry_pending_sweep(self) -> None:
        """close 失敗で pending に残った workspace の workspace.close を再試行する (§4.3)。

        list_panes / close_workspace(None) から呼ばれ、掃除失敗した空 workspace を **同一
        世代内で** 回収する (起動時 sweep は gen < current のみ対象で次 boot まで待つため)。
        **再試行も物理的な空を確認してから close する** (Codex P2): pending 中に人間 / 別プロセスが
        その workspace へ pane を作成 / 移送しうるため、無条件 close だと非 owned pane を巻き添える。
        closed / gone (回収完了) でのみ pending から外す。occupied (lag or foreign) /
        close_failed / unknown は保持して次ラウンドで再試行する (lag なら追いつき後に closed、
        foreign なら閉じないまま defer が続き isolation を保つ)。
        """
        with self._lock:
            pending = list(self._pending_sweep.values())
        for sp in pending:
            status = self._close_workspace_if_empty(
                sp.workspace_id, self._own_pane_ids(sp)
            )
            if status in ("closed", "gone"):
                with self._lock:
                    self._pending_sweep.pop(sp.workspace_id, None)

    def close_workspace(self, workspace_id: str | None = None) -> bool:
        """workspace を後始末する (§4.1: close-authority owned set のメンバのみ)。

        ``workspace_id=None`` は **close-authority owned set の全 workspace** を閉じる
        (org down、§4.1)。全て成功で ``True``、1 つでも失敗で ``False`` (失敗した space は
        pending_sweep に残す)。``workspace_id`` 指定はその 1 つだけを閉じる (sole-pane
        fallback / 空スペース掃除)。**owned set 外の workspace は決して閉じない**
        (foreign / 他 org の巻き添えを防ぐ self-ownership ゲート)。冪等: 該当なしは ``True``。
        """
        if workspace_id is None:
            with self._lock:
                keys = list(self._spaces.keys())
            ok = True
            for key in keys:
                with self._lock:
                    sp = self._spaces.get(key)
                if sp is None:
                    continue
                if not self._close_owned_workspace(sp.workspace_id):
                    ok = False
            # org down では pending-sweep (掃除失敗で owned 外に退避した空 workspace) も
            # 世代内で回収する (§4.3)。閉じ切れなければ ok=False。
            self._retry_pending_sweep()
            with self._lock:
                if self._pending_sweep:
                    ok = False
            return ok
        # 指定 workspace_id が owned set のメンバか (self-ownership ゲート)。
        with self._lock:
            member = any(
                s.workspace_id == workspace_id for s in self._spaces.values()
            )
        if not member:
            return False  # owned 外は閉じない
        return self._close_owned_workspace(workspace_id)

    def _close_owned_workspace(self, workspace_id: str) -> bool:
        """owned workspace を 1 つ閉じ、成功時に _spaces / registry から除去する。

        失敗 (HerdrError) は ``False`` を返し state を **保持** して次ラウンド再試行可能に
        する (成功を偽装しない、Codex round3 P2)。``workspace_not_found`` は既に消えている
        = 掃除完了とみなし ``True`` + 除去する。
        """
        try:
            self._client.request(
                "workspace.close", {"workspace_id": workspace_id}
            )
        except HerdrError as exc:
            if exc.code == CODE_PANE_NOT_FOUND:  # 既に消滅 = 掃除完了
                self._drop_workspace_bookkeeping(workspace_id, WS_SWEPT)
                return True
            return False
        self._drop_workspace_bookkeeping(workspace_id, WS_SWEPT)
        return True

    def _drop_workspace_bookkeeping(self, workspace_id: str, state: str) -> None:
        """workspace_id に対応する _Space / owned pane / pending_sweep を除去する。"""
        with self._lock:
            keys = [
                k for k, s in self._spaces.items()
                if s.workspace_id == workspace_id
            ]
            for k in keys:
                sp = self._spaces.pop(k, None)
                if sp is not None:
                    sp.state = state
            self._pending_sweep.pop(workspace_id, None)  # pending は workspace_id キー
            for pid in [
                p for p, r in self._owned_panes.items()
                if r.workspace_id == workspace_id
            ]:
                self._owned_panes.pop(pid, None)

    # ---------------------------------------------------- startup stale sweep
    def _acquire_sweep_lock(self) -> bool:
        """single-live-daemon lock を取得する (§5.3)。

        launcher が state_dir ごとの二重起動を既に防ぐが、rolling / overlapping restart
        への防御として lock ファイルの pid を確認する。生存する別 daemon が保持していれば
        sweep を保留する (自己判断で旧世代を掃除しない)。取得できたら自 pid を書く。
        """
        if not self.state_dir:
            return False
        path = os.path.join(self.state_dir, _SWEEP_LOCK_FILE)
        try:
            with open(path, encoding="utf-8") as f:
                prev = int(f.read().strip() or "0")
        except (OSError, ValueError):
            prev = 0
        if prev and prev != os.getpid() and _pid_alive(prev):
            return False  # 生存する別 daemon が保持: 保留
        try:
            _atomic_write(path, str(os.getpid()))
        except OSError:
            return False
        return True

    def _startup_sweep(self) -> None:
        """daemon boot 時の旧世代 workspace 一括掃除 (§5.3)。

        1. workspace.list で全ラベル取得。
        2. ``{prefix}/{org_instance_id}/`` 前方一致で自 org を抽出 (別 org は絶対に触れない)。
        3. ``generation < 現 generation`` を旧世代孤児として workspace.close (§109 の掃除)。
        4. ``generation == 現 generation`` は clean boot では write-ahead 永続化で通常存在
           しないが、存在すれば suspect: live pane を持つものだけ adopt、空 / 死は掃除。
        """
        if not self._acquire_sweep_lock():
            return  # 旧 daemon 生存: 保留 (窓口エスカレーションは broker 側責務)
        prefix_oid = f"{self.label_prefix}/{self.org_instance_id}/"
        res = self._client.request("workspace.list", {})
        for w in res.get("workspaces") or []:
            wid = w.get("workspace_id")
            label = w.get("label") or ""
            gen, space_key = _parse_label(prefix_oid, label)
            if wid is None or gen is None or space_key is None:
                continue  # 別 org / prefix 無し / 不正ラベル: 絶対に触れない (§5.3 step 2)
            if gen < self.generation:
                self._sweep_old_generation(wid)
            elif gen == self.generation:
                self._adopt_or_sweep_current(wid, w, space_key)

    def _sweep_old_generation(self, workspace_id: str) -> None:
        """旧世代孤児 workspace を掃除する (§5.3 step 3、#109 の主目的)。

        single-live-daemon lock で旧 daemon の死は確認済み (§5.3 の primary guard / §14 の
        load-bearing 残存リスク)。旧世代 orphan は死んだ daemon の pane (root + 孤児 agent) を
        含むのが常態で、**それごと reap するのが本 sweep の主目的** (#109: 世代共有・孤児堆積の
        回収)。よって旧世代 workspace は **無条件に workspace.close** する。防御は「予期せぬ
        **現世代** pane が混ざっていないか」(= 自 registry の pane が居ないか) の確認に限定する。

        **設計裁定 (人間) の経緯**: 一時 round 6 で本経路を `_close_workspace_if_empty` (物理的
        空の関門) に通したが、旧世代 orphan の pane を全て「非 owned 占有」と誤判定し #109 /
        §5 の主目的 (pane ごと orphan 掃除) を壊した。round 6 指摘 (occupied 保護) は **現世代の
        ephemeral 掃除経路** (`_sweep_space` / `_retry_pending_sweep`) 限定で正しく、old-gen 経路
        への適用は過剰一般化だったため、人間裁定で §5.3 準拠 (無条件 close + 現世代混在チェックのみ)
        へ復帰した。現世代掃除経路は引き続き `_close_workspace_if_empty` で foreign を保護する。

        defer 意味論: 失敗は握り潰す (次 boot / 次ラウンドで再試行、成功を偽装しない)。
        """
        with self._lock:
            has_own = any(
                r.workspace_id == workspace_id for r in self._owned_panes.values()
            )
        if has_own:
            # 防御: 自 registry の pane が居る = **現世代** pane が混ざっている疑い → close しない
            # (§5.3 step 3。live 現世代 pane を殺さない)。boot 直後は registry 空なので通常は素通り。
            return
        try:
            self._client.request(
                "workspace.close", {"workspace_id": workspace_id}
            )
        except HerdrError:
            pass  # 失敗は次 boot に委ねる (成功を偽装しない)

    def _adopt_or_sweep_current(
        self, workspace_id: str, ws: dict, space_key: str
    ) -> None:
        """現世代ラベルの suspect 処理 (§5.3 step 4)。

        clean boot では write-ahead により通常存在しない。存在すれば crash mid-spawn 等の
        残骸なので **無条件 adopt せず**、live pane を持つものだけ _spaces へ adopt、空 / 死は
        掃除する。同一 (gen, space_key) が既に adopt 済みなら tie-break で本 workspace を掃除
        (live pane を持つ 1 つだけ adopt)。
        """
        try:
            res = self._client.request(
                "pane.list", {"workspace_id": workspace_id}
            )
            panes = res.get("panes") or []
        except HerdrError:
            panes = []
        with self._lock:
            already = space_key in self._spaces
        if not panes or already:
            # 空 / 死、または同 space_key を既に adopt 済み (tie-break) → 掃除。
            try:
                self._client.request(
                    "workspace.close", {"workspace_id": workspace_id}
                )
            except HerdrError:
                pass
            return
        # live pane を持つ現世代ラベル → adopt。tab_id は workspace.list の active_tab_id が
        # あれば使い、無ければ pane の tab_id、いずれも無ければ None (owned-tab フィルタは
        # skip される。稀な crash 回復経路の best-effort)。
        tab_id = ws.get("active_tab_id") or (panes[0].get("tab_id") if panes else None)
        with self._lock:
            self._spaces[space_key] = _Space(
                space_key=space_key,
                workspace_id=workspace_id,
                tab_id=tab_id or "",
                label=self._space_label(space_key),
                root_pane_id=None,
                created_at=time.time(),
            )
            # adopt した live pane を registry にも登録する (Codex P2): list_panes は
            # _owned_panes を一次ゲートにするため、登録しないと adopt した pane が不可視に
            # なり、project スペースは「空」と誤判定されて掃除されてしまう。
            for p in panes:
                pid = p.get("pane_id")
                if pid is None:
                    continue
                self._owned_panes[str(pid)] = _PaneRecord(
                    pane_id=str(pid),
                    space_key=space_key,
                    workspace_id=workspace_id,
                    tab_id=p.get("tab_id") or tab_id,
                    terminal_id=p.get("terminal_id"),
                    spawned_at=time.time(),
                )
