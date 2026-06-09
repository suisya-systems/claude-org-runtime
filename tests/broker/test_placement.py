# -*- coding: utf-8 -*-
"""Tests for the broker -> dispatcher.choose_split one-way reuse boundary.

Asserts the import contract (broker reuses the runtime's choose_split rather
than reimplementing balanced split) and that the thin
``choose_pane_split`` wrapper delegates faithfully.
"""

from __future__ import annotations

from claude_org_runtime.broker import choose_pane_split as pkg_choose_pane_split
from claude_org_runtime.broker import placement
from claude_org_runtime.dispatcher.runner import SplitChoice
from claude_org_runtime.dispatcher.runner import choose_split as runner_choose_split


def test_import_contract_is_one_way():
    # broker.placement reuses the runtime's choose_split (no reimplementation).
    assert placement.choose_split is runner_choose_split
    assert placement.choose_pane_split is pkg_choose_pane_split


def test_choose_pane_split_picks_dispatcher_target():
    # A wide dispatcher is the primary balanced-split target.
    panes = [
        {"id": 1, "name": "secretary", "role": "secretary",
         "x": 0, "y": 0, "width": 200, "height": 50, "focused": False},
        {"id": 2, "name": "dispatcher", "role": "dispatcher",
         "x": 200, "y": 0, "width": 200, "height": 50, "focused": True},
    ]
    choice = placement.choose_pane_split(panes)
    assert isinstance(choice, SplitChoice)
    assert choice.role == "dispatcher"
    assert choice.target_id == 2
    assert choice.direction == "vertical"


def test_choose_pane_split_returns_none_when_no_candidate():
    # A single tiny pane below the floors yields no split candidate.
    panes = [
        {"id": 1, "name": "worker", "role": "worker",
         "x": 0, "y": 0, "width": 10, "height": 4, "focused": True},
    ]
    assert placement.choose_pane_split(panes) is None


def test_choose_pane_split_matches_runner_on_parsed_panes():
    panes = [
        {"id": 7, "name": "worker-a", "role": "worker",
         "x": 0, "y": 0, "width": 160, "height": 48, "focused": False},
    ]
    parsed = [placement.Pane.from_dict(p) for p in panes]
    assert placement.choose_pane_split(panes) == runner_choose_split(parsed)
