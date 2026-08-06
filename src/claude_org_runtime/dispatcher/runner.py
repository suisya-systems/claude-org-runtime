"""Dispatcher state-machine helper for claude-org (port of `tools/dispatcher_runner.py`).

This module is the runtime port of the in-tree
``tools/dispatcher_runner.py`` helper from claude-org-ja. It computes the
deterministic parts of the Dispatcher delegation state machine (balanced
split target selection, name/cwd validation, instruction-template
rendering, worker seed + outbox file writes) and emits a JSON action plan
that Dispatcher Claude reads and executes via MCP tool calls.

The helper does NOT call MCP tools directly. Dispatcher remains the actor
that receives Secretary's DELEGATE, invokes this helper, reads the
returned plan, and performs the ``spawn_claude_pane`` / ``send_keys`` /
``send_message`` / etc. calls.

Behaviour parity with the original ``tools/dispatcher_runner.py`` is a
hard requirement -- claude-org-ja consumers can replace their in-tree
script with ``python -m claude_org_runtime.dispatcher.runner`` without
regression. The only surface change is the new ``--template-repo`` flag,
which lets the caller point the helper at an arbitrary repo root that
hosts the ``.claude/skills/org-delegate/references/instruction-template.md``
auto-expand template (default: current working directory, which matches
how the in-tree script was invoked from the claude-org-ja repo root).

Usage::

    python -m claude_org_runtime.dispatcher.runner delegate-plan \\
        --task-json .state/dispatcher/inbox/{task_id}.json \\
        --panes-json panes.json \\
        --template-repo /path/to/claude-org-ja

The capacity model is backend-aware (runtime Issue #99). Under ``--transport
renga`` (the geometry backend) the rect-based balanced split derives both the
spawn target and the concurrent-worker ceiling. Under ``--transport broker``
(independent detached sessions, no shared tab geometry) the rect ceiling is
replaced by an explicit ``--max-concurrent-workers`` policy and the spawn
addresses a stable adapter-resolvable target. The ``split_capacity_exceeded``
status is kept for both backends; only the escalation reason differs.

renga 2.0 (runtime Issue #158) scopes ``list_panes`` to the CALLER's tab, so
the pane snapshot stopped being a census of the org. The population source is
therefore split off into an optional ``--peers-json`` (``list_peers``, which
spans every tab), while ``--panes-json`` stays the geometry source that the
balanced split ranks. Optional ``--tab`` / ``--overflow-to-new-tab`` place a
worker in another tab; both fail closed unless the caller asserts
``--server-capability spawn_tab``. Every one of those inputs is optional and
omitting them reproduces the pre-#158 numbers and plan shape exactly.

Exit codes:
  0 -- plan emitted OK (status = ``ready_to_spawn``)
  1 -- input validation failed (status = ``input_invalid``)
  2 -- capacity exhausted and escalation is required
       (status = ``split_capacity_exceeded``): renga = no rect balanced-split
       candidate; broker = max_concurrent_workers reached

These three are the only process exit codes. The renga tab error codes
(``tab_not_found`` / ``tab_ambiguous`` / ``tab_limit_reached`` /
``target_tab_mismatch``) are PLAN-level: they lead the relevant ``errors[]``
or escalation string and key ``plan.on_spawn_error``, but never become an
exit status -- consumers branch on 0/1/2 and would misread a fourth value.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence, Union

# Re-export for documentation / downstream importers (Step B + C symbols).
from claude_org_runtime import prompts as _prompts  # noqa: F401
from claude_org_runtime import schema as _schema  # noqa: F401

# Matches renga's name/role validation (see `set_pane_identity` docs).
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_ALL_DIGITS = re.compile(r"^\d+$")

# Matches a Herdr pane handle ``w<workspace>:p<pane>`` (e.g. ``"w1W:p2"``,
# ``"w_live:p2"``). The workspace segment is everything between the leading
# ``w`` and the ``:p`` separator: the Herdr adapter does not constrain it to
# alphanumerics (its ids include ``w_live`` / ``w_old``) and ``_workspace_of``
# just splits on the first colon, so match any non-colon run here rather than a
# fixed char class. The pane segment is numeric. The captured group is the
# trailing pane number, which :func:`_parse_pane_id` reduces to its int
# tie-breaker key (see terminal/herdr.py ``_workspace_of`` and ``list_panes``).
_HERDR_PANE_ID = re.compile(r"^w[^:]+:p([0-9]+)$")

# Balanced split constants -- keep in sync with
# claude-org-ja `.claude/skills/org-delegate/references/pane-layout.md`.
MIN_PANE_WIDTH = 20
MIN_PANE_HEIGHT = 5
# SECRETARY_MIN_WIDTH was 140 (claude-org-ja #310) but that made the
# secretary unsplittable on common laptop terminals: a 258w secretary
# halves to 129w < 140, so choose_split returned no candidate even though
# an operator-forced vertical split at 129w was perfectly usable in live
# operation (claude-org-runtime #35). Lowered to 120 so a vertical split
# at the operator-confirmed-usable 129w (and similar widths) is allowed.
SECRETARY_MIN_WIDTH = 120
SECRETARY_MIN_HEIGHT = 30

# Comfortable minimum width for the dispatcher's own (left) viewport.
# The dispatcher is now the *primary* balanced-split target (top
# ``_ROLE_PRIORITY``): new workers are spawned by vertically splitting the
# dispatcher so they take its right-hand zone. To keep the dispatcher's own
# (left) monitoring viewport usable it only stays the top split target while
# that left child remains >= this width; once the dispatcher has been split
# down to its comfortable width it is demoted to a strict last resort
# (``_DISPATCHER_NARROW_PRIORITY``) so further workers split each other
# instead of halving the dispatcher's viewport past usability. The
# dispatcher is a compact relay/control pane, so this floor sits below the
# secretary's content-pane floor.
DISPATCHER_MIN_WIDTH = 80

# ---------------------------------------------------------------------------
# renga layout mirrors -- REPORTING ONLY, NEVER ARITHMETIC (runtime #158)
# ---------------------------------------------------------------------------
#
# These mirror renga's own layout constants so escalation copy can *name* the
# panels that are eating the frame. They must NEVER be subtracted from a
# ``Pane`` rect, and no capacity comparison may take one as an operand.
#
# Why: renga carves the org sidebar, the file tree and a swapped preview off
# the frame BEFORE the pane layout runs -- ``layout_geometry::compute`` does
# ``pane_w = width - org_w - tree_w - preview_w`` and advances the pane area's
# origin past each panel (renga src/app/layout_geometry.rs:143-176). That
# ``layout.panes`` Rect is the only thing ever handed to
# ``LayoutNode::calculate_rects``, whose output becomes ``last_pane_rects``,
# which ``pane_infos_for_workspace`` copies verbatim onto the wire
# (app_core.rs:387-427 -> ipc_handlers.rs:193-214). So every rect this module
# receives is FRAME-ABSOLUTE and already net of the sidebar: ``x`` includes the
# offset and ``width`` is the post-carve remainder. Subtracting a sidebar width
# here would be a straight double subtraction, and worse it would DESYNC this
# module's prediction from renga's own split guard (layout_ops.rs:244-263,
# ``rect.width / 2 < min_pane_width``), which judges the very same rects --
# manufacturing split_capacity_exceeded on layouts renga would happily split.
# The measured alternative is :func:`pane_area_bbox`; see its docstring.
ORG_SIDEBAR_DEFAULT_WIDTH = 26      # renga DEFAULT_ORG_SIDEBAR_WIDTH
ORG_SIDEBAR_COMPACT_WIDTH = 16      # renga ORG_SIDEBAR_COMPACT_WIDTH
DEFAULT_FILE_TREE_WIDTH = 20        # renga AppCore::file_tree_width default
RENGA_MAX_PANES = 16                # renga layout_ops MAX_PANES (per tab)
RENGA_MAX_TABS = 16                 # renga layout_ops MAX_TABS (per window)

# renga protocol capability tokens (renga src/ipc/mod.rs:77/89/103).
#
# Deliberately three distinct tokens, and this module honours the distinction:
# a #288-era server advertises ``caller_scope`` while STILL silently dropping
# cross-tab sends, so ``caller_scope`` can never authorise cross-tab reasoning
# and ``cross_tab_peers`` can never authorise a ``tab`` spawn key.
#
# NOTE (renga src/mcp_peer/mod.rs): the MCP surface does not expose the
# server's capability list to a tool caller -- ``send_request_requiring``
# gates internally and only surfaces an ``[server_too_old] ...`` error. So
# these are an ASSERTION the caller passes in (``--server-capability``), not a
# value this module can read. That is exactly why nothing is ever inferred
# from them and why omission fails closed.
CAP_CALLER_SCOPE = "caller_scope"
CAP_CROSS_TAB_PEERS = "cross_tab_peers"
CAP_SPAWN_TAB = "spawn_tab"

# Spawn coordinates for a spawn directed into an EXISTING non-caller tab.
#
# Structurally identical to the broker pair but for a different reason, so
# they are named separately rather than aliased: peers carry no geometry at
# all (renga PeerInfo has no rect, and PeerList deliberately skips the rect
# refresh), so there is no cross-tab rect to rank and ``choose_split`` cannot
# participate. ``"focused"`` is resolved by renga INSIDE the selected tab,
# which is also what structurally prevents ``target_tab_mismatch``: this
# module never pairs a caller-tab numeric target with a foreign-tab selector.
# ``direction`` is required by renga's schema for an existing-tab selector.
_TAB_SPAWN_TARGET = "focused"
_TAB_SPAWN_DIRECTION = "vertical"

# The externally-tagged renga TabSelector keys (exactly one per selector) and
# the error codes a tab-directed spawn can come back with. ``split_refused``
# is included in the recovery table because renga returns it for "terminal too
# small to lay out a new background tab" -- deliberately distinct from
# ``tab_limit_reached`` (MAX_TABS) and from MAX_PANES-within-a-tab refusal.
#
# ``pane_not_found`` is in the table because this module CHOOSES to address
# existing tabs by their anchor pane id: ``resolve_tab_placement``
# canonicalises ``name:`` / ``index:`` down to ``{"pane_id": N}`` so the plan
# survives a tab close between emission and the spawn call. renga resolves
# that selector with ``workspace_index_of_pane(..).ok_or_else(|| CodedError::
# new(err_code::PANE_NOT_FOUND, "tab anchor pane {id} not found in any
# workspace"))`` (renga src/app/layout_ops.rs:828-835) -- so when the ANCHOR
# itself dies (a worker finishing is the common case) the code that comes back
# is ``pane_not_found``, not ``tab_not_found``. Leaving it out would leave the
# dispatcher with no recovery entry -- and no ``remove_state_writes`` -- for
# the most likely failure mode of the strategy this module picked.
TAB_SELECTOR_KEYS = ("name", "index", "pane_id", "new")
TAB_SPAWN_ERROR_CODES = (
    "tab_not_found",
    "tab_ambiguous",
    "tab_limit_reached",
    "target_tab_mismatch",
    "pane_not_found",
    "server_too_old",
    "split_refused",
)

# The subset of TAB_SPAWN_ERROR_CODES that a *pre-flight* (plan-time) check
# can raise against the caller's own inputs, before any state file is written.
# These become ``status="input_invalid"`` (exit 1) rather than an escalation:
# the operator asked for a tab that the peer census says cannot be addressed,
# which is a bad argument, not exhausted capacity. ``tab_limit_reached`` is
# deliberately NOT here -- a full tab table is capacity, so it escalates
# (exit 2) like every other capacity refusal.
_TAB_PREFLIGHT_INPUT_CODES = (
    "server_too_old",
    "tab_not_found",
    "tab_ambiguous",
    "target_tab_mismatch",
)

# Role priority for balanced-split target selection. Higher wins.
# Mirrors claude-org-ja's pane-layout.md sort regime: priority is the
# primary key (so a higher-priority pane always beats a lower-priority
# one regardless of metric), with metric desc and id asc as tie
# breakers.
#
# The dispatcher is the primary split target: workers are carved out of the
# dispatcher's pane first (while it stays wide enough -- see
# ``DISPATCHER_MIN_WIDTH`` / ``_DISPATCHER_NARROW_PRIORITY``), and the
# secretary is the last resort so its content viewport is kept intact.
# The full ordering is dispatcher > curator > worker > secretary; the
# load-bearing intent is "dispatcher first, secretary last", while the
# curator > worker middle ordering is carried over unchanged from the
# previous regime.
_ROLE_PRIORITY = {
    "dispatcher": 4,
    "curator": 3,
    "worker": 2,
    "secretary": 1,
}

# Priority assigned to a dispatcher that has already been split down to (or
# below) ``DISPATCHER_MIN_WIDTH``: strictly below every entry in
# ``_ROLE_PRIORITY`` (all >= 1) so a narrow dispatcher becomes a pure
# last-resort fallback and its monitoring viewport is protected from being
# halved again while any other pane (worker, curator, even the secretary)
# can absorb the next worker.
_DISPATCHER_NARROW_PRIORITY = 0

# Default Claude model for worker panes. The auto-mode safety classifier
# is unstable on sonnet -- opus-only per the claude-org-ja worker-model
# feedback note.
DEFAULT_WORKER_MODEL = "opus"


# ----------------------------------------------------------------------------
# Backend-aware capacity policy (runtime Issue #99)
# ----------------------------------------------------------------------------
#
# The rect-based balanced split (choose_split) derives *both* the spawn
# target/direction *and* the implicit concurrent-worker ceiling from renga's
# "one terminal tab tiled across every pane" geometry: once no child clears
# the MIN_PANE_* floors, choose_split returns no candidate and build_plan
# raises ``split_capacity_exceeded``. That geometry ceiling is a real physical
# constraint under renga but has *no* physical basis under the broker
# transport, where every pane is an independent detached session with its own
# terminal size -- a broker pane's geometry says nothing about how many more
# workers the operator can usefully run.
#
# So capacity is made backend-aware: the renga path keeps the rect ceiling
# unchanged, while the broker path replaces it with an explicit
# ``max_concurrent_workers`` policy (:class:`CapacityPolicy`). The transport is
# passed in explicitly by the caller (``build_plan(..., transport=...)`` / CLI
# ``--transport``); it is NEVER inferred from the ``list_panes`` snapshot shape,
# because the broker's logical-pane ``w=h=0`` sentinel is retained for
# duplicate detection and spawned broker panes carry real session sizes, so a
# "0 => broker, positive => renga" heuristic would break the moment a worker is
# live (design-review Blocker).

# Values accepted for the ``transport`` selector. Mirrors
# ``claude_org_runtime.transport.descriptor.TRANSPORTS`` but is duplicated here
# as a bare literal to avoid a runner -> descriptor -> broker.surface ->
# broker/__init__ -> placement -> runner import cycle (the descriptor layer
# imports the broker surface, whose package init pulls placement, which imports
# this module). The CLI resolves the env-default transport via
# ``descriptor.resolve_transport`` in a lazy import instead.
TRANSPORTS = ("renga", "broker")

# Default concurrent-worker ceiling for the broker path. Finite by default so a
# misconfigured or missing policy cannot spawn an unbounded worker fleet; a
# value tuned for the dispatcher's /loop-3m monitoring cadence and the
# secretary's serialized approval gate (design-review Major #3). ``unlimited``
# is an explicit opt-in; ``0`` disables spawning entirely.
DEFAULT_MAX_CONCURRENT_WORKERS = 8

# How long a worker seed file counts as an outstanding capacity reservation
# (runtime #158). See :func:`count_unbound_reservations` for why the ledger
# exists at all; this is only the expiry.
#
# The value is the ``after_spawn`` peer-bind wait ("retry up to ~30s") plus
# headroom, because that wait IS the window during which an overflowed worker
# is invisible to both snapshots. Too short reopens the race it closes; too
# long makes a failed spawn hold a slot longer than necessary. Reservations
# are self-expiring rather than explicitly released because nothing in the
# system reliably runs after a spawn fails -- a TTL needs no cleanup step and
# cannot leak a slot permanently.
WORKER_BIND_WINDOW_SECONDS = 45

# Broker-path spawn coordinates. Under the broker transport the adapter does
# not split a specific pane by geometry (it opens a fresh detached session), so
# choose_split() is bypassed and the spawn addresses a stable, adapter-
# resolvable target rather than a geometry-derived balanced-split target that
# would carry no meaning (design-review Major #4). ``"focused"`` is the literal
# the broker spawn surface always resolves (the dispatcher issuing the spawn);
# ``direction`` is required by the spawn schema but ignored by the broker
# adapter, so a stable ``"vertical"`` is emitted.
_BROKER_SPAWN_TARGET = "focused"
_BROKER_SPAWN_DIRECTION = "vertical"


@dataclass(frozen=True)
class CapacityPolicy:
    """Explicit concurrent-worker ceiling for the broker transport.

    ``max_concurrent_workers`` is either a non-negative int (a finite ceiling;
    ``0`` disables spawning) or ``None`` (unlimited -- explicit opt-in). The
    renga transport ignores this policy: its ceiling comes from the rect-based
    :func:`choose_split` geometry instead.
    """

    max_concurrent_workers: Optional[int]

    def __post_init__(self) -> None:
        m = self.max_concurrent_workers
        if m is None:
            return
        if isinstance(m, bool) or not isinstance(m, int) or m < 0:
            raise ValueError(
                "max_concurrent_workers must be a non-negative int or None "
                f"(unlimited); got {m!r}"
            )

    @classmethod
    def default(cls) -> "CapacityPolicy":
        """Finite default ceiling (:data:`DEFAULT_MAX_CONCURRENT_WORKERS`)."""
        return cls(max_concurrent_workers=DEFAULT_MAX_CONCURRENT_WORKERS)

    @classmethod
    def unlimited(cls) -> "CapacityPolicy":
        """Unbounded worker fleet (explicit opt-in)."""
        return cls(max_concurrent_workers=None)

    @property
    def is_unlimited(self) -> bool:
        return self.max_concurrent_workers is None


def parse_capacity_policy(raw: str) -> CapacityPolicy:
    """Parse a ``--max-concurrent-workers`` value (``N`` | ``unlimited``).

    ``"unlimited"`` (case-insensitive) yields the unbounded policy; any other
    value must be a non-negative integer. Raises :class:`ValueError` otherwise.
    """
    s = raw.strip()
    if s.lower() == "unlimited":
        return CapacityPolicy.unlimited()
    try:
        n = int(s)
    except ValueError:
        raise ValueError(
            f"--max-concurrent-workers must be a non-negative int or "
            f"'unlimited', got {raw!r}"
        ) from None
    if n < 0:
        raise ValueError(
            f"--max-concurrent-workers must be >= 0 (or 'unlimited'), got {n}"
        )
    return CapacityPolicy(max_concurrent_workers=n)


def count_active_workers(
    panes: "Sequence[Union[Pane, Peer]]",
    live_worker_names: Optional[set[str]] = None,
) -> int:
    """Count panes acting as live workers for the broker capacity check.

    By default every pane with ``role == "worker"`` counts. When
    ``live_worker_names`` is supplied, only worker panes whose ``name`` is in
    that set count -- this lets the caller reconcile the ``list_panes`` view
    against registry liveness so a stale worker pane left behind by a dead
    session does not permanently consume a capacity slot (design-review
    Minor #5). The reconciliation set is a caller input rather than a registry
    lookup inside ``build_plan`` so the planner stays a pure function of its
    arguments.

    runtime #158 widened the *annotation* only: :class:`Peer` duck-types on
    ``.role`` / ``.name``, which is everything this function reads, so a peer
    snapshot can be counted with the identical body. The parameter is
    deliberately still named ``panes`` -- renaming it to something
    population-flavoured would silently break any keyword caller
    (``count_active_workers(panes=...)``), and a keyword break is not an
    annotation-only change. This stays the legacy *scalar* entry point;
    :func:`count_worker_population` is the auditable union that #158 needs.
    """
    workers = [p for p in panes if p.role == "worker"]
    if live_worker_names is None:
        return len(workers)
    return sum(1 for p in workers if p.name in live_worker_names)

# Path of the instruction template, relative to the consumer repo root.
INSTRUCTION_TEMPLATE_PATH = (
    ".claude/skills/org-delegate/references/instruction-template.md"
)
_TEMPLATE_START_MARKER = "<!-- AUTO-EXPAND-TEMPLATE-START -->"
_TEMPLATE_END_MARKER = "<!-- AUTO-EXPAND-TEMPLATE-END -->"

# Variables understood by the auto-expand template. branch_strategy is
# required: defaulting it would silently mis-instruct Pattern B (worktree)
# workers to commit on main.
_REQUIRED_VARS = (
    "task_description", "dir_setup", "branch_strategy", "verification_depth",
)
# Optional-var keys understood by the template. The default for each key
# is supplied by :class:`LocaleConfig` so consumers can override locale-
# sensitive copy without forking the runner. ``constraints`` is the only
# entry whose default is locale-sensitive in practice; ``report_target``
# and ``claude_md_filename`` are structural identifiers shared by every
# locale, but they live on :class:`LocaleConfig` for symmetry so a
# consumer can shift them too if the convention is different.
_OPTIONAL_VAR_KEYS = ("constraints", "report_target", "claude_md_filename")
_ALLOWED_VARS = set(_REQUIRED_VARS) | set(_OPTIONAL_VAR_KEYS)
_VERIFICATION_DEPTHS = ("full", "minimal")


# ----------------------------------------------------------------------------
# Locale configuration
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class LocaleConfig:
    """Locale-sensitive copy used when rendering worker instructions.

    The runtime ships an English default (``LocaleConfig.english()``) per
    the Layer 2 extraction policy. ``claude-org-ja`` -- whose workers run
    in Japanese -- is expected to construct a Japanese
    :class:`LocaleConfig` and pass it through ``build_plan`` /
    ``write_instruction`` from its adoption layer.

    Fields
    ------
    constraints_default
        Filler for ``instruction_vars.constraints`` when the caller omits
        it. Surfaces in the rendered worker instruction.
    report_target_default
        Default value for ``instruction_vars.report_target``.
    claude_md_filename_default
        Default value for ``instruction_vars.claude_md_filename``.
    instruction_template
        Format string used by :func:`write_instruction` to compose the
        ``<task_id>-instruction.md`` file body. Receives three named
        placeholders: ``{task_id}``, ``{worker_dir}``, ``{instruction}``.
    """

    constraints_default: str = "(none)"
    report_target_default: str = "secretary"
    claude_md_filename_default: str = "CLAUDE.md"
    instruction_template: str = (
        "# Task: {task_id}\n"
        "\n"
        "Worker instruction expanded by the dispatcher runner from a "
        "secretary delegation.\n"
        "Working directory: `{worker_dir}`\n"
        "\n"
        "## Instruction\n"
        "{instruction}\n"
    )

    @classmethod
    def english(cls) -> "LocaleConfig":
        """Return the English default (matches the runtime ship config)."""
        return cls()

    def optional_var_defaults(self) -> dict[str, str]:
        """Return the ``{key: default}`` map for ``_OPTIONAL_VAR_KEYS``."""
        return {
            "constraints": self.constraints_default,
            "report_target": self.report_target_default,
            "claude_md_filename": self.claude_md_filename_default,
        }


_DEFAULT_LOCALE = LocaleConfig.english()


# ----------------------------------------------------------------------------
# Pane model
# ----------------------------------------------------------------------------


def _parse_pane_id(raw: Any) -> int:
    """Normalise a pane id to an int, accepting all three transports' formats.

    renga emits numeric pane ids (``1``, ``"2"``); the broker/tmux backend
    emits tmux ``pane_id`` strings of the form ``%N`` (``"%0"``, ``"%1"``);
    the Herdr backend emits ``w<workspace>:p<pane>`` handles (``"w1W:p2"``,
    ``"w_live:p2"``). All three are accepted and reduced to the
    integer ``N`` -- for Herdr ``N`` is the trailing pane number. The value is
    used only as the deterministic tie-breaker in :func:`choose_split` (the
    spawn itself targets by ``target_name``), so the ``%`` prefix / Herdr
    workspace segment carry no information worth preserving and a single
    integer key keeps the sort total.

    A single ``delegate-plan`` call only ever sees panes from one transport,
    so the fact that tmux ``%1``, renga ``1`` and Herdr ``w9:p1`` collapse to
    the same int is harmless -- they never coexist in one ``panes`` list. Two
    Herdr panes from different workspaces (``w1:p2`` / ``w2:p2``) can collapse
    likewise; that only affects tie-break ordering among otherwise-equal
    candidates (a stable sort keeps their input order), never which pane is
    spawned into, so it is harmless for the same reason.
    """
    if isinstance(raw, bool):
        # bool is an int subclass; reject it explicitly so ``True``/``False``
        # in the JSON don't silently become pane ids 1/0.
        raise ValueError(f"pane id must be an int or string, got bool {raw!r}")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        body = s[1:] if s.startswith("%") else s
        if body.isdigit():
            return int(body)
        herdr = _HERDR_PANE_ID.match(s)
        if herdr is not None:
            return int(herdr.group(1))
    raise ValueError(
        f"unrecognised pane id {raw!r}: expected a renga numeric id "
        f"(e.g. 1, \"2\"), a tmux pane_id (e.g. \"%0\", \"%1\") or a Herdr "
        f"handle (e.g. \"w1W:p2\")"
    )


def _pane_id_parseable(raw: Any) -> bool:
    """Return True if ``raw`` is a numeric pane id (renga ``N`` or tmux ``%N``).

    Used by :func:`_parse_panes` to tell a non-addressable logical pane (whose
    id is not a numeric pane id -- e.g. the broker's human-driven secretary
    surface ``"manual-test"``) apart from a genuinely malformed entry, so the
    former can be skipped rather than rejecting the whole snapshot.
    """
    try:
        _parse_pane_id(raw)
    except ValueError:
        return False
    return True


def _is_int_zero(v: Any) -> bool:
    """Return True iff ``v`` is the integer ``0`` (and not a bool or float).

    ``int(0.5)`` would round a malformed float geometry down to ``0``, so the
    sentinel check below must reject non-int values rather than coerce them --
    a non-int geometry is malformed input, not the broker's ``0`` sentinel.
    """
    return isinstance(v, int) and not isinstance(v, bool) and v == 0


def _has_logical_pane_geometry(d: dict[str, Any]) -> bool:
    """Return True iff ``d``'s geometry is the broker's logical-pane sentinel.

    The broker's list_panes emits integer ``w=h=0`` (both dimensions zero) for
    a non-renderable logical pane (e.g. the human-driven secretary surface).
    Used by :func:`_parse_panes` to confine the non-numeric-id skip to exactly
    that shape: a partially-degenerate pane (``w=0,h>0`` or ``w>0,h=0``), or one
    whose geometry is missing or non-int (e.g. ``0.5``), is *not* a logical pane
    and is left to be surfaced as an input error rather than silently skipped.
    Accepts the broker's ``w``/``h`` as aliases for renga's ``width``/``height``
    (see :meth:`Pane.from_dict`).
    """
    return _is_int_zero(d.get("width", d.get("w"))) and _is_int_zero(
        d.get("height", d.get("h"))
    )


@dataclass
class Pane:
    id: int
    name: Optional[str]
    role: Optional[str]
    focused: bool
    x: int
    y: int
    width: int
    height: int

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Pane":
        # Geometry field names differ by transport: renga emits
        # ``width``/``height``, the broker's list_panes emits ``w``/``h``.
        # Accept ``w``/``h`` as aliases so a broker-native snapshot is taken
        # without a hand remap (Refs suisya-systems/claude-org-ja#580). A
        # missing key under both names yields ``None`` -> ``int(None)`` raises
        # TypeError, preserving the "malformed geometry is input error" contract.
        return cls(
            id=_parse_pane_id(d["id"]),
            name=d.get("name"),
            role=d.get("role"),
            focused=bool(d.get("focused", False)),
            x=int(d["x"]),
            y=int(d["y"]),
            width=int(d.get("width", d.get("w"))),
            height=int(d.get("height", d.get("h"))),
        )


def rect_adjacent(a: Pane, b: Pane) -> bool:
    """Return True if ``a`` and ``b`` share a full edge."""
    horizontal_share = (
        a.x + a.width == b.x or b.x + b.width == a.x
    ) and (max(a.y, b.y) < min(a.y + a.height, b.y + b.height))
    vertical_share = (
        a.y + a.height == b.y or b.y + b.height == a.y
    ) and (max(a.x, b.x) < min(a.x + a.width, b.x + b.width))
    return horizontal_share or vertical_share


# ----------------------------------------------------------------------------
# Balanced-split algorithm
# ----------------------------------------------------------------------------


@dataclass
class SplitChoice:
    target_name: str
    target_id: int
    direction: str  # "vertical" | "horizontal"
    new_w: int
    new_h: int
    metric: int
    role: str = ""


def _split_options(p: Pane) -> list[tuple[str, int, int, int]]:
    """Return the size-satisfying split options for ``p`` as a list of
    ``(direction, new_w, new_h, metric)`` tuples.

    Both directions are evaluated -- ``"vertical"`` halves the width,
    ``"horizontal"`` halves the height -- and only directions whose
    resulting child clears the ``MIN_PANE_*`` floors (and the
    ``SECRETARY_MIN_*`` floors when ``p`` is the secretary) are returned.
    The pre-#35 algorithm committed to a single aspect-ratio-derived
    direction and dropped the pane entirely if that one failed the floors,
    never trying the other direction; evaluating both lets a wide-short
    pane fall back to the fitting split instead of yielding no candidate.

    ``metric`` is the resulting size along the halved dimension (``new_w``
    for vertical, ``new_h`` for horizontal), so a larger metric means a
    larger remaining child -- the key both this function's caller (to pick
    the better of the two directions) and :func:`choose_split` (to rank
    candidates) sort on.
    """
    options: list[tuple[str, int, int, int]] = []
    for direction, new_w, new_h, metric in (
        ("vertical", p.width // 2, p.height, p.width // 2),
        ("horizontal", p.width, p.height // 2, p.height // 2),
    ):
        if new_w < MIN_PANE_WIDTH or new_h < MIN_PANE_HEIGHT:
            continue
        if p.role == "secretary" and (
            new_w < SECRETARY_MIN_WIDTH or new_h < SECRETARY_MIN_HEIGHT
        ):
            continue
        options.append((direction, new_w, new_h, metric))
    return options


def choose_split(panes: list[Pane]) -> Optional[SplitChoice]:
    """Select the next balanced-split target/direction, or None if no candidate.

    Mirrors Step 3-1b of claude-org-ja's ``org-delegate`` skill.
    """
    curator = next((p for p in panes if p.role == "curator"), None)

    # Each entry pairs a SplitChoice with the role priority used to rank it.
    # The priority is normally ``_ROLE_PRIORITY[role]`` but a dispatcher that
    # has been split down to its comfortable width is demoted (see below), so
    # it is tracked separately rather than re-derived from ``choice.role`` at
    # sort time.
    candidates: list[tuple[int, SplitChoice]] = []
    for p in panes:
        if p.role not in ("secretary", "dispatcher", "worker", "curator"):
            continue

        # The dispatcher is the primary split target, but when a curator is
        # actually resident we keep the original adjacency requirement: a
        # dispatcher detached from the resident curator is an unexpected
        # layout, so skip it rather than carve workers out of it. After the
        # curator was made on-demand (claude-org-ja #503) ``curator is None``
        # is the normal steady state and this gate is a no-op.
        if p.role == "dispatcher":
            if curator is not None and not rect_adjacent(p, curator):
                continue

        if p.name is None:
            continue

        options = _split_options(p)
        if not options:
            continue

        # Both directions may satisfy the floors; pick the one with the
        # larger remaining child (metric desc). ``max`` returns the first
        # maximal element, and vertical is listed first in _split_options,
        # so ties deterministically favour the vertical split.
        direction, new_w, new_h, metric = max(options, key=lambda o: o[3])
        priority = _ROLE_PRIORITY.get(p.role or "", 0)

        # Dispatcher-first split: spawn the worker by vertically splitting the
        # dispatcher so it takes the right-hand zone, keeping the dispatcher's
        # top priority -- but only while the dispatcher's own (left) child
        # stays >= DISPATCHER_MIN_WIDTH. Once the dispatcher has been split
        # down to (or below) its comfortable width, demote it to a strict
        # last resort so further workers split each other instead of halving
        # the dispatcher's monitoring viewport past usability.
        if p.role == "dispatcher":
            vert = next((o for o in options if o[0] == "vertical"), None)
            if vert is not None and vert[1] >= DISPATCHER_MIN_WIDTH:
                direction, new_w, new_h, metric = vert
            else:
                priority = _DISPATCHER_NARROW_PRIORITY

        candidates.append((priority, SplitChoice(
            target_name=p.name,
            target_id=p.id,
            direction=direction,
            new_w=new_w,
            new_h=new_h,
            metric=metric,
            role=p.role or "",
        )))

    if not candidates:
        return None

    candidates.sort(
        key=lambda pc: (-pc[0], -pc[1].metric, pc[1].target_id)
    )
    return candidates[0][1]


# ----------------------------------------------------------------------------
# Multi-tab population / geometry / placement (runtime #158, renga 2.0)
# ----------------------------------------------------------------------------
#
# renga 2.0 (renga#287-#291) turned one window into many tabs and scoped
# ``list_panes`` to the CALLER's tab, which broke the one assumption every
# capacity number in this module rested on: "the pane snapshot is the whole
# org". A dispatcher in tab 0 counting worker panes now misses every worker in
# tabs 1..N and happily spawns past the ceiling.
#
# The fix splits the two jobs the ``panes`` argument used to do:
#
#   * ``panes`` stays the GEOMETRY source. It is the only input carrying rects,
#     it is caller-tab-scoped, and it is what ``choose_split`` ranks. Unchanged.
#   * ``peers`` becomes the POPULATION / IDENTITY source. renga's ``list_peers``
#     spans every tab on 2.0 and is already caller-tab-only on 1.4, so counting
#     worker peers yields the org-wide number on 2.0 and the caller-tab number
#     on 1.4 -- correct on both WITHOUT knowing which one is on the wire. That
#     is why the count needs no capability token (see :func:`derive_tab_awareness`
#     for the three things that DO need one).
#
# Everything below is additive. ``choose_split`` / ``_split_options`` / ``Pane``
# / ``SplitChoice`` are deliberately untouched, and the sidebar constants above
# appear in exactly one place in this section -- inside
# :func:`explain_left_panels` -- so no arithmetic can ever double-subtract them.


@dataclass
class Peer:
    """One entry of a renga ``list_peers`` (or broker ``list_peers``) snapshot.

    Deliberately NOT a :class:`Pane`: renga's ``PeerInfo`` carries no rect and
    no ``focused`` flag (renga src/ipc/mod.rs:523-560 -- ``PeerList``
    deliberately skips the rect refresh that ``List`` performs), so a peer
    forced into ``Pane`` would need fabricated zero geometry -- which collides
    with the broker's *meaningful* ``w=h=0`` logical-pane sentinel that
    :func:`_parse_panes` / :func:`_has_logical_pane_geometry` depend on.
    Keeping the types separate makes the geometry-vs-population split
    structural rather than conventional: there is no rect on a ``Peer`` to
    accidentally feed into :func:`choose_split`.
    """

    id: str
    name: Optional[str] = None
    role: Optional[str] = None
    kind: Optional[str] = None
    receive_mode: Optional[str] = None
    cwd: Optional[str] = None
    summary: Optional[str] = None
    tab: Optional[int] = None
    tab_name: Optional[str] = None
    same_tab: Optional[bool] = None
    # True iff the SOURCE DICT carried at least one of tab / tab_name /
    # same_tab. Captured before defaults are applied, because *presence* (not
    # value) is the old-vs-new server discriminator: renga 2.0 sets all three
    # unconditionally -- ``tab: Some(ws_idx), tab_name: Some(..), same_tab:
    # Some(ws_idx == caller_ws)`` (renga src/app/ipc_handlers.rs:253-255) -- so
    # even a SINGLE-TAB 2.0 server emits them, while renga 1.4 declares all
    # three ``skip_serializing_if = "Option::is_none"`` and never had the
    # fields at all. A value-based probe ("does anyone report same_tab?") is
    # ambiguous between "1.4" and "2.0 with one tab"; presence is not.
    has_tab_metadata: bool = False

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Peer":
        """Parse one peer dict, rejecting malformed tab metadata.

        ``id`` is required and kept as ``str(...)`` VERBATIM -- deliberately
        not run through :func:`_parse_pane_id`. That function justifies its id
        collisions with "a single delegate-plan call only ever sees panes from
        one transport", which does not cover ids that legitimately span tabs,
        and the broker's peer ids are agent handles (``"worker-foo"``,
        ``"manual-test"``) that are not numeric at all. The int was only ever
        a :func:`choose_split` tie-breaker, and a peer can never be a split
        candidate (it has no rect), so nothing needs the reduction.

        Absence and malformation are different: a missing ``tab`` is renga 1.4
        (fine, see ``has_tab_metadata``), but ``"tab": "2"`` / ``-1`` / ``True``
        is a broken transcription and must not be silently coerced into a tab
        index that then addresses the wrong tab.
        """
        if d["id"] is None:
            # ``str(None)`` would mint the literal handle ``"None"``: a null id
            # is not an id, and two null-id peers would collapse onto the same
            # synthetic anchor. ``_parse_peers``' contract is "every entry that
            # HAS an id parses", so this belongs with the other field errors.
            raise ValueError("peer id must not be null")
        tab = d.get("tab")
        if tab is not None and (
            isinstance(tab, bool) or not isinstance(tab, int) or tab < 0
        ):
            raise ValueError(
                f"peer tab must be a non-negative int (or absent), got {tab!r}"
            )
        same_tab = d.get("same_tab")
        if same_tab is not None and not isinstance(same_tab, bool):
            raise ValueError(
                f"peer same_tab must be a bool (or absent), got {same_tab!r}"
            )
        tab_name = d.get("tab_name")
        if tab_name is not None and not isinstance(tab_name, str):
            raise ValueError(
                f"peer tab_name must be a string (or absent), got {tab_name!r}"
            )
        return cls(
            id=str(d["id"]),
            name=d.get("name"),
            role=d.get("role"),
            kind=d.get("kind"),
            receive_mode=d.get("receive_mode"),
            cwd=d.get("cwd"),
            summary=d.get("summary"),
            tab=tab,
            tab_name=tab_name,
            same_tab=same_tab,
            has_tab_metadata=(
                "tab" in d or "tab_name" in d or "same_tab" in d
            ),
        )


def _peer_numeric_id(peer: Peer) -> Optional[int]:
    """Return ``peer.id`` as an int, or ``None`` when it is not numeric.

    renga peer ids are numeric pane ids; broker peer ids are agent handles.
    Used only to pick a tab's ``anchor_pane_id``, so a non-numeric handle is a
    normal answer ("this transport has no addressable anchor"), not an error.
    """
    try:
        return _parse_pane_id(peer.id)
    except ValueError:
        return None


@dataclass(frozen=True)
class TabCensus:
    """Peer-derived census of one observed tab.

    ``anchor_pane_id`` is the numeric id of a peer known to live in this tab.
    It is the STABLE address: renga documents the tab index as display
    metadata that shifts when tabs close and is never an address (renga
    src/mcp_peer/mod.rs:531). Canonicalising a name/index selector down to
    ``{"pane_id": anchor_pane_id}`` is what keeps an emitted plan valid across
    a tab close between plan emission and the spawn call. The smallest numeric
    peer id in the tab is used so the anchor does not depend on the order the
    caller happened to transcribe ``list_peers`` in.

    ``pane_ids`` is EVERY numeric peer id the census places in this tab, and it
    exists because ``anchor_pane_id`` must not do double duty. The anchor is an
    ordering-stability device (``min()``); membership is a different question,
    and the ``target_tab_mismatch`` guard asks the membership one -- "does the
    caller's own ``list_panes`` also claim this pane?". Keying that guard on
    the anchor would make it fire only when the operator happened to name the
    smallest id in the tab and sail past the identical contradiction on every
    other id.
    """

    index: Optional[int]
    name: Optional[str]
    peers: int
    workers: int
    is_caller: bool
    anchor_pane_id: Optional[int]
    pane_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class TabAwareness:
    """What this module knows about tabs, and on whose authority.

    ``tabs`` is a LOWER BOUND on the real tab count: a tab whose panes are all
    non-peer (a bare shell) is invisible to ``list_peers``, so this can never
    be a hard MAX_TABS gate -- renga stays authoritative and answers
    ``tab_limit_reached`` itself. The pre-flight check here only avoids
    obviously-doomed spawns; it never claims to be exhaustive.
    """

    cross_tab: bool                     # peers demonstrably span tabs
    spawn_tab: bool                     # a `tab` key may be emitted
    capabilities_known: bool            # caller asserted a token list
    caller_tab: Optional[int]
    caller_tab_name: Optional[str]
    tabs: tuple[TabCensus, ...]

    @classmethod
    def none(cls) -> "TabAwareness":
        """Nothing is known: no peers supplied and no capability asserted."""
        return cls(
            cross_tab=False,
            spawn_tab=False,
            capabilities_known=False,
            caller_tab=None,
            caller_tab_name=None,
            tabs=(),
        )


@dataclass(frozen=True)
class WorkerPopulation:
    """Auditable worker count with its provenance.

    ``total`` is the UNION of worker panes and worker peers deduped on
    ``name``. Union, not replacement, because a freshly spawned worker exists
    as a pane for the ~10-30s before its peer bind registers -- this module's
    own ``after_spawn`` step says "wait for {worker} to appear as a peer
    (retry up to ~30s)" -- so a second delegate-plan inside that window would
    undercount and over-spawn if peers simply replaced panes.

    Dedup is on ``name`` because it is the ONLY key present on both surfaces
    of every transport: renga's list_panes / list_peers agree on the numeric
    id AND the name, but the broker's list_peers emits ``id = agent_id``
    (``worker-foo``) while its list_panes emits ``id = adapter handle``
    (``%3``), so a pane-id dedup would double-count every broker worker
    (broker/surface.py:775 vs broker/server.py:508).

    ``anonymous`` workers (``role="worker"`` with no name) cannot be deduped
    at all and are simply added on top: dropping them would undercount, and
    merging them would collapse two real workers into one.
    """

    total: int
    source: str                 # "panes" | "panes+peers"
    scope: str                  # "caller_tab" | "all_tabs"
    panes_only: int
    peers_only: int
    both: int
    anonymous: int              # role=worker with no name; cannot be deduped
    names: tuple[str, ...]      # sorted; excludes anonymous
    tab_metadata: bool


@dataclass(frozen=True)
class PaneArea:
    """The caller tab's pane area, MEASURED from the snapshot.

    Exact, not predicted: ``calculate_rects`` / ``split_rect`` tile
    ``layout.panes`` with no gaps and no overlap (renga
    src/app/layout_tree.rs:222-242), so the bounding box of the pane rects IS
    the pane area renga computed after carving off every left panel.

    ``left_panels_columns`` (== ``x``) is the sidebar folded into the capacity
    model as an OBSERVED quantity. It is automatically right in every sidebar
    mode (default 26 / compact 16 / off / replace) because widening or hiding
    the sidebar simply changes the rects in the next snapshot, and it needs no
    terminal width -- which the runtime never receives. What it is NOT is
    decomposable: ``min(x) = org_w + tree_w + (preview_w if swapped)`` is one
    equation in three unknowns, so any attribution is a hypothesis and
    :func:`explain_left_panels` phrases it as one.
    """

    x: int
    y: int
    width: int
    height: int
    pane_count: int

    @property
    def left_panels_columns(self) -> int:
        """Columns consumed by renga's left panels, measured (== ``x``)."""
        return self.x

    def fits_new_pane(self) -> bool:
        """Would a *whole* pane of this size clear the MIN_PANE_* floors?

        No ``// 2`` anywhere, deliberately: a ``tab:{new}`` pane is the new
        tab's ONLY pane, not a split child (renga workspace_state.rs:128
        creates the workspace single-pane). Halving here would refuse an
        overflow that renga would have laid out fine.
        """
        return self.width >= MIN_PANE_WIDTH and self.height >= MIN_PANE_HEIGHT

    def to_dict(self) -> dict[str, int]:
        """Geometry only -- ``pane_count`` is deliberately NOT serialized.

        It is not the same number as the plan's ``panes_in_tab``, which is
        ``len(panes)``: this bbox counts only entries with positive geometry,
        so the broker's ``w=h=0`` logical-pane sentinel is in one and not the
        other. Emitting both under near-identical names would invite a
        consumer to treat them as interchangeable.
        """
        return {
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
        }


