#!/usr/bin/env python3
from __future__ import annotations

import argparse
from contextlib import contextmanager
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid

from bridge_identity import resolve_caller_from_pane
from bridge_participants import active_participants, load_session, normalize_alias, room_status
from bridge_paths import state_root
from bridge_util import TmuxCaptureError, append_jsonl, read_json, run_tmux_capture, utc_now, write_json_atomic


DEFAULT_LINES = 120
DEFAULT_MAX_CHARS = 12000
MAX_LINES = 1000
MAX_CHARS = 200000
SNAPSHOT_CAPTURE_LINES = 100000
SNAPSHOT_MAX_BYTES_PER_ROOM = 100 * 1024 * 1024
SINCE_SCAN_LINES = 100000
SINCE_TAIL_LINES = 30
MIN_OVERLAP_LINES = 10
SEARCH_CONTEXT = 8
MAX_MATCHES = 5
MAX_LINE_CHARS = 1000
WRITE_FAILURE_ERRNOS = {errno.EROFS, errno.EACCES, errno.EPERM}
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)")


def clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)


def validate_caller(args: argparse.Namespace) -> tuple[str, str]:
    session = args.session or os.environ.get("AGENT_BRIDGE_SESSION") or ""
    caller = args.sender or os.environ.get("AGENT_BRIDGE_AGENT") or ""
    resolution = resolve_caller_from_pane(
        pane=os.environ.get("TMUX_PANE"),
        explicit_session=session,
        explicit_alias=caller,
        allow_spoof=args.allow_spoof,
        tool_name="agent_view_peer",
    )
    if not resolution.ok:
        raise SystemExit(resolution.error)
    return session or resolution.session, caller or resolution.alias


def resolve_target(state: dict, caller: str, target: str, allow_self: bool) -> dict:
    participants = active_participants(state)
    try:
        target = normalize_alias(target)
    except ValueError as exc:
        raise SystemExit(f"agent_view_peer: {exc}") from exc
    if caller not in participants:
        aliases = ", ".join(sorted(participants)) or "(none)"
        raise SystemExit(f"agent_view_peer: caller {caller!r} is not active in this room; active aliases: {aliases}")
    if target not in participants:
        aliases = ", ".join(sorted(participants)) or "(none)"
        raise SystemExit(f"agent_view_peer: unknown target alias {target!r}; active aliases: {aliases}")
    if target == caller and not allow_self:
        raise SystemExit("agent_view_peer: refusing self view; pass --self to view your own pane")
    record = participants[target]
    if not record.get("pane"):
        raise SystemExit(f"agent_view_peer: target {target!r} has no pane recorded")
    return record


def capture_response_dir(session: str) -> Path:
    return state_root() / session / "captures" / "responses"


def capture_bus_file(session: str, state: dict | None = None) -> Path:
    if state:
        raw = state.get("bus_file") or state.get("state_file")
        if raw:
            return Path(str(raw))
    return state_root() / session / "events.raw.jsonl"


def capture_via_daemon(
    args: argparse.Namespace,
    *,
    session: str,
    caller: str,
    target: str,
    state: dict,
    pane: str,
    start: int,
    end: int | str | None = None,
    raw: bool = False,
    direct_error: str = "",
) -> str:
    request_id = f"cap-{uuid.uuid4().hex}"
    status = room_status(session)
    if status.state not in {"alive", "unknown"}:
        raise SystemExit(
            "agent_view_peer: local tmux capture failed and daemon capture is unavailable. "
            f"tmux error: {direct_error}. daemon status: {status.reason}. "
            "Restart or reattach the bridge room from the host tmux shell."
        )
    response_file = capture_response_dir(session) / f"{request_id}.json"
    request = {
        "ts": utc_now(),
        "agent": "bridge",
        "event": "capture_request",
        "bridge_session": session,
        "request_id": request_id,
        "from_agent": caller,
        "target": target,
        "pane": pane,
        "start": start,
        "end": end,
        "raw": bool(raw),
        "response_file": str(response_file),
        "direct_error": direct_error,
    }
    try:
        response_file.parent.mkdir(parents=True, exist_ok=True)
        append_jsonl(capture_bus_file(session, state), request)
    except OSError as exc:
        raise SystemExit(
            "agent_view_peer: local tmux capture failed and daemon capture could not be requested. "
            f"tmux error: {direct_error}. request error: {exc}. "
            "Recreate the room with AGENT_BRIDGE_RUNTIME_DIR pointing to a writable path shared by the agents."
        ) from exc

    deadline = time.time() + max(0.1, float(args.capture_timeout))
    while time.time() < deadline:
        if response_file.exists():
            response = read_json(response_file, {})
            response_file.unlink(missing_ok=True)
            if response.get("ok"):
                return str(response.get("text") or "")
            raise SystemExit(f"agent_view_peer: daemon capture failed for pane {pane}: {response.get('error') or 'unknown error'}")
        time.sleep(0.1)

    response_file.unlink(missing_ok=True)
    raise SystemExit(
        "agent_view_peer: local tmux capture failed and daemon capture timed out. "
        f"tmux error: {direct_error}. "
        "Check that the bridge daemon is running and that the room runtime is writable/shared."
    )


