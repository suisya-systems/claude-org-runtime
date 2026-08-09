# -*- coding: utf-8 -*-
"""org up / org down launcher のテスト (runtime#63 タスク 2)。

launcher は制御面 (sidecar / admin RPC / journal_offset スライス) の薄い wrapper
なので、ここでは **wrapper の分岐**を検証する:

- up: 走行中 daemon の **再利用** と、不在時の **新規起動** の分岐。
- up: secretary が既に登録済みの生存 daemon は no-op (already up)。
- up: 生存 daemon の backend 不一致は競合エラー (二重 daemon を作らない)。
- up: secretary-mcp.json が 0600 で書かれる。
- up: 起動 argv に headless flag が混入しない (課金中立 builder 経由)。
- down: journal_offset スライスで broker_stopped を厳密 1 回検証する。

走行中 daemon は本物の :class:`Broker` を ephemeral port で起動し、sidecar を
ディスクに書いて launcher に発見させる。claude TUI 起動 (exec/subprocess) と
daemon バックグラウンド起動 (subprocess.Popen) は注入差し替えで副作用を避ける。
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import stat
import threading
import time

import pytest

from claude_org_runtime.broker import cli as broker_cli
from claude_org_runtime.broker import launcher
from claude_org_runtime.broker import sidecar
from claude_org_runtime.broker.server import Broker
from claude_org_runtime.terminal import default_backend

from .conftest import FakeAdapter


# --------------------------------------------------------------------- helpers
def _up_args(state_dir, *, backend=None, name="secretary", root_cwd=None,
             model=None, permission_mode=None, claude_arg=None, reap=False):
    return argparse.Namespace(
        state_dir=str(state_dir), backend=backend, name=name,
        root_cwd=root_cwd, model=model, permission_mode=permission_mode,
        claude_arg=claude_arg, reap=reap,
    )


def _down_args(state_dir, *, reap=False, root_cwd=None):
    return argparse.Namespace(state_dir=str(state_dir), reap=reap, root_cwd=root_cwd)


def _adopt_args(state_dir, *, owner="w1", resume=None, continue_session=False,
                in_flight="requeue", force=False, status=False, root_cwd=None,
                model=None, permission_mode=None, claude_arg=None):
    """``org adopt`` の args namespace (#166)。既定は parser の既定と揃える。"""
    return argparse.Namespace(
        state_dir=str(state_dir), owner=owner, resume=resume,
        continue_session=continue_session, in_flight=in_flight, force=force,
        status=status, root_cwd=root_cwd, model=model,
        permission_mode=permission_mode, claude_arg=claude_arg,
    )


@pytest.fixture
def live_daemon(tmp_path):
    """本物の started Broker (admin token 付き) + ディスク上の sidecar。

    launcher が read_sidecar / read_admin_token で発見できるよう、sidecar を
    実 broker の host/port に向けて書く。backend は OS 既定 (up が --backend 省略時に
    要求する値) を記録し、再利用分岐がデフォルトで成立するようにする。
    """
    state_dir = str(tmp_path / "broker")
    b = Broker(state_dir=state_dir, adapter=None, port=0, admin_token="ADMIN-SECRET")
    b.start()
    sidecar.write_sidecar(
        state_dir, pid=os.getpid(), host=b.host, port=b.port,
        backend=default_backend(), started_at=time.time(),
        journal_offset=0,
    )
    sidecar.write_admin_token(state_dir, "ADMIN-SECRET")
    try:
        yield b, state_dir
    finally:
        b.stop()


# ===================================================================== up: reuse
def test_org_up_reuses_live_healthy_daemon(live_daemon):
    b, state_dir = live_daemon
    captured = {}
    spawn_calls = []

    def fake_spawn(*a, **k):
        spawn_calls.append((a, k))
        raise AssertionError("spawn_daemon must not run when reusing a live daemon")

    def fake_launch(argv, state_dir=None, observer_secret=None, root_cwd=None):
        captured["argv"] = argv
        captured["state_dir"] = state_dir
        captured["observer_secret"] = observer_secret
        return 0

    rc = launcher.org_up(_up_args(state_dir), spawn_daemon=fake_spawn,
                         launch=fake_launch)
    assert rc == 0
    assert spawn_calls == []                       # 再利用 → 新規起動しない
    # org_up threads the (absolutized) state_dir into launch so the root
    # secretary env gets ORG_BROKER_STATE_DIR (#122).
    assert captured["state_dir"] == sidecar.absolutize(state_dir)
    # secretary token が mint され、mcp-config が書かれ、argv が組まれた。
    cfg_path = os.path.join(state_dir, "secretary-mcp.json")
    cfg = json.loads(open(cfg_path, encoding="utf-8").read())
    hdr = cfg["mcpServers"]["org-broker"]["headers"]["Authorization"]
    minted_token = hdr.removeprefix("Bearer ")
    assert b.get_bind(minted_token) is not None
    assert b.get_bind(minted_token).auth_role == "secretary"
    # launch に渡った argv が claude 対話 TUI。
    assert captured["argv"][0] == "claude"
    assert "--mcp-config" in captured["argv"]


def test_org_up_reused_secretary_named_secretary(live_daemon):
    b, state_dir = live_daemon
    launcher.org_up(_up_args(state_dir, name="secretary"),
                    spawn_daemon=lambda *a, **k: (_ for _ in ()).throw(AssertionError()),
                    launch=lambda argv, state_dir=None, observer_secret=None, root_cwd=None: 0)
    # mint された bind の agent_id は 'secretary' (root name 契約)。
    assert any(bnd.agent_id == "secretary" and bnd.auth_role == "secretary"
               for bnd in b._binds.values())


# =============================================================== up: already up
def test_org_up_noop_when_secretary_already_registered(live_daemon):
    b, state_dir = live_daemon
    # 既に secretary が登録済みの状態を作る (前回 up の残り)。
    b.issue_token("secretary", "secretary", "secretary", auth_role="secretary",
                  unique=True)
    launched = []
    rc = launcher.org_up(
        _up_args(state_dir, name="secretary"),
        spawn_daemon=lambda *a, **k: (_ for _ in ()).throw(AssertionError()),
        launch=lambda argv, state_dir=None, observer_secret=None, root_cwd=None: launched.append(argv) or 0,
    )
    assert rc == 0
    assert launched == []                          # 二人目の secretary を起動しない
    # 0600 config も書かない (mint していない)。
    assert not os.path.exists(os.path.join(state_dir, "secretary-mcp.json"))


# =============================================================== up: backend 競合
def test_org_up_errors_on_live_backend_conflict(live_daemon):
    b, state_dir = live_daemon  # sidecar backend = default_backend()
    other = "wezterm" if default_backend() != "wezterm" else "tmux"
    launched = []
    rc = launcher.org_up(
        _up_args(state_dir, backend=other),
        spawn_daemon=lambda *a, **k: (_ for _ in ()).throw(AssertionError()),
        launch=lambda argv, state_dir=None, observer_secret=None, root_cwd=None: launched.append(argv) or 0,
    )
    assert rc == 2                                 # 競合エラー
    assert launched == []                          # 起動しない


# =========================================== up: backend x platform fail-fast (#120)
def test_org_up_fails_fast_on_herdr_native_windows(tmp_path, monkeypatch, capsys):
    """herdr on native Windows fails fast (rc 2) *before* any daemon spawn.

    Regression for Issue #120: the daemon used to die on boot (no AF_UNIX socket
    on native Windows) and org up only surfaced a 20s no-info sidecar timeout.
    Now the launcher validates backend x platform via the shared SoT helper and
    returns an ASCII, actionable error naming wezterm - no spawn_daemon call.
    """
    monkeypatch.setattr(launcher.os, "name", "nt")
    state_dir = str(tmp_path / "broker")
    launched = []
    rc = launcher.org_up(
        _up_args(state_dir, backend="herdr"),
        spawn_daemon=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("spawn_daemon must not run for an unsupported backend")
        ),
        launch=lambda argv, state_dir=None, observer_secret=None, root_cwd=None: launched.append(argv) or 0,
    )
    assert rc == 2
    assert launched == []                          # secretary TUI not launched
    err = capsys.readouterr().err
    assert "herdr" in err
    # Fully actionable: names all three escape hatches the AC mandates.
    assert "wezterm" in err                        # native-Windows alternative
    assert "WSL" in err                            # or run under WSL
    assert "renga" in err                          # or the renga transport
    err.encode("cp932")                            # cp932-safe (no em-dash / kanji)
    err.encode("ascii")                            # strictly ASCII


def test_org_up_errors_on_unknown_backend(tmp_path, capsys):
    """An unknown --backend is rejected distinctly from an unsupported one."""
    state_dir = str(tmp_path / "broker")
    launched = []
    rc = launcher.org_up(
        _up_args(state_dir, backend="screen"),
        spawn_daemon=lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("spawn_daemon must not run for an unknown backend")
        ),
        launch=lambda argv, state_dir=None, observer_secret=None, root_cwd=None: launched.append(argv) or 0,
    )
    assert rc == 2
    assert launched == []
    err = capsys.readouterr().err
    assert "unknown backend" in err
    assert "screen" in err


# ===================================================================== up: fresh
def test_org_up_starts_fresh_when_no_daemon(tmp_path):
    """sidecar 不在 → spawn_daemon が呼ばれ、その daemon に mint して起動する。"""
    state_dir = str(tmp_path / "broker")
    # 注入 spawn: 本物の Broker を起動し host/port/admin_token を返す。
    started: list[Broker] = []

    def fake_spawn(sd, backend, root_cwd):
        assert sd == sidecar.absolutize(state_dir)
        b = Broker(state_dir=sd, adapter=None, port=0, admin_token="FRESH-ADMIN")
        b.start()
        started.append(b)
        return b.host, b.port, "FRESH-ADMIN"

    captured = {}

    def fake_launch(argv, state_dir=None, observer_secret=None, root_cwd=None):
        captured["argv"] = argv
        return 0

    rc = launcher.org_up(_up_args(state_dir), spawn_daemon=fake_spawn,
                         launch=fake_launch)
    try:
        assert rc == 0
        assert len(started) == 1                   # 新規起動された
        b = started[0]
        # mint された secretary token が新 daemon に bind され、TUI argv が組まれた。
        cfg_path = os.path.join(state_dir, "secretary-mcp.json")
        cfg = json.loads(open(cfg_path, encoding="utf-8").read())
        tok = cfg["mcpServers"]["org-broker"]["headers"]["Authorization"].removeprefix("Bearer ")
        assert b.get_bind(tok).auth_role == "secretary"
        assert captured["argv"][0] == "claude"
    finally:
        for b in started:
            b.stop()


