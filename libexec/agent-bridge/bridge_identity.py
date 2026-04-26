#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess

from bridge_participants import active_participants, load_session, participants_from_state, save_session_state
from bridge_pane_probe import probe_agent_process
from bridge_paths import state_root
from bridge_util import append_jsonl, locked_json, locked_json_read, utc_now


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


def recover_mapping_from_session_state(agent_type: str, session_id: str) -> dict | None:
    if not agent_type or not session_id:
        return None
    candidates: list[dict] = []
    for path in state_root().glob("*/session.json"):
        bridge_session = path.parent.name
        state = load_session(bridge_session)
        for alias, participant in participants_from_state(state).items():
            if str(participant.get("agent_type") or "") != agent_type:
                continue
            if str(participant.get("hook_session_id") or "") != session_id:
                continue
            status = str(participant.get("status") or "active")
            detach_reason = str(participant.get("detach_reason") or "")
            if status == "detached" and not detach_reason.endswith(" ended"):
                continue
            state_dir = Path(state.get("state_dir") or state_root() / bridge_session)
            candidates.append(
                {
                    "agent": agent_type,
                    "alias": alias,
                    "session_id": session_id,
                    "bridge_session": bridge_session,
                    "pane": str(participant.get("pane") or ""),
                    "target": str(participant.get("target") or ""),
                    "events_file": str(Path(state.get("bus_file") or state_dir / "events.raw.jsonl")),
                    "public_events_file": str(Path(state.get("events_file") or state_dir / "events.jsonl")),
                    "queue_file": str(Path(state.get("queue_file") or state_dir / "pending.json")),
                    "attached_at": str(participant.get("attached_at") or participant.get("last_seen_at") or utc_now()),
                }
            )
    if len(candidates) != 1:
        return None
    return candidates[0]


def read_pane_lock(pane: str) -> dict:
    data = locked_json_read(pane_locks_file(), {"version": 1, "panes": {}})
    record = (data.get("panes") or {}).get(pane) or {}
    return dict(record) if isinstance(record, dict) else {}


def read_live_by_pane(pane: str) -> dict:
    data = locked_json_read(live_sessions_file(), {"version": 1, "panes": {}, "sessions": {}})
    record = (data.get("panes") or {}).get(pane) or {}
    return dict(record) if isinstance(record, dict) else {}


def live_record_matches(record: dict, agent_type: str, session_id: str) -> bool:
    return (
        bool(record)
        and str(record.get("agent") or "") == agent_type
        and str(record.get("session_id") or "") == session_id
    )


def verified_process_identity(record: dict) -> dict:
    identity = record.get("process_identity") if isinstance(record, dict) else {}
    if isinstance(identity, dict) and str(identity.get("status") or "") == "verified":
        return dict(identity)
    return {}


def resume_from_unknown_disabled() -> bool:
    return os.environ.get("AGENT_BRIDGE_NO_RESUME_FROM_UNKNOWN") == "1"


def probe_live_record_current(record: dict, agent_type: str = "", session_id: str = "") -> dict:
    agent_type = agent_type or str(record.get("agent") or "")
    session_id = session_id or str(record.get("session_id") or "")
    pane = str(record.get("pane") or "")
    if not live_record_matches(record, agent_type, session_id):
        return {"status": "mismatch", "reason": "live_record_mismatch", "pane": pane, "agent": agent_type, "processes": []}
    stored_identity = verified_process_identity(record)
    if not stored_identity:
        return {"status": "unknown", "reason": "missing_verified_prior", "pane": pane, "agent": agent_type, "processes": []}
    try:
        return probe_agent_process(pane, agent_type, stored_identity)
    except Exception as exc:
        return {"status": "unknown", "reason": "probe_exception", "detail": str(exc), "pane": pane, "agent": agent_type, "processes": []}


def _last_seen_key(record: dict) -> str:
    return str(record.get("last_seen_at") or "")


def find_verified_live_record_for_identity(agent_type: str, session_id: str, *, prefer_pane: str = "", exclude_pane: str = "") -> dict:
    if not (agent_type and session_id):
        return {}

    candidates: list[dict] = []
    seen: set[str] = set()
    if prefer_pane and prefer_pane != exclude_pane:
        preferred = read_live_by_pane(prefer_pane)
        if live_record_matches(preferred, agent_type, session_id):
            preferred["pane"] = str(preferred.get("pane") or prefer_pane)
            candidates.append(preferred)
            seen.add(str(preferred.get("pane") or ""))

    remaining = [
        record
        for record in live_records_for_identity(agent_type, session_id, exclude_pane=exclude_pane)
        if str(record.get("pane") or "") not in seen
    ]
    remaining.sort(key=_last_seen_key, reverse=True)
    candidates.extend(remaining)

    for record in candidates:
        probe = probe_live_record_current(record, agent_type, session_id)
        if str(probe.get("status") or "") == "verified":
            verified = dict(record)
            verified["process_identity"] = probe
            return verified
    return {}


