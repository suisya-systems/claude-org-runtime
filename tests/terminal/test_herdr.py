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
from claude_org_runtime.terminal.base import (
    SPACE_CONTROL,
    SPACE_UNASSIGNED,
    SpaceDescriptor,
)
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
    # the reuse path verifies the cached workspace still exists (§4.2 / Codex P2) via
    # workspace.list before reusing it — wire it present so reuse proceeds.
    server.on("workspace.list", {"workspaces": [{"workspace_id": "w1", "label": "l"}]})
    server.on(
        "agent.start",
        {"type": "agent_started", "agent": {"pane_id": "w1:p3", "name": "x"}},
    )
    ref2 = a.spawn(["codex"], cwd=str(tmp_path))
    assert ref2.pane_id == "w1:p3"
    # no second workspace.create (reuse), no root cleanup; agent.start into the same w1/t1.
    assert "workspace.create" not in server.methods_called()
    assert "agent.start" in server.methods_called()
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
    # Two panes survive the registry + workspace filter; only the focused_pane_id
    # gets active=True — exercises the active=False branch of the geometry merge.
    # Both panes must be in the adapter registry (Issue #110 §4.1 primary gate), so
    # spawn twice to record w1:p2 and w1:p3.
    _wire_spawn(server)
    a = _adapter(server)
    a.spawn(["claude"], cwd=str(tmp_path))
    server.on(
        "agent.start",
        {"type": "agent_started", "agent": {"pane_id": "w1:p3", "name": "x"}},
    )
    a.spawn(["codex"], cwd=str(tmp_path))
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


def test_list_panes_workspace_not_found_marks_degraded_not_clear(
    server: FakeHerdrServer, tmp_path
) -> None:
    # Issue #110 §4.2 supersede: a single workspace_not_found must NOT clear/recreate
    # the space (that eager clear was the #109/#114 orphan-proliferation arm). It marks
    # the space DEGRADED and keeps it (no auto-recreate). list_panes returns [] for that
    # (degraded) workspace but does not drop the space.
    _wire_spawn(server)
    a = _adapter(server)
    a.spawn(["claude"], cwd=str(tmp_path))
    server.on(
        "pane.list",
        {"error": {"code": "workspace_not_found", "message": "gone"}},
    )
    # workspace.list still shows w1 -> transient blip, stays DEGRADED (not GONE).
    server.on(
        "workspace.list",
        {"workspaces": [{"workspace_id": "w1", "label": a._spaces["control"].label}]},
    )
    assert a.list_panes() == []          # benign empty for the degraded workspace
    assert a._workspace_id == "w1"       # NOT cleared: space retained as DEGRADED
    assert a._spaces["control"].state == herdr_mod.WS_DEGRADED


def test_list_panes_degraded_escapes_to_gone_and_recreates(
    server: FakeHerdrServer, tmp_path
) -> None:
    # DEGRADED bounded escape (§4.2): once consecutive misses exceed the threshold and
    # workspace.list confirms the workspace is truly gone, the space is dropped (GONE) so
    # a later spawn lazily recreates a fresh one — no eager recreate on the first blip.
    _wire_spawn(server)
    a = _adapter(server)
    a.spawn(["claude"], cwd=str(tmp_path))
    server.on(
        "pane.list",
        {"error": {"code": "workspace_not_found", "message": "gone"}},
    )
    server.on("workspace.list", {"workspaces": []})  # truly gone
    for _ in range(HerdrAdapter.degraded_max_misses):
        assert a.list_panes() == []
    assert a._workspace_id is None       # GONE -> space dropped -> recreatable
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
    assert ref.window_id == "w2"          # a fresh workspace was created
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
    # pretend a workspace was bound (seed the owned-set directly; _workspace_id is now
    # a read-only accessor over the space map, Issue #110 §4.1).
    a._spaces[herdr_mod.SPACE_CONTROL] = herdr_mod._Space(
        space_key=herdr_mod.SPACE_CONTROL,
        workspace_id="w1",
        tab_id="w1:t1",
        label="l",
    )
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


# ---------------------------------------------------------------------------
# workspace layout (Issue #110): multi-space placement, generation labels,
# owned-set closure, ephemeral project-space sweep, startup stale sweep.
# ---------------------------------------------------------------------------


