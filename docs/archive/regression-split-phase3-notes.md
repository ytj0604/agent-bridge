# Regression Split Phase 3 Notes

Date: 2026-04-29

Phase 3 scope: move low-risk regression domains out of
`scripts/regression_interrupt.py` while preserving the exact legacy scenario
order.

## Changes

- Added `scripts/regression/install_uninstall.py`.
  - 43 scenario entries.
- Added `scripts/regression/hook_config.py`.
  - 37 scenario entries.
- Added `scripts/regression/view_peer.py`.
  - 52 scenario entries.
- Added `scripts/regression/docs_contracts.py`.
  - 10 scenario entries.
- Added `scripts/regression/send_enqueue.py`.
  - 71 scenario entries.
- Moved nine shared helpers into `scripts/regression/harness.py`:
  - `_write_seed_hook_configs`
  - `_fake_install_env`
  - `_participants_state`
  - `_qualifying_message`
  - `_delivered_request`
  - `_import_enqueue_module`
  - `_run_enqueue_main`
  - `_patch_enqueue_for_unit`
  - `_write_json`
- Added `scenario_index()` to `scripts/regression/registry.py`.
- Updated `scripts/regression_interrupt.py`.
  - The legacy file still owns the stable entrypoint.
  - `scenarios()` imports the moved module `SCENARIOS` lists and uses a flat
    label map to keep the original global order.
- Trimmed generated module imports after structure review flagged the initial
  broad import headers as a maintainability risk.

Moved scenario entries: 213.

Remaining legacy `scenario_` functions: 402.

## Validation

Syntax:

```sh
python3 -m py_compile scripts/regression_interrupt.py scripts/regression/*.py
```

Registry count and moved module smoke:

```sh
python3 scripts/regression_interrupt.py --list | wc -l
# 615
```

Registry order/function-name comparison against `HEAD`:

```text
615 615
same True
```

Targeted runs:

```sh
python3 scripts/regression_interrupt.py \
  --match uninstall \
  --match install_sh \
  --match direct_exec \
  --match healthcheck_executable \
  --match python_env_override \
  --match bridge_healthcheck_sh \
  --fail-fast
# 54 passed, 0 failed

python3 scripts/regression_interrupt.py \
  --match bridge_install_hooks \
  --match codex_config \
  --fail-fast
# 37 passed, 0 failed

python3 scripts/regression_interrupt.py \
  --match view_peer \
  --match format_peer_list \
  --match bridge_manage_summary \
  --match model_safe_participants \
  --match list_peers \
  --fail-fast
# 53 passed, 0 failed

python3 scripts/regression_interrupt.py \
  --match doc_surfaces \
  --match probe_prompt \
  --match watchdog_phase_doc \
  --match wait_status_doc \
  --match aggregate_status_doc \
  --fail-fast
# 10 passed, 0 failed

python3 scripts/regression_interrupt.py \
  --match send_peer \
  --match enqueue \
  --fail-fast
# 75 passed, 0 failed
```

Diff hygiene:

```sh
git diff --check
```

Full suite:

```sh
python3 scripts/regression_interrupt.py
# 615 passed, 0 failed
```

## Review Results

Structure review requested read-only with this scope:

```text
Changed scope: scripts/regression_interrupt.py, scripts/regression/registry.py,
scripts/regression/harness.py, and new low-risk modules under
scripts/regression/.

Check scenario count/order, missing moved scenarios, module boundaries, import
drift, and whether the flat ordered registry preserves the original order. Do
not edit files.
```

Result: no blocking findings.

The structure reviewer verified:

- Current `load_scenarios()` is 615 entries.
- The current label/function-name order exactly matches `HEAD`.
- There are no duplicate labels.
- The moved-module counts are:
  - `regression.install_uninstall`: 43
  - `regression.hook_config`: 37
  - `regression.view_peer`: 52
  - `regression.docs_contracts`: 10
  - `regression.send_enqueue`: 71
  - legacy `regression_interrupt`: 402
- Every moved scenario definition is loaded through `SCENARIOS`.

The reviewer also surfaced broad generated import headers as a non-blocking
maintainability issue; the imports were trimmed before the final validation run.

Isolation review requested read-only with this scope:

```text
Changed scope: scripts/regression_interrupt.py, scripts/regression/registry.py,
scripts/regression/harness.py, and new scripts/regression/{install_uninstall.py,
hook_config.py, view_peer.py, docs_contracts.py, send_enqueue.py}.

Focus on env restoration, sys.argv/sys.stdin restoration, subprocess/module
monkeypatch restoration, tempdir isolation, and whether moved harness helpers
preserve prior semantics. Do not edit files.
```

Result: no findings.

The isolation reviewer checked `sys.argv`/`sys.stdin` wrappers, subprocess
stubs, view-peer monkeypatch blocks, environment handling, and the moved harness
helpers. No Phase 3 isolation regression was found.

Reviewer validation:

```sh
python3 scripts/regression_interrupt.py --match send_peer --match enqueue --fail-fast
# 75 passed, 0 failed

python3 scripts/regression_interrupt.py --match view_peer --fail-fast
# 44 passed, 0 failed

python3 scripts/regression_interrupt.py --match install --match uninstall --match hook --fail-fast
# 85 passed, 0 failed
```
