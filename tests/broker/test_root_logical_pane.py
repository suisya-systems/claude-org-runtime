# -*- coding: utf-8 -*-
"""startup root の論理ペイン ``manual-test`` が secretary の登録後に stray で残らない。

serve は起動時に root token を ``manual-test`` で発行して論理ペインに載せる (#57
anchor)。org up は続けて secretary を admin mint_token(name="secretary") で発行する
ので、list_panes に ``manual-test`` (role=worker, 閉じられない) が余計に残っていた。
secretary mint 時に secretary 自身を論理ペインに載せ替え、root の stray を外す。
"""

from __future__ import annotations

import json

from claude_org_runtime.broker.server import Broker
from claude_org_runtime.broker.surface import dispatch_tool


def _text(out):
    return json.loads(out["content"][0]["text"])


def _serve_like_startup(tmp_path, fake_adapter):
    """cli.run と同じ: root token 発行 + 論理ペイン登録 (secretary mint 前)。"""
    b = Broker(state_dir=tmp_path, adapter=fake_adapter)
    root_tok = b.issue_token("manual-test", "manual-test", "worker", auth_role="worker")
    b.register_logical_pane(root_tok)
    return b


def _mint_secretary(b):
    res = b.admin_mint_token({"role": "secretary", "name": "secretary"})
    assert res["ok"] is True, res
    return res["token"]


def test_secretary_mint_replaces_stray_root_pane(tmp_path, fake_adapter):
    b = _serve_like_startup(tmp_path, fake_adapter)
    tok = _mint_secretary(b)
    sec = b.get_bind(tok)
    panes = _text(dispatch_tool(b, sec, "list_panes", {}))["panes"]
    ids = [p["id"] for p in panes]
    assert ids == ["secretary"], ids
    assert panes[0]["role"] == "secretary"
    assert b.resolve_target("secretary") == "secretary"