@dataclass(frozen=True)
class TabPlacement:
    """Resolved tab placement for one spawn.

    ``kind == "caller"`` means no ``tab`` key is emitted at all, which is the
    only shape that existed before #158 -- and therefore the shape every
    pre-#158 caller must keep getting.
    """

    kind: str                           # "caller" | "existing" | "new"
    # Wire TabSelector; None for "caller".
    selector: Optional[dict[str, Any]] = None
    error_code: Optional[str] = None    # one of TAB_SPAWN_ERROR_CODES
    error: Optional[str] = None         # "<code>: <human message>"
    reasons: tuple[str, ...] = ()

    @classmethod
    def caller(cls) -> "TabPlacement":
        """The pre-#158 placement: spawn into the caller's own tab."""
        return cls(kind="caller")


def _dedup_name(value: Any) -> Optional[str]:
    """Return ``value`` when it can serve as a dedup key, else ``None``.

    Neither ``Pane.from_dict`` nor :meth:`Peer.from_dict` type-checks ``name``
    -- ``d.get("name")`` takes whatever the transcription put there -- so a
    snapshot carrying ``"name": ["dispatcher"]`` reaches here with an
    UNHASHABLE name. Before #158 nothing hashed a name on the renga path
    (``len(workers)`` and ``p.name == worker_name`` both tolerate any type), so
    building a set here without this guard would turn a tolerated-if-odd input
    into a bare ``TypeError`` traceback -- exactly what ``_parse_panes``'
    comment promises not to do, and a regression for every existing caller.

    A non-string (or empty) name simply cannot be a dedup key, so it is
    reported as anonymous: that is the branch this module already documents for
    "cannot be deduped", and it errs toward over-counting, which is the
    fail-safe direction for a capacity ceiling.
    """
    return value if isinstance(value, str) and value else None


