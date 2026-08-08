# Channel delivery model: owner-scoped exclusive claim vs broadcast + dedupe

**Status:** Decided (re-derived once - see §10.2).
**Decision:** Adopt broadcast **per owner, in stages**, starting with the operator-facing channel. Keep
exclusive claim as the default mode for all other owners until the at-most-once question in §6.5 is settled.
**Refs:** #162 (this decision), #125 (generation/instance fencing), #129 (observer lease, bg-hosted marker).
**Scope:** This note contains a decision only. No behavior change ships with it.

---

## 1. Decision

Introduce `BROADCAST` as a third per-owner delivery mode alongside the existing `PUSH` and `PULL`. Enable it
first for the operator-facing owner (the secretary). Leave every other owner on exclusive claim until §6.5 is
resolved.

This is **not** the contention-triggered hybrid evaluated and rejected earlier (§10.1). That design flipped an
owner between modes dynamically on a wall-clock contention window, and it fell to a timing argument. This one
is **static per-owner policy**: an owner is in one mode until an operator flips it. There is no window, no
oscillation, and no contested-mode row state.

Three things make this the derived answer rather than a compromise:

- The status quo is **silently broken by default** for every `spawn_claude` owner (§4.1). This is not a
  hypothetical risk; it is current behavior, and it is invisible to the operator.
- Broadcast closes a correctness defect that exclusive claim **cannot** close in-model (§6.4), and it fails
  toward duplication rather than silence (§5).
- The one genuine reason not to broadcast everything - at-most-once traffic (§6.5) - is a property of
  *particular owners*, not of the model, and the per-owner mode machinery to act on that distinction
  **already exists** (§6.6).

The central finding of the analysis stands regardless of mode: **the underlying problem is identity, not
delivery model.** The persisted mcp-config carries both the full agent token and the delivery credential in
replayable form, and the only non-replayable discriminator (the observer secret in process env) is applied to
exactly one owner and only on the push path. The identity work in §8 is required under *either* model and is
not superseded by this decision.

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
sidecar presents the *same* delivery credential and is indistinguishable from the original by credential
alone.

The takeover happens at **registration**, not in a poll race. `/claim-owner` bumps the owner's generation and
records the caller as the sole current-generation claimer (`store.py:419-422`), so the last sidecar to
register wins outright and every other instance is refused `stale_sidecar` on every subsequent poll
(`store.py:471-480`) regardless of who polls first. The winner's rows are emitted into a session nobody is
watching, then confirmed. The journal reads `claimed` + `delivered`. The human sees nothing.

Where an observer lease *is* active, the replayed sidecar is instead rejected at registration (`unobserved`,
`store.py:412-418`) and never reaches the claim loop. §4.1 shows how rarely that applies.

---

## 4. What we verified

Everything in this section was read in the source, not inferred. Several premises did not survive.

### 4.1 "This works today" is false for the majority of owners

The observer lease is **opt-in and almost never on**. It is asserted only when a caller explicitly passes
`observer: True` (`server.py:278-283`, `:318-319`), which in practice is only `org up`'s secretary.
`spawn_claude` issues a delivery credential and attaches the channel sidecar but **never asserts a lease**.

For every worker and dispatcher pane, therefore, `register_delivery_instance` falls through to
**last-register-wins** (`store.py:419-422`). A fork does not merely have a chance of winning - its register
*deterministically* fences the original, which then receives `stale_sidecar` on every poll and deliberately
never re-registers, because re-registering would start a generation war (`channel_sidecar.py:279-286`). It
polls forever and emits nothing.

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

Adopting broadcast therefore means building the fan-out *and*, separately, the dedupe that does not exist.

### 4.4 The PULL path is completely unfenced

All three mechanisms guard only the push path. `drain()` takes only an `AgentBind` - no generation, no
instance id, no observer check (`store.py:343-361`) - and a fork replays the **full agent token** too, since
`mcp_config_for` embeds it in static headers (`tokens.py:192-205`).

A forked session's `check_messages` therefore destructively consumes the operator's rows with zero fencing,
and they are never redelivered. **This is the #162 failure with no sidecar involved at all**, and it is
independent of which delivery model is chosen (§6.2).

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

renga is not a more reliable design to copy. It is a *less* reliable one that fails in a different direction.
What is worth taking from it is the failure direction, not the machinery. Broker's broadcast mode must be
built on broker's own durable-row substrate.

---

## 5. Failure direction

This is the core of the judgement:

- Exclusive claim fails toward **silence**. Silence is invisible, unbounded, and indistinguishable from "no
  one sent anything".
