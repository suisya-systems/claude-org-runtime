# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `claude_org_runtime.settings.generator`: Pattern A/B/C-aware sandbox
  selection on worker roles (Refs `claude-org-runtime#13`).
  - `worker_roles[<role>].sandbox_by_pattern: {A?, B?, C?}` declares
    one sandbox surface per dispatch pattern. The pattern keys are
    exactly `A` / `B` / `C` (matching the resolver / delegate-payload
    normalization in claude-org-ja). The generator picks
    `sandbox_by_pattern[--pattern]` and treats it as the role's
    sandbox; missing pattern keys are an authoring error rather than
    a silent fallthrough so Pattern B's distinct
    `additionalDirectories` / `base_clone` surface is never replaced
    by an A/C surface (Codex Blocker 1).
  - `sandbox` and `sandbox_by_pattern` are MUTUALLY EXCLUSIVE on
    worker roles; declaring both surfaces a `ValueError`. Org roles
    (`roles[<role>]`: secretary / dispatcher / curator) keep the
    single `sandbox` shape and may NOT declare `sandbox_by_pattern`
    (Codex Major 1).
  - `_VALID_ANCHORS` gains `base_clone` so Pattern B sandbox entries
    can reference Git metadata via
    `<base_clone>/.git/worktrees/<task_id>`,
    `<base_clone>/.git/objects`, etc. (Codex Blocker 3, contract:
    `docs/contracts/role-pattern-sandbox-contract.md` §4.2.1).
    `anchor='base_clone'` without a generator `base_clone` context
    surfaces a usable error message pointing at `--base-clone`.
  - CLI: `--pattern` is now `choices=('A','B','C')` so typos like
    `--pattern b` fail fast instead of falling through silently
    (Codex Nit 1).
  - Pattern B's *command-isolation* guardrails (`Bash(git worktree *)`
    deny + `block-dangerous-git.sh`) are intentionally NOT modeled in
    `sandbox_by_pattern`. The runtime sandbox layer is path-isolation
    only; command isolation lives in the per-role `permissions.deny`
    / `.hooks` (handled by the paired claude-org-ja Phase 1 PR4 --
    Codex Major 3).

### Notes

- Backward-compatible: roles using the legacy single `sandbox` shape
  render unchanged, and `--pattern` stays informational on those
  roles.
- Pattern C sub-modes (ephemeral vs gitignored_repo_root) are out of
  scope for this PR; `sandbox_by_pattern.C` captures only the
  surface common to both sub-modes.
- This release does not ship concrete
  `worker_roles[*].sandbox_by_pattern` bodies. The paired
  claude-org-ja Phase 1 PR4 lands the concrete bodies, plumbs
  `--pattern` / `--base-clone` / `--task-id` / `--branch-ref` through
  `tools/resolve_worker_layout.py` / `tools/gen_delegate_payload.py`,
  and updates `tools/check_runtime_schema_drift.py` to render A/B/C
  fixtures. Until that PR lands, the runtime CLI exposes the surface
  but the standard dispatch path does not yet exercise it.

## [0.1.6] - 2026-05-10

### Added

- `claude_org_runtime.settings.generator`: Phase 1 sandbox schema +
  generator extension (Refs `claude-org-ja#378`, `claude-org-ja#376`).
  - Structured anchor entry shape on
    `sandbox.filesystem.denyRead` / `denyWrite`. Each entry may now be
    either a legacy raw string (anchored at `worker_dir` for relative
    paths, treated literally for absolute paths) or a structured object
    `{anchor: 'home'|'worker_dir'|'claude_org_path'|'absolute', path:
    string, suppressOnSymlinkEscape: bool, default true}`. The
    structured form fixes the prior ambiguity where home-anchor
    entries (`~/.aws/**`) were misjudged as `worker_dir`-relative.
    Existing string entries continue to parse via the legacy adapter
    (`_normalize_sandbox_entry`) so no consumer migration is required.
  - `render_role_with_metadata(..., role_kind='org'|'worker')`: callers
    can now render the org-side roles (`schema['roles'][...]`) in
    addition to the worker-side templates (`schema['worker_roles'][...]`).
    Default is `'worker'` for backward compatibility.
  - Pattern B context parameters (`base_clone`, `task_id`, `branch_ref`,
    `pattern`) on both `render_role` and `render_role_with_metadata`.
    Their `{...}` placeholders are substituted alongside `{worker_dir}`
    / `{claude_org_path}` in entry paths and `additionalDirectories`
    before realpath evaluation.
  - Per-entry `suppressOnSymlinkEscape: false` opt-out: a structured
    entry with this flag is preserved in the rendered output even when
    its realpath escapes the sandbox read roots (e.g. for entries the
    operator wants surfaced for the launcher regardless of
    reachability).
  - `GeneratorContext` dataclass and `_VALID_ANCHORS` constant exported
    as the canonical generator inputs.
  - CLI: `claude-org-runtime settings generate` and `settings show`
    now expose `--role-kind {worker,org}`, `--base-clone`, `--task-id`,
    `--branch-ref`, and `--pattern` so the new generator surface is
    reachable from the public command-line entry point as well.
    `settings generate --role-kind org` is rejected (org
    `settings.local.json` files are hand-maintained); use `settings
    show --role-kind org` for inspection.
- `docs/cli.md`: documented the new `--role-kind` / `--base-clone` /
  `--task-id` / `--branch-ref` / `--pattern` flags on both `settings
  generate` and `settings show`, plus the org-rejection behavior.
