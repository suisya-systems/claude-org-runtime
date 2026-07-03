#!/usr/bin/env python3
"""Herdr misreap 再現ハーネス (Issue #114)。

完全に隔離した herdr インスタンス (専用 XDG_CONFIG_HOME + 専用 session) に対し、
HerdrAdapter が spawn 時に踏む正確な RPC 列を raw socket で再走し、
「agent.start の split が dedicated workspace ではなくフォーカス中の startup
workspace を分割 -> root pane close で dedicated workspace 消滅 -> pane.list が
workspace_not_found -> 生存 pane が list から永久欠落」という連鎖を実測する。

ユーザーの live herdr (default socket) には一切触れない。socket path は argv で受ける。
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time

_id = [0]


def rt(sock_path: str, method: str, params: dict) -> dict:
    """1 リクエスト 1 往復 (herdr は non-subscription を one-shot で閉じる)。"""
    _id[0] += 1
    payload = (json.dumps({"id": f"r{_id[0]}", "method": method, "params": params}) + "\n").encode()
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


def show(label: str, resp: dict) -> None:
    if "error" in resp:
        print(f"  {label}: ERROR {resp['error']}")
    else:
        print(f"  {label}: ok  {json.dumps(resp.get('result', {}), ensure_ascii=False)[:500]}")


def ws_list(sock_path: str) -> list[dict]:
    return rt(sock_path, "workspace.list", {}).get("result", {}).get("workspaces", [])


def pane_ids(sock_path: str, wid: str):
    pl = rt(sock_path, "pane.list", {"workspace_id": wid})
    if "error" in pl:
        return f"ERROR:{pl['error'].get('code')}"
    return [(p.get("pane_id"), p.get("workspace_id")) for p in pl["result"].get("panes", [])]


def main() -> int:
    sock_path = sys.argv[1]
    focus_fix = "--focus-fix" in sys.argv  # 修正案A: agent.start 前に dedicated ws を focus
    with_user_ws = "--with-user-ws" in sys.argv  # ライブ TUI の startup workspace を模擬
    print(f"[repro] socket = {sock_path}  focus_fix={focus_fix}  with_user_ws={with_user_ws}")

    show("ping", rt(sock_path, "ping", {}))

    startup = ws_list(sock_path)
    print(f"[repro] startup workspaces = {[(w.get('workspace_id'), w.get('focused')) for w in startup]}")

    if with_user_ws:
        # ライブサーバでは TUI クライアント接続時に headless server が startup
        # workspace を 1 つ作りフォーカスする。headless-no-client では作られないため、
        # ここで手動生成して「フォーカス済み既存 workspace」状態を再現する。
        uw = rt(sock_path, "workspace.create", {"label": "user-startup", "cwd": os.path.expanduser("~")})
        user_ws = uw.get("result", {}).get("workspace", {}).get("workspace_id")
        rt(sock_path, "workspace.focus", {"workspace_id": user_ws})
        print(f"[repro] simulated user startup workspace = {user_ws!r} (focused)")

    pid = os.getpid()
    wc = rt(sock_path, "workspace.create", {"label": f"claude-org-{pid}", "cwd": os.getcwd()})
    show("workspace.create (dedicated)", wc)
    wcr = wc.get("result", {})
    ws = wcr.get("workspace", {})
    dedicated_ws = ws.get("workspace_id")
    tab_id = ws.get("active_tab_id")
    root_pane = (wcr.get("root_pane") or {}).get("pane_id")
    print(f"[repro] dedicated_ws={dedicated_ws!r} tab={tab_id!r} root_pane={root_pane!r}")

    print("[repro] workspaces after create:")
    for w in ws_list(sock_path):
        print(f"[repro]   {w.get('workspace_id')} focused={w.get('focused')} panes={w.get('pane_count')}")

    if focus_fix:
        show("workspace.focus(dedicated)", rt(sock_path, "workspace.focus", {"workspace_id": dedicated_ws}))

    ag = rt(sock_path, "agent.start", {
        "name": f"claude-org-{pid}-1",
        "argv": ["bash", "-lc", "echo DISPATCHER_ALIVE; sleep 600"],
        "workspace": dedicated_ws,
        "tab": tab_id,
        "split": "down",
        "cwd": os.getcwd(),
    })
    show("agent.start (dispatcher)", ag)
    agent = ag.get("result", {}).get("agent", {})
    agent_pane = agent.get("pane_id")
    print(f"[repro] *** dispatcher pane_id={agent_pane!r} workspace_id(on agent)={agent.get('workspace_id')!r}")
    landed = agent_pane.split(":")[0] if agent_pane else "?"
    print(f"[repro] *** dedicated_ws={dedicated_ws!r} vs dispatcher landed in {landed!r} "
          f"=> {'DIVERGED (bug)' if landed != dedicated_ws else 'same (ok)'}")

    print("[repro] pane.list per workspace BEFORE root cleanup:")
    for w in ws_list(sock_path):
        print(f"[repro]   ws {w.get('workspace_id')}: {pane_ids(sock_path, w.get('workspace_id'))}")

    print(f"[repro] closing root pane {root_pane!r} (adapter root-cleanup step)...")
    show("pane.close(root)", rt(sock_path, "pane.close", {"pane_id": root_pane}))
    time.sleep(0.5)

    after = [w.get("workspace_id") for w in ws_list(sock_path)]
    print(f"[repro] workspaces AFTER root close = {after}")
    print(f"[repro] *** dedicated_ws {dedicated_ws!r} still present? {dedicated_ws in after}")

    pl = rt(sock_path, "pane.list", {"workspace_id": dedicated_ws})
    show(f"pane.list(dedicated {dedicated_ws})", pl)
    if "error" in pl:
        print(f"[repro] *** pane.list(tracked ws) -> {pl['error'].get('code')} "
              f"=> adapter.list_panes() returns [] => broker never sees live pane => REAP")

    print("[repro] pane.list per surviving workspace AFTER root close:")
    for w in ws_list(sock_path):
        print(f"[repro]   ws {w.get('workspace_id')}: {pane_ids(sock_path, w.get('workspace_id'))}")

    if agent_pane:
        cl = rt(sock_path, "pane.close", {"pane_id": agent_pane})
        show(f"pane.close(dispatcher {agent_pane}) [= reap physical close]", cl)
        if "error" not in cl:
            print("[repro] *** pane.close on dispatcher SUCCEEDED (closed_via=pane.close) "
                  "=> matches journal kill={'closed_via':'pane.close'} => LIVE pane was reaped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
