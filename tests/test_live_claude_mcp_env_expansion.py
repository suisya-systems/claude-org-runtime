# -*- coding: utf-8 -*-
"""Live check: Claude Code expands ``${ORG_BROKER_TOKEN}`` in MCP headers (#218).

The broker hands its agent token to org up / adopt / herdr panes through process
env, and the mcp-config only says ``Authorization: Bearer ${ORG_BROKER_TOKEN}``.
That relies on undocumented Claude Code behaviour (measured on 2.1.296, Issue
#218): the placeholder is expanded from the launching process's env, and an
unset variable is sent literally (which the broker answers with 401). If a
release changes either, this test fails instead of org up silently losing its
MCP surface.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from claude_org_runtime.broker.tokens import TOKEN_ENV, TokenMixin

pytestmark = pytest.mark.skipif(
    os.environ.get("CLAUDE_ORG_RUNTIME_LIVE_CLAUDE") != "1" or not shutil.which("claude"),
    reason="live Claude Code check; set CLAUDE_ORG_RUNTIME_LIVE_CLAUDE=1 with an authenticated `claude` on PATH",
)


class _Recorder(BaseHTTPRequestHandler):
    seen: list[str] = []

    def _reject(self) -> None:
        self.seen.append(self.headers.get("Authorization", ""))
        self.send_response(401)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST = do_DELETE = _reject

    def log_message(self, *args) -> None:
        pass


def _authorization_sent(tmp_path: Path, token: str | None) -> list[str]:
    _Recorder.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/mcp"
        cfg = TokenMixin.mcp_config_for(SimpleNamespace(url=url), "unused", token_env=True)
        cfg_path = tmp_path / "mcp.json"
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k != TOKEN_ENV}
        if token is not None:
            env[TOKEN_ENV] = token
        subprocess.run(
            ["claude", "-p", "Reply with the single word OK.", "--strict-mcp-config",
             "--mcp-config", str(cfg_path), "--output-format", "text"],
            cwd=tmp_path, capture_output=True, text=True, timeout=300,
            stdin=subprocess.DEVNULL, env=env,
        )
        return [h for h in _Recorder.seen if h]
    finally:
        server.shutdown()
        server.server_close()


def test_header_placeholder_is_expanded_from_process_env(tmp_path: Path) -> None:
    sent = _authorization_sent(tmp_path, "live-probe-token")
    assert sent, "claude never contacted the MCP server"
    assert set(sent) == {"Bearer live-probe-token"}, sent


def test_unset_placeholder_is_sent_literally(tmp_path: Path) -> None:
    sent = _authorization_sent(tmp_path, None)
    assert sent, "claude never contacted the MCP server"
    assert set(sent) == {f"Bearer ${{{TOKEN_ENV}}}"}, sent
