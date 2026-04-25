# Agent Bridge

Local tmux-based bridge that lets Claude Code and Codex agents talk to each other in parallel — send requests, broadcast, view peer screens, with hook-driven turn boundaries and a queued message bus.

## What It Does

- Keeps Claude/Codex agents visible in separate tmux panes.
- Logs redacted lifecycle events to the bridge state directory.
- Injects peer messages with visible `[bridge:<nonce>]` markers.
- Uses hooks for turn boundaries instead of scraping full terminal output.
- Supports true parallel dispatch through `agent_send_peer`: the requester keeps working while the peer runs, and the peer result is queued until the requester is idle.
- Supports one-to-one rooms and multi-agent rooms by alias.
- Supports multiple independent rooms by session name.
- Lazily restarts a stale room daemon from `state/<session>/session.json` when a model runs `agent_send_peer`, `agent_list_peers`, or `agent_view_peer`.

## Requirements

- **OS**: macOS or Linux (Windows via WSL only; tmux is required)
- **Python**: 3.10 or newer (uses PEP 604 `X | None` syntax)
- **tmux**: 2.6 or newer
- **Bash**: 4.0 or newer
- **ps** (macOS BSD ps or Linux procps) on `PATH`
- A working Claude Code and/or Codex CLI install

## Install

From the repo checkout:

```bash
./install.sh
```

Or install non-interactively and auto-append the PATH line to `~/.bashrc`:

```bash
./install.sh --yes
```

The installer creates stable shims in `${XDG_BIN_HOME:-~/.local/bin}`:

```text
bridge_run
bridge_manage
bridge_healthcheck
agent_send_peer
agent_list_peers
agent_view_peer
```

Directory roles are intentionally separated:

```text
bin/                  human-facing CLI wrappers only
model-bin/            model-facing tools: agent_send_peer, agent_list_peers, agent_view_peer
hooks/                Claude/Codex hook entrypoints
libexec/agent-bridge/ internal implementation files
```

### Post-install steps (important)

After running `install.sh`, three things must happen before the bridge works in an existing terminal:

1. **Reload your shell** so the new shims are on `PATH`. Either open a new terminal, or:

   ```bash
   source ~/.bashrc     # or: hash -r   (if PATH already contained the bin dir)
   ```

2. **Restart any already-running Claude Code / Codex sessions.** They cache hook config at startup and will not pick up the newly installed hooks until they restart. tmux itself can stay; only `/exit` the agents running inside their panes and relaunch.

3. **Verify the install:**

   ```bash
   bridge_healthcheck
   ```

If `bridge_healthcheck` reports a missing shim or a hook that is not wired up, follow the hint it prints. The most common issue is `~/.local/bin` not being on `PATH` — add the line the installer suggests to `~/.bashrc` or use `./install.sh --yes`.

## Quickstart

```bash
# 1. Install (once)
./install.sh --yes
source ~/.bashrc
bridge_healthcheck

# 2. Launch agents in tmux panes (bridge attaches, it does not spawn them)
tmux new -s work
# split the window, run `claude` in one pane and `codex` in another

# 3. Attach the bridge
bridge_run
# interactive picker: select the panes, pick aliases, done.

# 4. From inside a Claude/Codex pane, the model can now use:
#    agent_list_peers
#    agent_send_peer --to <alias> 'request'
#    agent_send_peer --all 'broadcast'
#    agent_view_peer <alias> --onboard
```

## Daily Use

Main entrypoint:

```bash
bridge_run
```

`bridge_run` immediately opens the tmux pane picker. Start Claude/Codex yourself first, then select the panes to attach to a bridge room.

Use a non-default bridge room name:

```bash
bridge_run --session my-room
```

Manage rooms, agents, and daemon stop:

```bash
bridge_manage
```

`bridge_manage` shows active bridge rooms first. After selecting a room, it offers join agent, leave agent, show daemon status, tail daemon log, stop daemon, and switch room.

## Model-Facing Commands

Models communicate through these shell tools exposed on `PATH`:

```bash
agent_list_peers
agent_send_peer --to <alias> 'message'
agent_send_peer --all 'message'
agent_send_peer --kind notice --to <alias> 'FYI'
agent_send_peer [--to <alias>|--all] --watchdog <sec> 'request'
agent_view_peer <alias> --onboard [--tail 50]
agent_view_peer <alias> --older
agent_view_peer <alias> --since-last
agent_view_peer <alias> --search 'text' [--live]
agent_alarm <sec> [--note 'text']
agent_extend_wait <message_id> <sec>
agent_interrupt_peer <alias>
agent_interrupt_peer <alias> --clear-hold
agent_interrupt_peer [<alias>] --status
```

If exactly two participants are active, the shorter form is valid:

```bash
agent_send_peer 'Review this.'
```

For rooms with three or more participants, the target must be explicit (`--to alias` or `--all`). Models do not need to pass `--session` or `--from`; those are resolved from the caller tmux pane via the pane-lock registry.

`agent_list_peers` prints the current roster and a compact command cheat sheet. **If a model forgets the protocol at any point, it should just run `agent_list_peers`.**

`agent_view_peer` lets a model read a peer's tmux pane without asking the human to copy terminal text:

- `--onboard` — capture a stable snapshot and show the latest page
- `--older` — page older lines from that snapshot
- `--since-last` — only the screen output added since this caller last viewed the target
- `--search 'text'` — search inside the saved snapshot
- `--search 'text' --live` — search the current live scrollback instead of the snapshot
- `--tail N` — show N lines for page-style modes (default 50)

When a participant joins or leaves, the daemon sends a `membership_update` notice to the remaining participants.

## Routing Semantics

- `kind=request`: asks the peer to do work; the peer's next normal response is returned to the requester once via auto-routing.
- `kind=result`: delivers a result back to a previous requester; the recipient's response is **not** returned again.
- `kind=notice`: sends information **with no return route**. Even if the peer chooses to write a response, the bridge will not deliver it back to the sender — only `agent_view_peer` can observe it. Body text saying "please reply" does not change this; the kind on the wire decides routing. Use `request` when you actually need an answer.
- `agent_send_peer '...'` sends `kind=request` by default.
- `agent_send_peer --all '...'` sends one request to each peer and returns one aggregated result after every peer replies.
- When answering a peer request, reply normally — the bridge returns that normal response automatically.
- Relay depth is capped (default `max_hops=4`) to prevent runaway chains.

## Watchdogs, Alarms, and Interrupt

### Watchdogs (request only)

- `agent_send_peer --to <alias> --watchdog <sec> 'request body'` adds a watchdog. Counted from the moment the prompt is **delivered** to the peer (the `prompt_submitted` hook fires), not from enqueue. If the peer has not produced a `response_finished` after `<sec>` seconds since delivery, the bridge sends a `[bridge:watchdog]` notice back to the sender.
- `kind=request` (the default) gets a default 5-minute watchdog automatically. Override the default via the env `AGENT_BRIDGE_DEFAULT_WATCHDOG_SEC` (set 0 or a non-positive value to disable the default globally). On a per-call basis, `--watchdog 0` explicitly disables the watchdog for that one request.
- `--watchdog` is rejected for `kind=notice` (and any other non-request). Notices do not have a return route, so a watchdog watching for "no reply" is meaningless. Use `agent_alarm` instead.
- The watchdog is automatically cancelled when the peer's reply is auto-routed back (single request) or when the aggregate completes (`--all`). It fires only if the peer takes too long.
- The wake notice tells the sender exactly which actions are appropriate:
  - `agent_extend_wait <msg_id> <sec>` to keep waiting on the same request,
  - `agent_interrupt_peer <alias>` to cancel and stop the peer,
  - `agent_view_peer <alias>` to first inspect what the peer is doing.
- The wake notice also describes the message status: if the request never reached the peer (still pending or inflight, e.g. the peer is in `held_interrupt`), the wake notice flags that and recommends `agent_interrupt_peer --status` / `--clear-hold` instead of `agent_extend_wait`.
- `agent_extend_wait <message_id> <sec>` re-arms the watchdog for the same request, counted from now + `<sec>`. Only the original sender can extend; aggregate-member messages are not supported in v1.5 (`aggregate_extend_not_supported`). Watchdog fires are one-shot — without an explicit `agent_extend_wait`, you will not be reminded again about that request.

