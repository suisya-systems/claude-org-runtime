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
import stat
import threading
import time

import pytest

from claude_org_runtime.broker import cli as broker_cli
from claude_org_runtime.broker import launcher
from claude_org_runtime.broker import sidecar
from claude_org_runtime.broker.server import Broker
from claude_org_runtime.terminal import default_backend


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

    def fake_launch(argv):
        captured["argv"] = argv
        return 0

    rc = launcher.org_up(_up_args(state_dir), spawn_daemon=fake_spawn,
                         launch=fake_launch)
    assert rc == 0
    assert spawn_calls == []                       # 再利用 → 新規起動しない
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
                    launch=lambda argv: 0)
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
        launch=lambda argv: launched.append(argv) or 0,
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
        launch=lambda argv: launched.append(argv) or 0,
    )
    assert rc == 2                                 # 競合エラー
    assert launched == []                          # 起動しない


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

    def fake_launch(argv):
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
                         launch=lambda argv: launched.append(argv) or 0)
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


# =================================================== up: unhealthy live daemon
def test_org_up_errors_when_mcp_surface_unhealthy(live_daemon, monkeypatch):
    """admin は応答するが MCP 面が健全でない生存 daemon は unhealthy エラー。"""
    b, state_dir = live_daemon
    monkeypatch.setattr(launcher, "_mcp_surface_ok", lambda *a, **k: False)
    launched = []
    rc = launcher.org_up(
        _up_args(state_dir),
        spawn_daemon=lambda *a, **k: (_ for _ in ()).throw(AssertionError()),
        launch=lambda argv: launched.append(argv) or 0,
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
        launch=lambda argv: 0,
    )
    # admin-* の probe bind は close() で registered=False に落ちている。
    registered_admin = [
        bnd for bnd in b._binds.values()
        if bnd.agent_id.startswith("admin-") and bnd.registered and not bnd.revoked
    ]
    assert registered_admin == []