# ================================================ up: stale sidecar cold notice (#141)
def test_org_up_notifies_when_discarding_stale_sidecar(tmp_path, monkeypatch, capsys):
    """到達不能な stale sidecar を無警告で cold 上書きしない (Issue #141)。

    前回 daemon が clean に終われば org down が daemon.json を消しているはずで、
    残存 sidecar + 到達不能はクラッシュ / 強制終了のサイン。org up はそこへ到達
    できず cold へ倒れ、新 daemon を起動する。その際 stderr に 1 行の告知を残す。

    stale host/port への probe **だけ** を URLError にし、新 daemon への mint は
    本物の ``_admin_rpc`` で通す (グローバル os/sys の monkeypatch はしない。#143)。
    """
    import urllib.error

    state_dir = str(tmp_path / "broker")
    stale_pid = 4321
    stale_host, stale_port = "127.0.0.1", 59999
    # daemon.json + admin.token を書く (admin.token 有 → 半公開ではなく probe へ進む)。
    sidecar.write_sidecar(
        state_dir, pid=stale_pid, host=stale_host, port=stale_port,
        backend=default_backend(), started_at=time.time(), journal_offset=0,
    )
    sidecar.write_admin_token(state_dir, "STALE-ADMIN")

    # stale host/port への RPC だけ到達不能にし、新 daemon への mint は素通しする。
    real_rpc = launcher._admin_rpc

    def selective_rpc(host, port, *a, **k):
        if (host, port) == (stale_host, stale_port):
            raise urllib.error.URLError("refused")
        return real_rpc(host, port, *a, **k)

    monkeypatch.setattr(launcher, "_admin_rpc", selective_rpc)

    started: list[Broker] = []

    def fake_spawn(sd, backend, root_cwd):
        b = Broker(state_dir=sd, adapter=None, port=0, admin_token="FRESH-ADMIN")
        b.start()
        started.append(b)
        return b.host, b.port, "FRESH-ADMIN"

    rc = launcher.org_up(
        _up_args(state_dir), spawn_daemon=fake_spawn,
        launch=lambda argv, state_dir=None, observer_secret=None, root_cwd=None: 0,
    )
    try:
        assert rc == 0
        assert len(started) == 1                       # stale を捨てて新規起動
        err = capsys.readouterr().err
        assert f"discarded stale sidecar for dead pid={stale_pid}" in err
        assert "did not shut down cleanly" in err
        assert "starting fresh" in err
    finally:
        for b in started:
            b.stop()


def test_org_up_no_stale_notice_on_clean_first_start(tmp_path, capsys):
    """sidecar 不在の正常な初回起動では stale 告知を出さない (誤警告を避ける)。"""
    state_dir = str(tmp_path / "broker")
    started: list[Broker] = []

    def fake_spawn(sd, backend, root_cwd):
        b = Broker(state_dir=sd, adapter=None, port=0, admin_token="FRESH-ADMIN")
        b.start()
        started.append(b)
        return b.host, b.port, "FRESH-ADMIN"

    rc = launcher.org_up(
        _up_args(state_dir), spawn_daemon=fake_spawn,
        launch=lambda argv, state_dir=None, observer_secret=None, root_cwd=None: 0,
    )
    try:
        assert rc == 0
        assert len(started) == 1
        assert "stale sidecar" not in capsys.readouterr().err
    finally:
        for b in started:
            b.stop()


# =============================================================== up: 0600 config
def test_secretary_mcp_config_written_0600(tmp_path):
    cfg = {"mcpServers": {"org-broker": {"type": "http", "url": "http://x",
                                         "headers": {"Authorization": "Bearer T"}}}}
    path = launcher.write_secretary_mcp_config(str(tmp_path), cfg)
    assert json.loads(path.read_text(encoding="utf-8")) == cfg
    assert not (tmp_path / (launcher.SECRETARY_MCP_NAME + ".tmp")).exists()
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode & stat.S_IRUSR
    if os.name != "nt":
        assert mode & (stat.S_IRWXG | stat.S_IRWXO) == 0


# ====================================================== up: 課金中立 argv (headless)
def test_up_argv_has_no_headless_flag():
    cfg = {"mcpServers": {}}
    argv = launcher.build_up_argv(cfg, model="opus", permission_mode="default")
    assert argv[0] == "claude"
    assert "--mcp-config" in argv
    assert "--model" in argv and "opus" in argv
    # headless flag は構造的に混入しない。
    for bad in ("-p", "--print", "--output-format", "--headless"):
        assert bad not in argv


def test_up_argv_rejects_headless_extra():
    from claude_org_runtime.broker.surface import ToolArgError
    with pytest.raises(ToolArgError):
        launcher.build_up_argv({"mcpServers": {}}, extra=["-p"])


# ============================================== up: channel 配線 (dev-channel flag)
def test_up_argv_adds_dev_channel_flag_when_config_has_channel():
    # mcp_config に org-broker-channel sidecar があれば、子経路と同じ dev-channel
    # flag が argv に乗る (secretary 起動経路の push 一次配送 = 本タスクの本丸)。
    cfg = {"mcpServers": {
        "org-broker": {"type": "http", "url": "http://x",
                       "headers": {"Authorization": "Bearer T"}},
        "org-broker-channel": {"command": "py", "args": ["-m", "x"], "env": {}},
    }}
    argv = launcher.build_up_argv(cfg)
    assert "--dangerously-load-development-channels" in argv
    i = argv.index("--dangerously-load-development-channels")
    assert argv[i + 1] == "server:org-broker-channel"


def test_up_argv_no_dev_channel_flag_without_channel():
    # channel sidecar が無い mcp_config では dev-channel flag を出さない
    # (flag は config 実体に従属。drift しない)。
    cfg = {"mcpServers": {"org-broker": {"type": "http", "url": "http://x",
                                         "headers": {"Authorization": "Bearer T"}}}}
    argv = launcher.build_up_argv(cfg)
    assert "--dangerously-load-development-channels" not in argv


def test_org_up_wires_channel_into_secretary(live_daemon):
    # end-to-end: org up の secretary mint が secretary-mcp.json に org-broker-channel を
    # 書き、launch argv に dev-channel flag を付ける (root も子と同じ channel 配線)。
    b, state_dir = live_daemon
    captured = {}

    def fake_launch(argv, state_dir=None, observer_secret=None, root_cwd=None):
        captured["argv"] = argv
        captured["observer_secret"] = observer_secret
        return 0

    rc = launcher.org_up(
        _up_args(state_dir, name="secretary"),
        spawn_daemon=lambda *a, **k: (_ for _ in ()).throw(AssertionError()),
        launch=fake_launch,
    )
    assert rc == 0
    # 書き出された 0600 mcp-config に channel sidecar が積まれている。
    cfg_path = os.path.join(state_dir, "secretary-mcp.json")
    cfg = json.loads(open(cfg_path, encoding="utf-8").read())
    assert "org-broker-channel" in cfg["mcpServers"]
    assert cfg["mcpServers"]["org-broker-channel"]["env"][
        "ORG_BROKER_CHANNEL_OWNER"] == "secretary"
    # secretary 宛の delivery-scoped credential が daemon 側に発行されている。
    assert any(bnd.scope == "delivery" and bnd.agent_id == "secretary"
               for bnd in b._binds.values())
    # launch argv に dev-channel flag が乗る (子経路ミラー)。
    argv = captured["argv"]
    assert "--dangerously-load-development-channels" in argv
    assert "server:org-broker-channel" in argv
    # observed-session binding (Issue #129 問題 A): channel mint が observer 秘密を返し、
    # それが launch へ threaded される (子プロセス env 注入の材料)。
    observer_secret = captured["observer_secret"]
    assert observer_secret and isinstance(observer_secret, str)
    # 秘密は **persisted mcp-config には載らない** (fork replay 面。ここに入ると
    # observed 束縛が復元でき破れる)。非 replay の env 信号であることを固定する。
    raw_cfg = open(cfg_path, encoding="utf-8").read()
    assert observer_secret not in raw_cfg
    # daemon 側に observer lease が assert されている (delivery_dump の observers)。
    assert "secretary" in b.delivery_dump()["observers"]


# ===================================================================== down
def test_org_down_verifies_broker_stopped_via_offset_slice(tmp_path):
    """run() を thread で起動し、org down が shutdown → offset スライスで
    broker_stopped を厳密 1 回検証し、sidecar を後始末することを end-to-end で確認。"""
    state_dir = str(tmp_path / "broker")
    args = broker_cli.build_parser().parse_args(
        ["serve", "--port", "0", "--no-nudge", "--state-dir", state_dir]
    )
    rc_box: dict = {}
    t = threading.Thread(target=lambda: rc_box.setdefault("rc", broker_cli.run(args)),
                         daemon=True)
    t.start()

    # sidecar 公開待ち。
    deadline = time.time() + 10
    while time.time() < deadline:
        if (sidecar.read_sidecar(state_dir) is not None
                and sidecar.read_admin_token(state_dir) is not None):
            break
        time.sleep(0.02)
    assert sidecar.read_sidecar(state_dir) is not None, "sidecar never published"

    rc = launcher.org_down(_down_args(state_dir))
    assert rc == 0

    t.join(timeout=10)
    assert not t.is_alive(), "run() did not return after org down"
    assert rc_box["rc"] == 0

    # sidecar は後始末済み。
    assert sidecar.read_sidecar(state_dir) is None
    assert sidecar.read_admin_token(state_dir) is None
    # journal_offset=0 起点でも当該 run の broker_stopped は 1 回。
    sliced = sidecar.read_journal_since(state_dir, 0)
    stopped = [e for e in sliced if e.get("event") == "broker_stopped"]
    assert len(stopped) == 1
    assert any(e.get("event") == "broker_started" for e in sliced)


def test_org_down_no_sidecar_is_noop(tmp_path):
    rc = launcher.org_down(_down_args(str(tmp_path / "broker")))
    assert rc == 0


