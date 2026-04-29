#!/usr/bin/env python3
"""Safely force Claude Code editorMode for reliable bridge prompt paste.

Test-only hook: BRIDGE_EDITOR_MODE_TEST_RACE_FILE points at bytes to copy over
the live config immediately before the concurrent-change guard.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any


def display_path(path: Path) -> str:
    return str(path)


def backup_path(path: Path, pid: int) -> Path:
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return path.with_name(f"{path.name}.agent-bridge.bak.{stamp}.{pid}")


def find_nested_editor_modes(value: Any, path: str = "$") -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key == "editorMode" and path != "$":
                found.append(child_path)
            found.extend(find_nested_editor_modes(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(find_nested_editor_modes(child, f"{path}[{index}]"))
    return found


def maybe_trigger_test_race(path: Path) -> None:
    race_file = os.environ.get("BRIDGE_EDITOR_MODE_TEST_RACE_FILE")
    if not race_file:
        return
    path.write_bytes(Path(race_file).read_bytes())


def write_atomic_preserving_mode(path: Path, text: str, *, original_bytes: bytes, backup: Path) -> bool:
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.agent-bridge.tmp.",
            delete=False,
        ) as tmp:
            tmp_name = tmp.name
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
        tmp_path = Path(tmp_name)
        shutil.copymode(path, tmp_path)
        # Preserves mode; assumes the installer runs as the file owner.
        maybe_trigger_test_race(path)
        current_bytes = path.read_bytes()
        if current_bytes != original_bytes:
            return False
        shutil.copy2(path, backup)
        os.replace(tmp_path, path)
        tmp_name = None
        return True
    finally:
        if tmp_name:
            try:
                Path(tmp_name).unlink()
            except FileNotFoundError:
                pass


def update_editor_mode(config_path: Path, *, dry_run: bool) -> int:
    link_path = config_path.expanduser()
    target_path = Path(os.path.realpath(link_path)) if os.path.islink(link_path) else link_path

    try:
        original_bytes = target_path.read_bytes()
    except FileNotFoundError:
        if os.path.islink(link_path) and not target_path.exists():
            print(
                f"WARNING: {display_path(link_path)}: symlink target is missing at {display_path(target_path)}; skipping editorMode update.",
                file=sys.stderr,
            )
        print(f"skip: Claude Code config absent at {display_path(link_path)}; editorMode unchanged")
        return 0

    try:
        original_text = original_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        print(
            f"WARNING: {display_path(link_path)}: cannot read Claude Code config as UTF-8 ({exc}); skipping editorMode update.",
            file=sys.stderr,
        )
        print(f"skip: Claude Code config unreadable at {display_path(link_path)}; editorMode unchanged")
        return 0

    try:
        data = json.loads(original_text)
    except json.JSONDecodeError as exc:
        print(
            f"WARNING: {display_path(link_path)}: cannot parse Claude Code config JSON ({exc}); skipping editorMode update.",
            file=sys.stderr,
        )
        print(f"skip: Claude Code config parse failed at {display_path(link_path)}; editorMode unchanged")
        return 0

    if not isinstance(data, dict):
        print(
            f"WARNING: {display_path(link_path)}: Claude Code config JSON root is not an object; skipping editorMode update.",
            file=sys.stderr,
        )
        print(f"skip: Claude Code config root is not an object at {display_path(link_path)}; editorMode unchanged")
        return 0

    nested_paths = find_nested_editor_modes(data)
    if nested_paths:
        print(
            f"WARNING: {display_path(link_path)}: nested editorMode key(s) found at {', '.join(nested_paths)}.",
            file=sys.stderr,
        )

    if data.get("editorMode") == "normal":
        print(f"skip: Claude Code editorMode already normal in {display_path(link_path)}")
        return 0

    if "editorMode" not in data and nested_paths:
        print(f"skip: root editorMode absent and nested editorMode found in {display_path(link_path)}; editorMode unchanged")
        return 0

    prior = data.get("editorMode")
    action = "add" if "editorMode" not in data else "set"
    data["editorMode"] = "normal"
    prior_note = "" if action == "add" else f" (was {json.dumps(prior, ensure_ascii=False)})"
    normalize_note = "JSON whitespace/escape style normalized"
    if dry_run:
        print(
            f"dry-run: would {action} Claude Code editorMode=normal for reliable bridge prompt paste "
            f"in {display_path(link_path)}{prior_note}; {normalize_note}"
        )
        return 0

    updated_text = json.dumps(data, ensure_ascii=False, indent=2, separators=(",", ": "))
    if original_bytes.endswith(b"\n"):
        updated_text += "\n"
    backup = backup_path(target_path, os.getpid())
    changed = write_atomic_preserving_mode(target_path, updated_text, original_bytes=original_bytes, backup=backup)
    if not changed:
        print(
            f"WARNING: concurrent change detected at {display_path(link_path)}; editorMode unchanged. "
            "Re-run install.sh after closing Claude Code.",
            file=sys.stderr,
        )
        print(f"skip: concurrent change detected at {display_path(link_path)}; editorMode unchanged")
        return 0
    print(
        f"{action} Claude Code editorMode=normal for reliable bridge prompt paste "
        f"in {display_path(link_path)}{prior_note}; backup: {display_path(backup)}; {normalize_note}"
    )
    print("note: restart active Claude Code sessions if they do not pick up editorMode changes.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Set Claude Code editorMode to normal safely.")
    parser.add_argument("--path", default=str(Path.home() / ".claude.json"), help="Claude Code config path")
    parser.add_argument("--dry-run", action="store_true", help="preview changes without writing")
    args = parser.parse_args(argv)
    try:
        return update_editor_mode(Path(args.path), dry_run=args.dry_run)
    except OSError as exc:
        print(f"ERROR: failed to update Claude Code editorMode: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
