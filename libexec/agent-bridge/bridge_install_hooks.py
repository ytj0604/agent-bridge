#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

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


def write_codex_config(path: Path, text: str, dry_run: bool) -> None:
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    except PermissionError as exc:
        raise SystemExit(f"{path}: permission denied while writing codex config ({exc})")


def _read_codex_config(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _toml_table_header(line: str) -> tuple[str, str] | None:
    stripped = line.lstrip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("[["):
        close = stripped.find("]]", 2)
        if close < 0:
            return None
        kind = "array"
        name = stripped[2:close]
        rest = stripped[close + 2:]
    elif stripped.startswith("["):
        close = stripped.find("]", 1)
        if close < 0:
            return None
        kind = "table"
        name = stripped[1:close]
        rest = stripped[close + 1:]
    else:
        return None
    rest = rest.strip()
    if rest and not rest.startswith("#"):
        return None
    return kind, name


def _is_toml_table_header(line: str) -> bool:
    return _toml_table_header(line) is not None


def _is_table_header(line: str, name: str) -> bool:
    header = _toml_table_header(line)
    return header == ("table", name)


def _first_table_index(lines: list[str]) -> int:
    for idx, line in enumerate(lines):
        if _is_toml_table_header(line):
            return idx
    return len(lines)


def _table_section(lines: list[str], name: str) -> tuple[int, int] | None:
    # If a malformed config repeats [features], the first exact section wins.
    for idx, line in enumerate(lines):
        if not _is_table_header(line, name):
            continue
        end = len(lines)
        for next_idx in range(idx + 1, len(lines)):
            if _is_toml_table_header(lines[next_idx]):
                end = next_idx
                break
        return idx, end
    return None


def _line_ending(line: str) -> tuple[str, str]:
    if line.endswith("\r\n"):
        return line[:-2], "\r\n"
    if line.endswith("\n"):
        return line[:-1], "\n"
    return line, ""


def _assignment_match(line: str, key: str) -> tuple[re.Match[str], str] | None:
    body, ending = _line_ending(line)
    if body.lstrip().startswith("#"):
        return None
    match = re.match(rf"^(\s*)({re.escape(key)})(\s*=\s*)([^#]*?)(\s*(#.*)?)$", body)
    if not match:
        return None
    return match, ending


def _set_bool_key_in_range(lines: list[str], start: int, end: int, key: str) -> str | None:
    for idx in range(start, end):
        matched = _assignment_match(lines[idx], key)
        if not matched:
            continue
        match, ending = matched
        if match.group(4).strip() == "true":
            return "already"
        lines[idx] = f"{match.group(1)}{key}{match.group(3)}true{match.group(5)}{ending}"
        return "updated"
    return None


def _ensure_previous_line_terminated(lines: list[str], insert_at: int) -> None:
    if insert_at > 0 and not lines[insert_at - 1].endswith(("\n", "\r")):
        lines[insert_at - 1] += "\n"


def _insert_line(lines: list[str], insert_at: int, line: str) -> None:
    _ensure_previous_line_terminated(lines, insert_at)
    lines.insert(insert_at, line)


def _append_features_section(lines: list[str]) -> None:
    if lines:
        _ensure_previous_line_terminated(lines, len(lines))
        if lines[-1].strip():
            lines.append("\n")
    lines.extend(["[features]\n", "codex_hooks = true\n"])


# This is a small scoped TOML text editor for two Codex booleans, not a full
# TOML writer. It intentionally ignores inline tables (`features = { ... }`)
# and quoted keys (`"codex_hooks" = true`, `'disable_paste_burst' = true`)
# rather than guessing at rewrites.
def ensure_codex_hooks_feature(path: Path, dry_run: bool) -> str:
    lines = _read_codex_config(path).splitlines(keepends=True)
    section = _table_section(lines, "features")
    if section is not None:
        start, end = section
        action = _set_bool_key_in_range(lines, start + 1, end, "codex_hooks")
        if action == "already":
            return f"{path}: codex_hooks already enabled"
        if action is None:
            _insert_line(lines, end, "codex_hooks = true\n")
    else:
        _append_features_section(lines)

    write_codex_config(path, "".join(lines), dry_run)
    return f"{path}: codex_hooks enabled"


def ensure_disable_paste_burst(path: Path, dry_run: bool) -> str:
    # bridge_run sends the ~1100-char probe prompt via `tmux send-keys -l`; codex's
    # TUI paste-burst heuristic swallows it as a "[Pasted Content N chars]" block
    # and the following Enter fails to submit, so the UserPromptSubmit hook never
    # fires and attach times out. Forcing this off at install time keeps probes submittable.
    lines = _read_codex_config(path).splitlines(keepends=True)
    top_level_end = _first_table_index(lines)
    action = _set_bool_key_in_range(lines, 0, top_level_end, "disable_paste_burst")
    if action == "already":
        return f"{path}: disable_paste_burst already enabled"
    if action is None:
        _insert_line(lines, top_level_end, "disable_paste_burst = true\n")

    write_codex_config(path, "".join(lines), dry_run)
    return f"{path}: disable_paste_burst enabled"


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
        actions.append(ensure_disable_paste_burst(Path(args.codex_config), args.dry_run))

    for action in actions:
        print(action)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
