"""Frozen-dataclass mirror of a ``journal.jsonl`` record.

We deliberately avoid ``pydantic`` / ``msgspec`` here so the runtime keeps
zero non-stdlib runtime dependencies (the only added dep, ``jsonschema``,
is opt-in via :mod:`json_schema` validation paths). Round-trip is provided
through :meth:`JournalEvent.from_dict` and :meth:`JournalEvent.to_dict`.

Forward compatibility: any payload key the dataclass does not name
explicitly is collected into :attr:`JournalEvent.extra`. ``to_dict`` re-emits
``extra`` keys at the top level, so legacy payloads round-trip losslessly.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, Mapping

from .enums import JournalEventType

# Payload keys the dataclass surfaces as named attributes; anything else
# observed in journals lands in ``extra`` (forward-compat catch-all).
_KNOWN_KEYS: frozenset[str] = frozenset(
    {
        "ts",
        "event",
        # canonical identifiers
        "task_id",
        "pane_id",
        "pane_name",
        "worker_dir",
        # legacy aliases preserved verbatim under polymorphic posture
        "worker",
        "pane",
        "dir",
        # commonly-observed payload fields across journals
        "task",
        "status",
        "reason",
        "note",
        "kind",
        "confidence",
        "matched",
        "cursor",
        "source",
        "model",
        "pattern",
        "permission_mode",
        "peer",
        "commit",
        "commits",
        "result",
        "pr",
        "repo",
        "duration_sec",
        "active_workers",
        "pending_items",
        "pane_closed",
        "worktree_removed",
        # set by the migrate script when ``event`` falls back to MISC
        "original_event",
    }
)


@dataclass(frozen=True)
class JournalEvent:
    """One ``journal.jsonl`` line.

    Only ``ts`` and ``event`` are required. Every other named field is
    optional, mirroring the payload-key matrix from the 2026-05-02
    measurement (see ``measurement-2026-05-02.md`` section 3.1).
    """

    ts: str
    event: JournalEventType

    # Canonical identifiers (Step B)
    task_id: str | None = None
    pane_id: int | None = None
    pane_name: str | None = None
    worker_dir: str | None = None

    # Legacy aliases (kept under polymorphic posture; migrate writes both)
    worker: str | None = None
    pane: Any | None = None
    dir: str | None = None

    # Common payload fields
    task: str | None = None
    status: str | None = None
    reason: str | None = None
    note: str | None = None
    kind: str | None = None
    confidence: float | None = None
    matched: str | None = None
    cursor: str | None = None
    source: str | None = None
    model: str | None = None
    pattern: str | None = None
    permission_mode: str | None = None
    peer: str | None = None
    commit: str | None = None
    commits: list[str] | None = None
    result: str | None = None
    pr: int | None = None
    repo: str | None = None
    duration_sec: int | None = None
    active_workers: list[Any] | None = None
    pending_items: list[Any] | None = None
    pane_closed: bool | None = None
    worktree_removed: bool | None = None

    # Set when ``event`` is MISC: preserves the original event name.
    original_event: str | None = None

    # Forward-compatibility bucket for keys not yet promoted to a field.
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "JournalEvent":
        """Construct from a parsed JSON object.

        Unknown ``event`` strings fall back to :data:`JournalEventType.MISC`
        with the original string preserved on ``original_event``. Unknown
        payload keys land in ``extra``.
        """

        if "ts" not in data or "event" not in data:
            raise ValueError("JournalEvent requires 'ts' and 'event' keys")

        raw_event = data["event"]
        try:
            event = JournalEventType(raw_event)
            original_event = data.get("original_event")
        except ValueError:
            event = JournalEventType.MISC
            original_event = raw_event

        kwargs: dict[str, Any] = {"ts": data["ts"], "event": event}
        if original_event is not None:
            kwargs["original_event"] = original_event

        extra: dict[str, Any] = {}
        for key, value in data.items():
            if key in {"ts", "event", "original_event"}:
                continue
            if key in _KNOWN_KEYS:
                kwargs[key] = value
            else:
                extra[key] = value
        if extra:
            kwargs["extra"] = extra
        return cls(**kwargs)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict suitable for ``json.dumps``.

        ``None`` fields are omitted; ``extra`` keys are flattened back to
        the top level so the round-trip ``from_dict(to_dict(x)) == x``
        holds for inputs originally produced by :meth:`from_dict`.
        """

        out: dict[str, Any] = {"ts": self.ts, "event": self.event.value}
        for f in fields(self):
            if f.name in {"ts", "event", "extra"}:
                continue
            value = getattr(self, f.name)
            if value is None:
                continue
            out[f.name] = value
        for key, value in self.extra.items():
            out[key] = value
        return out
