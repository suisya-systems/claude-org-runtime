# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- `broker`: `spawn_claude_pane` / `spawn_codex_pane` / `spawn_pane` now
  resolve a **relative** `cwd` against the **caller pane's** cwd before
  handing it to the terminal adapter, matching the documented renga
  contract (absolute paths used as-is; relative resolved against the
  caller pane's cwd). Previously the relative path was passed straight to
  the adapter, where `tmux new-session -c` re-resolved it against the
  daemon/server base — in the `#515` broker dogfood this dropped the
  `dogfood/` segment and landed the dispatcher in the wrong tree. The
  caller's cwd is the broker bind's `cwd`, and the **resolved absolute**
  cwd is what is now stored in the pane registry, so a child's own
  relative spawns anchor correctly down the tree. When a relative `cwd` is
  requested but the caller's cwd is unknown (e.g. a logical root pane
  registered without a cwd), the spawn is **rejected deterministically**
  (`[cwd_unanchored]`, invalid-params) rather than silently resolved
  against the daemon's base. Absolute-path detection accepts both POSIX
  and native-absolute forms so canonical POSIX paths (`/repo`) are honored
  as absolute regardless of the daemon platform. Closes
  `claude-org-runtime#61`.

### Added

- `broker serve --root-cwd <dir>` (default: the daemon's launch directory,
  `os.getcwd()`): gives the manually-launched root pane (the human-driven
  secretary) a cwd in its bind, so relative-`cwd` spawns from that pane
  have a deterministic resolution anchor. The documented operating
  contract is that the daemon is launched from the session root; pass
  `--root-cwd` explicitly when launching from elsewhere. This is the
  root-cause companion to the `#61` fix: the dogfood secretary's bind cwd
  was `null`, leaving relative spawns with no anchor.

## [0.1.19] - 2026-06-12

### Added

- `broker`: the terminal adapter now advertises an `isolated_session`
  capability flag, exposing whether the backend spawns panes in an
  isolated session so callers can branch on session-isolation support
  instead of assuming it.

### Fixed

