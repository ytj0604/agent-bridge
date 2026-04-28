#!/usr/bin/env python3
"""Interrupt a peer or manage legacy interrupt hold state.

Modes:
- (default) interrupt key sequence + cancel active/post-pane-touch peer work
  only; other pending queued messages are not removed. Use
  agent_cancel_message for pending, inflight pre-pane-touch/pre-paste, or
  pane-mode-deferred queued-message retraction. Claude targets may receive
  one Ctrl-C after ESC to clear submitted-prompt input residue:
    agent_interrupt_peer <alias>
- Clear legacy hold residue without sending ESC. Normally unnecessary for
  post-v1.5 corrections. Use only after --status shows held=true or
  interrupt_partial_failure_blocked=true and idle fields busy=false,
  current_prompt_id=null, inflight_count=0, delivered_count=0:
    agent_interrupt_peer <alias> --clear-hold
- Inspect status of a peer or all peers:
    agent_interrupt_peer [<alias>] --status
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys

from bridge_daemon_client import ensure_daemon_running
from bridge_identity import resolve_caller_from_pane
from bridge_participants import active_participants, load_session, room_status
from bridge_paths import run_root


def interrupt_summary(target: str, response: dict) -> dict:
    summary = {
        "target": target,
        "esc_sent": bool(response.get("esc_sent")),
        "interrupt_ok": bool(response.get("interrupt_ok", response.get("esc_sent"))),
        "interrupt_keys": response.get("interrupt_keys") or [],
        "cc_sent": response.get("cc_sent"),
        "held": bool(response.get("held")),
        "cancelled_message_ids": response.get("cancelled_message_ids") or [],
        "prior_active_message_id": response.get("prior_active_message_id"),
    }
    if response.get("esc_error"):
        summary["esc_error"] = response["esc_error"]
    if response.get("cc_error"):
        summary["cc_error"] = response["cc_error"]
    return summary


def interrupt_text(summary: dict) -> tuple[str, str, str]:
    target = str(summary.get("target") or "<peer>")
    cancelled = list(summary.get("cancelled_message_ids") or [])
    cancelled_count = len(cancelled)
    if not bool(summary.get("interrupt_ok")):
        error = str(summary.get("cc_error") or summary.get("esc_error") or "interrupt key sequence failed")
        code = "interrupt_partial_failure" if summary.get("esc_sent") else "interrupt_failed"
        body = (
            f"agent_interrupt_peer: interrupt key sequence for {target} failed "
            f"({code}); cancelled={cancelled_count}. Error: {error}"
        )
        hint = (
            f"Queued delivery to {target} may be blocked to avoid dirty input; inspect with "
            f"agent_interrupt_peer {target} --status and use --clear-hold only when idle."
        )
        return "", body, hint
    body = f"agent_interrupt_peer: interrupt sent to {target} (interrupted); cancelled={cancelled_count}."
    if summary.get("prior_active_message_id"):
        body += f" prior_active_message_id={summary.get('prior_active_message_id')}."
    if cancelled_count:
        hint = "Pending queued messages were not removed; use agent_cancel_message <msg_id> for queued-message retraction."
    else:
        hint = "No active message id was cancelled; if this was a no-op, queued follow-ups still flow. Pending queued messages were not removed."
    return body, "", hint


def clear_hold_summary(target: str, response: dict) -> dict:
    return {
        "target": target,
        "had_hold": bool(response.get("had_hold")),
        "info": response.get("info") or {},
        "warning": response.get("warning"),
    }


def clear_hold_text(summary: dict) -> tuple[str, str]:
    target = str(summary.get("target") or "<peer>")
    if summary.get("had_hold"):
        body = f"agent_interrupt_peer: cleared hold state for {target} (hold_cleared)."
    else:
        body = f"agent_interrupt_peer: no hold state was present for {target} (no_hold)."
    warning = str(summary.get("warning") or "").strip()
    hint = warning or "Use --status first; --clear-hold is only safe when held/partial-failure and idle fields are confirmed."
    return body, hint


def send_command(bridge_session: str, payload: dict) -> tuple[bool, dict, str]:
    socket_path = run_root() / f"{bridge_session}.sock"
    if not socket_path.exists():
        return False, {}, f"daemon socket not found: {socket_path}"
    request = json.dumps(payload, ensure_ascii=True) + "\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5.0)
            client.connect(str(socket_path))
            client.sendall(request.encode("utf-8"))
            raw = b""
            while b"\n" not in raw and len(raw) < 2_000_000:
                chunk = client.recv(65536)
                if not chunk:
                    break
                raw += chunk
    except OSError as exc:
        return False, {}, f"daemon socket error: {exc}"
    try:
        response = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        return False, {}, f"invalid daemon response: {exc}"
    if not response.get("ok"):
        return False, {}, str(response.get("error") or "daemon rejected request")
    return True, response, ""


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="agent_interrupt_peer",
        description=(
            "Send the agent-type-specific interrupt key sequence (Codex: Escape; Claude: Escape then "
            "one Ctrl-C when active work exists) and cancel only the active/post-pane-touch peer message "
            "(inflight post-pane-touch, submitted, delivered, or responding active turns), force-clear "
            "a legacy interrupt hold, or inspect peer status. Interrupt removes the active turn only, "
            "not other pending queued messages; use agent_cancel_message for pending, inflight pre-pane-touch/pre-paste, "
            "or pane-mode-deferred queued-message retraction. If the peer is already past the inflight "
            "phase, interrupt can be a no-op: the response and queued follow-ups still flow."
        ),
    )
    parser.add_argument("target", nargs="?", help="peer alias to act on (omit with --status to query all peers)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--clear-hold", action="store_true", help="clear held_interrupt residue or partial interrupt-failure gate only after --status shows held=true or interrupt_partial_failure_blocked=true and idle fields busy=false, current_prompt_id=null, inflight_count=0, delivered_count=0; unsafe with turn in progress, dirty input, or active delivered/inflight work")
    mode.add_argument("--status", action="store_true", help="show busy/held/queue state for the target (or all peers if no target)")
    parser.add_argument("--session", dest="session")
    parser.add_argument("--from", dest="sender")
    parser.add_argument("--allow-spoof", action="store_true")
    parser.add_argument("--json", action="store_true", help="print compatibility JSON for action modes; --status is already JSON")
    args = parser.parse_args()

    if args.status and args.json:
        print("agent_interrupt_peer: --json is redundant with --status; --status always returns JSON.", file=sys.stderr)
        return 2

    session = args.session or os.environ.get("AGENT_BRIDGE_SESSION") or ""
    sender = args.sender or os.environ.get("AGENT_BRIDGE_AGENT") or ""

    resolution = resolve_caller_from_pane(
        pane=os.environ.get("TMUX_PANE"),
        explicit_session=session,
        explicit_alias=sender,
        allow_spoof=args.allow_spoof,
        tool_name="agent_interrupt_peer",
    )
    if not resolution.ok:
        print(resolution.error, file=sys.stderr)
        return 2
    session = session or resolution.session
    sender = sender or resolution.alias

    if not session or not sender:
        print("agent_interrupt_peer: cannot infer bridge session or sender alias; reattach a bridge room first.", file=sys.stderr)
        return 2

    ensure_error = ensure_daemon_running(session)
    if ensure_error:
        print(f"agent_interrupt_peer: {ensure_error}", file=sys.stderr)
        return 2

    status = room_status(session)
    if not status.active_enough_for_enqueue:
        print(f"agent_interrupt_peer: {status.reason}; reattach/start a bridge room first.", file=sys.stderr)
        return 2

    state = load_session(session)
    participants = active_participants(state)
    if sender not in participants:
        aliases = ", ".join(sorted(participants)) or "(none)"
        print(
            f"agent_interrupt_peer: sender {sender!r} is not active in bridge room {session!r}; active aliases: {aliases}.",
            file=sys.stderr,
        )
        return 2

    # --status branch (target optional)
    if args.status:
        target = args.target or ""
        if target and target not in participants:
            aliases = ", ".join(sorted(participants)) or "(none)"
            print(
                f"agent_interrupt_peer: target {target!r} is not active in bridge room {session!r}; active aliases: {aliases}.",
                file=sys.stderr,
            )
            return 2
        ok, response, error = send_command(session, {"op": "status", "from": sender, "target": target})
        if not ok:
            print(f"agent_interrupt_peer: {error}", file=sys.stderr)
            return 1
        print(json.dumps({"peers": response.get("peers") or []}, ensure_ascii=False, indent=2))
        return 0

    # --clear-hold and default branches require a target
    if not args.target:
        print("agent_interrupt_peer: target alias required (or use --status)", file=sys.stderr)
        return 2
    if args.target not in participants:
        aliases = ", ".join(sorted(participants)) or "(none)"
        print(
            f"agent_interrupt_peer: target {args.target!r} is not active in bridge room {session!r}; active aliases: {aliases}.",
            file=sys.stderr,
        )
        return 2
    if args.target == sender and not args.clear_hold:
        print("agent_interrupt_peer: cannot interrupt yourself", file=sys.stderr)
        return 2

    if args.clear_hold:
        ok, response, error = send_command(session, {"op": "clear_hold", "from": sender, "target": args.target})
        if not ok:
            print(f"agent_interrupt_peer: {error}", file=sys.stderr)
            return 1
        summary = clear_hold_summary(args.target, response)
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            body, hint = clear_hold_text(summary)
            print(body)
            if hint:
                print(f"Hint: {hint}", file=sys.stderr)
        return 0

    # default: ESC + interrupt
    ok, response, error = send_command(session, {"op": "interrupt", "from": sender, "target": args.target})
    if not ok:
        print(f"agent_interrupt_peer: {error}", file=sys.stderr)
        return 1
    summary = interrupt_summary(args.target, response)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        stdout_body, stderr_body, hint = interrupt_text(summary)
        if stdout_body:
            print(stdout_body)
        if stderr_body:
            print(stderr_body, file=sys.stderr)
        if hint:
            print(f"Hint: {hint}", file=sys.stderr)
    return 0 if summary["interrupt_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