def count_worker_population(
    panes: list[Pane],
    peers: Optional[list[Peer]] = None,
    live_worker_names: Optional[set[str]] = None,
) -> WorkerPopulation:
    """Count live workers across panes AND peers, with provenance.

    ``peers is None`` (every pre-#158 caller) reproduces
    :func:`count_active_workers` exactly -- same number, no dedup, no tab
    scope -- so the fallback path is numerically identical to today.

    ``peers`` supplied unions the two snapshots on ``name`` (see
    :class:`WorkerPopulation` for why ``name`` and not the pane id). The
    result is tab-AGNOSTIC on purpose: ``same_tab`` never changes the count,
    which is what makes the same code correct against renga 1.4 (peers are
    already caller-tab-only there) and renga 2.0 (peers span tabs) without a
    capability token and without a paired consumer-side release.
    """
    pane_workers = [p for p in panes if p.role == "worker"]
    if live_worker_names is not None:
        # ``_dedup_name`` rather than ``p.name`` for the same reason the sets
        # below use it: a set lookup hashes its operand, and #158 made this
        # function run on the renga path too (build_plan calls it
        # unconditionally), where an unvalidated name never used to be hashed.
        pane_workers = [
            p for p in pane_workers if _dedup_name(p.name) in live_worker_names
        ]

    if peers is None:
        pane_names = {
            n for n in (_dedup_name(p.name) for p in pane_workers)
            if n is not None
        }
        return WorkerPopulation(
            # len(), not len(pane_names): identical to count_active_workers,
            # which has never deduped. Changing the number on the fallback
            # path would be a silent capacity change for every caller that
            # passes nothing new.
            total=len(pane_workers),
            source="panes",
            scope="caller_tab",
            panes_only=len(pane_workers),
            peers_only=0,
            both=0,
            anonymous=sum(
                1 for p in pane_workers if _dedup_name(p.name) is None
            ),
            names=tuple(sorted(pane_names)),
            tab_metadata=False,
        )

    peer_workers = [q for q in peers if q.role == "worker"]
    if live_worker_names is not None:
        peer_workers = [
            q for q in peer_workers if _dedup_name(q.name) in live_worker_names
        ]

    pane_names = {
        n for n in (_dedup_name(p.name) for p in pane_workers) if n is not None
    }
    peer_names = {
        n for n in (_dedup_name(q.name) for q in peer_workers) if n is not None
    }
    both = pane_names & peer_names
    anonymous = (
        sum(1 for p in pane_workers if _dedup_name(p.name) is None)
        + sum(1 for q in peer_workers if _dedup_name(q.name) is None)
    )
    names = pane_names | peer_names
    has_tab_metadata = any(q.has_tab_metadata for q in peers)
    return WorkerPopulation(
        total=len(names) + anonymous,
        source="panes+peers",
        scope="all_tabs" if has_tab_metadata else "caller_tab",
        panes_only=len(pane_names - peer_names),
        peers_only=len(peer_names - pane_names),
        both=len(both),
        anonymous=anonymous,
        names=tuple(sorted(names)),
        tab_metadata=has_tab_metadata,
    )


def _tab_pane_ids(group: list[Peer]) -> tuple[int, ...]:
    """Every numeric peer id in ``group``, sorted. Empty on a tabless handle."""
    return tuple(sorted(
        n for n in (_peer_numeric_id(q) for q in group) if n is not None
    ))


def _tab_anchor_pane_id(group: list[Peer]) -> Optional[int]:
    """Smallest numeric peer id in ``group``, or None on a tabless transport."""
    numeric = _tab_pane_ids(group)
    return numeric[0] if numeric else None


