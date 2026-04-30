# Daemon Refactor Stage 8 Notes

Date: 2026-04-30

Stage 8 scope: extract hook/event routing for `prompt_submitted`,
`response_finished`, and `message_queued` into an event service while
preserving stale-response classification, nonce fail-closed behavior, and
ingressing finalization under the existing daemon lock facade.

## Reviewer Roles

- `worker`: owns the Stage 8 diff, validation, reviewer coordination, and
  eventual commit.
- `reviewer2`: primary reviewer, concurrency reviewer, and test reviewer.
- `reviewer1`: structure reviewer for module boundaries, imports, and facade
  compatibility.
- `manager`: not assigned a reviewer role.

## Files Changed

- `libexec/agent-bridge/bridge_daemon.py`
- `libexec/agent-bridge/bridge_daemon_events.py`
- `docs/daemon-refactor-stage8-notes.md`

## Event Module

Added `bridge_daemon_events.py` as a function-based event routing service that
takes the daemon instance as `d`. The module does not import
`bridge_daemon.py`.

Moved constant:

```text
EMPTY_RESPONSE_BODY
```

`bridge_daemon.py` imports and re-exports that name for compatibility.

Moved implementations:

```text
find_inflight_candidate
handle_prompt_submitted
response_fingerprint
maybe_return_response
_cleanup_terminal_context_locked
handle_response_finished
handle_external_message_queued
_drop_ingress_row_from_blocked_sender
_apply_alarm_cancel_to_queued_message
```

`BridgeDaemon` keeps thin wrappers for all moved methods.

## Compatibility

`handle_record` remains in `bridge_daemon.py` as the hook dispatcher. It still
routes `prompt_submitted`, `response_finished`, and `message_queued` records
through the daemon wrapper methods, and capture handling remains outside the
new event module.

Moved event handlers call retained behavior through `d.*` where tests,
overrides, or future service boundaries may depend on the daemon facade:

```text
d.participant_alias
d.handle_clear_prompt_submitted_locked
d.handle_clear_response_finished_locked
d._match_interrupted_prompt
d._match_interrupted_response
d._pop_interrupted_tombstone
d._supersede_interrupted_no_turn_suppression
d.mark_message_delivered_by_id
d._cancel_active_messages_for_target
d._record_message_tombstone
d.discard_nonce
d.remove_delivered_message
d.promote_pending_self_clear_locked
d.maybe_return_response
d.collect_aggregate_response
d.redirect_oversized_result_body
d.queue_message
d.suppress_pending_watchdog_wakes
d.cancel_watchdogs_for_message
d.cancel_watchdogs_for_aggregate
d._maybe_cancel_alarms_for_incoming
d._strip_no_auto_return_watchdog_metadata
d.try_deliver
```

Intentionally retained daemon support helpers:

```text
handle_record
handle_capture_request
participant_alias
remember_nonce
cached_nonce
discard_nonce
handle_clear_prompt_submitted_locked
handle_clear_response_finished_locked
_match_interrupted_prompt
_match_interrupted_response
_pop_interrupted_tombstone
_supersede_interrupted_no_turn_suppression
_expire_turn_id_mismatch_contexts
_recover_ingressing_messages
_promote_aged_ingressing
```

## Lock And Ordering Invariants

- `handle_prompt_submitted` still reloads participants before acquiring
  `d.state_lock`.
- Prompt submission still uses daemon-side inflight lookup as authoritative.
  The observed hook nonce is only a cross-check before marking a message
  delivered; missing or mismatched nonce leaves the row inflight and clears
  retry-enter state.
- User prompt interception still happens only when no message was marked
  delivered, after nonce/candidate checks fail closed.
- `handle_response_finished` still classifies stale/held/interrupted/turn-id
  mismatch responses before terminal cleanup, busy/reserved/current prompt
  mutation, response routing, or delivery kick.
- Terminal cleanup still suppresses pending watchdog wakes, removes delivered
  rows, clears nonce cache, updates busy/reserved/current prompt state, and
  records tombstones in the prior order.
- `maybe_return_response` preserves auto-return ordering: requester-cleared
  suppression, inactive/no-auto-return exits, terminal watchdog suppression,
  aggregate response collection, oversized redirect, `d.queue_message`,
  `response_return_queued` logging, then final watchdog cancellation.
- `_apply_alarm_cancel_to_queued_message` remains the shared finalizer for
  socket ingress and file-fallback ingress. Alarm/body mutation happens before
  status promotion from `ingressing` to `pending`, and the `status ==
  "ingressing"` gate keeps the helper idempotent.
