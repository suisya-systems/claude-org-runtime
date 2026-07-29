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

### Symlink canonicalization of deny paths

Suppressing a Layer 3 entry is not enough on its own, because Claude Code
merges **both** layers into the single deny set it hands to bubblewrap
([docs](https://code.claude.com/docs/en/sandboxing): "Paths from both
`sandbox.filesystem` settings and permission rules are merged together
into the final sandbox configuration"). A Layer 2 credential mirror kept
as the compensating control for a suppressed Layer 3 entry — e.g.
`Read(~/.aws/*)` — therefore re-injects the very path that was
suppressed.

That matters because bubblewrap materializes one mount point per deny
path inside a staging newroot *before* the pivot. An **absolute** symlink
anywhere in the chain resolves against a root where the target does not
exist yet, so mount-point creation fails and bwrap aborts the whole
launch:

```
bwrap: Can't create file at /home/<user>/.aws/config: No such file or directory
```

The launch failure is not fail-closed: Claude Code's escape hatch then
retries the command with `dangerouslyDisableSandbox`, so every subsequent
Bash command runs unsandboxed with no standing signal. On WSL2 this fires
whenever a credential directory is a symlink into `/mnt/c`.

So the renderer **rewrites** an escaping deny path to its realpath rather
than dropping it, across both layers:

| Rendered as | Becomes |
|-------------|---------|
| `Read(~/.aws/*)` (with `~/.aws -> /mnt/c/Users/u/.aws`) | `Read(//mnt/c/Users/u/.aws/*)` |
| `sandbox.filesystem.denyRead: ["/home/u/.aws/config"]` | `["/mnt/c/Users/u/.aws/config"]` |

Both guarantees survive: bwrap can bind the realpath form, and the Layer 2
tool-level block still applies to reads issued through the original
symlinked path, because Claude Code resolves symlinks when matching
`Read` / `Edit` deny rules.

Only *absolute* symlinks are rewritten. Relative symlinks resolve
correctly inside bwrap's staging tree, and unanchored globs such as
`Read(**/credentials*)` are never expanded into host paths, so neither
needs canonicalization. Rewrites are reported in
`settings show --explain` (`rewrites`) and appended to the emitted
`$comment` as `; symlink-canonicalized deny paths: [...]` — the
contract-fixed `platform=<linux|wsl>, layer-3 entries suppressed: [`
prefix is left byte-identical.

## `sandbox doctor`

Preflight a rendered `settings.local.json` and fail loudly when the
sandbox would not actually start. The generator canonicalizes what it
renders, but a worker's *effective* deny set is the merge of several
settings scopes (user `~/.claude/settings.json`, project, managed) and
only some come from this runtime — any scope can contribute a path that
takes the sandbox down.

```sh
claude-org-runtime sandbox doctor --settings path/to/settings.local.json
```

| Flag | Description |
|------|-------------|
| `--settings PATH` | Settings file to check. Required; repeat to add scopes. |
| `--no-merge-scopes` | Check only the given files; skip user / managed settings. |
| `--json` | Machine-readable report instead of the text one. |
| `--verbose` | List every deny target, not just failing ones. |
| `--no-probe-bwrap` | Static analysis only; skip the live bwrap canary. |

By default the user settings (`~/.claude/settings.json`) and managed
settings are merged in alongside the given file, because Claude Code
unions the deny arrays across scopes: a symlinked path in *any* scope
aborts the launch no matter how clean the rendered worker file is.
Checking the worker file alone would report a clean preflight for a
sandbox that cannot start. Each finding names the file that contributed
it, so the fix lands in the right place. `sandbox.enabled` is resolved
conservatively — the gate relaxes only when no scope enables the sandbox
and at least one explicitly disables it.

It does two independent checks:

1. **Static analysis** — collects every deny path the settings contribute
   (Layer 3 `deny{Read,Write}` plus Layer 2 `Read` / `Edit` rules) and
   flags those crossing an absolute symlink, with the realpath rewrite
   that would fix each.
2. **Live canary** — when `bwrap` is on `PATH`, actually launches it with
   those paths bound and reports whether the sandbox comes up. This
   catches unbindable paths whose cause is *not* a symlink.

The canary deliberately passes no `--proc` / `--dev`. Those mount fresh
filesystems *over* the corresponding host trees, and a shadowed region
contains no symlink for bwrap to trip over — it just creates plain
directories and succeeds. Probing with them would blind the canary to any
deny path under the shadowed prefix and make it contradict the static
analysis.

That shadowing is also the only case where the two checks can disagree: a
deny path crossing an absolute symlink binds fine *while* some mount hides
the link. The doctor still reports it as a failure, and says why — a deny
path that works only because something happens to be mounted over it
aborts the launch the moment that stops being true.

Exit status is `0` when the deny paths are usable, `1` when either check
fails, and `2` on a missing / malformed settings file — so it can gate a
worker launch rather than being advisory. The shapes it reads are
validated up front, because a `deny` given as a bare string is iterable
and would otherwise be scanned character by character and reported clean.

Settings that explicitly set `sandbox.enabled: false` pass the gate:
no sandbox launches, so no launch can be aborted. Any finding is still
printed and labelled latent, because the deny arrays merge across
settings scopes and become live as soon as another scope enables the
sandbox. An *absent* `sandbox` key is treated as unknown rather than
off, since user or managed settings can enable it for a role that never
mentions it.

### On `failIfUnavailable` and `allowUnsandboxedCommands`

`sandbox.failIfUnavailable` does **not** cover this failure. Per the
[official docs](https://code.claude.com/docs/en/sandboxing) it governs a
*missing dependency* such as bubblewrap not being installed, which blocks
Claude Code from starting — not a per-command bwrap launch failure on a
machine where bwrap is present and working.

The knob that governs the silent fallback is
`sandbox.allowUnsandboxedCommands: false` (shown as **Strict sandbox
mode** in the `/sandbox` Overrides tab), which makes the
`dangerouslyDisableSandbox` retry be ignored. This runtime does **not**
set it, because the blast radius is fleet-wide: Claude Code's docs list
`docker` as incompatible with the sandbox, and the `default` and
`claude-org-self-edit` worker roles both allow `docker build` while the
runtime ships no `excludedCommands`. Turning strict mode on without first
adding those exclusions would make those workers fail outright rather
than silently lose isolation. `sandbox doctor` is the non-breaking half
of the answer: it makes the loss of isolation visible without changing
what happens when a command cannot be sandboxed.

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

`org down` discovers the daemon from its sidecar, closes residual broker
panes, requests a signal-free `shutdown`, and verifies `broker_stopped`
appears exactly once in this run's `journal_offset` slice before cleaning
up the sidecar. The pane-close scope follows the daemon's backend: on an
isolated backend (tmux) all broker-owned panes are closed (including
generic `spawn_pane` panes like the attention watcher); on a global-mux
backend (wezterm) only `claude` / `codex` agent children are closed to
avoid killing unrelated panes. With no sidecar it is a no-op.

### Resident pre-flight (Issue #142)

Both commands run a **pre-flight** over the broker-*unmanaged* resident
registry at `<state-dir>/residents/*.json` -- processes that live outside
the daemon's lifecycle (e.g. a queue / attention watcher) and can be left
behind by a crashed generation. `org up` sweeps **only on a cold start**
(never on a healthy-daemon reuse, so it cannot terminate a running org's own
live residents); `org down` sweeps **after** teardown and never changes its
own return code. The default is **announce-only**: it prints what it found
and leaves everything in place. `--reap` (opt-in) terminates residents whose
ownership *and* identity both verify and removes stale registrations; it
never touches records owned by a different org instance, and never kills on
an unknown schema version or an unverifiable identity. On Windows a matched
resident is stopped with a graceful `taskkill` and, if it survives, manual
`taskkill /F` guidance is printed rather than an auto hard-kill. An
absent/empty registry prints nothing. The registry schema, ownership /
identity model, and full decision matrix are the contract in
[`docs/broker-residents-registry-contract.md`](broker-residents-registry-contract.md).

### `org up` flags

| Flag | Description |
|------|-------------|
| `--state-dir PATH` | Daemon state dir (sidecar / queue). Default: `.state/broker`. |
| `--backend NAME` | Terminal backend for the daemon: `tmux`, `wezterm`, or `herdr` (default: OS auto - POSIX=tmux / Windows=wezterm). `herdr` is an opt-in **POSIX / WSL-only** backend and is not supported on native Windows (see note below). Must match a running daemon when reusing. |
| `--root-cwd PATH` | cwd given to the secretary bind = anchor for relative-`cwd` spawns (Issue #61). Default: the directory `org up` runs in. |
| `--name NAME` | secretary agent id/name to mint. Default: `secretary`. |
| `--model VALUE` | Forwarded to the secretary TUI as `--model <value>`. |
| `--permission-mode VALUE` | Forwarded to the secretary TUI as `--permission-mode <value>`. |
| `--claude-arg ARG` | Extra interactive `claude` flag appended after the structured fields (repeatable). Reserved / headless flags are rejected by the builder. |
| `--reap` | Terminate broker-unmanaged residents whose ownership **and** identity both verify, and remove stale registrations (default: announce only). Cold start only. See resident pre-flight above. |

#### Backend support

`tmux` (POSIX) and `wezterm` (POSIX / Windows) are the general-purpose
backends. `herdr` is an **opt-in POSIX / WSL-only** backend: it talks to the
`herdr` daemon over a Unix domain socket, which native Windows does not provide,
so `--backend herdr` is unsupported when running on native Windows (`os.name ==
"nt"`). There `org up` fails fast with an actionable error before spawning the
daemon (rather than the daemon dying and timing out); use `--backend wezterm`
on native Windows or run under WSL. To drive a remote `herdr` session from
Windows, use the renga transport instead.

### `org down` flags

| Flag | Description |
|------|-------------|
| `--state-dir PATH` | Daemon state dir to discover the sidecar. Default: `.state/broker`. |
| `--reap` | Terminate broker-unmanaged residents whose ownership **and** identity both verify, and remove stale registrations (default: announce only). See resident pre-flight above. |
| `--root-cwd PATH` | Repo root used to match resident ownership. Default: read from the daemon sidecar, else the directory `org down` runs in. |

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | up: launched (or already up); down: `broker_stopped` verified (or no sidecar). |
| `1` | down: shutdown requested but `broker_stopped` not observed / daemon unreachable. |
| `2` | up: unknown backend, a backend unsupported on this platform (e.g. `herdr` on native Windows), backend conflict with a live daemon, or admin mint / MCP surface unhealthy. |

## `broker send`

Enqueue one message to another agent through a running broker daemon. This is a
thin, **best-effort** bridge for plain CLI subprocesses (e.g. a pane's
`pr-watch` or `claude-org-ja`'s `tools/peer_notify.py`) that cannot call the
`mcp__org-broker__send_message` MCP tool directly.

```
claude-org-runtime broker send --to <agent_id> --message <text> [--state-dir PATH]
```

### Flags

| Flag | Description |
|------|-------------|
| `--to AGENT_ID` | Recipient agent id or name (resolved by the broker queue). Required. |
| `--message TEXT` | Message text to enqueue. Required. |
| `--state-dir PATH` | Daemon state dir used to discover the sidecar. Optional; see resolution order below. |

### State-dir resolution order

The daemon's state dir is resolved with the precedence:

1. the `--state-dir` flag (when explicitly passed);
2. the `ORG_BROKER_STATE_DIR` environment variable;
3. the default `.state/broker` (relative to the current working directory).

When the broker spawns a pane, it injects `ORG_BROKER_STATE_DIR` (the daemon's
**absolute** state dir) into the pane's environment. A CLI subprocess running
inside that pane therefore reaches the correct daemon **without** having to
thread `--state-dir` through — this is what lets `broker send` work when the
daemon was started with a non-default `--state-dir` (Issue #122). The env var
name is a contract shared with the `claude-org-ja` consumer; do not rename it.

### Exit codes and diagnostics

`broker send` never raises: every failure is absorbed into a non-zero exit and a
short one-line ASCII diagnostic on stderr (the message body is never echoed).

| Code | Meaning |
|------|---------|
| `0` | Enqueued (delivered to the broker queue). |
| non-0 | Undelivered (no sidecar / auth failure / unknown recipient / daemon unreachable). |

If the recorded daemon pid is no longer alive when the daemon is unreachable,
the diagnostic appends a `stale sidecar? pass --state-dir or set
ORG_BROKER_STATE_DIR` hint pointing at the two resolution knobs above.

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
