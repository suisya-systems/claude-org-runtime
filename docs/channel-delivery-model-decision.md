# Channel delivery model: owner-scoped exclusive claim vs broadcast + dedupe

**Status:** Decided, after two reversals (§10.2). §6.5.3 / §7 / §8 refreshed 2026-08 against the shipped code.
**Decision:** Keep the owner-scoped exclusive claim. Harden it with identity work (§8). Do not adopt broadcast.
**Refs:** #162 (this decision), #125 (generation/instance fencing), #129 (observer lease, bg-hosted marker),
#165 (§8 item 1), #170 (item 3), #171 (item 5), #166 (item 2, the adopt path).
**Scope:** The decision itself shipped no behavior change. §7 and §8 are **living status** and are updated as
each item lands - a stale item list here is worse than none, because these sections are read as the design
rationale for the next change.

---

## 1. Decision

Keep exclusive claim as the delivery model. Close the failure class in §3 with **identity** work rather than by
changing how rows are handed out.

The basis is narrow and deliberate: **exclusive claim caps the number of acting control-plane actors at one,
and that cap is presupposed by this organisation's stated invariants** - one worker to one task to one scope,
the multi-step `pending_decisions` register update, name uniqueness at spawn. Broadcast removes the cap by
construction: every registered instance is entitled to act. Removing it is an *organisational* design change,
not a broker design change, and a note about the broker's delivery path is the wrong place to make it.

Note what this decision is **not** based on:

- Not on broadcast introducing a novel hazard. §6.2 shows the comparison is genuinely mixed by branch.
- Not on migration cost. Labour is discounted in both directions (§6.4).
- Not on the claim that duplication is cheap because a human reads the channel. That premise was refuted
  (§5) - every owner here is an agent that acts on injection.

"Keep" does **not** mean "keep as-is". The current implementation is silently broken by default for most
owners (§4.1), and the identity holes reach past delivery into bind and session ownership (§4.5). The work in
§8 is required, and it is the substance of this decision rather than an afterthought to it.

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

Read in the source, not inferred. Several premises did not survive.

### 4.1 "This works today" is false for the majority of owners

The observer lease is **opt-in and almost never on**. It is asserted only when a caller explicitly passes
`observer: True` (`server.py:278-283`, `:318-319`), which in practice is only `org up`'s secretary.
`spawn_claude` issues a delivery credential and attaches the channel sidecar but **never asserts a lease**.

For every worker and dispatcher pane, `register_delivery_instance` therefore falls through to
**last-register-wins** (`store.py:419-422`). A fork's register *deterministically* fences the original, which
then receives `stale_sidecar` on every poll and deliberately never re-registers, because re-registering would
start a generation war (`channel_sidecar.py:279-286`). It polls forever and emits nothing.

Both fallbacks are disarmed in this state:

- `drain()` returns only `UNDELIVERED` rows, so during the fork's claim window the operator's own
  `check_messages` returns `[]`.
- `_nudge_worker`'s pending predicate counts `UNDELIVERED` **or** `CLAIMED` rows (`server.py:380-383`). During
  the claim window it fires a nudge for an inbox that reads empty; once the fork confirms, the row is
  `DELIVERED`, `pending` goes false, and the nudge stops entirely.

Silence that survives the operator's own recovery attempt, leaving no symptom where a human looks. It is the
**default** for `spawn_claude`-spawned owners.

### 4.2 Broker has no receiver-side dedupe

The sidecar keeps no seen-set. `msg_id` is written exactly once - into the notification meta
(`channel_sidecar.py:148`) - and never read anywhere in `src/`. Every other occurrence is a docstring or a log
string. Dedupe is a delegated promise to the host, and nothing here can verify the host honours it.

### 4.3 "Broadcast + dedupe" is two answers to two different problems

- `msg_id` dedupe addresses **redelivery to the same session** (lease expiry, epoch flip).
- The duplication broadcast introduces is delivery to a **different session**, which receiver-side dedupe
  would not suppress - and must not, since that second copy is the entire point.

Dedupe is per-process, so it cannot coordinate two sessions. Cross-session duplicate *action* is not
addressable by dedupe at all.

### 4.4 The PULL path is unfenced

