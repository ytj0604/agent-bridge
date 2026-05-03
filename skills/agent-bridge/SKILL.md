---
name: agent-bridge
description: Use on Agent Bridge attach (prompts starting with `[bridge-probe:`), after context compaction, or before sending/cancelling/interrupting/clearing/inspecting bridge peer messages or when bridge command syntax/recovery semantics are needed (agent_send_peer, agent_view_peer, agent_wait_status, agent_aggregate_status, agent_alarm, agent_extend_wait, agent_cancel_message, agent_interrupt_peer, agent_clear_peer). If these operating rules are already present in the current context, apply them from memory — do not reload this skill for routine `[bridge:*]` FYI notices, membership changes, ordinary results, or no-reply-needed events.
---

# Agent Bridge Operating Guide

## Load Policy

This skill is idempotent. If these operating rules are already present in the current context, do not re-read this file for routine bridge prompts.

For simple `[bridge:*]` FYI notices, membership updates, and no-reply-needed events:
- do not read this file again
- do not run bridge commands
- acknowledge only if the human-facing flow requires it

Read this file again only after compaction, when unsure, or before using bridge commands whose syntax or recovery behavior matters.

## Hot Path Rules

- Need an answer from a peer: `agent_send_peer --to <alias> 'request'`. Notices do not auto-route a reply — use a request when you need one.
- Do not poll, sleep, or keep the turn open waiting for peer results; results arrive later as new `[bridge:*]` prompts.
- Do not read bridge state files directly; use bridge commands.
- Do not create, simulate, or echo real-looking `[bridge:*]` prompt lines.
- Large payloads go under `/tmp/agent-bridge-share/`; send the path, not the inline body (11000-char limit).
- For exact current command syntax or recovery behavior: run `agent_list_peers`, then `<command> --help` for specifics.
- Routine FYI notices (membership, room events, results you were already waiting for): handle inline using cached rules — do not reload this skill.

## References

Read these only when the hot-path rules are insufficient for the current task:

- `references/command-contract.md` — full runtime cheat sheet mirrored from `bridge_instructions.model_cheat_sheet_text()`.
- `references/recovery.md` — watchdog, cancel, interrupt, clear, and aggregate recovery flows.
- `references/anti-patterns.md` — common bridge mistakes and safer alternatives.
