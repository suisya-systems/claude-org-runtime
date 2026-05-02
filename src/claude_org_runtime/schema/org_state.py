"""Parser for the ``org-state.md`` Worker Directory Registry table.

The Registry is rendered as a GitHub-flavoured markdown table whose column
set has drifted over time -- the parser accepts both the legacy column
names (``worker``, ``pane``, ``dir``) and the canonical ones (``task_id``,
``pane_id`` / ``pane_name``, ``worker_dir``) so that step B's migrate is
non-breaking under polymorphic posture.

The first table containing a ``task_id`` *or* ``worker`` header column is
treated as the Registry; trailing tables (e.g. retro tables) are skipped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .enums import WorkerStatus

# Header tokens recognised under polymorphic posture; the canonical form
# is the dict key, all alias spellings live in the value tuple.
_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "task_id": ("task_id", "worker", "task"),
    "pane_id": ("pane_id",),
    "pane_name": ("pane_name", "pane"),
    "worker_dir": ("worker_dir", "dir"),
    "status": ("status",),
    "note": ("note", "notes"),
}

_INT_RE = re.compile(r"^-?\d+$")


@dataclass(frozen=True)
class WorkerDirEntry:
    """One row of the Worker Directory Registry table.

    Both the canonical and the legacy keys are populated when present in
    the input, matching the polymorphic migration posture (Q4=c, measurement
    worker recommendation): consumers can read either form during the
    transition.
    """

    task_id: str | None = None
    pane_id: int | None = None
    pane_name: str | None = None
    worker_dir: str | None = None
    status: WorkerStatus | None = None
    note: str | None = None

    # Legacy aliases preserved verbatim
    worker: str | None = None
    pane: str | None = None
    dir: str | None = None

    extra: dict[str, Any] = field(default_factory=dict)


def _split_row(line: str) -> list[str]:
    parts = [c.strip() for c in line.strip().strip("|").split("|")]
    return parts


def _is_separator(cells: list[str]) -> bool:
    return all(re.fullmatch(r":?-{3,}:?", c) for c in cells if c)


def _normalise_header(cell: str) -> str | None:
    token = cell.strip().lower().replace(" ", "_")
    for canonical, aliases in _HEADER_ALIASES.items():
        if token in aliases:
            return canonical
    return None


def parse_worker_directory_registry(markdown: str) -> list[WorkerDirEntry]:
    """Parse the first Registry table found in ``markdown``.

    Returns an empty list if no table with a recognisable identifier
    column (``task_id`` / ``worker`` / ``task``) is found.
    """

    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("|") and "|" in line[1:]:
            header_cells = _split_row(line)
            if i + 1 < len(lines):
                sep_cells = _split_row(lines[i + 1])
                if _is_separator(sep_cells) and len(sep_cells) == len(header_cells):
                    canonical = [_normalise_header(c) for c in header_cells]
                    if any(h in {"task_id"} for h in canonical):
                        return _parse_rows(canonical, header_cells, lines[i + 2 :])
        i += 1
    return []


def _parse_rows(
    canonical: list[str | None],
    raw_headers: list[str],
    body: list[str],
) -> list[WorkerDirEntry]:
    out: list[WorkerDirEntry] = []
    for raw in body:
        line = raw.strip()
        if not line.startswith("|"):
            break
        cells = _split_row(line)
        if len(cells) != len(canonical):
            continue
        kwargs: dict[str, Any] = {}
        extra: dict[str, Any] = {}
        legacy_pane_seen = False
        for header_canonical, header_raw, value in zip(canonical, raw_headers, cells):
            value = value.strip()
            if not value or value == "-":
                continue
            if header_canonical is None:
                extra[header_raw.strip()] = value
                continue
            if header_canonical == "task_id":
                kwargs["task_id"] = value
                if header_raw.strip().lower() == "worker":
                    kwargs["worker"] = value
            elif header_canonical == "pane_id":
                if _INT_RE.match(value):
                    kwargs["pane_id"] = int(value)
                else:
                    kwargs.setdefault("pane_name", value)
            elif header_canonical == "pane_name":
                # legacy ``pane`` may carry an int (then -> pane_id) or str
                if header_raw.strip().lower() == "pane":
                    legacy_pane_seen = True
                    kwargs["pane"] = value
                    if _INT_RE.match(value):
                        kwargs.setdefault("pane_id", int(value))
                    else:
                        kwargs["pane_name"] = value
                else:
                    kwargs["pane_name"] = value
            elif header_canonical == "worker_dir":
                kwargs["worker_dir"] = value
                if header_raw.strip().lower() == "dir":
                    kwargs["dir"] = value
            elif header_canonical == "status":
                try:
                    kwargs["status"] = WorkerStatus(value.lower())
                except ValueError:
                    extra["status_raw"] = value
            elif header_canonical == "note":
                kwargs["note"] = value
        if extra:
            kwargs["extra"] = extra
        if not legacy_pane_seen:
            # nothing to do; kept for future polymorphic-pane handling
            pass
        out.append(WorkerDirEntry(**kwargs))
    return out
