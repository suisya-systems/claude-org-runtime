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

Exit codes:
  0 -- plan emitted OK (status = ``ready_to_spawn``)
  1 -- input validation failed (status = ``input_invalid``)
  2 -- algorithm produced no candidate and escalation is required
       (status = ``split_capacity_exceeded``)
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

# Re-export for documentation / downstream importers (Step B + C symbols).
from claude_org_runtime import prompts as _prompts  # noqa: F401
from claude_org_runtime import schema as _schema  # noqa: F401

# Matches renga's name/role validation (see `set_pane_identity` docs).
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_ALL_DIGITS = re.compile(r"^\d+$")

# Balanced split constants -- keep in sync with
# claude-org-ja `.claude/skills/org-delegate/references/pane-layout.md`.
MIN_PANE_WIDTH = 20
MIN_PANE_HEIGHT = 5
SECRETARY_MIN_WIDTH = 125
SECRETARY_MIN_HEIGHT = 45

# Default Claude model for worker panes. The auto-mode safety classifier
# is unstable on sonnet -- opus-only per the claude-org-ja worker-model
# feedback note.
DEFAULT_WORKER_MODEL = "opus"

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
        return cls(
            id=int(d["id"]),
            name=d.get("name"),
            role=d.get("role"),
            focused=bool(d.get("focused", False)),
            x=int(d["x"]),
            y=int(d["y"]),
            width=int(d["width"]),
            height=int(d["height"]),
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


def choose_split(panes: list[Pane]) -> Optional[SplitChoice]:
    """Select the next balanced-split target/direction, or None if no candidate.

    Mirrors Step 3-1b of claude-org-ja's ``org-delegate`` skill.
    """
    curator = next((p for p in panes if p.role == "curator"), None)

    candidates: list[SplitChoice] = []
    for p in panes:
        if p.role not in ("secretary", "dispatcher", "worker"):
            continue

        if p.role == "dispatcher":
            if curator is None or not rect_adjacent(p, curator):
                continue

        if p.width > p.height * 2:
            direction = "vertical"
            new_w = p.width // 2
            new_h = p.height
            metric = new_w
        else:
            direction = "horizontal"
            new_w = p.width
            new_h = p.height // 2
            metric = new_h

        if new_w < MIN_PANE_WIDTH or new_h < MIN_PANE_HEIGHT:
            continue

        if p.role == "secretary" and (
            new_w < SECRETARY_MIN_WIDTH or new_h < SECRETARY_MIN_HEIGHT
        ):
            continue

        if p.name is None:
            continue

        candidates.append(SplitChoice(
            target_name=p.name,
            target_id=p.id,
            direction=direction,
            new_w=new_w,
            new_h=new_h,
            metric=metric,
        ))

    if not candidates:
        return None

    candidates.sort(key=lambda c: (-c.metric, c.target_id))
    return candidates[0]


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


def build_plan(
    task: dict[str, Any],
    panes: list[Pane],
    state_dir: Path,
    template_repo: Optional[Path] = None,
    locale: Optional[LocaleConfig] = None,
) -> ActionPlan:
    task_id = task.get("task_id", "")
    plan = ActionPlan(status="ready_to_spawn", task_id=task_id)

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
    if any(p.name == worker_name for p in panes):
        plan.status = "input_invalid"
        plan.errors.append(
            f"pane named {worker_name!r} already exists in the tab; "
            "close it first or pick a different task_id"
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

    choice = choose_split(panes)
    if choice is None:
        plan.status = "split_capacity_exceeded"
        plan.escalate = {
            "tool": "send_message",
            "to_id": "secretary",
            "message": (
                f"SPLIT_CAPACITY_EXCEEDED: no balanced-split target found for "
                f"task {task_id!r}. The rect-based balanced split's MIN_PANE / "
                "adjacency constraints produced 0 candidates. Likely terminal "
                "size shortage or unexpected layout -- human judgment required."
            ),
        }
        return plan

    permission_mode = task.get("permission_mode", "auto")
    model = task.get("model") or DEFAULT_WORKER_MODEL
    extra_args = task.get("args") or []

    spawn: dict[str, Any] = {
        "tool": "spawn_claude_pane",
        "target": choice.target_name,
        "direction": choice.direction,
        "name": worker_name,
        "role": "worker",
        "cwd": cwd,
        "permission_mode": permission_mode,
        "model": model,
    }
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
            "reason": "approve 'Load development channel?' Y/n prompt",
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
    return [Pane.from_dict(d) for d in panes_list]


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

    state_dir = Path(args.state_dir).resolve()
    template_repo = (
        Path(args.template_repo).resolve() if args.template_repo else None
    )
    locale = _load_locale(args.locale_json)

    plan = build_plan(
        task, panes, state_dir,
        template_repo=template_repo, locale=locale,
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
