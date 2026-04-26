# Agent Bridge

Local tmux-based bridge that lets Claude Code and Codex agents talk to each other in parallel — send requests, broadcast, view peer screens, with hook-driven turn boundaries and a queued message bus.

## What It Does

Multiple AI coding agents (Claude Code, Codex) running locally on the same machine can collaborate through a queued message bus:

- **Ask a peer to do work** with `agent_send_peer --to <alias> '...'`, or shorthand `agent_send_peer <alias> '...'`. The inline body must be one shell argument; use `--stdin` with a quoted heredoc for apostrophes, newlines, or option-like text. Put options before `--to`/`--all` or before an implicit leading alias. The sender continues independent work or ends its turn; when the peer replies, the bridge auto-routes the result back as the sender's next prompt. Both agents work in parallel — the sender does not block.
- **Broadcast or partial-broadcast** with `--all` or `--to <a>,<b>`. One merged result returns after every addressed peer has replied.
- **Time-bound a wait** with per-request watchdogs, self-scheduled alarms, and ESC-cancel interrupts.
- **Read a peer's terminal** via `agent_view_peer` — paginated snapshots, since-last delta, search — so a model can inspect what its peer is doing without asking the human to copy/paste output.

Each room is a background daemon attached to existing tmux panes (the bridge does not spawn the agents themselves). Multiple independent rooms run concurrently by session name.

## Requirements

- macOS or Linux (Windows via WSL); tmux 2.6+, Python 3.10+, Bash 4.0+
- Working Claude Code and/or Codex CLI install

## Install

```bash
./install.sh         # interactive
./install.sh --yes   # non-interactive, auto-append PATH
```

After install:

1. **Reload your shell** (`source ~/.bashrc`) or open a new terminal so the new shims are on `PATH`.
2. **Restart any running Claude / Codex sessions** so they pick up the new hooks.
3. **Verify**: `bridge_healthcheck`.

## Quickstart

```bash
# 1. Launch agents in tmux panes (bridge attaches; it does not spawn them)
tmux new -s work
# split + run `claude` in one pane, `codex` in another

# 2. Attach the bridge — interactive picker
bridge_run

# 3. From inside any agent pane, the model can now use:
#    agent_list_peers
#    agent_send_peer --to <alias> 'request'
#    agent_send_peer <alias> 'request'        # shorthand for --to <alias>
#    agent_send_peer --to <alias> --stdin <<'EOF'
#    request with apostrophes, newlines, or --option-like text
#    EOF
#    agent_view_peer <alias> --onboard
```

## Daily Use

- `bridge_run` — open the pane picker and attach a new room. Run with `--help` for flags (custom session name, etc.).
- `bridge_manage` — interactive: list active rooms, join/leave agents, tail daemon log, stop a daemon.

## Configuration

- `AGENT_BRIDGE_DEFAULT_WATCHDOG_SEC` — default request watchdog in seconds; `0` disables the default.
- `AGENT_BRIDGE_PANE_MODE_GRACE_SEC` — how long the daemon leaves a target message pending while the target tmux pane is in copy/view mode before force-cancelling that mode. Default is `180`. Set `0` to disable force-cancel and wait indefinitely; negative values also disable it and log a warning.
- `AGENT_BRIDGE_TURN_ID_MISMATCH_GRACE_SEC` — how long the daemon keeps a turn-id-mismatched active context before unblocking the target if the matching Stop hook never arrives. Default is `300`; `0` expires on the next maintenance sweep.
- `AGENT_BRIDGE_TURN_ID_MISMATCH_POST_WATCHDOG_GRACE_SEC` — extra seconds after a matching request or aggregate watchdog deadline before turn-id-mismatch expiry may unblock the target. Default is `1`.

If the daemon restarts while a target pane is in copy/view mode, avoid editing that pane's prompt buffer until the bridge submits or recovers the deferred message.

## Model Commands

These shell tools are installed on `PATH` for the agents themselves:

```text
agent_list_peers              show roster + cheat sheet
agent_send_peer               request / notice / broadcast / partial broadcast
agent_view_peer               read a peer's terminal (snapshot, since-last, search)
agent_alarm                   self-wake notice (cancelled by qualifying peer message)
agent_extend_wait             extend a watchdog after a wake
agent_interrupt_peer          ESC + cancel a peer's active turn
```

From inside an agent pane, `agent_list_peers` prints the full cheat sheet — exact flag semantics, watchdog/alarm/interrupt rules, large-payload guidance — so a model can self-recover the protocol without human help.

## Known Limitations

Current limitations and design caveats are tracked in [`docs/known-issues.md`](docs/known-issues.md).

## Uninstall

```bash
./uninstall.sh                  # remove shims + hook entries (default)
./uninstall.sh --dry-run        # preview only
./uninstall.sh --keep-hooks     # keep hooks, remove shims
./uninstall.sh --state          # also delete runtime state/log/run files
./uninstall.sh --repo           # also delete the repo/install root
```
