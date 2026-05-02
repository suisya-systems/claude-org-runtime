"""Schema migration scripts for ``.state/`` artefacts.

Public surface (re-exported from :mod:`v1_to_v2`):

- :func:`migrate_journal_event` -- migrate a single ``journal.jsonl``
  event dict.
- :func:`migrate_journal_lines` -- migrate a stream of JSONL lines.
- :func:`migrate_org_state_markdown` -- migrate the Worker Directory
  Registry table inside ``org-state.md``.
"""

from .v1_to_v2 import (
    migrate_journal_event,
    migrate_journal_lines,
    migrate_org_state_markdown,
)

__all__ = [
    "migrate_journal_event",
    "migrate_journal_lines",
    "migrate_org_state_markdown",
]