def _wire_multi(server: FakeHerdrServer) -> dict:
    """Stateful multi-space wiring: ``workspace.create`` hands out w1,w2,... and
    ``agent.start`` lands the pane directly in the requested workspace (strategy A
    respected in the fake -> no move needed), recording each pane in its own space.
    """
    state = {"ws": 0, "pane": 1}

    def create(_params: dict) -> dict:
        state["ws"] += 1
        wid = f"w{state['ws']}"
        return {
            "workspace": {"workspace_id": wid, "active_tab_id": f"{wid}:t1"},
            "root_pane": {"pane_id": f"{wid}:p0"},
        }

    def start(params: dict) -> dict:
        state["pane"] += 1
        wid = params.get("workspace")
        return {
            "agent": {
                "pane_id": f"{wid}:p{state['pane']}",
                "workspace_id": wid,
                "name": params.get("name"),
                "terminal_id": f"t{state['pane']}",
            }
        }

    server.on("workspace.create", create)
    server.on("agent.start", start)
    server.on("pane.close", {"type": "ok"})
    return state


def test_supports_space_layout_flag_is_true() -> None:
    # Herdr declares the workspace-layout capability; flat backends do not (broker
    # reads it via getattr and only passes SpaceDescriptor when True, §6.2).
    assert HerdrAdapter.supports_space_layout is True
    from claude_org_runtime.terminal.tmux import TmuxAdapter
    from claude_org_runtime.terminal.wezterm import WezTermAdapter

    assert getattr(TmuxAdapter, "supports_space_layout", False) is False
    assert getattr(WezTermAdapter, "supports_space_layout", False) is False


def test_spawn_no_space_defaults_to_control(server: FakeHerdrServer, tmp_path) -> None:
    # A spawn without a SpaceDescriptor collapses into the control space (back-compat
    # single-workspace behaviour): one workspace labelled .../control.
    _wire_multi(server)
    a = HerdrAdapter(socket_path=server.path, timeout=2.0,
                     org_instance_id="oid", generation=3)
    a.spawn(["claude"], cwd=str(tmp_path))
    label = server.params_for("workspace.create")["label"]
    assert label == "claude-org/oid/g3/control"


def test_spawn_routes_control_and_project_to_distinct_workspaces(
    server: FakeHerdrServer, tmp_path
) -> None:
    _wire_multi(server)
    a = _adapter(server)
    ref_c = a.spawn(["claude"], cwd=str(tmp_path), space=SpaceDescriptor(SPACE_CONTROL))
    ref_p = a.spawn(["claude"], cwd=str(tmp_path), space=SpaceDescriptor("project:x"))
    # distinct workspaces for control vs project
    assert ref_c.window_id != ref_p.window_id
    creates = [
        r["params"]["label"] for r in server.requests
        if r["method"] == "workspace.create"
    ]
    assert any(lbl.endswith("/control") for lbl in creates)
    assert any(lbl.endswith("/project:x") for lbl in creates)
    # both panes live in the owned set (2 spaces)
    assert set(a._spaces) == {SPACE_CONTROL, "project:x"}


def test_workspace_label_encodes_generation_and_space_key(
    server: FakeHerdrServer, tmp_path
) -> None:
    _wire_multi(server)
    a = HerdrAdapter(socket_path=server.path, timeout=2.0,
                     org_instance_id="abc123", generation=7)
    a.spawn(["claude"], cwd=str(tmp_path), space=SpaceDescriptor("project:transport-lab"))
    label = server.params_for("workspace.create")["label"]
    assert label == "claude-org/abc123/g7/project:transport-lab"


def test_unassigned_space_key_used_for_projectless_worker(
    server: FakeHerdrServer, tmp_path
) -> None:
    _wire_multi(server)
    a = HerdrAdapter(socket_path=server.path, timeout=2.0,
                     org_instance_id="o", generation=1)
    a.spawn(["claude"], cwd=str(tmp_path), space=SpaceDescriptor(SPACE_UNASSIGNED))
    assert server.params_for("workspace.create")["label"].endswith(f"/{SPACE_UNASSIGNED}")


