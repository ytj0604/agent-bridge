#!/usr/bin/env python3
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import shutil
import sys

from bridge_paths import state_root


def captures_root(session: str) -> Path:
    return state_root() / session / "captures"


def captures_lock_path(session: str) -> Path:
    return state_root() / session / "captures.lock"


@contextmanager
def captures_lock(session: str):
    lock_path = captures_lock_path(session)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def iter_files(root: Path):
    if not root.exists():
        return
    for path in root.rglob("*"):
        if path.is_file():
            yield path


def format_bytes(size: int) -> str:
    units = ("B", "KiB", "MiB", "GiB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def summarize(session: str) -> dict:
    root = captures_root(session)
    files = list(iter_files(root) or [])
    snapshots_root = root / "snapshots"
    cursors_root = root / "cursors"
    snapshot_texts = list(snapshots_root.rglob("*.txt")) if snapshots_root.exists() else []
    snapshot_metas = list(snapshots_root.rglob("*.json")) if snapshots_root.exists() else []
    cursor_files = list(cursors_root.glob("*.json")) if cursors_root.exists() else []
    total_bytes = 0
    for path in files:
        try:
            total_bytes += path.stat().st_size
        except OSError:
            pass
    return {
        "session": session,
        "path": str(root),
        "exists": root.exists(),
        "files": len(files),
        "bytes": total_bytes,
        "bytes_human": format_bytes(total_bytes),
        "snapshots": len(snapshot_texts),
        "snapshot_meta_files": len(snapshot_metas),
        "cursors": len(cursor_files),
    }


def print_summary(summary: dict) -> None:
    if not summary["exists"] or summary["files"] == 0:
        print(f"peer view cache for {summary['session']}: empty")
        print(f"path: {summary['path']}")
        return
    print(f"peer view cache for {summary['session']}: {summary['bytes_human']} in {summary['files']} files")
    print(f"snapshots: {summary['snapshots']} text files, {summary['snapshot_meta_files']} metadata files")
    print(f"cursors: {summary['cursors']}")
    print(f"path: {summary['path']}")


def clear(session: str) -> dict:
    root = captures_root(session)
    before = summarize(session)
    if root.exists():
        shutil.rmtree(root)
    after = summarize(session)
    return {"session": session, "cleared": True, "before": before, "after": after}


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect or clear agent_view_peer capture cache.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_summary = sub.add_parser("summary")
    p_summary.add_argument("-s", "--session", required=True)
    p_summary.add_argument("--json", action="store_true")

    p_clear = sub.add_parser("clear")
    p_clear.add_argument("-s", "--session", required=True)
    p_clear.add_argument("--json", action="store_true")

    args = parser.parse_args()
    with captures_lock(args.session):
        if args.command == "summary":
            result = summarize(args.session)
            if args.json:
                print(json.dumps(result, ensure_ascii=True, indent=2))
            else:
                print_summary(result)
            return 0
        if args.command == "clear":
            result = clear(args.session)
            if args.json:
                print(json.dumps(result, ensure_ascii=True, indent=2))
            else:
                before = result["before"]
                print(
                    f"cleared peer view cache for {args.session}: "
                    f"{before['bytes_human']} in {before['files']} files removed"
                )
            return 0
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.exit(130)
