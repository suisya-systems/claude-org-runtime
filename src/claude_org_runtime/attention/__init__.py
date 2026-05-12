"""Attention scan/watch surface for ``claude-org-runtime`` (Issue #19/#20).

The ``attention`` subpackage classifies events from ``.state/state.db``
and ``.state/pending_decisions.json`` into human-required notifications
(approval blocked, CI failed, pending decision, etc.) and dispatches
desktop notifications + terminal-bell fallback. See
``docs/design/attention-notification.md`` (claude-org-ja) §5 / §6 for the
authoritative requirements.

Public re-exports keep the import surface stable for ja consumers:

* :class:`AttentionEvent` — normalized notification record
* :class:`AttentionConfig` — config dataclass (Issue #19 + #20)
* :func:`classify_all` — pure classifier (events + pending → events)
* :func:`notify` — OS dispatch (subprocess + bell + stdout log)
"""

from __future__ import annotations

from . import notify as _notify_module  # keep submodule accessible
from .classifier import AttentionEvent, classify_all, classify_event, classify_pending
from .config import AttentionConfig, Template, load_config
from .notify import FormattedNotification, render_text
from .notify import notify as send_notification

# Re-exporting the function as a different name avoids the submodule
# being shadowed by a same-named function attribute on the package
# (which broke ``monkeypatch.setattr('...attention.notify.xxx')`` paths).
notify = _notify_module

__all__ = [
    "AttentionConfig",
    "AttentionEvent",
    "FormattedNotification",
    "Template",
    "classify_all",
    "classify_event",
    "classify_pending",
    "load_config",
    "notify",
    "render_text",
    "send_notification",
]
