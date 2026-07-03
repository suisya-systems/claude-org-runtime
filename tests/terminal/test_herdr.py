"""Tests for ``claude_org_runtime.terminal.herdr.HerdrAdapter``.

The live behaviour (real Herdr server) runs in the fork harness. Here a small
**in-process Unix domain socket JSON-lines server** stands in for Herdr so the
tests pin the wire protocol the adapter speaks — one-shot connect/roundtrip,
method + params construction, response parsing, geometry merge, error-code
normalization, cwd preflight, and the isolation/benign-vs-fatal policy of
``list_panes`` — without needing the ``herdr`` binary (matches the design's
fake-socket test strategy and the Codex review recommendation).
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
from typing import Any, Callable

import pytest

from claude_org_runtime.terminal import herdr as herdr_mod
from claude_org_runtime.terminal.herdr import (
    CODE_ADAPTER_UNAVAILABLE,
    CODE_CWD_INVALID,
    CODE_INTERNAL,
    CODE_INVALID_PARAMS,
    CODE_NAME_IN_USE,
    CODE_PANE_NOT_FOUND,
    PANE_LIVE_ALIVE,
    PANE_LIVE_GONE,
    PANE_LIVE_REUSED,
    PANE_LIVE_UNKNOWN,
    HerdrAdapter,
    HerdrError,
    resolve_socket_path,
)

# The Herdr adapter is POSIX-only (AF_UNIX). The fake server binds an AF_UNIX
# socket, which does not exist on Windows, so skip the whole module there
# (the adapter itself raises adapter_unavailable on Windows — see
# test_windows_is_adapter_unavailable, exercised on POSIX via monkeypatch).
pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="Herdr adapter is POSIX-only (AF_UNIX socket)"
)

# ---------------------------------------------------------------------------
# Fake Herdr Socket API server (newline-delimited JSON over AF_UNIX)
# ---------------------------------------------------------------------------

Handler = Callable[[dict], dict]  # params -> result-or-error body


class FakeHerdrServer:
    """One-shot JSON-lines server mimicking Herdr's socket semantics.

    * Accepts a connection, reads exactly one JSON request line, writes one
      response line, then closes the connection (Herdr closes after each
      non-subscription request — spike §1.1).
    * ``handlers`` maps a method name to a callable returning either a
      ``result`` dict or an ``{"error": {...}}`` dict. Every request is
      recorded in ``requests`` for assertions.
    * ``mode="broken_pipe"`` closes the connection without responding (models
      a server that drops the connection mid-roundtrip).
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self.handlers: dict[str, Handler] = {}
        self.requests: list[dict] = []
        self.mode = "normal"
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(path)
        self._sock.listen(8)
        self._sock.settimeout(0.25)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> "FakeHerdrServer":
        self._thread.start()
        return self

    def on(self, method: str, result: Any) -> "FakeHerdrServer":
        """Register a static ``result`` (dict) or a callable handler."""
        if callable(result):
            self.handlers[method] = result
        else:
            self.handlers[method] = lambda _params, _r=result: _r
        return self

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                self._handle_conn(conn)

    def _handle_conn(self, conn: socket.socket) -> None:
        conn.settimeout(1.0)
        buf = b""
        try:
            while b"\n" not in buf:
                data = conn.recv(65536)
                if not data:
                    return
                buf += data
        except OSError:
            return
        line = buf.split(b"\n", 1)[0]
        try:
            req = json.loads(line.decode("utf-8"))
        except ValueError:
            return
        self.requests.append(req)
        if self.mode == "broken_pipe":
            return  # close without responding
        if self.mode == "garbage":
            # a full newline-framed line that is NOT JSON (schema/protocol break,
            # not a socket fault) — must map to internal, not adapter_unavailable.
            try:
                conn.sendall(b"this is not json\n")
            except OSError:
                pass
            return
        method = req.get("method")
        handler = self.handlers.get(method)
        if handler is None:
            body = {"error": {"code": "invalid_request", "message": f"unknown {method}"}}
        else:
            body = handler(req.get("params") or {})
        resp: dict = {"id": req.get("id")}
        if "error" in body:
            resp["error"] = body["error"]
        else:
            resp["result"] = body
        try:
            conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
        except OSError:
            pass

    def methods_called(self) -> list[str]:
        return [r.get("method") for r in self.requests]

    def params_for(self, method: str) -> dict:
        for r in self.requests:
            if r.get("method") == method:
                return r.get("params") or {}
        raise AssertionError(f"method {method!r} was never called")

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        try:
            self._sock.close()
        finally:
            if os.path.exists(self.path):
                os.unlink(self.path)


@pytest.fixture
def server() -> FakeHerdrServer:
    # macOS/BSD cap the AF_UNIX sun_path at ~104 bytes; pytest's tmp_path embeds
    # the (long) test node id and overflows bind() on macOS runners. Bind under
    # a short mkdtemp dir instead (socket path stays ~25 bytes). Not tmp_path.
    sockdir = tempfile.mkdtemp(prefix="hrdr")
    srv = FakeHerdrServer(os.path.join(sockdir, "s.sock")).start()
    yield srv
    srv.close()  # unlinks the socket file
    try:
        os.rmdir(sockdir)  # now-empty dir; harmless if it lingers
    except OSError:
        pass