`drain()` takes only an `AgentBind` - no generation, no instance id, no observer check (`store.py:343-361`) -
and a fork replays the **full agent token**, since `mcp_config_for` embeds it in static headers
(`tokens.py:192-205`). A forked session's `check_messages` destructively consumes the operator's rows with
zero fencing. This is the #162 failure with no sidecar involved, and it is independent of delivery model.

### 4.5 The identity holes reach past delivery into bind and session ownership

Bind identity is keyed on **token**, not session, and a fork replays the token. Two consequences, both
verified, neither touched by any delivery model:

- **Session steal.** `initialize` mints a fresh `session_id` and writes it onto the *shared* bind, setting
  `registered = True` (`server.py:1802-1806`). When a fork initializes, it overwrites the original's session.
  The original's next `tools/call` fails `[session_invalid]` (`server.py:1789`) - the original is evicted from
  its own bind by a sibling.
- **DELETE collateral.** A fork's clean shutdown sets `bind.session_id = None` and `bind.registered = False`
  (`server.py:1560`, `:1563`). `enqueue` only resolves to `registered` binds (`store.py:311-317`), so
  `send_message` addressed to a **still-live** original returns `[peer_not_found]`. A sibling's orderly exit
  silently un-addresses a running agent.

These support the central thesis more strongly than the delivery analysis does: **the problem is identity, and
no choice of delivery model reaches it.**

### 4.6 renga's shape does not port

- The bus is **in-process**: `EventBus::emit` clones into a per-subscriber bounded `sync_channel`
  (`CHANNEL_CAPACITY = 256`, `src/ipc/events.rs:34`, `:89-122`). Broker's sidecars are separate processes
  polling over HTTP.
- It is explicitly **best-effort**: a slow subscriber has events **dropped**, signalled by a synthetic
  `EventsDropped` (`events.rs:113-120`). The module documents itself as "not a reliable replication source".
- **No persistence, no ack.** If no subscriber is live at emit time the message evaporates; there is no
  analogue of `/confirm-delivered`.
- **No message id in the wire type** (`src/ipc/mod.rs:941-956`), so receiver-side dedupe is impossible there.
  The receiver relies solely on the pane filter (`src/mcp_peer/mod.rs:3136-3143`), keyed on `RENGA_PANE_ID`
  from process env - structurally non-replayable in the way broker's credential is not.

Note also, correcting an earlier draft: broker's advantage over renga is **per-row delivery state, not
durability**. `_rows` is in-memory and the journal is never replayed (`store.py:325-332`). A daemon restart
discards every undelivered row (§7.6).

---

## 5. Failure direction, and the premise that failed

The original framing: exclusive claim fails toward **silence** (invisible, unbounded), broadcast fails toward
**duplication** (visible, self-limiting), and for an operator-facing report channel duplication is safer.

**The second half of that premise is false here.** This channel is not a display surface. Its purpose is
*in-band injection that wakes an idle session* (`channel_sidecar.py` module docstring), and every owner on
this queue is a Claude agent that acts on what it receives - the secretary decomposes and delegates
(`prompts/templates/secretary.md`), the dispatcher receives `DELEGATE` and spawns worker panes
(`prompts/templates/dispatcher.md:17,23`). There is no owner whose traffic is merely read.

So "duplication" here means **duplicate agent action**, not a duplicate line of text. The failure-direction
argument does not transfer from report channels to control planes, and it is not a carrying reason in this
decision.

What remains true and is retained: silence is the worse *diagnostic* failure, because nothing signals that
anything is owed. That is why §8 exists and why observability (§8 item 3) is part of it.

---

## 6. Why exclusive claim

### 6.1 The actor cap is load-bearing for the organisation, not just for delivery

Exclusive claim's relevant property is not "exactly-once delivery" - and, correcting the first statement of
this section, it is **not** a guarantee that at most one actor acts on a message either. Delivery is
at-least-once by construction, and there is a real window in which two sessions act on the same row:

1. Sidecar A claims row R and emits it (`channel_sidecar.py:290`). Session A is woken and acts.
2. Before A confirms, a new instance registers. `register_delivery_instance` requeues A's still-`CLAIMED` row
   to `UNDELIVERED` (`store.py:424-428`), and A's later confirm is rejected as stale (`store.py:551-567`).
3. Sidecar B claims R and emits it. Session B is woken and acts.

With no receiver-side dedupe (§4.2), and none possible across processes (§4.3), both sessions act. **The
current design already violates a strict at-most-once reading of the organisation's invariants**, in a window
roughly one poll interval wide.

