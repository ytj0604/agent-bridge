# agent_wait_status --why Plan

Status: proposed. This document is an implementation-ready specification for a
human-oriented `agent_wait_status --why` mode. It intentionally describes only
the next practical increment; it does not require daemon state redesign.

## Problem

`agent_wait_status` currently answers the debugging question "what raw bridge
state is associated with me?" as JSON. That is useful for tests and precise
inspection, but it is not a good operator answer to "why am I waiting, and what
should I do next?"

In real bridge use, the JSON shape is easy to misread:

- `pending_inbound.total_count > 0` means a bridge prompt is queued for this
  caller, not necessarily that the peer is still thinking.
- `outstanding_requests` and `watchdogs` are split across sections, so the
  caller has to correlate message ids by hand.
- `alarms` are notice-workflow safety wakes, not request result waits.
- `aggregate_waits` tells which peers are missing, but the recommended next
  action is usually to wait for a `[bridge:*]` prompt, not poll.
- Empty status is currently just an empty JSON object per section; it does not
  clearly say "there is nothing bridge-related to wait on."

The desired improvement is a concise explanation mode that keeps the current
JSON contract intact while translating it into model/operator-safe guidance.

## Current Implementation

### CLI Entry Point

`libexec/agent-bridge/bridge_wait_status.py`

- Parses `--summary`, `--json`, `--session`, `--from`, and `--allow-spoof`.
- Resolves the caller via `resolve_caller_from_pane`.
- Ensures the daemon is running and the room is active enough for read.
- Sends exactly:

```json
{"op": "wait_status", "from": "<alias>"}
```

- Prints the daemon response as JSON. `--summary` prints only the top-level
  `ok`, `bridge_session`, `caller`, `generated_ts`, `limits`, and `summary`.
- `--json` is currently accepted but effectively redundant because JSON is the
  default output mode.

### Daemon Command

`libexec/agent-bridge/bridge_daemon_commands.py::handle_command_connection`

- Handles `op == "wait_status"`.
- Reloads participants.
- Rejects missing, bridge, or inactive senders.
- Calls `d.build_wait_status(sender)`.

### Status Builder

`libexec/agent-bridge/bridge_daemon_status.py::build_wait_status`

- Takes `command_state_lock(command_class="wait_status")`.
- Snapshots watchdog state via `d.watchdog_snapshot()`.
- Reads the queue via `d.queue.read()`.
- Releases daemon locks, then reads aggregate JSON with
  `d.aggregates.read_aggregates()` so aggregate store access stays outside the
  routing lock chain.
- Returns:

```text
ok
bridge_session
caller
generated_ts
limits.per_section
summary
outstanding_requests
aggregate_waits
alarms
watchdogs
pending_inbound
```

The current section meanings are:

- `outstanding_requests`: caller-originated auto-return requests in
  `pending`, `inflight`, `submitted`, or `delivered` queue states. Rows include
  message id, target, status, timestamps, aggregate id, and linked watchdog
  wake ids.
- `watchdogs`: non-alarm watchdog wakes owned by the caller. Rows include wake
  id, message/aggregate id, target, phase, deadline, kind, and intent.
- `alarms`: caller-owned alarm wakes. Rows include wake id, deadline, and note.
- `aggregate_waits`: incomplete aggregates requested by the caller. Rows
  include expected/replied counts, missing peers, message ids, and update time.
- `pending_inbound`: queue rows addressed to the caller with `status=pending`.
  Rows include metadata and `body_chars`, deliberately not body previews.

### Existing Regression Coverage

Primary test file:

`scripts/regression/aggregate_wait_status.py`

Important scenarios:

- `wait_status_empty_self_view`
- `wait_status_uses_watchdog_snapshot`
- `wait_status_outstanding_watchdogs_alarms_pending_inbound`
- `wait_status_aggregate_waits_privacy_and_completed_result`
- `wait_status_caps_and_summary_counts`
- `wait_status_cli_summary_and_json`
- `wait_status_cli_unsupported_old_daemon`
- `wait_status_cli_rejects_inactive_sender`

Registry entries live in `scripts/regression/registry.py`.