# =================================================== up: split-brain guard (Blocker)
def test_org_up_does_not_cold_start_when_admin_token_missing(tmp_path, monkeypatch):
    """daemon.json はあるが admin.token が (grace 内に) 現れない半公開状態では、
    新規 daemon を二重起動してはならない (split-brain 回避。Codex review Blocker)。"""
    state_dir = str(tmp_path / "broker")
    # daemon.json のみ書く (admin.token は書かない = 公開途中 / クラッシュを模す)。
    sidecar.write_sidecar(
        state_dir, pid=4321, host="127.0.0.1", port=59999,
        backend=default_backend(), started_at=time.time(), journal_offset=0,
    )
    # grace を短縮してテストを速くする。
    monkeypatch.setattr(launcher, "ADMIN_TOKEN_GRACE", 0.2)
    launched = []

    def fake_spawn(*a, **k):
        raise AssertionError("must not spawn a second daemon over a claimed state_dir")

    rc = launcher.org_up(_up_args(state_dir), spawn_daemon=fake_spawn,
                         launch=lambda argv, state_dir=None, observer_secret=None, root_cwd=None: launched.append(argv) or 0)
    assert rc == 2                                 # token_missing → 明示エラー
    assert launched == []                          # TUI も起動しない
    assert not os.path.exists(os.path.join(state_dir, "secretary-mcp.json"))


# ================================================ down: keep sidecar if not stopped (Blocker)
def test_org_down_keeps_sidecar_when_stop_unconfirmed(tmp_path, monkeypatch):
    """shutdown を要求しても broker_stopped を確認できない (= 生存中かもしれない)
    daemon の sidecar は **消さない** (孤立させない。Codex review Blocker)。

    run() ループを持たない started Broker を使う: shutdown RPC は _shutdown_event を
    立てるが待つ側がいないので broker_stopped は書かれず、daemon は生き続ける。
    """
    state_dir = str(tmp_path / "broker")
    b = Broker(state_dir=state_dir, adapter=None, port=0, admin_token="ADMIN-SECRET")
    b.start()
    sidecar.write_sidecar(
        state_dir, pid=os.getpid(), host=b.host, port=b.port,
        backend=default_backend(), started_at=time.time(), journal_offset=0,
    )
    sidecar.write_admin_token(state_dir, "ADMIN-SECRET")
    monkeypatch.setattr(launcher, "STOP_WAIT_TIMEOUT", 0.3)
    try:
        rc = launcher.org_down(_down_args(state_dir))
        assert rc == 1                             # 停止未確認
        # 生存 daemon の discovery / admin 経路は残す。
        assert sidecar.read_sidecar(state_dir) is not None
        assert sidecar.read_admin_token(state_dir) is not None
    finally:
        b.stop()


def test_org_down_cleans_stale_sidecar_when_unreachable(tmp_path, monkeypatch):
    """daemon に一度も到達できない (dead) ときは stale sidecar を後始末して返す。"""
    import urllib.error

    state_dir = str(tmp_path / "broker")
    sidecar.write_sidecar(
        state_dir, pid=4321, host="127.0.0.1", port=59998,
        backend=default_backend(), started_at=time.time(), journal_offset=0,
    )
    sidecar.write_admin_token(state_dir, "STALE-ADMIN")
    # admin RPC を確定的に「到達不能」にする (OS の connect-timeout 挙動に依存しない)。
    monkeypatch.setattr(
        launcher, "_admin_rpc",
        lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("refused")),
    )
    monkeypatch.setattr(launcher, "STOP_WAIT_TIMEOUT", 0.3)
    rc = launcher.org_down(_down_args(state_dir))
    assert rc == 1
    assert sidecar.read_sidecar(state_dir) is None       # stale → 後始末済み
    assert sidecar.read_admin_token(state_dir) is None


def test_org_down_keeps_sidecar_when_admin_token_missing(tmp_path, monkeypatch):
    """admin.token が無く shutdown を要求できない場合は、生存 daemon を孤立させない
    よう sidecar を残す (daemon.json のみで誤って discovery 経路を消さない)。"""
    state_dir = str(tmp_path / "broker")
    sidecar.write_sidecar(
        state_dir, pid=4321, host="127.0.0.1", port=59997,
        backend=default_backend(), started_at=time.time(), journal_offset=0,
    )
    # admin.token は書かない。
    monkeypatch.setattr(launcher, "STOP_WAIT_TIMEOUT", 0.3)
    rc = launcher.org_down(_down_args(state_dir))
    assert rc == 1
    assert sidecar.read_sidecar(state_dir) is not None    # discovery 経路を残す


# ============================= down: half-dead daemon guidance (Issue #140)
def _pin_os(monkeypatch, os_name):
    """ガイダンスの OS 分岐 seam (``launcher._current_os``) **だけ** を固定し、
    コマンド期待値をどの CI ランナー (Linux/macOS/Windows) でも決定的にする。

    ``os_name`` は ``'linux'`` / ``'darwin'`` / ``'windows'``。グローバルな
    ``os.name`` / ``sys.platform`` は patch しない: Windows ランナーで
    ``os.name='posix'`` を注入すると ``pathlib`` が壊れ pytest 自体が
    ``INTERNALERROR`` になるため (CI #143 3周目)。"""
    monkeypatch.setattr(launcher, "_current_os", lambda: os_name)


def test_current_os_returns_known_token():
    """platform seam は既知 3 値のいずれかを返す (どのランナーでも成立)。分岐の
    期待値は _pin_os でこの seam を差し替えて決定的に検証する。"""
    assert launcher._current_os() in {"linux", "darwin", "windows"}


def test_org_down_guidance_when_pid_alive_gives_stop_hint(
        tmp_path, monkeypatch, capsys):
    """admin.token 欠落かつ pid が生存とみなせる半死状態では、案内に生存確認
    (ps -p) / LISTEN 確認 (ss ... grep) / SIGTERM 停止 (kill) の具体手掛かりを
    含める。sidecar は残す (生存 daemon を孤立させない)。"""
    _pin_os(monkeypatch, "linux")  # Linux ツール期待値を決定的に
    state_dir = str(tmp_path / "broker")
    sidecar.write_sidecar(
        state_dir, pid=4321, host="127.0.0.1", port=59997,
        backend=default_backend(), started_at=time.time(), journal_offset=0,
    )
    # pid 生存判定を確定的に固定する (実プロセスに依存しない)。
    monkeypatch.setattr(launcher.sidecar, "pid_alive", lambda _pid: True)
    monkeypatch.setattr(launcher, "STOP_WAIT_TIMEOUT", 0.3)

    rc = launcher.org_down(_down_args(state_dir))
    err = capsys.readouterr().err

    assert rc == 1
    assert "ALIVE" in err
    assert "ps -p 4321" in err            # (1) プロセス生存確認
    assert "ss -ltnp | grep 59997" in err  # (2) LISTEN 確認
    assert "kill 4321" in err              # (3) SIGTERM 停止
    assert "SIGTERM" in err
    assert sidecar.read_sidecar(state_dir) is not None


def test_org_down_guidance_when_pid_dead_suggests_stale_cleanup(
        tmp_path, monkeypatch, capsys):
    """pid が「確実に死んでいる」ときは stale sidecar の掃除 (rm daemon.json) を
    案内する。ただし down 自体は保守側で sidecar を残す (誤削除しない)。"""
    _pin_os(monkeypatch, "linux")  # rm/ss 期待値を決定的に
    state_dir = str(tmp_path / "broker")
    sidecar.write_sidecar(
        state_dir, pid=4321, host="127.0.0.1", port=59996,
        backend=default_backend(), started_at=time.time(), journal_offset=0,
    )
    monkeypatch.setattr(launcher.sidecar, "pid_alive", lambda _pid: False)
    monkeypatch.setattr(launcher, "STOP_WAIT_TIMEOUT", 0.3)

    rc = launcher.org_down(_down_args(state_dir))
    err = capsys.readouterr().err

    assert rc == 1
    assert "DEAD" in err
    assert "stale" in err
    # 掃除コマンドは実装と同じ変換 (os.path.join + shlex.quote) で期待値を作る。
    # Windows では tmp_path が backslash を含み、shlex.quote が path を quote する
    # (backslash は shlex 的に unsafe) ため、直書き `rm <path>` だと不一致で落ちる。
    # 実装の変換を鏡写しにすることで全 OS で決定的に一致させる (CI #143 4周目)。
    sidecar_path = os.path.join(state_dir, sidecar.SIDECAR_NAME)
    assert f"rm {shlex.quote(sidecar_path)}" in err
    # 保守: 案内はしても down 自体は sidecar を残す (確証なき削除をしない)。
    assert sidecar.read_sidecar(state_dir) is not None


def test_half_dead_guidance_alive_linux_commands(monkeypatch):
    """Linux では ps -p / ss ... grep <port> / kill <pid> (SIGTERM) を提示する。"""
    _pin_os(monkeypatch, "linux")
    monkeypatch.setattr(launcher.sidecar, "pid_alive", lambda _pid: True)
    msg = launcher._half_dead_daemon_guidance(
        "/tmp/isolated/broker", "127.0.0.1", 59997, 4321)
    assert "ALIVE" in msg
    assert "ps -p 4321" in msg
    assert "ss -ltnp | grep 59997" in msg
    assert "kill 4321" in msg
    assert "SIGTERM" in msg


def test_half_dead_guidance_macos_uses_lsof_not_ss(monkeypatch):
    """macOS (Darwin) は ss が既定で無いので LISTEN 確認に lsof を提示する。
    プロセス確認・停止は POSIX 共通 (ps -p / kill) のまま。"""
    _pin_os(monkeypatch, "darwin")
    monkeypatch.setattr(launcher.sidecar, "pid_alive", lambda _pid: True)
    msg = launcher._half_dead_daemon_guidance(
        "/tmp/isolated/broker", "127.0.0.1", 59997, 4321)
    assert "lsof -nP -iTCP:59997 -sTCP:LISTEN" in msg
    assert "ss -ltnp" not in msg           # Linux 専用ツールを macOS に出さない
    assert "ps -p 4321" in msg and "kill 4321" in msg


