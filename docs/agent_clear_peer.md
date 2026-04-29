# agent_clear_peer

`agent_clear_peer` runs a controlled `/clear` for a bridge participant. It is
the supported way for an agent or operator to clear a peer while preserving the
bridge's routing identity.

## Usage

```bash
agent_clear_peer <alias> [--force] [--json]
agent_clear_peer --to <alias> [--force] [--json]
agent_clear_peer <alias>,<alias>[,...] [--json]
agent_clear_peer --to <alias>,<alias>[,...] [--json]
agent_clear_peer --all [--json]
```

`bridge_manage` also exposes the same operation as **Clear agent context**.

## Semantics

The daemon reserves the target before touching the pane, injects `/clear`, waits
for a short post-clear settle delay, then injects a bridge probe. It does not use
`SessionStart` from `/clear` as a progress signal; completion is driven by the
probe prompt's `prompt_submitted` hook and the probe `response_finished` hook.
Only after that terminal probe event does the daemon replace the old hook
identity with the new session id across `session.json`,
`attached-sessions.json`, `pane-locks.json`, and `live-sessions.json`.

Self-clear is supported. `agent_clear_peer <your-alias>` schedules the clear
after the current turn ends; once the clear is reserved, later sends from that
alias are rejected until the clear finishes.

## Multi-Target Clear

`agent_clear_peer` accepts comma-separated aliases and `--all` for batch
clears. Attached agents get the same `--all` target semantics as
`agent_send_peer --all`: every active peer except the caller. A bridge/admin
caller (`--from bridge --allow-spoof`) expands `--all` to every active alias.

Multi-target clear is intentionally narrower than single-target clear:

- `--force` is rejected when more than one target remains after deduplication
  (`multi_force_disallowed`).
- Self-clear must be the only target (`multi_self_disallowed`). Use
  `agent_clear_peer <your-alias>` for the existing deferred self-clear path, or
  omit yourself from the batch.
- The daemon evaluates all targets from one queue snapshot and one aggregate
  snapshot, then installs every reservation before touching any pane. If any
  target has a hard or soft blocker, the whole batch is rejected and no target
  is reserved. Multi-target refusals name the target for each blocker and never
  suggest `--force`.
- A pending self-clear for a target is treated as `clear_already_pending` for
  both single-target and multi-target clears.

Execution after reservation is sequential and best-effort. A target that fails
after pane input may have been touched is force-left; a pre-pane failure is
reported as `failed`; later targets still run. Text output groups mixed
outcomes, for example:

```text
agent_clear_peer: cleared alice, carol; forced-leave bob (probe_timeout).
```

Human-facing text uses `forced-leave`; JSON status keys use `forced_leave`.
Multi-target JSON keeps requested order in `results` and includes a `summary`
with per-status counts and target lists. Single-target JSON is unchanged. The
multi-target CLI exits 0 only when every target is `cleared`; mixed or all-failed
execution summaries exit 1 after printing the per-target result.

## `--force`

Without `--force`, outstanding target-originated requests, target-owned alarms,
and cancellable inbound pending/pre-active rows block the clear.

With `--force`, the daemon cancels target-owned alarms, removes cancellable
inbound rows, and preserves target-originated requests with `auto_return`
disabled. When those preserved requests eventually finish, the responder gets a
bridge notice explaining that the requester was cleared and the response was
not delivered.

Incomplete aggregate waits owned by the target also block without `--force`.
With `--force`, those aggregate waits are cancelled as requester-cleared.

Refusal text partitions blockers by whether `--force` can recover them:

- `hard:<code>` means force-impossible. Examples include a clear already
  pending for the target, a busy target/current prompt, and active or
  post-pane-touch inbound work. Interrupt or let that work finish before
  clearing.
- `soft:<code>` means force-recoverable. Examples include cancellable inbound
  rows, target-owned alarms, target-originated requests whose return route can
  be disabled, and incomplete aggregate waits owned by the target.

When a non-force refusal includes soft blockers, the message ends with a
`Retry with --force to ...` clause that describes only the soft blocker actions
present in that refusal. A refusal after `--force` was already attempted does
not repeat that retry guidance.

JSON responses use the same partitioning: `hard_blockers` contains only
force-impossible blockers, and `soft_blockers` contains only force-recoverable
blockers.

## Failure Policy

If the daemon fails before mutating the pane, it releases the reservation and
leaves queue/watchdog state intact. Once pane input may have been touched,
timeout or probe failure forces a leave-style cleanup for the target rather
than returning it to a stale-active state.

## Operational Notes

- Do not type `/clear` manually in an attached bridge pane.
- `agent_clear_peer` waits synchronously for non-self clears, with a 180 second
  socket timeout for one target. Multi-target clears use a batch timeout:
  180 seconds per target, plus any configured settle delay above the 1 second
  default per target, plus a 10 second batch margin
  (`CLEAR_MULTI_TIMEOUT_MARGIN_SECONDS`).
- `AGENT_BRIDGE_CLEAR_POST_CLEAR_DELAY_SEC` controls the post-clear settle
  delay. The default is 1 second; `0` disables the delay while keeping the
  post-clear pane readiness check. Invalid or non-finite values use the default,
  and negative values are treated as `0`.
- Hook routing during clear uses `run/controlled-clears.json` so `SessionStart`
  can record the new session id and the probe `prompt_submitted`/`response_finished`
  hooks can route by probe marker before the attached registry is replaced.
- During controlled clear, an old-session `SessionEnd` that exactly matches the
  active marker's pane, agent type, and old session id is treated as expected
  `/clear` session rotation. The old live-session record may be cleaned, but
  participant, attached-session, and pane-lock detach are deferred. On success,
  `replace_attached_session_identity_for_clear()` replaces the old identity
  after the probe `response_finished`; on post-pane-touch failure,
  `force_leave_after_clear_failure()` owns the cleanup.
- Use `agent_interrupt_peer <alias>` for stuck active work; use
  `agent_cancel_message <msg_id>` for a specific pending/pre-active message.