### Alarms (self-wake; cancelled by qualifying incoming messages)

- `agent_alarm <sec> --note '<desc>'` schedules a self-addressed wake notice. End your turn after setting it; the bridge will deliver the notice when the deadline elapses, **unless** the alarm is cancelled first.
- An alarm is cancelled the moment a "qualifying" peer message is enqueued for the alarm owner. Qualifying = `kind != "result"`, `from != alarm_owner`, `from != "bridge"`. So peer-initiated `request` and `notice` messages from another agent both cancel; system-internal `result` (auto-returned replies) and bridge-synthetic notices (watchdog wakes, interrupt notices, alarm notices) do **not** cancel.
- When an alarm is cancelled by an incoming message, that triggering message is delivered with a short `[bridge:alarm_cancelled]` notice **prepended** to its body so you cannot miss it under truncation. The notice tells you to re-arm with `agent_alarm` if the message is not what you were waiting for.
- Alarms are best for "I delegated something via notice and want a safety wake if no follow-up arrives". They are **not** the right tool for waiting on `auto_return` reply results — use `--watchdog` and `agent_extend_wait` for that.

### Interrupt and held_interrupt

- `agent_interrupt_peer <alias>` sends ESC to the peer's pane and **cancels** the active in-flight message (the message is removed; pending messages stay queued). The peer is placed in a `held_interrupt` state and stops receiving new deliveries until either:
  - the next `response_finished` event from that peer arrives (the bridge interprets it as the drain Stop and auto-releases the hold without routing the response), or
  - you run `agent_interrupt_peer <alias> --clear-hold` to force the release manually.
  If `tmux send-keys` fails (no pane / tmux error), the bridge fail-closes — no state changes.
- `agent_interrupt_peer <alias> --clear-hold` is **unsafe** if the peer might still be running, because forcing release before the drain Stop arrives can cause the late response to be misrouted to the next prompt. Always inspect with `--status` first.
- `agent_interrupt_peer [<alias>] --status` prints `busy`, `held`, `current_prompt_id`, and `delivered` / `pending` queue counts for the target (or all peers). Use it before clearing a hold or to debug delivery stalls.

### Large payloads

For design docs, code dumps, long plans, or any body longer than a few hundred characters: write to a shared path (e.g. `/tmp/agent-bridge-share/...`) and send only the path plus a brief description. Peers run on the same host and can Read the file directly. Inlining large bodies forces `tmux send-keys` to inject every character into the peer's pane, which is slow and can trip paste-burst submit.

### Limits

- `held_interrupt`, watchdogs, and alarms are kept in daemon memory only and do not survive a daemon restart. After restart, run `agent_interrupt_peer --status` to inspect for `delivered` queue items that may need manual intervention.
- The interrupt design relies on the next `response_finished` from the held peer to release the hold. If the peer never produces one (e.g. it ignored ESC), the hold is preserved indefinitely — manual `--clear-hold` is required, with the routing risk noted above.
- Watchdogs are armed at delivery time. If the message never delivers (e.g. peer permanently held or unreachable), the watchdog also never arms. Use `agent_interrupt_peer --status` to spot stuck `pending`/`inflight` items.
- The file-write fallback in `agent_send_peer` (used when the daemon socket is unreachable, e.g. from a sandboxed peer) writes the message in a transient `status="ingressing"` state. The daemon's main loop detects the new `message_queued` event and runs the same finalize step as the socket path — alarm cancel + body prepend, then promote to `status="pending"`. So both ingress paths reach the same end state. If the daemon is **down** at the time of the fallback write, the message stays as `ingressing` until daemon startup recovery promotes it (alarm cancel is skipped in that recovery, since alarms are in-memory). The CLI prints a stderr warning on every fallback so the operator notices.

## Known Issues