def test_half_dead_guidance_windows_commands(monkeypatch):
    """Windows では tasklist / netstat / taskkill を提示し、POSIX コマンドを
    誤案内しない。"""
    _pin_os(monkeypatch, "windows")
    monkeypatch.setattr(launcher.sidecar, "pid_alive", lambda _pid: True)
    msg = launcher._half_dead_daemon_guidance(
        "C:/state/broker", "127.0.0.1", 59997, 4321)
    assert "tasklist" in msg
    assert "netstat -ano" in msg
    assert "taskkill /PID 4321" in msg
    assert "ss -ltnp" not in msg and "kill 4321" not in msg


def test_half_dead_guidance_missing_pid_probes_endpoint(monkeypatch):
    """pid が無い (古い/壊れた sidecar) 場合は endpoint 探索の手掛かりに切り替える。"""
    _pin_os(monkeypatch, "linux")
    msg = launcher._half_dead_daemon_guidance(
        "/tmp/isolated/broker", "127.0.0.1", 59997, None)
    assert "no usable pid" in msg
    assert "ss -ltnp | grep 59997" in msg
    assert "127.0.0.1:59997" in msg
    # pid 依存のコマンドは出さない。
    assert "ps -p" not in msg and "kill " not in msg


def test_half_dead_guidance_posix_cleanup_path_is_shell_safe(monkeypatch):
    """POSIX の掃除コマンドは、スペース / 単一引用符を含む state-dir でも貼り付け
    安全 (shlex.quote 相当) であること。素朴な 'path' 囲みでは壊れる edge を守る。"""
    _pin_os(monkeypatch, "linux")
    monkeypatch.setattr(launcher.sidecar, "pid_alive", lambda _pid: False)
    state_dir = "/tmp/weird ' dir/broker"
    msg = launcher._half_dead_daemon_guidance(state_dir, "127.0.0.1", 59997, 999999)
    sidecar_path = os.path.join(state_dir, sidecar.SIDECAR_NAME)
    quoted = shlex.quote(sidecar_path)
    assert f"rm {quoted}" in msg
    # 貼り付け安全性: 提示トークンをシェル解釈すると元の path 1 個に戻る。
    assert shlex.split(quoted) == [sidecar_path]


@pytest.mark.parametrize("pid", [4321, None])
def test_half_dead_guidance_is_ascii_only(monkeypatch, pid):
    """案内文は実端末 (cp932 コンソール) でも壊れないよう ASCII のみで構成する。"""
    monkeypatch.setattr(launcher.sidecar, "pid_alive", lambda _pid: True)
    msg = launcher._half_dead_daemon_guidance(
        "/tmp/isolated/broker", "127.0.0.1", 59997, pid)
    assert msg.isascii()
    msg.encode("cp932")  # cp932 で encode 不能な文字がないことを保証する


# =================================================== up: unhealthy live daemon
def test_org_up_errors_when_mcp_surface_unhealthy(live_daemon, monkeypatch):
    """admin は応答するが MCP 面が健全でない生存 daemon は unhealthy エラー。"""
    b, state_dir = live_daemon
    monkeypatch.setattr(launcher, "_mcp_surface_ok", lambda *a, **k: False)
    launched = []
    rc = launcher.org_up(
        _up_args(state_dir),
        spawn_daemon=lambda *a, **k: (_ for _ in ()).throw(AssertionError()),
        launch=lambda argv, state_dir=None, observer_secret=None, root_cwd=None: launched.append(argv) or 0,
    )
    assert rc == 2
    assert launched == []


def test_reuse_probe_session_is_deregistered(live_daemon):
    """健全性 probe の使い捨て無名 token は MCP DELETE で de-register され、
    list_peers (registered bind) に残らない (probe orphan の蓄積を抑える)。"""
    b, state_dir = live_daemon
    launcher.org_up(
        _up_args(state_dir),
        spawn_daemon=lambda *a, **k: (_ for _ in ()).throw(AssertionError()),
        launch=lambda argv, state_dir=None, observer_secret=None, root_cwd=None: 0,
    )
    # admin-* の probe bind は close() で registered=False に落ちている。
    registered_admin = [
        bnd for bnd in b._binds.values()
        if bnd.agent_id.startswith("admin-") and bnd.registered and not bnd.revoked
    ]
    assert registered_admin == []


# ============================================ down: pane close 範囲 (backend 別判定)
def test_backend_is_isolated_mapping():
    # adapter ClassVar (tmux=True / wezterm=False / herdr=True) を非インスタンス化
    # で引く。org down はこの写像で pane close 範囲を決めるため、herdr の isolated
    # 経路 (_BACKEND_ADAPTER_CLASS の "herdr" エントリ) を launcher 側で明示検証する
    # (adapter の ClassVar 直読みだけでは配線 drift を捕まえられない)。
    assert launcher._backend_is_isolated("tmux") is True
    assert launcher._backend_is_isolated("wezterm") is False
    assert launcher._backend_is_isolated("herdr") is True
    assert launcher._backend_is_isolated(None) is False     # 未知/None は保守的に False


def _broker_with_three_panes(tmp_path, *, isolated):
    """claude / codex / generic の 3 ペインを持つ started Broker を作る。

    論理 root を 1 つ登録しておき、isolated 分岐で全件 close しても last-pane ガードに
    引っかからないようにする。返り値は (broker, control_token, {kind: pane_id})。
    """
    state_dir = str(tmp_path / "broker")
    b = Broker(state_dir=state_dir, adapter=FakeAdapter(isolated_session=isolated),
               port=0, admin_token="A")
    b.start()
    sec = b.issue_token("sec", "sec", "secretary", auth_role="secretary")
    b.register_logical_pane(sec)                    # 窓口 (論理ペイン) を +1 計上
    c = launcher._McpClient(b.host, b.port, sec)
    c.initialize()
    claude_id = c.call_tool("spawn_claude_pane", {"direction": "vertical", "name": "w1"})["id"]
    codex_id = c.call_tool("spawn_codex_pane", {"direction": "vertical", "name": "w2"})["id"]
    gen_id = c.call_tool("spawn_pane", {"direction": "vertical", "name": "gen",
                                        "command": "x"})["id"]
    c.close()
    ctrl = b.issue_token("ctrl", "ctrl", "secretary", auth_role="secretary")
    return b, ctrl, {"claude": claude_id, "codex": codex_id, "generic": gen_id}


def test_close_managed_panes_isolated_closes_all_kinds(tmp_path):
    # isolated (tmux): list_panes は全て broker 所有 → kind 不問で generic も close。
    b, ctrl, ids = _broker_with_three_panes(tmp_path, isolated=True)
    try:
        closed = launcher._close_managed_panes(b.host, b.port, ctrl, isolated=True)
        assert set(closed) == set(ids.values())     # claude / codex / generic 全部
    finally:
        b.stop()


def test_close_managed_panes_global_mux_limits_to_agent_kinds(tmp_path):
    # global-mux (wezterm): 無関係 pane 巻き添え回避のため agent 子 (claude/codex) のみ。
    b, ctrl, ids = _broker_with_three_panes(tmp_path, isolated=False)
    try:
        closed = launcher._close_managed_panes(b.host, b.port, ctrl, isolated=False)
        assert set(closed) == {ids["claude"], ids["codex"]}
        assert ids["generic"] not in closed         # generic (kind=None) は残す
    finally:
        b.stop()


# ============================== up: secretary TUI に broker transport を注入 (Issue #70)
def test_launch_claude_posix_exec_injects_broker_transport(monkeypatch):
    """POSIX exec 経路: execvpe に渡る子環境に ORG_TRANSPORT=broker が含まれる。

    既定 transport は broker に昇格済み (Epic #586 Phase 2) だが、この明示注入は
    ORG_TRANSPORT=renga の opt-in fallback が子へ漏れて renga 経路 (RENGA_PANE_ID
    不在で停止) に落ちるのを防ぐ二重の安全弁として維持する (Issue #70)。
    """
    monkeypatch.setattr(launcher.os, "name", "posix")
    captured = {}

    def fake_execvpe(file, argv, env):
        captured["file"] = file
        captured["argv"] = argv
        captured["env"] = env

    monkeypatch.setattr(launcher.os, "execvpe", fake_execvpe)
    launcher._launch_claude(["claude", "--mcp-config", "{}"])
    assert captured["file"] == "claude"
    assert captured["env"].get("ORG_TRANSPORT") == "broker"


def test_launch_claude_windows_subprocess_injects_broker_transport(monkeypatch):
    """Windows subprocess 経路: subprocess.call に渡る env に ORG_TRANSPORT=broker。"""
    monkeypatch.setattr(launcher.os, "name", "nt")
    captured = {}

    def fake_call(argv, *, env):
        captured["argv"] = argv
        captured["env"] = env
        return 0

    monkeypatch.setattr(launcher.subprocess, "call", fake_call)
    rc = launcher._launch_claude(["claude", "--mcp-config", "{}"])
    assert rc == 0
    assert captured["env"].get("ORG_TRANSPORT") == "broker"


