"""Tests for the dispatcher runner port."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_org_runtime.dispatcher import runner
from claude_org_runtime.dispatcher.runner import (
    ActionPlan,
    LocaleConfig,
    Pane,
    build_plan,
    choose_split,
    main,
    rect_adjacent,
    validate_cwd,
    validate_instruction_vars,
    validate_task_id,
    write_instruction,
)


@pytest.fixture(autouse=True)
def _isolate_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Run each test from an empty directory.

    ``runner._default_template_repo`` walks ancestors of CWD looking for
    the auto-expand template, and the worktree this suite runs from has
    ancestors that may or may not contain a real template. Pinning CWD
    to a clean ``tmp_path`` keeps the discovery deterministic.
    """
    monkeypatch.chdir(tmp_path)


# ---------------------------------------------------------------------------
# Pane geometry helpers
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


def test_rect_adjacent_left_right() -> None:
    a = _pane(1, x=0, y=0, w=100, h=50)
    b = _pane(2, x=100, y=0, w=100, h=50)
    assert rect_adjacent(a, b)


def test_rect_adjacent_no_overlap() -> None:
    a = _pane(1, x=0, y=0, w=100, h=50)
    b = _pane(2, x=200, y=0, w=100, h=50)
    assert not rect_adjacent(a, b)


# ---------------------------------------------------------------------------
# choose_split
# ---------------------------------------------------------------------------


def test_choose_split_picks_dispatcher_when_curator_unsplittable() -> None:
    # Curator too small to split (would fall under MIN_PANE_HEIGHT after
    # halving). The dispatcher is the primary split target (top priority) and
    # is adjacent to the curator with a vertical child (100) >=
    # DISPATCHER_MIN_WIDTH, so it is chosen with a vertical split.
    panes = [
        _pane(1, name="curator", role="curator", x=0, y=0, w=18, h=8),
        _pane(2, name="dispatcher", role="dispatcher", x=18, y=0, w=200, h=50),
    ]
    choice = choose_split(panes)
    assert choice is not None
    assert choice.target_name == "dispatcher"
    assert choice.direction == "vertical"


def test_choose_split_dispatcher_first_outranks_larger_pane() -> None:
    # Dispatcher-first regime: the dispatcher is the primary split target
    # (priority 4), so it is chosen even when another pane (the secretary,
    # priority 1) offers a larger split metric. The dispatcher is adjacent to
    # the resident curator (gate satisfied) and its vertical child (100)
    # clears DISPATCHER_MIN_WIDTH=80, so it keeps top priority.
    panes = [
        _pane(1, name="curator", role="curator", x=0, y=0, w=100, h=50),
        _pane(2, name="dispatcher", role="dispatcher", x=100, y=0, w=200, h=50),
        _pane(3, name="secretary", role="secretary", x=0, y=50, w=300, h=100),
    ]
    choice = choose_split(panes)
    assert choice is not None
    assert choice.target_name == "dispatcher"
    assert choice.role == "dispatcher"
    assert choice.direction == "vertical"
    assert choice.new_w == 100


def test_choose_split_returns_none_when_no_candidate() -> None:
    panes = [_pane(1, name="dispatcher", role="dispatcher", w=10, h=2)]
    assert choose_split(panes) is None


def test_choose_split_dispatcher_requires_curator_adjacency() -> None:
    # Curator deliberately too small to split (so it doesn't itself
    # become the chosen candidate) -- the assertion is specifically
    # that the non-adjacent dispatcher is rejected.
    panes = [
        _pane(1, name="curator", role="curator", x=0, y=0, w=18, h=8),
        # dispatcher not adjacent to curator (gap)
        _pane(2, name="dispatcher", role="dispatcher", x=200, y=0, w=200, h=50),
    ]
    assert choose_split(panes) is None


def test_choose_split_role_priority_outranks_metric() -> None:
    # Role priority is the primary sort key: the curator (priority 3) is
    # chosen over a worker (priority 2) even though the worker's split metric
    # (vertical -> 200x60, metric=200) is far larger than the curator's
    # (vertical -> 50x60, metric=50). No dispatcher is present, so the
    # dispatcher-first rule does not apply here.
    panes = [
        _pane(1, name="curator", role="curator", x=0, y=0, w=100, h=60),
        _pane(4, name="worker-a", role="worker",
              x=0, y=60, w=400, h=60),
    ]
    choice = choose_split(panes)
    assert choice is not None
    assert choice.target_name == "curator"
    assert choice.role == "curator"


