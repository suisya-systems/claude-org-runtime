"""Peer parsing, forward-compat discrimination and worker population (#158).

Design section 10 groups B / C / D. What these pin, and why each is not
satisfiable by the pre-#158 code:

* **B (10-20)** -- ``Peer`` is a population/identity record with no geometry,
  and ``Peer.has_tab_metadata`` records field PRESENCE rather than value.
  Tests 10 + 11 together ARE the forward-compat discriminator: renga 2.0 sets
  ``tab`` / ``tab_name`` / ``same_tab`` unconditionally (renga
  src/app/ipc_handlers.rs:253-255), so even a SINGLE-TAB 2.0 server is
  detectable, while renga 1.4 declares all three
  ``skip_serializing_if = "Option::is_none"`` and never had the fields. A
  value-based probe ("did anyone report same_tab?") cannot tell those two
  apart; presence can.
* **C (21-28)** -- the population union. Test 25 pins that the dedup key is
  ``name``, not a parsed pane id: the broker's two surfaces use disjoint id
  spaces (``list_peers`` emits ``agent_id``, ``list_panes`` emits the adapter
  handle ``%3``), so a pane-id dedup would double-count every broker worker.
* **D (29-33)** -- ``build_plan`` wiring. Test 29 is the headline #158 bug:
  ``list_panes`` is caller-tab-scoped after renga#288, so a broker/renga 2.0
  ceiling computed from panes alone under-counts and over-spawns. Tests 31 and
  32 pin that the duplicate-name guard was WIDENED into a union, not replaced
  -- a pane with no peer bind must still block.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Optional

import pytest

from claude_org_runtime.dispatcher import runner
from claude_org_runtime.dispatcher.runner import (
    CAP_CALLER_SCOPE,
    CAP_CROSS_TAB_PEERS,
    CAP_SPAWN_TAB,
    CapacityPolicy,
    Pane,
    Peer,
    _parse_panes,
    _parse_peers,
    build_plan,
    count_active_workers,
    count_worker_population,
    derive_tab_awareness,
)


@pytest.fixture(autouse=True)
def _isolate_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run each test from an empty directory.

    Same reason as ``tests/test_dispatcher_runner.py``: ``_default_template_repo``
    walks CWD ancestors looking for the auto-expand template, and this
    worktree's ancestors may or may not contain a real one.
    """
    monkeypatch.chdir(tmp_path)


# ---------------------------------------------------------------------------
# Fixture helpers (pane dicts built the way test_dispatcher_runner.py does)
# ---------------------------------------------------------------------------


