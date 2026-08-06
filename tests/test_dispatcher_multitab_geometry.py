"""Sidebar geometry / anti-double-count tests for the dispatcher runner.

Covers runtime #158 (renga 2.0 multi-tab capacity), design section 10 group A.

The whole point of this module is that renga's org sidebar and file tree are
folded into the capacity model by MEASUREMENT and never by ARITHMETIC. renga
carves every left panel off the frame BEFORE the pane layout runs
(``layout_geometry::compute`` does ``pane_w = width - org_w - tree_w -
preview_w`` and advances the pane area's origin past each panel), and the
resulting ``layout.panes`` Rect is the only thing ever handed to
``LayoutNode::calculate_rects`` -- whose output is copied verbatim onto the
wire. So every rect this runtime receives is already net of the sidebar.

That makes "subtract ORG_SIDEBAR_DEFAULT_WIDTH from the pane before halving
it" the single most attractive wrong fix in this codebase: it reads like it is
accounting for the sidebar, and it is in fact a second subtraction of a width
renga already removed. It would also desync this module's prediction from
renga's own split guard (``rect.width / 2 < min_pane_width``), which judges the
very same rects -- manufacturing split_capacity_exceeded on layouts renga would
happily split.

Tests 1-3 below are therefore FUSES, not coverage: they exist to fail loudly
the day somebody "fixes" the sidebar by subtracting it. Their assertion
messages explain the trap so the failure is self-diagnosing at 3am.

Test 4 is the sibling fuse on the real claude-org-runtime #35 live layout; the
plain regression form of it lives in ``tests/test_dispatcher_runner.py`` as
``test_choose_split_live_failure_258x42_yields_valid_choice`` and is
deliberately left untouched there, so the fuse here only adds the
anti-subtraction assertions that the plain form cannot make. It carries a
DIFFERENT name on purpose: two tests sharing one name across modules would
make ``-k`` select both and would leave a failure report ambiguous about
which property actually broke -- the layout regression or the sidebar fuse.
"""

from __future__ import annotations

import pytest

from claude_org_runtime.dispatcher import runner
from claude_org_runtime.dispatcher.runner import (
    DEFAULT_FILE_TREE_WIDTH,
    MIN_PANE_HEIGHT,
    MIN_PANE_WIDTH,
    ORG_SIDEBAR_COMPACT_WIDTH,
    ORG_SIDEBAR_DEFAULT_WIDTH,
    Pane,
    PaneArea,
    SplitChoice,
    choose_split,
    explain_left_panels,
    new_tab_pane_estimate,
    pane_area_bbox,
)

# The real-world left-panel total behind claude-org-ja#823: the default org
# sidebar plus the default file tree put the first pane's origin at x=46. Used
# as the translation offset and as the "wrong subtrahend" in the fuses below.
_LIVE_LEFT_PANELS = ORG_SIDEBAR_DEFAULT_WIDTH + DEFAULT_FILE_TREE_WIDTH  # 46


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
    """Build a Pane the same way ``tests/test_dispatcher_runner.py`` does."""
    return Pane(
        id=pid, name=name, role=role, focused=focused,
        x=x, y=y, width=w, height=h,
    )


def _ok_panes() -> list[Pane]:
    """The canonical splittable layout from ``tests/test_dispatcher_runner.py``."""
    return [
        _pane(1, name="curator", role="curator", x=0, y=0, w=100, h=50),
        _pane(2, name="dispatcher", role="dispatcher", x=100, y=0, w=200, h=50),
    ]


def _live_35_panes() -> list[Pane]:
    """The claude-org-runtime #35 live failure layout (secretary 258x42)."""
    return [
        _pane(3, name="secretary", role="secretary", x=0, y=0, w=258, h=42),
        _pane(2, name="dispatcher", role="dispatcher", x=0, y=42, w=258, h=42),
    ]