def test_list_panes_unions_owned_workspaces(
    server: FakeHerdrServer, tmp_path
) -> None:
    # list_panes queries each owned workspace and unions, filtering by registry.
    _wire_multi(server)
    a = _adapter(server)
    ref_c = a.spawn(["claude"], cwd=str(tmp_path), space=SpaceDescriptor(SPACE_CONTROL))
    ref_p = a.spawn(["claude"], cwd=str(tmp_path), space=SpaceDescriptor("project:x"))

    def plist(params: dict) -> dict:
        wid = params.get("workspace_id")
        return {"panes": [
            {"pane_id": ref_c.pane_id if wid == ref_c.window_id else ref_p.pane_id,
             "workspace_id": wid, "tab_id": f"{wid}:t1"},
        ]}

    server.on("pane.list", plist)
    server.on("pane.layout", {"layout": {"panes": []}})
    ids = sorted(p["pane_id"] for p in a.list_panes())
    assert ids == sorted([ref_c.pane_id, ref_p.pane_id])


def test_degraded_one_workspace_does_not_empty_others(
    server: FakeHerdrServer, tmp_path
) -> None:
    # A single workspace's workspace_not_found must NOT drop the other workspace's
    # panes (§4.2: set-wise degraded isolation).
    _wire_multi(server)
    a = _adapter(server)
    ref_c = a.spawn(["claude"], cwd=str(tmp_path), space=SpaceDescriptor(SPACE_CONTROL))
    ref_p = a.spawn(["claude"], cwd=str(tmp_path), space=SpaceDescriptor("project:x"))

    def plist(params: dict) -> dict:
        wid = params.get("workspace_id")
        if wid == ref_p.window_id:
            return {"error": {"code": "workspace_not_found", "message": "gone"}}
        return {"panes": [{"pane_id": ref_c.pane_id, "workspace_id": wid,
                           "tab_id": f"{wid}:t1"}]}

    server.on("pane.list", plist)
    server.on("pane.layout", {"layout": {"panes": []}})
    server.on("workspace.list", {"workspaces": [
        {"workspace_id": ref_c.window_id, "label": "x"},
        {"workspace_id": ref_p.window_id, "label": "y"},
    ]})
    ids = [p["pane_id"] for p in a.list_panes()]
    assert ids == [ref_c.pane_id]                    # control pane survives
    assert a._spaces["project:x"].state == herdr_mod.WS_DEGRADED  # project degraded


def test_close_workspace_all_closes_every_owned(
    server: FakeHerdrServer, tmp_path
) -> None:
    _wire_multi(server)
    a = _adapter(server)
    a.spawn(["claude"], cwd=str(tmp_path), space=SpaceDescriptor(SPACE_CONTROL))
    a.spawn(["claude"], cwd=str(tmp_path), space=SpaceDescriptor("project:x"))
    server.on("workspace.close", {"type": "ok"})
    assert a.close_workspace() is True
    closes = sorted(
        r["params"]["workspace_id"] for r in server.requests
        if r["method"] == "workspace.close"
    )
    assert closes == ["w1", "w2"]
    assert a._spaces == {}


def test_project_space_swept_on_last_pane_close_control_preserved(
    server: FakeHerdrServer, tmp_path
) -> None:
    _wire_multi(server)
    a = _adapter(server)
    ref_c = a.spawn(["claude"], cwd=str(tmp_path), space=SpaceDescriptor(SPACE_CONTROL))
    ref_p = a.spawn(["claude"], cwd=str(tmp_path), space=SpaceDescriptor("project:x"))
    # closing the sole project pane escalates to workspace.close of the project ws;
    # the control ws is untouched (control is org-lifetime, §4.3).
    server.on("pane.close", {"error": {"code": "single_pane", "message": "last"}})

    def plist(params: dict) -> dict:
        wid = params.get("workspace_id")
        pid = ref_p.pane_id if wid == ref_p.window_id else ref_c.pane_id
        return {"panes": [{"pane_id": pid, "workspace_id": wid, "tab_id": f"{wid}:t1"}]}

    server.on("pane.list", plist)
    server.on("workspace.close", {"type": "ok"})
    a.kill_pane(ref_p.pane_id)
    closes = [
        r["params"]["workspace_id"] for r in server.requests
        if r["method"] == "workspace.close"
    ]
    assert ref_p.window_id in closes         # project workspace swept
    assert ref_c.window_id not in closes     # control preserved
    assert "project:x" not in a._spaces
    assert SPACE_CONTROL in a._spaces


