# -*- coding: utf-8 -*-
"""tmux terminal adapter (POSIX 正準 backend)。

設計 SoT: docs/design/ja-migration-plan.md §4 (runtime 抽出設計) /
docs/design/renga-decoupling.md §4.7 (adapter 境界と能力表)。
現行 canonical は本モジュール。歴史的 origin: claude-org-transport-lab
spike/tmux_adapter.py (Phase 2 で検証され本 subpackage に faithful port された)。

WezTerm adapter (Phase 1) と同一の `TerminalAdapter` 面を実装し、AC-1 / AC-2
ハーネスを backend 非依存に green にする。

要求面 (Issue #2): spawn / send-keys / capture-pane / list-panes。target は
pane id (`%N`) を全呼出で明示する (確定事項 (4) の backend 横断適用)。

tmux が WezTerm より素直な点 (Issue #2 が活かせと言う点):
- `send-keys` が一級プリミティブ。Enter は `send-keys Enter`、Ctrl+C は
  `send-keys C-c`、本文 1 行は `send-keys -l -- <text>` で送れる。WezTerm の
  「send-text --no-paste + CR」「paste + settle + CR」のような小細工が要らない。
- detached session でも tmux サーバーが各 pane の仮想端末を保持するため、
  GUI / ディスプレイ無しで spawn → capture-pane が成立する (WSL2 / CI 向き)。

分離 (設計書 §7.5 / Phase 1 README の本体非干渉方針の踏襲):
- 専用 socket (`-L claude-org-broker`) 上に session を作り、既存 tmux サーバー
  (もし renga 等が使っていても) と完全に分離する。本 backend は自分が作った session の
  pane (`%N`) のみ操作する。
- session 名は pid + 連番でユニーク化し、並走・再実行で衝突しない。

未送信複数行テキストのみ bracketed paste (`paste-buffer -p`) を使う。これは
「改行を行ごとの submit に化けさせない」ための必須処理であり、WezTerm でも tmux でも
同じ理由で必要 (backend 差ではなく TUI 入力欄のセマンティクス)。
"""

from __future__ import annotations

import itertools
import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import ClassVar

from .base import NUDGE_TEXT, PaneRef  # noqa: F401  (NUDGE_TEXT 再利用)

# 専用 socket 名: 既存 tmux サーバーと分離する (本体非干渉)。
BROKER_SOCKET = "claude-org-broker"

# detached session の仮想端末サイズ。80x24 だと Claude TUI が折返し過多で
# プロンプト / ヒント行の検出が不安定になるため広めに取る。
DEFAULT_WIDTH = 220
DEFAULT_HEIGHT = 50

# list-panes -F のフィールド順 (geometry / cursor を含む。能力表 §4.7 の裏取り)。
_LIST_FIELDS = (
    "pane_id", "window_id", "session", "left", "top",
    "width", "height", "cursor_x", "cursor_y", "active", "pane_pid",
)
_LIST_FMT = "\t".join(
    "#{" + {
        "session": "session_name", "left": "pane_left", "top": "pane_top",
        "width": "pane_width", "height": "pane_height", "active": "pane_active",
    }.get(f, f) + "}"
    for f in _LIST_FIELDS
)


def find_tmux() -> str:
    exe = shutil.which("tmux")
    if exe:
        return exe
    raise FileNotFoundError("tmux not found in PATH")


