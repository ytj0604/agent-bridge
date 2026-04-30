# Daemon Refactor Stage 7 Notes

Date: 2026-04-30

Stage 7 scope: extract reservation, pane-mode deferral, tmux delivery,
retry-enter, and undeliverable finalization into a delivery service while
preserving the existing single daemon lock scope and facade compatibility.

## Reviewer Roles

- `worker`: owns the Stage 7 diff, validation, reviewer coordination, and
  eventual commit.
- `reviewer2`: primary reviewer, concurrency reviewer, and test reviewer.
- `reviewer1`: structure reviewer for module boundaries, imports, and facade
  compatibility.
- `manager`: not assigned a reviewer role.

## Files Changed

- `libexec/agent-bridge/bridge_daemon.py`
- `libexec/agent-bridge/bridge_daemon_delivery.py`
- `docs/daemon-refactor-stage7-notes.md`

## Delivery Module

Added `bridge_daemon_delivery.py` as a function-based delivery service that
takes the daemon instance as `d`. The module does not import
`bridge_daemon.py`.

Moved constants and helpers:

```text
PANE_MODE_FORCE_CANCEL_MODES
TMUX_DELIVERY_WORST_CASE_SECONDS
PANE_MODE_METADATA_KEYS
PANE_MODE_ENTER_DEFER_KEYS
RESTART_INFLIGHT_METADATA_KEYS
pane_mode_block_since_ts
```

`bridge_daemon.py` re-exports those names for compatibility.

Moved implementations:

```text
try_deliver_command_aware
next_pending_candidate
annotate_pending_pane_mode_block
clear_pane_mode_metadata
mark_pane_mode_unforceable
mark_pane_mode_cancel_failed
mark_pane_mode_probe_failed
defer_inflight_for_pane_mode_probe_failed
blocked_duration
maybe_defer_for_pane_mode
defer_inflight_for_pane_mode
mark_enter_deferred_for_pane_mode
clear_enter_deferred_metadata
queue_message
reserve_next
try_deliver
deliver_reserved
mark_message_pending
mark_message_submitted
mark_message_delivered_by_id
remove_delivered_message
finalize_undeliverable_message
retry_enter_for_inflight
```

`BridgeDaemon` keeps thin wrappers for all moved methods.

## Compatibility

Delivery paths call through `d.*` where tests or downstream code may override
daemon behavior:

```text
d.try_deliver
d.deliver_reserved
d.try_deliver_command_aware
d.queue_message
d.run_tmux_send_literal
d.run_tmux_enter
pane-mode helper wrappers
```

`BridgeDaemon.run_tmux_send_literal(...)` and
`BridgeDaemon.run_tmux_enter(...)` are narrow wrappers around the existing
`bridge_daemon.py` module-global names. This preserves tests and callers that
monkeypatch `bridge_daemon.run_tmux_send_literal` or
`bridge_daemon.run_tmux_enter`.

`try_deliver_command_aware` moved without importing `CommandLockWaitExceeded`.
It catches only exceptions whose class name is `CommandLockWaitExceeded`,
preserves the prior `command_delivery_deferred_lock_wait` log fields and
`exc.command_class` fallback behavior, and re-raises unrelated exceptions.

## Lock And Ordering Invariants

- `try_deliver` still calls `d.reload_participants()` before acquiring
  `d.state_lock`.
- `try_deliver` still holds `d.state_lock` through target iteration,
  `d.next_pending_candidate`, pane-mode pre-reserve checks, `d.reserve_next`,
  and `d.deliver_reserved`.
- `reserve_next` still runs its queue update under `d.state_lock`, blocks
  behind inflight/submitted/delivered rows for the same target, and arms the
  delivery watchdog before returning.
- `deliver_reserved` remains called from inside `try_deliver`'s lock scope,
  and pane-mode probes plus tmux paste/enter stay in their prior positions.
- `mark_message_delivered_by_id` keeps its own `d.state_lock` scope around
  authoritative delivery completion.
- `retry_enter_for_inflight` still holds `d.state_lock` across the queue scan,
  endpoint checks, pane-mode checks, and tmux enter retry.
- `finalize_undeliverable_message` preserves queue removal, nonce/reserved/current
  prompt cleanup, watchdog cancellation/suppression, tombstone recording,
  aggregate synthetic reply handling, and undeliverable result queueing.

## Explicit Non-Changes

- Command socket dispatch did not move.
- Hook/event routing did not move.
- Interrupt and controlled clear flows did not move.
- Aggregate collection/completion did not move.
- Watchdog/alarm service behavior did not move.
- Participant and endpoint identity resolution did not move.
- Tmux helper implementations did not move.
- No lock splitting or tmux lock-scope changes were introduced.

## Validation

Syntax/import check:

```sh
python3 -m py_compile \
  libexec/agent-bridge/bridge_daemon.py \
  libexec/agent-bridge/bridge_daemon_delivery.py \
  libexec/agent-bridge/bridge_daemon_watchdogs.py
```

Result: passed.

Explicit compatibility checks:

```text
BridgeDaemon wrappers exist for all moved delivery methods
bridge_daemon_delivery does not import bridge_daemon.py
delivery constants re-export from bridge_daemon.py
monkeypatching bridge_daemon.run_tmux_send_literal intercepts d.try_deliver
monkeypatching bridge_daemon.run_tmux_enter intercepts d.try_deliver
monkeypatching bridge_daemon.run_tmux_enter intercepts d.retry_enter_for_inflight
try_deliver_command_aware logs class-name CommandLockWaitExceeded exceptions
try_deliver_command_aware re-raises unrelated exceptions
```

Result: passed.

Focused Stage 7 suites:

```sh
python3 scripts/run_regressions.py --match pane_mode --fail-fast
python3 scripts/run_regressions.py --match delivery_watchdog --fail-fast
python3 scripts/run_regressions.py --match retry_enter --fail-fast
python3 scripts/run_regressions.py --match identity --fail-fast
python3 scripts/run_regressions.py --match interrupt --fail-fast
python3 scripts/run_regressions.py --match clear --fail-fast
python3 scripts/run_regressions.py --match watchdog --fail-fast
python3 scripts/run_regressions.py --match ingressing --fail-fast
python3 scripts/run_regressions.py --match tmux_paste_buffer_delivery_sequence --fail-fast
python3 scripts/run_regressions.py --match prompt_submitted --fail-fast
```

Results:

```text
pane_mode: 13 passed, 0 failed
delivery_watchdog: 12 passed, 0 failed
retry_enter: 2 passed, 0 failed
identity: 10 passed, 0 failed
interrupt: 49 passed, 0 failed
clear: 52 passed, 0 failed
watchdog: 59 passed, 0 failed
ingressing: 7 passed, 0 failed
tmux_paste_buffer_delivery_sequence: 1 passed, 0 failed
prompt_submitted: 5 passed, 0 failed
```

Full suite, run because delivery, pane-mode, and completion facades moved:

```sh
python3 scripts/run_regressions.py
```

Result:

```text
615 passed, 0 failed
```

The full run emitted the known live-session-dependent skip line for
`list_peers_json_daemon_status_strips_pid`; the final summary remained green.
