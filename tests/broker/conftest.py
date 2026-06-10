# -*- coding: utf-8 -*-
"""Shared fixtures for broker tests.

A real :class:`~claude_org_runtime.broker.server.Broker` is started on an
ephemeral localhost port with ``adapter=None`` (nudge disabled — no terminal
backend is touched), and a tiny stdlib MCP-over-HTTP client drives the
JSON-RPC surface. This mirrors the verified ``spike/mcp_smoke_test.py``
harness: protocol behaviour is exercised without spawning a real Claude.
"""

from __future__ import annotations

import itertools
import json
import urllib.error
import urllib.request

import pytest

from claude_org_runtime.broker.server import Broker
from claude_org_runtime.terminal import PaneRef


class FakeAdapter:
    """In-memory TerminalAdapter for pane-op tests (no real backend).

    Mirrors the tmux-style native ``list_panes`` schema the broker normalizes
    (``pane_id`` / ``left`` / ``top`` / ``width`` / ``height`` / ``active`` /
    ``cursor_x`` / ``cursor_y``). ``spawn`` records the built argv so tests can
    assert what the broker's structured builders emitted.
    """

    def __init__(self) -> None:
        self._panes: dict[int, dict] = {}
        self._screens: dict[int, str] = {}
        self.spawned: list[dict] = []
        self.killed: list[int] = []
        self._counter = itertools.count(1)

    # bootstrap a pre-existing pane (e.g. the caller pane) ------------------
    def add_pane(self, active: bool = False, **geom) -> int:
        handle = next(self._counter)
        rec = {
            "pane_id": handle, "active": active, "left": 0, "top": 0,
            "width": 80, "height": 24, "cursor_x": 0, "cursor_y": 0,
        }
        rec.update(geom)
        self._panes[handle] = rec
        self._screens[handle] = ""
        return handle

    def set_focused(self, handle: int) -> None:
        for h, p in self._panes.items():
            p["active"] = (h == handle)

    # TerminalAdapter Protocol --------------------------------------------
    def spawn(self, argv, cwd=None, new_window=True) -> PaneRef:
        handle = self.add_pane()
        self.spawned.append({"argv": list(argv), "cwd": cwd, "handle": handle})
        return PaneRef(pane_id=handle)

    def list_panes(self) -> list[dict]:
        return [dict(p) for p in self._panes.values()]

    def pane_exists(self, pane_id) -> bool:
        return pane_id in self._panes

    def get_text(self, pane_id, escapes: bool = False) -> str:
        return self._screens.get(pane_id, "")

    def type_text(self, pane_id, text) -> None:
        self._screens[pane_id] = self._screens.get(pane_id, "") + text

    def send_enter(self, pane_id) -> None:
        self._screens[pane_id] = self._screens.get(pane_id, "") + "\n"

    def send_line(self, pane_id, text, settle: float = 0.0) -> None:
        self.type_text(pane_id, text)
        self.send_enter(pane_id)

    def send_interrupt(self, pane_id) -> None:
        self._screens[pane_id] = self._screens.get(pane_id, "") + "<C-c>"

    def kill_pane(self, pane_id) -> None:
        self._panes.pop(pane_id, None)
        self._screens.pop(pane_id, None)
        self.killed.append(pane_id)


@pytest.fixture
def fake_adapter():
    return FakeAdapter()


class MiniMcpClient:
    """Minimal MCP streamable-HTTP client (ported from the spike smoke test)."""

    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self.token = token
        self.session_id: str | None = None
        self._id = 0

    def _post(self, payload: dict | None, expect_status: int = 200,
              method: str = "POST"):
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {self.token}",
                **({"Mcp-Session-Id": self.session_id} if self.session_id else {}),
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status = resp.status
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self.session_id = sid
                body = resp.read()
        except urllib.error.HTTPError as e:
            status = e.code
            body = e.read()
        assert status == expect_status, f"status {status} != {expect_status}: {body!r}"
        return json.loads(body) if body else None

    def rpc(self, method: str, params: dict | None = None, expect_status: int = 200):
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            payload["params"] = params
        return self._post(payload, expect_status)

    def notify(self, method: str):
        self._post({"jsonrpc": "2.0", "method": method}, expect_status=202)

    def delete(self, expect_status: int = 200):
        self._post(None, expect_status=expect_status, method="DELETE")

    def call_tool(self, name: str, args: dict | None = None) -> dict:
        res = self.rpc("tools/call", {"name": name, "arguments": args or {}})
        assert "result" in res, res
        return json.loads(res["result"]["content"][0]["text"])


@pytest.fixture
def broker(tmp_path):
    """A started broker on an ephemeral port (adapter=None -> nudge disabled)."""
    b = Broker(state_dir=tmp_path / "broker", adapter=None, port=0)
    b.start()
    try:
        yield b
    finally:
        b.stop()


@pytest.fixture
def client_factory(broker):
    """Factory that issues a token and returns a connected MiniMcpClient."""

    def make(agent_id: str, name: str | None = None, role: str = "worker",
             initialize: bool = True) -> MiniMcpClient:
        token = broker.issue_token(agent_id, name or agent_id, role)
        c = MiniMcpClient(broker.url, token)
        if initialize:
            c.rpc("initialize", {"protocolVersion": "2025-06-18"})
            c.notify("notifications/initialized")
        return c

    return make