def capture_text(
    args: argparse.Namespace,
    *,
    session: str,
    caller: str,
    target: str,
    state: dict,
    pane: str,
    start: int,
    end: int | str | None = None,
    raw: bool = False,
) -> str:
    if args.capture_file:
        return Path(args.capture_file).read_text(encoding="utf-8", errors="replace")
    try:
        return run_tmux_capture(pane, start, end, raw=raw)
    except TmuxCaptureError as exc:
        return capture_via_daemon(
            args,
            session=session,
            caller=caller,
            target=target,
            state=state,
            pane=pane,
            start=start,
            end=end,
            raw=raw,
            direct_error=str(exc),
        )


def clean_lines(text: str, raw: bool = False) -> list[str]:
    if not raw:
        text = ANSI_RE.sub("", text)
    lines = []
    blank_count = 0
    for line in text.splitlines():
        line = line.rstrip()
        if not raw and len(line) > MAX_LINE_CHARS:
            line = line[:MAX_LINE_CHARS] + " ...[line truncated]"
        if not line:
            blank_count += 1
            if blank_count > 2:
                continue
        else:
            blank_count = 0
        lines.append(line)
    return lines


def page_bounds(total: int, lines: int, page: int) -> tuple[int, int]:
    end = max(0, total - (page * lines))
    start = max(0, end - lines)
    return start, end


def cap_lines(lines: list[str], max_chars: int) -> tuple[list[str], bool]:
    kept = []
    used = 0
    truncated = False
    for line in lines:
        add = len(line) + 1
        if kept and used + add > max_chars:
            truncated = True
            break
        if not kept and add > max_chars:
            kept.append(line[:max_chars] + " ...[output truncated]")
            truncated = True
            break
        kept.append(line)
        used += add
    return kept, truncated


def snapshot_root(session: str, target: str) -> Path:
    return state_root() / session / "captures" / "snapshots" / safe_name(target)


@contextmanager
def captures_lock(session: str):
    lock_path = state_root() / session / "captures.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@contextmanager
def optional_captures_lock(session: str):
    try:
        with captures_lock(session):
            yield True
    except OSError as exc:
        if exc.errno in WRITE_FAILURE_ERRNOS:
            yield False
            return
        raise


def cursor_path(session: str, caller: str, target: str) -> Path:
    # Aliases cannot contain "@", so this delimiter cannot collide with valid
    # caller/target pairs such as ("a", "b__c") and ("a__b", "c").
    name = f"{caller}@{target}.json"
    return state_root() / session / "captures" / "cursors" / name


def legacy_cursor_path(session: str, caller: str, target: str) -> Path:
    name = f"{safe_name(caller)}__{safe_name(target)}.json"
    return state_root() / session / "captures" / "cursors" / name


def new_snapshot_id() -> str:
    stamp = utc_now().replace("-", "").replace(":", "").replace(".", "").replace("Z", "Z")
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


def snapshot_paths(session: str, target: str, snapshot_id: str) -> tuple[Path, Path]:
    root = snapshot_root(session, target)
    return root / f"{snapshot_id}.txt", root / f"{snapshot_id}.json"


def save_snapshot(session: str, target: str, lines: list[str], meta: dict) -> str:
    snapshot_id = new_snapshot_id()
    text_path, meta_path = snapshot_paths(session, target, snapshot_id)
    text_path.parent.mkdir(parents=True, exist_ok=True)
    text_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    write_json_atomic(meta_path, {**meta, "snapshot_id": snapshot_id, "line_count": len(lines), "created_at": utc_now()})
    prune_snapshots(session, protect={(safe_name(target), snapshot_id)})
    return snapshot_id


