# Daemon Refactor Stage 10c Notes

Date: 2026-04-30

Stage 10c scope: convert the named clear, interrupt, terminal response,
watchdog, and aggregate delivery triggers to the scheduler facade while
preserving immediate inline delivery timing and keeping one physical state
lock.

## Reviewer Roles

- `worker`: owns the Stage 10c diff, validation, reviewer coordination, and
  eventual commit.
- `reviewer2`: primary reviewer, concurrency reviewer, and test reviewer.
- `reviewer1`: structure reviewer for module boundaries, imports, and facade
  compatibility.
- `manager`: not assigned a reviewer role.

## Files Changed

- `libexec/agent-bridge/bridge_daemon_clear_flow.py`
- `libexec/agent-bridge/bridge_daemon_events.py`
- `libexec/agent-bridge/bridge_daemon_interrupts.py`
- `docs/daemon-refactor-stage10c-notes.md`

## Converted Triggers

Clear completion:

```text
clear_batch_release -> d.request_and_drain_delivery(target, command_aware=True, reason="clear_batch_release")
clear_success       -> d.request_and_drain_delivery(target, command_aware=True, reason="clear_success")
```

Interrupt and hold release:

```text
interrupt_success   -> d.request_and_drain_delivery(target, command_aware=True, reason="interrupt_success")
hold_release_target -> d.request_and_drain_delivery(target, command_aware=True, reason="hold_release_target")
hold_release_global -> d.request_and_drain_delivery(command_aware=True, reason="hold_release_global")
```

Terminal response and held-stop event paths:

```text
held_stop_sender          -> d.request_and_drain_delivery(sender, command_aware=False, reason="held_stop_sender")
held_stop_global          -> d.request_and_drain_delivery(command_aware=False, reason="held_stop_global")
response_finished_sender  -> d.request_and_drain_delivery(sender, command_aware=False, reason="response_finished_sender")
response_finished_global  -> d.request_and_drain_delivery(command_aware=False, reason="response_finished_global")
```

The terminal response conversions use `command_aware=False` to preserve the old
event-tail `d.try_deliver(...)` behavior and avoid adding command deadline
semantics to hook paths.

## Already Converted Through Queue Message

Watchdog fire and aggregate completion were already routed through the
Stage 10b `queue_message(..., deliver=True/default)` conversion:

```text
watchdog fire -> d.queue_message(synthetic, log_event=True)
aggregate completion -> d.queue_message(message, deliver=deliver)
```

Stage 10c does not add extra drains for those paths. Validation confirms they
still drain exactly once through the `queue_message` scheduler path when
delivery is enabled.

## Explicit Non-Conversions

The following direct triggers intentionally remain direct for later stages:

```text
command cancel replacement delivery
prompt_submitted / prompt interception delivery
external file-fallback message_queued delivery
stale inflight requeue delivery
turn-id mismatch expiry delivery
maintenance loop delivery tick
startup recovery delivery
```

Those paths are either outside the Stage 10c named domain set or belong with
the Stage 11 maintenance scheduler migration.

Direct trigger inventory after Stage 10c:

```sh
rg -n "try_deliver_command_aware\\(|try_deliver\\(|request_and_drain_delivery" \
  libexec/agent-bridge/bridge_daemon.py \
  libexec/agent-bridge/bridge_daemon_*.py
```

Result: clear completion, interrupt/hold release, and terminal response sites
now call `d.request_and_drain_delivery(...)`; watchdog and aggregate completion
continue through `queue_message`; the non-converted direct triggers above
remain direct and documented.

## Lock And Ordering Invariants

- Converted clear and interrupt command paths remain command-aware and keep
  their old call positions after lock release.
- Partial interrupt key failure still logs `interrupt_delivery_skipped` and
  does not request delivery.
- Held release still drains target first and global second.
- Terminal response and held-stop paths still drain sender/target first and
  global second, after the existing state cleanup lock section.
- No converted path changes state-lock scope.
- The scheduler remains passive: no new locks, timers, sleeps, logging,
  queue/file/tmux access, or retained queue/history.

## Validation

Syntax/import check:

```sh
python3 -m py_compile \
  libexec/agent-bridge/bridge_daemon.py \
  libexec/agent-bridge/bridge_daemon_events.py \
  libexec/agent-bridge/bridge_daemon_interrupts.py \
  libexec/agent-bridge/bridge_daemon_clear_flow.py \
  libexec/agent-bridge/bridge_daemon_watchdogs.py \
  libexec/agent-bridge/bridge_daemon_aggregates.py \
  libexec/agent-bridge/bridge_daemon_delivery.py \
  libexec/agent-bridge/bridge_daemon_maintenance.py
```

Result: passed.

Explicit scheduler conversion checks:

```text
single clear success drains once with reason=clear_success and command_aware=True
clear batch release drains once per released target, in target order, with reason=clear_batch_release
interrupt success drains once with reason=interrupt_success and command_aware=True
partial interrupt key failure does not request delivery
release_hold drains target then global with command_aware=True
terminal response drains sender then global with command_aware=False
terminal response scheduler calls occur after busy/reserved/current_prompt cleanup
converted paths do not call direct d.try_deliver or d.try_deliver_command_aware in the checked paths
```

Result: passed.

Focused Stage 10c suites:

```sh
python3 scripts/run_regressions.py --match watchdog_fire --fail-fast
python3 scripts/run_regressions.py --match aggregate_completion --fail-fast
python3 scripts/run_regressions.py --match interrupt --fail-fast
python3 scripts/run_regressions.py --match clear_success_delivery --fail-fast
python3 scripts/run_regressions.py --match response_finished --fail-fast
python3 scripts/run_regressions.py --match response --fail-fast
python3 scripts/run_regressions.py --match clear --fail-fast
python3 scripts/run_regressions.py --match aggregate --fail-fast
python3 scripts/run_regressions.py --match delivery --fail-fast
python3 scripts/run_regressions.py --match command_state_lock --fail-fast
```

Results:

```text
watchdog_fire: 4 passed, 0 failed
aggregate_completion: 1 passed, 0 failed
interrupt: 49 passed, 0 failed
clear_success_delivery: 2 passed, 0 failed
response_finished: 1 passed, 0 failed
response: 30 passed, 0 failed
clear: 52 passed, 0 failed
aggregate: 46 passed, 0 failed
delivery: 19 passed, 0 failed
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
