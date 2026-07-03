# -*- coding: utf-8 -*-
"""Shared fixtures for broker tests.

A real :class:`~claude_org_runtime.broker.server.Broker` is started on an
ephemeral localhost port with ``adapter=None`` (nudge disabled — no terminal
backend is touched), and a tiny stdlib MCP-over-HTTP client drives the
JSON-RPC surface. This mirrors the verified ``spike/mcp_smoke_test.py``
harness: protocol behaviour is exercised without spawning a real Claude.
"""

from __future__ import annotations

import itertools
import json
import urllib.error
import urllib.request

import pytest

from claude_org_runtime.broker.server import Broker
from claude_org_runtime.terminal import PaneRef
from claude_org_runtime.terminal.keys import CANONICAL_KEYS


class FakeAdapter:
    """In-memory TerminalAdapter for pane-op tests (no real backend).

    Mirrors the tmux-style native ``list_panes`` schema the broker normalizes
    (``pane_id`` / ``left`` / ``top`` / ``width`` / ``height`` / ``active`` /
    ``cursor_x`` / ``cursor_y``). ``spawn`` records the built argv so tests can
    assert what the broker's structured builders emitted.
    """

    # full raw-key vocabulary を emit できる backend (tmux / Herdr) を模す。
    # WezTerm subset を模すテストは別途 supported_named_keys を差し替える。
    supported_named_keys = frozenset(CANONICAL_KEYS)

    def __init__(
        self,
        isolated_session: bool = True,
        reap_min_age_seconds: float = 0.0,
        reap_min_missing_snapshots: int = 1,
        reap_min_missing_seconds: float = 0.0,
        detailed_kill: bool = False,
        kill_ineffective: bool = False,
    ) -> None:
        # 既定 True (tmux-style: adapter は自分が spawn した pane のみ見せる)。
        # global-mux backend (wezterm) を模すテストは False を渡す。
        self.isolated_session = isolated_session
        # backend-aware reap 閾値 (broker が getattr で読む)。既定は tmux/wezterm と
        # 同じ即時 reap (0.0 / 1)。Herdr の eventually-consistent 判定を模すテストは
        # 大きめの値を渡す。
        self.reap_min_age_seconds = reap_min_age_seconds
        self.reap_min_missing_snapshots = reap_min_missing_snapshots
        self.reap_min_missing_seconds = reap_min_missing_seconds
        # kill_ineffective: kill を発行しても pane が消えない (Herdr の close 拒否や
        # 実 kill 失敗を模す)。reap の「消せなかったら bookkeeping を保持する」defer
        # 経路 (Codex P2) を検証するため。
        self._kill_ineffective = kill_ineffective
        if detailed_kill:
            self.kill_pane_detailed = self._kill_pane_detailed_impl
        self._panes: dict[int, dict] = {}
        self._screens: dict[int, str] = {}
        # snapshot ラグ (eventually consistent) を模す: ここに入れた handle は
        # list_panes から隠れるが pane_exists では依然「存在」する (物理的には生きて
        # いるのに snapshot に現れない Herdr 挙動)。
        self._snapshot_hidden: set = set()
        self.spawned: list[dict] = []
        self.killed: list[int] = []
        self._counter = itertools.count(1)

    # bootstrap a pre-existing pane (e.g. the caller pane) ------------------
    def add_pane(self, active: bool = False, handle=None, **geom):
        # handle 明示で非数字 native handle (tmux "%N" / Herdr "wN:pN") を模せる。
        # 既定は数字 counter (既存 WezTerm-style int handle)。
        if handle is None:
            handle = next(self._counter)
        rec = {
            "pane_id": handle, "active": active, "left": 0, "top": 0,
            "width": 80, "height": 24, "cursor_x": 0, "cursor_y": 0,
        }
        rec.update(geom)
        self._panes[handle] = rec
        self._screens[handle] = ""
        return handle

    def set_focused(self, handle: int) -> None:
        for h, p in self._panes.items():
            p["active"] = (h == handle)

    # TerminalAdapter Protocol --------------------------------------------
    def spawn(self, argv, cwd=None, new_window=True) -> PaneRef:
        handle = self.add_pane()
        self.spawned.append({"argv": list(argv), "cwd": cwd, "handle": handle})
        return PaneRef(pane_id=handle)

    def list_panes(self) -> list[dict]:
        # snapshot ラグ中の pane は list から隠れる (eventually consistent)。
        return [
            dict(p) for h, p in self._panes.items()
            if h not in self._snapshot_hidden
        ]

    def pane_exists(self, pane_id) -> bool:
        # 物理存在確認は snapshot ラグに影響されない (fresh probe を模す)。
        return pane_id in self._panes

    def get_text(self, pane_id, escapes: bool = False) -> str:
        return self._screens.get(pane_id, "")

    def type_text(self, pane_id, text) -> None:
        self._screens[pane_id] = self._screens.get(pane_id, "") + text

    def send_enter(self, pane_id) -> None:
        self._screens[pane_id] = self._screens.get(pane_id, "") + "\n"

    def send_line(self, pane_id, text, settle: float = 0.0) -> None:
        self.type_text(pane_id, text)
        self.send_enter(pane_id)

    def send_interrupt(self, pane_id) -> None:
        self._screens[pane_id] = self._screens.get(pane_id, "") + "<C-c>"

    def send_named_keys(self, pane_id, keys) -> None:
        # canonical キー列を batch 送出する新経路。enter は改行、ctrl+c は既存の
        # <C-c> マーカーに畳んで従来の send_enter / send_interrupt と観測を揃え、
        # その他の raw key は <key> マーカーで screen に残す (テストが確認できる形)。
        for k in keys:
            if k == "enter":
                self.send_enter(pane_id)
            elif k == "ctrl+c":
                self.send_interrupt(pane_id)
            else:
                self._screens[pane_id] = self._screens.get(pane_id, "") + f"<{k}>"

    def kill_pane(self, pane_id) -> None:
        # 実 backend の kill は既に消えた pane に対しては no-op。reap は「消えていそう」
        # の事前 probe に頼らず常に close を発行する (Codex round2 P2) ため、gone な
        # handle にも kill_pane が来る。`killed` は「生きた pane を実際に kill したか」を
        # 表す指標として、存在した時だけ記録する (no-op kill を混ぜない)。
        existed = pane_id in self._panes
        self._panes.pop(pane_id, None)
        self._screens.pop(pane_id, None)
        self._snapshot_hidden.discard(pane_id)
        if existed:
            self.killed.append(pane_id)

    # Herdr-style detailed kill, only bound as an attribute when the adapter
    # was built with detailed_kill=True. This lets tests cover BOTH the
    # broker's detailed path (getattr finds it) and its kill_pane fallback
    # (getattr returns None) with the same fake class.
    def _kill_pane_detailed_impl(self, pane_id) -> dict:
        """Mirror ``HerdrAdapter.kill_pane_detailed`` return shape."""
        if self._kill_ineffective:
            # close refused / kill failed: pane stays physically alive.
            return {"closed_via": "refused", "still_present": pane_id in self._panes}
        present = pane_id in self._panes
        self.kill_pane(pane_id)
        return {
            "closed_via": "pane.close" if present else "already_gone",
            "still_present": pane_id in self._panes,
        }

    # simulate a pane that exits on its own (self-termination) --------------
    def terminate(self, pane_id) -> None:
        """Drop a pane WITHOUT going through ``kill_pane``.

        Models a managed pane whose process exited by itself: it vanishes from
        ``list_panes`` but the broker never called ``kill_pane`` (not recorded
        in ``self.killed``). Used to exercise the opportunistic reap path.
        """
        self._panes.pop(pane_id, None)
        self._screens.pop(pane_id, None)
        self._snapshot_hidden.discard(pane_id)

    # simulate eventually-consistent snapshot lag ---------------------------
    def desync_hide(self, pane_id) -> None:
        """Hide a pane from ``list_panes`` while keeping it physically present.

        Models Herdr's eventually-consistent ``pane.list``: a live pane can
        transiently drop out of the snapshot (boot / lag) even though the
        process is running (``pane_exists`` still True).
        """
        self._snapshot_hidden.add(pane_id)

    def desync_show(self, pane_id) -> None:
        """Reveal a previously hidden pane again (snapshot caught up)."""
        self._snapshot_hidden.discard(pane_id)


