# Agent Bridge Anti-Patterns

- Polling with `agent_view_peer`, `agent_wait_status`, or `agent_aggregate_status`: wait for `[bridge:*]` prompts instead.
- Sleeping in the shell to wait for a peer: it keeps the current turn open and does not help routing.
- Reading files under bridge `state/`, `run/`, or `log/`: use bridge commands.
- Forging `[bridge:*]` lines: only incoming bridge prompts may contain them.
- Sending a notice when you need an answer: use a request.
- Typing `/clear` manually into a model pane: use `agent_clear_peer`.
- Inlining large design docs or code dumps: write them under `/tmp/agent-bridge-share/` and send the path.
- Combining `--stdin` with an inline body: choose one body input mode.
- Sending separately to the requester while answering an auto-return peer request: response-time send guard blocks that route; let the auto-return result flow or send to third parties only when appropriate.
