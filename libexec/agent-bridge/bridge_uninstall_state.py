#!/usr/bin/env python3
"""Helper used by uninstall.sh to remove bridge runtime state safely.

Resolves the actual `state_root() / run_root() / log_root()` (which honor
XDG and env overrides — uninstall.sh's old `$root/state` heuristic missed
the real `/tmp/agent-bridge-*/state` location). Refuses to delete unless
all paths look like agent-bridge runtime directories. Refuses if any
bridge daemon is currently running unless `--force` is passed.

Modes:
  --print-paths   : print JSON {state, run, log}; do not delete.
  --dry-run       : print what would be deleted; no daemon check.
  (default)       : delete after safety checks.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

LIBEXEC = Path(__file__).resolve().parent
sys.path.insert(0, str(LIBEXEC))

from bridge_paths import APP_NAME, log_root, run_root, state_root  # noqa: E402


# Paths that look "system-wide" enough that we never delete them, even if a
# malicious env override pointed us at them.
DANGEROUS_PATHS = {
    Path("/"),
    Path("/usr"),
    Path("/usr/local"),
    Path("/etc"),
    Path("/var"),
    Path("/home"),
    Path("/root"),
    Path("/Users"),
    Path.home().resolve() if Path.home().exists() else Path("/dev/null"),
    Path.cwd().resolve(),
}


def _is_safe_runtime_path(path: Path) -> tuple[bool, str]:
    resolved = path.expanduser().resolve(strict=False)
    if resolved in DANGEROUS_PATHS:
        return False, f"refuses to delete dangerous path: {resolved}"
    if not str(resolved).rstrip("/"):
        return False, "refuses to delete root '/'"
    # Heuristic: agent-bridge runtime paths always include the APP_NAME
    # somewhere (default 'agent-bridge'). This catches both XDG_RUNTIME_DIR
    # (`/tmp/agent-bridge-0/state`) and install-root fallbacks
    # (`/root/agent-bridge/state`). An empty APP_NAME would silently
    # disable this check, so refuse outright if APP_NAME is empty.
    if not APP_NAME:
        return False, "AGENT_BRIDGE_APP_NAME is empty; refusing to delete (would disable safety guard)"
    if APP_NAME not in str(resolved):
        return False, f"path does not contain APP_NAME ({APP_NAME!r}): {resolved}"
    return True, ""


def _running_daemons() -> list[str]:
    """Return the list of session names whose daemon appears to be alive.

    Best-effort: scan run_root() for `<session>.pid` files and check if
    each PID exists. We avoid importing bridge_daemon_ctl to keep this
    helper lightweight.
    """
    sessions: list[str] = []
    run = run_root()
    if not run.exists():
        return sessions
    try:
        for pid_file in run.glob("*.pid"):
            try:
                pid = int(pid_file.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                continue
            try:
                os.kill(pid, 0)
            except PermissionError:
                # PID exists but we can't signal (different uid, namespace, etc.).
                # Treat as alive — it is safer to refuse the destructive action
                # than to delete state out from under a still-running daemon.
                sessions.append(pid_file.stem)
                continue
            except ProcessLookupError:
                continue
            except OSError:
                continue
            sessions.append(pid_file.stem)
    except OSError:
        pass
    return sessions


def main() -> int:
    parser = argparse.ArgumentParser(prog="bridge_uninstall_state")
    parser.add_argument("--print-paths", action="store_true", help="print resolved paths as JSON and exit")
    parser.add_argument("--dry-run", action="store_true", help="show what would be deleted without deleting")
    parser.add_argument("--force", action="store_true", help="proceed even if a daemon appears to be running")
    parser.add_argument("--keep-state", action="store_true", help="skip removing state_root (only run/log)")
    parser.add_argument("--keep-run", action="store_true", help="skip removing run_root (only state/log)")
    parser.add_argument("--keep-log", action="store_true", help="skip removing log_root (only state/run)")
    args = parser.parse_args()

    paths = {
        "state": str(state_root()),
        "run": str(run_root()),
        "log": str(log_root()),
    }
    if args.print_paths:
        print(json.dumps(paths, ensure_ascii=True))
        return 0

    targets: list[tuple[str, Path]] = []
    if not args.keep_state:
        targets.append(("state", state_root()))
    if not args.keep_run:
        targets.append(("run", run_root()))
    if not args.keep_log:
        targets.append(("log", log_root()))

    # Safety: make sure each target looks like a real bridge runtime dir.
    for label, path in targets:
        ok, reason = _is_safe_runtime_path(path)
        if not ok:
            print(f"bridge_uninstall_state: refuses to delete {label}: {reason}", file=sys.stderr)
            return 2

    # Refuse if a daemon is alive (prevents wedging running peers).
    if not args.force and not args.dry_run:
        running = _running_daemons()
        if running:
            print(
                "bridge_uninstall_state: refusing to delete state while daemons are alive: "
                + ", ".join(sorted(running))
                + ". Stop them first (bridge_daemon_ctl stop --all) or pass --force.",
                file=sys.stderr,
            )
            return 2

    removed = []
    skipped = []
    for label, path in targets:
        if not path.exists():
            skipped.append(f"{label} (missing): {path}")
            continue
        if args.dry_run:
            print(f"dry-run: would rm -rf {label} ({path})")
            removed.append(str(path))
            continue
        try:
            shutil.rmtree(path)
            removed.append(str(path))
            print(f"removed {label}: {path}")
        except OSError as exc:
            print(f"bridge_uninstall_state: failed to remove {label} ({path}): {exc}", file=sys.stderr)
            return 1

    if args.dry_run:
        print("dry-run: nothing actually deleted")
    elif not removed:
        print("bridge_uninstall_state: nothing to delete")
    if skipped:
        for line in skipped:
            print(f"  skipped: {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
