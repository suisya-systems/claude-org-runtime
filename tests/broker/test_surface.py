# -*- coding: utf-8 -*-
"""Unit tests for the stateless MCP surface (tool catalogue + dispatch)."""

from __future__ import annotations

import json

import pytest

from claude_org_runtime.broker import surface
from claude_org_runtime.broker.server import Broker
from claude_org_runtime.broker.surface import ToolArgError, dispatch_tool


def test_tool_catalogue_is_the_worker_surface():
    names = [t["name"] for t in surface.TOOLS]
    assert names == ["send_message", "check_messages", "list_peers", "set_summary"]


def test_default_protocol_is_first_listed():
    assert surface.PROTOCOL_VERSIONS[0] == "2025-06-18"


def _bind(broker, agent_id, register=True):
    token = broker.issue_token(agent_id, agent_id, "worker")
    if register:
        broker.register_local(token)
    return broker.get_bind(token)


def test_dispatch_send_message_validates_types(tmp_path):
    b = Broker(state_dir=tmp_path, adapter=None)
    src = _bind(b, "src")
    with pytest.raises(ToolArgError):
        dispatch_tool(b, src, "send_message", {"to_id": "x"})  # missing message


def test_dispatch_set_summary_validates_type(tmp_path):
    b = Broker(state_dir=tmp_path, adapter=None)
    src = _bind(b, "src")
    with pytest.raises(ToolArgError):
        dispatch_tool(b, src, "set_summary", {"summary": 123})


def test_dispatch_unknown_tool_is_iserror(tmp_path):
    b = Broker(state_dir=tmp_path, adapter=None)
    src = _bind(b, "src")
    out = dispatch_tool(b, src, "spawn_agent", {})
    assert out["isError"] is True
    assert "[unknown_tool]" in out["content"][0]["text"]


def test_dispatch_list_peers_reflects_summary(tmp_path):
    b = Broker(state_dir=tmp_path, adapter=None)
    src = _bind(b, "src")
    _bind(b, "other")
    dispatch_tool(b, src, "set_summary", {"summary": "hi"})
    out = dispatch_tool(b, src, "list_peers", {})
    payload = json.loads(out["content"][0]["text"])
    by_id = {p["id"]: p for p in payload["peers"]}
    assert by_id["src"]["summary"] == "hi"
    assert set(by_id) == {"src", "other"}