Related docs-contract test:

- `scripts/regression/docs_contracts.py::scenario_wait_status_doc_surfaces_anti_polling`

## Gaps

The current JSON is precise but lacks interpretation:

- It does not rank what matters first. For example, a pending inbound prompt is
  usually more immediately actionable than a future alarm.
- It does not explain the state transition implied by request statuses:
  pending delivery vs pasted prompt vs waiting for response.
- It does not tell the caller which command, if any, is appropriate next.
- It does not strongly reinforce the anti-polling rule in the moment where the
  user/model is tempted to poll.
- It exposes multiple sections that can describe the same wait from different
  angles, so a model has to deduplicate mentally.
- It is intentionally privacy-preserving, but that also means the operator has
  no short body preview to anchor on. The explanation should preserve that
  privacy property while still naming message ids, peers, and high-level kinds.

## Proposed CLI Contract

Add:

```sh
agent_wait_status --why
```

Behavior:

- Fetches the same daemon `wait_status` JSON as today.
- Does not mutate daemon, queue, aggregate, watchdog, alarm, or cursor state.
- Prints a concise human-readable explanation to stdout.
- Keeps current exit codes and preflight errors.
- Is explicitly not a polling primitive.

Recommended parser behavior:

- `--why` is an output mode.
- Existing default JSON output remains unchanged.
- `--summary` and `--why` should be mutually exclusive; if both are passed,
  exit 2 with a clear argparse/parser error.
- `--json --why` should produce structured JSON for automation and tests:

```json
{
  "ok": true,
  "bridge_session": "...",
  "caller": "...",
  "generated_ts": "...",
  "why": {
    "status": "waiting|idle|attention",
    "items": [
      {
        "category": "pending_inbound|request|aggregate|alarm|watchdog",
        "severity": "attention|waiting|info",
        "subject": "...",
        "reason": "...",
        "next_action": "...",
        "refs": {"message_id": "...", "wake_id": "...", "target": "..."}
      }
    ],
    "next_actions": ["..."]
  }
}
```

Without `--json`, `--why` prints text only. The text should be stable enough
for tests but not as rigid as the JSON schema.

## Explanation Rules

Build the explanation from the existing daemon response. Prefer implementing
this in `bridge_wait_status.py` first:

```text
build_why_model(response: dict) -> dict
format_why_text(model: dict) -> str
```

No daemon-side change is required for the first increment.

### Priority Order

Render items in this order:

1. `pending_inbound`
2. `outstanding_requests`
3. `aggregate_waits`
4. `alarms`
5. `watchdogs` that are not already referenced by an outstanding request or
   aggregate wait
6. idle/no waits

Rationale: pending inbound means something is already queued for this caller;
request and aggregate waits represent active delegated work; alarms are safety
wakes; orphan watchdog rows are diagnostic.

### Pending Inbound

Input source:

`response["pending_inbound"]["items"]`

Text guidance:

- "A bridge message is queued for you from `<from>`."
- "Finish the current turn and wait for the incoming `[bridge:*]` prompt."
- "Do not poll."

Do not include body previews. Use `body_chars`, `kind`, `intent`,
`ref_message_id`, and `ref_aggregate_id` only.

Example text:

```text
- Pending inbound notice msg-123 from reviewer2 is queued for this pane (42 chars). Finish this turn and wait for the [bridge:*] prompt; do not poll.
```

### Outstanding Requests

Input source:

`response["outstanding_requests"]["items"]`

Status-specific guidance:

- `pending`: queued for target but not yet active. Normal action is wait. If
  the human wants to retract before active delivery, use
  `agent_cancel_message <message_id>`.
- `inflight`: delivery is reserved/in progress. Wait for prompt submission or
  a watchdog. If this seems stuck after a wake, inspect with
  `agent_view_peer <target>` or use `agent_interrupt_peer <target>` if the
  prompt is wrong/active.
- `submitted`: target prompt was submitted and response is starting/active.
  Wait for result or watchdog. Do not send duplicate requests.
- `delivered`: target has the bridge prompt; waiting for response. If a
  watchdog fires, choose one of `agent_extend_wait`, `agent_view_peer`, or
  `agent_interrupt_peer` based on the wake text.