- Socket ingress via `enqueue_ipc_message` still calls
  `d._apply_alarm_cancel_to_queued_message(...)` under the existing
  `d.state_lock`, matching file-fallback event finalization.

## Explicit Non-Changes

- Delivery reservation, pane-mode, tmux delivery, retry-enter, and
  undeliverable finalization did not move.
- Watchdog/alarm service behavior did not move.
- Aggregate collection/completion did not move.
- Interrupt and controlled clear flows did not move.
- Command socket dispatch and status builders did not move.
- Capture routing did not move.
- Startup and maintenance ingress recovery did not move.
- No lock splitting or file-lock ordering changes were introduced.

## Validation

Syntax/import check:

```sh
python3 -m py_compile \
  libexec/agent-bridge/bridge_daemon.py \
  libexec/agent-bridge/bridge_daemon_events.py \
  libexec/agent-bridge/bridge_daemon_delivery.py \
  libexec/agent-bridge/bridge_daemon_watchdogs.py
```

Result: passed.

Explicit compatibility checks:

```text
BridgeDaemon wrappers exist for all moved event methods
bridge_daemon_events does not import bridge_daemon.py
bridge_daemon.EMPTY_RESPONSE_BODY matches bridge_daemon_events.EMPTY_RESPONSE_BODY
direct d.handle_prompt_submitted marks an inflight row delivered
direct d.handle_response_finished consumes a delivered row
direct d.handle_external_message_queued finalizes an ingressing row
direct d._apply_alarm_cancel_to_queued_message promotes ingressing to pending
direct d.maybe_return_response queues a result
```

Result: passed.

Focused Stage 8 suites:

```sh
python3 scripts/run_regressions.py --match prompt --fail-fast
python3 scripts/run_regressions.py --match response --fail-fast
python3 scripts/run_regressions.py --match turn_id_mismatch --fail-fast
python3 scripts/run_regressions.py --match ingressing --fail-fast
python3 scripts/run_regressions.py --match interrupt --fail-fast
python3 scripts/run_regressions.py --match clear --fail-fast
python3 scripts/run_regressions.py --match watchdog --fail-fast
python3 scripts/run_regressions.py --match aggregate --fail-fast
python3 scripts/run_regressions.py --match prompt_submitted --fail-fast
python3 scripts/run_regressions.py --match response_finished --fail-fast
python3 scripts/run_regressions.py --match nonce --fail-fast
python3 scripts/run_regressions.py --match consume_once --fail-fast
python3 scripts/run_regressions.py --match redirect --fail-fast
python3 scripts/run_regressions.py --match fallback_path_alarm_cancel --fail-fast
python3 scripts/run_regressions.py --match aggregate_fallback_finalize --fail-fast
python3 scripts/run_regressions.py --match requester_cleared --fail-fast
```

Results:

```text
prompt: 23 passed, 0 failed
response: 30 passed, 0 failed
turn_id_mismatch: 15 passed, 0 failed
ingressing: 7 passed, 0 failed
interrupt: 49 passed, 0 failed
clear: 52 passed, 0 failed
watchdog: 59 passed, 0 failed
aggregate: 46 passed, 0 failed
prompt_submitted: 5 passed, 0 failed
response_finished: 1 passed, 0 failed
nonce: 7 passed, 0 failed
consume_once: 4 passed, 0 failed
redirect: 10 passed, 0 failed
fallback_path_alarm_cancel: 1 passed, 0 failed
aggregate_fallback_finalize: 1 passed, 0 failed
requester_cleared: 1 passed, 0 failed
```

Risk-focused scenarios covered by the focused filters and full suite:

```text
aggregate_fallback_finalize
fallback_path_alarm_cancel
ingressing_not_delivered_before_finalize
prompt_intercept_aggregate_completes
consume_once_basic
consume_once_empty_response
nonce_mismatch_fail_closed
no_observed_nonce_with_candidate_fail_closed
requester_cleared_prompt_guard_and_notice
clear_aggregate_requester_late_reply_suppressed
```

Full suite, run because prompt/response event routing and ingress finalization
facades moved:

```sh
python3 scripts/run_regressions.py
```

Result:

```text
615 passed, 0 failed
```

The full run emitted the known live-session-dependent skip line for
`list_peers_json_daemon_status_strips_pid`; the final summary remained green.
