"""Backend-agnostic terminal adapter subpackage.

Ported from the ``claude-org-transport-lab`` spike (Phase 1-5, verified) per
``docs/design/ja-migration-plan.md`` §4 (runtime extraction). ``broker`` /
``harness`` depend on this subpackage only through the
:class:`~claude_org_runtime.terminal.base.TerminalAdapter` Protocol and the
:func:`~claude_org_runtime.terminal.base.make_adapter` factory.

Dependency direction is one-way: this is a leaf package (stdlib + backend CLI
invocation only). It imports neither ``claude-org-ja`` nor other
``claude_org_runtime`` internals; only ``broker -> terminal`` will hold later.

Importing the package is side-effect free: backend binaries are resolved lazily
at adapter *instantiation* (``find_tmux`` / ``find_wezterm`` run via the
dataclass ``default_factory``), never at import time.
"""

from __future__ import annotations

from .base import (
    NUDGE_TEXT,
    VALID_BACKENDS,
    PaneId,
    PaneRef,
    TerminalAdapter,
    classify_pane_state,
    default_backend,
    make_adapter,
    wait_for_state,
)
from .tmux import TmuxAdapter
from .wezterm import WezTermAdapter

__all__ = [
    "NUDGE_TEXT",
    "VALID_BACKENDS",
    "PaneId",
    "PaneRef",
    "TerminalAdapter",
    "TmuxAdapter",
    "WezTermAdapter",
    "classify_pane_state",
    "default_backend",
    "make_adapter",
    "wait_for_state",
]
