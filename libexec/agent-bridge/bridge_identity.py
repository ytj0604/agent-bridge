#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess

from bridge_participants import active_participants, load_session, save_session_state
from bridge_paths import state_root
from bridge_util import locked_json, locked_json_read, utc_now


def attached_sessions_file() -> Path:
    return Path(os.environ.get("AGENT_BRIDGE_ATTACH_REGISTRY", str(state_root() / "attached-sessions.json")))


def pane_locks_file() -> Path:
    return Path(os.environ.get("AGENT_BRIDGE_PANE_LOCKS", str(state_root() / "pane-locks.json")))


def live_sessions_file() -> Path:
    return Path(os.environ.get("AGENT_BRIDGE_LIVE_SESSIONS", str(state_root() / "live-sessions.json")))


def identity_key(agent_type: str, session_id: str) -> str:
    return f"{agent_type}:{session_id}"


def read_attached_mapping(agent_type: str, session_id: str) -> dict | None:
    if not agent_type or not session_id:
        return None
    data = locked_json_read(attached_sessions_file(), {"version": 1, "sessions": {}})
    mapping = (data.get("sessions") or {}).get(identity_key(agent_type, session_id))
    return dict(mapping) if isinstance(mapping, dict) else None


def read_pane_lock(pane: str) -> dict:
    data = locked_json_read(pane_locks_file(), {"version": 1, "panes": {}})
    record = (data.get("panes") or {}).get(pane) or {}
    return dict(record) if isinstance(record, dict) else {}


def read_live_by_pane(pane: str) -> dict:
    data = locked_json_read(live_sessions_file(), {"version": 1, "panes": {}, "sessions": {}})
    record = (data.get("panes") or {}).get(pane) or {}
    return dict(record) if isinstance(record, dict) else {}


def read_live_by_identity(agent_type: str, session_id: str) -> dict:
    data = locked_json_read(live_sessions_file(), {"version": 1, "panes": {}, "sessions": {}})
    record = (data.get("sessions") or {}).get(identity_key(agent_type, session_id)) or {}
    return dict(record) if isinstance(record, dict) else {}


