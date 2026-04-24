#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import tempfile
import json
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


def _config_base() -> Path:
    override = os.environ.get("AGENT_BRIDGE_CONFIG_DIR")
    if override:
        return _expand(override)
    if os.environ.get("XDG_CONFIG_HOME"):
        return _expand(os.environ["XDG_CONFIG_HOME"]) / APP_NAME
    return Path.home().expanduser() / ".config" / APP_NAME


def runtime_config_file() -> Path:
    override = os.environ.get("AGENT_BRIDGE_RUNTIME_FILE")
    if override:
        return _expand(override)
    return _config_base() / "runtime.json"


def _read_runtime_config() -> dict:
    path = runtime_config_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_runtime_config(data: dict) -> None:
    path = runtime_config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _pinned_path(key: str) -> Path | None:
    raw = (_read_runtime_config().get("runtime") or {}).get(key)
    if not raw:
        return None
    try:
        return _expand(str(raw))
    except OSError:
        return None


def _user_state_base() -> Path:
    uid = getattr(os, "getuid", lambda: "user")()
    candidates: list[Path] = [
        Path(tempfile.gettempdir()) / f"{APP_NAME}-{uid}",
    ]
    if os.environ.get("XDG_STATE_HOME"):
        candidates.append(_expand(os.environ["XDG_STATE_HOME"]) / APP_NAME)
    candidates.append(Path.home().expanduser() / ".local" / "state" / APP_NAME)
    for candidate in candidates:
        if _can_write_dir(candidate):
            return candidate.resolve()
    return candidates[-1].resolve()


def _allow_install_root_runtime() -> bool:
    raw = os.environ.get("AGENT_BRIDGE_ALLOW_INSTALL_ROOT_RUNTIME", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _is_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False
    except OSError:
        return False


def _is_install_root_runtime(path: Path) -> bool:
    return _is_under(path, install_root())


def _portable_runtime_root() -> Path:
    override = os.environ.get("AGENT_BRIDGE_RUNTIME_DIR")
    if override:
        return _expand(override)
    uid = getattr(os, "getuid", lambda: "user")()
    return (Path(tempfile.gettempdir()) / f"{APP_NAME}-{uid}").resolve()


def _default_runtime_root(kind: str) -> Path:
    root = _portable_runtime_root()
    if kind == "state_root":
        return root / "state"
    if kind == "run_root":
        return root / "run"
    if kind == "log_root":
        return root / "log"
    raise ValueError(f"unknown runtime root kind: {kind}")


def _usable_pinned_path(key: str) -> Path | None:
    pinned = _pinned_path(key)
    if pinned is None:
        return None
    if _is_install_root_runtime(pinned) and not _allow_install_root_runtime():
        return None
    return pinned


def _runtime_dir(
    env_name: str,
    legacy_name: str,
    fallback_subdir: str | None = None,
    *,
    pin_key: str,
) -> Path:
    override = os.environ.get(env_name)
    if override:
        return _expand(override)
    if os.environ.get("AGENT_BRIDGE_RUNTIME_DIR"):
        return _default_runtime_root(pin_key)
    pinned = _usable_pinned_path(pin_key)
    if pinned is not None:
        return pinned
    portable = _default_runtime_root(pin_key)
    if _can_write_dir(portable):
        return portable
    legacy = install_root() / legacy_name
    if _allow_install_root_runtime() and _can_write_dir(legacy):
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
    return _runtime_dir("AGENT_BRIDGE_STATE_DIR", "state", pin_key="state_root")


def run_root() -> Path:
    override = os.environ.get("AGENT_BRIDGE_RUN_DIR")
    if override:
        return _expand(override)
    if os.environ.get("AGENT_BRIDGE_RUNTIME_DIR"):
        return _default_runtime_root("run_root")
    pinned = _usable_pinned_path("run_root")
    if pinned is not None:
        return pinned
    portable = _default_runtime_root("run_root")
    if _can_write_dir(portable):
        return portable
    legacy = install_root() / "run"
    if _allow_install_root_runtime() and _can_write_dir(legacy):
        return legacy
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime:
        runtime = _expand(xdg_runtime) / APP_NAME
        if _can_write_dir(runtime):
            return runtime
    state_run = state_root() / "run"
    if _can_write_dir(state_run):
        return state_run
    return _user_state_base() / "run"


def log_root() -> Path:
    return _runtime_dir("AGENT_BRIDGE_LOG_DIR", "log", "log", pin_key="log_root")


def require_writable_dir(path: Path, label: str) -> None:
    if _can_write_dir(path):
        return
    raise RuntimeError(
        f"Agent Bridge {label} is not writable: {path}. "
        "Fix the directory permissions, or stop/recreate the bridge room with AGENT_BRIDGE_RUNTIME_DIR (or AGENT_BRIDGE_STATE_DIR/AGENT_BRIDGE_RUN_DIR/AGENT_BRIDGE_LOG_DIR) pointing to writable paths shared by the agents."
    )


def ensure_runtime_writable() -> None:
    require_writable_dir(state_root(), "state_root")
    require_writable_dir(run_root(), "run_root")
    require_writable_dir(log_root(), "log_root")


def pin_runtime_roots() -> dict:
    roots = {
        "state_root": str(state_root()),
        "run_root": str(run_root()),
        "log_root": str(log_root()),
    }
    for label, raw in roots.items():
        require_writable_dir(Path(raw), label)
    config = _read_runtime_config()
    if (config.get("runtime") or {}) == roots:
        return config
    config["version"] = 1
    config["install_root"] = str(install_root())
    config["runtime"] = roots
    try:
        _write_runtime_config(config)
    except OSError as exc:
        raise RuntimeError(
            f"Agent Bridge cannot write runtime config: {runtime_config_file()}: {exc}. "
            "Fix permissions or set AGENT_BRIDGE_RUNTIME_FILE to a writable config path."
        ) from exc
    return config


def python_exe() -> str:
    return os.environ.get("AGENT_BRIDGE_PYTHON") or sys.executable or "python3"


def script_path(name: str) -> Path:
    return bin_dir() / name


def internal_path(name: str) -> Path:
    return libexec_dir() / name


def main() -> int:
    keys = {"install", "bin", "model-bin", "hook", "libexec", "state", "run", "log", "runtime-config"}
    if len(sys.argv) != 2 or sys.argv[1] not in keys:
        print("usage: bridge_paths.py install|bin|model-bin|hook|libexec|state|run|log|runtime-config", file=sys.stderr)
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
        "runtime-config": runtime_config_file,
    }
    print(paths[key]())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