def _wire_spawn(server: FakeHerdrServer, *, pane_id: str = "w1:p2") -> None:
    """Register the happy-path spawn chain: workspace.create -> agent.start ->
    pane.close (root cleanup)."""
    server.on(
        "workspace.create",
        {
            "type": "workspace_created",
            "workspace": {"workspace_id": "w1", "active_tab_id": "w1:t1"},
            "tab": {"tab_id": "w1:t1", "workspace_id": "w1"},
            "root_pane": {"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1"},
        },
    )
    server.on(
        "agent.start",
        lambda params: {
            "type": "agent_started",
            "agent": {"pane_id": pane_id, "name": params.get("name")},
        },
    )
    server.on("pane.close", {"type": "ok"})


def _adapter(server: FakeHerdrServer) -> HerdrAdapter:
    return HerdrAdapter(socket_path=server.path, timeout=2.0)


# ---------------------------------------------------------------------------
# socket path resolution (no server needed)
# ---------------------------------------------------------------------------

def test_resolve_socket_path_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "/cfg")
    monkeypatch.setenv("HERDR_SOCKET_PATH", "/env/sock")
    monkeypatch.setenv("HERDR_SESSION", "envsess")
    # explicit arg wins over everything
    assert resolve_socket_path("/explicit/sock") == "/explicit/sock"
    # HERDR_SOCKET_PATH wins over session
    assert resolve_socket_path() == "/env/sock"
    monkeypatch.delenv("HERDR_SOCKET_PATH")
    # session env -> sessions/<name>/herdr.sock
    assert resolve_socket_path() == "/cfg/herdr/sessions/envsess/herdr.sock"
    monkeypatch.delenv("HERDR_SESSION")
    # default socket under config dir
    assert resolve_socket_path() == "/cfg/herdr/herdr.sock"
    # explicit session arg beats env-derived default
    assert resolve_socket_path(session="s2") == "/cfg/herdr/sessions/s2/herdr.sock"


def test_find_herdr_raises_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(herdr_mod.shutil, "which", lambda _n: None)
    with pytest.raises(FileNotFoundError):
        herdr_mod.find_herdr()


def test_windows_is_adapter_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(herdr_mod.os, "name", "nt")
    with pytest.raises(HerdrError) as exc:
        HerdrAdapter(socket_path="/whatever.sock")
    assert exc.value.code == CODE_ADAPTER_UNAVAILABLE


def test_isolated_session_is_true() -> None:
    # 専用 workspace で無関係 pane を厳格フィルタするため isolated_session=True。
    assert HerdrAdapter.isolated_session is True


def test_make_adapter_herdr_branch() -> None:
    # --backend herdr wiring: make_adapter routes to HerdrAdapter. Instantiation
    # only builds the socket client (no connection), so this is safe offline.
    from claude_org_runtime.terminal import make_adapter

    adapter = make_adapter("herdr")
    assert isinstance(adapter, HerdrAdapter)


def test_herdr_in_valid_backends() -> None:
    from claude_org_runtime.terminal import VALID_BACKENDS

    assert "herdr" in VALID_BACKENDS


# ---------------------------------------------------------------------------
# spawn
# ---------------------------------------------------------------------------

def test_spawn_creates_workspace_then_agent(server: FakeHerdrServer, tmp_path) -> None:
    _wire_spawn(server, pane_id="w1:p2")
    a = _adapter(server)
    ref = a.spawn(["claude", "--flag"], cwd=str(tmp_path))
    assert (ref.pane_id, ref.window_id, ref.tab_id) == ("w1:p2", "w1", "w1:t1")
    # order: workspace.create -> agent.start -> pane.close (root cleanup)
    assert server.methods_called() == [
        "workspace.create",
        "agent.start",
        "pane.close",
    ]
    ap = server.params_for("agent.start")
    assert ap["argv"] == ["claude", "--flag"]
    assert ap["workspace"] == "w1" and ap["tab"] == "w1:t1"
    assert ap["cwd"] == str(tmp_path)
    # root shell pane (w1:p1) is the one closed
    assert server.params_for("pane.close")["pane_id"] == "w1:p1"


def test_spawn_second_reuses_workspace_no_recreate(
    server: FakeHerdrServer, tmp_path
) -> None:
    _wire_spawn(server)
    a = _adapter(server)
    a.spawn(["claude"], cwd=str(tmp_path))
    server.requests.clear()
    server.on(
        "agent.start",
        {"type": "agent_started", "agent": {"pane_id": "w1:p3", "name": "x"}},
    )
    ref2 = a.spawn(["codex"], cwd=str(tmp_path))
    assert ref2.pane_id == "w1:p3"
    # no second workspace.create, no root cleanup; just agent.start into w1/t1
    assert server.methods_called() == ["agent.start"]
    assert server.params_for("agent.start")["workspace"] == "w1"
    assert server.params_for("agent.start")["split"] == "down"


