# Regression Split Phase 1 Notes

Date: 2026-04-29

Phase 1 scope: extract the regression runner and registry while leaving all
scenario bodies in `scripts/regression_interrupt.py`.

## Changes

- Added `scripts/regression/__init__.py`.
- Added `scripts/regression/runner.py`.
  - Owns argparse.
  - Owns scenario filtering.
  - Owns the run loop and summary output.
  - Supports `--keep-tmp`, `--list`, `--match TEXT`, and `--fail-fast`.
- Added `scripts/regression/registry.py`.
  - Defines scenario types.
  - Loads a scenario source.
  - Validates duplicate labels and duplicate functions.
  - Can fall back to the legacy `regression_interrupt.py` scenario source.
- Changed `scripts/regression_interrupt.py`.
  - Scenario bodies remain in place.
  - The ordered scenario list is exposed as `scenarios()`.
  - `main()` delegates to `regression.runner.main()`.

## Validation

Syntax:

```sh
python3 -m py_compile \
  scripts/regression_interrupt.py \
  scripts/regression/runner.py \
  scripts/regression/registry.py
```

List count:

```sh
python3 scripts/regression_interrupt.py --list | wc -l
# 615
```

List pipe behavior:

```sh
python3 scripts/regression_interrupt.py --list | head -n 3
python3 scripts/regression_interrupt.py --list | tail -n 3
```

Match/filter behavior:

```sh
python3 scripts/regression_interrupt.py --list --match interrupt | wc -l
# 49

python3 scripts/regression_interrupt.py --match interrupt --fail-fast
# 49 passed, 0 failed
```

No-match behavior:

```sh
python3 scripts/regression_interrupt.py --match definitely_no_such_scenario
# No scenarios matched.
# exit_status=1
```

Full suite:

```sh
python3 scripts/regression_interrupt.py
# 615 passed, 0 failed
# elapsed_seconds=22
```

Diff hygiene:

```sh
git diff --check
```

## Review Request

A read-only Phase 1 structure review was requested with this scope:

```text
Changed scope:
- scripts/regression_interrupt.py
- scripts/regression/__init__.py
- scripts/regression/runner.py
- scripts/regression/registry.py

Check scenario count/order preservation, wrapper compatibility, option
behavior, import/circular import risks, and whether registry.py design preserves
the Phase 0 warning about non-contiguous domains. Do not edit files.
```

## Structure Reviewer Findings

Read-only review completed with no blocking findings.

Summary:

- The new `scenarios()` list is label/function-order equivalent to the old
  inline `main()` registry:
  - 615 entries
  - same first and last labels
  - no duplicates
- The `python3 scripts/regression_interrupt.py` wrapper path remains compatible.
- Passing the local `scenarios` callable into `runner.main()` avoids circular
  import risk for the legacy entrypoint.
- `--keep-tmp`, `--list`, `--match`, and `--fail-fast` are centralized in the
  runner as intended.
- `--list` avoids tempdir creation and handles broken pipes.
- Registry validation preserves order by materializing the provided sequence as
  a list and validating it without sorting or grouping.
- Phase 0's non-contiguous-domain warning remains preserved because the registry
  still consumes one flat ordered source.

Carry-forward note for later phases: keep a flat ordered registry or ordered
fragment model. Do not switch to coarse per-module concatenation unless the
registry can place non-contiguous fragments back at their exact original
positions.
