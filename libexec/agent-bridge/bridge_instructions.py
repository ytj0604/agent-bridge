#!/usr/bin/env python3
from __future__ import annotations


def model_cheat_sheet() -> list[str]:
    return [
        "Commands:",
        "- agent_list_peers : list aliases and show this cheat sheet.",
        "- agent_send_peer --to <alias> 'request' : ask one peer; keep working while the bridge auto-routes the peer's reply back to you as a [bridge:*] result. Inline body must be one shell argument.",
        "- agent_send_peer --to <a>,<b>[,...] 'request' : partial broadcast to the listed peers; same aggregate UX as --all, one merged result returns once all listed peers reply.",
        "- agent_send_peer --all 'message' : broadcast one request to every other peer; one aggregated result returns after all peers reply. Do not put an alias before the body.",
        "- agent_send_peer --kind notice --to <alias> 'FYI' : send info without expecting a reply. The bridge does NOT route any peer reply back; if you want a safety wake set agent_alarm separately.",
        "- agent_send_peer --watchdog <sec> [--to <alias>|--to <a>,<b>|--all] 'request' : add a watchdog that wakes you with a [bridge:watchdog] notice if the request has not been answered <sec> seconds after the prompt is delivered to the peer. Put options before the destination. Request only. --watchdog 0 disables the default. Default delay is set via env AGENT_BRIDGE_DEFAULT_WATCHDOG_SEC (300).",
        "- For apostrophes, newlines, text beginning with '-', or any complex body, use stdin: agent_send_peer --to <alias> --stdin <<'EOF' ... EOF. Quote the heredoc delimiter as <<'EOF' when the body must be literal.",
        "- agent_alarm <sec> [--note 'text'] : schedule a self-addressed wake notice. The alarm is automatically cancelled when ANY incoming peer message (kind != result, from != you, from != bridge) arrives at you; that triggering message is prepended with a [bridge:alarm_cancelled] notice telling you to re-arm if it is not what you were waiting for.",
        "- agent_extend_wait <message_id> <sec> : after a watchdog wake, keep waiting on the SAME request for <sec> more seconds. Only the original sender can extend; aggregate broadcasts cannot be per-message extended.",
        "- agent_interrupt_peer <alias> : use when you sent the wrong prompt or a peer is stuck. ESC + cancel the active message; it is removed, not requeued. No default hold: queued or newly sent corrections may deliver after ESC succeeds. Identifiable late output from the cancelled turn is ignored.",
        "- agent_interrupt_peer <alias> --clear-hold : force-release a legacy held_interrupt without sending ESC. UNSAFE if the peer might still be running a turn — late Stop events can misroute. Inspect with --status first.",
        "- agent_interrupt_peer [<alias>] --status : print busy / held / current_prompt_id / delivered+pending counts for the target (or all peers if no alias).",
        "- agent_view_peer <alias> --onboard [--tail N] : capture a stable screen/history snapshot.",
        "- agent_view_peer <alias> --older : page older lines from that snapshot.",
        "- agent_view_peer <alias> --since-last : view latest changes since your last view.",
        "- agent_view_peer <alias> --search 'text' [--live] : search saved snapshot, or current live screen with --live.",
        "Kinds and routing contract:",
        "- request: the bridge auto-routes the peer's next response back to you as a [bridge:*] result. Sender stays woken up by that reply arrival. Default kind. Watchdog applies. Only request and notice can be sent by agents — 'result' kind is system-only (set by the bridge for auto-returns).",
        "- notice: the bridge does NOT route any reply back. Even if the peer writes a response, it stays in the peer's pane — observe via agent_view_peer. If you need an answer, use request, not notice. The kind on the wire beats any 'please reply' hint in the body.",
        "- result: internal kind for auto-returned replies. Cannot be sent directly by agents.",
        "Waking and timing:",
        "- After sending a request, do not sleep or poll for the reply. Continue independent local work or end your turn. The peer's reply arrives automatically as a [bridge:*] result. With the default watchdog enabled you also get a [bridge:watchdog] notice if the peer takes too long.",
        "- A watchdog wake explicitly tells you to choose ONE of: agent_extend_wait <msg_id> <sec>, agent_interrupt_peer <alias>, or just agent_view_peer <alias> first to inspect. Pick one — it's not a polling primitive.",
        "- agent_alarm is for 'I delegated work via notice and want a safety wake if no follow-up arrives'. It is NOT for waiting on auto-routed reply results — for that use --watchdog / agent_extend_wait.",
        "- Do not poll with agent_view_peer or schedule a wakeup to check progress; use agent_view_peer only when you suspect the peer is stuck or need to debug the bridge.",
        "- Never read bridge state files directly. Replies arrive as [bridge:*] prompts; use the bridge commands above for everything else.",
        "- Large payloads (design docs, code, long plans): inline agent_send_peer bodies are limited to 11000 chars. Write larger content to a shared path under /tmp/agent-bridge-share/ and send only the path + brief description. Peers read the file directly. Inlining big bodies is slow and can break paste-burst submit.",
    ]