def test_reconcile_moves_diverged_project_pane_into_its_own_tab_not_foreign(
    server: FakeHerdrServer, tmp_path
) -> None:
    # agent.start rides along into the focused *foreign* workspace w9; reconcile must
    # pane.move it into the project space's own tab and NEVER close the foreign ws
    # (self-ownership gate, §7.3 BLOCKER invariant).
    state = {"ws": 0}

    def create(_params: dict) -> dict:
        state["ws"] += 1
        wid = f"w{state['ws']}"
        return {"workspace": {"workspace_id": wid, "active_tab_id": f"{wid}:t1"},
                "root_pane": {"pane_id": f"{wid}:p0"}}

    server.on("workspace.create", create)
    server.on(
        "agent.start",
        lambda p: {"agent": {"pane_id": "w9:p5", "workspace_id": "w9",
                             "terminal_id": "tX"}},
    )
    server.on(
        "pane.move",
        lambda p: {"move_result": {"pane": {
            "pane_id": p["destination"]["tab_id"].split(":")[0] + ":p5",
            "terminal_id": "tX",
        }}},
    )
    server.on("pane.close", {"type": "ok"})
    a = _adapter(server)
    ref = a.spawn(["claude"], cwd=str(tmp_path), space=SpaceDescriptor("project:x"))
    assert ref.window_id == "w1"             # project space is the first-created ws
    assert ref.pane_id == "w1:p5"            # moved into the project tab
    mv = server.params_for("pane.move")
    assert mv["destination"]["tab_id"] == "w1:t1"
    # foreign w9 is never adopted into the owned set and never closed
    assert "workspace.close" not in server.methods_called()
    assert not any(s.workspace_id == "w9" for s in a._spaces.values())
    assert a._owned_panes["w1:p5"].workspace_id == "w1"


def test_startup_sweep_closes_old_generation_only(
    server: FakeHerdrServer, tmp_path
) -> None:
    # boot sweep closes gen < current for our own org, leaves foreign orgs / humans
    # untouched, and closes empty current-gen remnants (§5.3).
    oid = "abc"
    server.on("workspace.list", {"workspaces": [
        {"workspace_id": "w_old", "label": f"claude-org/{oid}/g1/control"},
        {"workspace_id": "w_cur_empty", "label": f"claude-org/{oid}/g2/project:x"},
        {"workspace_id": "w_other", "label": "claude-org/OTHERORG/g1/control"},
        {"workspace_id": "w_human", "label": "my-terminal"},
    ]})
    server.on("pane.list", {"panes": []})       # current-gen remnant has no live pane
    server.on("workspace.close", {"type": "ok"})
    # generation=2 explicit (skips bump); state_dir present -> sweep runs at construction.
    HerdrAdapter(socket_path=server.path, timeout=2.0,
                 org_instance_id=oid, generation=2, state_dir=str(tmp_path))
    closes = [
        r["params"]["workspace_id"] for r in server.requests
        if r["method"] == "workspace.close"
    ]
    assert "w_old" in closes            # old generation swept
    assert "w_cur_empty" in closes      # empty current-gen remnant swept
    assert "w_other" not in closes      # foreign org untouched (isolation)
    assert "w_human" not in closes      # unrelated human workspace untouched


def test_startup_sweep_adopts_live_current_generation(
    server: FakeHerdrServer, tmp_path
) -> None:
    # A current-gen label WITH live panes is adopted into the owned set (crash mid-spawn
    # recovery), not closed (§5.3 step 4).
    oid = "def"
    server.on("workspace.list", {"workspaces": [
        {"workspace_id": "w_live", "active_tab_id": "w_live:t1",
         "label": f"claude-org/{oid}/g5/control"},
    ]})
    server.on("pane.list", {"panes": [{"pane_id": "w_live:p2", "workspace_id": "w_live"}]})
    server.on("workspace.close", {"type": "ok"})
    a = HerdrAdapter(socket_path=server.path, timeout=2.0,
                     org_instance_id=oid, generation=5, state_dir=str(tmp_path))
    assert "workspace.close" not in server.methods_called()   # adopted, not closed
    assert a._spaces[SPACE_CONTROL].workspace_id == "w_live"


