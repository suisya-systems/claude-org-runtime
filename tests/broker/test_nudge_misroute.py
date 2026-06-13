# -*- coding: utf-8 -*-
"""Regression: broker nudge mis-route silent-drop under renga<->broker 併存 (#76).

無人の自動委譲サイクルが renga<->broker 併存 (ambient renga-peers が居る) 条件下で
完走しない潜在 defect の回帰ガード。原因は 2 つ、いずれか単独でも解消するが防御
多重で両方を直す:

  (a) NUDGE_TEXT が bare 名 'check_messages' を直書きしていた。ambient renga-peers
      も同名 'check_messages' を公開するため nudge が renga-peers 側へ誤ルートし、
      broker queue が drain されず silent drop する。-> FQ 名 'mcp__org-broker__
      check_messages' にして宛先 server を一意化する。
  (b) build_claude_argv が --strict-mcp-config を注入しなかった。spawn された pane が
      ambient .mcp.json (renga-peers) も load するため、bare 'check_messages' が
      ambiguous になる。-> --strict-mcp-config を常に注入し、broker MCP のみ load
      させて誤ルート自体を構造的に断つ。

**テストの限界 (誠実な明記)**: 実 Claude のツール解決を unit test では走らせられない。
本テストは併存下の「bare 名は ≥2 server が同名 'check_messages' を公開するため broker
へ届く保証が無い = silent drop」という *ambiguity* を決定的にモデル化し、解消後に
broker queue が実際に drain される (real store の行状態) ことで fail-before/pass-after
を証明する。誰がツールを勝ち取るかは仮定せず、「一意に broker へ解決できるか」だけを
判定基準にする (検証不能な特定挙動を焼き込まない)。
"""

from __future__ import annotations

import json
import re

import pytest

from claude_org_runtime.broker.surface import build_claude_argv
from claude_org_runtime.broker.store import UNDELIVERED
from claude_org_runtime.terminal import NUDGE_TEXT

# server 名に '_' は使わない (org-broker / renga-peers)。FQ 区切り '__' との衝突回避。
_TOOL_RE = re.compile(r"(?:mcp__([A-Za-z0-9-]+)__)?check_messages")


def _bind(broker, agent_id, role="worker"):
    token = broker.issue_token(agent_id, agent_id, role)
    broker.register_local(token)
    return broker.get_bind(token)


def _pending(broker, agent_id):
    """まだ drain されていない (UNDELIVERED) 行 = 受信側に未到達のメッセージ。"""
    with broker._lock:
        return [
            r for r in broker._rows.values()
            if r.to_id == agent_id and r.state == UNDELIVERED
        ]


def _resolve_candidates(nudge_text, loaded_servers):
    """nudge prose のツール参照を、load 済み server 上の候補集合に解決する。

    - FQ 参照 (mcp__<server>__check_messages) -> その server 1 つに一意解決。
    - bare 参照 (check_messages) -> 'check_messages' を公開する load 済み server
      すべてが候補 (併存下では複数になりうる = ambiguous)。
    """
    m = _TOOL_RE.search(nudge_text)
    assert m, f"no check_messages reference in {nudge_text!r}"
    server = m.group(1)
    if server is not None:  # FQ: 宛先 server が一意
        return {(server, "check_messages")}
    return {
        (s, "check_messages")
        for s, tools in loaded_servers.items()
        if "check_messages" in tools
    }


def _deliver_nudge(broker, recipient_bind, nudge_text, loaded_servers):
    """nudge を受けた pane の check_messages 実行をモデル化する。

    候補が **broker ただ 1 つ** に一意解決できる時だけ broker queue を drain する
    (= 受信が確実に broker へ届く)。ambiguous (bare 名 + 併存) の時は broker へ届く
    保証が無く、行は UNDELIVERED のまま残る (#76 の silent drop)。
    """
    candidates = _resolve_candidates(nudge_text, loaded_servers)
    if candidates == {("org-broker", "check_messages")}:
        return broker.drain(recipient_bind)
    return []  # silent drop: broker queue は触られない


def _loaded_servers(argv, mcp_config_servers, ambient_servers):
    """spawn argv から、pane が実際に load する MCP server 集合を導く。

    --strict-mcp-config があれば --mcp-config の server のみ。無ければ ambient な
    .mcp.json 由来 (renga-peers 等) も併せて load される。
    """
    servers = dict(mcp_config_servers)
    if "--strict-mcp-config" not in argv:
        servers.update(ambient_servers)
    return servers


# --------------------------------------------------------------------------- (a)
def test_fq_nudge_drains_broker_queue_under_coexistence(broker):
    """FQ 化 (a): 併存 (renga-peers + org-broker 双方 load) でも nudge が broker へ
    一意解決し queue を drain する。fail-before: bare NUDGE_TEXT は ambiguous で
    silent drop -> queue が残る。pass-after: FQ NUDGE_TEXT は一意 -> drain 成立。"""
    sender = _bind(broker, "sender")
    recip = _bind(broker, "worker-a")
    broker.enqueue(sender, "worker-a", "hello")
    assert len(_pending(broker, "worker-a")) == 1  # 投入済み・未到達

    # 併存: spawn された pane は ambient renga-peers と broker の双方を load しており、
    # どちらも bare 'check_messages' を公開している。
    loaded = {
        "renga-peers": {"check_messages", "send_message"},
        "org-broker": {"check_messages", "send_message"},
    }
    drained = _deliver_nudge(broker, recip, NUDGE_TEXT, loaded)

    assert [m["message"] for m in drained] == ["hello"]
    assert _pending(broker, "worker-a") == []  # silent drop していない