def test_spawn_cwd_preflight_rejects_before_any_socket_call(
    server: FakeHerdrServer,
) -> None:
    _wire_spawn(server)
    a = _adapter(server)
    with pytest.raises(HerdrError) as exc:
        a.spawn(["claude"], cwd="/no/such/dir/xyz")
    assert exc.value.code == CODE_CWD_INVALID
    # layout must not be mutated: no request reached the server
    assert server.requests == []


# ---------------------------------------------------------------------------
# placement reconciliation (Issue #114 Fix-C: agent.start ignores workspace param
# and lands in the focused *user* workspace; spawn must pane.move it back into the
# dedicated tab, before root cleanup, only when actually diverged)
# ---------------------------------------------------------------------------

def _wire_spawn_diverged(
    server: FakeHerdrServer,
    *,
    dedicated: str = "w2",
    tab: str = "w2:t1",
    root: str = "w2:p1",
    landed_pane: str = "w1:p2",
    landed_ws: str = "w1",
    moved_pane: str = "w2:p2",
    terminal_id: str = "term_disp",
) -> None:
    """Wire the DIVERGED spawn chain (Issue #114): ``agent.start`` reports the pane
    landed in the focused *user* workspace (``landed_ws``), diverging from the
    dedicated one (``dedicated``), so spawn must ``pane.move`` it into the dedicated
    tab and return the post-move id (``moved_pane``)."""
    server.on(
        "workspace.create",
        {
            "type": "workspace_created",
            "workspace": {"workspace_id": dedicated, "active_tab_id": tab},
            "root_pane": {"pane_id": root, "workspace_id": dedicated, "tab_id": tab},
        },
    )
    server.on(
        "agent.start",
        lambda params: {
            "type": "agent_started",
            "agent": {
                "pane_id": landed_pane,
                "workspace_id": landed_ws,
                "terminal_id": terminal_id,
                "name": params.get("name"),
            },
        },
    )
    server.on(
        "pane.move",
        {
            "type": "pane_move",
            "move_result": {
                "changed": True,
                "previous_pane_id": landed_pane,
                "pane": {
                    "pane_id": moved_pane,
                    "workspace_id": dedicated,
                    "terminal_id": terminal_id,
                },
            },
        },
    )
    server.on("pane.close", {"type": "ok"})


def test_spawn_diverged_moves_pane_into_dedicated_tab_before_root_cleanup(
    server: FakeHerdrServer, tmp_path
) -> None:
    _wire_spawn_diverged(server)
    a = _adapter(server)
    ref = a.spawn(["claude"], cwd=str(tmp_path))
    # PaneRef reports the POST-move id + dedicated ws + preserved terminal_id.
    assert ref.pane_id == "w2:p2"
    assert ref.window_id == "w2"
    assert ref.terminal_id == "term_disp"
    # order matters: move must precede root cleanup (else the dedicated ws auto-closes
    # when its root pane is closed and the move target tab disappears).
    assert server.methods_called() == [
        "workspace.create",
        "agent.start",
        "pane.move",
        "pane.close",
    ]
    mv = server.params_for("pane.move")
    assert mv["pane_id"] == "w1:p2"
    assert mv["destination"] == {"type": "tab", "tab_id": "w2:t1", "split": "down"}
    # root cleanup closes the dedicated ws's root pane, NOT the moved agent pane.
    assert server.params_for("pane.close")["pane_id"] == "w2:p1"


def test_spawn_not_diverged_skips_move_idempotent(
    server: FakeHerdrServer, tmp_path
) -> None:
    """Idempotency (requirement): if a future Herdr honors the workspace param and the
    pane lands directly in the dedicated ws, spawn must NOT issue a (double) move."""
    server.on(
        "workspace.create",
        {
            "type": "workspace_created",
            "workspace": {"workspace_id": "w2", "active_tab_id": "w2:t1"},
            "root_pane": {"pane_id": "w2:p1", "workspace_id": "w2", "tab_id": "w2:t1"},
        },
    )
    server.on(
        "agent.start",
        lambda params: {
            "type": "agent_started",
            "agent": {
                "pane_id": "w2:p2",
                "workspace_id": "w2",  # honored: lands in dedicated
                "terminal_id": "term_ok",
                "name": params.get("name"),
            },
        },
    )
    server.on("pane.close", {"type": "ok"})
    a = _adapter(server)
    ref = a.spawn(["claude"], cwd=str(tmp_path))
    assert ref.pane_id == "w2:p2"
    assert ref.terminal_id == "term_ok"
    assert "pane.move" not in server.methods_called()
    assert server.methods_called() == ["workspace.create", "agent.start", "pane.close"]


def test_spawn_divergence_detected_via_pane_id_prefix_when_workspace_id_absent(
    server: FakeHerdrServer, tmp_path
) -> None:
    """If agent.start omits ``workspace_id`` (older/newer schema), divergence is still
    detected from the ``wN:pM`` pane_id prefix, so the move still fires."""
    _wire_spawn_diverged(server)
    # drop workspace_id from the agent response; keep the w1 pane_id prefix.
    server.on(
        "agent.start",
        lambda params: {
            "type": "agent_started",
            "agent": {"pane_id": "w1:p2", "terminal_id": "term_disp",
                      "name": params.get("name")},
        },
    )
    a = _adapter(server)
    ref = a.spawn(["claude"], cwd=str(tmp_path))
    assert ref.pane_id == "w2:p2"  # moved
    assert "pane.move" in server.methods_called()


