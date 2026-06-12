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
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from ..terminal import default_backend
from . import sidecar, surface

DEFAULT_STATE_DIR = ".state/broker"
SECRETARY_MCP_NAME = "secretary-mcp.json"
DEFAULT_ROOT_NAME = "secretary"

# daemon バックグラウンド起動後に sidecar (daemon.json + admin.token) が公開される
# のを待つ上限。子の stdout には依存しない (sidecar が唯一の情報源) ため poll する。
SIDECAR_WAIT_TIMEOUT = 20.0
# shutdown 要求後に daemon が finally (stop → sidecar 削除) を終えるのを待つ上限。
STOP_WAIT_TIMEOUT = 15.0
_POLL_INTERVAL = 0.05


# ===========================================================================
# HTTP クライアント (urllib 最小実装 — 制御面は localhost HTTP のみ)
# ===========================================================================

def _admin_rpc(
    host: str, port: int, admin_token: str, method: str,
    params: dict | None = None, *, timeout: float = 10.0,
) -> dict | None:
    """admin HTTP RPC を 1 回叩く。返り値は応答 JSON (本体なしは None)。

    接続不可 (daemon 不在/停止) は :class:`urllib.error.URLError` を送出する
    (= 呼び元が「到達不能 = 要起動」の判定に使う)。HTTP エラー応答 (401/400/404)
    は本体を parse して返す (RPC レベルの拒否は例外にしない)。
    """
    url = f"http://{host}:{port}/admin"
    body = json.dumps({"method": method, "params": params or {}}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {admin_token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
    return json.loads(raw) if raw else None


class _McpClient:
    """走行中 broker への最小 MCP-over-HTTP クライアント (initialize / tools)。

    org up の健全性確認 (initialize → tools/list 往復) と org down の pane 操作
    (list_panes / close_pane) に使う。conftest の MiniMcpClient を src 側に
    最小移植したもの (テスト harness と同じ JSON-RPC 契約)。接続不可は URLError。
    """

    def __init__(self, host: str, port: int, token: str, *, timeout: float = 10.0):
        self.url = f"http://{host}:{port}/mcp"
        self.token = token
        self.timeout = timeout
        self.session_id: str | None = None
        self._id = 0

    def _post(self, payload: dict) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.token}",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode("utf-8"),
            headers=headers, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self.session_id = sid
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raw = e.read()
        return json.loads(raw) if raw else {}

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        payload: dict = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            payload["params"] = params
        return self._post(payload)

    def initialize(self) -> dict:
        return self._rpc("initialize", {"protocolVersion": surface.PROTOCOL_VERSIONS[0]})

    def tools_list(self) -> list[dict]:
        res = self._rpc("tools/list")
        return (res.get("result") or {}).get("tools", [])

    def call_tool(self, name: str, args: dict | None = None) -> dict:
        res = self._rpc("tools/call", {"name": name, "arguments": args or {}})
        result = res.get("result") or {}
        content = result.get("content") or [{}]
        text = content[0].get("text", "{}")
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return {"raw": text, "isError": result.get("isError", False)}


def _mcp_surface_ok(host: str, port: int, token: str) -> bool:
    """minted token で MCP initialize → tools/list が往復し公開面が返ることを確認。

    secretary tier の token なので全 13 面が見える前提。往復できれば daemon の
    MCP 面は健全 (admin 面だけでなく per-agent 面も生きている)。接続不可は
    URLError を送出する (呼び元が握る)。
    """
    client = _McpClient(host, port, token)
    client.initialize()
    return len(client.tools_list()) > 0


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
    state_dir: str, timeout: float = SIDECAR_WAIT_TIMEOUT,
) -> tuple[dict, str]:
    """daemon.json と admin.token の双方が公開されるまで poll する。

    両方揃って初めて daemon は admin RPC を受けられる (admin.token は atomic
    publish。:func:`sidecar.read_admin_token` は空文字列を None 扱いにするため
    部分書きを拾わない)。タイムアウトは RuntimeError。
    """
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
    """
    return _admin_rpc(
        host, port, admin_token, "mint_token",
        {"role": "secretary", "name": name, "cwd": root_cwd},
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
    """
    return surface.build_claude_argv(
        mcp_config_json=json.dumps(mcp_config),
        model=model, permission_mode=permission_mode, extra_args=extra,
    )


