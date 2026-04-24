#!/usr/bin/env python3
import argparse
import json
import os
import re
import sys
from pathlib import Path

from bridge_paths import state_root
from bridge_identity import update_live_session
from bridge_util import append_jsonl, locked_json_read, public_record as redact_public_record, utc_now


NONCE_PATTERN = re.compile(r"\[bridge:([^\]]+)\]")
ATTACH_PROBE_PATTERN = re.compile(r"\[bridge-probe:([^\]]+)\]")
STATE_ROOT = state_root()
ATTACH_REGISTRY = STATE_ROOT / "attached-sessions.json"
ATTACH_DISCOVERY = STATE_ROOT / "attach-discovery.jsonl"
PUBLIC_RECORD_FIELDS = (
    "ts",
    "agent",
    "bridge_session",
    "bridge_agent",
    "event",
    "hook_event_name",
    "session_id",
    "turn_id",
    "cwd",
    "model",
    "nonce",
    "attach_probe",
    "pane",
    "tool_name",
    "source",
    "notification_type",
    "title",
    "message",
    "stop_hook_active",
)


def extract_nonce(text: str | None) -> str | None:
    if not text:
        return None
    match = NONCE_PATTERN.search(text)
    return match.group(1) if match else None


def extract_attach_probe(text: str | None) -> str | None:
    if not text:
        return None
    match = ATTACH_PROBE_PATTERN.search(text)
    return match.group(1) if match else None


def normalized_event(payload: dict) -> str:
    hook_event = payload.get("hook_event_name", "unknown")
    notification_type = payload.get("notification_type")

    if hook_event == "SessionStart":
        return "session_start"
    if hook_event == "UserPromptSubmit":
        return "prompt_submitted"
    if hook_event == "Stop":
        return "response_finished"
    if hook_event == "PermissionRequest":
        return "approval_pending"
    if hook_event == "SessionEnd":
        return "session_ended"
    if hook_event == "Notification":
        if notification_type == "permission_prompt":
            return "approval_prompt"
        if notification_type == "idle_prompt":
            return "idle_prompt"
        return "notification"
    return hook_event.lower()


def build_record(agent: str, payload: dict) -> dict:
    prompt = payload.get("prompt")
    last_message = payload.get("last_assistant_message")
    bridge_session = os.environ.get("AGENT_BRIDGE_SESSION")
    bridge_agent = os.environ.get("AGENT_BRIDGE_AGENT")
    record = {
        "ts": utc_now(),
        "agent": agent,
        "bridge_session": bridge_session,
        "bridge_agent": bridge_agent,
        "event": normalized_event(payload),
        "hook_event_name": payload.get("hook_event_name"),
        "session_id": payload.get("session_id"),
        "turn_id": payload.get("turn_id"),
        "cwd": payload.get("cwd"),
        "transcript_path": payload.get("transcript_path"),
        "model": payload.get("model"),
        "nonce": extract_nonce(prompt),
        "attach_probe": extract_attach_probe(prompt),
        "prompt": prompt,
        "last_assistant_message": last_message,
        "tool_name": payload.get("tool_name"),
        "tool_input": payload.get("tool_input"),
        "source": payload.get("source"),
        "notification_type": payload.get("notification_type"),
        "title": payload.get("title"),
        "message": payload.get("message"),
        "stop_hook_active": payload.get("stop_hook_active"),
        "pane": os.environ.get("TMUX_PANE"),
    }
    return {key: value for key, value in record.items() if value is not None}


def attach_registry_path() -> Path:
    return Path(os.environ.get("AGENT_BRIDGE_ATTACH_REGISTRY", str(ATTACH_REGISTRY)))


def read_registry(path: Path | None = None) -> dict:
    if path is None:
        path = attach_registry_path()
    data = locked_json_read(path, {"version": 1, "sessions": {}})
    if not isinstance(data, dict):
        return {"version": 1, "sessions": {}}
    data.setdefault("version", 1)
    data.setdefault("sessions", {})
    return data


def attached_mapping(agent: str, session_id: object) -> dict | None:
    if not session_id:
        return None
    key = f"{agent}:{session_id}"
    mapping = read_registry().get("sessions", {}).get(key)
    return mapping if isinstance(mapping, dict) else None


def attach_discovery_path() -> Path:
    return Path(os.environ.get("AGENT_BRIDGE_ATTACH_DISCOVERY", str(ATTACH_DISCOVERY)))


def load_payload() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"hook_event_name": "raw_input", "raw_stdin": raw}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", choices=["claude", "codex"], required=True)
    parser.add_argument(
        "--state-file",
        default=str(STATE_ROOT / "events.jsonl"),
    )
    parser.add_argument("--public-state-file")
    args = parser.parse_args()

    payload = load_payload()
    record = build_record(args.agent, payload)

    bridge_session = os.environ.get("AGENT_BRIDGE_SESSION")
    mapping = None
    if not bridge_session:
        mapping = attached_mapping(args.agent, payload.get("session_id"))
        if mapping:
            bridge_session = mapping.get("bridge_session")
            record["bridge_session"] = bridge_session
            record["bridge_agent"] = mapping.get("alias") or args.agent

    live_mapping = update_live_session(
        agent_type=args.agent,
        session_id=str(payload.get("session_id") or ""),
        pane=str(os.environ.get("TMUX_PANE") or ""),
        bridge_session=str(record.get("bridge_session") or ""),
        alias=str(record.get("bridge_agent") or ""),
        event=str(record.get("event") or ""),
    )
    if live_mapping:
        mapping = live_mapping
        record["bridge_session"] = live_mapping.get("bridge_session")
        record["bridge_agent"] = live_mapping.get("alias") or args.agent
        record["pane"] = live_mapping.get("pane") or record.get("pane")

    should_log_unscoped = os.environ.get("AGENT_BRIDGE_LOG_UNSCOPED") == "1"

    if record.get("attach_probe"):
        append_jsonl(attach_discovery_path(), redact_public_record(record, allowed_fields=PUBLIC_RECORD_FIELDS))

    if not bridge_session and record.get("attach_probe"):
        if args.agent == "codex" and payload.get("hook_event_name") == "Stop":
            sys.stdout.write("{}\n")
        return 0

    if not bridge_session and not should_log_unscoped:
        if args.agent == "codex" and payload.get("hook_event_name") == "Stop":
            sys.stdout.write("{}\n")
        return 0

    state_file = (
        os.environ.get("AGENT_BRIDGE_BUS")
        or (mapping or {}).get("events_file")
        or os.environ.get("AGENT_BRIDGE_EVENTS")
        or args.state_file
    )
    public_state_file = (
        os.environ.get("AGENT_BRIDGE_PUBLIC_EVENTS")
        or (mapping or {}).get("public_events_file")
        or args.public_state_file
    )
    append_jsonl(Path(state_file), record)
    if public_state_file and public_state_file != state_file:
        append_jsonl(Path(public_state_file), redact_public_record(record, allowed_fields=PUBLIC_RECORD_FIELDS))

    # Codex Stop hooks require JSON on stdout when exiting 0.
    if args.agent == "codex" and payload.get("hook_event_name") == "Stop":
        sys.stdout.write("{}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
