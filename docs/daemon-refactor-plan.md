# Daemon Split And Lock Refactor Plan

Status: draft plan. This document is the working plan for splitting
`libexec/agent-bridge/bridge_daemon.py` and later reducing lock contention
without weakening routing correctness.

The plan intentionally separates code-structure work from lock-behavior work.
The first stages must preserve behavior and keep the existing single
`state_lock`. Lock splitting starts only after module boundaries and state
ownership are explicit enough to review.

## Goals

- Make daemon responsibilities easier to review and change.
- Reduce command-socket latency caused by long `state_lock` holds.
- Preserve the current routing safety model: no duplicate prompt injection,
  no stale response cleanup of a fresh turn, no interrupt/clear interleaving,
  and no delivery of unfinalized ingress rows.
- Keep each stage small enough to be one focused commit.

## References

- `docs/milestones.md`: high-level daemon split and lock-contention goals.
- `docs/known-issues.md`: current live-state and ingressing recovery limits.
- `docs/review-process.md`: multi-agent review protocol.
- `docs/archive/regression-split-plan.md`: model for staged, reviewable
  structural changes.

## Agent Roles

- `worker`: the single writer for the current stage. Owns the diff, validation
  commands, reviewer coordination, and final commit message. The worker must
  not proceed to the next stage until the current stage has completed review.
- `primary reviewer`: coordinates review for the stage. Receives the worker's
  review notice, asks secondary reviewers for input, then sends one
  consolidated result back to the worker. The primary reviewer should not edit.
- `structure reviewer`: read-only reviewer for module boundaries, imports,
  helper placement, facade shape, public symbol compatibility, and commit size.
- `concurrency reviewer`: read-only reviewer for lock ordering, target-level
  routing invariants, queue/aggregate file-lock order, tmux interleaving,
  watchdog/alarm races, and deadlock risk.
- `test reviewer`: read-only reviewer for regression coverage, isolation risks,
  monkeypatch fallout, CLI compatibility, and missing scenario checks.
- `domain worker`: optional writer for a later disjoint implementation slice.
  A domain worker may edit only the file set explicitly assigned by the worker
  and must not revert or rewrite another agent's changes.

For this repository's live bridge, `reviewer1` is a good default primary
reviewer for structure-heavy stages, and `reviewer2` is a good default primary
reviewer for lock/concurrency stages. Future rooms may assign aliases
differently; the role matters more than the alias.

## Stage Review Matrix

- Stage 0: primary reviewer plus all roles. This is the plan/baseline gate.
- Stages 1-4: `structure reviewer` primary; `test reviewer` required when
  compatibility wrappers, command socket behaviour, or public symbols change.
- Stages 5-9b: `concurrency reviewer` and `test reviewer` both required;
  `structure reviewer` remains responsible for module boundaries.
- Stages 10a-15b: `concurrency reviewer` primary; `test reviewer` required for
  every stage because these stages change scheduling or lock behaviour.
- Stage 16: `structure reviewer` primary, with `concurrency reviewer` checking
  the final lock-order documentation.

## Review Protocol

Use `docs/review-process.md` for every implementation stage:

1. Worker prepares the stage diff.
2. Worker sends one notice to all reviewers naming the primary reviewer.
3. Reviewers inspect independently; the primary reviewer requests input from
   secondary reviewers.
4. The primary reviewer sends a single consolidated notice to the worker.
5. Worker fixes findings, then requests final review if behavior, locking, or
   state management changed.

Do not ask reviewers to edit unless the worker explicitly assigns a disjoint
write scope. Review prompts must include:

- stage number and review stage (`plan review`, `code review`, or
  `final review`);
- primary reviewer alias;
- exact file set or diff scope;
- expected output format;
- "Reviewers must inspect first; the primary collects secondary input via
  request, then delivers only the consolidated result to the worker as a
  notice."

## Global Invariants

These invariants apply to every stage.

- Preserve the current lock ordering until a stage explicitly changes it:
  daemon state lock before `QueueStore.update()` before queue file lock.
- Queue mutator callbacks must not call back into daemon methods, log, run
  tmux, or acquire unrelated daemon locks.
- Do not call aggregate JSON mutators while holding queue file locks. If a
  stage changes aggregate/queue ordering, document the new order and add a
  deadlock-oriented test.
- Prefer no nested aggregate JSON/file lock. Operations that need aggregate
  mutation and routing effects should mutate or snapshot the aggregate store,
  release it, then perform target/watchdog/queue effects in a separate phase.
