# -*- coding: utf-8 -*-
"""terminal adapter の共有基盤。

設計 SoT: docs/design/ja-migration-plan.md §4 (runtime 抽出設計) /
docs/design/renga-decoupling.md §4.7 (adapter 境界と能力表)。
canonical 実装: claude-org-transport-lab spike/terminal_adapter.py
(Phase 1-5 で検証済。本 subpackage への faithful port)。

Phase 1 (WezTerm / Windows) で確立した adapter 面を backend 非依存に抽象化し、
Phase 2 で tmux (POSIX 正準 backend) を第二実装として追加した。broker / harness は
本モジュールの `TerminalAdapter` 面と `make_adapter()` ファクトリ経由でのみ backend に
触り、WezTerm / tmux のどちらでも同一の AC-1 / AC-2 テストを green にする。

intent レベルの面 (broker / harness が実際に使う最小集合):
  spawn / list_panes / pane_exists / get_text /
  type_text (未送信で置く) / send_enter (確定) / send_line (型+確定) /
  send_interrupt (Ctrl+C) / kill_pane

backend ごとの「打鍵の小細工」の差はここで吸収する:
- WezTerm: send-text 既定が bracketed paste のため、Enter は `--no-paste + CR`、
  未送信テキストは paste で置く、という小細工が要る (確定事項 (1))。
- tmux: send-keys が一級プリミティブ。Enter は `send-keys Enter`、Ctrl+C は
  `send-keys C-c` で素直に出せる。未送信の複数行テキストのみ bracketed paste
  (paste-buffer -p) を使い、改行が submit に化けないようにする。

画面状態ヒューリスティック (classify_pane_state) は受信側の Claude TUI が同一で
あるため backend 非依存。本モジュールに置き、両 adapter から共有する。
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, Union, runtime_checkable

if TYPE_CHECKING:  # 実体は wezterm / tmux (循環 import 回避で遅延)
    pass

# pane 識別子の型。WezTerm は整数 (例 3)、tmux は文字列 (例 "%3")。
# broker / harness は不透明値として扱い、算術や解釈をしない (確定事項 (4) の
# 「全呼出で target を明示する」を backend 横断で守るための単一の出入口)。
PaneId = Union[int, str]

# ナッジ定型 1 行 (docs/design/renga-decoupling.md §4.3)。本文は PTY を通さない。
NUDGE_TEXT = "📨 新着あり。check_messages を実行"


@dataclass
class PaneRef:
    """spawn した pane の追跡情報。毎回 target を明示するために保持する。

    pane_id は backend ネイティブ型 (WezTerm=int / tmux=str)。tab_id / window_id は
    backend での「タブ / ウィンドウ」相当 (tmux では window_id / session を充てる)。
    """

    pane_id: PaneId
    tab_id: PaneId | None = None
    window_id: PaneId | None = None


@runtime_checkable
class TerminalAdapter(Protocol):
    """broker / harness が依存する terminal backend の最小面 (構造的型)。

    WezTermAdapter / TmuxAdapter が本 Protocol を満たす。全メソッドが target
    (pane_id) を明示で受け取り、フォーカス先や環境変数へのフォールバックをしない。

    能力フラグ ``isolated_session`` (bool, ClassVar): backend が「自分が spawn
    した pane だけ」を ``list_panes()`` で見せるか (= dedicated session 分離) を
    表す。tmux (専用 socket -L claude-org-spike) は True (人間の窓口 pane は別
    サーバーにあり出ない)、wezterm (cli list, global mux) は False (窓口の実
    pane も匿名で出る)。broker の close_pane が論理ペイン (人間駆動の窓口) を
    last-pane ガードに +1 計上してよいかの判断に使う (isolated な時だけ窓口は
    adapter の外におり +1 が正当)。本 Protocol は ``@runtime_checkable`` で
    ``issubclass`` 検査に使うため、非メソッド member を**注釈として宣言しない**
    (注釈すると issubclass が TypeError)。concrete adapter が ClassVar として
    持ち、broker は ``getattr(adapter, "isolated_session", False)`` で読む。
    """

    def spawn(
        self, argv: list[str], cwd: str | None = ..., new_window: bool = ...
    ) -> PaneRef: ...

    def list_panes(self) -> list[dict]: ...

    def pane_exists(self, pane_id: PaneId) -> bool: ...

    def get_text(self, pane_id: PaneId, escapes: bool = ...) -> str: ...

    def type_text(self, pane_id: PaneId, text: str) -> None: ...

    def send_enter(self, pane_id: PaneId) -> None: ...

    def send_line(self, pane_id: PaneId, text: str, settle: float = ...) -> None: ...

    def send_interrupt(self, pane_id: PaneId) -> None: ...

    def kill_pane(self, pane_id: PaneId) -> None: ...


# ---------------------------------------------------------------------------
# 画面状態ヒューリスティック (AC-1 自動判定の根拠、backend 非依存)
# ---------------------------------------------------------------------------

# Claude Code TUI が応答生成中に表示する割り込みヒント (busy 判定はこの
# 文字列のみで行う。スピナーグリフは点滅で取りこぼすため判定に使わない)
_BUSY_MARKERS = ("esc to interrupt", "ctrl+c to stop", "esc to cancel")


def classify_pane_state(screen: str) -> str:
    """grid scrape の画面テキストから受信側状態を分類する。

    返り値: "busy" | "input_pending" | "idle" | "unknown"

    受信側の Claude TUI が backend 非依存に同一描画であるため、WezTerm get-text /
    tmux capture-pane のいずれの scrape でも同じ判定ロジックで分類できる
    (Phase 2 で tmux capture-pane に対しても妥当性を実測)。

    実測較正 (claude 2.1.168):
    - idle 時の入力プロンプトは水平罫線に挟まれた "❯ " 行
      (旧バージョンの "│ > │" 枠形式もフォールバックで残す)。
    - 応答生成中は画面下部に "(esc to interrupt)" 等のヒントが出る。

    限界 (spike/manual-ime-test.md にも明記): grid scrape は PTY 内の文字 grid
    のみを観測する。IME の変換窓・候補 UI は OS 側のオーバーレイであり
    ここからは観測できない。よって IME 変換中の判定は自動化対象外。
    """
    lines = [ln.rstrip() for ln in screen.splitlines()]
    # 1) busy: 応答生成中ヒントが画面下部にある
    tail = "\n".join(lines[-20:]).lower()
    if any(m in tail for m in _BUSY_MARKERS):
        return "busy"

    # 2) 入力プロンプト行を下から探す ("❯ ..." / "│ > ... │" / "> ...")
    prompt_content: str | None = None
    for ln in reversed(lines):
        s = ln.strip()
        if s.startswith("❯"):
            prompt_content = s[1:].strip()
            break
        if s.startswith("│") and s.endswith("│") and len(s) > 2:
            inner = s[1:-1].strip()
            if inner.startswith(">"):
                prompt_content = inner[1:].strip()
                break

    if prompt_content is None:
        return "unknown"
    if prompt_content:
        return "input_pending"
    return "idle"


def wait_for_state(
    adapter: TerminalAdapter,
    pane_id: PaneId,
    want: str,
    timeout: float = 30.0,
    interval: float = 1.0,
) -> bool:
    """pane が目的状態になるまで poll。到達で True。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if classify_pane_state(adapter.get_text(pane_id)) == want:
            return True
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# backend ファクトリ
# ---------------------------------------------------------------------------

VALID_BACKENDS = ("wezterm", "tmux")


def default_backend() -> str:
    """実行環境の既定 backend。

    - Windows (native): WezTerm (tmux はネイティブ Windows で動かない)。
    - POSIX (Linux / macOS / WSL2): tmux (POSIX 正準 backend)。
    明示の `--backend` / 環境変数 SPIKE_BACKEND が優先される。
    """
    env = os.environ.get("SPIKE_BACKEND")
    if env:
        return env
    if os.name == "nt" or sys.platform.startswith("win"):
        return "wezterm"
    return "tmux"


def make_adapter(backend: str | None = None) -> TerminalAdapter:
    """backend 名から adapter を生成する。

    循環 import を避けるため adapter 実体は関数内で遅延 import する
    (wezterm / tmux は本モジュールを import するため)。
    """
    backend = backend or default_backend()
    if backend == "tmux":
        from .tmux import TmuxAdapter

        return TmuxAdapter()
    if backend == "wezterm":
        from .wezterm import WezTermAdapter

        return WezTermAdapter()
    raise ValueError(
        f"unknown backend {backend!r} (valid: {', '.join(VALID_BACKENDS)})"
    )
