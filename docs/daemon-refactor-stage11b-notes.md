# Daemon Refactor Stage 11b Notes

Date: 2026-04-30

Stage 11b scope: move core time-based maintenance tasks into the
maintenance scheduler while keeping one physical daemon state lock and leaving
retry-enter, periodic delivery tick, and capture cleanup in the event-tail
loop.

## Reviewer Roles

- `worker`: owns the Stage 11b diff, validation, reviewer coordination, and
  eventual commit.
- `reviewer2`: primary reviewer, concurrency reviewer, and test reviewer.
- `reviewer1`: structure reviewer for module boundaries, imports, and facade
  compatibility.
- `manager`: not assigned a reviewer role.

## Files Changed

- `libexec/agent-bridge/bridge_daemon.py`
- `libexec/agent-bridge/bridge_daemon_maintenance.py`
- `docs/daemon-refactor-stage11b-notes.md`

## Scheduler Task Migration

`MaintenanceScheduler` now owns the fixed core maintenance task list:

```text
stale_inflight -> d.requeue_stale_inflight()
watchdogs -> d.check_watchdogs()
turn_id_mismatch -> d.expire_turn_id_mismatch_contexts()
aged_ingressing -> d._promote_aged_ingressing()
```

The same `run_once(d)` task list and order is used by both the background
scheduler thread and the deterministic `--once` inline path.

Each task exception is caught per task, logged with:

```text
d.safe_log("maintenance_task_failed", task=<task_name>, error=repr(exc))
```

and later tasks continue. The scheduler itself does not acquire daemon locks;
tasks keep their existing daemon-side locking behavior.

## Non-Overlap And Wake Behavior

`MaintenanceScheduler.run_once(d)` uses a scheduler-local nonblocking run lock.
If a run is already active, a second inline run returns `False` instead of
overlapping task execution. This lock belongs only to the scheduler and is not
the daemon state lock.

The background loop runs one immediate task pass, then waits on the wake/stop
event with the existing short cadence. Repeated `wake()` calls do not create
additional workers or concurrent task executions.

## Shutdown Semantics

`BridgeDaemon.stop_maintenance_scheduler()` now calls:

```text
self.maintenance_scheduler.stop(timeout=None)
```

Daemon shutdown therefore uses a blocking join. If a maintenance task is
blocked behind an active command or state-lock holder, shutdown waits until
that task exits and then clears the scheduler thread reference. This satisfies
the Stage 11b requirement that shutdown while a command is active must not
leave the scheduler alive.

The stop method remains idempotent and still supports a bounded timeout for
direct tests or future non-daemon callers.

## Follow Loop Behavior

For non-`--once` daemons, `follow()` no longer calls the moved tasks directly
from the event-tail loop. The background scheduler owns:

```text
requeue_stale_inflight()
check_watchdogs()
expire_turn_id_mismatch_contexts()
_promote_aged_ingressing()
```

For `--once`, no scheduler thread is started. `follow()` calls
`run_maintenance_once()` at the old moved-task position before the EOF break,
so deterministic one-shot daemons still run the moved maintenance work.

Retained event-tail tasks remain in place:

```text
retry_enter_for_inflight()
periodic delivery tick
cleanup_capture_responses()
```

Stage 11b intentionally does not move retry-enter, delivery tick, capture
cleanup, command socket lifecycle, delivery routing, watchdog logic, aggregate
logic, interrupt flow, clear flow, or tmux behavior.

## Validation

Syntax/import check:

```sh
python3 -m py_compile \
  libexec/agent-bridge/bridge_daemon.py \
  libexec/agent-bridge/bridge_daemon_maintenance.py \
  libexec/agent-bridge/bridge_daemon_watchdogs.py \
  libexec/agent-bridge/bridge_daemon_delivery.py \
  libexec/agent-bridge/bridge_daemon_events.py
```

Result: passed.

Explicit Stage 11b scheduler checks:

```text
run_once uses the fixed task order shared by background and --once paths
task exceptions log maintenance_task_failed and later tasks continue
concurrent run_once returns False while a task is already running
--once starts no scheduler thread and runs inline maintenance before EOF break
non-once follow does not directly call moved tasks from the event-tail loop
blocking daemon shutdown waits for a state_lock-blocked scheduler task
no bridge-maintenance-scheduler thread remains after blocking stop completes
```

Result: passed.

Focused Stage 11b suites:

```sh
python3 scripts/run_regressions.py --match watchdog --fail-fast
python3 scripts/run_regressions.py --match turn_id_mismatch --fail-fast
python3 scripts/run_regressions.py --match aged_ingressing --fail-fast
python3 scripts/run_regressions.py --match requeue --fail-fast
python3 scripts/run_regressions.py --match lifecycle --fail-fast
python3 scripts/run_regressions.py --match bridge_daemon_ctl --fail-fast
python3 scripts/run_regressions.py --match daemon_follow_from_start --fail-fast
python3 scripts/run_regressions.py --match retry_enter --fail-fast
python3 scripts/run_regressions.py --match delivery --fail-fast
python3 scripts/run_regressions.py --match command_state_lock --fail-fast
python3 scripts/run_regressions.py --match ingressing --fail-fast
python3 scripts/run_regressions.py --match aggregate --fail-fast
```

Results:

```text
watchdog: 59 passed, 0 failed
turn_id_mismatch: 15 passed, 0 failed
aged_ingressing: 3 passed, 0 failed
requeue: 4 passed, 0 failed
lifecycle: 1 passed, 0 failed
bridge_daemon_ctl: 3 passed, 0 failed
daemon_follow_from_start: 3 passed, 0 failed
retry_enter: 2 passed, 0 failed
delivery: 19 passed, 0 failed
command_state_lock: 1 passed, 0 failed
ingressing: 7 passed, 0 failed
aggregate: 46 passed, 0 failed
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