def live_records_for_identity(agent_type: str, session_id: str, *, exclude_pane: str = "") -> list[dict]:
    if not agent_type or not session_id:
        return []
    data = locked_json_read(live_sessions_file(), {"version": 1, "panes": {}, "sessions": {}})
    records: list[dict] = []
    seen_panes: set[str] = set()
    for pane, record in (data.get("panes") or {}).items():
        if not isinstance(record, dict):
            continue
        if str(pane) == exclude_pane:
            continue
        if live_record_matches(record, agent_type, session_id):
            copied = dict(record)
            copied["pane"] = str(copied.get("pane") or pane)
            records.append(copied)
            seen_panes.add(str(copied.get("pane") or ""))
    indexed = (data.get("sessions") or {}).get(identity_key(agent_type, session_id)) or {}
    indexed_pane = str(indexed.get("pane") or "")
    if (
        isinstance(indexed, dict)
        and indexed_pane
        and indexed_pane != exclude_pane
        and indexed_pane not in seen_panes
        and live_record_matches(indexed, agent_type, session_id)
    ):
        records.append(dict(indexed))
    records.sort(key=lambda record: str(record.get("last_seen_at") or ""))
    return records


def read_live_by_identity(agent_type: str, session_id: str) -> dict:
    mapping = read_attached_mapping(agent_type, session_id) or recover_mapping_from_session_state(agent_type, session_id)
    if mapping:
        pane = str(mapping.get("pane") or "")
        live = read_live_by_pane(pane) if pane else {}
        if live_record_matches(live, agent_type, session_id):
            return live
    records = live_records_for_identity(agent_type, session_id)
    return records[-1] if records else {}


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


def remove_pane_lock(pane: str) -> dict:
    removed: dict = {}
    with locked_json(pane_locks_file(), {"version": 1, "panes": {}}) as data:
        panes = data.setdefault("panes", {})
        record = panes.get(pane)
        if isinstance(record, dict):
            removed = dict(record)
            del panes[pane]
    return removed


def detach_stale_pane_lock(pane: str, reason: str) -> dict:
    removed = remove_pane_lock(pane)

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


def mark_endpoint_lost_for_pane_lock(pane: str, reason: str) -> dict:
    """Remove a stale pane lock while keeping the participant visible to operators."""
    removed = remove_pane_lock(pane)

    bridge_session = str(removed.get("bridge_session") or "")
    alias = str(removed.get("alias") or "")
    hook_session_id = str(removed.get("hook_session_id") or "")
    if bridge_session and alias:
        state = load_session(bridge_session)
        participant = (state.get("participants") or {}).get(alias)
        if isinstance(participant, dict) and (
            not hook_session_id or str(participant.get("hook_session_id") or "") == hook_session_id
        ):
            participant["status"] = "active"
            participant["endpoint_status"] = "endpoint_lost"
            participant["endpoint_lost_at"] = utc_now()
            participant["endpoint_lost_reason"] = reason
            participant["last_pane"] = pane
            save_session_state(state)
    return removed


def update_attached_endpoint(
    mapping: dict,
    pane: str,
    target: str | None = None,
    *,
    persist: bool = True,
    cwd: str = "",
    model: str = "",
    clear_existing_locks: bool = True,
) -> dict:
    if not mapping or not pane:
        return mapping
    agent_type = str(mapping.get("agent") or "")
    session_id = str(mapping.get("session_id") or "")
    bridge_session = str(mapping.get("bridge_session") or "")
    alias = str(mapping.get("alias") or "")
    if not (agent_type and session_id and bridge_session and alias):
        return mapping
    if persist:
        target = target or tmux_target_for_pane(pane)
    else:
        target = target or str(mapping.get("target") or "") or pane

    updated = dict(mapping)
    updated.update({"pane": pane, "target": target, "last_seen_at": utc_now()})
    if cwd:
        updated["cwd"] = cwd
    if model:
        updated["model"] = model

    if not persist:
        return updated

    with locked_json(attached_sessions_file(), {"version": 1, "sessions": {}}) as data:
        sessions = data.setdefault("sessions", {})
        sessions[identity_key(agent_type, session_id)] = updated

    with locked_json(pane_locks_file(), {"version": 1, "panes": {}}) as data:
        panes = data.setdefault("panes", {})
        if clear_existing_locks:
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
        participant["last_seen_at"] = utc_now()
        if cwd:
            participant["cwd"] = cwd
        if model:
            participant["model"] = model
        participant.pop("detached_at", None)
        participant.pop("detach_reason", None)
        participant.pop("endpoint_status", None)
        participant.pop("endpoint_lost_at", None)
        participant.pop("endpoint_lost_reason", None)
        participant.pop("last_pane", None)
        state.setdefault("panes", {})[alias] = pane
        state.setdefault("targets", {})[alias] = target
        state.setdefault("hook_session_ids", {})[alias] = session_id
        save_session_state(state)

    return updated