- `broker`: the root secretary is now registered as a logical pane in the
  broker registry. Previously the root agent had a bind but no pane
  registry entry, so it never appeared in `list_panes`, and `close_pane`
  mis-counted the live panes and could trip the last-pane guard. Closes
  `claude-org-runtime#57` (PR #58).

## [0.1.18] - 2026-06-12

### Added

- `broker serve --root-role {worker,curator,dispatcher,secretary}`
  (default `worker`): the manual-verification token issued by
  `broker serve` was previously pinned to the worker tier (`issue_token`
  hard-coded `"worker"`), so there was no CLI path to bind the root agent
  at the secretary (or any other) tier. The new flag flows into the issued
  token's `auth_role`, so `tools/list` is structurally narrowed to that
  tier's surface. `default=worker` keeps the current behavior unchanged
  (messaging 4 面). Token issuance is extracted into `issue_root_token()`
  so the `--root-role → auth_role → 公開面` boundary is unit-testable
  independently of the blocking `serve` loop. The `--mcp-config` display
  and the billing-neutral `spawn` guard are unchanged. Closes
  `claude-org-runtime#53` (PR #54).

## [0.1.17] - 2026-06-11

### Added

- `transport`: new subpackage holding the **transport surface descriptor**
  (ja-migration-plan §5.2 (i) / §5.3 / §3.1) — the single SoT mapping a
  transport `flag` (`renga` | `broker`) to its concrete wiring:
  `{server 名, spawn 注入 flag, role tier -> MCP tool 名集合}`. Additive,
  flag-aware API consumed (via pin) by both the runtime `settings/generator`
  and the ja-side generators (`tools/gen_delegate_payload.py` / worker_brief),
  so the transport prefix / tool set lives in one place instead of being
  hardcoded per generator (drift 防止).
  - `renga`: server `renga-peers`, injection
    `--dangerously-load-development-channels server:renga-peers`, **全ロール
    一様の required 14 面** (`tools/check_renga_compat.py` REQUIRED_MCP_TOOLS /
    renga 0.18.0 と一致; renga には構造的 tier gating が無いため一様)。
  - `broker`: server `org-broker`, injection `--mcp-config <broker>`,
    **role tier 別** (secretary 13 / dispatcher 12 / worker・curator 4)。
    tier 別集合は `claude_org_runtime.broker.surface` の `tools_for` から
    導出 (ハードコード二重管理を避ける — drift lock test 付き)。
  - Public surface: `get_surface` / `resolve_transport` (explicit >
    `ORG_TRANSPORT` env > default `renga`) / `tools_for_role` /
    `allow_entries_for_role` / `TransportSurface`.
- `settings.generator.transport_allowlist(role, *, transport=None, env=None)`:
  descriptor-driven, flag-aware MCP allowlist projection. With the default
  `renga` flag the emitted `mcp__renga-peers__*` entries are **bit-equivalent
  with the current shared surface** (§5.3 non-breaking guarantee, regression
  test included); `ORG_TRANSPORT=broker` yields the tier-appropriate
  `mcp__org-broker__*` set. The transport is read from `ORG_TRANSPORT`
  (env-only flag, §5.1 — no persisted config file so no Set C amendment).

### Notes

- Default transport stays `renga` (`ORG_TRANSPORT` unset ⇒ current behavior
  unchanged). The broker default-flip (§8 Issue G) is a post-dogfood human
  decision and is **not** made here.
- Scope is runtime-only: ja-side wiring (pin bump / `gen_delegate_payload.py`
  / worker_brief / golden) is ja#513 and prose/contract revisions are ja#514;
  release publish (git tag / PyPI) + paired ja sync are coordinated by the
  desk and intentionally **not** performed in this change.

## [0.1.16] - 2026-06-10

### Added

- `broker`: new subpackage porting the `claude-org-transport-lab`
  `spike/broker.py` org-broker (Phase 4/5 で確定した MCP surface +
  allowlist guard + session 検証) into `claude_org_runtime/broker/`,
  split into four responsibilities — `surface` (MCP 面: PROTOCOL_VERSIONS /
  SERVER_INFO / TOOLS / ToolArgError / `dispatch_tool`), `tokens`
  (`AgentBind` + `TokenMixin`), `store` (`StoreMixin`: queue 永続化 +
  JSONL journal), and `server` (`Broker` orchestrator: localhost HTTP MCP
  server + nudge delivery + `_McpHandler`). The single-lock concurrency
  contract (nudge double-injection check-and-set / DELETE deadlock
  avoidance) is carried over unchanged. Nudge delivery lives in `server`,
  not `store`, so queue persistence and PTY injection stay decoupled.
- `broker`: queue journal is now written to `.state/broker/queue.jsonl`
  (CWD-relative default; the spike wrote to its self-contained
  `spike/broker-state/`).
- `broker serve`: new daemon CLI entry, exposed both as
  `claude-org-runtime broker serve ...` and
  `python -m claude_org_runtime.broker`.
- `broker.placement`: thin one-way reuse of
  `dispatcher.runner.choose_split` (`list_panes(dict) -> Pane.from_dict ->
  choose_split`) for balanced-split placement. Pure-function wrapper only;
  it is intentionally NOT wired into spawn (the terminal adapter exposes no
  split-target surface — that deeper integration is tracked separately).
  Dependency direction is one-way: `broker -> terminal` / `choose_split`;
  `claude-org-ja` does not import `broker` (flag-gated, inactive by default
  under renga).
- `schema.broker_queue_event_schema()`: Contract Set C amendment for
  `.state/broker/` — a bundled JSON Schema (Draft 2020-12) for a
  `queue.jsonl` line. `ts` is a float epoch (`time.time()`), distinct from
  `journal_event`'s ISO8601 string timestamp.
- `broker`: pane-control MCP surface brought to the renga-peers **golden
  shape** (Issue C / Epic #6 next stage C — drop-in form-difference-zero).
  The catalogue grows from the 4 messaging tools to the 13-tool golden shape:
  the 12 ported faces (`send_message` / `check_messages` / `list_peers` /
  `set_summary` / `list_panes` / `inspect_pane` / `send_keys` / `poll_events`
  / `close_pane` / `set_pane_identity` / `spawn_claude_pane` / `spawn_pane`)
  **plus the newly added `spawn_codex_pane`**. `new_tab` / `focus_pane` are
  intentionally excluded from the initial surface (human-judgment).
- `broker`: structured `spawn_claude_pane` / `spawn_codex_pane` builders
  assemble the interactive-TUI argv inside the broker (Claude gets the broker
  MCP injected via `--mcp-config <token>` instead of renga's dev-channel
  flag). The billing-neutral guard is now a **default-deny allowlist on the
  broker's own builder output** (not caller-argv inspection), closing the
  false-reject surface: value-flags carry arity, `argv[0]` is matched by
  basename, and subcommands / bare positionals / `--` / unknown or headless
  flags are rejected. `spawn_codex_pane` structurally restricts to the
  interactive TUI — `exec` / `review` / `mcp-server` / `app-server` /
  `exec-server` / `apply` / `sandbox` / `completion` and any other
  subcommand are default-denied (mandatory test coverage).
- `broker`: pane addressing resolves three ways (`Broker.resolve_target`) —
  all-digit string → handle, non-digit string → stable name, `'focused'` →
  focused pane — matching renga's addressing.
- `broker`: `list_peers` / `list_panes` now carry **cwd** (kept in the broker
  bind / pane registry at spawn time, since `tmux capture-pane` does not
  expose it). `receive_mode` is the constant `"pull"` (broker delivery is
  uniformly pull via `check_messages`; a Set D amendment vs renga's
  push/poll distinction) and `kind` reflects the spawned client
  (`"claude"` / `"codex"` / `null`).
- `broker`: `set_pane_identity` gains renga three-state semantics
  (omit = keep / `null` = clear / string = set) for `name` / `role`. The
  display `role` is decoupled from an immutable `auth_role`: **tier gating
  (§4.2) is decided by `auth_role` only**, so renaming a pane's display role
  cannot escalate its privileges (Issue B codex Blocker carried forward as an
  intentional security strengthening).
- `broker`: role-scoped tool exposure (`tools/list` and dispatch are filtered
  by `auth_role`) — worker / curator see messaging only; dispatcher adds the
  pane-control tools; secretary additionally gets the generic `spawn_pane`
  (attention-watcher launch). Reaching a tool outside one's tier is rejected
  structurally, not by permission config.

### Changed

- `broker`: `list_peers` output gains `cwd` / `kind` / `receive_mode` fields
  (additive; existing `id` / `name` / `role` / `summary` unchanged).

### Known limitations

- `broker`: this stage establishes the **surface shape** (catalogue + schemas
  + builders + guards + target resolution + cwd parity + three-state
  identity); the terminal adapter's native capabilities are out of scope and
  tracked for Epic #6 next stage (#4, full backend adapter). Concretely:
  directional split is accepted for shape parity but `adapter.spawn` opens a
  new window/session (no in-place split); `send_keys` validates the full
  renga key vocabulary (unknown names → `-32602`) but only emits the keys the
  current adapter supports (literal text / Enter / Ctrl+C) — other valid keys
  (e.g. Shift+Tab) return `[key_unsupported]`; `poll_events` is served from a
  broker-internal lifecycle ring (spawn / close) rather than native backend
  events; and `spawn_codex_pane` does not yet inject the broker MCP into Codex
  (renga relies on a `RENGA_PEER_CLIENT_KIND` env that `adapter.spawn` cannot
  set today). `claude-org-ja` is untouched (flag-gated, inactive under renga).

## [0.1.14] - 2026-06-09

### Changed

- `dispatcher.choose_split`: `_ROLE_PRIORITY` を反転し、**dispatcher を最優先
  分割ターゲット (priority=4)、secretary を最低優先 (priority=1)** に変更。
  worker は dispatcher ペインを垂直分割して spawn されるようになり (curator=3 >
  worker=2 は従来の相対順を維持)、secretary の content viewport は last-resort
  までは分割されない。
- dispatcher の viewport 保護ロジックを再構成。従来は「last-resort の dispatcher
  を curator priority へ昇格させる freed-curator-zone reclaim」だったが、
  dispatcher が常時最優先になったため、垂直分割で残る左 child が
  `DISPATCHER_MIN_WIDTH`(=80) 以上の間だけ最優先を保ち、それを下回ると新定数
  `_DISPATCHER_NARROW_PRIORITY`(=0、全ロール未満) へ降格して active 監視ペインが
  繰り返し半減されないよう self-limit する方式に変更。dispatcher の
  adjacency gate (resident curator 非隣接時のスキップ) は不変。

## [0.1.13] - 2026-06-09

### Fixed

- `dispatcher.choose_split`: curator のオンデマンド化 (claude-org-ja #503)
  で dispatcher が吸収した「旧 curator スロット」の空きスペースが balanced
  split のターゲット選定で考慮されず、bottom zone が無駄になっていた問題を
  修正。curator 不在かつ dispatcher の垂直分割で残る左 child が新定数
  `DISPATCHER_MIN_WIDTH`(=80) 以上のとき、その垂直分割を curator の
  role-priority スロットに昇格させ、下位 priority の worker を半減させる前に
  空きスペースを worker zone として埋める。`DISPATCHER_MIN_WIDTH` フロアが
  self-limit として働き、dispatcher が快適幅まで縮んだ後は last-resort に
  戻るため active 監視ペインの viewport が繰り返し半減されることはない。
  既存の #35/#36 挙動 (curator 不在時の last-resort 候補化・両方向評価・
  resident-curator レイアウト) は不変。

## [0.1.12] - 2026-06-09

### Fixed

- `dispatcher.choose_split`: curator オンデマンド化後に通常サイズの端末で
  `None` (`SPLIT_CAPACITY_EXCEEDED`) を返していた問題を修正。curator 不在時に
  dispatcher を last-resort 候補に含め、両方向を min-size fallback 付きで
  評価、`SECRETARY_MIN_WIDTH` を 140→120 に。Closes `claude-org-runtime#35`
  (PR #36).

## [0.1.11] - 2026-05-13

### Added

- `attention.classifier`: new `notify_sent.kind = "awaiting_user"`
  subkind maps to attention kind `secretary_awaiting_user` at default
  `urgent` severity. Designed to fire when the secretary is waiting on
  user input at the 3 canonical gates — worker completion approval,
  CI-green merge approval, and escalation reply forward. Refs
  `claude-org-runtime#28` (PR #30).
- `attention.platform`: WSL attention backend now invokes
  `wsl-notify-send.exe` when the binary is on `PATH`, producing real
  Windows toast notifications instead of the previous
  `Write-Host` no-op. Original `wsl` PowerShell backend retained
  bit-for-bit as a fallback when the binary is absent. Beep dispatched
  as a separate `powershell.exe` call so toast delivery is independent
  of sound playback. Closes `claude-org-runtime#25` (PR #27).
- `attention.config`: `pending_decisions` TTL ladder. Two new knobs —
  `pending_decision_max` (default 1440 minutes / 24h, urgent → normal
  demote) and `pending_decision_drop` (default 10080 minutes / 7d,
  suppress to `--json-only`) — applied symmetrically to both
  `pending_decision` and `user_reply_not_forwarded` attention kinds.
  `AttentionEvent.suppressed` flag added so callers can distinguish
  the drop-to-json-only tier from active dispatch. Closes
  `claude-org-runtime#26` (PR #29).

### Changed

- `attention.classifier` `DEFAULT_NOTIFY` severity rebalance: 6
  anomaly subkinds demoted from `urgent` to `normal` —
  `relay_gap_suspected`, `silent_worker_output`, `pane_silent`,
  `worker_stalled`, `worker_not_reported`, `worker_error`. `urgent`
  is now reserved for action-required moments only:
  `approval_blocked`, `pending_decision`,
  `user_reply_not_forwarded`, `ci_failed`, `pane_crashed`. Closes
  `claude-org-runtime#26` (PR #29).

## [0.1.10] - 2026-05-13

### Added

- `claude_org_runtime.attention`: new top-level package implementing the
  attention / notification watcher per `claude-org-ja`
  `docs/design/attention-notification.md` §5 / §6 (merged ja PR #443,
  2026-05-12). Closes `claude-org-runtime#19` and `claude-org-runtime#20`.
  - New CLI subcommand family mounted on the top-level
    `claude-org-runtime` entry point:
    - `claude-org-runtime attention scan --state-dir DIR [--dry-run]
      [--json]` — one-shot pass over `state.db` events + pending
      decisions, classifies and (unless `--dry-run`) dispatches a
      notification per anomaly, dedup'd against prior runs.
    - `claude-org-runtime attention watch --state-dir DIR
      [--config PATH]` — long-running poll loop with backend probing
      and config-driven template / severity overrides.
  - `attention.classifier`: pure events / pending → `AttentionEvent`
    mapping. Covers the 3 design-doc subkinds plus the production
    `notify_sent.kind` vocabulary: every `schema.AnomalyKind` enum
    (`pane_silent` / `pane_crashed` / `worker_stalled` /
    `worker_not_reported`) and the dispatcher prompt's freeform
    `error` tag (`prompts/templates/dispatcher.md` line 410). All
    map to urgent attention with bundled English titles that templates
    may override.
  - `attention.config`: `AttentionConfig` dataclass + JSON loader
    (`load_config`). Operators may override per-`kind` severity via a
    `notify` map (e.g. `{"worker_completed": "urgent"}`) which now
    reaches the emitted `AttentionEvent.severity` — the classifier
    accepts a `notify_map` parameter instead of hard-coding severity.
  - `attention.notify`: template render + truncation + subprocess
    dispatch. `_placeholders` enforces a flat identifier allowlist —
    attribute / index forms like `{summary[0]}` or
    `{summary.__class__}` are rejected before reaching `format_map`,
    so templates cannot reach into arbitrary `AttentionEvent`
    internals. `_strip_control` also drops DEL (0x7f) per its
    docstring intent. `max_title` / `max_body` truncation is applied
    post-render.
  - `attention.platform`: macOS / Linux / Windows / WSL / stdout
    backend probing. Windows / WSL PowerShell commands gate the
    `[console]::beep(800,200)` invocation on `play_sound=True` so
    `sound="off"` actually silences the watcher on those platforms;
    when both sound and the visible PowerShell host stream are
    suppressed the dispatch downgrades to intentional stdout-only
    delivery (`desktop_intended=False`) so `reached_user` stays
    honest. macOS / Linux paths now also ring the terminal bell on
    successful `osascript` / `notify-send` delivery so the §5 urgent
    sound row actually fires (visual-only delivery was the previous
    behaviour).
  - `attention.dedup`: atomic JSON state with corruption recovery —
    a malformed dedup file is treated as empty rather than crashing
    the watcher.
  - `attention.readers`: `state.db` (sqlite3, `events` table) and
    `pending_decisions.json` readers. Both tolerate corruption:
    `read_events` traps `sqlite3.Error` so a non-SQLite / corrupt
    `state.db` does not crash a long-running watch loop, matching
    the pending-decisions reader posture. `_minutes_since` returns
    `+inf` for missing or malformed ISO timestamps so the pending
    classifier alerts on a corrupt `received_at` / `user_replied_at`
    instead of silently treating the entry as "0 minutes old" —
    false-positive is the right error direction for a relay-gap
    watcher.
  - Dedup contract: an event is only marked delivered when something
    actually reached the user. Desktop subprocess success OR bell
    fallback OR explicit stdout-only / desktop-disabled mode all
    count; a silently-failing `notify-send` (non-zero returncode)
    retries on the next poll instead of being dedup'd into oblivion.
    `_dispatch_desktop` runs subprocesses with `check=False` and
    inspects the returncode itself.
  - `attention scan --json` payload reports the rendered title /
    body from `FormattedNotification` (post-template, post-truncation)
    plus a `delivered` boolean mirroring `reached_user`. Machine
    consumers (notably the planned `claude-org-ja#445` golden test)
    can now tell a classified event from one that actually reached
    the user without re-implementing the dispatch contract.
  - `attention` CLI wraps `load_config` in `_load_cfg_or_exit` so a
    malformed config JSON produces a one-line error + exit code 2
    instead of a Python traceback.

### Notes

- Tests under `tests/attention/` cover every §5 / §6 acceptance
  criterion — backend selection across all 5 platforms (macOS / Linux
  / Windows / WSL / stdout), dry-run subprocess suppression, dedup
  recovery from broken JSON, template unknown-placeholder fallback,
  `max_title` / `max_body` truncation, the dedup-retry contract on
  desktop-dispatch failure, PowerShell beep gating, malformed-config
  CLI error path, missing / malformed ISO timestamp handling, and a
  Japanese template smoke test (109 attention tests, 292 tests
  total).
- Tag-triggered release workflow at `.github/workflows/release.yml`
  builds sdist + wheel and publishes to PyPI via OIDC Trusted
  Publisher, then attaches artifacts to the GitHub Release. PyPI
  publication is out of worker scope; the `v0.1.10` tag push
  triggers it.

## [0.1.9] - 2026-05-11

### Changed

- `claude_org_runtime/settings/role_configs_schema.json`: schema mirror
  sync from `claude-org-ja` Phase 2 worker git guardrails
  (Refs `claude-org-ja#379`, paired with ja PR #420
  `feat/phase2-worker-git-guardrails-impl`). Brings the runtime-bundled
  schema back into byte-equivalence with ja's
  `tools/org_extension_schema.json` so
  `tools/check_runtime_schema_drift.py` passes inside ja's pin window.
  - `roles.worker`:
    - `required_allow`: drop `Bash(git worktree:*)` (worktree creation
      now denied at the worker layer).
    - `required_deny`: add the dangerous-git family — `Bash(git worktree)`
      / `Bash(git worktree *)`, `Bash(git fetch)` / `Bash(git fetch *)`,
      `Bash(git pull)` / `Bash(git pull *)`, `Bash(git submodule)` /
      `Bash(git submodule *)`, `Bash(git lfs)` / `Bash(git lfs *)`,
      `Bash(git gc)` / `Bash(git gc *)`,
      `Bash(git filter-branch)` / `Bash(git filter-branch *)`,
      `Bash(git filter-repo)` / `Bash(git filter-repo *)`,
      `Bash(git replace)` / `Bash(git replace *)`,
      `Bash(git update-ref)` / `Bash(git update-ref *)`,
      `Bash(git config --global *)`, `Bash(git config --local *)`,
      `Bash(git config --worktree *)`.
    - `required_hooks`: attach `block-dangerous-git.sh` and
      `block-no-verify.sh` on the `Bash` matcher (alongside the
      existing `block-git-push.sh` / `block-org-structure.sh`).
    - `disallow_allow_regex`: add `^Bash\(git worktree.*\)$`.
  - `worker_roles.default` and `worker_roles.claude-org-self-edit`:
    - `permissions.allow`: drop `Bash(git worktree:*)`.
    - `permissions.deny`: add the same dangerous-git family as
      `roles.worker` plus the `git -C <dir>` variants, the `git remote
      add|set-url|remove|rm` family (with `-C` variants), and the
      `git reflog expire|delete` family (with `-C` variants).
    - `hooks.PreToolUse[matcher=Bash]`: attach
      `block-dangerous-git.sh` and `block-no-verify.sh` after
      `block-git-push.sh`. `worker_roles.default` keeps
      `block-org-structure.sh` last; `worker_roles.claude-org-self-edit`
      retains its existing org-structure carve-out (no
      `block-org-structure.sh` on the self-edit role by design).

### Notes

- Runtime evaluator behaviour is unchanged. This release ships only
  the schema surface needed for ja's Phase 2 worker git guardrails so
  ja's drift CI passes once the runtime pin window widens to include
  `0.1.9`.
- Concrete `sandbox` / `sandbox_by_pattern` bodies remain ja-side
  policy and are not bundled with the runtime; the byte-drift check
  strips both sides' bodies before comparison
  (`_strip_ja_only_sandbox_bodies` in
  `tools/check_runtime_schema_drift.py`).
- Tagging, GitHub release, and PyPI publish are handled secretary-side
  post-merge (see `knowledge/curated/release-process.md`).

## [0.1.8] - 2026-05-11

### Added

- `claude_org_runtime.settings.generator`: Phase 3 case E — extend WSL
  detection markers and emit sandbox suppression `$comment` metadata
  (Refs `claude-org-ja#392`, `claude-org-ja#389`).
  - `_detect_wsl` now matches `Microsoft` / `WSL` in `/proc/version`
    (covers WSL1 `Linux version 4.4.0-19041-Microsoft` and WSL2
    proc/version-only detection paths) in addition to the historical
    `microsoft-standard-WSL` marker on `/proc/sys/kernel/osrelease`,
    per `phase3-bootstrap-policy-design.md` §5.2(a).
  - `$comment` is emitted on the rendered settings whenever the runtime
    suppressed at least one Layer 3 `sandbox.filesystem.denyRead` /
    `denyWrite` entry. Format follows
    `sandbox-launcher-contract.md` §2.1:
    `platform=<linux|wsl>, layer-3 entries suppressed: [<list>]`. The
    launcher's `/sandbox` status surface parses the fixed prefix to
    discover the suppressed set without re-deriving it. Structured
    anchor entries render as `<anchor>:<path>`; legacy raw strings
    render as-is. Layer 2 `permissions.deny` is untouched per design
    §5.2(b).
  - `settings show` text mode now surfaces the `$comment` line in both
    bare and `--explain` modes so operators get an at-a-glance
    suppression summary even without `--explain`'s full per-entry
    block. JSON mode already exposed it via `payload['settings']`.
  - Documentation: `render_role` docstring now distinguishes between
    `$comment` keys dropped from input role specs vs. the suppression
    `$comment` the runtime adds to the rendered output.
  - `_normalize_sandbox_entry` docstring clarifies that legacy raw
    `~/...` strings are NOT auto-expanded — operators wiring
    home-relative case E suppression must use the structured
    `{anchor: 'home', path: ...}` form (Phase 1 backward-compat
    decision).

### Notes

- Realpath-escape suppression on `sandbox.filesystem.denyRead` /
  `denyWrite` itself is unchanged from 0.1.4; this release ships only
  the metadata + observability surface alongside the broadened WSL
  detection markers.
- Out of scope (deferred per `claude-org-ja#392` task brief): case A
  bootstrap fallback (launcher-side, `sandbox-launcher-contract.md`
  §3), `failIfUnavailable` redefinition (pends case A), and the
  `/sandbox` status output (claude-org-ja territory).

## [0.1.7] - 2026-05-10

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
    `<base_clone>/.git/objects`, etc. (Codex Blocker 3; contract
    SoT: claude-org-ja's
    `docs/contracts/role-pattern-sandbox-contract.md` §4.2.1, not
    redistributed here). `anchor='base_clone'` without a generator
    `base_clone` context surfaces a usable error message pointing at
    `--base-clone`.
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
