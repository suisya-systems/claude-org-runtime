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
    CODE_INVALID_PARAMS,
    CODE_NAME_IN_USE,
    CODE_PANE_NOT_FOUND,
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
