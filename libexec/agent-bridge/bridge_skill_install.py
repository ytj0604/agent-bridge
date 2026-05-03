#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
from typing import Iterable

from bridge_paths import install_root
from bridge_util import write_json_atomic


SKILL_NAME = "agent-bridge"
MANIFEST_NAME = ".agent-bridge-managed.json"
MANIFEST_VERSION = 1
CODEX_OPENAI_REL = PurePosixPath("agents/openai.yaml")
CODEX_OPENAI_YAML = """\
display_name: Agent Bridge
description: Persistent operating guide for Agent Bridge peer messaging.
policy:
  allow_implicit_invocation: true
"""


def default_skill_source() -> Path:
    return install_root() / "skills" / SKILL_NAME


def codex_home() -> Path:
    raw = os.environ.get("CODEX_HOME")
    return Path(raw).expanduser() if raw else Path.home().expanduser() / ".codex"


def codex_skill_target() -> Path:
    return codex_home() / "skills" / SKILL_NAME


def claude_skill_target() -> Path:
    return Path.home().expanduser() / ".claude" / "skills" / SKILL_NAME


def skill_targets() -> dict[str, Path]:
    return {
        "claude": claude_skill_target(),
        "codex": codex_skill_target(),
    }


def manifest_path(target: Path) -> Path:
    return target / MANIFEST_NAME


def _display(path: Path) -> str:
    return str(path.expanduser())


def _relative_key(path: PurePosixPath) -> str:
    return path.as_posix()


def validate_manifest_relative_path(raw: object) -> PurePosixPath:
    if not isinstance(raw, str):
        raise ValueError("manifest path is not a string")
    if raw == "":
        raise ValueError("manifest path is empty")
    if raw.endswith("/"):
        raise ValueError(f"manifest path has trailing slash: {raw!r}")
    if "//" in raw:
        raise ValueError(f"manifest path contains repeated slash: {raw!r}")
    if raw == "." or raw.startswith("./") or "/./" in raw or raw.endswith("/."):
        raise ValueError(f"manifest path contains unsafe component: {raw!r}")
    rel = PurePosixPath(raw)
    if rel.is_absolute():
        raise ValueError(f"manifest path is absolute: {raw!r}")
    if any(part in {"", ".", ".."} for part in rel.parts):
        raise ValueError(f"manifest path contains unsafe component: {raw!r}")
    if rel.as_posix() != raw:
        raise ValueError(f"manifest path is not canonical: {raw!r}")
    return rel


def validate_target_path(target: Path, rel: PurePosixPath, *, must_be_file: bool = False) -> Path:
    candidate = target.joinpath(*rel.parts)
    cursor = target
    if cursor.is_symlink():
        raise ValueError(f"managed target is a symlink: {target}")
    for part in rel.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError(f"manifest path crosses symlink: {rel.as_posix()}")
    try:
        resolved_target = target.resolve(strict=False)
        resolved_candidate = candidate.resolve(strict=False)
        resolved_candidate.relative_to(resolved_target)
    except (OSError, ValueError) as exc:
        raise ValueError(f"manifest path escapes managed target: {rel.as_posix()}") from exc
    if candidate.exists():
        if must_be_file and not candidate.is_file():
            raise ValueError(f"manifest path is not a file: {rel.as_posix()}")
    return candidate


def _reject_existing_symlink_shape(kind: str, target: Path) -> None:
    anchors = {
        "claude": Path.home().expanduser() / ".claude",
        "codex": codex_home(),
    }
    anchor = anchors[kind]
    for path in (anchor, target.parent, target):
        if path.is_symlink():
            raise SystemExit(f"agent-bridge: refusing {kind} skill target with symlinked path component: {path}")
        if path.exists() and not path.is_dir():
            raise SystemExit(f"agent-bridge: refusing {kind} skill target; expected directory path: {path}")


def _target_has_unmanaged_content(target: Path) -> bool:
    if not target.exists() or not target.is_dir():
        return False
    try:
        return any(True for _ in target.iterdir())
    except OSError as exc:
        raise SystemExit(f"agent-bridge: cannot inspect existing skill target {target}: {exc}")


