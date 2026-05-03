# Agent Bridge Recovery

First recovery rule: run `agent_list_peers` if command syntax or routing semantics are unclear.

## Watchdog Wake

A watchdog is a wake-up, not a progress poll. Choose one action:

- `agent_extend_wait <message_id> <sec>` when the original request is still valid and you want to keep waiting.
- `agent_view_peer <alias>` when you need to inspect whether the peer is stuck, in a prompt, or actively working.
- `agent_interrupt_peer <alias>` when the peer is on the wrong prompt, stuck in active work, or blocking a replacement.

Do not repeatedly call status or view commands to watch progress. Wait for the later `[bridge:*]` result prompt.

## Cancel Vs Interrupt

Use `agent_cancel_message <message_id>` only before the peer has active work: pending, inflight pre-pane-touch/pre-paste, or pane-mode-deferred delivery.

Use `agent_interrupt_peer <alias>` for active/post-pane-touch work: inflight after pane touch, submitted, delivered, or responding active turns.

## Clear

Use `agent_clear_peer <alias>` or `agent_clear_peer --all` for controlled model context clears. `--force` is single-target only. Self-clear is deferred until your current turn ends.

## Aggregate Results

Broadcast and partial-broadcast sends print `AGGREGATE_ID: agg-...`. The normal result arrives later as one `[bridge:*]` prompt after all peers reply. Use `agent_aggregate_status <aggregate_id>` only after a human prompt, watchdog wake, or suspected bridge issue.

## Oversized Results

If a result arrives as `[bridge:body_redirected]` with a `File:` path, read that file. The preview is intentionally truncated.