If `watchdog_wake_ids` is non-empty, mention the wake ids and point to the
`watchdogs` section for deadlines in JSON mode; in text mode, include the first
matching deadline if available from `response["watchdogs"]`.

Example text:

```text
- Request msg-abc to reviewer1 is delivered; waiting for reviewer1 to answer. Watchdog wake wake-1 is scheduled for 2026-04-30T13:30:00.000000Z.
```

### Aggregate Waits

Input source:

`response["aggregate_waits"]["items"]`

Guidance:

- Explain progress: `replied_count / expected_count`.
- Name missing peers, capped if long.
- Recommend waiting for the aggregate result prompt.
- Mention `agent_aggregate_status <aggregate_id>` only as a human/debug action,
  not as polling.

Example text:

```text
- Aggregate agg-123 is waiting for 1 of 3 peers: reviewer2. Wait for the aggregate [bridge:*] result; use agent_aggregate_status agg-123 only for a human-prompted debug check.
```

### Alarms

Input source:

`response["alarms"]["items"]`

Guidance:

- Explain that alarms are safety wakes for notice-driven workflows.
- They are cancelled by incoming peer request/notice, not by result messages.
- Do not imply that an alarm means a request is outstanding.

Example text:

```text
- Alarm wake-abc is scheduled for 2026-04-30T13:30:00.000000Z: waiting for notice-workflow follow-up. It will cancel on an incoming peer request/notice; do not poll.
```

### Orphan/Standalone Watchdogs

Input source:

`response["watchdogs"]["items"]`

Skip watchdogs already referenced by:

- `outstanding_requests[].watchdog_wake_ids`
- aggregate waits when a future implementation can correlate aggregate ids

Guidance:

- A standalone watchdog is diagnostic; it may refer to a message no longer in
  the outstanding request section or to aggregate response tracking.
- Tell the caller to wait for the wake or use status/debug only if
  human-prompted.

Example text:

```text
- Watchdog wake-orphan for msg-old is still registered in delivery phase. This is diagnostic state; wait for a bridge wake or inspect only if debugging.
```

### Idle

If all sections have zero `total_count`:

```text
No bridge waits for worker. There are no outstanding requests, aggregate waits, alarms, watchdogs, or pending inbound bridge messages.
```

If sections are truncated, append:

```text
Some sections are truncated; rerun agent_wait_status --json for full capped metadata.
```

Note: the daemon already caps each section at `WAIT_STATUS_SECTION_LIMIT`; `--why`
should not bypass or increase the cap.

## Output Examples

### Idle

```text
Bridge wait status for worker at 2026-04-30T13:00:00.000000Z
No bridge waits for worker. There are no outstanding requests, aggregate waits, alarms, watchdogs, or pending inbound bridge messages.
```

### Pending Inbound Plus Alarm

```text
Bridge wait status for worker at 2026-04-30T13:00:00.000000Z
- Pending inbound notice msg-relay from reviewer2 is queued for this pane (42 chars). Finish this turn and wait for the [bridge:*] prompt; do not poll.
- Alarm wake-followup is scheduled for 2026-04-30T13:05:00.000000Z: waiting for reviewer2 relay. It will cancel on an incoming peer request/notice; do not poll.
Next: finish the current response and wait for the bridge prompt.
```

### Outstanding Request

```text
Bridge wait status for worker at 2026-04-30T13:00:00.000000Z
- Request msg-review to reviewer1 is delivered; waiting for reviewer1 to answer. Watchdog wake wake-review is scheduled for 2026-04-30T13:03:00.000000Z.
Next: wait for the [bridge:*] result. If a watchdog wake arrives, choose one recovery action from that wake.
```

## Affected Files

Expected implementation files:

- `libexec/agent-bridge/bridge_wait_status.py`
  - Add parser flag `--why`.
  - Add `build_why_model(response: dict) -> dict`.
  - Add `format_why_text(model: dict) -> str`.
  - Keep current JSON and `--summary` behavior unchanged when `--why` is not
    used.
