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

See [`docs/cli.md`](docs/cli.md) for the full CLI reference and the
migration recipe for `claude-org-ja` consumers.

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
  — Phase 4 epic that motivated extracting this runtime.
- [Issues](https://github.com/suisya-systems/claude-org-runtime/issues) for
  this repo.

## License

MIT — see [LICENSE](LICENSE).
