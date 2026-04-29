# Regression Split Phase 5 Notes

Date: 2026-04-29

Phase 5 scope: move the final high-risk regression domains out of
`scripts/regression_interrupt.py` without changing scenario behavior or order.

## Changes

- Added `scripts/regression/interrupt.py`.
  - 41 scenario entries.
  - Includes interrupt, held-interrupt, interrupted tombstone, interrupt CLI,
    and interrupt key helpers.
- Added `scripts/regression/identity_recovery.py`.
  - 56 scenario entries.
  - Includes endpoint verification, identity backfill, unscoped hook
    canonicalization, target recovery, resume/reconnect, and endpoint-lost
    scenarios.
- Added `scripts/regression/clear_peer.py`.
  - 45 scenario entries.
  - Includes clear guard, multi-clear, clear CLI, clear marker, post-clear
    routing, clear identity, lock/deadline, and force-leave cleanup scenarios.
- Updated `scripts/regression_interrupt.py`.
  - No `scenario_` functions remain in the legacy file.
  - `scenarios()` still preserves the flat ordered registry and maps all moved
    labels through `scenario_index(...)`.

Moved Phase 5 scenario entries: 142.

Total scenario entries: 615.

## Validation

Pre-Phase-5 full suite:

```sh
python3 scripts/regression_interrupt.py
# 615 passed, 0 failed
```

After `interrupt.py`:

```sh
python3 -m py_compile scripts/regression_interrupt.py scripts/regression/*.py

python3 scripts/regression_interrupt.py \
  --match interrupt \
  --match interrupted \
  --fail-fast
# 49 passed, 0 failed

python3 scripts/regression_interrupt.py
# 615 passed, 0 failed
```

After `identity_recovery.py`:

```sh
python3 -m py_compile scripts/regression_interrupt.py scripts/regression/*.py

python3 scripts/regression_interrupt.py \
  --match endpoint \
  --match backfill \
  --match unscoped \
  --match scoped \
  --match resolver \
  --match reconnect \
  --match recovery \
  --match resume \
  --match probe \
  --fail-fast
# 62 passed, 0 failed

python3 scripts/regression_interrupt.py
# 615 passed, 0 failed
```

After `clear_peer.py`:

```sh
python3 -m py_compile scripts/regression_interrupt.py scripts/regression/*.py

python3 scripts/regression_interrupt.py --match clear --fail-fast
# 52 passed, 0 failed

python3 scripts/regression_interrupt.py
# 615 passed, 0 failed
```

Final registry checks:

```sh
python3 scripts/regression_interrupt.py --list | wc -l
# 615
```

Runtime `(label, function.__name__)` comparison against the saved Phase 4
baseline:

```text
615 True
```

Scenario definition and local `SCENARIOS` counts:

```text
scripts/regression/interrupt.py defs 41 entries 41
scripts/regression/identity_recovery.py defs 56 entries 56
scripts/regression/clear_peer.py defs 45 entries 45
scripts/regression_interrupt.py defs 0 entries 0
```

Diff hygiene:

```sh
git diff --check
```

## Review Requests

Preflight review results:

- Structure preflight reported no current count/order issue. It identified the
  exact high-risk groups as global labels `001-041`, `434-488`, `490`, and
  `570-614`; the implemented module boundaries match those groups.
- Isolation preflight reported no findings. It confirmed the planned helper
  locality and checked env restoration, CLI stream restoration, module
  monkeypatch restoration, and thread/lock cleanup patterns.

Structure review requested read-only with this scope:

```text
Changed scope: scripts/regression/{interrupt.py,identity_recovery.py,
clear_peer.py}, scripts/regression_interrupt.py, and
docs/regression-split-phase5-notes.md once it appears.

Check scenario count/order against current HEAD, module SCENARIOS entries,
missing/unloaded scenario defs, helper locality, duplicate scenarios(), and
import drift. Phase 6 cleanup is still planned, but report anything that should
block Phase 5. Do not edit files.
```

Isolation review requested read-only with this scope:

```text
Changed scope: scripts/regression/{interrupt.py,identity_recovery.py,
clear_peer.py}, scripts/regression_interrupt.py, and
docs/regression-split-phase5-notes.md once it appears.

Focus on env restoration, sys.argv/sys.stdin restoration, daemon/global
monkeypatches, bridge_identity/clear_marker state, sockets, threads, locks,
timers, fake subprocesses, and moved helper locality. Do not edit files.
```

## Review Results

Final structure review found no blocking structure issues.

The reviewer confirmed scenario count/order matches current `HEAD` at 615
labels, exact order is preserved, the new module ranges are correct
(`interrupt` labels `001-041`, `identity_recovery` labels `434-488` and `490`,
`clear_peer` labels `570-614`), each new module's `SCENARIOS` entries resolve to
local scenario definitions, there are no missing or unloaded local
`scenario_*` definitions, there are no duplicate labels, and the wrapper has one
`scenarios()` definition.

The only structure-review blocker was commit-scope: the three new Phase 5
modules are untracked until the Phase 5 commit stages them. The reviewer also
noted broad import drift in the wrapper and new modules as non-blocking Phase 6
cleanup.

The structure review could not inspect this notes file because it was added
after that review started.

Final isolation review reported no findings.

The reviewer confirmed environment changes are contained, CLI stream helpers
restore `sys.argv` and `sys.stdin` in `finally`, module monkeypatches and fake
subprocess stubs restore in `finally`, clear-marker and identity-store tests are
rooted in temp paths, threaded clear/lock scenarios release and join worker
threads, and helper locality is preserved across the three new modules.

The isolation review could not inspect this notes file because it was added
after that review started.

## Phase 6 Handoff

Cleanup remains intentionally separate:

- remove unused imports from split modules
- collapse the legacy entrypoint once registry ownership is finalized
- run the Phase 6 final validation commands from the split plan
