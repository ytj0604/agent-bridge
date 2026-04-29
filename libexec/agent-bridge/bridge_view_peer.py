#!/usr/bin/env python3
from __future__ import annotations

import argparse
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import uuid

from bridge_identity import resolve_caller_from_pane, resolve_participant_endpoint_detail
from bridge_participants import active_participants, load_session, normalize_alias, room_status
from bridge_paths import state_root
from bridge_util import TmuxCaptureError, append_jsonl, read_json, run_tmux_capture, short_id, utc_now, write_json_atomic


DEFAULT_LINES = 50
DEFAULT_MAX_CHARS = 12000
MAX_LINES = 1000
MAX_CHARS = 200000
SNAPSHOT_CAPTURE_LINES = 100000
SNAPSHOT_MAX_BYTES_PER_ROOM = 100 * 1024 * 1024
SINCE_SCAN_LINES = 100000
SINCE_TAIL_LINES = 30
SINCE_CURSOR_VERSION = 2
SINCE_ANCHOR_LIMIT = 16
SINCE_ANCHOR_SCAN_RAW_LINES = 300
SINCE_ANCHOR_SCAN_STABLE_LINES = 200
SINCE_ANCHOR_PRIMARY_LINES = 4
SINCE_ANCHOR_FALLBACK_LINES = 3
SINCE_ANCHOR_MIN_LINE_INFO_CHARS = 20
SINCE_ANCHOR_MIN_WINDOW_CHARS = {4: 80, 3: 90}
SINCE_CONSUMED_TAIL_MAX_LINES = 30
SINCE_CONSUMED_TAIL_MAX_CHARS = 2000
SEARCH_CONTEXT = 8
MAX_MATCHES = 5
MAX_LINE_CHARS = 1000
SNAPSHOT_REF_CHARS = 6
WRITE_FAILURE_ERRNOS = {errno.EROFS, errno.EACCES, errno.EPERM}
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)")
USEFUL_ANCHOR_TOKEN_RE = re.compile(
    r"(?:\bmsg-[A-Za-z0-9]+\b|\bcausal-[A-Za-z0-9]+\b|\bwake-[A-Za-z0-9]+\b|"
    r"\bcap-[A-Za-z0-9]+\b|\bagg-[A-Za-z0-9]+\b|/[A-Za-z0-9_./-]{3,}|"
    r"\b[A-Za-z0-9_.-]+\.(?:py|sh|md|json|toml|txt|yaml|yml)\b|"
    r"\b(?:agent_send_peer|agent_view_peer|agent_alarm|agent_interrupt_peer|agent_clear_peer|agent_cancel_message|agent_wait_status|agent_aggregate_status|bridge|python3|pytest|rg|git)\b)"
)
DURATION_COUNTER_RE = re.compile(
    r"\b\d+\s*(?:ms|s|sec|secs|seconds?|m|min|mins|minutes?|h|hr|hrs|hours?)\b|"
    r"\b\d+m\s+\d+s\b|\b\d+\s+tokens?\b|[0-9]+(?:\.[0-9]+)?[kKmM]?\s+tokens?",
    re.IGNORECASE,
)
STATUS_DURATION_RE = re.compile(
    r"(?:<n>|\b\d+m\s+\d+s\b|\b\d+\s*(?:ms|s|sec|secs|seconds?|m|min|mins|minutes?|h|hr|hrs|hours?)\b)",
    re.IGNORECASE,
)
STRICT_ACTIVE_RARE_GLYPH_MARKER_RE = re.compile(
    r"(?:[\u2191\u2193]\s*(?:<n>|\d+(?:\.\d+)?[kKmM]?)\s+tokens?|\b(?:<n>|\d+(?:\.\d+)?[kKmM]?)\s+tokens?|thought\s+for|thinking\s+with\s+high\s+effort|"
    r"still\s+thinking|almost\s+done\s+thinking|running\s+stop\s+hook)",
    re.IGNORECASE,
)
ACTIVE_ARROW_TOKEN_COUNTER_RE = re.compile(r"[\u2191\u2193]\s*(?:<n>|\d+(?:\.\d+)?[kKmM]?)\s+tokens?", re.IGNORECASE)
ACTIVE_THOUGHT_DURATION_RE = re.compile(rf"thought\s+for\s+{STATUS_DURATION_RE.pattern}", re.IGNORECASE)
ACTIVE_EFFORT_MARKER_RE = re.compile(
    r"(?:thinking\s+with\s+high\s+effort|still\s+thinking|almost\s+done\s+thinking)",
    re.IGNORECASE,
)
ACTIVE_TUI_DURATION_PREFIX_RE = re.compile(rf"^\s*{STATUS_DURATION_RE.pattern}\s*(?:\u00b7|$)", re.IGNORECASE)
ACTIVE_RUNNING_STOP_HOOK_TUI_RE = re.compile(
    rf"(?:^|\u00b7\s*)running\s+stop\s+hook(?:\s*$|\s*\u00b7\s*(?:{STATUS_DURATION_RE.pattern}|[\u2191\u2193]\s*(?:<n>|\d+(?:\.\d+)?[kKmM]?)\s+tokens?))",
    re.IGNORECASE,
)
MODEL_FOOTER_RE = re.compile(r"^\s*(?:gpt|claude)[A-Za-z0-9_.-]*(?:\s+\w+)?\s+\u00b7\s+")
CODEX_PROMPT_PLACEHOLDER_RE = re.compile(
    r"^\u203a\s+(?:Improve documentation in @filename|Message Codex|Ask Codex|Type a message)\s*$",
    re.IGNORECASE,
)
CLAUDE_RARE_PARTIAL_STATUS_RE = re.compile(
    r"(?P<glyph>[\u273b\u273d])\s+[A-Z][A-Za-z-]*(?:\u2026|\.{3})(?:\s*\((?P<payload>[^)]*)\))?"
)
CLAUDE_DOT_PARTIAL_STATUS_RE = re.compile(
    r"\u00b7\s+(?P<verb>[A-Z][A-Za-z-]*ing)(?:\u2026|\.{3})(?:\s*\((?P<payload>[^)]*)\))?"
)


def clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)


def snapshot_ref(snapshot_id: str) -> str:
    snapshot_id = str(snapshot_id or "")
    return snapshot_id[-SNAPSHOT_REF_CHARS:] if len(snapshot_id) > SNAPSHOT_REF_CHARS else snapshot_id


def model_safe_capture_error(value: str, pane: str) -> str:
    text = str(value or "").replace("\n", " ")
    if pane:
        text = text.replace(str(pane), "<target-pane>")
    text = re.sub(r"%\d+", "<pane>", text)
    return text[:200]


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
    request_id = short_id("cap")
    status = room_status(session)
    display_error = model_safe_capture_error(direct_error, pane)
    if status.state not in {"alive", "unknown"}:
        raise SystemExit(
            "agent_view_peer: local tmux capture failed and daemon capture is unavailable. "
            f"tmux error: {display_error}. daemon status: {status.reason}. "
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
            f"tmux error: {display_error}. request error: {exc}. "
            "Recreate the room with AGENT_BRIDGE_RUNTIME_DIR pointing to a writable path shared by the agents."
        ) from exc

    deadline = time.time() + max(0.1, float(args.capture_timeout))
    while time.time() < deadline:
        if response_file.exists():
            response = read_json(response_file, {})
            response_file.unlink(missing_ok=True)
            if response.get("ok"):
                return str(response.get("text") or "")
            error = model_safe_capture_error(str(response.get("error") or "unknown error"), pane)
            raise SystemExit(f"agent_view_peer: daemon capture failed for target {target}: {error}")
        time.sleep(0.1)

    response_file.unlink(missing_ok=True)
    raise SystemExit(
        "agent_view_peer: local tmux capture failed and daemon capture timed out. "
        f"tmux error: {display_error}. "
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
    participant = active_participants(state).get(target) or {"pane": pane}
    endpoint_detail = resolve_participant_endpoint_detail(session, target, participant, purpose="read")
    endpoint = str(endpoint_detail.get("pane") or "") if endpoint_detail.get("ok") else ""
    if not endpoint:
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
            direct_error=f"no verified live endpoint: {endpoint_detail.get('reason') or 'unknown'}",
        )
    try:
        return run_tmux_capture(endpoint, start, end, raw=raw)
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


def snapshot_ids(session: str, target: str) -> set[str]:
    root = snapshot_root(session, target)
    if not root.exists():
        return set()
    ids = {path.stem for path in root.glob("*.txt")}
    ids.update(path.stem for path in root.glob("*.json"))
    return ids


def snapshot_display_ref(session: str, target: str, snapshot_id: str) -> str:
    snapshot_id = str(snapshot_id or "")
    if not snapshot_id:
        return ""
    ids = snapshot_ids(session, target)
    if snapshot_id not in ids:
        return snapshot_ref(snapshot_id)
    for width in range(min(SNAPSHOT_REF_CHARS, len(snapshot_id)), len(snapshot_id) + 1):
        ref = snapshot_id[-width:]
        if sum(1 for ident in ids if ident.endswith(ref)) == 1:
            return ref
    return snapshot_id


