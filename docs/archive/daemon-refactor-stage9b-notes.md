# Daemon Refactor Stage 9b Notes

Date: 2026-04-30

Stage 9b scope: extract controlled clear handling into a clear-flow service
while preserving the existing physical lock scope, tmux call positions,
identity replacement compatibility, command-lock responses, and Stage 8/9a
facade behavior.

## Reviewer Roles

- `worker`: owns the Stage 9b diff, validation, reviewer coordination, and
  eventual commit.
- `reviewer2`: primary reviewer, concurrency reviewer, and test reviewer.
- `reviewer1`: structure reviewer for module boundaries, imports, and facade
  compatibility.
- `manager`: not assigned a reviewer role.

## Files Changed

- `libexec/agent-bridge/bridge_daemon.py`
- `libexec/agent-bridge/bridge_daemon_clear_flow.py`
- `docs/daemon-refactor-stage9b-notes.md`

## Clear Flow Module

Added `bridge_daemon_clear_flow.py` as a function-based controlled-clear
service that takes the daemon instance as `d`. The module does not import
`bridge_daemon.py`.

Moved constants:

```text
CLEAR_PROBE_TIMEOUT_SECONDS
CLEAR_POST_CLEAR_DELAY_DEFAULT_SECONDS
CLEAR_POST_LOCK_WORST_CASE_SECONDS
CLEAR_CLIENT_TIMEOUT_SECONDS
CLEAR_MULTI_TIMEOUT_MARGIN_SECONDS
CLEAR_LOCK_WAIT_BUDGET_SECONDS
```

`bridge_daemon.py` imports and re-exports those names for compatibility.

Moved implementations:

```text
clear_peer_post_lock_worst_case_seconds
clear_peer_batch_post_lock_worst_case_seconds
clear_peer_client_timeout_seconds
clear_target_count_from_request
sender_blocked_by_clear
_clear_guard_from_snapshots
clear_guard
clear_guard_multi
clear_target_lookup_error
validate_clear_targets_payload
clear_batch_summary
normalize_clear_batch_result
clear_batch_exception_requires_forced_leave
hold_clear_reservation_for_batch_failure
apply_force_clear_invalidation
force_leave_after_clear_failure
_mark_participant_detached_for_clear
handle_clear_peer
handle_clear_peers
_new_clear_reservation
_write_clear_marker_locked
_clear_probe_text
_wait_for_clear_settle_locked
run_clear_peer
handle_clear_prompt_submitted_locked
handle_clear_response_finished_locked
promote_pending_self_clear_locked
_self_clear_worker
```

`BridgeDaemon` keeps thin wrappers for all moved methods.

## Retained Helpers

These clear-adjacent helpers intentionally remain in `bridge_daemon.py` for
Stage 9b:

```text
resolve_clear_post_clear_delay_seconds
_clear_tmux_send
clear_process_identity_for_replacement
replace_attached_session_identity_for_clear
```

`resolve_clear_post_clear_delay_seconds` remains next to the generic daemon
environment resolver. `_clear_tmux_send` and
`clear_process_identity_for_replacement` stay on `BridgeDaemon` to preserve
existing monkeypatch surfaces.

`BridgeDaemon.replace_attached_session_identity_for_clear(...)` was added as a
mandatory facade wrapper. It resolves the existing `bridge_daemon.py`
module-global `replace_attached_session_identity_for_clear`, and moved
`run_clear_peer` calls `d.replace_attached_session_identity_for_clear(...)`.
This preserves tests and callers that monkeypatch
`bridge_daemon.replace_attached_session_identity_for_clear`.

`bridge_daemon_clear_flow.py` directly imports
`bridge_identity.verified_process_identity` only for the final live-record
verification after identity replacement. The monkeypatch-sensitive process
probe path still goes through `d.clear_process_identity_for_replacement(...)`.

## Dependency Strategy

Moved command-lock paths do not import `CommandLockWaitExceeded`. They catch
`Exception`, handle only exceptions whose class name is
`CommandLockWaitExceeded`, and re-raise unrelated exceptions.

Moved clear code calls retained behavior through `d.*` where tests, overrides,
or other services depend on the daemon facade:

```text
d.command_state_lock
d.command_deadline_ok
d.clear_guard
d.clear_guard_multi
d._reload_participants_unlocked
d.resolve_endpoint_detail
d.pane_mode_status
d._clear_tmux_send
d.clear_process_identity_for_replacement
d.replace_attached_session_identity_for_clear
d.try_deliver_command_aware
d.force_leave_after_clear_failure
d.apply_force_clear_invalidation
d._mark_participant_detached_for_clear
d.queue_message
d._record_aggregate_interrupted_reply
d.cancel_watchdogs_for_message
d.suppress_pending_watchdog_wakes
d.log
d.safe_log
```

## Lock And Ordering Invariants

- Clear guard and reservation still happen before any pane touch.
- Multi-clear still validates and reserves all targets under the command lock
  before sequential per-target execution.
- Clear reservations still use `threading.Condition(d.state_lock)`, backed by
  the raw daemon state lock.
- `run_clear_peer` keeps endpoint lookup, pane-mode check, marker write,
  `/clear`, settle wait, probe send, and probe wait under the first command
  state lock.
- Identity replacement still happens outside the first command state lock.
- Final cleanup and participant reload still happen under a reacquired command
  state lock.
- Pane-touched failures still force leave. Pre-pane failures still release or
  hold reservations as before.
- Batch-held successful clears still hold reservations until the batch release
  block, and delivery reopens only after release through
  `d.try_deliver_command_aware(...)`.

## Explicit Non-Changes

- Generic clear environment resolution did not move.
- Tmux send helper execution did not move.
- Process identity probing did not move.
- Interrupt, delivery, event, watchdog, aggregate, command, and status service
  behavior did not move.
- No lock splitting or tmux lock-scope changes were introduced.

## Validation

Syntax/import check:

```sh
python3 -m py_compile \
  libexec/agent-bridge/bridge_daemon.py \
  libexec/agent-bridge/bridge_daemon_clear_flow.py \
  libexec/agent-bridge/bridge_daemon_interrupts.py \
  libexec/agent-bridge/bridge_daemon_events.py \
  libexec/agent-bridge/bridge_daemon_delivery.py
```

Result: passed.

Explicit compatibility checks:

```text
bridge_daemon_clear_flow does not import bridge_daemon.py
clear constants re-export from bridge_daemon.py with unchanged values
BridgeDaemon wrappers exist for moved clear methods
threading.Condition(d.state_lock) accepts the raw daemon state lock
single clear reaches d._clear_tmux_send
single clear reaches d.clear_process_identity_for_replacement
single clear reaches d.replace_attached_session_identity_for_clear
multi-clear reaches d._clear_tmux_send for each target
multi-clear reaches d.clear_process_identity_for_replacement for each target
multi-clear reaches d.replace_attached_session_identity_for_clear for each target
```

Result: passed.

Focused Stage 9b suites:

```sh
python3 scripts/run_regressions.py --match clear --fail-fast
python3 scripts/run_regressions.py --match clear_identity --fail-fast
python3 scripts/run_regressions.py --match clear_force_leave --fail-fast
python3 scripts/run_regressions.py --match clear_multi --fail-fast
python3 scripts/run_regressions.py --match self_clear --fail-fast
python3 scripts/run_regressions.py --match requester_cleared --fail-fast
python3 scripts/run_regressions.py --match interrupt --fail-fast
python3 scripts/run_regressions.py --match prompt_submitted --fail-fast
python3 scripts/run_regressions.py --match response_finished --fail-fast
```

Results:

```text
clear: 52 passed, 0 failed
clear_identity: 2 passed, 0 failed
clear_force_leave: 2 passed, 0 failed
clear_multi: 12 passed, 0 failed
self_clear: 3 passed, 0 failed
requester_cleared: 1 passed, 0 failed
interrupt: 49 passed, 0 failed
prompt_submitted: 5 passed, 0 failed
response_finished: 1 passed, 0 failed
```

Full suite:

```sh
python3 scripts/run_regressions.py
```

Result: 615 passed, 0 failed.

Diff hygiene:

```sh
git diff --check
```

Result: passed.
