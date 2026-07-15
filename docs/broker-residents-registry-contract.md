# broker resident registry contract (v1)

Status: runtime-side implemented (Issue #142). Registrant side (claude-org) is a
follow-up in a separate issue and is **out of scope** for this document's
implementation, though this contract is written *for* that registrant to build
against.

This document is the **source of truth** for the `<state-dir>/residents/*.json`
PID registry that `org up` / `org down` use to detect -- and, with `--reap`,
terminate -- broker-*unmanaged* resident processes.

---

## 1. Purpose and scope

The org-broker daemon manages the panes it spawns. Some processes, however, run
*outside* the daemon's lifecycle and outlive it -- for example a secretary queue
watcher or an attention watcher started by the org tooling. When a prior
generation crashes, such a process can be left running with nothing tracking it.
The next `org up` has no way to notice it.

The **residents registry** is a directory of small JSON files, one per resident,
that a *registrant* (the process itself, or the tooling that spawns it) writes so
that the runtime can:

- **scan** the registry at `org up` (cold start only) and `org down` (post
  teardown),
- **announce** what broker-unmanaged residents exist (the default), and
- optionally **reap** them with `--reap`: terminate the ones whose ownership
  *and* identity both verify, and remove stale registrations.

### Runtime vs registrant split

| Actor | Responsibility |
|-------|----------------|
| **runtime** (this PR) | scan, match, announce, and -- only under `--reap` -- terminate owned+verified live residents and delete owned stale registrations. **Never writes a registration.** |
| **registrant** (claude-org follow-up, out of scope) | create/update/remove `residents/<name>.json` for each resident it owns, atomically. |

The runtime is deliberately a pure *consumer* of the registry. It writes nothing
into `residents/` except deleting a stale file it owns during `--reap`.

---

## 2. In scope / out of scope (expectation adjustment)

**Only registered residents are detected.** A resident with no
`residents/<name>.json` file is invisible to pre-flight and to `--reap`.
Unregistered processes, and any process from a generation that predates the
registrant implementation, are **out of scope by design**.

This narrows Issue #142's original phrasing ("detect broker-unmanaged
processes"): the runtime detects broker-unmanaged processes *that have been
registered*, not arbitrary processes. Discovering unregistered processes would
require enumerating and heuristically classifying every process on the machine --
unsafe (misidentification -> mis-kill) and not portable in stdlib. The registry
is the safe, explicit contract that makes detection and reaping sound.

---

## 3. Registry location and file lifecycle

- Location: `<state-dir>/residents/<name>.json` (default state-dir `.state/broker`
  => `.state/broker/residents/`).
- One file per logical resident. The filename stem SHOULD equal the record's
  `name`.
- The runtime globs `residents/*.json`. An absent or empty directory means "zero
  residents" and the runtime prints **nothing** (no per-run noise while the
  registry is empty, which is the normal state until the registrant ships).
- The registrant MUST publish each file **atomically** (write a temp file, then
  `os.replace` into place) so the runtime never reads a torn file. The runtime
  additionally re-reads a file immediately before deleting it and only deletes if
  the content is unchanged (see section 8).

---

## 4. Schema v1

Validated with `jsonschema` (draft 2020-12). `additionalProperties` is permitted
at every level so the registrant may carry extra audit fields.

| Field | Req | Type | Meaning |
|-------|-----|------|---------|
| `version` | yes | integer | Registry schema version. Runtime supports `{1}`. Missing/unknown => announce warn-only, **never** killed or deleted (section 9). |
| `name` | yes | string | Logical resident id; SHOULD equal the filename stem. |
| `pid` | yes | integer >= 1 | OS process id. |
| `started_at` | yes | number (epoch s) | **Kernel process start time** (see the warning below). The identity anchor; PID-recycle defense. |
| `owner` | yes | object | Ownership identity (section 5). |
| `owner.state_dir` | yes | string | Absolute realpath of the owning state-dir. |
| `owner.root_cwd` | yes | string | Absolute repo root of the owning org. |
| `owner.fingerprint` | yes | string `^root:[0-9a-f]{16}$` | `residents.repo_fingerprint(root_cwd, state_dir)` (section 5). The ownership decision key. |
| `owner.instance_id` | no | string\|null | Audit/announce only; not in the ownership predicate. |
| `identity` | no | object | Corroborating identity strengtheners. |
| `identity.exe` | no | string\|null | Absolute exe path as observed at registration. Demotes to mismatch only if both sides present and differ. |
| `identity.cwd` | no | string\|null | Process cwd. Same demotion rule. |
| `identity.token` | no | string\|null | Registry-integrity/correlation only; **not** verified against a live process in v1. |
| `cmdline` | no | array\|string\|null | **Display only. Never used in any match predicate** (argv representation varies across OS/shell). |
| `role` | no | string\|null | Display/audit. |
| `registered_at` | no | number\|null | Display/audit (registration wall-clock). |

