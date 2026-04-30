# Daemon Refactor Stage 3 Notes

Date: 2026-04-30

Stage 3 scope: introduce state containers and a single-lock facade while
preserving existing `BridgeDaemon` field access, command lock behavior, and the
single physical `state_lock`.

## Reviewer Roles

- `worker`: owns the Stage 3 diff, validation, reviewer coordination, and
  eventual commit.
- `reviewer1`: primary reviewer and structure reviewer.
- `reviewer2`: concurrency reviewer and test reviewer.
- `manager`: not assigned a reviewer role.

## Files Changed

- `libexec/agent-bridge/bridge_daemon.py`
- `libexec/agent-bridge/bridge_daemon_state.py`
- `docs/daemon-refactor-stage3-notes.md`

## State Module

Added to `bridge_daemon_state.py`:

```text
StateField
BoundedSet
LockFacade
ParticipantCache
RoutingState
WatchdogState
ClearState
MaintenanceState
```

`StateField` is a small descriptor that keeps existing `self.field` reads and
writes compatible while storing the value under a per-instance state object.
The descriptor does not run daemon behavior, logging, tmux, queue/aggregate
operations, or file I/O.

`BoundedSet` moved from `bridge_daemon.py` to `bridge_daemon_state.py`.
`bridge_daemon.py` imports and re-exports it for compatibility.

## Moved Fields

`ParticipantCache` owns:

```text
session_state
participants
panes
session_mtime_ns
startup_backfill_summary
```

`RoutingState` owns:

```text
busy
reserved
current_prompt_by_agent
injected_by_nonce
last_enter_ts
interrupted_turns
```

`WatchdogState` owns:

```text
processed_returns
processed_capture_requests
watchdogs
alarm_wake_tombstones
```

`ClearState` owns:

```text
held_interrupt
interrupt_partial_failure_blocks
clear_reservations
pending_self_clears
```

`MaintenanceState` owns:

```text
last_maintenance
last_capture_cleanup
last_ingressing_check
stop_logged
last_delivery_tick
```

`LockFacade` owns:

```text
state_lock
```

## Intentional Non-Moves

`command_context = threading.local()` remains directly on `BridgeDaemon`.
It is thread-local command execution context, not shared routing state.

Path/config/runtime setup also remains directly on `BridgeDaemon`, including
queue/store paths, command socket fields, daemon args, timing configuration,
and command server thread/socket fields.

## Lock Compatibility

`BridgeDaemon.state_lock` is a `StateField` that returns the exact raw
underlying RLock object stored on `LockFacade`. No per-domain physical locks
were introduced.

Compatibility checks:

```text
d.state_lock is d.lock_facade.state_lock => True
type(d.state_lock).__module__ => _thread
threading.Condition(d.state_lock) => succeeds
```

Existing lock usage remains unchanged:

```text
with self.state_lock
self.state_lock.acquire()
self.state_lock.acquire(timeout=...)
self.state_lock.release()
self.state_lock._is_owned()
threading.Condition(self.state_lock)
```

`command_state_lock()` logic, constants, budgets, and call sites were not
changed.

## Descriptor Reassignment Checks

Descriptor-backed fields that are reassigned after initialization write through
to the backing state object. Explicit checks were run for:

```text
participants
session_mtime_ns
last_delivery_tick
startup_backfill_summary
```

`bridge_daemon.BoundedSet is bridge_daemon_state.BoundedSet` was also confirmed
as `True`.

## Explicit Non-Changes

- No delivery transitions moved.
- No clear, interrupt, watchdog, aggregate, queue, tmux, or file-order behavior
  changed.
- No queue/aggregate store behavior changed.
- No direct state access was hidden behind a method that performs tmux, file
  I/O, queue/aggregate mutation, or logging.
- No physical lock splitting was introduced.

## Validation

Syntax/import check:

```sh
python3 -m py_compile \
  libexec/agent-bridge/bridge_daemon.py \
  libexec/agent-bridge/bridge_daemon_state.py
```

Result: passed.

Focused Stage 3 suites:

```sh
python3 scripts/run_regressions.py --match command_state_lock --fail-fast
python3 scripts/run_regressions.py --match wait_status --fail-fast
python3 scripts/run_regressions.py --match interrupt --fail-fast
python3 scripts/run_regressions.py --match clear --fail-fast
```

Results:

```text
command_state_lock: 1 passed, 0 failed
wait_status: 8 passed, 0 failed
interrupt: 49 passed, 0 failed
clear: 52 passed, 0 failed
```

Full suite, run because `d.state_lock` and broad state field compatibility
changed:

```sh
python3 scripts/run_regressions.py
```

Result:

```text
615 passed, 0 failed
```

The full run emitted the known live-session-dependent skip line for
`list_peers_json_daemon_status_strips_pid`; the final summary remained green.