def count_unbound_reservations(
    state_dir: Path,
    counted_names: Iterable[str],
    now: Optional[float] = None,
) -> tuple[str, ...]:
    """Worker seeds written recently whose worker is in NEITHER snapshot.

    Closes the one hole the pane/peer union cannot close on its own, and it
    exists only because ``--overflow-to-new-tab`` opened it.

    The union is what makes a *same-tab* spawn safe across the peer-bind
    delay: the new pane shows up in the caller's ``list_panes`` immediately,
    so it is counted from the moment it exists even though its peer bind is
    still 10-30s away (this module's own ``after_spawn`` step waits up to
    ~30s for that bind). An OVERFLOW spawn has no such cover. It lands in a
    tab of its own, which renga#288 scoping keeps out of the caller's
    ``list_panes`` forever, and it is not a peer yet -- so for the length of
    that window it is invisible to both inputs, and back-to-back delegations
    each re-observe the same census and each admit another worker. Measured
    on the pre-fix build: a ceiling of 2 admitted three workers, every plan
    reporting ``free_worker_slots: 2``.

    The reservation ledger is the seed file the helper itself already writes
    on ``ready_to_spawn`` (:func:`write_worker_seed`, ``Status: planned``).
    Reading it is not a new kind of impurity: ``build_plan`` already stats
    these exact paths for the duplicate-state-file guard.

    Two rules keep the ledger honest:

    - **Excluded if already counted.** A worker that has since become a pane
      or a peer is in ``counted_names`` and must not be added twice.
    - **Expired by mtime.** Nothing ever deletes a seed file, so counting
      them unconditionally would make every worker the org has ever planned
      consume a slot forever. A reservation is only credible while the bind
      it is waiting for is still plausible, hence
      :data:`WORKER_BIND_WINDOW_SECONDS`. That also makes the ledger
      self-healing: a spawn that failed outright frees its slot once the
      window passes, with no cleanup step and no operator action.

    Returns the reserved worker names, sorted -- names rather than a count so
    the plan can show the operator WHICH workers are holding the slots.
    """
    workers_dir = state_dir / "workers"
    if not workers_dir.is_dir():
        return ()
    already = set(counted_names)
    # Injected by the caller in tests; ``None`` means "ask the clock". Kept a
    # parameter rather than a module-level seam so the planner stays a pure
    # function of its arguments for any caller that wants determinism.
    if now is None:
        now = time.time()
    reserved: list[str] = []
    for seed in workers_dir.glob("*.md"):
        name = seed.stem
        if name in already:
            continue
        try:
            age = now - seed.stat().st_mtime
        except OSError:
            # A seed that vanished mid-scan cannot be holding a slot. Skip it
            # rather than fail the plan: capacity accounting must not be the
            # thing that breaks a delegation.
            continue
        # A negative age (clock skew, or a file stamped in the future) is
        # treated as fresh. Erring toward counting the reservation is the
        # fail-safe direction: over-counting refuses a spawn, under-counting
        # exceeds the only ceiling this mode has.
        if age <= WORKER_BIND_WINDOW_SECONDS:
            reserved.append(name)
    return tuple(sorted(reserved))


def derive_tab_awareness(
    peers: Optional[list[Peer]],
    server_capabilities: Optional[frozenset[str]] = None,
) -> TabAwareness:
    """Derive what may be *claimed* about tabs, and on whose authority.

    Three independent signals, each gating exactly one thing, and nothing is
    inferred from a data shape to guess a server version:

    * ``spawn_tab`` -- an explicit assertion, NEVER inferred. It is true iff
      the caller passed a capability set containing ``spawn_tab``. Omission
      fails closed, which is why an existing caller (who passes
      ``server_capabilities=None``) can never have a ``tab`` key emitted into
      its plan and therefore can never be sent a request a renga 1.4 server
      would refuse with ``[server_too_old]``.
    * ``cross_tab`` -- the peer snapshot carries tab structure (field
      presence, see :attr:`Peer.has_tab_metadata`) AND nothing contradicts
      it. When the caller asserted a capability list, the deliberately
      distinct ``cross_tab_peers`` token must be in it: a renga#288-era
      server advertises ``caller_scope`` while STILL silently dropping
      cross-tab sends (renga src/ipc/mod.rs:77/89/103), so ``caller_scope``
      alone must never authorise cross-tab reasoning.
    * ``capabilities_known`` -- whether an assertion was made at all. Purely
      a label for the plan JSON, so a reader can tell "no tokens asserted"
      apart from "tokens asserted and this one was absent".

    Note that the worker COUNT is deliberately absent from that list: see
    :func:`count_worker_population`.
    """
    caps_known = server_capabilities is not None
    spawn_tab = caps_known and CAP_SPAWN_TAB in (server_capabilities or ())
    if peers is None:
        if not caps_known:
            return TabAwareness.none()
        # No census, but the caller did assert capabilities -- an operator can
        # legitimately drive `--tab new` / `--overflow-to-new-tab` with no
        # `--peers-json` at all, so the assertion must survive the empty
        # census (the selector then passes through unresolved, with a reason).
        return TabAwareness(
            cross_tab=False,
            spawn_tab=spawn_tab,
            capabilities_known=True,
            caller_tab=None,
            caller_tab_name=None,
            tabs=(),
        )

    has_tab_metadata = any(q.has_tab_metadata for q in peers)
    if caps_known:
        cross_tab = has_tab_metadata and CAP_CROSS_TAB_PEERS in (
            server_capabilities or ()
        )
    else:
        cross_tab = has_tab_metadata

    # The caller's own tab is read off the peer that says ``same_tab is True``
    # rather than off list order: renga sets the flag per peer, and the caller
    # is not guaranteed to be first (or present) in the transcription.
    caller = next((q for q in peers if q.same_tab is True), None)

    # Grouping key, and why it is not simply ``q.tab``:
    #
    # renga's MCP ``list_peers`` is TEXT, and the dispatcher transcribes it.
    # That renderer annotates the CALLER's own rows as a bare ``[your tab]``
    # -- no index, no label -- and only foreign rows as ``[tab N "label"]``
    # (renga src/mcp_peer/mod.rs:904-910, ``match (p.same_tab, p.tab)`` with
    # ``(Some(true), _) => " [your tab]"``). So a FAITHFUL transcription of a
    # renga 2.0 org yields caller peers carrying ``same_tab: true`` and no
    # ``tab`` key at all. Skipping those (the obvious ``if q.tab is None:
    # continue``) silently drops the caller's entire tab: ``by_tab`` would omit
    # it while ``scope`` claims "all_tabs", no census row would ever be
    # ``caller``, and ``tabs_seen`` would be off by one -- which also puts the
    # tab_limit_reached pre-flight permanently out of reach, because 15 foreign
    # tabs plus the caller's own is 16 real tabs but only 15 counted ones.
    #
    # So ``same_tab is True`` is a grouping key in its own right. When some
    # caller row DOES carry an index (a structured transcription, or a future
    # renderer), the index-less rows join that same group rather than forming a
    # second one; otherwise the caller's tab is censused with ``index=None``,
    # which is honest -- the wire genuinely did not say which index it is, and
    # ``TabCensus.index`` has always been Optional for exactly this reason.
    caller_index = next(
        (q.tab for q in peers if q.same_tab is True and q.tab is not None),
        None,
    )
    by_index: dict[Optional[int], list[Peer]] = {}
    for q in peers:
        if q.tab is not None:
            key: Optional[int] = q.tab
        elif q.same_tab is True:
            key = caller_index
        else:
            # No tab metadata at all (renga 1.4 / the tabless broker), or a
            # foreign peer whose index the transcription lost: there is nothing
            # to census it under, and inventing a tab would be a guess.
            continue
        by_index.setdefault(key, []).append(q)

    tabs = tuple(
        TabCensus(
            index=idx,
            name=next((q.tab_name for q in group if q.tab_name), None),
            peers=len(group),
            workers=sum(1 for q in group if q.role == "worker"),
            is_caller=any(q.same_tab is True for q in group),
            anchor_pane_id=_tab_anchor_pane_id(group),
            pane_ids=_tab_pane_ids(group),
        )
        # ``sorted`` cannot compare None with int, and the index-less caller
        # entry is the caller's own tab, so it deliberately leads the census.
        for idx, group in sorted(
            by_index.items(),
            key=lambda kv: (kv[0] is not None, kv[0] or 0),
        )
    )

    return TabAwareness(
        cross_tab=cross_tab,
        spawn_tab=spawn_tab,
        capabilities_known=caps_known,
        caller_tab=caller.tab if caller is not None else None,
        caller_tab_name=caller.tab_name if caller is not None else None,
        tabs=tabs,
    )


def pane_area_bbox(panes: list[Pane]) -> Optional[PaneArea]:
    """Measure the caller tab's pane area as the bounding box of its rects.

    This is the sidebar folded into the capacity model WITHOUT any
    subtraction: renga tiles ``layout.panes`` with no gaps and no overlap, so
    ``(min x, min y, max(x+w) - min x, max(y+h) - min y)`` reconstructs the
    post-carve pane area exactly. Prediction is not an option anyway -- the
    runtime never receives a frame width, and the left-panel total is one
    equation in three unknowns (see :class:`PaneArea`).

    Only panes with ``width > 0 and height > 0`` are bounded: the broker's
    ``w=h=0`` logical-pane sentinel is a real entry in ``panes`` (kept for
    duplicate-name detection) and would otherwise drag the origin to 0,0 and
    report a pane area that includes columns nothing is drawn in.
    """
    real = [p for p in panes if p.width > 0 and p.height > 0]
    if not real:
        return None
    x = min(p.x for p in real)
    y = min(p.y for p in real)
    return PaneArea(
        x=x,
        y=y,
        width=max(p.x + p.width for p in real) - x,
        height=max(p.y + p.height for p in real) - y,
        pane_count=len(real),
    )


def new_tab_pane_estimate(panes: list[Pane]) -> Optional[dict[str, Any]]:
    """Advisory estimate of the lone pane a ``tab:{new}`` would create.

    A fresh renga workspace is created single-pane with the file tree visible
    and no preview, so its pane area equals the caller tab's own measured
    bbox whenever the caller's tab is in that same default state. That is a
    hypothesis about the caller's UI state, not a fact, which is why the
    result is flagged ``advisory``.

    ``advisory`` describes the NUMBER's provenance, not the caller's freedom
    to ignore it. ``build_plan`` does gate on ``fits``, but only for the
    IMPLICIT overflow (``--overflow-to-new-tab``), never for an explicit
    ``--tab new`` -- an operator who named the tab is left to renga. That
    asymmetry is the whole reason the flag is not a hard precondition here:
    refusing an implicit fallback costs the caller the pre-#158 escalation it
    would have got anyway, while refusing an explicit request would override
    an instruction on the strength of a hypothesis. Note the estimate can err
    in BOTH directions -- renga's own background-tab refusal is
    ``terminal_too_small_for_layout()`` on the whole terminal, not a test on
    the pane area (renga src/app/app_core.rs:363, :625-628), so renga may
    well accept a spawn this estimate calls unfit; and a caller tab with a
    preview swapped in measures narrower than the fresh tab actually would
    (renga src/app/workspace_state.rs:118-130). See the gate's own comment in
    ``build_plan`` for the full reasoning.
    """
    area = pane_area_bbox(panes)
    if area is None:
        return None
    return {
        "width": area.width,
        "height": area.height,
        "fits": area.fits_new_pane(),
        "advisory": True,
    }


def explain_left_panels(columns: int) -> str:
    """Human-readable, ASCII-only account of the measured left-panel columns.

    The ONLY place the ``ORG_SIDEBAR_*`` / ``DEFAULT_FILE_TREE_WIDTH``
    constants are ever read. They are interpolated into prose here and are
    never an operand in a comparison, which is what makes a future
    double-subtraction structurally impossible rather than merely discouraged.

    The attribution is phrased as a candidate on purpose: ``columns`` is a
    measured total and ``org_w + tree_w + maybe preview_w`` is one equation in
    three unknowns, so naming a single decomposition would be a guess dressed
    as a fact at 3am.

    ``columns == 0`` gets its own sentence. This text is appended to the
    pre-#158 rect escalation, which claude-org-ja forwards to the secretary
    VERBATIM and which a pane at ``x=0`` reaches today with none of the new
    flags. Offering a "26 + 20" attribution for a measured total of zero is
    self-contradicting, and telling a human to reclaim columns that do not
    exist -- via a toggle that is already off -- sends them chasing nothing.
    """
    if columns <= 0:
        return (
            "The pane area starts at column 0, so renga's left panels are "
            "consuming nothing: the org sidebar and the file tree are already "
            "hidden. There are no columns left to reclaim here."
        )
    return (
        f"{columns} columns left of the pane area are consumed by renga's "
        "left panels; that total is not decomposable from a list_panes "
        "snapshot, but a candidate attribution is an org sidebar "
        f"({ORG_SIDEBAR_DEFAULT_WIDTH} default / "
        f"{ORG_SIDEBAR_COMPACT_WIDTH} compact) plus a file tree "
        f"(~{DEFAULT_FILE_TREE_WIDTH}). Reclaim them with Ctrl+B or "
        "[ui] org_sidebar = \"off\" and re-run."
    )


def parse_tab_selector(raw: str) -> dict[str, Any]:
    """Parse a ``--tab`` value into renga's externally-tagged TabSelector.

    Accepted forms (exactly the renga variants, no aliases)::

        pane_id:N   -> {"pane_id": N}    stable anchor, preferred
        index:N     -> {"index": N}      0-based, shifts when a tab closes
        name:LABEL  -> {"name": LABEL}    exact match, never first-match
        new         -> {"new": {}}
        new:LABEL   -> {"new": {"name": LABEL}}

    Raises :class:`ValueError` on anything else. There is no "bare N" form:
    an unprefixed integer is ambiguous between an index and a pane id, and
    guessing wrong addresses a different tab than the operator meant.
    """
    if not isinstance(raw, str):
        raise ValueError(f"--tab must be a string, got {type(raw).__name__}")
    s = raw.strip()
    if not s:
        raise ValueError(
            "--tab is empty; expected pane_id:N, index:N, name:LABEL, new, "
            "or new:LABEL"
        )
    if s == "new":
        return {"new": {}}
    key, sep, value = s.partition(":")
    if not sep:
        raise ValueError(
            f"--tab {raw!r} has no selector prefix; expected pane_id:N, "
            "index:N, name:LABEL, new, or new:LABEL"
        )
    if key == "new":
        if not value:
            raise ValueError("--tab new:LABEL requires a non-empty LABEL")
        return {"new": {"name": value}}
    if key == "name":
        if not value:
            raise ValueError("--tab name:LABEL requires a non-empty LABEL")
        return {"name": value}
    if key in ("index", "pane_id"):
        if not value.isdigit():
            # ``isdigit`` rejects "-1" and "abc" in one test. A negative index
            # is not "out of range" (which is a tab_not_found at plan time),
            # it is a malformed argument.
            raise ValueError(
                f"--tab {key}:{value!r} must be a non-negative integer"
            )
        return {key: int(value)}
    raise ValueError(
        f"--tab {raw!r} has unknown selector {key!r}; expected one of "
        f"{list(TAB_SELECTOR_KEYS)}"
    )


def validate_tab_selector(selector: Any) -> Optional[str]:
    """Return an error string for a malformed TabSelector, else ``None``.

    renga's TabSelector is externally tagged, so EXACTLY one key is legal --
    a two-key object is not "the first one wins", it is a schema violation.
    This also guards the direct-API caller who builds the dict by hand rather
    than going through :func:`parse_tab_selector` -- which claude-org-ja does,
    so "the CLI happens to strip it" is not protection.

    Emptiness is tested after ``strip()`` because renga tests it that way:
    ``Some(s) if !s.trim().is_empty()`` for ``tab.name`` and
    ``v.as_str().map(str::trim)`` for ``tab.new.name`` (renga
    src/mcp_peer/mod.rs:1173-1178, :1205-1215). A whitespace-only label would
    otherwise pass here, get a seed file and an instruction file written for
    it, and then be rejected by renga with a JSON-RPC -32602 -- the retry
    lockout ``on_spawn_error`` exists to prevent. The VALUE is not stripped,
    matching renga, which stores tab labels verbatim and matches them exactly.
    """
    if not isinstance(selector, dict):
        return (
            f"tab selector must be an object, got {type(selector).__name__}"
        )
    keys = [k for k in selector]
    if len(keys) != 1:
        return (
            f"tab selector must carry exactly one of {list(TAB_SELECTOR_KEYS)}, "
            f"got {sorted(keys)}"
        )
    key = keys[0]
    if key not in TAB_SELECTOR_KEYS:
        return (
            f"unknown tab selector key {key!r}; expected one of "
            f"{list(TAB_SELECTOR_KEYS)}"
        )
    value = selector[key]
    if key == "name":
        if not isinstance(value, str) or not value.strip():
            return f"tab selector name must be a non-empty string, got {value!r}"
        return None
    if key in ("index", "pane_id"):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            # bool is an int subclass; ``{"index": True}`` must not become
            # tab 1.
            return (
                f"tab selector {key} must be a non-negative int, got {value!r}"
            )
        return None
    # key == "new": renga's NewTab payload carries an optional display name
    # and nothing else.
    if not isinstance(value, dict):
        return f"tab selector new must be an object, got {value!r}"
    extra = sorted(set(value) - {"name"})
    if extra:
        return f"tab selector new accepts only 'name', got extra keys {extra}"
    if "name" in value and (
        not isinstance(value["name"], str) or not value["name"].strip()
    ):
        return (
            "tab selector new.name must be a non-empty string, got "
            f"{value['name']!r}"
        )
    return None