def test_generation_bumps_monotonically_per_boot(tmp_path) -> None:
    # Each adapter construction with a state_dir bumps the persisted generation counter
    # (write-ahead), so a restarted daemon gets a strictly newer generation (§5.2).
    sd = str(tmp_path)
    g1 = herdr_mod._bump_generation(sd)
    g2 = herdr_mod._bump_generation(sd)
    assert g2 == g1 + 1


def test_org_instance_id_is_stable_across_reads(tmp_path) -> None:
    sd = str(tmp_path)
    a = herdr_mod._read_or_create_org_instance_id(sd)
    b = herdr_mod._read_or_create_org_instance_id(sd)
    assert a == b and len(a) >= 32   # >=128-bit hex, collision-resistant (§5.2)


def test_failed_project_sweep_retries_via_pending_and_respawn_creates_fresh(
    server: FakeHerdrServer, tmp_path
) -> None:
    # Regression (adversarial review MAJOR, §4.3): when a project space's sweep
    # workspace.close FAILS, the space is removed from the owned set and retained in
    # _pending_sweep (keyed by workspace_id); a respawn to the same slug creates a
    # FRESH workspace (no SWEPT-entry overwrite orphan); the failed workspace is
    # retried and reclaimed within the generation.
    _wire_multi(server)
    a = _adapter(server)
    a.space_sweep_grace_seconds = 0.0   # sweep immediately when empty
    a.spawn(["claude"], cwd=str(tmp_path), space=SpaceDescriptor(SPACE_CONTROL))   # w1
    ref_p = a.spawn(["claude"], cwd=str(tmp_path), space=SpaceDescriptor("project:x"))  # w2
    # close the sole project pane; pane.close succeeds; the empty-space sweep verifies the
    # workspace is physically empty (pane.list -> []) then its workspace.close fails.
    server.on("pane.close", {"type": "ok"})
    server.on("pane.list", {"panes": []})   # physically empty -> sweep proceeds to close
    server.on("workspace.close", {"error": {"code": "boom", "message": "x"}})
    a.kill_pane(ref_p.pane_id)
    # removed from owned set (not left as a SWEPT-in-_spaces entry) and retained in
    # pending by workspace_id for retry.
    assert "project:x" not in a._spaces
    assert ref_p.window_id in a._pending_sweep
    # a respawn to the SAME slug creates a fresh workspace (no overwrite orphan).
    ref_p2 = a.spawn(["claude"], cwd=str(tmp_path), space=SpaceDescriptor("project:x"))  # w3
    assert ref_p2.window_id != ref_p.window_id
    assert a._spaces["project:x"].workspace_id == ref_p2.window_id
    # the old failed workspace is retried and reclaimed within the generation.
    server.on("workspace.close", {"type": "ok"})
    a._retry_pending_sweep()
    assert ref_p.window_id not in a._pending_sweep


def test_reconcile_move_honors_per_space_split_direction(
    server: FakeHerdrServer, tmp_path
) -> None:
    # §8: under strategy C the operative placement is pane.move, so the per-space
    # split_direction must reach pane.move (agent.start's split is ignored by Herdr).
    state = {"ws": 0}

    def create(_p: dict) -> dict:
        state["ws"] += 1
        wid = f"w{state['ws']}"
        return {"workspace": {"workspace_id": wid, "active_tab_id": f"{wid}:t1"},
                "root_pane": {"pane_id": f"{wid}:p0"}}

    server.on("workspace.create", create)
    server.on("agent.start",
              lambda p: {"agent": {"pane_id": "w9:p5", "workspace_id": "w9"}})
    server.on("pane.move",
              lambda p: {"move_result": {"pane": {"pane_id": "w1:p5"}}})
    server.on("pane.close", {"type": "ok"})
    a = _adapter(server)
    a.spawn(["claude"], cwd=str(tmp_path),
            space=SpaceDescriptor(SPACE_CONTROL, split_direction="right"))
    assert server.params_for("pane.move")["destination"]["split"] == "right"


