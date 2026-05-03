#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

import bridge_instructions
import bridge_skill_install
from bridge_identity import backfill_session_process_identities
from bridge_participants import load_session
from bridge_paths import bin_dir, hook_dir, install_root, libexec_dir, log_root, model_bin_dir, run_root, runtime_config_file, state_root

REQUIRED_SKILL_COMMANDS = [
    "agent_send_peer",
    "agent_view_peer",
    "agent_wait_status",
    "agent_aggregate_status",
    "agent_alarm",
    "agent_extend_wait",
    "agent_cancel_message",
    "agent_interrupt_peer",
    "agent_clear_peer",
]


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


def _frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        out[key.strip()] = value.strip()
    return out


def check_skill_source_valid(source: Path | None = None) -> tuple[bool, str]:
    source = source or bridge_skill_install.default_skill_source()
    skill = source / "SKILL.md"
    references = [
        source / "references" / "command-contract.md",
        source / "references" / "recovery.md",
        source / "references" / "anti-patterns.md",
    ]
    try:
        text = skill.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"{skill}: {exc}"
    meta = _frontmatter(text)
    if meta.get("name") != "agent-bridge":
        return False, f"{skill}: frontmatter name must be agent-bridge"
    description = meta.get("description") or ""
    missing_commands = [cmd for cmd in REQUIRED_SKILL_COMMANDS if cmd not in description]
    if missing_commands:
        return False, f"{skill}: description missing trigger commands: {', '.join(missing_commands)}"
    if "agent_list_peers" not in text:
        return False, f"{skill}: missing mandatory agent_list_peers recovery rule"
    for ref in references:
        rel = ref.relative_to(source).as_posix()
        if rel not in text:
            return False, f"{skill}: does not link {rel}"
        if not ref.is_file():
            return False, f"{ref}: missing"
    return True, str(source)


def check_skill_references_in_sync(source: Path | None = None) -> tuple[bool, str]:
    source = source or bridge_skill_install.default_skill_source()
    contract = source / "references" / "command-contract.md"
    try:
        actual = bridge_skill_install.normalize_command_contract_text(contract.read_text(encoding="utf-8"))
    except OSError as exc:
        return False, f"{contract}: {exc}"
    expected = bridge_instructions.model_cheat_sheet_text()
    if actual != expected:
        return False, f"{contract}: differs from bridge_instructions.model_cheat_sheet_text()"
    return True, f"{contract}: in sync"


