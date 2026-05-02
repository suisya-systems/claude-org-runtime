# claude-org-runtime

Python runtime extracted from [claude-org-ja](https://github.com/suisya-systems/claude-org-ja)
for Claude Code orchestrator setups using renga panes. Sibling of
[core-harness](https://github.com/suisya-systems/core-harness); provides a
dispatcher runner, state schema, and reference role prompts (secretary /
dispatcher / curator).

Not an AI agent framework — this is plumbing for human-driven Claude Code
orchestrator workflows, not an autonomous-agent toolkit.

## Status

Pre-0.1.0: schema + migrate. The `0.0.1` release established the repository
structure, packaging metadata, and CI scaffolding. The current `Unreleased`
work adds the `.state/` schema (Python Enums + Draft 2020-12 JSON Schema)
and the `v1->v2` polymorphic migrate (`python -m
claude_org_runtime.migrate.v1_to_v2`). The dispatcher and prompt-template
bundle land in subsequent releases.

## Install

Not yet on PyPI. To experiment locally:

```sh
git clone https://github.com/suisya-systems/claude-org-runtime
cd claude-org-runtime
python -m pip install -e .[dev]
```

PyPI publish (under the `claude-org-runtime` name) is **deferred until the
repo gating signal**; the first publish will go through the Trusted Publisher
workflow already wired up in `.github/workflows/release.yml`.

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