def test_spawn_diverged_move_failure_closes_stranded_pane_and_raises(
    server: FakeHerdrServer, tmp_path
) -> None:
    """If the corrective move fails, the pane is stranded in the user's workspace
    (isolation broken). spawn best-effort closes it and propagates the error rather
    than returning an isolation-broken PaneRef; root cleanup never runs."""
    _wire_spawn_diverged(server)
    server.on("pane.move", {"error": {"code": "invalid_request", "message": "nope"}})
    a = _adapter(server)
    with pytest.raises(HerdrError) as exc:
        a.spawn(["claude"], cwd=str(tmp_path))
    assert exc.value.code == CODE_INVALID_PARAMS
    # the stranded landed pane (w1:p2) is best-effort closed; root (w2:p1) is NOT.
    closes = [r for r in server.requests
              if r.get("method") == "pane.close"]
    assert [c["params"]["pane_id"] for c in closes] == ["w1:p2"]


def test_spawn_diverged_move_missing_post_move_id_is_internal(
    server: FakeHerdrServer, tmp_path
) -> None:
    _wire_spawn_diverged(server)
    server.on("pane.move", {"type": "pane_move", "move_result": {"changed": True}})
    a = _adapter(server)
    with pytest.raises(HerdrError) as exc:
        a.spawn(["claude"], cwd=str(tmp_path))
    assert exc.value.code == CODE_INTERNAL


# ---------------------------------------------------------------------------
# pane_liveness (Issue #114 Fix-D: workspace-independent authoritative liveness
# via pane.get + terminal_id reuse guard)
# ---------------------------------------------------------------------------

def test_pane_liveness_alive_when_present_and_terminal_id_matches(
    server: FakeHerdrServer,
) -> None:
    server.on(
        "pane.get",
        {"type": "pane_info",
         "pane": {"pane_id": "w2:p2", "terminal_id": "term_disp", "workspace_id": "w2"}},
    )
    a = _adapter(server)
    assert a.pane_liveness("w2:p2", "term_disp") == PANE_LIVE_ALIVE


def test_pane_liveness_reused_when_terminal_id_differs(
    server: FakeHerdrServer,
) -> None:
    """pane_id resolves but to a DIFFERENT terminal (id reused by a foreign pane):
    our pane is gone; the broker must reap bookkeeping WITHOUT closing that id."""
    server.on(
        "pane.get",
        {"type": "pane_info",
         "pane": {"pane_id": "w2:p2", "terminal_id": "term_SOMEONE_ELSE"}},
    )
    a = _adapter(server)
    assert a.pane_liveness("w2:p2", "term_disp") == PANE_LIVE_REUSED


def test_pane_liveness_gone_when_pane_not_found(server: FakeHerdrServer) -> None:
    server.on("pane.get", {"error": {"code": "pane_not_found", "message": "no"}})
    a = _adapter(server)
    assert a.pane_liveness("w9:p9", "term_x") == PANE_LIVE_GONE


def test_pane_liveness_gone_when_workspace_not_found(server: FakeHerdrServer) -> None:
    # workspace_not_found normalizes to pane_not_found (CODE_PANE_NOT_FOUND) -> gone.
    server.on("pane.get", {"error": {"code": "workspace_not_found", "message": "no"}})
    a = _adapter(server)
    assert a.pane_liveness("w9:p9", "term_x") == PANE_LIVE_GONE


def test_pane_liveness_unknown_when_backend_unreachable(tmp_path) -> None:
    # no server bound at this path -> socket unreachable -> adapter_unavailable -> unknown
    # (must NOT be treated as gone: an unreachable backend is not proof of death).
    a = HerdrAdapter(socket_path=str(tmp_path / "absent.sock"), timeout=0.3)
    assert a.pane_liveness("w1:p1", "term_x") == PANE_LIVE_UNKNOWN


def test_pane_liveness_no_recorded_terminal_id_treats_present_as_alive(
    server: FakeHerdrServer,
) -> None:
    """Old registry entry without a recorded terminal_id: the reuse guard is skipped
    (can't compare), but workspace-independent liveness still resolves present->alive."""
    server.on(
        "pane.get",
        {"type": "pane_info", "pane": {"pane_id": "w2:p2", "terminal_id": "term_disp"}},
    )
    a = _adapter(server)
    assert a.pane_liveness("w2:p2", None) == PANE_LIVE_ALIVE


# ---------------------------------------------------------------------------
# list_panes
# ---------------------------------------------------------------------------

def test_list_panes_empty_before_spawn(server: FakeHerdrServer) -> None:
    a = _adapter(server)
    assert a.list_panes() == []
    assert server.requests == []  # no workspace -> no socket call