def tmux_target_for_pane(pane: str) -> str:
    if not pane:
        return ""
    proc = subprocess.run(
        ["tmux", "display-message", "-p", "-t", pane, "#{session_name}:#{window_index}.#{pane_index}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    return pane


def mapping_matches_lock(mapping: dict, lock: dict) -> bool:
    return (
        bool(mapping)
        and bool(lock)
        and str(lock.get("bridge_session") or "") == str(mapping.get("bridge_session") or "")
        and str(lock.get("alias") or "") == str(mapping.get("alias") or "")
        and str(lock.get("hook_session_id") or "") == str(mapping.get("session_id") or "")
        and str(lock.get("agent") or "") == str(mapping.get("agent") or "")
    )


def detach_stale_pane_lock(pane: str, reason: str) -> dict:
    removed: dict = {}
    with locked_json(pane_locks_file(), {"version": 1, "panes": {}}) as data:
        panes = data.setdefault("panes", {})
        record = panes.get(pane)
        if isinstance(record, dict):
            removed = dict(record)
            del panes[pane]

    bridge_session = str(removed.get("bridge_session") or "")
    alias = str(removed.get("alias") or "")
    hook_session_id = str(removed.get("hook_session_id") or "")
    if bridge_session and alias:
        state = load_session(bridge_session)
        participant = (state.get("participants") or {}).get(alias)
        if isinstance(participant, dict) and (
            not hook_session_id or str(participant.get("hook_session_id") or "") == hook_session_id
        ):
            participant["status"] = "detached"
            participant["detached_at"] = utc_now()
            participant["detach_reason"] = reason
            participant["last_pane"] = pane
            save_session_state(state)
    return removed


def update_attached_endpoint(mapping: dict, pane: str, target: str | None = None, *, persist: bool = True) -> dict:
    if not mapping or not pane:
        return mapping
    target = target or tmux_target_for_pane(pane)
    agent_type = str(mapping.get("agent") or "")
    session_id = str(mapping.get("session_id") or "")
    bridge_session = str(mapping.get("bridge_session") or "")
    alias = str(mapping.get("alias") or "")
    if not (agent_type and session_id and bridge_session and alias):
        return mapping

    updated = dict(mapping)
    updated.update({"pane": pane, "target": target, "last_seen_at": utc_now()})

    if not persist:
        return updated

    with locked_json(attached_sessions_file(), {"version": 1, "sessions": {}}) as data:
        sessions = data.setdefault("sessions", {})
        sessions[identity_key(agent_type, session_id)] = updated

    with locked_json(pane_locks_file(), {"version": 1, "panes": {}}) as data:
        panes = data.setdefault("panes", {})
        for existing_pane, record in list(panes.items()):
            if existing_pane == pane:
                continue
            if (
                record.get("bridge_session") == bridge_session
                and (
                    record.get("alias") == alias
                    or record.get("hook_session_id") == session_id
                )
            ):
                del panes[existing_pane]
        panes[pane] = {
            "bridge_session": bridge_session,
            "agent": agent_type,
            "alias": alias,
            "target": target,
            "hook_session_id": session_id,
            "locked_at": updated.get("attached_at") or utc_now(),
            "last_seen_at": utc_now(),
        }

    state = load_session(bridge_session)
    participants = state.setdefault("participants", {})
    participant = participants.get(alias)
    if isinstance(participant, dict):
        participant["pane"] = pane
        participant["target"] = target
        participant["status"] = "active"
        participant["hook_session_id"] = session_id
        participant.pop("detached_at", None)
        participant.pop("detach_reason", None)
        state.setdefault("panes", {})[alias] = pane
        state.setdefault("targets", {})[alias] = target
        state.setdefault("hook_session_ids", {})[alias] = session_id
        save_session_state(state)

    return updated


def update_live_session(
    *,
    agent_type: str,
    session_id: str,
    pane: str,
    bridge_session: str = "",
    alias: str = "",
    event: str = "",
    target: str = "",
) -> dict | None:
    if not (agent_type and session_id and pane):
        return None
    now = utc_now()
    target = target or tmux_target_for_pane(pane)
    if event == "session_ended":
        with locked_json(live_sessions_file(), {"version": 1, "panes": {}, "sessions": {}}) as data:
            panes = data.setdefault("panes", {})
            sessions = data.setdefault("sessions", {})
            pane_record = panes.get(pane) or {}
            if pane_record.get("agent") == agent_type and pane_record.get("session_id") == session_id:
                del panes[pane]
            key = identity_key(agent_type, session_id)
            if key in sessions and sessions[key].get("pane") == pane:
                del sessions[key]
        lock = read_pane_lock(pane)
        if (
            lock
            and str(lock.get("agent") or "") == agent_type
            and str(lock.get("hook_session_id") or "") == session_id
        ):
            detach_stale_pane_lock(pane, f"{agent_type}:{session_id} ended")
        return None

    record = {
        "agent": agent_type,
        "session_id": session_id,
        "pane": pane,
        "target": target,
        "bridge_session": bridge_session,
        "alias": alias,
        "event": event,
        "last_seen_at": now,
    }

    old_pane_record: dict = {}
    with locked_json(live_sessions_file(), {"version": 1, "panes": {}, "sessions": {}}) as data:
        panes = data.setdefault("panes", {})
        sessions = data.setdefault("sessions", {})
        old_pane_record = dict(panes.get(pane) or {})
        for existing_pane, existing in list(panes.items()):
            if existing_pane != pane and existing.get("agent") == agent_type and existing.get("session_id") == session_id:
                del panes[existing_pane]
        old_key = identity_key(str(old_pane_record.get("agent") or ""), str(old_pane_record.get("session_id") or ""))
        if old_pane_record and old_key in sessions and sessions[old_key].get("pane") == pane:
            del sessions[old_key]
        panes[pane] = record
        sessions[identity_key(agent_type, session_id)] = record

    mapping = read_attached_mapping(agent_type, session_id)
    if mapping:
        if bridge_session:
            mapping["bridge_session"] = bridge_session
        if alias:
            mapping["alias"] = alias
        return update_attached_endpoint(mapping, pane, target)

    lock = read_pane_lock(pane)
    if lock and (
        str(lock.get("hook_session_id") or "") != session_id
        or str(lock.get("agent") or "") != agent_type
    ):
        detach_stale_pane_lock(pane, f"pane now hosts {agent_type}:{session_id}")
    return None


@dataclass
class CallerResolution:
    ok: bool
    session: str = ""
    alias: str = ""
    agent: str = ""
    session_id: str = ""
    pane: str = ""
    error: str = ""


def resolve_caller_from_pane(
    *,
    pane: str | None,
    explicit_session: str = "",
    explicit_alias: str = "",
    allow_spoof: bool = False,
    tool_name: str = "agent",
    allow_explicit_without_pane: bool = False,
) -> CallerResolution:
    pane = pane or ""
    if allow_spoof:
        if pane and (not explicit_session or not explicit_alias):
            live = read_live_by_pane(pane)
            if live:
                mapping = read_attached_mapping(str(live.get("agent") or ""), str(live.get("session_id") or ""))
                if mapping:
                    explicit_session = explicit_session or str(mapping.get("bridge_session") or "")
                    explicit_alias = explicit_alias or str(mapping.get("alias") or "")
            if not explicit_session or not explicit_alias:
                lock = read_pane_lock(pane)
                explicit_session = explicit_session or str(lock.get("bridge_session") or "")
                explicit_alias = explicit_alias or str(lock.get("alias") or "")
        return CallerResolution(True, explicit_session, explicit_alias, pane=pane)

    if not pane:
        if explicit_session or explicit_alias:
            if allow_explicit_without_pane:
                return CallerResolution(True, explicit_session, explicit_alias)
            return CallerResolution(
                False,
                error=(
                    f"{tool_name}: refusing explicit --session/--from outside an attached tmux pane; "
                    "pass --allow-spoof only for an explicit admin/test operation"
                ),
            )
        return CallerResolution(True, explicit_session, explicit_alias)

    live = read_live_by_pane(pane)
    if live:
        mapping = read_attached_mapping(str(live.get("agent") or ""), str(live.get("session_id") or ""))
        if mapping:
            mapping = update_attached_endpoint(mapping, pane, str(live.get("target") or ""), persist=False)
            session = str(mapping.get("bridge_session") or "")
            alias = str(mapping.get("alias") or "")
            if explicit_session and explicit_session != session:
                return CallerResolution(False, error=f"{tool_name}: --session {explicit_session!r} does not match live attached room {session!r}")
            if explicit_alias and explicit_alias != alias:
                return CallerResolution(False, error=f"{tool_name}: --from {explicit_alias!r} does not match live attached alias {alias!r}")
            return CallerResolution(
                True,
                session=session,
                alias=alias,
                agent=str(mapping.get("agent") or live.get("agent") or ""),
                session_id=str(mapping.get("session_id") or live.get("session_id") or ""),
                pane=pane,
            )

        lock = read_pane_lock(pane)
        stale_detail = ""
        if lock:
            stale_detail = (
                f" A stale pane lock still points at room {lock.get('bridge_session')!r} "
                f"alias {lock.get('alias')!r}, but this live session is different."
            )
        return CallerResolution(
            False,
            error=(
                f"{tool_name}: current pane hosts {live.get('agent')} session {live.get('session_id')}, "
                "which is not attached to any active bridge room."
                f"{stale_detail} Use bridge_manage to join it."
            ),
        )

    lock = read_pane_lock(pane)
    if not lock:
        return CallerResolution(False, error=f"{tool_name}: caller tmux pane is not attached to an active bridge room")
    return CallerResolution(
        False,
        error=(
            f"{tool_name}: no live hook session observed for this pane, so the stale pane lock is not trusted. "
            "Submit a prompt in this agent or reattach/join it with bridge_run/bridge_manage."
        ),
    )


def resolve_participant_endpoint(bridge_session: str, alias: str, participant: dict) -> str:
    agent_type = str(participant.get("agent_type") or participant.get("agent") or "")
    session_id = str(participant.get("hook_session_id") or "")
    pane = str(participant.get("pane") or "")
    if session_id:
        live = read_live_by_identity(agent_type, session_id)
        if live:
            mapping = read_attached_mapping(agent_type, session_id)
            if mapping and mapping.get("bridge_session") == bridge_session and mapping.get("alias") == alias:
                mapping = update_attached_endpoint(mapping, str(live.get("pane") or pane), str(live.get("target") or ""), persist=False)
                return str(mapping.get("pane") or "")
        if pane:
            live_pane = read_live_by_pane(pane)
            if live_pane and (
                str(live_pane.get("session_id") or "") != session_id
                or str(live_pane.get("agent") or "") != agent_type
            ):
                detach_stale_pane_lock(pane, f"pane no longer hosts {agent_type}:{session_id}")
                return ""

    lock = read_pane_lock(pane) if pane else {}
    if (
        lock
        and lock.get("bridge_session") == bridge_session
        and lock.get("alias") == alias
        and (not session_id or lock.get("hook_session_id") == session_id)
    ):
        return pane
    return ""


def active_participant_endpoint(bridge_session: str, alias: str) -> str:
    state = load_session(bridge_session)
    participant = active_participants(state).get(alias)
    if not participant:
        return ""
    return resolve_participant_endpoint(bridge_session, alias, participant)
