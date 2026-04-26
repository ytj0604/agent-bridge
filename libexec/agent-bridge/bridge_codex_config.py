#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from bridge_util import write_json_atomic

MARKER_VERSION = 1
CODEX_HOOKS_FLAG = "features.codex_hooks"
DISABLE_PASTE_FLAG = "top.disable_paste_burst"
KNOWN_FLAGS = {CODEX_HOOKS_FLAG, DISABLE_PASTE_FLAG}


def normalize_config_path(path: Path) -> str:
    return str(path.expanduser().resolve(strict=False))


def managed_marker_path(config_path: Path) -> Path:
    return Path(normalize_config_path(config_path)).parent / ".bridge_managed.json"


def read_codex_config(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


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


def _bool_key_in_range(lines: list[str], start: int, end: int, key: str) -> tuple[int, re.Match[str], str] | None:
    for idx in range(start, end):
        matched = _assignment_match(lines[idx], key)
        if not matched:
            continue
        match, ending = matched
        return idx, match, ending
    return None


def _set_bool_key_in_range(
    lines: list[str],
    start: int,
    end: int,
    key: str,
) -> tuple[str, str | None]:
    found = _bool_key_in_range(lines, start, end, key)
    if not found:
        return "missing", None
    idx, match, ending = found
    if match.group(4).strip() == "true":
        return "already", None
    original_line = lines[idx]
    lines[idx] = f"{match.group(1)}{key}{match.group(3)}true{match.group(5)}{ending}"
    return "updated", original_line


def _remove_bool_key_in_range(lines: list[str], start: int, end: int, key: str) -> tuple[str, str | None]:
    found = _bool_key_in_range(lines, start, end, key)
    if not found:
        return "missing", None
    idx, match, _ending = found
    if match.group(4).strip() != "true":
        return "changed", match.group(4).strip()
    removed = lines.pop(idx)
    return "removed", removed


def _replace_bool_key_in_range(
    lines: list[str],
    start: int,
    end: int,
    key: str,
    original_line: str,
) -> tuple[str, str | None]:
    found = _bool_key_in_range(lines, start, end, key)
    if not found:
        return "missing", None
    idx, match, _ending = found
    if match.group(4).strip() != "true":
        return "changed", match.group(4).strip()
    lines[idx] = original_line
    return "restored", None


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


def _feature_section_is_blank(lines: list[str], start: int, end: int) -> bool:
    return all(not line.strip() for line in lines[start + 1:end])


def _remove_feature_section(lines: list[str]) -> None:
    section = _table_section(lines, "features")
    if section is None:
        return
    start, end = section
    if _feature_section_is_blank(lines, start, end):
        del lines[start:end]


def _load_marker(marker_path: Path) -> dict[str, Any] | None:
    try:
        text = marker_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SystemExit(f"agent-bridge: cannot read managed marker {marker_path}: {exc}; refusing to restore.")
    except UnicodeDecodeError as exc:
        raise SystemExit(
            f"agent-bridge: invalid managed marker {marker_path}: cannot decode UTF-8 at byte {exc.start}; "
            "refusing to restore."
        )
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"agent-bridge: invalid managed marker {marker_path}: line {exc.lineno} column {exc.colno}: {exc.msg}; "
            "refusing to restore."
        )
    if not isinstance(data, dict):
        raise SystemExit(f"agent-bridge: invalid managed marker {marker_path}: expected JSON object; refusing to restore.")
    return data


def _validate_marker(data: dict[str, Any], marker_path: Path, config_path: Path) -> dict[str, Any]:
    version = data.get("version")
    if version != MARKER_VERSION:
        raise SystemExit(
            f"agent-bridge: unsupported marker schema version {version}; refusing to restore. "
            f"Manually inspect {marker_path} or remove it."
        )
    marker_config = data.get("codex_config")
    if not isinstance(marker_config, str):
        raise SystemExit(f"agent-bridge: invalid managed marker {marker_path}: missing codex_config; refusing to restore.")
    expected = normalize_config_path(config_path)
    actual = normalize_config_path(Path(marker_config))
    if actual != expected:
        raise SystemExit(
            f"agent-bridge: managed marker {marker_path} belongs to {actual}, not {expected}; refusing to restore."
        )
    flags = data.get("flags")
    if not isinstance(flags, dict):
        raise SystemExit(f"agent-bridge: invalid managed marker {marker_path}: flags must be an object; refusing to restore.")
    for flag_key, raw_entry in flags.items():
        if flag_key not in KNOWN_FLAGS:
            raise SystemExit(
                f"agent-bridge: invalid managed marker {marker_path}: unknown flag key {flag_key!r}; refusing to restore."
            )
        if not isinstance(raw_entry, dict):
            raise SystemExit(
                f"agent-bridge: invalid managed marker {marker_path}: marker {flag_key} entry must be an object; "
                "refusing to restore."
            )
        operation = raw_entry.get("operation")
        if operation not in {"inserted", "updated"}:
            raise SystemExit(
                f"agent-bridge: invalid managed marker {marker_path}: unsupported marker operation {operation!r} "
                f"for {flag_key}; refusing to restore."
            )
        if operation == "updated":
            original_line = raw_entry.get("original_line")
            if not isinstance(original_line, str) or not original_line:
                raise SystemExit(
                    f"agent-bridge: invalid managed marker {marker_path}: marker {flag_key} 'updated' entry "
                    "missing/non-string original_line; refusing to restore."
                )
        if operation == "inserted" and flag_key == CODEX_HOOKS_FLAG and not isinstance(raw_entry.get("section_inserted"), bool):
            raw_entry["section_inserted"] = False
    return data