def test_choose_split_includes_curator_as_candidate() -> None:
    # The curator is a candidate at priority 3 (above worker=2). Here the
    # dispatcher is already at its comfortable width (vertical child 50 <
    # DISPATCHER_MIN_WIDTH=80, so demoted to last resort), so the curator
    # outranks the worker and is selected.
    panes = [
        _pane(1, name="curator", role="curator", x=0, y=0, w=200, h=60),
        _pane(2, name="dispatcher", role="dispatcher",
              x=200, y=0, w=100, h=60),
        _pane(3, name="worker-a", role="worker",
              x=0, y=60, w=200, h=60),
    ]
    choice = choose_split(panes)
    assert choice is not None
    assert choice.target_name == "curator"
    assert choice.role == "curator"


def test_choose_split_secretary_280x43_picks_secretary() -> None:
    # Regression for the secretary split-floor tweak (#310 / #35). The
    # secretary is now the lowest-priority split target, so to exercise its
    # floor in isolation it is the only pane present (with a dispatcher in the
    # layout the dispatcher would win). Under the current thresholds
    # (SECRETARY_MIN_WIDTH=120, SECRETARY_MIN_HEIGHT=30) the vertical split
    # (140x43) clears both floors while the horizontal split (280x21) fails
    # the height floor, so the vertical split is chosen.
    panes = [
        _pane(3, name="secretary", role="secretary",
              x=0, y=0, w=280, h=43),
    ]
    choice = choose_split(panes)
    assert choice is not None
    assert choice.target_name == "secretary"
    assert choice.direction == "vertical"
    assert choice.new_w == 140
    assert choice.new_h == 43


def test_choose_split_tie_break_by_id_within_same_role() -> None:
    # Two workers with identical metrics -> id asc breaks the tie.
    # Curator deliberately tiny so it doesn't outrank the workers.
    panes = [
        _pane(1, name="curator", role="curator", x=0, y=0, w=18, h=8),
        _pane(2, name="dispatcher", role="dispatcher",
              x=18, y=0, w=100, h=60),
        _pane(7, name="worker-b", role="worker",
              x=0, y=60, w=200, h=60),
        _pane(5, name="worker-a", role="worker",
              x=0, y=120, w=200, h=60),
    ]
    choice = choose_split(panes)
    assert choice is not None
    assert choice.target_name == "worker-a"
    assert choice.target_id == 5


# --- claude-org-runtime #35 regression coverage ---------------------------


def test_choose_split_dispatcher_candidate_when_no_curator() -> None:
    # Acceptance (a): after the curator was made on-demand
    # (claude-org-ja #503), ``curator is None`` is the steady state. The
    # pre-#35 gate dropped the dispatcher unconditionally in that case,
    # leaving zero candidates. With no curator present the dispatcher must
    # be a valid candidate -- and under the dispatcher-first regime it is the
    # primary one, picked with a vertical split.
    panes = [
        _pane(2, name="dispatcher", role="dispatcher", x=0, y=0, w=200, h=50),
    ]
    choice = choose_split(panes)
    assert choice is not None
    assert choice.target_name == "dispatcher"
    assert choice.role == "dispatcher"
    assert choice.direction == "vertical"


def test_choose_split_wide_short_secretary_picks_fitting_direction() -> None:
    # Acceptance (b): a wide-short secretary whose aspect-derived direction
    # (vertical, since 200 > 2*60) fails the secretary width floor
    # (200//2 = 100 < SECRETARY_MIN_WIDTH=120). The pre-#35 algorithm
    # committed to that single direction and returned None. Evaluating both
    # directions lets the horizontal split (200x30, clearing 120/30) win
    # instead of yielding no candidate.
    panes = [
        _pane(3, name="secretary", role="secretary", x=0, y=0, w=200, h=60),
    ]
    choice = choose_split(panes)
    assert choice is not None
    assert choice.target_name == "secretary"
    assert choice.direction == "horizontal"
    assert choice.new_w == 200
    assert choice.new_h == 30