def check_installed_skill(kind: str, target: Path) -> tuple[bool, str]:
    skill = target / "SKILL.md"
    manifest = bridge_skill_install.manifest_path(target)
    if not target.exists():
        return False, f"{target}: missing skill target"
    if not target.is_dir():
        return False, f"{target}: not a directory"
    if not manifest.is_file():
        return False, f"{manifest}: missing Agent Bridge manifest"
    if not skill.is_file():
        return False, f"{skill}: missing"
    if kind == "codex":
        openai = target / "agents" / "openai.yaml"
        if not bridge_skill_install.codex_openai_allows_implicit_invocation(openai):
            return False, f"{openai}: missing policy.allow_implicit_invocation: true"
        return True, f"{target}: installed; restart Codex to activate"
    return True, f"{target}: installed; active in new or existing Claude Code sessions"


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
    parser.add_argument("--backfill-endpoints", action="store_true", help="repair live endpoint process fingerprints for active rooms")
    parser.add_argument("--session", help="with --backfill-endpoints, limit repair to one bridge room")
    args = parser.parse_args()

    if args.backfill_endpoints:
        root = state_root()
        if args.session:
            sessions = [args.session]
        elif root.exists():
            sessions = sorted(path.name for path in root.iterdir() if path.is_dir() and (path / "session.json").exists())
        else:
            sessions = []
        results = []
        for session in sessions:
            state = load_session(session)
            summary = backfill_session_process_identities(session, state)
            results.append({"session": session, "aliases": summary})
        if args.json:
            print(json.dumps(results, ensure_ascii=True, indent=2))
        else:
            for item in results:
                print(f"endpoint backfill: {item['session']}")
                aliases = item.get("aliases") or {}
                if not aliases:
                    print("  (no active participants)")
                for alias, status in aliases.items():
                    print(f"  {alias}: {status.get('status')} {status.get('reason') or ''}".rstrip())
                print("  note: endpoint backfill only refreshes endpoints with prior live hook evidence; reattach/join with normal probing if no verified prior exists.")
        return 1 if any(
            status.get("status") != "verified"
            for item in results
            for status in (item.get("aliases") or {}).values()
        ) else 0

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
    add("agent_bridge_on_path", shutil.which("agent-bridge") is not None, shutil.which("agent-bridge") or "not found")
    add("bridge_run_on_path", shutil.which("bridge_run") is not None, shutil.which("bridge_run") or "not found")
    add("bridge_manage_on_path", shutil.which("bridge_manage") is not None, shutil.which("bridge_manage") or "not found")
    add("bridge_healthcheck_on_path", shutil.which("bridge_healthcheck") is not None, shutil.which("bridge_healthcheck") or "not found")
    add("agent_send_peer_on_path", shutil.which("agent_send_peer") is not None, shutil.which("agent_send_peer") or "not found")
    add("agent_list_peers_on_path", shutil.which("agent_list_peers") is not None, shutil.which("agent_list_peers") or "not found")
    add("agent_view_peer_on_path", shutil.which("agent_view_peer") is not None, shutil.which("agent_view_peer") or "not found")
    add("agent_alarm_on_path", shutil.which("agent_alarm") is not None, shutil.which("agent_alarm") or "not found")
    add("agent_interrupt_peer_on_path", shutil.which("agent_interrupt_peer") is not None, shutil.which("agent_interrupt_peer") or "not found")
    add("agent_clear_peer_on_path", shutil.which("agent_clear_peer") is not None, shutil.which("agent_clear_peer") or "not found")
    add("agent_extend_wait_on_path", shutil.which("agent_extend_wait") is not None, shutil.which("agent_extend_wait") or "not found")
    add("agent_cancel_message_on_path", shutil.which("agent_cancel_message") is not None, shutil.which("agent_cancel_message") or "not found")
    add("agent_wait_status_on_path", shutil.which("agent_wait_status") is not None, shutil.which("agent_wait_status") or "not found")
    add("agent_aggregate_status_on_path", shutil.which("agent_aggregate_status") is not None, shutil.which("agent_aggregate_status") or "not found")
    add("agent_bridge_underscore_not_on_path", shutil.which("agent_bridge") is None, shutil.which("agent_bridge") or "not found")
    add("bridge_peer_not_on_path", shutil.which("bridge_peer") is None, shutil.which("bridge_peer") or "not found")
    add("list_peer_not_on_path", shutil.which("list_peer") is None, shutil.which("list_peer") or "not found")
    executable_targets = [
        ("agent_bridge_target", bin_dir() / "agent-bridge"),
        ("bridge_run_target", bin_dir() / "bridge_run.sh"),
        ("bridge_manage_target", bin_dir() / "bridge_manage.sh"),
        ("bridge_healthcheck_target", bin_dir() / "bridge_healthcheck.sh"),
        ("agent_send_peer_model_tool", model_bin_dir() / "agent_send_peer"),
        ("agent_list_peers_model_tool", model_bin_dir() / "agent_list_peers"),
        ("agent_view_peer_model_tool", model_bin_dir() / "agent_view_peer"),
        ("agent_alarm_model_tool", model_bin_dir() / "agent_alarm"),
        ("agent_interrupt_peer_model_tool", model_bin_dir() / "agent_interrupt_peer"),
        ("agent_clear_peer_model_tool", model_bin_dir() / "agent_clear_peer"),
        ("agent_extend_wait_model_tool", model_bin_dir() / "agent_extend_wait"),
        ("agent_cancel_message_model_tool", model_bin_dir() / "agent_cancel_message"),
        ("agent_wait_status_model_tool", model_bin_dir() / "agent_wait_status"),
        ("agent_aggregate_status_model_tool", model_bin_dir() / "agent_aggregate_status"),
        ("bridge_hook_entrypoint", hook_dir() / "bridge-hook"),
    ]
    for label, path in executable_targets:
        ok, detail = check_executable(path)
        add(label, ok, detail)
    ok, detail = hook_command_status(Path.home() / ".claude" / "settings.json", "claude")
    add("claude_hooks", ok, detail)
    ok, detail = hook_command_status(Path.home() / ".codex" / "hooks.json", "codex")
    add("codex_hooks", ok, detail)
    ok, detail = check_skill_source_valid()
    add("agent_bridge_skill_source_valid", ok, detail)
    ok, detail = check_skill_references_in_sync()
    add("agent_bridge_skill_references_in_sync", ok, detail)
    ok, detail = check_installed_skill("claude", bridge_skill_install.claude_skill_target())
    add("claude_agent_bridge_skill_installed", ok, detail)
    ok, detail = check_installed_skill("codex", bridge_skill_install.codex_skill_target())
    add("codex_agent_bridge_skill_installed", ok, detail)

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
        "agent_bridge_on_path",
        "agent_send_peer_on_path",
        "agent_list_peers_on_path",
        "agent_view_peer_on_path",
        "agent_alarm_on_path",
        "agent_interrupt_peer_on_path",
        "agent_clear_peer_on_path",
        "agent_extend_wait_on_path",
        "agent_cancel_message_on_path",
        "agent_wait_status_on_path",
        "agent_aggregate_status_on_path",
        "agent_bridge_underscore_not_on_path",
        "bridge_peer_not_on_path",
        "list_peer_not_on_path",
        "agent_bridge_target",
        "bridge_run_target",
        "bridge_manage_target",
        "bridge_healthcheck_target",
        "agent_send_peer_model_tool",
        "agent_list_peers_model_tool",
        "agent_view_peer_model_tool",
        "agent_alarm_model_tool",
        "agent_interrupt_peer_model_tool",
        "agent_clear_peer_model_tool",
        "agent_extend_wait_model_tool",
        "agent_cancel_message_model_tool",
        "agent_wait_status_model_tool",
        "agent_aggregate_status_model_tool",
        "bridge_hook_entrypoint",
        "agent_bridge_skill_source_valid",
        "agent_bridge_skill_references_in_sync",
        "claude_agent_bridge_skill_installed",
        "codex_agent_bridge_skill_installed",
    }
    return 1 if any((not c["ok"]) and c["name"] in hard_failures for c in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
