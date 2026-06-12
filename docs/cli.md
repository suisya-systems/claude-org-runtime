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
| `--role-kind {worker,org}` | Schema bucket: `worker` (default, `schema['worker_roles']`) or `org` (`schema['roles']`). NOTE: `--role-kind org` is rejected by `settings generate` because org `settings.local.json` files are hand-maintained; use `settings show --role-kind org` for inspection. |
| `--base-clone PATH` | Pattern B context: substituted as `{base_clone}` in entry paths and `additionalDirectories` before realpath evaluation. |
| `--task-id ID` | Pattern B context: substituted as `{task_id}`. |
| `--branch-ref REF` | Pattern B context: substituted as `{branch_ref}`. |
| `--pattern {A,B,C}` | Dispatch pattern. Required when the selected role declares `sandbox_by_pattern`; the renderer then forwards `sandbox_by_pattern[<pattern>]` as the role's sandbox surface (contract SoT: claude-org-ja's `docs/contracts/role-pattern-sandbox-contract.md`, not part of this runtime repo). For roles using the legacy single `sandbox` shape it stays informational and is ignored by the renderer. Free-form values like `b` are rejected by argparse to prevent silent fallthrough. |

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
| `--role-kind {worker,org}` | Schema bucket: `worker` (default) or `org` (for inspecting secretary / dispatcher / curator sandbox intent). |
| `--base-clone PATH` | Pattern B context: substituted as `{base_clone}` before realpath evaluation. |
| `--task-id ID` | Pattern B context: substituted as `{task_id}`. |
| `--branch-ref REF` | Pattern B context: substituted as `{branch_ref}`. |
| `--pattern {A,B,C}` | Same as `settings generate`: required when the role declares `sandbox_by_pattern`, otherwise informational. |

The runtime applies WSL/realpath suppression at render time: any
`sandbox.filesystem.denyRead / denyWrite` entry whose realpath escapes
the sandbox read roots (`worker_dir` + `additionalDirectories`) is
dropped from the rendered sandbox object — this handles WSL
(`/home/<u>/...` resolving into `/mnt/c/...`) and devcontainer
(`/workspaces` symlink) cases without hard-coding any host path.
`permissions.deny Read(...) / Write(...)` (Layer 2) is **never**
suppressed.

## `org up` / `org down`

A thin session launcher over the broker control plane (the `daemon.json`
sidecar + admin RPC). It does **not** re-implement any control-plane
logic; it orchestrates the existing primitives.

```sh
claude-org-runtime org up               # reuse-or-start the daemon, launch secretary TUI
claude-org-runtime org down             # stop the daemon (signal-free) and verify
```

`org up`:

1. Reads the `daemon.json` sidecar under `--state-dir` and judges health
   by **reachability** (not PID liveness): it mints a `secretary`-tier
   root token via the admin RPC and confirms an MCP `initialize` ->
   `tools/list` round-trip. Reachable -> reuse; unreachable (stale
   sidecar) -> start a fresh daemon in the background and discover its
   port from the newly published sidecar.
2. A *live* daemon with a different `--backend` than requested is a
   conflict (run `org down` first); an already-registered `secretary` on a
   live daemon makes `org up` a no-op ("already up").
3. Writes the minted secretary's `--mcp-config` to
   `<state-dir>/secretary-mcp.json` (mode `0600`).
4. Launches the interactive `claude` TUI. The argv is built only through
   the billing-neutral builder, so headless flags can never leak in. POSIX
   `exec`s; Windows launches a subprocess (falling back to printing the
   command if `claude` is not found).

`org down` discovers the daemon from its sidecar, closes residual
`claude` / `codex` agent panes, requests a signal-free `shutdown`, and
verifies `broker_stopped` appears exactly once in this run's
`journal_offset` slice before cleaning up the sidecar. With no sidecar it
is a no-op.

### `org up` flags

| Flag | Description |
|------|-------------|
| `--state-dir PATH` | Daemon state dir (sidecar / queue). Default: `.state/broker`. |
| `--backend NAME` | Terminal backend for the daemon (default: OS auto — POSIX=tmux / Windows=wezterm). Must match a running daemon when reusing. |
| `--root-cwd PATH` | cwd given to the secretary bind = anchor for relative-`cwd` spawns (Issue #61). Default: the directory `org up` runs in. |
| `--name NAME` | secretary agent id/name to mint. Default: `secretary`. |
| `--model VALUE` | Forwarded to the secretary TUI as `--model <value>`. |
| `--permission-mode VALUE` | Forwarded to the secretary TUI as `--permission-mode <value>`. |
| `--claude-arg ARG` | Extra interactive `claude` flag appended after the structured fields (repeatable). Reserved / headless flags are rejected by the builder. |

### `org down` flags

| Flag | Description |
|------|-------------|
| `--state-dir PATH` | Daemon state dir to discover the sidecar. Default: `.state/broker`. |

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | up: launched (or already up); down: `broker_stopped` verified (or no sidecar). |
| `1` | down: shutdown requested but `broker_stopped` not observed / daemon unreachable. |
| `2` | up: backend conflict with a live daemon, or admin mint / MCP surface unhealthy. |

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