def resolve_snapshot_id(session: str, target: str, snapshot_id: str) -> str:
    snapshot_id = str(snapshot_id or "")
    if not snapshot_id:
        return snapshot_id
    text_path, _meta_path = snapshot_paths(session, target, snapshot_id)
    if text_path.exists():
        return snapshot_id

    ids = snapshot_ids(session, target)
    matches = sorted(ident for ident in ids if ident.endswith(snapshot_id))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        refs = ", ".join(snapshot_display_ref(session, target, item) for item in matches[:5])
        raise SystemExit(f"agent_view_peer: ambiguous snapshot ref {snapshot_ref(snapshot_id)!r}; matching refs: {refs}")
    return snapshot_id


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
    snapshot_id = resolve_snapshot_id(session, target, snapshot_id)
    text_path, meta_path = snapshot_paths(session, target, snapshot_id)
    if not text_path.exists():
        raise SystemExit(f"agent_view_peer: snapshot not found: {snapshot_display_ref(session, target, snapshot_id) or '(empty)'}")
    text = text_path.read_text(encoding="utf-8", errors="replace")
    meta = read_json(meta_path, {})
    meta.setdefault("snapshot_id", snapshot_id)
    return text.splitlines(), meta


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


def normalize_since_text(line: str) -> str:
    text = str(line or "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def mostly_separator_line(text: str) -> bool:
    stripped = re.sub(r"\s+", "", text)
    if len(stripped) < 8:
        return False
    separator_chars = set("-_=*.\u2500\u2501\u2502\u2503\u2504\u2505\u2508\u2509\u2550")
    return all(char in separator_chars for char in stripped)


def strip_since_frame_edges(text: str) -> str:
    return re.sub(r"^[\s\u2502\u2551\u2503\u2506\u2507\u250a\u250b]+|[\s\u2502\u2551\u2503\u2506\u2507\u250a\u250b]+$", "", text)


def has_strict_active_status_marker(payload: str, glyph: str) -> bool:
    payload = payload or ""
    if glyph in {"\u273b", "\u273d"}:
        return bool(STRICT_ACTIVE_RARE_GLYPH_MARKER_RE.search(payload))
    if ACTIVE_ARROW_TOKEN_COUNTER_RE.search(payload):
        return True
    if ACTIVE_THOUGHT_DURATION_RE.search(payload):
        return True
    if ACTIVE_RUNNING_STOP_HOOK_TUI_RE.search(payload):
        return True
    if ACTIVE_EFFORT_MARKER_RE.search(payload) and ACTIVE_TUI_DURATION_PREFIX_RE.search(payload):
        return True
    return False


def is_claude_rare_partial_status_line(stripped: str) -> bool:
    partial = CLAUDE_RARE_PARTIAL_STATUS_RE.fullmatch(stripped)
    if not partial:
        return False
    payload = partial.group("payload")
    if payload is None:
        return True
    return has_strict_active_status_marker(payload, partial.group("glyph") or "")


def is_claude_dot_partial_status_line(stripped: str) -> bool:
    partial = CLAUDE_DOT_PARTIAL_STATUS_RE.fullmatch(stripped)
    if not partial:
        return False
    payload = partial.group("payload")
    return payload is not None and has_strict_active_status_marker(payload, "")


def is_claude_status_line(text: str) -> bool:
    stripped = strip_since_frame_edges(text)
    duration = STATUS_DURATION_RE.pattern
    if re.fullmatch(r"(?:\u2022\s+)?Running Stop hook", stripped, re.IGNORECASE):
        return True
    if re.fullmatch(rf"[\u2500\u2501]+\s*[A-Z][A-Za-z-]*\s+for\s+{duration}\s*[\u2500\u2501]+", stripped):
        return True
    if re.fullmatch(rf"[\u273b\u273d]\s+[A-Z][^\s()]*\s+for\s+{duration}(?:\s+\u00b7\s+.*)?", stripped):
        return True
    if is_claude_rare_partial_status_line(stripped) or is_claude_dot_partial_status_line(stripped):
        return True
    active = re.fullmatch(r"(?P<glyph>[\u273b\u273d]|\*)?\s*(?P<verb>[A-Z][^\s()]*ing)(?:\u2026|\.{3})\s*\((?P<payload>[^)]*)\)", stripped)
    if active and has_strict_active_status_marker(active.group("payload"), active.group("glyph") or ""):
        return True
    return False


def is_model_footer_line(line: str) -> bool:
    return bool(MODEL_FOOTER_RE.search(strip_since_frame_edges(normalize_since_text(line))))


def is_codex_bridge_prompt_line(line: str) -> bool:
    return strip_since_frame_edges(normalize_since_text(line)).startswith("\u203a [bridge:")


def is_codex_prompt_candidate_line(line: str) -> bool:
    stripped = strip_since_frame_edges(normalize_since_text(line))
    return bool(CODEX_PROMPT_PLACEHOLDER_RE.fullmatch(stripped)) and not stripped.startswith("\u203a [bridge:")


def is_stored_codex_prompt_placeholder_line(line: str) -> bool:
    stripped = strip_since_frame_edges(normalize_since_text(line))
    if stripped.startswith("\u203a [bridge:"):
        return False
    return bool(CODEX_PROMPT_PLACEHOLDER_RE.fullmatch(stripped))


def is_since_volatile_line(line: str, *, context_volatile: bool = False) -> bool:
    if context_volatile:
        return True
    text = normalize_since_text(line)
    if not text:
        return True
    stripped = strip_since_frame_edges(text)
    lowered = stripped.lower()
    text_lowered = text.lower()
    if mostly_separator_line(text):
        return True
    if re.fullmatch(r"[\u276f>]+", stripped):
        return True
    if "bypass permissions" in text_lowered or "shift+tab" in text_lowered or "esc to interrupt" in text_lowered:
        return True
    if "remote control active" in lowered and len(stripped) <= 40:
        return True
    if is_model_footer_line(text):
        return True
    if is_claude_status_line(text):
        return True
    return False


def has_semantic_after(lines: list[str], start: int, base_volatile: list[bool], context_volatile: set[int] | None = None) -> bool:
    context_volatile = context_volatile or set()
    for idx in range(start + 1, len(lines)):
        if not normalize_since_text(lines[idx]):
            continue
        if is_since_volatile_line(lines[idx], context_volatile=idx in context_volatile or base_volatile[idx]):
            continue
        return True
    return False


def has_model_footer_after(lines: list[str], start: int, base_volatile: list[bool], context_volatile: set[int] | None = None) -> bool:
    context_volatile = context_volatile or set()
    for idx in range(start + 1, len(lines)):
        if not normalize_since_text(lines[idx]):
            continue
        if is_model_footer_line(lines[idx]):
            return True
        if is_since_volatile_line(lines[idx], context_volatile=idx in context_volatile or base_volatile[idx]):
            continue
        return False
    return False


def trailing_tui_start(lines: list[str], base_volatile: list[bool]) -> int:
    start = len(lines)
    for idx in range(len(lines) - 1, -1, -1):
        line = lines[idx]
        if not normalize_since_text(line) or base_volatile[idx] or is_codex_prompt_candidate_line(line):
            start = idx
            continue
        stripped = strip_since_frame_edges(normalize_since_text(line))
        if CLAUDE_DOT_PARTIAL_STATUS_RE.fullmatch(stripped):
            start = idx
            continue
        break
    return start


def has_adjacent_tui_evidence(lines: list[str], idx: int, base_volatile: list[bool], context_volatile: set[int]) -> bool:
    for step in (-2, -1, 1, 2):
        other = idx + step
        if other < 0 or other >= len(lines) or not normalize_since_text(lines[other]):
            continue
        if base_volatile[other] or other in context_volatile or is_model_footer_line(lines[other]):
            return True
    return False


def since_context_volatile_indexes(lines: list[str]) -> set[int]:
    base_volatile = [is_since_volatile_line(line) for line in lines]
    context_volatile: set[int] = set()
    trailing_start = trailing_tui_start(lines, base_volatile)

    for idx, line in enumerate(lines):
        if not is_codex_prompt_candidate_line(line) or idx < trailing_start:
            continue
        if has_model_footer_after(lines, idx, base_volatile, context_volatile) or not has_semantic_after(lines, idx, base_volatile, context_volatile):
            context_volatile.add(idx)

    for idx, line in enumerate(lines):
        stripped = strip_since_frame_edges(normalize_since_text(line))
        partial = CLAUDE_DOT_PARTIAL_STATUS_RE.fullmatch(stripped)
        if not partial or idx in context_volatile:
            continue
        payload = partial.group("payload")
        if payload and has_strict_active_status_marker(payload, ""):
            context_volatile.add(idx)
        elif idx >= trailing_start and has_adjacent_tui_evidence(lines, idx, base_volatile, context_volatile):
            context_volatile.add(idx)

    return context_volatile


def normalize_since_anchor_line(line: str, *, context_volatile: bool = False) -> str:
    text = normalize_since_text(line)
    if not text or is_since_volatile_line(text, context_volatile=context_volatile):
        return ""
    text = DURATION_COUNTER_RE.sub("<n>", text)
    text = re.sub(r"\s+", " ", text).strip()
    info = re.sub(r"[\W_]+", "", text)
    if len(info) < SINCE_ANCHOR_MIN_LINE_INFO_CHARS and not USEFUL_ANCHOR_TOKEN_RE.search(text):
        return ""
    return text


def stable_since_projection(
    lines: list[str],
    *,
    max_raw_lines: int | None = SINCE_ANCHOR_SCAN_RAW_LINES,
    max_stable_lines: int | None = SINCE_ANCHOR_SCAN_STABLE_LINES,
) -> list[dict]:
    projection = []
    raw_min = max(0, len(lines) - max_raw_lines) if max_raw_lines and max_raw_lines > 0 else 0
    context_volatile = since_context_volatile_indexes(lines)
    for idx, line in enumerate(lines):
        if idx < raw_min:
            continue
        norm = normalize_since_anchor_line(line, context_volatile=idx in context_volatile)
        if norm:
            projection.append({"raw_index": idx, "norm": norm})
    if max_stable_lines and max_stable_lines > 0:
        return projection[-max_stable_lines:]
    return projection


def find_all_subsequences(values: list[str], needle: list[str]) -> list[int]:
    if not needle:
        return []
    limit = len(values) - len(needle)
    if limit < 0:
        return []
    matches = []
    for idx in range(0, limit + 1):
        if values[idx : idx + len(needle)] == needle:
            matches.append(idx)
    return matches


def anchor_window_valid(window: list[dict], projection_norms: list[str]) -> bool:
    norms = [str(item.get("norm") or "") for item in window]
    if not anchor_lines_quality_valid(norms):
        return False
    return len(find_all_subsequences(projection_norms, norms)) == 1


def anchor_lines_quality_valid(norms: list[str]) -> bool:
    if len(norms) not in SINCE_ANCHOR_MIN_WINDOW_CHARS:
        return False
    if len(set(norms)) != len(norms):
        return False
    combined_info = sum(len(re.sub(r"[\W_]+", "", norm)) for norm in norms)
    if combined_info < SINCE_ANCHOR_MIN_WINDOW_CHARS[len(norms)]:
        return False
    return True


def build_since_anchors(lines: list[str]) -> list[dict]:
    projection = stable_since_projection(lines)
    projection_norms = [str(item["norm"]) for item in projection]
    anchors = []
    seen: set[tuple[str, ...]] = set()
    for end in range(len(projection), 0, -1):
        for size in (SINCE_ANCHOR_PRIMARY_LINES, SINCE_ANCHOR_FALLBACK_LINES):
            start = end - size
            if start < 0:
                continue
            window = projection[start : start + size]
            norms = tuple(str(item["norm"]) for item in window)
            if norms in seen or not anchor_window_valid(window, projection_norms):
                continue
            anchors.append(
                {
                    "lines": list(norms),
                    "raw_start": int(window[0]["raw_index"]),
                    "raw_end": int(window[-1]["raw_index"]),
                    "stable_count": size,
                }
            )
            seen.add(norms)
            break
        if len(anchors) >= SINCE_ANCHOR_LIMIT:
            break
    return anchors


def since_anchor_identity(lines: list[str]) -> str:
    body = "\n".join(str(line) for line in lines)
    digest = hashlib.sha256(body.encode("utf-8", "surrogatepass")).hexdigest()
    return f"sha256:{digest}"


def update_since_cursor(session: str, caller: str, target: str, lines: list[str], extra: dict | None = None, *, cache_writable: bool = True) -> None:
    if not cache_writable:
        return
    tail = lines[-SINCE_TAIL_LINES:]
    anchors = build_since_anchors(lines)
    cursor = load_cursor(session, caller, target)
    cursor.update({
        "cursor_version": SINCE_CURSOR_VERSION,
        "caller": caller,
        "target": target,
        "last_line_count": len(lines),
        "last_tail_lines": tail,
        "since_anchors": anchors,
    })
    if extra:
        cursor.update(extra)
    consumed_tail = cursor.get("since_consumed_tail")
    anchor_identities = {since_anchor_identity([str(line) for line in anchor.get("lines") or []]) for anchor in anchors if isinstance(anchor, dict)}
    if not extra or "since_consumed_tail" not in extra:
        cursor["since_consumed_tail"] = build_since_consumed_tail_for_anchors(lines, anchors)
    elif not isinstance(consumed_tail, dict) or consumed_tail.get("anchor_identity") not in anchor_identities:
        cursor["since_consumed_tail"] = {}
    save_cursor(session, caller, target, cursor)


def cursor_anchors(previous: dict) -> tuple[list[dict], str]:
    raw_anchors = previous.get("since_anchors")
    if isinstance(raw_anchors, list):
        anchors = []
        for anchor in raw_anchors:
            if not isinstance(anchor, dict):
                continue
            lines = []
            for line in anchor.get("lines") or []:
                if is_stored_codex_prompt_placeholder_line(str(line)):
                    continue
                norm = normalize_since_anchor_line(str(line))
                if norm:
                    lines.append(norm)
            if not anchor_lines_quality_valid(lines):
                continue
            anchors.append({**anchor, "lines": lines})
        return anchors, "stored"

    old_tail = previous.get("last_tail_lines") or []
    if isinstance(old_tail, list) and old_tail:
        return build_since_anchors([str(line) for line in old_tail]), "legacy"
    return [], "none"


def trim_since_delta(lines: list[str]) -> tuple[list[str], str]:
    if not lines:
        return [], "no meaningful new output since last view"
    context_volatile = since_context_volatile_indexes(lines)
    end = len(lines)
    trimmed_volatile = False
    while end > 0 and is_since_volatile_line(lines[end - 1], context_volatile=(end - 1) in context_volatile):
        trimmed_volatile = True
        end -= 1
    while end > 0 and not normalize_since_text(lines[end - 1]):
        end -= 1
    display = lines[:end]
    meaningful = [
        line
        for idx, line in enumerate(display)
        if normalize_since_text(line) and not is_since_volatile_line(line, context_volatile=idx in context_volatile)
    ]
    if not meaningful:
        if any(is_since_volatile_line(line, context_volatile=idx in context_volatile) for idx, line in enumerate(lines)):
            return [], "only volatile TUI status changed; peer may still be working"
        return [], "no meaningful new output since last view"
    if trimmed_volatile:
        return display, "trimmed volatile TUI status suffix"
    return display, ""


def consumed_tail_entries(lines: list[str]) -> list[dict]:
    context_volatile = since_context_volatile_indexes(lines)
    entries = []
    for idx, line in enumerate(lines):
        norm = normalize_since_text(line)
        if not norm or is_since_volatile_line(line, context_volatile=idx in context_volatile):
            continue
        entries.append({"raw_index": idx, "norm": norm})
    return entries


def build_since_consumed_tail(anchor_identity: str, raw_delta: list[str]) -> dict:
    if not anchor_identity:
        return {}
    entries = consumed_tail_entries(raw_delta)
    kept = []
    used = 0
    truncated = False
    for entry in entries:
        norm = str(entry.get("norm") or "")
        add = len(norm) + 1
        if len(kept) >= SINCE_CONSUMED_TAIL_MAX_LINES or (kept and used + add > SINCE_CONSUMED_TAIL_MAX_CHARS):
            truncated = True
            break
        if not kept and add > SINCE_CONSUMED_TAIL_MAX_CHARS:
            truncated = True
            break
        kept.append(norm)
        used += add
    if len(kept) < len(entries):
        truncated = True
    if not kept:
        return {}
    return {"anchor_identity": anchor_identity, "lines": kept, "truncated": truncated}


def build_since_consumed_tail_for_anchors(lines: list[str], anchors: list[dict]) -> dict:
    if not anchors:
        return {}
    anchor = anchors[0]
    anchor_lines = [str(line) for line in anchor.get("lines") or [] if str(line)]
    if not anchor_lines:
        return {}
    try:
        raw_end = int(anchor.get("raw_end"))
    except (TypeError, ValueError):
        return {}
    return build_since_consumed_tail(since_anchor_identity(anchor_lines), lines[raw_end + 1 :])


def apply_consumed_tail(previous: dict, anchor_identity: str, raw_delta: list[str]) -> tuple[list[str], str]:
    memo = previous.get("since_consumed_tail")
    if not isinstance(memo, dict) or not memo:
        return raw_delta, "none"
    if not anchor_identity or memo.get("anchor_identity") != anchor_identity:
        return raw_delta, "cleared"
    stored = [str(line) for line in memo.get("lines") or [] if str(line)]
    if not stored:
        return raw_delta, "cleared"
    entries = consumed_tail_entries(raw_delta)
    if len(entries) < len(stored):
        return raw_delta, "cleared"
    current_prefix = [str(entry.get("norm") or "") for entry in entries[: len(stored)]]
    if current_prefix != stored:
        return raw_delta, "cleared"
    skip_to = int(entries[len(stored) - 1]["raw_index"]) + 1
    return raw_delta[skip_to:], "skipped"


def compute_since_delta_detail(previous: dict, lines: list[str]) -> dict:
    if not previous:
        return {
            "delta": lines[-DEFAULT_LINES:],
            "confidence": "new",
            "note": "no previous cursor; showing latest lines",
            "matched_anchor_identity": "",
            "consumed_raw_delta": [],
        }

    anchors, anchor_source = cursor_anchors(previous)
    if not anchors:
        if "since_anchors" not in previous:
            return {
                "delta": lines[-DEFAULT_LINES:],
                "confidence": "upgrade_reset",
                "note": "no usable legacy since-last anchor; cursor reset from current capture",
                "matched_anchor_identity": "",
                "consumed_raw_delta": [],
            }
        return {
            "delta": lines[-DEFAULT_LINES:],
            "confidence": "new",
            "note": "no usable since-last anchor; cursor reset from current capture",
            "matched_anchor_identity": "",
            "consumed_raw_delta": [],
        }

    projection = stable_since_projection(lines, max_raw_lines=None, max_stable_lines=None)
    current_norms = [str(item["norm"]) for item in projection]
    ambiguous = 0
    absent = 0
    for anchor_index, anchor in enumerate(anchors):
        needle = [str(line) for line in anchor.get("lines") or [] if str(line)]
        if len(needle) < SINCE_ANCHOR_FALLBACK_LINES:
            absent += 1
            continue
        matches = find_all_subsequences(current_norms, needle)
        if not matches:
            absent += 1
            continue
        if len(matches) > 1:
            ambiguous += 1
            continue
        match_end = matches[0] + len(needle) - 1
        raw_end = int(projection[match_end]["raw_index"])
        raw_delta = lines[raw_end + 1 :]
        anchor_identity = since_anchor_identity(needle)
        display_raw_delta, consumed_action = apply_consumed_tail(previous, anchor_identity, raw_delta)
        delta, delta_note = trim_since_delta(display_raw_delta)
        confidence = "high" if len(needle) >= SINCE_ANCHOR_PRIMARY_LINES else "medium"
        note = f"matched {len(needle)} stable {anchor_source} anchor lines"
        if anchor_index:
            note = f"{note} after skipping {anchor_index} newer anchor(s)"
        if consumed_action == "skipped":
            note = f"{note}; skipped previously consumed short output"
        elif consumed_action == "cleared":
            note = f"{note}; reset stale consumed-tail memo"
        if delta_note:
            note = f"{note}; {delta_note}"
        return {
            "delta": delta,
            "confidence": confidence,
            "note": note,
            "matched_anchor_identity": anchor_identity,
            "consumed_raw_delta": raw_delta,
        }

    detail = []
    if absent:
        detail.append(f"{absent} absent")
    if ambiguous:
        detail.append(f"{ambiguous} ambiguous")
    reason = ", ".join(detail) if detail else "no usable anchors"
    return {
        "delta": lines[-DEFAULT_LINES:],
        "confidence": "uncertain",
        "note": f"cursor anchor not found ({reason}); showing latest lines only; cursor not advanced; run agent_view_peer <target> --onboard to reset",
        "matched_anchor_identity": "",
        "consumed_raw_delta": [],
    }


def compute_since_delta(previous: dict, lines: list[str]) -> tuple[list[str], str, str]:
    detail = compute_since_delta_detail(previous, lines)
    return list(detail["delta"]), str(detail["confidence"]), str(detail["note"])


def since_confidence_advances_cursor(confidence: str) -> bool:
    return confidence in {"high", "medium", "new", "upgrade_reset"}


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
    agent_type = target_record.get("agent_type") or "unknown"
    print(f"Peer view: {target} ({agent_type})")
    parts = [f"mode={mode}", f"lines={len(shown)}/{total_lines}"]
    if snapshot_id:
        parts.append(f"snapshot={snapshot_display_ref(room, target, snapshot_id)}")
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
    snapshot_id = str(meta.get("snapshot_id") or snapshot_id)
    page = args.page if args.page is not None else int(cursor.get("snapshot_page") or 0) + 1
    start, end = page_bounds(len(lines), lines_count, page)
    cursor.update({"snapshot_id": snapshot_id, "snapshot_page": page})
    save_cursor(session, caller, target, cursor, cache_writable=cache_writable)
    note = f"saved snapshot {snapshot_display_ref(session, target, snapshot_id)} ({human_age_from_iso(str(meta.get('created_at') or ''))})"
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
    detail = compute_since_delta_detail(previous, lines)
    delta = list(detail["delta"])
    confidence = str(detail["confidence"])
    note = str(detail["note"])
    shown_delta = delta[-lines_count:] if len(delta) > lines_count else delta
    advance_cursor = since_confidence_advances_cursor(confidence)
    if len(delta) > lines_count:
        omitted = len(delta) - lines_count
        action = "cursor advanced to latest" if advance_cursor and cache_writable else "cursor not advanced"
        note = f"{note}; {omitted} older delta lines omitted; {action}"
    if not cache_writable:
        note = f"{note}; capture cache is read-only, cursor not advanced"
    if advance_cursor:
        update_since_cursor(
            session,
            caller,
            target,
            lines,
            {"last_since_confidence": confidence},
            cache_writable=cache_writable,
        )
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
        snapshot_id = str(meta.get("snapshot_id") or snapshot_id)
        snapshot_age = human_age_from_iso(str(meta.get("created_at") or ""))
        source = f"saved snapshot {snapshot_display_ref(session, target, snapshot_id)} ({snapshot_age})"
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
    parser.add_argument("--search", help="search current snapshot, or live scrollback if no snapshot cursor exists; case-insensitive literal substring (no regex)")
    parser.add_argument("--live", action="store_true", help="with --search, ignore saved snapshot and search current live scrollback")
    parser.add_argument("--snapshot", help="snapshot id to read for --older or --search")
    parser.add_argument("--page", type=int, help="latest-based page number; 0 is newest")
    parser.add_argument("--lines", type=int)
    parser.add_argument("--tail", type=int, help="alias for --lines; show N latest/relevant lines")
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS)
    parser.add_argument("--capture-timeout", type=float, default=10.0, help="seconds to wait for daemon-mediated capture fallback")
    parser.add_argument("--context", type=int, default=SEARCH_CONTEXT, help="search context lines")
    parser.add_argument("--raw", action="store_true", help="preserve raw capture text as much as possible")
    parser.add_argument("--self", action="store_true", help="allow viewing the caller's own pane")
    parser.add_argument("--allow-spoof", action="store_true", help="allow explicit --session/--from outside the caller tmux pane")
    parser.add_argument("--capture-file", help=argparse.SUPPRESS)
    args = parser.parse_args()

    # argparse preserves explicit empty values (`--search ""`), so mode checks
    # must test flag presence rather than query truthiness.
    search_requested = args.search is not None
    modes = sum(bool(value) for value in (args.onboard, args.older, args.since_last, search_requested))
    if modes > 1:
        raise SystemExit("agent_view_peer: choose only one of --onboard, --older, --since-last, or --search")
    if args.live and not search_requested:
        raise SystemExit("agent_view_peer: --live is only valid with --search")
    if args.live and args.snapshot:
        raise SystemExit("agent_view_peer: --live and --snapshot cannot be used together")
    if args.page is not None and args.page < 0:
        raise SystemExit("agent_view_peer: --page must be >= 0")
    if search_requested and not str(args.search).strip():
        raise SystemExit("agent_view_peer: --search query must be non-empty")
    if args.snapshot is not None and not (args.older or search_requested):
        raise SystemExit("agent_view_peer: --snapshot is only valid with --older or --search")
    if args.page is not None and (args.since_last or search_requested):
        raise SystemExit("agent_view_peer: --page is only valid with live view, --onboard, or --older")
    if args.tail is not None and args.lines is not None:
        raise SystemExit("agent_view_peer: use either --tail or --lines, not both")

    raw_lines = args.tail if args.tail is not None else args.lines
    lines_count = clamp(raw_lines if raw_lines is not None else DEFAULT_LINES, 1, MAX_LINES)
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
        elif search_requested:
            handle_search(args, session, caller, target, state, record, lines_count, max_chars)
        else:
            handle_live(args, session, caller, target, state, record, lines_count, max_chars, cache_writable)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
