# Known Issues

This document tracks current unresolved issues and structural limits for Agent
Bridge. Fixed issues should be removed, not retained as resolved entries.

Resolved lineage lives in git history and the companion trackers:
`docs/agent-bridge-field-test-2026-04-27-fixes.md` and
`docs/agent-bridge-followup-issues-2026-04-28.md`.

Evidence references prefer stable symbol names over line numbers because line
anchors drift quickly in this codebase.

---

## Intended Structural Limits

These are current design tradeoffs operators should know. They are not
necessarily permanent architecture; persistence, quiesce, or transport
redesign work could harden several of them later.

### Shared Pane Channel

Bridge prompts and human input share the same tmux pane. The daemon can validate
nonce/status and route protocol results, but it cannot force the model's text to
semantically address the peer rather than the human when pane context is mixed.
Evidence: `bridge_daemon_events.py::handle_prompt_submitted`,
`bridge_daemon_events.py::handle_response_finished`.

### In-Memory Live Routing State

Current prompts, clear reservations, target locks, watchdogs, alarms,
`last_enter_ts`, interrupt holds, partial-failure gates, and recent tombstones
are daemon memory. Queue rows and aggregate JSON persist, but restart can lose
live routing context. Evidence: `bridge_daemon.py::BridgeDaemon.__init__`,
`bridge_daemon.py::_preserve_startup_inflight_messages`,
`bridge_daemon.py::_recover_orphan_delivered_messages`, `bridge_daemon_ctl.py`.

Pre-existing `status="inflight"` queue rows are preserved after daemon restart
as delivery blockers because they may already have been pasted into the target
pane. The new daemon waits for a matching `prompt_submitted` hook to bind the
row by nonce; if that hook never arrives, operator recovery is explicit via
`agent_interrupt_peer <alias>` or `agent_cancel_message <msg_id>` when the row is
still safely cancellable.

### Lock Order And Service Boundaries

The daemon is split into service modules, but `BridgeDaemon` remains the facade
and compatibility surface for command handlers, hooks, and regression tests.
The final routing lock order is target lock(s) in sorted alias order, then
`state_lock`, then `watchdog_lock`, then the queue file lock used by
`QueueStore.update()`. Watchdog-only operations such as `agent_alarm`
registration may take `watchdog_lock` without `state_lock`. `AggregateStore`
reads and writes are intentionally kept outside that chain: aggregate state is
snapshotted or mutated first, released, and then routing/watchdog/queue effects
run in a separate phase.

`state_lock` is still a deliberate coarse region for short shared-state and
room-level cache mutations: participant/session cache, pane cache, busy/reserved
flags, current prompt contexts, nonce caches, and tombstones. Slow tmux IO for
normal delivery, interrupt, and controlled-clear pane-touch paths runs outside
`state_lock`, guarded by per-target locks or clear reservations instead. A
small compatibility path still drains delivery while `state_lock` is already
held for callers that cannot yet switch lock ownership. During controlled
clear, reservations keep delivery gated while `run_clear_peer` may release and
reacquire the target lock around settle/probe waits. Evidence:
`bridge_daemon.py::BridgeDaemon.__init__`,
`bridge_daemon_state.py::TargetLockManager`,
`bridge_daemon_delivery.py::try_deliver`,
`bridge_daemon_maintenance.py::DeliveryScheduler`,
`bridge_daemon_interrupts.py::handle_interrupt`, and
`bridge_daemon_clear_flow.py::run_clear_peer`.

### Bounded Tombstone Window

Recent terminal/idempotency tombstones are best-effort memory: 600 seconds and
16 records per target agent. They improve near-term diagnostics and stale wake
suppression, not durable history. Evidence:
`bridge_daemon_interrupts.py::INTERRUPTED_TOMBSTONE_TTL_SECONDS`,
`bridge_daemon_interrupts.py::INTERRUPTED_TOMBSTONE_LIMIT_PER_AGENT`.

### Best-Effort Status Snapshots

`agent_wait_status` and `agent_aggregate_status` are debug snapshots, not
transactional state dumps. Queue/watchdog state is snapshotted under daemon
lock; aggregate JSON is read afterward to avoid lock-order risk. `wake_id`
values are also in-memory correlation ids only. Evidence:
`bridge_daemon_status.py::build_wait_status`,
`bridge_daemon_status.py::build_aggregate_status`.

### Multi-Clear Atomic Guard, Best-Effort Execution

