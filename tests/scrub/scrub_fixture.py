"""Scrub PII / secrets from claude-org-ja `.state/` snapshots before committing
them as test fixtures in claude-org-runtime.

Implements the four Q-Scrub decisions from
claude-org-ja#208 (2026-05-02 comment):

* Q-Scrub-1 (what): URLs, emails, API keys, session narratives
  (long H2 blocks in `org-state.md`), and free-text `note` fields in
  `journal.jsonl`. Structural identifiers (`task_id`, `event`, `ts`,
  `pane_id`, `pane_name`, status fields) are preserved verbatim.
* Q-Scrub-2 (how): hybrid -- this script performs the deterministic
  pass; a human Lead reviews the diff before commit.
* Q-Scrub-3 (count): out of scope for this script. The Lead curates
  3-4 situational fixtures separately.
* Q-Scrub-4 (placement): in-repo under `tests/fixtures/`, no LFS.

Input format is auto-detected by file extension:

* ``.jsonl`` -> parsed line by line; regex scrubbing is applied to
  string values only, and structural fields are skipped. ``note``
  fields with `len(value) >= 50` are replaced wholesale with
  ``[NOTE REDACTED]``.
* ``.md`` (or anything else) -> treated as plain text. H2 blocks
  whose heading matches a session-narrative shape are replaced with
  ``[SESSION NARRATIVE REDACTED]``; remaining text is passed through
  the regex scrubbers.

Usage::

    python -m tests.scrub.scrub_fixture --in <path> --out <path> [--diff]
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

URL_RE = re.compile(r"https?://[^\s\"']+")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# Common high-entropy secret shapes. Order matters: more specific first.
API_KEY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"ghp_[A-Za-z0-9]{36}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gh[oushr]_[A-Za-z0-9]{36}"),
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
)

URL_REPLACEMENT = "https://example.com/REDACTED"
EMAIL_REPLACEMENT = "redacted@example.com"
KEY_REPLACEMENT = "[REDACTED_KEY]"
NOTE_REPLACEMENT = "[NOTE REDACTED]"
SESSION_REPLACEMENT = "[SESSION NARRATIVE REDACTED]"

# Fields that must never be modified -- they are stable identifiers
# the migrate-script tests rely on.
PRESERVED_FIELDS = frozenset(
    {
        "task_id",
        "event",
        "ts",
        "pane_id",
        "pane_name",
        "status",
        "state",
    }
)

NOTE_REDACT_THRESHOLD = 50

SESSION_HEADING_RE = re.compile(
    r"^##\s+\d{4}-\d{2}-\d{2}.*$",
    re.MULTILINE,
)


def scrub_text(value: str) -> str:
    """Apply the regex-based scrubbers to a free-text string."""
    out = URL_RE.sub(URL_REPLACEMENT, value)
    out = EMAIL_RE.sub(EMAIL_REPLACEMENT, out)
    for pattern in API_KEY_PATTERNS:
        out = pattern.sub(KEY_REPLACEMENT, out)
    return out


def _scrub_json_value(value: Any, key: str | None = None) -> Any:
    if key in PRESERVED_FIELDS:
        return value
    if isinstance(value, str):
        if key == "note" and len(value) >= NOTE_REDACT_THRESHOLD:
            return NOTE_REPLACEMENT
        return scrub_text(value)
    if isinstance(value, dict):
        return {k: _scrub_json_value(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_json_value(v, key) for v in value]
    return value


def scrub_jsonl(text: str) -> str:
    """Scrub a JSONL document, line by line."""
    out_lines: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            out_lines.append(line)
            continue
        record = json.loads(line)
        scrubbed = _scrub_json_value(record)
        out_lines.append(json.dumps(scrubbed, ensure_ascii=False, sort_keys=True))
    trailing_nl = "\n" if text.endswith("\n") else ""
    return "\n".join(out_lines) + trailing_nl


def scrub_markdown(text: str) -> str:
    """Replace session-narrative H2 blocks, then run regex scrubbers."""
    headings = list(SESSION_HEADING_RE.finditer(text))
    if not headings:
        return scrub_text(text)
    pieces: list[str] = []
    cursor = 0
    for idx, match in enumerate(headings):
        pieces.append(scrub_text(text[cursor : match.start()]))
        # Find end of this H2 block: next H2 (any) or EOF.
        next_h2 = re.search(r"^##\s+", text[match.end():], re.MULTILINE)
        block_end = match.end() + next_h2.start() if next_h2 else len(text)
        heading_line = match.group(0)
        pieces.append(f"{heading_line}\n\n{SESSION_REPLACEMENT}\n\n")
        cursor = block_end
    pieces.append(scrub_text(text[cursor:]))
    return "".join(pieces)


def scrub_path(path: Path, text: str) -> str:
    if path.suffix.lower() == ".jsonl":
        return scrub_jsonl(text)
    return scrub_markdown(text)


def _summarize_diff(before: str, after: str, label: str) -> str:
    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"{label} (before)",
        tofile=f"{label} (after)",
        n=2,
    )
    return "".join(diff)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scrub_fixture",
        description="Scrub PII/secrets from a .state snapshot for fixture use.",
    )
    parser.add_argument("--in", dest="in_path", required=True, type=Path)
    parser.add_argument("--out", dest="out_path", required=True, type=Path)
    parser.add_argument(
        "--diff",
        action="store_true",
        help="Print a unified diff of the scrub to stdout.",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    original = args.in_path.read_text(encoding="utf-8")
    scrubbed = scrub_path(args.in_path, original)

    args.out_path.parent.mkdir(parents=True, exist_ok=True)
    args.out_path.write_text(scrubbed, encoding="utf-8")

    if args.diff:
        sys.stdout.write(_summarize_diff(original, scrubbed, str(args.in_path)))
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