def _new_tab_placement(
    new_body: dict[str, Any],
    awareness: TabAwareness,
    worker_name: str,
    reasons: list[str],
) -> TabPlacement:
    """Build a ``tab:{new}`` placement, refusing at the observed MAX_TABS."""
    tabs_seen = len(awareness.tabs)
    if tabs_seen >= RENGA_MAX_TABS:
        return TabPlacement(
            kind="new",
            selector=None,
            error_code="tab_limit_reached",
            error=(
                f"tab_limit_reached: the peer census already sees {tabs_seen} "
                f"tabs and renga's MAX_TABS is {RENGA_MAX_TABS}, so a fresh "
                "background tab cannot be created. Close a tab or wait for a "
                "worker to finish."
            ),
            reasons=tuple(reasons),
        )
    # An operator-supplied label wins; otherwise the tab is named after the
    # worker. ``worker-{task_id}`` is unique by construction (a duplicate
    # task_id was already rejected upstream) and matches renga's
    # [A-Za-z0-9_-]+ because validate_task_id enforces it, so the operator
    # gets a greppable label instead of a bare index.
    label = new_body.get("name") or worker_name
    return TabPlacement(
        kind="new",
        selector={"new": {"name": label}},
        reasons=tuple(reasons),
    )


def resolve_tab_placement(
    selector: Optional[dict[str, Any]],
    awareness: TabAwareness,
    *,
    overflow: bool,
    worker_name: str,
    caller_pane_ids: frozenset[int] = frozenset(),
) -> TabPlacement:
    """Resolve where this worker's pane should be created.

    ``caller_pane_ids`` is the set of pane ids in the caller's own
    ``list_panes`` snapshot. It is used for one thing only: the
    ``target_tab_mismatch`` snapshot-contradiction guard below.

    Precedence, and why:

    1. Nothing requested -> ``caller``. This is the pre-#158 shape and MUST be
       what every existing caller keeps getting.
    2. Requested but ``spawn_tab`` was not asserted -> fail closed. An
       explicit ``--tab`` is a hard input error (the operator asked for
       something that cannot be emitted); ``--overflow-to-new-tab`` merely
       degrades back to the pre-#158 escalation, because it is a *fallback*
       the operator armed, not a demand.
    3. An explicit selector always beats overflow. Overflow is what happens
       when the caller tab is full, never a preference.
    """
    reasons: list[str] = []

    if selector is None and not overflow:
        return TabPlacement.caller()

    if not awareness.spawn_tab:
        if selector is not None:
            return TabPlacement(
                kind="caller",
                error_code="server_too_old",
                error=(
                    "server_too_old: --tab was requested but the caller did "
                    "not assert --server-capability spawn_tab. The renga MCP "
                    "surface does not report its capability list, so this is "
                    "an operator assertion and omission fails closed -- "
                    "emitting a tab key against a server that predates it "
                    "would be refused outright. Re-run with "
                    "--server-capability spawn_tab, or drop --tab."
                ),
                reasons=tuple(reasons),
            )
        reasons.append(
            "--overflow-to-new-tab was ignored: --server-capability spawn_tab "
            "was not asserted, so no tab key may be emitted; falling back to "
            "the pre-#158 capacity escalation"
        )
        return TabPlacement(kind="caller", reasons=tuple(reasons))

    if selector is None:
        # Overflow: armed, permitted, and only ever *used* when the caller
        # tab yields no balanced-split candidate (build_plan demotes this
        # back to ``caller`` when choose_split finds a target).
        reasons.append(
            "--overflow-to-new-tab is armed and --server-capability spawn_tab "
            "was asserted, so a saturated caller tab overflows into a fresh "
            "background tab instead of escalating"
        )
        return _new_tab_placement({}, awareness, worker_name, reasons)

    shape_err = validate_tab_selector(selector)
    if shape_err is not None:
        # Reported under tab_not_found because that is the renga code a
        # caller recovers from the same way (re-read list_peers, re-target);
        # the message leads with the concrete shape defect.
        return TabPlacement(
            kind="caller",
            error_code="tab_not_found",
            error=f"tab_not_found: malformed tab selector -- {shape_err}",
            reasons=tuple(reasons),
        )

    if overflow:
        reasons.append(
            "an explicit --tab selector was supplied, so "
            "--overflow-to-new-tab was ignored (overflow is a fallback, not a "
            "preference)"
        )

    if "new" in selector:
        return _new_tab_placement(
            selector["new"], awareness, worker_name, reasons,
        )

    census = awareness.tabs
    if not census:
        reasons.append(
            "no peer census was available (--peers-json omitted, or the "
            "server predates renga 2.0 and sends no tab metadata), so the "
            "selector is emitted unresolved; an index selector is "
            "index-shift-prone because renga renumbers tabs when one closes"
        )
        return TabPlacement(
            kind="existing", selector=dict(selector), reasons=tuple(reasons),
        )

    key = next(iter(selector))
    value = selector[key]

    if key == "name":
        matches = [t for t in census if t.name == value]
        if not matches:
            seen = sorted(t.name for t in census if t.name)
            return TabPlacement(
                kind="existing",
                error_code="tab_not_found",
                error=(
                    f"tab_not_found: no tab is named {value!r}; the peer "
                    f"census sees {seen}. Note the census only sees tabs that "
                    "contain at least one peer."
                ),
                reasons=tuple(reasons),
            )
        if len(matches) > 1:
            idxs = [t.index for t in matches]
            return TabPlacement(
                kind="existing",
                error_code="tab_ambiguous",
                error=(
                    f"tab_ambiguous: display name {value!r} matches tabs "
                    f"{idxs}; renga never first-matches. Re-target by pane_id "
                    "(the stable anchor) -- e.g. --tab pane_id:"
                    f"{matches[0].anchor_pane_id}."
                ),
                reasons=tuple(reasons),
            )
        target = matches[0]
    elif key == "index":
        matches = [t for t in census if t.index == value]
        if not matches:
            idxs = sorted(t.index for t in census if t.index is not None)
            return TabPlacement(
                kind="existing",
                error_code="tab_not_found",
                error=(
                    f"tab_not_found: tab index {value} is out of range; the "
                    f"peer census sees indices {idxs}. Indices are display "
                    "metadata and shift when a tab closes -- prefer "
                    "--tab pane_id:N."
                ),
                reasons=tuple(reasons),
            )
        target = matches[0]
    else:
        # pane_id: already the stable address renga documents, so there is
        # nothing to canonicalise -- only the contradiction guard applies.
        #
        # Membership is asked of ``pane_ids``, NOT of ``anchor_pane_id``: the
        # anchor is ``min()`` of the tab's peer ids, an ordering-stability
        # device with nothing to say about whether the two snapshots disagree.
        # Keying the guard on it would refuse ``pane_id:11`` and wave through
        # the structurally identical ``pane_id:17`` purely because 11 < 17.
        target = next(
            (t for t in census if value in t.pane_ids), None,
        )
        if target is not None and not target.is_caller and (
            value in caller_pane_ids
        ):
            return _target_tab_mismatch(value, target, reasons)
        return TabPlacement(
            kind="existing", selector=dict(selector), reasons=tuple(reasons),
        )

    anchor = target.anchor_pane_id
    if anchor is None:
        reasons.append(
            "the selected tab has no numeric peer id to canonicalise against "
            "(a tabless / handle-addressed transport), so the operator's "
            "selector is emitted unchanged"
        )
        return TabPlacement(
            kind="existing", selector=dict(selector), reasons=tuple(reasons),
        )
    # The name/index branch asks the guard about the ANCHOR rather than about
    # every id in the tab, and that asymmetry with the pane_id branch above is
    # deliberate: here the anchor is the id this plan is about to EMIT, so the
    # coherence question is whether that specific id is contradicted. There the
    # operator named an id and the question was membership.
    if not target.is_caller and anchor in caller_pane_ids:
        return _target_tab_mismatch(anchor, target, reasons)
    reasons.append(
        f"canonicalised {key}:{value!r} to the stable anchor "
        f"pane_id:{anchor}; renga documents the tab index as display metadata "
        "that shifts when tabs close, so an emitted plan must not carry one"
    )
    return TabPlacement(
        kind="existing", selector={"pane_id": anchor}, reasons=tuple(reasons),
    )


def _target_tab_mismatch(
    pane_id: int, target: TabCensus, reasons: list[str],
) -> TabPlacement:
    """Refuse a spawn whose two input snapshots disagree about a pane's tab.

    renga answers ``target_tab_mismatch`` when a request pairs an existing-tab
    selector with a numeric target owned by a *different* tab (renga
    src/mcp_peer/mod.rs:573-590). For runtime-emitted plans that is
    STRUCTURALLY prevented -- a non-caller placement always emits
    ``target="focused"``, which renga resolves inside the selected tab -- so
    the only way to reach it is a contradiction between the two snapshots the
    caller supplied: ``--panes-json`` (caller-tab-scoped after renga#288)
    lists this pane id, while ``--peers-json`` places it in a tab that is not
    the caller's. One of the two is stale. Refusing here costs a re-read;
    proceeding would address the wrong tab.
    """
    return TabPlacement(
        kind="existing",
        error_code="target_tab_mismatch",
        error=(
            f"target_tab_mismatch: pane id {pane_id} appears in the caller "
            "tab's list_panes snapshot, but the peer census places it in tab "
            f"{target.index} ({target.name!r}). The two snapshots disagree, "
            "so one of them is stale. Re-capture list_panes and list_peers "
            "together, then re-run delegate-plan."
        ),
        reasons=tuple(reasons),
    )


def tab_spawn_error_actions(
    state_writes: list[str],
) -> dict[str, dict[str, Any]]:
    """Recovery table the dispatcher consults when a tab-directed spawn fails.

    Emitted only when the plan actually carries a ``spawn["tab"]`` key, so a
    pre-#158 plan is byte-unchanged.

    ``remove_state_writes`` names a concrete lockout rather than being
    advice: ``cmd_delegate_plan`` writes the worker seed and the instruction
    file as soon as the status is ``ready_to_spawn``, and :func:`build_plan`
    hard-fails when either already exists. A tab spawn that fails after those
    writes would therefore block its own retry until someone deletes them by
    hand. The files to remove are exactly ``plan.state_writes``, which is why
    the list is passed in rather than reconstructed.

    The flag is True whenever this table is emitted at all: :func:`build_plan`
    populates ``plan.state_writes`` unconditionally and knows nothing about
    ``--dry-run`` (that flag is honoured in :func:`cmd_delegate_plan`, one
    layer up). So a dry-run plan carries a populated ``state_writes`` and a
    True flag for files that were never created -- deletion must therefore be
    best-effort on the consumer side (``missing_ok``), not an assertion that
    the paths exist. ``state_writes`` stays a parameter because it names WHICH
    paths, and because a future caller that emits the table before the writes
    are decided must not silently inherit a hard-coded True.
    """
    remove = bool(state_writes)
    return {
        "tab_not_found": {
            "meaning": (
                "the selected tab did not resolve; the tab table shifted "
                "since the snapshot"
            ),
            "action": "refresh_snapshot_and_replan",
            "remove_state_writes": remove,
            "next": (
                "re-run list_peers, then re-run delegate-plan; prefer "
                "--tab pane_id:N (ids never shift)"
            ),
        },
        "tab_ambiguous": {
            "meaning": (
                "more than one tab carries that display name; renga never "
                "first-matches"
            ),
            "action": "refresh_snapshot_and_replan",
            "remove_state_writes": remove,
            "next": "re-run with --tab pane_id:N taken from list_peers",
        },
        "tab_limit_reached": {
            "meaning": (
                f"renga MAX_TABS ({RENGA_MAX_TABS}) is exhausted; this is NOT "
                "split_refused (that is MAX_PANES within one tab)"
            ),
            "action": "escalate",
            "remove_state_writes": remove,
            "next": (
                "send_message to secretary: close a tab or wait for a worker "
                "to finish -- human judgment required"
            ),
        },
        "target_tab_mismatch": {
            "meaning": (
                "the numeric target is owned by another tab; unreachable for "
                "runtime-emitted plans, which always use target=focused"
            ),
            "action": "refresh_snapshot_and_replan",
            "remove_state_writes": remove,
            "next": (
                "re-run list_panes + list_peers and re-run delegate-plan; if "
                "it recurs, report a runtime/ja contract bug"
            ),
        },
        "pane_not_found": {
            # The most likely failure of the anchor-pane strategy this module
            # picked: name/index selectors are canonicalised to the tab's
            # smallest peer id, and that peer is often a worker that can finish
            # (and close) between plan emission and the spawn call. renga
            # answers PANE_NOT_FOUND, not TAB_NOT_FOUND, for a dead anchor
            # (renga src/app/layout_ops.rs:828-835).
            "meaning": (
                "the tab anchor pane in spawn.tab.pane_id no longer exists; "
                "the peer that anchored the selected tab closed since the "
                "snapshot. The tab itself may still be open"
            ),
            "action": "refresh_snapshot_and_replan",
            "remove_state_writes": remove,
            "next": (
                "re-run list_peers to pick a live anchor in the same tab, "
                "then re-run delegate-plan with --tab pane_id:N"
            ),
        },
        "server_too_old": {
            "meaning": (
                "this renga server does not advertise spawn_tab; the asserted "
                "capability set is stale"
            ),
            "action": "replan_without_tab",
            "remove_state_writes": remove,
            "next": (
                "re-run delegate-plan without --tab / --overflow-to-new-tab "
                "and without --server-capability spawn_tab"
            ),
        },
        "split_refused": {
            "meaning": (
                "terminal too small to lay out a new background tab; this is "
                "terminal size, not tab count"
            ),
            "action": "escalate",
            "remove_state_writes": remove,
            "next": (
                "send_message to secretary: enlarge the terminal, or reclaim "
                "columns per plan.layout.reclaim_hint"
            ),
        },
    }


# ----------------------------------------------------------------------------
# Instruction template auto-expansion
# ----------------------------------------------------------------------------


def _candidate_template_repos() -> Iterable[Path]:
    """Yield template-repo candidates in priority order.

    Priority:
    1. ``__file__``-relative ancestors (``parents[2..4]``). The original
       in-tree helper at ``<repo>/tools/dispatcher_runner.py`` anchored
       to ``__file__.parent.parent``; after the move into the runtime
       package the equivalent anchor lives a few levels up. In editable
       dev installs this resolves to the worktree root, which keeps the
       behaviour close to the in-tree script when the runtime is checked
       out next to the consumer repo for development.
    2. ``Path.cwd()`` and every ancestor. This is the production
       invocation pattern: Dispatcher runs ``python -m
       claude_org_runtime.dispatcher.runner ...`` from somewhere inside
       the consumer repo (typically the repo root), so walking up from
       CWD finds the template without requiring ``--template-repo``.
    """
    here = Path(__file__).resolve()
    for n in (2, 3, 4):
        if n < len(here.parents):
            yield here.parents[n]
    cwd = Path.cwd()
    yield cwd
    yield from cwd.parents


def _default_template_repo() -> Path:
    """Pick the first :func:`_candidate_template_repos` that has the template.

    Falls back to CWD when no candidate contains the template; callers
    of :func:`load_instruction_template` then surface a clear
    ``ValueError`` with the ``--template-repo`` hint instead of a
    cryptic ``FileNotFoundError``.
    """
    for candidate in _candidate_template_repos():
        if (candidate / INSTRUCTION_TEMPLATE_PATH).is_file():
            return candidate
    return Path.cwd()


def load_instruction_template(repo_root: Optional[Path] = None) -> str:
    """Read and extract the strict-format template body."""
    root = repo_root or _default_template_repo()
    template_path = root / INSTRUCTION_TEMPLATE_PATH
    if not template_path.is_file():
        raise ValueError(
            f"instruction template not found at {template_path}; "
            "pass --template-repo to point at the consumer repo root "
            "(the directory that contains "
            f"{INSTRUCTION_TEMPLATE_PATH})"
        )
    src = template_path.read_text(encoding="utf-8")
    start = src.find(_TEMPLATE_START_MARKER)
    end = src.find(_TEMPLATE_END_MARKER)
    if start < 0 or end < 0 or end <= start:
        raise ValueError(
            f"AUTO-EXPAND markers not found in {INSTRUCTION_TEMPLATE_PATH}"
        )
    section = src[start + len(_TEMPLATE_START_MARKER):end]
    fence_open = section.find("```")
    if fence_open < 0:
        raise ValueError("opening code fence missing in auto-expand section")
    body_start = section.find("\n", fence_open) + 1
    fence_close = section.find("```", body_start)
    if fence_close < 0:
        raise ValueError("closing code fence missing in auto-expand section")
    return section[body_start:fence_close].rstrip("\n")


