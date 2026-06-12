# claude-org-runtime

Python runtime extracted from [claude-org-ja](https://github.com/suisya-systems/claude-org-ja)
for Claude Code orchestrator setups using renga panes. Sibling of
[core-harness](https://github.com/suisya-systems/core-harness); provides a
dispatcher runner, state schema, and reference role prompts (secretary /
dispatcher / curator).

Not an AI agent framework — this is plumbing for human-driven Claude Code
orchestrator workflows, not an autonomous-agent toolkit.

## Status

**0.1.0**: dispatcher runner + settings generator + reference role
prompts + state schema + `v1->v2` polymorphic migrate. This is the first
release that consumers (notably `claude-org-ja`) can `pip install` to
replace their in-tree `tools/dispatcher_runner.py` /
`tools/generate_worker_settings.py` / `tools/role_configs_schema.json`.

## Install

```sh
pip install claude-org-runtime==0.1.0
```

For local development:

```sh
git clone https://github.com/suisya-systems/claude-org-runtime
cd claude-org-runtime
python -m pip install -e .[dev]
```

## Quick start

The single-command front door brings up a whole orchestrator session:

```sh
# Start the broker daemon, mint a secretary token, write its mcp-config,
# and drop you into an interactive Claude Code TUI — one command:
claude-org-runtime org up

# Tear it back down: close residual broker panes, request a signal-free
# shutdown, verify it stopped, and clean up the sidecar:
claude-org-runtime org down
```

`org up` is reuse-or-start: a reachable daemon is reused, a stale sidecar
is replaced. The lower-level building blocks are still available directly:

```sh
# Render a per-role settings.local.json:
claude-org-runtime settings generate \
    --role default \
    --worker-dir /path/to/worker \
    --claude-org-path /path/to/claude-org \
    --out /path/to/worker/.claude/settings.local.json

# Compute a Dispatcher delegation action plan:
claude-org-runtime dispatcher delegate-plan \
    --task-json .state/dispatcher/inbox/<task_id>.json \
    --panes-json panes.json \
    --state-dir .state
```

See [`docs/cli.md`](docs/cli.md) for the full CLI reference, the `org up` /
`org down` flags, and the migration recipe for `claude-org-ja` consumers.

## Broker

The broker is a renga-free transport for orchestrator sessions: a localhost
MCP daemon plus a persisted queue and a terminal adapter (tmux / wezterm
backends). Agents talk to it over plain HTTP MCP instead of a renga tab, so
a session can run without the renga UI. It is billing-neutral by design —
it only ever launches the interactive Claude Code TUI through a builder
that rejects headless flags. `org up` / `org down` are a thin launcher over
this control plane (the `daemon.json` sidecar + admin RPC); see
[`docs/cli.md`](docs/cli.md) for details.

## Reference role prompts

The runtime ships English reference prompts for the three roles used in
the `claude-org-ja` reference organization (`secretary`, `dispatcher`,
`curator`):

```python
from claude_org_runtime.prompts import load, load_meta

prompt = load("dispatcher")          # raw markdown, frontmatter included
meta = load_meta("dispatcher")       # {'role': 'dispatcher', 'source': ..., 'status': ...}
```

These are **reference**, not prescriptive. They capture one working
configuration from `claude-org-ja`, not an "agent framework" opinion.
Consumers are expected to load them as a starting point and then override
or rewrite sections from their own project-root `CLAUDE.md` (or skill
files) to match their organization's conventions, terminology, and slash
commands.

## Related

- [core-harness](https://github.com/suisya-systems/core-harness) — sibling
  repo with reusable safety primitives for Claude Code harnesses.
- [claude-org-ja#129](https://github.com/suisya-systems/claude-org-ja/issues/129)
  — tracking issue for the extraction effort that produced this runtime.
- [Issues](https://github.com/suisya-systems/claude-org-runtime/issues) for
  this repo.

## License

MIT — see [LICENSE](LICENSE).
