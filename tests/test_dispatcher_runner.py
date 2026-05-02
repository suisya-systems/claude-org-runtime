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


def test_choose_split_prefers_dispatcher_when_only_candidate() -> None:
    panes = [
        _pane(1, name="curator", role="curator", x=0, y=0, w=100, h=50),
        _pane(2, name="dispatcher", role="dispatcher", x=100, y=0, w=200, h=50),
    ]
    choice = choose_split(panes)
    assert choice is not None
    assert choice.target_name == "dispatcher"
    assert choice.direction == "vertical"


def test_choose_split_picks_largest_metric() -> None:
    # Secretary 300x100 -> vertical split -> 150x100, metric=150.
    # Dispatcher 200x50 -> vertical split -> 100x50, metric=100.
    panes = [
        _pane(1, name="curator", role="curator", x=0, y=0, w=100, h=50),
        _pane(2, name="dispatcher", role="dispatcher", x=100, y=0, w=200, h=50),
        _pane(3, name="secretary", role="secretary", x=0, y=50, w=300, h=100),
    ]
    choice = choose_split(panes)
    assert choice is not None
    assert choice.target_name == "secretary"


def test_choose_split_returns_none_when_no_candidate() -> None:
    panes = [_pane(1, name="dispatcher", role="dispatcher", w=10, h=2)]
    assert choose_split(panes) is None


def test_choose_split_dispatcher_requires_curator_adjacency() -> None:
    panes = [
        _pane(1, name="curator", role="curator", x=0, y=0, w=100, h=50),
        # dispatcher not adjacent to curator (gap)
        _pane(2, name="dispatcher", role="dispatcher", x=200, y=0, w=200, h=50),
    ]
    assert choose_split(panes) is None


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
