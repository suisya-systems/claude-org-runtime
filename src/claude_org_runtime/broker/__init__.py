# -*- coding: utf-8 -*-
"""org-broker subpackage: localhost HTTP MCP server + queue store + nudge delivery.

claude-org-transport-lab の spike/broker.py (Phase 4/5 で確定した MCP surface +
allowlist guard + session 検証) を faithful port し、4 つの責務に分割したもの:

- :mod:`~claude_org_runtime.broker.surface` -- MCP 面 (PROTOCOL_VERSIONS /
  SERVER_INFO / TOOLS / ToolArgError / dispatch_tool)。状態なし leaf。
- :mod:`~claude_org_runtime.broker.tokens` -- AgentBind + TokenMixin
  (per-agent token bind と登録検知)。
- :mod:`~claude_org_runtime.broker.store` -- StoreMixin (queue 永続化 + journal)。
- :mod:`~claude_org_runtime.broker.server` -- Broker orchestrator
  (HTTP lifecycle + nudge 配達 + _McpHandler)。
- :mod:`~claude_org_runtime.broker.placement` -- dispatcher.choose_split 再利用境界。
- :mod:`~claude_org_runtime.broker.cli` -- daemon CLI entry。

依存方向は一方向: broker -> terminal / dispatcher.choose_split。
claude-org-ja は broker を import しない (broker 機能には MCP 経由で到達し、
descriptor を pin consume するのみ。Epic #586 Phase 2 で broker が既定 transport)。
"""

from __future__ import annotations

from .placement import choose_pane_split
from .server import Broker
from .surface import PROTOCOL_VERSIONS, SERVER_INFO, TOOLS, ToolArgError
from .tokens import AgentBind

__all__ = [
    "AgentBind",
    "Broker",
    "PROTOCOL_VERSIONS",
    "SERVER_INFO",
    "TOOLS",
    "ToolArgError",
    "choose_pane_split",
]