def _preflight_paths(target: Path, rels: Iterable[PurePosixPath], *, must_be_file: bool = False) -> None:
    for rel in rels:
        try:
            validate_target_path(target, rel, must_be_file=must_be_file)
        except ValueError as exc:
            raise SystemExit(f"agent-bridge: unsafe skill path for {target}: {exc}")


def _source_files(source: Path) -> dict[PurePosixPath, Path]:
    if not source.exists():
        raise SystemExit(f"agent-bridge: skill source missing: {source}")
    if not source.is_dir():
        raise SystemExit(f"agent-bridge: skill source is not a directory: {source}")
    files: dict[PurePosixPath, Path] = {}
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise SystemExit(f"agent-bridge: refusing symlink in skill source: {path}")
        if not path.is_file():
            continue
        rel = PurePosixPath(path.relative_to(source).as_posix())
        validate_manifest_relative_path(rel.as_posix())
        files[rel] = path
    if PurePosixPath("SKILL.md") not in files:
        raise SystemExit(f"agent-bridge: skill source missing SKILL.md: {source}")
    return files


def _read_manifest(target: Path) -> dict | None:
    path = manifest_path(target)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"agent-bridge: invalid skill manifest {path}: {exc}")
    if not isinstance(data, dict):
        raise SystemExit(f"agent-bridge: invalid skill manifest {path}: expected object")
    if data.get("version") != MANIFEST_VERSION:
        raise SystemExit(f"agent-bridge: unsupported skill manifest version in {path}: {data.get('version')!r}")
    if data.get("skill") != SKILL_NAME:
        raise SystemExit(f"agent-bridge: manifest {path} is not for {SKILL_NAME}")
    files = data.get("files")
    if not isinstance(files, list):
        raise SystemExit(f"agent-bridge: invalid skill manifest {path}: files must be a list")
    return data


def _manifest_files(target: Path, manifest: dict | None) -> set[PurePosixPath]:
    if not manifest:
        return set()
    out: set[PurePosixPath] = set()
    for raw in manifest.get("files") or []:
        try:
            rel = validate_manifest_relative_path(raw)
            validate_target_path(target, rel, must_be_file=True)
        except ValueError as exc:
            raise SystemExit(f"agent-bridge: unsafe skill manifest {manifest_path(target)}: {exc}")
        out.add(rel)
    return out


def _remove_file(path: Path, dry_run: bool, actions: list[str]) -> None:
    actions.append(f"remove {_display(path)}")
    if not dry_run:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _prune_empty_dirs(target: Path, relative_dirs: Iterable[PurePosixPath], dry_run: bool, actions: list[str]) -> None:
    for rel in sorted(relative_dirs, key=lambda item: len(item.parts), reverse=True):
        if rel == PurePosixPath("."):
            continue
        try:
            path = validate_target_path(target, rel)
        except ValueError as exc:
            raise SystemExit(f"agent-bridge: unsafe skill directory path during prune: {exc}")
        if not path.exists() or not path.is_dir():
            continue
        try:
            next(path.iterdir())
        except StopIteration:
            actions.append(f"remove empty dir {_display(path)}")
            if not dry_run:
                path.rmdir()


def _write_file(path: Path, text: str | bytes, dry_run: bool, actions: list[str]) -> None:
    actions.append(f"write {_display(path)}")
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="utf-8")


