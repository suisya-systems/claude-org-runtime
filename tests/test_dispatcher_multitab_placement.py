"""Tab placement, overflow, plan shape and CLI tests for runtime #158.

Covers design section 10 groups E (tab placement), F (overflow), G (plan
shape / regression) and H (CLI). Group B/C/D (peer parsing, population,
duplicate-name) live in a sibling module; groups A (anti-double-count) and I
(untouched contracts) are pinned elsewhere.

The load-bearing safety properties in here, in the order a reviewer should
read them:

* :func:`test_build_plan_never_emits_tab_key_without_spawn_tab_capability` --
  a ``spawn["tab"]`` key may only exist when the caller EXPLICITLY asserted
  ``--server-capability spawn_tab``. renga's MCP surface cannot be probed for
  its capability list, so omission must fail closed; emitting a tab key
  against a renga 1.4 server gets the whole spawn refused with
  ``[server_too_old]``.
* :func:`test_build_plan_tab_new_omits_target_and_direction` -- for a
  ``tab:{new}`` selector renga forbids ``target`` / ``direction`` at the
  SCHEMA level, so they must be absent KEYS. A JSON ``null`` is present and
  would be rejected.
* :func:`test_build_plan_overflow_applies_fleet_ceiling` (paired with the
  pre-existing ``test_build_plan_renga_ignores_capacity_policy`` in
  ``tests/test_dispatcher_runner.py``) -- the fleet ceiling is scoped to
  overflow mode ONLY, because overflow is the only mode that deletes the rect
  ceiling.
* :func:`test_plan_json_is_serializable_and_cp932_safe` and
  :func:`test_cli_delegate_plan_help_is_cp932_encodable` -- the plan document
  is printed to stdout and ``--help`` is printed to a real console, so a
  single em-dash crashes a cp932 terminal. ``redirect_stdout`` captures UTF-8
  and cannot catch that, which is why the encoding is asserted explicitly.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import time
from pathlib import Path
from typing import Any, Optional

import pytest

from claude_org_runtime.dispatcher.runner import (
    CAP_CALLER_SCOPE,
    CAP_CROSS_TAB_PEERS,
    CAP_SPAWN_TAB,
    MIN_PANE_HEIGHT,
    MIN_PANE_WIDTH,
    RENGA_MAX_TABS,
    TAB_SPAWN_ERROR_CODES,
    WORKER_BIND_WINDOW_SECONDS,
    CapacityPolicy,
    Pane,
    Peer,
    _TAB_SPAWN_DIRECTION,
    _TAB_SPAWN_TARGET,
    build_plan,
    build_parser,
    choose_split,
    count_unbound_reservations,
    derive_tab_awareness,
    main,
    parse_tab_selector,
    validate_tab_selector,
    write_instruction,
    write_worker_seed,
)

# The renga rect escalation exactly as it read before #158, byte for byte
# (git HEAD runner.py:948-951 with task_id "demo"). claude-org-ja forwards this
# text to the secretary verbatim, so #158 is only allowed to APPEND to it --
# test 57 asserts this is still a literal prefix of the emitted message.
_PRE_158_RECT_MESSAGE = (
    "SPLIT_CAPACITY_EXCEEDED: no balanced-split target found for task 'demo'. "
    "The rect-based balanced split's MIN_PANE / adjacency constraints produced "
    "0 candidates. Likely terminal size shortage or unexpected layout -- human "
    "judgment required."
)

# The ActionPlan field set as it stood before #158, in declaration order.
_PRE_158_PLAN_KEYS = [
    "status",
    "task_id",
    "spawn",
    "after_spawn",
    "state_writes",
    "escalate",
    "warnings",
    "errors",
    "capacity",
]

# The spawn dict key order as it stood before #158 (git HEAD runner.py:962-971).
# Order is pinned, not just membership: consumers diff these plans by eye.
_PRE_158_SPAWN_KEYS = [
    "tool",
    "target",
    "direction",
    "name",
    "role",
    "cwd",
    "permission_mode",
    "model",
]

_ALL_CAPS = frozenset({CAP_CALLER_SCOPE, CAP_CROSS_TAB_PEERS, CAP_SPAWN_TAB})
_SPAWN_TAB_ONLY = frozenset({CAP_SPAWN_TAB})


@pytest.fixture(autouse=True)
def _isolate_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run each test from an empty directory.

    ``runner._default_template_repo`` walks ancestors of CWD looking for the
    auto-expand template, and this worktree has ancestors that may or may not
    contain a real one. Pinning CWD to a clean ``tmp_path`` keeps discovery
    deterministic (mirrors ``tests/test_dispatcher_runner.py``).
    """
    monkeypatch.chdir(tmp_path)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _pane(
    pid: int,
    *,
    name: str | None = None,
    role: str | None = None,
    x: int = 0,
    y: int = 0,
    w: int = 200,
    h: int = 50,
    focused: bool = False,
) -> Pane:
    return Pane(
        id=pid, name=name, role=role, focused=focused,
        x=x, y=y, width=w, height=h,
    )


def _peer(
    pid: object,
    *,
    name: str | None = None,
    role: str | None = None,
    tab: int | None = None,
    tab_name: str | None = None,
    same_tab: bool | None = None,
) -> Peer:
    """Build a :class:`Peer` through ``from_dict``, never the constructor.

    ``has_tab_metadata`` is derived from key PRESENCE, which is the entire
    renga-1.4-vs-2.0 discriminator, so a fixture that sets the dataclass field
    by hand would test a shape no transcription can actually produce. Omitted
    keyword args therefore leave the key out of the dict entirely, exactly as
    a renga 1.4 ``list_peers`` transcription does.
    """
    d: dict[str, Any] = {"id": pid}
    if name is not None:
        d["name"] = name
    if role is not None:
        d["role"] = role
    if tab is not None:
        d["tab"] = tab
    if tab_name is not None:
        d["tab_name"] = tab_name
    if same_tab is not None:
        d["same_tab"] = same_tab
    return Peer.from_dict(d)


def _task(tmp_path: Path, task_id: str = "demo") -> dict[str, Any]:
    return {"task_id": task_id, "worker_dir": str(tmp_path), "instruction": "x"}


def _ok_panes() -> list[Pane]:
    """A caller tab choose_split CAN split (it picks the dispatcher)."""
    return [
        _pane(1, name="curator", role="curator", x=0, y=0, w=100, h=50),
        _pane(2, name="dispatcher", role="dispatcher", x=100, y=0, w=200, h=50),
    ]


def _unsplittable_panes() -> list[Pane]:
    """A saturated caller tab whose pane area would still host a whole pane.

    A lone 130x40 secretary: a vertical split leaves 65 < SECRETARY_MIN_WIDTH
    and a horizontal split leaves 20 < SECRETARY_MIN_HEIGHT, so ``_split_options``
    yields nothing and ``choose_split`` returns None. Its measured bbox is
    still 130x40, comfortably over the MIN_PANE_* floors -- which is the exact
    situation ``--overflow-to-new-tab`` exists for: no room to SPLIT, plenty of
    room for a fresh tab's single pane. Placed at x=46 so the measured
    ``left_panels_columns`` is a real (non-zero) number.
    """
    return [_pane(1, name="secretary", role="secretary", x=46, y=1, w=130, h=40)]


def _tiny_panes() -> list[Pane]:
    """A caller tab so small even a whole new-tab pane would not fit.

    bbox 10x2 is below MIN_PANE_WIDTH x MIN_PANE_HEIGHT, so renga would answer
    ``split_refused`` for the new tab too and overflow cannot help.
    """
    return [_pane(1, name="curator", role="curator", x=46, y=1, w=10, h=2)]


def _two_tab_peers() -> list[Peer]:
    """Caller tab 0 "main"; a non-caller tab 1 "build" anchored at pane 11."""
    return [
        _peer(3, name="dispatcher", role="dispatcher",
              tab=0, tab_name="main", same_tab=True),
        _peer(11, name="worker-a", role="worker",
              tab=1, tab_name="build", same_tab=False),
        _peer(17, name="worker-b", role="worker",
              tab=1, tab_name="build", same_tab=False),
    ]