def test_launch_claude_fallback_command_prefixes_broker_transport(monkeypatch, capsys):
    """claude 不在時の 1 行コマンド表示 fallback にも env 前置を含める
    (手で起動しても同じ broker transport になる)。"""
    monkeypatch.setattr(launcher.os, "name", "nt")

    def fake_call(argv, *, env):
        raise FileNotFoundError("claude not found")

    monkeypatch.setattr(launcher.subprocess, "call", fake_call)
    rc = launcher._launch_claude(["claude", "--mcp-config", "{}"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ORG_TRANSPORT=broker" in out


def test_launch_claude_posix_exec_injects_broker_state_dir(monkeypatch):
    """POSIX exec 経路: state_dir 指定時 ORG_BROKER_STATE_DIR(絶対) を子環境へ注入 (#122)。"""
    monkeypatch.setattr(launcher.os, "name", "posix")
    captured = {}
    monkeypatch.setattr(
        launcher.os, "execvpe",
        lambda file, argv, env: captured.update(env=env),
    )
    launcher._launch_claude(["claude"], state_dir="/abs/state")
    assert captured["env"]["ORG_BROKER_STATE_DIR"] == "/abs/state"
    assert captured["env"]["ORG_TRANSPORT"] == "broker"


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX exec / login-shell wrapper (Windows uses subprocess + env PATH)"
)
def test_launch_claude_posix_activates_root_cwd_venv(monkeypatch, tmp_path):
    """Issue #130: root_cwd/.venv があれば secretary もそれを継承する (POSIX exec)。

    server._adapter_spawn (worker/dispatcher の本丸) に対する secretary 用の補助整合。
    VIRTUAL_ENV は env dict、PATH は argv を post-profile login-shell wrapper に包む。
    POSIX 専用経路 (execvpe) のため native os.name を使い Windows では skip する
    (os.name を強制すると pathlib PosixPath の罠を踏む恐れがあるため)。"""
    monkeypatch.setenv("SHELL", "/bin/bash")
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python").write_text("")
    captured = {}
    monkeypatch.setattr(
        launcher.os, "execvpe",
        lambda file, argv, env: captured.update(file=file, argv=argv, env=env),
    )
    launcher._launch_claude(["claude", "--mcp-config", "{}"], root_cwd=str(tmp_path))
    venv = str(tmp_path / ".venv")
    assert captured["env"]["VIRTUAL_ENV"] == venv
    # argv is wrapped in the login-shell PATH prepend; file is the shell now.
    # Path expectation derived from launcher.venv_bin_dir so it is separator-agnostic.
    assert captured["file"] == "/bin/bash"
    assert captured["argv"][1] == "-lc"
    assert "export PATH=" in captured["argv"][2]
    assert launcher.venv_bin_dir(venv) in captured["argv"][2]
    assert captured["argv"][-3:] == ["claude", "--mcp-config", "{}"]


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX exec path (native os.name; no forcing)"
)
def test_launch_claude_posix_noop_without_root_cwd_venv(monkeypatch, tmp_path):
    """Issue #130: root_cwd に .venv が無ければ完全 no-op (argv/env 不変で従来挙動)。"""
    captured = {}
    monkeypatch.setattr(
        launcher.os, "execvpe",
        lambda file, argv, env: captured.update(file=file, argv=argv, env=env),
    )
    launcher._launch_claude(["claude", "--mcp-config", "{}"], root_cwd=str(tmp_path))
    assert captured["file"] == "claude"
    assert captured["argv"] == ["claude", "--mcp-config", "{}"]
    assert "VIRTUAL_ENV" not in captured["env"]


def test_launch_claude_windows_activates_root_cwd_venv(monkeypatch, tmp_path):
    """Issue #130: native Windows は subprocess.call(env=) が子環境を直接決めるため、
    PATH を env dict へ直接前置する (cmd profile による PATH 再構築が無く %PATH% 不要)。

    os.name="nt" 強制は安全: この経路は state_dir を渡さず (sidecar.absolutize 不発火)
    pathlib.Path を構築しないため、強制中に WindowsPath が実体化されない。これで
    Linux/macOS CI でも native-Windows branch を被覆できる。"""
    monkeypatch.setattr(launcher.os, "name", "nt")
    (tmp_path / ".venv" / "Scripts").mkdir(parents=True)
    (tmp_path / ".venv" / "Scripts" / "python.exe").write_text("")
    captured = {}

    def fake_call(argv, *, env):
        captured.update(argv=argv, env=env)
        return 0

    monkeypatch.setattr(launcher.subprocess, "call", fake_call)
    launcher._launch_claude(["claude"], root_cwd=str(tmp_path))
    venv = str(tmp_path / ".venv")
    scripts = launcher.venv_bin_dir(venv)
    assert captured["env"]["VIRTUAL_ENV"] == venv
    # Windows は argv を包まず、PATH を env dict に os.pathsep で前置する (%PATH% なし)。
    # claude が venv Scripts に無い通常形では argv[0] は不変 (ambient PATH 解決のまま)。
    assert captured["argv"] == ["claude"]
    assert captured["env"]["PATH"].startswith(scripts + os.pathsep)


def test_launch_claude_windows_resolves_exe_in_venv_scripts(monkeypatch, tmp_path):
    """Codex P2: Windows subprocess.call は env= の PATH で argv[0] を解決しないため、
    venv Scripts にしか無い実行体を明示解決する。venv 内でヒットしたら argv[0] を差し替える。"""
    monkeypatch.setattr(launcher.os, "name", "nt")
    (tmp_path / ".venv" / "Scripts").mkdir(parents=True)
    (tmp_path / ".venv" / "Scripts" / "python.exe").write_text("")
    fake_exe = str(tmp_path / ".venv" / "Scripts" / "claude.cmd")
    # shutil.which(path=<venv Scripts>) が venv 内の実行体を返す状況を模す。
    monkeypatch.setattr(
        launcher.shutil, "which",
        lambda cmd, path=None: fake_exe if cmd == "claude" else None,
    )
    captured = {}
    monkeypatch.setattr(
        launcher.subprocess, "call",
        lambda argv, *, env: captured.update(argv=argv, env=env) or 0,
    )
    launcher._launch_claude(["claude", "--x"], root_cwd=str(tmp_path))
    # argv[0] は venv 内の実行体へ解決され、残りの引数は保たれる。
    assert captured["argv"] == [fake_exe, "--x"]
    assert captured["env"]["VIRTUAL_ENV"] == str(tmp_path / ".venv")


def test_launch_claude_omits_state_dir_when_not_given(monkeypatch):
    """state_dir 未指定 (既定 None) なら ORG_BROKER_STATE_DIR は注入しない (後方互換)。"""
    monkeypatch.setattr(launcher.os, "name", "posix")
    captured = {}
    monkeypatch.setattr(
        launcher.os, "execvpe",
        lambda file, argv, env: captured.update(env=env),
    )
    launcher._launch_claude(["claude"])
    assert "ORG_BROKER_STATE_DIR" not in captured["env"]


def test_launch_claude_fallback_prefixes_state_dir(monkeypatch, capsys):
    """claude 不在の 1 行コマンド fallback にも ORG_BROKER_STATE_DIR 前置を含める (#122)。"""
    monkeypatch.setattr(launcher.os, "name", "nt")
    monkeypatch.setattr(
        launcher.subprocess, "call",
        lambda argv, *, env: (_ for _ in ()).throw(FileNotFoundError()),
    )
    launcher._launch_claude(["claude"], state_dir="/abs/state")
    out = capsys.readouterr().out
    assert "ORG_BROKER_STATE_DIR=/abs/state" in out
    assert "ORG_TRANSPORT=broker" in out


# ============ up: observed-session binding secret injection (Issue #129 問題 A)
def test_launch_claude_posix_exec_injects_observer_secret(monkeypatch):
    """POSIX exec 経路: observer_secret 指定時、子環境へ ORG_BROKER_CHANNEL_OBSERVER を
    注入する (mcp-config には載らない非 replay 信号 = observed session だけが提示できる)。"""
    monkeypatch.setattr(launcher.os, "name", "posix")
    captured = {}
    monkeypatch.setattr(
        launcher.os, "execvpe",
        lambda file, argv, env: captured.update(env=env),
    )
    launcher._launch_claude(["claude"], observer_secret="obs-secret")
    assert captured["env"]["ORG_BROKER_CHANNEL_OBSERVER"] == "obs-secret"


def test_launch_claude_omits_observer_when_not_given(monkeypatch):
    """observer_secret 未指定 (None) なら ORG_BROKER_CHANNEL_OBSERVER は注入しない。"""
    monkeypatch.setattr(launcher.os, "name", "posix")
    monkeypatch.delenv("ORG_BROKER_CHANNEL_OBSERVER", raising=False)
    captured = {}
    monkeypatch.setattr(
        launcher.os, "execvpe",
        lambda file, argv, env: captured.update(env=env),
    )
    launcher._launch_claude(["claude"])
    assert "ORG_BROKER_CHANNEL_OBSERVER" not in captured["env"]


def test_launch_claude_fallback_includes_observer_secret(monkeypatch, capsys):
    """claude 不在 fallback の 1 行コマンドに ORG_BROKER_CHANNEL_OBSERVER 前置を **含める**
    (Codex P2: daemon は lease を assert 済なので、秘密無しで手起動すると sidecar が
    unobserved で止まり push が届かない。手起動でも observed になるよう secret を渡す)。"""
    monkeypatch.setattr(launcher.os, "name", "nt")
    monkeypatch.setattr(
        launcher.subprocess, "call",
        lambda argv, *, env: (_ for _ in ()).throw(FileNotFoundError()),
    )
    rc = launcher._launch_claude(
        ["claude", "--mcp-config", "{}"], observer_secret="obs-handoff-secret")
    assert rc == 0
    out = capsys.readouterr().out
    assert "ORG_BROKER_CHANNEL_OBSERVER=obs-handoff-secret" in out


# =========================== org adopt: 配達所有権の明示 handover (#166)
# adopt は「daemon 側で旧 session を fence してから、新しい秘密を持つ claude を起こす」
# 操作。fence は RPC 応答の時点で既に済んでいるので、CLI 側の分岐が 1 つ狂うと
# 「所有権は動いたのに誰も配達しない」窓が残る。ここでは wrapper の分岐と、秘密が
# **env 経路 (launch kwarg) だけ**を通ることを固定する。

_ADOPT_MCP_CONFIG = {"mcpServers": {
    "org-broker": {"type": "http", "url": "http://127.0.0.1:1/mcp",
                   "headers": {"Authorization": "Bearer OWNER-TOKEN"}},
    "org-broker-channel": {"command": "py",
                           "args": ["-m", "claude_org_runtime.broker.channel_sidecar"],
                           "env": {"ORG_BROKER_CHANNEL_OWNER": "w1"}},
}}


def _write_discoverable_sidecar(state_dir, *, host="127.0.0.1", port=59990):
    """``org adopt`` が「走行中 daemon」を発見できる最小の sidecar を書く。

    RPC はスタブするので host/port は到達不能で構わない (daemon 発見の分岐だけを
    成立させるためのもの)。"""
    sidecar.write_sidecar(
        state_dir, pid=os.getpid(), host=host, port=port,
        backend=default_backend(), started_at=time.time(), journal_offset=0,
    )
    sidecar.write_admin_token(state_dir, "ADMIN-SECRET")