def _install_one(kind: str, source_files: dict[PurePosixPath, Path], target: Path, source: Path, dry_run: bool) -> list[str]:
    _reject_existing_symlink_shape(kind, target)
    actions = [f"{kind}: install skill to {_display(target)}"]
    manifest = _read_manifest(target)
    if manifest is None and _target_has_unmanaged_content(target):
        raise SystemExit(
            f"agent-bridge: refusing to overwrite unmanaged existing {kind} skill at {target}; "
            f"remove it or move it aside before installing {SKILL_NAME}."
        )
    previous = _manifest_files(target, manifest)
    desired = dict(source_files)
    if kind == "codex":
        # Implicit invocation is intentional: the skill exists to restore bridge
        # operating knowledge after context compaction without requiring a user
        # to remember an explicit skill name.
        desired[CODEX_OPENAI_REL] = Path()
    _preflight_paths(target, previous, must_be_file=True)
    _preflight_paths(target, desired)
    for rel in sorted(previous - set(desired)):
        _remove_file(validate_target_path(target, rel, must_be_file=True), dry_run, actions)
    for rel, source_path in sorted(desired.items(), key=lambda item: item[0].as_posix()):
        dest = validate_target_path(target, rel)
        if rel == CODEX_OPENAI_REL:
            _write_file(dest, CODEX_OPENAI_YAML, dry_run, actions)
        else:
            _write_file(dest, source_path.read_bytes(), dry_run, actions)
    files = [_relative_key(rel) for rel in sorted(desired)]
    manifest = {
        "version": MANIFEST_VERSION,
        "skill": SKILL_NAME,
        "target_kind": kind,
        "source": str(source.resolve(strict=False)),
        "files": files,
    }
    actions.append(f"write {_display(manifest_path(target))}")
    if not dry_run:
        target.mkdir(parents=True, exist_ok=True)
        write_json_atomic(manifest_path(target), manifest, ensure_ascii=True)
    return actions


def install_skills(source: Path | None = None, *, dry_run: bool = False) -> list[str]:
    source = (source or default_skill_source()).expanduser()
    source_files = _source_files(source)
    actions: list[str] = []
    for kind, target in skill_targets().items():
        actions.extend(_install_one(kind, source_files, target, source, dry_run))
    return actions


def _uninstall_one(kind: str, target: Path, dry_run: bool) -> list[str]:
    _reject_existing_symlink_shape(kind, target)
    manifest = _read_manifest(target)
    actions = [f"{kind}: uninstall skill from {_display(target)}"]
    if manifest is None:
        actions.append(f"skip {_display(target)}: no Agent Bridge manifest")
        return actions
    files = _manifest_files(target, manifest)
    _preflight_paths(target, files, must_be_file=True)
    dirs: set[PurePosixPath] = set()
    for rel in sorted(files, key=lambda item: item.as_posix()):
        _remove_file(validate_target_path(target, rel, must_be_file=True), dry_run, actions)
        parent = rel.parent
        while parent != PurePosixPath("."):
            dirs.add(parent)
            parent = parent.parent
    actions.append(f"remove {_display(manifest_path(target))}")
    if not dry_run:
        manifest_path(target).unlink(missing_ok=True)
    _prune_empty_dirs(target, dirs, dry_run, actions)
    if target.exists() and target.is_dir():
        try:
            next(target.iterdir())
        except StopIteration:
            actions.append(f"remove empty dir {_display(target)}")
            if not dry_run:
                target.rmdir()
    return actions


def uninstall_skills(*, dry_run: bool = False) -> list[str]:
    actions: list[str] = []
    for kind, target in skill_targets().items():
        actions.extend(_uninstall_one(kind, target, dry_run))
    return actions


def command_contract_text(path: Path | None = None) -> str:
    target = path or default_skill_source() / "references" / "command-contract.md"
    return target.read_text(encoding="utf-8")


def normalize_command_contract_text(text: str) -> str:
    return text[:-1] if text.endswith("\n") else text


def codex_openai_allows_implicit_invocation(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return (
        "policy:" in text
        and "allow_implicit_invocation: true" in text
        and "display_name:" in text
        and "description:" in text
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install or remove Agent Bridge skills.")
    sub = parser.add_subparsers(dest="command", required=True)
    install_parser = sub.add_parser("install")
    install_parser.add_argument("--source", type=Path, default=default_skill_source())
    install_parser.add_argument("--dry-run", action="store_true")
    uninstall_parser = sub.add_parser("uninstall")
    uninstall_parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "install":
        actions = install_skills(args.source, dry_run=args.dry_run)
        for action in actions:
            prefix = "dry-run: " if args.dry_run else ""
            print(f"{prefix}{action}")
        print("Claude Code skill: active in new or existing Claude Code sessions.")
        print("Codex skill: installed on disk; restart Codex for the skill to become available.")
        return 0
    if args.command == "uninstall":
        actions = uninstall_skills(dry_run=args.dry_run)
        for action in actions:
            prefix = "dry-run: " if args.dry_run else ""
            print(f"{prefix}{action}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
