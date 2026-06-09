# -*- coding: utf-8 -*-
"""token ↔ agent/pane bind の保持 (broker の認証/登録状態)。

設計 SoT: docs/design/renga-decoupling.md §4.4 (per-agent token + static
headers)。canonical 実装: claude-org-transport-lab spike/broker.py の
faithful port。

:class:`AgentBind` は broker のみが保持する bind レコード。:class:`TokenMixin`
は bind 表 (``_binds``) を操作するメソッド群で、:class:`~claude_org_runtime.
broker.server.Broker` に mix-in される。``_binds`` / ``_lock`` の実体は
``Broker.__init__`` が確立する (この mixin の前提契約)。
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..terminal import PaneId

if TYPE_CHECKING:  # mixin が前提とする Broker の共有状態 (型のみ)
    pass


@dataclass
class AgentBind:
    """token ↔ agent/pane の bind (設計書 §4.4)。broker のみが保持する。"""

    token: str
    agent_id: str
    name: str
    role: str
    pane_id: PaneId | None = None     # backend ネイティブ型 (WezTerm=int / tmux="%N"=str)
    registered: bool = False          # MCP initialize 到達で True (AC-2-3 の検知点)
    registered_at: float | None = None
    session_id: str | None = None
    summary: str = ""
    revoked: bool = False


class TokenMixin:
    """bind 表 (token 発行 / pane bind / 登録検知 / mcp-config 生成)。

    共有状態の前提 (Broker.__init__ が確立):
    - ``self._lock``: binds / queues を一括ガードする単一 Lock。
    - ``self._binds``: ``token -> AgentBind``。
    - ``self._queues``: ``agent_id -> list[dict]`` (issue 時に setdefault する)。
    """

    # 型注釈のみ (実体は Broker.__init__)。mixin の自己文書化。
    _lock: threading.Lock
    _binds: dict[str, AgentBind]
    _queues: dict[str, list[dict]]

    def issue_token(
        self, agent_id: str, name: str, role: str, pane_id: PaneId | None = None
    ) -> str:
        """spawn 時の per-agent token 発行 (設計書 §4.4)。"""
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._binds[token] = AgentBind(
                token=token, agent_id=agent_id, name=name, role=role, pane_id=pane_id
            )
            self._queues.setdefault(agent_id, [])
        self._journal("token_issued", agent_id=agent_id, role=role, pane_id=pane_id)
        return token

    def bind_pane(self, token: str, pane_id: PaneId) -> None:
        with self._lock:
            self._binds[token].pane_id = pane_id

    def register_local(self, token: str) -> None:
        """MCP を経由しない server-side 合成エージェント (検証用 observer 等) を
        登録済みにする。実エージェントの登録は initialize 到達でのみ行う。"""
        with self._lock:
            bind = self._binds[token]
            bind.registered = True
            bind.registered_at = time.time()

    def mcp_config_for(self, token: str, server_name: str = "org-broker") -> dict:
        """--mcp-config に渡す JSON。token は static headers に埋める (確定事項 (2))。

        env 参照 (${VAR}) は config parse 時の失敗リスクがあるため使わない。
        """
        return {
            "mcpServers": {
                server_name: {
                    "type": "http",
                    "url": self.url,
                    "headers": {"Authorization": f"Bearer {token}"},
                }
            }
        }

    def get_bind(self, token: str) -> AgentBind | None:
        with self._lock:
            bind = self._binds.get(token)
            if bind and not bind.revoked:
                return bind
            return None

    def find_registered(self, agent_id: str) -> AgentBind | None:
        """list_peers 相当の登録検知 (AC-2-3)。bind 表ベース。"""
        with self._lock:
            for b in self._binds.values():
                if b.agent_id == agent_id and b.registered and not b.revoked:
                    return b
        return None

    if TYPE_CHECKING:  # 他 mixin / server が供給するメンバ (型チェッカ向け宣言)
        url: str

        def _journal(self, event: str, **fields: object) -> None: ...
