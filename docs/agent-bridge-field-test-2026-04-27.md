# Agent Bridge Field Test - 2026-04-27

Live-room test of `agent-bridge` using three peers:

- `claude-reviewer` and `codex-reviewer` reviewed the repository as real collaborators.
- `claude-proxy` was used for notice, alarm, interrupt, and watchdog control tests.
- `codex-worker` drove the test and wrote this report.

The goal was to exercise model-facing bridge commands under realistic cooperative work, not only dry-run unit tests.

## Commands Exercised

Core discovery and status:

- `agent_list_peers`
- `agent_interrupt_peer --status`

Send paths:

- `agent_send_peer --to <alias> 'body'`
- `agent_send_peer <alias> 'body'`
- `agent_send_peer --to <alias> --stdin <<'EOF' ... EOF`
- `agent_send_peer --kind notice --to <alias> ...`
- `agent_send_peer --to a,b ...`
- `agent_send_peer --all ...`
- `agent_send_peer --watchdog <sec> ...`
- `agent_send_peer --watchdog 0 ...`

Wake/control paths:

- `agent_alarm <sec> --note ...`
- `agent_extend_wait <msg_id> <sec>`
- `agent_interrupt_peer <alias>`
- `agent_interrupt_peer <alias> --clear-hold`

View paths:

- `agent_view_peer <alias> --onboard --tail N`
- `agent_view_peer <alias> --older --page N --lines N`
- `agent_view_peer <alias> --since-last`
- `agent_view_peer <alias> --search <text>`
- `agent_view_peer <alias> --search <text> --live`

Validation/error paths:

- `agent_send_peer --kind result ...` rejected by argparse.
- Split inline body rejected with the shell-argument hint.
- Option after destination rejected.
- Dash-prefixed inline body rejected with the `--stdin` hint.
- `--kind notice --watchdog` rejected with notice/alarm guidance.
- Self-view rejected unless `--self` is passed.
- Aggregate member `agent_extend_wait` rejected as unsupported.

Local validation:

- `./install.sh --dry-run` completed.
- `bin/bridge_healthcheck.sh` completed with all checks `ok`.
- `python3 -m py_compile libexec/agent-bridge/*.py scripts/regression_interrupt.py` completed.
- Before implementation, `python3 scripts/regression_interrupt.py` completed with `369 passed, 0 failed`.
- After the interrupt fix and code-review fixes, `python3 scripts/regression_interrupt.py` completed with `386 passed, 0 failed`.

## What Worked Well

- Basic request routing worked: reviewer prompts arrived with the bridge envelope and reviewers responded normally.
- Partial broadcast routing worked: `agent_send_peer --to claude-reviewer,codex-reviewer ...` returned one aggregate result after both peers replied.
- Full broadcast routing worked: `agent_send_peer --all ...` returned one aggregate result after all three other peers replied.
- Notice semantics worked: a notice to `claude-proxy` did not create an auto-routed result.
- Explicit notice follow-up worked: `claude-proxy` sent a separate `--kind notice` back to `codex-worker`.
- Alarm cancellation worked: the incoming peer notice cancelled the outstanding alarm and prepended `[bridge:alarm_cancelled]` with a re-arm hint.
- `agent_view_peer` was useful in practice. `--onboard`, `--older`, `--since-last`, saved-snapshot search, and live search all returned actionable model-safe output.
- Syntax guardrails are effective. Most invalid sends fail closed before enqueue and include useful repair hints.
- `agent_interrupt_peer --status` gives enough high-level state to tell idle/busy/held/pending/inflight apart.

## Bugs / Unstable Behavior

### 1. Interrupting an inflight prompt can leave prompt text in the peer pane

Severity: high.

Reproduction from the live test:

1. Sent `msg-76d23d0a70ba` to `claude-proxy` with a slow-task prompt.
2. `agent_interrupt_peer claude-proxy --status` showed the message as `reserved_message_id=msg-76d23d0a70ba`, `inflight_count=1`, not delivered.
3. `agent_interrupt_peer claude-proxy` returned `esc_sent: true` and `cancelled_message_ids: ["msg-76d23d0a70ba"]`.
4. Later sent `msg-c73362d1cc7c` with `--watchdog 3`.
5. `agent_view_peer claude-proxy --since-last` showed the cancelled prompt text and the new prompt text concatenated in the pane. `claude-proxy` answered after `sleep 12`.
6. `agent_interrupt_peer --status` still showed `claude-proxy` with `reserved_message_id=msg-c73362d1cc7c` and `inflight_count=1`. No watchdog/result reached the sender.
7. A second `agent_interrupt_peer claude-proxy` was required to clear `msg-c73362d1cc7c`.

