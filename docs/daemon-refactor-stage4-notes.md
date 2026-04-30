# Daemon Refactor Stage 4 Notes

Date: 2026-04-30

Stage 4 scope: extract command socket/request parsing and read-only status
builders while preserving `BridgeDaemon` wrapper methods, command timeout
behavior, and status snapshot lock ordering.

## Reviewer Roles

- `worker`: owns the Stage 4 diff, validation, reviewer coordination, and
  eventual commit.
- `reviewer1`: primary reviewer and structure reviewer.
- `reviewer2`: test reviewer with concurrency review for command timeout and
  status snapshot lock boundaries.
- `manager`: not assigned a reviewer role.

## Files Changed

- `libexec/agent-bridge/bridge_daemon.py`
- `libexec/agent-bridge/bridge_daemon_commands.py`
- `libexec/agent-bridge/bridge_daemon_status.py`
- `docs/daemon-refactor-stage4-notes.md`

## Command Module

Added `bridge_daemon_commands.py` for command socket lifecycle and request
parsing:

```text
start_command_server
stop_command_server
command_server_loop
handle_command_worker
handle_command_connection
peer_uid
```

`BridgeDaemon` keeps thin wrappers for the same method names, so direct tests
and external callers still use the daemon facade. Concrete command handlers
remain on `BridgeDaemon`; the command module dispatches to those existing
handlers through the daemon instance.

`handle_command_worker` preserves the prior lock-wait behavior:

- It catches only lock-wait exceptions, using class-name matching to avoid a
  circular import back into `bridge_daemon.py`.
- It returns `d.lock_wait_exceeded_response(exc.command_class)` for those
  exceptions.
- It re-raises unrelated exceptions.
- It always clears `d.command_context.info = {}` in `finally`.
- It sends one JSON-line response when a response exists.

Command timeout budget/deadline helpers and `command_state_lock()` were not
moved.

## Status Module

Added `bridge_daemon_status.py` for read-only wait/aggregate status builders
and their private formatting helpers:

```text
_wait_status_section
_wait_status_deadline_iso
_wait_status_message_watchdog_index
_wait_status_counts
_build_outstanding_requests
_build_wait_status_watchdogs
_build_wait_status_alarms
_build_wait_status_pending_inbound
_build_wait_status_aggregates
build_wait_status
_aggregate_status_not_found
_aggregate_status_legacy_min_ts
_aggregate_status_section
_aggregate_status_alias_list
_aggregate_status_tombstone_for_message
_aggregate_terminal_status_from_reason
_aggregate_status_response_watchdog
_aggregate_status_reply_leg
_aggregate_status_build_legs
build_aggregate_status
```

`BridgeDaemon` keeps thin wrappers for the public and private status method
names used by tests. Moved status helpers call other functions in
`bridge_daemon_status.py` directly, not the `BridgeDaemon` wrappers, avoiding
wrapper recursion.

`WAIT_STATUS_SECTION_LIMIT` and `AGGREGATE_STATUS_LEG_LIMIT` moved to
`bridge_daemon_status.py` and remain import-compatible from `bridge_daemon.py`.

## Lock Boundaries

Status snapshot ordering is preserved:

- `build_wait_status` snapshots queue and watchdog state under
  `command_state_lock(command_class="wait_status")`, releases the daemon lock,
  then reads aggregate JSON through `d.aggregates.read_aggregates()`.
- `build_aggregate_status` snapshots queue, watchdogs, and interrupt
  tombstones under `command_state_lock(command_class="aggregate_status")`,
  releases the daemon lock, then reads one aggregate through
  `d.aggregates.get(aggregate_id)`.

The comments documenting best-effort snapshot semantics were kept with the
moved builders. No helper combines daemon state snapshots and aggregate JSON
reads under one daemon lock.

## Compatibility Checks

Explicit checks confirmed:

```text
bridge_daemon.WAIT_STATUS_SECTION_LIMIT == bridge_daemon_status.WAIT_STATUS_SECTION_LIMIT
bridge_daemon.AGGREGATE_STATUS_LEG_LIMIT == bridge_daemon_status.AGGREGATE_STATUS_LEG_LIMIT
bridge_daemon.daemon_commands is bridge_daemon_commands
bridge_daemon.daemon_status is bridge_daemon_status
d._wait_status_section(..., limit=1) returns one item
d._aggregate_status_alias_list(["codex", "", None]) == ["codex"]
d.handle_command_connection(...) preserves malformed JSON, non-dict request,
unsupported command, and wait_status success responses
```

Full-suite regressions cover the command-dispatch compatibility surfaces called
out in plan review, including malformed JSON, unsupported commands, peer UID
rejection, and direct `d.handle_command_connection()` tests.

## Explicit Non-Changes

- Command handlers were not moved.
- `command_state_lock`, command budget/deadline helpers, and command timeout
  constants were not moved.
- Status pane probing behavior for the `status` command was not moved.
- No delivery, clear, interrupt, watchdog, aggregate, queue/store, tmux, or
  file-lock ordering behavior changed.

## Validation

Syntax/import check:

```sh
python3 -m py_compile \
  libexec/agent-bridge/bridge_daemon.py \
  libexec/agent-bridge/bridge_daemon_commands.py \
  libexec/agent-bridge/bridge_daemon_status.py
```

Result: passed.

Focused Stage 4 suites:

```sh
python3 scripts/run_regressions.py --match wait_status --fail-fast
python3 scripts/run_regressions.py --match aggregate_status --fail-fast
python3 scripts/run_regressions.py --match command_state_lock --fail-fast
python3 scripts/run_regressions.py --match alarm_request_retries --fail-fast
```

Results:

```text
wait_status: 8 passed, 0 failed
aggregate_status: 12 passed, 0 failed
command_state_lock: 1 passed, 0 failed
alarm_request_retries: 2 passed, 0 failed
```

Full suite, run because command/status wrappers and import-compatible constants
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