def test_fresh_state_dir_is_created_and_generation_persisted(tmp_path) -> None:
    # Codex P1: the adapter can be constructed before Broker/sidecar create state_dir
    # (cli.py: make_adapter runs before Broker). __post_init__ must create the dir so the
    # write-ahead generation bump does not crash a first-time Herdr daemon.
    sd = os.path.join(str(tmp_path), "does", "not", "exist", "yet")
    a = HerdrAdapter(
        socket_path=os.path.join(tempfile.mkdtemp(prefix="hrdr"), "absent.sock"),
        timeout=0.5, state_dir=sd,  # unreachable socket -> startup sweep skips (caught)
    )
    assert os.path.isdir(sd)
    assert a.generation == 1
    assert os.path.exists(os.path.join(sd, herdr_mod._GENERATION_FILE))


def test_project_space_dropped_immediately_on_last_pane_close_then_respawn_fresh(
    server: FakeHerdrServer, tmp_path
) -> None:
    # Codex P2: closing the last owned pane of a project space must drop the space
    # IMMEDIATELY (bypassing the 8s grace), because Herdr auto-closes the workspace on
    # last-pane exit; a grace-lingering LIVE entry would make a quick respawn reuse a
    # dead workspace. A respawn then creates a fresh workspace.
    _wire_multi(server)
    a = _adapter(server)
    a.spawn(["claude"], cwd=str(tmp_path), space=SpaceDescriptor(SPACE_CONTROL))   # w1
    ref_p = a.spawn(["claude"], cwd=str(tmp_path), space=SpaceDescriptor("project:x"))  # w2
    server.on("pane.close", {"type": "ok"})
    # Herdr auto-closed the workspace when the last pane exited: the sweep's physical-empty
    # probe (pane.list) returns workspace_not_found -> the space is dropped as SWEPT.
    server.on("pane.list", {"error": {"code": "workspace_not_found", "message": "auto"}})
    a.kill_pane(ref_p.pane_id)
    assert "project:x" not in a._spaces          # dropped immediately, no grace linger
    ref_p2 = a.spawn(["claude"], cwd=str(tmp_path), space=SpaceDescriptor("project:x"))  # w3
    assert ref_p2.window_id != ref_p.window_id   # fresh workspace, not the dead one


def test_startup_adopt_registers_panes_so_they_are_visible(
    server: FakeHerdrServer, tmp_path
) -> None:
    # Codex P2: adopting a live current-gen workspace must also register its panes in
    # _owned_panes, else list_panes (registry-primary filter) hides them and the project
    # space is later treated as empty and swept.
    oid = "reg"
    server.on("workspace.list", {"workspaces": [
        {"workspace_id": "w_live", "active_tab_id": "w_live:t1",
         "label": f"claude-org/{oid}/g5/project:x"},
    ]})
    server.on("pane.list", {"panes": [
        {"pane_id": "w_live:p2", "workspace_id": "w_live", "tab_id": "w_live:t1"},
    ]})
    server.on("pane.layout", {"layout": {"panes": []}})
    a = HerdrAdapter(socket_path=server.path, timeout=2.0,
                     org_instance_id=oid, generation=5, state_dir=str(tmp_path))
    assert "w_live:p2" in a._owned_panes                       # registered on adopt
    assert any(p["pane_id"] == "w_live:p2" for p in a.list_panes())  # visible in list


def test_reused_pane_liveness_drops_registry_but_does_not_sweep_workspace(
    server: FakeHerdrServer, tmp_path
) -> None:
    # Codex P2 (re-review): on PANE_LIVE_REUSED the pane_id now belongs to a different
    # process; forgetting our registry entry must NOT sweep/close the workspace (that
    # would kill the reused pane, violating the REUSED hands-off / isolation guard).
    _wire_multi(server)
    a = _adapter(server)
    a.space_sweep_grace_seconds = 0.0
    ref_p = a.spawn(["claude"], cwd=str(tmp_path), space=SpaceDescriptor("project:x"))
    server.on("pane.get", {"pane": {"pane_id": ref_p.pane_id, "terminal_id": "OTHER"}})
    server.on("workspace.close", {"type": "ok"})
    verdict = a.pane_liveness(ref_p.pane_id, terminal_id=ref_p.terminal_id)
    assert verdict == PANE_LIVE_REUSED
    assert "workspace.close" not in server.methods_called()   # NOT swept on REUSED
    assert str(ref_p.pane_id) not in a._owned_panes           # registry entry dropped