def validate_instruction_vars(
    raw: Any,
    locale: Optional[LocaleConfig] = None,
) -> tuple[Optional[dict[str, str]], Optional[str]]:
    """Normalize and validate ``instruction_vars``. Returns (vars, error).

    ``locale`` overrides the optional-var defaults (notably
    ``constraints``); ``None`` keeps the runtime's English defaults.
    """
    if not isinstance(raw, dict):
        return None, "instruction_vars must be a JSON object"
    norm: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str):
            return None, f"instruction_vars key {k!r} is not a string"
        if v is None:
            return None, f"instruction_vars[{k!r}] is null"
        norm[k] = str(v)

    unknown = sorted(set(norm) - _ALLOWED_VARS)
    if unknown:
        return None, (
            f"instruction_vars contains unknown keys: {unknown}; "
            f"allowed: {sorted(_ALLOWED_VARS)}"
        )

    missing = [k for k in _REQUIRED_VARS if not norm.get(k, "").strip()]
    if missing:
        return None, f"instruction_vars missing required keys: {missing}"

    depth = norm["verification_depth"].strip()
    if depth not in _VERIFICATION_DEPTHS:
        return None, (
            f"instruction_vars.verification_depth must be one of "
            f"{list(_VERIFICATION_DEPTHS)}, got {depth!r}"
        )
    norm["verification_depth"] = depth

    locale = locale or _DEFAULT_LOCALE
    for k, default in locale.optional_var_defaults().items():
        if not norm.get(k, "").strip():
            norm[k] = default
    return norm, None


def render_instruction(
    instruction_vars: dict[str, str],
    repo_root: Optional[Path] = None,
) -> str:
    template = load_instruction_template(repo_root=repo_root)
    return template.format_map(instruction_vars)


# ----------------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------------


def validate_task_id(task_id: str) -> Optional[str]:
    if not task_id:
        return "task_id is empty"
    if not _NAME_PATTERN.match(task_id):
        return (f"task_id {task_id!r} contains disallowed chars "
                "(allowed: [A-Za-z0-9_-])")
    worker_name = f"worker-{task_id}"
    if _ALL_DIGITS.match(worker_name):
        return f"derived worker name {worker_name!r} is all-digit"
    return None


def validate_cwd(cwd_str: str) -> Optional[str]:
    if not cwd_str:
        return "cwd is empty"
    p = Path(cwd_str)
    if not p.exists():
        return f"cwd {cwd_str!r} does not exist"
    if not p.is_dir():
        return f"cwd {cwd_str!r} is not a directory"
    return None


# ----------------------------------------------------------------------------
# Action plan
# ----------------------------------------------------------------------------


@dataclass
class ActionPlan:
    status: str  # "ready_to_spawn" | "split_capacity_exceeded" | "input_invalid"
    task_id: str
    spawn: Optional[dict[str, Any]] = None
    after_spawn: list[dict[str, Any]] = field(default_factory=list)
    state_writes: list[str] = field(default_factory=list)
    escalate: Optional[dict[str, Any]] = None
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    # Backend-aware capacity report (broker transport only). ``None`` on the
    # renga path, whose ceiling is the rect geometry rather than a slot count.
    # Shape: ``{"transport", "max_concurrent_workers", "active_workers",
    # "free_worker_slots"}`` where ``max_concurrent_workers`` /
    # ``free_worker_slots`` are ``"unlimited"`` under the unlimited policy. This
    # is the runtime's first-class free-capacity report; ja's work-discovery
    # ``--free-panes`` reads it as "free worker slots" (paired ja follow-up).
    capacity: Optional[dict[str, Any]] = None
    # --- runtime #158 (renga 2.0 multi-tab), all additive -------------------
    #
    # All three are ``None`` on every path that exists today, with one
    # documented exception: on the renga ``split_capacity_exceeded`` path
    # ``layout`` becomes a diagnostics object, because that escalation is
    # exactly where a human needs to know how many columns the left panels
    # are eating. ``status``, the 0/1/2 exit codes, ``spawn``, ``after_spawn``,
    # ``state_writes`` and ``capacity`` are unchanged everywhere.
    #
    # ``population``  -- auditable worker census; set iff ``peers`` was passed.
    # ``layout``      -- renga-only measured pane area + tab diagnostics.
    # ``on_spawn_error`` -- recovery table; set iff ``spawn["tab"]`` was
    #                    emitted, so it can only appear on a #158 plan.
    population: Optional[dict[str, Any]] = None
    layout: Optional[dict[str, Any]] = None
    on_spawn_error: Optional[dict[str, dict[str, Any]]] = None


def _population_report(
    population: WorkerPopulation, awareness: TabAwareness,
) -> dict[str, Any]:
    """Render :class:`WorkerPopulation` + :class:`TabAwareness` for the plan.

    Every tuple is converted to a list: the plan is emitted through
    ``json.dumps(dataclasses.asdict(plan))`` and a tuple would round-trip as a
    list anyway, so leaking one only makes the in-process dict differ from the
    serialized one.

    ``by_tab`` counts are PEER-derived only. ``panes_only`` workers live in
    the caller's tab and are not represented there, so the per-tab worker
    counts deliberately need not sum to ``active_workers``.
    """
    by_tab = None
    if population.tab_metadata:
        by_tab = [
            {
                "tab": t.index,
                "tab_name": t.name,
                "caller": t.is_caller,
                "peers": t.peers,
                "workers": t.workers,
                "anchor_pane_id": t.anchor_pane_id,
            }
            for t in awareness.tabs
        ]
    return {
        "source": population.source,
        "scope": population.scope,
        "active_workers": population.total,
        "panes_only": population.panes_only,
        "peers_only": population.peers_only,
        "both": population.both,
        "anonymous": population.anonymous,
        "names": list(population.names),
        "tab_metadata": population.tab_metadata,
        "capabilities_known": awareness.capabilities_known,
        "cross_tab_peers": awareness.cross_tab,
        "spawn_tab": awareness.spawn_tab,
        "by_tab": by_tab,
    }


def _tab_placement_report(
    placement: TabPlacement,
) -> Optional[dict[str, Any]]:
    """Render a :class:`TabPlacement`, or ``None`` when there is nothing to say.

    A bare ``caller`` placement with no reasons is the pre-#158 shape, so it
    reports ``null`` rather than an object full of defaults. A ``caller``
    placement that DOES carry reasons is not silent: that is how a degraded
    ``--overflow-to-new-tab`` (armed, but no ``spawn_tab`` token) records why
    it fell back.
    """
    if (
        placement.kind == "caller"
        and not placement.reasons
        and placement.error_code is None
    ):
        return None
    report: dict[str, Any] = {
        "kind": placement.kind,
        "selector": (
            dict(placement.selector) if placement.selector is not None else None
        ),
        "reasons": list(placement.reasons),
    }
    if placement.error_code is not None:
        report["error_code"] = placement.error_code
        report["error"] = placement.error
    return report


def _layout_report(
    panes: list[Pane], awareness: TabAwareness, placement: TabPlacement,
) -> dict[str, Any]:
    """Measured layout diagnostics for the renga path.

    Everything here is observed or a named constant -- no rect is adjusted, no
    sidebar width is subtracted. ``tabs_seen`` is ``None`` rather than ``0``
    when there is no tab metadata, because "no census" and "one tab" are
    different facts and only the census-bearing one is a lower bound worth
    printing.
    """
    area = pane_area_bbox(panes)
    return {
        "transport": "renga",
        "pane_area": area.to_dict() if area is not None else None,
        "left_panels_columns": (
            area.left_panels_columns if area is not None else None
        ),
        "reclaim_hint": (
            explain_left_panels(area.left_panels_columns)
            if area is not None else None
        ),
        "panes_in_tab": len(panes),
        "max_panes": RENGA_MAX_PANES,
        "tabs_seen": len(awareness.tabs) or None,
        "tabs_seen_is_lower_bound": True,
        "max_tabs": RENGA_MAX_TABS,
        "new_tab_estimate": new_tab_pane_estimate(panes),
        "tab_placement": _tab_placement_report(placement),
    }


def _overflow_note(overflow_requested: bool, awareness: TabAwareness) -> str:
    """One ASCII sentence telling the operator how to reach the overflow path."""
    if overflow_requested and not awareness.spawn_tab:
        return (
            "--overflow-to-new-tab was armed but ignored: "
            "--server-capability spawn_tab was not asserted, so no tab key "
            "may be emitted. Re-run with both to place this worker in a fresh "
            "background tab instead."
        )
    return (
        "Re-run with --overflow-to-new-tab (needs --server-capability "
        "spawn_tab) to place this worker in a fresh background tab instead."
    )


def _renga_rect_escalation_message(
    task_id: str, layout: dict[str, Any], overflow_note: str,
) -> str:
    """The pre-#158 rect escalation, byte-for-byte, plus measured diagnostics.

    The first sentence is preserved EXACTLY and stays a prefix of the result:
    claude-org-ja forwards this text to the secretary verbatim, and consumers
    (plus this repo's own tests) key on ``"MIN_PANE" in message`` and on
    ``"max_concurrent_workers" not in message`` to tell the rect reason apart
    from the fleet-ceiling reason. Rewriting it would be the riskier change,
    so the diagnostics are APPENDED rather than merged in -- and the appended
    text must never mention ``max_concurrent_workers`` for the same reason.
    """
    base = (
        f"SPLIT_CAPACITY_EXCEEDED: no balanced-split target found for "
        f"task {task_id!r}. The rect-based balanced split's MIN_PANE / "
        "adjacency constraints produced 0 candidates. Likely terminal "
        "size shortage or unexpected layout -- human judgment required."
    )
    parts: list[str] = []
    area = layout.get("pane_area")
    if area is not None:
        parts.append(
            f"The pane area is {area['width']}x{area['height']} at "
            f"x={area['x']},y={area['y']} ({layout['panes_in_tab']} panes, "
            f"cap {layout['max_panes']})."
        )
    hint = layout.get("reclaim_hint")
    if hint:
        parts.append(hint)
    estimate = layout.get("new_tab_estimate")
    if estimate is not None:
        parts.append(
            f"A fresh tab would give this worker about {estimate['width']}x"
            f"{estimate['height']} (advisory)."
        )
    if layout.get("tabs_seen") is not None:
        parts.append(
            f"Tabs seen: {layout['tabs_seen']} of {layout['max_tabs']} "
            "(lower bound)."
        )
    # ``overflow_note`` is unconditional and both of its branches return a
    # sentence, so ``parts`` is never empty and the diagnostics paragraph is
    # never optional. There is deliberately no ``if not parts: return base``
    # guard here -- it would be dead code that reads as if the pre-#158 message
    # could still be emitted unchanged, which since #158 it cannot.
    parts.append(overflow_note)
    return base + " " + " ".join(parts)


def _tab_limit_escalation(
    plan: "ActionPlan",
    task_id: str,
    panes: list[Pane],
    awareness: TabAwareness,
    placement: TabPlacement,
) -> "ActionPlan":
    """Mark ``plan`` as refused because renga's tab table is full.

    Shared by the two sites that can reach it -- an explicit ``--tab new``
    (refused in the pre-flight) and an armed ``--overflow-to-new-tab`` (refused
    only after ``choose_split`` has been asked, so a usable in-tab split still
    wins). One builder so the two can never drift into two different messages
    for the same renga condition.
    """
    plan.status = "split_capacity_exceeded"
    plan.layout = _layout_report(panes, awareness, placement)
    plan.escalate = {
        "tool": "send_message",
        "to_id": "secretary",
        "message": (
            f"SPLIT_CAPACITY_EXCEEDED: {placement.error} No worker was "
            f"planned for task {task_id!r}; human judgment required."
        ),
    }
    return plan