- The intended later physical-lock order is: room/global snapshot lock, target
  lock(s) in sorted alias order, watchdog/alarm lock, then queue store/file
  lock. Aggregate store/file mutation should not be nested inside that chain
  unless a stage documents and tests a stricter order.
- A target may have at most one active inbound turn. Any queue row with
  `status in {"inflight", "submitted", "delivered"}` blocks fresh delivery to
  that target.
- `pending -> inflight` must remain coupled to nonce assignment, delivery
  attempt increment, and delivery watchdog arming.
- `prompt_submitted` must cross-check daemon-side inflight state; hook nonce
  alone is never authoritative.
- Stale `response_finished` classification must happen before clearing
  `busy`, `reserved`, or `current_prompt_by_agent`.
- Interrupt key delivery for one target must not interleave with replacement
  delivery to that target. Claude `Escape` -> `C-c` ordering remains protected.
- Clear reservations are delivery gates. No normal delivery may enter a target
  while that target has an active clear reservation.
- `status="ingressing"` rows must not deliver before alarm cancel/body-prepend
  finalization or explicit recovery/promotion.
- Aggregate completion must enqueue at most one aggregate result.

## Target Module Shape

Exact names may change during implementation, but keep this ownership model:

```text
libexec/agent-bridge/
  bridge_daemon.py                  # main facade, CLI, orchestration
  bridge_daemon_messages.py         # prompt/body/message helpers
  bridge_daemon_tmux.py             # tmux send/probe/enter adapters
  bridge_daemon_store.py            # QueueStore, AggregateStore
  bridge_daemon_state.py            # state dataclasses and lock facade
  bridge_daemon_commands.py         # command socket server/dispatch
  bridge_daemon_status.py           # wait_status and aggregate_status builders
  bridge_daemon_watchdogs.py        # watchdog/alarm service
  bridge_daemon_delivery.py         # reserve/deliver/pane-mode delivery flow
  bridge_daemon_events.py           # prompt/response/message_queued event flow
  bridge_daemon_aggregates.py       # aggregate collection/completion
  bridge_daemon_interrupts.py       # interrupt flow
  bridge_daemon_clear_flow.py       # controlled clear flow
  bridge_daemon_maintenance.py      # housekeeping scheduler/timers
```

During early stages, preserve existing imports used by regressions. If tests or
external tools import `bridge_daemon.make_message`, `bridge_daemon.QueueStore`,
or tmux helpers, keep compatibility wrappers or re-exports until a deliberate
cleanup stage.

## Stage 0: Baseline And Inventory

Goal: record current behavior before changes.

Scope:

- No production code movement.
- Capture function inventory and regression baseline in working notes.
- Confirm whether there are unrelated dirty worktree changes.

Validation:

```sh
python3 scripts/run_regressions.py --list | wc -l
python3 scripts/run_regressions.py --match daemon --fail-fast
python3 scripts/run_regressions.py
```

Checklist:

- Observed scenario count is recorded in stage notes.
- Full suite passes before structural work starts.
- Known slow or flaky tests are documented, not silently ignored.
- Reviewers agree on this plan before Stage 1 starts.

Review focus:

- `structure reviewer`: stage boundaries and module layout.
- `concurrency reviewer`: invariants and lock-split sequencing.
- `test reviewer`: validation commands and missing coverage.

## Stage 1: Extract Pure Message And Tmux Helpers

Goal: remove low-risk pure/helper code from the daemon without state changes.

Scope:

- Move prompt/body/message helpers to `bridge_daemon_messages.py`.
- Move tmux helper functions to `bridge_daemon_tmux.py`.
- Keep compatibility imports/re-exports in `bridge_daemon.py`.
- Do not move delivery state transitions.
- Do not change lock scope.

Validation:

```sh
python3 scripts/run_regressions.py --match prompt --fail-fast
python3 scripts/run_regressions.py --match redirect --fail-fast
python3 scripts/run_regressions.py --match pane_mode --fail-fast
python3 scripts/run_regressions.py --match interrupt --fail-fast
```

Checklist:

- `bridge_daemon.build_peer_prompt`, `make_message`, and tmux helper monkeypatch
  tests still work.
- No daemon state field moved.
- No tmux call changed argument order or timeout.
- Full suite passes if any helper re-export changed.

## Stage 2: Introduce Store Abstractions