def load_existing_marker(config_path: Path) -> dict[str, Any] | None:
    marker_path = managed_marker_path(config_path)
    data = _load_marker(marker_path)
    if data is None:
        return None
    return _validate_marker(data, marker_path, config_path)


def _new_marker(config_path: Path, *, config_existed: bool) -> dict[str, Any]:
    return {
        "version": MARKER_VERSION,
        "codex_config": normalize_config_path(config_path),
        "config_existed": bool(config_existed),
        "flags": {},
    }


def _write_marker(marker_path: Path, marker: dict[str, Any]) -> None:
    # install.sh does not serialize concurrent invocations; if operators run two
    # installs at once this marker is last-write-wins, matching the surrounding
    # config writes.
    try:
        write_json_atomic(marker_path, marker, ensure_ascii=False)
    except OSError as exc:
        raise SystemExit(f"agent-bridge: failed to write managed marker {marker_path}: {exc}")


def _record_marker_entry(marker: dict[str, Any], flag_key: str, entry: dict[str, Any]) -> None:
    flags = marker.setdefault("flags", {})
    if not isinstance(flags, dict):
        raise SystemExit("agent-bridge: invalid in-memory managed marker: flags must be an object")
    # Existing marker entries are the authoritative pre-Bridge state. Reinstall
    # may re-force a key to true, but it must not replace the original value
    # needed for clean uninstall.
    if flag_key not in flags:
        flags[flag_key] = entry


# This is a small scoped TOML text editor for two Codex booleans, not a full
# TOML writer. It intentionally ignores inline tables (`features = { ... }`)
# and quoted keys (`"codex_hooks" = true`, `'disable_paste_burst' = true`)
# rather than guessing at rewrites.
def ensure_codex_config_flags(path: Path, dry_run: bool) -> list[str]:
    config_existed = path.exists()
    lines = read_codex_config(path).splitlines(keepends=True)
    original_lines = list(lines)
    marker_path = managed_marker_path(path)
    marker = load_existing_marker(path) or _new_marker(path, config_existed=config_existed)

    actions = []
    section = _table_section(lines, "features")
    if section is not None:
        start, end = section
        action, original_line = _set_bool_key_in_range(lines, start + 1, end, "codex_hooks")
        if action == "already":
            actions.append(f"{path}: codex_hooks already enabled")
        elif action == "updated":
            _record_marker_entry(marker, CODEX_HOOKS_FLAG, {"operation": "updated", "original_line": original_line})
            actions.append(f"{path}: codex_hooks enabled")
        else:
            _insert_line(lines, end, "codex_hooks = true\n")
            _record_marker_entry(marker, CODEX_HOOKS_FLAG, {"operation": "inserted", "section_inserted": False})
            actions.append(f"{path}: codex_hooks enabled")
    else:
        _append_features_section(lines)
        _record_marker_entry(marker, CODEX_HOOKS_FLAG, {"operation": "inserted", "section_inserted": True})
        actions.append(f"{path}: codex_hooks enabled")

    top_level_end = _first_table_index(lines)
    action, original_line = _set_bool_key_in_range(lines, 0, top_level_end, "disable_paste_burst")
    if action == "already":
        actions.append(f"{path}: disable_paste_burst already enabled")
    elif action == "updated":
        _record_marker_entry(marker, DISABLE_PASTE_FLAG, {"operation": "updated", "original_line": original_line})
        actions.append(f"{path}: disable_paste_burst enabled")
    else:
        _insert_line(lines, top_level_end, "disable_paste_burst = true\n")
        _record_marker_entry(marker, DISABLE_PASTE_FLAG, {"operation": "inserted"})
        actions.append(f"{path}: disable_paste_burst enabled")

    if not dry_run and lines != original_lines:
        flags = marker.get("flags") if isinstance(marker.get("flags"), dict) else {}
        if flags:
            _write_marker(marker_path, marker)
        # If this write fails after the marker was written, the stale marker is
        # safe: uninstall only restores keys whose current scoped value is still
        # the bridge-managed true value, and skips otherwise.
        write_codex_config(path, "".join(lines), dry_run)
    return actions


