# Daemon Refactor Stage 11c Notes

Date: 2026-04-30

Stage 11c scope: finish the maintenance scheduler migration by moving
retry-enter, periodic delivery tick, and capture cleanup into the existing
maintenance scheduler task pass while preserving one physical daemon state
lock.

## Reviewer Roles

- `worker`: owns the Stage 11c diff, validation, reviewer coordination, and
  eventual commit.
- `reviewer2`: primary reviewer, concurrency reviewer, and test reviewer.
- `reviewer1`: structure reviewer for module boundaries, imports, and facade
  compatibility.
- `manager`: not assigned a reviewer role.

## Files Changed

- `libexec/agent-bridge/bridge_daemon.py`
- `libexec/agent-bridge/bridge_daemon_maintenance.py`
- `docs/daemon-refactor-stage11c-notes.md`

## Scheduler Task Order

`MaintenanceScheduler.TASKS` now owns the full event-tail maintenance set in
this fixed order:

```text
stale_inflight -> d.requeue_stale_inflight()
watchdogs -> d.check_watchdogs()
turn_id_mismatch -> d.expire_turn_id_mismatch_contexts()
aged_ingressing -> d._promote_aged_ingressing()
retry_enter -> d.retry_enter_for_inflight()
delivery_tick -> d.maintenance_delivery_tick()
capture_cleanup -> d.cleanup_capture_responses()
```

The scheduler-local nonblocking run lock from Stage 11b remains the only
scheduler guard. It prevents overlapping task passes without adding a daemon
state lock.

Per-task exception logging is unchanged:

```text
d.safe_log("maintenance_task_failed", task=<task_name>, error=repr(exc))
```

Later tasks continue after a task failure, including capture cleanup after a
delivery tick failure.

## Delivery Tick Facade

`BridgeDaemon.maintenance_delivery_tick()` preserves the old event-tail
delivery tick logic:

```text
now = time.time()
if now - self.last_delivery_tick >= 0.5:
    self.last_delivery_tick = now
    self.try_deliver()
```

It does not use the command-aware delivery path, scheduler delivery requests,
logging, or new locks.

## Follow Loop Behavior

`BridgeDaemon.follow()` no longer directly calls these maintenance tasks from
the event-tail loop:

```text
retry_enter_for_inflight()
periodic delivery tick
cleanup_capture_responses()
```

For non-`--once` daemons, the scheduler thread owns all seven maintenance
tasks. For `--once`, no scheduler thread is started; `run_maintenance_once()`
runs the same expanded task pass at the old maintenance position before the
EOF break.

Startup capture cleanup remains separate and unchanged:

```text
cleanup_capture_responses(force=True)
```

## Explicit Non-Moves

Stage 11c does not move capture request handling, command socket lifecycle,
event dispatch, delivery implementation, watchdog implementation, clear flow,
interrupt flow, aggregate flow, or tmux behavior.

## Validation

Syntax/import check:

```sh
python3 -m py_compile \
  libexec/agent-bridge/bridge_daemon.py \
  libexec/agent-bridge/bridge_daemon_maintenance.py \
  libexec/agent-bridge/bridge_daemon_delivery.py \
  libexec/agent-bridge/bridge_daemon_events.py \
  libexec/agent-bridge/bridge_daemon_watchdogs.py
```

Result: passed.

Explicit Stage 11c scheduler checks:

```text
TASKS order is stale_inflight, watchdogs, turn_id_mismatch, aged_ingressing,
  retry_enter, delivery_tick, capture_cleanup
run_once executes the same seven-task order
delivery_tick exceptions log maintenance_task_failed and capture_cleanup still runs
concurrent run_once callers return False while a task pass is active
--once starts no scheduler thread and runs the expanded task pass before EOF break
startup cleanup_capture_responses(force=True) still runs separately before the loop
non-once follow does not call moved tasks directly from the event-tail loop
maintenance_delivery_tick below 0.5 seconds does not call delivery
maintenance_delivery_tick at 0.5 seconds updates last_delivery_tick and calls try_deliver()
maintenance_delivery_tick does not call try_deliver_command_aware() or request_and_drain_delivery()
```

Result: passed.

Focused Stage 11c suites:

```sh
python3 scripts/run_regressions.py --match retry_enter --fail-fast
python3 scripts/run_regressions.py --match delivery --fail-fast
python3 scripts/run_regressions.py --match capture --fail-fast
python3 scripts/run_regressions.py --match watchdog --fail-fast
python3 scripts/run_regressions.py --match requeue --fail-fast
python3 scripts/run_regressions.py --match lifecycle --fail-fast
python3 scripts/run_regressions.py --match daemon_follow_from_start --fail-fast
python3 scripts/run_regressions.py --match command_state_lock --fail-fast
```

Results:

```text
retry_enter: 2 passed, 0 failed
delivery: 19 passed, 0 failed
capture: 2 passed, 0 failed
watchdog: 59 passed, 0 failed
requeue: 4 passed, 0 failed
lifecycle: 1 passed, 0 failed
daemon_follow_from_start: 3 passed, 0 failed
command_state_lock: 1 passed, 0 failed
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