Goal: make file-backed stores explicit while preserving file-lock behavior.

Scope:

- Move `QueueStore` to `bridge_daemon_store.py`.
- Add `AggregateStore` for `aggregates.json` read/update operations.
- Replace direct aggregate mutations with store methods.
- Inventory remaining direct `aggregate_file` reads/writes after the move.
  Read-only best-effort snapshots may remain direct only when the stage notes
  explain why they should not use `AggregateStore`.
- Preserve current queue and aggregate file-lock order.

Validation:

```sh
python3 scripts/run_regressions.py --match aggregate_status --fail-fast
python3 scripts/run_regressions.py --match aggregate --fail-fast
python3 scripts/run_regressions.py --match watchdog --fail-fast
python3 scripts/run_regressions.py --match clear --fail-fast
```

Checklist:

- Queue mutators remain small and side-effect-free.
- Aggregate store mutators do not call daemon methods.
- No queue file lock is held while acquiring aggregate file lock unless that
  ordering already existed and is documented in the diff.
- No aggregate file lock is held while queue routing effects are performed.
- File formats are unchanged.

## Stage 3: Introduce State Facade With One Physical Lock

Goal: clarify ownership before splitting locks.

Scope:

- Add `bridge_daemon_state.py` with dataclasses such as `RoutingState`,
  `WatchdogState`, `ClearState`, and `ParticipantCache`.
- Add a lock facade that initially wraps the existing single `threading.RLock`.
- Move field initialization into state objects, but keep `BridgeDaemon` access
  compatible through properties or narrow forwarding attributes.
- Do not introduce per-domain physical locks in this stage.

Validation:

```sh
python3 scripts/run_regressions.py --match command_state_lock --fail-fast
python3 scripts/run_regressions.py --match wait_status --fail-fast
python3 scripts/run_regressions.py --match interrupt --fail-fast
python3 scripts/run_regressions.py --match clear --fail-fast
```

Checklist:

- `d.state_lock` remains available for tests and existing code.
- `threading.Condition(d.state_lock)` clear tests still pass.
- Command lock timeout behavior remains unchanged.
- No direct state access is hidden behind a method that also runs tmux or file
  I/O.

## Stage 4: Extract Command Server And Read-Only Status Builders

Goal: remove command socket and debug snapshot code from the daemon facade.

Scope:

- Move command server socket lifecycle and request parsing to
  `bridge_daemon_commands.py`.
- Keep command handlers either on the daemon facade or service objects.
- Move `build_wait_status` and `build_aggregate_status` helpers to
  `bridge_daemon_status.py`.
- Preserve command timeout and `lock_wait_exceeded` responses.

Validation:

```sh
python3 scripts/run_regressions.py --match wait_status --fail-fast
python3 scripts/run_regressions.py --match aggregate_status --fail-fast
python3 scripts/run_regressions.py --match command_state_lock --fail-fast
python3 scripts/run_regressions.py --match alarm_request_retries --fail-fast
```

Checklist:

- Unsupported commands and malformed JSON responses are unchanged.
- Socket enqueue, alarm, interrupt, clear, cancel, wait status, and aggregate
  status still route through the same validation.
- Best-effort snapshot semantics remain documented: queue/watchdog under daemon
  lock, aggregate JSON read outside the state snapshot when required to avoid
  lock-order risk.

## Stage 5: Extract Aggregate Service

Goal: isolate aggregate collection/completion without changing result routing.

Scope:

- Move aggregate metadata helpers and `collect_aggregate_response` logic to
  `bridge_daemon_aggregates.py`.
- Aggregate completion should call back into daemon routing only after aggregate
  file mutation is complete.
- Keep aggregate completion tombstones, watchdog cancellation, and result queue
  enqueue in the same logical order.

Validation:

```sh
python3 scripts/run_regressions.py --match aggregate --fail-fast
python3 scripts/run_regressions.py --match aggregate_status --fail-fast
python3 scripts/run_regressions.py --match aggregate_completion --fail-fast
python3 scripts/run_regressions.py --match prompt_intercept_aggregate --fail-fast
```

Checklist:

- Aggregate result is still queued exactly once.
- Synthetic interrupted/cancelled/endpoint-lost replies still progress
  aggregate state.
- Aggregate watchdogs survive partial aggregate progress and cancel only on
  aggregate completion or explicit requester clearing.

## Stage 6: Extract Watchdog And Alarm Service

Goal: isolate watchdog/alarm logic while retaining the single lock facade.