- Broadcast fails toward **duplication**. Duplication is visible, self-limiting, and self-reporting: the
  duplicate arrives in the channel the operator already reads, requiring no dashboard, no alert plumbing, and
  no consumer.

For an operator-facing report channel, duplication is the safer direction, and the scarce resource this
protects is **human attention**. A duplicate costs a human one second of confusion. A silence costs an
unbounded amount of time, because nothing signals that anything is owed.

This argument carries substantial weight in the derivation below. What limits it is not cost (§6.3) but
traffic semantics (§6.5): duplication is only cheap where the recipient is a human reading reports.

---

## 6. Deriving the decision

### 6.1 Withdrawn: "broadcast regresses the one protected owner"

This argument was made and weighted heavily in the first draft. It did not survive review; it is recorded here
so that it is not raised again.

**The protection is narrower than claimed.** The observer lease rejects a fork's *sidecar*, which sets
`_stood_down` and never enters the claim loop (`store.py:412-418`, `channel_sidecar.py:191-198`, `:254-258`).
It does nothing about a fork's *pull*. Per §4.4, a forked session's `check_messages` still consumes the
secretary's rows. The secretary is correct only against the push path.

**The harm attributed to broadcast was a drafting defect, not a property of the model.** With per-row
per-instance state, a fork's ack completes only that fork's delivery and the watched session still receives
its copy. Retiring a row on any single ack is a defect of one draft (§6.3), and charging broadcast for it
compares against a known-broken variant.

**The fallback confidentiality form also fails.** The secretary's mcp-config is persisted to
`<state-dir>/secretary-mcp.json` at mode 0600 (`launcher.py:195-217`, `:558`) with the delivery credential in
it literally - but that same file carries the **full agent token** (`server.py:296`, `tokens.py:192-205`),
which already grants destructive read of the entire queue via `check_messages`. Broadcast would add a route to
data the file holder can already read, not a new capability.

Net: **broadcast costs the protected owner nothing that keeping does not already cost it.**

### 6.2 Withdrawn: "switching does not close #162"

Also made in an earlier draft, also withdrawn.

The observation behind it is true - §4.4, the pull path is unfenced - but it does **not** distinguish the two
models, because the defect is **common to both**. Exclusive claim leaves the pull path exactly as it is (§8
item 5 defers it to a separate issue), so the argument charged broadcast for a hole that keeping also declines
to close.

The asymmetry, if anything, runs the other way: a full broadcast migration must confront pull reconciliation,
and one that does so need not retain today's destructive global `drain()` semantics.

The pull path belongs to the identity work in §8, not to the case for either model.

### 6.3 What remains once migration labour is discounted

The surviving argument for keeping was migration cost. That argument mixes two different things, and they
deserve different weights:

- **(a) The labour of migrating** - rewriting roughly 44 of 77 delivery tests, redesigning a specification
  that was wrong on first drafting. This is **discounted**. The work is performed by an agent; engineering
  hours are not the scarce resource here, and a decision that preserves a known-broken default in order to
  avoid keystrokes is not defensible.
- **(b) The risk of changing semantics in a path that has already produced silent-failure incidents.** This is
  **not** discounted - but the reason is not effort. It is that the scarce resource is the **human attention
  needed to notice silence**, and this path has already demonstrated that it can fail without any signal
  reaching a human.

Restated as "what can break" rather than "what is hard", (b) is the observation that **a broadcast migration's
own failure modes are themselves silence-shaped** in at least three places:

1. **Retirement.** If a row is retired on any single ack, a fork's ack retires it before the watched
   session's instance is offered a copy - silence. *Design rule: never retire on a single ack. Retire only
   when every live instance has acked, or do not delete at all and track per-instance cursors.*
2. **Liveness/membership.** Broadcast needs a live-instance set. Judging a live-but-slow instance dead removes
   it from the fan-out and it stops receiving copies - silence. Today's analogue (lease reap) fails toward
   *redelivery* instead, because the row returns to `UNDELIVERED`. *Design rule: on ambiguity, offer anyway;
   never drop an instance from the fan-out to resolve uncertainty.*
3. **Push/pull reconciliation.** `drain()`'s single-drainer property rests explicitly on row-level claim
   ownership rather than on mode (`store.py:348-349`). Removing exclusivity means rebuilding that invariant;
   a pull predicate that errs toward *hiding* a row produces silence. *Design rule: scope pull suppression to
   the calling session only, never to another instance's ack.*

This is a real risk and it is the one thing that genuinely argues for caution. Two facts bound it:

- Each of the three has a known design rule that keeps the failure on the duplication side. They are not
  unknown unknowns; they are the specific defects adversarial review already surfaced in the draft spec.
- It must be weighed against a silence path that is **already firing by default** (§4.1). The comparison is
  not "risk of silence vs. no silence" - it is "a known, currently-firing silence path" vs. "new silence paths
  that are preventable by rule and containable by staging (§6.6)".

### 6.4 The residual that only broadcast closes

Exclusive claim's §4.1 defect can be mitigated in-model: a **live-incumbent guard** in the ten-line
last-register-wins branch (`store.py:419-422`), reusing `_delivery_poll_seen`, refusing a generation bump
while the current instance is demonstrably still polling (constraints in §8.1).

But that guard converts "last register wins" into "incumbent wins", and neither rule is right without proof of
which session a human is watching. The residual: **an incumbent that is alive and polling but abandoned - a
background pane nobody reads - keeps confirming messages into an unwatched session and indefinitely blocks
the resumed session the operator is actually looking at.** Not 30 seconds; unbounded.

This residual is **not closable inside the exclusive-claim model** without extending the observer lease to
every pane. Broadcast closes it outright: both instances simply receive a copy, and the watched one shows it.

Under the earlier weighting this point was acknowledged and then outweighed by migration cost. With (a)
discounted, it stands as a **correctness** argument for broadcast, and it is unrebutted.

### 6.5 The one real argument against broadcasting everything

Duplication is cheap when the recipient is a human reading reports. It is **not** cheap when the recipient is
an agent that will act on the message.

This queue carries both. Operator-facing reports go to the secretary; DELEGATE-class instructions go to worker
owners. A duplicate report costs a moment of confusion. A duplicate delegate instruction delivered to two live
sessions of the same worker can cause **double execution of a non-idempotent task** - and unlike silence, that
failure is not merely invisible, it is actively harmful.

Note carefully what this is and is not:

- It is **not** an argument that broadcast is wrong. It is an argument that the right delivery policy depends
  on **what the owner's traffic means**, which varies per owner.
- It is **not** symmetrical with the defects charged against exclusive claim. §4.1 fires today with no
  precondition; this one requires a fork *and* non-idempotent traffic *and* both sessions acting.

Resolving it needs a ruling this note cannot make: whether DELEGATE-class traffic requires at-most-once
delivery, or whether workers are expected to be idempotent under redelivery (the current design already
redelivers on lease reap and epoch flip, so some tolerance is already assumed). Until it is ruled, worker
owners stay on exclusive claim.

### 6.6 Staged adoption is available on machinery that already exists

The decisive practical finding. Delivery mode is **already per-owner and already atomically flippable**:

- `_delivery_modes: dict[str, str]` maps `agent_id -> PUSH/PULL`, defaulting to `PUSH` (`server.py:105`).
- `flip_mode(owner, mode)` bumps the mode-epoch and atomically requeues that owner's in-flight `CLAIMED` rows,
  with stale-epoch confirms rejected afterwards (`store.py:599-626`).
- It is exposed as an admin RPC at runtime (`server.py:1607-1618`).
- Reverting an owner to defaults is a designed operation (`reset_delivery_state`, `store.py:650`).

So `BROADCAST` can be added as a **third value of an existing per-owner enum**, not as a replacement of the
delivery core. The claim path stays intact and stays the default; broadcast is a parallel mode selected per
owner, and rollback is an existing atomic call.

That reduces (b) from "change the semantics of the delivery path for everyone at once" to "enable a new mode
for one owner, watch it, and flip it back with a call that already exists". Blast radius equals the set of
owners flipped.

**A correction to the framing this was raised under:** `ORG_TRANSPORT` is *not* the right lever. It selects
broker vs renga at the transport layer (`transport/descriptor.py:52-56`); dogfooding broadcast under it would
mean running renga, a different system. `flip_mode` is the correct mechanism and is strictly better suited -
finer granularity, runtime-flippable, already epoch-fenced.

**First target: the secretary.** It is the right dogfood owner because a human watches it continuously, so
both failure directions are immediately observable by the one detector that matters; its traffic is reports to
a human, so duplication is harmless (§6.5 does not apply); and it is the only owner with an observer lease
today, so the new mode can be compared against the protection actually in place.

### 6.7 What the decision rests on

Reasons **withdrawn** under review: §6.1, §6.2. Reason **discounted** by reweighting: §6.3(a).

What carries the decision:

1. The status quo fails silently by default for most owners (§4.1) - verified, current, invisible.
2. Broadcast closes a residual that exclusive claim cannot close in-model (§6.4) - a correctness argument,
   unrebutted.