def test_choose_split_both_directions_valid_prefers_larger_child() -> None:
    # #35 factor 2: when BOTH directions clear the floors, pick the one with
    # the larger remaining child (metric desc). Worker 150x100 is only
    # mildly wide (150 <= 2*100), so the pre-#35 aspect heuristic would have
    # chosen horizontal (150x50, metric=50); max-metric instead picks
    # vertical (75x100, metric=75). Pins the new tie-break direction.
    panes = [
        _pane(4, name="worker-a", role="worker", x=0, y=0, w=150, h=100),
    ]
    choice = choose_split(panes)
    assert choice is not None
    assert choice.target_name == "worker-a"
    assert choice.direction == "vertical"
    assert choice.new_w == 75
    assert choice.new_h == 100
    assert choice.metric == 75


def test_choose_split_equal_metric_directions_prefer_vertical() -> None:
    # #35 factor 2 tie-break: a square pane (100x100) yields equal metrics
    # for both directions (vertical 50x100 metric=50, horizontal 100x50
    # metric=50). The documented tie-break favours vertical; pin it so a
    # future reordering of _split_options can't silently flip the choice.
    panes = [
        _pane(4, name="worker-a", role="worker", x=0, y=0, w=100, h=100),
    ]
    choice = choose_split(panes)
    assert choice is not None
    assert choice.direction == "vertical"
    assert choice.new_w == 50
    assert choice.new_h == 100


def test_choose_split_live_failure_258x42_yields_valid_choice() -> None:
    # Acceptance: the live failure layout (claude-org-runtime #35) --
    # secretary 258x42, dispatcher present, no curator -- must yield a valid
    # SplitChoice instead of None. Under the dispatcher-first regime the
    # dispatcher (priority 4) is the target; its vertical split at 129x42
    # clears DISPATCHER_MIN_WIDTH=80, so the new worker is carved out of the
    # dispatcher rather than the secretary.
    panes = [
        _pane(3, name="secretary", role="secretary", x=0, y=0, w=258, h=42),
        _pane(2, name="dispatcher", role="dispatcher",
              x=0, y=42, w=258, h=42),
    ]
    choice = choose_split(panes)
    assert choice is not None
    assert choice.target_name == "dispatcher"
    assert choice.role == "dispatcher"
    assert choice.direction == "vertical"
    assert choice.new_w == 129
    assert choice.new_h == 42


def test_choose_split_resident_curator_wide_dispatcher_wins() -> None:
    # Dispatcher-first even with a resident curator: a wide dispatcher that is
    # adjacent to the curator (vertical child 100 >= DISPATCHER_MIN_WIDTH=80)
    # keeps its top priority (4) and is chosen over both the resident curator
    # (priority 3) and the secretary (priority 1).
    panes = [
        _pane(1, name="curator", role="curator", x=0, y=0, w=150, h=100),
        _pane(2, name="dispatcher", role="dispatcher",
              x=150, y=0, w=200, h=100),
        _pane(3, name="secretary", role="secretary",
              x=0, y=100, w=350, h=100),
    ]
    choice = choose_split(panes)
    assert choice is not None
    assert choice.target_name == "dispatcher"
    assert choice.role == "dispatcher"
    assert choice.direction == "vertical"
    assert choice.new_w == 100
    assert choice.new_h == 100


# --- dispatcher-first split target (viewport self-limit) -------------------


def test_choose_split_wide_dispatcher_outranks_worker() -> None:
    # Dispatcher-first: a wide dispatcher (vertical child 130 >=
    # DISPATCHER_MIN_WIDTH=80) keeps its top priority (4) and outranks an
    # existing worker (priority 2), so the new worker is carved out of the
    # dispatcher's pane before the existing worker's viewport is halved.
    panes = [
        _pane(2, name="dispatcher", role="dispatcher",
              x=0, y=40, w=260, h=40),
        _pane(5, name="worker-a", role="worker",
              x=0, y=0, w=200, h=40),
    ]
    choice = choose_split(panes)
    assert choice is not None
    assert choice.target_name == "dispatcher"
    assert choice.role == "dispatcher"
    assert choice.direction == "vertical"
    assert choice.new_w == 130


def test_choose_split_secretary_is_lowest_priority() -> None:
    # The secretary is now the lowest-priority split target: a worker
    # (priority 2) is carved up before the secretary (priority 1) so the
    # secretary's content viewport is preserved. Both panes clear their split
    # floors, so the choice is decided purely by role priority.
    panes = [
        _pane(3, name="secretary", role="secretary", x=0, y=0, w=300, h=100),
        _pane(5, name="worker-a", role="worker", x=0, y=100, w=300, h=100),
    ]
    choice = choose_split(panes)
    assert choice is not None
    assert choice.target_name == "worker-a"
    assert choice.role == "worker"