def _translate_x(panes: list[Pane], dx: int) -> list[Pane]:
    """Shift every rect right by ``dx``, preserving widths.

    This is exactly what turning the sidebar on does to a real snapshot: the
    origins move right, the widths are already the post-carve remainder. A
    correct implementation must be blind to it, because ``rect_adjacent`` --
    the only geometric relation ``choose_split`` consults -- is
    translation-invariant.
    """
    return [
        _pane(p.id, name=p.name, role=p.role, x=p.x + dx, y=p.y,
              w=p.width, h=p.height, focused=p.focused)
        for p in panes
    ]


# ---------------------------------------------------------------------------
# 1-4. Anti-double-count fuses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "layout_name, layout",
    [
        ("ok_panes", _ok_panes()),
        ("live_35", _live_35_panes()),
    ],
)
@pytest.mark.parametrize(
    "dx",
    [
        ORG_SIDEBAR_DEFAULT_WIDTH,   # 26 -- sidebar at its default width
        ORG_SIDEBAR_COMPACT_WIDTH,   # 16 -- sidebar in compact mode
        _LIVE_LEFT_PANELS,           # 46 -- sidebar + file tree (ja#823)
    ],
)
def test_choose_split_identical_when_all_rects_translated_by_sidebar_width(
    layout_name: str, layout: list[Pane], dx: int,
) -> None:
    # THE PRIMARY DOUBLE-COUNT FUSE.
    #
    # Turning renga's left panels on translates every pane rect right by their
    # total width; it does not change any pane's width. choose_split must
    # therefore return a SplitChoice equal in EVERY field, because the only
    # geometric relation it consults (rect_adjacent) is translation-invariant
    # and _split_options only ever halves widths/heights.
    #
    # If someone ever teaches _split_options / choose_split about
    # ORG_SIDEBAR_*, the shifted layout stops matching the unshifted one and
    # this test fires.
    baseline = choose_split(layout)
    shifted = choose_split(_translate_x(layout, dx))

    assert baseline is not None, f"{layout_name} must be splittable to begin with"
    assert shifted is not None, (
        f"choose_split found no candidate for the {layout_name} layout once "
        f"every rect was translated +{dx} columns in x. Translation cannot "
        f"remove a split candidate: renga's rects are FRAME-ABSOLUTE and "
        f"already net of the left panels (layout_geometry::compute subtracts "
        f"org_w/tree_w/preview_w before calculate_rects runs), so a sidebar "
        f"only moves origins. A None here means split capacity is being "
        f"computed from x -- i.e. a sidebar width is being subtracted a "
        f"SECOND time. Fold the sidebar in by MEASUREMENT (pane_area_bbox), "
        f"never by arithmetic on a rect."
    )
    assert shifted == baseline, (
        f"choose_split is not translation-invariant on the {layout_name} "
        f"layout: shifting every rect +{dx} columns in x changed the choice "
        f"from {baseline!r} to {shifted!r}. This is the double-count trap. "
        f"renga already removed the left-panel columns before it handed these "
        f"rects over (pane_w = width - org_w - tree_w - preview_w), so "
        f"subtracting ORG_SIDEBAR_* / DEFAULT_FILE_TREE_WIDTH here subtracts "
        f"them twice AND desyncs this module from renga's own split guard "
        f"(layout_ops.rs: rect.width / 2 < min_pane_width), which judges these "
        f"exact rects -- manufacturing split_capacity_exceeded on layouts "
        f"renga would happily split."
    )