def model_cheat_sheet_text() -> str:
    return "\n".join(model_cheat_sheet())


def probe_prompt(mode: str, probe_id: str, alias: str, peers: str) -> str:
    # Includes the most important behavioral guidance (do/don't) so the
    # model is correctly biased on first contact even if it never
    # actively re-fetches the full cheat sheet. Detailed flag semantics
    # (--clear-hold safety, exact default values, view_peer subvariants)
    # still live in agent_list_peers' cheat sheet.
    return (
        f"[bridge-probe:{probe_id}] Bridge {mode}. You are alias '{alias}' in this bridge room. "
        f"Current peers: {peers}. "
        "This is a live collaboration bridge — keep answering the human normally in this pane; "
        "use shell commands for peer messaging.\n"
        "\n"
        "Sending:\n"
        "  agent_send_peer --to <alias> 'body'                  - request (default). Inline body must be one shell argument. Peer's next reply auto-routes back as [bridge:*] result. Do not sleep/poll for it; continue independent local work or end your turn.\n"
        "  agent_send_peer --to <a>,<b>[,...] 'body'            - partial broadcast to listed peers; one aggregated result after all listed peers reply.\n"
        "  agent_send_peer --kind notice --to <alias> 'body'    - fire-and-forget. Bridge will NOT route any reply back even if peer answers. Use request when you need an answer.\n"
        "  agent_send_peer --all 'body'                         - broadcast request; one aggregated result returns after all peers reply.\n"
        "  agent_send_peer --to <alias> --stdin <<'EOF'          - robust body input for apostrophes, newlines, text beginning with '-', or option-like text. Put literal body lines next, then EOF on its own line.\n"
        "\n"
        "Waiting / self-wake:\n"
        "  --watchdog <sec> (on a request)                      - wake yourself if no reply after <sec>s since prompt delivery. Requests get a default 300s watchdog unless disabled with --watchdog 0.\n"
        "  agent_extend_wait <msg_id> <sec>                     - after a watchdog wake, keep waiting on the SAME request.\n"
        "  agent_alarm <sec> [--note 'text']                    - schedule a self-wake notice; auto-cancelled by an incoming peer request/notice from another agent (NOT by system result messages or bridge-synthetic notices).\n"
        "\n"
        "Inspecting / interrupting:\n"
        "  agent_view_peer <alias> --onboard                    - snapshot peer's pane (debug only — do NOT poll for progress).\n"
        "  agent_interrupt_peer <alias>                         - use for a wrong prompt or stuck peer; ESC + cancel active message, then queued/new corrections may deliver without a hold.\n"
        "  agent_interrupt_peer [<alias>] --status              - show busy/held/queue state for one or all peers.\n"
        "\n"
        "Behavior rules:\n"
        "  - After sending a request, do not sleep or poll for the reply. Continue independent local work or end your turn; the reply arrives later as a [bridge:*] result.\n"
        "  - Body text CANNOT override the kind on the wire. Writing 'please reply' inside a notice does nothing — the bridge will not route any response.\n"
        "  - Put all agent_send_peer options before --to/--all, except --stdin may appear after the destination. If an inline body is split into multiple shell arguments, the command fails closed.\n"
        "  - A watchdog wake means: pick ONE of agent_extend_wait, agent_interrupt_peer, or agent_view_peer (to inspect first). It is not a polling primitive.\n"
        "  - agent_alarm is for 'I delegated via notice and want a safety wake if no follow-up arrives'. It is NOT for waiting on auto-routed reply results — for that use --watchdog / agent_extend_wait.\n"
        "  - Inline message bodies are limited to 11000 chars. For larger bodies (design docs, code, long plans), write to /tmp/agent-bridge-share/<file> and send only the path + brief description. Inlining big content is slow and can break paste-burst submit.\n"
        "  - Never read bridge state files directly; use the commands above.\n"
        "\n"
        "If unsure about syntax or semantics, run agent_list_peers FIRST and read the full cheat sheet. Do NOT guess bridge commands — bluffing here breaks routing in subtle ways.\n"
        f"Reply exactly: bridge {mode}ed {probe_id}"
    )