- `scripts/regression/aggregate_wait_status.py`
  - Add CLI tests for `--why` text output.
  - Add CLI tests for `--why --json` structured output.
  - Add parser conflict test for `--summary --why` if the implementation makes
    them mutually exclusive.
- `scripts/regression/registry.py`
  - Register new scenarios.
- `libexec/agent-bridge/bridge_instructions.py`
  - Update the `agent_wait_status` line to mention `--why` as a human-readable
    diagnosis mode, while preserving the anti-polling warning.
- `scripts/regression/docs_contracts.py`
  - Update `wait_status_doc_surfaces_anti_polling` to expect `--why` and
    confirm it is framed as diagnostic, not polling.

Optional files:

- `libexec/agent-bridge/bridge_daemon_status.py`
  - Not needed for the first increment. Only touch this if the team decides
    the daemon should produce the structured `why` model directly.

## Required Tests

Add focused tests before broad suites:

1. `wait_status_cli_why_idle`
   - Patch `bridge_wait_status.send_command` to return an empty wait-status
     response.
   - Run `agent_wait_status --why`.
   - Assert exit 0, stderr empty, stdout contains "No bridge waits" and does
     not contain JSON braces as the primary format.

2. `wait_status_cli_why_pending_inbound`
   - Response includes one pending inbound notice with `body_chars`.
   - Assert text mentions pending inbound, sender, message id, finish current
     turn, wait for `[bridge:*]`, and do not poll.
   - Assert sender-controlled body text is not printed.

3. `wait_status_cli_why_request_with_watchdog`
   - Response includes one delivered outstanding request and a matching
     watchdog deadline.
   - Assert text says waiting for target to answer and includes wake id/deadline.

4. `wait_status_cli_why_aggregate_alarm`
   - Response includes one aggregate wait and one alarm.
   - Assert text distinguishes aggregate wait from alarm safety wake.
   - Assert alarm wording says incoming peer request/notice cancels it and does
     not claim result messages cancel alarms.

5. `wait_status_cli_why_json`
   - Run `agent_wait_status --why --json`.
   - Assert stdout is JSON and contains `why.status`, `why.items`, and
     `why.next_actions`.

6. `wait_status_cli_why_summary_conflict`
   - If `--summary --why` is rejected, assert exit 2 and clear error.
   - If implementation chooses different parser semantics, document and test
     that exact behavior.

7. Existing coverage must still pass:

```sh
python3 scripts/run_regressions.py --match wait_status --fail-fast
python3 scripts/run_regressions.py --match wait_status_doc --fail-fast
python3 -m py_compile libexec/agent-bridge/bridge_wait_status.py scripts/regression/aggregate_wait_status.py scripts/regression/docs_contracts.py
```

## Safety And Privacy Requirements

- Do not include body previews from queued messages. `pending_inbound.body_chars`
  is safe; body content is not.
- Do not read bridge state files directly from the CLI. Keep using the daemon
  command path.
- Do not mutate queue, watchdog, aggregate, cursor, or delivery state.
- Do not encourage polling. Every output path should frame `--why` as a
  human-prompted diagnostic check.
- Do not advise `agent_wait_status --why` as the normal next step after every
  wait. Normal progress still arrives as `[bridge:*]` prompts.
- Do not expose other requesters' aggregate bodies or foreign watchdogs.
  Rely on the existing daemon response filters.

## Non-Goals

- No durable wait-state persistence.
- No change to watchdog/alarm semantics.
- No change to `agent_aggregate_status`.
- No automatic recovery action.
- No live peer pane capture; use `agent_view_peer` for that.
- No broad replacement of JSON output. Existing default JSON remains the stable
  machine/debug contract.

## Implementation Notes

- Keep the explanation renderer deterministic. Tests should not depend on
  wall-clock relative time. Prefer the absolute `deadline` strings already in
  the daemon response.
- The text output can be line-oriented and compact; avoid tables because rows
  have different meanings.
- The structured `why` JSON should be generated from the same model used by the
  text formatter so tests cover both outputs consistently.
- Start with CLI-only implementation. If future users want `why` data from the
  daemon socket, add an explicit daemon command later rather than overloading
  `wait_status` silently.
