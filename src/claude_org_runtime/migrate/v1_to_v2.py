"""Migrate ``.state/`` artefacts from the v1 (claude-org-ja) layout to v2.

Posture is **polymorphic** (Q4=c + measurement-worker recommendation):
legacy keys are not stripped, the canonical key is added alongside. This
keeps the migration non-destructive so consumers can flip over at their
own pace.

Drift axes resolved here (per measurement 2026-05-02):

- ``worker`` (opaque string) -> also written as ``task_id``.
- ``pane`` (polymorphic int|str) -> copied to ``pane_id`` if numeric,
  otherwise to ``pane_name``.
- ``dir`` -> also written as ``worker_dir``.
- ``worker_pane_closed`` event remains its own type (kept separate from
  ``pane_closed`` because the dispatcher emits it under a distinct code
  path -- consolidation is a downstream policy decision, not a migration
  concern).
- Any unknown event name -> ``event=misc`` with the original tag preserved
  on ``original_event``.

CLI:

    python -m claude_org_runtime.migrate.v1_to_v2 --in IN --out OUT [--kind journal|org_state]

If ``--kind`` is omitted the kind is inferred from the input suffix
(``.jsonl`` -> journal, ``.md`` -> org_state).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, Iterable

from ..schema.enums import JournalEventType


def migrate_journal_event(event: dict[str, Any]) -> dict[str, Any]:
    """Return a new dict with canonical keys added alongside legacy ones.

    The input is not mutated.
    """

    out = dict(event)

    # task_id canonicalisation. Preference order, per measurement 2026-05-02
    # section 3.2: a structural slug (``task``) takes priority over an opaque
    # instance handle (``worker``). Both legacy keys are kept verbatim.
    if "task_id" not in out:
        if isinstance(out.get("task"), str):
            out["task_id"] = out["task"]
        elif "worker" in out:
            out["task_id"] = out["worker"]
    if "worker" not in out and "task_id" in out:
        out["worker"] = out["task_id"]

    # pane (polymorphic) -> pane_id / pane_name
    if "pane" in out:
        pane_val = out["pane"]
        if isinstance(pane_val, bool):
            # bool is a subclass of int; treat it as opaque, leave alone
            pass
        elif isinstance(pane_val, int):
            out.setdefault("pane_id", pane_val)
        elif isinstance(pane_val, str):
            if pane_val.lstrip("-").isdigit():
                out.setdefault("pane_id", int(pane_val))
            else:
                out.setdefault("pane_name", pane_val)

    # dir <-> worker_dir
    if "dir" in out and "worker_dir" not in out:
        out["worker_dir"] = out["dir"]
    elif "worker_dir" in out and "dir" not in out:
        out["dir"] = out["worker_dir"]

    # event normalisation
    raw_event = out.get("event")
    if isinstance(raw_event, str):
        try:
            JournalEventType(raw_event)
        except ValueError:
            out["original_event"] = raw_event
            out["event"] = JournalEventType.MISC.value

    return out


def migrate_journal_lines(lines: Iterable[str]) -> Iterable[str]:
    """Yield migrated JSONL lines for an iterable of v1 JSONL lines.

    Blank lines are preserved as blank; lines that fail to parse as JSON
    are passed through unchanged so the migration never destroys data.
    """

    for line in lines:
        stripped = line.rstrip("\n")
        if not stripped.strip():
            yield ""
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            yield stripped
            continue
        if not isinstance(obj, dict):
            yield stripped
            continue
        migrated = migrate_journal_event(obj)
        yield json.dumps(migrated, ensure_ascii=False, sort_keys=True)


def migrate_org_state_markdown(markdown: str) -> str:
    """Rewrite the Worker Directory Registry table header in-place.

    Adds canonical column names alongside the legacy ones rather than
    replacing them, so downstream tooling that still reads ``worker`` or
    ``dir`` keeps working. Body cells are duplicated to fill the new
    columns.

    If no Registry table is found the input is returned unchanged.
    """

    lines = markdown.splitlines(keepends=False)
    out_lines: list[str] = []
    i = 0
    rewrote = False
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if (
            not rewrote
            and stripped.startswith("|")
            and i + 1 < len(lines)
            and _is_separator_line(lines[i + 1])
        ):
            header_cells = _split_row(stripped)
            lower = [c.strip().lower() for c in header_cells]
            if "worker" in lower or "task_id" in lower:
                new_header, new_sep, mapping = _augment_header(header_cells)
                out_lines.append(new_header)
                out_lines.append(new_sep)
                i += 2
                while i < len(lines) and lines[i].strip().startswith("|"):
                    body_cells = _split_row(lines[i].strip())
                    out_lines.append(_augment_row(body_cells, mapping))
                    i += 1
                rewrote = True
                continue
        out_lines.append(line)
        i += 1
    suffix = "\n" if markdown.endswith("\n") else ""
    return "\n".join(out_lines) + suffix


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_separator_line(line: str) -> bool:
    cells = _split_row(line)
    if not cells:
        return False
    return all(re.fullmatch(r":?-{3,}:?", c) for c in cells if c)


_PANE_NUM_RE = re.compile(r"^-?\d+$")


def _copy(value: str) -> str:
    return value


def _pane_id_only(value: str) -> str:
    return value if _PANE_NUM_RE.match(value) else ""


def _pane_name_only(value: str) -> str:
    if not value or value == "-":
        return ""
    return "" if _PANE_NUM_RE.match(value) else value


# Per-output-column instruction: (header_label, source_index, value_transform)
_ColumnSpec = tuple[str, int, Callable[[str], str]]


def _augment_header(cells: list[str]) -> tuple[str, str, list[_ColumnSpec]]:
    """Plan the rewritten Worker Directory Registry header.

    For each legacy column we keep the original column verbatim and append
    the canonical column(s) it maps to (when not already present). The
    polymorphic ``pane`` column expands into both ``pane_id`` and
    ``pane_name``; per-row, the value is routed to whichever output column
    matches the value's type.
    """

    seen: set[str] = {c.strip().lower() for c in cells}
    plan: list[_ColumnSpec] = []
    for idx, cell in enumerate(cells):
        label = cell.strip()
        lower = label.lower()
        plan.append((label, idx, _copy))
        if lower == "worker" and "task_id" not in seen:
            plan.append(("task_id", idx, _copy))
            seen.add("task_id")
        elif lower == "dir" and "worker_dir" not in seen:
            plan.append(("worker_dir", idx, _copy))
            seen.add("worker_dir")
        elif lower == "pane":
            if "pane_id" not in seen:
                plan.append(("pane_id", idx, _pane_id_only))
                seen.add("pane_id")
            if "pane_name" not in seen:
                plan.append(("pane_name", idx, _pane_name_only))
                seen.add("pane_name")
    header = "| " + " | ".join(label for label, _, _ in plan) + " |"
    separator = "| " + " | ".join("---" for _ in plan) + " |"
    return header, separator, plan


def _augment_row(cells: list[str], plan: list[_ColumnSpec]) -> str:
    out: list[str] = []
    for _, src, transform in plan:
        raw = cells[src] if src < len(cells) else ""
        out.append(transform(raw))
    return "| " + " | ".join(out) + " |"


def _detect_kind(path: Path, override: str | None) -> str:
    if override:
        return override
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        return "journal"
    if suffix == ".md":
        return "org_state"
    raise SystemExit(
        f"cannot infer migration kind from suffix {suffix!r}; pass --kind"
    )


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Attach the v1->v2 migrate flags to an existing parser.

    Used by both the standalone module CLI (``python -m
    claude_org_runtime.migrate.v1_to_v2``) and the unified
    ``claude-org-runtime migrate v1-to-v2`` entry point.
    """
    parser.add_argument("--in", dest="src", required=True, help="input file")
    parser.add_argument("--out", dest="dst", required=True, help="output file")
    parser.add_argument(
        "--kind",
        choices=("journal", "org_state"),
        default=None,
        help="explicit kind; inferred from suffix when omitted",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude_org_runtime.migrate.v1_to_v2",
        description="Migrate .state/ artefacts from claude-org-ja v1 to v2 schema.",
    )
    add_arguments(parser)
    return parser


def run(args: argparse.Namespace) -> int:
    src = Path(args.src)
    dst = Path(args.dst)
    kind = _detect_kind(src, args.kind)

    if kind == "journal":
        with src.open("r", encoding="utf-8") as fh:
            migrated = list(migrate_journal_lines(fh))
        dst.parent.mkdir(parents=True, exist_ok=True)
        with dst.open("w", encoding="utf-8") as fh:
            for line in migrated:
                fh.write(line + "\n")
    else:
        text = src.read_text(encoding="utf-8")
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(migrate_org_state_markdown(text), encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
