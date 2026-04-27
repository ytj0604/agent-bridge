# Known Issues

Tracker for design and implementation issues observed in v1.5.x. Each
entry: status, symptom, root cause, current mitigation/fix, residual
risk. Resolved entries stay listed (with their fix) so the lineage is
visible in one place.

---

## I-01: claude `response_finished` auto-routes indefinitely until next `prompt_submitted`

**Status**: partially fixed in v1.5.2 (consume-once + state-based delivery
matching). Residual: model-mental-confusion variant — claude addresses
"the user" inside a reply that the bridge correctly routes to a peer.

### Symptom

A peer (e.g. `codex2`) sends a `kind=request` to claude. While claude is
processing (or before claude reads the bridge prompt), the human types
unrelated messages in claude's pane and claude replies to those. The
peer then receives one of claude's replies as `kind=result` for its
original request, even though claude was answering the human, not the
peer.

Originally reported during v1.5 testing as "codex2 received claude's
status update about daemon restart timing as the auto-routed result of
`fresh trigger G2.1`". Subsequent investigation of the events log showed
only a single `prompt_submitted`/`response_finished` pair in the leak
window — i.e. the bridge routed correctly per protocol, but claude's
reply *content* was addressed to "the user" because the bridge inject
arrives in the same pane channel as user typing and the model can lose
track of who it is replying to. See the residual section below.

### Root cause (the routing-state half — fixed in v1.5.2)

Daemon tracks the most recent prompt context per agent in
`self.current_prompt_by_agent[agent]`. Pre-v1.5.2 the slot was set in
`handle_prompt_submitted` and only overwritten by the next
`prompt_submitted`; it was never explicitly cleared after a
`response_finished` consumed it. Effects:

1. After a peer-bridge prompt fired `prompt_submitted` (auto_return=True),
   every subsequent `response_finished` on that pane would route to the
   peer until another `prompt_submitted` overwrote the slot.
2. Some intervening prompts to claude do not fire `UserPromptSubmit`
   (system reminders, harness-injected prompts, certain notification
   types). These did not reset the slot.

Codex peers were less exposed because codex hooks include `turn_id`,
which the daemon uses to discard stale `response_finished` events. Claude
hooks do not include `turn_id`, so claude has no analogous defense.

### What v1.5.2 fixes

- **consume-once**: `handle_response_finished` pops
  `current_prompt_by_agent[sender]` at the end of the normal terminal
  path (regardless of whether routing actually emitted a reply).
  Held-drain and `turn_id_mismatch` branches intentionally skip the pop —
  those paths leave ctx untouched. A peer request now gets at most one
  auto-routed reply.
- **state-based delivery matching**: `handle_prompt_submitted` no longer
  trusts the hook-extracted nonce alone. It calls
  `find_inflight_candidate(agent)` (reserved + queue scan, both filtered
  by `to=agent && status=inflight`), then cross-checks the candidate's
  nonce against the observed nonce. Mismatch, missing observed nonce,
  or no candidate are all fail-closed (`nonce_mismatch`,
  `nonce_missing_for_candidate`, `orphan_nonce_in_user_prompt` log
  events; no delivery state mutation).
- **anchored regex**: `NONCE_PATTERN.match(text.lstrip())` only matches
  when `[bridge:<nonce>]` is the first token, eliminating the
  quoted-transcript false positive at the hook layer (see I-04).
- **orphan nonce never enters ctx**: `current_prompt_by_agent[agent]`
  receives `nonce` only from the matched message, so a stale nonce in
  user-typed text cannot taint `discard_nonce` cleanup at terminal time.

### Residual: semantic channel confusion (NOT fixed in v1.5.2)

Even with routing correct per protocol, the bridge injects peer prompts
through the same pane channel that the human uses, so the model can
write a reply that *addresses* "the user" while the bridge *routes* it
to the originating peer. This is what the original incident actually
exhibited. Mitigations require either a separate inbox channel or
strong envelope cues that the model reliably honours; both are larger
than v1.5.2 and are tracked for v1.6+.

