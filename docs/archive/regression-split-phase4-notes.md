# Regression Split Phase 4 Notes

Date: 2026-04-29

Phase 4 scope: move medium-risk daemon-adjacent regression domains out of
`scripts/regression_interrupt.py` while preserving exact scenario order.

## Changes

- Added `scripts/regression/join_leave_attach.py`.
  - 19 scenario entries.
- Added `scripts/regression/aggregate_wait_status.py`.
  - 23 scenario entries.
- Added `scripts/regression/watchdog_alarm.py`.
  - 64 scenario entries.
- Added `scripts/regression/cancel_message.py`.
  - 11 scenario entries.
- Added `scripts/regression/pane_mode_delivery.py`.
  - 13 scenario entries.
- Added `scripts/regression/daemon_core.py`.
  - 130 scenario entries.
- Moved twelve shared helpers into `scripts/regression/harness.py`:
  - `_enqueue_alarm`
  - `_set_response_context`
  - `_watchdogs_for_message`
  - `_make_inflight`
  - `_make_delivered_context`
  - `_queue_item`
  - `_active_turn`
  - `_plant_watchdog`
  - `_auto_return_results`
  - `_assert_auto_return_result_shape`
  - `_import_daemon_ctl`
  - `_daemon_command_result`
- Updated `scripts/regression_interrupt.py`.
  - The legacy file still owns the stable entrypoint.
  - `scenarios()` now indexes Phase 3 and Phase 4 module `SCENARIOS` lists
    through the same flat label map.
  - High-risk interrupt, identity recovery, and clear-peer scenario families
    remain in the legacy file for Phase 5.

Moved Phase 4 scenario entries: 260.

Remaining legacy `scenario_` functions: 142.

Total moved scenario entries after Phase 4: 473.

## Validation

Syntax:

```sh
python3 -m py_compile scripts/regression_interrupt.py scripts/regression/*.py
```

Registry count:

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
  --match detached \
  --match join \
  --match bridge_attach \
  --match bridge_daemon_ctl \
  --match bridge_daemon_rejects \
  --match daemon_command \
  --match daemon_follow \
  --fail-fast
# 19 passed, 0 failed

python3 scripts/regression_interrupt.py \
  --match aggregate_status \
  --match wait_status \
  --match aggregate_trigger \
  --fail-fast
# 25 passed, 0 failed

python3 scripts/regression_interrupt.py \
  --match watchdog \
  --match alarm \
  --match extend_wait \
  --fail-fast
# 95 passed, 0 failed

python3 scripts/regression_interrupt.py --match cancel_message --fail-fast
# 11 passed, 0 failed

python3 scripts/regression_interrupt.py \
  --match pane_mode \
  --match retry_enter \
  --match enter_deferred \
  --match pre_enter \
  --match wait_for_probe \
  --fail-fast
# 18 passed, 0 failed

python3 scripts/regression_interrupt.py \
  --match lifecycle \
  --match response_send_guard \
  --match ingressing \
  --match daemon_prior_hint \
  --match prompt_ \
  --match consume_once \
  --match empty_response \
  --match nonce \
  --match turn_id_mismatch \
  --match resolve_targets \
  --match prune \
  --match queue_status \
  --match peer_result_redirect \
  --match tmux_paste \
  --fail-fast
# 118 passed, 0 failed
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

Isolation review result: no findings.

During validation, two mechanical extraction misses were fixed before the final
suite run:

- `isolated_bridge_cli_env` kept its `@contextmanager` decorator in the moved
  join/leave/attach module.
- `FakeProbeClock` moved with the wait-for-probe retry helper.

## Review Requests

Structure review requested read-only with this scope:

```text
Changed scope: scripts/regression/{join_leave_attach.py,
aggregate_wait_status.py, watchdog_alarm.py, cancel_message.py,
pane_mode_delivery.py, daemon_core.py}, scripts/regression/harness.py, and
scripts/regression_interrupt.py.

Check scenario count/order, missing or unloaded scenario definitions, module
SCENARIOS entries, flat registry order preservation, helper placement, and
import drift. Do not edit files.
```

Isolation review requested read-only with this scope:

```text
Changed scope: scripts/regression/{join_leave_attach.py,
aggregate_wait_status.py, watchdog_alarm.py, cancel_message.py,
pane_mode_delivery.py, daemon_core.py}, scripts/regression/harness.py, and
scripts/regression_interrupt.py.

Focus on env restoration, sys.argv/sys.stdin restoration, subprocess/module
monkeypatch restoration, daemon globals, sockets, tempdirs, locks, fake clocks,
time monkeypatches, and moved harness helper semantics. Do not edit files.
```

## Review Results

Structure review found one blocking issue:

- `scripts/regression_interrupt.py` had a duplicate `scenarios()` definition
  after the `if __name__ == "__main__"` block.

The follow-up structure review also flagged import drift in
`scripts/regression_interrupt.py` after the Phase 4 moves.

Isolation review found no blocking or non-blocking issues. The reviewer checked
environment restoration, CLI stream restoration, subprocess and module
monkeypatch cleanup, daemon globals, sockets, tempdirs, locks, fake clocks, time
monkeypatches, and moved harness helper semantics.

Resolution:

- Removed the duplicate trailing `scenarios()` definition.
- Confirmed only one `scenarios()` definition remains before `main()`.
- Trimmed stale legacy imports from `scripts/regression_interrupt.py`.
- Re-ran syntax, registry count, exact `HEAD` order/function-name comparison,
  and the full suite.

Post-fix validation:

```sh
rg -n "^def scenarios|^def main|__main__" scripts/regression_interrupt.py
# 5126:def scenarios() -> list[tuple[str, object]]:
# 5773:def main(argv: list[str] | None = None) -> int:
# 5779:if __name__ == "__main__":

python3 -m py_compile scripts/regression_interrupt.py scripts/regression/*.py

python3 scripts/regression_interrupt.py --list | wc -l
# 615
```

Unused-import sweep for `scripts/regression_interrupt.py`:

```text
unused []
```

```sh
python3 scripts/regression_interrupt.py
# 615 passed, 0 failed
```