def test_list_panes_merges_geometry_and_filters_workspace(
    server: FakeHerdrServer, tmp_path
) -> None:
    _wire_spawn(server)
    a = _adapter(server)
    a.spawn(["claude"], cwd=str(tmp_path))
    server.requests.clear()
    server.on(
        "pane.list",
        {
            "type": "pane_list",
            "panes": [
                {"pane_id": "w1:p2", "workspace_id": "w1", "tab_id": "w1:t1",
                 "cwd": "/work", "label": "claude"},
                # an unrelated pane leaking a different workspace must be dropped
                {"pane_id": "w9:p9", "workspace_id": "w9", "tab_id": "w9:t1"},
            ],
        },
    )
    server.on(
        "pane.layout",
        {
            "type": "layout",
            "layout": {
                "area": {"x": 0, "y": 0, "width": 80, "height": 24},
                "focused_pane_id": "w1:p2",
                "panes": [
                    {"pane_id": "w1:p2", "rect": {"x": 26, "y": 1, "width": 54, "height": 23}},
                ],
            },
        },
    )
    panes = a.list_panes()
    assert len(panes) == 1  # w9:p9 filtered out by workspace_id
    rec = panes[0]
    assert rec["pane_id"] == "w1:p2"
    assert (rec["x"], rec["y"], rec["width"], rec["height"]) == (26, 1, 54, 23)
    assert rec["active"] is True  # matches focused_pane_id
    assert rec["cwd"] == "/work"
    assert rec["window_id"] == "w1"
    # pane.list scoped to our workspace
    assert server.params_for("pane.list")["workspace_id"] == "w1"


def test_list_panes_active_false_for_unfocused(
    server: FakeHerdrServer, tmp_path
) -> None:
    # Two panes survive the workspace filter; only the focused_pane_id gets
    # active=True — exercises the active=False branch of the geometry merge.
    _wire_spawn(server)
    a = _adapter(server)
    a.spawn(["claude"], cwd=str(tmp_path))
    server.on(
        "pane.list",
        {
            "panes": [
                {"pane_id": "w1:p2", "workspace_id": "w1"},
                {"pane_id": "w1:p3", "workspace_id": "w1"},
            ]
        },
    )
    server.on(
        "pane.layout",
        {
            "layout": {
                "focused_pane_id": "w1:p3",
                "panes": [
                    {"pane_id": "w1:p2", "rect": {"x": 0, "y": 0, "width": 40, "height": 20}},
                    {"pane_id": "w1:p3", "rect": {"x": 40, "y": 0, "width": 40, "height": 20}},
                ],
            }
        },
    )
    by_id = {p["pane_id"]: p for p in a.list_panes()}
    assert by_id["w1:p3"]["active"] is True
    assert by_id["w1:p2"]["active"] is False


def test_list_panes_workspace_gone_returns_empty_and_recovers(
    server: FakeHerdrServer, tmp_path
) -> None:
    _wire_spawn(server)
    a = _adapter(server)
    a.spawn(["claude"], cwd=str(tmp_path))
    server.on(
        "pane.list",
        {"error": {"code": "workspace_not_found", "message": "gone"}},
    )
    assert a.list_panes() == []  # benign: our workspace was closed externally
    # cached workspace state must be cleared so the next spawn re-creates it
    # (else agent.start targets the vanished workspace forever).
    assert a._workspace_id is None
    server.requests.clear()
    _wire_spawn(server, pane_id="w2:p2")  # re-arm workspace.create/agent.start
    server.on(
        "workspace.create",
        {
            "workspace": {"workspace_id": "w2", "active_tab_id": "w2:t1"},
            "root_pane": {"pane_id": "w2:p1"},
        },
    )
    ref = a.spawn(["claude"], cwd=str(tmp_path))
    assert ref.window_id == "w2"  # a fresh workspace was created
    assert "workspace.create" in server.methods_called()


def test_list_panes_drops_panes_missing_workspace_id(
    server: FakeHerdrServer, tmp_path
) -> None:
    # Strict filter: an unscoped / older-schema pane record with no workspace_id
    # must NOT leak into list_panes (isolated_session -> org down would close it).
    _wire_spawn(server)
    a = _adapter(server)
    a.spawn(["claude"], cwd=str(tmp_path))
    server.on(
        "pane.list",
        {"panes": [
            {"pane_id": "w1:p2", "workspace_id": "w1"},
            {"pane_id": "x:p9"},  # no workspace_id -> must be dropped
        ]},
    )
    server.on("pane.layout", {"layout": {"panes": []}})
    ids = [p["pane_id"] for p in a.list_panes()]
    assert ids == ["w1:p2"]


def test_list_panes_unreachable_raises_not_empty() -> None:
    # A dead socket must surface as adapter_unavailable, NOT be flattened to []
    # (else pane_exists misreads "backend down" as "pane missing"). Short path
    # so this tests file-not-found, not the macOS AF_UNIX length limit.
    a = HerdrAdapter(
        socket_path=os.path.join(tempfile.mkdtemp(prefix="hrdr"), "nope.sock"),
        timeout=1.0,
    )
    a._workspace_id = "w1"  # pretend a workspace was bound
    a._tab_id = "w1:t1"
    with pytest.raises(HerdrError) as exc:
        a.list_panes()
    assert exc.value.code == CODE_ADAPTER_UNAVAILABLE