def log_endpoint_auto_reconnected(
    bridge_session: str,
    alias: str,
    agent_type: str,
    session_id: str,
    old_pane: str,
    new_pane: str,
    reason: str,
    purpose: str,
    old_status: str = "",
) -> dict:
    note = {
        "old_pane": old_pane,
        "new_pane": new_pane,
        "reason": reason,
        "purpose": purpose,
        "old_status": old_status,
    }
    state = load_session(bridge_session) if bridge_session else {}
    state_dir = Path(state.get("state_dir") or state_root() / bridge_session) if bridge_session else Path()
    event = {
        "ts": utc_now(),
        "agent": "bridge",
        "event": "endpoint_auto_reconnected",
        "bridge_session": bridge_session,
        "alias": alias,
        "agent_type": agent_type,
        "hook_session_id": session_id,
        **note,
    }
    bus_file = state.get("bus_file") or (state_dir / "events.raw.jsonl" if bridge_session else "")
    if bus_file:
        try:
            append_jsonl(Path(bus_file), event)
        except OSError:
            pass
    return note


def auto_reconnect_attached_endpoint(
    mapping: dict,
    live_record: dict,
    reason: str,
    *,
    purpose: str = "write",
    old_pane: str = "",
    old_status: str = "",
    clear_old_locks: bool = True,
) -> dict:
    """Race-safe endpoint migration to a currently verified same-session pane.

    Resolver callers mutate endpoint state only after re-reading the attached
    mapping, so an older resolver pass cannot overwrite a newer hook migration.
    Unknown-old migration is a policy exception for hook/live evidence plus a
    current verified fingerprint; it can be disabled with
    AGENT_BRIDGE_NO_RESUME_FROM_UNKNOWN=1 and never clears old pane locks.
    """
    agent_type = str(mapping.get("agent") or live_record.get("agent") or "")
    session_id = str(mapping.get("session_id") or live_record.get("session_id") or "")
    bridge_session = str(mapping.get("bridge_session") or live_record.get("bridge_session") or "")
    alias = str(mapping.get("alias") or live_record.get("alias") or "")
    new_pane = str(live_record.get("pane") or "")
    if not (agent_type and session_id and bridge_session and alias and new_pane):
        return {}

    expected_pane = str(mapping.get("pane") or "")
    current = read_attached_mapping(agent_type, session_id) or recover_mapping_from_session_state(agent_type, session_id)
    if not current:
        return {}
    if str(current.get("bridge_session") or "") != bridge_session or str(current.get("alias") or "") != alias:
        return {}
    current_pane = str(current.get("pane") or "")
    if current_pane and current_pane not in {expected_pane, new_pane}:
        current_live = read_live_by_pane(current_pane)
        if live_record_matches(current_live, agent_type, session_id):
            current_probe = probe_live_record_current(current_live, agent_type, session_id)
            if str(current_probe.get("status") or "") == "verified":
                return current
        return {}

    target = str(live_record.get("target") or current.get("target") or tmux_target_for_pane(new_pane) or "")
    already_current = current_pane == new_pane
    if old_status == "unknown" and resume_from_unknown_disabled() and not already_current:
        return {}

    candidate_probe = probe_live_record_current(live_record, agent_type, session_id)
    if str(candidate_probe.get("status") or "") != "verified":
        return {}
    verified_record = dict(live_record)
    verified_record["process_identity"] = candidate_probe

    updated = update_attached_endpoint(
        current,
        new_pane,
        target,
        cwd=str(verified_record.get("cwd") or current.get("cwd") or ""),
        model=str(verified_record.get("model") or current.get("model") or ""),
        clear_existing_locks=clear_old_locks and old_status != "unknown",
    )
    reconnect_reason = "endpoint_auto_reconnected_via_read" if purpose == "read" else reason
    updated["_endpoint_auto_reconnected"] = log_endpoint_auto_reconnected(
        bridge_session,
        alias,
        agent_type,
        session_id,
        old_pane or expected_pane or current_pane,
        new_pane,
        reconnect_reason,
        purpose,
        old_status,
    )
    return updated


