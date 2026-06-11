# -*- coding: utf-8 -*-
"""Transport surface descriptor implementation.

A :class:`TransportSurface` answers three questions for one transport flag:

1. ``server`` / ``fq_prefix`` — which MCP server backs the transport and the
   fully-qualified tool prefix (``mcp__renga-peers__`` / ``mcp__org-broker__``).
2. :meth:`TransportSurface.spawn_inject` — the flag the launcher injects when
   it spawns a child pane wired to this transport.
3. :meth:`TransportSurface.tools_for_role` /
   :meth:`TransportSurface.allow_entries_for_role` — the role's exposed MCP
   tool-name set (bare names) and the corresponding ``mcp__<server>__<tool>``
   allowlist entries.

renga (§3.1): server ``renga-peers``, injection
``--dangerously-load-development-channels server:renga-peers``, **全ロール同一
surface = required 14 面** (``tools/check_renga_compat.py`` の
``REQUIRED_MCP_TOOLS`` / renga 0.18.0 と一致)。renga には構造的 tier gating が
無いため、transport surface はロールに依らず一様 (schema の per-role narrowing は
descriptor の transport surface ではなく defense-in-depth の subset)。

broker (§4.2 / §5.3): server ``org-broker``, injection ``--mcp-config
<broker>``, **role tier 別**。tier 別集合は
:mod:`claude_org_runtime.broker.surface` の ``tools_for`` から導出する
(ハードコード二重管理を避ける = drift 防止, §5.2)。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from ..broker import surface as _broker_surface

# ---------------------------------------------------------------------------
# flag resolution
# ---------------------------------------------------------------------------

#: 環境変数キー (§5.1: 初期は env のみ。永続ファイル化は Set C 改訂を伴うため別 Issue)。
ENV_KEY = "ORG_TRANSPORT"

#: 既定 transport。**無設定時は renga = 現行挙動不変 (非破壊)**。完全移行後の
#: broker 反転は dogfood ゲート (§8 Issue G) 通過後の人間判断であり、runtime の
#: 既定値はここでは反転させない (§5.1)。
DEFAULT_TRANSPORT = "renga"

#: 受理する transport flag (org 全体で 1 値, §5.1)。
TRANSPORTS = ("renga", "broker")

# ---------------------------------------------------------------------------
# renga required surface (SoT mirror)
# ---------------------------------------------------------------------------
#
# tools/check_renga_compat.py の REQUIRED_MCP_TOOLS (renga 0.18.0, ちょうど 14)
# と一致する順序付き正本。順序は ja の user_common ``required_allow``
# (= 全ロールが継承する共有 surface) と bit 一致させ、ja が descriptor から
# 再生成しても現行 settings と byte 同一になるようにする (§5.3 bit 等価)。
RENGA_REQUIRED_TOOLS = (
    "set_summary",
    "list_peers",
    "send_message",
    "check_messages",
    "list_panes",
    "spawn_pane",
    "close_pane",
    "focus_pane",
    "new_tab",
    "inspect_pane",
    "poll_events",
    "send_keys",
    "spawn_claude_pane",
    "set_pane_identity",
)

# ---------------------------------------------------------------------------
# role -> broker auth tier
# ---------------------------------------------------------------------------
#
# broker は token の ``auth_role`` で公開面を構造的に絞る (§4.2)。runtime の
# role 名を broker tier にマップする。
# - secretary -> secretary (全面 + generic spawn_pane)
# - dispatcher -> dispatcher (messaging + pane 操作, generic spawn_pane を除く)
# - curator / worker -> worker (messaging 4 のみ。pane 操作を一切呼ばない, §5.6)
# - user_common (全ロール共有の ~/.claude/settings.json) -> worker
#   broker では上位 tier の pane 操作は各ロール自身の settings ファイルが担うため、
#   共有ファイルの broker baseline は messaging 4 に収める (renga の共有 14 とは
#   flag 依存で異なる。これは flag-aware 化が生む正当な差分であり bit 等価の対象外)。
_ROLE_TO_BROKER_TIER: dict[str, str] = {
    "secretary": "secretary",
    "dispatcher": "dispatcher",
    "curator": "worker",
    "worker": "worker",
    "user_common": "worker",
}

# broker tool catalogue の正準順 (surface.TOOLS の宣言順)。tier 集合をこの順で
# 並べて出力安定性を担保する。
_BROKER_TOOL_ORDER: tuple[str, ...] = tuple(t["name"] for t in _broker_surface.TOOLS)


def _broker_tools_for_tier(tier: str) -> tuple[str, ...]:
    """broker tier が到達できる bare tool 名集合 (catalogue 順)。

    ``broker.surface.tools_for`` を一次参照して導出する (二重管理回避, §5.2)。
    """
    names = {t["name"] for t in _broker_surface.tools_for(tier)}
    return tuple(n for n in _BROKER_TOOL_ORDER if n in names)


# ---------------------------------------------------------------------------
# surface
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TransportSurface:
    """Resolved wiring for one transport flag (immutable / hashable)."""

    flag: str
    server: str
    #: 注入 flag のテンプレート。``{broker_mcp_config}`` を含む場合は
    #: :meth:`spawn_inject` で具体化する。
    _inject_template: str

    @property
    def fq_prefix(self) -> str:
        """Fully-qualified MCP tool prefix, e.g. ``mcp__renga-peers__``."""
        return f"mcp__{self.server}__"

    def spawn_inject(self, *, broker_mcp_config: str | None = None) -> str:
        """子ペイン spawn 時にランチャが注入する flag 文字列を返す。

        renga は固定 (``--dangerously-load-development-channels
        server:renga-peers``)。broker は ``--mcp-config <broker>`` で、
        ``broker_mcp_config`` (token 入り MCP 設定 JSON / パス) を埋める。
        broker で ``broker_mcp_config`` を渡さない場合はテンプレートの
        ``<broker>`` プレースホルダを残したまま返す (prose 表示用)。
        """
        if "{broker_mcp_config}" not in self._inject_template:
            return self._inject_template
        value = broker_mcp_config if broker_mcp_config is not None else "<broker>"
        return self._inject_template.format(broker_mcp_config=value)

    def tools_for_role(self, role: str) -> tuple[str, ...]:
        """role が公開される **bare** MCP tool 名集合を返す。

        renga: 全ロール一様の required 14。broker: role -> tier マップ経由で
        ``broker.surface.tools_for`` から導出 (drift 防止)。未知 role は broker
        では messaging-only (worker tier) にフォールバックする
        (``capped_auth_role`` と同じ最小権限既定)。
        """
        if self.flag == "renga":
            return RENGA_REQUIRED_TOOLS
        tier = _ROLE_TO_BROKER_TIER.get(role, "worker")
        return _broker_tools_for_tier(tier)

    def allow_entries_for_role(self, role: str) -> tuple[str, ...]:
        """role の ``mcp__<server>__<tool>`` allowlist エントリを返す。

        :meth:`tools_for_role` の bare 名に :attr:`fq_prefix` を被せたもの。
        ``settings/generator`` と ja 側生成器が公開 tool 集合を出すための主面。
        """
        prefix = self.fq_prefix
        return tuple(f"{prefix}{name}" for name in self.tools_for_role(role))


_SURFACES: dict[str, TransportSurface] = {
    "renga": TransportSurface(
        flag="renga",
        server="renga-peers",
        _inject_template=(
            "--dangerously-load-development-channels server:renga-peers"
        ),
    ),
    "broker": TransportSurface(
        flag="broker",
        server="org-broker",
        _inject_template="--mcp-config {broker_mcp_config}",
    ),
}


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def resolve_transport(
    explicit: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """有効な transport flag を決定する。

    優先順: 明示引数 > ``ORG_TRANSPORT`` env > 既定 (``renga``)。空文字列・
    未知値は ``ValueError``。``env=None`` は ``os.environ`` を読む。
    """
    if explicit is not None:
        candidate = explicit
    else:
        source = os.environ if env is None else env
        candidate = source.get(ENV_KEY) or DEFAULT_TRANSPORT
    candidate = candidate.strip()
    if candidate not in _SURFACES:
        raise ValueError(
            f"unknown transport flag: {candidate!r}. valid: {list(TRANSPORTS)} "
            f"(set via {ENV_KEY} env; default {DEFAULT_TRANSPORT!r})"
        )
    return candidate


def get_surface(
    flag: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> TransportSurface:
    """transport flag の :class:`TransportSurface` を返す。

    ``flag=None`` のとき :func:`resolve_transport` (env -> 既定 renga) で解決。
    """
    resolved = resolve_transport(flag, env=env)
    return _SURFACES[resolved]


def tools_for_role(
    role: str,
    *,
    flag: str | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """role の bare MCP tool 名集合 (transport は flag / env / 既定で解決)。"""
    return get_surface(flag, env=env).tools_for_role(role)


def allow_entries_for_role(
    role: str,
    *,
    flag: str | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """role の ``mcp__<server>__<tool>`` allowlist エントリ (transport 解決込み)。"""
    return get_surface(flag, env=env).allow_entries_for_role(role)
