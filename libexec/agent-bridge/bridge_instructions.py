#!/usr/bin/env python3
from __future__ import annotations


def model_cheat_sheet() -> list[str]:
    return [
        "Commands:",
        "- agent_list_peers : list aliases and show this cheat sheet.",
        "- agent_send_peer --to <alias> 'request' : ask one peer; result arrives later as a new [bridge:*] prompt. Do independent work only; do not sleep/poll or keep this turn open waiting. Inline body must be one shell argument.",
        "- agent_send_peer <alias> 'request' : shorthand for --to <alias>; options may follow the alias before the body.",
        "- agent_send_peer --to <a>,<b>[,...] 'request' : partial broadcast to the listed peers; same aggregate UX as --all, one merged result arrives later as a new [bridge:*] prompt once all listed peers reply.",
        "- agent_send_peer --all 'message' : broadcast one request to every other peer; one aggregated result arrives later as a new [bridge:*] prompt after all peers reply. Do not put an alias before the body.",
        "- agent_send_peer --kind notice --to <alias> 'FYI' : send info without expecting a reply. No reply auto-routes; do not wait for one. Set agent_alarm only if a follow-up matters.",
        "- agent_send_peer [--to <alias>|--to <a>,<b>|--all] --watchdog <sec> 'request' : add watchdog wakes using the same <sec> per phase: first if bridge delivery/submission stalls, then again after prompt delivery if the peer does not answer. A request may therefore wait up to two phase intervals before a response-phase wake (for example default 300s delivery + 300s response). Put options before the inline body, or use --stdin for complex bodies. Request only. --watchdog 0 disables the default. Default delay is set via env AGENT_BRIDGE_DEFAULT_WATCHDOG_SEC (300).",
        "- For apostrophes, newlines, text beginning with '-', or any complex body, use stdin: agent_send_peer --to <alias> --stdin <<'EOF' ... EOF, or shorthand agent_send_peer <alias> --stdin <<'EOF' ... EOF. Quote the heredoc delimiter as <<'EOF' when the body must be literal.",
        "- agent_alarm <sec> [--note 'text'] : schedule a self-addressed wake notice. The wake arrives later as a new [bridge:*] notice prompt unless automatically cancelled when ANY incoming peer message (kind != result, from != you, from != bridge) arrives at you. A cancelled alarm prepends a [bridge:alarm_cancelled] notice with re-arm guidance. Do not sleep/poll or keep this turn open waiting.",
        "- agent_extend_wait <message_id> <sec> : after a watchdog wake, keep waiting on the SAME request for <sec> more seconds. Only the original sender can extend. Inflight/submitted delivery waits can be extended; delivered aggregate response waits cannot be per-message extended.",
        "- agent_cancel_message <message_id> : cancel one message you sent while it is still pending or in bridge delivery. This retracts a specific queued message; use agent_interrupt_peer <alias> instead for submitted/delivered active peer work.",
        "- agent_interrupt_peer <alias> : use when you sent the wrong prompt or a peer is stuck. Sends model-specific interrupt keys (Codex: Escape; Claude: Escape then one Ctrl-C when active work exists) and cancels the active message; it is removed, not requeued. No default hold: queued or newly sent corrections may deliver after the full key sequence succeeds. If the peer is already past the inflight phase, interrupt can be a no-op: the response and queued follow-ups still flow. Identifiable late output from the cancelled turn is ignored.",
        "- agent_interrupt_peer <alias> --clear-hold : clear legacy held_interrupt residue or a partial interrupt-failure delivery gate without sending keys. Normally unnecessary post-v1.5 because queued/new corrections deliver without --clear-hold after a successful interrupt. UNSAFE if the peer might still be running a turn or has dirty input — late Stop events may be ignored and queued prompts may paste into residue. Inspect with --status first.",
        "- agent_interrupt_peer [<alias>] --status : print busy / held / current_prompt_id / delivered+pending counts for the target (or all peers if no alias).",
        "- agent_view_peer <alias> --onboard [--tail N] : capture a stable screen/history snapshot.",
        "- agent_view_peer <alias> --older : page older lines from that snapshot.",
        "- agent_view_peer <alias> --since-last : view latest changes since your last view.",
        "- agent_view_peer <alias> --search 'text' [--live] : search saved snapshot, or current live screen with --live; query is a case-insensitive literal substring (no regex).",
        "Kinds and routing contract:",
        "- request: the bridge auto-routes the peer's next response back to you as a [bridge:*] result. Sender stays woken up by that reply arrival. Default kind. Watchdog applies. Only request and notice can be sent by agents — 'result' kind is system-only (set by the bridge for auto-returns).",
        "- notice: the bridge does NOT route any reply back. Even if the peer writes a response, it stays in the peer's pane — observe via agent_view_peer. If you need an answer, use request, not notice. The kind on the wire beats any 'please reply' hint in the body.",
        "- result: internal kind for auto-returned replies. Cannot be sent directly by agents.",
        "Waking and timing:",
        "- After sending a request, the result arrives later as a new [bridge:*] prompt. Do independent work only; do not sleep/poll or keep this turn open waiting. With the default watchdog enabled you can get a [bridge:watchdog] notice if bridge delivery/submission stalls or, after delivery, if the peer takes too long to answer.",
        "- A watchdog wake explicitly tells you to choose ONE of: agent_extend_wait <msg_id> <sec>, agent_interrupt_peer <alias>, or just agent_view_peer <alias> first to inspect. Pick one — it's not a polling primitive.",
        "- agent_alarm is for 'I delegated work via notice and want a safety wake if no follow-up arrives'; do not sleep/poll or keep this turn open waiting for the wake. It is NOT for auto-routed request results — for that use --watchdog / agent_extend_wait.",
        "- Do not poll with agent_view_peer or schedule a wakeup to check progress; use agent_view_peer only when you suspect the peer is stuck or need to debug the bridge.",
        "- If a human types into a pane while a bridge prompt is delivered but unsubmitted, the bridge cancels that delivered message, emits [bridge:interrupted] prompt_intercepted to the original sender, and drops that turn; expect model-driven retries.",
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
        "  agent_send_peer --to <alias> 'body'                  - request (default). Inline body must be one shell argument. Result arrives later as a new [bridge:*] prompt; do independent work only; do not sleep/poll or keep this turn open waiting.\n"
        "  agent_send_peer <alias> 'body'                       - shorthand for --to <alias>; options may follow the alias before the body.\n"
        "  agent_send_peer --to <a>,<b>[,...] 'body'            - partial broadcast to listed peers; one aggregated result arrives later as a new [bridge:*] prompt after all listed peers reply.\n"
        "  agent_send_peer --kind notice --to <alias> 'body'    - fire-and-forget. No reply auto-routes; do not wait for one. Use request when you need an answer.\n"
        "  agent_send_peer --all 'body'                         - broadcast request; one aggregated result arrives later as a new [bridge:*] prompt after all peers reply.\n"
        "  agent_send_peer --to <alias> --stdin <<'EOF'          - robust body input for apostrophes, newlines, text beginning with '-', or option-like text. Shorthand: agent_send_peer <alias> --stdin <<'EOF'. Put literal body lines next, then EOF on its own line.\n"
        "\n"
        "Waiting / self-wake:\n"
        "  --watchdog <sec> (on a request)                      - wake yourself using the same <sec> per phase: first if bridge delivery/submission stalls, then after prompt delivery if no reply arrives. A request may wait up to two phase intervals before a response-phase wake (for example default 300s delivery + 300s response). Requests get a configured default watchdog unless disabled with --watchdog 0.\n"
        "  agent_extend_wait <msg_id> <sec>                     - after a watchdog wake, keep waiting on the SAME active delivery/response wait.\n"
        "  agent_alarm <sec> [--note 'text']                    - schedule a self-wake notice; wake arrives later as a new [bridge:*] notice prompt unless auto-cancelled by an incoming peer request/notice from another agent (NOT by system result messages or bridge-synthetic notices). A cancelled alarm prepends [bridge:alarm_cancelled] with re-arm guidance. Do not sleep/poll or keep this turn open waiting.\n"
        "\n"
        "Inspecting / interrupting:\n"
        "  agent_view_peer <alias> --onboard                    - snapshot peer's pane (debug only — do NOT poll for progress).\n"
        "  agent_view_peer <alias> --search 'text' [--live]     - search snapshot/live screen; case-insensitive literal substring (no regex).\n"
        "  agent_cancel_message <msg_id>                        - cancel one specific message you sent while it is pending/in delivery; use interrupt for submitted/delivered active work.\n"
        "  agent_interrupt_peer <alias>                         - use for a wrong prompt or stuck peer; sends model-specific interrupt keys (Codex Escape; Claude Escape then Ctrl-C when active) and cancels the active message. If already past inflight phase, interrupt can be a no-op: the response and queued follow-ups still flow.\n"
        "  agent_interrupt_peer [<alias>] --status              - show busy/held/queue state for one or all peers.\n"
        "\n"
        "Behavior rules:\n"
        "  - After sending a request, the result arrives later as a new [bridge:*] prompt. Do independent work only; do not sleep/poll or keep this turn open waiting.\n"
        "  - Body text CANNOT override the kind on the wire. Writing 'please reply' inside a notice does nothing — the bridge will not route any response.\n"
        "  - Put agent_send_peer options before the inline body text; options may appear after --to/--all or an implicit leading alias. Use --stdin for complex bodies. If an inline body is split into multiple shell arguments, the command fails closed.\n"
        "  - A watchdog wake means: pick ONE of agent_extend_wait, agent_interrupt_peer, or agent_view_peer (to inspect first). It may refer to delivery/submission or response wait; it is not a polling primitive.\n"
        "  - agent_alarm is for 'I delegated via notice and want a safety wake if no follow-up arrives'; do not sleep/poll or keep this turn open waiting for the wake. It is NOT for auto-routed request results — for that use --watchdog / agent_extend_wait.\n"
        "  - If a human types into a pane while a bridge prompt is delivered but unsubmitted, the bridge cancels that delivered message, emits [bridge:interrupted] prompt_intercepted to the original sender, and drops that turn; expect model-driven retries.\n"
        "  - Inline message bodies are limited to 11000 chars. For larger bodies (design docs, code, long plans), write to /tmp/agent-bridge-share/<file> and send only the path + brief description. Inlining big content is slow and can break paste-burst submit.\n"
        "  - Never read bridge state files directly; use the commands above.\n"
        "\n"
        "If unsure about syntax or semantics, run agent_list_peers FIRST and read the full cheat sheet. Do NOT guess bridge commands — bluffing here breaks routing in subtle ways.\n"
        f"Reply exactly: bridge {mode}ed {probe_id}"
    )
