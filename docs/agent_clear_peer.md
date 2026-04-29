# agent_clear_peer

`agent_clear_peer` runs a controlled `/clear` for a bridge participant. It is
the supported way for an agent or operator to clear a peer while preserving the
bridge's routing identity.

## Usage

```bash
agent_clear_peer <alias> [--force] [--json]
agent_clear_peer --to <alias> [--force] [--json]
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

## `--force`

Without `--force`, outstanding target-originated requests, target-owned alarms,
and cancellable inbound pending/pre-active rows block the clear.

With `--force`, the daemon cancels target-owned alarms, removes cancellable
inbound rows, and preserves target-originated requests with `auto_return`
disabled. When those preserved requests eventually finish, the responder gets a
bridge notice explaining that the requester was cleared and the response was
not delivered.

Active/post-pane-touch work remains a hard blocker. Interrupt or let that work
finish before clearing.

## Failure Policy

If the daemon fails before mutating the pane, it releases the reservation and
leaves queue/watchdog state intact. Once pane input may have been touched,
timeout or probe failure forces a leave-style cleanup for the target rather
than returning it to a stale-active state.

## Operational Notes

- Do not type `/clear` manually in an attached bridge pane.
- `agent_clear_peer` waits synchronously for non-self clears, with a 180 second
  socket timeout.
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
