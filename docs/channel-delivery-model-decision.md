# Channel delivery model: owner-scoped exclusive claim vs broadcast + dedupe

**Status:** Decided.
**Decision:** Keep the owner-scoped exclusive claim. Do **not** switch to broadcast.
**Refs:** #162 (this decision), #125 (generation/instance fencing), #129 (observer lease, bg-hosted marker).
**Scope:** This note contains a decision only. No behavior change ships with it.

---

## 1. Decision

We keep option (1), the owner-scoped exclusive claim, as the channel delivery model.

"Keep" here does **not** mean "keep as-is". The analysis behind this decision found that the current
implementation is silently wrong by default for most owners, and that the issue's premise "This works today"
is false (§4.1). What we are keeping is the *model*; what we are rejecting is the claim that the *delivery
model* is the axis on which this defect should be fixed.

To be unambiguous about what was decided: the decision is **"do not change the delivery model"**. It is
explicitly **not** a finding that the current fencing is sufficient. The defect in §4.1 is real, is the
default for most owners, and must still be fixed - as identity work inside the existing model (§8), tracked
in separate issues rather than bundled here.

The central finding is that **this was never a delivery-model problem. It is an identity problem.** The
persisted mcp-config carries both the full agent token and the delivery credential in replayable form, and the
only non-replayable discriminator in the system (the observer secret in process env) is applied to exactly one
owner and only on the push path. Neither delivery model fixes that; broadcast merely relocates the failure
from the push path to the pull path.

Required follow-up work is listed in §8. It is deliberately not bundled here.

---

## 2. What delivery is today

