#!/usr/bin/env python3
from __future__ import annotations


def model_cheat_sheet() -> list[str]:
    return [
        "Commands:",
        "- agent_list_peers : list aliases and show this cheat sheet.",
        "- agent_send_peer --to <alias> 'request' : ask one peer; result arrives later as a new [bridge:*] prompt. Do independent work only; do not sleep/poll or keep this turn open waiting. Inline body must be one shell argument.",
        "- agent_send_peer <alias> 'request' : shorthand for --to <alias>; options may follow the alias before the body.",
        "- agent_send_peer --to <a>,<b>[,...] 'request' : partial broadcast to the listed peers; same aggregate UX as --all, one merged result arrives later as a new [bridge:*] prompt once all listed peers reply. Aggregate sends print AGGREGATE_ID: agg-... on stdout for agent_aggregate_status.",
        "- agent_send_peer --all 'message' : broadcast one request to every other peer; one aggregated result arrives later as a new [bridge:*] prompt after all peers reply. Do not put an alias before the body. Prints AGGREGATE_ID: agg-... on stdout.",
        "- agent_send_peer --kind notice --to <alias> 'FYI' : send info without expecting a reply. No reply auto-routes; do not wait for one. Set agent_alarm if a follow-up matters.",
        "- agent_send_peer [--to <alias>|--to <a>,<b>|--all] --watchdog <sec> 'request' : add watchdog wakes using the same <sec> per phase: delivery/submission first, then response after prompt delivery if the peer does not answer. A request may wait up to two phase intervals before a response-phase wake; AGENT_BRIDGE_DEFAULT_WATCHDOG_SEC=300 means up to 300s delivery + 300s response. Put options before the inline body, or use --stdin for complex bodies. Request only. --watchdog 0 disables the default.",
        "- For apostrophes, newlines, text beginning with '-', or any complex body, use stdin: agent_send_peer --to <alias> --stdin <<'EOF' ... EOF, or shorthand agent_send_peer <alias> --stdin <<'EOF' ... EOF. Body input is either one inline argument or --stdin heredoc, not both; missing_body, empty_stdin, unexpected_positional_after_stdin, stdin_inline_conflict, and piped_stdin_inline_conflict print self-recovery guidance.",
        "- agent_alarm <sec> [--note 'text'] : schedule a self-addressed wake notice. The wake arrives later as a new [bridge:*] notice prompt unless automatically cancelled when ANY incoming peer message (kind != result, from != you, from != bridge) arrives at you. A cancelled alarm prepends a [bridge:alarm_cancelled] notice with re-arm guidance. Do not sleep/poll or keep this turn open waiting.",
        "- agent_extend_wait <message_id> <sec> : after a watchdog wake, keep waiting on the SAME request for <sec> more seconds. Only the original sender can extend. Inflight/submitted delivery waits can be extended; delivered aggregate response waits cannot be per-message extended. If the result wins the race, stale watchdog wakes are suppressed where possible; extend_wait may say the [bridge:result] is already queued/arriving.",
        "- agent_cancel_message <message_id> : retract one specific message you sent before it becomes active: pending, inflight pre-pane-touch/pre-paste, or pane-mode-deferred delivery. It does not interrupt active peer work; use agent_interrupt_peer <alias> for inflight post-pane-touch, submitted, delivered, or responding active turns. Use --json for machine-readable output.",
        "- agent_wait_status [--summary] : show your own outstanding waits, alarms, watchdogs, and pending inbound bridge queue as JSON. Use only for a human-prompted check, after a watchdog wake, or suspected bridge-state debugging; do not poll for progress or replace waiting for [bridge:*] prompts. Use agent_view_peer for peer pane debugging.",
        "- agent_aggregate_status <aggregate_id> [--summary] : show leg-level progress for one broadcast/partial-broadcast aggregate you started. Aggregate sends print AGGREGATE_ID: agg-... on stdout; use that aggregate_id here after a human prompt, watchdog wake, or suspected bridge issue. agent_wait_status summarizes all waits; do not poll. Watchdog wake ids shown here are ephemeral/correlation-only.",
        "- agent_clear_peer <alias> [--force] : clear a peer's model context with a controlled /clear probe. Self-clear is deferred until your current turn ends; after reservation, later sends from that alias are blocked. --force clears soft blockers such as pending/pre-active inbound rows, target-owned alarms, and target-originated request return routes; active/post-pane-touch work remains a hard blocker.",
        "- agent_interrupt_peer <alias> : interrupt active/post-pane-touch peer work: inflight post-pane-touch, submitted, delivered, or responding active turns. Use for a stuck peer, wrong prompt, or replacement waiting behind active/post-pane-touch work. Sends model-specific interrupt keys (Codex: Escape; Claude: Escape then one Ctrl-C when active work exists) and removes only the active message/turn, not other pending queued messages or later corrections for that peer. Use agent_cancel_message <msg_id> for pending, inflight pre-pane-touch/pre-paste, or pane-mode-deferred queued-message retraction. If the peer is already past the inflight phase, interrupt can be a no-op: the response and queued follow-ups still flow. Identifiable late output from the cancelled turn is ignored. Use --json for machine-readable output.",
        "- agent_interrupt_peer <alias> --clear-hold : clear held_interrupt residue or an interrupt partial-failure delivery gate without sending keys only after agent_interrupt_peer --status shows held=true or interrupt_partial_failure_blocked=true AND the peer is idle: busy=false, current_prompt_id=null, inflight_count=0, delivered_count=0. Normally unnecessary post-v1.5. UNSAFE with turn in progress, dirty input possible, active delivered/inflight work exists, a partial-failure gate lacks idle confirmation, or status does not match that safe combination. Use --json for machine-readable output.",
        "- agent_interrupt_peer [<alias>] --status : print JSON busy / held / current_prompt_id / delivered+pending counts for the target (or all peers if no alias).",
        "- agent_view_peer <alias> --onboard [--tail N] : capture a stable screen/history snapshot.",
        "- agent_view_peer <alias> --older : page older lines from that snapshot.",
        "- agent_view_peer <alias> --since-last : view latest changes since your last view.",
        "- agent_view_peer <alias> --search 'text' [--live] : search saved snapshot, or current live screen with --live; query is a case-insensitive literal substring (no regex).",
        "Kinds and routing contract:",
        "- request: the bridge auto-routes the peer's next response back to you as a [bridge:*] result. Sender stays woken up by that reply arrival. Default kind. Watchdog applies. Only request and notice can be sent by agents — 'result' kind is system-only (set by the bridge for auto-returns).",
        "- notice: the bridge does NOT route any reply back. Even if the peer writes a response, it stays in the peer's pane — observe via agent_view_peer. If you need an answer, use request, not notice. The kind on the wire beats any 'please reply' hint in the body.",
        "- Response-time send guard: while responding to an auto-return peer request, separate agent_send_peer messages to the requester (current_prompt.from) are blocked/rejected; any target list containing that requester is blocked. Third-party peer sends for review/collaboration are not blocked by this guard, but other validations still apply.",
        "- result: internal kind for auto-returned replies. Cannot be sent directly by agents.",
        "Waking and timing:",
        "- After sending a request, the result arrives later as a new [bridge:*] prompt. Do independent work only; do not sleep/poll or keep this turn open waiting. With the default watchdog enabled you can get a [bridge:watchdog] notice if bridge delivery/submission stalls or, after delivery, if the peer takes too long to answer.",
        "- Watchdog timing: --watchdog is a per-phase watchdog timer, not a queue timer. Delivery countdown starts at pending -> inflight reservation; response countdown starts at inflight -> delivered after prompt submission/prompt delivery.",
        "- A watchdog wake explicitly tells you to choose ONE of: agent_extend_wait <msg_id> <sec>, agent_interrupt_peer <alias>, or just agent_view_peer <alias> first to inspect. Pick one — it's not a polling primitive. If the result arrives first, the bridge suppresses stale pending wakes where possible; otherwise extend_wait will report that the result may already be queued/arriving.",
        "- agent_alarm is for 'I delegated work via notice and want a safety wake if no follow-up arrives'; do not sleep/poll or keep this turn open waiting for the wake. It is NOT for auto-routed request results — for that use --watchdog / agent_extend_wait.",
        "- Do not poll with agent_view_peer or schedule a wakeup to check progress; use agent_view_peer only when you suspect the peer is stuck or need to debug the bridge.",
        "- Do not poll with agent_wait_status. It is a self status/debug command for human-prompted checks or watchdog recovery, not a replacement for waiting for [bridge:*] prompts.",
        "- Do not poll with agent_aggregate_status. It is a one-aggregate leg-level debug command; wait for [bridge:*] prompts for normal progress.",
        "- If a human types into a pane while a bridge prompt is delivered but unsubmitted, the bridge cancels that delivered message, emits [bridge:interrupted] prompt_intercepted to the original sender, and drops that turn; expect model-driven retries.",
        "- Never read bridge state files directly. Replies arrive as [bridge:*] prompts; use the bridge commands above for everything else.",
        "- Large payloads (design docs, code, long plans): inline agent_send_peer bodies are limited to 11000 chars. Write larger content to a shared path under /tmp/agent-bridge-share/ and send only the path + brief description. Peers read the file directly. Inlining big bodies is slow and can break paste-burst submit.",
        "- Oversized returned results may arrive as [bridge:body_redirected] with a File: path and a short Preview:. Read the file; the preview is intentionally truncated.",
    ]


