# Daemon Refactor Stage 12 Notes

Date: 2026-04-30

Stage 12 scope: introduce target-lock plumbing for normal delivery,
prompt-submitted, and response-finished paths while preserving the single
physical daemon `state_lock` and keeping tmux I/O globally serialized.

## Reviewer Roles

- `worker`: owns the Stage 12 diff, validation, reviewer coordination, and
  eventual commit.
- `reviewer2`: primary reviewer, concurrency reviewer, and test reviewer.
- `reviewer1`: structure reviewer for module boundaries, imports, and facade
  compatibility.
- `manager`: not assigned a reviewer role.

## Files Changed

- `libexec/agent-bridge/bridge_daemon.py`
- `libexec/agent-bridge/bridge_daemon_delivery.py`
- `libexec/agent-bridge/bridge_daemon_events.py`
- `libexec/agent-bridge/bridge_daemon_maintenance.py`
- `libexec/agent-bridge/bridge_daemon_state.py`
- `docs/daemon-refactor-stage12-notes.md`

## Target Lock Manager

Added `TargetLockManager` to `bridge_daemon_state.py`.

The manager:

```text
normalizes target sets as sorted unique aliases
acquires target locks in deterministic sorted order
releases locks in reverse acquisition order
has no high-level daemon imports
```

`BridgeDaemon` owns:

```text
self.target_locks = TargetLockManager()
target_locks_for(targets)
target_state_lock(targets)
```

`target_state_lock(targets)` enforces the Stage 12 lock-order rule: target
lock(s) first, then `state_lock`. It raises if called while the current thread
already owns `state_lock`.

## Lock Order

New Stage 12 normal-path invariant:

```text
target lock(s), sorted by alias -> state_lock
```

Never acquire target locks while already holding `state_lock`.

Tmux I/O still happens under `state_lock` in Stage 12. Different-target tmux
I/O independence remains deferred to Stage 13.

## Delivery Paths

Normal `try_deliver(target)` now:

```text
reload_participants()
normalize target list
acquire target lock(s)
enter state_lock
run the existing candidate/reserve/deliver loop
```

`try_deliver(None)` snapshots active participants after reload, sorts aliases,
acquires all target locks in that sorted order, then enters `state_lock`.

`try_deliver_command_aware()` preserves the old monkeypatch-compatible normal
call surface by calling `d.try_deliver(target)` without the new keyword. Only
the explicit legacy state-locked path passes through the legacy facade.

## Prompt And Response Paths

`handle_prompt_submitted()` acquires the target lock for the resolved agent
before its existing state mutation block. Nonce checking, stale/interrupted
classification, prompt intercept handling, and delivered marking order are
otherwise unchanged.

`handle_response_finished()` acquires the target lock for the sender before
its existing state mutation block. Stale, held-drain, interrupted tombstone,
turn-id mismatch, terminal cleanup, and auto-return classification order are
otherwise unchanged.

Direct post-lock delivery kicks run after both `state_lock` and the event
target lock are released.

## State-Lock-Held Delivery Trigger Inventory

The revised Stage 12 plan required inventorying all current paths that can
trigger delivery while `state_lock` is owned.

Handled by moving direct kicks post-lock:

```text
handle_response_finished held Stop path:
  collects held_stop_sender / held_stop_global requests under lock
  drains them after target lock and state_lock are released

handle_external_message_queued:
  finalizes ingress under state_lock
  drains target delivery after state_lock is released

requeue_stale_inflight:
  _requeue_stale_inflight_locked returns stale targets
  wrapper drains target delivery after state_lock is released
```

Handled by temporary legacy state-locked delivery:

```text
watchdog fire synthetic queue_message(deliver=True)
aggregate completion queue_message(deliver=True)
auto-return / requester-cleared queue_message(deliver=True)
undeliverable result queue_message(deliver=True)
interrupt/source notices queued during active cancellation
any other queue_message(deliver=True) reached while state_lock is owned
```

The temporary legacy path is named:

```text
drain_delivery_request_legacy_state_locked()
try_deliver_command_aware_legacy_state_locked()
try_deliver_legacy_state_locked()
DeliveryScheduler.drain_legacy_state_locked()
```

It is selected only when `BridgeDaemon._state_lock_owned_by_current_thread()`
is true. The daemon legacy facades enforce that `state_lock` is owned before
they delegate to the legacy drain. The path deliberately avoids target-lock
acquisition, preserving the pre-Stage-12 globally serialized behavior for
state-lock-held queue drains.

For command-aware queue-message drains, the legacy path still calls
`command_delivery_allowed()` through `try_deliver_command_aware(...,
use_target_locks=False)`, preserving delivery-budget checks and deferral
logging.

Post-lock direct trigger sites such as clear completion, interrupt completion,
normal response-finished tail drains, maintenance delivery tick, and
queue-message drains outside `state_lock` use the normal target-locking
delivery path.

## Validation

Syntax/import check:

```sh
python3 -m py_compile \
  libexec/agent-bridge/bridge_daemon.py \
  libexec/agent-bridge/bridge_daemon_state.py \
  libexec/agent-bridge/bridge_daemon_delivery.py \
  libexec/agent-bridge/bridge_daemon_events.py \
  libexec/agent-bridge/bridge_daemon_maintenance.py \
  libexec/agent-bridge/bridge_daemon_watchdogs.py
```

Result: passed.

Explicit Stage 12 lock checks:

```text
TargetLockManager sorts and deduplicates ['b', 'a', 'b'] as ['a', 'b']
same-target lock acquisition serializes competing threads
try_deliver(target) acquires target lock before state_lock work
try_deliver(None) acquires sorted active target locks before state_lock work
queue_message(deliver=True) inside state_lock uses legacy drain, not target locks
state-locked legacy queue_message still runs command-aware delivery checks
queue_message(deliver=True) outside state_lock uses normal target-locking path
prompt_submitted holds target lock during state mutation
response_finished releases target/state locks before direct post-lock drains
external message_queued fallback drains delivery after state_lock release
```

Result: passed.

Focused Stage 12 suites:

```sh
python3 scripts/run_regressions.py --match delivery --fail-fast
python3 scripts/run_regressions.py --match prompt --fail-fast
python3 scripts/run_regressions.py --match response --fail-fast
python3 scripts/run_regressions.py --match turn_id_mismatch --fail-fast
python3 scripts/run_regressions.py --match watchdog_fire --fail-fast
python3 scripts/run_regressions.py --match requeue --fail-fast
python3 scripts/run_regressions.py --match ingressing --fail-fast
python3 scripts/run_regressions.py --match fallback --fail-fast
python3 scripts/run_regressions.py --match lifecycle --fail-fast
python3 scripts/run_regressions.py --match command_state_lock --fail-fast
python3 scripts/run_regressions.py --match aggregate --fail-fast
python3 scripts/run_regressions.py --match interrupt --fail-fast
python3 scripts/run_regressions.py --match clear --fail-fast
python3 scripts/run_regressions.py --match retry_enter --fail-fast
```

Results:

```text
delivery: 19 passed, 0 failed
prompt: 23 passed, 0 failed
response: 30 passed, 0 failed
turn_id_mismatch: 15 passed, 0 failed
watchdog_fire: 4 passed, 0 failed
requeue: 4 passed, 0 failed
ingressing: 7 passed, 0 failed
fallback: 23 passed, 0 failed
lifecycle: 1 passed, 0 failed
command_state_lock: 1 passed, 0 failed
aggregate: 46 passed, 0 failed
interrupt: 49 passed, 0 failed
clear: 52 passed, 0 failed
retry_enter: 2 passed, 0 failed
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