def test_choose_split_narrow_dispatcher_demotes_below_secretary() -> None:
    # The narrow-dispatcher last resort sits strictly below every role,
    # including the lowest-priority secretary: when the only alternative to a
    # comfortable-width dispatcher (vertical child 60 < DISPATCHER_MIN_WIDTH)
    # is the secretary, the secretary (priority 1 > 0) absorbs the worker so
    # the dispatcher's viewport is protected.
    panes = [
        _pane(2, name="dispatcher", role="dispatcher",
              x=0, y=0, w=120, h=40),
        _pane(3, name="secretary", role="secretary",
              x=0, y=40, w=300, h=40),
    ]
    choice = choose_split(panes)
    assert choice is not None
    assert choice.target_name == "secretary"
    assert choice.role == "secretary"


def test_choose_split_narrow_dispatcher_stays_last_resort() -> None:
    # Self-limit guard: a dispatcher already at its comfortable width
    # (vertical child 60 < DISPATCHER_MIN_WIDTH=80) is demoted to a strict
    # last resort (below every role), so the worker (priority 2) is preferred.
    # This pins that the dispatcher-first rule does not repeatedly halve the
    # dispatcher's own monitoring viewport past usability.
    panes = [
        _pane(2, name="dispatcher", role="dispatcher",
              x=0, y=40, w=120, h=40),
        _pane(5, name="worker-a", role="worker",
              x=0, y=0, w=200, h=40),
    ]
    choice = choose_split(panes)
    assert choice is not None
    assert choice.target_name == "worker-a"


def test_choose_split_nonadjacent_dispatcher_gated_with_resident_curator() -> None:
    # The dispatcher's adjacency gate still applies when a curator is
    # resident: a wide dispatcher that is NOT adjacent to the resident curator
    # is skipped (unexpected layout), so the worker (priority 2) is the target
    # even though a wide dispatcher would otherwise outrank it. The curator is
    # deliberately too small to split, isolating the gate's effect.
    panes = [
        _pane(1, name="curator", role="curator", x=300, y=0, w=18, h=8),
        _pane(2, name="dispatcher", role="dispatcher",
              x=0, y=40, w=260, h=40),
        _pane(5, name="worker-a", role="worker",
              x=0, y=0, w=200, h=40),
    ]
    choice = choose_split(panes)
    assert choice is not None
    assert choice.target_name == "worker-a"


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def test_validate_task_id_accepts_slug() -> None:
    assert validate_task_id("step-d-runner") is None


def test_validate_task_id_rejects_bad_chars() -> None:
    assert validate_task_id("a/b") is not None


def test_validate_task_id_rejects_all_digit_worker_name() -> None:
    # worker-{tid} is "worker-..." which is never all digits, but empty is
    assert validate_task_id("") is not None


def test_validate_cwd_accepts_existing_dir(tmp_path: Path) -> None:
    assert validate_cwd(str(tmp_path)) is None


def test_validate_cwd_rejects_missing(tmp_path: Path) -> None:
    err = validate_cwd(str(tmp_path / "nope"))
    assert err is not None and "does not exist" in err


def test_validate_instruction_vars_unknown_key() -> None:
    raw = {
        "task_description": "x", "dir_setup": "x",
        "branch_strategy": "x", "verification_depth": "full",
        "rogue": "y",
    }
    norm, err = validate_instruction_vars(raw)
    assert norm is None and err is not None and "unknown" in err


def test_validate_instruction_vars_required_missing() -> None:
    raw = {"task_description": "x"}
    norm, err = validate_instruction_vars(raw)
    assert norm is None and err is not None and "missing required" in err


def test_validate_instruction_vars_bad_depth() -> None:
    raw = {
        "task_description": "x", "dir_setup": "x",
        "branch_strategy": "x", "verification_depth": "deep",
    }
    norm, err = validate_instruction_vars(raw)
    assert norm is None and err is not None and "verification_depth" in err


def test_validate_instruction_vars_applies_defaults() -> None:
    raw = {
        "task_description": "x", "dir_setup": "x",
        "branch_strategy": "x", "verification_depth": "full",
    }
    norm, err = validate_instruction_vars(raw)
    assert err is None and norm is not None
    assert norm["report_target"] == "secretary"
    assert norm["claude_md_filename"] == "CLAUDE.md"