def test_bare_nudge_under_coexistence_would_silently_drop(broker):
    """誤ルート再現の対照: bare 名 nudge は併存下で ambiguous -> drain されず行が残る
    (= #76 の silent drop)。FQ 化がこれを解消することの裏付け。"""
    sender = _bind(broker, "sender")
    recip = _bind(broker, "worker-a2")
    broker.enqueue(sender, "worker-a2", "hello")

    loaded = {
        "renga-peers": {"check_messages"},
        "org-broker": {"check_messages"},
    }
    bare = "📨 新着あり。check_messages を実行"  # 修正前の NUDGE_TEXT 形
    drained = _deliver_nudge(broker, recip, bare, loaded)

    assert drained == []                              # broker へ届かない
    assert len(_pending(broker, "worker-a2")) == 1    # 行は未到達のまま (silent drop)


def test_nudge_text_uses_fully_qualified_broker_tool():
    """構造ガード (a): NUDGE_TEXT は FQ ツール名を直書きする (SoT 5.2)。"""
    assert "mcp__org-broker__check_messages" in NUDGE_TEXT


# --------------------------------------------------------------------------- (b)
def test_strict_mcp_config_isolates_broker_pane(broker):
    """strict 注入 (b): bare 名 nudge でも、--strict-mcp-config で pane が broker MCP
    のみ load するため ambient renga-peers が除外され、check_messages が broker へ一意
    解決する。fail-before: strict 無し -> renga-peers も load -> ambiguous -> silent
    drop。pass-after: strict 有り -> broker のみ -> drain 成立。"""
    sender = _bind(broker, "sender")
    recip = _bind(broker, "worker-b")
    broker.enqueue(sender, "worker-b", "ping")

    # broker spawn 経路が組む pane argv (push 一次配送の channel 枝)。
    argv = build_claude_argv(
        mcp_config_json=json.dumps(broker.mcp_config_for(broker.issue_token(
            "probe-b", "probe-b", "worker"))),
        channel_server="org-broker-channel",
    )
    # ambient .mcp.json は renga-peers を、--mcp-config は org-broker を寄与する。
    ambient = {"renga-peers": {"check_messages"}}
    config_servers = {"org-broker": {"check_messages"}}
    loaded = _loaded_servers(argv, config_servers, ambient)

    assert set(loaded) == {"org-broker"}  # strict -> renga-peers は load されない

    # bare 名 nudge (worst case) でも broker へ一意解決する。
    bare = "📨 新着あり。check_messages を実行"
    drained = _deliver_nudge(broker, recip, bare, loaded)

    assert [m["message"] for m in drained] == ["ping"]
    assert _pending(broker, "worker-b") == []


def test_build_claude_argv_injects_strict_mcp_config():
    """構造ガード (b): build_claude_argv は --strict-mcp-config を常に 1 本注入する。"""
    assert build_claude_argv(mcp_config_json="{}").count("--strict-mcp-config") == 1
    # channel (broker) 枝でも 1 本。
    withch = build_claude_argv(mcp_config_json="{}", channel_server="org-broker-channel")
    assert withch.count("--strict-mcp-config") == 1


def test_build_claude_argv_accepts_idempotent_caller_strict_flag():
    """caller の --strict-mcp-config 付与は reject されず (reserved から外した)、
    重複は畳まれて 1 本に正規化される。"""
    argv = build_claude_argv(mcp_config_json="{}", extra_args=["--strict-mcp-config"])
    assert argv.count("--strict-mcp-config") == 1


def test_build_claude_argv_keeps_strict_token_in_value_position():
    """dedup は **standalone** の --strict-mcp-config のみ畳み、value-flag の値位置に
    現れた同名トークンは arity を壊さず保持する (Codex Minor 対応)。"""
    argv = build_claude_argv(
        mcp_config_json="{}", extra_args=["--add-dir", "--strict-mcp-config"],
    )
    # 注入分 (standalone) + --add-dir の値として 1 つ = 計 2。
    assert argv.count("--strict-mcp-config") == 2
    # value-flag と値の隣接が保たれている (arity 不変)。
    assert argv[argv.index("--add-dir") + 1] == "--strict-mcp-config"


def test_no_strict_would_load_ambient_renga_and_misroute():
    """誤ルート再現の対照 (b): strict 注入が無い argv だと ambient renga-peers も
    load され、bare 名が ambiguous になる (= 修正前の経路)。"""
    argv_without_strict = ["claude", "--mcp-config", "{}"]  # 修正前の build 出力相当
    loaded = _loaded_servers(
        argv_without_strict,
        {"org-broker": {"check_messages"}},
        {"renga-peers": {"check_messages"}},
    )
    assert set(loaded) == {"org-broker", "renga-peers"}
    candidates = _resolve_candidates("📨 新着あり。check_messages を実行", loaded)
    assert candidates != {("org-broker", "check_messages")}  # ambiguous -> 誤ルート