def _live_record_current_status(record: dict, agent_type: str, session_id: str) -> str:
    if not record:
        return "unknown"
    if not live_record_matches(record, agent_type, session_id):
        return "mismatch"
    probe = probe_live_record_current(record, agent_type, session_id)
    status = str(probe.get("status") or "unknown")
    return status if status in {"verified", "mismatch"} else "unknown"


def _current_live_record_verified(record: dict, agent_type: str, session_id: str) -> bool:
    return _live_record_current_status(record, agent_type, session_id) == "verified"


def _auto_reconnect_endpoint_detail(
    mapping: dict,
    live_record: dict,
    reason: str,
    *,
    purpose: str,
    old_pane: str,
    old_status: str,
    clear_old_locks: bool,
) -> dict | None:
    updated = auto_reconnect_attached_endpoint(
        mapping,
        live_record,
        reason,
        purpose=purpose,
        old_pane=old_pane,
        old_status=old_status,
        clear_old_locks=clear_old_locks,
    )
    if not updated:
        return None
    pane = str(updated.get("pane") or live_record.get("pane") or "")
    return _endpoint_detail(
        True,
        pane=pane,
        reason=reason,
        probe_status="verified",
        detail="endpoint auto reconnected to verified live pane",
        reconnected=updated.get("_endpoint_auto_reconnected") or {},
    )


