# -*- coding: utf-8 -*-
"""Contract Set C amendment: .state/broker/queue.jsonl schema.

Validates the bundled ``broker_queue_event.schema.json`` against journal
lines actually emitted by the broker, and asserts the intentional ``ts``
type divergence from ``journal_event`` (float epoch vs ISO8601 string).
"""

from __future__ import annotations

import json

import jsonschema
import pytest

from claude_org_runtime.schema import (
    broker_queue_event_schema,
    journal_event_schema,
)
from claude_org_runtime.schema.json_schema import broker_queue_event_schema as _direct


def test_schema_is_bundled_and_parsable():
    schema = broker_queue_event_schema()
    assert schema["title"] == "BrokerQueueEvent"
    assert schema is not _direct()  # fresh dict each load (not shared mutable)
    jsonschema.Draft202012Validator.check_schema(schema)


def test_ts_is_number_not_string():
    # Intentional divergence from journal_event (ISO8601 string).
    assert broker_queue_event_schema()["properties"]["ts"]["type"] == "number"
    assert journal_event_schema()["properties"]["ts"]["type"] == "string"


def test_real_broker_journal_validates(tmp_path):
    # Drive a broker through token issue + enqueue + drain, then validate
    # every emitted queue.jsonl line against the Set C schema.
    from claude_org_runtime.broker.server import Broker

    b = Broker(state_dir=tmp_path / "broker", adapter=None)
    tok = b.issue_token("src", "src", "worker")
    b.register_local(tok)
    tok2 = b.issue_token("dst", "dst", "worker")
    b.register_local(tok2)
    b.enqueue(b.get_bind(tok), "dst", "hi")
    b.drain(b.get_bind(tok2))

    schema = broker_queue_event_schema()
    validator = jsonschema.Draft202012Validator(schema)
    lines = (tmp_path / "broker" / "queue.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert lines
    for ln in lines:
        validator.validate(json.loads(ln))
