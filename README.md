# claude-org-runtime

Python runtime extracted from [claude-org-ja](https://github.com/suisya-systems/claude-org-ja)
for Claude Code orchestrator setups using renga panes. Sibling of
[core-harness](https://github.com/suisya-systems/core-harness); provides a
dispatcher runner, state schema, and reference role prompts (secretary /
dispatcher / curator).

Not an AI agent framework — this is plumbing for human-driven Claude Code
orchestrator workflows, not an autonomous-agent toolkit.

## Status

Pre-0.1.0 skeleton. There is **no public API yet**: this `0.0.1` release
establishes the repository structure, packaging metadata, and CI scaffolding.
The dispatcher, state schema, and prompt-template bundle land in subsequent
releases.

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

## Related

- [core-harness](https://github.com/suisya-systems/core-harness) — sibling
  repo with reusable safety primitives for Claude Code harnesses.
- [claude-org-ja#129](https://github.com/suisya-systems/claude-org-ja/issues/129)
  — Phase 4 epic that motivated extracting this runtime.
- [Issues](https://github.com/suisya-systems/claude-org-runtime/issues) for
  this repo.

## License

MIT — see [LICENSE](LICENSE).