Scope:

- Move watchdog registration, extension, cancellation, alarm registration, alarm
  tombstones, fire text, and stale skip logic to `bridge_daemon_watchdogs.py`.
- `fire_watchdog` may still enqueue via daemon callback.
- Keep `check_watchdogs` under the same physical lock for now.

Validation:

```sh
python3 scripts/run_regressions.py --match watchdog --fail-fast
python3 scripts/run_regressions.py --match alarm --fail-fast
python3 scripts/run_regressions.py --match extend_wait --fail-fast
python3 scripts/run_regressions.py --match ingressing --fail-fast
```

Checklist:

- Alarm idempotency and stable wake-id retry behavior are unchanged.
- Incoming message alarm-cancel/body-prepend behavior is unchanged.
- Terminal response still suppresses pending watchdog wake notices.
- No watchdog service method acquires queue file lock while holding aggregate
  file lock.

## Stage 7: Extract Delivery Service Without Lock Scope Changes

Goal: make the delivery pipeline explicit before changing contention behavior.

Scope:

- Move reservation, pane-mode deferral, tmux delivery, retry-enter, and
  undeliverable finalization to `bridge_daemon_delivery.py`.
- Keep the current global lock scope, including tmux I/O inside the lock.
- Make all calls that can trigger delivery obvious in code review.

Validation:

```sh
python3 scripts/run_regressions.py --match pane_mode --fail-fast
python3 scripts/run_regressions.py --match delivery_watchdog --fail-fast
python3 scripts/run_regressions.py --match retry_enter --fail-fast
python3 scripts/run_regressions.py --match identity --fail-fast
```

Checklist:

- No second pending row can deliver while an inflight/submitted/delivered row
  exists for the target.
- Delivery failure still reverts to pending or terminal undeliverable state.
- `last_enter_ts`, nonce cache, reserved target, and delivery watchdog updates
  remain coupled.

## Stage 8: Extract Hook/Event Routing

Goal: isolate `prompt_submitted`, `response_finished`, and `message_queued`
event handling.

Scope:

- Move hook routing to `bridge_daemon_events.py`.
- Preserve stale response judgement order exactly.
- Preserve nonce cross-check semantics.
- Preserve file-fallback ingress finalization under the single lock facade.

Validation:

```sh
python3 scripts/run_regressions.py --match prompt --fail-fast
python3 scripts/run_regressions.py --match response --fail-fast
python3 scripts/run_regressions.py --match turn_id_mismatch --fail-fast
python3 scripts/run_regressions.py --match ingressing --fail-fast
```

Checklist:

- Orphan or mismatched nonce does not mark a row delivered.
- User prompt interception still cancels active delivered work safely.
- Late Stop events cannot clear a fresh active prompt.
- File-fallback `ingressing` rows are not delivered before finalization.

## Stage 9a: Extract Interrupt Flow

Goal: move interrupt handling after supporting services are split.

Scope:

- Move interrupt handling to `bridge_daemon_interrupts.py`.
- Keep current physical lock and current tmux call positions.
- Do not move controlled clear in this stage.

Validation:

```sh
python3 scripts/run_regressions.py --match interrupt --fail-fast
python3 scripts/run_regressions.py --match interrupt_endpoint_lost --fail-fast
python3 scripts/run_regressions.py --match aggregate_interrupt --fail-fast
python3 scripts/run_regressions.py
```

Checklist:

- ESC failure still performs no state mutation.
- C-c partial failure still blocks replacement delivery.
- No replacement delivery can occur between Claude `Escape` and `C-c`.
- Interrupted-turn tombstones still suppress identifiable late hooks.
- Aggregate-bearing interrupted messages still record synthetic replies.

## Stage 9b: Extract Clear Flow

Goal: move controlled clear handling after interrupt extraction is stable.

Scope:

- Move controlled clear handling to `bridge_daemon_clear_flow.py`.
- Keep current physical lock and current tmux call positions.
- Keep `Condition(state_lock)` behavior unless this stage explicitly updates
  tests and docs.

Validation:

```sh
python3 scripts/run_regressions.py --match clear --fail-fast
python3 scripts/run_regressions.py --match clear_identity --fail-fast
python3 scripts/run_regressions.py --match clear_force_leave --fail-fast
python3 scripts/run_regressions.py
```

Checklist:

- Clear guard/reservation still happens before any pane touch.
- Pane-touched clear failure still forces leave or holds reservation as before.
- Multi-clear reservations still gate all targets before sequential execution.

## Stage 10a: Introduce Delivery Scheduler API

Goal: add a scheduler/request abstraction without changing delivery behaviour.

Scope:

- Add `bridge_daemon_maintenance.py` or a small scheduler object that records
  delivery requests.
- Preserve current inline drain behaviour through the scheduler API.
- Keep one physical state lock.

Validation:

```sh
python3 scripts/run_regressions.py --match delivery --fail-fast
python3 scripts/run_regressions.py --match command_state_lock --fail-fast
python3 scripts/run_regressions.py
```

Checklist:

- The new scheduler is a facade; call order and inline delivery timing are
  unchanged.
- No producer path is converted in this stage.
- The scheduler records enough context for command deadline-aware delivery.

## Stage 10b: Convert Normal Queue Delivery Requests

Goal: stop ordinary queue append paths from hiding direct delivery calls.

Scope:

- Convert `queue_message(..., deliver=True)` to request scheduler delivery.
- Preserve prompt delivery timing with an immediate scheduler drain when safe.
- Do not convert watchdog, aggregate, interrupt, or clear trigger sites unless
  they already route through the converted queue-message path.

Validation:

```sh
python3 scripts/run_regressions.py --match pending_replacement --fail-fast
python3 scripts/run_regressions.py --match clear_success_delivery --fail-fast
python3 scripts/run_regressions.py --match response --fail-fast
python3 scripts/run_regressions.py
```

Checklist:

- Queue append no longer recurses directly into tmux delivery from state cleanup
  callbacks.
- Converted paths still drain exactly once.
- Command deadline-aware delivery deferral still logs the same events.

## Stage 10c: Convert Watchdog, Aggregate, Interrupt, And Clear Triggers

Goal: remove remaining hidden delivery trigger sites by domain.

Scope:

- Convert watchdog fire trigger sites.
- Convert aggregate completion trigger sites.
- Convert terminal response, interrupt, and clear completion trigger sites.
- Keep one physical state lock.

Validation:

```sh
python3 scripts/run_regressions.py --match watchdog_fire --fail-fast
python3 scripts/run_regressions.py --match aggregate_completion --fail-fast
python3 scripts/run_regressions.py --match interrupt --fail-fast
python3 scripts/run_regressions.py --match clear_success_delivery --fail-fast
python3 scripts/run_regressions.py
```

Checklist:

- Each converted domain drains exactly once.
- Pending work still delivers promptly after terminal response, interrupt,
  clear completion, watchdog fire, and aggregate completion.
- No converted path recurses into tmux delivery from a state cleanup callback.
- No new polling loop is required for normal prompt flow.

## Stage 11a: Add Maintenance Scheduler Harness

Goal: create the maintenance thread/timer boundary without moving tasks.

Scope:

- Add a dedicated maintenance scheduler/thread abstraction.
- Keep existing housekeeping execution in the event-tail loop.
- Keep thread startup/shutdown deterministic for `--once` and dry-run tests.
- Keep the single state lock for this stage.

Validation:

```sh
python3 scripts/run_regressions.py --match lifecycle --fail-fast
python3 scripts/run_regressions.py --match bridge_daemon_ctl --fail-fast
python3 scripts/run_regressions.py
```

Checklist:

- `--once` behavior remains deterministic.
- `--once` must not leave background work running.
- Daemon shutdown closes command socket and the scheduler harness cleanly.
- Shutdown while a command worker is active is tested or explicitly staged for
  the next maintenance commit.

## Stage 11b: Move Core Time-Based Maintenance

Goal: move queue/watchdog housekeeping into the scheduler before delivery tick.

Scope:

- Move stale inflight requeue, watchdog checks, turn-id mismatch expiry, and
  aged ingressing promotion into the maintenance scheduler.
- Keep retry-enter and periodic delivery tick in the event-tail loop for now.

Validation:

```sh
python3 scripts/run_regressions.py --match watchdog --fail-fast
python3 scripts/run_regressions.py --match turn_id_mismatch --fail-fast
python3 scripts/run_regressions.py --match aged_ingressing --fail-fast
python3 scripts/run_regressions.py --match requeue --fail-fast
python3 scripts/run_regressions.py
```

Checklist:

- No maintenance task can run concurrently with itself.
- Command socket operations no longer wait on event-tail sleep cadence for these
  time-based tasks.