def _adopt_ok(*, adoption_id="ad-01", secret="rotated-observer-secret",
              owner="w1", in_flight="requeue", rows=2):
    """``adopt_delivery`` の成功応答 (server が mcp_config を畳んだ後の形)。"""
    return {
        "ok": True, "owner": owner, "adoption_id": adoption_id,
        "observer_secret": secret, "generation": 8,
        "in_flight_policy": in_flight, "in_flight_rows": rows,
        "arming_seconds": 300.0, "armed_until": 1.0,
        "mcp_config": _ADOPT_MCP_CONFIG,
    }


def _pending(adoption_id="ad-01", *, seconds=42.0, policy="requeue", rows=2):
    """``adopt_status`` の pending ブロック (秘密を含まない契約)。"""
    return {"adoption_id": adoption_id, "armed_seconds_remaining": seconds,
            "in_flight_policy": policy, "in_flight_rows": rows,
            "fenced_generation": 8}


def _status_ok(pending=None, *, owner="w1"):
    return {"ok": True, "owner": owner, "generation": 8, "instance_id": None,
            "observer_state": "armed", "pending": pending}


def _stub_admin_rpc(monkeypatch, *, adopt=None, status=None):
    """``launcher._admin_rpc`` をスタブし ``(method, params)`` の呼び出し列を返す。

    org_adopt は **モジュールグローバル** の ``_admin_rpc`` を呼ぶ契約なので、ここを
    差し替えるだけで daemon 無しに全分岐を駆動できる (この契約が崩れて import 時
    束縛になると、以降のテストは本物の RPC を叩いて別の失敗をする)。値に例外
    インスタンスを渡すとその method で送出する (到達不能の再現)。
    """
    calls: list[tuple[str, dict]] = []

    def fake(host, port, admin_token, method, params=None, **kw):
        calls.append((method, dict(params or {})))
        res = adopt if method == "adopt_delivery" else status
        if isinstance(res, BaseException):
            raise res
        return res

    monkeypatch.setattr(launcher, "_admin_rpc", fake)
    return calls


def _recording_launch():
    """claude 起動 seam のスパイ。``(呼び出し記録, launch)`` を返す。"""
    calls: list[dict] = []

    def launch(argv, state_dir=None, observer_secret=None, root_cwd=None):
        calls.append({"argv": argv, "state_dir": state_dir,
                      "observer_secret": observer_secret, "root_cwd": root_cwd})
        return 0

    return calls, launch


def test_build_up_argv_forwards_session_selector():
    """build_up_argv が resume / continue_session を builder へそのまま流す。

    adopt が argv を自前で組み直すと default-deny guard の適用が二重実装になり、
    片方だけ緩む。selector は既存 builder の構造化フィールドに流すことでのみ描画される
    (相互排他の検査も builder 側の 1 箇所に残る)。
    """
    from claude_org_runtime.broker.surface import ToolArgError

    cfg = {"mcpServers": {}}
    argv = launcher.build_up_argv(cfg, resume="sess-abc")
    assert argv[argv.index("--resume") + 1] == "sess-abc"
    assert "--continue" not in argv

    argv = launcher.build_up_argv(cfg, continue_session=True)
    assert "--continue" in argv
    assert "--resume" not in argv

    # 相互排他は builder が持つ (adopt 側で握り潰さない)。
    with pytest.raises(ToolArgError):
        launcher.build_up_argv(cfg, resume="sess-abc", continue_session=True)


def test_org_adopt_hands_rotated_secret_to_the_launched_process(tmp_path, monkeypatch):
    """rotate された observer 秘密が ``launch`` の observer_secret kwarg で届く。

    これが切れると adopt は「旧 session を fence しただけ」になり、新 session は秘密を
    持てず ``unobserved`` で沈黙する = owner への push が恒久停止する (adopt が daemon 側で
    先に fence する以上、起動側の取りこぼしは無音の配送停止になる)。同時に、秘密が
    **argv に載っていない**ことも固定する: argv (= mcp-config) は fork/resume が replay
    する面なので、そこへ載った瞬間 lease の存在根拠が消える。
    """
    state_dir = str(tmp_path / "broker")
    _write_discoverable_sidecar(state_dir)
    calls = _stub_admin_rpc(monkeypatch, adopt=_adopt_ok(),
                            status=_status_ok(_pending("ad-01")))
    launched, launch = _recording_launch()

    rc = launcher.org_adopt(_adopt_args(state_dir, owner="w1"), launch=launch)

    assert rc == 0
    assert len(launched) == 1
    assert launched[0]["observer_secret"] == "rotated-observer-secret"
    assert "rotated-observer-secret" not in json.dumps(launched[0]["argv"])
    # 子環境の state_dir / cwd アンカーも org up と同じ契約で渡る。
    assert launched[0]["state_dir"] == sidecar.absolutize(state_dir)
    assert launched[0]["root_cwd"] == os.getcwd()
    # rotate は 1 回だけ。operator の選択 (policy / force) はそのまま daemon へ渡る。
    assert [m for m, _ in calls] == ["adopt_delivery", "adopt_status"]
    assert calls[0][1] == {"owner": "w1", "in_flight": "requeue", "force": False}


def test_org_adopt_threads_resume_selector_into_argv(tmp_path, monkeypatch):
    """``--resume <id>`` が起動 argv に構造化 selector として届く。

    adopt は新プロセスを起こす以外に秘密を渡す手段が無いので、引き継ぎたい会話は
    selector でしか繋がらない。ここが落ちると operator は「adopt したのに真っさらな
    session が開いた」状態になり、直前の文脈を失う。
    """
    state_dir = str(tmp_path / "broker")
    _write_discoverable_sidecar(state_dir)
    _stub_admin_rpc(monkeypatch, adopt=_adopt_ok(), status=_status_ok(_pending()))
    launched, launch = _recording_launch()

    rc = launcher.org_adopt(
        _adopt_args(state_dir, resume="sess-123"), launch=launch)

    assert rc == 0
    argv = launched[0]["argv"]
    assert argv[argv.index("--resume") + 1] == "sess-123"
    assert "--continue" not in argv


def test_org_adopt_threads_continue_selector_into_argv(tmp_path, monkeypatch):
    """``--continue`` も同じく構造化 selector として argv に届く (resume は出さない)。

    resume と continue が同時に出ると claude 側の解決順に暗黙依存する argv になるため、
    片方だけが描画されることを固定する。
    """
    state_dir = str(tmp_path / "broker")
    _write_discoverable_sidecar(state_dir)
    _stub_admin_rpc(monkeypatch, adopt=_adopt_ok(), status=_status_ok(_pending()))
    launched, launch = _recording_launch()

    rc = launcher.org_adopt(
        _adopt_args(state_dir, continue_session=True), launch=launch)

    assert rc == 0
    argv = launched[0]["argv"]
    assert "--continue" in argv
    assert "--resume" not in argv


def test_org_adopt_without_daemon_sidecar_refuses(tmp_path, monkeypatch, capsys):
    """sidecar 不在 (daemon が居ない) は rc 2 で、claude を起動しない。

    daemon が居なければ引き継ぐ配達所有権も存在しない。ここで起動してしまうと、
    誰も配達できない session を「adopt 成功」として operator に渡すことになる。
    """
    state_dir = str(tmp_path / "broker")     # sidecar を書かない
    _stub_admin_rpc(monkeypatch, adopt=AssertionError("must not RPC without a daemon"))
    launched, launch = _recording_launch()

    rc = launcher.org_adopt(_adopt_args(state_dir), launch=launch)

    assert rc == 2
    assert launched == []
    err = capsys.readouterr().err
    assert "no broker daemon sidecar" in err
    assert "org up" in err                   # 次の一手を示す
    # 可変部 (state-dir) はランナー依存で非 ASCII を含みうるので伏せてから検査する。
    scrubbed = err.replace(sidecar.absolutize(state_dir), "<state-dir>")
    scrubbed.encode("ascii")
    scrubbed.encode("cp932")


def test_org_adopt_refuses_when_admin_token_missing(tmp_path, monkeypatch, capsys):
    """daemon.json はあるが admin.token が現れない半公開状態でも起動しない (rc 2)。

    admin.token が無ければ fence を要求する術がない。それでも claude を起こすと、
    旧 session が生きたまま新 session も走り、二重配達 / 二重応答の窓を作る。
    """
    state_dir = str(tmp_path / "broker")
    sidecar.write_sidecar(
        state_dir, pid=4321, host="127.0.0.1", port=59991,
        backend=default_backend(), started_at=time.time(), journal_offset=0,
    )                                        # admin.token は書かない
    monkeypatch.setattr(launcher, "ADMIN_TOKEN_GRACE", 0.2)
    _stub_admin_rpc(monkeypatch, adopt=AssertionError("must not RPC without a token"))
    launched, launch = _recording_launch()

    rc = launcher.org_adopt(_adopt_args(state_dir), launch=launch)

    assert rc == 2
    assert launched == []
    err = capsys.readouterr().err
    assert "admin.token" in err
    assert "Not adopting" in err
    err.encode("cp932")


def test_org_adopt_never_cold_starts_a_daemon(tmp_path, monkeypatch, capsys):
    """``org adopt`` は daemon を **起動しない** (org up と決定的に違う点)。

    org up の解決関数を流用すると、adopt が daemon を起こしたうえ名前付き secretary を
    mint してしまう — adopt の前提 (「その owner は既に居る」) と真逆の副作用で、
    「引き継いだつもりで新しい空の org を作った」事故になる。daemon 起動の seam を
    どちらも爆発させ、触れずに rc 2 で終わることを固定する。
    """
    def boom(*a, **k):
        raise AssertionError("org adopt must never start a daemon")

    monkeypatch.setattr(launcher, "_spawn_daemon", boom)
    monkeypatch.setattr(launcher.subprocess, "Popen", boom)
    state_dir = str(tmp_path / "broker")     # sidecar 不在 = up なら cold start する状況
    launched, launch = _recording_launch()

    rc = launcher.org_adopt(_adopt_args(state_dir), launch=launch)

    assert rc == 2
    assert launched == []
    assert not os.path.exists(os.path.join(state_dir, sidecar.SIDECAR_NAME))
    assert "Nothing to adopt" in capsys.readouterr().err


