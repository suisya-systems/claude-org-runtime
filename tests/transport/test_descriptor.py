# -*- coding: utf-8 -*-
"""Tests for the transport surface descriptor (ja-migration-plan §5.2 (i) / §5.3).

Covers:
- descriptor golden for both flags (server / prefix / inject / role->tools),
- drift lock: broker tier sets are derived from ``broker.surface.tools_for``,
- renga required-14 == ``tools/check_renga_compat`` REQUIRED_MCP_TOOLS surface,
- flag resolution (explicit > env > default renga),
- bit-equivalence anchor: renga surface == bundled schema's shared renga 14.
"""

from __future__ import annotations

import json
from importlib.resources import files

import pytest

from claude_org_runtime.broker import surface as broker_surface
from claude_org_runtime.transport import descriptor as td


# ---------------------------------------------------------------------------
# golden snapshots (both flags)
# ---------------------------------------------------------------------------

# renga: 全ロール一様の required 14 (check_renga_compat REQUIRED_MCP_TOOLS と一致)。
_GOLDEN_RENGA_TOOLS = (
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

# broker: tier 別 (catalogue 順)。secretary=13 / dispatcher=12 / worker=4。
_GOLDEN_BROKER_BY_ROLE = {
    "secretary": (
        "send_message",
        "check_messages",
        "list_peers",
        "set_summary",
        "list_panes",
        "inspect_pane",
        "send_keys",
        "poll_events",
        "close_pane",
        "set_pane_identity",
        "spawn_claude_pane",
        "spawn_pane",
        "spawn_codex_pane",
    ),
    "dispatcher": (
        "send_message",
        "check_messages",
        "list_peers",
        "set_summary",
        "list_panes",
        "inspect_pane",
        "send_keys",
        "poll_events",
        "close_pane",
        "set_pane_identity",
        "spawn_claude_pane",
        "spawn_codex_pane",
    ),
    "curator": ("send_message", "check_messages", "list_peers", "set_summary"),
    "worker": ("send_message", "check_messages", "list_peers", "set_summary"),
    "user_common": ("send_message", "check_messages", "list_peers", "set_summary"),
}


def test_renga_surface_golden() -> None:
    s = td.get_surface("renga")
    assert s.flag == "renga"
    assert s.server == "renga-peers"
    assert s.fq_prefix == "mcp__renga-peers__"
    assert (
        s.spawn_inject()
        == "--dangerously-load-development-channels server:renga-peers"
    )
    # 全ロール一様 (renga には構造的 tier gating が無い)。
    for role in ("secretary", "dispatcher", "curator", "worker", "user_common"):
        assert s.tools_for_role(role) == _GOLDEN_RENGA_TOOLS
    assert s.allow_entries_for_role("worker") == tuple(
        f"mcp__renga-peers__{t}" for t in _GOLDEN_RENGA_TOOLS
    )


def test_broker_surface_golden() -> None:
    s = td.get_surface("broker")
    assert s.flag == "broker"
    assert s.server == "org-broker"
    assert s.fq_prefix == "mcp__org-broker__"
    # inject: 未指定はプレースホルダ、指定で具体化。
    assert s.spawn_inject() == "--mcp-config <broker>"
    assert s.spawn_inject(broker_mcp_config="/run/broker.json") == (
        "--mcp-config /run/broker.json"
    )
    for role, expected in _GOLDEN_BROKER_BY_ROLE.items():
        assert s.tools_for_role(role) == expected, role
    assert s.allow_entries_for_role("dispatcher") == tuple(
        f"mcp__org-broker__{t}" for t in _GOLDEN_BROKER_BY_ROLE["dispatcher"]
    )


def test_broker_tier_counts() -> None:
    s = td.get_surface("broker")
    assert len(s.tools_for_role("secretary")) == 13
    assert len(s.tools_for_role("dispatcher")) == 12
    assert len(s.tools_for_role("worker")) == 4
    assert len(s.tools_for_role("curator")) == 4


# ---------------------------------------------------------------------------
# drift lock: broker descriptor derives from broker.surface (no double SoT)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role,tier",
    [
        ("secretary", "secretary"),
        ("dispatcher", "dispatcher"),
        ("curator", "worker"),
        ("worker", "worker"),
    ],
)
def test_broker_tools_track_surface_tools_for(role: str, tier: str) -> None:
    """descriptor の broker tier 集合は surface.tools_for と同集合 (drift 防止)。"""
    descriptor_names = set(td.get_surface("broker").tools_for_role(role))
    surface_names = {t["name"] for t in broker_surface.tools_for(tier)}
    assert descriptor_names == surface_names