# ---------------------------------------------------------------------------
# build_plan
# ---------------------------------------------------------------------------


def _ok_panes() -> list[Pane]:
    return [
        _pane(1, name="curator", role="curator", x=0, y=0, w=100, h=50),
        _pane(2, name="dispatcher", role="dispatcher", x=100, y=0, w=200, h=50),
    ]


def test_build_plan_ready_to_spawn(tmp_path: Path) -> None:
    task = {
        "task_id": "demo",
        "worker_dir": str(tmp_path),
        "instruction": "do the thing",
        "task_description": "demo task",
    }
    plan = build_plan(task, _ok_panes(), tmp_path / ".state")
    assert plan.status == "ready_to_spawn"
    assert plan.spawn is not None
    assert plan.spawn["name"] == "worker-demo"
    assert plan.spawn["model"] == runner.DEFAULT_WORKER_MODEL
    assert plan.spawn["permission_mode"] == "auto"
    assert any(step["tool"] == "send_message" for step in plan.after_spawn)
    assert plan.errors == []


def test_build_plan_input_invalid_bad_task_id(tmp_path: Path) -> None:
    plan = build_plan(
        {"task_id": "", "worker_dir": str(tmp_path)},
        _ok_panes(),
        tmp_path / ".state",
    )
    assert plan.status == "input_invalid"
    assert plan.errors


def test_build_plan_input_invalid_missing_cwd(tmp_path: Path) -> None:
    plan = build_plan(
        {"task_id": "demo"},
        _ok_panes(),
        tmp_path / ".state",
    )
    assert plan.status == "input_invalid"
    assert any("worker_dir" in e for e in plan.errors)


def test_build_plan_input_invalid_duplicate_pane(tmp_path: Path) -> None:
    panes = _ok_panes() + [_pane(99, name="worker-demo", role="worker")]
    plan = build_plan(
        {"task_id": "demo", "worker_dir": str(tmp_path)},
        panes,
        tmp_path / ".state",
    )
    assert plan.status == "input_invalid"
    assert any("already exists" in e for e in plan.errors)


def test_build_plan_input_invalid_existing_state_file(tmp_path: Path) -> None:
    state_dir = tmp_path / ".state"
    seed = state_dir / "workers" / "worker-demo.md"
    seed.parent.mkdir(parents=True)
    seed.write_text("stale", encoding="utf-8")
    plan = build_plan(
        {"task_id": "demo", "worker_dir": str(tmp_path)},
        _ok_panes(),
        state_dir,
    )
    assert plan.status == "input_invalid"


def test_build_plan_split_capacity_exceeded(tmp_path: Path) -> None:
    # Only an unsplittable curator pane -> no candidates.
    panes = [_pane(1, name="curator", role="curator", w=10, h=2)]
    plan = build_plan(
        {"task_id": "demo", "worker_dir": str(tmp_path)},
        panes,
        tmp_path / ".state",
    )
    assert plan.status == "split_capacity_exceeded"
    assert plan.escalate is not None
    assert plan.escalate["to_id"] == "secretary"


def test_build_plan_warns_when_both_instruction_and_vars(tmp_path: Path) -> None:
    task = {
        "task_id": "demo",
        "worker_dir": str(tmp_path),
        "instruction": "explicit wins",
        "instruction_vars": {"task_description": "x"},
    }
    plan = build_plan(task, _ok_panes(), tmp_path / ".state")
    assert plan.status == "ready_to_spawn"
    assert any("explicit `instruction` wins" in w for w in plan.warnings)


# ---------------------------------------------------------------------------
# Side-effect writers (via the CLI dry-run path)
# ---------------------------------------------------------------------------