What exclusive claim actually provides is that **duplicate action is a bounded race, not a design property.**
It is confined to the emit-to-requeue window, it is per-message, and it does not scale with the number of
replayed instances. Broadcast makes duplicate action the steady state: entitlement to a copy is entitlement to
act, and acting sessions scale with replays.

**That difference is the basis for this decision.** Moving an exposure from "rare, bounded, and arguably a
bug" to "always, unbounded, and by design" is an organisational change rather than a broker one, and a
delivery-path note is the wrong place to make it.

What this correction costs, stated plainly: the ruling that selected this option was expressed in terms of a
cap at one actor, and that cap does not exist as an absolute. What survives is the distinction between a
bounded race and a design property - weaker, but real. And if the organisation's invariants do require strict
at-most-once action, they are **already** violated today (§7.8), which is a defect to fix rather than a
property to preserve.

### 6.2 The branch analysis, corrected

An earlier draft of this note argued broadcast adds no spurious actor, on this table:

| fork scenario | spurious | legitimate | operator sees |
|---|---|---|---|
| exclusive claim | 1 (fork) | 0 | nothing |
| broadcast | 1 (fork) | 1 | the message |

**That was arithmetic over a single branch**, and the branch chosen was the one most favourable to broadcast:
an *accidental* fork, live, singular. Enumerated properly:

| branch | exclusive claim | broadcast |
|---|---|---|
| accidental fork, original live | 1 spurious, 0 legitimate | 1 spurious, 1 legitimate |
| **intentional resume, old pane abandoned** | **0 spurious, 1 legitimate** | **1 spurious, 1 legitimate** |
| N replayed instances | 1 actor in steady state | acting sessions = N |

These counts are **steady-state**, not guarantees. Per §6.1 there is a race window in which exclusive claim
also produces two acting sessions; it is bounded per message and does not scale with N, which is the
distinction that matters here.

The middle row is the common shape in this organisation - a human opens a new pane, resumes, and leaves the
old one running. There, last-register-wins happens to select the *new* session, so exclusive claim is simply
correct, and broadcast **adds** a spurious actor by waking the abandoned pane. The bottom row is the general
case: exclusive claim caps actors at one; broadcast scales spurious actors with the number of replays.

The original table also rested on an assumption this note had already abandoned elsewhere: that we can tell
which session the human is watching. If we could, the whole problem would be solved.

### 6.3 Arguments that did not survive review

Recorded so they are not raised again.

**"Broadcast regresses the one protected owner."** Withdrawn. The observer lease protects only the push path
(§4.4 shows pull is open), the harm attributed to broadcast came from a drafting defect rather than the model,
and the confidentiality form fails because `secretary-mcp.json` already carries the **full agent token**
(`server.py:296`, `tokens.py:192-205`), which grants destructive queue read via `check_messages`. Broadcast
would add a route to data the file holder can already read.

**"Switching does not close #162."** Withdrawn. True but not distinguishing: the unfenced pull path is common
to both models, and keeping defers it too (§8 item 4). The asymmetry runs the other way, since a broadcast
migration would be forced to confront pull reconciliation.

**"Duplication is cheap because a human reads the channel."** Refuted in §5.

### 6.4 Cost is not a reason, in either direction

Migration labour is discounted: the work is performed by an agent, and preserving a broken default to avoid
keystrokes is not defensible.

**This cuts both ways.** Discounting broadcast's ~44-of-77 test rewrite equally discounts the labour of
extending the observer lease to every spawned pane (§8 item 1). Neither side gets a cost argument, and the
decision rests entirely on which failure model is right.

What is *not* discounted is the risk of changing semantics in a path whose failures are silent, since the
scarce resource is the human attention needed to notice silence. But that is a reason for care in
implementation, not a reason to prefer one model.

### 6.5 What this decision does not buy

Stated plainly, because a decision recorded without its weaknesses is not usable.

1. **It depends on an unverifiable external assumption.** The observer fence works because mcp-config is
   replayed on fork/resume and process env is not. We neither control nor test that behaviour. If it changes,
   the fence **dissolves silently** - a design chosen to defeat silence would fail in exactly that shape. The
   mitigating point: the current observer lease already depends on this, so §8 adds no new exposure, only
   more reliance on an existing bet.
