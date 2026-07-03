#!/usr/bin/env python3
"""修正案の単一呼び出し実現性を測る (Issue #114)。

F1: agent.start に focus:true + workspace/tab を渡すと dedicated に着地するか
    (workspace.focus の別呼び出し無しで直る = 最小修正)。
F2: agent.start が focused に相乗りした後、pane.move (destination=tab+split) で
    dedicated tab へ移送できるか + 移送後の pane_id。
"""
from __future__ import annotations
import json, os, socket, sys
_id = [0]

def rt(sock, method, params):
    _id[0] += 1
    payload = (json.dumps({"id": f"r{_id[0]}", "method": method, "params": params}) + "\n").encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(10.0); s.connect(sock); s.sendall(payload)
        buf = b""
        while b"\n" not in buf:
            d = s.recv(65536)
            if not d: break
            buf += d
    return json.loads(buf.split(b"\n", 1)[0].decode("utf-8", "replace"))

def landed(resp):
    if "error" in resp: return f"ERROR:{resp['error'].get('code')}"
    a = resp.get("result", {}).get("agent", {})
    return a.get("pane_id"), a.get("workspace_id")

def user_ws(sock):
    u = rt(sock, "workspace.create", {"label": "user", "cwd": os.path.expanduser("~")})
    uid = u["result"]["workspace"]["workspace_id"]
    rt(sock, "workspace.focus", {"workspace_id": uid}); return uid

def dedicated(sock):
    w = rt(sock, "workspace.create", {"label": f"ded-{os.getpid()}", "cwd": os.getcwd()})["result"]
    return w["workspace"]["workspace_id"], w["workspace"]["active_tab_id"], w["root_pane"]["pane_id"]

def foc(sock):
    return [w["workspace_id"] for w in rt(sock, "workspace.list", {})["result"]["workspaces"] if w.get("focused")]

def main():
    sock = sys.argv[1]

    # F1: agent.start に focus:true を付けると dedicated に置かれるか
    u = user_ws(sock); d, dt, dr = dedicated(sock)
    print(f"[F1] user(focused)={u} dedicated={d}  focus now={foc(sock)}")
    ag = rt(sock, "agent.start", {"name": "f1", "argv": ["bash", "-lc", "sleep 300"],
                                  "workspace": d, "tab": dt, "split": "down",
                                  "focus": True, "cwd": os.getcwd()})
    print(f"[F1] agent.start(focus=true, workspace={d}) -> landed {landed(ag)} (want w in {d})")

    # F2: 相乗り pane を pane.move(destination: split into dedicated tab) で移送
    u2 = user_ws(sock); d2, dt2, dr2 = dedicated(sock)
    rt(sock, "workspace.focus", {"workspace_id": u2})
    ag2 = rt(sock, "agent.start", {"name": "f2", "argv": ["bash", "-lc", "sleep 300"],
                                   "workspace": d2, "tab": dt2, "split": "down", "cwd": os.getcwd()})
    lp = landed(ag2)
    print(f"[F2] agent.start landed {lp} (mis-placed in user ws)")
    if isinstance(lp, tuple):
        pane = lp[0]
        # destination スキーマ候補 (CLI: pane move <id> --tab <tab> --split down)
        for dest in (
            {"pane_id": pane, "destination": {"tab": {"tab_id": dt2, "split": "down"}}},
            {"pane_id": pane, "destination": {"type": "tab", "tab_id": dt2, "split": "down"}},
            {"pane_id": pane, "destination": {"existing_tab": {"tab_id": dt2, "split": "down"}}},
            {"pane_id": pane, "destination": {"tab_id": dt2, "split": "down"}},
        ):
            mv = rt(sock, "pane.move", dest)
            if "error" in mv:
                print(f"[F2] move dest={json.dumps(dest['destination'])[:60]} -> ERROR:{mv['error'].get('code')}:{mv['error'].get('message','')[:70]}")
            else:
                print(f"[F2] move dest={json.dumps(dest['destination'])[:60]} -> OK result={json.dumps(mv['result'])[:200]}")
                # 移送後、dedicated に居るか + pane_id 変化
                pl = rt(sock, "pane.list", {"workspace_id": d2})
                ids = [(p.get("pane_id"), p.get("workspace_id")) for p in pl.get("result", {}).get("panes", [])]
                print(f"[F2]   after move, pane.list(dedicated {d2}) = {ids}")
                break
    return 0

if __name__ == "__main__":
    sys.exit(main())
