# -*- coding: utf-8 -*-
"""WezTerm terminal adapter (Windows 正準 backend, minimal surface)。

設計 SoT: docs/design/ja-migration-plan.md §4 (runtime 抽出設計) /
docs/design/renga-decoupling.md §4.7 (adapter 境界と能力表)。
canonical 実装: claude-org-transport-lab spike/wezterm_adapter.py
(Phase 1 で検証済。本 subpackage への faithful port)。

スパイク要求面 (事前 codex design review 確定事項 (1)):
  spawn / send-text / get-text / list の 4 面。

設計上の固定事項:
- 全 `wezterm cli` 呼び出しで `--pane-id` を明示する (確定事項 (4))。
  省略時は WEZTERM_PANE / フォーカス先にフォールバックし誤配送の温床になるため。
- adapter は spawn した pane の window_id / tab_id / pane_id を保持する。
- 承認打鍵 (Enter 相当) は send-text --no-paste + CR で行う (確定事項 (1))。
  send-text 既定は bracketed paste 動作のため CR が Enter として解釈されない。
- spike は自分が spawn した pane のみ操作する。既存 pane (renga 等) には触らない。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import ClassVar

# 共有基盤から再エクスポート (既存 import 経路 `from wezterm import ...` を壊さない
# ため)。NUDGE_TEXT / PaneRef / classify_pane_state / wait_for_state は backend
# 非依存で base に集約した (Phase 2)。
from .base import (  # noqa: F401
    NUDGE_TEXT,
    PaneRef,
    classify_pane_state,
    wait_for_state,
)

WEZTERM_DEFAULT_EXE = r"C:\Program Files\WezTerm\wezterm.exe"


def find_wezterm() -> str:
    """PATH 優先、無ければ winget 既定の絶対パス (CLAUDE.md 記載) を使う。"""
    exe = shutil.which("wezterm")
    if exe:
        return exe
    if os.path.exists(WEZTERM_DEFAULT_EXE):
        return WEZTERM_DEFAULT_EXE
    raise FileNotFoundError(
        "wezterm not found in PATH nor at " + WEZTERM_DEFAULT_EXE
    )


@dataclass
class WezTermAdapter:
    # wezterm cli list は global mux を見せるため、窓口の実 pane も匿名で
    # list_panes() に出る (dedicated socket 分離が無い)。broker の last-pane
    # ガードは論理ペイン (窓口) を +1 計上しない backend (窓口は既に実 pane
    # として数えられる/不在なら +1 は stale なため)。backend 固定の能力なので
    # ClassVar (dataclass field にしない)。
    isolated_session: ClassVar[bool] = False

    exe: str = field(default_factory=find_wezterm)
    timeout: float = 15.0

    # ------------------------------------------------------------------ util
    def _cli(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        cmd = [self.exe, "cli", "--no-auto-start", *args]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout,
        )
        if check and proc.returncode != 0:
            raise RuntimeError(
                f"wezterm cli failed ({proc.returncode}): {' '.join(args)}\n"
                f"stderr: {proc.stderr.strip()}"
            )
        return proc

    # ------------------------------------------------------------------ list
    def list_panes(self) -> list[dict]:
        """`wezterm cli list --format json` の生エントリ一覧。"""
        proc = self._cli("list", "--format", "json")
        return json.loads(proc.stdout)

    def pane_exists(self, pane_id: int) -> bool:
        return any(p["pane_id"] == pane_id for p in self.list_panes())

    # ----------------------------------------------------------------- spawn
    def spawn(
        self,
        argv: list[str],
        cwd: str | None = None,
        new_window: bool = True,
    ) -> PaneRef:
        """新しい pane を spawn し PaneRef を返す。

        spike は別 WezTerm ウィンドウで検証する (renga の現行組織ペインに
        触らない) ため new_window=True が既定。
        """
        args = ["spawn"]
        if new_window:
            args.append("--new-window")
        if cwd:
            args += ["--cwd", cwd]
        args += ["--", *argv]
        proc = self._cli(*args)
        pane_id = int(proc.stdout.strip())
        ref = PaneRef(pane_id=pane_id)
        # window_id / tab_id を list から補完して保持する (確定事項 (4))
        for p in self.list_panes():
            if p["pane_id"] == pane_id:
                ref.tab_id = p["tab_id"]
                ref.window_id = p["window_id"]
                break
        return ref

    # ------------------------------------------------------------- send-text
    def send_text(self, pane_id: int, text: str, no_paste: bool = False) -> None:
        """pane へテキスト送出。--pane-id 明示必須。

        no_paste=False (既定): bracketed paste として送る。入力欄に文字列を
          置くだけで Enter にはならない (改行も paste 内改行として扱われる)。
        no_paste=True: 生のキー入力として送る。"\r" が Enter として解釈される。
        """
        args = ["send-text", "--pane-id", str(pane_id)]
        if no_paste:
            args.append("--no-paste")
        args += ["--", text]
        self._cli(*args)

    def send_enter(self, pane_id: int) -> None:
        """Enter 1 打。承認プロンプトの機械承認等に使う (確定事項 (1))。"""
        self.send_text(pane_id, "\r", no_paste=True)

    # ---- intent 面 (TerminalAdapter Protocol、backend 横断で harness が使う) ----
    def type_text(self, pane_id: int, text: str) -> None:
        """未送信で入力欄に置く (submit しない)。bracketed paste で複数行の
        改行も paste 内改行として扱い、行ごとの submit に化けさせない。"""
        self.send_text(pane_id, text, no_paste=False)

    def send_interrupt(self, pane_id: int) -> None:
        """Ctrl+C 1 打 (入力欄クリア)。WezTerm では生キー入力で ETX を送る。"""
        self.send_text(pane_id, "\x03", no_paste=True)

    def send_line(self, pane_id: int, text: str, settle: float = 0.15) -> None:
        """1 行送出 + Enter。ナッジ注入の正準形 (本文は通さない)。

        text 本体は paste で置き、確定の CR のみ --no-paste で送る。
        こうすると text 中の特殊文字がキー解釈されない。
        """
        self.send_text(pane_id, text, no_paste=False)
        time.sleep(settle)  # paste 反映と Enter の競合を避ける小休止
        self.send_enter(pane_id)

    # -------------------------------------------------------------- get-text
    def get_text(self, pane_id: int, escapes: bool = False) -> str:
        """pane の画面テキスト取得 (grid scrape)。--pane-id 明示必須。"""
        args = ["get-text", "--pane-id", str(pane_id)]
        if escapes:
            args.append("--escapes")
        proc = self._cli(*args)
        return proc.stdout

    # ------------------------------------------------------------------ kill
    def kill_pane(self, pane_id: int) -> None:
        """spawn した検証 pane の後始末 (kill-pane)。spike 内部用。"""
        self._cli("kill-pane", "--pane-id", str(pane_id), check=False)


# 画面状態ヒューリスティック (classify_pane_state / wait_for_state) は
# backend 非依存のため base に移動し、本モジュールの先頭で再エクスポートしている
# (Phase 2)。
