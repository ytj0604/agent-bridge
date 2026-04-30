# Daemon Refactor Stage 9a Notes

Date: 2026-04-30

Stage 9a scope: extract interrupt and interrupted-turn tombstone handling into
an interrupt service while preserving the existing physical lock scope, tmux
key positions, command-lock responses, and Stage 8 event facade behavior.
Controlled clear remains in `bridge_daemon.py` for Stage 9b.

## Reviewer Roles

- `worker`: owns the Stage 9a diff, validation, reviewer coordination, and
  eventual commit.
- `reviewer2`: primary reviewer, concurrency reviewer, and test reviewer.
- `reviewer1`: structure reviewer for module boundaries, imports, and facade
  compatibility.
- `manager`: not assigned a reviewer role.

## Files Changed

- `libexec/agent-bridge/bridge_daemon.py`
- `libexec/agent-bridge/bridge_daemon_interrupts.py`
- `docs/daemon-refactor-stage9a-notes.md`

## Interrupt Module

Added `bridge_daemon_interrupts.py` as a function-based interrupt service that
takes the daemon instance as `d`. The module does not import
`bridge_daemon.py`.

Moved constants:

```text
INTERRUPT_KEY_DELAY_DEFAULT_SECONDS
INTERRUPT_KEY_DELAY_MIN_SECONDS
INTERRUPT_KEY_DELAY_MAX_SECONDS
INTERRUPT_SEND_KEY_TIMEOUT_SECONDS
CLAUDE_INTERRUPT_KEYS_DEFAULT
INTERRUPTED_TOMBSTONE_LIMIT_PER_AGENT
INTERRUPTED_TOMBSTONE_TTL_SECONDS
```

`bridge_daemon.py` imports and re-exports those names for compatibility.

Moved implementations:

```text
_prune_interrupted_turns
_prune_interrupted_turns_for_all
_find_message_tombstone
_record_message_tombstone
_record_interrupted_turns
_match_interrupted_prompt
_supersede_interrupted_no_turn_suppression
_pop_interrupted_tombstone
_match_interrupted_response
target_has_active_interrupt_work
handle_interrupt
_build_interrupt_notice
release_hold
```

`BridgeDaemon` keeps thin wrappers for all moved methods.

## Retained Helpers

These interrupt-adjacent helpers intentionally remain in `bridge_daemon.py` for
Stage 9a:

```text
send_interrupt_key
cancel_message
_message_is_active_inflight_for_cancel
_remove_queue_message_by_id
_cancel_active_messages_for_target
resolve_interrupt_key_delay_seconds
resolve_claude_interrupt_keys
```

`send_interrupt_key` remains on the daemon module to preserve the existing
`bridge_daemon.subprocess.run` monkeypatch surface. Moved `handle_interrupt`
calls `d.send_interrupt_key(...)` for both `Escape` and `C-c`.

The interrupt config helpers remain on `bridge_daemon.py` and continue to use
the existing daemon-local `resolve_non_negative_env_seconds` helper plus the
imported/re-exported interrupt constants.

`_cancel_active_messages_for_target` remains shared daemon support because
Stage 8 prompt interception and Stage 9a interrupt both call it.

## Dependency Strategy

`bridge_daemon_interrupts.py` imports `PHYSICAL_AGENT_TYPES` from the neutral
`bridge_participants` module. It does not import `bridge_daemon.py`.

Moved command-lock paths do not import `CommandLockWaitExceeded`. They catch
`Exception`, handle only exceptions whose class name is
`CommandLockWaitExceeded`, and re-raise unrelated exceptions.

`handle_interrupt` uses:

```text
d.command_budget("interrupt")
d.command_state_lock(...)
d.command_deadline_ok(...)
```

This preserves the prior default command budget where the old code used
daemon-local command constants, while retaining the explicit
`INTERRUPT_SEND_KEY_TIMEOUT_SECONDS` budget for key-dispatch checks.

## Compatibility

Moved interrupt code calls retained behavior through `d.*` where tests,
overrides, or other services depend on the daemon facade:

```text
d.reload_participants
d.command_state_lock
d.command_deadline_ok
d.command_budget
d.resolve_endpoint_detail
d.finalize_undeliverable_message
d.target_has_active_interrupt_work
d.send_interrupt_key
d._cancel_active_messages_for_target
d._record_interrupted_turns
d._record_aggregate_interrupted_reply
d.queue_message
d.try_deliver_command_aware
d.log
d.safe_log
```

Stage 8 event code, watchdog code, aggregate code, and retained
`cancel_message` continue to reach interrupted tombstone helpers through
`d.*` facade methods.

## Lock And Ordering Invariants