3. Failure direction favours broadcast where a human is the recipient (§5).
4. The one countervailing argument (§6.5) is owner-specific, not model-wide.
5. The residual risk in §6.3(b) is preventable by three known design rules and containable by staging on
   machinery that already exists (§6.6).

Points 1-3 argue for broadcast. Point 4 argues against broadcasting *everything*. Point 5 says the change can
be made without betting the system on it. The conjunction is the staged per-owner decision in §1.

**What would make this wrong:** if adding a third mode turns out to entangle rather than parallel the claim
path - i.e. if `BROADCAST` cannot be added without changing `poll_claims`/`confirm_delivered` behaviour for
owners still in `PUSH` - then §6.6's containment argument fails and the risk in §6.3(b) returns at full
weight. **Validate that first**, before any owner is flipped (§8 item 0).

---

## 7. What this does not fix

1. **The identity problem is untouched by the mode choice.** mcp-config is replayable and carries both
   credentials; process env is not. That asymmetry is an assumption about Claude Code's fork/resume behaviour
   which we neither control nor test against.
2. **The pull path stays unfenced** until §8 item 5 lands, under either mode (§6.2).
3. **Receiver-side dedupe still does not exist** (§4.2). Broadcast makes it matter more, since same-session
   redelivery becomes more likely, not less.
4. **`_stood_down` is a latch with no `clear()`** (`channel_sidecar.py:86`).
5. **`poll_claims` renews and activates an armed lease for whatever instance is current-generation**
   (`store.py:485-487`) without checking it ever presented the secret.
6. **`duplicate_sidecar_detected` has no consumer** (`store.py:203-235`, `:466-468`). Detection without a
   consumer is not observability - though note that broadcast makes this less load-bearing, since the
   duplicate delivery itself becomes the operator-visible signal.

---

## 8. Work items (not in this PR)

Ordered. Each filed separately.

0. **Validate the containment assumption** (§6.7): confirm `BROADCAST` can be added as a parallel per-owner
   mode without altering behaviour for owners in `PUSH`. This gates everything below.
1. **Implement `BROADCAST` mode** with the three design rules from §6.3 as explicit invariants: never retire
   on a single ack; on liveness ambiguity offer anyway; scope pull suppression to the calling session.
2. **Implement receiver-side `msg_id` dedupe** (§4.2). Currently a promise; broadcast makes it load-bearing.
3. **Enable for the secretary owner and observe.** Success criteria in §9.
4. **Live-incumbent guard on last-register-wins** (`store.py:419-422`, constraints in §8.1). Still worth
   landing: it improves every owner that remains on exclusive claim, which under this decision is most of
   them.
5. **Track the pull / full-token identity door** (§4.4). Not a delivery-model question.
6. **Rework `_stood_down` into a recoverable state** (§7.4). Note the trap: a naive periodic re-register
   converts the observer lease from a permanent fence into a TTL-delayed fork takeover, which is worse. Do not
   land this without item 4.
7. **Resolve the at-most-once question for DELEGATE traffic** (§6.5). This gates any extension beyond the
   operator-facing owner.

### 8.1 Design constraints on the live-incumbent guard

Three constraints, each derived from a specific code path:

**Scope it to the no-lease branch.** The last-register-wins branch (`store.py:419-422`) is reached both when
there is no active lease *and* when there is an active lease and the caller presented the correct secret
(`store.py:412-418` falls through on a match). A guard placed unconditionally there would also fence the
*legitimate* secretary sidecar on restart. The observer secret is non-replayable proof; incumbency is
circumstantial. **Proof beats incumbency:** apply the guard only when `lease is None`.

**Use a new error code; never reuse `unobserved`.** The sidecar latches `_stood_down` on exactly two codes,
`suppressed_bg_hosted` and `unobserved` (`channel_sidecar.py:191-198`), and that latch has no `clear()`.
Rejecting with `unobserved` would mute a legitimate restart *permanently*. With a distinct non-latching code
the push loop retries about once a second while `_current_generation()` is `None`
(`channel_sidecar.py:266-276`) and recovers once the incumbent ages out.

**Key the guard on the current instance, not on "any recent poller".** `_note_poll_locked` runs *before* the
fence, deliberately, so stale-generation polls still produce a duplicate signal (`store.py:466-468`), and a
fenced fork keeps polling forever (`channel_sidecar.py:279-286`). A guard treating any recent poller as an
incumbent would let a *rejected fork* block the original's legitimate re-registration. Test specifically
whether `_delivery_instances[owner]` has polled recently.

