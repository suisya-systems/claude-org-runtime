# -*- coding: utf-8 -*-
"""MCP surface definitions for the org-broker (the "what tools exist / how
they are routed" layer).

設計 SoT: docs/design/renga-decoupling.md §4.2 (worker / curator 向け最小
MCP surface)。canonical 実装: claude-org-transport-lab spike/broker.py
(Phase 4/5 で確定した MCP surface + allowlist guard) の faithful port。

This module is a stateless leaf: it holds the protocol constants and the
tool catalogue, and routes ``tools/call`` to the stateful broker. It owns no
locks and no queues; :func:`dispatch_tool` receives the :class:`Broker`
(via :mod:`claude_org_runtime.broker.server`) and the caller's
:class:`~claude_org_runtime.broker.tokens.AgentBind` and performs the
verified allowlist dispatch.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 循環 import 回避 (server -> surface -> server を型のみで切る)
    from .server import Broker
    from .tokens import AgentBind

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "org-broker", "version": "0.1.0"}

# worker / curator 向け最小 MCP surface (設計書 §4.2)。allowlist guard は
# dispatch_tool の分岐そのもの: ここに無い tool は isError で弾く。
TOOLS = [
    {
        "name": "send_message",
        "description": "Send a message to another agent via the broker queue.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to_id": {"type": "string", "description": "Recipient agent id or name."},
                "message": {"type": "string", "description": "Text to deliver."},
            },
            "required": ["to_id", "message"],
        },
    },
    {
        "name": "check_messages",
        "description": "Drain queued messages addressed to this agent (at-most-once).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_peers",
        "description": "List registered agents visible to this agent.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_summary",
        "description": "Set a short summary of what this agent is working on.",
        "inputSchema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    },
]


class ToolArgError(ValueError):
    """tools/call の引数不正 (JSON-RPC -32602 invalid params に変換される)。"""


def dispatch_tool(broker: "Broker", bind: "AgentBind", name: str, args: dict) -> dict:
    """ツール実行 (allowlist 分岐)。引数不正は ToolArgError (handler 側で -32602 に変換)。

    元 ``Broker.call_tool`` の本体。faithful port のため list_peers / set_summary は
    broker 内部状態 (``_binds`` / ``_lock``) を直接読む。lock 内では I/O / journal を
    呼ばない (server 側の DELETE デッドロック回避契約と整合)。
    """
    if name == "send_message":
        to_id, message = args.get("to_id"), args.get("message")
        if not isinstance(to_id, str) or not isinstance(message, str):
            raise ToolArgError("send_message requires string to_id and message")
        result = broker.enqueue(bind, to_id, message)
    elif name == "check_messages":
        result = {"messages": broker.drain(bind)}
    elif name == "list_peers":
        with broker._lock:
            result = {
                "peers": [
                    {
                        "id": b.agent_id,
                        "name": b.name,
                        "role": b.role,
                        "summary": b.summary,
                    }
                    for b in broker._binds.values()
                    if b.registered and not b.revoked
                ]
            }
    elif name == "set_summary":
        summary = args.get("summary")
        if not isinstance(summary, str):
            raise ToolArgError("set_summary requires string summary")
        with broker._lock:
            bind.summary = summary
        result = {"ok": True}
    else:
        return {
            "content": [{"type": "text", "text": f"[unknown_tool] {name}"}],
            "isError": True,
        }
    return {
        "content": [
            {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
        ]
    }
