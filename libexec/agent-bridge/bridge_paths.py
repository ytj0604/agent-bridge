#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path


APP_NAME = os.environ.get("AGENT_BRIDGE_APP_NAME", "agent-bridge")


def _expand(value: str) -> Path:
    return Path(value).expanduser().resolve()


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
    override = os.environ.get("AGENT_BRIDGE_STATE_DIR")
    if override:
        return _expand(override)
    return install_root() / "state"


def run_root() -> Path:
    override = os.environ.get("AGENT_BRIDGE_RUN_DIR")
    if override:
        return _expand(override)
    return install_root() / "run"


def log_root() -> Path:
    override = os.environ.get("AGENT_BRIDGE_LOG_DIR")
    if override:
        return _expand(override)
    return install_root() / "log"


def python_exe() -> str:
    return os.environ.get("AGENT_BRIDGE_PYTHON") or sys.executable or "python3"


def script_path(name: str) -> Path:
    return bin_dir() / name


def internal_path(name: str) -> Path:
    return libexec_dir() / name
