# Regression Split Phase 6 Notes

Date: 2026-04-29

Postscript: on 2026-04-30, the stable entrypoint was renamed to
`scripts/run_regressions.py` and the old `scripts/regression_interrupt.py`
wrapper was removed.

Phase 6 scope: final cleanup after all regression scenarios were moved out of
`scripts/regression_interrupt.py`.

## Changes

- Moved the ordered default scenario registry from `scripts/regression_interrupt.py`
  into `scripts/regression/registry.py`.
- Reduced `scripts/regression_interrupt.py` to a stable entrypoint wrapper that
  calls `regression.runner.main()`.
- Updated `registry.load_scenarios()` so the default source is the ordered
  registry in `registry.scenarios`.
- Removed unused broad imports from:
  - `scripts/regression/interrupt.py`
  - `scripts/regression/identity_recovery.py`
  - `scripts/regression/clear_peer.py`
- Kept the documented command unchanged:

  ```sh
  python3 scripts/regression_interrupt.py
  ```

## Validation

Syntax:

```sh
python3 -m py_compile scripts/regression_interrupt.py scripts/regression/*.py
```

Scenario count:

```sh
python3 scripts/regression_interrupt.py --list | wc -l
# 615
```

Runtime `(label, function.__name__)` comparison against the saved Phase 5
baseline:

```text
count 615
same True
```

Runtime `(label, function.__name__)` comparison against `HEAD` Phase 5
`scripts/regression_interrupt.py`:

```text
head 615
current 615
same True
```

Clear-focused run:

```sh
python3 scripts/regression_interrupt.py --match clear --fail-fast
# 52 passed, 0 failed
```

Full suite:

```sh
python3 scripts/regression_interrupt.py
# 615 passed, 0 failed
```

Diff hygiene:

```sh
git diff --check
```

Unused import sweep:

```text
no unused import candidates reported
```

Install and health checks were not run because Phase 6 changed only regression
suite organization, not install, hook, shim, tmux, or runtime bridge behavior.

## Review Requests

Structure review requested read-only with this scope:

```text
Changed scope: scripts/regression_interrupt.py,
scripts/regression/registry.py, scripts/regression/{interrupt.py,
identity_recovery.py,clear_peer.py}, and
docs/regression-split-phase6-notes.md.

Check that the legacy file is only an entrypoint wrapper, default scenario
loading no longer depends on importing regression_interrupt, scenario count and
order match HEAD/Phase 5, module SCENARIOS entries remain complete, imports are
not drifting, and docs accurately describe validation. Do not edit files.
```

Isolation review requested read-only with this scope:

```text
Changed scope: scripts/regression_interrupt.py,
scripts/regression/registry.py, scripts/regression/{interrupt.py,
identity_recovery.py,clear_peer.py}, and
docs/regression-split-phase6-notes.md.

Focus on whether the registry move or import cleanup changes test isolation,
module import timing, env/sys.argv/sys.stdin restoration, monkeypatch cleanup,
or thread/lock behavior. Do not edit files.
```

## Review Results

Final structure review found no structural blockers.

The reviewer confirmed `scripts/regression_interrupt.py` is only the stable
entrypoint wrapper, `registry.load_scenarios()` defaults to `registry.scenarios`
without importing `regression_interrupt`, scenario count/order match `HEAD`
Phase 5 exactly at 615 labels and exact `(label, function.__name__)` sequence,
the three high-risk module `SCENARIOS` tables are complete, the changed files
have no unused import candidates, and the notes accurately describe validation.

The only structure-review issue was commit-scope: this notes file is untracked
until the Phase 6 commit stages it.

Final isolation review reported no findings.

The reviewer confirmed the registry move is isolation-neutral: importing
`scripts/regression_interrupt.py` now loads only the runner/registry path and no
bridge or scenario modules, default scenario loading imports split modules
inside `registry.scenarios()`, and the runtime `(label, function.__name__)` list
matches the Phase 5 baseline exactly at 615 entries. The import cleanup removed
only unused imports and did not change environment handling, CLI stream
restoration, monkeypatch cleanup, or thread/lock behavior.