def test_choose_split_child_width_is_exact_half() -> None:
    # Fuse #2, stated as arithmetic rather than as an invariance: on the
    # ja#823 x=46 layout (org sidebar 26 + file tree 20 pushed the pane area
    # right), the split child must be EXACTLY half the target's own width.
    # Every plausible subtraction produces a different number, and each one is
    # asserted against by value so the failure names the subtrahend.
    panes = [
        _pane(1, name="curator", role="curator",
              x=_LIVE_LEFT_PANELS, y=1, w=100, h=42),
        _pane(2, name="dispatcher", role="dispatcher",
              x=_LIVE_LEFT_PANELS + 100, y=1, w=212, h=42),
    ]
    choice = choose_split(panes)
    assert choice is not None
    assert choice.target_name == "dispatcher"

    target_width = 212
    assert choice.new_w == target_width // 2 == 106, (
        f"expected the split child to be exactly half of the dispatcher's own "
        f"{target_width}-column rect (106), got {choice.new_w}. The rect is "
        f"already net of the left panels; halving anything smaller is a "
        f"double subtraction."
    )
    for label, subtrahend in (
        ("ORG_SIDEBAR_DEFAULT_WIDTH", ORG_SIDEBAR_DEFAULT_WIDTH),
        ("ORG_SIDEBAR_COMPACT_WIDTH", ORG_SIDEBAR_COMPACT_WIDTH),
        ("DEFAULT_FILE_TREE_WIDTH", DEFAULT_FILE_TREE_WIDTH),
        ("sidebar + file tree", _LIVE_LEFT_PANELS),
        # x itself is the measured left-panel total; subtracting the observed
        # value is the same bug wearing a different hat.
        ("the measured x offset", panes[1].x),
    ):
        assert choice.new_w != (target_width - subtrahend) // 2, (
            f"choose_split returned {choice.new_w}, which is exactly "
            f"({target_width} - {label}) // 2. Some code path is subtracting "
            f"{label} from a pane rect before halving it. renga already "
            f"subtracted the left panels before emitting these rects -- see "
            f"the module docstring; fold the sidebar in via pane_area_bbox "
            f"(measured) instead."
        )
    # The halved dimension is the only one that changes.
    assert choice.new_h == 42


@pytest.mark.parametrize(
    "const_name",
    [
        "ORG_SIDEBAR_DEFAULT_WIDTH",
        "ORG_SIDEBAR_COMPACT_WIDTH",
        "DEFAULT_FILE_TREE_WIDTH",
    ],
)
def test_sidebar_constants_are_documentation_only(
    monkeypatch: pytest.MonkeyPatch, const_name: str,
) -> None:
    # Fuse #3, and the structural one: the layout-mirror constants are prose,
    # not operands. Set one to an absurd value and NOTHING about split
    # capacity may move.
    #
    # The explain_left_panels() assertion at the end is what keeps this test
    # from being vacuous: it proves the monkeypatch actually took effect (the
    # constant is read from module globals at call time, not frozen into a
    # default argument at import), so the unchanged choose_split results below
    # are evidence rather than an artefact.
    layouts = [_ok_panes(), _live_35_panes(),
               _translate_x(_ok_panes(), _LIVE_LEFT_PANELS)]
    before = [choose_split(layout) for layout in layouts]
    assert all(choice is not None for choice in before)

    monkeypatch.setattr(runner, const_name, 999)

    after = [choose_split(layout) for layout in layouts]
    assert after == before, (
        f"setting runner.{const_name} to 999 changed choose_split's output "
        f"from {before!r} to {after!r}. The renga layout mirrors are "
        f"REPORTING ONLY: they exist so escalation copy can name the panels "
        f"eating the frame, and they must never be an operand in a capacity "
        f"comparison. A capacity number that moves when a documentation "
        f"constant moves is a double subtraction of a width renga already "
        f"removed."
    )
    assert "999" in explain_left_panels(46), (
        f"runner.{const_name} was patched to 999 but explain_left_panels() "
        f"did not report it, so this test proved nothing about choose_split. "
        f"The constant must be read from module globals at call time and only "
        f"there (explain_left_panels is its single reader)."
    )


