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

Models communicate through three shell tools exposed on `PATH`:

```bash
agent_list_peers
agent_send_peer --to <alias> 'message'
agent_send_peer --all 'message'
agent_send_peer --kind notice --to <alias> 'FYI'
agent_view_peer <alias> --onboard
agent_view_peer <alias> --older
agent_view_peer <alias> --since-last
agent_view_peer <alias> --search 'text' [--live]
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

When a participant joins or leaves, the daemon sends a `membership_update` notice to the remaining participants.

## Routing Semantics

- `kind=request`: asks the peer to do work; the peer's next normal response is returned to the requester once.
- `kind=result`: delivers a result back to a previous requester; the recipient's response is **not** returned again.
- `kind=notice`: sends information without expecting a response.
- `agent_send_peer '...'` sends `kind=request` by default.
- When answering a peer request, reply normally — the bridge returns that normal response automatically.
- Relay depth is capped (default `max_hops=4`) to prevent runaway chains.

## Sender Identity and Spoofing

`agent_send_peer` resolves `session` and `from` from the caller's tmux pane via the pane-lock registry. Passing `--from` or `--session` that does not match the caller pane is rejected unless `--allow-spoof` is set (reserved for admin/test use).

## Runtime Files

Default runtime files live under the resolved install root unless overridden:

```text
state/<session>/events.jsonl
state/<session>/events.raw.jsonl
state/<session>/pending.json
state/<session>/captures/snapshots/<alias>/<id>.{txt,json}
state/<session>/captures/cursors/<caller>@<target>.json
run/<session>.pid
run/<session>.json
run/<session>.stop
log/<session>.daemon.log
```

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