2. **The pull door narrows but does not close.** The full token is in the persisted config *by design*: HTTP
   MCP can only carry static headers (`tokens.py:192-205`), so there is no `${VAR}` indirection available.
   Item 4 can reduce the window; it cannot reach zero without changing how the token is transported.
3. **Adoption is discipline-dependent.** ~~A legitimate session started by hand (`claude --resume`) carries no
   observer secret, so it registers `unobserved`, sets `_stood_down`, and - because that latch has no
   `clear()` - **never receives push for the life of the process**.~~

   **Superseded by #171 and #166 (updated 2026-08).** The refusal is now split by whether it can ever be
   reversed (`store.py` `LATCHING_REFUSALS`). A session presenting *no* secret gets `observer_pending`, which
   is **non-latching**: the sidecar keeps retrying at poll cadence and takes over the moment the incumbent's
   lease is released. Only a session presenting a *stale* secret - one actually superseded by a rotate - gets
   the latching `unobserved`. So a hand-started `claude --resume` is no longer permanently mute; it is queued
   behind the incumbent.

   What remains true is the shape of the concern: waiting behind an incumbent is not the same as taking over
   from one, and the incumbent's lease does not expire on heartbeat loss (§7.5 note). **Item 2 (#166) is what
   closes the gap**, by making the takeover an explicit operation rather than something the operator has to
   provoke. See §8 item 2 for what shipped.

---

## 7. What keeping does not fix

1. **Identity, not delivery, is the root** (§4.5). Session steal and DELETE collateral remain until item 4.
2. **The pull path stays unfenced** until item 4 (§4.4).
3. **Receiver-side dedupe still does not exist** (§4.2).
4. ~~**`_stood_down` is an unrecoverable latch** - item 5.~~ **Fixed in #171.** The latch now applies only to
   refusals that can never be reversed (`suppressed_bg_hosted`, `unobserved`); `observer_pending` is retried.
   The sidecar keeps its own copy of that table and treats **unknown codes as non-latching**, so a version
   skew fails toward retrying rather than toward silence.
5. ~~**`poll_claims` renews and activates an armed lease for whatever instance is current-generation**
   without checking it ever presented the secret.~~ **Fixed in #171.** `poll_claims` renews only a lease that
   is already activated (`expires_at is not None`); activation stayed the exclusive privilege of a register
   that presented the secret. Otherwise an instance that never held the secret could activate the lease and
   silently void the arming deadline that §8 item 1 depends on.
6. **There is no durability.** `_rows` is in-memory, the journal is never replayed. A daemon restart drops
   every undelivered row silently. Neither model addresses this. **Still open.**
7. ~~**`duplicate_sidecar_detected` has no consumer** - item 3.~~ **Fixed in #170**, and extended in #166:
   the attention watcher now also consumes `delivery_register_superseded` and `delivery_adopt_expired`
   (`attention/readers.py`, `attention/classifier.py`), so a session going mute and a failed handover both
   reach the operator instead of only appearing in an admin-only dump.
8. **Duplicate agent action is already reachable today** (§6.1): a row emitted but not yet confirmed is
   requeued when a new instance registers (`store.py:424-428`), so two sessions can be woken by the same
   message. This is inherent to emitting before confirming and is not fixed by keeping. If strict
   at-most-once action is required, it needs its own item - and receiver-side dedupe (§4.2) does not solve it,
   since the two sessions are different processes.

---

## 8. Work items

In the order ruled. Each filed separately. Status updated 2026-08.

