#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
import os
import re
from pathlib import Path

from bridge_paths import run_root, state_root
from bridge_util import read_json, write_json_atomic


PHYSICAL_AGENT_TYPES = {"claude", "codex"}
RESERVED_TARGETS = {"all", "ALL", "*"}
ALIAS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")


def normalize_alias(value: str) -> str:
    alias = str(value or "").strip()
    if not ALIAS_RE.match(alias):
        raise ValueError(f"invalid alias {alias!r}; use letters, digits, _, ., - and start with a letter")
    return alias


def normalize_agent_type(value: str) -> str:
    agent_type = str(value or "").strip().lower()
    if agent_type not in PHYSICAL_AGENT_TYPES:
        raise ValueError(f"invalid agent type {agent_type!r}; expected claude or codex")
    return agent_type


def session_dir(session: str) -> Path:
    return state_root() / session


def session_file(session: str) -> Path:
    return session_dir(session) / "session.json"


def session_state_exists(session: str) -> bool:
    return session_file(session).exists()


def read_pid(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # If the OS refuses the probe, the PID still exists from our perspective.
        return True


def pid_probe_namespace_untrusted() -> bool:
    raw = os.environ.get("AGENT_BRIDGE_PID_PROBE_UNTRUSTED", "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    try:
        status = Path("/proc/self/status").read_text(encoding="utf-8", errors="replace")
        for line in status.splitlines():
            if line.startswith("NSpid:") and len(line.split()) > 2:
                return True
    except OSError:
        pass
    try:
        cmdline = Path("/proc/1/cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="replace")
    except OSError:
        return False
    lowered = cmdline.lower()
    markers = (
        "bwrap",
        "bubblewrap",
        "codex-linux-sandbox",
        "--unshare-pid",
    )
    return any(marker in lowered for marker in markers)


@dataclass(frozen=True)
class RoomStatus:
    state: str
    reason: str = ""
    pid: int | None = None
    pid_probe_untrusted: bool = False

    @property
    def active_enough_for_read(self) -> bool:
        return self.state in {"alive", "unknown"}

    @property
    def active_enough_for_enqueue(self) -> bool:
        return self.state in {"alive", "unknown"}


def room_status(session: str) -> RoomStatus:
    session = str(session or "").strip()
    if not session:
        return RoomStatus("missing", "bridge session is required")
    if not session_state_exists(session):
        return RoomStatus("missing", f"bridge room {session!r} is not active or was stopped/cleaned up")
    pid = read_pid(run_root() / f"{session}.pid")
    if pid is None:
        return RoomStatus("dead", f"bridge room {session!r} has no running daemon")
    if pid_probe_namespace_untrusted():
        return RoomStatus(
            "unknown",
            f"bridge room {session!r} daemon pid {pid} is not visible from this sandbox/PID namespace",
            pid=pid,
            pid_probe_untrusted=True,
        )
    if process_is_alive(pid):
        return RoomStatus("alive", pid=pid)
    return RoomStatus("dead", f"bridge room {session!r} daemon pid {pid} is not running", pid=pid)


def room_inactive_reason(session: str) -> str:
    status = room_status(session)
    return "" if status.active_enough_for_read else status.reason


def load_session(session: str) -> dict:
    path = session_file(session)
    if path.exists():
        return read_json(path, {"session": session})
    return {"session": session}


def save_session_state(state: dict) -> None:
    session = state["session"]
    write_json_atomic(session_file(session), state)


def participant_record(
    alias: str,
    agent_type: str,
    pane: str,
    target: str = "",
    session_id: str = "",
    cwd: str = "",
    status: str = "active",
) -> dict:
    alias = normalize_alias(alias)
    agent_type = normalize_agent_type(agent_type)
    return {
        "alias": alias,
        "agent_type": agent_type,
        "pane": str(pane),
        "target": str(target or ""),
        "hook_session_id": str(session_id or ""),
        "cwd": str(cwd or ""),
        "status": status,
    }


def participants_from_state(state: dict) -> dict[str, dict]:
    raw = state.get("participants")
    participants: dict[str, dict] = {}
    if isinstance(raw, dict):
        for alias, record in raw.items():
            if not isinstance(record, dict):
                continue
            try:
                clean_alias = normalize_alias(record.get("alias") or alias)
                agent_type = normalize_agent_type(record.get("agent_type") or record.get("type") or clean_alias)
            except ValueError:
                continue
            pane = record.get("pane") or (state.get("panes") or {}).get(clean_alias) or ""
            if not pane:
                continue
            participants[clean_alias] = {
                **record,
                "alias": clean_alias,
                "agent_type": agent_type,
                "pane": str(pane),
                "target": str(record.get("target") or (state.get("targets") or {}).get(clean_alias) or ""),
                "status": record.get("status") or "active",
            }
    if participants:
        return participants

    panes = state.get("panes") or {}
    targets = state.get("targets") or {}
    hook_ids = state.get("hook_session_ids") or {}
    for alias in ("claude", "codex"):
        pane = panes.get(alias)
        if not pane:
            continue
        participants[alias] = participant_record(
            alias=alias,
            agent_type=alias,
            pane=str(pane),
            target=str(targets.get(alias) or ""),
            session_id=str(hook_ids.get(alias) or ""),
            cwd=str(state.get("cwd") or ""),
        )
    return participants


def active_participants(state: dict) -> dict[str, dict]:
    return {
        alias: record
        for alias, record in participants_from_state(state).items()
        if record.get("status", "active") == "active"
    }


def peer_aliases(state: dict, sender: str | None = None) -> list[str]:
    participants = active_participants(state)
    aliases = sorted(participants)
    if sender:
        aliases = [alias for alias in aliases if alias != sender]
    return aliases


def resolve_targets(state: dict, sender: str, target: str | None) -> list[str]:
    participants = active_participants(state)
    sender = str(sender or "")
    raw = str(target or "").strip()
    if raw in RESERVED_TARGETS:
        return [alias for alias in sorted(participants) if alias != sender]
    if not raw:
        peers = [alias for alias in sorted(participants) if alias != sender]
        if len(peers) == 1:
            return peers
        raise ValueError("target is required when more than two participants are active; use --to alias or --all")
    if raw not in participants:
        raise ValueError(f"unknown target alias {raw!r}; active aliases: {', '.join(sorted(participants))}")
    if raw == sender:
        raise ValueError("target must differ from sender")
    return [raw]


def format_peer_list(state: dict, current_alias: str | None = None) -> str:
    participants = active_participants(state)
    if not participants:
        return "No active bridge participants."
    lines = []
    for alias in sorted(participants):
        record = participants[alias]
        marker = " (You)" if current_alias and alias == current_alias else ""
        lines.append(f"- {alias}{marker}: {record.get('agent_type')} pane={record.get('pane')}")
    return "\n".join(lines)


def format_peer_summary(state: dict) -> str:
    participants = active_participants(state)
    if not participants:
        return "No active participants remain."
    items = [
        f"{alias}({record.get('agent_type')})"
        for alias, record in sorted(participants.items())
    ]
    return f"Participants now ({len(items)}): {', '.join(items)}."
