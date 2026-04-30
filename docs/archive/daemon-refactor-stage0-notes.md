# Daemon Refactor Stage 0 Notes

Date: 2026-04-30

Stage 0 scope: no production code movement. This note records the regression
baseline, dirty-worktree check, and daemon function inventory before any
structural split work starts.

## Reviewer Roles

- `worker`: owns Stage 0 notes, validation, reviewer coordination, and the
  eventual Stage 1 handoff.
- `reviewer1`: primary reviewer and structure reviewer.
- `reviewer2`: concurrency reviewer and test reviewer.
- `manager`: not assigned a reviewer role.

## Dirty Worktree Check

Before creating this note, the worktree had no unrelated dirty changes:

```sh
git status --short
git diff --stat
git diff --name-status
```

Observed output was empty for all three commands.

## Baseline Validation

Scenario count:

```sh
python3 scripts/run_regressions.py --list | wc -l
```

Result:

```text
615
```

Daemon-focused baseline:

```sh
python3 scripts/run_regressions.py --match daemon --fail-fast
```

Result:

```text
48 passed, 0 failed
```

One environment-dependent skip line was observed during this filtered run:
`list_peers_json_daemon_status_strips_pid`, because there was no matching live
session. This was not a failure.

Full regression baseline:

```sh
python3 scripts/run_regressions.py
```

Result:

```text
615 passed, 0 failed
```

The full run also emitted the same live-session-dependent skip line for
`list_peers_json_daemon_status_strips_pid`; the final suite summary remained
green. No slow or flaky failures were observed.

## Function Inventory

Inventory command:

```sh
python3 - <<'PY'
import ast
from pathlib import Path
path = Path('libexec/agent-bridge/bridge_daemon.py')
mod = ast.parse(path.read_text())
for node in mod.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        print(f'{node.lineno}:{node.__class__.__name__}:{node.name}')
PY
```

Top-level inventory for `libexec/agent-bridge/bridge_daemon.py`:

```text
lines=7574
top_level_defs_or_classes=27
top_level_functions=22
top_level_classes=5
```

Top-level definitions:

```text
187:FunctionDef:_request_stop
192:FunctionDef:install_signal_handlers
197:ClassDef:BoundedSet
212:ClassDef:CommandLockWaitExceeded
218:ClassDef:PeerResultRedirectError
225:FunctionDef:one_line
229:FunctionDef:normalize_prompt_body_text
234:FunctionDef:prompt_body
241:FunctionDef:kind_expects_response
245:FunctionDef:_tmux_buffer_component
259:FunctionDef:tmux_prompt_buffer_name
276:FunctionDef:run_tmux_send_literal
311:FunctionDef:run_tmux_enter
315:FunctionDef:run_tmux_send_literal_touch_result
374:FunctionDef:probe_tmux_pane_mode
390:FunctionDef:cancel_tmux_pane_mode
405:FunctionDef:resolve_pane_mode_grace_seconds
420:FunctionDef:resolve_non_negative_env_seconds
433:FunctionDef:resolve_clear_post_clear_delay_seconds
455:FunctionDef:resolve_interrupt_key_delay_seconds
477:FunctionDef:resolve_claude_interrupt_keys
515:FunctionDef:pane_mode_block_since_ts
530:FunctionDef:build_peer_prompt
561:FunctionDef:make_message
596:ClassDef:QueueStore
609:ClassDef:BridgeDaemon
7543:FunctionDef:main
```

Class method counts:

```text
BoundedSet=2
CommandLockWaitExceeded=1
PeerResultRedirectError=1
QueueStore=3
BridgeDaemon=194
```

Current extraction landmarks for later stages:

```text
message/prompt helpers: one_line through kind_expects_response, lines 225-244;
  build_peer_prompt/make_message, lines 530-594
tmux helpers: _tmux_buffer_component through cancel_tmux_pane_mode, lines 245-404
env/config helpers: resolve_* and pane_mode_block_since_ts, lines 405-528
QueueStore: lines 596-608
BridgeDaemon facade/body: lines 609-7541
CLI entrypoint: main, line 7543
```

## Stage 0 Review Scope

Stage 0 review is a plan review only. Reviewers should inspect:

- `docs/daemon-refactor-plan.md`
- `docs/review-process.md`
- this Stage 0 note
- validation commands and outputs recorded above
- the current daemon inventory and whether it is sufficient for Stage 1

Expected focus by role:

- Structure reviewer: stage boundaries, proposed module layout, compatibility
  wrappers/re-exports, and whether Stage 1 is small enough.
- Concurrency reviewer: global invariants, lock-split sequencing, and whether
  early stages correctly preserve the single `state_lock`.
- Test reviewer: baseline commands, missing coverage, skipped/flaky behavior,
  and Stage 1 validation selection.

Stage 1 must not start until the primary reviewer delivers a consolidated
Stage 0 plan-review notice to `worker`.