See [`docs/known-issues.md`](docs/known-issues.md) for the current list of v1.5 limitations and design caveats. Notable:
- **I-01** (open): claude `response_finished` auto-routes indefinitely until the next `UserPromptSubmit` resets the daemon's per-agent prompt slot. A peer request can absorb multiple unrelated claude turns. Fix planned for v2 (consume-once auto-route).
- **I-02** (mitigated): codex sandbox cannot connect to the daemon socket; fallback file-write path now applies the same alarm-cancel semantics under a transient `status="ingressing"`.
- **I-03** (open): daemon in-memory state (`held_interrupt`, watchdogs, alarms) is lost on restart. Persist to disk in a future revision.

## Sender Identity and Spoofing

`agent_send_peer` resolves `session` and `from` from the caller's tmux pane via the pane-lock registry. Passing `--from` or `--session` that does not match the caller pane is rejected unless `--allow-spoof` is set (reserved for admin/test use).

## Runtime Files

Default runtime files live under a writable runtime root, not under the install directory. By default the runtime root is:

```text
${TMPDIR:-/tmp}/agent-bridge-$uid/
```

This is intentional: Codex/Claude tool sandboxes often see the install directory as read-only, while `/tmp` is commonly bind-mounted writable. You can override the runtime root explicitly with `AGENT_BRIDGE_RUNTIME_DIR`, or override individual roots with `AGENT_BRIDGE_STATE_DIR`, `AGENT_BRIDGE_RUN_DIR`, and `AGENT_BRIDGE_LOG_DIR`.

The runtime layout is:

```text
<runtime>/state/<session>/events.jsonl
<runtime>/state/<session>/events.raw.jsonl
<runtime>/state/<session>/pending.json
<runtime>/state/<session>/captures/snapshots/<alias>/<id>.{txt,json}
<runtime>/state/<session>/captures/cursors/<caller>@<target>.json
<runtime>/run/<session>.pid
<runtime>/run/<session>.json
<runtime>/run/<session>.stop
<runtime>/run/<session>.sock
<runtime>/log/<session>.daemon.log
```

Install-root runtime directories such as `<install>/state` are ignored by default because they are not sandbox-safe. Set `AGENT_BRIDGE_ALLOW_INSTALL_ROOT_RUNTIME=1` only for a controlled local setup where every agent can write that path.

Stopping a room from `bridge_manage` first writes the `.stop` sentinel so the daemon exits cooperatively. If that does not complete before the timeout, it falls back to `SIGTERM`/`SIGKILL`. Successful stop also notifies active panes that the room is closed, cleans active registry/pane-lock records, and moves per-room state under `state/.forgotten/` for diagnostics.

If the daemon dies without an intentional stop, model-facing commands call `bridge_daemon_ctl ensure` before reading or writing the queue. If the room state still exists and the room was not created with `--no-daemon`, the daemon is restarted in the background. Set `AGENT_BRIDGE_AUTO_RESTART=0` to disable this lazy restart behavior.

Snapshot files are pruned automatically when a room's total snapshot size exceeds 100 MB. Snapshots currently referenced by a cursor are never evicted.

## Rename Safety

Runtime code does not hardcode the install directory name. It resolves paths from the executed script location, with optional overrides:

```bash
AGENT_BRIDGE_HOME=/path/to/install
AGENT_BRIDGE_STATE_DIR=/path/to/state
AGENT_BRIDGE_RUN_DIR=/path/to/run
AGENT_BRIDGE_LOG_DIR=/path/to/log
AGENT_BRIDGE_PYTHON=python3
```

Hooks should point at the hook entrypoint, not the human CLI bin:

```bash
~/.agent-bridge/hooks/bridge-hook --agent claude
~/.agent-bridge/hooks/bridge-hook --agent codex
```

That keeps Claude/Codex config independent of the repo directory name.

## Uninstall

Remove installed shims and Claude/Codex hook entries:

```bash
./uninstall.sh
```

Preview first:

```bash
./uninstall.sh --dry-run
```

Keep hook config but remove shims:

```bash
./uninstall.sh --keep-hooks
```

Also delete runtime state/log/run files:

```bash
./uninstall.sh --state
```

Also delete the repo/install root:

```bash
./uninstall.sh --repo
```