def preflight_codex_restore(path: Path) -> dict[str, Any] | None:
    return load_existing_marker(path)


def _restore_updated_flag(
    lines: list[str],
    flag_key: str,
    entry: dict[str, Any],
) -> tuple[str, str]:
    original_line = entry.get("original_line")
    if not isinstance(original_line, str):
        return "skip", f"skipping {flag_key} restore: marker missing original_line"
    if flag_key == CODEX_HOOKS_FLAG:
        section = _table_section(lines, "features")
        if section is None:
            return "skip", "skipping codex_hooks restore: [features] section missing"
        status, current = _replace_bool_key_in_range(lines, section[0] + 1, section[1], "codex_hooks", original_line)
        label = "codex_hooks"
    elif flag_key == DISABLE_PASTE_FLAG:
        top_end = _first_table_index(lines)
        status, current = _replace_bool_key_in_range(lines, 0, top_end, "disable_paste_burst", original_line)
        label = "disable_paste_burst"
    else:
        raise SystemExit(f"agent-bridge: invalid managed marker: unknown flag key {flag_key!r}; refusing to restore.")
    if status == "restored":
        return "changed", f"restored {label} from managed marker"
    if status == "changed":
        return "skip", f"skipping {label} restore: current value is {current}, expected true"
    return "skip", f"skipping {label} restore: current key is missing"


def _restore_inserted_flag(
    lines: list[str],
    flag_key: str,
    entry: dict[str, Any],
) -> tuple[str, str]:
    if flag_key == CODEX_HOOKS_FLAG:
        section = _table_section(lines, "features")
        if section is None:
            return "skip", "skipping codex_hooks removal: [features] section missing"
        status, current = _remove_bool_key_in_range(lines, section[0] + 1, section[1], "codex_hooks")
        if status == "removed":
            if bool(entry.get("section_inserted")):
                _remove_feature_section(lines)
            return "changed", "removed managed codex_hooks setting"
        if status == "changed":
            return "skip", f"skipping codex_hooks removal: current value is {current}, expected true"
        return "skip", "skipping codex_hooks removal: current key is missing"
    if flag_key == DISABLE_PASTE_FLAG:
        top_end = _first_table_index(lines)
        status, current = _remove_bool_key_in_range(lines, 0, top_end, "disable_paste_burst")
        if status == "removed":
            return "changed", "removed managed disable_paste_burst setting"
        if status == "changed":
            return "skip", f"skipping disable_paste_burst removal: current value is {current}, expected true"
        return "skip", "skipping disable_paste_burst removal: current key is missing"
    raise SystemExit(f"agent-bridge: invalid managed marker: unknown flag key {flag_key!r}; refusing to restore.")


def restore_codex_config_flags(path: Path, marker: dict[str, Any] | None, dry_run: bool) -> list[str]:
    if marker is None:
        return []
    lines = read_codex_config(path).splitlines(keepends=True)
    original_lines = list(lines)
    actions: list[str] = []
    for flag_key, raw_entry in (marker.get("flags") or {}).items():
        if not isinstance(raw_entry, dict):
            actions.append(f"skipping {flag_key} restore: marker entry is invalid")
            continue
        operation = raw_entry.get("operation")
        if operation == "updated":
            status, action = _restore_updated_flag(lines, str(flag_key), raw_entry)
        elif operation == "inserted":
            status, action = _restore_inserted_flag(lines, str(flag_key), raw_entry)
        else:
            status, action = "skip", f"skipping {flag_key} restore: unsupported marker operation {operation}"
        actions.append(f"{path}: {action}")

    marker_path = managed_marker_path(path)
    text = "".join(lines)
    should_remove_config = bool(marker.get("config_existed") is False and not text.strip())
    if not dry_run:
        if lines != original_lines:
            if should_remove_config:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            else:
                write_codex_config(path, text, dry_run)
        elif should_remove_config and path.exists():
            path.unlink()
        try:
            marker_path.unlink()
        except FileNotFoundError:
            pass
    if should_remove_config:
        actions.append(f"{path}: removed bridge-created Codex config")
    actions.append(f"{marker_path}: removed managed marker" if not dry_run else f"{marker_path}: would remove managed marker")
    return actions