> **Field-name collisions with `daemon.json` -- read carefully.**
> `daemon.json` also has fields literally named `started_at` and `host`, but they
> mean **different things** there:
> - `daemon.json.started_at` is the daemon's **wall-clock** boot time
>   (`time.time()`). A resident's `started_at` is the **kernel process start
>   time** -- a different clock. Do **not** copy the daemon convention.
> - `daemon.json.host` is the daemon's **bind address** (`127.0.0.1`). The
>   fingerprint's host is the **machine hostname** (`socket.gethostname()`) --
>   see section 5. Feeding the bind address into the fingerprint would make every
>   record mismatch and silently under-reap forever.

### How the registrant must record `started_at`

The runtime observes the kernel start time itself and compares. To get a
deterministic zero-delta match, the registrant MUST record the value the runtime
will later read, using the shipped helper:

```python
from claude_org_runtime.broker import residents
obs = residents._observe_process_identity(os.getpid(), os_name=residents._current_os())
started_at = obs.start_time    # kernel start time; store this verbatim
```

Recording wall-clock (`time.time()`) at spawn instead is tolerated only within
`START_TIME_TOLERANCE` (2.0 s) and, combined with a recycled PID landing in that
window, could theoretically false-match. Kernel start time is therefore a **MUST**,
not a SHOULD.

### Valid example (`.state/broker/residents/secretary_queue_watcher.json`)

```json
{
  "version": 1,
  "name": "secretary_queue_watcher",
  "pid": 40521,
  "started_at": 1752600000.0,
  "owner": {
    "state_dir": "/home/u/proj/.state/broker",
    "root_cwd": "/home/u/proj",
    "fingerprint": "root:9f3a1c74b2e05d8a",
    "instance_id": null
  },
  "identity": {
    "exe": "/usr/bin/python3.11",
    "cwd": "/home/u/proj",
    "token": "3f9a7c2e1b8d4a60"
  },
  "cmdline": ["python", "-m", "claude_org.secretary.queue_watcher"],
  "role": "attention-watcher",
  "registered_at": 1752600001.2
}
```

---

## 5. Ownership model

Ownership answers "is this resident **mine** to touch?" It must not rest on the
state-dir path alone: a `.state/broker` path can be reused by a different org
instance over time, and reaping across that reuse would be a mis-kill
(constraint 3).

The ownership key is a **repo-root fingerprint** (single source of truth, shipped
so both sides compute it identically):

```python
def repo_fingerprint(root_cwd, state_dir, *, host=None):
    host = host if host is not None else socket.gethostname()
    canon = "\0".join([
        host,
        os.path.normcase(os.path.realpath(root_cwd)),
        os.path.normcase(os.path.realpath(state_dir)),
    ])
    return "root:" + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]
```

`ownership_match` returns `match` only when the record's `owner.state_dir`
(realpath+normcase) equals this run's state-dir **and** the fingerprints are
equal. Because the fingerprint encodes host + root_cwd + state_dir, a different
checkout, a different host, or a different org reusing the same path all yield
`mismatch`, and the kill/delete paths are unreachable for them.

### Why fingerprint, not a per-boot instance_id

Residents **outlive daemon boots**, so a per-boot random instance id would never
match a survivor from a previous boot -- the exact process we most need to reap.
The repo-root fingerprint is stable across boots and derivable identically by
both sides with **no new runtime-written file**, keeping this PR cleanly
consumer-only. `owner.instance_id` remains an optional audit field.

