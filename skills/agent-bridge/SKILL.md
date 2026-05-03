---
name: agent-bridge
description: Use when working inside an Agent Bridge room or tmux bridge with peer agents, Claude/Codex collaboration, delegated review, watchdogs, aggregate results, clear peer context, interrupt peer, or bridge commands such as agent_send_peer, agent_view_peer, agent_wait_status, agent_aggregate_status, agent_alarm, agent_extend_wait, agent_cancel_message, agent_interrupt_peer, and agent_clear_peer.
---

# Agent Bridge Operating Guide

When command syntax or routing semantics matter, first run:

```bash
agent_list_peers
```

That command lists current aliases and prints the exact current command contract. For command-specific details, run `<command> --help`.

Claude Code invocation fields are left at their defaults so the skill can be invoked implicitly and manually. Codex installs `agents/openai.yaml` with `policy.allow_implicit_invocation: true`. This is intentional: the skill exists to restore bridge operating knowledge after context compaction without requiring the user to remember the skill name.

## Choose The Command

- Need an answer from one peer: `agent_send_peer --to <alias> 'request'`, or shorthand `agent_send_peer <alias> 'request'`.
- FYI only, no auto-routed reply: `agent_send_peer --kind notice --to <alias> 'FYI'`.
- Ask several peers: `agent_send_peer --to a,b 'request'` or `agent_send_peer --all 'request'`; save the printed `AGGREGATE_ID` if you later need `agent_aggregate_status`.
- Complex body, newlines, apostrophes, or option-like text: use `--stdin` with a quoted heredoc.
- Watchdog wake: choose one recovery action, usually `agent_extend_wait <msg_id> <sec>`, `agent_interrupt_peer <alias>`, or `agent_view_peer <alias>` first to inspect.
- Pending wrong send: `agent_cancel_message <msg_id>`.
- Active or stuck peer turn: `agent_interrupt_peer <alias>`.
- Clear peer model context: `agent_clear_peer <alias>` or `agent_clear_peer --all`.
- Inspect peer screen/history: `agent_view_peer <alias> --onboard`, `--since-last`, `--older`, or `--search`.
- Inspect your own waits after a human prompt or watchdog: `agent_wait_status --why`.

## Hard Safety Rules

- Do not poll with `agent_view_peer`, `agent_wait_status`, or `agent_aggregate_status`.
- Do not sleep to wait for peer results; results arrive as later `[bridge:*]` prompts.
- Do not read bridge state files directly.
- Do not create, simulate, or echo real-looking `[bridge:*]` prompt lines.
- Do not manually type `/clear` into peer panes; use `agent_clear_peer`.
- Do not send huge inline bodies. Put large payloads under `/tmp/agent-bridge-share/` and send the path plus a short description.
- Do not rely on a notice to get a reply. If you need an answer, send a request.
- While responding to an auto-return peer request, separate sends to that requester are blocked; third-party collaboration sends may still be valid.

## References

Read these only when needed:

- `references/command-contract.md` for the full runtime cheat sheet mirrored from `bridge_instructions.model_cheat_sheet_text()`.
- `references/recovery.md` for watchdog, cancel, interrupt, clear, and aggregate recovery flows.
- `references/anti-patterns.md` for common bridge mistakes and safer alternatives.