Likely mechanism: the first interrupt cancels bridge state but does not reliably clear the model input buffer. The next bridge paste can append to residual text. Because the nonce/prompt-submitted transition is not recognized cleanly, the new row remains `inflight`.

Related code:

- Interrupt cancels only active statuses: `bridge_daemon.py` around `_cancel_active_messages_for_target(... cancel_statuses={"delivered", "inflight", "submitted"})`.
- Watchdogs arm only after delivery, so an `inflight` prompt stuck before delivery never wakes: `bridge_daemon.py` `mark_message_delivered_by_id`.

Suggested fixes:

- Use agent-type-specific interrupt keys. Direct key tests showed Codex clears a submitted task with `Escape`, while Claude Code leaves the interrupted prompt in the input buffer after `Escape` and clears it with one following `Ctrl-C`. Keep Codex on `Escape`; for Claude send `Escape` then `Ctrl-C` before allowing the next delivery.
- After interrupt, verify the pane prompt buffer is actually clear before allowing the next delivery.
- Add an inflight-delivery timeout or watchdog distinct from the response watchdog.
- Consider a stronger tmux clear-line sequence for Claude/Codex panes after ESC, or a model-specific recovery check before pasting the next prompt.

Implementation follow-up: the current working tree implements the
agent-type-specific key sequence and records the durable issue/fix in
`docs/known-issues.md` as I-07.

Post-fix live verification:

- `codex-reviewer` received `LIVE_INTERRUPT_TEST_CODEX` and was
  interrupted while running a long `sleep`. `agent_interrupt_peer
  codex-reviewer` returned `interrupt_keys: ["Escape"]`,
  `cc_sent: null`, `interrupt_ok: true`, and cancelled the active
  message. Follow-up status showed no busy/reserved/current/pending
  state and no partial-failure gate.
- `claude-reviewer` received `LIVE_INTERRUPT_TEST_CLAUDE` and was
  interrupted while active. `agent_interrupt_peer claude-reviewer`
  returned `interrupt_keys: ["Escape", "C-c"]`, `cc_sent: true`,
  `interrupt_ok: true`, and cancelled the active message. Follow-up
  status showed no busy/reserved/current/pending state and no
  partial-failure gate.
- A post-interrupt notice delivered cleanly to both reviewers. The
  Claude pane showed the new bridge envelope at an empty prompt and
  acknowledged clean delivery. The Codex pane also showed the new bridge
  envelope cleanly; its earlier background `sleep` later produced
  `CODEX_SLEEP_DONE` in the peer pane, but that stale pane output did
  not create bridge pending/inflight/result state.

### 2. Watchdog wake can become stale before the model acts on it

Severity: medium.

Observed: a watchdog notice for `msg-53e9f4cf3e8f` instructed the sender to choose `agent_extend_wait`, `agent_interrupt_peer`, or `agent_view_peer`. Choosing `agent_extend_wait msg-53e9f4cf3e8f 180` immediately failed:

```text
agent_extend_wait: message 'msg-53e9f4cf3e8f' not found in queue (already responded, interrupted, or invalid id).
```

At that point `codex-reviewer` was idle and its response was visible in the peer pane, so the queue had already resolved between watchdog firing and the sender acting.

The auto-routed result for `msg-53e9f4cf3e8f` arrived later in the sender pane, confirming that the failed `extend_wait` did not mean the peer result was lost. It meant the watchdog notice had become stale relative to the queue/result state visible to the sender.

The same pattern repeated for `msg-ddb7001183a4` to `claude-reviewer`: a watchdog notice arrived after `agent_view_peer` had already shown the review response and `agent_interrupt_peer --status` showed `claude-reviewer` idle with no delivered/inflight/pending queue rows.

This is not necessarily a routing bug, but the model-facing UX is confusing because the watchdog notice presents actions as if the message is still live.

Suggested fixes:

- In the `message_not_found` error, explicitly mention that a result may already be queued for the sender and that ending the turn may deliver it.
- If possible, suppress or rewrite watchdog notices that are delivered after the original queue row has already resolved.

### 3. `agent_interrupt_peer <alias>` does not cancel queued pending work

Severity: medium.

The prompt says interrupt is for a wrong prompt or stuck peer. In code, interrupt cancels `delivered`, `inflight`, and `submitted`, but not `pending`. If a mistaken request is queued behind current peer work, `agent_interrupt_peer <alias>` does not remove it and it can deliver later.

Suggested fixes:

- Update prompt/cheat-sheet wording: "Interrupt cancels the peer's active delivered/inflight/submitted turn; it does not delete pending queued messages."
- Add a model-facing `agent_cancel_message <msg_id>` or `agent_send_peer --cancel <msg_id>` for queued pending messages.

### 4. Notice success output includes a misleading wait hint

Severity: medium.

Observed notice output:

```text
notice sent. Safety wake: agent_alarm <sec> --note '<desc>'.
End your turn; sleep/polling blocks the wake you await.
```

For notice, there is no auto-routed reply and no wake unless the sender explicitly arms `agent_alarm`. The second line can teach the model that a notice has a pending response path.

Suggested fix: only print the "End your turn" wait hint for request sends. For notices, print a notice-specific line such as:

```text
NOTICE_SENT: no reply auto-routes. Do not wait for one; set agent_alarm only if a follow-up matters.
```

### 5. `agent_alarm` lacks the anti-polling hint

Severity: low/medium.

Observed `agent_alarm 90 --note ...` printed only the wake id. Since alarm is specifically a self-wake mechanism, it should carry an anti-busy-wait hint: do independent work only; do not sleep/poll or keep the turn open waiting.

Suggested fix: print a short stderr hint after the stdout wake id:

```text
ALARM_SET: wake arrives later as a new [bridge:*] notice prompt unless cancelled by an incoming peer request/notice; do not sleep/poll or keep this turn open waiting.
```

### 6. `--watchdog` with no auto-return is nonsensical for internal callers

Severity: low/medium.

`--no-auto-return` is not exposed by the user-facing `agent_send_peer` shim, so normal agents should not hit this. But bridge-internal/system callers can create a request that has a watchdog while also disabling the return route. That watchdog can only fire, because the daemon is intentionally not going to auto-route a reply.

Suggested fix: suppress default watchdog creation when `no_auto_return` is set, or reject an explicit watchdog with `--no-auto-return`.

## Documentation / Prompt Ambiguities

### 1. Watchdog timing is delivery-time, not send-time

The initial prompt says requests get a default watchdog, and the full cheat sheet says the watchdog wakes after the prompt is delivered. The distinction is easy to miss.

Code confirms delivery-time arming in `bridge_daemon.py`: `mark_message_delivered_by_id` arms the watchdog only after the queue row changes from `inflight` to `delivered`. If a peer is busy, dead, held, or stuck in `inflight`, no response watchdog fires.

Suggested wording:

```text
Watchdogs are response timers, not queue/delivery timers. The clock starts only after the bridge prompt is submitted to the peer.
```

### 2. Aggregate wait behavior needs stronger upfront guidance

Partial broadcast and `--all` worked, but aggregate waiting is harder for a model to reason about:

- `agent_extend_wait` is not supported for aggregate member messages.
- `--watchdog 0` on an aggregate can leave the sender with no automatic timeout.
- There is no concise model-facing aggregate progress command; progress appears only in aggregate watchdog text.
- `agent_extend_wait` returns a clear aggregate-member error when invoked, but only after a daemon round trip. A local precheck or stronger watchdog text would make the constraint easier to learn.

The partial-broadcast field test completed successfully as aggregate `agg-c54803d56dab`, and both reviewers independently used their replies to point at aggregate/notice UX issues. The full-broadcast field test also completed successfully as `agg-b08adac322d6` with replies from `claude-proxy`, `claude-reviewer`, and `codex-reviewer`; two replies again pointed at hardcoded watchdog-default wording and aggregate timeout guidance. This supports treating aggregate delivery as functional while still improving the model-facing wait guidance.

Suggested wording:

```text
For broadcasts, one aggregate result returns after every addressed peer replies. Per-message agent_extend_wait is unavailable for aggregate members. Keep a watchdog unless you are willing to inspect or interrupt slow peers manually.
```

### 3. Alarm cancellation wording should emphasize unrelated peer messages

The observed alarm cancellation behavior matched the daemon: any incoming non-result message from another non-bridge sender to the alarm owner cancels alarms and prepends `[bridge:alarm_cancelled]`.

Suggested wording:

```text
Alarms are broad safety wakes. Any incoming peer request/notice cancels them, even if unrelated. If the prepended cancellation notice is not what you were waiting for, re-arm.
```

### 4. Request prompt says "do not call agent_send_peer" more broadly than the guard enforces

`build_peer_prompt` says: "Reply normally; do not call agent_send_peer; bridge auto-returns your reply." The response guard blocks sends back to the requester, but allows sends to third peers.

The current wording is safe, but it hides a potentially useful cooperative workflow: a reviewer can ask a third peer for help, then answer normally to the original requester.

Suggested wording:

