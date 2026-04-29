#!/usr/bin/env python3
"""Clear a peer's model context through a controlled /clear probe."""

from __future__ import annotations

import argparse
import json
import math
import os
import socket
import sys

from bridge_daemon_client import ensure_daemon_running
from bridge_identity import resolve_caller_from_pane
from bridge_participants import active_participants, load_session, room_status
from bridge_paths import run_root


# Keep in sync with bridge_daemon.CLEAR_CLIENT_TIMEOUT_SECONDS.
CLEAR_SOCKET_TIMEOUT_SECONDS = 180.0
CLEAR_POST_CLEAR_DELAY_DEFAULT_SECONDS = 1.0
CLEAR_MULTI_TIMEOUT_MARGIN_SECONDS = 10.0
MAX_MULTI_RESULT_REASON_CHARS = 120
RESERVED_CLEAR_TARGETS = {"ALL", "all", "*"}
MULTI_FORCE_DISALLOWED = "multi_force_disallowed"
MULTI_FORCE_DISALLOWED_MESSAGE = "--force is only supported for single-target clear; specify exactly one alias when using --force"
MULTI_SELF_DISALLOWED = "multi_self_disallowed"
MULTI_SELF_DISALLOWED_MESSAGE = "self-clear must be the only target; specify only your own alias or omit yourself"


class ClearTargetError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def configured_clear_post_clear_delay_seconds() -> float:
    raw = os.environ.get("AGENT_BRIDGE_CLEAR_POST_CLEAR_DELAY_SEC")
    if raw is None or str(raw).strip() == "":
        return CLEAR_POST_CLEAR_DELAY_DEFAULT_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return CLEAR_POST_CLEAR_DELAY_DEFAULT_SECONDS
    if not math.isfinite(value):
        return CLEAR_POST_CLEAR_DELAY_DEFAULT_SECONDS
    if value < 0:
        return 0.0
    return value


def clear_socket_timeout_seconds(target_count: int) -> float:
    count = max(1, int(target_count or 1))
    if count <= 1:
        return CLEAR_SOCKET_TIMEOUT_SECONDS
    settle_extra = max(0.0, configured_clear_post_clear_delay_seconds() - CLEAR_POST_CLEAR_DELAY_DEFAULT_SECONDS)
    return (CLEAR_SOCKET_TIMEOUT_SECONDS + settle_extra) * count + CLEAR_MULTI_TIMEOUT_MARGIN_SECONDS


def send_command(bridge_session: str, payload: dict, *, timeout_seconds: float = CLEAR_SOCKET_TIMEOUT_SECONDS) -> tuple[bool, dict, str]:
    socket_path = run_root() / f"{bridge_session}.sock"
    if not socket_path.exists():
        return False, {}, f"daemon socket not found: {socket_path}"
    request = json.dumps(payload, ensure_ascii=True) + "\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout_seconds)
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
        return False, response if isinstance(response, dict) else {}, str(response.get("error") or "daemon rejected clear")
    return True, response, ""


def clear_summary(target: str, response: dict) -> dict:
    return {
        "target": response.get("target") or target,
        "cleared": bool(response.get("cleared")),
        "deferred": bool(response.get("deferred")),
        "force": bool(response.get("force")),
        "new_session_id": response.get("new_session_id"),
    }


def resolve_clear_targets(state: dict, sender: str, raw_target: str, *, target_all: bool = False) -> list[str]:
    participants = active_participants(state)
    sender = str(sender or "")
    raw = "ALL" if target_all else str(raw_target or "").strip()
    if not raw:
        raise ValueError("target alias required")

    tokens = [tok.strip() for tok in raw.split(",")]
    tokens = [tok for tok in tokens if tok]
    if not tokens:
        raise ValueError("target list is empty after stripping commas/whitespace")

    has_reserved = any(tok in RESERVED_CLEAR_TARGETS for tok in tokens)
    if has_reserved:
        if len(tokens) > 1:
            raise ValueError(
                "reserved target token (all/ALL/*) cannot be combined with explicit aliases; "
                "use either --all or --to <a>,<b>"
            )
        if sender and sender != "bridge":
            return [alias for alias in sorted(participants) if alias != sender]
        return [alias for alias in sorted(participants)]

    seen: set[str] = set()
    targets: list[str] = []
    for token in tokens:
        if token in seen:
            continue
        if token not in participants:
            aliases = ", ".join(sorted(participants)) or "(none)"
            raise ValueError(f"target {token!r} is not active in bridge room; active aliases: {aliases}.")
        seen.add(token)
        targets.append(token)

    if len(targets) > 1 and sender and sender != "bridge" and sender in targets:
        raise ClearTargetError(MULTI_SELF_DISALLOWED, MULTI_SELF_DISALLOWED_MESSAGE)
    return targets