@dataclass
class TmuxAdapter:
    # 専用 socket (-L claude-org-broker) で既存サーバーと完全分離するため、
    # list_panes() は自分が spawn した pane のみ見せる (人間の窓口 pane は出ない)。
    # broker の last-pane ガードが論理ペイン (窓口) を +1 計上してよい backend。
    # backend 固定の能力なので ClassVar (dataclass field にしない)。
    isolated_session: ClassVar[bool] = True

    exe: str = field(default_factory=find_tmux)
    socket: str = BROKER_SOCKET
    timeout: float = 15.0
    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    _counter: "itertools.count[int]" = field(
        default_factory=lambda: itertools.count(1), repr=False
    )

    # ------------------------------------------------------------------ util
    def _tmux(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        cmd = [self.exe, "-L", self.socket, *args]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout,
        )
        if check and proc.returncode != 0:
            raise RuntimeError(
                f"tmux failed ({proc.returncode}): {' '.join(args)}\n"
                f"stderr: {proc.stderr.strip()}"
            )
        return proc

    def _new_session_name(self) -> str:
        return f"claude-org-broker-{os.getpid()}-{next(self._counter)}"

    # ------------------------------------------------------------------ list
    def list_panes(self) -> list[dict]:
        """`tmux list-panes -a -F` を geometry / cursor 付きで dict 化する。"""
        proc = self._tmux("list-panes", "-a", "-F", _LIST_FMT, check=False)
        if proc.returncode != 0:
            # 「サーバー未起動 / session 皆無」だけを空扱いにする。socket 権限・
            # サーバー異常・format エラー等を一律 [] にすると pane_exists() が
            # backend unreachable を pane missing と誤判定するため、それ以外は例外。
            #   - 専用 socket がまだ無い: "error connecting to ... (No such file ...)"
            #   - サーバーは落ちたが socket 残: "no server running on ..."
            stderr = (proc.stderr or "").lower()
            benign = (
                "no server" in stderr
                or "error connecting" in stderr
                or "no such file" in stderr
                or "no sessions" in stderr
            )
            if benign:
                return []
            raise RuntimeError(
                f"tmux list-panes failed ({proc.returncode}): {proc.stderr.strip()}"
            )
        out: list[dict] = []
        for line in proc.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) != len(_LIST_FIELDS):
                continue
            rec = dict(zip(_LIST_FIELDS, parts))
            for k in ("left", "top", "width", "height", "cursor_x", "cursor_y",
                      "pane_pid"):
                rec[k] = int(rec[k])
            rec["active"] = rec["active"] == "1"
            out.append(rec)
        return out

    def pane_exists(self, pane_id: str) -> bool:
        return any(p["pane_id"] == pane_id for p in self.list_panes())

    # ----------------------------------------------------------------- spawn
    def spawn(
        self,
        argv: list[str],
        cwd: str | None = None,
        new_window: bool = True,
    ) -> PaneRef:
        """新しい detached session に argv を起動し PaneRef を返す。

        tmux の「ウィンドウ」相当は session (本 backend は常に専用 socket 上の
        新規 session で検証する)。new_window は WezTerm 面との互換のため受けるが、
        tmux では常に新 session を作る (既存 renga 等の pane には触らない)。
        """
        session = self._new_session_name()
        # argv を 1 本の shell-command 文字列にして渡す (tmux はこれを既定 shell
        # 経由で実行する)。shlex.quote で各要素を安全に連結する。
        cmd_str = " ".join(shlex.quote(a) for a in argv)
        args = [
            "new-session", "-d", "-s", session,
            "-x", str(self.width), "-y", str(self.height),
            "-P", "-F", "#{pane_id}\t#{window_id}\t#{session_name}",
        ]
        if cwd:
            args += ["-c", cwd]
        args += [cmd_str]
        proc = self._tmux(*args)
        pane_id, window_id, _sess = proc.stdout.strip().split("\t")
        # tab_id には window_id を充てる (single-tab addressing is window 単位)。
        return PaneRef(pane_id=pane_id, window_id=window_id, tab_id=window_id)

    # ------------------------------------------------------------- send-keys
    def send_enter(self, pane_id: str) -> None:
        """Enter 1 打 (一級プリミティブ)。承認プロンプト機械承認 / submit に使う。"""
        self._tmux("send-keys", "-t", str(pane_id), "Enter")

    def send_interrupt(self, pane_id: str) -> None:
        """Ctrl+C 1 打 (一級プリミティブ。入力欄クリア)。"""
        self._tmux("send-keys", "-t", str(pane_id), "C-c")

    def type_text(self, pane_id: str, text: str) -> None:
        """未送信で入力欄に置く (submit しない)。

        複数行の改行が行ごとの submit に化けないよう bracketed paste を使う
        (`set-buffer` → `paste-buffer -p`)。-d で paste 後にバッファを掃除する。
        """
        buf = f"claude-org-broker-buf-{os.getpid()}-{next(self._counter)}"
        self._tmux("set-buffer", "-b", buf, "--", text)
        self._tmux("paste-buffer", "-t", str(pane_id), "-b", buf, "-p", "-d")

    def send_line(self, pane_id: str, text: str, settle: float = 0.15) -> None:
        """1 行送出 + Enter。ナッジ注入の正準形 (本文は PTY を通すが queue 経由の
        本文ではなく定型 1 行のみ)。

        tmux の一級 send-keys を活かし、本文を `-l` (literal) で 1 行入れてから
        Enter を素直に送る。WezTerm の paste + --no-paste CR のような小細工は不要。
        text は単一行を想定 (改行を含めると send-keys が行ごとに Enter 解釈しうる)。
        """
        self._tmux("send-keys", "-t", str(pane_id), "-l", "--", text)
        time.sleep(settle)  # literal 反映と Enter の競合を避ける小休止
        self._tmux("send-keys", "-t", str(pane_id), "Enter")

    # ----------------------------------------------------------- capture-pane
    def get_text(self, pane_id: str, escapes: bool = False) -> str:
        """pane の画面テキスト取得 (grid scrape)。-t で target 明示必須。"""
        args = ["capture-pane", "-t", str(pane_id), "-p"]
        if escapes:
            args.append("-e")
        proc = self._tmux(*args)
        return proc.stdout

    # ------------------------------------------------------------------ kill
    def kill_pane(self, pane_id: str) -> None:
        """spawn した検証 pane の後始末 (kill-pane)。単一 pane の session なら
        session ごと消える。本 backend 内部用。"""
        self._tmux("kill-pane", "-t", str(pane_id), check=False)

    def kill_server(self) -> None:
        """専用 socket のサーバーごと落とす (本 backend が作った全 session の一括後始末)。"""
        self._tmux("kill-server", check=False)


if __name__ == "__main__":
    # 簡易自己診断 (Claude 不要・無課金): cat を spawn し、send-keys / capture を確認。
    import sys

    a = TmuxAdapter()
    ref = a.spawn(["cat"])
    print(f"spawned pane_id={ref.pane_id} window_id={ref.window_id}")
    a.type_text(ref.pane_id, "hello-tmux-adapter")
    time.sleep(0.3)
    a.send_enter(ref.pane_id)
    time.sleep(0.3)
    screen = a.get_text(ref.pane_id)
    print("--- capture ---")
    print(screen)
    panes = a.list_panes()
    print("--- list-panes ---")
    for p in panes:
        print(p)
    a.kill_pane(ref.pane_id)
    a.kill_server()
    sys.exit(0 if "hello-tmux-adapter" in screen else 1)
