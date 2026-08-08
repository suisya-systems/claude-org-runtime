# -*- coding: utf-8 -*-
"""End-to-end contract: broker detection -> attention notification (Issue #167).

Every other test in this file's neighbourhood stubs one side of the
seam. This one does not: it drives a **real** :class:`Broker` into the
double-claimer condition, then reads the journal it actually wrote with
the attention reader and classifies the result.

That is the regression this issue exists to prevent. Detection has been
in ``store.py`` since Issue #125 and was correct the whole time — it just
had no consumer, so an operator learned about a double sidecar by
noticing that reports had stopped arriving. A rename of the journal
event or of its ``owner`` / ``instances`` fields would silently restore
exactly that state; the unit tests on either side would stay green.
"""

from __future__ import annotations

import time
from pathlib import Path

from claude_org_runtime.attention.classifier import classify_broker_duplicates
from claude_org_runtime.attention.readers import read_broker_duplicates
from claude_org_runtime.broker.server import Broker


def _registered(b: Broker, agent_id: str):
    tok = b.issue_token(agent_id, agent_id, "worker")
    b.register_local(tok)
    return b.get_bind(tok)


def _drive_duplicate(state_dir: Path) -> Broker:
    """Make two sidecar instances poll one owner inside a lease window."""
    b = Broker(state_dir=state_dir, adapter=None, lease_seconds=30.0)
    _registered(b, "secretary")
    cred = b.issue_delivery_cred("secretary")
    first = b.register_delivery_instance(cred, "inst-a")
    second = b.register_delivery_instance(cred, "inst-b")
    # ``inst-a`` is fenced off by the generation bump but still polls —
    # which is precisely the live-double-sidecar shape.
    b.poll_claims(cred, first["generation"], "inst-a")
    b.poll_claims(cred, second["generation"], "inst-b")
    return b


def test_real_broker_duplicate_reaches_the_attention_layer(
    tmp_path: Path,
) -> None:
    b = _drive_duplicate(tmp_path / "broker")

    rows = read_broker_duplicates(
        b.state_dir, now_epoch=time.time(), window_sec=300.0,
    )
    assert len(rows) == 1

    events = classify_broker_duplicates(rows)
    assert len(events) == 1
    ev = events[0]
    assert ev.kind == "duplicate_sidecar"
    assert ev.severity == "urgent"
    # Acceptance: names the owner and enough instance detail to identify
    # which sessions are competing.
    assert ev.worker == "secretary"
    assert ev.summary == "inst-a, inst-b"
    assert "secretary" in ev.body
    assert "inst-a" in ev.body and "inst-b" in ev.body


def test_healthy_single_sidecar_produces_no_attention_event(
    tmp_path: Path,
) -> None:
    """No false positives: the normal deployment stays quiet."""
    b = Broker(state_dir=tmp_path / "broker", adapter=None, lease_seconds=30.0)
    _registered(b, "secretary")
    cred = b.issue_delivery_cred("secretary")
    reg = b.register_delivery_instance(cred, "solo")
    for _ in range(3):
        b.poll_claims(cred, reg["generation"], "solo")

    rows = read_broker_duplicates(
        b.state_dir, now_epoch=time.time(), window_sec=300.0,
    )
    assert rows == []
    assert classify_broker_duplicates(rows) == []


def test_store_cooldown_survives_the_consumer(tmp_path: Path) -> None:
    """Issue #167 asks that the existing per-pair cooldown keep holding.

    The store emits once per instance pair per lease window; repeated
    polls inside that window add no journal lines, and the classifier
    collapses whatever does land into one event per pair.
    """
    b = _drive_duplicate(tmp_path / "broker")
    cred = b.issue_delivery_cred("secretary")
    for _ in range(5):
        b.poll_claims(cred, 1, "inst-a")
        b.poll_claims(cred, 2, "inst-b")

    rows = read_broker_duplicates(
        b.state_dir, now_epoch=time.time(), window_sec=300.0,
    )
    assert len(rows) == 1
    assert len(classify_broker_duplicates(rows)) == 1