def test_choose_split_live_258x42_child_is_not_sidebar_adjusted() -> None:
    # Fuse #4: the same claude-org-runtime #35 live layout that
    # test_choose_split_live_failure_258x42_yields_valid_choice pins in
    # tests/test_dispatcher_runner.py, re-asserted here for the one thing
    # that test cannot say -- that 129 is not any sidebar-adjusted number.
    # The name deliberately differs from the plain regression's: a shared
    # name would make a failure report ambiguous about which of the two
    # properties broke. A subtraction survives the "is not None" check on this
    # layout (258 - 26 = 232, whose half 116 still clears DISPATCHER_MIN_WIDTH
    # = 80), so the value assertions are what actually catch it.
    choice = choose_split(_live_35_panes())
    assert choice is not None
    assert choice.target_name == "dispatcher"
    assert choice.role == "dispatcher"
    assert choice.direction == "vertical"
    assert choice.new_h == 42
    assert choice.new_w == 258 // 2 == 129, (
        f"the #35 live layout must still split the dispatcher's 258-column "
        f"rect at exactly 129, got {choice.new_w}."
    )
    for label, subtrahend in (
        ("ORG_SIDEBAR_DEFAULT_WIDTH", ORG_SIDEBAR_DEFAULT_WIDTH),
        ("ORG_SIDEBAR_COMPACT_WIDTH", ORG_SIDEBAR_COMPACT_WIDTH),
        ("DEFAULT_FILE_TREE_WIDTH", DEFAULT_FILE_TREE_WIDTH),
        ("sidebar + file tree", _LIVE_LEFT_PANELS),
    ):
        assert choice.new_w != (258 - subtrahend) // 2, (
            f"the #35 dispatcher child came back as (258 - {label}) // 2. "
            f"That is the double-count trap: renga's 258 is already the "
            f"post-carve width. Operators confirmed 129x42 usable in live "
            f"operation; a narrower child refuses splits renga allows."
        )


# ---------------------------------------------------------------------------
# 5-7. pane_area_bbox -- the sidebar as an OBSERVED quantity
# ---------------------------------------------------------------------------


def test_pane_area_bbox_measures_exact_tiled_area() -> None:
    # renga's calculate_rects / split_rect tile layout.panes with no gaps and
    # no overlap, so the bounding box of the pane rects IS the pane area renga
    # computed after carving off every left panel. Two panes tiled side by
    # side at x=46 must reconstruct it exactly -- no rounding, no slack.
    panes = [
        _pane(1, name="dispatcher", role="dispatcher", x=46, y=1, w=100, h=42),
        _pane(2, name="worker-a", role="worker", x=146, y=1, w=112, h=42),
    ]
    area = pane_area_bbox(panes)
    assert area == PaneArea(x=46, y=1, width=212, height=42, pane_count=2)
    # 46 + 212 == 258: the measurement accounts for every column from the
    # pane area's origin to the right edge of the last tile.
    assert area.x + area.width == 258
    assert area.left_panels_columns == 46


def test_pane_area_bbox_ignores_zero_geometry_and_returns_none_when_empty() -> None:
    # The broker's ``w=h=0`` logical-pane sentinel is a real entry in ``panes``
    # (kept alive on purpose so duplicate-name detection still sees it), and it
    # is conventionally parked at the origin. If it were bounded it would drag
    # the measured origin to x=0 and report the left panels as 0 columns --
    # silently deleting the sidebar from the capacity model.
    sentinel = _pane(9, name="worker-logical", role="worker", x=0, y=0, w=0, h=0)
    panes = [
        sentinel,
        _pane(1, name="dispatcher", role="dispatcher", x=46, y=1, w=212, h=42),
    ]
    area = pane_area_bbox(panes)
    assert area is not None
    assert area.x == 46, (
        f"the w=h=0 logical-pane sentinel dragged the measured pane-area "
        f"origin to x={area.x}. Zero-geometry panes must be excluded from the "
        f"bounding box: they are identity records, not rects, and bounding "
        f"them reports left_panels_columns=0 on a layout that really has a "
        f"sidebar."
    )
    assert area.pane_count == 1, (
        "pane_count must count only measurable rects; the logical sentinel "
        "is not one."
    )
    assert area.width == 212 and area.height == 42 and area.y == 1

    # Nothing measurable at all -> None, not a zero-sized PaneArea (which
    # would read as "the pane area is 0x0" and fail every floor).
    assert pane_area_bbox([]) is None
    assert pane_area_bbox([sentinel]) is None


