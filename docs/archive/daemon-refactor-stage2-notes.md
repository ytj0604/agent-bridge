# Daemon Refactor Stage 2 Notes

Date: 2026-04-30

Stage 2 scope: make file-backed queue and aggregate stores explicit while
preserving file formats, file-lock behavior, daemon state, delivery behavior,
and compatibility imports from `bridge_daemon.py`.

## Reviewer Roles

- `worker`: owns the Stage 2 diff, validation, reviewer coordination, and
  eventual commit.
- `reviewer1`: primary reviewer and structure reviewer.
- `reviewer2`: concurrency reviewer and test reviewer.
- `manager`: not assigned a reviewer role.

## Files Changed

- `libexec/agent-bridge/bridge_daemon.py`
- `libexec/agent-bridge/bridge_daemon_store.py`
- `docs/daemon-refactor-stage2-notes.md`

## Store Changes

Moved unchanged to `bridge_daemon_store.py`:

```text
QueueStore
```

Added to `bridge_daemon_store.py`:

```text
AggregateStore
```

`AggregateStore` is a thin file-store abstraction over the same
`locked_json()` and `read_json()` primitives used before this stage. It owns no
routing behavior.

`AggregateStore.read()` always returns an envelope-shaped dict with at least:

```text
{"version": 1, "aggregates": {}}
```

`AggregateStore.read_aggregates()` returns `{}` unless the nested `aggregates`
value is a dict. `AggregateStore.get()` returns one aggregate dict snapshot or
`{}`.

## Compatibility Surface

`bridge_daemon.py` imports and re-exports `QueueStore` and `AggregateStore`.
Compatibility checks:

```text
bridge_daemon.QueueStore is bridge_daemon_store.QueueStore => True
bridge_daemon.AggregateStore is importable => True
```

`BridgeDaemon.__init__` still sets:

```text
self.aggregate_file = Path(args.queue_file).parent / "aggregates.json"
```

This keeps existing tests and external callers that inspect `d.aggregate_file`
working. Runtime daemon aggregate file reads/writes now use:

```text
self.aggregates = AggregateStore(self.aggregate_file)
```

## Aggregate Access Inventory

Plan-review baseline in `bridge_daemon.py`:

```text
direct read_json(self.aggregate_file, ...) sites: 7
direct locked_json(self.aggregate_file, ...) mutation sites: 2
```

Replacements:

```text
clear_guard:
  read_json(...).get("aggregates") -> self.aggregates.read_aggregates()

handle_clear_peers guard snapshot:
  read_json(...).get("aggregates") -> self.aggregates.read_aggregates()

build_wait_status:
  read_json(...).get("aggregates") -> self.aggregates.read_aggregates()

build_aggregate_status:
  read_json(... aggregate_id lookup) -> self.aggregates.get(aggregate_id)

_aggregate_watchdog_progress_text:
  read_json(... aggregate_id lookup) -> self.aggregates.get(aggregate_id)

watchdog_fire_skip_reason aggregate-complete check:
  read_json(... aggregate_id lookup) -> self.aggregates.get(aggregate_id)

collect_aggregate_response post-update count path:
  read_json(... aggregate_id lookup) -> self.aggregates.get(aggregate_id)

apply_force_clear_invalidation aggregate cancellation:
  locked_json(self.aggregate_file, ...) -> self.aggregates.update(aggregate_mutator)

collect_aggregate_response reply collection/completion:
  locked_json(self.aggregate_file, ...) -> self.aggregates.update(mutator)
```

Post-change inventory in `bridge_daemon.py`:

```text
direct runtime read_json(self.aggregate_file, ...) sites: 0
direct runtime locked_json(self.aggregate_file, ...) sites: 0
remaining self.aggregate_file uses: compatibility Path assignment and
  AggregateStore construction only
```

Tests still directly read or mutate `d.aggregate_file`; that is intentional
test-facing compatibility and was not changed in Stage 2.

## Lock-Order Notes

- `QueueStore.update()` remains a queue-list-only mutator wrapper over
  `locked_json(self.path, [])`.
- No `AggregateStore` call was added inside a `QueueStore.update()` mutator.
- Aggregate update mutators remain side-effect-free: they mutate only the
  aggregate JSON data passed to them.
- Watchdog cancellation, queue/result effects, tombstone recording, and logging
  still happen after the aggregate file lock is released.
- `wait_status` and `aggregate_status` still read aggregate JSON after
  releasing `state_lock`, preserving their existing best-effort snapshot lock
  ordering.
- File format is unchanged: aggregate JSON remains an envelope containing
  `version` and `aggregates`.

## Validation

Syntax/import check:

```sh
python3 -m py_compile \
  libexec/agent-bridge/bridge_daemon.py \
  libexec/agent-bridge/bridge_daemon_store.py
```

Result: passed.

Focused Stage 2 suites:

```sh
python3 scripts/run_regressions.py --match aggregate_status --fail-fast
python3 scripts/run_regressions.py --match aggregate --fail-fast
python3 scripts/run_regressions.py --match watchdog --fail-fast
python3 scripts/run_regressions.py --match clear --fail-fast
```

Results:

```text
aggregate_status: 12 passed, 0 failed
aggregate: 46 passed, 0 failed
watchdog: 59 passed, 0 failed
clear: 52 passed, 0 failed
```

Full suite, run because store compatibility and aggregate access changed:

```sh
python3 scripts/run_regressions.py
```

Result:

```text
615 passed, 0 failed
```

The full run emitted the known live-session-dependent skip line for
`list_peers_json_daemon_status_strips_pid`; the final summary remained green.
