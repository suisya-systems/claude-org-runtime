# -*- coding: utf-8 -*-
"""Unit tests for the broker queue store + nudge delivery (no HTTP).

These drive the :class:`~claude_org_runtime.broker.store.StoreMixin` methods
and the server-side nudge worker directly, with a fake terminal adapter so
no real backend is touched. Covers the verified concurrency contract: only
registered binds are delivery targets, at-most-once drain, idle-gated nudge
injection, and the single-flight (no double-injection) guard.
"""

from __future__ import annotations

import threading
import time

import pytest

from claude_org_runtime.broker.server import Broker

# An "idle" Claude TUI screen per classify_pane_state: a bare "❯ " prompt.
IDLE_SCREEN = "\n".join(["output line", "─" * 20, "❯ ", "─" * 20])
BUSY_SCREEN = "\n".join(["working… (esc to interrupt)"])


class FakeAdapter:
    """Records send_line calls; returns a canned screen from get_text."""

    def __init__(self, screen: str = IDLE_SCREEN) -> None:
        self.screen = screen
        self.sent: list[tuple[object, str]] = []
        self._lock = threading.Lock()
        self.sent_event = threading.Event()

    def get_text(self, pane_id, escapes: bool = False) -> str:
        return self.screen

    def send_line(self, pane_id, text: str, settle: float = 0.15) -> None:
        with self._lock:
            self.sent.append((pane_id, text))
        self.sent_event.set()


def _registered(broker: Broker, agent_id: str, pane_id=None):
    token = broker.issue_token(agent_id, agent_id, "worker", pane_id=pane_id)
    broker.register_local(token)
    return broker.get_bind(token)


def test_enqueue_only_to_registered(tmp_path):
    b = Broker(state_dir=tmp_path, adapter=None)
    src = _registered(b, "src")
    # 未登録の宛先には配送しない。
    b.issue_token("dst", "dst", "worker")  # token only, not registered
    res = b.enqueue(src, "dst", "hi")
    assert res["ok"] is False and "peer_not_found" in res["error"]
    # 登録すると配送先になる。
    _registered(b, "dst2")
    assert b.enqueue(src, "dst2", "hi")["ok"] is True


def test_enqueue_matches_by_name(tmp_path):
    b = Broker(state_dir=tmp_path, adapter=None)
    src = _registered(b, "src")
    token = b.issue_token("dst-id", "dst-name", "worker")
    b.register_local(token)
    assert b.enqueue(src, "dst-name", "via name")["delivered_to"] == "dst-id"


def test_drain_is_at_most_once(tmp_path):
    b = Broker(state_dir=tmp_path, adapter=None)
    src = _registered(b, "src")
    dst = _registered(b, "dst")
    b.enqueue(src, "dst", "m1")
    b.enqueue(src, "dst", "m2")
    msgs = b.drain(dst)
    assert [m["message"] for m in msgs] == ["m1", "m2"]
    assert b.drain(dst) == []


def test_nudge_injected_once_when_idle(tmp_path):
    adapter = FakeAdapter(IDLE_SCREEN)
    b = Broker(state_dir=tmp_path, adapter=adapter, nudge_defer_interval=0.01)
    src = _registered(b, "src")
    _registered(b, "dst", pane_id="%9")
    b.enqueue(src, "dst", "hello")
    assert adapter.sent_event.wait(timeout=5.0)
    # 定型 1 行のみ注入される (本文は通さない)。
    assert len(adapter.sent) == 1
    pane_id, text = adapter.sent[0]
    assert pane_id == "%9"
    assert "check_messages" in text


def test_nudge_skips_when_no_pane(tmp_path):
    adapter = FakeAdapter(IDLE_SCREEN)
    b = Broker(state_dir=tmp_path, adapter=adapter)
    src = _registered(b, "src")
    _registered(b, "dst")  # no pane_id -> no nudge
    b.enqueue(src, "dst", "hello")
    time.sleep(0.2)
    assert adapter.sent == []


def test_nudge_single_flight_under_concurrent_sends(tmp_path):
    # 同一宛先への並行 send で nudge worker が二重起動しないこと (round 3 Major)。
    # busy 画面で worker を defer ループに留め、その間に複数 send しても
    # 配達スレッドは 1 本に冪等化される。
    adapter = FakeAdapter(BUSY_SCREEN)
    b = Broker(state_dir=tmp_path, adapter=adapter,
               nudge_defer_interval=0.05, nudge_defer_max_tries=3)
    src = _registered(b, "src")
    _registered(b, "dst", pane_id="%1")
    threads = [threading.Thread(target=b.enqueue, args=(src, "dst", f"m{i}"))
               for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # busy のまま defer 上限に達するので send_line は呼ばれない。
    # 起動された nudge スレッドは 1 本だけ (= 二重注入の起点が単一)。
    with b._lock:
        assert len(b._nudge_threads) == 1
    assert adapter.sent == []