Multi-target `agent_clear_peer` is atomic only through guard and reservation:
the daemon snapshots queue and aggregate state, evaluates every target, and
reserves every target before any pane is touched. Execution is sequential and
best-effort after that point. A later pane/probe/identity failure can produce
mixed `cleared`, `forced_leave`, and `failed` results, and inbound delivery to
all batch targets is held for the duration of the batch. Evidence:
`bridge_daemon_clear_flow.py::handle_clear_peers`,
`bridge_daemon_clear_flow.py::run_clear_peer`.

### Model-Facing Payload Limits And Stable Body Errors

Inline model-facing message bodies are capped at 11000 chars; larger payloads
should be written under `/tmp/agent-bridge-share/` and sent by path. Body-input
diagnostics currently expose five stable codes: `missing_body`, `empty_stdin`,
`unexpected_positional_after_stdin`, `stdin_inline_conflict`, and
`piped_stdin_inline_conflict`. Evidence:
`bridge_util.py::MAX_INLINE_SEND_BODY_CHARS`,
`bridge_send_peer.py::body_input_error`, and
`bridge_send_peer.py::BodyInputError`.

Auto-routed returned results use the daemon's 12000-char body guard. When a
returned result would exceed that guard, the daemon writes the normalized full
result to a private daemon-managed directory under `/tmp/agent-bridge-share/`
and delivers a small `[bridge:body_redirected]` wrapper with a `File:` line and
short JSON-escaped preview. The wrapper's preview is intentionally incomplete;
recipients should read the file. If redirect fails, the
receiver-visible reason is one of `permission_denied`, `no_space`, `collision`,
`symlink_unsafe`, `unsafe_path`, `write_failed`, or `wrapper_too_large`.
Redirected result files are not currently cleaned up automatically. Evidence:
`bridge_daemon.py::redirect_oversized_result_body`.

### Cancel / Interrupt Boundary

`agent_cancel_message` retracts pending, inflight pre-pane-touch/pre-paste, or
pane-mode-deferred work. After pane touch, submitted, delivered, or responding
states, `agent_interrupt_peer` owns the active-work path. Fast delivery can move
past the cancellable window before cancel wins. Evidence:
`bridge_daemon.py::cancel_message`.

### Requester-Only Response-Time Send Guard

While responding to an auto-return peer request, separate `agent_send_peer`
messages to requester `current_prompt.from` are blocked. Third-party
review/collaboration sends are intentionally not blocked by this guard, though
other validations still apply. Evidence:
`bridge_response_guard.py::format_response_send_violation`.

### Compact Attach Probe

The attach-time probe is a compact quickstart, not the full reference.
`agent_list_peers` prints the expanded cheat sheet when command syntax or
semantics are unclear. Evidence: `bridge_instructions.py::probe_prompt`,
`bridge_instructions.py::model_cheat_sheet`.

### User-Side Esc Cancellation Is Undetectable

When a user presses Esc directly in a peer pane to cancel an in-progress
generation, neither Claude Code nor Codex CLI fires a Stop/`response_finished`
hook. The daemon therefore cannot observe the cancellation and the peer stays
`busy=True` with `current_prompt_id` still bound; further inbound messages
queue behind that stuck context. Recovery requires either (a) the user
submitting any follow-up prompt in that pane so a normal
`prompt_submitted`/`response_finished` cycle resolves the matching, or (b)
another agent calling `agent_interrupt_peer <alias>` to force daemon-side
cleanup. Operationally users should avoid Esc-only cancellation while bridge
routing is active.

### User-Side `/clear` Drops The Alias

When a user types `/clear` directly in a peer pane, the daemon treats the
peer as detached: identity rotation, fingerprint, and routing state cannot be
recovered without a controlled-clear marker. The alias remains visible in
session state but stops receiving new bridge traffic. Recovery is to clear
that peer through `agent-bridge manage`, through another agent's
`agent_clear_peer <alias>`, or by asking the peer itself to `agent_clear_peer`
its own alias. Operationally users should not invoke `/clear` directly while
bridge routing is active.

---

## Current Issues

### K-01: Shared pane prompts can still confuse reply semantics

**Status**: current, low-priority residual.

**Symptom / risk**: The model can generate a reply that semantically addresses
the human while the bridge correctly auto-routes it to the peer that sent the
active request. A rarer variant is prompt-body contamination where the live
nonce is valid but extra human-typed content is concatenated into the same pane
submission.

**Root cause**: The bridge validates the protocol envelope, not the model's
internal addressee or exact submitted body. Nonce/status checks prevent stale
quoted markers from binding routing context, but they do not prove the full
prompt body equals the daemon-injected body.