def test_pane_exists_uses_list(server: FakeHerdrServer, tmp_path) -> None:
    _wire_spawn(server)
    a = _adapter(server)
    a.spawn(["claude"], cwd=str(tmp_path))
    server.on(
        "pane.list",
        {"panes": [{"pane_id": "w1:p2", "workspace_id": "w1"}]},
    )
    server.on("pane.layout", {"layout": {"panes": []}})
    assert a.pane_exists("w1:p2") is True
    assert a.pane_exists("w1:p99") is False


# ---------------------------------------------------------------------------
# get_text / send primitives
# ---------------------------------------------------------------------------

def test_get_text_visible_text(server: FakeHerdrServer) -> None:
    server.on("pane.read", {"read": {"text": "screen contents", "format": "text"}})
    a = _adapter(server)
    assert a.get_text("w1:p2") == "screen contents"
    p = server.params_for("pane.read")
    assert p["source"] == "visible" and p["format"] == "text"


def test_get_text_escapes_uses_ansi(server: FakeHerdrServer) -> None:
    server.on("pane.read", {"read": {"text": "\x1b[0m", "format": "ansi"}})
    a = _adapter(server)
    a.get_text("w1:p2", escapes=True)
    assert server.params_for("pane.read")["format"] == "ansi"


def test_type_text_send_text(server: FakeHerdrServer) -> None:
    server.on("pane.send_text", {"type": "ok"})
    a = _adapter(server)
    a.type_text("w1:p2", "hello there")
    assert server.params_for("pane.send_text") == {"pane_id": "w1:p2", "text": "hello there"}


def test_send_enter_and_interrupt(server: FakeHerdrServer) -> None:
    server.on("pane.send_keys", {"type": "ok"})
    a = _adapter(server)
    a.send_enter("w1:p2")
    assert server.params_for("pane.send_keys")["keys"] == ["enter"]
    server.requests.clear()
    a.send_interrupt("w1:p2")
    assert server.params_for("pane.send_keys")["keys"] == ["ctrl+c"]


def test_send_named_keys_maps_canonical_to_herdr_tokens(
    server: FakeHerdrServer,
) -> None:
    # representative raw keys mapped to Herdr tokens (backtab -> shift+tab,
    # esc -> escape), batched into one pane.send_keys request, order preserved
    # (Issue #108). Tokens pinned against the live Herdr 0.7.1 probe.
    server.on("pane.send_keys", {"type": "ok"})
    a = _adapter(server)
    a.send_named_keys("w1:p2", ["backtab", "esc", "up", "left", "ctrl+a"])
    p = server.params_for("pane.send_keys")
    assert p["pane_id"] == "w1:p2"
    assert p["keys"] == ["shift+tab", "escape", "up", "left", "ctrl+a"]


def test_supported_named_keys_is_measured_subset() -> None:
    # Herdr 0.7.1 send-keys is NOT the full vocabulary: Delete/Home/End/
    # PageUp/PageDown are rejected by the server (invalid_key), so they are
    # intentionally excluded. Everything else (arrows, backtab, ctrl+a..z) is
    # supported. Pinned against the live-server probe.
    from claude_org_runtime.terminal.keys import CANONICAL_KEYS

    missing = CANONICAL_KEYS - HerdrAdapter.supported_named_keys
    assert missing == {"delete", "home", "end", "pageup", "pagedown"}
    assert HerdrAdapter.supported_named_keys < CANONICAL_KEYS


def test_send_named_keys_empty_is_noop(server: FakeHerdrServer) -> None:
    a = _adapter(server)
    a.send_named_keys("w1:p2", [])
    assert server.methods_called() == []


