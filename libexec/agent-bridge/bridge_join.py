#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path

from bridge_attach import (
    AttachProbeTimeout,
    discovery_file,
    infer_agent_type,
    interactive_select,
    list_panes,
    load_process_table,
    pane_locks_file,
    prompt_participant_aliases,
    registry_file,
    resolve_pane,
    send_prompt,
    wait_for_probe,
)
from bridge_identity import backfill_session_process_identities, verify_existing_live_process_identity
from bridge_participants import active_participants, format_peer_summary, load_session, normalize_agent_type, participant_record, save_session_state, session_state_exists
from bridge_instructions import probe_prompt
from bridge_paths import ensure_runtime_writable, libexec_dir, state_root
from bridge_util import locked_json, locked_json_read, utc_now


def next_alias(state: dict, agent_type: str) -> str:
    participants = active_participants(state)
    idx = 1
    while True:
        alias = f"{agent_type}{idx}"
        if alias not in participants:
            return alias
        idx += 1


def update_registry(mapping: dict) -> None:
    """Incrementally add one participant while replacing stale records for it.

    Full attach replaces all records for a room. Join is intentionally narrower:
    it preserves existing participants and only removes prior records that refer
    to the same hook session or alias in this bridge room. Pane alone is not a
    stable identity because agents can be killed/resumed in reused tmux panes.
    """
    with locked_json(registry_file(), {"version": 1, "sessions": {}}) as data:
        sessions = data.setdefault("sessions", {})
        bridge_session = mapping["bridge_session"]
        for key, record in list(sessions.items()):
            if record.get("bridge_session") != bridge_session:
                continue
            if record.get("session_id") == mapping["session_id"] or record.get("alias") == mapping["alias"]:
                del sessions[key]
        sessions[f"{mapping['agent']}:{mapping['session_id']}"] = mapping


def update_pane_lock(mapping: dict) -> None:
    with locked_json(pane_locks_file(), {"version": 1, "panes": {}}) as data:
        panes = data.setdefault("panes", {})
        panes[mapping["pane"]] = {
            "bridge_session": mapping["bridge_session"],
            "agent": mapping["agent"],
            "alias": mapping["alias"],
            "target": mapping["target"],
            "hook_session_id": mapping["session_id"],
            "locked_at": mapping["attached_at"],
        }