def truncate_reason(text: str) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= MAX_MULTI_RESULT_REASON_CHARS:
        return text
    return text[: MAX_MULTI_RESULT_REASON_CHARS - 3].rstrip() + "..."


def multi_clear_summary(targets: list[str], response: dict) -> dict:
    results = response.get("results") if isinstance(response.get("results"), list) else []
    normalized: list[dict] = []
    by_target = {str(item.get("target") or ""): item for item in results if isinstance(item, dict)}
    for target in targets:
        item = dict(by_target.get(target) or {"target": target, "status": "failed", "error": "missing result"})
        item["target"] = target
        item["status"] = str(item.get("status") or "failed")
        normalized.append(item)
    summary = response.get("summary") if isinstance(response.get("summary"), dict) else {}
    if not summary:
        cleared = [item["target"] for item in normalized if item.get("status") == "cleared"]
        forced_leave = [item["target"] for item in normalized if item.get("status") == "forced_leave"]
        failed = [item["target"] for item in normalized if item.get("status") == "failed"]
        summary = {
            "counts": {"cleared": len(cleared), "forced_leave": len(forced_leave), "failed": len(failed)},
            "cleared": cleared,
            "forced_leave": forced_leave,
            "failed": failed,
            "all_cleared": len(cleared) == len(normalized),
        }
    return {"ok": True, "targets": targets, "results": normalized, "summary": summary}


