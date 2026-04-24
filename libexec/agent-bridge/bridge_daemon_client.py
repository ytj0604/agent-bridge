#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess

from bridge_paths import libexec_dir, python_exe
from bridge_participants import pid_probe_namespace_untrusted, room_status


def auto_restart_enabled() -> bool:
    raw = os.environ.get("AGENT_BRIDGE_AUTO_RESTART", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def ensure_daemon_running(session: str) -> str:
    # Fast path must be read-only: listing/sending should not take daemon locks
    # when the daemon is alive, or when PID namespace isolation makes host PID
    # liveness unverifiable from a model sandbox.
    status = room_status(session)
    if status.state in {"alive", "unknown"}:
        return ""
    if pid_probe_namespace_untrusted():
        return (
            f"{status.reason}. This command is running inside a sandbox/PID namespace, "
            "so it will not auto-start a bridge daemon that would lack host tmux access. "
            "Restart or reattach the bridge room from the host tmux shell."
        )
    if not auto_restart_enabled():
        return ""
    proc = subprocess.run(
        [
            python_exe(),
            str(libexec_dir() / "bridge_daemon_ctl.py"),
            "ensure",
            "-s",
            session,
            "--quiet",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode == 0:
        return ""
    return (proc.stderr or proc.stdout or f"bridge daemon ensure failed with exit code {proc.returncode}").strip()
