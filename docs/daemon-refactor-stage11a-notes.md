# Daemon Refactor Stage 11a Notes

Date: 2026-04-30

Stage 11a scope: add a passive maintenance scheduler/thread harness while
keeping all housekeeping work in the event-tail loop and preserving one
physical daemon state lock.

## Reviewer Roles

- `worker`: owns the Stage 11a diff, validation, reviewer coordination, and
  eventual commit.
- `reviewer2`: primary reviewer, concurrency reviewer, and test reviewer.
- `reviewer1`: structure reviewer for module boundaries, imports, and facade
  compatibility.
- `manager`: not assigned a reviewer role.

## Files Changed

- `libexec/agent-bridge/bridge_daemon.py`
- `libexec/agent-bridge/bridge_daemon_maintenance.py`
- `docs/daemon-refactor-stage11a-notes.md`

## Maintenance Scheduler Harness

Added `MaintenanceScheduler` to `bridge_daemon_maintenance.py`.

The harness owns:

```text
_stop_event
_wake_event
_thread
start_count
stop_count
```

Public methods:

```text
start(d)
stop(timeout=2.0)
wake()
is_running()
loop(d)
```

The scheduler thread is passive in Stage 11a. It waits on an event and exits
when stopped. It does not run daemon maintenance tasks, mutate daemon state,
log, acquire daemon locks, touch queue or aggregate files, or run tmux.

`stop()` is idempotent: it sets stop/wake events, joins a live thread, clears
the thread reference when stopped, and tolerates being called before start or
after an earlier stop.

## Daemon Facade

`BridgeDaemon.__init__` now initializes:

```text
self.maintenance_scheduler = MaintenanceScheduler()
```

`BridgeDaemon` adds thin wrappers:

```text
start_maintenance_scheduler()
stop_maintenance_scheduler()
wake_maintenance_scheduler()
maintenance_scheduler_running()
```

The existing `delivery_scheduler` and raw `state_lock` are unchanged.

## Follow Lifecycle

`follow()` now starts the scheduler inside a `try/finally` that always stops it
after the scheduler can be started:

```text
start_command_server()
try:
    if not self.once:
        start_maintenance_scheduler()
    cleanup_capture_responses(force=True)
    open state stream
    startup logging/recovery
    existing event-tail loop
finally:
    stop_maintenance_scheduler()
    stop_command_server()
```

This covers failures from startup cleanup, file open, startup logging,
recovery, and event-tail handling. `--once` daemons never start the maintenance
thread.

Stop order is scheduler first, then command server. Because no maintenance
tasks moved into the scheduler in this stage, this does not introduce a new
lock or routing order.

Active command-worker join behavior remains staged for a later maintenance
commit. Stage 11a preserves the existing command socket shutdown behavior:
`stop_command_server()` closes/unlinks the server socket, but active worker
threads are not joined here.

## Explicit Non-Moves

All housekeeping remains in the event-tail loop:

```text
requeue_stale_inflight()
retry_enter_for_inflight()
check_watchdogs()
expire_turn_id_mismatch_contexts()
_promote_aged_ingressing()
periodic delivery tick
cleanup_capture_responses()
```

No retry-enter, watchdog, turn-id mismatch, aged-ingressing, stale requeue,
capture cleanup, or delivery tick logic moved to the scheduler in Stage 11a.

## Validation

Syntax/import check:

```sh
python3 -m py_compile \
  libexec/agent-bridge/bridge_daemon.py \
  libexec/agent-bridge/bridge_daemon_maintenance.py \
  libexec/agent-bridge/bridge_daemon_commands.py
```

Result: passed.

Explicit scheduler lifecycle checks:

```text
MaintenanceScheduler starts a daemon thread for non-once daemon
start() is idempotent while running
stop_maintenance_scheduler() joins and clears the thread reference
stop_maintenance_scheduler() is idempotent
wake_maintenance_scheduler() is safe before start, during run, and after stop
BridgeDaemon.follow once=True does not start the scheduler
startup exception after scheduler start stops scheduler and propagates
scheduler loop start/wake/stop calls no housekeeping methods
d.state_lock remains d.lock_facade.state_lock
threading.Condition(d.state_lock) still works
```

Result: passed.

Housekeeping location check:

```sh
rg -n "requeue_stale_inflight\\(|retry_enter_for_inflight\\(|check_watchdogs\\(|expire_turn_id_mismatch_contexts\\(|_promote_aged_ingressing\\(|cleanup_capture_responses\\(|last_delivery_tick" \
  libexec/agent-bridge/bridge_daemon.py \
  libexec/agent-bridge/bridge_daemon_maintenance.py
```

Result: housekeeping calls remain in `BridgeDaemon.follow`; the scheduler
harness does not call them.

Focused Stage 11a suites:

```sh
python3 scripts/run_regressions.py --match lifecycle --fail-fast
python3 scripts/run_regressions.py --match bridge_daemon_ctl --fail-fast
python3 scripts/run_regressions.py --match daemon_follow_from_start --fail-fast
python3 scripts/run_regressions.py --match restart_dry_run --fail-fast
```

Results:

```text
lifecycle: 1 passed, 0 failed
bridge_daemon_ctl: 3 passed, 0 failed
daemon_follow_from_start: 3 passed, 0 failed
restart_dry_run: 1 passed, 0 failed
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
