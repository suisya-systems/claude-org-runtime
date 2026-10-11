# -*- coding: utf-8 -*-
"""herdr 0.9.x (protocol 22) re-measurement + broker E2E (Issue #237).

Run through ``investigation/run_09_probe.sh``, which starts an isolated
``herdr --session herdr-0-9-spike`` server and tears it down afterwards.

usage: herdr_09_probe.py <socket> <out.json> [--skip-e2e | --e2e-only]

Sections (each records into the JSON report; one failing section does not stop
the others):

1. ping              - version / protocol / capabilities.
2. keys              - pane.send_keys vocabulary, token by token, against a pane
                       whose tty is raw (``stty raw``) so no key can kill it.
3. ids               - pane / tab / workspace id format incl. pane number >= 10.
4. list_consistency  - pane.list right after pane.split, and after a pane exits.
5. events            - events.subscribe burst delivery and overflow signalling.
6. agent             - agent.start errors, agent_status of a real claude,
                       pane.report_agent accepted states.
7. e2e               - broker over MCP HTTP on the herdr backend:
                       spawn_claude_pane, send_message delivery (nudge ->
                       check_messages -> reply), inspect_pane, send_keys,
                       close_pane.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request

from claude_org_runtime.terminal.herdr import (
    HerdrAdapter,
    HerdrError,
    _HerdrClient,
)

SOCK = sys.argv[1]
OUT = sys.argv[2]
SKIP_E2E = "--skip-e2e" in sys.argv
E2E_ONLY = "--e2e-only" in sys.argv
if E2E_ONLY:  # keep the full-probe report from an earlier run
    OUT = OUT.replace(".json", "_e2e.json")
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

C = _HerdrClient(SOCK, 15.0)
REPORT: dict = {}


def raw(method: str, params: dict) -> dict:
    """1 request, raw outcome (ok result or error code) without raising."""
    try:
        return {"ok": True, "result": C.request(method, params)}
    except HerdrError as e:
        return {"ok": False, "code": e.code, "raw": getattr(e, "raw", None),
                "msg": str(e)[:300]}


def section(name):
    def deco(fn):
        if E2E_ONLY and name != "e2e":
            return fn
        t0 = time.time()
        try:
            REPORT[name] = fn()
        except Exception:  # noqa: BLE001 - keep measuring the other sections
            REPORT[name] = {"crashed": traceback.format_exc()}
        REPORT[name + "_seconds"] = round(time.time() - t0, 1)
        print(f"[probe] {name}: done", flush=True)
        return fn
    return deco


def new_workspace(label: str) -> tuple[str, str, str]:
    res = C.request("workspace.create", {"label": label, "cwd": REPO})
    ws = res["workspace"]["workspace_id"]
    return ws, res["workspace"].get("active_tab_id"), res["root_pane"]["pane_id"]


def close_ws(ws: str) -> None:
    raw("workspace.close", {"workspace_id": ws})


def read_screen(pane_id: str) -> str:
    r = raw("pane.read", {"pane_id": pane_id, "source": "visible", "format": "text"})
    return (r.get("result") or {}).get("read", {}).get("text", "") if r["ok"] else ""


# --------------------------------------------------------------------- 1 ping
@section("ping")
def _ping():
    return C.request("ping", {})


# --------------------------------------------------------------------- 2 keys
KEY_TOKENS = [
    "enter", "return", "tab", "space", "esc", "escape", "backspace", "bs",
    "up", "down", "left", "right", "shift+tab", "backtab",
    *[f"ctrl+{c}" for c in "abcdefghijklmnopqrstuvwxyz"],
    "delete", "del", "home", "end", "pageup", "pagedown", "page_up", "insert",
    "Enter", "ESCAPE", "C-c", "ctrl-c", "alt+x", "f1", "f12",
]


@section("keys")
def _keys():
    ws, _tab, root = new_workspace("probe237-keys")
    try:
        # raw tty: ctrl+c/d/z/\ are plain bytes, so a key can't kill the pane
        # (the 0.7.1 probe mis-read a dead pane as "key rejected").
        C.request("pane.send_text", {
            "pane_id": root, "text": "stty raw -echo; exec cat > /dev/null\n"})
        time.sleep(1.0)
        accepted, rejected, other = [], {}, {}
        for tok in KEY_TOKENS:
            r = raw("pane.send_keys", {"pane_id": root, "keys": [tok]})
            if r["ok"]:
                accepted.append(tok)
            elif r.get("raw") == "invalid_key" or "invalid_key" in r.get("msg", ""):
                rejected[tok] = r["msg"]
            else:
                other[tok] = r
        alive = raw("pane.get", {"pane_id": root})["ok"]
        mixed = raw("pane.send_keys", {"pane_id": root, "keys": ["enter", "home"]})
        return {"accepted": accepted, "rejected_invalid_key": sorted(rejected),
                "other_errors": other, "pane_alive_after": alive,
                "mixed_enter_home": mixed}
    finally:
        close_ws(ws)


# ---------------------------------------------------------------------- 3 ids
@section("ids")
def _ids():
    ws, tab, root = new_workspace("probe237-ids")
    seen = []
    try:
        # pane numbers are per-workspace monotonic; split+close 11 times so the
        # last ids pass pane number 10.
        for _ in range(11):
            r = C.request("pane.split", {"workspace_id": ws, "target_pane_id": root,
                                         "direction": "right"})
            pid = r["pane"]["pane_id"]
            seen.append(pid)
            raw("pane.close", {"pane_id": pid})
        return {"workspace_id": ws, "tab_id": tab, "root_pane_id": root,
                "split_pane_ids": seen,
                "terminal_id_sample": r["pane"].get("terminal_id")}
    finally:
        close_ws(ws)


# --------------------------------------------------------- 4 list consistency
@section("list_consistency")
def _list():
    ws, _tab, root = new_workspace("probe237-list")
    out = {"split_then_list_missing": 0, "trials": 20, "exit": []}
    try:
        for _ in range(out["trials"]):
            r = C.request("pane.split", {"workspace_id": ws, "target_pane_id": root,
                                         "direction": "down"})
            pid = r["pane"]["pane_id"]
            ids = [p["pane_id"] for p in
                   C.request("pane.list", {"workspace_id": ws})["panes"]]
            if pid not in ids:
                out["split_then_list_missing"] += 1
            raw("pane.close", {"pane_id": pid})
        # process exit: how long until pane.list / pane.get drop the pane?
        for _ in range(3):
            r = C.request("pane.split", {"workspace_id": ws, "target_pane_id": root,
                                         "direction": "down"})
            pid = r["pane"]["pane_id"]
            time.sleep(0.8)
            C.request("pane.send_text", {"pane_id": pid, "text": "exit\n"})
            t0 = time.time()
            gone_list = gone_get = None
            while time.time() - t0 < 10 and (gone_list is None or gone_get is None):
                ids = [p["pane_id"] for p in
                       C.request("pane.list", {"workspace_id": ws})["panes"]]
                if gone_list is None and pid not in ids:
                    gone_list = round(time.time() - t0, 2)
                if gone_get is None and not raw("pane.get", {"pane_id": pid})["ok"]:
                    gone_get = round(time.time() - t0, 2)
                time.sleep(0.05)
            out["exit"].append({"pane": pid, "gone_from_list_s": gone_list,
                                "gone_from_get_s": gone_get})
        return out
    finally:
        close_ws(ws)


# ------------------------------------------------------------------- 5 events
def _subscribe(subs: list[dict]) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(SOCK)
    req = {"id": "sub1", "method": "events.subscribe",
           "params": {"subscriptions": subs}}
    s.sendall((json.dumps(req) + "\n").encode())
    return s


def _drain(s: socket.socket, quiet: float) -> list[dict]:
    s.settimeout(quiet)
    buf, lines = b"", []
    while True:
        try:
            chunk = s.recv(65536)
        except socket.timeout:
            break
        if not chunk:
            lines.append({"_eof": True})
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            try:
                lines.append(json.loads(line))
            except ValueError:
                lines.append({"_unparsed": line[:200].decode("utf-8", "replace")})
    return lines


@section("events")
def _events():
    ws, _tab, _root = new_workspace("probe237-events")
    out = {}
    try:
        for n, stall in ((200, False), (1500, True)):
            s = _subscribe([{"type": "workspace.renamed"}])
            ack = _drain(s, 1.0)
            for i in range(n):
                raw("workspace.rename", {"workspace_id": ws, "label": f"probe237-ev-{i}"})
            if stall:
                time.sleep(3.0)  # reader stalled while the burst queues up
            got = _drain(s, 3.0)
            s.close()
            ev = [x for x in got if x.get("event") or (x.get("data") or {}).get("type")]
            errs = [x for x in got if "error" in x]
            out[f"burst_{n}{'_stalled' if stall else ''}"] = {
                "sent": n, "ack": ack[:1], "received": len(ev),
                "errors": errs[:3], "eof": any(x.get("_eof") for x in got),
                "sample": ev[:1],
            }
        return out
    finally:
        close_ws(ws)


# -------------------------------------------------------------------- 6 agent
@section("agent")
def _agent():
    ws, _tab, root = new_workspace("probe237-agent")
    out = {}
    try:
        r = C.request("pane.split", {"workspace_id": ws, "target_pane_id": root,
                                     "direction": "down", "cwd": REPO})
        pid = r["pane"]["pane_id"]
        out["bad_name"] = raw("agent.start", {"name": "Bad", "kind": "claude",
                                              "pane_id": pid, "timeout_ms": 10000})
        out["bad_timeout"] = raw("agent.start", {"name": "p237a", "kind": "claude",
                                                 "pane_id": pid, "timeout_ms": 3000})
        out["bad_kind"] = raw("agent.start", {"name": "p237a", "kind": "nope",
                                              "pane_id": pid, "timeout_ms": 10000})
        # immediately after split the shell may not be at a prompt yet
        out["start"] = raw("agent.start", {"name": "p237a", "kind": "claude",
                                           "pane_id": pid, "args": [],
                                           "timeout_ms": 15000})
        busy_retries = 0
        while not out["start"]["ok"] and out["start"].get("code") == "agent_pane_busy" \
                and busy_retries < 40:
            time.sleep(0.25)
            busy_retries += 1
            out["start"] = raw("agent.start", {"name": "p237a", "kind": "claude",
                                               "pane_id": pid, "args": [],
                                               "timeout_ms": 15000})
        out["busy_retries"] = busy_retries
        out["start_on_running_agent"] = raw(
            "agent.start", {"name": "p237b", "kind": "claude", "pane_id": pid,
                            "timeout_ms": 10000})
        trace, t0, last = [], time.time(), None
        while time.time() - t0 < 30:
            p = raw("pane.get", {"pane_id": pid})
            st = ((p.get("result") or {}).get("pane") or {}).get("agent_status")
            ag = ((p.get("result") or {}).get("pane") or {}).get("agent")
            if (st, ag) != last:
                trace.append({"t": round(time.time() - t0, 1), "status": st, "agent": ag})
                last = (st, ag)
            time.sleep(0.5)
        out["status_trace"] = trace
        out["agent_list"] = raw("agent.list", {})
        if out["agent_list"]["ok"]:
            agents = out["agent_list"]["result"].get("agents", [])
            out["agent_list"] = {"ok": True, "count": len(agents),
                                 "sample": [a for a in agents
                                            if a.get("pane_id") == pid][:1]}
        out["screen_tail"] = read_screen(pid).splitlines()[-12:]
        # report_agent on a plain shell pane
        r2 = C.request("pane.split", {"workspace_id": ws, "target_pane_id": root,
                                      "direction": "right"})
        p2 = r2["pane"]["pane_id"]
        out["report_agent"] = {
            st: raw("pane.report_agent", {"pane_id": p2, "source": "probe237",
                                          "agent": "probe", "state": st})
            for st in ("idle", "working", "blocked", "unknown", "done")
        }
        # claude exit -> pane stays as a shell?
        C.request("pane.send_text", {"pane_id": pid, "text": "/exit"})
        time.sleep(0.3)
        C.request("pane.send_keys", {"pane_id": pid, "keys": ["enter"]})
        time.sleep(4)
        out["after_claude_exit_pane_exists"] = raw("pane.get", {"pane_id": pid})["ok"]
        return out
    finally:
        close_ws(ws)


# ---------------------------------------------------------------------- 7 e2e
class Mcp:
    def __init__(self, url: str, token: str) -> None:
        self.url, self.token, self.sid, self.n = url, token, None, 0

    def post(self, payload):
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream",
                     "Authorization": f"Bearer {self.token}",
                     **({"Mcp-Session-Id": self.sid} if self.sid else {})})
        with urllib.request.urlopen(req, timeout=120) as resp:
            self.sid = resp.headers.get("Mcp-Session-Id") or self.sid
            body = resp.read()
        return json.loads(body) if body else None

    def tool(self, name: str, args: dict) -> dict:
        self.n += 1
        res = self.post({"jsonrpc": "2.0", "id": self.n, "method": "tools/call",
                         "params": {"name": name, "arguments": args}})
        if "result" not in res:
            return {"_rpc_error": res.get("error")}
        text = res["result"]["content"][0]["text"]
        try:
            return json.loads(text)
        except ValueError:
            return {"_text": text, "_is_error": res["result"].get("isError")}


def _e2e():
    from claude_org_runtime.broker.server import Broker

    state = tempfile.mkdtemp(prefix="probe237-broker-")
    adapter = HerdrAdapter(socket_path=SOCK, state_dir=os.path.join(state, "st"))
    broker = Broker(state_dir=os.path.join(state, "st"), adapter=adapter, port=0,
                    root_cwd=REPO)
    broker.start()
    out: dict = {"adapter_protocol": adapter._protocol}
    try:
        tok = broker.issue_token("e2e-root", "secretary", "secretary", cwd=REPO,
                                 auth_role="secretary")
        root = broker.register_logical_pane(tok)
        m = Mcp(broker.url, tok)
        m.post({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"}})
        try:
            m.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except urllib.error.HTTPError:
            pass

        sp = m.tool("spawn_claude_pane", {
            "direction": "vertical", "name": "e2e-worker", "role": "worker",
            "cwd": REPO,
            # --allowedTools is not on the broker's claude-arg allowlist;
            # --settings is.
            "args": ["--settings", json.dumps({"permissions": {"allow": [
                "mcp__org-broker__check_messages",
                "mcp__org-broker__send_message"]}})],
        })
        out["spawn_claude_pane"] = sp
        target = sp.get("id") or "e2e-worker"

        # startup dialogs (folder trust / dev channel): same screen-driven loop
        # the dispatcher uses (spawn_prompt.decide), never a blind Enter.
        from claude_org_runtime.dispatcher.spawn_prompt import decide, screen_lines

        ready, prev, t0, steps = False, None, time.time(), []
        while time.time() - t0 < 120:
            res = m.tool("inspect_pane", {"target": target})
            # inspect_pane answers plain screen text, which Mcp.tool wraps as _text
            scr = screen_lines(res.get("_text", res))
            d = decide(scr, prev, int((time.time() - t0) * 1000))
            steps.append({"t": round(time.time() - t0, 1), "state": d["state"],
                          "action": d["action"]})
            if d["action"] == "done":
                ready = True
                break
            if d["action"] == "escalate":
                out["startup_escalated_screen"] = scr[-20:]
                break
            if d["action"] == "send_keys":
                m.tool("send_keys", {"target": target, **d["send_keys"]})
                prev = None
            else:
                prev = scr
            time.sleep(1.5)
        out["startup_steps"] = [x for i, x in enumerate(steps)
                                if i == 0 or x["state"] != steps[i - 1]["state"]
                                or x["action"] != "wait"]
        out["claude_ready_s"] = round(time.time() - t0, 1) if ready else None
        out["inspect_pane"] = m.tool("inspect_pane", {"target": target, "lines": 15})

        out["list_panes"] = m.tool("list_panes", {})
        # the worker becomes addressable once its MCP session registers
        t1 = time.time()
        while True:
            sent = m.tool("send_message", {
                "to_id": "e2e-worker",
                "message": ("E2E #237: reply now with send_message to_id='e2e-root' "
                            "and message exactly 'ACK-237'. Do nothing else."),
            })
            if "peer_not_found" not in json.dumps(sent) or time.time() - t1 > 60:
                break
            time.sleep(2)
        out["send_message"] = sent
        out["send_message_registered_after_s"] = round(time.time() - t1, 1)
        reply, t0 = None, time.time()
        while time.time() - t0 < 180 and reply is None:
            msgs = m.tool("check_messages", {}).get("messages", [])
            for msg in msgs:
                if "ACK-237" in json.dumps(msg, ensure_ascii=False):
                    reply = msg
            time.sleep(3)
        out["reply_received_s"] = round(time.time() - t0, 1) if reply else None
        out["reply"] = reply
        if reply is None:
            out["screen_on_timeout"] = m.tool("inspect_pane", {"target": target})

        # send_keys: text + Escape, then Shift+Tab (permission-mode cycle), and a
        # key herdr can't emit -> [key_unsupported]
        out["send_keys_text"] = m.tool("send_keys", {
            "target": target, "text": "hello-237"})
        before = json.dumps(m.tool("inspect_pane", {"target": target, "lines": 6}),
                            ensure_ascii=False)
        out["send_keys_backtab"] = m.tool("send_keys", {"target": target,
                                                        "keys": ["Shift+Tab"]})
        time.sleep(1.5)
        after = json.dumps(m.tool("inspect_pane", {"target": target, "lines": 6}),
                           ensure_ascii=False)
        out["backtab_changed_footer"] = before != after
        out["backtab_footer_after"] = after[-400:]
        out["send_keys_home"] = m.tool("send_keys", {"target": target,
                                                     "keys": ["Home"]})

        out["close_pane"] = m.tool("close_pane", {"target": target})
        time.sleep(2)
        out["list_after_close"] = m.tool("list_panes", {})
        out["root"] = root
    finally:
        broker.stop()
        try:
            adapter.close_workspace()
        except Exception:  # noqa: BLE001
            pass
    return out


if not SKIP_E2E:
    section("e2e")(_e2e)

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(REPORT, f, ensure_ascii=False, indent=2, default=str)
print(f"[probe] report: {OUT}")