@pytest.mark.parametrize(
    "x, why",
    [
        (0, "sidebar and file tree both off"),
        (ORG_SIDEBAR_COMPACT_WIDTH, "compact sidebar alone"),
        (ORG_SIDEBAR_DEFAULT_WIDTH, "default sidebar alone"),
        (_LIVE_LEFT_PANELS, "default sidebar + file tree (ja#823)"),
        (33, "a width no constant in this module predicts"),
        (120, "sidebar + file tree + a swapped preview panel"),
    ],
)
def test_pane_area_left_panels_columns_is_measured_not_assumed(
    x: int, why: str,
) -> None:
    # left_panels_columns is the sidebar folded into the capacity model as an
    # OBSERVED quantity, so it must equal whatever the snapshot says -- in
    # every sidebar mode, including ones no constant here describes. The
    # decomposition (org_w + tree_w + maybe preview_w) is one equation in
    # three unknowns and is never attempted.
    area = pane_area_bbox(
        [_pane(1, name="dispatcher", role="dispatcher", x=x, y=1, w=212, h=42)]
    )
    assert area is not None
    assert area.left_panels_columns == x == area.x, (
        f"left_panels_columns must be the measured origin ({x}, {why}), not a "
        f"constant this module believes in. Got {area.left_panels_columns}."
    )
    # And the pane's own width is untouched by the measurement: measuring the
    # left panels must never shrink the pane area it measured them from.
    assert area.width == 212


# ---------------------------------------------------------------------------
# 8-9. new_tab_pane_estimate / explain_left_panels
# ---------------------------------------------------------------------------


def test_new_tab_pane_estimate_does_not_halve() -> None:
    # A ``tab:{new}`` pane is the new tab's ONLY pane, not a split child
    # (renga creates the workspace single-pane), so the estimate must compare
    # the WHOLE measured bbox against the MIN_PANE_* floors. Halving here --
    # copy-pasting the _split_options habit -- would refuse overflow into a
    # fresh tab that renga would lay out fine.
    panes = [
        _pane(1, name="dispatcher", role="dispatcher", x=46, y=1, w=100, h=42),
        _pane(2, name="worker-a", role="worker", x=146, y=1, w=112, h=42),
    ]
    estimate = new_tab_pane_estimate(panes)
    assert estimate is not None
    assert estimate["width"] == 212, (
        f"the new-tab estimate reported width {estimate['width']} for a "
        f"212-column pane area. The new tab's pane is not a split child; "
        f"nothing may be halved and no sidebar may be subtracted."
    )
    assert estimate["height"] == 42
    assert estimate["fits"] is True
    assert estimate["advisory"] is True

    # ``fits`` flips exactly at the MIN_PANE_* floors, with no halving. A pane
    # area of exactly MIN_PANE_WIDTH x MIN_PANE_HEIGHT fits; one column or one
    # row less does not. Under a ``// 2`` implementation the exact-floor case
    # would be refused, which is what these three cases pin.
    def _estimate_for(w: int, h: int) -> dict[str, object]:
        est = new_tab_pane_estimate([_pane(1, name="d", role="dispatcher",
                                           x=46, y=1, w=w, h=h)])
        assert est is not None
        return est

    exact = _estimate_for(MIN_PANE_WIDTH, MIN_PANE_HEIGHT)
    assert exact["fits"] is True, (
        f"a pane area of exactly {MIN_PANE_WIDTH}x{MIN_PANE_HEIGHT} must fit "
        f"a new tab's single pane. A False here means the estimate halved the "
        f"bbox (or subtracted a sidebar) before comparing it to the floors."
    )
    assert exact["width"] == MIN_PANE_WIDTH
    assert exact["height"] == MIN_PANE_HEIGHT
    assert _estimate_for(MIN_PANE_WIDTH - 1, MIN_PANE_HEIGHT)["fits"] is False
    assert _estimate_for(MIN_PANE_WIDTH, MIN_PANE_HEIGHT - 1)["fits"] is False
    # Just under twice the floors: a halving implementation would refuse this,
    # a correct one accepts it. The sharpest single anti-halving case.
    near_double = _estimate_for(2 * MIN_PANE_WIDTH - 1, 2 * MIN_PANE_HEIGHT - 1)
    assert near_double["fits"] is True, (
        f"a {2 * MIN_PANE_WIDTH - 1}x{2 * MIN_PANE_HEIGHT - 1} pane area was "
        f"refused. Halved it would be just under the floors -- so the "
        f"estimate is halving. It must not: the new tab's pane is the whole "
        f"area, not half of it."
    )
    assert near_double["advisory"] is True

    # Unmeasurable input -> None, never a fabricated estimate.
    assert new_tab_pane_estimate([]) is None


