# -*- coding: utf-8 -*-
"""``org up`` / ``org down`` — broker 制御面 (PR #67) の薄い launcher wrapper。

設計 SoT: runtime#63 org up/down launcher の Codex design review
(tmp/codex-review-runtime-broker-control-plane.md)。本モジュールは制御面
(sidecar 契約 / admin RPC mint_token・shutdown / journal_offset スライス) の
ロジックを**再実装しない**。それらを順番に呼ぶだけの wrapper に徹する。

``org up``:
  1. sidecar を読み、走行中 daemon の **健全性** を判定する。判定基準は PID 生存
     ではなく **到達性** — admin RPC (mint_token) が応答し、minted token で MCP
     ``initialize`` → ``tools/list`` が往復できること。到達できれば再利用、到達
     できなければ (URLError = stale sidecar) daemon をバックグラウンド起動する。
  2. admin RPC ``mint_token`` で secretary tier の root token を発行する
     (root name = ``secretary``)。``--root-cwd`` を relative-spawn 解決アンカーと
     して bind に持たせる。
  3. mcp-config を ``<state-dir>/secretary-mcp.json`` に 0600 で書く。
  4. 対話型 claude TUI を起動する (argv は **既存** の課金中立 builder
     :func:`surface.build_claude_argv` 経由。二重実装しない)。POSIX は exec、
     Windows は subprocess 起動か 1 行コマンド表示の fallback。

``org down``:
  1. sidecar から daemon を発見する。
  2. 残存 broker ペイン (claude / codex 子) を close する (token revoke を兼ねる。
     last-pane / 論理ペイン / isolated_session の backend 別判定は close_pane が
     broker 内で行うので down は薄く呼ぶだけ)。
  3. admin RPC ``shutdown`` で graceful 停止 (シグナル非依存 = Windows 要件)。
  4. ``journal_offset`` スライスで ``broker_stopped`` を検証する (全履歴 grep の
     偽陽性回避)。
  5. sidecar を後始末する (daemon の finally と冪等)。

全パスは入口でパスを絶対化する (:func:`sidecar.absolutize`、Windows ``isabs`` の
罠を避けるため ``posixpath`` 併用)。
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

from ..terminal import (
    VALID_BACKENDS,
    HerdrAdapter,
    TmuxAdapter,
    WezTermAdapter,
    backend_unavailable_reason,
    default_backend,
    find_workspace_venv,
    login_shell_venv_wrapper,
    venv_bin_dir,
)
from . import residents, sidecar, surface
from .rpc import ADMIN_RPC_TIMEOUT, _McpClient, _admin_rpc  # noqa: F401 - 後方互換 re-export

DEFAULT_STATE_DIR = ".state/broker"
SECRETARY_MCP_NAME = "secretary-mcp.json"
DEFAULT_ROOT_NAME = "secretary"

# daemon バックグラウンド起動後に sidecar (daemon.json + admin.token) が公開される
# のを待つ上限。子の stdout には依存しない (sidecar が唯一の情報源) ため poll する。
SIDECAR_WAIT_TIMEOUT = 20.0
# shutdown 要求後に daemon が finally (stop → sidecar 削除) を終えるのを待つ上限。
STOP_WAIT_TIMEOUT = 15.0
# daemon.json は見えているが admin.token がまだ無いときに、その公開 window
# (serve は write_sidecar → write_admin_token の順で連続して書く) を乗り切るための
# 短い猶予。これを越えても admin.token が現れなければ「半公開 / クラッシュ」と判断し、
# **新規起動はしない** (生存 daemon が同 state_dir を所有している可能性があるため
# 二重 daemon = split-brain を避ける)。
ADMIN_TOKEN_GRACE = 3.0
_POLL_INTERVAL = 0.05

# admin HTTP RPC / MCP-over-HTTP クライアント (_admin_rpc / _McpClient) は notify
# helper (broker send) と共有するため :mod:`rpc` に factor out した (Issue #93)。
# 既存テストの ``launcher._admin_rpc`` / ``launcher._McpClient`` 参照と monkeypatch
# は上の re-import で不変に保つ (挙動・名前とも等価)。


# ===========================================================================
# HTTP クライアント補助 (制御面固有)
# ===========================================================================

def _mcp_surface_ok(host: str, port: int, token: str) -> bool:
    """minted token で MCP initialize → tools/list が往復し公開面が返ることを確認。

    secretary tier の token なので全 13 面が見える前提。往復できれば daemon の
    MCP 面は健全 (admin 面だけでなく per-agent 面も生きている)。接続不可は
    URLError を送出する (呼び元が握る)。確認後はセッションを DELETE で閉じ、
    使い捨て probe token を走行中 daemon に登録したまま残さない。
    """
    client = _McpClient(host, port, token)
    try:
        client.initialize()
        return len(client.tools_list()) > 0
    finally:
        client.close()


# ===========================================================================
# org up
# ===========================================================================

def _spawn_daemon(state_dir: str, backend: str, root_cwd: str) -> tuple[str, int, str]:
    """broker daemon をバックグラウンド起動し sidecar 公開を待つ。

    POSIX は ``start_new_session=True`` で detach、Windows は
    ``CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS`` で親コンソールから切り離す。
    子の stdout/stderr は DEVNULL に捨てる (sidecar が唯一の情報源で stdout に
    依存しない契約)。``--port 0`` で ephemeral bind し、実ポートは sidecar から
    読む (well-known ポート衝突を避け、発見は常に sidecar 経由)。
    """
    argv = [
        sys.executable, "-m", "claude_org_runtime.broker", "serve",
        "--state-dir", state_dir, "--port", "0",
        "--backend", backend, "--root-cwd", root_cwd,
    ]
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(argv, **kwargs)
    sc, admin_token = _wait_for_sidecar(state_dir)
    return sc["host"], sc["port"], admin_token


def _wait_for_sidecar(
    state_dir: str, timeout: float | None = None,
) -> tuple[dict, str]:
    """daemon.json と admin.token の双方が公開されるまで poll する。

    両方揃って初めて daemon は admin RPC を受けられる (admin.token は atomic
    publish。:func:`sidecar.read_admin_token` は空文字列を None 扱いにするため
    部分書きを拾わない)。タイムアウトは RuntimeError。
    """
    if timeout is None:
        timeout = SIDECAR_WAIT_TIMEOUT
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sc = sidecar.read_sidecar(state_dir)
        admin_token = sidecar.read_admin_token(state_dir)
        if sc is not None and admin_token is not None:
            return sc, admin_token
        time.sleep(_POLL_INTERVAL)
    raise RuntimeError(
        f"daemon sidecar did not appear under {state_dir!r} within {timeout}s"
    )


def _mint_secretary(
    host: str, port: int, admin_token: str, name: str, root_cwd: str,
) -> dict | None:
    """admin RPC で secretary tier の root token を mint する。

    返り値は admin 応答 (``{ok, token, agent_id, role, mcp_config}`` または
    ``{ok: False, error}``)。``cwd`` (= root_cwd) を bind に持たせて relative-spawn
    の解決アンカーにする (Issue #61。serve の --root-cwd と同じ役割)。接続不可は
    URLError を送出する (呼び元が「到達不能」を判定)。

    ``channel: True`` を要求し、返る ``mcp_config`` に push 一次配送の channel
    sidecar (``org-broker-channel``, OWNER=secretary) を載せさせる。これにより
    root(窓口) Claude Code も子 (dispatcher/worker) と同じく dev-channel sidecar を
    持ち push が届く (本タスク: secretary 起動経路の channel 配線欠落の修正)。
    control-plane の probe / down ctrl token は別経路 (channel 非要求) なので
    使い捨て token に未使用 delivery cred を leak しない。

    ``observer: True`` を要求し、observed-session binding (Issue #129 問題 A) の
    observer lease を assert させて ``observer_secret`` を受け取る。org up はこの秘密を
    子プロセス env (``ORG_BROKER_CHANNEL_OBSERVER``) へ handoff し、この observed
    secretary session だけが delivery generation を bump できるようにする (fork replay の
    takeover を断つ)。observer は org up の human-facing 経路だけが指定する opt-in で、
    他の admin channel mint (secret handoff を持たない) は従来どおり last-register-wins。
    """
    return _admin_rpc(
        host, port, admin_token, "mint_token",
        {"role": "secretary", "name": name, "cwd": root_cwd,
         "channel": True, "observer": True},
    )


def write_secretary_mcp_config(state_dir: str, mcp_config: dict) -> Path:
    """secretary の --mcp-config を ``<state-dir>/secretary-mcp.json`` に 0600 で書く。

    admin.token と同じ atomic publish (temp 0600 → os.replace) で torn read を
    避ける。token を含む秘密ファイルなので 0600 (Windows は read-only ビットのみ
    実効の既知制限。localhost-only daemon の前提で補う — sidecar.py と同方針)。
    """
    state_dir_p = Path(state_dir)
    state_dir_p.mkdir(parents=True, exist_ok=True)
    path = state_dir_p / SECRETARY_MCP_NAME
    tmp = state_dir_p / (SECRETARY_MCP_NAME + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(mcp_config, f, ensure_ascii=False, indent=2)
    finally:
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def build_up_argv(
    mcp_config: dict, *, model: str | None = None,
    permission_mode: str | None = None, extra: list[str] | None = None,
) -> list[str]:
    """secretary TUI の argv を **既存** の課金中立 builder で組む (二重実装禁止)。

    :func:`surface.build_claude_argv` が ``--mcp-config`` (inline JSON) を注入し、
    default-deny guard を通すので、headless flag (``-p`` / ``--print`` 等) は構造的に
    argv に混入しない。inline JSON は spawn_claude_pane が子に渡すのと同じ契約
    (token は localhost-only daemon の前提で許容。0600 file は再接続/検査用の
    durable artifact として別に残す)。

    ``mcp_config`` に channel sidecar (``org-broker-channel``) が積まれていれば、
    子経路 (spawn_claude) と同じく dev-channel flag
    (``--dangerously-load-development-channels server:org-broker-channel``) を argv に
    付ける。flag は config の実体に**従属**させる (config に channel があるときだけ
    flag を出す) ことで、両者が必ず一致し drift しない (secretary mint が channel を
    載せれば root も push 一次配送 sidecar を load する)。
    """
    channel_server = (
        "org-broker-channel"
        if "org-broker-channel" in mcp_config.get("mcpServers", {})
        else None
    )
    return surface.build_claude_argv(
        mcp_config_json=json.dumps(mcp_config),
        model=model, permission_mode=permission_mode, extra_args=extra,
        channel_server=channel_server,
    )


def _launch_claude(
    argv: list[str], state_dir: str | None = None,
    observer_secret: str | None = None,
    root_cwd: str | None = None,
) -> int:
    """secretary TUI を起動する。POSIX は exec で置換、Windows は subprocess。

    POSIX: ``os.execvpe`` で現プロセスを claude に置換する (TUI が端末を引き継ぐ。
    これ以降は返らない)。Windows: exec セマンティクスが無いため subprocess で
    起動し前景で待つ。claude バイナリが見つからない場合は 1 行コマンドを表示して
    人間に委ねる fallback (課金中立 argv はそのまま手で起動できる)。

    どの経路でも子環境に ``ORG_TRANSPORT=broker`` を注入する (Issue #70)。Epic
    #586 Phase 2 で既定 transport は broker に昇格したため env 未設定でも broker に
    解決されるが、この明示注入は (a) 既定変更に依らず broker 経路を保証し、(b)
    ``ORG_TRANSPORT=renga`` の opt-in fallback が org up 配下の子へ漏れて renga 経路
    (``RENGA_PANE_ID`` 不在で停止) に落ちるのを防ぐ二重の安全弁として維持する。org
    up は broker 制御面を起動しているので、子は常に broker transport を使う。
    fallback の表示コマンドにも env 前置を含め、手で起動しても同じ transport に
    なるようにする。

    ``state_dir`` が渡されれば ``ORG_BROKER_STATE_DIR`` (絶対パス) も注入する
    (Issue #122)。root secretary 内で起動される CLI subprocess (例 ``broker send``)
    が、非既定 ``--state-dir`` で起動した daemon の queue を発見できるようにするため。
    daemon-spawned pane と対称の注入 (server._adapter_spawn)。

    ``root_cwd`` が渡され、そこに ``.venv`` があれば secretary もそれを継承する
    (Issue #130)。daemon-spawned worker/dispatcher ペイン (server._adapter_spawn が
    本丸) に対する secretary 用の補助整合で、``.venv`` が無ければ完全 no-op。

    ``observer_secret`` が渡されれば ``ORG_BROKER_CHANNEL_OBSERVER`` を子環境へ注入する
    (Issue #129 問題 A)。これは **mcp-config には載せない** observed-session binding の
    非 replay 秘密で、この org up が起動する observed live session の channel sidecar
    だけが register 時に提示できる。fork/resume は persisted mcp-config (delivery cred
    込み) を replay しても、この process env の秘密は継承しないため generation を bump
    できず (daemon が ``unobserved`` で拒否)、observed session を takeover できない。
    claude が見つからない fallback の 1 行コマンドにも ``ORG_BROKER_CHANNEL_OBSERVER``
    前置を **含める**: daemon は mint 時に observer lease を assert 済で、秘密無しで手起動
    すると sidecar が ``unobserved`` で止まり push が届かなくなるため、手起動でも observed
    になるよう secret を渡す (Codex review P2)。表示 argv は既に delivery cred 入りの
    mcp-config を含む (どちらも localhost-only 信頼前提の秘密) ため追加の露出増ではない。

    **段1 folder-trust は意図的に機械承認しない (ja#575 設計判断)**: 起動した
    secretary は (未 trust の cwd では) 初回に Claude Code の folder-trust プロンプトを
    出すが、本関数はそこへ Enter を**送らない**。exec/subprocess で launcher 自身が
    secretary プロセスになる/それにブロックされるため、その PTY に後から打鍵できる
    別プロセスが構造的に存在しない (= 残存ギャップは genuine-user 検出ではなく純構造)。
    段2/段3 は daemon-spawned pane なので呼び出し元 agent が send_keys で承認できる
    (wire seam は tests/broker/test_bootstrap_folder_trust.py が FakeAdapter で固定。
    実プロンプトが CR を受理することは ja#515 dogfood = 実端末で実証済、本コードでは
    再証明しない)。段1 は human が org up 実行直後に 1 回 Enter する production path と
    し、blind Enter をここに足さない (表示前取りこぼし + 二重 Enter の空 turn 暴発を
    防ぐ。理由と将来の sanctioned mechanism = faithful POSIX PTY-wrapper は
    docs/broker-bootstrap-stage1-folder-trust-design.md)。
    """
    env = {**os.environ, "ORG_TRANSPORT": "broker"}
    if state_dir:
        env["ORG_BROKER_STATE_DIR"] = sidecar.absolutize(state_dir)
    if observer_secret:
        env["ORG_BROKER_CHANNEL_OBSERVER"] = observer_secret
    # Issue #130: secretary も workspace の .venv を継承させる (root_cwd = secretary の
    # cwd)。これは daemon-spawned worker/dispatcher ペイン (server._adapter_spawn が主戦場)
    # に対する secretary 用の補助整合で、root TUI を子ペインと揃える。.venv が無ければ
    # 完全 no-op (env/argv 不変で従来挙動)。VIRTUAL_ENV は env dict に載せ、PATH は
    # POSIX では argv を post-profile login-shell wrapper に包んで prepend する
    # (login shell の profile 再構築後に効かせるため — Blocker 2)。Windows は
    # subprocess.call(env=) が子環境を直接決めるため (cmd profile による PATH 再構築が
    # 無い) PATH を env dict に直に前置してよい (%PATH% 展開は不要)。
    venv = find_workspace_venv(root_cwd)
    if venv is not None:
        bin_dir = venv_bin_dir(venv)
        env["VIRTUAL_ENV"] = venv
        if os.name == "nt":
            env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
            # Windows subprocess.call resolves argv[0] against the CURRENT PATH,
            # not env=, so a command that lives only in the venv Scripts dir
            # would not be found (Codex P2). Resolve argv[0] against the venv bin
            # dir and substitute only on a hit; on a miss argv[0] is unchanged so
            # ambient-PATH resolution stays exactly as before (no regression for
            # a globally-installed claude, the normal case). The POSIX branch is
            # unaffected: execvpe(shell) resolves the exec'd argv via env's PATH.
            venv_exe = shutil.which(argv[0], path=bin_dir)
            if venv_exe:
                argv = [venv_exe, *argv[1:]]
        else:
            # run_cwd = root_cwd so a login profile that cd's cannot move the
            # secretary out of its workspace root (self-review MINOR).
            argv = login_shell_venv_wrapper(argv, bin_dir, run_cwd=root_cwd)
    if os.name != "nt":
        os.execvpe(argv[0], argv, env)  # 返らない (プロセス置換)
        return 0  # pragma: no cover (execvpe 成功時は到達しない)
    try:
        return subprocess.call(argv, env=env)
    except (FileNotFoundError, OSError):
        import shlex
        prefix = "ORG_TRANSPORT=broker "
        if state_dir:
            prefix += f"ORG_BROKER_STATE_DIR={shlex.quote(sidecar.absolutize(state_dir))} "
        if observer_secret:
            # observed になるよう secret を前置する (無しだと sidecar が unobserved で
            # 止まり push が届かない — Codex P2)。argv は既に delivery cred 入り。
            prefix += f"ORG_BROKER_CHANNEL_OBSERVER={shlex.quote(observer_secret)} "
        print("claude を起動できませんでした。以下を手動で実行してください:")
        print("  " + prefix + " ".join(shlex.quote(a) for a in argv))
        return 0


def _read_admin_token_with_grace(
    state_dir: str, grace: float | None = None,
) -> str | None:
    """admin.token を読む。無ければ公開 window を乗り切るため短時間だけ poll する。

    serve は ``write_sidecar`` (daemon.json) の **後** に ``write_admin_token`` を
    書くため、daemon.json が見えていても admin.token が一瞬遅れる window がある。
    その間に「token 不在」を即断すると新規 daemon を二重起動しかねない (split-brain)。
    grace 内に現れれば返し、現れなければ None (= 半公開 / クラッシュの疑い)。
    """
    if grace is None:
        grace = ADMIN_TOKEN_GRACE
    deadline = time.monotonic() + grace
    while True:
        tok = sidecar.read_admin_token(state_dir)
        if tok is not None:
            return tok
        if time.monotonic() >= deadline:
            return None
        time.sleep(_POLL_INTERVAL)


def _resolve_existing_daemon(
    state_dir: str, requested_backend: str, name: str, root_cwd: str,
) -> dict:
    """既存 sidecar から走行中 daemon を解決し、org up の分岐を 1 つ決める。

    返り値 ``{"kind": ...}``:
    - ``cold``      — daemon 不在 / 到達不能 (stale) → 新規起動する。到達不能で
      stale sidecar を捨てた場合は ``stale_pid`` (元 sidecar の pid) を伴い、
      org_up が 1 行告知する。sidecar 不在の初回起動では ``stale_pid`` を持たない。
    - ``token_missing`` — daemon.json はあるが admin.token が grace 内に現れない
      (半公開 / クラッシュ疑い)。生存 daemon が同 state_dir を所有しているかも
      しれないため **新規起動しない** (split-brain 回避)。
    - ``unhealthy`` — admin は応答するが mint / MCP 面が健全でない。
    - ``conflict``  — 生存かつ健全だが backend が要求と不一致 (down してからやり直す)。
    - ``already_up``— 生存・健全・backend 一致だが secretary が既に登録済み (no-op)。
    - ``reuse``     — 再利用可。``mint`` (secretary mint 結果) / ``host`` / ``port`` を伴う。

    **重要 (Codex review Major 対応)**: 生存/MCP 健全性は **無名 (auto-unique)** の
    probe token で確認し、backend 整合も済ませた **後で初めて** 名前付き
    ``secretary`` を mint する。健全性確認や backend 判定の失敗パスで
    ``name="secretary"`` の orphan bind を残さない (= 次回の正常な org up が
    ``name_taken`` で "already up" 扱いになり起動不能になる事故を防ぐ)。
    """
    sc = sidecar.read_sidecar(state_dir)
    if sc is None:
        return {"kind": "cold"}
    # daemon.json がこの dir を主張している。admin.token を (公開 window を
    # 乗り切りつつ) 読む。
    admin_token = _read_admin_token_with_grace(state_dir)
    host, port = sc["host"], sc["port"]
    if admin_token is None:
        return {"kind": "token_missing", "host": host}
    # 生存 + MCP 健全性を **無名 probe token** で確認する (失敗しても named secretary を
    # 汚さない)。到達不能 = stale sidecar → 新規起動。
    try:
        probe = _admin_rpc(host, port, admin_token, "mint_token", {"role": "secretary"})
    except urllib.error.URLError:
        # 到達不能 = stale sidecar。sc["pid"] を運んで org_up 側で 1 行告知する
        # (無警告の cold 上書きを避ける。Issue #141)。sidecar 不在の cold とは
        # ``stale_pid`` キーの有無で区別する。
        return {"kind": "cold", "stale_pid": sc.get("pid")}
    if not (probe and probe.get("ok")):
        return {"kind": "unhealthy", "host": host}
    try:
        if not _mcp_surface_ok(host, port, probe["token"]):
            return {"kind": "unhealthy", "host": host}
    except urllib.error.URLError:
        return {"kind": "unhealthy", "host": host}
    # daemon は生存・健全。ここで初めて backend を判定し、その後 named secretary を
    # mint する (どちらの失敗パスも named orphan を残さない)。
    if sc.get("backend") != requested_backend:
        return {"kind": "conflict", "backend": sc.get("backend")}
    res = _mint_secretary(host, port, admin_token, name, root_cwd)
    if res and res.get("ok"):
        return {"kind": "reuse", "mint": res, "host": host, "port": port}
    if res and "name_taken" in (res.get("error") or ""):
        return {"kind": "already_up"}
    return {"kind": "unhealthy", "host": host}


def org_up(
    args: argparse.Namespace, *,
    spawn_daemon=_spawn_daemon, launch=_launch_claude,
) -> int:
    """``org up`` 本体。``spawn_daemon`` / ``launch`` はテスト用に注入可能。"""
    state_dir = sidecar.absolutize(args.state_dir)
    root_cwd = (
        sidecar.absolutize(args.root_cwd) if args.root_cwd is not None
        else os.getcwd()
    )
    requested_backend = args.backend or default_backend()
    name = args.name
    extra = list(args.claude_arg or [])

    mint: dict | None = None
    host = port = None
    reused = False

    # --- fail-fast: backend x platform validation (before any daemon work) ----
    # Placed *before* _resolve_existing_daemon so an unusable backend on this
    # platform surfaces a clear, actionable error instead of being masked by a
    # stale-sidecar / backend-conflict / token-missing path or degrading into a
    # 20s no-info sidecar timeout (Issue #120). --backend has no argparse
    # choices constraint here, so distinguish an unknown backend from one that
    # is valid but unsupported on this platform.
    if requested_backend not in VALID_BACKENDS:
        print(
            f"org up: unknown backend {requested_backend!r} "
            f"(valid: {', '.join(VALID_BACKENDS)}).",
            file=sys.stderr,
        )
        return 2
    unavailable = backend_unavailable_reason(requested_backend)
    if unavailable:
        print(f"org up: {unavailable}", file=sys.stderr)
        return 2

    # --- 健全性判定 (到達性ベース。失敗パスで named secretary を汚さない) -----
    decision = _resolve_existing_daemon(state_dir, requested_backend, name, root_cwd)
    kind = decision["kind"]
    if kind == "conflict":
        print(
            f"org up: a daemon is already running with backend "
            f"{decision['backend']!r}, but backend {requested_backend!r} was "
            f"requested. Run 'org down' first, or omit --backend.",
            file=sys.stderr,
        )
        return 2
    if kind == "token_missing":
        print(
            f"org up: a daemon sidecar (daemon.json) exists at {decision['host']} "
            f"but its admin.token never appeared (daemon booting or crashed "
            f"mid-publish). Not starting a second daemon over the same state_dir; "
            f"run 'org down' to clean up, then retry.",
            file=sys.stderr,
        )
        return 2
    if kind == "unhealthy":
        print(
            "org up: a daemon is reachable but unhealthy (admin mint or MCP surface "
            "did not respond as expected). Run 'org down' first.",
            file=sys.stderr,
        )
        return 2
    if kind == "already_up":
        print(
            f"org up: a secretary ({name!r}) is already registered on the running "
            f"daemon - org is already up. Use 'org down' to stop it."
        )
        return 0
    if kind == "reuse":
        mint = decision["mint"]
        host, port = decision["host"], decision["port"]
        reused = True

    # --- 新規起動 (kind == "cold": sidecar 不在 / 到達不能 = stale) ----------
    if not reused:
        # broker 管理外 resident の pre-flight は **cold パスでのみ** 走らせる (Issue #142)。
        # kind=="cold" はこの ownership の daemon が確実に不在/到達不能 = live な所有
        # resident は前世代クラッシュの genuine な orphan、と判定できる。reuse/already_up
        # 等では走らせない: ownership+identity は「クラッシュした前世代の orphan」と「今
        # 健全な現世代が所有する live resident」を区別できない (resident は daemon boot を
        # 跨いで生き残る設計) ため、reuse で走らせると org up --reap が **稼働中 org 自身の
        # 生きた watcher を SIGTERM してしまう** (red-team Blocker)。live resident の回収は
        # daemon 停止後の org down が本来の担い手。
        residents.preflight_residents(
            state_dir, root_cwd, reap=args.reap, prefix="org up",
        )
        # 到達不能な stale sidecar を無警告で上書きしない。前回 daemon が clean に
        # 終われば org down が sidecar を消しているはずで、残存 + 到達不能は
        # クラッシュ / 強制終了のサイン。原因追跡の手掛かりに 1 行残す (Issue #141)。
        # sidecar 不在の正常な初回起動 (stale_pid キー無し) では出さない。
        if "stale_pid" in decision:
            print(
                f"org up: discarded stale sidecar for dead "
                f"pid={decision['stale_pid']} (previous daemon did not shut down "
                f"cleanly); starting fresh",
                file=sys.stderr,
            )
        host, port, admin_token = spawn_daemon(state_dir, requested_backend, root_cwd)
        try:
            res = _mint_secretary(host, port, admin_token, name, root_cwd)
        except urllib.error.URLError:
            print("org up: freshly started daemon did not accept admin RPC.",
                  file=sys.stderr)
            return 2
        if not (res and res.get("ok")):
            err = res.get("error") if res else "no response"
            print(f"org up: admin mint_token failed on fresh daemon: {err}",
                  file=sys.stderr)
            return 2
        mint = res

    assert mint is not None  # 上の分岐いずれかで必ず設定される
    # --- mcp-config (0600) + secretary TUI 起動 --------------------------
    cfg_path = write_secretary_mcp_config(state_dir, mint["mcp_config"])
    argv = build_up_argv(
        mint["mcp_config"], model=args.model,
        permission_mode=args.permission_mode, extra=extra,
    )
    status = "reused running" if reused else "started"
    print(f"org up: {status} daemon at http://{host}:{port}")
    print(f"org up: minted secretary token (agent_id={mint['agent_id']})")
    print(f"org up: wrote mcp-config to {cfg_path} (0600)")
    print(f"org up: launching claude secretary TUI ({len(argv)} argv tokens)")
    # observed-session binding (Issue #129 問題 A): channel mint が返した observer 秘密を
    # 子環境へ注入する (mcp-config には載せない非 replay 信号)。channel 非要求 mint や
    # 旧 daemon 応答では None で、その場合は従来の last-register-wins に委ねる。
    return launch(argv, state_dir, observer_secret=mint.get("observer_secret"),
                  root_cwd=root_cwd)


# ===========================================================================
# org down
# ===========================================================================

_AGENT_PANE_KINDS = {"claude", "codex"}

# backend 名 → adapter クラス (isolated_session ClassVar を **非インスタンス化**で
# 読むため。インスタンス化は backend バイナリ解決を伴うので避ける)。
_BACKEND_ADAPTER_CLASS = {
    "tmux": TmuxAdapter,
    "wezterm": WezTermAdapter,
    "herdr": HerdrAdapter,
}


def _backend_is_isolated(backend: str | None) -> bool:
    """sidecar の backend 名から isolated_session 能力フラグを引く。

    isolated-socket backend (tmux) は自分が spawn した pane のみ list_panes に
    見せる (= 載っている pane は全て broker 所有)。global-mux backend (wezterm) は
    無関係 pane も見せる。adapter クラスの ``isolated_session`` ClassVar を
    インスタンス化せず読むことで、能力判定の単一の出所を adapter に保つ
    (launcher 側で bool をハードコードして drift させない)。未知 / None は False
    (保守的に「管理外 pane が混じり得る」側に倒す)。
    """
    cls = _BACKEND_ADAPTER_CLASS.get(backend or "")
    return bool(getattr(cls, "isolated_session", False)) if cls is not None else False


def _close_managed_panes(
    host: str, port: int, token: str, *, isolated: bool,
) -> list:
    """走行中 broker の残存 broker ペインを close する (backend 別判定)。

    secretary tier の制御 token で list_panes → close_pane を呼ぶ。close_pane が
    内部で token revoke / last-pane ガード / 論理ペイン拒否 / isolated_session の
    last-pane カウントを行うので、down は薄く呼ぶだけ (制御面ロジックは再実装しない)。

    **backend 別の close 範囲 (Issue #63 ユーザー判断: 案 B)**:
    - ``isolated`` (tmux, isolated-socket): list_panes に載るのは **全て broker 所有**
      なので ``kind`` を問わず close する。これで generic ``spawn_pane`` (attention
      watcher 等, kind=None) も含めて org のペインを掃除できる。論理ペイン (窓口) は
      close_pane が ``[logical_pane]`` で拒否し、最後の 1 枚は ``[last_pane]`` で守る
      ため、全件 close を試みても安全。
    - ``not isolated`` (wezterm, global-mux): list_panes は broker 管理外の無関係
      pane も返すため、``kind∈{claude,codex}`` の **org エージェント子のみ** に限定し、
      無関係 pane の巻き添え kill を避ける。

    接続不可は URLError を送出する (呼び元が握る)。
    """
    client = _McpClient(host, port, token)
    try:
        client.initialize()
        panes = client.call_tool("list_panes").get("panes", [])
        closed: list = []
        for pane in panes:
            # global-mux では org エージェント子のみ。isolated では kind 不問で全件。
            if not isolated and pane.get("kind") not in _AGENT_PANE_KINDS:
                continue
            res = client.call_tool("close_pane", {"target": str(pane.get("id"))})
            if res.get("ok"):
                closed.append(pane.get("id"))
        return closed
    finally:
        client.close()  # 使い捨て control token を de-register (down 直前の掃除)


def _wait_for_stop(
    state_dir: str, offset: int, timeout: float | None = None,
) -> bool:
    """daemon の停止を待ち、journal_offset スライスで broker_stopped を検証する。

    run() の finally は ``stop()`` (broker_stopped を append) → ``remove_sidecar()``
    の順に進むため、sidecar が消えた時点で broker_stopped は必ず書かれている。
    ``offset`` (= この run の起点) 以降のスライスのみを見て当該 run の
    broker_stopped を確認する (全履歴 grep の偽陽性回避。Codex review Major)。
    """
    if timeout is None:
        timeout = STOP_WAIT_TIMEOUT
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sliced = sidecar.read_journal_since(state_dir, offset)
        if any(e.get("event") == "broker_stopped" for e in sliced):
            return True
        if sidecar.read_sidecar(state_dir) is None:
            # sidecar 削除済み = finally 完了。最後にもう一度スライスを確認する。
            sliced = sidecar.read_journal_since(state_dir, offset)
            return any(e.get("event") == "broker_stopped" for e in sliced)
        time.sleep(_POLL_INTERVAL)
    return False


def _current_os() -> str:
    """実行 OS を ``'windows'`` / ``'darwin'`` / ``'linux'`` に正規化する platform
    seam。半死 daemon 案内のコマンド分岐 (ss / lsof / netstat 等) はこの関数経由で
    行う。

    テストがグローバルな ``os.name`` / ``sys.platform`` を monkeypatch すると、
    Windows ランナーで ``os.name='posix'`` を注入した瞬間に ``pathlib`` が壊れ
    (``PosixPath`` を生成できず) pytest の失敗レポート生成すら ``INTERNALERROR`` に
    なる。分岐点を関数へ切り出し、テストはここ **だけ** を差し替える (CI #143)。
    """
    if os.name == "nt" or sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def _half_dead_daemon_guidance(state_dir: str, host, port, pid) -> str:
    """admin.token 欠落 (半死 daemon) 時の ``org down`` 案内文を組み立てる (Issue #140)。

    従来の「investigate the daemon, then retry」は具体手段を欠いていた。sidecar の
    ``pid`` / ``host:port`` と ``sidecar.pid_alive`` を使い、(1) プロセス生存確認、
    (2) LISTEN 確認、(3) 生存なら SIGTERM で止めて retry / 死んでいれば stale
    sidecar を掃除、という追跡可能な手掛かりを提示する。

    - 出力は stderr。実端末 (cp932 コンソール等) でも壊れないよう **ASCII のみ**で
      構成する (em-dash 等 non-ascii 禁止。CLAUDE.md)。
    - コマンド例は OS で分岐する: Linux は ``ps`` / ``ss`` / ``kill``、macOS は
      LISTEN 確認のみ ``lsof`` (Darwin に ``ss`` は既定で無い)、Windows は
      ``tasklist`` / ``netstat`` / ``taskkill``。誤った OS のコマンドを案内して
      利用者を惑わせない。
    - ``pid_alive`` は保守的 (不確実なら生存扱い) なので、DEAD 判定が出たときだけ
      stale sidecar の削除を勧める。
    """
    cur_os = _current_os()
    win = cur_os == "windows"
    sidecar_path = os.path.join(state_dir, sidecar.SIDECAR_NAME)
    # LISTEN 確認は OS で使えるツールが異なる: Linux=ss, macOS=lsof (ss 不在),
    # Windows=netstat。
    if win:
        port_probe = f"netstat -ano | findstr :{port}"
    elif cur_os == "darwin":
        port_probe = f"lsof -nP -iTCP:{port} -sTCP:LISTEN"
    else:
        port_probe = f"ss -ltnp | grep {port}"
    # path はスペースや引用符を含みうるので、そのまま貼れるよう対象シェル流に
    # quote する。POSIX は shlex.quote (単一引用符の混入も安全に escape)、Windows は
    # 二重引用符 (``"`` は Windows のファイル名で不正 = path に混入しえない)。
    rm_cmd = (f'del "{sidecar_path}"' if win else f"rm {shlex.quote(sidecar_path)}")
    lines = [
        "org down: no admin.token found, so shutdown could not be requested; "
        "the daemon may still be live. Leaving the sidecar in place "
        f"({sidecar.SIDECAR_NAME}) so a live daemon is not orphaned.",
    ]
    if isinstance(pid, int) and pid > 0:
        proc_probe = (f'tasklist /FI "PID eq {pid}"' if win
                      else f"ps -p {pid}")
        # graceful stop: POSIX は kill = SIGTERM、Windows は taskkill (/F で強制)。
        stop_cmd = (f"taskkill /PID {pid}" if win else f"kill {pid}")
        stop_note = "add /F to force" if win else "SIGTERM"
        if sidecar.pid_alive(pid):
            lines += [
                f"  recorded pid {pid} looks ALIVE. To confirm and stop it:",
                f"    1) confirm the process:   {proc_probe}",
                f"    2) confirm it listens:    {port_probe}",
                f"    3) stop it ({stop_note}), then rerun 'org down':   "
                f"{stop_cmd}",
            ]
        else:
            lines += [
                f"  recorded pid {pid} looks DEAD (no such process); the sidecar "
                "is probably stale. Verify, then clean up:",
                f"    1) confirm no process:    {proc_probe}",
                f"    2) confirm nothing binds: {port_probe}",
                f"    3) if nothing holds {host}:{port}, remove the stale "
                f"sidecar:   {rm_cmd}",
            ]
    else:
        lines += [
            "  the sidecar records no usable pid; probe the endpoint instead:",
            f"    1) check the listener:    {port_probe}",
            f"    2) if a process holds {host}:{port}, stop it, then rerun "
            "'org down';",
            f"       otherwise remove the stale sidecar:   {rm_cmd}",
        ]
    return "\n".join(lines)


def _org_down_daemon(args: argparse.Namespace, state_dir: str) -> int:
    """daemon 停止本体 (sidecar 発見 → pane close → shutdown → 検証 → 後始末)。

    ``org_down`` から抽出した (Issue #142)。resident pre-flight は ``org_down`` が本関数の
    **後** に post-flight として回すので、停止の成否 (戻り値) は sweep で変えない。
    ``org_down`` は teardown 前に sidecar を読んでおり (root_cwd の取得)、本関数が sidecar を
    消しても ownership アンカーは失われない。
    """
    sc = sidecar.read_sidecar(state_dir)
    if sc is None:
        print(f"org down: no daemon sidecar under {state_dir!r}; nothing to stop.")
        return 0

    host, port = sc["host"], sc["port"]
    offset = sc.get("journal_offset", 0)
    admin_token = sidecar.read_admin_token(state_dir)
    # close 範囲は sidecar に記録された backend の isolated_session 能力で決める
    # (案 B: isolated=tmux は全 broker ペイン / global-mux=wezterm は agent 子のみ)。
    isolated = _backend_is_isolated(sc.get("backend"))

    closed: list = []
    reachable = False
    attempted_admin = admin_token is not None
    if admin_token is not None:
        # pane 操作には pane 権限を持つ token が要る。down は **無名 (auto-unique)**
        # の制御 token を mint する: name="secretary" だと停止対象の生存 secretary と
        # 衝突 (name_taken) するため、必ず無名で発行する。
        try:
            ctrl = _admin_rpc(host, port, admin_token, "mint_token",
                              {"role": "secretary"})
            reachable = True
        except urllib.error.URLError:
            ctrl = None
        if ctrl and ctrl.get("ok"):
            try:
                closed = _close_managed_panes(
                    host, port, ctrl["token"], isolated=isolated,
                )
            except urllib.error.URLError:
                pass  # MCP 面が落ちていても shutdown は試みる
        # graceful shutdown (シグナル非依存)。
        try:
            _admin_rpc(host, port, admin_token, "shutdown")
            reachable = True
        except urllib.error.URLError:
            pass

    if closed:
        print(f"org down: closed {len(closed)} broker pane(s): {closed}")

    stopped = _wait_for_stop(state_dir, offset)

    # sidecar の削除は **daemon が止まった/死んでいる確証があるときだけ** 行う。
    # broker_stopped 未確認のまま無条件に消すと、停止に失敗した **生存** daemon の
    # 唯一の discovery / admin 経路を奪い、以後 org down で回収できなくする
    # (Codex review Blocker 対応)。
    if stopped:
        # clean stop。daemon の finally が既に消している場合が多いが冪等に後始末する。
        sidecar.remove_sidecar(state_dir)
        print(f"org down: broker_stopped verified at http://{host}:{port}; "
              f"sidecar removed.")
        return 0
    if attempted_admin and not reachable:
        # admin に一度も到達できなかった = daemon は死んでいる。sidecar は stale
        # なので安全に後始末する。
        sidecar.remove_sidecar(state_dir)
        print("org down: daemon was unreachable (dead); cleaned up stale sidecar.",
              file=sys.stderr)
        return 1
    if not attempted_admin:
        # admin.token が無く shutdown を要求できない。daemon が生存している可能性が
        # あるため sidecar は **残す** (誤って生存 daemon を孤立させない)。案内は
        # 「investigate」で終わらせず、pid / host:port から生存確認・停止・掃除の
        # 具体的な手掛かりを提示する (Issue #140)。
        print(_half_dead_daemon_guidance(state_dir, host, port, sc.get("pid")),
              file=sys.stderr)
        return 1
    # admin には到達できたが broker_stopped が timeout 内に観測できない。daemon は
    # まだ停止中 / 生存しているかもしれないので sidecar は残し、再試行に委ねる。
    print("org down: shutdown was requested but broker_stopped was not observed "
          "within the timeout; the daemon may still be stopping. Leaving the "
          "sidecar in place for a retry.", file=sys.stderr)
    return 1


def org_down(args: argparse.Namespace) -> int:
    """``org down`` 本体。daemon 停止 (:func:`_org_down_daemon`) の **後** に broker 管理外
    resident の pre-flight sweep を回す (Issue #142)。

    sweep は teardown の post-flight で回す (daemon が既に止まっているので、live resident の
    回収はここが本来の担い手)。停止の戻り値 (rc) は sweep で **変えない**: down の本務は
    teardown で、reap の不調が停止の成否を覆い隠してはならない。sweep は sidecar 有無に依らず
    **全経路**で回す (sidecar が無くても resident は存在しうる)。ownership アンカー root_cwd は
    ``--root-cwd`` 明示 > daemon.json の root_cwd > ``os.getcwd()`` の順で解決する
    (teardown が sidecar を消す前に読んでおく)。

    **回収 (--reap) は daemon 停止が確証できたときだけ**行う (codex P1)。``_org_down_daemon``
    が rc 0 を返すのは (a) ``broker_stopped`` 検証済 = daemon 確実に停止、または (b) sidecar
    不在 = そもそも動いていない、のいずれか。rc 非 0 は「半死 (admin.token 欠落 + pid 生存)」
    「shutdown 要求したが未確認」など **daemon が生存している可能性がある** 状態で、そこで reap
    すると停止していない現世代 org 自身の生きた resident を kill しうる。よって rc 非 0 では reap
    を **告知のみに降格** する (安全側 under-reap; daemon を落としてから再実行すればよい)。
    """
    state_dir = sidecar.absolutize(args.state_dir)
    sc_before = sidecar.read_sidecar(state_dir)
    rc = _org_down_daemon(args, state_dir)
    root_cwd = (
        sidecar.absolutize(args.root_cwd)
        if getattr(args, "root_cwd", None) is not None
        else ((sc_before.get("root_cwd") if sc_before else None) or os.getcwd())
    )
    reap = getattr(args, "reap", False)
    if reap and rc != 0:
        # daemon 停止が未確証。生存している可能性があるので reap せず告知のみに降格。
        print(
            "org down: daemon stop was not confirmed (see above); skipping --reap and "
            "only announcing residents. Re-run 'org down --reap' once the daemon is "
            "confirmed stopped.",
            file=sys.stderr,
        )
        reap = False
    residents.preflight_residents(
        state_dir, root_cwd, reap=reap, prefix="org down",
    )
    return rc


# ===========================================================================
# CLI wiring
# ===========================================================================

def _add_up_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--state-dir", default=DEFAULT_STATE_DIR,
        help=f"daemon state dir (sidecar / queue). Default: {DEFAULT_STATE_DIR}.",
    )
    parser.add_argument(
        "--backend", default=None,
        help=(
            "terminal backend for the daemon (default: OS auto - POSIX=tmux / "
            "Windows=wezterm). 'herdr' is an opt-in POSIX / WSL-only backend "
            "(not supported on native Windows). Must match a running daemon "
            "when reusing."
        ),
    )
    parser.add_argument(
        "--root-cwd", default=None,
        help=(
            "cwd given to the secretary bind = anchor for relative-cwd spawns "
            "(Issue #61). Default: the directory org up runs in (os.getcwd)."
        ),
    )
    parser.add_argument(
        "--name", default=DEFAULT_ROOT_NAME,
        help=f"secretary agent id/name to mint. Default: {DEFAULT_ROOT_NAME!r}.",
    )
    parser.add_argument(
        "--model", default=None,
        help="passed to the secretary TUI as --model <value>.",
    )
    parser.add_argument(
        "--permission-mode", default=None,
        help="passed to the secretary TUI as --permission-mode <value>.",
    )
    parser.add_argument(
        "--claude-arg", action="append", default=None, metavar="ARG",
        help=(
            "extra interactive claude flag appended after the structured fields "
            "(repeatable). Reserved/headless flags are rejected by the builder."
        ),
    )
    parser.add_argument(
        "--reap", action="store_true", default=False,
        help=(
            "terminate broker-unmanaged residents whose ownership AND identity both "
            "verify, and remove stale registrations (default: announce only). On a "
            "cold start only (never touches a reused live org's own residents)."
        ),
    )


def _add_down_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--state-dir", default=DEFAULT_STATE_DIR,
        help=f"daemon state dir to discover the sidecar. Default: {DEFAULT_STATE_DIR}.",
    )
    parser.add_argument(
        "--reap", action="store_true", default=False,
        help=(
            "terminate broker-unmanaged residents whose ownership AND identity both "
            "verify, and remove stale registrations (default: announce only)."
        ),
    )
    parser.add_argument(
        "--root-cwd", default=None,
        help=(
            "repo root used to match resident ownership (default: read from the daemon "
            "sidecar, else the directory org down runs in)."
        ),
    )


def add_subparsers(subparsers: argparse._SubParsersAction) -> None:
    """top-level CLI (``claude-org-runtime org ...``) に up / down を生やす。"""
    up_p = subparsers.add_parser(
        "up",
        help=(
            "Ensure a broker daemon is up (reuse if healthy, else start), mint a "
            "secretary token, write its 0600 mcp-config, and launch the secretary "
            "claude TUI."
        ),
    )
    _add_up_arguments(up_p)
    up_p.set_defaults(func=org_up)

    down_p = subparsers.add_parser(
        "down",
        help=(
            "Discover the broker daemon from its sidecar, close residual agent "
            "panes, request a signal-free shutdown, and verify broker_stopped."
        ),
    )
    _add_down_arguments(down_p)
    down_p.set_defaults(func=org_down)
