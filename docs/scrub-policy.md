# Scrub Policy for Migrate-Script Fixtures

This document records the scrub policy used when promoting `.state/`
snapshots from `claude-org-ja` into the `claude-org-runtime` test
suite as fixtures for the migrate-script.

It implements the four `Q-Scrub` decisions made in the 2026-05-02
comment on `claude-org-ja#208`.

## Q-Scrub-1: What is scrubbed?

The deterministic scrubbers in `tests/scrub/scrub_fixture.py` redact
the following classes of data:

| Class                 | Replacement                       | Detection |
| --------------------- | --------------------------------- | --------- |
| URL                   | `https://example.com/REDACTED`    | Regex `https?://[^\s\"']+` |
| Email                 | `redacted@example.com`            | Regex `[\w.+-]+@[\w-]+\.[\w.-]+` |
| API key / secret      | `[REDACTED_KEY]`                  | Targeted patterns: `ghp_*`, `github_pat_*`, `gho_*` / `ghu_*` / `ghs_*` / `ghr_*`, `sk-*` / `sk-proj-*`, `AKIA*` / `ASIA*`, `xox[abprs]-*` |
| Session narrative     | `[SESSION NARRATIVE REDACTED]`    | H2 blocks in Markdown whose heading matches `^## YYYY-MM-DD ...` |
| Worker free-text note | `[NOTE REDACTED]`                 | JSONL `note` field with `len(value) >= 50` |

Structural identifiers are **never** modified:

- `task_id`
- `event`
- `ts`
- `pane_id`, `pane_name`
- `status`
- `state`

### Rationale: why preserve `task_id`?

The migrate-script tests assert on the structure of the journal:
which `task_id` transitioned through which states, in which order.
If `task_id` were rewritten the fixtures would lose the property
under test. Identifiers in this project are opaque (`T-001` etc.)
and do not carry user-identifying information.

## Q-Scrub-2: How is scrubbing performed?

A **hybrid** workflow:

1. The deterministic script (`tests/scrub/scrub_fixture.py`) runs
   first to catch the bulk of regex-detectable PII and to collapse
   long free-text into placeholders.
2. A human Lead reviews the unified diff (`--diff`) before the
   scrubbed output is committed, applying any additional manual
   redactions the regex pass missed.

### Rationale: why hybrid?

A pure-regex pass produces false negatives on novel PII shapes
(unusual key formats, proper nouns, customer names embedded in
prose). A pure-manual pass is too easy to skip under deadline
pressure. The deterministic pass gives the Lead a consistent
baseline; the review gate ensures nothing leaks just because a
regex did not match.

## Q-Scrub-3: How many fixtures?

The Lead curates **3-4 situational fixtures** outside this PR:

- `SUSPENDED` state with no active workers
- `ACTIVE` state with concurrent workers
- `ACTIVE` state mixing completed tasks
- (optional) anomaly slice

The synthetic example shipped with this PR
(`tests/fixtures/synthetic/scrub_input_sample.jsonl` plus
`expected_output.jsonl`) is **not** one of those situational
fixtures. It exists only to exercise the scrub script.

## Q-Scrub-4: Where do fixtures live?

In-repo, under `tests/fixtures/`. No Git LFS. The synthetic
example lives under `tests/fixtures/synthetic/`; real curated
fixtures will live alongside it under sibling directories.

## Operational procedure

When the Lead promotes a new real fixture:

1. Place the source `.state/` snapshot from `claude-org-ja`
   somewhere outside the repository (it must not be committed
   in raw form).
2. Run the scrub script:

   ```sh
   python -m tests.scrub.scrub_fixture \
       --in <local snapshot path> \
       --out tests/fixtures/<situation>/journal.jsonl \
       --diff
   ```

3. Inspect the diff. If any PII or secret slipped through, edit
   the output file by hand or extend the script's patterns and
   re-run.
4. Run the test suite (`pytest -x -q tests/scrub/`) to confirm
   the scrubbers still behave as documented.
5. Commit the scrubbed file and any pattern updates together.

If new classes of PII appear, prefer extending
`tests/scrub/scrub_fixture.py` over one-off manual edits so the
next Lead inherits the coverage.