def enqueue_membership_notice(session: str, body: str) -> None:
    subprocess.run(
        [
            sys.executable,
            str(libexec_dir() / "bridge_enqueue.py"),
            "--session",
            session,
            "--from",
            "bridge",
            "--all",
            "--kind",
            "notice",
            "--intent",
            "membership_update",
            "--body",
            body,
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def read_pane_locks() -> dict:
    data = locked_json_read(pane_locks_file(), {"version": 1, "panes": {}})
    return data.get("panes") or {}


def pane_is_registered(pane_id: str, state: dict) -> bool:
    for record in (state.get("participants") or {}).values():
        if (record.get("status") or "active") != "active":
            continue
        if str(record.get("pane") or "") == pane_id:
            return True
    lock = read_pane_locks().get(pane_id)
    return isinstance(lock, dict) and bool(lock.get("bridge_session"))


def select_join_participant(state: dict, args: argparse.Namespace) -> dict:
    process_table = load_process_table()
    candidates = []
    for pane in list_panes():
        if pane_is_registered(pane["pane_id"], state):
            continue
        agent = infer_agent_type(pane, process_table)
        if agent:
            candidates.append({"pane": {**pane, "detected_agent": agent}, "agent": agent, "selected": False})

    if not candidates:
        detail = process_table.get("error") or "all detected Claude Code/Codex panes are already registered"
        raise SystemExit(f"no unregistered Claude Code/Codex panes available to join ({detail})")

    if not sys.stdin.isatty():
        raise SystemExit("join requires a TTY to select an unregistered pane; pass --agent and --pane explicitly for non-interactive use")

    panes = [item["pane"] for item in candidates]
    pane = interactive_select("unregistered agent", panes)
    agent_type = pane.get("detected_agent") or infer_agent_type(pane, process_table)
    default_alias = args.alias or next_alias(state, agent_type)
    participant = {
        "alias": default_alias,
        "agent": agent_type,
        "pane": pane,
        "session_id": "",
    }
    participant = prompt_participant_aliases([participant])[0]
    return {"alias": participant["alias"], "agent_type": participant["agent"], "pane": pane}


def main() -> int:
    parser = argparse.ArgumentParser(description="Join an existing Claude/Codex pane to a bridge session.")
    parser.add_argument("-s", "--session", default="agent-bridge-auto")
    parser.add_argument("--agent", "--agent-type", dest="agent_type", choices=["claude", "codex"])
    parser.add_argument("--alias")
    parser.add_argument("--pane", help="tmux pane id or target to join")
    parser.add_argument("--pane-target", help="display target to store when --no-resolve-pane is used")
    parser.add_argument("--cwd", default="")
    parser.add_argument("--no-resolve-pane", action="store_true", help="advanced/test mode: do not ask tmux to resolve the pane")
    parser.add_argument("--probe-timeout", type=float, default=45.0)
    parser.add_argument("--submit-delay", type=float, default=0.4)
    parser.add_argument("--no-probe", action="store_true")
    parser.add_argument("--session-id")
    parser.add_argument("--no-notify", action="store_true")
    parser.add_argument("--json", action="store_true", help="print join result as JSON")
    args = parser.parse_args()

    try:
        ensure_runtime_writable()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    if not session_state_exists(args.session):
        raise SystemExit(
            f"bridge room {args.session!r} is not present under pinned state root {state_root()}. "
            "Use bridge_run to create/attach the room, or check the runtime config path with bridge_healthcheck."
        )

    state = load_session(args.session)
    participants = active_participants(state)
    if args.agent_type or args.pane:
        if not args.agent_type or not args.pane:
            raise SystemExit("use --agent and --pane together, or omit both for interactive pane selection")
        agent_type = normalize_agent_type(args.agent_type)
        alias = args.alias or next_alias(state, agent_type)
        if args.no_resolve_pane:
            pane = {
                "pane_id": args.pane,
                "target": args.pane_target or args.pane,
                "cwd": args.cwd,
            }
        else:
            pane = resolve_pane(args.pane, list_panes())
    else:
        selected = select_join_participant(state, args)
        agent_type = normalize_agent_type(selected["agent_type"])
        pane = selected["pane"]
        alias = args.alias or selected["alias"]

    if alias in participants:
        raise SystemExit(f"alias already exists in {args.session}: {alias}")
    if pane_is_registered(pane["pane_id"], state):
        raise SystemExit(f"pane is already registered: {pane['pane_id']}")

    if args.no_probe:
        if not args.session_id:
            raise SystemExit("--no-probe requires --session-id")
        probe_record = {"session_id": args.session_id}
        hook_session_id = args.session_id
        probe = verify_existing_live_process_identity(agent_type, hook_session_id, str(pane.get("pane_id") or ""))
        if probe.get("status") != "verified":
            raise SystemExit(
                f"--no-probe could not verify {alias} pane process: {probe.get('status')}:{probe.get('reason')}. "
                "--no-probe is expert-only: host-side probing verifies the pane process, "
                "but cannot prove the supplied hook_session_id is correct. Use normal probing unless you know the session id."
            )
    else:
        discovery_file().parent.mkdir(parents=True, exist_ok=True)
        discovery_file().touch(exist_ok=True)
        probe_id = f"{args.session}:{agent_type}:{uuid.uuid4().hex[:10]}"
        peers = format_peer_summary(state)
        prompt = probe_prompt("join", probe_id, alias, peers)
        print(f"Probing {alias} pane {pane['pane_id']} ({pane['target']})")
        send_prompt(pane["pane_id"], prompt, args.submit_delay)
        try:
            probe_record = wait_for_probe(
                probe_id,
                agent_type,
                args.probe_timeout,
                alias=alias,
                pane_desc=f"{pane['pane_id']} ({pane['target']})",
                pane_id=pane["pane_id"],
            )
            hook_session_id = probe_record["session_id"]
        except AttachProbeTimeout as exc:
            raise SystemExit(str(exc)) from exc

    state.setdefault("participants", {})
    state.setdefault("panes", {})
    state.setdefault("targets", {})
    state.setdefault("hook_session_ids", {})
    endpoint_summary = {}
    if args.no_probe:
        endpoint_summary = dict(probe)
    state["participants"][alias] = participant_record(
        alias=alias,
        agent_type=agent_type,
        pane=pane["pane_id"],
        target=pane["target"],
        session_id=hook_session_id,
        cwd=probe_record.get("cwd") or pane.get("cwd") or "",
        model=probe_record.get("model") or "",
    )
    if endpoint_summary:
        state["participants"][alias]["endpoint_backfill"] = endpoint_summary
    state["panes"][alias] = pane["pane_id"]
    state["targets"][alias] = pane["target"]
    state["hook_session_ids"][alias] = hook_session_id
    save_session_state(state)

    mapping = {
        "agent": agent_type,
        "alias": alias,
        "session_id": hook_session_id,
        "bridge_session": args.session,
        "pane": pane["pane_id"],
        "target": pane["target"],
        "events_file": str(Path(state.get("bus_file") or Path(state.get("state_dir", "")) / "events.raw.jsonl")),
        "public_events_file": str(Path(state.get("events_file") or Path(state.get("state_dir", "")) / "events.jsonl")),
        "queue_file": str(Path(state.get("queue_file") or Path(state.get("state_dir", "")) / "pending.json")),
        "attached_at": utc_now(),
    }
    update_registry(mapping)
    update_pane_lock(mapping)
    if not args.no_probe:
        backfill_summary = backfill_session_process_identities(args.session, state, aliases=[alias], allow_create_from_hook=True)
        state["participants"][alias]["endpoint_backfill"] = backfill_summary.get(alias, {})
        save_session_state(state)

    if not args.no_notify:
        body = (
            f"Bridge membership: {alias} joined as {agent_type}. "
            f"{format_peer_summary(state)} "
            "Use agent_list_peers for details."
        )
        enqueue_membership_notice(args.session, body)

    result = {"session": args.session, "joined": alias, "participants": sorted(active_participants(state))}
    if args.json:
        print(json.dumps(result, ensure_ascii=True, indent=2))
    else:
        print(f"Joined {alias} ({agent_type}) to {args.session}: {pane['pane_id']} {pane['target']}")
        print(f"Participants: {', '.join(result['participants'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