# ---------------------------------------------------------------------------
# E. Tab selector parsing / placement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("pane_id:7", {"pane_id": 7}),
        ("index:2", {"index": 2}),
        ("name:build", {"name": "build"}),
        ("new", {"new": {}}),
        ("new:build", {"new": {"name": "build"}}),
    ],
)
def test_parse_tab_selector_forms(raw: str, expected: dict[str, Any]) -> None:
    got = parse_tab_selector(raw)
    assert got == expected
    # renga's TabSelector is externally tagged: exactly one variant key, and
    # the numeric forms must be real ints (a string "7" addresses nothing).
    assert len(got) == 1
    assert validate_tab_selector(got) is None


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "bogus:1",
        "index:-1",
        "index:abc",
        "name:",
        "new:",
        # No bare-N form on purpose: an unprefixed integer is ambiguous
        # between a tab index and a pane id, and guessing wrong addresses a
        # different tab than the operator meant.
        "7",
    ],
)
def test_parse_tab_selector_rejects(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_tab_selector(raw)


@pytest.mark.parametrize(
    "selector",
    [
        {},
        {"name": "a", "index": 1},
        # bool is an int subclass; {"index": True} must not become tab 1.
        {"index": True},
        {"pane_id": True},
        {"index": -1},
        {"name": ""},
        {"new": {"x": 1}},
        {"new": "build"},
        {"tab": 1},
        "index:1",
        # Whitespace-only labels: renga trims BEFORE its emptiness test
        # (`Some(s) if !s.trim().is_empty()` for tab.name, `v.as_str()
        # .map(str::trim)` for tab.new.name -- src/mcp_peer/mod.rs:1173-1178,
        # :1205-1215), so accepting them here would write a seed file and an
        # instruction file for a spawn renga answers with JSON-RPC -32602.
        # parse_tab_selector's strip() protects the CLI only incidentally; a
        # direct build_plan caller (which claude-org-ja is) has no such shield.
        {"name": " "},
        {"new": {"name": "   "}},
    ],
)
def test_validate_tab_selector_requires_exactly_one_key(selector: Any) -> None:
    err = validate_tab_selector(selector)
    assert err is not None and isinstance(err, str)
    # The message is emitted into a plan that is printed to a cp932 console.
    err.encode("cp932")


@pytest.mark.parametrize(
    "caps,tab,overflow,expect_tab_key",
    [
        # Nothing asserted -- what every pre-#158 caller passes.
        (None, {"new": {}}, False, False),
        (None, None, True, False),
        # An assertion was made, but not the one that authorises a tab key.
        # renga made the three tokens deliberately distinct: a #288-era server
        # advertises caller_scope while STILL dropping cross-tab sends, so
        # neither token may stand in for spawn_tab.
        (frozenset({CAP_CALLER_SCOPE}), {"new": {}}, False, False),
        (frozenset({CAP_CROSS_TAB_PEERS}), {"new": {}}, False, False),
        (frozenset({CAP_CALLER_SCOPE, CAP_CROSS_TAB_PEERS}), None, True, False),
        # Positive control: with the token asserted the key DOES appear, so
        # the negatives above are not passing for some unrelated reason.
        (_SPAWN_TAB_ONLY, {"new": {}}, False, True),
        (_ALL_CAPS, None, True, True),
    ],
)
def test_build_plan_never_emits_tab_key_without_spawn_tab_capability(
    tmp_path: Path,
    caps: Optional[frozenset[str]],
    tab: Optional[dict[str, Any]],
    overflow: bool,
    expect_tab_key: bool,
) -> None:
    # Unsplittable panes so the overflow rows actually reach the overflow
    # decision instead of being demoted back to an in-tab split.
    plan = build_plan(
        _task(tmp_path),
        _unsplittable_panes(),
        tmp_path / ".state",
        transport="renga",
        peers=_two_tab_peers(),
        server_capabilities=caps,
        tab=tab,
        overflow_to_new_tab=overflow,
    )
    spawn = plan.spawn or {}
    assert ("tab" in spawn) is expect_tab_key
    # on_spawn_error is the recovery table for a tab-directed spawn; it must
    # track the tab key exactly, never appear on its own.
    assert (plan.on_spawn_error is not None) is expect_tab_key


def test_build_plan_tab_without_capability_is_server_too_old_preflight(
    tmp_path: Path,
) -> None:
    plan = build_plan(
        _task(tmp_path),
        _ok_panes(),
        tmp_path / ".state",
        transport="renga",
        peers=_two_tab_peers(),
        server_capabilities=None,
        tab={"index": 1},
    )
    assert plan.status == "input_invalid"
    assert plan.spawn is None
    # Leads with renga's own code token so the plan is greppable by the same
    # string renga would have returned.
    assert plan.errors and plan.errors[0].startswith("server_too_old:")
    assert "--server-capability spawn_tab" in plan.errors[0]
    # Pre-flight means BEFORE any side effect: nothing to roll back, and the
    # operator can simply re-run with the flag.
    assert plan.state_writes == []
    assert not (tmp_path / ".state").exists()
    # The refusal is still explained: layout carries the placement diagnostics.
    assert plan.layout is not None
    assert plan.layout["tab_placement"]["error_code"] == "server_too_old"


def test_build_plan_tab_new_omits_target_and_direction(tmp_path: Path) -> None:
    plan = build_plan(
        _task(tmp_path),
        _ok_panes(),
        tmp_path / ".state",
        transport="renga",
        server_capabilities=_SPAWN_TAB_ONLY,
        tab={"new": {"name": "pinned"}},
    )
    assert plan.status == "ready_to_spawn"
    spawn = plan.spawn
    assert spawn is not None
    assert spawn["tab"] == {"new": {"name": "pinned"}}
    # ABSENT KEYS, not None: renga forbids target/direction at the schema
    # level for a tab:{new} selector and a JSON null is present, so the whole
    # request would be rejected.
    assert "target" not in spawn
    assert "direction" not in spawn
    # Belt and braces at the wire level -- an accidental ``None`` would
    # serialize as ``"target": null`` and still be a present key.
    wire = json.dumps(spawn)
    assert '"target"' not in wire
    assert '"direction"' not in wire


def test_build_plan_tab_existing_uses_focused_vertical_and_warns(
    tmp_path: Path,
) -> None:
    plan = build_plan(
        _task(tmp_path),
        _ok_panes(),                 # pane ids 1,2 -- deliberately not 11
        tmp_path / ".state",
        transport="renga",
        peers=_two_tab_peers(),
        server_capabilities=_ALL_CAPS,
        tab={"pane_id": 11},
    )
    assert plan.status == "ready_to_spawn"
    spawn = plan.spawn
    assert spawn is not None
    assert spawn["tab"] == {"pane_id": 11}
    # Peers carry no geometry (renga's PeerInfo has no rect and PeerList skips
    # the rect refresh), so no balanced split can be ranked inside the target
    # tab. "focused" is resolved by renga INSIDE the selected tab, which is
    # also what structurally prevents target_tab_mismatch.
    assert spawn["target"] == _TAB_SPAWN_TARGET == "focused"
    assert spawn["direction"] == _TAB_SPAWN_DIRECTION == "vertical"
    # The lost capability is stated, not silently swallowed.
    assert any(
        "no balanced split can be ranked" in w for w in plan.warnings
    )
    # The in-tab candidate choose_split WOULD have picked is not used.
    assert choose_split(_ok_panes()).target_name == "dispatcher"
    assert spawn["target"] != "dispatcher"


def test_build_plan_canonicalizes_name_selector_to_pane_id(
    tmp_path: Path,
) -> None:
    plan = build_plan(
        _task(tmp_path),
        _ok_panes(),
        tmp_path / ".state",
        transport="renga",
        peers=_two_tab_peers(),
        server_capabilities=_ALL_CAPS,
        tab={"name": "build"},
    )
    assert plan.status == "ready_to_spawn"
    selector = plan.spawn["tab"]
    # renga documents the tab index as DISPLAY metadata that shifts when a tab
    # closes, so an emitted plan must never carry one -- nor a display name
    # that could have been resolved. The anchor is the smallest numeric peer
    # id in the tab, so it does not depend on transcription order.
    assert selector == {"pane_id": 11}
    assert "name" not in selector
    assert "index" not in selector
    reasons = plan.layout["tab_placement"]["reasons"]
    assert any("canonicalised" in r for r in reasons)


def test_build_plan_tab_name_zero_match_is_tab_not_found(
    tmp_path: Path,
) -> None:
    plan = build_plan(
        _task(tmp_path),
        _ok_panes(),
        tmp_path / ".state",
        transport="renga",
        peers=_two_tab_peers(),
        server_capabilities=_ALL_CAPS,
        tab={"name": "nope"},
    )
    assert plan.status == "input_invalid"
    assert plan.spawn is None
    assert plan.errors[0].startswith("tab_not_found:")
    assert plan.state_writes == []
    assert not (tmp_path / ".state").exists()


def test_build_plan_tab_name_two_matches_is_tab_ambiguous(
    tmp_path: Path,
) -> None:
    peers = [
        _peer(3, name="dispatcher", role="dispatcher",
              tab=0, tab_name="main", same_tab=True),
        _peer(11, name="worker-a", role="worker",
              tab=1, tab_name="build", same_tab=False),
        _peer(17, name="worker-b", role="worker",
              tab=2, tab_name="build", same_tab=False),
    ]
    plan = build_plan(
        _task(tmp_path),
        _ok_panes(),
        tmp_path / ".state",
        transport="renga",
        peers=peers,
        server_capabilities=_ALL_CAPS,
        tab={"name": "build"},
    )
    assert plan.status == "input_invalid"
    assert plan.spawn is None
    # renga never first-matches a duplicate display name, so neither may this.
    assert plan.errors[0].startswith("tab_ambiguous:")
    assert "pane_id" in plan.errors[0]
    assert plan.state_writes == []


def test_build_plan_tab_index_out_of_range_is_tab_not_found(
    tmp_path: Path,
) -> None:
    plan = build_plan(
        _task(tmp_path),
        _ok_panes(),
        tmp_path / ".state",
        transport="renga",
        peers=_two_tab_peers(),      # only tabs 0 and 1 are observed
        server_capabilities=_ALL_CAPS,
        tab={"index": 9},
    )
    assert plan.status == "input_invalid"
    assert plan.spawn is None
    assert plan.errors[0].startswith("tab_not_found:")
    assert "pane_id" in plan.errors[0]     # points at the stable alternative
    assert plan.state_writes == []


def test_build_plan_tab_selector_unresolvable_without_peers_passes_through_with_reason(
    tmp_path: Path,
) -> None:
    # No --peers-json at all: there is no census to canonicalise against, so
    # the documented degradation is RECORDED rather than guessed at.
    plan = build_plan(
        _task(tmp_path),
        _ok_panes(),
        tmp_path / ".state",
        transport="renga",
        peers=None,
        server_capabilities=_SPAWN_TAB_ONLY,
        tab={"index": 2},
    )
    assert plan.status == "ready_to_spawn"
    assert plan.spawn["tab"] == {"index": 2}      # emitted unchanged
    placement = plan.layout["tab_placement"]
    assert placement["kind"] == "existing"
    assert any("no peer census" in r for r in placement["reasons"])
    assert any("index-shift-prone" in r for r in placement["reasons"])
    # No census -> no population report either; the two are independent inputs.
    assert plan.population is None


def test_build_plan_target_tab_mismatch_preflight(tmp_path: Path) -> None:
    # The one reachable contradiction: --panes-json (caller-tab-scoped after
    # renga#288) lists pane 11, while --peers-json places pane 11 in a tab
    # that is NOT the caller's. One of the two snapshots is stale, so the
    # spawn is refused before anything is written rather than addressing the
    # wrong tab.
    panes = _ok_panes() + [
        _pane(11, name="worker-a", role="worker", x=300, y=0, w=100, h=50),
    ]
    plan = build_plan(
        _task(tmp_path),
        panes,
        tmp_path / ".state",
        transport="renga",
        peers=_two_tab_peers(),
        server_capabilities=_ALL_CAPS,
        tab={"pane_id": 11},
    )
    assert plan.status == "input_invalid"
    assert plan.spawn is None
    assert plan.errors[0].startswith("target_tab_mismatch:")
    assert "stale" in plan.errors[0]
    assert plan.state_writes == []


def test_build_plan_target_tab_mismatch_is_not_keyed_on_the_anchor_id(
    tmp_path: Path,
) -> None:
    # Regression, adversarial review (Major): the contradiction guard used to
    # look the operator's pane id up against TabCensus.anchor_pane_id, which is
    # min() of the tab's peer ids. So it fired only when the operator happened
    # to name the SMALLEST id in the tab and waved the structurally identical
    # contradiction through on every other one -- a safety guard whose
    # behaviour depended on an ordering tie-break. tab 1 "build" holds peers 11
    # and 17, so 11 is the anchor and 17 is the one that used to slip past.
    peers = _two_tab_peers()
    anchors = {t.index: t.anchor_pane_id
               for t in derive_tab_awareness(peers, None).tabs}
    assert anchors[1] == 11                      # guard the premise
    panes = _ok_panes() + [
        _pane(11, name="worker-a", role="worker", x=300, y=0, w=100, h=50),
        _pane(17, name="worker-b", role="worker", x=400, y=0, w=100, h=50),
    ]
    for pane_id in (11, 17):
        plan = build_plan(
            _task(tmp_path, f"mismatch{pane_id}"),
            panes,
            tmp_path / ".state",
            transport="renga",
            peers=peers,
            server_capabilities=_ALL_CAPS,
            tab={"pane_id": pane_id},
        )
        # Two structurally identical stale-snapshot contradictions must get the
        # same verdict, not opposite ones.
        assert plan.status == "input_invalid", pane_id
        assert plan.errors[0].startswith("target_tab_mismatch:"), pane_id
        assert str(pane_id) in plan.errors[0]
        assert plan.spawn is None
        assert plan.state_writes == []


# ---------------------------------------------------------------------------
# F. Overflow
# ---------------------------------------------------------------------------


def test_build_plan_overflow_off_by_default_escalates_as_today(
    tmp_path: Path,
) -> None:
    # Full renga 2.0 capabilities AND a peer census -- everything overflow
    # needs is present except the opt-in flag. Overflow silently flips exit 2
    # into exit 0 and puts a worker in an invisible background tab, so it must
    # never be the default.
    plan = build_plan(
        _task(tmp_path),
        _unsplittable_panes(),
        tmp_path / ".state",
        transport="renga",
        peers=_two_tab_peers(),
        server_capabilities=_ALL_CAPS,
        overflow_to_new_tab=False,
    )
    assert plan.status == "split_capacity_exceeded"
    assert plan.spawn is None
    assert plan.escalate["message"].startswith(_PRE_158_RECT_MESSAGE)
    assert plan.on_spawn_error is None


def test_build_plan_overflow_emits_new_tab_named_after_worker(
    tmp_path: Path,
) -> None:
    panes = _unsplittable_panes()
    assert choose_split(panes) is None          # guard the premise
    plan = build_plan(
        _task(tmp_path),
        panes,
        tmp_path / ".state",
        transport="renga",
        peers=_two_tab_peers(),
        server_capabilities=_ALL_CAPS,
        overflow_to_new_tab=True,
    )
    # The would-be exit 2 becomes an exit 0 -- that is the whole point of the
    # opt-in.
    assert plan.status == "ready_to_spawn"
    # worker-{task_id} is unique by construction (a duplicate task_id is
    # rejected upstream) and matches renga's [A-Za-z0-9_-]+, so the operator
    # gets a greppable tab label instead of a bare index.
    assert plan.spawn["tab"] == {"new": {"name": "worker-demo"}}
    assert "target" not in plan.spawn
    assert "direction" not in plan.spawn
    assert plan.state_writes  # a real spawn plan, so the seed files are named


def test_build_plan_overflow_not_used_when_split_candidate_exists(
    tmp_path: Path,
) -> None:
    # Overflow is a FALLBACK, never a preference: with room left in the
    # caller's tab the worker goes there and no tab key is emitted at all.
    plan = build_plan(
        _task(tmp_path),
        _ok_panes(),
        tmp_path / ".state",
        transport="renga",
        peers=_two_tab_peers(),
        server_capabilities=_ALL_CAPS,
        overflow_to_new_tab=True,
    )
    assert plan.status == "ready_to_spawn"
    assert "tab" not in plan.spawn
    assert plan.spawn["target"] == "dispatcher"
    assert plan.on_spawn_error is None
    # Nothing to report about placement: it degraded to the pre-#158 shape.
    assert plan.layout is not None      # the flag was requested, so diagnostics
    assert plan.layout["tab_placement"] is None


def test_build_plan_overflow_without_capability_degrades_to_escalation(
    tmp_path: Path,
) -> None:
    # --overflow-to-new-tab is a fallback the operator ARMED, not a demand, so
    # a missing spawn_tab token degrades it back to the pre-#158 escalation
    # rather than failing the run the way an explicit --tab does.
    plan = build_plan(
        _task(tmp_path),
        _unsplittable_panes(),
        tmp_path / ".state",
        transport="renga",
        peers=_two_tab_peers(),
        server_capabilities=None,
        overflow_to_new_tab=True,
    )
    assert plan.status == "split_capacity_exceeded"
    msg = plan.escalate["message"]
    assert msg.startswith(_PRE_158_RECT_MESSAGE)
    assert "max_concurrent_workers" not in msg
    # The missing token is named, so the operator can act on it.
    assert "--server-capability spawn_tab was not asserted" in msg
    placement = plan.layout["tab_placement"]
    assert placement is not None and placement["kind"] == "caller"
    assert any("spawn_tab" in r for r in placement["reasons"])


def test_build_plan_overflow_applies_fleet_ceiling(tmp_path: Path) -> None:
    # Under renga the ONLY worker ceiling has ever been "choose_split found
    # nothing" -- and overflow deletes exactly that. It does not self-limit
    # either: list_panes is caller-tab-scoped after renga#288, so the next
    # delegation re-observes the same saturated caller tab and mints ANOTHER
    # tab. capacity_policy is the only bound left.
    peers = _two_tab_peers() + [
        _peer(23, name="worker-c", role="worker",
              tab=2, tab_name="more", same_tab=False),
    ]
    policy = CapacityPolicy(max_concurrent_workers=3)
    plan = build_plan(
        _task(tmp_path),
        _unsplittable_panes(),
        tmp_path / ".state",
        transport="renga",
        capacity_policy=policy,
        peers=peers,
        server_capabilities=_ALL_CAPS,
        overflow_to_new_tab=True,
    )
    assert plan.status == "split_capacity_exceeded"
    assert plan.spawn is None
    msg = plan.escalate["message"]
    assert "max_concurrent_workers=3" in msg
    assert "active_workers=3" in msg
    # The fleet reason must stay distinguishable from the rect reason: ja
    # branches on the wording.
    assert "MIN_PANE" not in msg
    # No seed files exist under this tmp_path, so the reservation ledger is
    # empty and the ceiling is decided by the census alone. The two reserved_*
    # keys are still present: their ABSENCE would mean the ledger was skipped.
    assert plan.capacity == {
        "transport": "renga",
        "max_concurrent_workers": 3,
        "active_workers": 3,
        "reserved_workers": 0,
        "reserved_worker_names": [],
        "free_worker_slots": 0,
    }

    # ...and the control, which is the load-bearing half: the IDENTICAL inputs
    # with overflow off must not consult the policy at all. Paired with the
    # pre-existing test_build_plan_renga_ignores_capacity_policy this pins the
    # ceiling to overflow mode ONLY.
    off = build_plan(
        _task(tmp_path),
        _unsplittable_panes(),
        tmp_path / ".state",
        transport="renga",
        capacity_policy=policy,
        peers=peers,
        server_capabilities=_ALL_CAPS,
        overflow_to_new_tab=False,
    )
    assert off.status == "split_capacity_exceeded"
    assert off.capacity is None
    assert "max_concurrent_workers" not in off.escalate["message"]
    assert "MIN_PANE" in off.escalate["message"]


def _overflow_once(
    tmp_path: Path, task_id: str, peers: list[Peer], policy: CapacityPolicy,
) -> Any:
    """One overflow delegation, including the state writes the CLI performs.

    ``cmd_delegate_plan`` writes the seed and instruction files on
    ``ready_to_spawn``; the reservation ledger is those seed files, so a test
    that skipped the writes would not be simulating consecutive delegations
    at all.
    """
    state = tmp_path / ".state"
    task = {"task_id": task_id, "worker_dir": str(tmp_path), "instruction": "x"}
    plan = build_plan(
        task, _unsplittable_panes(), state,
        transport="renga",
        capacity_policy=policy,
        peers=peers,
        server_capabilities=_ALL_CAPS,
        overflow_to_new_tab=True,
    )
    if plan.status == "ready_to_spawn":
        write_worker_seed(state, task, plan.task_id, plan.spawn or {})
        write_instruction(state, task, plan.task_id)
    return plan


def test_build_plan_overflow_ceiling_counts_unbound_reservations(
    tmp_path: Path,
) -> None:
    # The hole the pane/peer union cannot close, and the reason the ledger
    # exists. A same-tab spawn is covered by the union because its pane shows
    # up in the caller's list_panes at once. An OVERFLOW spawn lands in a tab
    # of its own -- kept out of list_panes forever by renga#288 scoping -- and
    # is not a peer for another 10-30s, so during that window it is invisible
    # to BOTH inputs. Before the ledger, three consecutive delegations against
    # a ceiling of 2 all returned ready_to_spawn, each reporting
    # "free_worker_slots: 2".
    peers = [_peer(3, name="secretary", role="secretary",
                   tab=0, tab_name="main", same_tab=True)]
    policy = CapacityPolicy(max_concurrent_workers=2)

    first = _overflow_once(tmp_path, "t1", peers, policy)
    assert first.status == "ready_to_spawn"
    assert first.capacity["reserved_workers"] == 0

    second = _overflow_once(tmp_path, "t2", peers, policy)
    assert second.status == "ready_to_spawn"
    # t1 is now a reservation: still not a pane (other tab), still not a peer.
    assert second.capacity["reserved_workers"] == 1
    assert second.capacity["reserved_worker_names"] == ["worker-t1"]
    assert second.capacity["free_worker_slots"] == 1

    third = _overflow_once(tmp_path, "t3", peers, policy)
    assert third.status == "split_capacity_exceeded"
    assert third.spawn is None
    assert third.capacity["active_workers"] == 0
    assert third.capacity["reserved_workers"] == 2
    assert third.capacity["free_worker_slots"] == 0
    msg = third.escalate["message"]
    # The operator is told WHICH workers hold the slots, and that the hold is
    # self-releasing -- otherwise "0 active, 0 free" reads as a bug.
    assert "reserved_workers=2" in msg
    assert "worker-t1" in msg and "worker-t2" in msg
    assert f"{WORKER_BIND_WINDOW_SECONDS}s" in msg
    msg.encode("cp932")


def test_overflow_reservations_expire_and_are_not_double_counted(
    tmp_path: Path,
) -> None:
    # Two properties that together stop the ledger from becoming a leak.
    peers = [_peer(3, name="secretary", role="secretary",
                   tab=0, tab_name="main", same_tab=True)]
    policy = CapacityPolicy(max_concurrent_workers=2)
    _overflow_once(tmp_path, "t1", peers, policy)
    _overflow_once(tmp_path, "t2", peers, policy)
    seeds = sorted((tmp_path / ".state" / "workers").glob("*.md"))
    assert len(seeds) == 2

    # 1. EXPIRY. Nothing ever deletes a seed file, so without a TTL every
    #    worker the org has ever planned would hold a slot forever. Age them
    #    past the bind window: a spawn that never came up must free its slot
    #    with no cleanup step and no operator action.
    stale = time.time() - (WORKER_BIND_WINDOW_SECONDS + 60)
    for seed in seeds:
        os.utime(seed, (stale, stale))
    assert count_unbound_reservations(tmp_path / ".state", ()) == ()
    revived = _overflow_once(tmp_path, "t3", peers, policy)
    assert revived.status == "ready_to_spawn"

    # 2. NO DOUBLE COUNT. Once the worker does bind, it is in the census --
    #    and a reservation that is also a peer would consume its slot twice,
    #    halving the effective ceiling. (t3's own seed is fresh from the
    #    revived delegation above, so all three are live reservations now.)
    for seed in seeds:
        os.utime(seed, None)  # fresh again
    assert count_unbound_reservations(tmp_path / ".state", ()) == (
        "worker-t1", "worker-t2", "worker-t3",
    )
    assert count_unbound_reservations(
        tmp_path / ".state", ("worker-t1", "worker-t2", "worker-t3"),
    ) == ()
    # Partial binding frees exactly the bound slots, not all of them.
    assert count_unbound_reservations(
        tmp_path / ".state", ("worker-t1",),
    ) == ("worker-t2", "worker-t3")


def test_count_unbound_reservations_tolerates_a_missing_state_dir(
    tmp_path: Path,
) -> None:
    # The very first delegation of a session runs before .state/workers
    # exists. Capacity accounting must not be the thing that breaks it.
    assert count_unbound_reservations(tmp_path / "nope", ()) == ()


def test_overflow_reservations_ignored_outside_overflow_mode(
    tmp_path: Path,
) -> None:
    # The ledger is scoped to the one mode that needs it. A fresh seed file
    # must not perturb the broker ceiling or the non-overflow renga path,
    # whose numbers pre-date #158.
    state = tmp_path / ".state"
    (state / "workers").mkdir(parents=True)
    (state / "workers" / "worker-ghost.md").write_text("x", encoding="utf-8")

    broker = build_plan(
        _task(tmp_path, "b1"), _unsplittable_panes(), state,
        transport="broker",
        capacity_policy=CapacityPolicy(max_concurrent_workers=1),
    )
    assert broker.status == "ready_to_spawn"
    assert "reserved_workers" not in (broker.capacity or {})

    renga_off = build_plan(
        _task(tmp_path, "r1"), _unsplittable_panes(), state,
        transport="renga",
        capacity_policy=CapacityPolicy(max_concurrent_workers=1),
        peers=[_peer(3, name="secretary", role="secretary",
                     tab=0, tab_name="main", same_tab=True)],
        server_capabilities=_ALL_CAPS,
        overflow_to_new_tab=False,
    )
    assert renga_off.status == "split_capacity_exceeded"
    assert renga_off.capacity is None


def _full_tab_table_peers() -> list[Peer]:
    """A peer census that already sees renga's MAX_TABS tabs."""
    return [
        _peer(100 + i, name=f"agent-{i}", role="dispatcher",
              tab=i, tab_name=f"t{i}", same_tab=(i == 0))
        for i in range(RENGA_MAX_TABS)
    ]


def test_build_plan_overflow_blocked_at_tab_limit(tmp_path: Path) -> None:
    # A full tab table IS capacity, so it escalates (exit 2) rather than
    # failing as a bad argument -- and it is deliberately NOT split_refused
    # (that is MAX_PANES within one tab) nor the rect/MIN_PANE reason.
    #
    # UNSPLITTABLE panes on purpose. The tab table is only the binding
    # constraint once the caller's own tab has nothing left to split; with a
    # split candidate still available the armed fallback must not fire at all,
    # which is what the companion test below pins.
    panes = _unsplittable_panes()
    assert choose_split(panes) is None
    plan = build_plan(
        _task(tmp_path),
        panes,
        tmp_path / ".state",
        transport="renga",
        peers=_full_tab_table_peers(),
        server_capabilities=_ALL_CAPS,
        overflow_to_new_tab=True,
    )
    assert plan.status == "split_capacity_exceeded"
    assert plan.spawn is None
    msg = plan.escalate["message"]
    assert msg.startswith("SPLIT_CAPACITY_EXCEEDED: ")   # the prefix ja keys on
    assert "tab_limit_reached:" in msg
    assert "MIN_PANE" not in msg
    assert "split_refused" not in msg
    assert plan.layout["tabs_seen"] == RENGA_MAX_TABS == 16

    # An EXPLICIT --tab new is still refused in the pre-flight, before
    # choose_split is consulted at all: the operator named that placement, so
    # there is no fallback to demote and nothing else to try.
    explicit = build_plan(
        _task(tmp_path),
        _ok_panes(),                       # splittable, and deliberately so
        tmp_path / ".state",
        transport="renga",
        peers=_full_tab_table_peers(),
        server_capabilities=_ALL_CAPS,
        tab={"new": {}},
    )
    assert explicit.status == "split_capacity_exceeded"
    assert "tab_limit_reached:" in explicit.escalate["message"]


def test_build_plan_armed_overflow_at_tab_limit_still_uses_in_tab_split(
    tmp_path: Path,
) -> None:
    # Regression, adversarial review (Major): the tab_limit_reached refusal used
    # to run as a PRE-FLIGHT, i.e. before choose_split and before the
    # overflow->caller demotion. Merely ARMING --overflow-to-new-tab therefore
    # turned a perfectly healthy in-tab split into exit 2 as soon as the census
    # saw MAX_TABS tabs -- and an org that keeps the flag on as a standing
    # setting is driven toward exactly that state by overflow itself, so every
    # delegation would escalate to a human. Overflow is a FALLBACK: the tab
    # table cannot bind while the caller's own tab can still host the worker.
    panes = _ok_panes()
    assert choose_split(panes).target_name == "dispatcher"   # guard the premise
    peers = _full_tab_table_peers()

    armed = build_plan(
        _task(tmp_path, "armed"),
        panes,
        tmp_path / ".state",
        transport="renga",
        peers=peers,
        server_capabilities=_ALL_CAPS,
        overflow_to_new_tab=True,
    )
    assert armed.status == "ready_to_spawn"
    assert armed.spawn["target"] == "dispatcher"
    assert "tab" not in armed.spawn          # demoted back to the caller's tab
    assert armed.on_spawn_error is None

    # The control that makes this a regression test rather than a tautology:
    # the IDENTICAL inputs with the flag off already behaved this way, so
    # arming a fallback must not change the answer.
    off = build_plan(
        _task(tmp_path, "off"),
        panes,
        tmp_path / ".state",
        transport="renga",
        peers=peers,
        server_capabilities=_ALL_CAPS,
        overflow_to_new_tab=False,
    )
    assert off.status == "ready_to_spawn"
    assert off.spawn["target"] == armed.spawn["target"]


def test_build_plan_overflow_requires_peer_census(tmp_path: Path) -> None:
    # Regression, adversarial review (Major): overflow deletes the rect ceiling
    # and the fleet ceiling is the ONLY bound left -- but it is counted from
    # the population, which falls back to the caller tab's panes when no census
    # is supplied. Overflowed workers live in their own new tabs by
    # construction, so they are never in the caller's list_panes again: the
    # ceiling read 0 forever and never bound. Twelve consecutive delegations
    # each reported "8 free slots" and each minted another tab.
    panes = _unsplittable_panes()
    assert choose_split(panes) is None                # guard the premise

    refused = build_plan(
        _task(tmp_path),
        panes,
        tmp_path / ".state",
        transport="renga",
        peers=None,                                   # the whole point
        server_capabilities=_SPAWN_TAB_ONLY,
        overflow_to_new_tab=True,
    )
    # An input error, not exhausted capacity: the fix is one flag, and nothing
    # has been written, so the operator simply re-runs.
    assert refused.status == "input_invalid"
    assert refused.spawn is None
    assert refused.capacity is None
    assert refused.state_writes == []
    assert not (tmp_path / ".state").exists()
    assert "--peers-json" in refused.errors[0]
    assert "--overflow-to-new-tab" in refused.errors[0]
    refused.errors[0].encode("cp932")

    # Positive control: the same invocation WITH a census plans the overflow,
    # so the refusal is about the missing census and nothing else.
    allowed = build_plan(
        _task(tmp_path),
        panes,
        tmp_path / ".state",
        transport="renga",
        peers=_two_tab_peers(),
        server_capabilities=_SPAWN_TAB_ONLY,
        overflow_to_new_tab=True,
    )
    assert allowed.status == "ready_to_spawn"
    assert allowed.spawn["tab"] == {"new": {"name": "worker-demo"}}
    # And the ceiling it reports is now countable across tabs rather than 0.
    assert allowed.capacity["active_workers"] == 2

    # The requirement is scoped to the path that actually mints a tab. Without
    # the spawn_tab token overflow degrades to the pre-#158 escalation and
    # never touches the tab table, so demanding a census there would fail a run
    # that is behaving exactly as it did before #158.
    degraded = build_plan(
        _task(tmp_path),
        panes,
        tmp_path / ".state",
        transport="renga",
        peers=None,
        server_capabilities=None,
        overflow_to_new_tab=True,
    )
    assert degraded.status == "split_capacity_exceeded"
    assert degraded.escalate["message"].startswith(_PRE_158_RECT_MESSAGE)


def test_build_plan_overflow_refused_when_new_tab_would_not_fit(
    tmp_path: Path,
) -> None:
    # The fresh tab's lone pane is estimated from the caller tab's MEASURED
    # pane area (10x2 here). Below the MIN_PANE_* floors renga would answer
    # split_refused for the new tab too, so overflow cannot help and the
    # escalation says so instead of promising a spawn that will bounce.
    plan = build_plan(
        _task(tmp_path),
        _tiny_panes(),
        tmp_path / ".state",
        transport="renga",
        peers=_two_tab_peers(),
        server_capabilities=_ALL_CAPS,
        overflow_to_new_tab=True,
    )
    assert plan.status == "split_capacity_exceeded"
    assert plan.spawn is None
    msg = plan.escalate["message"]
    assert "split_refused:" in msg
    assert "10x2" in msg
    assert "advisory" in msg
    assert f"{MIN_PANE_WIDTH}x{MIN_PANE_HEIGHT}" in msg
    # The reclaim hint is appended so the operator has a concrete remedy.
    assert "columns left of the pane area" in msg
    assert plan.layout["new_tab_estimate"] == {
        "width": 10, "height": 2, "fits": False, "advisory": True,
    }


def test_build_plan_explicit_tab_wins_over_overflow(tmp_path: Path) -> None:
    # Both supplied, and the caller tab still HAS a split candidate: the
    # explicit selector the operator typed wins over both the in-tab split and
    # the overflow fallback, and the ignored flag is recorded.
    panes = _ok_panes()
    assert choose_split(panes) is not None
    plan = build_plan(
        _task(tmp_path),
        panes,
        tmp_path / ".state",
        transport="renga",
        peers=_two_tab_peers(),
        server_capabilities=_ALL_CAPS,
        tab={"new": {"name": "pinned"}},
        overflow_to_new_tab=True,
    )
    assert plan.status == "ready_to_spawn"
    assert plan.spawn["tab"] == {"new": {"name": "pinned"}}
    assert any(
        "explicit --tab selector wins" in w for w in plan.warnings
    )
    assert any(
        "--overflow-to-new-tab was ignored" in r
        for r in plan.layout["tab_placement"]["reasons"]
    )


# ---------------------------------------------------------------------------
# G. Plan shape / regression
# ---------------------------------------------------------------------------


def test_build_plan_renga_default_plan_adds_only_null_keys(
    tmp_path: Path,
) -> None:
    # The golden diff for every caller that passes nothing new: three extra
    # keys, all null. Nothing else about the document may move.
    plan = build_plan(_task(tmp_path), _ok_panes(), tmp_path / ".state")
    d = dataclasses.asdict(plan)
    assert list(d) == _PRE_158_PLAN_KEYS + [
        "population", "layout", "on_spawn_error",
    ]
    assert d["population"] is None
    assert d["layout"] is None
    assert d["on_spawn_error"] is None
    assert d["capacity"] is None
    assert d["status"] == "ready_to_spawn"
    # The spawn dict keeps its pre-#158 keys AND their order: no tab key, and
    # nothing reshuffled by the incremental assembly the tab:{new} shape needs.
    assert list(d["spawn"]) == _PRE_158_SPAWN_KEYS
    assert d["spawn"]["target"] == "dispatcher"


def test_build_plan_renga_escalation_preserves_message_and_appends(
    tmp_path: Path,
) -> None:
    plan = build_plan(
        _task(tmp_path), _tiny_panes(), tmp_path / ".state", transport="renga",
    )
    assert plan.status == "split_capacity_exceeded"
    msg = plan.escalate["message"]
    # ja forwards this text to the secretary verbatim and both this repo and
    # its consumers key on "MIN_PANE in message" / "max_concurrent_workers not
    # in message" to tell the rect reason from the fleet reason. So the
    # original sentence must survive as a literal PREFIX.
    assert msg.startswith(_PRE_158_RECT_MESSAGE)
    assert "MIN_PANE" in msg
    assert "max_concurrent_workers" not in msg
    appended = msg[len(_PRE_158_RECT_MESSAGE):]
    assert appended                       # something really was appended
    # The measured pane area, the measured left-panel columns, and the sidebar
    # named as a *candidate* attribution (never subtracted from any rect).
    assert "The pane area is 10x2 at x=46,y=1" in appended
    assert "46 columns left of the pane area" in appended
    assert "org sidebar" in appended
    assert 'org_sidebar = "off"' in appended
    assert plan.layout["left_panels_columns"] == 46
    assert plan.layout["pane_area"] == {"x": 46, "y": 1, "width": 10,
                                        "height": 2}


def test_build_plan_renga_capacity_stays_none_outside_overflow(
    tmp_path: Path,
) -> None:
    # plan.capacity is None on the renga path is pinned by three existing
    # tests and consumed by ja's --free-panes. Passing peers / capabilities /
    # an explicit --tab must not start populating it; only overflow mode may.
    common: dict[str, Any] = {
        "transport": "renga",
        "peers": _two_tab_peers(),
        "server_capabilities": _ALL_CAPS,
        "capacity_policy": CapacityPolicy(max_concurrent_workers=1),
    }
    ready = build_plan(
        _task(tmp_path), _ok_panes(), tmp_path / ".state", **common,
    )
    assert ready.status == "ready_to_spawn"
    assert ready.capacity is None

    escalated = build_plan(
        _task(tmp_path), _unsplittable_panes(), tmp_path / ".state", **common,
    )
    assert escalated.status == "split_capacity_exceeded"
    assert escalated.capacity is None

    tab_directed = build_plan(
        _task(tmp_path), _ok_panes(), tmp_path / ".state",
        tab={"new": {"name": "z"}}, **common,
    )
    assert tab_directed.status == "ready_to_spawn"
    assert tab_directed.capacity is None

    # The population census goes to plan.population instead, so the renga
    # capacity contract is untouched while the numbers are still auditable.
    assert ready.population["active_workers"] == 2
    assert ready.population["source"] == "panes+peers"


def test_build_plan_on_spawn_error_present_only_when_tab_emitted(
    tmp_path: Path,
) -> None:
    default = build_plan(_task(tmp_path), _ok_panes(), tmp_path / ".state")
    assert default.on_spawn_error is None

    broker = build_plan(
        _task(tmp_path), _ok_panes(), tmp_path / ".state", transport="broker",
    )
    assert broker.on_spawn_error is None

    escalated = build_plan(
        _task(tmp_path), _unsplittable_panes(), tmp_path / ".state",
        transport="renga",
    )
    assert escalated.on_spawn_error is None

    tabbed = build_plan(
        _task(tmp_path), _ok_panes(), tmp_path / ".state",
        transport="renga",
        server_capabilities=_SPAWN_TAB_ONLY,
        tab={"new": {"name": "z"}},
    )
    assert "tab" in tabbed.spawn
    table = tabbed.on_spawn_error
    assert table is not None
    # Every code renga can answer a tab-directed spawn with is covered; a
    # missing one would leave the dispatcher with no decision at 3am.
    assert set(table) == set(TAB_SPAWN_ERROR_CODES)
    for code, entry in table.items():
        assert set(entry) == {"meaning", "action", "remove_state_writes",
                              "next"}, code
        # remove_state_writes names a concrete lockout: build_plan hard-fails
        # on pre-existing state files, so a failed tab spawn would otherwise
        # block its own retry.
        assert entry["remove_state_writes"] is True
        assert isinstance(entry["next"], str) and entry["next"]


def test_build_plan_tab_and_overflow_ignored_under_broker(
    tmp_path: Path,
) -> None:
    # The broker has no tab concept at any layer, so a tab flag is inert
    # rather than wrong. Following the --max-concurrent-workers precedent, a
    # flag with no effect warns and continues instead of failing the run.
    plan = build_plan(
        _task(tmp_path),
        _ok_panes(),
        tmp_path / ".state",
        transport="broker",
        peers=_two_tab_peers(),
        server_capabilities=_ALL_CAPS,
        tab={"new": {"name": "z"}},
        overflow_to_new_tab=True,
    )
    assert plan.status == "ready_to_spawn"
    assert any("renga-only" in w for w in plan.warnings)
    assert "tab" not in plan.spawn
    assert plan.spawn["target"] == "focused"
    assert plan.spawn["direction"] == "vertical"
    assert plan.on_spawn_error is None
    assert plan.layout is None            # layout is a renga-only diagnostic
    assert plan.capacity["transport"] == "broker"


def test_plan_json_is_serializable_and_cp932_safe(tmp_path: Path) -> None:
    # The plan is printed to stdout, so a single em-dash / smart quote /
    # arrow anywhere in it crashes a cp932 console. pytest captures stdout as
    # UTF-8 and cannot catch that, so the encoding is asserted directly.
    plans = {
        "default": build_plan(
            _task(tmp_path), _ok_panes(), tmp_path / ".state",
        ),
        "rect_escalation": build_plan(
            _task(tmp_path), _tiny_panes(), tmp_path / ".state",
            transport="renga",
        ),
        "overflow_ready": build_plan(
            _task(tmp_path), _unsplittable_panes(), tmp_path / ".state",
            transport="renga", peers=_two_tab_peers(),
            server_capabilities=_ALL_CAPS, overflow_to_new_tab=True,
        ),
        "tab_directed": build_plan(
            _task(tmp_path), _ok_panes(), tmp_path / ".state",
            transport="renga", peers=_two_tab_peers(),
            server_capabilities=_ALL_CAPS, tab={"name": "build"},
        ),
        "preflight_refusal": build_plan(
            _task(tmp_path), _ok_panes(), tmp_path / ".state",
            transport="renga", peers=_two_tab_peers(),
            server_capabilities=_ALL_CAPS, tab={"name": "nope"},
        ),
        "tab_limit": build_plan(
            _task(tmp_path), _ok_panes(), tmp_path / ".state",
            transport="renga",
            peers=[
                _peer(100 + i, name=f"agent-{i}", role="dispatcher",
                      tab=i, tab_name=f"t{i}", same_tab=(i == 0))
                for i in range(RENGA_MAX_TABS)
            ],
            server_capabilities=_ALL_CAPS, overflow_to_new_tab=True,
        ),
    }
    for label, plan in plans.items():
        d = dataclasses.asdict(plan)
        text = json.dumps(d, ensure_ascii=False)
        # ensure_ascii=False is what the CLI uses, so this is the real byte
        # stream a console would have to render.
        text.encode("cp932")
        # No tuple / frozenset may leak into the report: it would round-trip
        # as a list and make the in-process dict differ from the emitted one.
        assert json.loads(text) == d, label


# ---------------------------------------------------------------------------
# H. CLI
# ---------------------------------------------------------------------------


def _write_cli_inputs(
    tmp_path: Path,
    panes: list[dict[str, Any]],
    peers: Optional[list[dict[str, Any]]] = None,
    *,
    task_id: str = "cli",
) -> list[str]:
    """Write task / panes / peers JSON and return the shared argv prefix."""
    task = {"task_id": task_id, "worker_dir": str(tmp_path), "instruction": "x"}
    task_path = tmp_path / "task.json"
    panes_path = tmp_path / "panes.json"
    task_path.write_text(json.dumps(task), encoding="utf-8")
    panes_path.write_text(json.dumps(panes), encoding="utf-8")
    argv = [
        "delegate-plan",
        "--task-json", str(task_path),
        "--panes-json", str(panes_path),
        "--state-dir", str(tmp_path / ".state"),
    ]
    if peers is not None:
        peers_path = tmp_path / "peers.json"
        peers_path.write_text(json.dumps(peers), encoding="utf-8")
        argv += ["--peers-json", str(peers_path)]
    return argv


def _renga_panes() -> list[dict[str, Any]]:
    return [
        {"id": 1, "name": "curator", "role": "curator",
         "x": 0, "y": 0, "width": 100, "height": 50},
        {"id": 2, "name": "dispatcher", "role": "dispatcher",
         "x": 100, "y": 0, "width": 200, "height": 50},
    ]


def _saturated_renga_panes() -> list[dict[str, Any]]:
    """The CLI twin of :func:`_unsplittable_panes`."""
    return [
        {"id": 1, "name": "secretary", "role": "secretary",
         "x": 46, "y": 1, "width": 130, "height": 40},
    ]


def test_cli_omitting_peers_json_matches_today(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    argv = _write_cli_inputs(tmp_path, _renga_panes())
    rc = main(argv + ["--transport", "renga", "--dry-run"])
    assert rc == 0
    plan = json.loads(capsys.readouterr().out)
    # The emitted document differs from the pre-#158 one by exactly three null
    # keys -- nothing else moved, including the spawn key ORDER.
    assert list(plan) == _PRE_158_PLAN_KEYS + [
        "population", "layout", "on_spawn_error",
    ]
    assert plan["population"] is None
    assert plan["layout"] is None
    assert plan["on_spawn_error"] is None
    assert plan["capacity"] is None
    assert list(plan["spawn"]) == _PRE_158_SPAWN_KEYS
    assert plan["spawn"]["target"] == "dispatcher"


def test_cli_peers_json_drives_broker_ceiling(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    # The headline #158 bug, end to end. list_panes is caller-tab-scoped after
    # renga#288, so the caller's own tab shows 2 workers while the org really
    # runs 6. Without --peers-json the ceiling is read against 2 and the
    # dispatcher spawns straight past it.
    panes = [
        {"id": 1, "name": "dispatcher", "role": "dispatcher",
         "x": 0, "y": 0, "width": 200, "height": 50},
        {"id": 2, "name": "worker-a", "role": "worker",
         "x": 200, "y": 0, "width": 100, "height": 50},
        {"id": 3, "name": "worker-b", "role": "worker",
         "x": 300, "y": 0, "width": 100, "height": 50},
    ]
    peers = [
        # worker-a / worker-b are the SAME two workers seen from the peer
        # surface. The union dedups them on `name`, which is the only key both
        # surfaces of every transport carry.
        {"id": "worker-a", "name": "worker-a", "role": "worker"},
        {"id": "worker-b", "name": "worker-b", "role": "worker"},
        {"id": "worker-c", "name": "worker-c", "role": "worker"},
        {"id": "worker-d", "name": "worker-d", "role": "worker"},
        {"id": "worker-e", "name": "worker-e", "role": "worker"},
        {"id": "worker-f", "name": "worker-f", "role": "worker"},
    ]

    argv = _write_cli_inputs(tmp_path, panes, peers)
    rc = main(argv + [
        "--transport", "broker", "--max-concurrent-workers", "5", "--dry-run",
    ])
    assert rc == 2                      # split_capacity_exceeded
    plan = json.loads(capsys.readouterr().out)
    assert plan["population"]["active_workers"] == 6
    assert plan["population"]["source"] == "panes+peers"
    assert plan["population"]["both"] == 2       # deduped, not double counted
    assert plan["capacity"]["active_workers"] == 6

    # The control that makes this a regression test rather than a tautology:
    # the identical panes with --peers-json omitted still read 2 and exit 0.
    argv_no_peers = _write_cli_inputs(tmp_path, panes)
    rc_no_peers = main(argv_no_peers + [
        "--transport", "broker", "--max-concurrent-workers", "5", "--dry-run",
    ])
    assert rc_no_peers == 0
    stale = json.loads(capsys.readouterr().out)
    assert stale["population"] is None
    assert stale["capacity"]["active_workers"] == 2


def test_cli_empty_peers_json_is_rejected_not_silently_ignored(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    # A wrapper that builds the invocation as `--peers-json "$PEERS"` with an
    # unset variable hands argparse the empty string. Under a truthiness gate
    # that silently reverted to the caller-tab-only count #158 exists to fix,
    # with `population: null` as the only signal -- no error, no warning, exit
    # 0. Presence, not truthiness, matching --tab two lines away.
    task_path = tmp_path / "task.json"
    panes_path = tmp_path / "panes.json"
    task_path.write_text(
        json.dumps({"task_id": "cli", "worker_dir": str(tmp_path),
                    "instruction": "x"}), encoding="utf-8",
    )
    panes_path.write_text(json.dumps(_renga_panes()), encoding="utf-8")
    rc = main([
        "delegate-plan",
        "--task-json", str(task_path),
        "--panes-json", str(panes_path),
        "--state-dir", str(tmp_path / ".state"),
        "--peers-json", "",
        "--transport", "renga", "--dry-run",
    ])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""            # no half-formed plan for ja to parse
    assert "--peers-json" in captured.err


def test_cli_server_capability_flag_is_repeatable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    peers = [
        {"id": 3, "name": "dispatcher", "role": "dispatcher",
         "tab": 0, "tab_name": "main", "same_tab": True},
        {"id": 11, "name": "worker-a", "role": "worker",
         "tab": 1, "tab_name": "build", "same_tab": False},
    ]
    argv = _write_cli_inputs(tmp_path, _renga_panes(), peers)

    rc = main(argv + [
        "--transport", "renga",
        "--server-capability", CAP_CALLER_SCOPE,
        "--server-capability", CAP_CROSS_TAB_PEERS,
        "--dry-run",
    ])
    assert rc == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["population"]["capabilities_known"] is True
    assert plan["population"]["cross_tab_peers"] is True
    # Two tokens asserted, and spawn_tab is not one of them: it must stay
    # false. renga made the tokens distinct precisely because a #288-era
    # server advertises caller_scope while still refusing a tab-directed spawn.
    assert plan["population"]["spawn_tab"] is False

    rc = main(argv + [
        "--transport", "renga",
        "--server-capability", CAP_CALLER_SCOPE,
        "--server-capability", CAP_CROSS_TAB_PEERS,
        "--server-capability", CAP_SPAWN_TAB,
        "--dry-run",
    ])
    assert rc == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["population"]["spawn_tab"] is True
    # All three appended, so the flag really accumulates rather than
    # last-one-wins.
    assert plan["population"]["cross_tab_peers"] is True


def test_cli_tab_flag_selector_parsing_and_bad_value_exit_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    argv = _write_cli_inputs(tmp_path, _renga_panes())
    rc = main(argv + [
        "--transport", "renga",
        "--server-capability", CAP_SPAWN_TAB,
        "--tab", "new:pinned",
        "--dry-run",
    ])
    assert rc == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["spawn"]["tab"] == {"new": {"name": "pinned"}}
    assert "target" not in plan["spawn"]
    assert "direction" not in plan["spawn"]

    # A malformed selector is rejected by parse_tab_selector before anything
    # else happens: rc 1 with a stderr message, and no plan document at all
    # (a half-formed plan on stdout would be parsed by ja as a real answer).
    rc = main(argv + [
        "--transport", "renga",
        "--server-capability", CAP_SPAWN_TAB,
        "--tab", "bogus:1",
        "--dry-run",
    ])
    assert rc == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--tab" in captured.err
    assert "unknown selector" in captured.err


def test_cli_overflow_end_to_end_exit_zero_and_writes_state_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    # Overflow is a REAL ready_to_spawn, not a softened escalation: the seed
    # and instruction files are written exactly as for an in-tab spawn.
    #
    # --peers-json is mandatory here, not decorative: overflow removes the rect
    # ceiling and the fleet ceiling that replaces it is counted from the peer
    # census (see test_build_plan_overflow_requires_peer_census).
    peers = [
        {"id": 3, "name": "dispatcher", "role": "dispatcher",
         "tab": 0, "tab_name": "main", "same_tab": True},
    ]
    argv = _write_cli_inputs(tmp_path, _saturated_renga_panes(), peers)
    rc = main(argv + [
        "--transport", "renga",
        "--server-capability", CAP_SPAWN_TAB,
        "--overflow-to-new-tab",
    ])
    assert rc == 0
    state_dir = tmp_path / ".state"
    assert (state_dir / "workers" / "worker-cli.md").exists()
    assert (
        state_dir / "dispatcher" / "outbox" / "cli-instruction.md"
    ).exists()

    plan = json.loads(capsys.readouterr().out)
    assert plan["status"] == "ready_to_spawn"
    assert plan["spawn"]["tab"] == {"new": {"name": "worker-cli"}}
    assert "target" not in plan["spawn"]
    assert "direction" not in plan["spawn"]
    # The recovery table names the very files just written, so a bounced tab
    # spawn does not lock out its own retry.
    assert set(plan["on_spawn_error"]) == set(TAB_SPAWN_ERROR_CODES)
    assert plan["state_writes"] == [
        str(state_dir / "workers" / "worker-cli.md"),
        str(state_dir / "dispatcher" / "outbox" / "cli-instruction.md"),
    ]


def test_cli_overflow_exhausted_still_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    # Overflow mode makes --max-concurrent-workers live on the renga path (it
    # is inert there otherwise), so a malformed or exhausted value now matters.
    peers = [
        {"id": "worker-a", "name": "worker-a", "role": "worker",
         "tab": 1, "tab_name": "build", "same_tab": False},
        {"id": "worker-b", "name": "worker-b", "role": "worker",
         "tab": 2, "tab_name": "more", "same_tab": False},
    ]
    argv = _write_cli_inputs(tmp_path, _saturated_renga_panes(), peers)
    rc = main(argv + [
        "--transport", "renga",
        "--server-capability", CAP_SPAWN_TAB,
        "--overflow-to-new-tab",
        "--max-concurrent-workers", "2",
    ])
    assert rc == 2
    plan = json.loads(capsys.readouterr().out)
    assert plan["capacity"]["transport"] == "renga"
    assert plan["capacity"]["active_workers"] == 2
    assert "max_concurrent_workers=2" in plan["escalate"]["message"]
    # The four renga tab codes stay plan-level: a capacity refusal is exit 2
    # like every other one, and the code itself rides in escalate.message.
    # ja branches on 0/1/2, so a fourth exit code would be a silent break.
    assert "tab_limit_reached" not in plan["escalate"]["message"]
    # No state files: an exit-2 plan is not a spawn.
    assert not (tmp_path / ".state").exists()


def test_cli_delegate_plan_help_is_cp932_encodable() -> None:
    # The documented crash trap: argparse prints --help straight to the
    # console, so an em-dash (U+2014), a smart quote or a Unicode arrow in any
    # help= string raises UnicodeEncodeError on a cp932 terminal. pytest's
    # redirect_stdout captures UTF-8 and cannot catch it, so the strings are
    # encoded explicitly here.
    parser = build_parser()
    subparsers = [
        a for a in parser._actions
        if isinstance(a, argparse._SubParsersAction)
    ]
    assert subparsers, "delegate-plan is registered on a subparsers action"
    dp = subparsers[0].choices["delegate-plan"]

    helps = [a.help for a in dp._actions if a.help]
    # Guard the premise: the #158 flags really are on this parser, so an empty
    # / stale action list cannot make this test vacuously pass.
    joined = " ".join(helps)
    for flag in (
        "--peers-json", "--server-capability", "--tab",
        "--overflow-to-new-tab", "--max-concurrent-workers",
    ):
        assert any(flag in a.option_strings for a in dp._actions), flag
    assert "spawn_tab" in joined
    assert "--overflow-to-new-tab is set" in joined  # the reworded ceiling help

    for text in helps:
        text.encode("cp932")
    # The rendered pages too -- usage lines, metavars and the description all
    # reach the same console.
    dp.format_help().encode("cp932")
    parser.format_help().encode("cp932")
    for attr in (dp.description, dp.prog, parser.description, parser.prog):
        if attr:
            attr.encode("cp932")


# ---------------------------------------------------------------------------
# I. Untouched contracts
# ---------------------------------------------------------------------------
#
# Design tests 52 (``test_build_plan_renga_ignores_capacity_policy``), 62
# (``test_action_plan_dataclass_default``), 70
# (``tests/broker/test_placement.py``) and 71
# (``tests/transport/test_descriptor.py``) are EXISTING tests in other
# modules. They are deliberately not duplicated here -- their value is that
# they were written before #158 and still pass unedited. The overflow-scoping
# half of 52 is asserted as an in-test control inside
# :func:`test_build_plan_overflow_applies_fleet_ceiling` above so this module
# still fails on its own if the fleet ceiling ever leaks out of overflow mode.