def test_gone_pane_liveness_sweeps_empty_project_workspace(
    server: FakeHerdrServer, tmp_path
) -> None:
    # The GONE path (pane truly gone) DOES sweep the now-empty project workspace.
    _wire_multi(server)
    a = _adapter(server)
    a.space_sweep_grace_seconds = 0.0
    ref_p = a.spawn(["claude"], cwd=str(tmp_path), space=SpaceDescriptor("project:x"))
    server.on("pane.get", {"error": {"code": "pane_not_found", "message": "gone"}})
    server.on("pane.list", {"panes": []})   # physically empty -> sweep proceeds to close
    server.on("workspace.close", {"type": "ok"})
    verdict = a.pane_liveness(ref_p.pane_id, terminal_id=ref_p.terminal_id)
    assert verdict == PANE_LIVE_GONE
    assert "workspace.close" in server.methods_called()       # swept on GONE
    assert "project:x" not in a._spaces


def test_pending_sweep_retried_on_poll_even_with_no_owned_spaces(
    server: FakeHerdrServer, tmp_path
) -> None:
    # Codex P2 (re-review): a single-project session whose only space became empty and
    # whose workspace.close failed (now only in _pending_sweep, _spaces empty) must still
    # be retried on a normal poll — the empty-query early return must not skip the retry.
    _wire_multi(server)
    a = _adapter(server)
    a.space_sweep_grace_seconds = 0.0
    ref_p = a.spawn(["claude"], cwd=str(tmp_path), space=SpaceDescriptor("project:x"))  # w1
    server.on("pane.close", {"type": "ok"})
    server.on("pane.list", {"panes": []})   # physically empty -> sweep proceeds to close
    server.on("workspace.close", {"error": {"code": "boom", "message": "x"}})
    a.kill_pane(ref_p.pane_id)          # sweep fails -> pending; _spaces now empty
    assert a._spaces == {}
    assert ref_p.window_id in a._pending_sweep
    # a subsequent poll (list_panes) with no owned spaces/panes must retry & reclaim it.
    server.on("workspace.close", {"type": "ok"})
    assert a.list_panes() == []
    assert ref_p.window_id not in a._pending_sweep    # reclaimed within the generation


def test_sweep_does_not_close_workspace_with_non_owned_pane(
    server: FakeHerdrServer, tmp_path
) -> None:
    # Codex P1: _sweep_space verifies physical emptiness before workspace.close. If a
    # non-owned/reused pane occupies the workspace, it must NOT close it (that would kill
    # the pane) — it relinquishes the space instead.
    _wire_multi(server)
    a = _adapter(server)
    a.space_sweep_grace_seconds = 0.0
    ref_p = a.spawn(["claude"], cwd=str(tmp_path), space=SpaceDescriptor("project:x"))  # w1
    # our pane goes GONE (registry entry dropped) but a foreign pane occupies the workspace.
    server.on("pane.get", {"error": {"code": "pane_not_found", "message": "gone"}})
    server.on("pane.list", {"panes": [{"pane_id": "w1:pX", "workspace_id": "w1"}]})
    server.on("workspace.close", {"type": "ok"})
    a.pane_liveness(ref_p.pane_id, terminal_id=ref_p.terminal_id)  # GONE -> sweep attempt
    assert "workspace.close" not in server.methods_called()   # NOT closed (foreign pane)
    assert "project:x" not in a._spaces                       # relinquished, not swept


def test_ensure_space_recreates_when_cached_workspace_auto_closed(
    server: FakeHerdrServer, tmp_path
) -> None:
    # Codex P2: a worker self-exits outside kill_pane; Herdr auto-closes the workspace but
    # the cached _Space stays LIVE (sweep hasn't run yet). A respawn must verify existence
    # and recreate a fresh workspace instead of reusing the dead one.
    _wire_multi(server)
    a = _adapter(server)
    ref_p = a.spawn(["claude"], cwd=str(tmp_path), space=SpaceDescriptor("project:x"))  # w1
    server.on("workspace.list", {"workspaces": []})   # w1 no longer exists (auto-closed)
    ref_p2 = a.spawn(["claude"], cwd=str(tmp_path), space=SpaceDescriptor("project:x"))  # w2
    assert ref_p2.window_id != ref_p.window_id
    assert a._spaces["project:x"].workspace_id == ref_p2.window_id
