#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


APP_NAME = os.environ.get("AGENT_BRIDGE_APP_NAME", "agent-bridge")


def _expand(value: str) -> Path:
    return Path(value).expanduser().resolve()


def _can_write_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".write-test-{os.getpid()}"
        probe.write_text("ok\n", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _user_state_base() -> Path:
    candidates: list[Path] = []
    if os.environ.get("XDG_STATE_HOME"):
        candidates.append(_expand(os.environ["XDG_STATE_HOME"]) / APP_NAME)
    candidates.append(Path.home().expanduser() / ".local" / "state" / APP_NAME)
    uid = getattr(os, "getuid", lambda: "user")()
    candidates.append(Path(tempfile.gettempdir()) / f"{APP_NAME}-{uid}")
    for candidate in candidates:
        if _can_write_dir(candidate):
            return candidate.resolve()
    return candidates[-1].resolve()


def _runtime_dir(env_name: str, legacy_name: str, fallback_subdir: str | None = None) -> Path:
    override = os.environ.get(env_name)
    if override:
        return _expand(override)
    legacy = install_root() / legacy_name
    if _can_write_dir(legacy):
        return legacy
    base = _user_state_base()
    return base if fallback_subdir is None else base / fallback_subdir


def install_root() -> Path:
    override = os.environ.get("AGENT_BRIDGE_HOME")
    if override:
        return _expand(override)
    return Path(__file__).resolve().parents[2]


def bin_dir() -> Path:
    return install_root() / "bin"


def model_bin_dir() -> Path:
    return install_root() / "model-bin"


def hook_dir() -> Path:
    return install_root() / "hooks"


def libexec_dir() -> Path:
    return install_root() / "libexec" / "agent-bridge"


def state_root() -> Path:
    return _runtime_dir("AGENT_BRIDGE_STATE_DIR", "state")


def run_root() -> Path:
    override = os.environ.get("AGENT_BRIDGE_RUN_DIR")
    if override:
        return _expand(override)
    legacy = install_root() / "run"
    if _can_write_dir(legacy):
        return legacy
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime:
        runtime = _expand(xdg_runtime) / APP_NAME
        if _can_write_dir(runtime):
            return runtime
    return state_root() / "run"


def log_root() -> Path:
    return _runtime_dir("AGENT_BRIDGE_LOG_DIR", "log", "log")


def python_exe() -> str:
    return os.environ.get("AGENT_BRIDGE_PYTHON") or sys.executable or "python3"


def script_path(name: str) -> Path:
    return bin_dir() / name


def internal_path(name: str) -> Path:
    return libexec_dir() / name


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"install", "bin", "model-bin", "hook", "libexec", "state", "run", "log"}:
        print("usage: bridge_paths.py install|bin|model-bin|hook|libexec|state|run|log", file=sys.stderr)
        return 2
    key = sys.argv[1]
    paths = {
        "install": install_root,
        "bin": bin_dir,
        "model-bin": model_bin_dir,
        "hook": hook_dir,
        "libexec": libexec_dir,
        "state": state_root,
        "run": run_root,
        "log": log_root,
    }
    print(paths[key]())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
