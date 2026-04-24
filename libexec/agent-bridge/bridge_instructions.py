#!/usr/bin/env python3
from __future__ import annotations


def model_cheat_sheet() -> list[str]:
    return [
        "Commands:",
        "- agent_list_peers : list aliases and show this cheat sheet.",
        "- agent_send_peer --to <alias> 'request' : ask one peer; keep working while the bridge returns its result.",
        "- agent_send_peer --all 'message' : broadcast to every other participant. Do not put an alias before the body.",
        "- agent_send_peer --kind notice --to <alias> 'FYI' : send info without expecting a reply.",
        "- agent_view_peer <alias> --onboard : capture a stable screen/history snapshot.",
        "- agent_view_peer <alias> --older : page older lines from that snapshot.",
        "- agent_view_peer <alias> --since-last : view latest changes since your last view.",
        "- agent_view_peer <alias> --search 'text' [--live] : search saved snapshot, or current live screen with --live.",
        "Receiving: after sending a request, end your turn. The peer's reply arrives automatically as a [bridge:*] result, same delivery path as a user prompt. Do not poll with agent_view_peer or schedule a wakeup to check progress; use agent_view_peer only when you suspect the peer is stuck or need to debug the bridge.",
        "Kinds: request expects one normal peer response; result is a returned peer answer; notice expects no response.",
    ]


def model_cheat_sheet_text() -> str:
    return "\n".join(model_cheat_sheet())


def probe_prompt(mode: str, probe_id: str, alias: str, peers: str) -> str:
    return (
        f"[bridge-probe:{probe_id}] Bridge {mode}. You are alias '{alias}' in this bridge room. "
        f"Current peers: {peers}. "
        "This is a live collaboration bridge; continue answering the human normally in this pane. "
        "Use shell commands for peer collaboration: "
        "agent_list_peers shows aliases and the command cheat sheet; "
        "agent_send_peer --to <alias> 'request' asks one peer and lets you keep working while the result returns later as a [bridge:*] result; "
        "agent_send_peer --all 'message' broadcasts to every other participant; "
        "agent_send_peer --kind notice --to <alias> 'FYI' sends information without expecting a reply; "
        "agent_view_peer <alias> --onboard captures a stable peer screen/history snapshot; "
        "--older pages older snapshot lines; --since-last shows latest changes; "
        "--search 'text' searches the snapshot; --search 'text' --live searches current live screen. "
        "When a peer request arrives, reply normally; the bridge returns your normal response automatically. "
        "After you send a request, end your turn — the peer's reply arrives automatically as a [bridge:*] result, same delivery path as a user prompt. "
        "Do not poll with agent_view_peer or schedule a wakeup to check progress; use agent_view_peer only when you suspect the peer is stuck or need to debug the bridge. "
        "If you forget the protocol, run agent_list_peers. "
        f"Reply exactly: bridge {mode}ed {probe_id}"
    )