def test_org_adopt_unreachable_daemon_says_nothing_was_rotated(
        tmp_path, monkeypatch, capsys):
    """daemon 不到達は「何も rotate していない」と言い切る (rc 2、起動なし)。

    adopt は現職を fence する操作なので、失敗時に所有権が動いたのか動いていないのかが
    曖昧だと operator は次の一手を選べない。rotate は RPC が通って初めて起きるため、
    URLError の時点では **確実に** 未変更 — その確定情報を文言として固定する。
    """
    import urllib.error

    state_dir = str(tmp_path / "broker")
    _write_discoverable_sidecar(state_dir)
    _stub_admin_rpc(monkeypatch, adopt=urllib.error.URLError("refused"))
    launched, launch = _recording_launch()

    rc = launcher.org_adopt(_adopt_args(state_dir), launch=launch)

    assert rc == 2
    assert launched == []
    err = capsys.readouterr().err
    assert "nothing was rotated" in err
    err.encode("cp932")


@pytest.mark.parametrize("bad", [
    {"resume": "sid", "continue_session": True},        # 相互排他の session selector
    {"resume": "--print"},                              # 値位置に headless flag
    {"resume": "   "},                                  # 空の session id
    {"claude_arg": ["--resume", "other"]},              # 予約 flag を args[] から
    {"claude_arg": ["-p"]},                             # headless flag
])
def test_org_adopt_validates_launch_args_before_rotating(
    tmp_path, monkeypatch, capsys, bad,
):
    """**回帰**: ローカル引数の不正は **fence する前に** 落とす (rc 2、RPC を投げない)。

    adopt_delivery は現職をその場で fence するので、その後で argv 組み立てが例外を
    投げると owner は claimer 不在のまま arming deadline まで放置される — しかも原因は
    operator の typo という、最も直しやすいはずのもの。さらに素通しだと ToolArgError が
    traceback のまま端末に出る。spawn_claude が token 発行前に argv を pre-validate する
    のと同じ理由・同じ順序 (副作用の前に検証)。
    """
    state_dir = str(tmp_path / "broker")
    _write_discoverable_sidecar(state_dir)
    calls = _stub_admin_rpc(monkeypatch, adopt=_adopt_ok())
    launched, launch = _recording_launch()

    rc = launcher.org_adopt(_adopt_args(state_dir, **bad), launch=launch)

    assert rc == 2
    assert calls == [], "the owner was fenced before the arguments were validated"
    assert launched == []
    err = capsys.readouterr().err
    assert "invalid launch arguments" in err and "nothing was rotated" in err


def test_org_adopt_surfaces_rpc_error_verbatim(tmp_path, monkeypatch, capsys):
    """``ok: False`` の error 文字列をそのまま stderr に出す (rc 2、起動なし)。

    daemon 側のエラーは何を直せばよいかを名指ししている ([no_delivery_credential] なら
    channel 付きで mint し直す等)。CLI が自前の要約に置き換えると、その手掛かりが
    operator に届かない。
    """
    state_dir = str(tmp_path / "broker")
    _write_discoverable_sidecar(state_dir)
    error = ("[no_delivery_credential] owner 'w1' holds no delivery credential; "
             "nothing to adopt (mint or spawn it with channel first)")
    calls = _stub_admin_rpc(monkeypatch, adopt={"ok": False, "error": error})
    launched, launch = _recording_launch()

    rc = launcher.org_adopt(_adopt_args(state_dir), launch=launch)

    assert rc == 2
    assert launched == []
    assert error in capsys.readouterr().err
    # 失敗した adopt の後追い status は投げない (rotate していないので見るものが無い)。
    assert [m for m, _ in calls] == ["adopt_delivery"]


def test_org_adopt_status_reports_idle_owner_without_rotating(
        tmp_path, monkeypatch, capsys):
    """``--status`` は adopt_status だけを叩き、rotate も起動もしない (rc 0)。

    状態を見るつもりのコマンドが所有権を動かしたら、確認行為そのものが現職を fence して
    しまう。読み取り専用であることを呼び出し列で固定する (adopt_delivery が呼ばれたら
    スタブが爆発する)。
    """
    state_dir = str(tmp_path / "broker")
    _write_discoverable_sidecar(state_dir)
    calls = _stub_admin_rpc(
        monkeypatch, adopt=AssertionError("--status must not rotate"),
        status=_status_ok(None),
    )
    launched, launch = _recording_launch()

    rc = launcher.org_adopt(_adopt_args(state_dir, status=True), launch=launch)

    assert rc == 0
    assert launched == []
    assert [m for m, _ in calls] == ["adopt_status"]
    out = capsys.readouterr().out
    assert "owner=w1" in out and "generation=8" in out
    assert "observer=armed" in out
    assert "no adoption in flight" in out


def test_org_adopt_status_reports_pending_adoption(tmp_path, monkeypatch, capsys):
    """``--status`` は進行中 adopt の残り時間 / policy / 件数を出す (rc 0、起動なし)。

    adopt は fence してから起動が landing するまでの窓を持つ。その窓で「誰も配達して
    いない」ことを説明できる唯一の operator 向け経路がこの表示なので、pending 分岐が
    無言になると、無音の配送停止と区別できなくなる。
    """
    state_dir = str(tmp_path / "broker")
    _write_discoverable_sidecar(state_dir)
    _stub_admin_rpc(
        monkeypatch, adopt=AssertionError("--status must not rotate"),
        status=_status_ok(_pending("ad-77", seconds=12.5, policy="drop", rows=3)),
    )
    launched, launch = _recording_launch()

    rc = launcher.org_adopt(_adopt_args(state_dir, status=True), launch=launch)

    assert rc == 0
    assert launched == []
    out = capsys.readouterr().out
    assert "ad-77" in out
    assert "12.5s more" in out
    assert "drop" in out and "3 rows" in out


@pytest.mark.parametrize("pending", [_pending("someone-elses-adopt"), None])
def test_org_adopt_refuses_to_launch_when_preflight_shows_another_adoption(
        tmp_path, monkeypatch, capsys, pending):
    """exec 直前の preflight で自分の adoption が現職でないなら起動しない (rc 2)。

    並行 ``--force`` に負けた側の秘密は既に無効で、それを持って起動した session は
    ``unobserved`` のまま恒久沈黙する。負けを黙って起動に変えると、operator は「adopt は
    成功した」と信じたまま届かない pane を眺めることになる。pending が別 ID の場合と、
    そもそも消えている場合の双方を同じ扱いにする。
    """
    state_dir = str(tmp_path / "broker")
    _write_discoverable_sidecar(state_dir)
    _stub_admin_rpc(monkeypatch, adopt=_adopt_ok(adoption_id="ad-01"),
                    status=_status_ok(pending))
    launched, launch = _recording_launch()

    rc = launcher.org_adopt(_adopt_args(state_dir), launch=launch)

    assert rc == 2
    assert launched == []
    err = capsys.readouterr().err
    assert "superseded" in err
    err.encode("cp932")


@pytest.mark.parametrize(
    "status", [None, {"ok": False, "error": "[adopt_status_failed] boom"}])
def test_org_adopt_preflight_failure_is_advisory_and_still_launches(
        tmp_path, monkeypatch, status):
    """preflight が失敗 / 不到達でも起動は続行する (preflight は助言に過ぎない)。

    fence は adopt_delivery が返った時点で **既に完了している**。確認できないことを
    理由に起動を止めると、fence 済みで誰も配達しない owner を残したまま CLI が降りる
    ことになり、preflight が守るはずのもの (無音の配送停止) を自分で作ってしまう。
    """
    import urllib.error

    state_dir = str(tmp_path / "broker")
    _write_discoverable_sidecar(state_dir)
    _stub_admin_rpc(
        monkeypatch, adopt=_adopt_ok(),
        status=urllib.error.URLError("refused") if status is None else status,
    )
    launched, launch = _recording_launch()

    rc = launcher.org_adopt(_adopt_args(state_dir), launch=launch)

    assert rc == 0
    assert len(launched) == 1
    assert launched[0]["observer_secret"] == "rotated-observer-secret"


def test_org_adopt_output_is_ascii_and_cp932_safe(tmp_path, monkeypatch, capsys):
    """``org adopt`` が書く行はすべて ASCII で、cp932 コンソールでも壊れない。

    pytest は stdout/stderr を UTF-8 で捕まえるため、em-dash が 1 文字混ざっても実端末
    でしか露見せず、そこでは UnicodeEncodeError でコマンドごと落ちる。adopt は fence 済みの
    状態を説明する経路なので、落ちれば「所有権は動いたが結果が読めない」最悪の診断状況に
    なる。主要な全分岐の出力をまとめて検査する。
    """
    import urllib.error

    state_dir = str(tmp_path / "broker")
    collected: list[str] = []

    def drain():
        cap = capsys.readouterr()
        collected.append(cap.out + cap.err)

    launched, launch = _recording_launch()
    # 1) sidecar 不在 (cold)。
    launcher.org_adopt(_adopt_args(state_dir), launch=launch)
    drain()
    # 2) daemon.json のみ = admin.token 欠落。
    sidecar.write_sidecar(
        state_dir, pid=4321, host="127.0.0.1", port=59992,
        backend=default_backend(), started_at=time.time(), journal_offset=0,
    )
    monkeypatch.setattr(launcher, "ADMIN_TOKEN_GRACE", 0.05)
    launcher.org_adopt(_adopt_args(state_dir), launch=launch)
    drain()
    # 3) daemon 不到達。
    sidecar.write_admin_token(state_dir, "ADMIN-SECRET")
    _stub_admin_rpc(monkeypatch, adopt=urllib.error.URLError("refused"))
    launcher.org_adopt(_adopt_args(state_dir), launch=launch)
    drain()
    # 4) daemon が拒否。
    _stub_admin_rpc(monkeypatch, adopt={"ok": False, "error": "[unknown_owner] no bind"})
    launcher.org_adopt(_adopt_args(state_dir), launch=launch)
    drain()
    # 5) --status (pending 有り)。
    _stub_admin_rpc(monkeypatch, status=_status_ok(_pending("ad-99", seconds=7.5)))
    launcher.org_adopt(_adopt_args(state_dir, status=True), launch=launch)
    drain()
    # 6) 成功 (rotate → 起動)。
    _stub_admin_rpc(monkeypatch, adopt=_adopt_ok(), status=_status_ok(_pending()))
    launcher.org_adopt(_adopt_args(state_dir), launch=launch)
    drain()

    assert len(collected) == 6
    for text in collected:
        assert text                                   # どの分岐も無言で終わらない
        # 可変部 (state-dir) はランナー依存で非 ASCII を含みうるので伏せて検査する。
        scrubbed = text.replace(sidecar.absolutize(state_dir), "<state-dir>")
        scrubbed.encode("ascii")
        scrubbed.encode("cp932")


