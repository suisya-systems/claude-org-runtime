# -*- coding: utf-8 -*-
"""broker org-start の bootstrap folder-trust 機械承認の検証ハーネス。

調査主導タスク broker-bootstrap-folder-trust-approve の成果物。Refs ja#566 / ja#515。
SoT: docs/broker-bootstrap-folder-trust-approval.md。

**確定した契約 (verify)**: `ORG_TRANSPORT=broker` 下では、spawn された Claude pane の
初回 folder-trust プロンプトを抑止する flag/settings は存在せず (Claude Code 公式 +
anthropics/claude-code#29285)、機械承認の唯一の手段は `send_keys(enter=true)` =
adapter の CR 送出である。bootstrap 段のうち **dispatcher / worker** は呼び出し元
agent (secretary / dispatcher) が `send_keys(target=<name>, enter=true)` で承認する
(ja org-start Block D-1 / spawn-flow 3-3b)。runtime はこの承認プリミティブと、
名前で addressable な spawn 後 pane を提供する責務を持つ。

本ファイルは runtime が所有するその契約を固定する:
1. spawn_claude_pane で起こした pane が **安定名で send_keys(enter=true) 承認できる**
   (= Block D-1 / 3-3b が依拠する spawn->approve シーム)。
2. 承認 Enter は **対象 pane だけ**に届く (他 pane を巻き込まない)。
3. broker は spawn 時に folder-trust を **auto-clear しない** (意図的。spawn 直後の
   blind Enter は表示前取りこぼし + agent 側承認との二重 Enter = 空 turn 暴発を招く
   ため、承認は「画面に出てから 1 回」= agent 駆動に委ねる設計)。

adapter 層の CR 等価 (tmux=`send-keys Enter` / wezterm=`send-text --no-paste -- \r`)
は tests/terminal/test_tmux.py::test_send_enter /
tests/terminal/test_wezterm.py::test_send_enter_is_raw_cr で別途固定済み。本ファイルは
その上に立つ broker surface の spawn->machine-approve 契約を検証する。
"""

from __future__ import annotations

import json

from claude_org_runtime.broker.server import Broker
from claude_org_runtime.broker.surface import dispatch_tool

from .conftest import FakeAdapter


def _broker_with_caller(tmp_path, role):
    """ops-tier caller (secretary / dispatcher) + その caller pane を持つ broker を作る。

    secretary は dispatcher を、dispatcher は worker を spawn->approve する。
    どちらも pane 操作 tier なので role を差し替えて両段を同型に検証できる。
    返り値: (broker, adapter, caller_bind)。
    """
    adapter = FakeAdapter()
    adapter.add_pane(active=True)                       # caller pane (focused)
    b = Broker(state_dir=tmp_path / "broker", adapter=adapter)
    tok = b.issue_token(role, role, role, auth_role=role)
    b.register_local(tok)
    return b, adapter, b.get_bind(tok)


def _text(out):
    return json.loads(out["content"][0]["text"])


def _enter_count(adapter: FakeAdapter, handle: int) -> int:
    """FakeAdapter.send_enter は対象 pane の screen に "\\n" を 1 つ足す。

    その個数 = その pane に届いた Enter 回数 (= 機械承認の打鍵数)。
    """
    return adapter.get_text(handle).count("\n")


def _spawn(b, caller, name, role="worker", cwd="/repo"):
    """spawn_claude_pane を発火し、(結果 dict, adapter handle) を返す。"""
    out = dispatch_tool(b, caller, "spawn_claude_pane", {
        "direction": "vertical", "name": name, "role": role, "cwd": cwd,
    })
    res = _text(out)
    handle = b.adapter.spawned[-1]["handle"]
    return res, handle


# ===========================================================================
# 1. spawn 後 pane は安定名で send_keys(enter=true) 承認できる (Block D-1 / 3-3b シーム)
# ===========================================================================

def test_spawned_pane_is_machine_approvable_by_name_enter(tmp_path):
    """secretary が dispatcher を spawn し、安定名で folder-trust を Enter 承認する。

    これが ja org-start Block D-1 の機械承認シーム。runtime はこの spawn->approve を
    成立させる責務を持つ (folder-trust 抑止 flag は存在しないため Enter が唯一手段)。
    """
    b, adapter, secretary = _broker_with_caller(tmp_path, "secretary")

    _, disp_handle = _spawn(b, secretary, "dispatcher", role="dispatcher")
    # spawn 直後は未承認 (Enter は届いていない)。
    assert _enter_count(adapter, disp_handle) == 0

    # 機械承認: 安定名で Enter (= Block D-1 の send_keys(target="dispatcher", enter=true))。
    out = dispatch_tool(b, secretary, "send_keys",
                        {"target": "dispatcher", "enter": True})
    assert _text(out)["ok"] is True
    assert _enter_count(adapter, disp_handle) == 1      # 1 回だけ承認 Enter が届いた


def test_dispatcher_approves_worker_same_seam(tmp_path):
    """段3: dispatcher が worker を spawn し 3-3b で Enter 承認する (段2 と同型)。"""
    b, adapter, dispatcher = _broker_with_caller(tmp_path, "dispatcher")

    _, w_handle = _spawn(b, dispatcher, "worker-foo", role="worker")
    out = dispatch_tool(b, dispatcher, "send_keys",
                        {"target": "worker-foo", "enter": True})
    assert _text(out)["ok"] is True
    assert _enter_count(adapter, w_handle) == 1


# ===========================================================================
# 2. 承認 Enter は対象 pane だけに届く (巻き込み無し)
# ===========================================================================

def test_machine_approval_targets_only_named_pane(tmp_path):
    """2 つ spawn し片方だけ承認 -> その pane だけに Enter。

    他の boot 中 pane に Enter を巻き込むと空 turn 暴発になるため、target 解決が
    名前で厳密であることを固定する。
    """
    b, adapter, secretary = _broker_with_caller(tmp_path, "secretary")

    _, disp_handle = _spawn(b, secretary, "dispatcher", role="dispatcher")
    _, w_handle = _spawn(b, secretary, "worker-foo", role="worker")

    dispatch_tool(b, secretary, "send_keys", {"target": "worker-foo", "enter": True})

    assert _enter_count(adapter, w_handle) == 1         # 対象は承認された
    assert _enter_count(adapter, disp_handle) == 0      # 非対象は無傷


# ===========================================================================
# 3. broker は spawn 時に folder-trust を auto-clear しない (意図的設計)
# ===========================================================================

def test_broker_spawn_does_not_auto_clear_trust(tmp_path):
    """spawn_claude_pane は spawn 後に Enter を送らない (blind auto-clear しない)。

    spawn 直後の blind Enter は (a) folder-trust 表示前で取りこぼす、(b) agent 側の
    Block D-1 / 3-3b 承認と二重 Enter になり空 turn を暴発させる、ため意図的に
    未実装。承認は「画面に出てから 1 回」= agent 駆動に委ねる。将来 broker 内
    auto-approve を入れる変更はこのテストを破り、二重 Enter リスクの再評価を強制する。
    """
    b, adapter, secretary = _broker_with_caller(tmp_path, "secretary")

    _, disp_handle = _spawn(b, secretary, "dispatcher", role="dispatcher")
    # send_keys を呼ばない限り Enter は 1 つも届かない。
    assert _enter_count(adapter, disp_handle) == 0
    # adapter にも spawn 以外の打鍵 (send_enter 由来の screen 改変) は無い。
    assert adapter.get_text(disp_handle) == ""
