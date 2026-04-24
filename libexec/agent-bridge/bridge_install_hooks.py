#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path

from bridge_util import read_json, write_json_atomic

CLAUDE_EVENTS = {
    "SessionStart": None,
    "UserPromptSubmit": None,
    "PermissionRequest": None,
    "Notification": "permission_prompt|idle_prompt",
    "Stop": None,
    "SessionEnd": None,
}

CODEX_EVENTS = {
    "SessionStart": None,
    "UserPromptSubmit": None,
    "PermissionRequest": "Bash",
    "Stop": None,
}


def load_json(path: Path) -> dict:
    data = read_json(path, {})
    if not isinstance(data, dict):
        raise SystemExit(f"{path} is not a JSON object")
    return data


def save_config(path: Path, data: dict, dry_run: bool) -> None:
    if dry_run:
        return
    try:
        write_json_atomic(path, data, ensure_ascii=False)
    except PermissionError as exc:
        raise SystemExit(f"{path}: permission denied while writing hook config ({exc})")


def is_bridge_hook(command: str, agent: str) -> bool:
    if f"--agent {agent}" not in command:
        return False
    return "bridge_hook_logger" in command or "bridge-hook" in command or "agent-bridge" in command


def hook_block(command: str, matcher: str | None) -> dict:
    block: dict = {
        "hooks": [
            {
                "type": "command",
                "command": command,
            }
        ]
    }
    if matcher:
        block["matcher"] = matcher
    return block


def ensure_hook(data: dict, event: str, matcher: str | None, command: str, agent: str) -> str:
    hooks = data.setdefault("hooks", {})
    blocks = hooks.setdefault(event, [])
    if not isinstance(blocks, list):
        raise SystemExit(f"hooks.{event} must be a list")

    for block in blocks:
        if not isinstance(block, dict):
            continue
        nested = block.get("hooks")
        if not isinstance(nested, list):
            continue
        for item in nested:
            if not isinstance(item, dict):
                continue
            old = str(item.get("command") or "")
            if is_bridge_hook(old, agent):
                item["type"] = "command"
                item["command"] = command
                if matcher:
                    block["matcher"] = matcher
                return "updated"

    blocks.append(hook_block(command, matcher))
    return "added"


def install_json_hooks(path: Path, events: dict[str, str | None], command: str, agent: str, dry_run: bool) -> list[str]:
    data = load_json(path)
    actions = []
    for event, matcher in events.items():
        action = ensure_hook(data, event, matcher, command, agent)
        actions.append(f"{path}: {event} {action}")
    save_config(path, data, dry_run)
    return actions


def ensure_codex_hooks_feature(path: Path, dry_run: bool) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""

    if re.search(r"(?m)^\s*codex_hooks\s*=\s*true\s*$", text):
        return f"{path}: codex_hooks already enabled"

    if re.search(r"(?m)^\s*codex_hooks\s*=", text):
        new_text = re.sub(r"(?m)^(\s*codex_hooks\s*=\s*).*$", r"\1true", text)
    elif re.search(r"(?m)^\[features\]\s*$", text):
        new_text = re.sub(r"(?m)^(\[features\]\s*)$", "\\1\ncodex_hooks = true", text, count=1)
    else:
        suffix = "" if text.endswith("\n") or not text else "\n"
        new_text = f"{text}{suffix}\n[features]\ncodex_hooks = true\n"

    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(new_text, encoding="utf-8")
            tmp.replace(path)
        except PermissionError as exc:
            raise SystemExit(f"{path}: permission denied while writing codex config ({exc})")
    return f"{path}: codex_hooks enabled"


def main() -> int:
    parser = argparse.ArgumentParser(description="Install Agent Bridge hooks for Claude Code and Codex.")
    parser.add_argument("--hook-command", required=True, help="absolute command path for bridge-hook")
    parser.add_argument("--claude-settings", default=str(Path.home() / ".claude" / "settings.json"))
    parser.add_argument("--codex-hooks", default=str(Path.home() / ".codex" / "hooks.json"))
    parser.add_argument("--codex-config", default=str(Path.home() / ".codex" / "config.toml"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-claude", action="store_true")
    parser.add_argument("--skip-codex", action="store_true")
    args = parser.parse_args()

    actions: list[str] = []
    if not args.skip_claude:
        command = f"{args.hook_command} --agent claude"
        actions.extend(install_json_hooks(Path(args.claude_settings), CLAUDE_EVENTS, command, "claude", args.dry_run))
    if not args.skip_codex:
        command = f"{args.hook_command} --agent codex"
        actions.extend(install_json_hooks(Path(args.codex_hooks), CODEX_EVENTS, command, "codex", args.dry_run))
        actions.append(ensure_codex_hooks_feature(Path(args.codex_config), args.dry_run))

    for action in actions:
        print(action)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
