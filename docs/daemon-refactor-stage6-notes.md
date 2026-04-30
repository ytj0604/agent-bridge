# Daemon Refactor Stage 6 Notes

Date: 2026-04-30

Stage 6 scope: extract watchdog and alarm helper logic while preserving the
single daemon state lock, watchdog cancellation behavior, alarm idempotency,
status facade compatibility, and queue/aggregate lock ordering.

## Reviewer Roles

- `worker`: owns the Stage 6 diff, validation, reviewer coordination, and
  eventual commit.
- `reviewer1`: primary reviewer and structure reviewer.
- `reviewer2`: concurrency reviewer and test reviewer for watchdog/alarm
  locking, cancellation, stale skip, and focused regression coverage.
- `manager`: not assigned a reviewer role.

## Files Changed

- `libexec/agent-bridge/bridge_daemon.py`
- `libexec/agent-bridge/bridge_daemon_watchdogs.py`
- `docs/daemon-refactor-stage6-notes.md`

## Watchdog Module

Added `bridge_daemon_watchdogs.py` as a function-based watchdog/alarm service
that takes the daemon instance as `d`.

Moved constants:

```text
WATCHDOG_PHASE_DELIVERY
WATCHDOG_PHASE_RESPONSE
WATCHDOG_PHASE_ALARM
ALARM_CLIENT_WAKE_ID_RE
ALARM_WAKE_TOMBSTONE_TTL_SECONDS
ALARM_WAKE_TOMBSTONE_LIMIT
RESPONSE_LIKE_TOMBSTONE_REASONS
EXTEND_WATCHDOG_HINTS
WATCHDOG_REQUIRES_AUTO_RETURN_ERROR
WATCHDOG_REQUIRES_AUTO_RETURN_TEXT
```

`bridge_daemon.py` re-exports those constants for compatibility. The private
`RESPONSE_LIKE_TOMBSTONE_REASONS` duplicate in `bridge_daemon_status.py` is
intentionally unchanged in Stage 6.

Moved implementations:

```text
_finite_watchdog_delay
_watchdog_strip_log_fields
_strip_no_auto_return_watchdog_metadata
validate_enqueue_watchdog_metadata
_maybe_cancel_alarms_for_incoming
suppress_pending_watchdog_wakes
arm_message_watchdog
tombstone_extend_error
extend_watchdog_error_hint
upsert_message_watchdog
register_watchdog
normalize_watchdog_phase
_prune_alarm_wake_tombstones
_record_alarm_wake_tombstone
_same_alarm_request
_alarm_conflict_reason
_log_alarm_register_conflict
register_alarm_result
register_alarm
check_watchdogs
stamp_turn_id_mismatch_post_watchdog_unblock
build_watchdog_fire_text
_lookup_queue_item
_watchdog_elapsed_text
_aggregate_watchdog_progress_text
watchdog_fire_skip_reason
fire_watchdog
cancel_watchdogs_for_message
cancel_watchdogs_for_aggregate
```

`BridgeDaemon` keeps thin wrappers for the moved public/private method names.
Existing call sites continue to use `self.*`, including clear, delivery,
interrupt/cancel, aggregate completion, terminal response, status, and
turn-id timeout paths.

## Dependency Handling

`upsert_message_watchdog` and `register_alarm_result` now derive the same
command lock budgets through `d.command_budget("extend_watchdog")` and
`d.command_budget("alarm")`. This avoids importing command constants back from
`bridge_daemon.py` while preserving the prior budget values.

The watchdog module avoids importing `CommandLockWaitExceeded` from
`bridge_daemon.py`. It catches lock-wait failures by exception class name,
returns the existing lock-wait responses, and re-raises unrelated exceptions.
After a successful `command_state_lock` enter, `lock_ctx.__exit__` still runs
from `finally`.

## Lock And Ordering Invariants

- `check_watchdogs` still holds the raw `d.state_lock` while selecting due
  watchdogs and invoking `d.fire_watchdog`.
- `register_watchdog` still mutates `d.watchdogs` under `d.state_lock`.
- `upsert_message_watchdog` and `register_alarm_result` still use
  `d.command_state_lock` with deadline checks and release in `finally`.
- `_maybe_cancel_alarms_for_incoming` remains lock-neutral and is only called
  from the existing state-lock-protected ingress/finalize paths.
- `suppress_pending_watchdog_wakes` only mutates queue rows and does not read
  aggregate state.
- `_aggregate_watchdog_progress_text` reads aggregate state for progress text
  without holding a queue file lock.
- `watchdog_fire_skip_reason` preserves the existing best-effort queue or
  aggregate reads and does not combine queue and aggregate file locks.
- `fire_watchdog` preserves sender reload, stale skip, tombstone recording,
  watchdog pop, body construction, queueing, and logging order.

## Explicit Non-Changes

- No delivery, clear, interrupt, aggregate collection, command dispatch,
  status builder, tmux, queue store, aggregate store, or lock-splitting
  behavior moved beyond wrapper delegation.
- Alarm cancellation notice text, note/body truncation limits, and log fields
  are unchanged.
- Watchdog cancellation still filters out alarms, preserves phase filtering,
  and logs the same cancellation fields.
- `bridge_daemon_status.py` still calls `d.normalize_watchdog_phase` through
  the daemon facade.

## Validation

Syntax/import check:

```sh
python3 -m py_compile \
  libexec/agent-bridge/bridge_daemon.py \
  libexec/agent-bridge/bridge_daemon_watchdogs.py \
  libexec/agent-bridge/bridge_daemon_status.py \
  libexec/agent-bridge/bridge_daemon_aggregates.py
```

Result: passed.

Explicit compatibility checks:

```text
bridge_daemon watchdog constants equal bridge_daemon_watchdogs constants
RESPONSE_LIKE_TOMBSTONE_REASONS re-export contents match
ALARM_CLIENT_WAKE_ID_RE still accepts generated wake ids
d.normalize_watchdog_phase works through the daemon facade
bridge_daemon_status uses d.normalize_watchdog_phase through the facade
cancel_watchdogs_for_message wrapper cancels a non-alarm message watchdog
cancel_watchdogs_for_aggregate wrapper cancels a non-alarm aggregate response watchdog
register_alarm_result idempotent replay behavior is preserved
```

Result: passed.

Focused Stage 6 suites:

```sh
python3 scripts/run_regressions.py --match watchdog --fail-fast
python3 scripts/run_regressions.py --match alarm --fail-fast
python3 scripts/run_regressions.py --match extend_wait --fail-fast
python3 scripts/run_regressions.py --match ingressing --fail-fast
python3 scripts/run_regressions.py --match aggregate --fail-fast
python3 scripts/run_regressions.py --match clear --fail-fast
```

Results:

```text
watchdog: 59 passed, 0 failed
alarm: 28 passed, 0 failed
extend_wait: 10 passed, 0 failed
ingressing: 7 passed, 0 failed
aggregate: 46 passed, 0 failed
clear: 52 passed, 0 failed
```

Full suite, run because watchdog/alarm helpers and facade wrappers moved:

```sh
python3 scripts/run_regressions.py
```

Result:

```text
615 passed, 0 failed
```

The full run emitted the known live-session-dependent skip line for
`list_peers_json_daemon_status_strips_pid`; the final summary remained green.
