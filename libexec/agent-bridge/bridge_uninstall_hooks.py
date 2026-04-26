#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from bridge_codex_config import preflight_codex_restore, restore_codex_config_flags
from bridge_util import read_json, write_json_atomic


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


def is_bridge_command(command: str, agent: str | None) -> bool:
    if agent and f"--agent {agent}" not in command:
        return False
    return "bridge-hook" in command or "bridge_hook_logger" in command or "agent-bridge" in command


def remove_hooks(path: Path, agent: str | None, dry_run: bool) -> tuple[int, int]:
    data = load_json(path)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return 0, 0

    removed = 0
    removed_blocks = 0
    for event in list(hooks):
        blocks = hooks.get(event)
        if not isinstance(blocks, list):
            continue
        kept_blocks = []
        for block in blocks:
            if not isinstance(block, dict):
                kept_blocks.append(block)
                continue
            nested = block.get("hooks")
            if not isinstance(nested, list):
                kept_blocks.append(block)
                continue
            kept_nested = []
            for item in nested:
                command = str(item.get("command") or "") if isinstance(item, dict) else ""
                if is_bridge_command(command, agent):
                    removed += 1
                else:
                    kept_nested.append(item)
            if kept_nested:
                block["hooks"] = kept_nested
                kept_blocks.append(block)
            else:
                removed_blocks += 1
        if kept_blocks:
            hooks[event] = kept_blocks
        else:
            del hooks[event]
    if not hooks:
        data.pop("hooks", None)
    save_config(path, data, dry_run)
    return removed, removed_blocks


def main() -> int:
    parser = argparse.ArgumentParser(description="Remove Agent Bridge hooks from Claude/Codex config.")
    parser.add_argument("--claude-settings", default=str(Path.home() / ".claude" / "settings.json"))
    parser.add_argument("--codex-hooks", default=str(Path.home() / ".codex" / "hooks.json"))
    parser.add_argument("--codex-config", default=str(Path.home() / ".codex" / "config.toml"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-claude", action="store_true")
    parser.add_argument("--skip-codex", action="store_true")
    args = parser.parse_args()

    codex_marker = None
    if not args.skip_codex:
        codex_marker = preflight_codex_restore(Path(args.codex_config))

    if not args.skip_claude:
        removed, blocks = remove_hooks(Path(args.claude_settings), "claude", args.dry_run)
        print(f"{args.claude_settings}: removed {removed} hook command(s), {blocks} empty block(s)")
    if not args.skip_codex:
        removed, blocks = remove_hooks(Path(args.codex_hooks), "codex", args.dry_run)
        print(f"{args.codex_hooks}: removed {removed} hook command(s), {blocks} empty block(s)")
        for action in restore_codex_config_flags(Path(args.codex_config), codex_marker, args.dry_run):
            print(action)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
