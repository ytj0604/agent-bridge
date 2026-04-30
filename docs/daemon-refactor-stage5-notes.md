# Daemon Refactor Stage 5 Notes

Date: 2026-04-30

Stage 5 scope: extract aggregate collection/completion helpers while
preserving aggregate file mutation, synthetic reply injection, watchdog
cancellation, result queueing, and daemon facade compatibility.

## Reviewer Roles

- `worker`: owns the Stage 5 diff, validation, reviewer coordination, and
  eventual commit.
- `reviewer1`: primary reviewer and structure reviewer.
- `reviewer2`: concurrency reviewer and test reviewer for aggregate completion
  ordering, watchdog/result interactions, and synthetic reply coverage.
- `manager`: not assigned a reviewer role.

## Files Changed

- `libexec/agent-bridge/bridge_daemon.py`
- `libexec/agent-bridge/bridge_daemon_aggregates.py`
- `docs/daemon-refactor-stage5-notes.md`

## Aggregate Module

Added `bridge_daemon_aggregates.py` as a function-based aggregate service that
takes the daemon instance as `d`.

Moved implementations:

```text
aggregate_expected_from_context
aggregate_message_ids_from_context
merge_ordered_aliases
aggregate_result_body
_record_aggregate_completion_tombstones
_record_aggregate_interrupted_reply
collect_aggregate_response
```

`BridgeDaemon` keeps thin wrappers for the same public/private method names.
Existing call sites continue to call `self.*`, including clear, interrupt,
cancel, status, and response-return paths.

`bridge_daemon_status.py` still uses `d.merge_ordered_aliases` through the
compatibility wrapper. Stage 5 does not couple status builders directly to the
aggregate module.

## Side-Effect Boundary

The `AggregateStore.update` mutator inside `collect_aggregate_response` only
mutates aggregate JSON and returns completion/cancelled state. It does not log,
call daemon methods, mutate queues, cancel watchdogs, touch tmux, or acquire
unrelated locks.

Side effects remain after the aggregate file mutation returns:

```text
aggregate_reply_collected log
aggregate result message construction
oversized result redirect handling
with d.state_lock:
  _record_aggregate_completion_tombstones
  cancel_watchdogs_for_aggregate
  suppress_pending_watchdog_wakes
  queue_message
  aggregate_result_queued log
```

Requester-cleared aggregates still return after
`aggregate_reply_ignored_requester_cleared` logging and do not queue a result.

## Synthetic Replies

`_record_aggregate_interrupted_reply` moved with aggregate-specific synthetic
reply injection. It preserves:

- aggregate context fields from the cancelled message
- reason-specific synthetic text
- `deliver` propagation into `collect_aggregate_response`
- `aggregate_interrupt_inject_failed` logging on unexpected failure

The moved function intentionally calls `d.collect_aggregate_response(...)`
through the daemon wrapper, preserving the existing facade call surface.

## Explicit Non-Changes

- `AggregateStore` behavior did not change.
- `bridge_daemon_status.py` aggregate status logic did not move.
- Watchdog service functions did not move.
- Queue/store, command, clear, interrupt, delivery, tmux, and lock-splitting
  semantics did not change beyond wrapper delegation.
- Aggregate result body text and synthetic reply text are unchanged.

## Validation

Syntax/import check:

```sh
python3 -m py_compile \
  libexec/agent-bridge/bridge_daemon.py \
  libexec/agent-bridge/bridge_daemon_aggregates.py \
  libexec/agent-bridge/bridge_daemon_status.py
```

Result: passed.

Explicit compatibility checks:

```text
BridgeDaemon aggregate helper wrappers match bridge_daemon_aggregates outputs
aggregate_expected_from_context parses and de-duplicates aliases
aggregate_message_ids_from_context filters blank keys
merge_ordered_aliases preserves first-seen order
aggregate_result_body matches moved implementation
collect_aggregate_response completes an aggregate and queues exactly one result
```

Result: passed.

Focused Stage 5 suites:

```sh
python3 scripts/run_regressions.py --match aggregate --fail-fast
python3 scripts/run_regressions.py --match aggregate_status --fail-fast
python3 scripts/run_regressions.py --match aggregate_completion --fail-fast
python3 scripts/run_regressions.py --match prompt_intercept_aggregate --fail-fast
python3 scripts/run_regressions.py --match watchdog --fail-fast
python3 scripts/run_regressions.py --match clear --fail-fast
```

Results:

```text
aggregate: 46 passed, 0 failed
aggregate_status: 12 passed, 0 failed
aggregate_completion: 1 passed, 0 failed
prompt_intercept_aggregate: 1 passed, 0 failed
watchdog: 59 passed, 0 failed
clear: 52 passed, 0 failed
```

Full suite, run because aggregate completion and private facade helpers moved:

```sh
python3 scripts/run_regressions.py
```

Result:

```text
615 passed, 0 failed
```

The full run emitted the known live-session-dependent skip line for
`list_peers_json_daemon_status_strips_pid`; the final summary remained green.