def test_explain_left_panels_is_a_hypothesis_and_ascii() -> None:
    # The decomposition of the measured column total is one equation in three
    # unknowns, so the copy must name the candidate widths WITHOUT claiming
    # them, and must survive a cp932 console: this string is interpolated into
    # plan JSON that is printed to stdout, where an em-dash or a smart quote
    # crashes the run on Windows. pytest captures stdout as UTF-8, so this
    # assertion is the only place that trap is catchable.
    text = explain_left_panels(46)

    assert "46" in text, "the MEASURED total must lead the explanation"
    assert str(ORG_SIDEBAR_DEFAULT_WIDTH) in text      # 26
    assert str(ORG_SIDEBAR_COMPACT_WIDTH) in text      # 16
    assert str(DEFAULT_FILE_TREE_WIDTH) in text        # 20
    assert "candidate" in text, (
        f"the attribution must be labelled a candidate, not stated as fact: "
        f"{text!r}"
    )
    # The remedy has to be actionable, not just diagnostic.
    assert "Ctrl+B" in text
    assert "org_sidebar" in text

    assert text.isascii(), (
        f"explain_left_panels emitted non-ASCII characters "
        f"{[c for c in text if not c.isascii()]!r}. This text reaches stdout "
        f"inside the plan JSON; use '--' for dashes and '->' for arrows."
    )
    text.encode("cp932")  # raises UnicodeEncodeError if the ASCII rule slipped

    # The explanation is prose about the measurement, so an arbitrary measured
    # total must appear verbatim rather than being rounded to a known mode.
    assert "137" in explain_left_panels(137)


def test_explain_left_panels_at_zero_does_not_invent_panels() -> None:
    # Adversarial review (Minor): with [ui] org_sidebar = "off" and the file
    # tree hidden the rects start at x=0, and this text is APPENDED to the
    # pre-#158 rect escalation -- a message claude-org-ja forwards to the
    # secretary verbatim, on a path that exists today and needs none of the new
    # flags. Offering a "26 default / 16 compact plus ~20" attribution for a
    # measured total of 0, and then telling a human to reclaim those columns
    # with a toggle that is already off, sends them chasing nothing.
    text = explain_left_panels(0)
    assert "column 0" in text
    assert "candidate attribution" not in text
    assert "Reclaim them" not in text
    assert str(ORG_SIDEBAR_DEFAULT_WIDTH) not in text
    assert str(DEFAULT_FILE_TREE_WIDTH) not in text
    assert text.isascii()
    text.encode("cp932")

    # It still says something: the operator learns the panels are not the
    # problem, which is the actionable half of a zero measurement.
    assert "hidden" in text


def test_split_choice_is_comparable_by_value() -> None:
    # Guard for the fuses above: they assert SplitChoice equality, which is
    # only meaningful while SplitChoice keeps dataclass-generated __eq__. If
    # someone adds eq=False (or swaps in a plain class), fuses 1 and 3 would
    # silently degrade to identity comparisons that can never pass -- or, with
    # a hand-written __eq__ that ignores fields, can never fail.
    a = SplitChoice(target_name="d", target_id=2, direction="vertical",
                    new_w=100, new_h=50, metric=100, role="dispatcher")
    b = SplitChoice(target_name="d", target_id=2, direction="vertical",
                    new_w=100, new_h=50, metric=100, role="dispatcher")
    assert a == b and a is not b
    assert a != SplitChoice(target_name="d", target_id=2, direction="vertical",
                            new_w=87, new_h=50, metric=87, role="dispatcher")