**Mitigation**: Handle bridge prompts promptly, avoid typing into the pane
during bridge delivery, and use `agent_view_peer`, `agent_wait_status`, or
`agent_aggregate_status` for debugging if a peer appears stuck or confused.

**Residual risk**: Fully closing this requires a stronger out-of-band inbox or
prompt-body integrity check that survives model TUI behavior.

**Evidence**: `bridge_daemon_events.py::handle_prompt_submitted`,
`bridge_daemon_events.py::handle_response_finished`,
`bridge_daemon_events.py::find_inflight_candidate`, fix lineage `93bce7c`.

---

### K-02: Codex sandbox blocks socket transport; file fallback is the de-facto path

**Status**: current; file fallback runs cleanly in normal operation. The only
unresolved part is the daemon-restart recovery race noted below.

**Symptom**: In default Codex sandbox modes, every `connect()` syscall from a
Codex-spawned subprocess returns `EPERM` regardless of socket family or
destination. Codex peers' `agent_send_peer` cannot reach
`<run_root>/<session>.sock`; the CLI silently falls back to writing a
`status="ingressing"` row into `queue.json` plus a `message_queued` event. The
daemon picks it up via its event-tail loop and finalizes (alarm cancel + body
prepend + promote to pending) under `state_lock`. Model-facing UX is identical
to socket transport.

**Root cause**: Codex spawns user subprocesses with a stricter sandbox profile
than the Codex CLI process itself. The Linux sandbox uses bubblewrap + seccomp;
`connect(2)` is blocked unconditionally for both `AF_UNIX` and `AF_INET`
regardless of the user's `[sandbox_workspace_write] network_access` config.
That config setting governs only the Codex CLI's own outbound API access, not
its spawned subprocesses. Upstream tracking:
[openai/codex#16910](https://github.com/openai/codex/issues/16910) (AF_UNIX
support feature request) and
[openai/codex#10797](https://github.com/openai/codex/issues/10797).

**Mitigation / candidate approaches**: The file-fallback path is the normal
operating mode for Codex peers under any non-`danger-full-access` sandbox, not
an exceptional code path. Ingressing rows finalize under daemon `state_lock`
with the same alarm-cancel and body-prepend semantics as socket ingress, so
end-to-end behaviour is functionally equivalent. The only setting that permits
direct socket connect from Codex peers today is
`sandbox_mode = "danger-full-access"`, which trades sandbox isolation for
direct transport. Future transport candidates are deferred:
- *Maildir-style atomic file queue* — structurally clean and would close the
  recovery race below by giving each message its own atomically-renamed
  crash-safe file. Significant implementation cost (queue redesign +
  platform-specific watchers) without an observed day-to-day benefit while the
  current fallback works; deferred unless Codex sandbox semantics change or the
  recovery race becomes operationally observed.
- *FIFO (named pipe)* — gives no recovery improvement over current fallback
  (in-flight bytes also lost on daemon crash) and `mkfifo()` may itself be
  sandbox-blocked. Not pursued.
- *TCP loopback* — empirically blocked by the Codex sandbox identically to
  `AF_UNIX`, so not a viable workaround.

**Residual risk**: `_recover_ingressing_messages` and `_promote_aged_ingressing`
promote stale rows on daemon restart but do not reapply the alarm-cancel
semantics that the live ingress path runs under `state_lock`. An alarm the live
ingress path would have cancelled can therefore fire later if the daemon dies
inside the few-millisecond finalize window. See also the *In-Memory Live
Routing State* structural limit above — alarm-cancel state is daemon memory
and does not survive into the recovery path.

**Evidence**: `bridge_enqueue.py::enqueue_via_daemon_socket`, `bridge_enqueue.py`
file-fallback block, `bridge_daemon_events.py::handle_external_message_queued`,
`bridge_daemon_events.py::_apply_alarm_cancel_to_queued_message`,
`bridge_daemon.py::_recover_ingressing_messages`, and
`bridge_daemon.py::_promote_aged_ingressing`. Empirical probe (2026-04-28): in
default Codex sandbox, a `python3` subprocess returned
`PermissionError [Errno 1] Operation not permitted` for both
`socket.AF_UNIX.connect('/tmp/agent-bridge-0/run/agent-bridge-auto.sock')` and
`socket.AF_INET.connect(('127.0.0.1', <live-listener>))` despite
`[sandbox_workspace_write] network_access = true` set in `~/.codex/config.toml`.
