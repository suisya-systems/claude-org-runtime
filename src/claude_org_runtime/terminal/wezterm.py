# -*- coding: utf-8 -*-
"""WezTerm terminal adapter (Windows 正準 backend, minimal surface)。

設計 SoT: docs/design/ja-migration-plan.md §4 (runtime 抽出設計) /
docs/design/renga-decoupling.md §4.7 (adapter 境界と能力表)。
現行 canonical は本モジュール。歴史的 origin: claude-org-transport-lab
spike/wezterm_adapter.py (Phase 1 で検証され本 subpackage に faithful port された)。

スパイク要求面 (事前 codex design review 確定事項 (1)):
  spawn / send-text / get-text / list の 4 面。

設計上の固定事項:
- 全 `wezterm cli` 呼び出しで `--pane-id` を明示する (確定事項 (4))。
  省略時は WEZTERM_PANE / フォーカス先にフォールバックし誤配送の温床になるため。
- adapter は spawn した pane の window_id / tab_id / pane_id を保持する。
- 承認打鍵 (Enter 相当) は send-text --no-paste + CR で行う (確定事項 (1))。
  send-text 既定は bracketed paste 動作のため CR が Enter として解釈されない。
- 本 backend は自分が spawn した pane のみ操作する。既存 pane (renga 等) には触らない。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import ClassVar

# 共有基盤から再エクスポート (既存 import 経路 `from wezterm import ...` を壊さない
# ため)。NUDGE_TEXT / PaneRef / classify_pane_state / wait_for_state は backend
# 非依存で base に集約した (Phase 2)。
from .base import (  # noqa: F401
    NUDGE_TEXT,
    PaneId,
    PaneRef,
    classify_pane_state,
    wait_for_state,
)

WEZTERM_DEFAULT_EXE = r"C:\Program Files\WezTerm\wezterm.exe"


def _cmd_line_from_argv(argv: list[str]) -> str:
    """Render argv into a ``cmd.exe /c`` command line that reaches the target
    program's argv verbatim, with cmd's own metacharacters neutralised (#122).

    Two escaping layers (the well-known "quote for CommandLineToArgvW, then
    caret-escape for cmd.exe" technique):

      1. ``subprocess.list2cmdline`` applies MSVCRT / ``CommandLineToArgvW``
         quoting (spaces, embedded quotes, backslashes) so each element parses
         back to one argv token in the launched program.
      2. Every ``cmd.exe`` metacharacter is caret-escaped so ``cmd`` does not
         interpret ``& | < > ( )`` (or a stray quote) as a separator /
         redirection BEFORE launching the program. ``cmd`` strips the carets and
         hands the rest to the program, so ``^"`` still arrives as ``"`` and the
         MSVCRT quoting survives. Without this, a caller-supplied argv value
         containing e.g. ``&`` (flag *values* are not allowlist-checked) would
         let ``cmd`` run an injected command.

    Known residual: ``%`` cannot be fully neutralised on a ``cmd`` command line
    (only ``%VAR%`` of an already-defined env var could still expand -- a value
    substitution, never arbitrary command execution).
    """
    line = subprocess.list2cmdline(argv)
    meta = set('()%!^"<>&|')
    return "".join("^" + ch if ch in meta else ch for ch in line)


def _env_wrapped_argv(argv: list[str], env: dict[str, str] | None) -> list[str]:
    """argv を「環境変数を先に設定してから argv を exec する」形に包む (Issue #122)。

    WezTerm の ``cli spawn`` は起動プログラムの環境を mux サーバー環境から引くため、
    ``subprocess.run(env=...)`` (cli クライアント側の env) は pane プロセスに届かない。
    そこで **起動する argv 自体** に env 設定を織り込む (design review が言う
    wezterm=argv 経路)。``env`` が空 / None なら argv をそのまま返す (完全に従来挙動)。

    - POSIX: ``env KEY=VALUE ... <argv>`` (``env`` は最初の非オプション語で option 解析を
      止めるので ``--`` は不要)。
    - native Windows: ``cmd /d /c set "KEY=VALUE"&& <argv>`` (Windows には ``env`` が無い)。
      ``set`` は cmd 組込みなので追加バイナリ依存を持ち込まない。program 部は
      :func:`_cmd_line_from_argv` で cmd メタ文字を caret エスケープし、caller 由来の
      argv 値に含まれる ``&`` 等を cmd が区切りとして解釈する injection を遮断する。
      ``set "K=V"`` の値は二重引用符で囲むため cmd のメタ文字解析から保護される
      (Windows パスに ``"`` は現れない)。

    値は追加分のみを重ねる (親環境全体を渡さない)。default-deny guard は
    :func:`surface.build_claude_argv` で **包む前** の argv に既に適用済み。
    """
    if not env:
        return list(argv)
    if os.name == "nt":
        assigns = "".join(f'set "{k}={v}"&& ' for k, v in env.items())
        return ["cmd", "/d", "/c", assigns + _cmd_line_from_argv(argv)]
    return ["env", *(f"{k}={v}" for k, v in env.items()), *argv]


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

    # raw-key vocabulary (Issue #108): WezTerm adapter は full gap ではなく
    # **既存実装で送れる subset** を宣言する (design review 確定事項 (5))。Enter は
    # send-text --no-paste + CR、Ctrl+C は ETX (\x03) の生キー送出で既に emit できる。
    # 矢印 / Shift+Tab / Home 等は本 adapter では未実装のため canonical に含めず、
    # broker preflight が text 送信前に [key_unsupported] で全体を拒否する。
    supported_named_keys: ClassVar[frozenset[str]] = frozenset({"enter", "ctrl+c"})

    exe: str = field(default_factory=find_wezterm)
    timeout: float = 15.0

    # 子ペイン (dispatcher / worker) を集約するアンカーウィンドウの window_id。
    # 最初の子 spawn でそのウィンドウを記録し、以降の子はこのウィンドウへ
    # タブとして spawn する (--window-id)。worker 増加でウィンドウが散らかる
    # のを防ぐ (Issue #86, #576 実機 dogfood)。アンカーが kill された場合は
    # 次 spawn で生存確認に失敗し --new-window へフォールバックして再確定する。
    # 構築引数ではなく内部状態なので init=False。
    _anchor_window_id: PaneId | None = field(default=None, init=False)

    # spawn() のアンカー判定 (check) -> spawn -> 記録 (set) を直列化する lock。
    # broker は ThreadingHTTPServer 配下で spawn を _lock 外 (slow adapter I/O を
    # broker lock に載せない契約) から並行に呼ぶため、これが無いと 2 つの spawn
    # が同時に _anchor_window_id is None を見て両方 --new-window を発行し、集約
    # 目的が並行 worker 起動時に破れる (Codex review Major)。spawn は低頻度なので
    # 全体を直列化してよい。repr には出さない (テストの assertion 出力を汚さない)。
    _spawn_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False, compare=False
    )

    # ------------------------------------------------------------------ util
    def _cli(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        cmd = [self.exe, "cli", "--no-auto-start", *args]
        run_kwargs: dict = {}
        if os.name == "nt":
            # wezterm.exe は GUI サブシステムバイナリなので cli 起動のたびに
            # ウィンドウが点滅する。monitoring/message のたびに _cli が発火するため抑止。
            run_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        proc = subprocess.run(
            cmd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout,
            **run_kwargs,
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

    def _window_alive(self, window_id: PaneId) -> bool:
        """window_id を持つ pane が 1 つでも生存しているか (アンカー生存確認)。"""
        return any(p["window_id"] == window_id for p in self.list_panes())

    # ----------------------------------------------------------------- spawn
    def spawn(
        self,
        argv: list[str],
        cwd: str | None = None,
        new_window: bool = True,
        env: dict[str, str] | None = None,
    ) -> PaneRef:
        """新しい pane を spawn し PaneRef を返す。

        new_window=True (既定) は子ペイン (dispatcher / worker) を単一の
        アンカーウィンドウに集約する (Issue #86):
        - 最初の子 (アンカー未確定) はウィンドウを新規に開き、その window_id を
          アンカーとして記録する。
        - 以降の子はアンカーへ新規タブとして spawn する (--window-id <anchor>)。
          --window-id と --new-window は排他なので、この時は --new-window を
          付けない。
        - アンカーが kill された場合は生存確認に失敗し --new-window へフォール
          バックして新しいアンカーを再確定する。

        new_window=False は集約を行わず、現在ペインのウィンドウへ spawn する
        (--new-window も --window-id も付けない既存挙動)。

        ``env`` (Issue #122): pane プロセスへ追加注入する環境変数。WezTerm では起動
        argv に env 設定を織り込む (:func:`_env_wrapped_argv`。``subprocess.run(env=)``
        は mux が pane に届けないため)。空 / None なら従来どおり argv をそのまま起動する。

        並行 spawn 時もアンカー判定〜記録が直列化されるよう _spawn_lock 下で
        実行する (Codex review Major、_spawn_lock 参照)。
        """
        with self._spawn_lock:
            args = ["spawn"]
            # アンカーへタブとして開いた spawn かどうか。新規ウィンドウを開いた
            # 場合のみ後段でアンカーを (再) 確定する。
            opened_new_window = False
            if new_window:
                if self._anchor_window_id is not None and self._window_alive(
                    self._anchor_window_id
                ):
                    # アンカー生存 -> 新規タブとして集約 (--new-window は付けない)
                    args += ["--window-id", str(self._anchor_window_id)]
                else:
                    # 最初の子、またはアンカー死亡 -> 新規ウィンドウで開き再確定
                    args.append("--new-window")
                    opened_new_window = True
            if cwd:
                args += ["--cwd", cwd]
            args += ["--", *_env_wrapped_argv(argv, env)]
            proc = self._cli(*args)
            pane_id = int(proc.stdout.strip())
            ref = PaneRef(pane_id=pane_id)
            # window_id / tab_id を list から補完して保持する (確定事項 (4))
            for p in self.list_panes():
                if p["pane_id"] == pane_id:
                    ref.tab_id = p["tab_id"]
                    ref.window_id = p["window_id"]
                    break
            # 新規ウィンドウを開いた集約 spawn なら、その window_id を以降の子の
            # アンカーとして記録する。
            if opened_new_window and ref.window_id is not None:
                self._anchor_window_id = ref.window_id
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

    def send_named_keys(self, pane_id: int, keys: Sequence[str]) -> None:
        """canonical キー列を送出する (WezTerm subset: enter / ctrl+c のみ)。

        broker が :attr:`supported_named_keys` で preflight 済みのため、通常は
        subset キーしか届かない。防御的に **batch 全体を先に検証**し、subset 外が
        1 つでもあれば 1 打も送らずに :class:`ValueError` を上げる (all-or-nothing:
        supported キーを先に打ってから後続で落ちて画面を半分壊さない。tmux / Herdr
        の ``[_MAP[k] for k in keys]`` 前段検証と同じ契約。broker preflight のバグや
        直接呼出に対する adapter 層のガード)。
        """
        unsupported = [k for k in keys if k not in self.supported_named_keys]
        if unsupported:
            raise ValueError(
                f"wezterm adapter cannot emit canonical keys {unsupported!r} "
                f"(supported subset: {sorted(self.supported_named_keys)})"
            )
        for k in keys:
            if k == "enter":
                self.send_enter(pane_id)
            elif k == "ctrl+c":
                self.send_interrupt(pane_id)

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
        """spawn した検証 pane の後始末 (kill-pane)。本 backend 内部用。"""
        self._cli("kill-pane", "--pane-id", str(pane_id), check=False)


# 画面状態ヒューリスティック (classify_pane_state / wait_for_state) は
# backend 非依存のため base に移動し、本モジュールの先頭で再エクスポートしている
# (Phase 2)。
