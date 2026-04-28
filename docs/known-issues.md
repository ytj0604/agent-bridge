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
Evidence: `bridge_daemon.py::handle_prompt_submitted`,
`bridge_daemon.py::handle_response_finished`.

### In-Memory Live Routing State

Current prompts, reservations, watchdogs, alarms, `last_enter_ts`, interrupt
holds, partial-failure gates, and recent tombstones are daemon memory. Queue rows
and aggregate JSON persist, but restart can lose live routing context. Evidence:
`bridge_daemon.py::BridgeDaemon.__init__`,
`bridge_daemon.py::_recover_orphan_delivered_messages`, `bridge_daemon_ctl.py`.

### Bounded Tombstone Window

Recent terminal/idempotency tombstones are best-effort memory: 600 seconds and
16 records per target agent. They improve near-term diagnostics and stale wake
suppression, not durable history. Evidence:
`INTERRUPTED_TOMBSTONE_TTL_SECONDS`,
`INTERRUPTED_TOMBSTONE_LIMIT_PER_AGENT`.

### Best-Effort Status Snapshots

`agent_wait_status` and `agent_aggregate_status` are debug snapshots, not
transactional state dumps. Queue/watchdog state is snapshotted under daemon
lock; aggregate JSON is read afterward to avoid lock-order risk. `wake_id`
values are also in-memory correlation ids only. Evidence:
`bridge_daemon.py::build_wait_status`,
`bridge_daemon.py::build_aggregate_status`.

### Model-Facing Payload Limits And Stable Body Errors

Inline model-facing message bodies are capped at 11000 chars; larger payloads
should be written under `/tmp/agent-bridge-share/` and sent by path. Body-input
diagnostics currently expose five stable codes: `missing_body`, `empty_stdin`,
`unexpected_positional_after_stdin`, `stdin_inline_conflict`, and
`piped_stdin_inline_conflict`. Evidence:
`bridge_util.py::MAX_INLINE_SEND_BODY_CHARS`,
`bridge_send_peer.py::body_input_error`, and
`bridge_send_peer.py::BodyInputError`.

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

**Evidence**: `bridge_daemon.py::handle_prompt_submitted`,
`bridge_daemon.py::handle_response_finished`,
`bridge_daemon.py::find_inflight_candidate`, fix lineage `93bce7c`.

---

### K-02: Codex sandbox socket block forces fallback with recovery gaps

**Status**: current.

**Symptom**: Some Codex sandbox profiles block daemon UNIX socket `connect()`,
so `agent_send_peer` uses file fallback (`pending.json` plus event log) instead
of daemon socket enqueue. The normal fallback path works, but recovery paths can
promote an `ingressing` row without the live path's alarm-cancel semantics.

**Root cause**: macOS/iOS `sandbox-exec` or runtime policy can block
`connect()` for UNIX sockets even when ordinary file writes are allowed. The
current shared JSON fallback keeps sends working, but it is weaker than daemon
socket enqueue as a transport and recovery boundary.

**Mitigation / candidate approaches**: Happy-path fallback rows use
`status="ingressing"` and are finalized under daemon `state_lock`, including
alarm cancellation and body prepend. Future transport candidates are proposals:
FIFO/named pipe could preserve stream-like delivery without UNIX socket
`connect()`, but needs portable blocking/error handling; Maildir-style atomic
file queues could avoid socket use and shared-file races, but require a larger
queue redesign and platform watchers; TCP loopback may work where sandbox
network-outbound is allowed, but needs port allocation and local access-control
design.

**Residual risk**: `_recover_ingressing_messages` and
`_promote_aged_ingressing` promote stale rows but do not apply alarm-cancel
semantics. An alarm that would have been cancelled by the live ingress path can
still fire later. See also the *In-Memory Live Routing State* structural limit
above — alarm-cancel state is daemon memory and does not survive into the
recovery path.

**Evidence**: `bridge_enqueue.py` fallback path,
`bridge_daemon.py::enqueue_ipc_message`,
`bridge_daemon.py::handle_external_message_queued`,
`bridge_daemon.py::_apply_alarm_cancel_to_queued_message`,
`bridge_daemon.py::_recover_ingressing_messages`, and
`bridge_daemon.py::_promote_aged_ingressing`.

---

### K-03: Daemon-socket mixed-target guard wording is less specific

**Status**: current, low-priority diagnostic parity issue.

**Symptom / risk**: During response-time send validation, a target list that
includes the active requester plus third-party peers can produce more precise
mixed-target wording on the file-fallback path than on the daemon-socket path.
Socket validation can report the single-target requester wording instead.

**Root cause**: File-fallback formatting still has the full target set. The
daemon socket path validates per-message payloads, so daemon-side formatting
lacks the original mixed target list.

**Mitigation**: Behavior remains safe and atomic: sends containing the requester
are rejected, third-party-only sends remain allowed by this guard, and the error
still tells the model to reply to the requester in the current response.

**Residual risk**: This is diagnostic consistency only, not a routing
correctness issue. Future cleanup could pass target-set metadata into
daemon-side validation to make socket and fallback wording identical.

**Evidence**: `bridge_response_guard.py::format_response_send_violation`,
`bridge_send_peer.py` response-guard tests, followup lineage `8273bca`.