def model_cheat_sheet_text() -> str:
    return "\n".join(model_cheat_sheet())


def probe_prompt(mode: str, probe_id: str, alias: str, peers: str) -> str:
    # Attach-time quickstart only: keep this passive prompt compact and
    # push detailed recovery/reference material to agent_list_peers.
    return (
        f"[bridge-probe:{probe_id}] Bridge {mode}. You are alias '{alias}' in this bridge room. "
        f"Current peers: {peers}. "
        "This is a live collaboration bridge — keep answering the human normally in this pane; "
        "use shell commands for peer messaging.\n"
        "\n"
        "Sending:\n"
        "  agent_send_peer --to <alias> 'body' | agent_send_peer <alias> 'body' - request; result arrives later as a new [bridge:*] prompt. Do independent work; do not sleep/poll.\n"
        "  agent_send_peer --to <a>,<b>[,...] 'body' | agent_send_peer --all 'body' - aggregate request; stdout prints AGGREGATE_ID: agg-... for agent_aggregate_status.\n"
        "  agent_send_peer --kind notice --to <alias> 'body' - no reply auto-routes; use request when you need an answer. Set agent_alarm if a follow-up matters.\n"
        "  Body input: one inline argument OR --stdin heredoc, not both. Stable codes print self-recovery guidance: missing_body, empty_stdin, unexpected_positional_after_stdin, stdin_inline_conflict, piped_stdin_inline_conflict.\n"
        "\n"
        "Waiting / status / action:\n"
        "  --watchdog <sec> on a request: per-phase watchdog, not a queue timer; delivery pending -> inflight, response inflight -> delivered. AGENT_BRIDGE_DEFAULT_WATCHDOG_SEC=300 means up to 300s delivery + 300s response; --watchdog 0 disables the default.\n"
        "  agent_extend_wait <msg_id> <sec> after a watchdog wake; if result arrived first it may report [bridge:result] queued/arriving. agent_alarm <sec> self-wakes later as [bridge:*] notice; auto-cancelled by any incoming peer request/notice from another agent.\n"
        "  agent_view_peer / agent_wait_status / agent_aggregate_status (leg-level) are debug surfaces, not polling primitives; use after a human prompt, watchdog wake, suspected stuck peer (view_peer), or bridge-state debug. Do not poll.\n"
        "  agent_cancel_message <msg_id>: retract pending, inflight pre-pane-touch/pre-paste, or pane-mode-deferred messages. agent_interrupt_peer <alias>: active/post-pane-touch work - wrong prompt, stuck peer, or replacement waiting behind active/post-pane-touch work. agent_clear_peer <alias> [--force]: controlled /clear; self-clear is deferred. Use --json for action machine output; --status is JSON.\n"
        "\n"
        "Critical rules:\n"
        "  - Body text cannot override kind: notice never auto-routes. Response-time send guard: while responding to an auto-return peer request, separate sends to requester current_prompt.from are blocked/rejected; third-party review/collaboration sends are not blocked by this guard, but other validations still apply.\n"
        "  - Inline bodies are limited to 11000 chars; for larger payloads, write under /tmp/agent-bridge-share/ and send the path. Never read bridge state files directly; use bridge commands.\n"
        "  - Returned results over the daemon body limit arrive as [bridge:body_redirected] with a File: path. Read that file; the preview is intentionally truncated.\n"
        "  - Run agent_list_peers for the full cheat sheet. Do NOT guess bridge command syntax or semantics.\n"
        f"Reply exactly: bridge {mode}ed {probe_id}"
    )