- `handle_interrupt` still reloads participants before acquiring the command
  state lock.
- `handle_interrupt` still holds the same daemon state lock across endpoint
  resolution, `Escape`, active cancellation/tombstone recording, optional
  `C-c`, and partial-failure block updates.
- `Escape` send failure returns before queue, current prompt, busy/reserved,
  tombstone, or aggregate state mutation.
- Endpoint-loss finalization remains inside the same lock position as before.
- No delivery kick can happen between `Escape` and `C-c`.
- A `C-c` failure still records `interrupt_partial_failure_blocks`, logs
  replacement blocking, and skips replacement delivery.
- Replacement delivery is kicked only after the configured key sequence
  fully succeeds.
- Aggregate-bearing interrupted messages still call
  `d._record_aggregate_interrupted_reply(...)` through the retained daemon
  facade.

## Explicit Non-Changes

- Controlled clear flow did not move.
- `send_interrupt_key` did not move.
- `cancel_message` did not move.
- Delivery reservation, pane-mode, retry-enter, and undeliverable
  finalization did not move.
- Event routing did not move.
- Watchdog/alarm service behavior did not move.
- Aggregate collection/completion did not move.
- No lock splitting or tmux lock-scope changes were introduced.

## Validation

Syntax/import check:

```sh
python3 -m py_compile \
  libexec/agent-bridge/bridge_daemon.py \
  libexec/agent-bridge/bridge_daemon_interrupts.py \
  libexec/agent-bridge/bridge_daemon_events.py \
  libexec/agent-bridge/bridge_daemon_delivery.py
```

Result: passed.

Explicit compatibility checks:

```text
BridgeDaemon wrappers exist for all moved interrupt/tombstone methods
bridge_daemon_interrupts does not import bridge_daemon.py
interrupt constants re-export from bridge_daemon.py with unchanged values
direct d.send_interrupt_key uses bridge_daemon.subprocess.run
handle_interrupt reaches d.send_interrupt_key and preserves bridge_daemon.subprocess.run monkeypatching
handle_interrupt re-raises unrelated command-lock exceptions
```

Result: passed.

Focused Stage 9a suites:

```sh
python3 scripts/run_regressions.py --match interrupt --fail-fast
python3 scripts/run_regressions.py --match interrupt_endpoint_lost --fail-fast
python3 scripts/run_regressions.py --match aggregate_interrupt --fail-fast
python3 scripts/run_regressions.py --match cancel_message --fail-fast
python3 scripts/run_regressions.py --match clear_hold --fail-fast
python3 scripts/run_regressions.py --match prompt_submitted --fail-fast
python3 scripts/run_regressions.py --match response_finished --fail-fast
python3 scripts/run_regressions.py --match watchdog --fail-fast
```

Results:

```text
interrupt: 49 passed, 0 failed
interrupt_endpoint_lost: 2 passed, 0 failed
aggregate_interrupt: 2 passed, 0 failed
cancel_message: 11 passed, 0 failed
clear_hold: 4 passed, 0 failed
prompt_submitted: 5 passed, 0 failed
response_finished: 1 passed, 0 failed
watchdog: 59 passed, 0 failed
```

Risk-focused scenarios covered by the focused filters and full suite:

```text
send_interrupt_key_timeout_returns_false
interrupt_env_override_disables_cc
interrupt_key_delay_env_nonfinite_uses_default
interrupt_key_delay_env_clamps_out_of_range
claude_interrupt_keys_invalid_uses_default
claude_interrupt_keys_empty_uses_default
clear_hold_clears_interrupt_partial_failure_gate
interrupted_late_prompt_submitted_before_replacement
interrupted_late_prompt_submitted_after_replacement
interrupted_late_turn_stop_preserves_replacement
interrupted_no_turn_stop_no_context_suppressed
interrupted_no_turn_race_routes_replacement_then_suppresses_old
interrupted_inflight_tombstone_retains_on_unrelated_stop
interrupted_tombstone_current_ctx_id_match_cleans
interrupted_tombstone_current_ctx_turn_match_cleans
interrupted_tombstone_stale_stop_preserves_replacement_watchdog
interrupted_tombstone_aggregate_ctx_does_not_cancel_aggregate_watchdog
aggregate_interrupt_synthetic_reply
cancel_message_idempotent_after_terminal
cancel_message_aggregate_per_leg_keeps_other_legs
```

Full suite, run because interrupt, tombstone, clear-hold, watchdog, and
event-facing facades moved:

```sh
python3 scripts/run_regressions.py
```

Result:

```text
615 passed, 0 failed
```

The full run emitted the known live-session-dependent skip line for
`list_peers_json_daemon_status_strips_pid`; the final summary remained green.
