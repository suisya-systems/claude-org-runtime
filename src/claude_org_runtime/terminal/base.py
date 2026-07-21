# -*- coding: utf-8 -*-
"""terminal adapter の共有基盤。

設計 SoT: docs/design/ja-migration-plan.md §4 (runtime 抽出設計) /
docs/design/renga-decoupling.md §4.7 (adapter 境界と能力表)。
現行 canonical は本モジュール。歴史的 origin: claude-org-transport-lab
spike/terminal_adapter.py (Phase 1-5 で検証され本 subpackage に faithful port された)。

Phase 1 (WezTerm / Windows) で確立した adapter 面を backend 非依存に抽象化し、
Phase 2 で tmux (POSIX 正準 backend) を第二実装として追加した。broker / harness は
本モジュールの `TerminalAdapter` 面と `make_adapter()` ファクトリ経由でのみ backend に
触り、WezTerm / tmux のどちらでも同一の AC-1 / AC-2 テストを green にする。

intent レベルの面 (broker / harness が実際に使う最小集合):
  spawn / list_panes / pane_exists / get_text /
  type_text (未送信で置く) / send_enter (確定) / send_line (型+確定) /
  send_interrupt (Ctrl+C) / send_named_keys (canonical raw-key batch) / kill_pane

raw-key vocabulary (Issue #108): 個々の named key は
:func:`~claude_org_runtime.terminal.keys.normalize_key` で **canonical 形**に畳んで
から adapter に渡す (正規化は broker/surface 側。adapter は canonical のみ受ける)。
adapter は :meth:`TerminalAdapter.send_named_keys` で canonical キー列を batch 送出し、
自 backend が emit 可能な canonical 集合を能力フラグ ``supported_named_keys`` で宣言する
(broker が text 送信の**前に**全キーを preflight し、途中まで打鍵してからの per-key
unsupported を避けるための all-or-nothing 契約)。

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
import shlex
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, Union, runtime_checkable

if TYPE_CHECKING:  # 実体は wezterm / tmux (循環 import 回避で遅延)
    pass

# pane 識別子の型。WezTerm は整数 (例 3)、tmux は文字列 (例 "%3")。
# broker / harness は不透明値として扱い、算術や解釈をしない (確定事項 (4) の
# 「全呼出で target を明示する」を backend 横断で守るための単一の出入口)。
PaneId = Union[int, str]

# ナッジ定型 1 行 (docs/design/renga-decoupling.md §4.3)。本文は PTY を通さない。
# ツール名は **FQ (fully-qualified)** で直書きする (SoT 5.2: Claude prose は FQ
# ツール名を書く)。renga<->broker 併存下では ambient renga-peers も同名 bare
# 'check_messages' を公開するため、bare 名だと nudge が renga-peers 側へ誤ルートして
# broker queue が silent drop する (#76)。FQ 名なら宛先 server が一意に解決する。
NUDGE_TEXT = "📨 新着あり。mcp__org-broker__check_messages を実行"


# Fix-D: workspace 非依存 liveness の判定結果 (Issue #114)。adapter の任意メソッド
# ``pane_liveness(pane_id, terminal_id)`` が返す語彙で、broker reaper が bookkeeping を
# 落とす前の権威判定に使う。list_panes/pane_exists は「自 workspace filter 越し」の
# liveness なので、placement バグや workspace 消失で生 pane を構造的に欠落させ、それを
# reaper が「消えた」と誤読して生 pane を close していた (真因 A/B)。pane.get のような
# workspace 非依存の直接 probe を持つ backend (Herdr) はこの verdict を返し、reaper は
# 盲目的な物理 close をやめて verdict に従う。持たない backend (tmux/wezterm) は従来の
# 物理 close 検証経路のまま (broker は getattr で存在を見て分岐する)。
PANE_LIVE_ALIVE = "alive"      # 直接 probe で present かつ terminal_id 一致 (= 同一プロセス)
PANE_LIVE_REUSED = "reused"    # present だが terminal_id 不一致 (pane_id が別 pane に再利用)
PANE_LIVE_GONE = "gone"        # 直接 probe で不在 (= 権威的に消滅)
PANE_LIVE_UNKNOWN = "unknown"  # backend 不通等で判定不能 (reaper は defer する)


@dataclass
class PaneRef:
    """spawn した pane の追跡情報。毎回 target を明示するために保持する。

    pane_id は backend ネイティブ型 (WezTerm=int / tmux=str)。tab_id / window_id は
    backend での「タブ / ウィンドウ」相当 (tmux では window_id / session を充てる)。

    terminal_id は backend が付ける **プロセス/端末の安定 identity** (Herdr
    ``terminal_id``)。pane_id は移送 (pane.move) や backend の id 再利用で変わりうるが
    terminal_id はプロセスに紐づき不変 (Issue #114 実測)。broker はこれを reaper の
    id 再利用ガード (workspace 非依存 liveness と照合) に使う。持たない backend
    (tmux/wezterm) は ``None`` (id 再利用ガードは効かないが従来経路は不変)。
    """

    pane_id: PaneId
    tab_id: PaneId | None = None
    window_id: PaneId | None = None
    terminal_id: PaneId | None = None


# ---------------------------------------------------------------------------
# workspace レイアウト配置ヒント (Issue #110 §6.2)
# ---------------------------------------------------------------------------

# space key: レイアウト上の論理スペース種別 (Issue #110 §2)。broker が role /
# project-slug から算出し、supports_space_layout=True の adapter (Herdr) が
# workspace へ解決する。
SPACE_CONTROL = "control"                     # dispatcher / watcher / secretary の制御面
SPACE_UNASSIGNED = "project:_unassigned"      # project 欠落 worker の catch-all (control を汚さない)


def project_space_key(slug: str) -> str:
    """project-slug を worker スペースの space key へ写像する (``project:<slug>``)。"""
    return f"project:{slug}"


@dataclass
class SpaceDescriptor:
    """spawn 時の workspace 配置ヒント (Issue #110 §6.2 Layer B → C)。

    broker が ``role`` / ``project_slug`` から算出し、``supports_space_layout=True``
    の adapter (Herdr) が ``spawn(space=...)`` で受けて workspace へ解決する。持たない
    adapter (tmux / wezterm) には **渡さない** — broker が
    ``getattr(adapter, "supports_space_layout", False)`` で分岐し、False の backend へは
    従来どおり ``spawn(argv, cwd, new_window)`` のみを呼ぶ (flat session 不変)。

    - ``space_key``: :data:`SPACE_CONTROL` / ``project:<slug>`` / :data:`SPACE_UNASSIGNED`。
    - ``split_direction``: per-space 既定分割方向 (Herdr ``"down"`` = 上下 / ``"right"``
      = 左右、Issue #110 §8)。``None`` は adapter の per-space policy 既定に委ねる。
    """

    space_key: str
    split_direction: str | None = None


@runtime_checkable
class TerminalAdapter(Protocol):
    """broker / harness が依存する terminal backend の最小面 (構造的型)。

    WezTermAdapter / TmuxAdapter が本 Protocol を満たす。全メソッドが target
    (pane_id) を明示で受け取り、フォーカス先や環境変数へのフォールバックをしない。

    能力フラグ ``isolated_session`` (bool, ClassVar): backend が「自分が spawn
    した pane だけ」を ``list_panes()`` で見せるか (= dedicated session 分離) を
    表す。tmux (専用 socket -L claude-org-broker) は True (人間の窓口 pane は別
    サーバーにあり出ない)、wezterm (cli list, global mux) は False (窓口の実
    pane も匿名で出る)。broker の close_pane が論理ペイン (人間駆動の窓口) を
    last-pane ガードに +1 計上してよいかの判断に使う (isolated な時だけ窓口は
    adapter の外におり +1 が正当)。本 Protocol は ``@runtime_checkable`` で
    ``issubclass`` 検査に使うため、非メソッド member を**注釈として宣言しない**
    (注釈すると issubclass が TypeError)。concrete adapter が ClassVar として
    持ち、broker は ``getattr(adapter, "isolated_session", False)`` で読む。

    能力フラグ ``supported_named_keys`` (frozenset[str], ClassVar): この backend が
    :meth:`send_named_keys` で emit 可能な **canonical** キー
    (:data:`~claude_org_runtime.terminal.keys.CANONICAL_KEYS` の部分集合) を宣言する。
    tmux は full vocabulary、Herdr は実測 subset (delete/home/end/pageup/pagedown を
    欠く)、WezTerm は既存実装で送れる最小 subset (``{"enter", "ctrl+c"}``) を宣言する。
    broker は text を送る**前に**送信予定の全
    canonical キーをこの集合で preflight し、未対応が 1 つでもあれば全体を
    ``[key_unsupported]`` で拒否する (all-or-nothing: 途中まで打鍵して壊さない)。
    ``isolated_session`` と同じ理由で **注釈しない** ClassVar とし、broker は
    ``getattr(adapter, "supported_named_keys", frozenset())`` で読む。

    opportunistic reap の tuning (任意 ClassVar、backend-aware):
    broker の入口 reap (自己終了した managed pane の bookkeeping 掃除) は既定で
    「snapshot に現れない = 即 reap」だが、``list_panes`` が **eventually consistent**
    な backend (Herdr: boot 中や snapshot ラグで生 pane が一時欠落しうる) では、生
    pane を誤 reap し孤児 TUI を残す。これを避けるため adapter は次の 2 つを ClassVar
    で宣言でき、broker は ``getattr(adapter, ..., default)`` で読む (宣言しない
    backend は既定 = 従来の即時 reap):
      - ``reap_min_age_seconds`` (float, 既定 0.0): spawn からこの秒数を超えるまで、
        snapshot 欠落があっても reap しない (boot 中の一時欠落を保護)。
      - ``reap_min_missing_snapshots`` (int, 既定 1): この回数 snapshot に現れて
        初めて reap 対象 (単発 snapshot ラグを弾く回数ベースの補助ゲート)。
      - ``reap_min_missing_seconds`` (float, 既定 0.0): 連続欠落が**実時間**でこの
        秒数継続して初めて reap 対象 (poll cadence 非依存の主ゲート)。broker は reap を
        request-driven に並行呼び出しするため、回数だけだと単一ラグ窓で複数スレッドが
        立て続けに count を積んで誤 reap しうる。実時間ゲートは「何回呼ばれても実時間が
        経たない限り成立しない」ので、この bursty 誤判定を構造的に断つ。
    これは表示面の判定 (``list_panes`` に載るか) は変えず、bookkeeping 削除の
    決定条件だけを backend の snapshot 一貫性に合わせて硬くする防御である。

    workspace 非依存 liveness (任意メソッド ``pane_liveness``、Issue #114 Fix-D):
    ``list_panes`` / ``pane_exists`` は「自 workspace filter 越し」の liveness で、
    placement バグ (Herdr ``agent.start`` が workspace param を無視し focused workspace
    へ配置する) や workspace 消失で **生 pane を構造的に欠落**させる。それを reaper が
    「消えた」と誤読すると生 pane を close して殺す (Issue #114 の isolation 崩壊)。
    これを断つため adapter は次の任意メソッドを実装でき、broker は
    ``getattr(adapter, "pane_liveness", None)`` で存在を見て使う:

        def pane_liveness(self, pane_id, terminal_id=None) -> str: ...

    workspace を指定しない直接 probe (Herdr ``pane.get``) で pane の実在を引き、記録済み
    terminal_id と照合して :data:`PANE_LIVE_ALIVE` / :data:`PANE_LIVE_REUSED` /
    :data:`PANE_LIVE_GONE` / :data:`PANE_LIVE_UNKNOWN` を返す。broker reaper はこの
    verdict に従い、ALIVE/UNKNOWN は reap を defer、GONE/REUSED のみ bookkeeping を掃除、
    かつ **REUSED では物理 close を発行しない** (その pane_id は今や無関係 pane で、
    close すると巻き添える)。宣言しない backend (tmux/wezterm) は従来の物理 close 検証
    経路のまま (getattr が None を返し reaper が旧経路にフォールバック)。
    ``isolated_session`` 等と同じ理由で **注釈しない** 任意メンバとする
    (``@runtime_checkable`` の issubclass 検査を tmux/wezterm で壊さないため)。

    workspace レイアウト (任意 ClassVar ``supports_space_layout``、Issue #110):
    backend が「control 面 1 スペース + ワーカーはプロジェクト単位スペース」の
    workspace レイアウトポリシー (§1.2) を持つかを宣言する。Herdr=True (org 所有
    workspace 集合を持ち、:class:`SpaceDescriptor` を workspace へ解決)、tmux/wezterm=
    False (flat session)。broker は ``getattr(adapter, "supports_space_layout", False)``
    で読み、True の時だけ role / project から :class:`SpaceDescriptor` を算出して
    :meth:`spawn` の ``space`` 引数へ渡し、空スペース掃除等のレイアウト挙動を有効化する。
    False の backend へは ``space`` を渡さず従来の flat spawn を呼ぶ (完全不変)。
    ``isolated_session`` 等と同じく **注釈しない** ClassVar とする。

    :meth:`spawn` の ``space`` 引数 (optional, Issue #110 §6.2 Layer C): 既定 ``None``
    で後方互換 (現行 flat 挙動)。``supports_space_layout`` な backend のみが解釈し、
    他は無視する。**この追加は Set D Surface 1 (spawn) の契約変更**だが default None で
    後方互換であり、契約 ratify は本体取り込みスコープ (別 PR)、本タスクは flag のみ
    (§10)。

    :meth:`spawn` の ``env`` 引数 (optional, Issue #122): pane プロセスの親環境へ
    **追加注入する環境変数** の辞書 (既定 ``None`` = 追加なしで後方互換)。broker は
    ここに ``ORG_BROKER_STATE_DIR`` (daemon の state dir 絶対パス) を載せ、pane 内で
    走る CLI subprocess (例 ``broker send`` を叩く ja ``peer_notify``) が非既定
    ``--state-dir`` の daemon を発見できるようにする (``mcp_config`` への env 追加では
    pane 内 subprocess に届かない)。値は **追加分のみ** で、親環境全体ではない
    (backend は自 backend の env 伝搬機構で既存環境の上に重ねる)。backend ごとの
    伝搬機構は異なる (tmux=``new-session -e`` / wezterm=argv の env 前置 / herdr=
    ``agent.start`` の env param) が、**観測挙動 (pane subprocess に届く) は backend
    間で一致させる**。``None`` / 空 dict は完全に従来挙動。

    agent kind (任意 ClassVar ``supports_agent_kind``、Issue #151): backend が
    :meth:`spawn` の ``kind`` 引数 (broker の意味的な種別 ``"claude"`` / ``"codex"`` /
    generic は ``None``) を解釈するかを宣言する。Herdr=True (herdr 0.7.5 の
    ``agent.start`` は ``kind`` が必須で、実行ファイルをこれで決める)、tmux/wezterm=
    False (argv をそのまま起動するので不要)。``space`` と同じく broker が
    ``getattr(adapter, "supports_agent_kind", False)`` で読み、True の時だけ ``kind``
    を渡す (flat backend の spawn シグネチャは不変)。**argv[0] から推測してはならない**:
    venv wrapper 経路では argv[0] がシェルになり、generic spawn では任意コマンドに
    なるため、broker が知っている種別を明示的に渡す唯一の経路とする。

    venv の PATH 継承方式 (任意 ClassVar ``venv_path_via_pane_env``、Issue #151):
    backend が ``PATH`` prepend を **argv の login-shell wrapper ではなく自前で**
    (pane 生成時 env や pane への打ち込みで) 行うかを宣言する。Herdr=True (0.7.5 の
    ``agent.start`` から ``argv`` が消え wrapper の運搬経路自体が無くなったため)、
    tmux/wezterm=False (:func:`venv_pane_prep` の wrapper 方式のまま不変)。True の
    backend へは broker が argv を書き換えず ``VIRTUAL_ENV`` のみ env に載せる
    (:func:`venv_pane_env`)。
    """

    def spawn(
        self,
        argv: list[str],
        cwd: str | None = ...,
        new_window: bool = ...,
        space: "SpaceDescriptor | None" = ...,
        env: "dict[str, str] | None" = ...,
        kind: "str | None" = ...,
    ) -> PaneRef: ...

    def list_panes(self) -> list[dict]: ...

    def pane_exists(self, pane_id: PaneId) -> bool: ...

    def get_text(self, pane_id: PaneId, escapes: bool = ...) -> str: ...

    def type_text(self, pane_id: PaneId, text: str) -> None: ...

    def send_enter(self, pane_id: PaneId) -> None: ...

    def send_line(self, pane_id: PaneId, text: str, settle: float = ...) -> None: ...

    def send_interrupt(self, pane_id: PaneId) -> None: ...

    def send_named_keys(self, pane_id: PaneId, keys: Sequence[str]) -> None: ...

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
# workspace virtualenv inheritance (Issue #130)
# ---------------------------------------------------------------------------

def find_workspace_venv(*bases: "str | None") -> str | None:
    """Return the absolute path of the first workspace ``.venv`` that owns the
    platform interpreter, scanning ``bases`` in order; ``None`` if none.

    Issue #130: org-spawned panes (worker / dispatcher) must inherit the
    workspace virtualenv, or venv-assuming tooling breaks. Discovery prefers the
    pane's own ``cwd/.venv`` and falls back to the daemon ``root_cwd/.venv`` --
    worker worktrees typically have no ``.venv`` of their own, so the fallback is
    the usual hit (Codex review Major). ``state_dir`` is deliberately NOT a base:
    it points at broker sidecar/state, not the work repo.

    Detection is a complete no-op unless a real venv interpreter exists
    (``.venv/bin/python`` on POSIX, ``.venv\\Scripts\\python.exe`` on native
    Windows). conda and other layouts have no ``.venv`` and are left untouched
    (Codex review Minor). native Windows (``os.name == "nt"``) uses
    ``Scripts`` + ``;``; WSL/Linux/macOS use ``bin`` + ``:`` (WSL is a Linux
    process, so it takes the POSIX layout).
    """
    for base in bases:
        if not base:
            continue
        venv = os.path.abspath(os.path.join(base, ".venv"))
        if os.name == "nt":
            interp = os.path.join(venv, "Scripts", "python.exe")
        else:
            interp = os.path.join(venv, "bin", "python")
        if os.path.isfile(interp):
            return venv
    return None


def venv_bin_dir(venv: str) -> str:
    """The venv's executables dir (``Scripts`` on native Windows, else ``bin``)."""
    return os.path.join(venv, "Scripts" if os.name == "nt" else "bin")


# $SHELL values whose syntax the wrapper script relies on (``export`` builtin,
# ``"$PATH"`` / ``"$@"`` expansion, combined ``-lc``). All POSIX-family shells
# honour these; fish / csh / tcsh / xonsh / nu / powershell do NOT (fish uses
# ``set -gx`` and has no ``$@``; csh uses ``setenv`` and rejects ``-lc``). Using
# the user's ``$SHELL`` blindly would make the wrapper unparseable and leave the
# pane / secretary unable to launch at all for those users -- a hard regression
# vs. launching claude directly (self-review MAJOR). So we only borrow ``$SHELL``
# when it is POSIX-family, else fall back to ``/bin/sh`` (always present, POSIX).
_POSIX_WRAPPER_SHELLS = frozenset({
    "sh", "bash", "zsh", "dash", "ksh", "ksh93", "pdksh", "mksh", "ash", "busybox",
})


def _venv_wrapper_shell() -> str:
    """Pick a POSIX-syntax login shell for :func:`login_shell_venv_wrapper`.

    Prefer the operator's ``$SHELL`` (so its login profile -- the one that
    rebuilds ``PATH`` -- is the one we supersede) but only when it is a
    POSIX-family shell whose syntax the wrapper script needs; otherwise
    ``/bin/sh`` (self-review MAJOR: a fish/csh ``$SHELL`` cannot parse the
    script and would break every venv-workspace pane launch).
    """
    shell = os.environ.get("SHELL")
    if shell and os.path.basename(shell) in _POSIX_WRAPPER_SHELLS:
        return shell
    return "/bin/sh"


def login_shell_venv_wrapper(
    argv: list[str], bin_dir: str, run_cwd: "str | None" = None,
) -> list[str]:
    """Wrap ``argv`` so ``bin_dir`` is prepended to ``PATH`` AFTER the login
    shell's profile init runs (POSIX only).

    Issue #130 / Codex review Blocker 2: prepending ``PATH`` through the adapter
    env mechanism (tmux ``new-session -e`` / herdr ``agent.start env``) is not
    enough -- those pass the value as the *parent* environment, and the pane's
    login shell then sources the user's profile, which rebuilds ``PATH`` from
    scratch and drops ``.venv/bin`` (the 2026-07-04 incident mechanism). So the
    prepend must run *after* profile init: invoke a login shell whose ``-c``
    script exports the venv ``PATH`` and then ``exec``s the real argv. ``-l``
    sources the profile *before* ``-c`` runs, so our export always wins. The
    ``$0`` label is cosmetic (it only shows in ``ps``); the real argv follows
    and is exec'd via ``"$@"``.

    ``run_cwd`` (when given) is re-entered right after profile init and before
    ``exec``: ``-l`` sources a login profile, and a profile that ends in an
    unconditional ``cd`` would otherwise relocate the pane away from the cwd the
    adapter placed it in (self-review MINOR). ``cd`` uses ``;`` (best-effort) so
    a missing dir still falls through to the ``exec`` rather than aborting.

    The shell is chosen by :func:`_venv_wrapper_shell` (POSIX-family ``$SHELL``
    or ``/bin/sh``), never a non-POSIX ``$SHELL`` that cannot parse the script.
    """
    shell = _venv_wrapper_shell()
    steps = []
    if run_cwd:
        steps.append(f"cd {shlex.quote(run_cwd)}")
    steps.append(f'export PATH={shlex.quote(bin_dir)}:"$PATH"')
    steps.append('exec "$@"')
    script = "; ".join(steps)
    return [shell, "-lc", script, "claude-org-runtime-venv", *argv]


def venv_pane_prep(
    argv: list[str], cwd: "str | None", root_cwd: "str | None",
) -> "tuple[list[str], dict[str, str]]":
    """Prepare ``(argv, extra_env)`` so an adapter-spawned pane inherits the
    workspace ``.venv`` (Issue #130). Complete no-op ``(list(argv), {})`` when no
    ``.venv`` is found (conda etc. untouched).

    ``VIRTUAL_ENV`` rides the adapter env dict -- the same channel as
    ``ORG_BROKER_STATE_DIR`` (Issue #122). A login shell's profile does not
    clobber ``VIRTUAL_ENV``, only ``PATH``, so the env dict is safe for it.

    The ``PATH`` prepend is platform-split because the two spawn transports
    rebuild ``PATH`` differently:

    - POSIX (tmux / wezterm / herdr): the pane runs a login shell that rebuilds
      ``PATH``, so the prepend is folded into the argv via a post-profile login
      shell (:func:`login_shell_venv_wrapper`). It stays in the env dict's
      sibling -- the argv -- so it survives the rebuild.
    - native Windows (wezterm): ``cmd`` has no ``PATH``-rebuilding profile, so
      the prepend rides the env dict as ``<Scripts>;%PATH%``. The wezterm
      adapter's ``cmd /d /c set "PATH=..."`` wrapper (``_env_wrapped_argv``)
      expands ``%PATH%`` at launch time, so a login-shell wrapper is unneeded
      (and ``cmd`` cannot express one anyway).
    """
    venv = find_workspace_venv(cwd, root_cwd)
    if venv is None:
        return list(argv), {}
    bin_dir = venv_bin_dir(venv)
    if os.name == "nt":
        return list(argv), {"VIRTUAL_ENV": venv, "PATH": f"{bin_dir};%PATH%"}
    # run_cwd = the pane's own cwd (not the venv location -- for the root_cwd
    # fallback the pane still runs in its own worktree, it just borrows root's
    # .venv), so a profile ``cd`` cannot move claude out of its worktree.
    return login_shell_venv_wrapper(argv, bin_dir, run_cwd=cwd), {"VIRTUAL_ENV": venv}


def venv_pane_env(cwd: "str | None", root_cwd: "str | None") -> "dict[str, str]":
    """``VIRTUAL_ENV`` だけを返す venv 継承 (Issue #151、``venv_path_via_pane_env``)。

    :func:`venv_pane_prep` の argv 書き換えを行わない版。``PATH`` の prepend は
    backend 自身が行う契約なのでここでは載せない (env dict に ``PATH`` を直に
    載せると login shell の profile 初期化に潰される = Issue #130 Blocker 2)。

    herdr 0.7.5 用: ``agent.start`` から ``argv`` が消え、post-profile login-shell
    wrapper という運搬経路そのものが無くなったため、broker は argv を触らず
    ``VIRTUAL_ENV`` のみ渡し、adapter が pane 生成後 (= profile 初期化完了後) に
    ``PATH`` を打ち込む。``.venv`` が無ければ空 dict (完全 no-op)。
    """
    venv = find_workspace_venv(cwd, root_cwd)
    return {} if venv is None else {"VIRTUAL_ENV": venv}


# ---------------------------------------------------------------------------
# backend ファクトリ
# ---------------------------------------------------------------------------

VALID_BACKENDS = ("wezterm", "tmux", "herdr")


def backend_unavailable_reason(backend: str) -> str | None:
    """Return an ASCII-only English reason the backend cannot run here, or None.

    Single source of truth for backend x platform support. Both the ``org up``
    launcher (fail-fast before spawning the daemon) and
    ``HerdrAdapter.__post_init__`` (foreground ``broker serve`` path) call this
    so the two never drift and both fail with the same actionable message.

    ``herdr`` speaks to its daemon over a stdlib ``AF_UNIX`` Unix domain socket,
    which native Windows (``os.name == "nt"``) lacks; it is POSIX / WSL only.
    The message is kept ASCII-only (no em-dash) so it survives a cp932 console.
    """
    if backend == "herdr" and os.name == "nt":
        return (
            "backend 'herdr' is not supported on native Windows: it requires a "
            "Unix domain socket (POSIX / WSL only). Use '--backend wezterm' on "
            "native Windows, or run under WSL. To drive a remote herdr session, "
            "use the renga transport instead."
        )
    return None


def default_backend() -> str:
    """実行環境の既定 backend。

    - Windows (native): WezTerm (tmux はネイティブ Windows で動かない)。
    - POSIX (Linux / macOS / WSL2): tmux (POSIX 正準 backend)。
    明示の `--backend` / 環境変数 ORG_BACKEND が優先される。

    herdr は POSIX 限定の opt-in backend で、既定には選ばれない (Herdr server の
    常駐が前提のため。`--backend herdr` / `ORG_BACKEND=herdr` で明示選択する)。
    """
    env = os.environ.get("ORG_BACKEND")
    if env:
        return env
    if os.name == "nt" or sys.platform.startswith("win"):
        return "wezterm"
    return "tmux"


def make_adapter(
    backend: str | None = None, *, state_dir: str | None = None
) -> TerminalAdapter:
    """backend 名から adapter を生成する。

    循環 import を避けるため adapter 実体は関数内で遅延 import する
    (wezterm / tmux は本モジュールを import するため)。

    ``state_dir`` は workspace レイアウトを持つ backend (Herdr) の世代識別
    (org_instance_id / generation) 永続と起動時 stale 掃除に使う (Issue #110 §5)。
    tmux / wezterm は無視する (flat session)。
    """
    backend = backend or default_backend()
    if backend == "tmux":
        from .tmux import TmuxAdapter

        return TmuxAdapter()
    if backend == "wezterm":
        from .wezterm import WezTermAdapter

        return WezTermAdapter()
    if backend == "herdr":
        from .herdr import HerdrAdapter

        return HerdrAdapter(state_dir=state_dir)
    raise ValueError(
        f"unknown backend {backend!r} (valid: {', '.join(VALID_BACKENDS)})"
    )