def test_cli_delegate_plan_writes_state_files(tmp_path: Path) -> None:
    task = {
        "task_id": "cli-demo",
        "worker_dir": str(tmp_path),
        "instruction": "from cli",
        "task_description": "smoke",
    }
    panes = [
        {"id": 1, "name": "curator", "role": "curator",
         "x": 0, "y": 0, "width": 100, "height": 50},
        {"id": 2, "name": "dispatcher", "role": "dispatcher",
         "x": 100, "y": 0, "width": 200, "height": 50},
    ]
    task_path = tmp_path / "task.json"
    panes_path = tmp_path / "panes.json"
    task_path.write_text(json.dumps(task), encoding="utf-8")
    panes_path.write_text(json.dumps(panes), encoding="utf-8")

    state_dir = tmp_path / ".state"
    rc = main([
        "delegate-plan",
        "--task-json", str(task_path),
        "--panes-json", str(panes_path),
        "--state-dir", str(state_dir),
    ])
    assert rc == 0
    assert (state_dir / "workers" / "worker-cli-demo.md").exists()
    assert (
        state_dir / "dispatcher" / "outbox" / "cli-demo-instruction.md"
    ).exists()


def test_cli_delegate_plan_dry_run_writes_nothing(tmp_path: Path) -> None:
    task = {
        "task_id": "dry", "worker_dir": str(tmp_path),
        "instruction": "x", "task_description": "x",
    }
    panes = [
        {"id": 1, "name": "curator", "role": "curator",
         "x": 0, "y": 0, "width": 100, "height": 50},
        {"id": 2, "name": "dispatcher", "role": "dispatcher",
         "x": 100, "y": 0, "width": 200, "height": 50},
    ]
    task_path = tmp_path / "task.json"
    panes_path = tmp_path / "panes.json"
    task_path.write_text(json.dumps(task), encoding="utf-8")
    panes_path.write_text(json.dumps(panes), encoding="utf-8")
    state_dir = tmp_path / ".state"
    rc = main([
        "delegate-plan",
        "--task-json", str(task_path),
        "--panes-json", str(panes_path),
        "--state-dir", str(state_dir),
        "--dry-run",
    ])
    assert rc == 0
    assert not (state_dir / "workers").exists()


def test_cli_delegate_plan_input_invalid_returns_1(tmp_path: Path) -> None:
    task = {"task_id": "", "worker_dir": str(tmp_path)}
    panes: list[dict] = []
    task_path = tmp_path / "task.json"
    panes_path = tmp_path / "panes.json"
    task_path.write_text(json.dumps(task), encoding="utf-8")
    panes_path.write_text(json.dumps(panes), encoding="utf-8")
    rc = main([
        "delegate-plan",
        "--task-json", str(task_path),
        "--panes-json", str(panes_path),
        "--state-dir", str(tmp_path / ".state"),
    ])
    assert rc == 1


def test_load_instruction_template_walks_up_to_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "consumer-repo"
    template = repo / runner.INSTRUCTION_TEMPLATE_PATH
    template.parent.mkdir(parents=True)
    template.write_text(
        "before\n"
        "<!-- AUTO-EXPAND-TEMPLATE-START -->\n"
        "```\n"
        "task={task_description}\n"
        "```\n"
        "<!-- AUTO-EXPAND-TEMPLATE-END -->\n"
        "after\n",
        encoding="utf-8",
    )
    nested = repo / "sub" / "deep"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    body = runner.load_instruction_template()
    assert "task={task_description}" in body


def test_load_instruction_template_missing_raises_value_error(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError) as info:
        runner.load_instruction_template(repo_root=tmp_path)
    assert "--template-repo" in str(info.value)


def test_unified_cli_migrate_subcommand(tmp_path: Path) -> None:
    from claude_org_runtime.cli import main as cli_main

    src = tmp_path / "journal.jsonl"
    src.write_text(
        '{"event": "worker_spawned", "worker": "demo"}\n', encoding="utf-8"
    )
    dst = tmp_path / "out.jsonl"
    rc = cli_main([
        "migrate", "v1-to-v2",
        "--in", str(src), "--out", str(dst),
    ])
    assert rc == 0
    assert dst.exists()
    line = dst.read_text(encoding="utf-8").splitlines()[0]
    assert '"task_id"' in line  # legacy `worker` was carried over


def test_unified_cli_dispatcher_subcommand_help() -> None:
    from claude_org_runtime.cli import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit) as info:
        parser.parse_args(["dispatcher", "delegate-plan", "--help"])
    assert info.value.code == 0


def test_locale_config_english_is_default() -> None:
    en = LocaleConfig.english()
    assert en.constraints_default == "(none)"
    assert en.report_target_default == "secretary"
    assert en.claude_md_filename_default == "CLAUDE.md"
    assert "Worker instruction expanded" in en.instruction_template