def update_live_session(
    *,
    agent_type: str,
    session_id: str,
    pane: str,
    bridge_session: str = "",
    alias: str = "",
    event: str = "",
    target: str = "",
    cwd: str = "",
    model: str = "",
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
        replacement = find_verified_live_record_for_identity(agent_type, session_id, exclude_pane=pane)
        if replacement:
            with locked_json(live_sessions_file(), {"version": 1, "panes": {}, "sessions": {}}) as data:
                sessions = data.setdefault("sessions", {})
                key = identity_key(agent_type, session_id)
                current_index = sessions.get(key) or {}
                current_index_pane = str(current_index.get("pane") or "")
                if not current_index_pane or current_index_pane == pane or not _current_live_record_verified(current_index, agent_type, session_id):
                    sessions[key] = replacement
        lock = read_pane_lock(pane)
        if (
            lock
            and str(lock.get("agent") or "") == agent_type
            and str(lock.get("hook_session_id") or "") == session_id
        ):
            mapping = read_attached_mapping(agent_type, session_id) or recover_mapping_from_session_state(agent_type, session_id)
            if (
                mapping
                and str(mapping.get("bridge_session") or "") == str(lock.get("bridge_session") or "")
                and str(mapping.get("alias") or "") == str(lock.get("alias") or "")
                and str(mapping.get("pane") or "") == pane
            ):
                if replacement:
                    update_attached_endpoint(
                        mapping,
                        str(replacement.get("pane") or ""),
                        str(replacement.get("target") or ""),
                        cwd=str(replacement.get("cwd") or ""),
                        model=str(replacement.get("model") or ""),
                    )
                else:
                    detach_stale_pane_lock(pane, f"{agent_type}:{session_id} ended")
            else:
                remove_pane_lock(pane)
        return None

    try:
        process_identity = probe_agent_process(pane, agent_type)
    except Exception as exc:
        process_identity = {
            "status": "unknown",
            "reason": "probe_exception",
            "detail": str(exc),
            "pane": pane,
            "agent": agent_type,
            "processes": [],
        }
    prior_live = read_live_by_pane(pane)
    prior_identity = verified_process_identity(prior_live) if live_record_matches(prior_live, agent_type, session_id) else {}
    process_identity_diagnostics: dict = {}
    if str(process_identity.get("status") or "") == "unknown" and prior_identity:
        process_identity_diagnostics = dict(process_identity)
        process_identity = prior_identity

    record = {
        "agent": agent_type,
        "session_id": session_id,
        "pane": pane,
        "target": target,
        "bridge_session": bridge_session,
        "alias": alias,
        "event": event,
        "last_seen_at": now,
        "process_identity": process_identity,
    }
    if process_identity_diagnostics:
        record["process_identity_diagnostics"] = process_identity_diagnostics
    if cwd:
        record["cwd"] = cwd
    if model:
        record["model"] = model
    new_process_verified = str(process_identity.get("status") or "") == "verified"

    old_pane_record: dict = {}
    with locked_json(live_sessions_file(), {"version": 1, "panes": {}, "sessions": {}}) as data:
        panes = data.setdefault("panes", {})
        sessions = data.setdefault("sessions", {})
        old_pane_record = dict(panes.get(pane) or {})
        old_key = identity_key(str(old_pane_record.get("agent") or ""), str(old_pane_record.get("session_id") or ""))
        if old_pane_record and old_key in sessions and sessions[old_key].get("pane") == pane:
            del sessions[old_key]
        panes[pane] = record
        key = identity_key(agent_type, session_id)
        current_index = sessions.get(key) or {}
        current_index_pane = str(current_index.get("pane") or "")
        current_index_live = bool(current_index_pane and live_record_matches(panes.get(current_index_pane) or {}, agent_type, session_id))
        if current_index_pane == pane or not current_index_live:
            sessions[key] = record

    if new_process_verified:
        live_index = locked_json_read(live_sessions_file(), {"version": 1, "panes": {}, "sessions": {}})
        current_index = (live_index.get("sessions") or {}).get(identity_key(agent_type, session_id)) or {}
        current_index_pane = str(current_index.get("pane") or "")
        if current_index_pane and current_index_pane != pane and live_record_matches(current_index, agent_type, session_id):
            current_status = _live_record_current_status(current_index, agent_type, session_id)
            if current_status == "mismatch" or (current_status == "unknown" and not resume_from_unknown_disabled()):
                with locked_json(live_sessions_file(), {"version": 1, "panes": {}, "sessions": {}}) as data:
                    sessions = data.setdefault("sessions", {})
                    latest = sessions.get(identity_key(agent_type, session_id)) or {}
                    if str(latest.get("pane") or "") == current_index_pane:
                        sessions[identity_key(agent_type, session_id)] = record

    mapping = read_attached_mapping(agent_type, session_id) or recover_mapping_from_session_state(agent_type, session_id)
    if mapping:
        mapped_pane = str(mapping.get("pane") or "")
        mapped_live = read_live_by_pane(mapped_pane) if mapped_pane else {}
        if mapped_pane == pane:
            if bridge_session:
                mapping["bridge_session"] = bridge_session
            if alias:
                mapping["alias"] = alias
            return update_attached_endpoint(mapping, pane, target, cwd=cwd, model=model)
        if new_process_verified:
            old_status = _live_record_current_status(mapped_live, agent_type, session_id)
            if old_status != "verified":
                if bridge_session:
                    mapping["bridge_session"] = bridge_session
                if alias:
                    mapping["alias"] = alias
                reason = "mapped_endpoint_mismatch" if old_status == "mismatch" else "mapped_endpoint_unknown"
                reconnected = auto_reconnect_attached_endpoint(
                    mapping,
                    record,
                    reason,
                    old_pane=mapped_pane,
                    old_status=old_status,
                    clear_old_locks=old_status != "unknown",
                )
                if reconnected:
                    return reconnected
        if not live_record_matches(mapped_live, agent_type, session_id):
            return None
        mapped_status = _live_record_current_status(mapped_live, agent_type, session_id)
        if mapped_status != "verified" and new_process_verified:
            if bridge_session:
                mapping["bridge_session"] = bridge_session
            if alias:
                mapping["alias"] = alias
            reason = "mapped_endpoint_mismatch" if mapped_status == "mismatch" else "mapped_endpoint_unknown"
            reconnected = auto_reconnect_attached_endpoint(
                mapping,
                record,
                reason,
                old_pane=mapped_pane,
                old_status=mapped_status,
                clear_old_locks=mapped_status != "unknown",
            )
            if reconnected:
                return reconnected
        if mapped_status != "verified":
            return None
        duplicate_lock = read_pane_lock(pane)
        if duplicate_lock:
            if (
                str(duplicate_lock.get("hook_session_id") or "") == session_id
                and str(duplicate_lock.get("agent") or "") == agent_type
            ):
                remove_pane_lock(pane)
            else:
                mark_endpoint_lost_for_pane_lock(pane, f"pane now hosts duplicate {agent_type}:{session_id}")
        return None

    lock = read_pane_lock(pane)
    if lock and (
        str(lock.get("hook_session_id") or "") != session_id
        or str(lock.get("agent") or "") != agent_type
    ):
        mark_endpoint_lost_for_pane_lock(pane, f"pane now hosts {agent_type}:{session_id}")
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
                mapping = read_attached_mapping(str(live.get("agent") or ""), str(live.get("session_id") or "")) or recover_mapping_from_session_state(
                    str(live.get("agent") or ""),
                    str(live.get("session_id") or ""),
                )
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
        mapping = read_attached_mapping(str(live.get("agent") or ""), str(live.get("session_id") or "")) or recover_mapping_from_session_state(
            str(live.get("agent") or ""),
            str(live.get("session_id") or ""),
        )
        if mapping:
            mapped_pane = str(mapping.get("pane") or "")
            mapped_live = read_live_by_pane(mapped_pane) if mapped_pane else {}
            live_agent = str(live.get("agent") or "")
            live_session = str(live.get("session_id") or "")
            if mapped_pane != pane:
                mapped_status = _live_record_current_status(mapped_live, live_agent, live_session)
                if mapped_status == "verified":
                    return CallerResolution(
                        False,
                        error=(
                            f"{tool_name}: this pane is a duplicate resume of attached alias "
                            f"{mapping.get('alias')!r}; the active bridge endpoint is pane {mapped_pane}. "
                            "Use bridge_manage to join this pane as a different alias, or close the duplicate."
                        ),
                    )
                live_status = _live_record_current_status(live, live_agent, live_session)
                if live_status == "verified":
                    reason = "caller_endpoint_mismatch" if mapped_status == "mismatch" else "caller_endpoint_unknown"
                    reconnected = auto_reconnect_attached_endpoint(
                        mapping,
                        live,
                        reason,
                        old_pane=mapped_pane,
                        old_status=mapped_status,
                        clear_old_locks=mapped_status != "unknown",
                    )
                    if reconnected and str(reconnected.get("pane") or "") == pane:
                        mapping = reconnected
                    else:
                        return CallerResolution(
                            False,
                            error=(
                                f"{tool_name}: attached alias {mapping.get('alias')!r} has no verified live endpoint "
                                f"on pane {mapped_pane}; reattach/join this pane before sending."
                            ),
                        )
                elif live_record_matches(mapped_live, live_agent, live_session):
                    return CallerResolution(
                        False,
                        error=(
                            f"{tool_name}: attached alias {mapping.get('alias')!r} is mapped to pane {mapped_pane}, "
                            "and this pane has no verified process fingerprint for safe automatic reconnect."
                        ),
                    )
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


def _write_live_record(record: dict, *, allow_create: bool = False) -> bool:
    agent_type = str(record.get("agent") or "")
    session_id = str(record.get("session_id") or "")
    pane = str(record.get("pane") or "")
    if not (agent_type and session_id and pane):
        return False
    with locked_json(live_sessions_file(), {"version": 1, "panes": {}, "sessions": {}}) as data:
        panes = data.setdefault("panes", {})
        sessions = data.setdefault("sessions", {})
        existing_pane = panes.get(pane) if isinstance(panes.get(pane), dict) else {}
        existing_index = sessions.get(identity_key(agent_type, session_id)) if isinstance(sessions.get(identity_key(agent_type, session_id)), dict) else {}
        if not allow_create:
            if existing_pane and not live_record_matches(existing_pane, agent_type, session_id):
                return False
            if not live_record_matches(existing_pane, agent_type, session_id) and not live_record_matches(existing_index, agent_type, session_id):
                return False
        panes[pane] = dict(record)
        sessions[identity_key(agent_type, session_id)] = dict(record)
    return True


def verify_existing_live_process_identity(agent_type: str, session_id: str, pane: str) -> dict:
    if not (agent_type and session_id and pane):
        return {"status": "unknown", "reason": "missing_identity", "pane": pane, "agent": agent_type}
    live = read_live_by_pane(pane)
    if live and not live_record_matches(live, agent_type, session_id):
        return {
            "status": "mismatch",
            "reason": "live_record_mismatch",
            "pane": pane,
            "agent": agent_type,
            "detail": f"pane live record belongs to {live.get('agent')}:{live.get('session_id')}",
        }
    if not live:
        return {"status": "unknown", "reason": "live_record_missing", "pane": pane, "agent": agent_type}
    stored_identity = verified_process_identity(live)
    if not stored_identity:
        return {"status": "unknown", "reason": "missing_verified_prior", "pane": pane, "agent": agent_type}
    try:
        return probe_agent_process(pane, agent_type, stored_identity)
    except Exception as exc:
        return {"status": "unknown", "reason": "probe_exception", "detail": str(exc), "pane": pane, "agent": agent_type, "processes": []}


def backfill_session_process_identities(
    session: str,
    state: dict | None = None,
    aliases: list[str] | None = None,
    *,
    allow_create_from_hook: bool = False,
) -> dict:
    """Host-side repair/backfill for live process fingerprints.

    This proves only that the pane currently hosts the expected physical agent
    process. It cannot prove that an operator-supplied hook_session_id is
    semantically correct; normal attach probes remain the authoritative path for
    session-id discovery. By default this only refreshes a previously verified
    live endpoint. Callers may pass allow_create_from_hook=True only while
    handling a fresh normal attach/join hook proof for the same session id.
    """
    state = state or load_session(session)
    participants = active_participants(state)
    wanted = set(aliases or participants.keys())
    summary: dict[str, dict] = {}
    for alias, participant in participants.items():
        if alias not in wanted:
            continue
        agent_type = str(participant.get("agent_type") or participant.get("agent") or "")
        session_id = str(participant.get("hook_session_id") or "")
        pane = str(participant.get("pane") or "")
        if not (agent_type and session_id and pane):
            summary[alias] = {"status": "unknown", "reason": "missing_identity", "pane": pane}
            continue
        live = read_live_by_pane(pane)
        if live and not live_record_matches(live, agent_type, session_id):
            summary[alias] = {
                "status": "mismatch",
                "reason": "live_record_mismatch",
                "pane": pane,
                "agent": agent_type,
                "hook_session_id": session_id,
            }
            continue
        if live and verified_process_identity(live):
            process_identity = verify_existing_live_process_identity(agent_type, session_id, pane)
        elif allow_create_from_hook:
            try:
                process_identity = probe_agent_process(pane, agent_type)
            except Exception as exc:
                process_identity = {
                    "status": "unknown",
                    "reason": "probe_exception",
                    "detail": str(exc),
                    "pane": pane,
                    "agent": agent_type,
                    "processes": [],
                }
        else:
            reason = "missing_verified_prior" if live else "live_record_missing"
            summary[alias] = {
                "status": "unknown",
                "reason": reason,
                "pane": pane,
                "agent": agent_type,
                "hook_session_id": session_id,
            }
            continue
        status = str(process_identity.get("status") or "unknown")
        reason = str(process_identity.get("reason") or "")
        summary[alias] = {
            "status": status,
            "reason": reason,
            "pane": pane,
            "agent": agent_type,
            "hook_session_id": session_id,
        }
        if status != "verified":
            continue
        record = {
            "agent": agent_type,
            "session_id": session_id,
            "pane": pane,
            "target": str(participant.get("target") or tmux_target_for_pane(pane) or ""),
            "bridge_session": session,
            "alias": alias,
            "event": "process_backfill",
            "last_seen_at": utc_now(),
            "cwd": str(participant.get("cwd") or ""),
            "model": str(participant.get("model") or ""),
            "process_identity": process_identity,
        }
        if not _write_live_record(record, allow_create=allow_create_from_hook):
            summary[alias] = {
                "status": "unknown",
                "reason": "backfill_without_matching_live_record",
                "pane": pane,
                "agent": agent_type,
                "hook_session_id": session_id,
            }
    return summary


def _endpoint_detail(
    ok: bool,
    pane: str = "",
    reason: str = "",
    *,
    probe_status: str = "",
    detail: str = "",
    should_detach: bool = False,
    reconnected: dict | None = None,
) -> dict:
    out = {
        "ok": bool(ok),
        "pane": pane if ok else "",
        "reason": reason or ("ok" if ok else "unknown"),
        "probe_status": probe_status,
        "detail": detail,
        "should_detach": bool(should_detach),
    }
    if reconnected:
        out["reconnected"] = dict(reconnected)
    return out


def resolve_participant_endpoint_detail(bridge_session: str, alias: str, participant: dict, *, purpose: str = "write") -> dict:
    agent_type = str(participant.get("agent_type") or participant.get("agent") or "")
    session_id = str(participant.get("hook_session_id") or "")
    pane = str(participant.get("pane") or "")
    if not (agent_type and session_id and pane and bridge_session and alias):
        return _endpoint_detail(False, reason="missing_identity", detail="participant lacks bridge session, alias, agent, session id, or pane")

    mapping = read_attached_mapping(agent_type, session_id) or recover_mapping_from_session_state(agent_type, session_id)
    if mapping and (str(mapping.get("bridge_session") or "") != bridge_session or str(mapping.get("alias") or "") != alias):
        return _endpoint_detail(False, reason="mapping_mismatch", detail="attached mapping points at a different room or alias")
    if not mapping:
        return _endpoint_detail(False, reason="mapping_missing", detail="no attached mapping for participant identity")

    mapped_pane = str(mapping.get("pane") or "")
    def reconnect_to_verified_candidate(reason: str, old_pane: str, old_status: str, *, clear_old_locks: bool) -> dict | None:
        candidate = find_verified_live_record_for_identity(agent_type, session_id, prefer_pane=mapped_pane)
        if not candidate:
            return None
        return _auto_reconnect_endpoint_detail(
            mapping,
            candidate,
            reason,
            purpose=purpose,
            old_pane=old_pane,
            old_status=old_status,
            clear_old_locks=clear_old_locks and purpose != "read",
        )

    if mapped_pane and mapped_pane != pane:
        mapped_live = read_live_by_pane(mapped_pane)
        mapped_status = _live_record_current_status(mapped_live, agent_type, session_id)
        if mapped_status == "verified":
            detail = _auto_reconnect_endpoint_detail(
                mapping,
                mapped_live,
                "participant_pane_stale",
                purpose=purpose,
                old_pane=pane,
                old_status="verified",
                clear_old_locks=False,
            )
            if detail:
                return detail
            return _endpoint_detail(False, reason="pane_mismatch", detail=f"mapping pane {mapped_pane} differs from participant pane {pane}")
        reconnect = reconnect_to_verified_candidate(
            "mapped_endpoint_mismatch" if mapped_status == "mismatch" else "mapped_endpoint_unknown",
            mapped_pane,
            mapped_status,
            clear_old_locks=mapped_status != "unknown",
        )
        if reconnect:
            return reconnect
        return _endpoint_detail(False, reason="pane_mismatch", detail=f"mapping pane {mapped_pane} differs from participant pane {pane}")

    live_pane = read_live_by_pane(pane)
    if live_pane and not live_record_matches(live_pane, agent_type, session_id):
        reconnect = reconnect_to_verified_candidate("participant_endpoint_mismatch", pane, "mismatch", clear_old_locks=True)
        if reconnect:
            return reconnect
        if purpose != "read":
            mark_endpoint_lost_for_pane_lock(pane, f"pane now hosts {live_pane.get('agent')}:{live_pane.get('session_id')}")
        return _endpoint_detail(
            False,
            reason="live_record_mismatch",
            probe_status="mismatch",
            detail="pane live record belongs to another identity",
            should_detach=purpose != "read",
        )
    if not live_pane:
        reconnect = reconnect_to_verified_candidate("participant_endpoint_unknown", pane, "unknown", clear_old_locks=False)
        if reconnect:
            return reconnect
        return _endpoint_detail(False, reason="live_record_missing", detail="no live hook/backfill record for participant pane")

    stored_identity = live_pane.get("process_identity") or {}
    if not isinstance(stored_identity, dict) or str(stored_identity.get("status") or "") != "verified":
        reconnect = reconnect_to_verified_candidate("participant_endpoint_unknown", pane, "unknown", clear_old_locks=False)
        if reconnect:
            return reconnect
        return _endpoint_detail(
            False,
            reason="missing_process_fingerprint",
            probe_status=str((stored_identity or {}).get("status") or "unknown") if isinstance(stored_identity, dict) else "unknown",
            detail="live record has no verified process fingerprint; run bin/bridge_healthcheck.sh --backfill-endpoints",
        )

    try:
        probe = probe_agent_process(pane, agent_type, stored_identity)
    except Exception as exc:
        return _endpoint_detail(False, reason="probe_unknown", probe_status="unknown", detail=str(exc))
    status = str(probe.get("status") or "unknown")
    reason = str(probe.get("reason") or "")
    if status == "verified":
        return _endpoint_detail(True, pane=pane, reason="ok", probe_status="verified", detail=reason)
    if status == "mismatch":
        reconnect = reconnect_to_verified_candidate("process_mismatch", pane, "mismatch", clear_old_locks=True)
        if reconnect:
            return reconnect
        if purpose != "read":
            mark_endpoint_lost_for_pane_lock(pane, f"process mismatch for {agent_type}:{session_id}: {reason}")
        return _endpoint_detail(
            False,
            reason="process_mismatch" if reason != "pane_unavailable" else "pane_unavailable",
            probe_status="mismatch",
            detail=reason,
            should_detach=purpose != "read",
        )
    reconnect = reconnect_to_verified_candidate("probe_unknown", pane, "unknown", clear_old_locks=False)
    if reconnect:
        return reconnect
    return _endpoint_detail(False, reason="probe_unknown", probe_status="unknown", detail=reason)


def resolve_participant_endpoint(bridge_session: str, alias: str, participant: dict) -> str:
    detail = resolve_participant_endpoint_detail(bridge_session, alias, participant)
    return str(detail.get("pane") or "") if detail.get("ok") else ""


def active_participant_endpoint(bridge_session: str, alias: str) -> str:
    state = load_session(bridge_session)
    participant = active_participants(state).get(alias)
    if not participant:
        return ""
    return resolve_participant_endpoint(bridge_session, alias, participant)