```text
Reply normally to the requester; the bridge auto-returns that reply. Do not send a separate message back to the requester unless using --force. Requests to other peers create separate workflows.
```

### 5. `--clear-hold` needs a decision rule

The cheat sheet correctly says `--clear-hold` is normally unnecessary and unsafe if the peer is still running. It should also tell the model how to decide.

Suggested wording:

```text
Use --clear-hold only after --status shows held=true while the peer is otherwise idle and not in pane mode.
```

## Missing / Useful Features

### 1. Model-facing outstanding wait list

`agent_interrupt_peer --status` shows counts, but not the specific outstanding message ids, aggregate ids, watchdogs, or alarms owned by the caller. During this test, `codex-worker` had `pending_count=5` but no model-safe command to list what those pending results/wakes were.

Suggested command:

```text
agent_wait_status
```

It should list the caller's outstanding requests, aggregate waits, alarms, watchdog deadlines, and pending inbound results/notices.

### 2. Cancel pending message by id

Interrupt handles active peer work but not queued pending messages. A model needs a way to cancel a mistaken queued send before delivery.

Suggested command:

```text
agent_cancel_message <msg_id>
```

It should only allow the original sender to cancel messages still in `pending` or maybe `inflight` before prompt submission.

### 3. Aggregate progress command

For long broadcasts, a sender should not need to inspect every peer pane to know which aggregate members are still missing.

Suggested command:

```text
agent_aggregate_status <aggregate_id>
```

At minimum, show expected peers, replied peers, missing peers, and whether an aggregate watchdog is armed.

### 4. Delivery timeout distinct from response watchdog

The response watchdog intentionally starts after delivery. The inflight-stuck proxy case shows the need for a separate delivery timeout or self-wake:

```text
agent_send_peer --delivery-watchdog <sec> --watchdog <sec> ...
```

or a default daemon notice when a message remains `inflight` too long without `prompt_submitted`.

## Minor UX Issues

- `agent_send_peer --to <alias> <body> --stdin` produces a technically correct but not very helpful "option appeared after inline body" style error. The docs say `--stdin` may appear after the destination, but that is only valid when no inline body is also present.
- `agent_send_peer <alias>` with no body falls through to a downstream "no target/body" style error. Prefer "missing message body - did you mean `agent_send_peer --to <alias> ...`?"
- After `--stdin`, a typo that is not a participant alias can report "cannot combine --stdin with a positional inline body" even when the real problem is that the intended alias did not resolve.
- Empty `--stdin` and no body share the generic "message body is required" error. Distinguish empty heredoc/pipe from missing argument.
- The initial prompt hardcodes "300s" for the default watchdog. If `AGENT_BRIDGE_DEFAULT_WATCHDOG_SEC` is changed, the prompt becomes inaccurate. Prefer "configured default" or render the actual value.
- `agent_alarm --note` accepts arbitrary length text, but daemon cancellation rendering truncates notes. A CLI warning or max-length validation would set expectations.
- Extremely large finite watchdog values are accepted. This is not a correctness bug, but a warning for values over a practical threshold would catch mistakes.
- The bridge prompt envelope is a single inline prefix followed by the possibly multi-line body. Current prompt text is workable, but docs should avoid implying that every body line is individually prefixed.

## Contributor-Facing Follow-Ups

- `bridge_daemon.py` documents a lock ordering rule for `state_lock` and `queue.update()` mutators, but it is only a comment. A debug-only guard could catch accidental logging or lock-order violations inside queue mutators during regression runs.
- `read_available_ambient_stdin_text` restores file descriptor flags in a `finally` block. The implementation looked correct in inspection; add a unit test asserting `fcntl(fd, F_GETFL)` is unchanged after the helper returns.

## False Positives / Non-Issues Checked

- `agent_view_peer --search` argparse help already says "case-insensitive literal substring (no regex)", so no help-text fix is needed there.
- `agent_extend_wait` already has a clear aggregate-member error when actually invoked:
  `message ... is part of an aggregate broadcast; per-message extend is not supported in v1.5.`
- `--kind result` is correctly blocked from model-facing sends.

## Recommended Priority

1. Fix or mitigate interrupt leaving residual prompt text, because it can wedge subsequent delivery in `inflight` and suppress both watchdog and auto-return.
2. Add a delivery-timeout/self-wake for stuck `inflight` messages.
3. Fix notice/alarm output hints so models do not wait on notices incorrectly and do not poll after arming alarms.
4. Document delivery-time watchdog semantics and pending-message non-cancellation in the initial prompt and full cheat sheet.
5. Add model-facing wait/aggregate status and pending-message cancellation commands.