@pytest.fixture
def fake_adapter():
    return FakeAdapter()


class MiniMcpClient:
    """Minimal MCP streamable-HTTP client (ported from the spike smoke test)."""

    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self.token = token
        self.session_id: str | None = None
        self._id = 0

    def _post(self, payload: dict | None, expect_status: int = 200,
              method: str = "POST"):
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {self.token}",
                **({"Mcp-Session-Id": self.session_id} if self.session_id else {}),
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                status = resp.status
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self.session_id = sid
                body = resp.read()
        except urllib.error.HTTPError as e:
            status = e.code
            body = e.read()
        assert status == expect_status, f"status {status} != {expect_status}: {body!r}"
        return json.loads(body) if body else None

    def rpc(self, method: str, params: dict | None = None, expect_status: int = 200):
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            payload["params"] = params
        return self._post(payload, expect_status)

    def notify(self, method: str):
        self._post({"jsonrpc": "2.0", "method": method}, expect_status=202)

    def delete(self, expect_status: int = 200):
        self._post(None, expect_status=expect_status, method="DELETE")

    def call_tool(self, name: str, args: dict | None = None) -> dict:
        res = self.rpc("tools/call", {"name": name, "arguments": args or {}})
        assert "result" in res, res
        return json.loads(res["result"]["content"][0]["text"])


@pytest.fixture
def broker(tmp_path):
    """A started broker on an ephemeral port (adapter=None -> nudge disabled)."""
    b = Broker(state_dir=tmp_path / "broker", adapter=None, port=0)
    b.start()
    try:
        yield b
    finally:
        b.stop()


@pytest.fixture
def client_factory(broker):
    """Factory that issues a token and returns a connected MiniMcpClient."""

    def make(agent_id: str, name: str | None = None, role: str = "worker",
             initialize: bool = True) -> MiniMcpClient:
        token = broker.issue_token(agent_id, name or agent_id, role)
        c = MiniMcpClient(broker.url, token)
        if initialize:
            c.rpc("initialize", {"protocolVersion": "2025-06-18"})
            c.notify("notifications/initialized")
        return c

    return make
