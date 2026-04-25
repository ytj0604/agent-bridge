#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

from bridge_paths import bin_dir, hook_dir, install_root, libexec_dir, log_root, model_bin_dir, run_root, runtime_config_file, state_root


def check_writable(path: Path) -> tuple[bool, str]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        probe.write_text("ok\n", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True, str(path)
    except Exception as exc:
        return False, f"{path}: {exc}"


def check_executable(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, f"{path}: missing"
    if not path.is_file():
        return False, f"{path}: not a file"
    if not os.access(path, os.X_OK):
        return False, f"{path}: exists but is not executable"
    return True, str(path)


def hook_command_status(path: Path, agent: str) -> tuple[bool, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, f"{path}: {exc}"
    needle = f"--agent {agent}"
    found_legacy = False
    for blocks in (data.get("hooks") or {}).values():
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            for item in (block.get("hooks") or []) if isinstance(block, dict) else []:
                command = str(item.get("command") or "") if isinstance(item, dict) else ""
                if needle not in command:
                    continue
                if "bridge-hook" in command:
                    return True, f"{path}: stable bridge-hook"
                if "bridge_hook_logger" in command:
                    found_legacy = True
    if found_legacy:
        return True, f"{path}: legacy direct hook path"
    return False, f"{path}: missing bridge hook"


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Agent Bridge installation.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    checks = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    add("install_root", install_root().exists(), str(install_root()))
    add("bin_dir", bin_dir().exists(), str(bin_dir()))
    add("model_bin_dir", model_bin_dir().exists(), str(model_bin_dir()))
    add("hook_dir", hook_dir().exists(), str(hook_dir()))
    add("libexec_dir", libexec_dir().exists(), str(libexec_dir()))
    add("runtime_config", True, str(runtime_config_file()))
    ok, detail = check_writable(runtime_config_file().parent)
    add("runtime_config_dir", ok, detail)
    for label, path in (("state_dir", state_root()), ("run_dir", run_root()), ("log_dir", log_root())):
        ok, detail = check_writable(path)
        add(label, ok, detail)
    add("python3", shutil.which("python3") is not None, shutil.which("python3") or "not found")
    add("tmux", shutil.which("tmux") is not None, shutil.which("tmux") or "not found")
    add("bridge_run_on_path", shutil.which("bridge_run") is not None, shutil.which("bridge_run") or "not found")
    add("bridge_manage_on_path", shutil.which("bridge_manage") is not None, shutil.which("bridge_manage") or "not found")
    add("bridge_healthcheck_on_path", shutil.which("bridge_healthcheck") is not None, shutil.which("bridge_healthcheck") or "not found")
    add("agent_send_peer_on_path", shutil.which("agent_send_peer") is not None, shutil.which("agent_send_peer") or "not found")
    add("agent_list_peers_on_path", shutil.which("agent_list_peers") is not None, shutil.which("agent_list_peers") or "not found")
    add("agent_view_peer_on_path", shutil.which("agent_view_peer") is not None, shutil.which("agent_view_peer") or "not found")
    add("agent_alarm_on_path", shutil.which("agent_alarm") is not None, shutil.which("agent_alarm") or "not found")
    add("agent_interrupt_peer_on_path", shutil.which("agent_interrupt_peer") is not None, shutil.which("agent_interrupt_peer") or "not found")
    add("agent_extend_wait_on_path", shutil.which("agent_extend_wait") is not None, shutil.which("agent_extend_wait") or "not found")
    add("bridge_peer_not_on_path", shutil.which("bridge_peer") is None, shutil.which("bridge_peer") or "not found")
    add("list_peer_not_on_path", shutil.which("list_peer") is None, shutil.which("list_peer") or "not found")
    executable_targets = [
        ("bridge_run_target", bin_dir() / "bridge_run.sh"),
        ("bridge_manage_target", bin_dir() / "bridge_manage.sh"),
        ("bridge_healthcheck_target", bin_dir() / "bridge_healthcheck.sh"),
        ("agent_send_peer_model_tool", model_bin_dir() / "agent_send_peer"),
        ("agent_list_peers_model_tool", model_bin_dir() / "agent_list_peers"),
        ("agent_view_peer_model_tool", model_bin_dir() / "agent_view_peer"),
        ("agent_alarm_model_tool", model_bin_dir() / "agent_alarm"),
        ("agent_interrupt_peer_model_tool", model_bin_dir() / "agent_interrupt_peer"),
        ("agent_extend_wait_model_tool", model_bin_dir() / "agent_extend_wait"),
        ("bridge_hook_entrypoint", hook_dir() / "bridge-hook"),
    ]
    for label, path in executable_targets:
        ok, detail = check_executable(path)
        add(label, ok, detail)
    ok, detail = hook_command_status(Path.home() / ".claude" / "settings.json", "claude")
    add("claude_hooks", ok, detail)
    ok, detail = hook_command_status(Path.home() / ".codex" / "hooks.json", "codex")
    add("codex_hooks", ok, detail)

    if args.json:
        print(json.dumps(checks, ensure_ascii=True, indent=2))
    else:
        for check in checks:
            status = "ok" if check["ok"] else "fail"
            print(f"{status:4} {check['name']}: {check['detail']}")

    hard_failures = {
        "install_root",
        "bin_dir",
        "model_bin_dir",
        "hook_dir",
        "libexec_dir",
        "runtime_config_dir",
        "state_dir",
        "run_dir",
        "log_dir",
        "python3",
        "tmux",
        "agent_send_peer_on_path",
        "agent_list_peers_on_path",
        "agent_view_peer_on_path",
        "agent_alarm_on_path",
        "agent_interrupt_peer_on_path",
        "agent_extend_wait_on_path",
        "bridge_peer_not_on_path",
        "list_peer_not_on_path",
        "bridge_run_target",
        "bridge_manage_target",
        "bridge_healthcheck_target",
        "agent_send_peer_model_tool",
        "agent_list_peers_model_tool",
        "agent_view_peer_model_tool",
        "agent_alarm_model_tool",
        "agent_interrupt_peer_model_tool",
        "agent_extend_wait_model_tool",
        "bridge_hook_entrypoint",
    }
    return 1 if any((not c["ok"]) and c["name"] in hard_failures for c in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