def test_unknown_role_falls_back_to_messaging_on_broker() -> None:
    # capped_auth_role と同じ最小権限既定: 未知 role は messaging-only。
    s = td.get_surface("broker")
    assert s.tools_for_role("totally-unknown-role") == (
        "send_message",
        "check_messages",
        "list_peers",
        "set_summary",
    )


# ---------------------------------------------------------------------------
# renga required 14 == check_renga_compat REQUIRED_MCP_TOOLS surface
# ---------------------------------------------------------------------------


def test_renga_required_is_exactly_14() -> None:
    assert len(td.RENGA_REQUIRED_TOOLS) == 14
    assert len(set(td.RENGA_REQUIRED_TOOLS)) == 14


def test_renga_required_matches_known_required_set() -> None:
    # renga 0.18.0 REQUIRED_MCP_TOOLS (tools/check_renga_compat.py の SoT)。
    expected = {
        "send_message",
        "set_summary",
        "check_messages",
        "list_peers",
        "list_panes",
        "inspect_pane",
        "send_keys",
        "poll_events",
        "close_pane",
        "set_pane_identity",
        "spawn_claude_pane",
        "spawn_pane",
        "new_tab",
        "focus_pane",
    }
    assert set(td.RENGA_REQUIRED_TOOLS) == expected


# ---------------------------------------------------------------------------
# flag resolution
# ---------------------------------------------------------------------------


def test_resolve_default_is_renga() -> None:
    assert td.resolve_transport(env={}) == "renga"


def test_resolve_env() -> None:
    assert td.resolve_transport(env={"ORG_TRANSPORT": "broker"}) == "broker"
    assert td.resolve_transport(env={"ORG_TRANSPORT": "renga"}) == "renga"


def test_resolve_empty_env_is_default() -> None:
    # 空文字列は未設定扱い (既定 renga)。
    assert td.resolve_transport(env={"ORG_TRANSPORT": ""}) == "renga"


def test_resolve_explicit_overrides_env() -> None:
    assert (
        td.resolve_transport("renga", env={"ORG_TRANSPORT": "broker"}) == "renga"
    )


def test_resolve_unknown_raises() -> None:
    with pytest.raises(ValueError):
        td.resolve_transport("tmux")
    with pytest.raises(ValueError):
        td.resolve_transport(env={"ORG_TRANSPORT": "zellij"})


def test_get_surface_default_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ORG_TRANSPORT", raising=False)
    assert td.get_surface().flag == "renga"
    monkeypatch.setenv("ORG_TRANSPORT", "broker")
    assert td.get_surface().flag == "broker"


# ---------------------------------------------------------------------------
# bit-equivalence anchor: renga surface == bundled schema's shared renga 14
# ---------------------------------------------------------------------------


def _schema_user_common_renga_tools() -> list[str]:
    resource = files("claude_org_runtime.settings").joinpath(
        "role_configs_schema.json"
    )
    schema = json.loads(resource.read_text(encoding="utf-8"))
    return [
        e
        for e in schema["roles"]["user_common"]["required_allow"]
        if e.startswith("mcp__renga-peers__")
    ]


def test_renga_surface_bit_equivalent_to_schema_user_common() -> None:
    """descriptor の renga allowlist (順序込み) == 現行 schema の共有 renga 14。

    これが「flag=renga で現行と bit 等価 (非破壊)」の anchor。descriptor を
    SoT に切り替えても ja の共有 surface が byte 同一であることを固定する。
    """
    descriptor_entries = list(
        td.get_surface("renga").allow_entries_for_role("user_common")
    )
    assert descriptor_entries == _schema_user_common_renga_tools()
