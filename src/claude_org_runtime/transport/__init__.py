# -*- coding: utf-8 -*-
"""Transport surface descriptor — the single SoT that maps a transport
``flag`` (``renga`` | ``broker``) to its concrete wiring:

- the MCP **server name** (``renga-peers`` / ``org-broker``),
- the **spawn injection flag** the launcher passes to a child pane, and
- the **role tier -> exposed MCP tool-name set**.

設計 SoT: docs/design/ja-migration-plan.md §5.2(i) / §5.3 / §3.1 / §4。

Why a descriptor (§5.2 (i) の単一 SoT 要請): ja の renga ツール参照は複数の
生成器 (runtime の ``settings/generator`` と ja 側の
``tools/gen_delegate_payload.py`` / worker_brief テンプレート) が別々に同じ
transport prefix / tool set を必要とする。各所にハードコードすると drift する
ため、**flag -> {server, 注入 flag, ロール別 tool 集合} を返す加算的 runtime
API を 1 つ置き、双方の生成器がこれを読む**。ja は pin consume する。

非破壊の絶対条件 (§5.3): 既定 (``ORG_TRANSPORT`` 無設定) = ``renga`` で現行と
bit 等価。broker は ``ORG_TRANSPORT=broker`` を明示した時のみ有効化される。

drift 防止 (§5.2): broker の tier 別 tool 集合は
:mod:`claude_org_runtime.broker.surface` の ``tools_for(auth_role)`` /
``capped_auth_role`` を一次参照して導出する (ハードコード二重管理を避ける)。
"""

from __future__ import annotations

from .descriptor import (
    DEFAULT_TRANSPORT,
    ENV_KEY,
    RENGA_REQUIRED_TOOLS,
    TRANSPORTS,
    TransportSurface,
    allow_entries_for_role,
    get_surface,
    resolve_transport,
    tools_for_role,
)

__all__ = [
    "DEFAULT_TRANSPORT",
    "ENV_KEY",
    "RENGA_REQUIRED_TOOLS",
    "TRANSPORTS",
    "TransportSurface",
    "allow_entries_for_role",
    "get_surface",
    "resolve_transport",
    "tools_for_role",
]
