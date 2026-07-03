#!/usr/bin/env python3
"""Issue #114 修正の end-to-end 検証 (/verify 相当の実行ビヘイビア観測)。

再現ハーネス (herdr_repro.py) が raw RPC で「修正前の事故」を再現するのに対し、本
スクリプトは **実 HerdrAdapter (Fix-C placement + Fix-D liveness 込み)** を隔離 herdr
サーバに対して駆動し、``--with-user-ws`` シナリオ (フォーカス済みユーザ workspace +
ユーザ pane) で:

  1. dispatcher が専用 workspace に着地する (Fix-C の pane.move で移送)。ユーザの
     workspace ではない。
  2. adapter.list_panes() が dispatcher を返す (workspace_not_found -> 恒常空 の
     誤 reap 前提が消える)。
  3. adapter.pane_liveness(pane_id, terminal_id) == 'alive' (Fix-D の権威 liveness)。
  4. isolation 維持: adapter は専用 workspace の pane のみ見え、ユーザ pane は
     list_panes に現れない。close_workspace はユーザ workspace/pane に触れない。
  5. terminal_id が PaneRef に載り、pane_id 再利用時に REUSED を検出する。

ユーザの live herdr (default socket) には一切触れない (socket path は argv で受ける)。
本スクリプトは PYTHONPATH=src で実 adapter を import する。
"""
from __future__ import annotations

import json
import os
import socket
import sys

from claude_org_runtime.terminal.herdr import HerdrAdapter
from claude_org_runtime.terminal.base import (
    PANE_LIVE_ALIVE,
    PANE_LIVE_GONE,
    PANE_LIVE_REUSED,
)

_id = [0]


def rt(sock_path: str, method: str, params: dict) -> dict:
    """raw RPC (ユーザ workspace のセットアップ / 検証用。adapter とは別経路)。"""
    _id[0] += 1
    payload = (json.dumps({"id": f"v{_id[0]}", "method": method, "params": params}) + "\n").encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(10.0)
        s.connect(sock_path)
        s.sendall(payload)
        buf = b""
        while b"\n" not in buf:
            data = s.recv(65536)
            if not data:
                break
            buf += data
    return json.loads(buf.split(b"\n", 1)[0].decode("utf-8", "replace"))


def ws_ids(sock_path: str) -> list[str]:
    return [w.get("workspace_id") for w in rt(sock_path, "workspace.list", {}).get("result", {}).get("workspaces", [])]


def pane_alive_raw(sock_path: str, pane_id: str) -> bool:
    """raw pane.get でユーザ pane の生存を独立確認する (adapter 非経由)。"""
    r = rt(sock_path, "pane.get", {"pane_id": pane_id})
    return "error" not in r


PASS, FAIL = [], []


def check(cond: bool, label: str) -> None:
    (PASS if cond else FAIL).append(label)
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")


def main() -> int:
    sock_path = sys.argv[1]
    print(f"[verify] socket = {sock_path}")
    print(f"[verify] adapter class = {HerdrAdapter.__module__}.{HerdrAdapter.__qualname__}")

    # --- ユーザの startup workspace (focused) + ユーザ pane を用意 ---
    uw = rt(sock_path, "workspace.create", {"label": "user-startup", "cwd": os.path.expanduser("~")})
    user_ws = uw["result"]["workspace"]["workspace_id"]
    user_pane = uw["result"]["root_pane"]["pane_id"]
    rt(sock_path, "workspace.focus", {"workspace_id": user_ws})
    print(f"[verify] user workspace={user_ws!r} (focused), user pane={user_pane!r}")

    # --- 実 adapter を駆動して dispatcher を spawn ---
    adapter = HerdrAdapter(socket_path=sock_path, timeout=10.0)
    ref = adapter.spawn(["bash", "-lc", "echo DISPATCHER_ALIVE; sleep 600"], cwd=os.getcwd())
    print(f"[verify] spawn -> pane_id={ref.pane_id!r} window(ws)={ref.window_id!r} terminal_id={ref.terminal_id!r}")
    ded_ws = adapter._workspace_id
    print(f"[verify] adapter dedicated workspace = {ded_ws!r}")

    # 1. placement: dispatcher は専用 workspace に居る (ユーザ ws ではない)
    landed = ref.pane_id.split(":")[0] if isinstance(ref.pane_id, str) and ":" in ref.pane_id else ref.pane_id
    check(landed == ded_ws, f"dispatcher landed in dedicated ws ({landed} == {ded_ws})")
    check(landed != user_ws, f"dispatcher NOT in user ws ({landed} != {user_ws})")

    # 2. list_panes が dispatcher を返す (恒常空 -> 誤 reap 前提が消える)
    panes = adapter.list_panes()
    pane_ids = [p["pane_id"] for p in panes]
    print(f"[verify] adapter.list_panes() = {pane_ids}")
    check(ref.pane_id in pane_ids, f"list_panes shows dispatcher ({ref.pane_id})")

    # 3. isolation: ユーザ pane は adapter.list_panes に現れない
    check(user_pane not in pane_ids, f"list_panes does NOT leak user pane ({user_pane})")

    # 4. Fix-D 権威 liveness: dispatcher は ALIVE
    v = adapter.pane_liveness(ref.pane_id, ref.terminal_id)
    check(v == PANE_LIVE_ALIVE, f"pane_liveness(dispatcher) == alive (got {v!r})")

    # 5. Fix-D id 再利用ガード: terminal_id が違えば REUSED
    v_reuse = adapter.pane_liveness(ref.pane_id, "term_SOMEONE_ELSE")
    check(v_reuse == PANE_LIVE_REUSED, f"pane_liveness(reused terminal_id) == reused (got {v_reuse!r})")

    # 6. Fix-D GONE: 存在しない pane は gone
    v_gone = adapter.pane_liveness("w404:p404", "term_x")
    check(v_gone == PANE_LIVE_GONE, f"pane_liveness(absent) == gone (got {v_gone!r})")

    # 7. isolation: ユーザ workspace / pane は spawn 儀式で無傷
    check(user_ws in ws_ids(sock_path), f"user workspace {user_ws} still present after spawn")
    check(pane_alive_raw(sock_path, user_pane), f"user pane {user_pane} still alive after spawn (raw pane.get)")

    # 8. isolation: close_workspace は専用 workspace のみ閉じ、ユーザ ws は残す
    ok = adapter.close_workspace()
    print(f"[verify] adapter.close_workspace() -> {ok}")
    after = ws_ids(sock_path)
    check(user_ws in after, f"close_workspace left user ws {user_ws} intact (workspaces now {after})")
    check(pane_alive_raw(sock_path, user_pane), f"user pane {user_pane} still alive after close_workspace")
    check(ded_ws not in after, f"close_workspace removed dedicated ws {ded_ws}")

    print()
    print(f"[verify] RESULT: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print(f"[verify]   FAILED: {f}")
        return 1
    print("[verify] *** ALL CHECKS PASSED: Issue #114 修正後、--with-user-ws で事故らない ***")
    return 0


if __name__ == "__main__":
    sys.exit(main())
