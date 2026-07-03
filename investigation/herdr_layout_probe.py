#!/usr/bin/env python3
"""probe 6: Herdr workspace レイアウト配置決定性の実測 (Issue #110 / 設計書 §11)。

設計書 herdr-workspace-layout.md §11 の probe 6a-6f を隔離 herdr 上で実測し、
multi-space レイアウト (control 面 + プロジェクト単位スペース) の配置戦略
(A: workspace 尊重 / B: focus-then-spawn / C: spawn-then-move) を確定する。

前提コード #115 は単一 dedicated workspace への pane.move 移送 (戦略 C) で
liveness を回復済み。本 probe は #110 が要求する **複数 workspace への決定的
配置** (各 space が別 workspace + 単一 tab) が同じ機構で成立するかを裏取りする。

隔離: run_layout_probe.sh が専用 XDG_CONFIG_HOME + 専用 session で headless
herdr を起動して本スクリプトを駆動する (ユーザの live herdr に非接触)。

実測項目:
  6a  agent.start が {workspace,tab} を尊重するか (戦略 A の成否)
  6c  pane.move の cross-workspace 可否 + id 保存性 + workspace_id 更新 (戦略 C)
  6d  agent.start {split} / pane.split {direction} の方向尊重 (§8)
  6e  throwaway workspace の root-pane close による auto-close 再現 (§7.4)
  6f  非フォーカス workspace の pane 監視到達性 (pane.get / pane.read / pane.list, §9)
  MS  multi-space: control + project の 2 workspace へ別々に決定的配置し、
      第3 (user) workspace が focused な状態で両 pane を観測できるか (§7.3 / §9)
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time

_id = [0]


def rt(sock_path: str, method: str, params: dict) -> dict:
    _id[0] += 1
    payload = (
        json.dumps({"id": f"r{_id[0]}", "method": method, "params": params}) + "\n"
    ).encode()
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


def err(resp):
    if isinstance(resp, dict) and "error" in resp:
        e = resp["error"] or {}
        return f"{e.get('code')}:{str(e.get('message',''))[:80]}"
    return None


def res(resp):
    return resp.get("result", {}) if isinstance(resp, dict) else {}


def make_ws(sock, label, focus=False):
    wc = rt(sock, "workspace.create", {"label": label, "cwd": os.getcwd()})
    if err(wc):
        raise SystemExit(f"workspace.create({label}) failed: {err(wc)}")
    w = res(wc)["workspace"]
    root = res(wc).get("root_pane", {}).get("pane_id")
    if focus:
        rt(sock, "workspace.focus", {"workspace_id": w["workspace_id"]})
    return w["workspace_id"], w["active_tab_id"], root


def start_agent(sock, name, wid, tab, split="down"):
    ag = rt(sock, "agent.start", {
        "name": name, "argv": ["bash", "-lc", "sleep 300"],
        "workspace": wid, "tab": tab, "split": split, "cwd": os.getcwd(),
    })
    if err(ag):
        return None, None, None, err(ag)
    a = res(ag)["agent"]
    return a.get("pane_id"), a.get("workspace_id"), a.get("terminal_id"), None


def move_to_tab(sock, pane_id, tab_id, split="down"):
    mv = rt(sock, "pane.move", {
        "pane_id": pane_id,
        "destination": {"type": "tab", "tab_id": tab_id, "split": split},
    })
    if err(mv):
        return None, None, err(mv)
    moved = (res(mv).get("move_result") or {}).get("pane") or {}
    return moved.get("pane_id"), moved.get("terminal_id"), None


def pane_get(sock, pane_id):
    g = rt(sock, "pane.get", {"pane_id": pane_id})
    if err(g):
        return None, err(g)
    return res(g).get("pane") or {}, None


RESULTS = {}


def record(key, verdict, detail):
    RESULTS[key] = (verdict, detail)
    print(f"[{key}] {verdict}: {detail}")


def main():
    sock = sys.argv[1]
    print(f"[probe6] socket={sock}")

    # user workspace を focus 済みにする (dogfood: 人間が TUI で見ている状態)。
    user_wid, _, _ = make_ws(sock, "user", focus=True)
    print(f"[setup] user_ws(focused)={user_wid}")

    # === 6a: agent.start が {workspace,tab} を尊重するか ===
    a_wid, a_tab, a_root = make_ws(sock, "probe-6a", focus=False)
    pid, landed_ws, tid, e = start_agent(sock, "p6a", a_wid, a_tab)
    if e:
        record("6a", "ERROR", e)
    elif landed_ws == a_wid:
        record("6a", "RESPECTED", f"landed in target {a_wid} (=> strategy A viable)")
    else:
        record("6a", "IGNORED",
               f"target={a_wid} but landed={landed_ws} (focused ride-along; A not viable)")

    # === 6c: pane.move cross-workspace + id 保存性 + workspace_id 更新 ===
    if pid and landed_ws and landed_ws != a_wid:
        new_pid, new_tid, merr = move_to_tab(sock, pid, a_tab)
        if merr:
            record("6c", "MOVE_FAILED", f"pane.move -> {merr} (strategy C not viable)")
        else:
            id_preserved = new_pid == pid
            pane, gerr = pane_get(sock, new_pid)
            landed_after = pane.get("workspace_id") if pane else None
            ws_updated = landed_after == a_wid
            tid_preserved = new_tid == tid if (tid and new_tid) else None
            record("6c", "MOVE_OK",
                   f"post-move pane_id={new_pid} (id_preserved={id_preserved}), "
                   f"pane.get workspace_id={landed_after} (updated_to_target={ws_updated}), "
                   f"terminal_id_preserved={tid_preserved}")
            pid = new_pid  # 以降で使う
    elif landed_ws == a_wid:
        record("6c", "SKIPPED", "6a respected placement; move not needed")

    # === 6f: 非フォーカス workspace の監視到達性 ===
    # user_ws を focus した状態で、probe-6a workspace (非フォーカス) の pane を観測。
    rt(sock, "workspace.focus", {"workspace_id": user_wid})
    time.sleep(0.5)
    if pid:
        pane, gerr = pane_get(sock, pid)
        get_ok = pane is not None and not gerr
        rd = rt(sock, "pane.read", {"pane_id": pid, "source": "visible", "format": "text"})
        read_ok = err(rd) is None
        pl = rt(sock, "pane.list", {"workspace_id": a_wid})
        listed = [p.get("pane_id") for p in res(pl).get("panes", [])] if not err(pl) else None
        list_ok = listed is not None and pid in listed
        if get_ok and read_ok and list_ok:
            record("6f", "REACHABLE",
                   f"non-focused ws pane observable: pane.get={get_ok} "
                   f"pane.read={read_ok} pane.list={list_ok}")
        else:
            record("6f", "DEGRADED",
                   f"pane.get={get_ok} pane.read={read_ok} pane.list={list_ok} "
                   f"(gerr={gerr}, read_err={err(rd)})")

    # === MS: multi-space 決定的配置 (control + project を別 workspace へ) ===
    rt(sock, "workspace.focus", {"workspace_id": user_wid})  # user を focus 継続
    c_wid, c_tab, c_root = make_ws(sock, "control", focus=False)
    p_wid, p_tab, p_root = make_ws(sock, "project-x", focus=False)
    # control 面へ 1 pane、project 面へ 1 pane を配置 (user focused のまま)。
    cpid, c_landed, ctid, ce = start_agent(sock, "ctl", c_wid, c_tab)
    ppid, p_landed, ptid, pe = start_agent(sock, "wrk", p_wid, p_tab)
    detail = []
    ok = True
    for tag, pid_, landed_, target_, tab_ in (
        ("control", cpid, c_landed, c_wid, c_tab),
        ("project", ppid, p_landed, p_wid, p_tab),
    ):
        if not pid_:
            detail.append(f"{tag}=START_FAILED")
            ok = False
            continue
        if landed_ != target_:
            npid, ntid, mverr = move_to_tab(sock, pid_, tab_)
            if mverr:
                detail.append(f"{tag}=MOVE_FAILED({mverr})")
                ok = False
                continue
            pid_ = npid
        pane, _ = pane_get(sock, pid_)
        final_ws = pane.get("workspace_id") if pane else None
        placed = final_ws == target_
        detail.append(f"{tag}->ws={final_ws}(target={target_},ok={placed})")
        ok = ok and placed
    # 2 pane が **別々の** workspace に居るか (isolation の核)
    cp_pane, _ = pane_get(sock, cpid if c_landed == c_wid else cpid)
    distinct = c_wid != p_wid
    record("MS", "MULTI_OK" if ok else "MULTI_DEGRADED",
           f"distinct_ws={distinct}; " + "; ".join(detail))

    # === 6e: throwaway workspace の auto-close (root pane close で ws ごと消えるか) ===
    e_wid, e_tab, e_root = make_ws(sock, "probe-6e", focus=False)
    # root pane しか居ない workspace の root を close する。
    rt(sock, "pane.close", {"pane_id": e_root})
    time.sleep(0.5)
    wl = rt(sock, "workspace.list", {})
    ws_ids = [w.get("workspace_id") for w in res(wl).get("workspaces", [])]
    if e_wid in ws_ids:
        record("6e", "SURVIVES",
               f"workspace {e_wid} still present after root close (no auto-close)")
    else:
        record("6e", "AUTO_CLOSED",
               f"workspace {e_wid} vanished after root pane close "
               "(=> root cleanup MUST be gated on real placement, §7.4)")

    # === 6d: split 方向の尊重 (agent.start split=down で上下積みになるか) ===
    d_wid, d_tab, d_root = make_ws(sock, "probe-6d", focus=True)
    p1, l1, _, _ = start_agent(sock, "d1", d_wid, d_tab, split="down")
    if p1 and l1 != d_wid:
        p1, _, _ = move_to_tab(sock, p1, d_tab, split="down")
    p2, l2, _, _ = start_agent(sock, "d2", d_wid, d_tab, split="down")
    if p2 and l2 != d_wid:
        p2, _, _ = move_to_tab(sock, p2, d_tab, split="down")
    lay = rt(sock, "pane.layout", {"pane_id": p1}) if p1 else {"error": {"code": "no_pane"}}
    if err(lay):
        record("6d", "NO_LAYOUT", f"pane.layout -> {err(lay)}")
    else:
        entries = res(lay).get("layout", res(lay)).get("panes", [])
        rects = {e.get("pane_id"): e.get("rect") for e in entries if e.get("rect")}
        r1, r2 = rects.get(p1), rects.get(p2)
        if r1 and r2:
            # 上下積み (down) なら y が異なり x が同じ傾向。
            stacked = r1.get("y") != r2.get("y")
            record("6d", "STACKED" if stacked else "SIDE_BY_SIDE",
                   f"p1.rect={r1} p2.rect={r2} (down=>vertical stack expected)")
        else:
            record("6d", "PARTIAL_LAYOUT", f"rects={rects}")

    # サマリ
    print("\n=== probe6 summary ===")
    for k in ("6a", "6c", "6d", "6e", "6f", "MS"):
        v = RESULTS.get(k)
        print(f"  {k}: {v[0] if v else 'N/A'} -- {v[1] if v else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