- `claude_org_runtime.settings.role_configs_schema.json`: documented
  the new structured anchor form via
  `worker_roles.$comment_sandbox_anchor` and added
  `roles.$comment_roles_sandbox` permitting the same `sandbox` shape on
  org-side roles (secretary / dispatcher / curator).

### Notes

- The matching `claude-org-ja`-side schema surface, drift CI extension,
  and pin bump are tracked separately as a follow-up after this
  runtime release lands.
- Concrete sandbox bodies for `roles.secretary` / `roles.dispatcher` /
  `roles.curator` are deliberately NOT populated in this PR. Phase 0
  contract (`docs/contracts/role-pattern-sandbox-contract.md` on the
  `claude-org-ja` side) is the SoT for which entries each org role
  declares; this PR is limited to the structural extension (schema +
  generator + CLI). The matching ja-side follow-up PR populates the
  bodies driven by that contract.

## [0.1.5] - 2026-05-10

### Changed

- `claude_org_runtime/settings/role_configs_schema.json`: add
  `Write(*/.worktrees/*/.claude/settings.local.json)` and
  `Edit(*/.worktrees/*/.claude/settings.local.json)` to the secretary
  role's `required_deny`. Extends Secretary's `permissions.deny`
  coverage to the `live_repo_worktree` (Pattern B) sub-mode where the
  worktree lives under `{claude_org_path}/.worktrees/...` rather than
  `{workers_dir}/{project_slug}/.worktrees/...`. The existing
  `*/workers/*/.worktrees/*/.claude/settings.local.json` pattern only
  covered worker-side worktrees; this adds a sibling glob (no
  role-specific gating in the pattern itself, mirroring how the
  existing entry is expressed) so the `claude-org-ja`-side org
  extension schema can pin a runtime release that already carries
  the matching deny coverage. Refs `claude-org-ja#300`,
  `claude-org-ja#289`.

## [0.1.4] - 2026-05-09

### Added

- `claude_org_runtime.settings.generator`: Phase 3 sandbox bootstrap
  policy MVP (case E only, refs `claude-org-ja#392`, `claude-org-ja#376`).
  - `worker_roles.<role>.sandbox` is a new optional object with shape
    `{enabled: bool, filesystem: {denyRead, denyWrite,
    additionalDirectories}, failIfUnavailable: bool}`. Documented via
    the new `worker_roles.$comment_sandbox` schema annotation. Existing
    roles without `sandbox` are unchanged (backward compatible — absent
    `sandbox` is treated as sandbox-disabled).
  - `render_role()` (and a new `render_role_with_metadata()` that
    returns a `RenderResult` carrying the suppression report) now apply
    Layer 3 suppression: each `sandbox.filesystem.denyRead` /
    `denyWrite` entry whose realpath escapes the sandbox read roots
    (`worker_dir` + `additionalDirectories`) is dropped from the
    rendered sandbox object. This handles the WSL case (`/home/<u>/...`
    that resolves into `/mnt/c/...`) and devcontainer case
    (`/workspaces` symlinks) without hard-coding `/mnt/c`. Layer 2
    `permissions.deny Read(...) / Write(...)` entries are NEVER
    suppressed.
  - Annotation-only WSL detection (`/proc/version`,
    `/proc/sys/kernel/osrelease` → `microsoft-standard-WSL`) recorded
    in suppression metadata for telemetry; the actual suppression
    decision is keyed on realpath escape.
- `claude-org-runtime settings show [--explain] [--json]`: new CLI
  surface that drives the same renderer as `settings generate` (single
  source of truth) and surfaces the rendered settings + sandbox
  suppression metadata. With `--explain`, the output includes
  `wsl_detected`, the resolved `sandbox_read_roots`, and the per-entry
  suppression list (`layer`, `entry`, `reason`, `realpath`).

### Deferred (per `tmp/codex-review-phase3-impl-392.md`)

- Case A bootstrap fallback (`bootstrap.py`, bwrap stderr parser):
  runtime does not control the bwrap launcher, so the helper would be
  dead code. Tracked for a follow-up after the launcher contract
  stabilizes.
- `failIfUnavailable` redefinition: kept the field in the schema but
  semantics are unchanged from prior usage.
- `sandbox_deny_skipped` journal events: requires the
  `claude-org-ja` `journal_append` contract, deferred to a separate PR.
- `profile-tightened.json` `$comment` updates and
  `docs/verification.md` reconciliation are `claude-org-ja`-side
  follow-ups for after the runtime release lands.

## [0.1.3] - 2026-05-09

### Changed

- `claude_org_runtime/settings/role_configs_schema.json`: sync 5 Read deny
  entries from claude-org-ja `feat/phase2-read-deny-gap-rows` (commit
  `68f502e`). Both `worker_roles.default.permissions.deny` and
  `worker_roles.claude-org-self-edit.permissions.deny` now include:
  `Read(.env)`, `Read(.env.*)`, `Read(**/credentials*)`, `Read(**/*.pem)`,
  `Read(~/.config/gh/hosts.yml)`. Closes the Phase 2 Read-tool gap rows
  surfaced by the sandbox-probe iter-c §4.3 #2 audit (Layer 2 perms.deny
  had no Read entries; Layer 3 sandbox.denyRead is Bash-tool-only).
  Refs `claude-org-ja#376`.

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
