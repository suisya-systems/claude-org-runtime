"""Tests for the bundled reference role prompts."""

from __future__ import annotations

import pytest

from claude_org_runtime.prompts import available_roles, load, load_meta

# Sanity-check threshold: catches empty/truncated templates without
# pinning a specific length. The dispatcher prompt is several hundred
# lines; the secretary and curator prompts are intentionally short
# because their source counterparts are short and we do not want to
# pad them with invented content.
_MIN_LINES = 40


@pytest.mark.parametrize("role", available_roles())
def test_load_returns_non_empty_markdown(role: str) -> None:
    text = load(role)
    # Tolerate either LF or CRLF line endings (Windows checkouts may
    # rewrite the bundled markdown depending on the user's git config).
    head = text.split("\n", 1)[0].rstrip("\r")
    assert head == "---", "frontmatter block must lead the file"
    line_count = text.count("\n")
    assert line_count >= _MIN_LINES, (
        f"prompt {role!r} has only {line_count} lines (< {_MIN_LINES})"
    )


@pytest.mark.parametrize("role", available_roles())
def test_load_meta_parses_frontmatter(role: str) -> None:
    meta = load_meta(role)
    assert meta["role"] == role
    assert meta["source"].startswith("claude-org-ja@")
    assert meta["status"].startswith("reference")


def test_load_rejects_unknown_role() -> None:
    with pytest.raises(ValueError):
        load("unknown")  # type: ignore[arg-type]


def test_available_roles_is_stable() -> None:
    assert available_roles() == ("secretary", "dispatcher", "curator")