def test_send_line_text_then_enter(
    server: FakeHerdrServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(herdr_mod.time, "sleep", lambda _s: None)
    server.on("pane.send_text", {"type": "ok"})
    server.on("pane.send_keys", {"type": "ok"})
    a = _adapter(server)
    a.send_line("w1:p2", "nudge")
    assert server.methods_called() == ["pane.send_text", "pane.send_keys"]
    assert server.params_for("pane.send_text")["text"] == "nudge"
    assert server.params_for("pane.send_keys")["keys"] == ["enter"]


def test_kill_pane_swallows_errors(server: FakeHerdrServer) -> None:
    server.on("pane.close", {"error": {"code": "pane_not_found", "message": "gone"}})
    a = _adapter(server)
    a.kill_pane("w1:p2")  # must not raise (best-effort cleanup)
    assert server.params_for("pane.close")["pane_id"] == "w1:p2"


def test_kill_pane_last_pane_falls_back_to_workspace_close(
    server: FakeHerdrServer, tmp_path
) -> None:
    # Herdr rejects closing the sole remaining pane of a tab; the adapter must
    # then close the whole workspace to actually reap the TUI, instead of
    # silently reporting success while it keeps running (Codex P1).
    _wire_spawn(server)
    a = _adapter(server)
    a.spawn(["claude"], cwd=str(tmp_path))
    server.on("pane.close", {"error": {"code": "single_pane", "message": "last pane"}})
    server.on("pane.list", {"panes": [{"pane_id": "w1:p2", "workspace_id": "w1"}]})
    server.on("pane.layout", {"layout": {"panes": []}})
    server.on("workspace.close", {"type": "ok"})
    a.kill_pane("w1:p2")
    assert "workspace.close" in server.methods_called()
    assert a._workspace_id is None  # workspace reaped -> state cleared


def test_kill_pane_non_last_does_not_close_workspace(
    server: FakeHerdrServer, tmp_path
) -> None:
    # When other panes remain, a pane.close failure must NOT tear down the whole
    # workspace (only the sole-pane case escalates to workspace.close).
    _wire_spawn(server)
    a = _adapter(server)
    a.spawn(["claude"], cwd=str(tmp_path))
    server.on("pane.close", {"error": {"code": "boom", "message": "x"}})
    server.on(
        "pane.list",
        {"panes": [
            {"pane_id": "w1:p2", "workspace_id": "w1"},
            {"pane_id": "w1:p3", "workspace_id": "w1"},
        ]},
    )
    server.on("pane.layout", {"layout": {"panes": []}})
    a.kill_pane("w1:p2")
    assert "workspace.close" not in server.methods_called()
    assert a._workspace_id == "w1"  # workspace preserved


def test_kill_pane_detailed_reports_pane_close_and_absence(
    server: FakeHerdrServer, tmp_path
) -> None:
    # kill_pane_detailed reports which close path fired + a post-close residual
    # check, so the broker's reap physical-close verification can journal it.
    _wire_spawn(server)
    a = _adapter(server)
    a.spawn(["claude"], cwd=str(tmp_path))
    server.on("pane.close", {"type": "ok"})
    server.on("pane.list", {"panes": []})            # gone after close
    server.on("pane.layout", {"layout": {"panes": []}})
    detail = a.kill_pane_detailed("w1:p2")
    assert detail["closed_via"] == "pane.close"
    assert detail["still_present"] is False


def test_kill_pane_detailed_reports_workspace_close_fallback(
    server: FakeHerdrServer, tmp_path
) -> None:
    # Sole-pane close refusal escalates to workspace.close; the detail records
    # the fallback path and that nothing remains afterwards.
    _wire_spawn(server)
    a = _adapter(server)
    a.spawn(["claude"], cwd=str(tmp_path))
    server.on("pane.close", {"error": {"code": "single_pane", "message": "last"}})
    server.on("pane.list", {"panes": [{"pane_id": "w1:p2", "workspace_id": "w1"}]})
    server.on("pane.layout", {"layout": {"panes": []}})
    server.on("workspace.close", {"type": "ok"})
    detail = a.kill_pane_detailed("w1:p2")
    assert detail["closed_via"] == "workspace.close"
    assert a._workspace_id is None
    assert detail["still_present"] is False


def test_kill_pane_detailed_workspace_close_failure_is_not_reported_success(
    server: FakeHerdrServer, tmp_path
) -> None:
    # If the sole-pane fallback's workspace.close itself fails, the detail must
    # NOT claim "workspace.close" success (that would let the broker drop
    # bookkeeping for a still-live pane and orphan it — Codex round3 P2). It
    # reports a distinct workspace_close_failed code and preserves the cached
    # workspace state (so it is not mistaken for a fresh workspace next spawn).
    _wire_spawn(server)
    a = _adapter(server)
    a.spawn(["claude"], cwd=str(tmp_path))
    server.on("pane.close", {"error": {"code": "single_pane", "message": "last"}})
    server.on("pane.list", {"panes": [{"pane_id": "w1:p2", "workspace_id": "w1"}]})
    server.on("pane.layout", {"layout": {"panes": []}})
    server.on("workspace.close", {"error": {"code": "boom", "message": "x"}})
    detail = a.kill_pane_detailed("w1:p2")
    assert detail["closed_via"] == "workspace_close_failed"
    assert a._workspace_id == "w1"       # state preserved on failure (retry-able)
    assert detail["still_present"] is True  # pane still listed -> not orphaned silently


def test_kill_pane_detailed_refusal_with_lagging_empty_list_is_refused(
    server: FakeHerdrServer, tmp_path
) -> None:
    # pane.close is refused as single_pane (proof the pane exists and is sole),
    # but the eventually-consistent pane.list lags and returns []. This must NOT
    # be reported as already_gone — trusting the stale empty list would let the
    # broker drop bookkeeping for a live pane and orphan it (Codex round4 P1).
    # With no positive sole-pane confirmation it degrades to refused (defer).
    _wire_spawn(server)
    a = _adapter(server)
    a.spawn(["claude"], cwd=str(tmp_path))
    server.on("pane.close", {"error": {"code": "single_pane", "message": "last"}})
    server.on("pane.list", {"panes": []})   # lag hides the live sole pane
    server.on("pane.layout", {"layout": {"panes": []}})
    detail = a.kill_pane_detailed("w1:p2")
    assert detail["closed_via"] == "refused"     # not "already_gone"
    assert "workspace.close" not in server.methods_called()
    assert a._workspace_id == "w1"               # workspace not torn down


def test_kill_pane_detailed_pane_not_found_is_already_gone(
    server: FakeHerdrServer, tmp_path
) -> None:
    # A definitive pane_not_found on close authoritatively means gone; we trust
    # the close error and do NOT consult the (lag-prone) list.
    _wire_spawn(server)
    a = _adapter(server)
    a.spawn(["claude"], cwd=str(tmp_path))
    server.on("pane.close", {"error": {"code": "pane_not_found", "message": "gone"}})
    server.on("pane.list", {"panes": []})
    server.on("pane.layout", {"layout": {"panes": []}})
    detail = a.kill_pane_detailed("w1:p2")
    assert detail["closed_via"] == "already_gone"
    assert "workspace.close" not in server.methods_called()


def test_close_workspace_returns_false_on_failure(
    server: FakeHerdrServer, tmp_path
) -> None:
    # close_workspace reports success/failure and only clears cached state when
    # the workspace.close actually succeeded.
    _wire_spawn(server)
    a = _adapter(server)
    a.spawn(["claude"], cwd=str(tmp_path))
    server.on("workspace.close", {"error": {"code": "boom", "message": "x"}})
    assert a.close_workspace() is False
    assert a._workspace_id == "w1"       # preserved on failure


def test_reap_thresholds_are_conservative_for_herdr() -> None:
    # Herdr's pane.list is eventually consistent, so the broker's opportunistic
    # reap must gate on pane age, multiple misses, AND a cadence-insensitive
    # wall-time-missing window (not the immediate tmux/wezterm reap). Pin the
    # backend-aware ClassVars.
    assert HerdrAdapter.reap_min_age_seconds > 0
    assert HerdrAdapter.reap_min_missing_snapshots >= 2
    assert HerdrAdapter.reap_min_missing_seconds > 0


def test_close_workspace(server: FakeHerdrServer, tmp_path) -> None:
    _wire_spawn(server)
    a = _adapter(server)
    a.spawn(["claude"], cwd=str(tmp_path))
    server.on("workspace.close", {"type": "ok"})
    a.close_workspace()
    assert server.params_for("workspace.close")["workspace_id"] == "w1"
    # idempotent: state cleared, a second call is a no-op (no socket call)
    server.requests.clear()
    a.close_workspace()
    assert server.requests == []


# ---------------------------------------------------------------------------
# error-code normalization
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw_code, expected",
    [
        ("pane_not_found", CODE_PANE_NOT_FOUND),
        ("workspace_not_found", CODE_PANE_NOT_FOUND),
        ("invalid_request", CODE_INVALID_PARAMS),
        ("invalid_key", CODE_INVALID_PARAMS),
        ("name_taken", CODE_NAME_IN_USE),
    ],
)
def test_error_code_mapping(server: FakeHerdrServer, raw_code: str, expected: str) -> None:
    server.on("pane.read", {"error": {"code": raw_code, "message": "x"}})
    a = _adapter(server)
    with pytest.raises(HerdrError) as exc:
        a.get_text("w1:p2")
    assert exc.value.code == expected
    assert exc.value.raw == raw_code