def build_plan(
    task: dict[str, Any],
    panes: list[Pane],
    state_dir: Path,
    template_repo: Optional[Path] = None,
    locale: Optional[LocaleConfig] = None,
    *,
    transport: str = "renga",
    capacity_policy: Optional[CapacityPolicy] = None,
    live_worker_names: Optional[set[str]] = None,
    peers: Optional[list[Peer]] = None,
    server_capabilities: Optional[frozenset[str]] = None,
    tab: Optional[dict[str, Any]] = None,
    overflow_to_new_tab: bool = False,
) -> ActionPlan:
    """Compute a worker delegation action plan.

    ``transport`` selects the capacity model (design-review Blocker #1): the
    caller passes the value it already resolved from ``ORG_TRANSPORT`` / the
    transport descriptor -- it is never inferred from the ``panes`` snapshot.
    The Python default is ``"renga"`` so existing direct-API callers keep the
    rect-based behaviour unchanged; production callers (the CLI) resolve the
    env default (broker) and pass it explicitly.

    - ``transport="renga"``: unchanged rect-based :func:`choose_split`; no
      candidate -> ``split_capacity_exceeded`` with the geometry message.
    - ``transport="broker"``: :func:`choose_split` is bypassed. ``capacity_policy``
      (default :meth:`CapacityPolicy.default`, a finite ceiling of
      :data:`DEFAULT_MAX_CONCURRENT_WORKERS`) caps concurrent workers; the spawn
      addresses a stable adapter-resolvable target. ``live_worker_names``, when
      given, reconciles the active-worker count against registry liveness (see
      :func:`count_active_workers`).

    runtime #158 adds four keyword-only inputs, appended AFTER the existing
    keyword-only block so nothing shifts for a positional caller (ja's
    documented ``build_plan(task, panes, state_dir, locale=ja)`` is untouched):

    - ``peers``: a ``list_peers`` snapshot. This is the org-wide worker
      population under renga 2.0, where ``list_panes`` only sees the caller's
      tab. Omitted -> the population is derived from ``panes`` alone and every
      number is numerically identical to today.
    - ``server_capabilities``: the renga protocol tokens the CALLER asserts
      the server advertises. The MCP surface cannot be probed for them, so
      this is an assertion; omitting it makes every tab feature fail closed.
    - ``tab``: an externally-tagged renga TabSelector (see
      :func:`parse_tab_selector`). Requires ``spawn_tab``.
    - ``overflow_to_new_tab``: when the caller tab has no balanced-split
      candidate left, place the worker in a fresh background tab instead of
      escalating. Requires ``spawn_tab``. This is the ONLY mode in which the
      renga path consults ``capacity_policy``, because it is the only mode in
      which the rect ceiling no longer bounds the fleet.
    """
    task_id = task.get("task_id", "")
    plan = ActionPlan(status="ready_to_spawn", task_id=task_id)

    if transport not in TRANSPORTS:
        plan.status = "input_invalid"
        plan.errors.append(
            f"unknown transport {transport!r}; expected one of {list(TRANSPORTS)}"
        )
        return plan

    err = validate_task_id(task_id)
    if err:
        plan.status = "input_invalid"
        plan.errors.append(err)
        return plan

    has_explicit = bool(str(task.get("instruction") or "").strip())
    has_vars = "instruction_vars" in task
    if not has_explicit and has_vars:
        norm_vars, vars_err = validate_instruction_vars(
            task["instruction_vars"], locale=locale,
        )
        if vars_err:
            plan.status = "input_invalid"
            plan.errors.append(vars_err)
            return plan
        try:
            task["_rendered_instruction"] = render_instruction(
                norm_vars, repo_root=template_repo,
            )
        except (KeyError, ValueError, OSError) as exc:
            plan.status = "input_invalid"
            plan.errors.append(
                f"failed to render instruction template: {exc}"
            )
            return plan
    elif has_explicit and has_vars:
        plan.warnings.append(
            "both `instruction` and `instruction_vars` provided; "
            "explicit `instruction` wins, `instruction_vars` ignored"
        )

    cwd = task.get("worker_dir") or task.get("cwd")
    if not cwd:
        plan.status = "input_invalid"
        plan.errors.append("task.worker_dir (or .cwd) is required")
        return plan
    cwd_err = validate_cwd(cwd)
    if cwd_err:
        plan.status = "input_invalid"
        plan.errors.append(cwd_err)
        return plan

    worker_name = f"worker-{task_id}"
    # Duplicate-name guard, WIDENED by #158 -- a UNION, never a replacement.
    #
    # A pane with no peer bind must still block (a worker is a pane for the
    # ~10-30s before its peer registers), and a peer with no pane in THIS tab
    # must now block too, because under renga 2.0 the colliding worker can
    # live in a tab ``list_panes`` cannot see. The pane / same-tab branch keeps
    # today's message byte-for-byte: consumers forward it verbatim and this
    # repo pins it.
    peer_name_hits = [q for q in (peers or []) if q.name == worker_name]
    # ``same_tab is not False`` deliberately absorbs both "renga says this peer
    # is in my tab" and "this server sends no tab metadata at all" (renga 1.4 /
    # the tabless broker), because in both cases "in the tab" is the honest
    # description and the classic wording is correct.
    same_tab_peer_hit = any(q.same_tab is not False for q in peer_name_hits)
    if any(p.name == worker_name for p in panes) or same_tab_peer_hit:
        plan.status = "input_invalid"
        plan.errors.append(
            f"pane named {worker_name!r} already exists in the tab; "
            "close it first or pick a different task_id"
        )
        return plan
    if peer_name_hits:
        # Every remaining hit is a peer renga placed in another tab.
        other = peer_name_hits[0]
        plan.status = "input_invalid"
        plan.errors.append(
            f"pane named {worker_name!r} already exists in tab {other.tab} "
            f"({other.tab_name!r}); worker-<task_id> is the org-wide identity "
            "behind the seed file, the outbox file and name-addressed "
            "send_message, so close it first or pick a different task_id"
        )
        return plan

    seed_path = state_dir / "workers" / f"{worker_name}.md"
    instr_path = state_dir / "dispatcher" / "outbox" / f"{task_id}-instruction.md"
    for existing in (seed_path, instr_path):
        if existing.exists():
            plan.status = "input_invalid"
            plan.errors.append(
                f"state file {str(existing)!r} already exists for task_id "
                f"{task_id!r}; remove it or pick a different task_id"
            )
            return plan

    # --- runtime #158: population census and tab pre-flight ----------------
    #
    # Placed AFTER the identity guards (so a duplicate task_id still fails
    # with its own message) and BEFORE the transport branch (so a tab refusal
    # happens while ``state_writes`` is still empty and nothing has to be
    # rolled back).
    awareness = derive_tab_awareness(peers, server_capabilities)
    population = count_worker_population(panes, peers, live_worker_names)
    if peers is not None:
        plan.population = _population_report(population, awareness)

    if transport == "broker":
        # The broker has no tab concept at any layer, so a tab flag is inert
        # rather than wrong. Warn and continue, following the
        # --max-concurrent-workers precedent: a flag with no effect must not
        # fail the run.
        if tab is not None or overflow_to_new_tab:
            plan.warnings.append(
                "--tab / --overflow-to-new-tab are renga-only; the broker has "
                "no tab concept. Ignored."
            )
        placement = TabPlacement.caller()
    else:
        if tab is not None and overflow_to_new_tab:
            plan.warnings.append(
                "both --tab and --overflow-to-new-tab were given; the "
                "explicit --tab selector wins (overflow is a fallback, not a "
                "preference)"
            )
        placement = resolve_tab_placement(
            tab,
            awareness,
            overflow=overflow_to_new_tab,
            worker_name=worker_name,
            caller_pane_ids=frozenset(p.id for p in panes),
        )
        if placement.error_code in _TAB_PREFLIGHT_INPUT_CODES:
            # A bad argument, not exhausted capacity: exit 1, and no state
            # file has been written yet, so the operator can simply re-run.
            plan.status = "input_invalid"
            plan.errors.append(placement.error or placement.error_code)
            plan.layout = _layout_report(panes, awareness, placement)
            return plan
        if placement.error_code == "tab_limit_reached" and tab is not None:
            # A full tab table IS capacity, so it escalates (exit 2) like
            # every other capacity refusal. The message leads with renga's own
            # code token so the plan is greppable by the same string renga
            # would have returned, and deliberately shares no wording with the
            # rect reason.
            #
            # ``tab is not None`` scopes this to an EXPLICIT --tab new. For an
            # ARMED --overflow-to-new-tab the refusal is deferred past
            # choose_split, because overflow is documented (and tested) as a
            # fallback, never a preference: refusing here would let merely
            # arming the flag turn a perfectly usable in-tab split into exit 2
            # the moment the census sees MAX_TABS -- and an org that keeps the
            # flag on as a standing setting is driven toward exactly that
            # state by overflow itself. The demotion that implements "fallback,
            # not preference" lives downstream, so this must not pre-empt it.
            return _tab_limit_escalation(
                plan, task_id, panes, awareness, placement,
            )

    if transport == "broker":
        # Broker path: bypass the rect ceiling and choose_split entirely.
        # Capacity is the explicit max_concurrent_workers policy; the spawn
        # target/direction are stable adapter-resolvable constants.
        policy = capacity_policy or CapacityPolicy.default()
        # #158: the union of worker panes and worker peers. With ``peers=None``
        # this is exactly ``count_active_workers(panes, live_worker_names)``.
        active = population.total
        if policy.is_unlimited:
            free_slots: Any = "unlimited"
            max_repr: Any = "unlimited"
            exceeded = False
        else:
            max_workers = policy.max_concurrent_workers
            max_repr = max_workers
            free_slots = max(0, max_workers - active)
            exceeded = active >= max_workers

        if exceeded:
            plan.status = "split_capacity_exceeded"
            plan.capacity = {
                "transport": "broker",
                "max_concurrent_workers": max_repr,
                "active_workers": active,
                "free_worker_slots": 0,
            }
            plan.escalate = {
                "tool": "send_message",
                "to_id": "secretary",
                "message": (
                    f"SPLIT_CAPACITY_EXCEEDED: worker capacity reached for task "
                    f"{task_id!r}. transport=broker, "
                    f"max_concurrent_workers={max_repr}, active_workers={active}, "
                    "free_worker_slots=0. The broker path does not use rect "
                    "geometry; this is the explicit max_concurrent_workers "
                    "ceiling. Raise it via --max-concurrent-workers "
                    "(N|unlimited), or wait for an active worker to finish -- "
                    "human judgment required."
                ),
            }
            return plan

        target_name = _BROKER_SPAWN_TARGET
        direction = _BROKER_SPAWN_DIRECTION
        plan.capacity = {
            "transport": "broker",
            "max_concurrent_workers": max_repr,
            "active_workers": active,
            "free_worker_slots": free_slots,
        }
    else:
        choice = choose_split(panes)

        # Overflow is a FALLBACK, never a preference: if the caller's tab
        # still has a balanced-split candidate the worker goes there and no
        # tab key is emitted at all. Only an explicit --tab (which the
        # operator typed on purpose) overrides a usable in-tab split.
        if tab is None and placement.kind == "new" and choice is not None:
            placement = TabPlacement.caller()

        if placement.kind == "caller":
            if choice is None:
                plan.status = "split_capacity_exceeded"
                plan.layout = _layout_report(panes, awareness, placement)
                plan.escalate = {
                    "tool": "send_message",
                    "to_id": "secretary",
                    "message": _renga_rect_escalation_message(
                        task_id,
                        plan.layout,
                        _overflow_note(overflow_to_new_tab, awareness),
                    ),
                }
                return plan
            target_name = choice.target_name
            direction = choice.direction
        elif placement.kind == "new":
            if placement.error_code == "tab_limit_reached":
                # Deferred from the pre-flight (see the ``tab is not None``
                # guard there). We are here only because choose_split ALSO
                # found nothing, so the caller's tab genuinely cannot host the
                # worker and the full tab table is genuinely the binding
                # constraint. Now it escalates.
                return _tab_limit_escalation(
                    plan, task_id, panes, awareness, placement,
                )
            if tab is None:
                # Overflow mode -- and ONLY overflow mode -- reinstates a fleet
                # ceiling on the renga path. Under renga the only worker
                # ceiling has ever been "choose_split found nothing", and
                # overflow deletes exactly that. Worse, it does not self-limit:
                # list_panes is caller-tab-scoped after renga#288, so the next
                # delegate-plan re-observes the same saturated caller tab, gets
                # None again, and mints ANOTHER tab -- N delegations produce N
                # tabs and never reuse the one just created. capacity_policy is
                # the only bound left. Outside overflow the renga path still
                # ignores capacity_policy entirely.
                #
                # ...which is precisely why the census is REQUIRED here. Every
                # worker overflow places lives in a tab of its own, so none of
                # them is ever in the caller's ``list_panes`` again. With
                # ``peers is None`` the count falls back to the caller tab
                # alone, reads 0 forever, and the one remaining bound never
                # binds: twelve consecutive delegations each report
                # "free_worker_slots: 8" and each mint another tab. Refusing
                # costs one flag; not refusing voids the invariant that tabs
                # minted by overflow <= max_concurrent_workers, and feeds
                # ja's --free-panes consumer a plan.capacity that is wrong.
                # This is an input error (exit 1), not exhausted capacity: the
                # fix is to pass --peers-json, and nothing has been written.
                if peers is None:
                    plan.status = "input_invalid"
                    plan.errors.append(
                        "--overflow-to-new-tab requires --peers-json: overflow "
                        "removes the rect ceiling, and the fleet ceiling that "
                        "replaces it is counted from the peer census. Workers "
                        "placed by a previous overflow live in their own tabs "
                        "and never appear in --panes-json again, so without "
                        "the census the ceiling reads 0 forever and mints one "
                        "tab per delegation without bound. Re-run with "
                        "--peers-json, or drop --overflow-to-new-tab to keep "
                        "the pre-#158 rect ceiling."
                    )
                    plan.layout = _layout_report(panes, awareness, placement)
                    return plan
                # The census alone still cannot see an overflow spawn during
                # its peer-bind window -- it is in another tab (so never in
                # ``panes`` again) and not yet a peer -- so the ceiling is
                # counted against the census PLUS the outstanding reservation
                # ledger. See count_unbound_reservations for the measured
                # failure this closes.
                reserved = count_unbound_reservations(
                    state_dir, population.names,
                )
                committed = population.total + len(reserved)
                policy = capacity_policy or CapacityPolicy.default()
                if policy.is_unlimited:
                    max_repr: Any = "unlimited"
                    free_slots: Any = "unlimited"
                    exceeded = False
                else:
                    max_workers = policy.max_concurrent_workers
                    max_repr = max_workers
                    free_slots = max(0, max_workers - committed)
                    exceeded = committed >= max_workers
                plan.capacity = {
                    "transport": "renga",
                    "max_concurrent_workers": max_repr,
                    # ``active_workers`` stays the OBSERVED census so the key
                    # keeps meaning the same thing it does on the broker path.
                    # The reservations are reported beside it rather than
                    # folded into it, because a consumer that shows a human
                    # "N workers running" must not count panes that may never
                    # come up.
                    "active_workers": population.total,
                    "reserved_workers": len(reserved),
                    "reserved_worker_names": list(reserved),
                    "free_worker_slots": 0 if exceeded else free_slots,
                }
                if exceeded:
                    plan.status = "split_capacity_exceeded"
                    plan.layout = _layout_report(panes, awareness, placement)
                    plan.escalate = {
                        "tool": "send_message",
                        "to_id": "secretary",
                        "message": (
                            "SPLIT_CAPACITY_EXCEEDED: worker capacity reached "
                            f"for task {task_id!r}. transport=renga "
                            "(--overflow-to-new-tab), "
                            f"max_concurrent_workers={max_repr}, "
                            f"active_workers={population.total}, "
                            f"reserved_workers={len(reserved)}"
                            + (
                                f" ({', '.join(reserved)}), " if reserved
                                else ", "
                            )
                            + "free_worker_slots=0. Overflow removes the rect "
                            "ceiling and mints one tab per delegation, so the "
                            "explicit fleet ceiling is what bounds it. "
                            + (
                                "A reserved worker is one this helper already "
                                "planned but that has not become a pane or a "
                                "peer yet; those slots free themselves "
                                f"{WORKER_BIND_WINDOW_SECONDS}s after the "
                                "seed was written if the spawn never came up. "
                                if reserved else ""
                            )
                            + "Raise the ceiling via "
                            "--max-concurrent-workers (N|unlimited), "
                            "or wait for an active worker to finish -- human "
                            "judgment required."
                        ),
                    }
                    return plan

                # The fresh tab's lone pane is estimated from the caller tab's
                # measured pane area, and the estimate refuses the IMPLICIT
                # overflow when a whole pane could not clear this module's own
                # MIN_PANE_* floors. An explicit --tab new is left to renga:
                # the operator asked for that tab by name.
                #
                # The message deliberately does NOT claim renga would refuse.
                # renga's background-tab refusal is
                # ``terminal_too_small_for_layout()`` -- ``last_term_size.cols
                # < 20 || rows < 5`` on the WHOLE terminal (renga
                # src/app/app_core.rs:363, :625-628) -- not a test on the pane
                # area, so renga might well accept this spawn. What the floors
                # say is that the pane the runtime would be planning is below
                # the size this module treats as usable. And the estimate can
                # be pessimistic in the other direction too: a fresh workspace
                # is always built with ``file_tree_visible: true`` and a blank
                # ``Preview`` (renga src/app/workspace_state.rs:118-130), so a
                # caller tab with a preview swapped in measures narrower than
                # the new tab actually would.
                estimate = new_tab_pane_estimate(panes)
                if estimate is not None and not estimate["fits"]:
                    plan.status = "split_capacity_exceeded"
                    plan.layout = _layout_report(panes, awareness, placement)
                    hint = plan.layout.get("reclaim_hint") or ""
                    plan.escalate = {
                        "tool": "send_message",
                        "to_id": "secretary",
                        "message": (
                            "SPLIT_CAPACITY_EXCEEDED: split_refused: "
                            "--overflow-to-new-tab cannot help task "
                            f"{task_id!r}. A fresh tab would give this worker "
                            f"about {estimate['width']}x{estimate['height']} "
                            "(advisory, measured from this tab), which is "
                            f"below the {MIN_PANE_WIDTH}x{MIN_PANE_HEIGHT} "
                            "floor this runtime treats as a usable pane, so "
                            "overflowing would just move the problem into a "
                            "new tab. " + hint
                        ),
                    }
                    return plan
            # A tab:{new} spawn carries neither target nor direction (see the
            # spawn assembly below); both stay unbound here on purpose.
            target_name = None
            direction = None
        else:  # placement.kind == "existing"
            plan.warnings.append(
                "tab-directed spawn: renga peers carry no geometry (PeerInfo "
                "has no rect and PeerList skips the rect refresh), so no "
                "balanced split can be ranked inside the target tab. The "
                f"spawn uses target={_TAB_SPAWN_TARGET!r} / "
                f"direction={_TAB_SPAWN_DIRECTION!r}, which renga resolves "
                "inside the selected tab."
            )
            target_name = _TAB_SPAWN_TARGET
            direction = _TAB_SPAWN_DIRECTION

    permission_mode = task.get("permission_mode", "auto")
    model = task.get("model") or DEFAULT_WORKER_MODEL
    extra_args = task.get("args") or []

    spawn: dict[str, Any] = {"tool": "spawn_claude_pane"}
    if placement.kind != "new":
        spawn["target"] = target_name
        spawn["direction"] = direction
    # else: ABSENT KEYS, not None. renga forbids target/direction at the
    # schema level for a tab:{new} selector and rejects the whole request when
    # either is present, and a JSON ``null`` is present.
    spawn["name"] = worker_name
    spawn["role"] = "worker"
    spawn["cwd"] = cwd
    spawn["permission_mode"] = permission_mode
    spawn["model"] = model
    if placement.selector is not None:
        spawn["tab"] = dict(placement.selector)
    if extra_args:
        spawn["args"] = list(extra_args)
    plan.spawn = spawn

    plan.after_spawn = [
        {
            "tool": "poll_events",
            "reason": "wait for pane_started",
            "types": ["pane_started"],
            "expect_name": worker_name,
            "deadline_ms": 3000,
        },
        {
            "tool": "send_keys",
            "target": worker_name,
            "enter": True,
            "reason": ("approve the spawn-ritual prompt (renga: 'Load "
                       "development channel?' Y/n / broker: folder-trust)"),
        },
        {
            "tool": "list_peers",
            "reason": (f"wait for {worker_name} to appear as a peer "
                       "(retry up to ~30s)"),
            "expect_peer": worker_name,
        },
        {
            "tool": "send_message",
            "to_id": worker_name,
            "message_file": str(
                state_dir / "dispatcher" / "outbox"
                / f"{task_id}-instruction.md"
            ),
            "reason": "deliver task instruction",
        },
    ]

    plan.state_writes = [
        str(state_dir / "workers" / f"{worker_name}.md"),
        str(state_dir / "dispatcher" / "outbox" / f"{task_id}-instruction.md"),
    ]

    # The recovery table is emitted only when a tab key actually went out, so
    # a plan that predates #158 in shape also predates it in size.
    if "tab" in spawn:
        plan.on_spawn_error = tab_spawn_error_actions(plan.state_writes)
    # Layout diagnostics accompany any renga plan where a tab feature was
    # requested (the escalation paths set it themselves before returning).
    if transport == "renga" and (tab is not None or overflow_to_new_tab):
        plan.layout = _layout_report(panes, awareness, placement)

    return plan


# ----------------------------------------------------------------------------
# Side-effect writers
# ----------------------------------------------------------------------------


