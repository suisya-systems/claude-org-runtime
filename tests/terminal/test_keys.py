# -*- coding: utf-8 -*-
"""Tests for the canonical raw-key vocabulary (``terminal.keys``).

These pin the single-source-of-truth invariants the whole send_keys pipeline
relies on (Issue #108): every alias folds to a canonical key, canonical keys
match exactly what the backend maps declare, and normalization is
case/whitespace-insensitive.
"""

from __future__ import annotations

import pytest

from claude_org_runtime.terminal.herdr import _HERDR_KEY_MAP
from claude_org_runtime.terminal.keys import (
    _ALIAS_TO_CANONICAL,
    ACCEPTED_KEY_NAMES,
    CANONICAL_KEYS,
    normalize_key,
)
from claude_org_runtime.terminal.tmux import _TMUX_KEY_MAP


def test_alias_values_are_all_canonical() -> None:
    # Every alias must fold to a member of the canonical set, and together the
    # aliases must cover the whole canonical set (no orphan canonical keys).
    assert set(_ALIAS_TO_CANONICAL.values()) == CANONICAL_KEYS


def test_accepted_names_is_alias_domain() -> None:
    assert ACCEPTED_KEY_NAMES == frozenset(_ALIAS_TO_CANONICAL)


@pytest.mark.parametrize(
    "raw,canonical",
    [
        ("Enter", "enter"),
        ("return", "enter"),
        ("Shift+Tab", "backtab"),
        ("BackTab", "backtab"),
        ("Escape", "esc"),
        ("ESC", "esc"),
        ("Del", "delete"),
        ("  PageUp  ", "pageup"),
        ("Ctrl+A", "ctrl+a"),
        ("ctrl+z", "ctrl+z"),
    ],
)
def test_normalize_folds_aliases(raw: str, canonical: str) -> None:
    assert normalize_key(raw) == canonical


@pytest.mark.parametrize("bad", ["hyper+z", "f13", "", "ctrl+1", "ctrl+ab"])
def test_normalize_unknown_returns_none(bad: str) -> None:
    assert normalize_key(bad) is None


def test_backend_maps_are_subsets_of_canonical() -> None:
    # Every backend-map key must be a real canonical key (an extra key would be
    # an un-declared token; drift guard). tmux claims the FULL vocabulary; Herdr
    # is a measured strict subset (no Delete/Home/End/PageUp/PageDown).
    assert set(_TMUX_KEY_MAP) == CANONICAL_KEYS
    assert set(_HERDR_KEY_MAP) < CANONICAL_KEYS
    assert set(_HERDR_KEY_MAP) <= CANONICAL_KEYS
    assert CANONICAL_KEYS - set(_HERDR_KEY_MAP) == {
        "delete", "home", "end", "pageup", "pagedown",
    }
