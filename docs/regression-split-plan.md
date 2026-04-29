# Regression Suite Split Plan

This document describes how to split `scripts/regression_interrupt.py` into
smaller regression modules without changing test behavior. The current suite is
still runnable in about 20-25 seconds, so the priority is maintainability and
reviewability, not parallel execution.

## Agent Roles

- `worker`: the single writer. Owns file moves, import cleanup, runner changes,
  and validation commands. This role should stay single-owner for each batch.
- `structure reviewer`: read-only reviewer for scenario coverage, registry
  order, imports, helper placement, and module boundaries.
- `isolation reviewer`: read-only reviewer for test behavior risks: environment
  restoration, `sys.argv` and `sys.stdin` restoration, subprocess stubs, tmpdir
  usage, threads, sockets, and monkeypatch cleanup.

Only one agent should edit the split files at a time. Review agents should be
asked for findings, not patches, unless the worker explicitly hands off a
disjoint file set.

## Invariants

- `python3 scripts/regression_interrupt.py` must keep working throughout.
- Scenario count must remain unchanged unless a test is intentionally added or
  removed in a separate change.
- Scenario order must remain unchanged. The suite currently depends on a single
  ordered list in `main()`.
- Do not introduce thread-based parallel execution. The tests mutate process
  globals such as `os.environ`, `sys.argv`, `sys.stdin`, module functions, and
  daemon constants. If parallel execution is added later, use process isolation.
- Each moved batch should pass a narrow `--match` run before broader validation.
- Run the full suite before and after high-risk moves.

## Target Layout

```text
scripts/regression_interrupt.py          # legacy wrapper
scripts/regression/
  __init__.py
  runner.py                              # argparse, run loop, output
  registry.py                            # ordered scenario list
  harness.py                             # shared fixtures/helpers
  daemon_core.py
  interrupt.py
  watchdog_alarm.py
  aggregate_wait_status.py
  cancel_message.py
  pane_mode_delivery.py
  clear_peer.py
  join_leave_attach.py
  install_uninstall.py
  hook_config.py
  view_peer.py
  identity_recovery.py
  send_enqueue.py
  docs_contracts.py
```

Keep `scripts/regression_interrupt.py` as the stable entrypoint. A final wrapper
should look like:

```python
#!/usr/bin/env python3
from regression.runner import main

if __name__ == "__main__":
    raise SystemExit(main())
```

## Phase 0: Baseline

1. Run the full suite:

   ```sh
   python3 scripts/regression_interrupt.py
   ```

2. Record the pass count and elapsed time in working notes. The expected current
   baseline is approximately:

   ```text
   615 passed, 0 failed
   ```

3. Inventory scenario functions with an AST or `rg` check. Capture:
   - total `scenario_` function count
   - ordered scenario tuple count
   - current largest helper clusters

4. Ask a `structure reviewer` for a read-only preflight review:

   ```text
   Review scripts/regression_interrupt.py read-only.
   Identify risky shared helpers, global monkeypatches, environment mutation,
   and scenario groups that should move late. Do not edit files.
   ```

## Phase 1: Extract Runner Without Moving Scenarios

Goal: make execution structure modular while scenario bodies remain in the
legacy file.

1. Add `scripts/regression/__init__.py`.
2. Add `scripts/regression/runner.py` with:
   - `main()`
   - argparse
   - scenario run loop
   - pass/fail/error summary
   - `--keep-tmp`
   - `--list`
   - `--match <pattern>`
   - `--fail-fast`
   - optional `--quiet`
3. Add `scripts/regression/registry.py`.
   - Initially it may import the legacy module or receive the current scenario
     list from it.
   - Preserve exact order.
4. Keep the legacy entrypoint working.
5. Validate:

   ```sh
   python3 scripts/regression_interrupt.py --list
   python3 scripts/regression_interrupt.py --match interrupt --fail-fast
   python3 scripts/regression_interrupt.py
   ```

6. Ask a `structure reviewer` to check:
   - scenario count
   - scenario order
   - wrapper compatibility
   - option behavior

## Phase 2: Extract Shared Harness

Goal: move common utilities before moving scenario bodies.

Move broadly shared items to `scripts/regression/harness.py`:

- `ROOT`, `LIBEXEC`
- executable target constants
- shared imports used by many scenario modules
- `make_daemon`
- `read_events`
- `assert_true`
- `FakeCommandConn`
- `patched_environ`
- `patched_redirect_root`
- `test_message`
- identity/runtime isolation helpers
- generic CLI capture helpers used by more than one domain

Rules:

- Do not move domain-specific helpers just because they are nearby.
- Keep helper imports explicit enough that moved modules remain easy to read.
- After helper extraction, run the full suite before moving scenario bodies.

Validation:

```sh
python3 scripts/regression_interrupt.py --match lifecycle
python3 scripts/regression_interrupt.py --match send_peer
python3 scripts/regression_interrupt.py
```

Ask an `isolation reviewer` to review read-only with this scope:

```text
Review harness extraction only.
Focus on env restoration, sys.argv/sys.stdin restoration, subprocess stubs,
module monkeypatch restoration, and tmpdir isolation. Do not edit files.
```

## Phase 3: Move Low-Risk Domains

Move scenarios in domain batches, not function-by-function. For each batch:

1. Create the target module.
2. Move scenario functions and tightly coupled local helpers.
3. Add `SCENARIOS = [(label, function), ...]` in that module.
4. Update `registry.py` to concatenate module `SCENARIOS` in the original order.
5. Run a narrow `--match` validation.
6. Run the full suite after every two or three batches.

Recommended order:

1. `install_uninstall.py`
   - `scenario_install_*`
   - `scenario_uninstall_*`
   - executable and healthcheck helper tests
2. `hook_config.py`
   - `scenario_bridge_install_hooks_*`
   - `scenario_codex_config_*`
3. `view_peer.py`
   - `scenario_view_peer_*`
   - view rendering/search/since-last helpers
4. `docs_contracts.py`
   - help text, cheat sheet, and documentation wording scenarios
5. `send_enqueue.py`
   - `scenario_send_peer_*`
   - `scenario_enqueue_*`

Use this review prompt after each batch:

```text
Review read-only.
Changed scope: scripts/regression/<module>.py, registry.py, harness.py.
Check for missing scenarios, registry order changes, import drift,
over-broad helper moves, under-broad helper moves, and isolation regressions.
Do not edit files.
```

## Phase 4: Move Medium-Risk Domains

These groups are more daemon-adjacent but still bounded. Run the full suite
after every one or two modules.

Recommended order:

1. `join_leave_attach.py`
   - join, leave, attach, daemon control CLI scenarios
2. `aggregate_wait_status.py`
   - aggregate status and wait status scenarios
3. `watchdog_alarm.py`
   - watchdog, alarm, and extend-wait scenarios
4. `cancel_message.py`
   - cancel-message scenarios
5. `pane_mode_delivery.py`
   - pane mode, retry-enter, and delivery deferral scenarios
6. `daemon_core.py`
   - lifecycle, queue, prompt/response, startup, and recovery scenarios

Validation pattern:

```sh
python3 scripts/regression_interrupt.py --match <domain-token> --fail-fast
python3 scripts/regression_interrupt.py
```

## Phase 5: Move High-Risk Domains

Move these last. They have the most shared daemon state, monkeypatching, thread
coordination, and identity-store interaction.

Recommended order:

1. `interrupt.py`
2. `identity_recovery.py`
3. `clear_peer.py`

Extra rules:

- Run the full suite before and after each module.
- Ask both a `structure reviewer` and an `isolation reviewer` to review.
- Do not combine these moves with refactors.
- Preserve helper locality where possible. If a helper is used only by
  `clear_peer.py`, keep it in `clear_peer.py`.

## Phase 6: Cleanup

1. Remove unused imports from split modules.
2. Confirm the legacy file is only an entrypoint wrapper.
3. Confirm scenario count and ordered labels match the baseline.
4. Update docs only if a new preferred command is added. Keep the legacy command
   documented.
5. Final validation:

   ```sh
   python3 scripts/regression_interrupt.py --list
   python3 scripts/regression_interrupt.py --match clear --fail-fast
   python3 scripts/regression_interrupt.py
   ```

6. Run install or health checks only if behavior outside tests changed:

   ```sh
   ./install.sh --dry-run
   bin/bridge_healthcheck.sh
   ```

## Optional Later Work: Process Parallelism

Do not add this during the split unless the suite becomes slow enough to justify
the extra moving parts. If needed later:

- Add `--jobs N` to `runner.py`.
- Use process-based execution, not threads.
- Capture each scenario's stdout/stderr in the child process and replay from the
  parent in deterministic order.
- Keep default `--jobs 1` until all modules are proven isolated.
- Start by allowing only explicitly safe modules to run in parallel.

## Acceptance Criteria

- `python3 scripts/regression_interrupt.py` passes.
- Scenario count is preserved.
- Scenario order is preserved.
- The legacy entrypoint remains the documented stable command.
- `--list`, `--match`, and `--fail-fast` work.
- Reviewers report no missing scenario, import drift, or isolation regression.