- Shutdown while a command is active does not leave the scheduler alive.

## Stage 11c: Move Retry-Enter, Delivery Tick, And Capture Cleanup

Goal: finish maintenance migration after core time-based tasks are stable.

Scope:

- Move retry-enter, periodic delivery tick, and capture cleanup into the
  scheduler.
- Preserve command deadline-aware delivery checks.

Validation:

```sh
python3 scripts/run_regressions.py --match retry_enter --fail-fast
python3 scripts/run_regressions.py --match delivery --fail-fast
python3 scripts/run_regressions.py --match capture --fail-fast
python3 scripts/run_regressions.py
```

Checklist:

- Retry-enter still skips pane-mode and endpoint-lost cases correctly.
- Periodic delivery still prevents pending work from stalling.
- Capture cleanup remains best-effort and does not block command workers.
- Command socket operations no longer wait on event-tail sleep cadence.

## Stage 12: Introduce Per-Target Locks

Goal: add target-lock plumbing while preserving global serialization where
tmux I/O still requires it.

Scope:

- Add a target lock manager with deterministic multi-target acquisition order.
- Start with normal single-target delivery, prompt submission, and response
  finish paths.
- Keep a short global lock for room-level participant snapshots and shared
  indexes if needed.
- Tmux I/O may still serialize behind the global lock until Stage 13.
- Do not migrate clear or interrupt in this stage unless the diff stays small.

Validation:

```sh
python3 scripts/run_regressions.py --match delivery --fail-fast
python3 scripts/run_regressions.py --match prompt --fail-fast
python3 scripts/run_regressions.py --match response --fail-fast
python3 scripts/run_regressions.py --match turn_id_mismatch --fail-fast
python3 scripts/run_regressions.py
```

Checklist:

- Same-target delivery and response remain serialized.
- Target locks are acquired and released in the intended places, but unrelated
  target tmux I/O independence is not required until Stage 13.
- Multi-target acquisition order is sorted and documented.
- Tests cover concurrent or simulated concurrent delivery attempts to the same
  target.

## Stage 13: Move Tmux Delivery Out Of The Global Lock

Goal: reduce the largest known contention source.

Scope:

- Keep target lock held across pane touch for the affected target.
- Release the global lock before tmux paste/probe/Enter where safe.
- Reacquire the necessary lock to finalize success/failure.
- Clearly model pane-touched states so cancel/interrupt behavior remains
  correct.

Validation:

```sh
python3 scripts/run_regressions.py --match cancel_message --fail-fast
python3 scripts/run_regressions.py --match interrupt --fail-fast
python3 scripts/run_regressions.py --match pane_mode --fail-fast
python3 scripts/run_regressions.py --match delivery_watchdog --fail-fast
python3 scripts/run_regressions.py
```

Checklist:

- Cancel boundary still rejects pane-touched inflight work with interrupt
  guidance.
- Prompt submission cannot race nonce/reserved state into an impossible state.
- Different targets can deliver without waiting on unrelated target tmux I/O.
- Delivery watchdogs still represent delivery and response phases correctly.
- Endpoint-lost finalization remains terminal and does not redeliver.

## Stage 14: Split Watchdog/Alarm Lock

Goal: remove independent alarm/watchdog operations from the routing critical
path where possible.

Scope:

- Add a physical watchdog/alarm lock.
- Define lock order explicitly. Recommended order:
  room/global snapshot lock, target lock(s), watchdog lock, then queue store.
- Do not nest aggregate store/file locks inside this order. Aggregate work that
  needs routing effects must happen as aggregate phase, release, routing phase.
- Convert due-watchdog collection into snapshot-under-watchdog-lock followed by
  routing effects under target/queue locks.
- Keep alarm cancel on incoming message atomic with ingress finalization.

Validation:

```sh
python3 scripts/run_regressions.py --match alarm --fail-fast
python3 scripts/run_regressions.py --match watchdog --fail-fast
python3 scripts/run_regressions.py --match ingressing --fail-fast
python3 scripts/run_regressions.py --match command_state_lock --fail-fast
python3 scripts/run_regressions.py
```

Checklist:

- Alarm registration can proceed without waiting on unrelated target tmux I/O.
- Watchdog fire and terminal response race resolves to one terminal outcome and
  no stale wake delivery.
- Ingressing finalize still atomically cancels target-owned alarms and promotes
  the row to pending.
- Requester clear racing aggregate completion has an explicit test or
  documented lock-order proof.
