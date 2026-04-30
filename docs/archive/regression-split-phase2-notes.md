# Regression Split Phase 2 Notes

Date: 2026-04-29

Phase 2 scope: extract broadly shared regression fixtures into
`scripts/regression/harness.py` before moving scenario bodies.

## Changes

- Added `scripts/regression/harness.py`.
- Moved shared constants:
  - `ROOT`
  - `LIBEXEC`
  - `DIRECT_EXECUTABLE_TARGETS`
  - `INSTALL_SHIM_TARGETS`
- Moved shared daemon/test helpers:
  - `make_daemon`
  - `read_events`
  - `assert_true`
  - `FakeCommandConn`
  - `patched_environ`
  - `patched_redirect_root`
  - `test_message`
- Moved top-level identity fixture helpers:
  - `isolated_identity_env`
  - `write_identity_fixture`
  - `verified_identity`
  - `identity_live_record`
  - `read_raw_events`
  - `write_live_identity_records`
  - `set_identity_target`
- Updated `scripts/regression_interrupt.py` to import those helpers from
  `regression.harness`.

Scenario bodies and domain-specific helpers remain in
`scripts/regression_interrupt.py`.

## Validation

Syntax:

```sh
python3 -m py_compile \
  scripts/regression_interrupt.py \
  scripts/regression/harness.py \
  scripts/regression/runner.py \
  scripts/regression/registry.py
```

Targeted runs:

```sh
python3 scripts/regression_interrupt.py --match lifecycle --fail-fast
# 1 passed, 0 failed

python3 scripts/regression_interrupt.py --match send_peer --fail-fast
# 52 passed, 0 failed

python3 scripts/regression_interrupt.py --match identity --fail-fast
# 10 passed, 0 failed
```

Registry count:

```sh
python3 scripts/regression_interrupt.py --list | wc -l
# 615
```

Registry/import smoke:

```sh
python3 - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path('scripts').resolve()))
import regression_interrupt as ri
items = ri.scenarios()
print(len(items))
print(items[0][0])
print(items[-1][0])
print(len({label for label, _ in items}), len({fn for _, fn in items}))
PY
# 615
# lifecycle_delivered_terminal
# clear_marker_ttl_expiry_removes_marker
# 615 615
```

Diff hygiene:

```sh
git diff --check
```

Full suite:

```sh
python3 scripts/regression_interrupt.py
# 615 passed, 0 failed
# elapsed_seconds=22
```

## Review Result

A read-only Phase 2 isolation review was requested with this scope:

```text
Changed scope:
- scripts/regression_interrupt.py
- scripts/regression/harness.py

Focus on environment restoration, sys.argv/sys.stdin restoration, subprocess
stubs, module monkeypatch restoration, tmpdir isolation, import-time side
effects from harness.py, and whether moved helpers preserve semantics. Do not
edit files.
```

Result: no findings.

The reviewer checked the moved helper bodies against the previous top-of-file
implementation and found no isolation regression. In particular:

- `patched_environ`, `patched_redirect_root`, and `isolated_identity_env` still
  restore their mutated state through `finally` blocks.
- `isolated_identity_env` preserves and restores the same identity environment
  key set as before.
- `sys.argv`, `sys.stdin`, and subprocess monkeypatch sites remain scenario-local
  in `scripts/regression_interrupt.py`.
- The `bridge_daemon` import and `sys.path` side effect moved into
  `scripts/regression/harness.py`, but normal `regression_interrupt.py`
  execution keeps the same effective import order.

Reviewer validation:

```sh
python3 scripts/regression_interrupt.py --match identity --fail-fast
# 10 passed, 0 failed
```