def _launch_claude(argv: list[str]) -> int:
    """secretary TUI を起動する。POSIX は exec で置換、Windows は subprocess。

    POSIX: ``os.execvp`` で現プロセスを claude に置換する (TUI が端末を引き継ぐ。
    これ以降は返らない)。Windows: exec セマンティクスが無いため subprocess で
    起動し前景で待つ。claude バイナリが見つからない場合は 1 行コマンドを表示して
    人間に委ねる fallback (課金中立 argv はそのまま手で起動できる)。
    """
    if os.name != "nt":
        os.execvp(argv[0], argv)  # 返らない (プロセス置換)
        return 0  # pragma: no cover (execvp 成功時は到達しない)
    try:
        return subprocess.call(argv)
    except (FileNotFoundError, OSError):
        import shlex
        print("claude を起動できませんでした。以下を手動で実行してください:")
        print("  " + " ".join(shlex.quote(a) for a in argv))
        return 0


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

    # --- 健全性判定 (到達性ベース) ---------------------------------------
    sc = sidecar.read_sidecar(state_dir)
    admin_token = sidecar.read_admin_token(state_dir)
    if sc is not None and admin_token is not None:
        try:
            res = _mint_secretary(sc["host"], sc["port"], admin_token, name, root_cwd)
        except urllib.error.URLError:
            res = None  # 到達不能 = stale sidecar → 新規起動へ
        if res is not None:
            # daemon は **生存** している (admin RPC が応答した)。
            if res.get("ok"):
                # 生存 daemon の backend 不一致は競合 (新規起動で二重 daemon を
                # 作らない)。明示的にエラーで止める (down してから up しなおす)。
                if sc.get("backend") != requested_backend:
                    print(
                        f"org up: a daemon is already running with backend "
                        f"{sc.get('backend')!r}, but backend {requested_backend!r} "
                        f"was requested. Run 'org down' first, or omit --backend.",
                        file=sys.stderr,
                    )
                    return 2
                # admin 面に加え per-agent MCP 面の健全性も確認する。
                try:
                    if not _mcp_surface_ok(sc["host"], sc["port"], res["token"]):
                        print(
                            "org up: daemon admin is up but its MCP surface returned "
                            "no tools (unhealthy). Run 'org down' first.",
                            file=sys.stderr,
                        )
                        return 2
                except urllib.error.URLError:
                    print(
                        "org up: daemon admin is up but its MCP port is unreachable "
                        "(unhealthy). Run 'org down' first.",
                        file=sys.stderr,
                    )
                    return 2
                mint = res
                host, port = sc["host"], sc["port"]
                reused = True
            elif "name_taken" in (res.get("error") or ""):
                # 生存 daemon に secretary が既に登録済み = もう up している。
                # 二人目の (誤名の) secretary を起動せず no-op で報告する。
                print(
                    f"org up: a secretary ({name!r}) is already registered on the "
                    f"running daemon - org is already up. Use 'org down' to stop it."
                )
                return 0
            else:
                print(
                    f"org up: admin mint_token failed: {res.get('error')}",
                    file=sys.stderr,
                )
                return 2

    # --- 新規起動 (sidecar 不在 / 到達不能 = stale) ----------------------
    if not reused:
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
    return launch(argv)


# ===========================================================================
# org down
# ===========================================================================

_AGENT_PANE_KINDS = {"claude", "codex"}


def _close_managed_panes(host: str, port: int, token: str) -> list:
    """走行中 broker の残存エージェントペイン (claude/codex 子) を close する。

    secretary tier の制御 token で list_panes → close_pane を呼ぶ。close_pane が
    内部で token revoke / last-pane ガード / 論理ペイン拒否 / isolated_session の
    backend 別判定を行うので、down は薄く呼ぶだけ (制御面ロジックを再実装しない)。

    close 対象は **claude/codex の子ペイン** に限定する: list_panes は backend に
    よっては broker 管理外の pane (global-mux の無関係 wezterm pane など) も返す
    ため、kind で broker が spawn した org エージェントだけを選ぶ (無関係 pane の
    巻き添え kill を避ける)。接続不可は URLError を送出する (呼び元が握る)。
    """
    client = _McpClient(host, port, token)
    client.initialize()
    panes = client.call_tool("list_panes").get("panes", [])
    closed: list = []
    for pane in panes:
        if pane.get("kind") not in _AGENT_PANE_KINDS:
            continue
        res = client.call_tool("close_pane", {"target": str(pane.get("id"))})
        if res.get("ok"):
            closed.append(pane.get("id"))
    return closed


def _wait_for_stop(
    state_dir: str, offset: int, timeout: float = STOP_WAIT_TIMEOUT,
) -> bool:
    """daemon の停止を待ち、journal_offset スライスで broker_stopped を検証する。

    run() の finally は ``stop()`` (broker_stopped を append) → ``remove_sidecar()``
    の順に進むため、sidecar が消えた時点で broker_stopped は必ず書かれている。
    ``offset`` (= この run の起点) 以降のスライスのみを見て当該 run の
    broker_stopped を確認する (全履歴 grep の偽陽性回避。Codex review Major)。
    """
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


def org_down(args: argparse.Namespace) -> int:
    """``org down`` 本体。sidecar 発見 → pane close → shutdown → 検証 → 後始末。"""
    state_dir = sidecar.absolutize(args.state_dir)
    sc = sidecar.read_sidecar(state_dir)
    if sc is None:
        print(f"org down: no daemon sidecar under {state_dir!r}; nothing to stop.")
        return 0

    host, port = sc["host"], sc["port"]
    offset = sc.get("journal_offset", 0)
    admin_token = sidecar.read_admin_token(state_dir)

    closed: list = []
    reachable = False
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
                closed = _close_managed_panes(host, port, ctrl["token"])
            except urllib.error.URLError:
                pass  # MCP 面が落ちていても shutdown は試みる
        # graceful shutdown (シグナル非依存)。
        try:
            _admin_rpc(host, port, admin_token, "shutdown")
            reachable = True
        except urllib.error.URLError:
            pass

    if closed:
        print(f"org down: closed {len(closed)} agent pane(s): {closed}")

    stopped = _wait_for_stop(state_dir, offset)
    # sidecar 後始末 (daemon の finally と冪等。daemon が既に消していれば no-op)。
    sidecar.remove_sidecar(state_dir)

    if stopped:
        print(f"org down: broker_stopped verified at http://{host}:{port}; "
              f"sidecar removed.")
        return 0
    if not reachable:
        # sidecar はあったが daemon に到達できず broker_stopped も確認できない
        # (clean stop 後に sidecar だけ残った等)。後始末は済ませ警告で返す。
        print("org down: daemon was unreachable and broker_stopped could not be "
              "confirmed; cleaned up stale sidecar.", file=sys.stderr)
        return 1
    print("org down: shutdown was requested but broker_stopped was not observed "
          "within the timeout.", file=sys.stderr)
    return 1


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
            "Windows=wezterm). Must match a running daemon when reusing."
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


def _add_down_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--state-dir", default=DEFAULT_STATE_DIR,
        help=f"daemon state dir to discover the sidecar. Default: {DEFAULT_STATE_DIR}.",
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