A message is a row in the daemon's in-memory store, moving `UNDELIVERED -> CLAIMED(lease, owner, epoch,
generation) -> DELIVERED` (`broker/store.py`). Two consumers exist for the same rows:

- **PUSH.** A tool-less stdio MCP sidecar (`broker/channel_sidecar.py`) polls `/poll-claims` about once a
  second, emits each claimed row as a `notifications/claude/channel` frame, then calls `/confirm-delivered`.
  The sidecar is spawned by Claude Code itself, because `channel_server_config` injects it into the child's
  `--mcp-config` (`broker/tokens.py:161-178`).
- **PULL.** `check_messages` calls `drain()`, which returns `UNDELIVERED`-and-unclaimed rows and marks them
  `DELIVERED` (`broker/surface.py:765`, `broker/store.py:343-361`).

Exclusivity is enforced by three mechanisms, all of which exist to tell two sidecars apart:

1. `(generation, instance_id)` fencing, rejecting the loser with `stale_sidecar` (`store.py:471-480`,
   `:551-567`).
2. An observer lease whose secret rides in **process env** (`ORG_BROKER_CHANNEL_OBSERVER`), not in mcp-config,
   precisely because mcp-config is what a fork replays (`store.py:412-418`, `launcher.py:313`).
3. An armed -> activated lease lifecycle so a slow-starting session is not fenced out by TTL
   (`store.py:195-201`, `:429-433`).

---

## 3. The failure class

A forked or resumed session is a legitimate child. It replays the persisted mcp-config verbatim, so its
sidecar presents the *same* delivery credential. `poll_claims` hands each row to whichever sidecar polls
first; the row is emitted into a session nobody is watching, and confirmed. The journal reads `claimed` +
`delivered`. The human sees nothing.

---

## 4. What we verified

Everything in this section was read in the source, not inferred. Several premises did not survive.

### 4.1 "This works today" is false for the majority of owners

The observer lease is **opt-in and almost never on**. It is asserted only when a caller explicitly passes
`observer: True` (`server.py:278-283`, `:318-319`), which in practice is only `org up`'s secretary.
`spawn_claude` issues a delivery credential and attaches the channel sidecar but **never asserts a lease**.

For every worker and dispatcher pane, therefore, `register_delivery_instance` falls through to
**last-register-wins** (`store.py:419-422`). A fork does not merely have a chance of winning the race - its
register *deterministically* bumps the generation and fences the original. The original sidecar then receives
`stale_sidecar` on every poll and deliberately never re-registers, because re-registering would start a
generation war (`channel_sidecar.py:279-286`). It polls forever and emits nothing.

Both fallbacks are disarmed in this state:

- `drain()` returns only `UNDELIVERED` rows, so during the fork's claim window the operator's own
  `check_messages` returns `[]`.
- `_nudge_worker`'s pending predicate counts `UNDELIVERED` **or** `CLAIMED` rows (`server.py:380-383`). During
  the claim window it fires a nudge for an inbox that reads empty; once the fork confirms, the row is
  `DELIVERED`, `pending` goes false, and the nudge stops entirely.

This is the worst shape a fault can take: silence that survives the operator's own recovery attempt and leaves
no symptom where a human looks. It is the **default** for `spawn_claude`-spawned owners, not a corner case.

### 4.2 Broker has no receiver-side dedupe

The sidecar keeps no seen-set. `msg_id` is written exactly once - into the notification meta
(`channel_sidecar.py:148`) - and is never read anywhere in `src/`. Every other occurrence in the repository is
a docstring or a log string. Dedupe is a *delegated promise to the host*, not an implemented mechanism, and
nothing in this repository can verify the host honours it.

### 4.3 "Broadcast + dedupe" is two answers to two different problems

The shorthand name suggests one mechanism with two halves. It is not.

- `msg_id` dedupe addresses **redelivery to the same session** (lease expiry, epoch flip).
- The duplication broadcast introduces is delivery to a **different session** (the fork), which receiver-side
  dedupe would not suppress - **and must not**, since that second copy reaching the still-live session is the
  entire point of the proposal.

So adopting option (2) means building the fan-out *and* separately building the dedupe that does not exist.

### 4.4 The PULL path is completely unfenced

All three mechanisms guard only the push path. `drain()` takes only an `AgentBind` - no generation, no
instance id, no observer check (`store.py:343-361`) - and a fork replays the **full agent token** too, since
`mcp_config_for` embeds it in static headers (`tokens.py:192-205`).

A forked session's `check_messages` therefore destructively consumes the operator's rows with zero fencing,
and they are never redelivered. **This is the #162 failure with no sidecar involved at all.** No delivery
model on the table closes it.

### 4.5 renga's shape does not port

renga does not have this failure class because its delivery is a broadcast, but the mechanism is not
transferable:

- The bus is **in-process**. `EventBus::emit` clones the event into a per-subscriber bounded `sync_channel`
  (`CHANNEL_CAPACITY = 256`, `src/ipc/events.rs:34`, `:89-122`). Subscribers are IPC connections inside a
  single long-lived server process. Broker's sidecars are separate processes polling over HTTP, with per-row
  state persisted in the daemon.
- It is explicitly **best-effort**. A slow subscriber has its events **dropped** and is told so via a
  synthetic `EventsDropped` meta-event (`events.rs:113-120`). The module documents itself as "not a reliable
  replication source".
- There is **no persistence and no ack**. If no subscriber is live at emit time, the message evaporates. There
  is no analogue of `/confirm-delivered` anywhere in renga.
- There is **no message id in the wire type at all** (`src/ipc/mod.rs:941-956`). Receiver-side msg-id dedupe
  is not merely absent in renga - it is impossible. The receiver relies solely on the pane filter
  (`src/mcp_peer/mod.rs:3136-3143`), and renga's addressing key (`RENGA_PANE_ID`) is read from process env,
  making it structurally non-replayable in exactly the way broker's delivery credential is not.

renga is not a more reliable design that we should copy. It is a *less* reliable design that fails in a
different direction. The property worth taking from it is the failure direction, not the machinery.

---

## 5. Failure direction

This is the core of the judgement, and it is genuinely in favour of broadcast:

- Exclusive claim fails toward **silence**. Silence is invisible, unbounded, and indistinguishable from "no
  one sent anything".
- Broadcast fails toward **duplication**. Duplication is visible, self-limiting, and self-reporting: the
  duplicate arrives in the channel the operator already reads, requiring no dashboard, no alert plumbing, and
  no consumer.

For an operator-facing report channel, duplication is the safer direction. We accept this argument. It is the
strongest case for switching, and it is why the decision below is a genuine trade rather than a dismissal.

The reason it does not carry the decision is §6.2: the failure direction argument is about the *push* path,
but the same silent loss is reachable through the *pull* path, which broadcast leaves untouched. Buying a
better failure direction on one of two paths, at the cost of a delivery-core rewrite, does not eliminate the
failure class - it narrows it. §6.1 and §6.3 add further costs, but §6.2 is what makes the trade unattractive.

---

## 6. Why we are not switching

### 6.1 Broadcast turns a persisted credential into a standing read capability

Today, for the secretary (the only owner with an observer lease), a fork's *sidecar* is rejected `unobserved`
(`store.py:412-418`); it sets `_stood_down` and its push loop returns before entering the claim loop
(`channel_sidecar.py:191-198`, `:254-258`). It never claims, never emits, never confirms.

Two corrections to how this argument was first drafted, both of which narrow it:

- It holds **only** for sidecar activity. The lease does not protect the secretary from a fork's *pull*: per
  §4.4, `drain()` authenticates nothing beyond the replayed `AgentBind`, so a forked session's
  `check_messages` still consumes the secretary's rows and marks them `DELIVERED`. The secretary is not
  "correct today" in general - it is correct only against the push path.
- Broadcast does **not** cost the watched session its copy. With the per-row-per-instance state that §6.3
  identifies as mandatory, a fork's ack completes only that fork's own delivery and does not retire the
  incumbent's. Retiring a row on any single ack is a defect of the proposal *as drafted* (§6.3), not a
  property of broadcast as a model, and charging broadcast for it would be comparing against a known-broken
  variant.

What survives is a **confidentiality** cost rather than an availability one. The delivery credential is stored
literally in the mcp-config (`tokens.py:161-178`, which deliberately avoids `${VAR}` indirection), and for the
secretary that config is persisted to `<state-dir>/secretary-mcp.json` at mode 0600 (`launcher.py:195-217`,
`:558`). Today a holder of that file gets **nothing** on the push path: register returns `unobserved`, the
generation is untouched, and no claim is ever issued. Under broadcast the same holder receives a **full copy
of every message** sent to the operator's own report channel, because broadcast's premise is that any
registered instance is entitled to a copy.

So broadcast would not silence the watched session, but it would convert a persisted, replayable credential
from an inert artifact into a standing read capability on the most sensitive queue in the system. That cost is
real and lands on the owner the operator actually watches - though it is narrower than "broadcast regresses
the protected owner", which is how this point was originally put here.

### 6.2 Switching does not close #162

Per §4.4, the pull path is unfenced and a fork replays the full token. Broadcast changes only the push path.
After a full migration, `check_messages` from a forked session would still destructively consume the
operator's rows. Switching relocates the failure rather than eliminating it, while paying full migration cost.

### 6.3 The migration cost is large and the specification is not converged

By a keyword scan over test bodies, 41 of 64 tests in `tests/broker/test_delivery.py` and 3 of 13 in
`tests/broker/test_channel_sidecar.py` reference generation / instance / observer / lease / `CLAIMED`
semantics - roughly 44 of 77. The scan is a proxy and likely undercounts indirect dependencies such as the
shared sidecar fixture. Switching rewrites that surface in the same commit that changes the semantics those
tests guard, in a path that has already produced incidents.

`QueueRow`'s claim identity is also single-valued (`lease_until`, `owner`, `claim_epoch`, `claim_generation`,
`store.py:104-107`), so broadcast is not a change to fan-out alone - it requires per-row-per-instance
delivery state, plus membership TTL, retirement/GC, and PULL reconciliation.

Two defects were found in the proposed design on its first writing:

- the offer predicate never consults the pull-drained state, re-opening the push/pull double delivery that
  `drain`'s claim-respecting behaviour exists to close;
- retiring a row on *any* single ack is cheaper than today's `(generation, instance_id, epoch)` triple and
  introduces a **new silence path**.

Both are repairable, and both point in the direction of the thesis. But a design whose central predicate is
wrong on first writing is not one whose cost can be estimated with confidence.

### 6.4 The bar the decision was held to

Switching required showing a concrete failure that **cannot be closed without changing the model**. The
failure found is severe and real (§4.1) - but it is closable within the model, cheaply: a **live-incumbent
guard** in the ten-line last-register-wins branch (`store.py:419-422`), reusing the `_delivery_poll_seen` map
the daemon already maintains, refusing a generation bump while an incumbent instance is demonstrably still
polling. The bar is therefore not met.

---

## 7. What keeping does not fix

Stated plainly, because these are the costs of this decision:

1. **Correctness rests on what a fork can and cannot inherit.** The argument is: mcp-config is replayable,
   process env is not. That is true today, but it is an assumption about Claude Code's fork/resume behaviour
   which we do not control and do not test against.
2. **The three mechanisms are unevenly applied.** Fencing covers all owners; the observer lease covers one.
   The lease is the only mechanism that actually distinguishes *observed* from *replayed*, and it is off for
   almost everyone (§4.1).
3. **The one anomaly signal has no consumer.** `_note_poll_locked` detects two live instances for an owner and
   journals `duplicate_sidecar_detected` (`store.py:203-235`, called before the fence at `:466-468` so even
   stale-generation polls are recorded). Nothing in this repository reads it. Detection without a consumer is
   not observability.
4. **`_stood_down` is a latch with no `clear()`** (`channel_sidecar.py:86`). A sidecar suppressed once stays
   silent for the life of the process, even after the condition that suppressed it has lapsed.
5. **`poll_claims` renews and activates an armed lease for whatever instance is current-generation**
   (`store.py:485-487`) without checking that instance ever presented the secret. After a lease re-assert, an
   older session's sidecar can activate the freshly-armed lease.
6. **The pull/full-token door stays open** (§4.4), independent of this decision.

Operationally, until (3) has a consumer, the fork failure is diagnosable only by reading `queue.jsonl` for
`duplicate_sidecar_detected` and `delivery_generation_registered` and correlating them by owner.

---

## 8. Required follow-up (not in this PR)

Ordered by severity. Each should be filed separately.

1. **Live-incumbent guard on last-register-wins** (`store.py:419-422`). Closes §4.1, the default-silence path
   for every `spawn_claude` owner. This is the single highest-value fix and the reason "keep" is defensible.
2. **Give `duplicate_sidecar_detected` a consumer.** Surface it where an operator sees it. Until then the
   detector is inert.
3. **Rework `_stood_down` into a recoverable state** (§7.4). Note the trap: a naive periodic re-register
   converts the observer lease from a permanent fence into a TTL-delayed fork takeover, which is strictly
   worse. The retry must not be able to win a race it is currently structurally barred from.
4. **Bind lease renewal to the instance that presented the secret** (§7.5).
5. **Track the pull / full-token identity door separately** (§4.4). This is not a delivery-model question and
   should not be filed as one.

Note that (1) and (3) interact: do not land (3) without (1).

### 8.1 Design constraints on the live-incumbent guard

The guard in (1) interacts with the observer lease, and a naive implementation reintroduces silence. Three
constraints, each derived from a specific code path:

**Scope it to the no-lease branch.** The last-register-wins branch (`store.py:419-422`) is reached in two
different situations: when there is no active lease at all, *and* when there is an active lease and the caller
presented the correct secret (`store.py:412-418` falls through on a match). A guard placed unconditionally in
that branch would therefore also fence the *legitimate* secretary sidecar on restart - a double fence, which
is exactly the collision to avoid. The observer secret is non-replayable proof of which session is observed;
incumbency is only circumstantial evidence. **Proof must beat incumbency:** apply the guard only when
`lease is None`.

**Use a new error code; never reuse `unobserved`.** The sidecar latches `_stood_down` on exactly two codes,
`suppressed_bg_hosted` and `unobserved` (`channel_sidecar.py:191-198`), and that latch has no `clear()`. If
the guard rejects with `unobserved`, a legitimate restart that merely arrived too early is muted
*permanently*. With a distinct, non-latching code, the push loop keeps retrying about once a second while
`_current_generation()` is `None` (`channel_sidecar.py:266-276`) and recovers on its own once the incumbent
ages out - a bounded delay on the order of `lease_seconds` (default 30s, `server.py:65`), not a mute.

**Key the guard on the current instance, not on "any recent poller".** `_note_poll_locked` is called *before*
the fence, deliberately, so that stale-generation polls still produce a duplicate signal
(`store.py:466-468`). A fenced fork keeps polling forever after `stale_sidecar`
(`channel_sidecar.py:279-286`), so it writes itself into `_delivery_poll_seen` every second. A guard that
treats any recent poller as an incumbent would therefore let a *rejected fork* block the original's legitimate
re-registration - for example after a daemon restart, when `cur_gen` is 0 and the original must register
again. The guard must test specifically whether `_delivery_instances[owner]`, the current-generation instance,
has polled recently.

**Apply the staleness threshold inside the guard.** The governing window is `lease_seconds` (default 30s), not
`observer_lease_seconds` (default 90s): `_delivery_poll_seen` is pruned with `window = self.lease_seconds`
(`store.py:215-221`). But that pruning runs only inside `_note_poll_locked`, which is reached only from
`poll_claims` (`store.py:468`) - **registration never prunes**. So when an incumbent dies and nothing else
polls for that owner, its entry is never removed, and a guard that merely tests for *presence* in the map
would block its replacement forever. The guard must compare the recorded timestamp against `lease_seconds`
itself rather than trusting the map to have been pruned.

**Residual, stated honestly.** Even correctly scoped, the guard converts "last register wins" into "incumbent
wins". Neither rule is right without proof of which session a human is watching. If an owner's incumbent is
alive and polling but *abandoned* - a background pane nobody reads - a deliberately started new session is
refused for as long as the incumbent keeps polling, which is unbounded rather than 30 seconds. Messages are
not lost in that case, but they arrive somewhere nobody is looking, which is the same thing from the
operator's chair. This is the residual that only the observer lease actually removes, and it is why §9
condition 3 (extending the lease to all spawned panes) is the structural fix rather than a nice-to-have.

---

## 9. Conditions to revisit switching

This decision should be reopened if any of the following is observed. Recording them is the point of this
section - without it, the same discussion recurs from scratch.

1. **`duplicate_sidecar_detected` fires materially for owners other than the secretary** in real `queue.jsonl`
   data, after follow-up (1) has landed. That would mean fork contention is common rather than incidental, and
   the residual after hardening is not small.
2. **The host is shown to coalesce or drop duplicate `notifications/claude/channel` frames.** The entire
   operator-safety case for broadcast rests on the duplicate being *visible*. If the host silently collapses
   duplicates, broadcast loses its main advantage and this decision becomes unambiguous.
3. **The observer lease is extended to all spawned panes** and proves stable in practice. That would make the
   protected-minority argument in §6.1 apply to everyone, materially changing the trade.
4. **A ruling that DELEGATE-class traffic on this queue requires at-most-once execution.** Unconditional
   broadcast would then be wrong for a subset of rows, pointing at per-row delivery policy set at enqueue
   rather than a global model.
5. **The fork/resume inheritance assumption breaks** - i.e. Claude Code begins propagating process env across
   resume, or stops replaying mcp-config verbatim. Assumption (§7.1) is load-bearing and externally owned.

---

## 10. Dissent

This is recorded because the decision went against the majority of the independent evaluation, and a future
reader should know that.

Three independent judges scored the options on separate lenses. Two recommended **switch**; one recommended
**keep**. An adversarial reviewer assigned to refute each option marked **keep disqualified**, **hybrid
disqualified**, and **switch viable-with-work**.

- The *operator-safety* judge (switch) held that the shape of the fault decides it: today's failure is silence
  that survives the operator's own recovery attempt, whereas under broadcast the duplicate *is* the fault
  report, delivered where the operator already looks, needing no consumer.
- The *correctness-under-fork* judge (switch) held that keep is "silently wrong by default for most owners"
  and that broadcast is the only option that stops asking a question no replayable credential can answer.
- The *implementation-cost* judge (keep) held that the test surface decides it: ~50 of 77 tests rewritten in
  an incident-prone path, with no testability gain, since fork-replay is trivially testable under every option.

The decision goes to keep on two grounds that outweigh the majority: switching **does not close #162**,
because the pull path is unfenced and a fork replays the full agent token (§6.2) - conceded by the correctness
judge, which explicitly noted that no option closes the pull door - and the migration is large while its
specification was demonstrably not converged (§6.3).

A third argument, that broadcast regresses the one protected owner, was raised by the adversarial reviewer and
initially carried more weight here than it deserved. Review narrowed it: with per-instance acks the watched
session still receives its copy, so the cost is confidentiality rather than lost delivery (§6.1). It counts
against switching but is not load-bearing, and the original phrasing overstated it.

A **hybrid** ("keep claim, degrade a contested owner to fan-out") was evaluated and rejected. Its safety is
contingent on a wall-clock contention window that collapses under the sidecar's own 5-second HTTP timeout, and
it needs the full per-row-per-instance rework of option (2) while retaining all of option (1)'s machinery -
maximum build, near-zero delete.
