"""Bundled JSON Schema files (Draft 2020-12).

Helpers load the schemas as parsed dicts so callers do not need to know
their on-disk paths.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_DIR = Path(__file__).parent


def _load(name: str) -> dict[str, Any]:
    with (_DIR / name).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def journal_event_schema() -> dict[str, Any]:
    """Return the parsed JSON Schema for :class:`JournalEvent`."""

    return _load("journal_event.schema.json")


def worker_dir_entry_schema() -> dict[str, Any]:
    """Return the parsed JSON Schema for :class:`WorkerDirEntry`."""

    return _load("worker_dir_entry.schema.json")


def broker_queue_event_schema() -> dict[str, Any]:
    """Return the parsed JSON Schema for a ``.state/broker/queue.jsonl`` line.

    Contract Set C amendment for ``.state/broker/`` (the org-broker journal).
    Unlike :func:`journal_event_schema`, ``ts`` is a float epoch (the broker
    writes ``time.time()``), not an ISO8601 string.
    """

    return _load("broker_queue_event.schema.json")


__all__ = [
    "broker_queue_event_schema",
    "journal_event_schema",
    "worker_dir_entry_schema",
]