### Workaround for residual

Avoid letting a peer-bridge prompt sit unanswered while you converse
with claude about other topics. Keep peer requests handled
synchronously, or have the human pause peer interactions while debug
chatting.

---

## I-02: codex sandbox cannot connect to daemon socket; uses file-fallback path

**Status**: mitigated in v1.5 (alarm cancel + body prepend now apply for
fallback ingress).
**Severity**: medium — environmental constraint, not a code bug.

### Symptom

`agent_send_peer` invoked from inside a codex pane may write the message
directly to `pending.json` and `events.raw.jsonl` instead of going through
the unix socket. Current versions keep this successful fallback silent in
the model pane and record an operator-only `enqueue_file_fallback` event
in `events.raw.jsonl`.

### Root cause

Codex's sandbox-exec policy blocks `connect()` on the daemon's unix
socket (`/tmp/agent-bridge-0/run/<session>.sock`). The daemon is
running and the socket exists with mode 0600 (root-owned), but
codex's process cannot reach it. Claude code, with a more permissive
sandbox, can connect normally.

### Mitigation in v1.5

The daemon's `handle_external_message_queued` detects file-fallback
ingress and applies the same `_apply_alarm_cancel_to_queued_message`
helper that the daemon-socket path runs. Both ingress paths funnel
through the same code path, so alarm cancel + body prepend semantics
hold regardless of how the message reached `pending.json`.

To prevent the daemon's periodic `try_deliver` from picking up a
fallback-written message before the alarm-cancel finalize step has
run, fallback writes use a transient `status="ingressing"` instead of
`"pending"`. The daemon promotes it to `"pending"` inside the same
`state_lock`-held step that runs alarm cancel.

### Residual risk

`"ingressing"` items get unblocked along three paths:

1. **Normal happy path** — daemon's tail loop reads the
   `message_queued` event, calls `handle_external_message_queued`,
   runs the finalize helper (`alarm cancel + promote`).
2. **Daemon-down case** — the writer wrote queue.json but the daemon
   was not alive to process the event. Next daemon startup runs
   `_recover_ingressing_messages` to promote any leftovers (alarms
   are in-memory only and lost across restart, so no alarm cancel
   here).
3. **Daemon-alive but event-missed case** — rare: queue write
   succeeded, event append failed, or the follow loop got out of
   sync. The maintenance pass `_promote_aged_ingressing` runs every
   `INGRESSING_CHECK_INTERVAL_SEC` (5s) and promotes any ingressing
   item older than `INGRESSING_AGE_PROMOTE_SEC` (30s). Operators
   see an `ingressing_promoted_aged` event for each promotion. Like
   path 2, this only promotes — no alarm cancel — so an alarm that
   would normally have been cancelled by this incoming message stays
   alive and will fire at its deadline.

---

## I-03: daemon-internal state is in-memory and lost on restart

**Status**: open (acknowledged limitation; v1.5.x added orphan-delivered
recovery and `bridge_daemon_ctl restart`).
**Severity**: low — operator-visible, recoverable.

`held_interrupt`, `watchdogs`, `current_prompt_by_agent`, and
`last_enter_ts` live only in daemon memory. Daemon crash or restart
loses them. Queued messages persist in `queue.json`, and watchdogs
re-arm at delivery time, so messages still flow. But:

- Held peers lose their `held_interrupt` block; new messages may
  deliver before the late Stop event (theoretical misroute window).
- Watchdogs and alarms registered before the crash are gone.
- `current_prompt_by_agent` reset means the first `response_finished`
  after restart has no context (treated as orphan; no auto-route).

### Recovery on restart (v1.5.x)

`bridge_daemon_ctl restart` and any normal daemon-restart codepath now
run two startup sweeps before delivery resumes:

- **Ingressing recovery** (`_recover_ingressing_messages`): items in the
  transient `ingressing` state are promoted back to `pending` so they
  can deliver again.
- **Orphan-delivered recovery** (`_recover_orphan_delivered_messages`):
  items in `delivered` state are removed from the queue. They depended
  on the previous daemon's in-memory `current_prompt_by_agent` to
  terminal-cleanup, and that ctx is gone. The peer's response_finished
  for those messages, if it ever arrives, is treated as a no-context
  user turn — **no auto-route to the original sender**. This unsticks
  the queue (delivered items had been blocking new deliveries) at the
  cost of losing the reply for those in-flight requests.

Operators should treat restart as a "best-effort" operation:

- `restart` without `--force` refuses if any `delivered` items exist;
  this is a preflight check, not a hard guarantee — the live daemon can
  flip an `inflight` to `delivered` between the count read and the
  stop, so a `--force`-less restart can still race into routing loss
  in the worst case. Closing this fully requires a `quiesce` command
  on the daemon, deferred to v1.6+.
- `restart --force` proceeds anyway; the result `warnings` list
  enumerates each affected message count.
- After restart, the original sender does not receive a result for
  any swept message; they can re-issue the request or rely on the
  watchdog/alarm they had set.

### Aggregate caveat

If a swept `delivered` item was an aggregate member (kind=request,
aggregate_id set), the aggregate has lost one of its expected replies.
The daemon does NOT inject a synthetic "interrupted" reply on restart-
sweep (unlike the `agent_interrupt_peer` path, which does inject one).
The aggregate result therefore never auto-completes through that
member's reply. Whether the originating sender ever notices is
best-effort:

- The aggregate's per-message watchdog was kept in daemon memory only,
  so the restart usually loses it. If a remaining (still-pending)
  member is re-delivered, that delivery re-arms a fresh watchdog and
  the wake will eventually fire. If every member was already in
  `delivered` state at restart time, no watchdog re-arms automatically
  and the sender may receive no notification.
- The aggregate state itself lives in `aggregates.json` /
  `events.raw.jsonl`, not in the queue, so an "aggregate parent" queue
  item is not necessarily present after sweep. Inspecting the
  aggregate's progress means checking the aggregate file/log, not the
  queue alone.

### Long-term direction

Persist `held_interrupt`, watchdog, and aggregate state to a small
snapshot file under `state/<session>/` so restart preserves more of
the in-flight context. Coupled with a daemon `quiesce` command this
would close both the routing-loss race above and the aggregate-orphan
gap.

---

## I-04: quoted bridge marker false-positive in nonce extraction (fixed in v1.5.2)

**Status**: fixed in v1.5.2.

### Symptom

A user pasted or quoted past bridge text — for example, copying a peer
pane snapshot into a question for claude — and the hook's
`NONCE_PATTERN` matched the embedded `[bridge:<nonce>]` mid-prompt. The
daemon then treated the user-typed prompt as a peer-prompt delivery
confirmation, binding `current_prompt_by_agent[agent]` to a stale
nonce. Concretely observed in the original I-01 leak session: a user
prompt at line 38 carried a 6-minute-old nonce pulled from quoted text.

### Fix

- Hook regex anchored: `extract_nonce` runs `NONCE_PATTERN.match(text.lstrip())`
  so only a leading `[bridge:<nonce>]` token matches; mid-prompt
  occurrences (typical for quoted bridge transcripts) are ignored.
  `extract_attach_probe` is anchored the same way for consistency.
- `nonce` body is now `[^\]\s]+` to reject embedded whitespace.
- Daemon-side defenses layered on top:
  - `find_inflight_candidate(agent)` requires the candidate to be
    `to=agent` and `status=inflight`.
  - `mark_message_delivered_by_id(agent, msg_id)` re-checks recipient
    and status before flipping state.
  - Observed nonce missing or mismatching the candidate's nonce is
    fail-closed; the candidate stays inflight and a diagnostic event
    is logged (`nonce_missing_for_candidate`, `nonce_mismatch`).
  - Orphan nonce in a user prompt without any candidate is logged as
    `orphan_nonce_in_user_prompt` and never stored in ctx.

