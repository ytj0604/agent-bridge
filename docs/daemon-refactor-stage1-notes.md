# Daemon Refactor Stage 1 Notes

Date: 2026-04-30

Stage 1 scope: extract pure prompt/body/message helpers and tmux adapters
without moving daemon state, delivery transitions, QueueStore, clear flow,
interrupt flow, watchdogs, or lock usage.

## Reviewer Roles

- `worker`: owns the Stage 1 diff, validation, reviewer coordination, and
  eventual commit.
- `reviewer1`: primary reviewer and structure reviewer.
- `reviewer2`: test reviewer, with concurrency attention on tmux interleaving
  and lock-scope preservation.
- `manager`: not assigned a reviewer role.

## Files Changed

- `libexec/agent-bridge/bridge_daemon.py`
- `libexec/agent-bridge/bridge_daemon_messages.py`
- `libexec/agent-bridge/bridge_daemon_tmux.py`
- `docs/daemon-refactor-stage1-notes.md`

## Extracted Message Helpers

Moved to `bridge_daemon_messages.py`:

```text
PROMPT_BODY_CONTROL_TRANSLATION
one_line
normalize_prompt_body_text
prompt_body
kind_expects_response
build_peer_prompt
make_message
```

`bridge_daemon_messages.py` imports `MAX_PEER_BODY_CHARS`, `normalize_kind`,
`short_id`, and `utc_now` directly from `bridge_util`, preserving the previous
helper API and call-time behavior.

## Extracted Tmux Helpers

Moved to `bridge_daemon_tmux.py`:

```text
PANE_MODE_PROBE_TIMEOUT_SECONDS
TMUX_SEND_TIMEOUT_SECONDS
_tmux_buffer_component
tmux_prompt_buffer_name
run_tmux_send_literal
run_tmux_enter
run_tmux_send_literal_touch_result
probe_tmux_pane_mode
cancel_tmux_pane_mode
```

The tmux subprocess command arguments, timeout values, stdout/stderr/text
handling, cleanup behavior, and return dict/tuple shapes were preserved.

## Compatibility Surface

`bridge_daemon.py` imports and re-exports the moved helper names. Existing
`BridgeDaemon` methods still resolve helper calls through `bridge_daemon.py`
module globals, so tests or external callers that monkeypatch names such as
`bridge_daemon.run_tmux_send_literal` and `bridge_daemon.run_tmux_enter` keep
the same interception point.

Compatibility names confirmed importable from `bridge_daemon.py`:

```text
build_peer_prompt
make_message
prompt_body
normalize_prompt_body_text
kind_expects_response
tmux_prompt_buffer_name
run_tmux_send_literal
run_tmux_enter
run_tmux_send_literal_touch_result
probe_tmux_pane_mode
cancel_tmux_pane_mode
TMUX_SEND_TIMEOUT_SECONDS
PANE_MODE_PROBE_TIMEOUT_SECONDS
```

After code review, the duplicate local definitions of `build_peer_prompt` and
`make_message` were removed from `bridge_daemon.py`. The facade bindings were
confirmed to be the extracted helper objects:

```sh
PYTHONPATH=libexec/agent-bridge python3 - <<'PY'
import bridge_daemon
import bridge_daemon_messages
print(bridge_daemon.build_peer_prompt is bridge_daemon_messages.build_peer_prompt)
print(bridge_daemon.make_message is bridge_daemon_messages.make_message)
PY
```

Result:

```text
True
True
```

## Explicit Non-Changes

- No daemon state fields moved.
- No `QueueStore` behavior changed.
- No delivery state transitions moved.
- No watchdog, interrupt, clear, aggregate, or response routing behavior moved.
- No `state_lock` scope or lock ordering changed.
- `resolve_*` config helpers and `pane_mode_block_since_ts` remain in
  `bridge_daemon.py`.

## Validation

Syntax/import check:

```sh
python3 -m py_compile \
  libexec/agent-bridge/bridge_daemon.py \
  libexec/agent-bridge/bridge_daemon_messages.py \
  libexec/agent-bridge/bridge_daemon_tmux.py
```

Result: passed.

Focused Stage 1 suites:

```sh
python3 scripts/run_regressions.py --match prompt --fail-fast
python3 scripts/run_regressions.py --match redirect --fail-fast
python3 scripts/run_regressions.py --match pane_mode --fail-fast
python3 scripts/run_regressions.py --match interrupt --fail-fast
```

Results:

```text
prompt: 23 passed, 0 failed
redirect: 10 passed, 0 failed
pane_mode: 13 passed, 0 failed
interrupt: 49 passed, 0 failed
```

Full suite, required because public helper re-exports changed:

```sh
python3 scripts/run_regressions.py
```

Result:

```text
615 passed, 0 failed
```

The full run emitted the known live-session-dependent skip line for
`list_peers_json_daemon_status_strips_pid`; the final summary remained green.

Named compatibility checks observed in the focused/full validation output:

```text
prompt_body_preserves_multiline_and_sanitizes
build_peer_prompt_signature_drops_max_hops
tmux_paste_buffer_delivery_sequence
pane_mode_pending_defers_without_attempt
pane_mode_clears_then_delivers
retry_enter_skips_pane_mode
```