def write_worker_seed(
    state_dir: Path, task: dict[str, Any], task_id: str,
    spawn: dict[str, Any],
) -> Path:
    target = state_dir / "workers" / f"worker-{task_id}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    body = (
        f"# Worker: worker-{task_id}\n"
        f"Task: {task_id}\n"
        f"Directory: {spawn['cwd']}\n"
        f"Pane Name: worker-{task_id}\n"
        f"Status: planned\n"
        "\n"
        "## Assignment\n"
        f"{task.get('task_description', '(no description provided)')}\n"
        "\n"
        "## Progress Log\n"
        "- [planned by dispatcher_runner] pane not yet spawned\n"
    )
    target.write_text(body, encoding="utf-8")
    return target


def write_instruction(
    state_dir: Path, task: dict[str, Any], task_id: str,
    locale: Optional[LocaleConfig] = None,
) -> Path:
    target = state_dir / "dispatcher" / "outbox" / f"{task_id}-instruction.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    explicit = str(task.get("instruction") or "")
    instruction = (
        explicit if explicit.strip() else (
            task.get("_rendered_instruction")
            or task.get("task_description")
            or ""
        )
    )
    locale = locale or _DEFAULT_LOCALE
    body = locale.instruction_template.format(
        task_id=task_id,
        worker_dir=task.get("worker_dir") or task.get("cwd") or "",
        instruction=instruction,
    )
    target.write_text(body, encoding="utf-8")
    return target


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------


def _load_json(source: Optional[str], stdin: bool) -> Any:
    if stdin:
        return json.loads(sys.stdin.read())
    if source is None:
        raise SystemExit("missing JSON source (pass a path or use stdin)")
    return json.loads(Path(source).read_text(encoding="utf-8"))


def _parse_panes(panes_data: Any) -> list[Pane]:
    if isinstance(panes_data, dict) and "panes" in panes_data:
        panes_list = panes_data["panes"]
    else:
        panes_list = panes_data
    if not isinstance(panes_list, list):
        raise SystemExit("panes JSON must be a list or {panes: [...]} object")
    panes: list[Pane] = []
    for i, d in enumerate(panes_list):
        try:
            pane = Pane.from_dict(d)
        except (ValueError, KeyError, TypeError) as exc:
            # A non-addressable logical pane is the exact shape the broker's
            # list_panes emits for human-driven surfaces: a present *string* id
            # handle that is not a numeric pane id (the secretary handle
            # ``"manual-test"``), an explicit ``kind=null``, *and* the broker's
            # integer w=h=0 sentinel geometry. It can never be a balanced-split
            # target, so skip it rather than rejecting the whole snapshot (Refs
            # suisya-systems/claude-org-ja#580). The guard matches all four
            # markers so genuinely malformed input is NOT silently dropped: a
            # missing/non-string id, a non-null ``kind`` (e.g. a real worker),
            # a positive- or partially-degenerate-geometry pane, a non-int
            # geometry, or a non-dict entry all still raise a clean SystemExit
            # (exit 1), matching the not-a-list path above instead of letting a
            # bare traceback escape -- and so still participate in downstream
            # duplicate detection.
            if (
                isinstance(d, dict)
                and isinstance(d.get("id"), str)
                and not _pane_id_parseable(d["id"])
                and "kind" in d
                and d["kind"] is None
                and _has_logical_pane_geometry(d)
            ):
                continue
            raise SystemExit(f"panes[{i}] is invalid: {exc}") from None
        # A zero-area pane that *does* carry a numeric id is kept rather than
        # dropped here: it can never host a split child, but ``choose_split``
        # already excludes it as a candidate (``_split_options`` yields nothing
        # below the MIN_PANE_* floors). Keeping it in the list preserves
        # ``build_plan``'s existing-worker-name duplicate detection, which scans
        # this same list -- dropping it would let a same-named worker slip past.
        panes.append(pane)
    return panes


def _parse_peers(peers_data: Any) -> list[Peer]:
    """Parse a ``list_peers`` snapshot, mirroring :func:`_parse_panes`.

    Accepts a bare list or ``{"peers": [...]}``, and fails the whole snapshot
    on a malformed entry with the same ``SystemExit`` (exit 1) shape.

    There is deliberately NO logical-peer skip here. ``_parse_panes`` skips a
    non-addressable logical pane because such an entry can never be a
    balanced-split target; peers are never split targets in the first place
    (they carry no geometry), and a non-numeric id is the NORMAL shape on the
    broker, whose peer ids are agent handles. So every entry that has an
    ``id`` parses, and anything that does not is a real input error.
    """
    if isinstance(peers_data, dict) and "peers" in peers_data:
        peers_list = peers_data["peers"]
    else:
        peers_list = peers_data
    if not isinstance(peers_list, list):
        raise SystemExit("peers JSON must be a list or {peers: [...]} object")
    peers: list[Peer] = []
    for i, d in enumerate(peers_list):
        try:
            peers.append(Peer.from_dict(d))
        except (ValueError, KeyError, TypeError) as exc:
            raise SystemExit(f"peers[{i}] is invalid: {exc}") from None
    return peers


def _parse_server_capabilities(
    values: Optional[list[str]],
) -> Optional[frozenset[str]]:
    """Normalise repeated ``--server-capability`` tokens into an assertion set.

    ``None`` (the flag omitted) is meaningfully different from an empty set:
    it means "the caller made no claim", which is what every pre-#158 caller
    does. Both fail closed for tab features, but only the former reports
    ``capabilities_known: false`` in the plan.

    Unknown tokens are kept rather than rejected. Nothing is ever inferred
    from a token this module does not recognise, so an unknown one is inert --
    whereas hard-failing on it would turn a future renga capability into an
    outage for a runtime that simply has not learned the name yet. A typo
    therefore fails closed, which is the documented default for every tab
    feature, and surfaces as a ``server_too_old:`` error that names the exact
    flag value to pass.
    """
    if values is None:
        return None
    tokens = [v.strip() for v in values]
    if any(not t for t in tokens):
        raise SystemExit("--server-capability token must not be empty")
    return frozenset(tokens)


def _load_locale(path: Optional[str]) -> Optional[LocaleConfig]:
    if not path:
        return None
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit(
            f"--locale-json {path!r} must be a JSON object whose keys "
            "match LocaleConfig field names"
        )
    allowed = {
        "constraints_default",
        "report_target_default",
        "claude_md_filename_default",
        "instruction_template",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise SystemExit(
            f"--locale-json contains unknown LocaleConfig fields: {unknown}; "
            f"allowed: {sorted(allowed)}"
        )
    # All LocaleConfig fields are strings. Reject non-string values up front
    # so a typo like `{"instruction_template": 123}` becomes a clean input
    # error instead of a partial-write crash later when ``str.format`` is
    # called on the bad value.
    for k, v in raw.items():
        if not isinstance(v, str):
            raise SystemExit(
                f"--locale-json field {k!r} must be a string, got "
                f"{type(v).__name__}"
            )
    if "instruction_template" in raw:
        tmpl = raw["instruction_template"]
        # Dry-run the format with sentinel values to surface any defect
        # (unbalanced braces, unknown placeholders, etc.) here, before
        # any worker-state files get written. Sentinel values let us
        # also confirm every required placeholder is referenced.
        sentinels = {
            "task_id": "\x00TID\x00",
            "worker_dir": "\x00WD\x00",
            "instruction": "\x00INS\x00",
        }
        try:
            rendered = tmpl.format(**sentinels)
        except (
            KeyError, IndexError, ValueError, AttributeError, TypeError,
        ) as exc:
            raise SystemExit(
                f"--locale-json instruction_template is invalid "
                f"(format() failed: {exc}); the template must use only "
                f"{sorted(sentinels)} placeholders and well-formed braces"
            ) from None
        missing_ph = [
            f"{{{k}}}" for k, v in sentinels.items() if v not in rendered
        ]
        if missing_ph:
            raise SystemExit(
                f"--locale-json instruction_template is missing required "
                f"placeholders: {missing_ph}; the template must reference "
                f"{[f'{{{k}}}' for k in sentinels]}"
            )
    return LocaleConfig(**raw)


def cmd_delegate_plan(args: argparse.Namespace) -> int:
    task = _load_json(args.task_json, stdin=args.task_stdin)
    if not isinstance(task, dict):
        print("task JSON must be an object", file=sys.stderr)
        return 1

    panes_raw = _load_json(args.panes_json, stdin=False)
    panes = _parse_panes(panes_raw)

    # #158: the peer snapshot is the org-wide population source. Omitted ->
    # peers stays None and every number matches the pre-#158 behaviour.
    #
    # Presence-checked, not truthiness-checked, and for the same reason --tab
    # is: a wrapper that builds the invocation as `--peers-json "$PEERS"` with
    # an unset variable yields the empty string, which is a supplied-but-broken
    # argument, not an omission. Under truthiness that silently reverts to the
    # caller-tab-only count #158 exists to fix, with `population: null` as the
    # only (easily missed) signal. An explicit refusal is the fail-closed
    # answer every other JSON input in this command already gives.
    if args.peers_json is not None and not args.peers_json.strip():
        print(
            "--peers-json was given an empty path; pass a real list_peers "
            "snapshot or omit the flag entirely",
            file=sys.stderr,
        )
        return 1
    peers = (
        _parse_peers(_load_json(args.peers_json, stdin=False))
        if args.peers_json is not None else None
    )
    server_capabilities = _parse_server_capabilities(args.server_capability)

    state_dir = Path(args.state_dir).resolve()
    template_repo = (
        Path(args.template_repo).resolve() if args.template_repo else None
    )
    locale = _load_locale(args.locale_json)

    # Resolve the transport the same way the rest of the org does: explicit
    # --transport wins, else ORG_TRANSPORT env, else the descriptor default
    # (broker). Imported lazily to avoid a module-load import cycle
    # (descriptor -> broker.surface -> broker/__init__ -> placement -> runner).
    from ..transport.descriptor import resolve_transport
    try:
        transport = resolve_transport(args.transport)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    # --tab is renga-only: build_plan warns and forces the caller placement
    # under the broker, which has no tab concept at any layer. So the selector
    # is parsed only where it can have an effect, for the same reason
    # --max-concurrent-workers is parsed only where IT can (see below) -- a
    # flag documented as "ignored under transport X" must not be able to fail
    # the run under transport X. This is also why the parse sits after the
    # transport resolution rather than with the other argument decoding above.
    try:
        tab = (
            parse_tab_selector(args.tab)
            if transport == "renga" and args.tab is not None else None
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    # --max-concurrent-workers is a broker-only ceiling; under renga the rect
    # path ignores it, so don't parse/reject it there (keeps the flag's "ignored
    # under --transport renga" contract honest -- a value that has no effect
    # must not fail the run). It is still validated on the broker path.
    #
    # #158 adds one renga case: --overflow-to-new-tab removes the rect ceiling,
    # so the fleet ceiling becomes live and the value stops being inert -- and
    # a value that DOES have an effect must be validated.
    #
    # ...but only when overflow is actually reachable. An explicit --tab beats
    # overflow (overflow is a fallback, never a preference), so with both
    # supplied the fleet ceiling is never consulted and the value is inert
    # again. Validating it there would fail a run over a number nothing reads.
    ceiling_applies = transport == "broker" or (
        transport == "renga"
        and args.overflow_to_new_tab
        and tab is None
    )
    if ceiling_applies and args.max_concurrent_workers is not None:
        try:
            capacity_policy: Optional[CapacityPolicy] = parse_capacity_policy(
                args.max_concurrent_workers
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    else:
        capacity_policy = None

    plan = build_plan(
        task, panes, state_dir,
        template_repo=template_repo, locale=locale,
        transport=transport, capacity_policy=capacity_policy,
        peers=peers, server_capabilities=server_capabilities,
        tab=tab, overflow_to_new_tab=args.overflow_to_new_tab,
    )

    # Not parsing --tab under the broker (above) means build_plan never sees it
    # and so cannot emit its own "renga-only, ignored" warning for it. Say it
    # here instead: not-failing-the-run is the contract, silently swallowing an
    # explicit operator instruction is not. Emitted whatever the selector's
    # shape, because under the broker its shape was never inspected.
    if transport == "broker" and args.tab is not None:
        plan.warnings.append(
            f"--tab {args.tab!r} was ignored: tab placement is renga-only and "
            "the broker has no tab concept at any layer (its panes are "
            "independent detached sessions). The selector was not even parsed, "
            "so a malformed one would not have been reported either."
        )

    if plan.status == "ready_to_spawn" and not args.dry_run:
        write_worker_seed(state_dir, task, plan.task_id, plan.spawn or {})
        write_instruction(state_dir, task, plan.task_id, locale=locale)

    json.dump(dataclasses.asdict(plan), sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")

    if plan.status == "input_invalid":
        return 1
    if plan.status == "split_capacity_exceeded":
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-org-runtime-dispatcher",
        description="Dispatcher state-machine helper for claude-org",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    add_subparsers(sub)
    return parser


def add_subparsers(sub: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    """Attach the dispatcher subcommands to an existing subparsers action.

    Exposed so the top-level ``claude-org-runtime`` CLI can mount the same
    subcommands without redefining them.
    """
    dp = sub.add_parser(
        "delegate-plan",
        help=("compute a worker delegation action plan from a task JSON "
              "and a list_panes snapshot"),
    )
    task_group = dp.add_mutually_exclusive_group(required=True)
    task_group.add_argument(
        "--task-json", help="path to the task JSON file",
    )
    task_group.add_argument(
        "--task-stdin", action="store_true",
        help="read task JSON from stdin",
    )
    dp.add_argument(
        "--panes-json", required=True,
        help=("path to a JSON file with renga `list_panes` output "
              "(a list of pane dicts, or {panes: [...]})"),
    )
    dp.add_argument(
        "--peers-json", default=None,
        help=("path to a JSON file with renga `list_peers` output (a list of "
              "peer dicts, or {peers: [...]}). Peers span every renga tab, so "
              "this is the org-wide worker population; --panes-json only sees "
              "the caller's tab. When omitted the population is derived from "
              "--panes-json alone, which is the renga 1.4 / broker behaviour"),
    )
    dp.add_argument(
        "--server-capability", action="append", default=None, metavar="TOKEN",
        help=("renga protocol capability the server advertises (caller_scope, "
              "cross_tab_peers, spawn_tab). Repeat once per token. Asserted by "
              "the caller, not probed. When omitted every tab feature fails "
              "closed"),
    )
    dp.add_argument(
        "--tab", default=None, metavar="SELECTOR",
        help=("place the worker in a specific renga tab: 'pane_id:N' (stable "
              "anchor, preferred), 'index:N' (0-based, shifts when tabs "
              "close), 'name:LABEL' (exact match; 0 -> tab_not_found, 2+ -> "
              "tab_ambiguous), 'new' or 'new:LABEL' (fresh background tab). "
              "Requires --server-capability spawn_tab. Ignored under "
              "--transport broker"),
    )
    dp.add_argument(
        "--overflow-to-new-tab", action="store_true",
        help=("under --transport renga, when no balanced-split candidate is "
              "left, plan a spawn into a fresh background tab instead of "
              "escalating. Requires --server-capability spawn_tab AND "
              "--peers-json. In this mode --max-concurrent-workers also gates "
              "the renga path, because the rect ceiling no longer bounds the "
              "fleet -- and that fleet ceiling is counted from the peer "
              "census, which is why --peers-json is required rather than "
              "optional here. Each overflow mints a new tab; it never reuses "
              "one. Ignored under --transport broker"),
    )
    dp.add_argument(
        "--state-dir", default=".state",
        help="state directory root (default: .state)",
    )
    dp.add_argument(
        "--template-repo", default=None,
        help=(
            "repo root that hosts "
            ".claude/skills/org-delegate/references/instruction-template.md "
            "(default: tries runtime package ancestors first, then walks "
            "up from the current working directory)"
        ),
    )
    dp.add_argument(
        "--locale-json", default=None,
        help=(
            "JSON file with LocaleConfig fields "
            "(constraints_default / report_target_default / "
            "claude_md_filename_default / instruction_template); used to "
            "override the runtime's English defaults for non-English "
            "consumers (e.g. claude-org-ja)"
        ),
    )
    dp.add_argument(
        "--transport", default=None, choices=list(TRANSPORTS),
        help=(
            "capacity backend: 'renga' uses the rect-based balanced split; "
            "'broker' uses the explicit --max-concurrent-workers ceiling. "
            "Default: resolved from ORG_TRANSPORT env, else the descriptor "
            "default (broker)"
        ),
    )
    dp.add_argument(
        "--max-concurrent-workers", default=None, metavar="N|unlimited",
        help=(
            "broker-transport worker ceiling: a non-negative int (0 disables "
            "spawning) or 'unlimited' (explicit opt-in). Ignored under "
            "--transport renga unless --overflow-to-new-tab is set (that mode "
            "removes the rect ceiling, so the fleet ceiling applies). Default "
            f"when omitted: {DEFAULT_MAX_CONCURRENT_WORKERS}"
        ),
    )
    dp.add_argument(
        "--dry-run", action="store_true",
        help="do not write worker seed / instruction files; just print the plan",
    )
    dp.set_defaults(func=cmd_delegate_plan)


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