### How `org down` obtains `root_cwd`

`org down` did not previously know the repo root. It is resolved in this order:

1. explicit `--root-cwd`,
2. `daemon.json.root_cwd` (now persisted by the daemon; read before teardown
   removes the sidecar),
3. `os.getcwd()` (back-compat when the daemon predates the sidecar field).

Only path 3 during an upgrade window could produce a wrong fingerprint, which
causes *under*-reap (a safe no-op), never a mis-kill.

---

## 6. Identity model

Identity answers "is the live PID still the process that was registered?" -- the
defense against **PID recycling** (a different process reusing the pid number
after the resident died).

- **Anchor: kernel start time.** A recycled pid necessarily started *later*, so a
  start-time comparison within `START_TIME_TOLERANCE` (2.0 s) is the primary
  discriminator. Outside tolerance => `mismatch` (recycled).
- **exe / cwd: corroboration only.** They demote a would-be match to `mismatch`
  *only when both the record and the observation have the value and they differ*.
  An absent observed field (e.g. Windows cwd) is benign.
- **cmdline: never in the predicate** (constraint 2).
- **identity.token: not live-verified in v1.** There is no portable stdlib way to
  read an arbitrary process's env/argv across Linux/macOS/Windows; a Linux-only
  check would break cross-platform parity. The token is registry-integrity /
  correlation metadata only.

`identity_match` returns `match` / `mismatch` / `unknown`. **`unknown` fails
closed** -- an unverifiable identity is never eligible for termination.

### Per-platform observability (stdlib only; no psutil)

Observation goes through the `residents._observe_process_identity(pid, *,
os_name, proc_root="/proc")` seam, which returns a `ProcObservation(start_time,
exe, cwd)` or `None`.

| Platform | start_time | exe | cwd | Source |
|----------|-----------|-----|-----|--------|
| Linux / WSL | yes | yes | yes | `/proc/<pid>/stat` field 22 + `/proc/stat` btime + `SC_CLK_TCK`; `/proc/<pid>/exe`, `/proc/<pid>/cwd` |
| macOS | yes (1 s) | yes | no | `ps -o lstart=,comm=` (run with `LC_ALL=C`) |
| Windows | yes | yes | no | `GetProcessTimes` creation FILETIME; `QueryFullProcessImageNameW` |
| other / unobservable | `None` -> identity `unknown` -> fail closed |

Where a platform cannot observe (`None`, or `start_time is None`), identity is
`unknown` and the resident is announced but **never reaped**.

---

## 7. Decision matrix

Precedence: unknown-version and malformed short-circuit before any process
observation. Ownership is binary (`match`/`mismatch`). Identity is
`match`/`mismatch`/`unknown`. `pid_alive` is conservative (alive on uncertainty).
**The runtime only ever touches (kills or deletes) records it owns.**

| # | version | pid_alive | ownership | identity | default (no `--reap`) | `--reap` |
|---|---------|-----------|-----------|----------|-----------------------|----------|
| 0 | unknown | any | any | any | announce "unknown schema version" | skip (never kill/delete) |
| 0b | malformed | -- | -- | -- | announce "malformed" | skip (never delete) |
| 1 | known | dead | match | n/a | announce "stale (not running)" | delete stale registration |
| 2 | known | dead | mismatch | n/a | announce "stale, different owner" | leave (not ours) |
| 3 | known | alive | match | match | announce "LIVE, reapable" | **terminate**, then delete on confirmed exit |
| 4 | known | alive | match | mismatch | announce "PID recycled; stale" | delete stale (do **not** kill) |
| 5 | known | alive | match | unknown | announce "identity unverifiable" | skip + print manual stop command |
| 6 | known | alive | mismatch | any | announce "different org instance" | skip (never touch a foreign process) |

Invariants (enforced by construction):

- **TERMINATE** <=> `reap & known & alive & ownership==match & identity==match`
  (row 3 only).
- **DELETE-STALE** <=> `reap & known & ownership==match & (dead |
  (alive & identity==mismatch))` (rows 1, 4, and row 3 after confirmed exit).
- **SKIP** <=> `reap & known & ownership==match & alive & identity==unknown`
  (row 5).