### Residual: missing / mismatched nonce stalls inflight briefly

If a user-typing collision causes the observed nonce to be missing or
to mismatch, the candidate stays in `inflight` until
`requeue_stale_inflight()` reverts it to `pending` after `submit_timeout`
seconds (default 30s). The delivery-time watchdog has not armed yet,
so it does not fire in this window.

v1.5.2 mitigates one side-effect of the wait: the fail-closed branches
clear `last_enter_ts` for the candidate, so `retry_enter_for_inflight()`
will not keep sending `Enter` into a pane the human is typing in. After
the requeue, `try_deliver` re-attempts delivery cleanly. While the
candidate is inflight, `busy[agent]` may stay `True` and delay other
queued deliveries to that pane. `agent_interrupt_peer <alias> --status`
shows the queue state.

### Residual hole: matching nonce, contaminated prompt body

v1.5.2 verifies `to == agent`, `status == inflight`, and that the
observed nonce equals the candidate's nonce. It does NOT verify that
the submitted prompt body matches the prompt the daemon actually sent.
Two ways this hole can be reached:

- The bridge inject and a user-typed prefix get concatenated into one
  submission; the prompt still starts with `[bridge:<nonce>]` (so the
  anchored regex matches) and the nonce matches the live candidate,
  but the body is contaminated with extra user content.
- A user pastes content that includes the live candidate's nonce as
  the leading token (rare in practice, but possible during
  copy-paste-heavy debugging).

In both cases the daemon marks the candidate `delivered` and binds
ctx, so the next `response_finished` will auto-route. A
prompt-body-hash cross-check would close this; deferred to v1.6.

---

## I-05: oversized peer bodies silently truncated at delivery (fixed)

**Status**: fixed after v1.5.2.

### Symptom

`agent_send_peer` accepted inline bodies larger than the daemon's
delivery guard. The sender saw a successful message id, but the peer
received only the first 12000 characters plus `[bridge truncated peer body]`.

### Root cause

`bridge_daemon.prompt_body()` defensively caps prompt injection at
12000 characters to keep `tmux send-keys` and model TUI paste handling
stable. `agent_send_peer` and `bridge_enqueue.py` did not enforce the
same limit before queueing.

### Fix

- `bridge_send_peer.py` rejects oversized bodies before spawning
  `bridge_enqueue.py`, avoiding argv/ARG_MAX failures for stdin input.
- `bridge_enqueue.py` enforces the same limit for direct/fallback use.
- External inline sends are capped at 11000 characters, leaving headroom
  for bridge-added notices under the daemon's 12000-character prompt
  guard. The shared limits live in `bridge_util.py`.
- The daemon still keeps the delivery-time truncation guard for legacy
  queued items and internal synthetic messages, but logs `body_truncated`
  when it fires.
- Alarm-cancel notices now shrink or omit their prepended notice rather
  than displacing an at-limit user body.

---

## I-06: stale pane endpoints can receive bridge input after agent exit (fixed)

**Status**: fixed after v1.5.2.

### Symptom

If an attached agent exited or was killed, its tmux pane could later be
reused by a shell or a different agent. Bridge operations such as
`bridge_leave`, room close, peer delivery, retry-Enter, or interrupt
could still type into that pane using stale room state.

### Root cause

Write-side endpoint resolution trusted `pane-locks.json` when no live
hook record was present, and the daemon could fall back to an old
`self.panes[target]` cache after fresh resolution failed. Direct
membership notices also bypassed the daemon queue and wrote to tmux
directly.

### Fix

- Endpoint writes and live reads now require a verified live hook/backfill
  record plus a current process fingerprint (`pid`, `/proc` start time,
  and boot id when available).