def test_validate_instruction_vars_locale_overrides_constraints_default() -> None:
    raw = {
        "task_description": "x", "dir_setup": "x",
        "branch_strategy": "x", "verification_depth": "full",
    }
    ja = LocaleConfig(constraints_default="(なし)")  # "(なし)"
    norm, err = validate_instruction_vars(raw, locale=ja)
    assert err is None and norm is not None
    assert norm["constraints"] == "(なし)"


def test_write_instruction_uses_locale_template(tmp_path: Path) -> None:
    state_dir = tmp_path / ".state"
    ja = LocaleConfig(
        instruction_template=(
            "# タスク: {task_id}\n"
            "Dir: {worker_dir}\n"
            "----\n{instruction}\n"
        ),
    )
    task = {
        "task_id": "demo", "worker_dir": str(tmp_path),
        "instruction": "do the thing",
    }
    out = write_instruction(state_dir, task, "demo", locale=ja)
    body = out.read_text(encoding="utf-8")
    assert body.startswith("# タスク: demo")
    assert "do the thing" in body
    assert "Worker instruction expanded" not in body


def test_cli_locale_json_overrides_constraints(tmp_path: Path) -> None:
    locale_path = tmp_path / "locale.json"
    locale_path.write_text(
        json.dumps({
            "constraints_default": "(なし)",
            "instruction_template": (
                "# T:{task_id}\nD:{worker_dir}\n--\n{instruction}\n"
            ),
        }),
        encoding="utf-8",
    )
    task = {
        "task_id": "loc",
        "worker_dir": str(tmp_path),
        "instruction_vars": {
            "task_description": "x",
            "dir_setup": "x",
            "branch_strategy": "x",
            "verification_depth": "full",
        },
    }
    panes = [
        {"id": 1, "name": "curator", "role": "curator",
         "x": 0, "y": 0, "width": 100, "height": 50},
        {"id": 2, "name": "dispatcher", "role": "dispatcher",
         "x": 100, "y": 0, "width": 200, "height": 50},
    ]
    task_path = tmp_path / "task.json"
    panes_path = tmp_path / "panes.json"
    task_path.write_text(json.dumps(task), encoding="utf-8")
    panes_path.write_text(json.dumps(panes), encoding="utf-8")

    state_dir = tmp_path / ".state"
    rc = main([
        "delegate-plan",
        "--task-json", str(task_path),
        "--panes-json", str(panes_path),
        "--state-dir", str(state_dir),
        "--locale-json", str(locale_path),
        # No real instruction-template available; the task has explicit
        # instruction_vars, so we'd hit load_instruction_template -- avoid
        # that by also providing --template-repo to a stub repo.
        "--template-repo", str(tmp_path / "stub-repo-no-template"),
    ])
    # Missing template -> input_invalid (rc 1) but the locale still
    # parsed cleanly, which is the assertion that matters: a malformed
    # --locale-json would have raised SystemExit before this point.
    assert rc == 1


def test_cli_locale_json_rejects_unknown_field(tmp_path: Path) -> None:
    locale_path = tmp_path / "locale.json"
    locale_path.write_text(json.dumps({"bogus": 1}), encoding="utf-8")
    task = {"task_id": "x", "worker_dir": str(tmp_path), "instruction": "x"}
    panes = [
        {"id": 1, "name": "curator", "role": "curator",
         "x": 0, "y": 0, "width": 100, "height": 50},
        {"id": 2, "name": "dispatcher", "role": "dispatcher",
         "x": 100, "y": 0, "width": 200, "height": 50},
    ]
    task_path = tmp_path / "t.json"
    panes_path = tmp_path / "p.json"
    task_path.write_text(json.dumps(task), encoding="utf-8")
    panes_path.write_text(json.dumps(panes), encoding="utf-8")
    with pytest.raises(SystemExit):
        main([
            "delegate-plan",
            "--task-json", str(task_path),
            "--panes-json", str(panes_path),
            "--state-dir", str(tmp_path / ".state"),
            "--locale-json", str(locale_path),
        ])


