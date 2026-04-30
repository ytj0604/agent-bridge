# Daemon Refactor Stage 10b Notes

Date: 2026-04-30

Stage 10b scope: convert normal `queue_message(..., deliver=True)` delivery
requests to the Stage 10a scheduler facade while preserving immediate inline
drain behavior and leaving direct domain trigger sites for later stages.

## Reviewer Roles

- `worker`: owns the Stage 10b diff, validation, reviewer coordination, and
  eventual commit.
- `reviewer2`: primary reviewer, concurrency reviewer, and test reviewer.
- `reviewer1`: structure reviewer for module boundaries, imports, and facade
  compatibility.
- `manager`: not assigned a reviewer role.

## Files Changed

- `libexec/agent-bridge/bridge_daemon_delivery.py`
- `docs/daemon-refactor-stage10b-notes.md`

## Queue Message Conversion

The only runtime code change is in `bridge_daemon_delivery.queue_message`.

Previous delivery block:

```text
d.try_deliver_command_aware(str(message["to"]), message_id=str(message.get("id") or ""))
```

New delivery block:

```text
d.request_and_drain_delivery(
    str(message["to"]),
    message_id=str(message.get("id") or ""),
    command_aware=True,
    reason="queue_message",
)
```

This preserves:

```text
target = str(message["to"])
message_id = str(message.get("id") or "")
command-aware delivery path
immediate inline drain timing
post-queue-update and post-message_queued log ordering
```

`queue_message(..., deliver=False)` still does not request or drain delivery.

Existing duplicate-id behavior is preserved: the queue mutator skips appending
the duplicate row, but the existing post-mutator log and delivery control flow
still runs. Stage 10b changes only the delivery call mechanism.

## Explicit Non-Conversions

No other direct delivery trigger sites moved in Stage 10b. The following
domains still call the direct daemon delivery APIs where they did before:

```text
clear
interrupt
event terminal handling
event prompt handling
command enqueue replacement delivery
watchdog fire
aggregate completion, except where it already routes through queue_message
retry-enter
maintenance loop delivery tick
turn-id mismatch expiry
stale inflight requeue
```

The direct trigger inventory was checked with:

```sh
rg -n "try_deliver_command_aware|try_deliver\\(|queue_message\\(" \
  libexec/agent-bridge/bridge_daemon.py \
  libexec/agent-bridge/bridge_daemon_*.py
```

Result: only the normal `queue_message(..., deliver=True)` producer now routes
through `d.request_and_drain_delivery(...)`; direct domain triggers remain
direct.

## Scheduler Behavior

The scheduler remains the passive Stage 10a facade:

- no background thread
- no timers or sleeps
- no new locks
- no logging
- no direct queue/file/tmux access
- no retained queue/history

Command deadline-aware delivery deferral still flows through
`d.try_deliver_command_aware(...)` during scheduler drain, so
`command_delivery_deferred_deadline` and lock-wait deferral logging remain in
the existing delivery implementation.

## Validation

Syntax/import check:

```sh
python3 -m py_compile \
  libexec/agent-bridge/bridge_daemon.py \
  libexec/agent-bridge/bridge_daemon_delivery.py \
  libexec/agent-bridge/bridge_daemon_maintenance.py
```

Result: passed.

Explicit queue scheduler checks:

```text
queue_message(deliver=True) calls d.request_and_drain_delivery exactly once
queue_message(deliver=True) passes target, message_id, command_aware=True, reason="queue_message"
queue_message(deliver=True) no longer calls d.try_deliver_command_aware directly
queue_message(deliver=False) does not request/drain delivery
duplicate-id queue_message preserves one queue row and still follows post-mutator delivery flow
command deadline-aware deferral still logs command_delivery_deferred_deadline
```

Result: passed.

Focused Stage 10b suites:

```sh
python3 scripts/run_regressions.py --match pending_replacement --fail-fast
python3 scripts/run_regressions.py --match clear_success_delivery --fail-fast
python3 scripts/run_regressions.py --match response --fail-fast
python3 scripts/run_regressions.py --match delivery --fail-fast
python3 scripts/run_regressions.py --match aggregate_completion --fail-fast
python3 scripts/run_regressions.py --match aggregate --fail-fast
python3 scripts/run_regressions.py --match watchdog_fire --fail-fast
python3 scripts/run_regressions.py --match command_state_lock --fail-fast
```

Results:

```text
pending_replacement: 1 passed, 0 failed
clear_success_delivery: 2 passed, 0 failed
response: 30 passed, 0 failed
delivery: 19 passed, 0 failed
aggregate_completion: 1 passed, 0 failed
aggregate: 46 passed, 0 failed
watchdog_fire: 4 passed, 0 failed
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