def test_unknown_raw_code_not_adapter_unavailable(server: FakeHerdrServer) -> None:
    # Unknown raw codes must NOT be mapped to adapter_unavailable (that would
    # break the adapter-down vs broker-down separation, design §4.6).
    server.on("pane.read", {"error": {"code": "some_new_code", "message": "x"}})
    a = _adapter(server)
    with pytest.raises(HerdrError) as exc:
        a.get_text("w1:p2")
    assert exc.value.code != CODE_ADAPTER_UNAVAILABLE
    assert exc.value.code == herdr_mod.CODE_INTERNAL
    assert exc.value.raw == "some_new_code"


def test_socket_unreachable_is_adapter_unavailable() -> None:
    a = HerdrAdapter(
        socket_path=os.path.join(tempfile.mkdtemp(prefix="hrdr"), "absent.sock"),
        timeout=1.0,
    )
    with pytest.raises(HerdrError) as exc:
        a.get_text("w1:p2")
    assert exc.value.code == CODE_ADAPTER_UNAVAILABLE


def test_broken_pipe_is_adapter_unavailable(server: FakeHerdrServer) -> None:
    server.mode = "broken_pipe"
    a = _adapter(server)
    with pytest.raises(HerdrError) as exc:
        a.get_text("w1:p2")
    assert exc.value.code == CODE_ADAPTER_UNAVAILABLE


def test_malformed_response_is_internal_not_adapter_unavailable(
    server: FakeHerdrServer,
) -> None:
    # A full newline-framed but non-JSON response is a protocol/schema break,
    # not a socket fault — it must NOT be classified as adapter_unavailable
    # (design §4.6: that code is reserved for confirmed socket unreachability,
    # so the dispatcher does not falsely fail over on a version mismatch).
    server.mode = "garbage"
    a = _adapter(server)
    with pytest.raises(HerdrError) as exc:
        a.get_text("w1:p2")
    assert exc.value.code == herdr_mod.CODE_INTERNAL