- Probe results are tri-state: `verified`, `mismatch`, or `unknown`.
  `unknown` fails closed but does not mutate membership or pane locks.
- Positive mismatches clear stale pane locks and mark the participant
  `endpoint_lost` while keeping the alias visible for operator cleanup.
- Daemon delivery, interrupt, retry-Enter, pane-mode cancel, room-close
  notices, leave notices, daemon capture, and `agent_view_peer` live
  capture use the strict endpoint path.
- Undeliverable auto-return requests complete with a bridge
  `[bridge:undeliverable]` result; aggregate members get a synthetic
  undeliverable aggregate reply instead of hanging forever.
- Daemon startup and `bridge_healthcheck.sh --backfill-endpoints` only
  refresh endpoints that already have prior verified live evidence.
  Fresh normal attach/join hook probes may create the initial
  fingerprint; `--no-probe` requires an existing verified live endpoint
  before it publishes room state.

---

## I-07: Claude submitted-prompt input residue after interrupt (fixed)

**Status**: fixed after v1.5.2.

### Symptom

`agent_interrupt_peer <alias>` sent `Escape` and cancelled bridge state,
but Claude Code could leave the interrupted submitted prompt restored in
the input buffer. The next bridge delivery could then append to that
residual text, preventing clean nonce recognition and leaving the new
message stuck in `inflight`.

Direct tmux key tests showed different model TUI behavior:

- Codex clears a submitted task with `Escape`; its input returns to an
  empty prompt.
- Claude Code restores the submitted prompt into the input field after
  `Escape`; one following `Ctrl-C` clears it and prints Claude's normal
  "press Ctrl-C again to exit" warning without exiting.

### Root cause

The daemon treated `Escape` success as both "turn cancelled" and
"prompt buffer clean". That was true for Codex in testing, but not for
Claude Code. Because delivery resumed immediately after state mutation,
queued replacements could be pasted before Claude's input buffer had
been drained.

### Fix

- Interrupt dispatch is agent-type-specific:
  - Codex targets receive `Escape`.
  - Claude targets receive `Escape`, then one `C-c` when the daemon saw
    active interrupt work.
- The `C-c` is gated on active work computed under `state_lock`, so an
  idle/no-op Claude interrupt does not send the first key of Claude's
  double-`Ctrl-C` exit sequence.
- The daemon keeps `state_lock` held through `Escape`, the short delay,
  `C-c`, and state mutation. This prevents hook events or replacement
  delivery from interleaving between the two keys.
- `AGENT_BRIDGE_CLAUDE_INTERRUPT_KEYS=esc` restores the legacy
  ESC-only behavior if a future Claude Code release changes key
  semantics.
- `AGENT_BRIDGE_INTERRUPT_KEY_DELAY_SEC` tunes the inter-key delay
  (default 0.15s, clamped to 0.05s..1.0s).
- Interrupt responses now include `interrupt_ok`, `interrupt_keys`,
  `cc_sent`, and `cc_error`. `interrupt_keys` is the attempted sequence;
  `cc_sent` / `cc_error` report whether Claude's follow-up key actually
  completed. `agent_interrupt_peer` exits non-zero when the configured
  key sequence only partially completes.

### Residual risk

If `Escape` succeeds but Claude's follow-up `C-c` fails, the daemon does
not roll back the already-committed cancellation. It reports
`interrupt_ok=false`, installs a per-target delivery gate so queued
replacements stay pending across both immediate and periodic delivery
ticks, and logs the partial failure for operator inspection. A later
successful `agent_interrupt_peer <alias>` clears the gate and resumes
delivery. `agent_interrupt_peer <alias> --clear-hold` can also clear the
gate manually, but that is unsafe unless the pane input is known clean.

The partial-failure gate is daemon-memory only. A daemon restart, crash,
or leave/rejoin under the same alias can lose or stale that gate; after
such recovery, operators should verify with `agent_view_peer <alias>`
that the pane input is clear before allowing queued prompts to continue.
