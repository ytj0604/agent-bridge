#!/usr/bin/env python3
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from bridge_paths import controlled_clears_file
from bridge_util import locked_json, locked_json_read, utc_now


CONTROLLED_CLEAR_MARKER_TTL_SECONDS = 180.0
CONTROLLED_CLEAR_MARKER_EXPIRY_MARGIN_SECONDS = 5.0


def ttl_for_clear_lifetime(*, settle_delay_seconds: float, probe_timeout_seconds: float) -> float:
    """Return a marker TTL that covers the configured clear lifetime."""
    try:
        settle = float(settle_delay_seconds)
    except (TypeError, ValueError):
        settle = 0.0
    try:
        probe_timeout = float(probe_timeout_seconds)
    except (TypeError, ValueError):
        probe_timeout = 0.0
    if settle < 0.0 or not (settle < float("inf")):
        settle = 0.0
    if probe_timeout < 0.0 or not (probe_timeout < float("inf")):
        probe_timeout = 0.0
    clear_lifetime = settle + probe_timeout + CONTROLLED_CLEAR_MARKER_EXPIRY_MARGIN_SECONDS
    return max(CONTROLLED_CLEAR_MARKER_TTL_SECONDS, clear_lifetime)


def _default() -> dict:
    return {"version": 1, "markers": {}}


def marker_id(bridge_session: str, alias: str, probe_id: str) -> str:
    return f"{bridge_session}:{alias}:{probe_id}"


def marker_path() -> Path:
    return controlled_clears_file()


def read_markers() -> dict:
    data = locked_json_read(marker_path(), _default())
    if not isinstance(data, dict):
        return _default()
    data.setdefault("version", 1)
    data.setdefault("markers", {})
    return data


def write_marker(marker: dict) -> str:
    mid = str(marker.get("id") or marker_id(str(marker.get("bridge_session") or ""), str(marker.get("alias") or ""), str(marker.get("probe_id") or "")))
    marker["id"] = mid
    with locked_json(marker_path(), _default()) as data:
        markers = data.setdefault("markers", {})
        markers[mid] = dict(marker)
    return mid


def remove_marker(mid: str) -> dict:
    removed: dict = {}
    with locked_json(marker_path(), _default()) as data:
        markers = data.setdefault("markers", {})
        record = markers.pop(str(mid), None)
        if isinstance(record, dict):
            removed = dict(record)
    return removed


def update_marker(mid: str, mutator: Callable[[dict], dict | None]) -> dict | None:
    out: dict | None = None
    with locked_json(marker_path(), _default()) as data:
        markers = data.setdefault("markers", {})
        current = markers.get(str(mid))
        if not isinstance(current, dict):
            return None
        updated = mutator(dict(current))
        if updated is None:
            return None
        updated["id"] = str(mid)
        markers[str(mid)] = dict(updated)
        out = dict(updated)
    return out


def _expired(marker: dict, now_ts: float | None = None) -> bool:
    now_ts = time.time() if now_ts is None else now_ts
    try:
        expires = float(marker.get("expires_ts") or 0.0)
    except (TypeError, ValueError):
        expires = 0.0
    return bool(expires and expires <= now_ts)


def cleanup_expired_or_orphaned(active_marker_ids: set[str] | None = None) -> list[dict]:
    active_marker_ids = set(active_marker_ids or set())
    removed: list[dict] = []
    now_ts = time.time()
    with locked_json(marker_path(), _default()) as data:
        markers = data.setdefault("markers", {})
        for mid, marker in list(markers.items()):
            if not isinstance(marker, dict):
                removed.append({"id": mid, "reason": "malformed"})
                del markers[mid]
                continue
            if _expired(marker, now_ts) or (active_marker_ids and mid not in active_marker_ids):
                removed.append(dict(marker))
                del markers[mid]
    return removed


def make_marker(
    *,
    bridge_session: str,
    alias: str,
    agent: str,
    old_session_id: str,
    probe_id: str,
    pane: str,
    target: str,
    events_file: str,
    public_events_file: str = "",
    caller: str = "",
    clear_id: str = "",
    ttl_seconds: float | None = None,
) -> dict:
    now_ts = time.time()
    mid = marker_id(bridge_session, alias, probe_id)
    try:
        ttl = float(ttl_seconds) if ttl_seconds is not None else CONTROLLED_CLEAR_MARKER_TTL_SECONDS
    except (TypeError, ValueError):
        ttl = CONTROLLED_CLEAR_MARKER_TTL_SECONDS
    if ttl <= 0.0 or not (ttl < float("inf")):
        ttl = CONTROLLED_CLEAR_MARKER_TTL_SECONDS
    return {
        "id": mid,
        "bridge_session": bridge_session,
        "alias": alias,
        "agent": agent,
        "old_session_id": old_session_id,
        "probe_id": probe_id,
        "pane": pane,
        "target": target,
        "events_file": events_file,
        "public_events_file": public_events_file,
        "caller": caller,
        "clear_id": clear_id,
        "phase": "pending_prompt",
        "created_at": utc_now(),
        "created_ts": now_ts,
        "deadline_ts": now_ts + ttl,
        "expires_ts": now_ts + ttl,
    }