def cursor_referenced_snapshot_keys(session: str) -> set[tuple[str, str]]:
    cursors = state_root() / session / "captures" / "cursors"
    protected: set[tuple[str, str]] = set()
    if not cursors.exists():
        return protected
    for path in cursors.glob("*.json"):
        cursor = read_json(path, {})
        target = str(cursor.get("target") or "")
        snapshot_id = str(cursor.get("snapshot_id") or "")
        if not target or not snapshot_id:
            continue
        protected.add((safe_name(target), snapshot_id))
    return protected


def snapshot_entries(session: str) -> list[dict]:
    root = state_root() / session / "captures" / "snapshots"
    if not root.exists():
        return []
    entries = []
    for target_dir in root.iterdir():
        if not target_dir.is_dir():
            continue
        ids = {path.stem for path in target_dir.glob("*.txt")}
        ids.update(path.stem for path in target_dir.glob("*.json"))
        for snapshot_id in ids:
            paths = [path for path in (target_dir / f"{snapshot_id}.txt", target_dir / f"{snapshot_id}.json") if path.exists()]
            size = 0
            mtime = 0.0
            for path in paths:
                try:
                    stat = path.stat()
                except OSError:
                    continue
                size += stat.st_size
                mtime = max(mtime, stat.st_mtime)
            entries.append(
                {
                    "key": (target_dir.name, snapshot_id),
                    "snapshot_id": snapshot_id,
                    "paths": paths,
                    "size": size,
                    "mtime": mtime,
                }
            )
    return entries


def prune_snapshots(session: str, protect: set[tuple[str, str]] | None = None) -> None:
    entries = snapshot_entries(session)
    total = sum(int(entry["size"]) for entry in entries)
    if total <= SNAPSHOT_MAX_BYTES_PER_ROOM:
        return
    protected = set(protect or set()) | cursor_referenced_snapshot_keys(session)
    for entry in sorted(entries, key=lambda item: (item["mtime"], item["snapshot_id"])):
        if total <= SNAPSHOT_MAX_BYTES_PER_ROOM:
            break
        if entry["key"] in protected:
            continue
        for path in entry["paths"]:
            path.unlink(missing_ok=True)
        total -= int(entry["size"])


def load_snapshot(session: str, target: str, snapshot_id: str) -> tuple[list[str], dict]:
    text_path, meta_path = snapshot_paths(session, target, snapshot_id)
    if not text_path.exists():
        raise SystemExit(f"agent_view_peer: snapshot not found: {snapshot_id}")
    text = text_path.read_text(encoding="utf-8", errors="replace")
    return text.splitlines(), read_json(meta_path, {})