**Apply the staleness threshold inside the guard.** The governing window is `lease_seconds` (default 30s), not
`observer_lease_seconds` (default 90s): `_delivery_poll_seen` is pruned with `window = self.lease_seconds`
(`store.py:215-221`). But that pruning runs only inside `_note_poll_locked`, reached only from `poll_claims`
(`store.py:468`) - **registration never prunes**. A guard testing for mere *presence* would block a
replacement forever. Compare the recorded timestamp against `lease_seconds` explicitly.

---

## 9. Conditions to extend, and to roll back

**Extend broadcast to further owners when:**

1. The secretary owner has run in `BROADCAST` for a sustained period with no observed row loss, and observed
   duplicates are attributable to real fork/resume events rather than to liveness misjudgement.
2. The at-most-once question in §6.5 is ruled, and the owner's traffic is on the tolerant side of the ruling.

**Roll back to exclusive claim (via `flip_mode`) if:**

1. Any row is observed reaching *no* live instance - the §6.3(b) failure. This is the stop condition; silence
   introduced by the new mode is strictly worse than the silence it replaces, because it would be novel and
   unmonitored.
2. Queue growth becomes unbounded because retirement never fires (the safe-side failure of design rule 1),
   and per-instance cursors do not resolve it.
3. The host is shown to coalesce or drop duplicate `notifications/claude/channel` frames, which would mean
   the operator-visibility premise in §5 is false and broadcast's main advantage does not exist.

**Reconsider the whole decision if** the fork/resume inheritance assumption breaks - Claude Code begins
propagating process env across resume, or stops replaying mcp-config verbatim (§7.1).

---

## 10. Dissent and decision history

### 10.1 The independent evaluation

Three independent judges scored the options on separate lenses. Two recommended **switch**; one recommended
**keep** on migration-cost grounds. An adversarial reviewer assigned to refute each option marked **keep
disqualified**, **hybrid disqualified**, and **switch viable-with-work**.

- *Operator-safety* (switch): today's failure is silence that survives the operator's own recovery attempt;
  under broadcast the duplicate *is* the fault report, delivered where the operator already looks.
- *Correctness-under-fork* (switch): keep is "silently wrong by default for most owners"; broadcast is the
  only option that stops asking a question no replayable credential can answer.
- *Implementation-cost* (keep): ~44 of 77 tests rewritten in an incident-prone path, with no testability gain.

The **hybrid rejected there** was contention-triggered: an owner degraded to fan-out when the daemon detected
two live pollers within a 5-second window. It fell to a timing argument - the sidecar's own daemon POST
timeout is exactly 5 seconds (`channel_sidecar.py:164`), so a single hung request can exceed the whole
detection window - and to needing contested-mode per-row state. The decision in §1 is **static per-owner
policy** and shares neither property.

### 10.2 How this decision moved

Recorded so a reader can follow why it reversed.

1. **First derivation: keep**, on three grounds - broadcast regresses the protected owner (§6.1), switching
   does not close #162 (§6.2), and migration cost (§6.3). Ratified on that basis.
2. **§6.1 withdrawn** under adversarial review: the protection is push-only, the harm was a drafting defect,
   and the confidentiality fallback fails because the same file already carries the full token.
3. **§6.2 withdrawn** under adversarial review: the unfenced pull path is common to both models and cannot
   distinguish them; the asymmetry runs the other way.
4. **§6.3 split and (a) discounted** on reweighting: migration *labour* is not a scarce resource when the
   work is performed by an agent. What remains is (b), the risk of introducing new silence into a path whose
   failures humans cannot detect.
5. **Re-derived: staged per-owner broadcast**, once (b) proved containable on existing per-owner mode
   machinery (§6.6) and the only model-wide objection proved to be owner-specific (§6.5).

Two observations a future reader should weigh:

**Every review finding ran in the same direction.** Across four rounds, adversarial review produced one P1 and
five P2 findings. All six were corrections of arguments overstated *in favour of keeping* - the conclusion the
note was arguing at the time. None was an error in the opposite direction. A systematic one-way bias in the
supporting arguments is itself evidence about how much the original conclusion was being reasoned toward
rather than derived, and it was treated as such here.

**The reversal is not itself proof of correctness.** The same discipline applies in the new direction: §6.5 is
a real limit on broadcast, and it is the reason this decision is staged and per-owner rather than a switch. If
§8 item 0 shows the mode cannot be added in parallel, the derivation in §6.7 fails at point 5 and should be
re-run - not patched.
