# claude-org-runtime CLI

`claude-org-runtime` exposes a single console entry point with two
subcommand groups -- `dispatcher` and `settings` -- plus the existing
`migrate` module. Each group can also be invoked directly via
`python -m`.

```sh
pip install claude-org-runtime
claude-org-runtime --version           # 0.1.0
claude-org-runtime --help
```

## `dispatcher delegate-plan`

Computes the deterministic parts of the Dispatcher delegation state
machine (balanced split target selection, name/cwd validation,
instruction-template rendering, worker seed + outbox file writes) and
emits a JSON action plan that Dispatcher Claude reads and executes via
MCP tool calls. The helper does NOT call MCP tools directly.

```sh
claude-org-runtime dispatcher delegate-plan \
    --task-json .state/dispatcher/inbox/<task_id>.json \
    --panes-json panes.json \
    --state-dir .state
```

Equivalent module form:

```sh
python -m claude_org_runtime.dispatcher.runner delegate-plan \
    --task-json ... --panes-json ... --state-dir .state
```

### Flags

| Flag | Description |
|------|-------------|
| `--task-json PATH` | Path to a task JSON file (object with `task_id`, `worker_dir`, `instruction` or `instruction_vars`, etc.). Mutually exclusive with `--task-stdin`. |
| `--task-stdin` | Read the task JSON from stdin. |
| `--panes-json PATH` | Path to a JSON file containing renga `list_panes` output (a list of pane dicts, or `{panes: [...]}`). |
| `--state-dir PATH` | State directory root. Default: `.state`. |
| `--template-repo PATH` | Repo root that hosts `.claude/skills/org-delegate/references/instruction-template.md`. Default: try the runtime package's ancestors first, then walk up from CWD. |
| `--locale-json PATH` | Override the English defaults for non-English consumers (e.g. claude-org-ja). The JSON file maps to `LocaleConfig` fields: `constraints_default`, `report_target_default`, `claude_md_filename_default`, `instruction_template`. |
| `--dry-run` | Compute and print the plan without writing the worker seed / outbox files. |

### LocaleConfig

The runtime ships English-only worker instruction copy
(`LocaleConfig.english()`). Consumers whose workers run in another
language can override the locale either programmatically:

```python
from claude_org_runtime.dispatcher import LocaleConfig
from claude_org_runtime.dispatcher.runner import build_plan

ja = LocaleConfig(
    constraints_default="(なし)",
    instruction_template=(
        "# タスク: {task_id}\n"
        "作業ディレクトリ: `{worker_dir}`\n\n"
        "## 指示\n{instruction}\n"
    ),
)
plan = build_plan(task, panes, state_dir, locale=ja)
```

or from the CLI via `--locale-json`:

```sh
claude-org-runtime dispatcher delegate-plan \
    --task-json ... --panes-json ... \
    --locale-json /path/to/locale.ja.json
```

`locale.ja.json` is a flat JSON object whose keys match the
`LocaleConfig` field names; unknown keys are rejected with a clear
error.

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | `ready_to_spawn` -- plan emitted, side-effect files written (unless `--dry-run`). |
| `1` | `input_invalid` -- task JSON / panes / cwd validation failed. |
| `2` | `split_capacity_exceeded` -- no balanced-split candidate; `escalate` field tells Dispatcher to notify Secretary for human judgment. |

## `settings generate`

Renders a per-role `<worker_dir>/.claude/settings.local.json` from the
bundled `role_configs_schema.json` (the SoT now ships with the runtime,
so consumers no longer need a `tools/role_configs_schema.json` copy).

```sh
claude-org-runtime settings generate \
    --role default \
    --worker-dir /path/to/worker \
    --claude-org-path /path/to/claude-org \
    --out /path/to/worker/.claude/settings.local.json
```

Equivalent module form:

```sh
python -m claude_org_runtime.settings.generator \
    --role default --worker-dir ... --claude-org-path ... --out ...
```

### Flags

| Flag | Description |
|------|-------------|
| `--role NAME` | Worker role (`default`, `claude-org-self-edit`, `doc-audit`, ...). |
| `--worker-dir PATH` | Absolute path that `{worker_dir}` resolves to. |
| `--claude-org-path PATH` | Absolute path to the claude-org repo (for hook script paths). |
| `--out PATH` | Output file. Default: stdout. |
| `--schema PATH` | Schema-path override. Default: bundled `role_configs_schema.json`. |

## `settings show`

Renders the same per-role settings as `settings generate` and, with
`--explain`, surfaces Phase 3 case E sandbox suppression metadata
(`worker_roles.<role>.sandbox` is described under
`worker_roles.$comment_sandbox` in the bundled schema). The `show` and
`generate` commands share the same renderer, so the deny set you see
under `--explain` is exactly what would be written by `generate`.

```sh
claude-org-runtime settings show \
    --role default \
    --worker-dir /path/to/worker \
    --claude-org-path /path/to/claude-org \
    --explain --json
```

### Flags

| Flag | Description |
|------|-------------|
| `--role NAME` | Same as `settings generate`. |
| `--worker-dir PATH` | Same as `settings generate`. |
| `--claude-org-path PATH` | Same as `settings generate`. |
| `--out PATH` | Output file. Default: stdout. |
| `--schema PATH` | Schema-path override. Default: bundled. |
| `--explain` | Include sandbox suppression metadata: `wsl_detected`, the normalized user-supplied `sandbox_read_roots` (the configured `worker_dir` + `additionalDirectories`, *not* realpath-resolved — the realpath only applies to deny entries during the escape check), and the per-entry `suppressions` list (`layer`, `entry`, `reason`, `realpath`). |
| `--json` | Emit a structured JSON payload instead of the human-readable text. |

The runtime applies WSL/realpath suppression at render time: any
`sandbox.filesystem.denyRead / denyWrite` entry whose realpath escapes
the sandbox read roots (`worker_dir` + `additionalDirectories`) is
dropped from the rendered sandbox object — this handles WSL
(`/home/<u>/...` resolving into `/mnt/c/...`) and devcontainer
(`/workspaces` symlink) cases without hard-coding any host path.
`permissions.deny Read(...) / Write(...)` (Layer 2) is **never**
suppressed.

## Migration from `claude-org-ja`'s `tools/`

If your `claude-org-ja` checkout was previously calling either of the
following in-tree scripts:

- `python tools/dispatcher_runner.py delegate-plan ...`
- `python tools/generate_worker_settings.py ...`

replace them with the runtime equivalents:

```diff
- python tools/dispatcher_runner.py delegate-plan --task-json ... --panes-json ...
+ python -m claude_org_runtime.dispatcher.runner delegate-plan --task-json ... --panes-json ...

- python tools/generate_worker_settings.py --role default --worker-dir ...
+ python -m claude_org_runtime.settings.generator --role default --worker-dir ...
```

The CLI flags are identical; the only behavioural difference is that
`dispatcher_runner` now defaults its instruction-template anchor to the
process's current working directory (the in-tree script anchored to
`<repo>/tools/..`). Pass `--template-repo /path/to/claude-org-ja` to
override if the helper is invoked from somewhere other than the
claude-org-ja repo root.

The bundled `role_configs_schema.json` mirrors
`claude-org-ja/tools/role_configs_schema.json` as of v0.1.0; subsequent
schema edits will land in their own runtime release rather than via
in-place tool edits.