- **ANNOUNCE-ONLY / LEAVE** <=> `ownership==mismatch` (rows 2, 6) or
  unknown-version/malformed (rows 0, 0b).

**TOCTOU guard:** row-3 termination re-observes the start time immediately before
signalling; if it drifted, the process died+recycled between scan and kill, so it
is reclassified as row 4 (delete stale, no kill).

---

## 8. `--reap` semantics and platform procedures

- `--reap` is **opt-in on both `up` and `down`**; the default on both is
  announce-only.
- `org up` sweeps residents **only on a cold start** (when no healthy daemon for
  this ownership exists). It never sweeps on a reuse / already-up path, because a
  healthy org's own live residents are indistinguishable from crashed-generation
  orphans and must not be terminated by an `org up --reap` that re-attaches.
- `org down` sweeps as a **post-teardown** step on every path (including "nothing
  to stop"); the daemon is already stopped, so down is the primary home for
  reaping live residents. The sweep never changes `org down`'s return code.

### Termination procedure

- **POSIX:** `SIGTERM`, wait up to `REAP_GRACE` (5 s), then `SIGKILL` if still
  alive. On confirmed exit the owned registration is deleted.
- **Windows:** graceful `taskkill /PID <pid>` (no `/F`). If still alive after the
  grace window, the runtime does **not** auto hard-kill -- it prints manual
  guidance (`taskkill /F /PID <pid>`) and leaves the registration in place.
  Windows has no graceful signal for console-less detached processes and auto
  `/F` risks mid-write corruption even under opt-in.
- **Delete safety:** before unlinking a stale registration, the runtime re-reads
  the file and deletes only if `pid` and `started_at` are unchanged from the
  classified record, so a registrant that atomically replaces the same filename
  with a *new* live registration between scan and delete is not clobbered.

---

## 9. Version policy

`version` is required. Only `{1}` is supported. A record with a missing or
unknown version, or one that fails schema validation, is **announced but never
killed and never deleted, even under `--reap`** (constraint 5). Version is
checked *before* schema validation so a forward-compatible future record is
reported as "unknown version", not mislabelled "malformed".

---

## 10. Migration: `.state/secretary_queue_watcher.json`

The existing single-file PID marker `.state/secretary_queue_watcher.json` (a
legacy, non-canonical location) and the `residents/` registry coexist:

- **`residents/` is canonical.** The runtime reads only `residents/*.json`.
- The runtime **never reads** the legacy single file. It is neither detected nor
  reaped by pre-flight.
- Note the path distinction: the legacy file lives at
  `.state/secretary_queue_watcher.json`, while a registered resident lives at
  `.state/broker/residents/secretary_queue_watcher.json`.

Migration plan:

1. **Phase 1 (this PR):** runtime consumes `residents/`. No registrant ships yet,
   so the registry is normally empty and pre-flight is silent.
2. **Phase 2 (registrant follow-up):** the secretary queue watcher writes a
   `residents/` registration in addition to (or instead of) the legacy file.
3. **Phase 3:** once all registrants write `residents/`, the legacy single file
   is retired.

---

## 11. Deliberately minimal for this runtime-only PR

- `identity.token` is not live-verified (no portable stdlib mechanism).
- macOS/Windows reap capability is lower than Linux: no cwd corroboration; macOS
  `lstart` has 1 s resolution (the tolerance floor).
- No registrant writer ships here -- registration is the claude-org follow-up.

## Open questions (for a human)

1. `START_TIME_TOLERANCE = 2.0 s` is uncalibrated. It depends on the registrant
   recording kernel start time (section 4). A laggy wall-clock registrant could
   exceed it and make residents un-reapable -- a safe but silent under-reap.
2. `daemon.json.root_cwd` back-compat: `org down` against a daemon started by an
   older runtime falls back to `getcwd()`, which may differ from the daemon's
   real root during the upgrade window (under-reap only, no mis-kill).
3. Sanctioned POSIX hardening: capture `os.pidfd_open(pid)` at classification and
   signal via the pidfd (`os.pidfd_send_signal`, Linux 3.9+) to close the
   residual scan->signal recycle window entirely. Not implemented in v1; the
   start-time re-observation guard is the cross-platform fallback.