def _pane(
    pid: int,
    *,
    name: Optional[str] = None,
    role: Optional[str] = None,
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


def _ok_panes() -> list[Pane]:
    return [
        _pane(1, name="curator", role="curator", x=0, y=0, w=100, h=50),
        _pane(2, name="dispatcher", role="dispatcher", x=100, y=0, w=200, h=50),
    ]


def _peer(
    pid: Any,
    *,
    name: Optional[str] = None,
    role: Optional[str] = None,
    tab: Optional[int] = None,
    tab_name: Optional[str] = None,
    same_tab: Optional[bool] = None,
    tab_metadata: bool = True,
) -> Peer:
    """Build a peer through ``from_dict`` so ``has_tab_metadata`` is real.

    Constructing :class:`Peer` directly would let a test hand-set
    ``has_tab_metadata`` and silently bypass the very presence rule under test,
    so every fixture goes through the parser. ``tab_metadata=False`` omits the
    three keys entirely, which is the renga 1.4 / broker wire shape.
    """
    d: dict[str, Any] = {"id": pid, "name": name, "role": role}
    if tab_metadata:
        d["tab"] = tab
        d["tab_name"] = tab_name
        d["same_tab"] = same_tab
    return Peer.from_dict(d)


def _task(tmp_path: Path, task_id: str = "demo") -> dict[str, Any]:
    return {"task_id": task_id, "worker_dir": str(tmp_path)}


# The message the pre-#158 duplicate-name guard emitted, byte-for-byte.
# Consumers (ja) forward it verbatim to the secretary, so it is quoted here as
# a literal rather than rebuilt from the source -- a test that recomputes the
# string from the implementation could not detect a reword.
_CLASSIC_DUPLICATE_MESSAGE = (
    "pane named 'worker-demo' already exists in the tab; "
    "close it first or pick a different task_id"
)


# ---------------------------------------------------------------------------
# B. Peer parsing / forward-compat discriminator
# ---------------------------------------------------------------------------


def test_peer_from_dict_renga_20_sets_has_tab_metadata() -> None:
    # renga 2.0 with a SINGLE tab: ipc_handlers.rs:253-255 sets all three
    # fields unconditionally, so tab=0 / same_tab=true is what a one-tab 2.0
    # server puts on the wire. The point of the assertion is that this is
    # still detectable as "new server" -- the values alone look unremarkable.
    peer = Peer.from_dict({
        "id": 5, "name": "w", "role": "worker",
        "tab": 0, "tab_name": "main", "same_tab": True,
    })
    assert peer.tab == 0
    assert peer.tab_name == "main"
    assert peer.same_tab is True
    assert peer.has_tab_metadata is True


def test_peer_from_dict_renga_14_has_no_tab_metadata() -> None:
    # renga 1.4: the fields did not exist. Parsing must succeed (this is the
    # supported forward-compat case, not an error) and land on all-None with
    # the discriminator off. Paired with the test above, this IS the
    # discriminator: same shape of assertion, opposite answer, and the only
    # difference in the input is key PRESENCE.
    peer = Peer.from_dict({"id": 5, "name": "w", "role": "worker"})
    assert peer.tab is None
    assert peer.tab_name is None
    assert peer.same_tab is None
    assert peer.has_tab_metadata is False


def test_peer_from_dict_renga_20_single_tab_is_distinguishable_from_14() -> None:
    # The discriminator stated as one assertion: a single-tab renga 2.0 peer
    # and a renga 1.4 peer are indistinguishable by VALUE reachable from the
    # dataclass alone once you ignore presence (both describe "everything is
    # in my tab"), but has_tab_metadata separates them.
    new_server = Peer.from_dict({
        "id": 5, "name": "w", "role": "worker",
        "tab": 0, "tab_name": "main", "same_tab": True,
    })
    old_server = Peer.from_dict({"id": 5, "name": "w", "role": "worker"})
    assert new_server.has_tab_metadata != old_server.has_tab_metadata
    # An explicit null is still PRESENCE: a transcription that writes the key
    # with a null value came from a server that knows about tabs.
    explicit_null = Peer.from_dict({"id": 5, "name": "w", "same_tab": None})
    assert explicit_null.has_tab_metadata is True
    assert explicit_null.same_tab is None


def test_peer_from_dict_broker_shape_parses() -> None:
    # The tabless broker peer dict (broker/surface.py:774-790). It has no tab
    # keys at all, so has_tab_metadata is False -- which is the CORRECT answer
    # for the broker, not a degraded one: it has no tab concept at any layer.
    peer = Peer.from_dict({
        "id": "worker-foo",
        "name": "worker-foo",
        "role": "worker",
        "kind": "claude",
        "receive_mode": "queue",
        "cwd": "/repo",
        "summary": "doing the thing",
    })
    assert peer.id == "worker-foo"
    assert peer.name == "worker-foo"
    assert peer.role == "worker"
    assert peer.kind == "claude"
    assert peer.receive_mode == "queue"
    assert peer.cwd == "/repo"
    assert peer.summary == "doing the thing"
    assert peer.has_tab_metadata is False
    # A Peer carries no geometry by construction, so it can never reach
    # choose_split by accident (renga PeerInfo has no rect at all).
    assert not hasattr(peer, "width")
    assert not hasattr(peer, "x")


def test_peer_id_is_not_reduced_to_int() -> None:
    # Pane.from_dict runs the id through _parse_pane_id (renga int / tmux %N /
    # herdr w:pN). Peer.from_dict deliberately does NOT: broker peer ids are
    # agent handles that are not numeric at all, and the int was only ever a
    # choose_split tie-breaker, which a peer can never participate in.
    assert Peer.from_dict({"id": "manual-test"}).id == "manual-test"
    assert Peer.from_dict({"id": "worker-foo"}).id == "worker-foo"
    # A numeric id is kept as its string form rather than coerced, so the
    # value is the transcription verbatim.
    assert Peer.from_dict({"id": 17}).id == "17"
    # The contrast that makes the point: the same handle through Pane.
    assert Pane.from_dict(
        {"id": "%3", "name": "w", "x": 0, "y": 0, "w": 1, "h": 1},
    ).id == 3
    with pytest.raises(ValueError):
        Pane.from_dict({"id": "manual-test", "x": 0, "y": 0, "w": 1, "h": 1})
    # id is required; absence is a broken transcription, not a 1.4 server.
    with pytest.raises(KeyError):
        Peer.from_dict({"name": "w", "role": "worker"})


@pytest.mark.parametrize(
    "bad",
    [
        {"id": 1, "tab": True},      # bool is an int in Python; not a tab index
        {"id": 1, "tab": "2"},       # string index would address tab 2 silently
        {"id": 1, "tab": -1},        # negative index is not a workspace
        {"id": 1, "tab": 1.0},       # float index
        {"id": 1, "same_tab": "yes"},   # truthy string is not a bool
        {"id": 1, "same_tab": 1},       # int is not a bool
        {"id": 1, "tab_name": 3},       # label must be a string
        # A null id is not an id: str(None) would mint the literal handle
        # "None" and two null-id peers would collapse onto one synthetic
        # anchor, while _parse_peers' contract says every entry that HAS an id
        # parses and anything else is a real input error.
        {"id": None, "name": "worker-a", "role": "worker"},
    ],
)
def test_peer_from_dict_rejects_malformed_tab_metadata(
    bad: dict[str, Any],
) -> None:
    # Absence and malformation are DIFFERENT: a missing tab is renga 1.4 and
    # parses fine (see above), but a present-and-broken tab is a bad
    # transcription that must not be coerced into an index addressing the
    # wrong tab.
    with pytest.raises(ValueError):
        Peer.from_dict(bad)


def test_parse_peers_accepts_list_and_object() -> None:
    entries = [
        {"id": 1, "name": "dispatcher", "role": "dispatcher",
         "tab": 0, "tab_name": "main", "same_tab": True},
        {"id": 11, "name": "worker-a", "role": "worker",
         "tab": 1, "tab_name": "workers", "same_tab": False},
    ]
    bare = _parse_peers(entries)
    wrapped = _parse_peers({"peers": entries})
    assert bare == wrapped
    assert [p.name for p in bare] == ["dispatcher", "worker-a"]
    assert [p.tab for p in bare] == [0, 1]
    assert all(p.has_tab_metadata for p in bare)
    # An empty snapshot is legal (nobody bound yet) and is NOT the same as
    # peers=None, which means "the caller supplied no snapshot at all".
    assert _parse_peers([]) == []


def test_parse_peers_rejects_non_list_and_bad_entry_with_systemexit() -> None:
    # Exit-1 parity with _parse_panes: a malformed snapshot fails the whole
    # run with a clean SystemExit rather than a bare traceback.
    with pytest.raises(SystemExit) as not_a_list:
        _parse_peers({"nope": 1})
    assert "peers JSON must be a list" in str(not_a_list.value)
    with pytest.raises(SystemExit):
        _parse_peers("peers")

    with pytest.raises(SystemExit) as bad_entry:
        _parse_peers([{"id": 1}, {"id": 2, "tab": "2"}])
    # The index is named so an operator can find the offending entry.
    assert "peers[1] is invalid" in str(bad_entry.value)

    with pytest.raises(SystemExit) as missing_id:
        _parse_peers([{"name": "w"}])
    assert "peers[0] is invalid" in str(missing_id.value)

    # There is deliberately NO logical-peer skip (unlike _parse_panes): a
    # non-numeric id is the NORMAL broker shape, so it must parse, not be
    # dropped.
    assert [p.id for p in _parse_peers([{"id": "manual-test"}])] == [
        "manual-test",
    ]


def test_derive_tab_awareness_never_infers_spawn_tab() -> None:
    # A snapshot dripping with tab metadata still may not authorise a `tab`
    # key. The MCP surface cannot be probed for capabilities at all
    # (send_request_requiring gates internally and only surfaces
    # "[server_too_old] ..."), so spawn_tab is an operator ASSERTION and
    # omitting it fails closed.
    peers = [
        _peer(1, name="dispatcher", role="dispatcher", tab=0,
              tab_name="main", same_tab=True),
        _peer(11, name="worker-a", role="worker", tab=1,
              tab_name="workers", same_tab=False),
    ]
    awareness = derive_tab_awareness(peers, None)
    assert awareness.cross_tab is True
    assert awareness.capabilities_known is False
    assert awareness.spawn_tab is False
    # cross_tab_peers alone does not authorise a tab spawn either -- renga
    # made the three tokens distinct on purpose.
    partial = derive_tab_awareness(peers, frozenset({CAP_CROSS_TAB_PEERS}))
    assert partial.capabilities_known is True
    assert partial.spawn_tab is False
    asserted = derive_tab_awareness(peers, frozenset({CAP_SPAWN_TAB}))
    assert asserted.spawn_tab is True


def test_derive_tab_awareness_caller_scope_alone_is_not_cross_tab() -> None:
    # A renga#288-era server advertises caller_scope while STILL silently
    # dropping cross-tab sends (renga src/ipc/mod.rs:77/89/103), so
    # caller_scope must never license cross-tab reasoning even when the peer
    # data would support it.
    peers = [
        _peer(1, name="dispatcher", role="dispatcher", tab=0,
              tab_name="main", same_tab=True),
        _peer(11, name="worker-a", role="worker", tab=1,
              tab_name="workers", same_tab=False),
    ]
    assert derive_tab_awareness(peers, None).cross_tab is True  # premise
    caller_scope_only = derive_tab_awareness(
        peers, frozenset({CAP_CALLER_SCOPE}),
    )
    assert caller_scope_only.cross_tab is False
    assert caller_scope_only.capabilities_known is True
    assert caller_scope_only.spawn_tab is False
    # The token that DOES license it.
    both_tokens = derive_tab_awareness(
        peers, frozenset({CAP_CALLER_SCOPE, CAP_CROSS_TAB_PEERS}),
    )
    assert both_tokens.cross_tab is True


def test_derive_tab_awareness_caller_tab_from_same_tab_flag() -> None:
    # The caller's own tab is read off the peer whose same_tab is True, not
    # off list order: renga sets the flag per peer and the transcription is
    # not guaranteed to put the caller first. The caller is deliberately LAST
    # here, and a decoy sits in tab 0 (the index a list-order reading would
    # wrongly report).
    peers = [
        _peer(11, name="worker-a", role="worker", tab=0,
              tab_name="main", same_tab=False),
        _peer(21, name="worker-b", role="worker", tab=2,
              tab_name="side", same_tab=False),
        _peer(31, name="dispatcher", role="dispatcher", tab=1,
              tab_name="control", same_tab=True),
    ]
    awareness = derive_tab_awareness(peers, None)
    assert awareness.caller_tab == 1
    assert awareness.caller_tab_name == "control"

    # renga 1.4 / broker: nobody reports same_tab, so there is no caller tab
    # to name and the field must stay None rather than defaulting to 0.
    tabless = derive_tab_awareness(
        [_peer("worker-a", name="worker-a", role="worker",
               tab_metadata=False)],
        None,
    )
    assert tabless.caller_tab is None
    assert tabless.caller_tab_name is None
    assert tabless.cross_tab is False
    assert tabless.tabs == ()

    # No snapshot at all: nothing is known and nothing is claimed.
    nothing = derive_tab_awareness(None, None)
    assert nothing == runner.TabAwareness.none()


def test_tab_census_counts_and_anchor() -> None:
    peers = [
        # tab 0 (the caller's): dispatcher + one worker.
        _peer(3, name="dispatcher", role="dispatcher", tab=0,
              tab_name="main", same_tab=True),
        _peer(7, name="worker-a", role="worker", tab=0,
              tab_name="main", same_tab=True),
        # tab 1: two workers; the anchor must come from a real peer id, and
        # the smaller of the two so it does not depend on transcription order.
        _peer(19, name="worker-c", role="worker", tab=1,
              tab_name="workers", same_tab=False),
        _peer(11, name="worker-b", role="worker", tab=1,
              tab_name="workers", same_tab=False),
        # tab 2: a lone non-worker peer.
        _peer(23, name="curator", role="curator", tab=2,
              tab_name="notes", same_tab=False),
    ]
    tabs = derive_tab_awareness(peers, None).tabs
    assert [t.index for t in tabs] == [0, 1, 2]
    assert [t.name for t in tabs] == ["main", "workers", "notes"]
    assert [t.peers for t in tabs] == [2, 2, 1]
    assert [t.workers for t in tabs] == [1, 2, 0]
    # Exactly one caller tab: is_caller is derived from same_tab, so a census
    # that flagged two would mean the snapshot itself is inconsistent.
    assert [t.is_caller for t in tabs] == [True, False, False]
    assert sum(1 for t in tabs if t.is_caller) == 1
    # Anchors are real peer ids present in that tab, never the tab index.
    assert [t.anchor_pane_id for t in tabs] == [3, 11, 23]
    ids_by_tab = {0: {3, 7}, 1: {11, 19}, 2: {23}}
    for t in tabs:
        assert t.anchor_pane_id in ids_by_tab[t.index]

    # A tabless transport has no addressable anchor; that is a normal answer,
    # not an error, so the census still reports the peer counts it can.
    broker_tabs = derive_tab_awareness(
        [_peer("worker-a", name="worker-a", role="worker", tab=0,
               tab_name="main", same_tab=True)],
        None,
    ).tabs
    assert len(broker_tabs) == 1
    assert broker_tabs[0].anchor_pane_id is None
    assert broker_tabs[0].workers == 1


def test_tab_census_includes_the_caller_tab_from_your_tab_rows() -> None:
    # Regression, adversarial review (Major): renga's MCP list_peers returns
    # TEXT and the dispatcher transcribes it, and that renderer annotates the
    # CALLER's own rows as a bare " [your tab]" -- no index, no label -- while
    # foreign rows get ' [tab N "label"]' (renga src/mcp_peer/mod.rs:904-910,
    # `match (p.same_tab, p.tab)` with `(Some(true), _) => " [your tab]"`). The
    # shipped dispatcher.md instructs exactly that transcription, so a faithful
    # caller row carries same_tab and NO tab key. Grouping the census strictly
    # on `tab` therefore dropped the caller's entire tab: by_tab omitted it
    # while scope claimed "all_tabs", no row was ever `caller`, and tabs_seen
    # was off by one -- which also put the tab_limit_reached pre-flight
    # permanently out of reach (15 foreign tabs + the caller's own is 16 real
    # tabs but only 15 counted ones).
    # Built from raw dicts, not the _peer helper: the load-bearing detail is
    # that the caller rows OMIT the tab / tab_name keys entirely, which is
    # what a "[your tab]" line transcribes to.
    peers = _parse_peers([
        {"id": 1, "name": "secretary", "role": "secretary", "same_tab": True},
        {"id": 2, "name": "dispatcher", "role": "dispatcher",
         "same_tab": True},
        {"id": 3, "name": "worker-a", "role": "worker", "same_tab": True},
        {"id": 11, "name": "worker-b", "role": "worker",
         "tab": 1, "tab_name": "workers", "same_tab": False},
        {"id": 12, "name": "worker-c", "role": "worker",
         "tab": 1, "tab_name": "workers", "same_tab": False},
        {"id": 13, "name": "worker-d", "role": "worker",
         "tab": 1, "tab_name": "workers", "same_tab": False},
    ])
    # Guard the premise: this fixture really is the index-less caller shape.
    assert all(q.tab is None for q in peers[:3])
    assert all(q.has_tab_metadata for q in peers[:3])

    tabs = derive_tab_awareness(peers, None).tabs
    assert len(tabs) == 2, "the caller's own tab must be censused"
    caller, foreign = tabs
    # The caller's tab leads the census and is honestly index-less: the wire
    # genuinely did not say which index it is, so None beats a guess.
    assert caller.is_caller is True
    assert caller.index is None
    assert caller.name is None
    assert (caller.peers, caller.workers) == (3, 1)
    assert caller.anchor_pane_id == 1
    assert foreign.is_caller is False
    assert (foreign.index, foreign.peers, foreign.workers) == (1, 3, 3)
    # Every peer is accounted for exactly once.
    assert sum(t.peers for t in tabs) == len(peers)

    # ...and the consequence that made this a Major: the pre-flight can now see
    # a genuinely full tab table. 15 foreign tabs plus the caller's own IS
    # MAX_TABS, and used to report 15.
    at_limit = _parse_peers(
        [{"id": 1, "name": "dispatcher", "role": "dispatcher",
          "same_tab": True}]
        + [
            {"id": 100 + i, "name": f"w{i}", "role": "worker",
             "tab": i, "tab_name": f"t{i}", "same_tab": False}
            for i in range(1, runner.RENGA_MAX_TABS)
        ]
    )
    assert len(derive_tab_awareness(at_limit, None).tabs) == runner.RENGA_MAX_TABS

    # When a caller row DOES carry an index (a structured transcription), the
    # index-less rows join that group rather than forming a phantom second one.
    mixed = _parse_peers([
        {"id": 1, "name": "dispatcher", "role": "dispatcher",
         "tab": 2, "tab_name": "control", "same_tab": True},
        {"id": 2, "name": "worker-a", "role": "worker", "same_tab": True},
    ])
    mixed_tabs = derive_tab_awareness(mixed, None).tabs
    assert len(mixed_tabs) == 1
    assert (mixed_tabs[0].index, mixed_tabs[0].peers) == (2, 2)


# ---------------------------------------------------------------------------
# C. Population (the headline #158 fix)
# ---------------------------------------------------------------------------


def test_count_active_workers_signature_and_numbers_unchanged() -> None:
    # #158 widened the ANNOTATION only. The first parameter is still named
    # `panes`, because a rename would break any keyword caller and a keyword
    # break is not an annotation-only change.
    params = list(inspect.signature(count_active_workers).parameters)
    assert params == ["panes", "live_worker_names"]

    panes = [
        _pane(1, name="dispatcher", role="dispatcher"),
        _pane(2, name="worker-a", role="worker"),
        _pane(3, name="worker-b", role="worker"),
    ]
    assert count_active_workers(panes) == 2
    assert count_active_workers(panes=panes) == 2  # keyword caller preserved
    assert count_active_workers(panes, live_worker_names={"worker-a"}) == 1

    # The widening itself: a Peer duck-types on .role / .name, which is
    # everything the body reads, so a peer snapshot counts with the same body.
    peers = [
        _peer(1, name="dispatcher", role="dispatcher", tab_metadata=False),
        _peer(11, name="worker-a", role="worker", tab_metadata=False),
        _peer(12, name="worker-stale", role="worker", tab_metadata=False),
    ]
    assert count_active_workers(peers) == 2
    assert count_active_workers(peers, live_worker_names={"worker-a"}) == 1


def test_count_worker_population_falls_back_to_panes_when_peers_none() -> None:
    panes = [
        _pane(1, name="dispatcher", role="dispatcher"),
        _pane(2, name="worker-a", role="worker"),
        _pane(3, name="worker-b", role="worker"),
    ]
    pop = count_worker_population(panes)
    assert pop.source == "panes"
    assert pop.scope == "caller_tab"
    assert pop.tab_metadata is False
    # Numerically identical to today for every pre-#158 caller.
    assert pop.total == count_active_workers(panes) == 2
    assert pop.panes_only == 2
    assert pop.peers_only == 0
    assert pop.both == 0
    assert pop.names == ("worker-a", "worker-b")

    # The fallback deliberately does NOT dedup: count_active_workers never
    # did, and quietly deduping here would be a silent capacity change for a
    # caller that passes nothing new.
    dupes = [
        _pane(2, name="worker-a", role="worker"),
        _pane(3, name="worker-a", role="worker"),
    ]
    assert count_worker_population(dupes).total == count_active_workers(dupes) == 2


def test_count_worker_population_counts_workers_across_tabs() -> None:
    # This is what --panes-json alone cannot see: list_panes is caller-tab
    # scoped after renga#288, so two of these three workers are invisible to
    # the pane snapshot entirely.
    peers = [
        _peer(3, name="dispatcher", role="dispatcher", tab=0,
              tab_name="main", same_tab=True),
        _peer(7, name="worker-a", role="worker", tab=0,
              tab_name="main", same_tab=True),
        _peer(11, name="worker-b", role="worker", tab=1,
              tab_name="workers", same_tab=False),
        _peer(21, name="worker-c", role="worker", tab=2,
              tab_name="more", same_tab=False),
    ]
    pop = count_worker_population([], peers)
    assert pop.total == 3
    assert pop.source == "panes+peers"
    assert pop.scope == "all_tabs"
    assert pop.tab_metadata is True
    assert pop.peers_only == 3
    assert pop.panes_only == 0
    assert pop.names == ("worker-a", "worker-b", "worker-c")

    # Without tab metadata the same union is still counted, but it is only
    # honest to label it caller-tab scope (renga 1.4 list_peers is caller-tab
    # scoped, and the broker has no tabs).
    tabless = count_worker_population(
        [],
        [_peer(f"worker-{c}", name=f"worker-{c}", role="worker",
               tab_metadata=False) for c in "abc"],
    )
    assert tabless.total == 3
    assert tabless.scope == "caller_tab"
    assert tabless.tab_metadata is False


def test_count_worker_population_unions_just_spawned_pane_not_yet_a_peer() -> None:
    # The boot race: a worker is a PANE for the ~10-30s before its peer bind
    # registers (this module's own after_spawn step waits up to ~30s for it).
    # If peers simply replaced panes, a second delegate-plan inside that
    # window would undercount and over-spawn.
    panes = [
        _pane(2, name="worker-fresh", role="worker"),
        _pane(1, name="dispatcher", role="dispatcher"),
    ]
    peers = [
        _peer(3, name="dispatcher", role="dispatcher", tab=0,
              tab_name="main", same_tab=True),
        _peer(11, name="worker-b", role="worker", tab=1,
              tab_name="workers", same_tab=False),
    ]
    pop = count_worker_population(panes, peers)
    assert pop.total == 2
    assert pop.panes_only == 1
    assert pop.peers_only == 1
    assert pop.both == 0
    assert pop.names == ("worker-b", "worker-fresh")
    assert "worker-fresh" in pop.names


def test_count_worker_population_dedupes_on_name_not_pane_id() -> None:
    # The broker's two surfaces use DISJOINT id spaces: list_peers emits
    # id=agent_id ("worker-a") while list_panes emits id=adapter handle
    # ("%3"). Dedup on a parsed pane id would therefore never match and would
    # double-count every broker worker; dedup on `name` is the only key
    # present on both surfaces of every transport.
    panes = _parse_panes([
        {"id": "%3", "name": "worker-a", "role": "worker",
         "x": 0, "y": 0, "w": 100, "h": 50},
        {"id": "%4", "name": "worker-b", "role": "worker",
         "x": 100, "y": 0, "w": 100, "h": 50},
    ])
    peers = _parse_peers([
        {"id": "worker-a", "name": "worker-a", "role": "worker"},
        {"id": "worker-b", "name": "worker-b", "role": "worker"},
    ])
    # Guard the premise: the id spaces really do not overlap.
    assert {p.id for p in panes} == {3, 4}
    assert {q.id for q in peers} == {"worker-a", "worker-b"}

    pop = count_worker_population(panes, peers)
    assert pop.total == 2, "the same two workers were counted twice"
    assert pop.both == 2
    assert pop.panes_only == 0
    assert pop.peers_only == 0
    assert pop.names == ("worker-a", "worker-b")

    # The renga case, where the numeric ids DO agree, must land on the same
    # answer -- the dedup key is name either way.
    renga_panes = _parse_panes([
        {"id": 7, "name": "worker-a", "role": "worker",
         "x": 0, "y": 0, "w": 100, "h": 50},
    ])
    renga_peers = _parse_peers([
        {"id": 7, "name": "worker-a", "role": "worker",
         "tab": 0, "tab_name": "main", "same_tab": True},
    ])
    assert count_worker_population(renga_panes, renga_peers).total == 1


def test_count_worker_population_is_not_gated_on_cross_tab_capability() -> None:
    # The count needs no discriminator and no capability token: renga 1.4
    # list_peers is already caller-tab-only and renga 2.0 spans tabs, so
    # counting every worker peer is correct on both without knowing which one
    # you are talking to. Gating the count on cross_tab_peers would make the
    # correctness fix inert until a second repo shipped a matching change.
    assert "server_capabilities" not in inspect.signature(
        count_worker_population,
    ).parameters

    peers = _parse_peers([
        {"id": 7, "name": "worker-a", "role": "worker",
         "tab": 0, "tab_name": "main", "same_tab": True},
        {"id": 11, "name": "worker-b", "role": "worker",
         "tab": 1, "tab_name": "workers", "same_tab": False},
        {"id": 21, "name": "worker-c", "role": "worker",
         "tab": 2, "tab_name": "more", "same_tab": False},
    ])
    assert count_worker_population([], peers).total == 3
    # And end to end: no token asserted anywhere, yet the broker ceiling still
    # sees all three tabs.
    assert derive_tab_awareness(peers, None).spawn_tab is False
    plan = build_plan(
        {"task_id": "demo", "worker_dir": "/tmp"},
        [],
        Path("/nonexistent-state"),
        transport="broker",
        capacity_policy=CapacityPolicy(max_concurrent_workers=3),
        peers=peers,
        server_capabilities=None,
    )
    assert plan.status == "split_capacity_exceeded"
    assert plan.capacity is not None
    assert plan.capacity["active_workers"] == 3
    assert plan.population is not None
    assert plan.population["capabilities_known"] is False


def test_count_worker_population_respects_live_worker_names() -> None:
    # A worker that renga still lists but the registry says is dead must not
    # permanently consume a slot -- including one in ANOTHER tab, which is
    # exactly the entry --panes-json could never have seen.
    panes = [_pane(2, name="worker-a", role="worker")]
    peers = [
        _peer(7, name="worker-a", role="worker", tab=0,
              tab_name="main", same_tab=True),
        _peer(11, name="worker-stale", role="worker", tab=1,
              tab_name="workers", same_tab=False),
    ]
    assert count_worker_population(panes, peers).total == 2  # premise
    pop = count_worker_population(panes, peers, live_worker_names={"worker-a"})
    assert pop.total == 1
    assert pop.names == ("worker-a",)
    assert pop.both == 1
    assert "worker-stale" not in pop.names

    # An anonymous worker is dropped by a non-None liveness set, because
    # ``None in live_worker_names`` is False -- matching today's behaviour.
    anon = count_worker_population(
        [_pane(3, role="worker")],
        [],
        live_worker_names={"worker-a"},
    )
    assert anon.total == 0
    assert anon.anonymous == 0


def test_count_worker_population_anonymous_workers_not_deduped() -> None:
    # role=worker with no name cannot be deduped at all: dropping them would
    # undercount and merging them would collapse two real workers into one.
    # So they are added on top of the name union.
    panes = [_pane(2, role="worker")]
    peers = [
        _peer(11, role="worker", tab=1, tab_name="workers", same_tab=False),
        _peer(12, name="worker-a", role="worker", tab=1,
              tab_name="workers", same_tab=False),
    ]
    pop = count_worker_population(panes, peers)
    assert pop.anonymous == 2
    assert pop.total == 3          # 2 anonymous + 1 named
    assert pop.names == ("worker-a",)   # names excludes the anonymous pair
    assert pop.both == 0


def test_count_worker_population_tolerates_an_unhashable_name(
    tmp_path: Path,
) -> None:
    # Regression, adversarial review (Major): neither Pane.from_dict nor
    # Peer.from_dict type-checks `name` (both do a bare `d.get("name")`), and
    # #158 made build_plan call count_worker_population UNCONDITIONALLY -- so a
    # snapshot carrying `"name": ["dispatcher"]` started dying with an
    # unhandled `TypeError: unhashable type: 'list'` and a bare traceback where
    # origin/main emitted a valid plan and exited 0. That is a regression for
    # every existing caller, not just the new --peers-json path, and it breaks
    # _parse_panes' own promise to turn malformed input into a clean SystemExit
    # "instead of letting a bare traceback escape".
    panes = _parse_panes([
        {"id": 1, "name": ["dispatcher"], "role": "worker", "focused": True,
         "x": 0, "y": 0, "width": 200, "height": 40},
    ])
    peers = _parse_peers([{"id": 2, "name": {"k": "v"}, "role": "worker"}])

    # An unusable dedup key is reported as anonymous -- the branch this module
    # already documents for "cannot be deduped" -- which over-counts rather
    # than under-counts, the fail-safe direction for a capacity ceiling.
    solo = count_worker_population(panes)
    assert solo.total == 1
    assert solo.anonymous == 1
    assert solo.names == ()

    union = count_worker_population(panes, peers)
    assert union.total == 2
    assert union.anonymous == 2
    assert union.names == ()

    # The liveness filter hashes its operand too, and #158 put that on the
    # renga path for the first time.
    assert count_worker_population(
        panes, peers, live_worker_names={"worker-a"},
    ).total == 0

    # End to end: a plan, not a traceback.
    plan = build_plan(
        {"task_id": "demo", "worker_dir": str(tmp_path)},
        panes,
        tmp_path / ".state",
        transport="renga",
    )
    assert plan.status == "ready_to_spawn"


# ---------------------------------------------------------------------------
# D. build_plan population / duplicate name
# ---------------------------------------------------------------------------


def _six_worker_peers() -> list[Peer]:
    """Six workers spread over three tabs; only two are visible as panes."""
    return [
        _peer(3, name="dispatcher", role="dispatcher", tab=0,
              tab_name="main", same_tab=True),
        _peer(7, name="worker-a", role="worker", tab=0,
              tab_name="main", same_tab=True),
        _peer(8, name="worker-b", role="worker", tab=0,
              tab_name="main", same_tab=True),
        _peer(11, name="worker-c", role="worker", tab=1,
              tab_name="workers", same_tab=False),
        _peer(12, name="worker-d", role="worker", tab=1,
              tab_name="workers", same_tab=False),
        _peer(21, name="worker-e", role="worker", tab=2,
              tab_name="more", same_tab=False),
        _peer(22, name="worker-f", role="worker", tab=2,
              tab_name="more", same_tab=False),
    ]


def _two_worker_panes() -> list[Pane]:
    """The caller-tab view of the same fleet: list_panes sees only these."""
    return [
        _pane(7, name="worker-a", role="worker", x=0, y=0, w=100, h=50),
        _pane(8, name="worker-b", role="worker", x=100, y=0, w=100, h=50),
    ]


def test_build_plan_broker_capacity_counts_cross_tab_peers(
    tmp_path: Path,
) -> None:
    # THE #158 BUG. list_panes is caller-tab-scoped after renga#288, so the
    # ceiling computed from panes alone reads 2 out of a real fleet of 6 and
    # happily spawns a seventh. With the peer snapshot the same ceiling reads
    # 6 and refuses.
    plan = build_plan(
        _task(tmp_path),
        _two_worker_panes(),
        tmp_path / ".state",
        transport="broker",
        capacity_policy=CapacityPolicy(max_concurrent_workers=5),
        peers=_six_worker_peers(),
    )
    assert plan.status == "split_capacity_exceeded"
    assert plan.capacity is not None
    assert plan.capacity["active_workers"] == 6
    assert plan.capacity["max_concurrent_workers"] == 5
    assert plan.capacity["free_worker_slots"] == 0
    assert plan.spawn is None
    assert plan.state_writes == []
    assert "active_workers=6" in plan.escalate["message"]
    # And the census is auditable rather than a bare number.
    assert plan.population is not None
    assert plan.population["active_workers"] == 6
    assert plan.population["source"] == "panes+peers"
    assert plan.population["scope"] == "all_tabs"
    assert plan.population["both"] == 2      # worker-a / worker-b on both
    assert plan.population["peers_only"] == 4
    assert plan.population["panes_only"] == 0
    assert plan.population["names"] == [
        "worker-a", "worker-b", "worker-c",
        "worker-d", "worker-e", "worker-f",
    ]


def test_build_plan_broker_capacity_without_peers_is_unchanged(
    tmp_path: Path,
) -> None:
    # The same fixture with no peer snapshot is exactly today's behaviour --
    # including today's over-spawn. This is the control that proves the test
    # above is measuring the peer snapshot and not something incidental, and
    # it pins that omitting --peers-json changes nothing for existing callers.
    plan = build_plan(
        _task(tmp_path),
        _two_worker_panes(),
        tmp_path / ".state",
        transport="broker",
        capacity_policy=CapacityPolicy(max_concurrent_workers=5),
    )
    assert plan.status == "ready_to_spawn"
    assert plan.capacity is not None
    assert plan.capacity["active_workers"] == 2
    assert plan.capacity["free_worker_slots"] == 3
    # All three #158 fields stay None on a path that existed before it.
    assert plan.population is None
    assert plan.layout is None
    assert plan.on_spawn_error is None
    assert "tab" not in plan.spawn


def test_build_plan_duplicate_name_same_tab_message_verbatim(
    tmp_path: Path,
) -> None:
    # Consumers forward this sentence verbatim, so it is pinned as a literal.
    # Both routes into it -- a pane hit (pre-#158) and a same-tab peer hit
    # (new) -- must produce the SAME string: "in the tab" is the honest
    # description in both cases, and rewording would break ja.
    pane_hit = build_plan(
        _task(tmp_path),
        _ok_panes() + [_pane(99, name="worker-demo", role="worker")],
        tmp_path / ".state",
    )
    assert pane_hit.status == "input_invalid"
    assert pane_hit.errors == [_CLASSIC_DUPLICATE_MESSAGE]

    peer_hit = build_plan(
        _task(tmp_path),
        _ok_panes(),
        tmp_path / ".state",
        peers=[
            _peer(3, name="dispatcher", role="dispatcher", tab=0,
                  tab_name="main", same_tab=True),
            _peer(99, name="worker-demo", role="worker", tab=0,
                  tab_name="main", same_tab=True),
        ],
    )
    assert peer_hit.status == "input_invalid"
    assert peer_hit.errors == [_CLASSIC_DUPLICATE_MESSAGE]

    # renga 1.4 / the tabless broker send no tab metadata at all. "in the tab"
    # is still the honest wording there, so the classic message applies rather
    # than one naming a tab index the server never reported.
    no_metadata = build_plan(
        _task(tmp_path),
        _ok_panes(),
        tmp_path / ".state",
        peers=[_peer("worker-demo", name="worker-demo", role="worker",
                     tab_metadata=False)],
    )
    assert no_metadata.status == "input_invalid"
    assert no_metadata.errors == [_CLASSIC_DUPLICATE_MESSAGE]


def test_build_plan_duplicate_name_is_union_of_panes_and_peers(
    tmp_path: Path,
) -> None:
    # The guard was WIDENED, not replaced. A worker that exists as a pane but
    # has not yet bound as a peer is absent from the peer snapshot entirely,
    # so a guard that only consulted peers would let a second worker-demo
    # spawn during the ~10-30s bind window.
    peers = [
        _peer(3, name="dispatcher", role="dispatcher", tab=0,
              tab_name="main", same_tab=True),
        _peer(11, name="worker-other", role="worker", tab=1,
              tab_name="workers", same_tab=False),
    ]
    assert all(q.name != "worker-demo" for q in peers)  # premise

    plan = build_plan(
        _task(tmp_path),
        _ok_panes() + [_pane(99, name="worker-demo", role="worker")],
        tmp_path / ".state",
        peers=peers,
    )
    assert plan.status == "input_invalid"
    assert plan.errors == [_CLASSIC_DUPLICATE_MESSAGE]

    # Same union, but through the zero-geometry pane that _parse_panes
    # deliberately keeps alive precisely so this guard can see it
    # (suisya-systems/claude-org-ja#580). Supplying peers must not drop it.
    zero_geometry = build_plan(
        _task(tmp_path),
        _parse_panes([
            {"id": "%0", "name": "dispatcher", "role": "dispatcher",
             "x": 0, "y": 0, "w": 200, "h": 50},
            {"id": "%9", "name": "worker-demo", "role": "worker",
             "x": 0, "y": 0, "w": 0, "h": 0},
        ]),
        tmp_path / ".state",
        peers=peers,
    )
    assert zero_geometry.status == "input_invalid"
    assert zero_geometry.errors == [_CLASSIC_DUPLICATE_MESSAGE]

    # And the other direction of the union: a peer-only hit with no pane.
    peer_only = build_plan(
        _task(tmp_path),
        _ok_panes(),
        tmp_path / ".state",
        peers=peers + [
            _peer(31, name="worker-demo", role="worker", tab=0,
                  tab_name="main", same_tab=True),
        ],
    )
    assert peer_only.status == "input_invalid"


def test_build_plan_duplicate_name_other_tab_names_the_tab(
    tmp_path: Path,
) -> None:
    # worker-<task_id> is the ORG-WIDE identity behind the seed file, the
    # outbox file and name-addressed send_message, so a collision in another
    # tab is just as fatal -- but the classic "in the tab" wording would send
    # the operator hunting through the wrong tab, so this branch names the
    # index and the label it must look in.
    plan = build_plan(
        _task(tmp_path),
        _ok_panes(),
        tmp_path / ".state",
        peers=[
            _peer(3, name="dispatcher", role="dispatcher", tab=0,
                  tab_name="main", same_tab=True),
            _peer(99, name="worker-demo", role="worker", tab=2,
                  tab_name="build", same_tab=False),
        ],
    )
    assert plan.status == "input_invalid"
    assert len(plan.errors) == 1
    msg = plan.errors[0]
    assert msg != _CLASSIC_DUPLICATE_MESSAGE
    assert "already exists in tab 2" in msg
    assert "'build'" in msg
    assert "org-wide identity" in msg
    # No state file was written, so the operator can simply re-run.
    assert plan.state_writes == []
    assert not (tmp_path / ".state").exists()
    # ASCII only: the plan is printed to stdout and a cp932 console would
    # crash on an em-dash.
    msg.encode("cp932")