- Deadlock-oriented tests or code assertions cover lock order.

## Stage 15a: Migrate Interrupt To Target Locks

Goal: move interrupt sequencing to target locks.

Scope:

- Interrupt: hold the target lock across pane resolve, ESC, optional C-c, active
  cancellation, tombstone recording, and partial-failure gate updates.
- Use the global lock only for room-level participant/session cache mutation.
- Do not migrate controlled clear in this stage.

Validation:

```sh
python3 scripts/run_regressions.py --match interrupt --fail-fast
python3 scripts/run_regressions.py --match interrupt_endpoint_lost --fail-fast
python3 scripts/run_regressions.py --match aggregate_interrupt --fail-fast
python3 scripts/run_regressions.py
```

Checklist:

- No delivery can occur between interrupt keys for the same target.
- Partial interrupt failure still blocks that target and only that target.
- Interrupted-turn tombstones and aggregate synthetic replies are unchanged.

## Stage 15b: Migrate Clear To Target Locks

Goal: move controlled clear to target locks after interrupt migration.

Scope:

- Clear: hold all batch target locks in deterministic order through guard and
  reservation. Keep pane-touched recovery and identity replacement phases
  explicit.
- Replace or adapt `Condition(state_lock)` only with tests and documentation in
  the same commit.
- Use the global lock only for room-level participant/session cache mutation.

Validation:

```sh
python3 scripts/run_regressions.py --match clear --fail-fast
python3 scripts/run_regressions.py --match clear_identity --fail-fast
python3 scripts/run_regressions.py --match clear_force_leave --fail-fast
python3 scripts/run_regressions.py --match force_clear --fail-fast
python3 scripts/run_regressions.py
```

Checklist:

- Clear reservation still blocks inbound delivery for every batch target before
  any pane is touched.
- Force clear still invalidates cancellable inbound rows, target-owned alarms,
  requester-originated requests, and incomplete aggregates consistently.
- Condition/wait signaling for clear probe remains correct after lock changes.

## Stage 16: Cleanup And Documentation

Goal: remove temporary compatibility once the new structure is stable.

Scope:

- Remove unused wrappers and re-exports only if no tests or public tools depend
  on them.
- Update `docs/known-issues.md` if live-state, restart, or lock-order behavior
  changed.
- Update `docs/milestones.md` with completed items.
- Document final lock order and service ownership in code comments.

Validation:

```sh
python3 scripts/run_regressions.py --list
python3 scripts/run_regressions.py
./install.sh --dry-run
bin/bridge_healthcheck.sh
```

Checklist:

- `bridge_run`, `bridge_manage`, and model-facing shims still locate the daemon.
- Public CLI output remains stable unless a stage explicitly changed it.
- Docs match actual lock order and recovery behavior.
- Reviewers report no remaining module-boundary or lock-order concerns.

## Additional Tests To Add During Lock-Behavior Stages

Add these when the relevant stage begins; do not front-load all of them into a
pure move commit.

- Same-target concurrent delivery attempts leave exactly one inflight row.
- Different-target delivery can proceed while one target's tmux send is slow.
- Watchdog fire racing terminal response produces either terminal cleanup or a
  skipped stale wake, never both a delivered wake and a returned result.
- File-fallback ingressing finalize racing alarm registration is idempotent and
  does not cancel a later alarm on event replay.
- Interrupt partial failure blocks replacement delivery only for the affected
  target.
- Multi-clear lock acquisition uses deterministic order and cannot deadlock
  with simultaneous single-target clear/interrupt operations.
- Aggregate completion racing requester clear does not enqueue duplicate result
  messages.

## Commit Discipline

- One stage should be one commit unless the worker announces a smaller commit
  split before editing.
- Do not mix behavior changes with mechanical file moves.
- Do not reformat whole files unless the stage explicitly owns formatting.
- Each commit message should use a short imperative subject, for example
  `Extract daemon tmux helpers` or `Add daemon state facade`.
- Every commit description should list validation commands and reviewer roles
  consulted.

## Acceptance Criteria

- The daemon is split into reviewable services with explicit state ownership.
- `state_lock` contention from unrelated targets is reduced.
- Command-socket operations no longer block behind unrelated long tmux delivery
  or housekeeping work.
- Existing regression suite passes.
- Added concurrency tests cover the new lock boundaries.
- Documentation describes the final lock order and any remaining deliberate
  coarse-lock regions.
