#!/usr/bin/env python3
"""agent.start の配置セマンティクス実測 (Issue #114 修正案の feasibility)。

事前フォーカス済み workspace (ユーザーの startup 相当) がある状態で、dedicated
workspace への agent 配置を複数の方法で試し、どれが「focused workspace 相乗り」を
回避できるかを測る。修正設計案の裏取り用。
"""
from __future__ import annotations

import json
import os
import socket
import sys

_id = [0]


def rt(sock_path: str, method: str, params: dict) -> dict:
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


def landed(resp):
    if "error" in resp:
        return f"ERROR:{resp['error'].get('code')}"
    a = resp.get("result", {}).get("agent", {})
    return a.get("pane_id"), a.get("workspace_id")


def make_user_ws(sock):
    uw = rt(sock, "workspace.create", {"label": "user", "cwd": os.path.expanduser("~")})
    uwid = uw["result"]["workspace"]["workspace_id"]
    rt(sock, "workspace.focus", {"workspace_id": uwid})
    return uwid


def make_dedicated(sock):
    wc = rt(sock, "workspace.create", {"label": f"ded-{os.getpid()}", "cwd": os.getcwd()})
    w = wc["result"]["workspace"]
    return w["workspace_id"], w["active_tab_id"], wc["result"]["root_pane"]["pane_id"]


def main():
    sock = sys.argv[1]
    print(f"[probe] socket={sock}")

    # --- Q1: split を省略したら workspace/tab を尊重するか? (dedicated 非フォーカス) ---
    uwid = make_user_ws(sock)
    dwid, dtab, droot = make_dedicated(sock)
    print(f"[Q1] user_ws(focused)={uwid} dedicated={dwid} (NOT focused)")
    ag = rt(sock, "agent.start", {
        "name": "q1", "argv": ["bash", "-lc", "sleep 300"],
        "workspace": dwid, "tab": dtab,  # NO split
        "cwd": os.getcwd(),
    })
    print(f"[Q1] agent.start (workspace={dwid}, NO split) -> landed {landed(ag)}  "
          f"(want w in {dwid})")

    # --- Q2: agent.start が focused に相乗りした pane を dedicated へ move できるか? ---
    uwid2 = make_user_ws(sock)
    dwid2, dtab2, droot2 = make_dedicated(sock)
    rt(sock, "workspace.focus", {"workspace_id": uwid2})  # user を focus
    print(f"[Q2] user_ws(focused)={uwid2} dedicated={dwid2}")
    ag2 = rt(sock, "agent.start", {
        "name": "q2", "argv": ["bash", "-lc", "sleep 300"],
        "workspace": dwid2, "tab": dtab2, "split": "down",
        "cwd": os.getcwd(),
    })
    pid_ws = landed(ag2)
    print(f"[Q2] agent.start landed {pid_ws}")
    if isinstance(pid_ws, tuple):
        mis_pane = pid_ws[0]
        # pane.move で dedicated tab へ移動できるか (cross-workspace move)
        for params in (
            {"pane_id": mis_pane, "workspace_id": dwid2, "tab_id": dtab2},
            {"pane_id": mis_pane, "tab": dtab2},
            {"pane_id": mis_pane, "workspace": dwid2, "tab": dtab2},
        ):
            mv = rt(sock, "pane.move", params)
            tag = "ok" if "error" not in mv else f"ERROR:{mv['error'].get('code')}:{mv['error'].get('message','')[:60]}"
            print(f"[Q2] pane.move {list(params.keys())} -> {tag}")
            if "error" not in mv:
                break

    # --- Q3: focus 後に agent.start し、その後 user ws に focus を戻せるか (UX 緩和) ---
    uwid3 = make_user_ws(sock)
    dwid3, dtab3, droot3 = make_dedicated(sock)
    rt(sock, "workspace.focus", {"workspace_id": dwid3})
    ag3 = rt(sock, "agent.start", {
        "name": "q3", "argv": ["bash", "-lc", "sleep 300"],
        "workspace": dwid3, "tab": dtab3, "split": "down", "cwd": os.getcwd(),
    })
    print(f"[Q3] focus(dedicated)+agent.start -> landed {landed(ag3)} (want {dwid3})")
    back = rt(sock, "workspace.focus", {"workspace_id": uwid3})
    print(f"[Q3] refocus user_ws {uwid3} -> "
          f"{'ok' if 'error' not in back else back['error']}")
    # 元の user ws にフォーカスが戻ったか確認
    wl = rt(sock, "workspace.list", {}).get("result", {}).get("workspaces", [])
    foc = [w['workspace_id'] for w in wl if w.get('focused')]
    print(f"[Q3] focused after refocus = {foc} (want [{uwid3}])")
    return 0


if __name__ == "__main__":
    sys.exit(main())
