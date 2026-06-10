# -*- coding: utf-8 -*-
"""Shared fixtures for broker tests.

A real :class:`~claude_org_runtime.broker.server.Broker` is started on an
ephemeral localhost port with ``adapter=None`` (nudge disabled — no terminal
backend is touched), and a tiny stdlib MCP-over-HTTP client drives the
JSON-RPC surface. This mirrors the verified ``spike/mcp_smoke_test.py``
harness: protocol behaviour is exercised without spawning a real Claude.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from claude_org_runtime.broker.server import Broker


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
