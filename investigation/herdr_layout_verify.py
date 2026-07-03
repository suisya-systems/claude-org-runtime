#!/usr/bin/env python3
"""E2E レイアウト検証: 実 HerdrAdapter (multi-space) を live herdr で駆動する。

Issue #110 の実装を **実 socket** に対して裏取りする (fake socket テストの補完)。
run_verify.sh (Issue #114) の隔離戦略を踏襲し、専用 XDG + session の headless herdr に
対して実 HerdrAdapter の spawn(space=) / list_panes / kill_pane / close_workspace を叩き、
以下のレイアウト不変条件を assert する:

  L1 control スペースと project スペースが **別 workspace** に決定的配置される
     (agent.start が focused に相乗りしても reconcile の pane.move で自 space へ)。
  L2 両スペースの pane が list_panes の union に現れる (§4.1 集合化 + §9 poll 監視)。
  L3 workspace ラベルが世代識別スキーマ ``{prefix}/{oid}/g{gen}/{space_key}`` に従う。
  L4 project スペースの最後の pane を閉じると **その workspace が掃除**され、control は
     残る (§4.3 ephemeral cleanup + control 除外)。
  L5 close_workspace() が **全 owned workspace** を閉じる (§4.1 org down)。

usage: herdr_layout_verify.py <socket_path>
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time

REPO_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
sys.path.insert(0, REPO_SRC)

from claude_org_runtime.terminal.base import SpaceDescriptor  # noqa: E402
from claude_org_runtime.terminal.herdr import HerdrAdapter  # noqa: E402

_id = [0]
FAILS: list[str] = []


def rt(sock_path: str, method: str, params: dict) -> dict:
    _id[0] += 1
    payload = (
        json.dumps({"id": f"v{_id[0]}", "method": method, "params": params}) + "\n"
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


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}: {detail}")
    if not cond:
        FAILS.append(name)


def main() -> int:
    sock = sys.argv[1]
    state_dir = os.path.join(os.path.dirname(sock), "broker_state")
    os.makedirs(state_dir, exist_ok=True)
    print(f"[verify] socket={sock} state_dir={state_dir}")

    # 人間が TUI で見ている状態を模す: user workspace を focus 済みにする
    # (これで agent.start は focused=user へ相乗りし、reconcile の move が必要になる)。
    uw = rt(sock, "workspace.create", {"label": "user", "cwd": os.getcwd()})
    user_wid = uw["result"]["workspace"]["workspace_id"]
    rt(sock, "workspace.focus", {"workspace_id": user_wid})
    print(f"[setup] user_ws(focused)={user_wid}")

    a = HerdrAdapter(socket_path=sock, timeout=10.0, state_dir=state_dir)
    print(f"[setup] adapter oid={a.org_instance_id} gen={a.generation}")

    argv = ["bash", "-lc", "sleep 300"]
    ref_c = a.spawn(argv, cwd=os.getcwd(), space=SpaceDescriptor("control"))
    ref_p = a.spawn(argv, cwd=os.getcwd(), space=SpaceDescriptor("project:demo"))
    print(f"[spawn] control -> {ref_c.pane_id}@{ref_c.window_id}  "
          f"project -> {ref_p.pane_id}@{ref_p.window_id}")

    # L1: 別 workspace に決定的配置 (agent.start 相乗りを reconcile が是正)
    check("L1_distinct_workspaces",
          ref_c.window_id != ref_p.window_id and ref_c.window_id != user_wid,
          f"control={ref_c.window_id} project={ref_p.window_id} user={user_wid}")

    # 実 pane.get で実配置 workspace を確認 (reconcile が効いているか)
    gc = rt(sock, "pane.get", {"pane_id": ref_c.pane_id}).get("result", {}).get("pane", {})
    gp = rt(sock, "pane.get", {"pane_id": ref_p.pane_id}).get("result", {}).get("pane", {})
    check("L1_control_placed", gc.get("workspace_id") == ref_c.window_id,
          f"pane.get={gc.get('workspace_id')} want={ref_c.window_id}")
    check("L1_project_placed", gp.get("workspace_id") == ref_p.window_id,
          f"pane.get={gp.get('workspace_id')} want={ref_p.window_id}")

    # L2: 両 pane が list_panes の union に現れる (user focused のまま非フォーカス観測)
    time.sleep(0.5)
    listed = {p["pane_id"]: p for p in a.list_panes()}
    check("L2_union_lists_both",
          ref_c.pane_id in listed and ref_p.pane_id in listed,
          f"listed={sorted(listed)}")

    # L3: 世代識別ラベル
    wl = rt(sock, "workspace.list", {}).get("result", {}).get("workspaces", [])
    labels = {w["workspace_id"]: w.get("label", "") for w in wl}
    exp_c = f"claude-org/{a.org_instance_id}/g{a.generation}/control"
    exp_p = f"claude-org/{a.org_instance_id}/g{a.generation}/project:demo"
    check("L3_control_label", labels.get(ref_c.window_id) == exp_c,
          f"got={labels.get(ref_c.window_id)!r} want={exp_c!r}")
    check("L3_project_label", labels.get(ref_p.window_id) == exp_p,
          f"got={labels.get(ref_p.window_id)!r} want={exp_p!r}")

    # L4: project スペースの最後の pane を閉じると workspace が掃除される。control は残る。
    a.kill_pane(ref_p.pane_id)
    time.sleep(0.5)
    wl2 = rt(sock, "workspace.list", {}).get("result", {}).get("workspaces", [])
    ws_ids2 = {w["workspace_id"] for w in wl2}
    check("L4_project_swept", ref_p.window_id not in ws_ids2,
          f"project ws {ref_p.window_id} present={ref_p.window_id in ws_ids2}")
    check("L4_control_preserved", ref_c.window_id in ws_ids2,
          f"control ws {ref_c.window_id} present={ref_c.window_id in ws_ids2}")

    # L5: close_workspace() が全 owned workspace を閉じる (org down)。user は残る。
    ok = a.close_workspace()
    time.sleep(0.5)
    wl3 = rt(sock, "workspace.list", {}).get("result", {}).get("workspaces", [])
    ws_ids3 = {w["workspace_id"] for w in wl3}
    check("L5_close_all_owned",
          ok and ref_c.window_id not in ws_ids3 and user_wid in ws_ids3,
          f"ok={ok} control_present={ref_c.window_id in ws_ids3} "
          f"user_present={user_wid in ws_ids3}")

    print("\n=== layout verify summary ===")
    print("FAILURES:", FAILS if FAILS else "none")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
