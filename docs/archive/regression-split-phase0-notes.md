# Regression Split Phase 0 Notes

Date: 2026-04-29

Phase 0 scope: baseline validation, scenario inventory, registry consistency
checks, and read-only structure-review request.

## Baseline

Command:

```sh
python3 scripts/regression_interrupt.py
```

Result:

```text
615 passed, 0 failed
exit_status=0
elapsed_seconds=22
```

Full command output was captured locally at:

```text
/tmp/regression-split-phase0-baseline.log
```

## Inventory

Source file:

```text
scripts/regression_interrupt.py
```

Counts:

```text
total_functions=737
scenario_functions=615
helper_functions=122
ordered_scenario_entries=615
unique_labels=615
unique_refs=615
missing_refs=0
unlisted_scenario_functions=0
duplicate_labels=0
duplicate_refs=0
scenarios_list_lines=18140-18756
```

The current ordered registry is internally consistent: every `scenario_*`
function is listed exactly once, every listed function exists, and labels are
unique.

Top scenario prefixes:

```text
send=53
view=44
clear=42
daemon=37
install=33
bridge=29
aggregate=24
interrupt=22
enqueue=20
codex=20
response=15
alarm=15
turn=14
delivery=12
resolve=12
held=11
interrupted=11
cancel=11
prompt=11
target=11
peer=11
wait=10
extend=9
pane=8
unscoped=8
```

Longest functions:

```text
main:18134-18775:642
scenario_clear_claude_post_clear_probe_pasted_and_completes:17086-17299:214
scenario_clear_marker_probe_id_fallback_contracts:16258-16417:160
scenario_watchdog_phase_doc_surfaces_are_consistent:9512-9633:122
scenario_pending_watchdog_wake_removed_on_terminal:2722-2842:121
scenario_command_state_lock_timeout_before_mutation:17364-17480:117
scenario_clear_post_clear_first_request_routes_reply:16420-16532:113
scenario_clear_codex_post_clear_endpoint_verify_immediately:16975-17083:109
scenario_clear_with_existing_inbound_queue_routes_replies:16535-16639:105
scenario_clear_post_clear_delay_config_and_settle:16713-16817:105
scenario_clear_force_leave_unowned_lock_serializes_cleanup:17796-17899:104
scenario_extend_wait_terminal_error_texts:12531-12624:94
scenario_tmux_paste_buffer_delivery_sequence:14739-14831:93
scenario_interrupt_peer_doc_surfaces_disclose_no_op_race:9214-9305:92
scenario_clear_guard_force_truth_matches_policy:15481-15559:79
scenario_aggregate_completion_suppresses_pending_watchdog_wake:3148-3224:77
scenario_clear_requires_verified_process_identity:16898-16972:75
scenario_daemon_reload_inflight_idempotent:5596-5669:74
scenario_clear_multi_real_pre_pane_failure_holds_until_batch_end:15903-15973:71
scenario_response_send_guard_doc_surfaces_are_precise:9352-9416:65
```

## Global-State Risk Signals

Approximate text-pattern counts:

```text
os.environ writes=143
sys.argv writes=36
sys.stdin writes=9
subprocess monkeypatch=16
bridge_daemon monkeypatch=116
bridge_identity monkeypatch=239
threading.Thread=17
socket.socket=10
tempfile.mkdtemp=2
SHARED_PAYLOAD_ROOT=3
```

These counts support the plan's warning against thread-based parallelism and
against concurrent multi-writer file splitting.

## Registry Notes

The first ten scenario labels are:

```text
lifecycle_delivered_terminal
held_interrupt_does_not_block_delivery
reserve_next_ignores_held_marker
held_marker_persists_through_delivery
esc_fail_no_state_change
clear_hold_logs_event
clear_hold_socket_still_pops_held
aggregate_interrupt_synthetic_reply
interrupt_pending_replacement_delivers
interrupt_new_replacement_after_interrupt_delivers
```

The last ten scenario labels are:

```text
clear_marker_phase2_missing_forces_leave
clear_marker_phase2_rejects_record_turn_without_marker_turn
run_clear_peer_phase2_failure_returns_immediately
clear_force_leave_cleans_identity_stores
clear_force_leave_unowned_lock_serializes_cleanup
clear_identity_helper_success_and_failure
force_clear_requester_dual_write_queue_and_active
clear_aggregate_requester_late_reply_suppressed
self_clear_promotion_and_cancel_notice_ordering
clear_marker_ttl_expiry_removes_marker
```

## Reviewer Request

A read-only Phase 0 preflight review was requested from the `structure reviewer`
with this scope:

```text
Review scripts/regression_interrupt.py read-only.
Identify risky shared helpers, global monkeypatches, environment mutation,
and scenario groups that should move late. Do not edit files.
```

## Structure Reviewer Findings

Read-only preflight completed with no edits.

Key findings:

- Inventory independently matched the worker inventory: 615 `scenario_`
  functions and 615 registry entries, with no missing functions, unregistered
  scenarios, duplicate labels, or duplicate functions.
- The canonical scenario order is the explicit `scenarios = [...]` list in
  `main()`, not function definition order.
- Broad shared harness candidates are:
  - `make_daemon`
  - `read_events`
  - `assert_true`
  - `FakeCommandConn`
  - `patched_environ`
  - `patched_redirect_root`
  - `test_message`
  - identity fixture helpers near the top of the file
- Domain-specific helpers such as `_make_inflight`,
  `_make_delivered_context`, `_active_turn`, and `_plant_watchdog` should not be
  moved into generic harness unless multiple split modules really need them.
- CLI helpers mutate `sys.argv`, `sys.stdin`, and module functions. These should
  stay domain-local or be wrapped in restoration-safe harness contexts.
- Some unit patch helpers intentionally rely on fresh `importlib.reload` rather
  than local restoration:
  - `_patch_enqueue_for_unit`
  - `_patch_wait_status_for_unit`
  - `_patch_aggregate_status_for_unit`
  - `_patch_send_peer_for_unit`
  Preserve that reload discipline when splitting.
- Environment mutation is mixed. `patched_environ` is safe, but some scenarios
  directly set or pop environment variables. Prefer wrapping these in shared env
  context helpers during extraction.
- One notable global patch hazard:
  `scenario_restart_dry_run_no_side_effect` patches `ctl.start_under_lock` and
  relies on later `ctl` reload behavior. Splitting can leak this patch if import
  and reload behavior changes.
- Move these groups late:
  - interrupt and daemon core
  - identity recovery
  - target recovery
  - alarm socket retry tests
  - clear peer
- Registry/order pitfall: target domains are not perfectly contiguous. A simple
  "concatenate whole modules once" registry can reorder tests. The registry may
  need ordered fragments, or some strays must remain in their original adjacent
  batch until a later cleanup.

Implication for Phase 1: preserve the current `scenarios = [...]` order as data
before moving scenario bodies. Avoid designing `registry.py` as only one
contiguous module block per domain unless it can represent ordered fragments.