def test_cli_locale_json_rejects_non_string_value(tmp_path: Path) -> None:
    locale_path = tmp_path / "locale.json"
    locale_path.write_text(
        json.dumps({"instruction_template": 123}), encoding="utf-8"
    )
    task = {"task_id": "x", "worker_dir": str(tmp_path), "instruction": "x"}
    panes = [
        {"id": 1, "name": "curator", "role": "curator",
         "x": 0, "y": 0, "width": 100, "height": 50},
        {"id": 2, "name": "dispatcher", "role": "dispatcher",
         "x": 100, "y": 0, "width": 200, "height": 50},
    ]
    task_path = tmp_path / "t.json"
    panes_path = tmp_path / "p.json"
    task_path.write_text(json.dumps(task), encoding="utf-8")
    panes_path.write_text(json.dumps(panes), encoding="utf-8")
    with pytest.raises(SystemExit) as info:
        main([
            "delegate-plan",
            "--task-json", str(task_path),
            "--panes-json", str(panes_path),
            "--state-dir", str(tmp_path / ".state"),
            "--locale-json", str(locale_path),
        ])
    assert "must be a string" in str(info.value)
    # Crucially, no side-effect files should have been written.
    assert not (tmp_path / ".state").exists()


def test_cli_locale_json_rejects_template_missing_placeholders(
    tmp_path: Path,
) -> None:
    locale_path = tmp_path / "locale.json"
    locale_path.write_text(
        json.dumps({"instruction_template": "no placeholders"}),
        encoding="utf-8",
    )
    task = {"task_id": "x", "worker_dir": str(tmp_path), "instruction": "x"}
    panes = [
        {"id": 1, "name": "curator", "role": "curator",
         "x": 0, "y": 0, "width": 100, "height": 50},
        {"id": 2, "name": "dispatcher", "role": "dispatcher",
         "x": 100, "y": 0, "width": 200, "height": 50},
    ]
    task_path = tmp_path / "t.json"
    panes_path = tmp_path / "p.json"
    task_path.write_text(json.dumps(task), encoding="utf-8")
    panes_path.write_text(json.dumps(panes), encoding="utf-8")
    with pytest.raises(SystemExit) as info:
        main([
            "delegate-plan",
            "--task-json", str(task_path),
            "--panes-json", str(panes_path),
            "--state-dir", str(tmp_path / ".state"),
            "--locale-json", str(locale_path),
        ])
    assert "missing required placeholders" in str(info.value)


@pytest.mark.parametrize("template,expected_msg", [
    # Unknown placeholder
    ("{task_id} {worker_dir} {instruction} {bogus}", "format() failed"),
    # Unbalanced trailing brace
    ("{task_id}{worker_dir}{instruction}{", "format() failed"),
    # Missing required placeholder (only worker_dir + instruction; no task_id)
    ("{worker_dir} -- {instruction}", "missing required placeholders"),
    # Attribute access on a string sentinel: AttributeError surface.
    ("{task_id.bogus_attr} {worker_dir} {instruction}", "format() failed"),
    # Item access with a non-int key: TypeError surface.
    ("{task_id[bogus]} {worker_dir} {instruction}", "format() failed"),
])
def test_cli_locale_json_rejects_malformed_template(
    tmp_path: Path, template: str, expected_msg: str,
) -> None:
    locale_path = tmp_path / "locale.json"
    locale_path.write_text(
        json.dumps({"instruction_template": template}), encoding="utf-8",
    )
    task = {"task_id": "x", "worker_dir": str(tmp_path), "instruction": "x"}
    panes = [
        {"id": 1, "name": "curator", "role": "curator",
         "x": 0, "y": 0, "width": 100, "height": 50},
        {"id": 2, "name": "dispatcher", "role": "dispatcher",
         "x": 100, "y": 0, "width": 200, "height": 50},
    ]
    task_path = tmp_path / "t.json"
    panes_path = tmp_path / "p.json"
    task_path.write_text(json.dumps(task), encoding="utf-8")
    panes_path.write_text(json.dumps(panes), encoding="utf-8")
    with pytest.raises(SystemExit) as info:
        main([
            "delegate-plan",
            "--task-json", str(task_path),
            "--panes-json", str(panes_path),
            "--state-dir", str(tmp_path / ".state"),
            "--locale-json", str(locale_path),
        ])
    assert expected_msg in str(info.value)
    # Reject before any worker-state file is written.
    assert not (tmp_path / ".state").exists()


def test_action_plan_dataclass_default() -> None:
    plan = ActionPlan(status="ready_to_spawn", task_id="x")
    assert plan.spawn is None
    assert plan.after_spawn == []
    assert plan.warnings == []
