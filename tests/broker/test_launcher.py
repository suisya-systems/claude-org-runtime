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
             model=None, permission_mode=None, claude_arg=None):
    return argparse.Namespace(
        state_dir=str(state_dir), backend=backend, name=name,
        root_cwd=root_cwd, model=model, permission_mode=permission_mode,
        claude_arg=claude_arg,
    )


def _down_args(state_dir):
    return argparse.Namespace(state_dir=str(state_dir))


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
def _pin_platform(monkeypatch, os_name, platform):
    """OS 検出 (launcher.os.name / launcher.sys.platform) を固定し、ガイダンス
    コマンドの期待値をどの CI ランナー (Linux/macOS/Windows) でも決定的にする。
    実装が実ランナーの OS を見て ss/lsof/netstat を切り替えるため、pin しないと
    Linux 期待値の直書き assert が macOS/Windows ランナーで落ちる (CI #143)。"""
    monkeypatch.setattr(launcher.os, "name", os_name)
    monkeypatch.setattr(launcher.sys, "platform", platform)


def test_org_down_guidance_when_pid_alive_gives_stop_hint(
        tmp_path, monkeypatch, capsys):
    """admin.token 欠落かつ pid が生存とみなせる半死状態では、案内に生存確認
    (ps -p) / LISTEN 確認 (ss ... grep) / SIGTERM 停止 (kill) の具体手掛かりを
    含める。sidecar は残す (生存 daemon を孤立させない)。"""
    _pin_platform(monkeypatch, "posix", "linux")  # Linux ツール期待値を決定的に
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
    _pin_platform(monkeypatch, "posix", "linux")  # rm/ss 期待値を決定的に
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
    # 掃除コマンドは対象 path を含む (tmp_path は特殊文字なし = shlex.quote は素通し)。
    assert f"rm {os.path.join(state_dir, sidecar.SIDECAR_NAME)}" in err
    # 保守: 案内はしても down 自体は sidecar を残す (確証なき削除をしない)。
    assert sidecar.read_sidecar(state_dir) is not None


def test_half_dead_guidance_alive_linux_commands(monkeypatch):
    """Linux では ps -p / ss ... grep <port> / kill <pid> (SIGTERM) を提示する。"""
    _pin_platform(monkeypatch, "posix", "linux")
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
    _pin_platform(monkeypatch, "posix", "darwin")
    monkeypatch.setattr(launcher.sidecar, "pid_alive", lambda _pid: True)
    msg = launcher._half_dead_daemon_guidance(
        "/tmp/isolated/broker", "127.0.0.1", 59997, 4321)
    assert "lsof -nP -iTCP:59997 -sTCP:LISTEN" in msg
    assert "ss -ltnp" not in msg           # Linux 専用ツールを macOS に出さない
    assert "ps -p 4321" in msg and "kill 4321" in msg


def test_half_dead_guidance_windows_commands(monkeypatch):
    """Windows では tasklist / netstat / taskkill を提示し、POSIX コマンドを
    誤案内しない。"""
    _pin_platform(monkeypatch, "nt", "win32")
    monkeypatch.setattr(launcher.sidecar, "pid_alive", lambda _pid: True)
    msg = launcher._half_dead_daemon_guidance(
        "C:/state/broker", "127.0.0.1", 59997, 4321)
    assert "tasklist" in msg
    assert "netstat -ano" in msg
    assert "taskkill /PID 4321" in msg
    assert "ss -ltnp" not in msg and "kill 4321" not in msg


def test_half_dead_guidance_missing_pid_probes_endpoint(monkeypatch):
    """pid が無い (古い/壊れた sidecar) 場合は endpoint 探索の手掛かりに切り替える。"""
    _pin_platform(monkeypatch, "posix", "linux")
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
    _pin_platform(monkeypatch, "posix", "linux")
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