def human_age_from_iso(value: str) -> str:
    if not value:
        return "unknown age"
    raw = value.strip()
    try:
        created = raw
        if created.endswith("Z"):
            created = created[:-1] + "+00:00"
        ts = time.time() - __import__("datetime").datetime.fromisoformat(created).timestamp()
    except (ValueError, TypeError):
        return "unknown age"
    if ts < 0:
        ts = 0
    if ts < 90:
        return f"{int(ts)}s old"
    minutes = int(ts // 60)
    if minutes < 90:
        return f"{minutes}m old"
    hours = int(minutes // 60)
    if hours < 48:
        return f"{hours}h old"
    return f"{int(hours // 24)}d old"


def load_cursor(session: str, caller: str, target: str) -> dict:
    path = cursor_path(session, caller, target)
    if path.exists():
        return read_json(path, {})
    return read_json(legacy_cursor_path(session, caller, target), {})


def save_cursor(session: str, caller: str, target: str, cursor: dict, *, cache_writable: bool = True) -> None:
    if not cache_writable:
        return
    write_json_atomic(cursor_path(session, caller, target), {**cursor, "updated_at": utc_now()})


def update_since_cursor(session: str, caller: str, target: str, lines: list[str], extra: dict | None = None, *, cache_writable: bool = True) -> None:
    if not cache_writable:
        return
    tail = lines[-SINCE_TAIL_LINES:]
    cursor = load_cursor(session, caller, target)
    cursor.update({
        "caller": caller,
        "target": target,
        "last_line_count": len(lines),
        "last_tail_lines": tail,
    })
    if extra:
        cursor.update(extra)
    save_cursor(session, caller, target, cursor)


def find_subsequence(lines: list[str], needle: list[str], start: int = 0) -> int:
    if not needle:
        return -1
    limit = len(lines) - len(needle)
    if limit < start:
        return -1
    for idx in range(max(0, start), limit + 1):
        if lines[idx : idx + len(needle)] == needle:
            return idx
    return -1


def find_last_subsequence(lines: list[str], needle: list[str]) -> int:
    if not needle:
        return -1
    limit = len(lines) - len(needle)
    for idx in range(limit, -1, -1):
        if lines[idx : idx + len(needle)] == needle:
            return idx
    return -1


def compute_since_delta(previous: dict, lines: list[str]) -> tuple[list[str], str, str]:
    old_tail = previous.get("last_tail_lines") or []
    old_count = int(previous.get("last_line_count") or 0)
    if not old_tail:
        return lines[-DEFAULT_LINES:], "new", "no previous cursor; showing latest lines"

    if len(lines) >= old_count and old_count >= len(old_tail):
        expected = old_count - len(old_tail)
        if lines[expected:old_count] == old_tail:
            return lines[old_count:], "high", "matched previous line count and tail"

    for overlap in range(min(len(old_tail), SINCE_TAIL_LINES), MIN_OVERLAP_LINES - 1, -1):
        needle = old_tail[-overlap:]
        idx = find_last_subsequence(lines, needle)
        if idx >= 0:
            confidence = "medium" if overlap >= 20 else "low"
            note = f"matched {overlap} cursor tail lines"
            return lines[idx + overlap :], confidence, note

    return lines[-DEFAULT_LINES:], "uncertain", "cursor tail not found; showing latest lines instead of guessing delta"


def render_output(
    *,
    room: str,
    caller: str,
    target: str,
    target_record: dict,
    mode: str,
    lines: list[str],
    total_lines: int,
    max_chars: int,
    note: str = "",
    snapshot_id: str = "",
    page: int | None = None,
    confidence: str = "",
) -> None:
    shown, truncated = cap_lines(lines, max_chars)
    print(f"Peer view: {target} ({target_record.get('agent_type')}) pane={target_record.get('pane')} room={room}")
    parts = [f"mode={mode}", f"viewer={caller}", f"lines={len(shown)}/{total_lines}"]
    if snapshot_id:
        parts.append(f"snapshot={snapshot_id}")
    if page is not None:
        parts.append(f"page={page}")
    if confidence:
        parts.append(f"confidence={confidence}")
    print(" ".join(parts))
    if note:
        print(f"note: {note}")
    if truncated:
        print(f"note: output capped at {max_chars} chars")
    print("")
    print("--- screen excerpt ---")
    if shown:
        print("\n".join(shown))
    else:
        print("(no new screen output)")
    print("--- end ---")
    print("")
    print(f"Next: agent_view_peer {target} --older | agent_view_peer {target} --since-last | agent_view_peer {target} --search '<text>'")


def handle_onboard(args: argparse.Namespace, session: str, caller: str, target: str, state: dict, record: dict, lines_count: int, max_chars: int, cache_writable: bool) -> None:
    text = capture_text(args, session=session, caller=caller, target=target, state=state, pane=str(record["pane"]), start=-SNAPSHOT_CAPTURE_LINES, end=None, raw=args.raw)
    lines = clean_lines(text, raw=args.raw)
    page = args.page or 0
    start, end = page_bounds(len(lines), lines_count, page)
    if not cache_writable:
        render_output(
            room=session,
            caller=caller,
            target=target,
            target_record=record,
            mode="onboard-stateless",
            lines=lines[start:end],
            total_lines=len(lines),
            max_chars=max_chars,
            page=page,
            note="capture cache is not writable from this sandbox; no snapshot/cursor was saved",
        )
        return
    snapshot_id = save_snapshot(
        session,
        target,
        lines,
        {"target": target, "caller": caller, "pane": record.get("pane"), "mode": "onboard"},
    )
    update_since_cursor(session, caller, target, lines, {"snapshot_id": snapshot_id, "snapshot_page": page})
    render_output(
        room=session,
        caller=caller,
        target=target,
        target_record=record,
        mode="onboard",
        lines=lines[start:end],
        total_lines=len(lines),
        max_chars=max_chars,
        snapshot_id=snapshot_id,
        page=page,
    )


def handle_older(args: argparse.Namespace, session: str, caller: str, target: str, record: dict, lines_count: int, max_chars: int, cache_writable: bool) -> None:
    cursor = load_cursor(session, caller, target)
    snapshot_id = str(args.snapshot or cursor.get("snapshot_id") or "")
    if not snapshot_id:
        raise SystemExit(f"agent_view_peer: no snapshot cursor for {target}; run: agent_view_peer {target} --onboard")
    lines, meta = load_snapshot(session, target, snapshot_id)
    page = args.page if args.page is not None else int(cursor.get("snapshot_page") or 0) + 1
    start, end = page_bounds(len(lines), lines_count, page)
    cursor.update({"snapshot_id": snapshot_id, "snapshot_page": page})
    save_cursor(session, caller, target, cursor, cache_writable=cache_writable)
    note = str(meta.get("created_at") or "")
    if not cache_writable:
        note = f"{note}; capture cache is read-only, page cursor not advanced".strip("; ")
    render_output(
        room=session,
        caller=caller,
        target=target,
        target_record=record,
        mode="older",
        lines=lines[start:end],
        total_lines=len(lines),
        max_chars=max_chars,
        snapshot_id=snapshot_id,
        page=page,
        note=note,
    )


def handle_since_last(args: argparse.Namespace, session: str, caller: str, target: str, state: dict, record: dict, lines_count: int, max_chars: int, cache_writable: bool) -> None:
    text = capture_text(args, session=session, caller=caller, target=target, state=state, pane=str(record["pane"]), start=-SINCE_SCAN_LINES, end=None, raw=args.raw)
    lines = clean_lines(text, raw=args.raw)
    previous = load_cursor(session, caller, target)
    delta, confidence, note = compute_since_delta(previous, lines)
    shown_delta = delta[-lines_count:] if len(delta) > lines_count else delta
    if len(delta) > lines_count:
        omitted = len(delta) - lines_count
        note = f"{note}; {omitted} older delta lines omitted; cursor advanced to latest"
    if not cache_writable:
        note = f"{note}; capture cache is read-only, cursor not advanced"
    update_since_cursor(session, caller, target, lines, {"last_since_confidence": confidence}, cache_writable=cache_writable)
    render_output(
        room=session,
        caller=caller,
        target=target,
        target_record=record,
        mode="since-last",
        lines=shown_delta,
        total_lines=len(delta),
        max_chars=max_chars,
        note=note,
        confidence=confidence,
    )


def handle_search(args: argparse.Namespace, session: str, caller: str, target: str, state: dict, record: dict, lines_count: int, max_chars: int) -> None:
    cursor = load_cursor(session, caller, target)
    snapshot_id = "" if args.live else str(args.snapshot or cursor.get("snapshot_id") or "")
    snapshot_age = ""
    if snapshot_id:
        lines, meta = load_snapshot(session, target, snapshot_id)
        snapshot_age = human_age_from_iso(str(meta.get("created_at") or ""))
        source = f"snapshot={snapshot_id} ({snapshot_age})"
    else:
        text = capture_text(args, session=session, caller=caller, target=target, state=state, pane=str(record["pane"]), start=-SNAPSHOT_CAPTURE_LINES, end=None, raw=args.raw)
        lines = clean_lines(text, raw=args.raw)
        source = "live scrollback"
    query = str(args.search)
    lowered = query.lower()
    chunks: list[str] = []
    matches = 0
    for idx, line in enumerate(lines):
        if lowered not in line.lower():
            continue
        matches += 1
        if matches > MAX_MATCHES:
            break
        start = max(0, idx - args.context)
        end = min(len(lines), idx + args.context + 1)
        chunks.append(f"[match {matches} lines {start + 1}-{end}]")
        chunks.extend(lines[start:end])
        chunks.append("")
    shown_matches = min(matches, MAX_MATCHES)
    note = f"query={query!r}; source={source}; matches_shown={shown_matches}"
    if matches > MAX_MATCHES:
        note += f"; more matches omitted after {MAX_MATCHES}"
    if matches == 0 and snapshot_id:
        note += "; no matches in saved snapshot; use --live for current screen or --onboard to refresh snapshot"
    render_output(
        room=session,
        caller=caller,
        target=target,
        target_record=record,
        mode="search",
        lines=chunks or [f"(no matches for {query!r})"],
        total_lines=len(lines),
        max_chars=max_chars,
        note=note,
        snapshot_id=snapshot_id,
    )


def handle_live(args: argparse.Namespace, session: str, caller: str, target: str, state: dict, record: dict, lines_count: int, max_chars: int, cache_writable: bool) -> None:
    page = args.page or 0
    text = capture_text(args, session=session, caller=caller, target=target, state=state, pane=str(record["pane"]), start=-SNAPSHOT_CAPTURE_LINES, end=None, raw=args.raw)
    lines = clean_lines(text, raw=args.raw)
    start, end = page_bounds(len(lines), lines_count, page)
    update_since_cursor(session, caller, target, lines, {"live_page": page}, cache_writable=cache_writable)
    note = "live tmux page; use --onboard for stable historical paging" if page else ""
    if not cache_writable:
        note = f"{note}; capture cache is read-only, cursor not advanced".strip("; ")
    render_output(
        room=session,
        caller=caller,
        target=target,
        target_record=record,
        mode="live",
        lines=lines[start:end],
        total_lines=len(lines),
        max_chars=max_chars,
        page=page,
        note=note,
    )


def main() -> int:
    parser = argparse.ArgumentParser(prog="agent_view_peer", description="View another active bridge participant's tmux pane.")
    parser.add_argument("target", help="target participant alias")
    parser.add_argument("--session")
    parser.add_argument("--from", dest="sender")
    parser.add_argument("--onboard", action="store_true", help="create a stable snapshot and show the latest page")
    parser.add_argument("--older", action="store_true", help="show the next older page from the current snapshot")
    parser.add_argument("--since-last", action="store_true", help="show screen output added since this caller last viewed the target")
    parser.add_argument("--search", help="search current snapshot, or live scrollback if no snapshot cursor exists")
    parser.add_argument("--live", action="store_true", help="with --search, ignore saved snapshot and search current live scrollback")
    parser.add_argument("--snapshot", help="snapshot id to read for --older or --search")
    parser.add_argument("--page", type=int, help="latest-based page number; 0 is newest")
    parser.add_argument("--lines", type=int, default=DEFAULT_LINES)
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    parser.add_argument("--capture-timeout", type=float, default=10.0, help="seconds to wait for daemon-mediated capture fallback")
    parser.add_argument("--context", type=int, default=SEARCH_CONTEXT, help="search context lines")
    parser.add_argument("--raw", action="store_true", help="preserve raw capture text as much as possible")
    parser.add_argument("--self", action="store_true", help="allow viewing the caller's own pane")
    parser.add_argument("--allow-spoof", action="store_true", help="allow explicit --session/--from outside the caller tmux pane")
    parser.add_argument("--capture-file", help=argparse.SUPPRESS)
    args = parser.parse_args()

    modes = sum(bool(value) for value in (args.onboard, args.older, args.since_last, args.search))
    if modes > 1:
        raise SystemExit("agent_view_peer: choose only one of --onboard, --older, --since-last, or --search")
    if args.live and not args.search:
        raise SystemExit("agent_view_peer: --live is only valid with --search")
    if args.live and args.snapshot:
        raise SystemExit("agent_view_peer: --live and --snapshot cannot be used together")
    if args.page is not None and args.page < 0:
        raise SystemExit("agent_view_peer: --page must be >= 0")

    lines_count = clamp(args.lines, 1, MAX_LINES)
    max_chars = clamp(args.max_chars, 1000, MAX_CHARS)
    args.context = clamp(args.context, 0, 50)

    session, caller = validate_caller(args)
    if not session:
        raise SystemExit("agent_view_peer: cannot infer bridge room; run from an attached agent pane or pass --allow-spoof --session")
    if not caller:
        raise SystemExit("agent_view_peer: cannot infer caller alias; run from an attached agent pane or pass --allow-spoof --from")

    state = load_session(session)
    record = resolve_target(state, caller, args.target, args.self)
    target = normalize_alias(args.target)

    with optional_captures_lock(session) as cache_writable:
        if args.onboard:
            handle_onboard(args, session, caller, target, state, record, lines_count, max_chars, cache_writable)
        elif args.older:
            handle_older(args, session, caller, target, record, lines_count, max_chars, cache_writable)
        elif args.since_last:
            handle_since_last(args, session, caller, target, state, record, lines_count, max_chars, cache_writable)
        elif args.search:
            handle_search(args, session, caller, target, state, record, lines_count, max_chars)
        else:
            handle_live(args, session, caller, target, state, record, lines_count, max_chars, cache_writable)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
