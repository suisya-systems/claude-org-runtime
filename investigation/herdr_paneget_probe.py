#!/usr/bin/env python3
"""pane.get の応答形状 + terminal_id 安定性を確定する (Issue #114 Fix-D 裏取り)。

Q1: pane.get(pane_id) の method 名 / 応答 shape / terminal_id 有無。
Q2: 存在しない pane_id への pane.get のエラーコード (= 権威 liveness の DEAD 判定)。
Q3: agent.start の terminal_id が pane.get 後も一致するか (id-reuse ガードの前提)。
Q4: pane.move で pane_id が変わっても terminal_id が保存されるか (プロセス不再起動)。
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


def main():
    sock = sys.argv[1]
    # user startup workspace (focused) を模擬
    u = rt(sock, "workspace.create", {"label": "user", "cwd": os.path.expanduser("~")})
    uid = u["result"]["workspace"]["workspace_id"]
    rt(sock, "workspace.focus", {"workspace_id": uid})
    # dedicated workspace
    w = rt(sock, "workspace.create", {"label": f"ded-{os.getpid()}", "cwd": os.getcwd()})["result"]
    dwid, dtab = w["workspace"]["workspace_id"], w["workspace"]["active_tab_id"]

    ag = rt(sock, "agent.start", {"name": "pg", "argv": ["bash", "-lc", "sleep 300"],
                                  "workspace": dwid, "tab": dtab, "split": "down", "cwd": os.getcwd()})
    agent = ag["result"]["agent"]
    pane = agent["pane_id"]
    tid_start = agent.get("terminal_id")
    print(f"[Q3] agent.start pane_id={pane} terminal_id={tid_start} landed_ws={agent.get('workspace_id')}")

    # Q1: pane.get shape
    pg = rt(sock, "pane.get", {"pane_id": pane})
    print(f"[Q1] pane.get({pane}) -> {json.dumps(pg)[:400]}")
    if "result" in pg:
        p = pg["result"].get("pane") or pg["result"]
        tid_get = p.get("terminal_id")
        print(f"[Q1] pane.get terminal_id={tid_get} workspace_id={p.get('workspace_id')} "
              f"=> terminal_id matches agent.start? {tid_get == tid_start}")

    # Q2: pane.get on non-existent pane
    pgx = rt(sock, "pane.get", {"pane_id": "w99:p99"})
    if "error" in pgx:
        print(f"[Q2] pane.get(w99:p99) -> ERROR code={pgx['error'].get('code')}")
    else:
        print(f"[Q2] pane.get(w99:p99) -> unexpected ok {json.dumps(pgx)[:200]}")

    # Q4: terminal_id stability across pane.move
    mv = rt(sock, "pane.move", {"pane_id": pane, "destination": {"type": "tab", "tab_id": dtab, "split": "down"}})
    if "result" in mv:
        mr = mv["result"].get("move_result", {})
        moved = mr.get("pane", {})
        new_pane = moved.get("pane_id")
        tid_moved = moved.get("terminal_id")
        print(f"[Q4] pane.move -> new_pane={new_pane} terminal_id={tid_moved} "
              f"=> preserved across move? {tid_moved == tid_start}")
        # pane.get の post-move id での terminal_id
        pg2 = rt(sock, "pane.get", {"pane_id": new_pane})
        if "result" in pg2:
            p2 = pg2["result"].get("pane") or pg2["result"]
            print(f"[Q4] pane.get({new_pane}) terminal_id={p2.get('terminal_id')} ws={p2.get('workspace_id')}")
        # old pane_id は pane.get でどうなるか (move 後の旧 id)
        pg3 = rt(sock, "pane.get", {"pane_id": pane})
        if "error" in pg3:
            print(f"[Q4] pane.get(old {pane}) -> ERROR code={pg3['error'].get('code')} (旧 id は解決不能)")
        else:
            p3 = pg3["result"].get("pane") or pg3["result"]
            print(f"[Q4] pane.get(old {pane}) -> ok terminal_id={p3.get('terminal_id')} ws={p3.get('workspace_id')}")
    else:
        print(f"[Q4] pane.move ERROR {mv.get('error')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
