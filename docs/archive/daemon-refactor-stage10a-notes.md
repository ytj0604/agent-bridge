# Daemon Refactor Stage 10a Notes

Date: 2026-04-30

Stage 10a scope: introduce a delivery scheduler/request API without changing
delivery behavior, producer call sites, physical lock scope, or inline delivery
timing.

## Reviewer Roles

- `worker`: owns the Stage 10a diff, validation, reviewer coordination, and
  eventual commit.
- `reviewer2`: primary reviewer, concurrency reviewer, and test reviewer.
- `reviewer1`: structure reviewer for module boundaries, imports, and facade
  compatibility.
- `manager`: not assigned a reviewer role.

## Files Changed

- `libexec/agent-bridge/bridge_daemon.py`
- `libexec/agent-bridge/bridge_daemon_maintenance.py`
- `docs/daemon-refactor-stage10a-notes.md`

## Scheduler API

Added `bridge_daemon_maintenance.py` with passive delivery scheduling types:

```text
DeliveryRequest
DeliveryScheduler
```

`DeliveryRequest` is a one-shot dataclass recording:

```text
target
message_id
command_aware
reason
created_ts
drained
```

`DeliveryScheduler.request_delivery(...)` creates a request object only. It
does not drain, log, acquire locks, run tmux, touch queue or aggregate files,
or retain requests in an unbounded in-memory queue.

`DeliveryScheduler.drain_inline(d, request)` is a transparent synchronous
facade:

```text
command_aware=True  -> d.try_deliver_command_aware(target, message_id=...)
command_aware=False -> d.try_deliver(target)
```

It does not catch exceptions and marks the request drained only after the
daemon delivery call returns.

`bridge_daemon_maintenance.py` does not import `bridge_daemon.py`.

## Daemon Facade

`BridgeDaemon.__init__` now initializes:

```text
self.delivery_scheduler = DeliveryScheduler()
```

This happens after the existing `LockFacade` setup and does not change
`state_lock`, state dataclass initialization, command context, or startup
recovery ordering.

`BridgeDaemon` re-exports/imports the scheduler types and adds thin wrappers:

```text
request_delivery(...)
drain_delivery_request(...)
request_and_drain_delivery(...)
```

The wrappers delegate directly to `self.delivery_scheduler` and preserve the
daemon method monkeypatch/override surfaces for delivery execution.

## Explicit Non-Conversions

No producer path was converted in Stage 10a. Existing runtime paths still call
the current delivery methods directly:

```text
d.try_deliver(...)
d.try_deliver_command_aware(...)
d.queue_message(..., deliver=True/default)
```

`queue_message(..., deliver=True)` still drains through
`d.try_deliver_command_aware(...)` in `bridge_daemon_delivery.py`. Event,
watchdog, aggregate, interrupt, clear, command, and maintenance loop delivery
triggers remain on their existing direct call sites. The only new calls to
`d.try_deliver(...)` / `d.try_deliver_command_aware(...)` are inside the new
transparent scheduler drain facade.

## Lock And Ordering Invariants

- The daemon still has one physical `state_lock`.
- Stage 10a adds no new locks, background threads, timers, sleeps, or polling
  loops.
- Inline delivery timing and call order remain unchanged because no producer
  path has been converted to the scheduler yet.
- Scheduler drains call back into existing daemon delivery wrappers, so
  command deadline checks, lock-wait handling, tmux positions, queue mutation,
  watchdog arming, and pane-mode behavior remain owned by the existing
  delivery implementation.
- Exceptions from `d.try_deliver(...)` and `d.try_deliver_command_aware(...)`
  propagate unchanged through scheduler drains.

## Validation

Syntax/import check:

```sh
python3 -m py_compile \
  libexec/agent-bridge/bridge_daemon.py \
  libexec/agent-bridge/bridge_daemon_maintenance.py \
  libexec/agent-bridge/bridge_daemon_delivery.py
```

Result: passed.

Explicit scheduler checks:

```text
bridge_daemon_maintenance does not import bridge_daemon.py
BridgeDaemon has delivery_scheduler and thin wrapper methods
DeliveryRequest and DeliveryScheduler are re-export-compatible from bridge_daemon.py
request_delivery records target/message_id/command_aware/reason without draining
request_and_drain_delivery(command_aware=True) calls d.try_deliver_command_aware(target, message_id=...)
request_and_drain_delivery(command_aware=False) calls d.try_deliver(target)
threading.Condition(d.state_lock) accepts the raw daemon state lock
unrelated exceptions from d.try_deliver propagate unchanged through drain_inline
unrelated exceptions from d.try_deliver_command_aware propagate unchanged through drain_inline
```

Result: passed.

Producer call-site check:

```sh
rg -n "try_deliver_command_aware|try_deliver\\(|queue_message\\(" \
  libexec/agent-bridge/bridge_daemon.py \
  libexec/agent-bridge/bridge_daemon_*.py
```

Result: existing producer call sites still call the direct delivery APIs;
only the new scheduler drain facade was added.

Focused Stage 10a suites:

```sh
python3 scripts/run_regressions.py --match delivery --fail-fast
python3 scripts/run_regressions.py --match command_state_lock --fail-fast
```

Results:

```text
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