def find_for_prompt(*, pane: str, agent: str, attach_probe: str) -> dict | None:
    if not (agent and attach_probe):
        return None
    candidates: list[dict] = []
    data = read_markers()
    for marker in (data.get("markers") or {}).values():
        if not isinstance(marker, dict) or _expired(marker):
            continue
        if str(marker.get("agent") or "") != agent:
            continue
        if str(marker.get("probe_id") or "") != attach_probe:
            continue
        candidates.append(dict(marker))
        if pane and str(marker.get("pane") or "") == pane:
            return dict(marker)
    if len(candidates) == 1:
        return candidates[0]
    return None


def mark_session_seen(marker: dict, *, new_session_id: str) -> dict | None:
    mid = str(marker.get("id") or "")
    if not mid:
        return None

    def mutate(current: dict) -> dict:
        if new_session_id:
            current["new_session_id"] = new_session_id
        current["session_seen_at"] = utc_now()
        return current

    return update_marker(mid, mutate)


def _matches_marker_session(marker: dict, session_id: str) -> bool:
    if not session_id:
        return False
    return session_id in {
        str(marker.get("old_session_id") or ""),
        str(marker.get("new_session_id") or ""),
    }


def _unique_marker(candidates: list[dict]) -> dict | None:
    if len(candidates) == 1:
        return candidates[0]
    return None


def find_for_stop(*, pane: str, agent: str, session_id: str = "", turn_id: str = "") -> dict | None:
    if not agent:
        return None
    candidates: list[dict] = []
    data = read_markers()
    for marker in (data.get("markers") or {}).values():
        if not isinstance(marker, dict) or _expired(marker):
            continue
        if str(marker.get("agent") or "") != agent:
            continue
        if str(marker.get("phase") or "") not in {"prompt_seen", "finalizing"}:
            continue
        if pane:
            if str(marker.get("pane") or "") != pane:
                continue
            if session_id and str(marker.get("new_session_id") or "") == session_id:
                return dict(marker)
            if turn_id and str(marker.get("probe_turn_id") or "") == turn_id:
                return dict(marker)
            continue
        if session_id and str(marker.get("new_session_id") or "") == session_id:
            candidates.append(dict(marker))
    return _unique_marker(candidates)


def find_for_clear_window(*, pane: str, agent: str, session_id: str = "") -> dict | None:
    if not agent:
        return None
    candidates: list[dict] = []
    data = read_markers()
    for marker in (data.get("markers") or {}).values():
        if not isinstance(marker, dict) or _expired(marker):
            continue
        if str(marker.get("agent") or "") != agent:
            continue
        if pane:
            if str(marker.get("pane") or "") == pane:
                return dict(marker)
            continue
        if _matches_marker_session(marker, session_id):
            candidates.append(dict(marker))
    return _unique_marker(candidates)


def find_for_old_session_end(*, pane: str, agent: str, old_session_id: str) -> dict | None:
    if not (pane and agent and old_session_id):
        return None
    candidates: list[dict] = []
    data = read_markers()
    for marker in (data.get("markers") or {}).values():
        if not isinstance(marker, dict) or _expired(marker):
            continue
        if str(marker.get("pane") or "") != pane:
            continue
        if str(marker.get("agent") or "") != agent:
            continue
        if str(marker.get("old_session_id") or "") != old_session_id:
            continue
        candidates.append(dict(marker))
    return _unique_marker(candidates)


def mark_prompt_seen(marker: dict, *, new_session_id: str, probe_turn_id: str) -> dict | None:
    mid = str(marker.get("id") or "")
    if not mid:
        return None

    def mutate(current: dict) -> dict:
        current["phase"] = "prompt_seen"
        if new_session_id:
            current["new_session_id"] = new_session_id
        if probe_turn_id:
            current["probe_turn_id"] = probe_turn_id
        current["prompt_seen_at"] = utc_now()
        return current

    return update_marker(mid, mutate)