def format_multi_clear_text(summary: dict) -> str:
    results = summary.get("results") if isinstance(summary.get("results"), list) else []
    by_status: dict[str, list[dict]] = {"cleared": [], "forced_leave": [], "failed": []}
    for item in results:
        if not isinstance(item, dict):
            continue
        by_status.setdefault(str(item.get("status") or "failed"), []).append(item)

    parts: list[str] = []
    cleared = [str(item.get("target") or "") for item in by_status.get("cleared", []) if item.get("target")]
    if cleared:
        parts.append(f"cleared {', '.join(cleared)}")
    for status, label in (("forced_leave", "forced-leave"), ("failed", "failed")):
        entries: list[str] = []
        for item in by_status.get(status, []):
            target = str(item.get("target") or "")
            reason = truncate_reason(str(item.get("error_kind") or item.get("error") or status))
            entries.append(f"{target} ({reason})" if reason else target)
        if entries:
            parts.append(f"{label} {', '.join(entries)}")
    return f"agent_clear_peer: {'; '.join(parts) if parts else 'no targets cleared'}."


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="agent_clear_peer",
        description="Run a controlled /clear for a bridge peer and wait for the bridge clear probe to finish.",
    )
    parser.add_argument("target", nargs="?", help="peer alias to clear")
    parser.add_argument("--to", dest="target_opt", help="peer alias to clear")
    parser.add_argument("--all", dest="target_all", action="store_true", help="clear all active peers except the caller; bridge/admin clears all active peers")
    parser.add_argument("--force", action="store_true", help="force-clear soft blockers such as pending rows, target-owned alarms, and target-originated auto-return routes")
    parser.add_argument("--session", dest="session")
    parser.add_argument("--from", dest="sender")
    parser.add_argument("--allow-spoof", action="store_true")
    parser.add_argument("--json", action="store_true", help="print machine-readable output")
    args = parser.parse_args()

    if args.target and args.target_opt:
        print("agent_clear_peer: use either positional target or --to, not both", file=sys.stderr)
        return 2
    if args.target_all and (args.target or args.target_opt):
        print("agent_clear_peer: use exactly one destination selector: <alias>, --to <alias>, or --all", file=sys.stderr)
        return 2
    target = args.target_opt or args.target or ""
    if not target and not args.target_all:
        print("agent_clear_peer: target alias required", file=sys.stderr)
        return 2

    session = args.session or os.environ.get("AGENT_BRIDGE_SESSION") or ""
    sender = args.sender or os.environ.get("AGENT_BRIDGE_AGENT") or ""
    resolution = resolve_caller_from_pane(
        pane=os.environ.get("TMUX_PANE"),
        explicit_session=session,
        explicit_alias=sender,
        allow_spoof=args.allow_spoof,
        tool_name="agent_clear_peer",
    )
    if not resolution.ok:
        print(resolution.error, file=sys.stderr)
        return 2
    session = session or resolution.session
    sender = sender or resolution.alias
    if not session or not sender:
        print("agent_clear_peer: cannot infer bridge session or sender alias; reattach a bridge room first.", file=sys.stderr)
        return 2

    ensure_error = ensure_daemon_running(session)
    if ensure_error:
        print(f"agent_clear_peer: {ensure_error}", file=sys.stderr)
        return 2

    status = room_status(session)
    if not status.active_enough_for_enqueue:
        print(f"agent_clear_peer: {status.reason}; reattach/start a bridge room first.", file=sys.stderr)
        return 2

    state = load_session(session)
    participants = active_participants(state)
    if sender != "bridge" and sender not in participants:
        aliases = ", ".join(sorted(participants)) or "(none)"
        print(f"agent_clear_peer: sender {sender!r} is not active in bridge room {session!r}; active aliases: {aliases}.", file=sys.stderr)
        return 2

    try:
        targets = resolve_clear_targets(state, sender, target, target_all=bool(args.target_all))
    except ClearTargetError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": exc.message, "error_kind": exc.code}, ensure_ascii=False, indent=2))
        else:
            print(f"agent_clear_peer: {exc.code}: {exc.message}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"agent_clear_peer: {exc}", file=sys.stderr)
        return 2
    if not targets:
        print("agent_clear_peer: target list is empty after excluding caller", file=sys.stderr)
        return 2
    if len(targets) > 1 and args.force:
        if args.json:
            print(json.dumps({"ok": False, "error": MULTI_FORCE_DISALLOWED_MESSAGE, "error_kind": MULTI_FORCE_DISALLOWED}, ensure_ascii=False, indent=2))
        else:
            print(f"agent_clear_peer: {MULTI_FORCE_DISALLOWED}: {MULTI_FORCE_DISALLOWED_MESSAGE}", file=sys.stderr)
        return 2

    payload = {"op": "clear_peer", "from": sender, "force": bool(args.force)}
    if len(targets) == 1:
        payload["target"] = targets[0]
    else:
        payload["targets"] = targets
    ok, response, error = send_command(session, payload, timeout_seconds=clear_socket_timeout_seconds(len(targets)))
    if not ok:
        if args.json:
            target_fields = {"target": targets[0]} if len(targets) == 1 else {"targets": targets}
            print(json.dumps({**target_fields, "ok": False, "error": error, **response}, ensure_ascii=False, indent=2))
        else:
            print(f"agent_clear_peer: {error}", file=sys.stderr)
        return 1

    if len(targets) == 1:
        summary = clear_summary(targets[0], response)
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        elif summary.get("deferred"):
            print(f"agent_clear_peer: self-clear for {targets[0]} is scheduled after the current turn ends (deferred).")
            print("Hint: further sends from this alias are blocked once the clear is reserved.", file=sys.stderr)
        else:
            print(f"agent_clear_peer: cleared {targets[0]}.")
        return 0

    summary = multi_clear_summary(targets, response)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(format_multi_clear_text(summary))
    return 0 if (summary.get("summary") or {}).get("all_cleared") else 1


if __name__ == "__main__":
    raise SystemExit(main())
