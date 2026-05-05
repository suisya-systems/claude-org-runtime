# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.2] - 2026-05-06

### Changed

- `dispatcher.runner.choose_split`: align balanced-split target selection
  with claude-org-ja PR #310 (`org-delegate` Step 3-1b /
  `references/pane-layout.md`).
  - `SECRETARY_MIN_WIDTH` 125 → 140.
  - `SECRETARY_MIN_HEIGHT` 45 → 30.
  - `curator` is now a valid split target (previously skipped).
  - Sort regime changed from `(metric desc, id asc)` to
    `(role priority desc, metric desc, id asc)` with priority
    `secretary=4 > curator=3 > worker=2 > dispatcher=1`.
  - Dispatcher's curator-rect adjacency requirement is unchanged.
  - `SplitChoice` gains a `role` field (defaulted to `""`) so the new
    sort key can read it; existing positional construction is not
    affected.
- Regression scenario covered: 280×86 terminal with secretary 280×43
  now correctly selects secretary for the next split (previously the
  secretary was never splittable under the old thresholds).

## [0.1.1] - 2026-05-03

### Changed

- Maintenance release: trigger first PyPI publish via Trusted Publisher
  (registered post-0.1.0). No code changes from 0.1.0.

## [0.1.0] - 2026-05-02

First release with a public CLI surface. Marks the completion of
Phase 4's Layer 2 extraction from `claude-org-ja` (refs
`claude-org-ja#129`): the in-tree `tools/dispatcher_runner.py`,
`tools/generate_worker_settings.py`, and `tools/role_configs_schema.json`
can now be replaced by `pip install claude-org-runtime` without
behavioural regression.

### Added

- `claude_org_runtime.dispatcher.runner` (Step D-1): port of
  `tools/dispatcher_runner.py`. Public API: `Pane`, `SplitChoice`,
  `ActionPlan`, `LocaleConfig`, `choose_split`, `build_plan`,
  `validate_task_id`, `validate_cwd`, `validate_instruction_vars`,
  `render_instruction`, `write_instruction`. CLI: `python -m
  claude_org_runtime.dispatcher.runner delegate-plan`. New
  `--template-repo` flag lets callers point the helper at the repo
  hosting `.claude/skills/org-delegate/references/instruction-template.md`;
  default resolution tries the runtime package's ancestors first, then
  walks up from CWD. New `--locale-json PATH` flag lets non-English
  consumers (notably `claude-org-ja`) override the runtime's English
  defaults via a `LocaleConfig` JSON file (`constraints_default`,
  `report_target_default`, `claude_md_filename_default`,
  `instruction_template`).
- `claude_org_runtime.settings.generator` (Step D-1): port of
  `tools/generate_worker_settings.py`. Public API: `load_schema`,
  `render_role`. CLI:
  `python -m claude_org_runtime.settings.generator`. The bundled
  schema is the new SoT.
- `claude_org_runtime.settings.role_configs_schema.json` (Step D-1):
  bundled copy of `tools/role_configs_schema.json` (SoT moved into the
  runtime package).
- `claude-org-runtime` console entry point with `dispatcher` /
  `settings` subcommand groups (e.g.
  `claude-org-runtime dispatcher delegate-plan ...`).
- `docs/cli.md`: CLI usage reference and migration recipe for
  `claude-org-ja` consumers replacing the in-tree `tools/` scripts.
- `claude_org_runtime.prompts` package: bundled English reference prompts
  for the `secretary`, `dispatcher`, and `curator` roles, plus
  `load(role)` / `load_meta(role)` / `available_roles()` (stdlib-only
  frontmatter parser). The templates are reference, not prescriptive —
  consumers override or adapt them from their own `CLAUDE.md`. Refs
  `claude-org-ja#129`.
- `tests/scrub/scrub_fixture.py`: deterministic scrubber for `.state/`
  snapshots (URLs, emails, API keys, session-narrative H2 blocks,
  long worker `note` fields). Preserves structural identifiers
  (`task_id`, `event`, `ts`, `pane_id`, `pane_name`, `status`, `state`).
- `docs/scrub-policy.md`: policy and operational procedure for
  promoting `claude-org-ja` `.state/` snapshots into fixtures.
- `tests/fixtures/synthetic/{scrub_input_sample,expected_output}.jsonl`:
  synthetic round-trip fixture exercising every scrubber class.
- Refs `claude-org-ja#208`.
- `claude_org_runtime.schema` package: `WorkerStatus`, `JournalEventType`,
  `AnomalyKind` (string-mixin Enums), frozen `JournalEvent` dataclass with
  `from_dict`/`to_dict` and an `extra` forward-compatibility bucket, and a
  `parse_worker_directory_registry` parser for `org-state.md` rows.
- Bundled JSON Schema (Draft 2020-12) files for `JournalEvent` and
  `WorkerDirEntry` under `claude_org_runtime.schema.json_schema`.
- `claude_org_runtime.migrate.v1_to_v2` polymorphic migrate (CLI:
  `python -m claude_org_runtime.migrate.v1_to_v2 --in IN --out OUT`):
  legacy keys (`worker`, `pane`, `dir`) are kept alongside canonical keys
  (`task_id`, `pane_id`/`pane_name`, `worker_dir`); unknown event names
  fall back to `event=misc` with `original_event` preserved.
- Synthetic fixtures and tests covering schema validation and the v1->v2
  migrate round-trip.
- Refs `claude-org-ja#129`.

### Changed

- Added `jsonschema>=4.18` as a runtime dependency (sole non-stdlib dep).
- Bumped package classifier from `Development Status :: 1 - Planning` to
  `Development Status :: 4 - Beta`.

## [0.0.1] - 2026-05-02

Initial skeleton (no public API).

- Package metadata in `pyproject.toml` (name `claude-org-runtime`, MIT, py>=3.10).
- `src/claude_org_runtime` package with version SoT in `__about__.py`.
- Smoke test asserting the exposed `__version__`.
- Pytest matrix CI (`.github/workflows/test.yml`) on ubuntu/macos/windows × py3.10–3.12.
- Trusted Publisher release skeleton (`.github/workflows/release.yml`), tag-triggered only.
- README, LICENSE, and `.gitignore`.