def test_org_adopt_end_to_end_records_adoption_on_live_daemon(live_daemon, capsys):
    """本物の daemon に対する adopt が、daemon 側に adoption を実際に記録する。

    スタブだけで固めると「CLI が期待どおりの JSON を組んだ」ことしか言えず、admin RPC の
    method 名 / params 名 / 応答キーが実装とずれても緑のままになる。owner を channel 付きで
    mint してから adopt し、daemon の delivery_dump に pending adoption が現れること、
    起動 argv が inline ``--mcp-config`` (その owner の channel sidecar 入り) を運ぶことを
    端から端で確認する。
    """
    b, state_dir = live_daemon
    mint = b.admin_mint_token({"role": "worker", "name": "w1", "channel": True})
    assert mint["ok"], mint
    launched, launch = _recording_launch()

    rc = launcher.org_adopt(_adopt_args(state_dir, owner="w1"), launch=launch)

    assert rc == 0
    # daemon 側に fence 済みの adoption が残っている (= RPC が本当に届いた)。
    adoptions = b.delivery_dump()["adoptions"]
    assert "w1" in adoptions
    adoption_id = adoptions["w1"]["adoption_id"]
    assert f"adoption {adoption_id}" in capsys.readouterr().out
    # argv は inline --mcp-config を運ぶ (0600 ファイル経路に依存しない)。
    argv = launched[0]["argv"]
    cfg = json.loads(argv[argv.index("--mcp-config") + 1])
    assert cfg["mcpServers"]["org-broker-channel"]["env"][
        "ORG_BROKER_CHANNEL_OWNER"] == "w1"
    # 秘密は env 経路 (launch kwarg) だけを通り、replay される argv には現れない。
    secret = launched[0]["observer_secret"]
    assert secret and isinstance(secret, str)
    assert secret not in json.dumps(argv)


# ================================ resident pre-flight wiring (Issue #142)
def _spy_preflight(monkeypatch):
    """launcher.residents.preflight_residents をスパイに差し替え、呼び出しを記録する。"""
    calls = []

    def spy(state_dir, root_cwd, *, reap=False, prefix="org up", **kw):
        calls.append({"state_dir": state_dir, "root_cwd": root_cwd,
                      "reap": reap, "prefix": prefix})

    monkeypatch.setattr(launcher.residents, "preflight_residents", spy)
    return calls


def test_org_up_sweeps_residents_only_on_cold_path(tmp_path, monkeypatch):
    """cold start では preflight が spawn 前に 1 回走り、reap フラグを転送する。"""
    calls = _spy_preflight(monkeypatch)
    state_dir = str(tmp_path / "broker")
    started: list[Broker] = []

    def fake_spawn(sd, backend, root_cwd):
        # cold sweep は spawn より前に走っている必要がある。
        assert len(calls) == 1, "resident sweep must run BEFORE spawn_daemon"
        b = Broker(state_dir=sd, adapter=None, port=0, admin_token="A")
        b.start()
        started.append(b)
        return b.host, b.port, "A"

    rc = launcher.org_up(
        _up_args(state_dir, reap=True), spawn_daemon=fake_spawn,
        launch=lambda argv, state_dir=None, observer_secret=None, root_cwd=None: 0,
    )
    try:
        assert rc == 0
        assert len(calls) == 1
        assert calls[0]["reap"] is True and calls[0]["prefix"] == "org up"
        assert calls[0]["state_dir"] == sidecar.absolutize(state_dir)
    finally:
        for b in started:
            b.stop()


def test_org_up_reuse_does_not_sweep_residents(live_daemon, monkeypatch):
    """健全な daemon を再利用する経路では preflight を **走らせない** (稼働中 org 自身の
    生きた resident を誤って告知/回収しないため — red-team Blocker 対策)。"""
    b, state_dir = live_daemon
    calls = _spy_preflight(monkeypatch)
    rc = launcher.org_up(
        _up_args(state_dir, reap=True),
        spawn_daemon=lambda *a, **k: (_ for _ in ()).throw(AssertionError()),
        launch=lambda argv, state_dir=None, observer_secret=None, root_cwd=None: 0,
    )
    assert rc == 0
    assert calls == []                                 # reuse では sweep しない


def test_org_up_conflict_does_not_sweep_residents(live_daemon, monkeypatch):
    """backend 競合など cold 以外の分岐でも sweep しない。"""
    b, state_dir = live_daemon
    calls = _spy_preflight(monkeypatch)
    other = "wezterm" if default_backend() != "wezterm" else "tmux"
    launcher.org_up(
        _up_args(state_dir, backend=other),
        spawn_daemon=lambda *a, **k: (_ for _ in ()).throw(AssertionError()),
        launch=lambda argv, state_dir=None, observer_secret=None, root_cwd=None: 0,
    )
    assert calls == []


def test_org_down_sweeps_residents_postflight_and_preserves_rc(tmp_path, monkeypatch):
    """down は teardown の **後** に sweep を回し、停止の rc を sweep で変えない。
    停止が確証できた (rc 0) ので --reap はそのまま転送される。"""
    calls = _spy_preflight(monkeypatch)
    state_dir = str(tmp_path / "broker")
    # teardown 本体をスタブし、clean stop (rc 0) を返させる (wrapper の配線だけを検証)。
    monkeypatch.setattr(launcher, "_org_down_daemon", lambda args, sd: 0)
    rc = launcher.org_down(_down_args(state_dir, reap=True))
    assert rc == 0
    assert len(calls) == 1
    assert calls[0]["reap"] is True and calls[0]["prefix"] == "org down"


def test_org_down_reap_downgraded_when_sidecar_still_present(tmp_path, monkeypatch, capsys):
    """teardown 後に sidecar が **残っている** (半死 / timeout = daemon 生存の可能性) とき、
    --reap は告知のみに降格する (現世代 daemon 自身の生きた resident を kill しない — codex P1)。"""
    calls = _spy_preflight(monkeypatch)
    state_dir = str(tmp_path / "broker")
    # daemon 生存疑いの状態を模す: teardown が sidecar を残したまま rc 1 を返す。
    sidecar.write_sidecar(state_dir, pid=os.getpid(), host="127.0.0.1", port=1,
                          backend=None, started_at=1.0, journal_offset=0)
    monkeypatch.setattr(launcher, "_org_down_daemon", lambda args, sd: 1)
    rc = launcher.org_down(_down_args(state_dir, reap=True))
    assert rc == 1                                     # 停止の rc は保持
    assert len(calls) == 1
    assert calls[0]["reap"] is False                   # reap は降格された
    assert "skipping --reap" in capsys.readouterr().err


def test_org_down_reap_allowed_after_stale_sidecar_cleanup(tmp_path, monkeypatch):
    """到達不能 = 死亡確定で teardown が stale sidecar を **消して** rc 1 を返す crash-recovery
    経路では、一発の org down --reap で orphan resident を回収できる (codex P2)。判定は rc では
    なく「sidecar が消えたか」で行うため rc 1 でも reap は許可される。"""
    calls = _spy_preflight(monkeypatch)
    state_dir = str(tmp_path / "broker")
    sidecar.write_sidecar(state_dir, pid=1, host="127.0.0.1", port=1, backend=None,
                          started_at=1.0, journal_offset=0, root_cwd="/daemon/root")

    def teardown_removes_sidecar(args, sd):
        sidecar.remove_sidecar(sd)                     # 死亡確定 → stale sidecar を掃除
        return 1

    monkeypatch.setattr(launcher, "_org_down_daemon", teardown_removes_sidecar)
    rc = launcher.org_down(_down_args(state_dir, reap=True))
    assert rc == 1
    assert len(calls) == 1
    assert calls[0]["reap"] is True                    # sidecar が消えた = 確証 down → reap 許可
    assert calls[0]["root_cwd"] == "/daemon/root"      # root_cwd は teardown 前に読んだ値


def test_org_down_sweeps_even_without_sidecar(tmp_path, monkeypatch):
    """sidecar が無く「nothing to stop」で終わる経路でも sweep は走る
    (resident は sidecar 有無に依らず存在しうる)。"""
    calls = _spy_preflight(monkeypatch)
    state_dir = str(tmp_path / "broker")            # sidecar 不在
    rc = launcher.org_down(_down_args(state_dir))
    assert rc == 0
    assert len(calls) == 1
    assert calls[0]["root_cwd"] == os.getcwd()      # 既定は getcwd


def test_org_down_root_cwd_resolution_prefers_flag_then_sidecar(tmp_path, monkeypatch):
    """root_cwd 解決順序: --root-cwd 明示 > daemon.json.root_cwd > getcwd。"""
    calls = _spy_preflight(monkeypatch)
    state_dir = str(tmp_path / "broker")
    sidecar.write_sidecar(
        state_dir, pid=os.getpid(), host="127.0.0.1", port=1, backend=None,
        started_at=1.0, journal_offset=0, root_cwd="/daemon/root",
    )
    monkeypatch.setattr(launcher, "_org_down_daemon", lambda args, sd: 0)

    # 明示 --root-cwd が最優先。
    launcher.org_down(_down_args(state_dir, root_cwd="/override"))
    assert calls[-1]["root_cwd"] == sidecar.absolutize("/override")

    # --root-cwd 無し → sidecar の root_cwd。
    launcher.org_down(_down_args(state_dir))
    assert calls[-1]["root_cwd"] == "/daemon/root"


def test_write_sidecar_persists_root_cwd(tmp_path):
    """daemon.json に root_cwd が (絶対化されて) 永続化され、省略時は None。"""
    sd = str(tmp_path / "broker")
    sidecar.write_sidecar(sd, pid=1, host="127.0.0.1", port=2, backend=None,
                          started_at=1.0, journal_offset=0, root_cwd="/repo/root")
    assert sidecar.read_sidecar(sd)["root_cwd"] == "/repo/root"
    sidecar.write_sidecar(sd, pid=1, host="127.0.0.1", port=2, backend=None,
                          started_at=1.0, journal_offset=0)
    assert sidecar.read_sidecar(sd)["root_cwd"] is None
