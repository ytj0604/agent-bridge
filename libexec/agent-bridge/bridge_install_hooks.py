#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from bridge_codex_config import ensure_codex_config_flags
from bridge_util import write_json_atomic

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
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except PermissionError as exc:
        raise SystemExit(f"{path}: permission denied while reading hook config ({exc})")
    except UnicodeDecodeError as exc:
        raise SystemExit(
            f"{path}: invalid JSON hook config: cannot decode UTF-8 at byte {exc.start}; "
            "refusing to overwrite existing file. Fix or move aside the file and rerun."
        )
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"{path}: invalid JSON hook config at line {exc.lineno} column {exc.colno}: {exc.msg}; "
            "refusing to overwrite existing file. Fix or move aside the file and rerun."
        )
    if not isinstance(data, dict):
        raise SystemExit(
            f"{path}: hook config must be a JSON object, got {type(data).__name__}; "
            "refusing to overwrite existing file. Fix or move aside the file and rerun."
        )
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
        actions.extend(ensure_codex_config_flags(Path(args.codex_config), args.dry_run))

    for action in actions:
        print(action)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