1. **Extend the observer lease to the `spawn_claude` path.** - **Done (#165).** This closed §4.1, the
   default-silence path for every spawned pane. `_adapter_spawn` already carried an env-injection route, so it
   repeated for spawned panes what `launcher.py` does for the secretary. Because the secret's arrival is
   backend-dependent, the lease is armed with a finite activation deadline: if nobody ever presents it, the
   lease is dropped rather than muting the owner forever.
2. **An explicit adopt / handover path.** - **Done (#166), and it is what this section's caveats were
   waiting on.** Three decisions are worth recording, because each rules out an obvious-looking alternative:

   *Adopt is a launcher, not a message to a running session.* The secret's whole power comes from riding in
   process env, which fork/resume does not replay. A running process's env cannot be rewritten from outside,
   so there is no way to hand a secret to a session that is already up. The alternative - an authenticated
   dynamic handoff channel in the sidecar - was rejected because a forked sidecar could call it too: it would
   delete the exact asymmetry the lease is built on. So `org adopt` rotates the lease and **starts a new
   claude process** holding the new secret, with `--resume` / `--continue` carrying the conversation across.

   *The handover boundary is the fence, not the RPC.* Rotating alone does not stop the incumbent: the old
   `(generation, instance_id)` stays current and `poll_claims` never re-checks the secret, so the old session
   would keep claiming until the new one registered. The adopt RPC therefore bumps the generation **and
   clears the registered instance** in the same lock scope, which fences every caller (`poll_claims` compares
   against a now-absent instance). Between that moment and the adopting sidecar's register, nobody delivers.

   *Issuing the secret is not success.* Because adopt fences first, a failed launch would leave an owner with
   no claimer at all - and a fenced sidecar never re-registers, so that state would be permanent. The adopt
   therefore carries an adoption id and a finite arming deadline; if no adopting sidecar registers in time the
   daemon **restores the previous generation and instance** (compare-and-restore) and journals
   `delivery_adopt_expired`, which the attention watcher reports. A concurrent adopt is rejected rather than
   silently winning, since last-rotate-wins would hand the earlier operator a success for a session that could
   never deliver.

   The in-flight question from §7.8 is answered by **not** answering it in the daemon: `--in-flight
   requeue|drop` is an operator choice, defaulting to `requeue` to match the existing at-least-once posture,
   with the count and the chosen policy recorded in both the response and the journal.
3. **Give `duplicate_sidecar_detected` a consumer.** - **Done (#170)**, extended by #166 (§7.7).
4. **Pull door and shared-bind identity** (§4.4, §4.5): session steal on `initialize`, DELETE collateral on a
   sibling's exit, and destructive `drain()` under a replayed token. Hardest, and separate - this is the root
   cause, not a delivery concern. **Still open.**
5. **Make `_stood_down` recoverable** (§6.5.3). - **Done (#171).** The trap named here was real and was
   avoided: recovery is *not* a periodic re-register (which would have converted the lease into a TTL-delayed
   fork takeover). Instead the refusal was split, so only the never-reversible cases latch.

A **live-incumbent guard** on the last-register-wins branch (`store.py:419-422`) was evaluated as an
alternative to item 1. It is not in the ruled order because item 1 supersedes it where the lease is present,
but it remains the fallback if lease extension proves unstable, and its design constraints are non-obvious
enough to record - see §8.1.

### 8.1 Design constraints on the live-incumbent guard (if it is ever built)

**Scope it to the no-lease branch.** The last-register-wins branch is reached both when no lease is active
*and* when a lease is active and the caller presented the correct secret (`store.py:412-418` falls through on
a match). An unconditional guard would fence the *legitimate* secretary sidecar on restart. Proof beats
incumbency: apply only when `lease is None`.

**Use a new error code; never reuse `unobserved`.** The sidecar latches `_stood_down` on exactly two codes
(`channel_sidecar.py:191-198`), and the latch has no `clear()`. Rejecting with `unobserved` would mute a
legitimate restart permanently. A distinct non-latching code lets the push loop retry about once a second
while `_current_generation()` is `None` (`channel_sidecar.py:266-276`).

**Key on the current instance, not "any recent poller".** `_note_poll_locked` runs *before* the fence,
deliberately, so stale-generation polls still register (`store.py:466-468`), and a fenced fork keeps polling
forever (`channel_sidecar.py:279-286`). A guard treating any recent poller as incumbent would let a rejected
fork block the original's legitimate re-registration.

**Apply the staleness threshold inside the guard.** The governing window is `lease_seconds` (default 30s), not
`observer_lease_seconds` (default 90s): `_delivery_poll_seen` is pruned with `window = self.lease_seconds`
(`store.py:215-221`). But that pruning runs only inside `_note_poll_locked`, reached only from `poll_claims`
(`store.py:468`) - **registration never prunes**. A guard testing for mere presence would block a replacement
forever.

---

## 9. Conditions to revisit

Reopen the delivery-model question if:

1. **The organisation's actor-count invariant changes.** If one-worker-one-task and the register's multi-step
   update stop being assumed, §6.1's basis dissolves and broadcast becomes a live option. This is the
   condition that actually governs - the others are secondary.
2. **The fork/resume inheritance assumption breaks** (§6.5.1): Claude Code begins propagating process env
   across resume, or stops replaying mcp-config verbatim. The observer fence would fail silently, and the
   decision would rest on nothing.
3. **Items 1-3 land and §4.1 still fires** in real `queue.jsonl` data. That would mean identity work cannot
   close the failure class in-model, which is the premise this decision assumes.
4. **A per-row delivery policy becomes necessary** - e.g. if some traffic is established as idempotent and
   some not. That points at policy set at enqueue rather than a global model, which neither option here
   evaluated.

---

## 10. Dissent and decision history

### 10.1 The independent evaluation

Three judges scored the options on separate lenses: two recommended **switch**, one **keep** on migration-cost
grounds. An adversarial reviewer marked **keep disqualified**, **hybrid disqualified**, **switch
viable-with-work**.

The majority is not followed. Its two switch votes rested on the failure-direction argument (refuted for a
control plane in §5) and on keep being "silently wrong by default" (true, §4.1, and addressed by §8 rather
than by changing models). The keep vote rested on migration cost, which is discounted (§6.4) - so this
decision agrees with the minority's conclusion while rejecting its reasoning.

The **hybrid rejected there** was contention-triggered: degrade an owner to fan-out when the daemon sees two
live pollers within a 5-second window. It fell to timing - the sidecar's own daemon POST timeout is exactly 5
seconds (`channel_sidecar.py:164`) - and to needing contested-mode per-row state.

### 10.2 How this decision moved

Recorded so a reader can follow two reversals.

1. **First derivation: keep**, on three grounds - broadcast regresses the protected owner, switching does not
   close #162, migration cost. Ratified on that basis.
2. **Two grounds withdrawn** under adversarial review (§6.3), leaving only cost.
3. **Reweighted**: migration *labour* discounted, since the work is performed by an agent. With the sole
   surviving ground discounted, the decision was **re-derived as staged per-owner broadcast**.
4. **That derivation's premise was refuted**: it assumed the operator-facing owner was exempt because "a human
   reads it". The channel wakes agents; the secretary delegates; `DELEGATE` lands on the dispatcher, which
   spawns panes. No owner is exempt (§5).
5. **Second opinion identified the remaining error**: the fork comparison table was arithmetic over the single
   branch most favourable to broadcast. Across branches, exclusive claim caps actors at one and broadcast
   scales spurious actors with replays (§6.2).
6. **Final: keep exclusive claim, harden identity** - on the narrow ground that the actor cap is an
   organisational invariant and not the broker's to remove (§6.1).

The staged-rollout plan from step 3 fell with it, and deserves its own epitaph: it nominated as first target
the owner where broadcast's benefit was *smallest* (the secretary, already lease-protected) and sequenced last
the owner where the benefit was real (the dispatcher, where §4.1 actually fires). A plan that defers its only
load-bearing step indefinitely is a decision not to act, written as a decision to act.

### 10.3 On the reliability of this document

Across six review rounds, adversarial review produced ten findings (two P1, eight P2). **Every one was an
overclaim in favour of whatever conclusion the note was arguing at the time.** When the conclusion reversed at
step 3, the direction of the bias reversed with it - six findings had favoured keeping, the next three
favoured broadcast, and the tenth favoured keeping again once the decision returned there. None ran against
the then-current position, in six consecutive rounds spanning two reversals.

The tenth is the sharpest instance, because it struck the ruling's own stated basis: §6.1 originally claimed
exclusive claim caps acting actors at one. It does not - delivery is at-least-once and there is a real
duplicate-action window (§6.1, §7.8). The decision survived on a corrected and weaker basis, but the ground it
was selected on had to be restated after selection.

Two things follow, and a reader should weigh both:

- The bias is not a preference for an answer; it is a pull toward the answer currently held. Immediately
  before the round that produced the three pro-broadcast findings, an explicit four-point self-check against
  exactly that inversion was written and reported. All three findings passed through it. Self-review cannot
  detect a tilt in the frame it is conducted from.
- **The conclusion is therefore better evidenced than the reasoning that reached it.** Keep survived both a
  hostile derivation and a reversal, and the ground it finally rests on (§6.1) came from outside this note. A
  reader should trust §4 (verified facts) most, §6.1 next, and treat the rest as reasoning that has repeatedly
  needed correction.
