# -*- coding: utf-8 -*-
"""走行中 broker daemon への最小 HTTP クライアント (admin RPC + MCP-over-HTTP)。

制御面 (``org up`` / ``org down`` = :mod:`launcher`) と notify helper
(``broker send`` = :mod:`notify`) が共有する localhost-only の HTTP primitive を
ここに集約する。元は launcher.py に private 実装としてあったものを、二つ目の
consumer (notify) が同じ sidecar 発見 + admin mint + MCP send を再利用できるよう
factor out したもの (Issue #93)。挙動は launcher のものと等価 (= 既存テストの
``launcher._admin_rpc`` / ``launcher._McpClient`` 参照は launcher 側の re-export で
不変に保つ)。

設計方針:
- 接続不可 (daemon 不在 / 停止 / stale sidecar) は :class:`urllib.error.URLError`
  を送出する (= 呼び元が「到達不能」を判定する単一のシグナル)。
- HTTP エラー応答 (401/400/404) は本体を parse して返す (RPC レベルの拒否は
  例外にしない)。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from . import surface

# admin HTTP RPC 1 回あたりの上限。dead port への connect が refuse されず timeout
# まで張り付く環境 (一部 Windows) でも呼び元が無限待ちしないための上限。
ADMIN_RPC_TIMEOUT = 10.0


def _admin_rpc(
    host: str, port: int, admin_token: str, method: str,
    params: dict | None = None, *, timeout: float | None = None,
) -> dict | None:
    """admin HTTP RPC を 1 回叩く。返り値は応答 JSON (本体なしは None)。

    接続不可 (daemon 不在/停止) は :class:`urllib.error.URLError` を送出する
    (= 呼び元が「到達不能 = 要起動 / 未配送」の判定に使う)。HTTP エラー応答
    (401/400/404) は本体を parse して返す (RPC レベルの拒否は例外にしない)。
    """
    if timeout is None:
        timeout = ADMIN_RPC_TIMEOUT
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

    org up の健全性確認 (initialize -> tools/list 往復)、org down の pane 操作
    (list_panes / close_pane)、notify の enqueue (send_message) に使う。conftest の
    MiniMcpClient を src 側に最小移植したもの (テスト harness と同じ JSON-RPC 契約)。
    接続不可は URLError。
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

    def send_message(self, to_id: str, message: str) -> dict:
        """``send_message`` tool を 1 回呼ぶ (notify helper の本体)。

        返り値は broker.enqueue の結果 dict: 配送成功は
        ``{"ok": True, "delivered_to": <agent_id>}``、宛先不在は
        ``{"ok": False, "error": "[peer_not_found] ..."}``。tier 外 / tool エラーは
        :meth:`call_tool` が ``{"raw": ..., "isError": True}`` を返す (どちらも
        ``ok`` が True にならないので呼び元は未配送と判定できる)。接続不可は
        URLError を送出する (呼び元が握る)。
        """
        return self.call_tool("send_message", {"to_id": to_id, "message": message})

    def close(self) -> None:
        """MCP セッションを DELETE で閉じる (best-effort)。

        bind の ``session_id`` を落とし ``registered=False`` にする (server の
        do_DELETE)。使い捨て token を list_peers / 配送先から de-register し、走行中
        daemon に idle な登録を残さないための後始末。初期化前 / 既に閉じている等は
        無視する。
        """
        if self.session_id is None:
            return
        req = urllib.request.Request(
            self.url,
            headers={"Authorization": f"Bearer {self.token}",
                     "Mcp-Session-Id": self.session_id},
            method="DELETE",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout):
                pass
        except Exception:  # noqa: BLE001 - close は純粋な best-effort cleanup。
            # URLError/HTTPError だけでなく read timeout (TimeoutError/OSError) 等も
            # 握る: cleanup の失敗が呼び元の結果 (例: notify の enqueue 成功 exit code)
            # を上書きしてはならない。
            pass
        self.session_id = None
