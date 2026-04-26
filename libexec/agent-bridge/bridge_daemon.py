#!/usr/bin/env python3
import argparse
from collections import OrderedDict
import hashlib
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from bridge_identity import backfill_session_process_identities, resolve_participant_endpoint_detail
from bridge_participants import active_participants, participant_record
from bridge_paths import model_bin_dir, state_root
from bridge_response_guard import (
    context_from_current_prompt,
    format_response_send_violation,
    response_send_violation,
)
from bridge_util import (
    MAX_PEER_BODY_CHARS,
    append_jsonl,
    locked_json,
    normalize_kind,
    public_record,
    read_json,
    run_tmux_capture,
    short_id,
    utc_now,
)


PHYSICAL_AGENT_TYPES = {"claude", "codex"}
MAX_PROCESSED_RETURNS = 4096
MAX_NONCE_CACHE = 1024
MAX_PROCESSED_CAPTURE_REQUESTS = 4096
MAX_CAPTURE_REQUEST_AGE_SECONDS = 60
CAPTURE_RESPONSE_TTL_SECONDS = 60 * 60
PANE_MODE_GRACE_DEFAULT_SECONDS = 180.0
TURN_ID_MISMATCH_GRACE_DEFAULT_SECONDS = 300.0
TURN_ID_MISMATCH_POST_WATCHDOG_GRACE_DEFAULT_SECONDS = 1.0
PANE_MODE_PROBE_TIMEOUT_SECONDS = 0.3
PANE_MODE_FORCE_CANCEL_MODES = {"copy-mode", "copy-mode-vi", "view-mode"}
PANE_MODE_METADATA_KEYS = (
    "pane_mode_blocked_since",
    "pane_mode_blocked_since_ts",
    "pane_mode_blocked_mode",
    "pane_mode_block_count",
    "pane_mode_unforceable_logged",
    "pane_mode_cancel_failed_logged",
    "pane_mode_probe_failed_logged",
    "last_pane_mode_cancel_error",
    "last_pane_mode_probe_error",
)
PANE_MODE_ENTER_DEFER_KEYS = (
    "pane_mode_enter_deferred_since",
    "pane_mode_enter_deferred_since_ts",
    "pane_mode_enter_deferred_mode",
)
INTERRUPTED_TOMBSTONE_LIMIT_PER_AGENT = 16
INTERRUPTED_TOMBSTONE_TTL_SECONDS = 600.0
EMPTY_RESPONSE_BODY = "(empty response)"
_STOP_SIGNAL: int | None = None
PROMPT_BODY_CONTROL_TRANSLATION = {
    codepoint: None
    for codepoint in (
        *range(0x00, 0x09),
        *range(0x0B, 0x20),
        0x7F,
        *range(0x80, 0xA0),
    )
}


def _request_stop(signum: int, _frame: object) -> None:
    global _STOP_SIGNAL
    _STOP_SIGNAL = signum


def install_signal_handlers() -> None:
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)


class BoundedSet:
    def __init__(self, max_size: int) -> None:
        self.max_size = max(1, int(max_size))
        self.items: OrderedDict[str, None] = OrderedDict()

    def add(self, item: str) -> bool:
        if item in self.items:
            self.items.move_to_end(item)
            return False
        self.items[item] = None
        while len(self.items) > self.max_size:
            self.items.popitem(last=False)
        return True


def one_line(text: str) -> str:
    return " ".join(str(text).split())


def normalize_prompt_body_text(text: str) -> str:
    raw = str(text).replace("\r\n", "\n").replace("\r", "\n")
    return raw.translate(PROMPT_BODY_CONTROL_TRANSLATION)


def prompt_body(text: str) -> str:
    raw = normalize_prompt_body_text(text)
    if len(raw) > MAX_PEER_BODY_CHARS:
        raw = raw[:MAX_PEER_BODY_CHARS] + "\n[bridge truncated peer body]"
    return raw


def kind_expects_response(kind: str) -> bool:
    return kind == "request"


def _tmux_buffer_component(value: object, fallback: str) -> str:
    raw = str(value or fallback)
    safe = "".join(
        ch if ("a" <= ch <= "z" or "A" <= ch <= "Z" or "0" <= ch <= "9" or ch in "._-") else "-"
        for ch in raw
    ).strip("._-")
    if not safe:
        safe = fallback
    if len(safe) > 48:
        digest = hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:10]
        safe = f"{safe[:37]}-{digest}"
    return safe


def tmux_prompt_buffer_name(
    bridge_session: str,
    target_alias: str,
    message_id: str,
    nonce: str,
) -> str:
    components = [
        _tmux_buffer_component(bridge_session, "session"),
        _tmux_buffer_component(target_alias, "target"),
        _tmux_buffer_component(message_id, "message"),
        _tmux_buffer_component(nonce, "nonce"),
        str(os.getpid()),
        uuid.uuid4().hex[:12],
    ]
    return "bridge-" + "-".join(components)


def run_tmux_send_literal(
    target: str,
    prompt: str,
    *,
    bridge_session: str = "",
    target_alias: str = "",
    message_id: str = "",
    nonce: str = "",
) -> None:
    buffer_name = tmux_prompt_buffer_name(bridge_session, target_alias or target, message_id, nonce)
    try:
        subprocess.run(
            ["tmux", "load-buffer", "-b", buffer_name, "-"],
            input=prompt.encode("utf-8"),
            check=True,
        )
        subprocess.run(
            ["tmux", "paste-buffer", "-p", "-r", "-d", "-b", buffer_name, "-t", target],
            check=True,
        )
    finally:
        try:
            subprocess.run(
                ["tmux", "delete-buffer", "-b", buffer_name],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except Exception:
            pass


def run_tmux_enter(target: str) -> None:
    subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], check=True)


def probe_tmux_pane_mode(target: str) -> dict:
    try:
        proc = subprocess.run(
            ["tmux", "display-message", "-p", "-t", target, "#{pane_in_mode}\t#{pane_mode}"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=PANE_MODE_PROBE_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        return {"in_mode": False, "mode": "", "error": str(exc)}
    flag, _, mode = proc.stdout.rstrip("\n").partition("\t")
    return {"in_mode": flag.strip() == "1", "mode": mode.strip(), "error": ""}


def cancel_tmux_pane_mode(target: str) -> tuple[bool, str]:
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", target, "-X", "cancel"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=PANE_MODE_PROBE_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        return False, str(exc)
    return True, ""


def resolve_pane_mode_grace_seconds() -> tuple[float | None, str | None]:
    raw = os.environ.get("AGENT_BRIDGE_PANE_MODE_GRACE_SEC")
    if raw is None or str(raw).strip() == "":
        return PANE_MODE_GRACE_DEFAULT_SECONDS, None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return PANE_MODE_GRACE_DEFAULT_SECONDS, f"invalid AGENT_BRIDGE_PANE_MODE_GRACE_SEC={raw!r}; using 180"
    if value <= 0:
        if value < 0:
            return None, f"AGENT_BRIDGE_PANE_MODE_GRACE_SEC={raw!r} is negative; force-cancel disabled"
        return None, None
    return value, None


def resolve_non_negative_env_seconds(env_name: str, default: float) -> tuple[float, str | None]:
    raw = os.environ.get(env_name)
    if raw is None or str(raw).strip() == "":
        return default, None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default, f"invalid {env_name}={raw!r}; using {default:g}"
    if value < 0:
        return default, f"{env_name}={raw!r} is negative; using {default:g}"
    return value, None


def pane_mode_block_since_ts(item: dict) -> float | None:
    raw_ts = item.get("pane_mode_blocked_since_ts")
    try:
        return float(raw_ts)
    except (TypeError, ValueError):
        pass
    raw_iso = item.get("pane_mode_blocked_since")
    if not raw_iso:
        return None
    try:
        return datetime.fromisoformat(str(raw_iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def build_peer_prompt(message: dict, nonce: str, max_hops: int) -> str:
    sender = str(message["from"])
    kind = normalize_kind(message.get("kind"), "request")
    details = [f"from={sender}", f"kind={kind}"]
    if kind == "result" and message.get("reply_to"):
        details.append(f"in_reply_to={message.get('reply_to')}")
    if message.get("causal_id"):
        details.append(f"causal_id={message.get('causal_id')}")
    if message.get("aggregate_id"):
        details.append(f"aggregate_id={message.get('aggregate_id')}")
    if kind == "request":
        label = "Request"
        hint = "Reply normally; do not call agent_send_peer; bridge auto-returns your reply."
    elif kind == "result":
        label = "Result"
        hint = "Use locally; do not reply to peer."
    else:
        label = "Notice"
        hint = "FYI; no reply needed."

    prefix = one_line(f"[bridge:{nonce}] {' '.join(details)}. {hint}")
    return f"{prefix} {label}: {prompt_body(message['body'])}"


def make_message(
    sender: str,
    target: str,
    intent: str,
    body: str,
    causal_id: str | None = None,
    hop_count: int = 0,
    auto_return: bool | None = None,
    kind: str = "request",
    reply_to: str | None = None,
    source: str = "daemon",
) -> dict:
    kind = normalize_kind(kind)
    if auto_return is None:
        auto_return = sender != "bridge" and target != "bridge" and kind_expects_response(kind)
    return {
        "id": short_id("msg"),
        "created_ts": utc_now(),
        "updated_ts": utc_now(),
        "from": sender,
        "to": target,
        "kind": kind,
        "intent": intent,
        "body": str(body),
        "causal_id": causal_id or short_id("causal"),
        "hop_count": int(hop_count),
        "auto_return": bool(auto_return),
        "reply_to": reply_to,
        "source": source,
        "status": "pending",
        "nonce": None,
        "delivery_attempts": 0,
    }


class QueueStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def update(self, mutator: Callable[[list[dict]], Any]) -> Any:
        with locked_json(self.path, []) as queue:
            result = mutator(queue)
            return result

    def read(self) -> list[dict]:
        return self.update(lambda queue: list(queue))


class BridgeDaemon:
    def __init__(self, args: argparse.Namespace) -> None:
        self.state_file = Path(args.state_file)
        self.public_state_file = Path(args.public_state_file) if args.public_state_file else None
        self.queue = QueueStore(args.queue_file)
        self.aggregate_file = Path(args.queue_file).parent / "aggregates.json"
        self.args = args
        self.session_file = Path(args.session_file) if args.session_file else Path(args.queue_file).parent / "session.json"
        self.session_state: dict = {}
        self.participants: dict[str, dict] = {}
        self.panes: dict[str, str] = {}
        self.session_mtime_ns: int | None = None
        self.max_hops = args.max_hops
        self.submit_delay = args.submit_delay
        self.submit_timeout = args.submit_timeout
        self.pane_mode_grace_seconds, self.pane_mode_grace_warning = resolve_pane_mode_grace_seconds()
        self.turn_id_mismatch_grace_seconds, turn_id_mismatch_grace_warning = resolve_non_negative_env_seconds(
            "AGENT_BRIDGE_TURN_ID_MISMATCH_GRACE_SEC",
            TURN_ID_MISMATCH_GRACE_DEFAULT_SECONDS,
        )
        (
            self.turn_id_mismatch_post_watchdog_grace_seconds,
            turn_id_mismatch_post_watchdog_grace_warning,
        ) = resolve_non_negative_env_seconds(
            "AGENT_BRIDGE_TURN_ID_MISMATCH_POST_WATCHDOG_GRACE_SEC",
            TURN_ID_MISMATCH_POST_WATCHDOG_GRACE_DEFAULT_SECONDS,
        )
        self.turn_id_mismatch_grace_warnings = [
            warning
            for warning in (turn_id_mismatch_grace_warning, turn_id_mismatch_post_watchdog_grace_warning)
            if warning
        ]
        self.from_start = args.from_start
        self.dry_run = args.dry_run
        self.stdout_events = args.stdout_events
        self.bridge_session = args.bridge_session
        self.stop_file = Path(args.stop_file) if args.stop_file else None
        self.command_socket = Path(args.command_socket) if args.command_socket else None
        self.command_server_thread: threading.Thread | None = None
        self.command_server_socket: socket.socket | None = None
        self.once = args.once
        self.busy: dict[str, bool] = {}
        self.reserved: dict[str, str | None] = {}
        self.current_prompt_by_agent: dict[str, dict] = {}
        self.injected_by_nonce: OrderedDict[str, dict] = OrderedDict()
        self.processed_returns = BoundedSet(MAX_PROCESSED_RETURNS)
        self.processed_capture_requests = BoundedSet(MAX_PROCESSED_CAPTURE_REQUESTS)
        self.last_maintenance = 0.0
        self.last_capture_cleanup = 0.0
        self.last_ingressing_check = 0.0
        self.stop_logged = False
        self.last_enter_ts: dict[str, float] = {}
        self.watchdogs: dict[str, dict] = {}
        # held_interrupt is a legacy/manual recovery state. New default
        # interrupts no longer enter it; --clear-hold can still release old
        # or manually planted holds. While set, reserve_next blocks delivery
        # to that alias.
        self.held_interrupt: dict[str, dict] = {}
        # interrupted_turns[alias] stores tombstones for interrupted prompts.
        # These do not block delivery; they suppress identifiable late
        # prompt_submitted / response_finished events from the cancelled turn
        # so corrected replacement prompts can run immediately after ESC.
        self.interrupted_turns: dict[str, list[dict]] = {}
        # Coarse RLock that serializes mutations to in-memory routing state
        # (busy, reserved, current_prompt_by_agent, held_interrupt,
        # interrupted_turns, last_enter_ts, watchdogs, panes, participants caches) and gates
        # event-handler / command-socket / maintenance interleaving.
        # Lock ordering rule: state_lock is ALWAYS acquired before
        # queue.update()'s file lock. Queue mutator callbacks must NOT
        # call back into self.* methods or invoke logging — they should
        # only manipulate the queue list and return data for callers to
        # process outside the mutator.
        self.state_lock = threading.RLock()
        self.last_delivery_tick = 0.0
        self.startup_backfill_summary: dict[str, dict] = {}
        self.reload_participants()
        if self.bridge_session and not self.dry_run:
            try:
                self.startup_backfill_summary = backfill_session_process_identities(self.bridge_session, self.session_state)
                self.reload_participants()
            except Exception as exc:
                self.startup_backfill_summary = {"_error": {"status": "unknown", "reason": str(exc)}}

    def start_command_server(self) -> None:
        if not self.command_socket:
            return
        if self.dry_run:
            return
        path = self.command_socket
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(path))
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            server.listen(16)
            server.settimeout(0.25)
        except OSError as exc:
            try:
                server.close()
            finally:
                self.command_server_socket = None
            self.log("command_socket_unavailable", command_socket=str(path), error=str(exc))
            return
        self.command_server_socket = server
        self.command_server_thread = threading.Thread(target=self.command_server_loop, name="bridge-command-socket", daemon=True)
        self.command_server_thread.start()

    def stop_command_server(self) -> None:
        server = self.command_server_socket
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
            self.command_server_socket = None
        if self.command_socket:
            try:
                self.command_socket.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass

    def command_server_loop(self) -> None:
        server = self.command_server_socket
        if server is None:
            return
        while not self.stop_requested():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                response = self.handle_command_connection(conn)
                try:
                    conn.sendall((json.dumps(response, ensure_ascii=True) + "\n").encode("utf-8"))
                except OSError:
                    pass

    def handle_enqueue_command(self, messages: list, force_response_send: bool = False) -> dict:
        if not isinstance(messages, list):
            return {"ok": False, "error": "messages must be a list"}
        ids = []
        self.reload_participants()
        with self.state_lock:
            validated: list[dict] = []
            for message in messages:
                if not isinstance(message, dict):
                    return {"ok": False, "error": "message entry must be an object"}
                if self.bridge_session and message.get("bridge_session") not in {None, "", self.bridge_session}:
                    return {"ok": False, "error": "bridge_session mismatch"}
                sender = str(message.get("from") or "")
                target = str(message.get("to") or "")
                if sender != "bridge" and sender not in self.participants:
                    return {"ok": False, "error": f"sender {sender!r} is not an active participant"}
                if target not in self.participants:
                    return {"ok": False, "error": f"target {target!r} is not an active participant"}
                if not message.get("id"):
                    message["id"] = short_id("msg")
                message["bridge_session"] = self.bridge_session
                context = self.current_prompt_by_agent.get(sender) or {}
                violation = response_send_violation(
                    sender=sender,
                    targets=[target],
                    outgoing_kind=normalize_kind(message.get("kind"), "request"),
                    force=bool(force_response_send),
                    contexts=[context_from_current_prompt(sender, context)] if context else [],
                    source="current_prompt",
                )
                if violation:
                    return {
                        "ok": False,
                        "error": format_response_send_violation(violation),
                        "error_kind": "response_send_guard",
                    }
                validated.append(message)
            for message in validated:
                self.enqueue_ipc_message(message)
                ids.append(message["id"])
        return {"ok": True, "ids": ids}

    def handle_command_connection(self, conn: socket.socket) -> dict:
        peer_uid = self.peer_uid(conn)
        if peer_uid is not None and peer_uid != os.getuid():
            return {"ok": False, "error": f"peer uid {peer_uid} is not allowed"}
        try:
            raw = b""
            while b"\n" not in raw and len(raw) < 2_000_000:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                raw += chunk
            request = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            return {"ok": False, "error": f"invalid request: {exc}"}

        if not isinstance(request, dict):
            return {"ok": False, "error": "unsupported command"}
        op = request.get("op")
        if op == "enqueue":
            return self.handle_enqueue_command(
                request.get("messages"),
                force_response_send=bool(request.get("force_response_send")),
            )
        if op == "alarm":
            self.reload_participants()
            sender = str(request.get("from") or "")
            if sender not in self.participants:
                return {"ok": False, "error": f"sender {sender!r} is not an active participant"}
            delay = request.get("delay_seconds")
            if delay is None:
                return {"ok": False, "error": "delay_seconds required"}
            try:
                delay_value = float(delay)
            except (TypeError, ValueError):
                return {"ok": False, "error": "delay_seconds must be a number"}
            if delay_value < 0:
                return {"ok": False, "error": "delay_seconds must be non-negative"}
            body = request.get("body")
            wake_id = self.register_alarm(sender, delay_value, body if isinstance(body, str) else None)
            if not wake_id:
                return {"ok": False, "error": "failed to register alarm"}
            return {"ok": True, "wake_id": wake_id}
        if op == "interrupt":
            self.reload_participants()
            sender = str(request.get("from") or "")
            target = str(request.get("target") or "")
            if sender and sender != "bridge" and sender not in self.participants:
                return {"ok": False, "error": f"sender {sender!r} is not an active participant"}
            if target not in self.participants:
                return {"ok": False, "error": f"target {target!r} is not an active participant"}
            result = self.handle_interrupt(sender, target)
            return {"ok": True, **result}
        if op == "clear_hold":
            self.reload_participants()
            sender = str(request.get("from") or "")
            target = str(request.get("target") or "")
            if sender and sender != "bridge" and sender not in self.participants:
                return {"ok": False, "error": f"sender {sender!r} is not an active participant"}
            if target not in self.participants:
                return {"ok": False, "error": f"target {target!r} is not an active participant"}
            info = self.release_hold(target, reason=f"manual_clear_by_{sender or 'bridge'}", by_sender=sender)
            return {
                "ok": True,
                "had_hold": info is not None,
                "info": info or {},
                "warning": (
                    "Forcing hold release before the peer's Stop event arrives can cause "
                    "late responses to misroute. Verify with --status that the peer is idle "
                    "before clearing."
                ),
            }
        if op == "extend_watchdog":
            self.reload_participants()
            sender = str(request.get("from") or "")
            message_id = str(request.get("message_id") or "")
            seconds = request.get("seconds")
            if sender and sender != "bridge" and sender not in self.participants:
                return {"ok": False, "error": f"sender {sender!r} is not an active participant"}
            if not message_id:
                return {"ok": False, "error": "message_id required"}
            try:
                additional_sec = float(seconds)
            except (TypeError, ValueError):
                return {"ok": False, "error": "seconds must be a number"}
            if additional_sec <= 0:
                return {"ok": False, "error": "seconds must be positive"}
            ok, err, deadline = self.upsert_message_watchdog(sender, message_id, additional_sec)
            if not ok:
                return {"ok": False, "error": err or "extend_failed"}
            return {"ok": True, "new_deadline": deadline, "message_id": message_id}
        if op == "status":
            self.reload_participants()
            target = str(request.get("target") or "")
            peers = []
            with self.state_lock:
                queue_snapshot = list(self.queue.read())
                aliases = [target] if target else sorted(self.participants)
                for alias in aliases:
                    if alias not in self.participants:
                        peers.append({"alias": alias, "active": False})
                        continue
                    participant = self.participants.get(alias) or {}
                    held = self.held_interrupt.get(alias)
                    cur = self.current_prompt_by_agent.get(alias) or {}
                    pane = str(participant.get("pane") or self.panes.get(alias) or "")
                    first_pending = next(
                        (
                            it for it in queue_snapshot
                            if it.get("to") == alias and it.get("status") == "pending"
                        ),
                        {},
                    )
                    delivered_count = sum(
                        1 for it in queue_snapshot
                        if it.get("to") == alias and it.get("status") == "delivered"
                    )
                    pending_count = sum(
                        1 for it in queue_snapshot
                        if it.get("to") == alias and it.get("status") == "pending"
                    )
                    inflight_count = sum(
                        1 for it in queue_snapshot
                        if it.get("to") == alias and it.get("status") in {"inflight", "submitted"}
                    )
                    peers.append({
                        "alias": alias,
                        "active": True,
                        "busy": bool(self.busy.get(alias)),
                        "reserved_message_id": self.reserved.get(alias),
                        "current_prompt_id": cur.get("id"),
                        "current_prompt_from": cur.get("from"),
                        "current_prompt_turn_id": cur.get("turn_id"),
                        "held": held is not None,
                        "held_info": held or {},
                        "_pane": pane,
                        "pane_mode_blocked_since": first_pending.get("pane_mode_blocked_since"),
                        "pane_mode_blocked_mode": first_pending.get("pane_mode_blocked_mode"),
                        "pane_mode_block_count": int(first_pending.get("pane_mode_block_count") or 0),
                        "delivered_count": delivered_count,
                        "inflight_count": inflight_count,
                        "pending_count": pending_count,
                    })
            for peer in peers:
                pane = peer.pop("_pane", "")
                if not peer.get("active") or not pane:
                    peer["pane_in_mode"] = False
                    peer["pane_mode"] = ""
                    continue
                status = self.pane_mode_status(str(pane))
                peer["pane_in_mode"] = bool(status.get("in_mode"))
                peer["pane_mode"] = str(status.get("mode") or "")
                if status.get("error"):
                    peer["pane_mode_error"] = status.get("error")
            return {"ok": True, "peers": peers}
        return {"ok": False, "error": "unsupported command"}

    def peer_uid(self, conn: socket.socket) -> int | None:
        so_peercred = getattr(socket, "SO_PEERCRED", None)
        if so_peercred is None:
            return None
        try:
            creds = conn.getsockopt(socket.SOL_SOCKET, so_peercred, struct.calcsize("3i"))
            _pid, uid, _gid = struct.unpack("3i", creds)
            return int(uid)
        except OSError:
            return None

    def enqueue_ipc_message(self, message: dict) -> None:
        # Daemon-socket ingress for an externally-originated message
        # (op=enqueue from bridge_enqueue.py). Append the message to the
        # queue, then run the unified alarm-cancel-on-incoming step
        # against the now-queued item. The same step is invoked by
        # handle_external_message_queued for the file-fallback ingress
        # path, so the alarm-cancel semantics is defined in exactly one
        # place (_apply_alarm_cancel_to_queued_message). Watchdog is
        # armed later at delivery time inside mark_message_delivered_by_id.
        with self.state_lock:
            if any(it.get("id") == message["id"] for it in self.queue.read()):
                self.log("message_enqueue_skipped_duplicate", message_id=message["id"], from_agent=message.get("from"), to=message.get("to"))
                return
            # Normalize incoming status. Official bridge_enqueue.py always
            # sends "ingressing", but defense-in-depth: if any external
            # client (including older or third-party CLIs) submits a
            # different status, force it to "ingressing" so the finalize
            # helper still runs for this message and alarm cancel does
            # not get silently bypassed.
            original_status = message.get("status")
            if original_status != "ingressing":
                message["status"] = "ingressing"
                if original_status is not None:
                    self.log(
                        "enqueue_status_normalized",
                        message_id=message.get("id"),
                        from_agent=message.get("from"),
                        from_status=original_status,
                    )

            def mutator(queue: list[dict]) -> None:
                queue.append(message)

            self.queue.update(mutator)
            self.log(
                "message_queued",
                message_id=message["id"],
                from_agent=message.get("from"),
                to=message.get("to"),
                kind=message.get("kind"),
                intent=message.get("intent"),
                causal_id=message.get("causal_id"),
                hop_count=message.get("hop_count"),
                auto_return=message.get("auto_return"),
                reply_to=message.get("reply_to"),
                aggregate_id=message.get("aggregate_id"),
                aggregate_expected=message.get("aggregate_expected"),
                source=message.get("source") or "ipc_enqueue",
                body=message.get("body"),
            )
            self._apply_alarm_cancel_to_queued_message(str(message["id"]))

    # v1.5 alarm cancellation: cap the bridge-added notice so it can never
    # crowd out the original body even after multiple alarms.
    _ALARM_NOTICE_MAX_NOTES = 3
    _ALARM_NOTICE_NOTE_CHARS = 80
    _ALARM_NOTICE_TOTAL_CHARS = 400

    def _maybe_cancel_alarms_for_incoming(self, message: dict) -> None:
        # Normalize kind defensively: malformed direct IPC could pass
        # arbitrary strings, but qualifying logic must be exact.
        kind = normalize_kind(message.get("kind"), "request")
        sender = str(message.get("from") or "")
        target = str(message.get("to") or "")
        if kind == "result":
            return
        if not target or not sender:
            return
        if sender == "bridge" or sender == target:
            return
        cancelled: list[tuple[str, dict]] = []
        for wake_id in list(self.watchdogs.keys()):
            wd = self.watchdogs.get(wake_id)
            if wd and wd.get("is_alarm") and wd.get("sender") == target:
                cancelled.append((wake_id, self.watchdogs.pop(wake_id)))
        if not cancelled:
            return
        notes: list[str] = []
        for _wake_id, wd in cancelled[: self._ALARM_NOTICE_MAX_NOTES]:
            note_raw = str(wd.get("alarm_body") or "").strip()
            if note_raw:
                if len(note_raw) > self._ALARM_NOTICE_NOTE_CHARS:
                    note_raw = note_raw[: self._ALARM_NOTICE_NOTE_CHARS - 1] + "…"
                notes.append(f'"{note_raw}"')
            else:
                notes.append("(no note)")
        omitted = max(0, len(cancelled) - self._ALARM_NOTICE_MAX_NOTES)
        if omitted:
            notes.append(f"+{omitted} more")
        notice_text = (
            f"[bridge:alarm_cancelled] {len(cancelled)} alarm(s) cancelled by this incoming "
            f"message: {', '.join(notes)}. Re-arm with `agent_alarm <sec> --note '<desc>'` "
            "if this is NOT what you were waiting for."
        )
        if len(notice_text) > self._ALARM_NOTICE_TOTAL_CHARS:
            notice_text = notice_text[: self._ALARM_NOTICE_TOTAL_CHARS - 1] + "…"
        compact_notice_text = f"[bridge:alarm_cancelled] {len(cancelled)} alarm(s) cancelled by this message."
        original_body = str(message.get("body") or "")
        notice_truncated = False
        notice_omitted = False
        notice_compacted = False
        if original_body:
            # Normal external sends reserve headroom below this guard. Legacy
            # queue items and internal synthetic bodies may still be near the
            # guard, so compact or omit the notice instead of emitting garbage.
            max_notice_chars = MAX_PEER_BODY_CHARS - len(original_body) - 2
            if max_notice_chars <= 0:
                notice_text = ""
                notice_omitted = True
            elif len(notice_text) + 2 + len(original_body) > MAX_PEER_BODY_CHARS:
                if len(compact_notice_text) <= max_notice_chars:
                    notice_text = compact_notice_text
                    notice_compacted = True
                else:
                    notice_text = ""
                    notice_omitted = True
                notice_truncated = notice_omitted or notice_compacted
        message["body"] = f"{notice_text}\n\n{original_body}" if notice_text and original_body else (notice_text or original_body)
        self.log(
            "alarm_cancelled_by_message",
            target=target,
            trigger_message_id=message.get("id"),
            trigger_from=sender,
            trigger_kind=kind,
            cancelled_count=len(cancelled),
            cancelled_wake_ids=[wid for wid, _ in cancelled],
            notice_truncated=notice_truncated,
            notice_omitted=notice_omitted,
            notice_compacted=notice_compacted,
            original_body_chars=len(original_body),
        )

    def fallback_session_state(self) -> dict:
        participants = {}
        if self.args.claude_pane:
            participants["claude"] = participant_record("claude", "claude", self.args.claude_pane)
        if self.args.codex_pane:
            participants["codex"] = participant_record("codex", "codex", self.args.codex_pane)
        return {
            "session": self.bridge_session or "",
            "participants": participants,
            "panes": {alias: record["pane"] for alias, record in participants.items()},
        }

    def reload_participants(self) -> None:
        state = {}
        if self.bridge_session:
            path = self.session_file
            try:
                mtime_ns = path.stat().st_mtime_ns
            except FileNotFoundError:
                mtime_ns = None
            if mtime_ns == self.session_mtime_ns and self.participants:
                return
            self.session_mtime_ns = mtime_ns
            state = read_json(path, {"session": self.bridge_session})
        if not active_participants(state):
            state = self.fallback_session_state()
        self.session_state = state
        self.participants = active_participants(state)
        self.panes = {
            alias: str(record.get("pane") or "")
            for alias, record in self.participants.items()
            if record.get("pane")
        }
        for alias in self.participants:
            self.busy.setdefault(alias, False)
            self.reserved.setdefault(alias, None)

    def participant_alias(self, record: dict) -> str | None:
        alias = record.get("bridge_agent") or record.get("agent")
        if alias in self.participants:
            return str(alias)
        physical = record.get("agent")
        matches = [
            candidate
            for candidate, participant in self.participants.items()
            if participant.get("agent_type") == physical
        ]
        if len(matches) == 1:
            return matches[0]
        return str(alias) if alias else None

    def log(self, event: str, **fields) -> None:
        record = {
            "ts": utc_now(),
            "agent": "bridge",
            "event": event,
            "bridge_session": self.bridge_session,
            **fields,
        }
        append_jsonl(self.state_file, record)
        if self.public_state_file and self.public_state_file != self.state_file:
            append_jsonl(self.public_state_file, public_record(record))
        if self.stdout_events:
            print(json.dumps(record, ensure_ascii=True), flush=True)

    def safe_log(self, event: str, **fields) -> None:
        try:
            self.log(event, **fields)
        except OSError:
            pass

    def remember_nonce(self, nonce: str, message: dict) -> None:
        self.injected_by_nonce[nonce] = dict(message)
        self.injected_by_nonce.move_to_end(nonce)
        while len(self.injected_by_nonce) > MAX_NONCE_CACHE:
            self.injected_by_nonce.popitem(last=False)

    def cached_nonce(self, nonce: str) -> dict | None:
        message = self.injected_by_nonce.get(nonce)
        if not message:
            return None
        self.injected_by_nonce.move_to_end(nonce)
        return dict(message)

    def find_inflight_candidate(self, agent: str) -> dict | None:
        """Pick the queue item that was just delivered to `agent`.

        Order of resolution:
          1. reserved[agent]: in-memory pointer set when prompt delivery was
             called and not yet cleared by the prompt_submitted that
             confirms submission. Verified against queue (must still be
             status=inflight and to=agent).
          2. queue scan for the unique item with to=agent and
             status=inflight. Covers daemon restart (in-memory reserved
             lost) and any path that bypassed reserved.

        Multiple inflight items for the same target indicate a queue
        invariant violation; we log and return None (fail-closed).

        Caller MUST hold state_lock.
        """
        queue = list(self.queue.read())
        msg_id = self.reserved.get(agent)
        if msg_id:
            for item in queue:
                if item.get("id") != msg_id:
                    continue
                if item.get("to") != agent or item.get("status") != "inflight":
                    # Stale reserved pointer: the message moved on (cancelled,
                    # already delivered, etc.) but reserved was not cleared.
                    break
                return dict(item)
        candidates = [
            item for item in queue
            if item.get("to") == agent and item.get("status") == "inflight"
        ]
        if len(candidates) == 1:
            return dict(candidates[0])
        if len(candidates) > 1:
            self.log(
                "ambiguous_inflight",
                agent=agent,
                count=len(candidates),
                message_ids=[c.get("id") for c in candidates],
            )
        return None

    def discard_nonce(self, nonce: str | None) -> None:
        if nonce:
            self.injected_by_nonce.pop(str(nonce), None)

    def stop_requested(self) -> bool:
        if self.stop_logged:
            return True

        if _STOP_SIGNAL is not None:
            try:
                signal_name = signal.Signals(_STOP_SIGNAL).name
            except ValueError:
                signal_name = str(_STOP_SIGNAL)
            self.log("daemon_stop_requested", signal=signal_name)
            self.stop_logged = True
            return True

        if self.stop_file and self.stop_file.exists():
            self.log("daemon_stop_requested", stop_file=str(self.stop_file))
            self.stop_logged = True
            return True

        return False

    def wait_or_stop(self, seconds: float) -> bool:
        deadline = time.time() + max(0.0, seconds)
        while time.time() < deadline:
            if self.stop_requested():
                return True
            time.sleep(min(0.25, deadline - time.time()))
        return self.stop_requested()

    def pane_mode_status(self, pane: str) -> dict:
        if self.dry_run:
            return {"in_mode": False, "mode": "", "error": ""}
        return probe_tmux_pane_mode(pane)

    def force_cancel_pane_mode(self, pane: str, mode: str) -> tuple[bool, str]:
        if self.dry_run:
            return True, ""
        return cancel_tmux_pane_mode(pane)

    def resolve_endpoint_detail(self, target: str, *, purpose: str = "write") -> dict:
        participant = self.participants.get(target)
        if not participant:
            self.panes.pop(target, None)
            return {"ok": False, "pane": "", "reason": "unknown_target", "probe_status": "", "detail": "", "should_detach": False}
        detail = resolve_participant_endpoint_detail(self.bridge_session or "", target, participant, purpose=purpose)
        if detail.get("ok"):
            self.panes[target] = str(detail.get("pane") or "")
            if detail.get("reconnected"):
                self.session_mtime_ns = None
                self.reload_participants()
                self.panes[target] = str(detail.get("pane") or "")
            return detail
        # Unit-style dry-run scenarios historically omit hook identities. Keep
        # that test fixture convenience, but real tmux writes never take this
        # path because dry_run=False in production.
        if self.dry_run and not participant.get("hook_session_id"):
            pane = str(participant.get("pane") or "")
            if pane:
                self.panes[target] = pane
                return {"ok": True, "pane": pane, "reason": "dry_run_unverified", "probe_status": "", "detail": "", "should_detach": False}
        self.panes.pop(target, None)
        return detail

    def resolve_target_pane(self, target: str) -> str:
        detail = self.resolve_endpoint_detail(target, purpose="write")
        return str(detail.get("pane") or "") if detail.get("ok") else ""

    def next_pending_candidate(self, target: str) -> dict | None:
        if target in self.held_interrupt:
            return None
        if self.busy.get(target) or self.reserved.get(target):
            return None

        def mutator(queue: list[dict]) -> dict | None:
            if any(item.get("to") == target and item.get("status") in {"inflight", "submitted", "delivered"} for item in queue):
                return None
            for item in queue:
                if item.get("to") == target and item.get("status") == "pending":
                    return dict(item)
            return None

        return self.queue.update(mutator)

    def annotate_pending_pane_mode_block(self, target: str, message_id: str, mode: str) -> dict | None:
        now_ts = time.time()
        now_iso = utc_now()

        def mutator(queue: list[dict]) -> dict | None:
            for item in queue:
                if item.get("id") != message_id or item.get("to") != target or item.get("status") != "pending":
                    continue
                prior_mode = str(item.get("pane_mode_blocked_mode") or "")
                since_ts = pane_mode_block_since_ts(item)
                started = since_ts is None
                if since_ts is None:
                    since_ts = now_ts
                    item["pane_mode_blocked_since"] = now_iso
                    item["pane_mode_blocked_since_ts"] = now_ts
                if prior_mode and prior_mode != mode:
                    item.pop("pane_mode_unforceable_logged", None)
                    item.pop("pane_mode_cancel_failed_logged", None)
                item.pop("pane_mode_probe_failed_logged", None)
                item.pop("last_pane_mode_probe_error", None)
                item["pane_mode_blocked_mode"] = mode
                item["pane_mode_block_count"] = int(item.get("pane_mode_block_count") or 0) + 1
                item["last_error"] = "pane_in_mode"
                item["updated_ts"] = now_iso
                result = dict(item)
                result["_pane_mode_started"] = started
                result["_pane_mode_blocked_age"] = max(0.0, now_ts - float(since_ts))
                return result
            return None

        return self.queue.update(mutator)

    def clear_pane_mode_metadata(self, message_id: str) -> dict | None:
        def mutator(queue: list[dict]) -> dict | None:
            for item in queue:
                if item.get("id") != message_id:
                    continue
                had_metadata = any(key in item for key in PANE_MODE_METADATA_KEYS)
                if not had_metadata:
                    return None
                prior = dict(item)
                for key in PANE_MODE_METADATA_KEYS:
                    item.pop(key, None)
                if item.get("last_error") in {"pane_in_mode", "pane_mode_unforceable", "pane_mode_cancel_failed", "pane_mode_probe_failed"}:
                    item.pop("last_error", None)
                item["updated_ts"] = utc_now()
                return prior
            return None

        return self.queue.update(mutator)

    def mark_pane_mode_unforceable(self, message_id: str) -> bool:
        def mutator(queue: list[dict]) -> bool:
            for item in queue:
                if item.get("id") != message_id:
                    continue
                already = bool(item.get("pane_mode_unforceable_logged"))
                item["pane_mode_unforceable_logged"] = True
                item["last_error"] = "pane_mode_unforceable"
                item["updated_ts"] = utc_now()
                return not already
            return False

        return bool(self.queue.update(mutator))

    def mark_pane_mode_cancel_failed(self, message_id: str, error: str) -> bool:
        def mutator(queue: list[dict]) -> bool:
            for item in queue:
                if item.get("id") != message_id:
                    continue
                already = bool(item.get("pane_mode_cancel_failed_logged"))
                item["pane_mode_cancel_failed_logged"] = True
                item["last_error"] = "pane_mode_cancel_failed"
                item["last_pane_mode_cancel_error"] = error
                item["updated_ts"] = utc_now()
                return not already
            return False

        return bool(self.queue.update(mutator))

    def mark_pane_mode_probe_failed(self, message_id: str, error: str) -> bool:
        def mutator(queue: list[dict]) -> bool:
            for item in queue:
                if item.get("id") != message_id:
                    continue
                already = bool(item.get("pane_mode_probe_failed_logged"))
                item["pane_mode_probe_failed_logged"] = True
                item["last_error"] = "pane_mode_probe_failed"
                item["last_pane_mode_probe_error"] = error
                item["updated_ts"] = utc_now()
                return not already
            return False

        return bool(self.queue.update(mutator))

    def defer_inflight_for_pane_mode_probe_failed(self, message: dict, error: str) -> dict | None:
        message_id = str(message.get("id") or "")
        target = str(message.get("to") or "")
        now_iso = utc_now()

        def mutator(queue: list[dict]) -> dict | None:
            for item in queue:
                if item.get("id") != message_id or item.get("to") != target or item.get("status") != "inflight":
                    continue
                already = bool(item.get("pane_mode_probe_failed_logged"))
                item["status"] = "pending"
                item["nonce"] = None
                item["delivery_attempts"] = max(0, int(item.get("delivery_attempts") or 0) - 1)
                item["pane_mode_probe_failed_logged"] = True
                item["last_error"] = "pane_mode_probe_failed"
                item["last_pane_mode_probe_error"] = error
                item["updated_ts"] = now_iso
                result = dict(item)
                result["_pane_mode_probe_failed_first"] = not already
                return result
            return None

        return self.queue.update(mutator)

    def blocked_duration(self, item: dict | None) -> float | None:
        if not item:
            return None
        since_ts = pane_mode_block_since_ts(item)
        if since_ts is None:
            return None
        return max(0.0, time.time() - since_ts)

    def maybe_defer_for_pane_mode(self, target: str, pane: str, message: dict) -> bool:
        status = self.pane_mode_status(pane)
        if status.get("error"):
            if self.mark_pane_mode_probe_failed(str(message.get("id") or ""), str(status.get("error") or "")):
                self.safe_log(
                    "pane_mode_probe_failed",
                    target=target,
                    pane=pane,
                    message_id=message.get("id"),
                    error=status.get("error"),
                    phase="pre_reserve",
                )
            return True
        if not status.get("in_mode"):
            cleared = self.clear_pane_mode_metadata(str(message.get("id") or ""))
            if cleared:
                self.log(
                    "pane_mode_block_cleared",
                    target=target,
                    pane=pane,
                    message_id=message.get("id"),
                    mode=cleared.get("pane_mode_blocked_mode"),
                    blocked_duration_sec=self.blocked_duration(cleared),
                )
            return False

        mode = str(status.get("mode") or "")
        blocked = self.annotate_pending_pane_mode_block(target, str(message.get("id") or ""), mode)
        if not blocked:
            return True
        if blocked.get("_pane_mode_started"):
            self.log(
                "pane_mode_block_started",
                target=target,
                pane=pane,
                message_id=message.get("id"),
                mode=mode,
            )
        age = float(blocked.get("_pane_mode_blocked_age") or 0.0)
        if self.pane_mode_grace_seconds is None or age < self.pane_mode_grace_seconds:
            return True
        if mode not in PANE_MODE_FORCE_CANCEL_MODES:
            if self.mark_pane_mode_unforceable(str(message.get("id") or "")):
                self.log(
                    "pane_mode_block_unforceable",
                    target=target,
                    pane=pane,
                    message_id=message.get("id"),
                    mode=mode,
                    blocked_duration_sec=age,
                )
            return True

        ok, error = self.force_cancel_pane_mode(pane, mode)
        if not ok:
            if self.mark_pane_mode_cancel_failed(str(message.get("id") or ""), error):
                self.log(
                    "pane_mode_force_cancelled",
                    target=target,
                    pane=pane,
                    message_id=message.get("id"),
                    mode=mode,
                    blocked_duration_sec=age,
                    success=False,
                    error=error,
                )
            return True

        after = self.pane_mode_status(pane)
        if after.get("error"):
            error = str(after.get("error"))
            if self.mark_pane_mode_cancel_failed(str(message.get("id") or ""), error):
                self.log(
                    "pane_mode_force_cancelled",
                    target=target,
                    pane=pane,
                    message_id=message.get("id"),
                    mode=mode,
                    blocked_duration_sec=age,
                    success=False,
                    error=error,
                )
            return True
        if after.get("in_mode"):
            error = f"pane still in mode {after.get('mode') or ''}".strip()
            if self.mark_pane_mode_cancel_failed(str(message.get("id") or ""), error):
                self.log(
                    "pane_mode_force_cancelled",
                    target=target,
                    pane=pane,
                    message_id=message.get("id"),
                    mode=mode,
                    blocked_duration_sec=age,
                    success=False,
                    error=error,
                )
            return True

        self.clear_pane_mode_metadata(str(message.get("id") or ""))
        self.log(
            "pane_mode_force_cancelled",
            target=target,
            pane=pane,
            message_id=message.get("id"),
            mode=mode,
            blocked_duration_sec=age,
            success=True,
        )
        return False

    def defer_inflight_for_pane_mode(self, message: dict, mode: str) -> dict | None:
        message_id = str(message.get("id") or "")
        target = str(message.get("to") or "")
        now_ts = time.time()
        now_iso = utc_now()

        def mutator(queue: list[dict]) -> dict | None:
            for item in queue:
                if item.get("id") != message_id or item.get("to") != target or item.get("status") != "inflight":
                    continue
                since_ts = pane_mode_block_since_ts(item)
                started = since_ts is None
                if since_ts is None:
                    since_ts = pane_mode_block_since_ts(message) or now_ts
                    item["pane_mode_blocked_since"] = message.get("pane_mode_blocked_since") or now_iso
                    item["pane_mode_blocked_since_ts"] = since_ts
                item["status"] = "pending"
                item["nonce"] = None
                item["pane_mode_blocked_mode"] = mode
                item["pane_mode_block_count"] = int(item.get("pane_mode_block_count") or 0) + 1
                item["delivery_attempts"] = max(0, int(item.get("delivery_attempts") or 0) - 1)
                item["last_error"] = "pane_in_mode"
                item["updated_ts"] = now_iso
                result = dict(item)
                result["_pane_mode_started"] = started
                result["_pane_mode_blocked_age"] = max(0.0, now_ts - float(since_ts))
                return result
            return None

        return self.queue.update(mutator)

    def mark_enter_deferred_for_pane_mode(self, message_id: str, target: str, mode: str, error: str = "") -> dict | None:
        now_ts = time.time()
        now_iso = utc_now()

        def mutator(queue: list[dict]) -> dict | None:
            for item in queue:
                if item.get("id") != message_id or item.get("to") != target or item.get("status") != "inflight":
                    continue
                started = "pane_mode_enter_deferred_since_ts" not in item
                if started:
                    item["pane_mode_enter_deferred_since"] = now_iso
                    item["pane_mode_enter_deferred_since_ts"] = now_ts
                item["pane_mode_enter_deferred_mode"] = mode
                if error:
                    item["last_pane_mode_probe_error"] = error
                    item["last_error"] = "pane_mode_probe_failed_waiting_enter"
                else:
                    item.pop("last_pane_mode_probe_error", None)
                    item["last_error"] = "pane_in_mode_waiting_enter"
                item["updated_ts"] = now_iso
                result = dict(item)
                result["_pane_mode_enter_defer_started"] = started
                return result
            return None

        return self.queue.update(mutator)

    def clear_enter_deferred_metadata(self, message_id: str) -> None:
        def mutator(queue: list[dict]) -> None:
            for item in queue:
                if item.get("id") != message_id:
                    continue
                for key in PANE_MODE_ENTER_DEFER_KEYS:
                    item.pop(key, None)
                item.pop("last_pane_mode_probe_error", None)
                if item.get("last_error") in {"pane_in_mode_waiting_enter", "pane_mode_probe_failed_waiting_enter"}:
                    item.pop("last_error", None)
                item["updated_ts"] = utc_now()

        self.queue.update(mutator)

    def queue_message(self, message: dict, log_event: bool = True) -> None:
        def mutator(queue: list[dict]) -> None:
            if not any(item.get("id") == message["id"] for item in queue):
                queue.append(message)

        self.queue.update(mutator)
        if log_event:
            self.log(
                "message_queued",
                message_id=message["id"],
                from_agent=message["from"],
                to=message["to"],
                kind=message.get("kind"),
                intent=message["intent"],
                causal_id=message["causal_id"],
                hop_count=message["hop_count"],
                auto_return=message["auto_return"],
                reply_to=message.get("reply_to"),
                aggregate_id=message.get("aggregate_id"),
                aggregate_expected=message.get("aggregate_expected"),
                source=message.get("source"),
            )
        self.try_deliver(str(message["to"]))

    def reserve_next(self, target: str) -> dict | None:
        # Block delivery while target is held_interrupt: hold is released
        # only by an incoming response_finished or an explicit clear_hold
        # command, never by a timer.
        if target in self.held_interrupt:
            return None
        if self.busy.get(target) or self.reserved.get(target):
            return None

        def mutator(queue: list[dict]) -> dict | None:
            # `delivered` is also a delivery blocker so that, even after a
            # daemon restart that wipes in-memory busy/reserved state, a
            # message that was already injected into the peer's pane is
            # not redelivered nor allowed to be overlaid by a fresh prompt
            # before its terminal response_finished is observed.
            if any(item.get("to") == target and item.get("status") in {"inflight", "submitted", "delivered"} for item in queue):
                return None
            for item in queue:
                if item.get("to") != target or item.get("status") != "pending":
                    continue
                item["status"] = "inflight"
                item["nonce"] = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
                item["updated_ts"] = utc_now()
                item["delivery_attempts"] = int(item.get("delivery_attempts") or 0) + 1
                return dict(item)
            return None

        return self.queue.update(mutator)

    def try_deliver(self, target: str | None = None) -> None:
        self.reload_participants()
        with self.state_lock:
            targets = [target] if target else sorted(self.participants)
            for agent in targets:
                if agent not in self.participants:
                    continue
                candidate = self.next_pending_candidate(agent)
                if not candidate:
                    continue
                pane = self.resolve_target_pane(agent)
                if pane and self.maybe_defer_for_pane_mode(agent, pane, candidate):
                    continue
                message = self.reserve_next(agent)
                if not message:
                    continue
                self.deliver_reserved(message)

    def deliver_reserved(self, message: dict) -> None:
        target = str(message["to"])
        nonce = str(message["nonce"])
        endpoint_detail = self.resolve_endpoint_detail(target, purpose="write")
        if not endpoint_detail.get("ok"):
            self.finalize_undeliverable_message(message, endpoint_detail, phase="pre_send")
            return
        pane = str(endpoint_detail.get("pane") or "")
        mode_status = self.pane_mode_status(pane)
        if mode_status.get("error"):
            deferred = self.defer_inflight_for_pane_mode_probe_failed(message, str(mode_status.get("error") or ""))
            if deferred and deferred.get("_pane_mode_probe_failed_first"):
                self.safe_log(
                    "pane_mode_probe_failed",
                    target=target,
                    pane=pane,
                    message_id=message.get("id"),
                    error=mode_status.get("error"),
                    phase="pre_send",
                )
            return
        elif mode_status.get("in_mode"):
            mode = str(mode_status.get("mode") or "")
            blocked = self.defer_inflight_for_pane_mode(message, mode)
            if blocked and blocked.get("_pane_mode_started"):
                self.log(
                    "pane_mode_block_started",
                    target=target,
                    pane=pane,
                    message_id=message.get("id"),
                    mode=mode,
                    phase="pre_send",
                )
            self.last_enter_ts.pop(str(message.get("id") or ""), None)
            return
        else:
            self.clear_pane_mode_metadata(str(message.get("id") or ""))

        self.reserved[target] = str(message["id"])
        self.remember_nonce(nonce, message)
        body_text = str(message.get("body") or "")
        normalized_body_text = normalize_prompt_body_text(body_text)
        if len(normalized_body_text) > MAX_PEER_BODY_CHARS:
            self.log(
                "body_truncated",
                message_id=message.get("id"),
                from_agent=message.get("from"),
                to=target,
                kind=message.get("kind"),
                intent=message.get("intent"),
                original_chars=len(body_text),
                normalized_chars=len(normalized_body_text),
                limit_chars=MAX_PEER_BODY_CHARS,
            )
        prompt = build_peer_prompt(message, nonce, self.max_hops)
        enter_deferred = False

        try:
            if not self.dry_run:
                run_tmux_send_literal(
                    pane,
                    prompt,
                    bridge_session=self.bridge_session,
                    target_alias=target,
                    message_id=str(message["id"]),
                    nonce=nonce,
                )
                time.sleep(self.submit_delay)
                enter_status = self.pane_mode_status(pane)
                if enter_status.get("error"):
                    enter_deferred = True
                    error = str(enter_status.get("error") or "")
                    defer_info = self.mark_enter_deferred_for_pane_mode(str(message["id"]), target, "probe_error", error=error)
                    if defer_info and defer_info.get("_pane_mode_enter_defer_started"):
                        self.safe_log(
                            "pane_mode_probe_failed",
                            target=target,
                            pane=pane,
                            message_id=message.get("id"),
                            error=error,
                            phase="pre_enter",
                        )
                elif enter_status.get("in_mode"):
                    enter_deferred = True
                    mode = str(enter_status.get("mode") or "")
                    defer_info = self.mark_enter_deferred_for_pane_mode(str(message["id"]), target, mode)
                    if defer_info and defer_info.get("_pane_mode_enter_defer_started"):
                        self.log(
                            "enter_deferred_pane_mode",
                            message_id=message["id"],
                            to=target,
                            mode=mode,
                        )
                else:
                    self.clear_enter_deferred_metadata(str(message["id"]))
                    run_tmux_enter(pane)
        except Exception as exc:
            self.mark_message_pending(str(message["id"]), str(exc))
            self.reserved[target] = None
            self.discard_nonce(nonce)
            self.log(
                "message_delivery_failed",
                message_id=message["id"],
                to=target,
                nonce=nonce,
                error=str(exc),
            )
            return

        self.last_enter_ts[str(message["id"])] = time.time()
        self.log(
            "message_delivery_attempted",
            message_id=message["id"],
            from_agent=message["from"],
            to=target,
            kind=message.get("kind"),
            intent=message["intent"],
            nonce=nonce,
            causal_id=message["causal_id"],
            hop_count=message["hop_count"],
            auto_return=message["auto_return"],
            aggregate_id=message.get("aggregate_id"),
            dry_run=self.dry_run,
            enter_deferred=enter_deferred,
        )

    def mark_message_pending(self, message_id: str, error: str | None = None) -> None:
        def mutator(queue: list[dict]) -> None:
            for item in queue:
                if item.get("id") != message_id:
                    continue
                item["status"] = "pending"
                item["nonce"] = None
                item["updated_ts"] = utc_now()
                if error:
                    item["last_error"] = error
                    if not str(error).startswith("pane_"):
                        for key in PANE_MODE_METADATA_KEYS + PANE_MODE_ENTER_DEFER_KEYS:
                            item.pop(key, None)

        self.queue.update(mutator)

    def mark_message_submitted(self, message_id: str) -> None:
        def mutator(queue: list[dict]) -> None:
            for item in queue:
                if item.get("id") != message_id:
                    continue
                item["status"] = "submitted"
                item["updated_ts"] = utc_now()
                item.pop("last_error", None)

        self.queue.update(mutator)

    def ack_message(self, nonce: str) -> dict | None:
        # Deprecated: previously removed the matching item from queue at
        # prompt_submit time. With the v1 message lifecycle, prompt_submit
        # only marks status="delivered" via mark_message_delivered_by_id, and the
        # queue removal happens at terminal handle_response_finished.
        # Kept temporarily for any external callers; not used internally.
        def mutator(queue: list[dict]) -> dict | None:
            found = None
            kept = []
            for item in queue:
                if item.get("nonce") == nonce:
                    found = dict(item)
                else:
                    kept.append(item)
            queue[:] = kept
            return found

        return self.queue.update(mutator)

    def mark_message_delivered_by_id(self, agent: str, message_id: str) -> dict | None:
        """Mark message status=delivered using id+recipient as the primary key.

        v1.5.2 authoritative path. Verifies recipient and status before
        flipping state — never trust hook-extracted nonce alone.
        """
        def mutator(queue: list[dict]) -> dict | None:
            for item in queue:
                if item.get("id") != message_id:
                    continue
                if item.get("to") != agent or item.get("status") != "inflight":
                    return None
                item["status"] = "delivered"
                item["delivered_ts"] = utc_now()
                item["updated_ts"] = utc_now()
                item.pop("last_error", None)
                for key in PANE_MODE_METADATA_KEYS + PANE_MODE_ENTER_DEFER_KEYS:
                    item.pop(key, None)
                return dict(item)
            return None

        message = self.queue.update(mutator)
        # v1.5 semantics: arm the watchdog at delivery time. The countdown
        # is "time since the prompt was actually injected into the peer's
        # pane", not since enqueue. If the message is never delivered
        # (peer held forever / dead), no watchdog ever fires; that is the
        # accepted v1.5 trade-off.
        if message and message.get("watchdog_delay_sec") is not None:
            try:
                delay = max(0.0, float(message["watchdog_delay_sec"]))
            except (TypeError, ValueError):
                delay = None
            if delay is not None:
                deadline = datetime.now(timezone.utc) + timedelta(seconds=delay)
                arm_msg = dict(message)
                arm_msg["watchdog_at"] = deadline.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                self.register_watchdog(arm_msg)
        return message

    def upsert_message_watchdog(self, sender: str, message_id: str, additional_sec: float) -> tuple[bool, str | None, str | None]:
        # All validation + upsert inside a single state_lock acquire so the
        # checks and the registration cannot interleave with delivery,
        # cancellation, or termination of the same message.
        # Returns (ok, error_code, new_deadline_iso).
        with self.state_lock:
            queue = list(self.queue.read())
            item = next((it for it in queue if it.get("id") == message_id), None)
            if not item:
                return False, "message_not_found", None
            if str(item.get("from") or "") != sender:
                return False, "not_owner", None
            if item.get("aggregate_id"):
                return False, "aggregate_extend_not_supported", None
            # D1 semantic: a watchdog only counts down once the prompt has
            # been delivered to the peer. Refuse to (re)arm for messages
            # that are still pending/inflight/submitted; the sender should
            # use agent_view_peer / agent_interrupt_peer to unblock instead.
            if item.get("status") != "delivered":
                return False, "message_not_in_delivered_state", None
            new_deadline = datetime.now(timezone.utc) + timedelta(seconds=max(0.0, float(additional_sec)))
            new_deadline_iso = new_deadline.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            for wake_id in list(self.watchdogs.keys()):
                wd = self.watchdogs.get(wake_id)
                if wd and wd.get("ref_message_id") == message_id and not wd.get("is_alarm"):
                    self.watchdogs.pop(wake_id, None)
            arm_msg = dict(item)
            arm_msg["watchdog_at"] = new_deadline_iso
            self.register_watchdog(arm_msg)
            self.log(
                "watchdog_extended",
                message_id=message_id,
                new_deadline=new_deadline_iso,
                additional_sec=float(additional_sec),
                status=item.get("status"),
            )
            return True, None, new_deadline_iso

    def remove_delivered_message(self, target: str, message_id: str) -> dict | None:
        def mutator(queue: list[dict]) -> dict | None:
            found = None
            kept = []
            for item in queue:
                if item.get("id") == message_id and item.get("to") == target:
                    found = dict(item)
                    continue
                kept.append(item)
            queue[:] = kept
            return found

        return self.queue.update(mutator)

    def finalize_undeliverable_message(self, message: dict, endpoint_detail: dict, *, phase: str) -> dict | None:
        message_id = str(message.get("id") or "")
        target = str(message.get("to") or "")
        if not message_id:
            return None

        def mutator(queue: list[dict]) -> dict | None:
            found = None
            kept = []
            for item in queue:
                if item.get("id") == message_id:
                    found = dict(item)
                    continue
                kept.append(item)
            queue[:] = kept
            return found

        removed = self.queue.update(mutator) or dict(message)
        nonce = str(removed.get("nonce") or message.get("nonce") or "")
        if nonce:
            self.discard_nonce(nonce)
        if self.reserved.get(target) == message_id:
            self.reserved[target] = None
        self.last_enter_ts.pop(message_id, None)
        active = self.current_prompt_by_agent.get(target) or {}
        if str(active.get("id") or "") == message_id:
            self.current_prompt_by_agent.pop(target, None)
            self.busy[target] = False
        self.cancel_watchdogs_for_message(message_id, reason="endpoint_lost")

        reason = str(endpoint_detail.get("reason") or "endpoint_lost")
        probe_status = str(endpoint_detail.get("probe_status") or "")
        self.log(
            "message_undeliverable",
            message_id=message_id,
            from_agent=removed.get("from"),
            to=target,
            kind=removed.get("kind"),
            aggregate_id=removed.get("aggregate_id"),
            reason=reason,
            probe_status=probe_status,
            detail=endpoint_detail.get("detail"),
            phase=phase,
        )

        if removed.get("aggregate_id"):
            self._record_aggregate_interrupted_reply(removed, by_sender="bridge", reason="endpoint_lost")
            return removed

        kind = normalize_kind(removed.get("kind"), "request")
        requester = str(removed.get("from") or "")
        if kind == "request" and bool(removed.get("auto_return")) and requester and requester != "bridge" and requester in self.participants:
            body = (
                f"[bridge:undeliverable] Message {message_id} to {target} could not be delivered because "
                f"the target has no verified live endpoint ({reason}). The target may have exited, been killed, "
                "or the tmux pane may have been reused. Ask the human to reattach/join the target, or remove it from the room."
            )
            result = make_message(
                sender="bridge",
                target=requester,
                intent="undeliverable_result",
                body=body,
                causal_id=str(removed.get("causal_id") or short_id("causal")),
                hop_count=int(removed.get("hop_count") or 0),
                auto_return=False,
                kind="result",
                reply_to=message_id,
                source="endpoint_lost",
            )
            self.queue_message(result)
        return removed

    def retry_enter_for_inflight(self) -> None:
        now = time.time()
        with self.state_lock:
            for item in list(self.queue.read()):
                if item.get("status") != "inflight":
                    continue
                msg_id = str(item.get("id") or "")
                if not msg_id:
                    continue
                last = self.last_enter_ts.get(msg_id)
                enter_deferred = bool(item.get("pane_mode_enter_deferred_since_ts"))
                if last is None and not enter_deferred:
                    continue
                if last is not None and now - last < 1.0:
                    continue
                target = str(item.get("to") or "")
                endpoint_detail = self.resolve_endpoint_detail(target, purpose="write")
                if not endpoint_detail.get("ok"):
                    self.finalize_undeliverable_message(item, endpoint_detail, phase="enter_retry")
                    self.last_enter_ts.pop(msg_id, None)
                    continue
                pane = str(endpoint_detail.get("pane") or "")
                mode_status = self.pane_mode_status(pane)
                if mode_status.get("error"):
                    error = str(mode_status.get("error") or "")
                    defer_info = self.mark_enter_deferred_for_pane_mode(msg_id, target, "probe_error", error=error)
                    self.last_enter_ts[msg_id] = now
                    if defer_info and defer_info.get("_pane_mode_enter_defer_started"):
                        self.safe_log(
                            "pane_mode_probe_failed",
                            target=target,
                            pane=pane,
                            message_id=msg_id,
                            error=error,
                            phase="enter_retry",
                        )
                    continue
                elif mode_status.get("in_mode"):
                    mode = str(mode_status.get("mode") or "")
                    defer_info = self.mark_enter_deferred_for_pane_mode(msg_id, target, mode)
                    self.last_enter_ts[msg_id] = now
                    if defer_info and defer_info.get("_pane_mode_enter_defer_started"):
                        self.log(
                            "enter_retry_deferred_pane_mode",
                            message_id=msg_id,
                            to=target,
                            mode=mode,
                        )
                    continue
                else:
                    self.clear_enter_deferred_metadata(msg_id)
                try:
                    if not self.dry_run:
                        run_tmux_enter(pane)
                except Exception as exc:
                    self.safe_log(
                        "enter_retry_failed",
                        message_id=msg_id,
                        to=target,
                        error=str(exc),
                    )
                    continue
                self.last_enter_ts[msg_id] = now
                self.log(
                    "enter_retry",
                    message_id=msg_id,
                    to=target,
                    attempts=int(item.get("delivery_attempts") or 0),
                )

    def register_watchdog(self, message: dict) -> None:
        deadline_iso = message.get("watchdog_at")
        if not deadline_iso:
            return
        try:
            deadline = datetime.fromisoformat(str(deadline_iso).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            self.safe_log(
                "watchdog_register_failed",
                message_id=message.get("id"),
                watchdog_at=deadline_iso,
                error="invalid_watchdog_at",
            )
            return
        sender = str(message.get("from") or "")
        if not sender or sender == "bridge":
            return
        aggregate_id = message.get("aggregate_id")
        message_id = message.get("id")
        with self.state_lock:
            if aggregate_id:
                # An aggregate yields N messages with the same aggregate_id; only
                # register a single watchdog so the sender receives at most one
                # wake notice when the deadline elapses.
                for existing in self.watchdogs.values():
                    if existing.get("ref_aggregate_id") == aggregate_id and not existing.get("is_alarm"):
                        return
            elif message_id:
                # Non-aggregate: dedupe by ref_message_id so that a redelivery
                # or extend_wait does not produce two concurrent watchdogs for
                # the same request.
                for wake_id_existing in list(self.watchdogs.keys()):
                    existing = self.watchdogs.get(wake_id_existing)
                    if existing and existing.get("ref_message_id") == message_id and not existing.get("is_alarm"):
                        self.watchdogs.pop(wake_id_existing, None)
            wake_id = short_id("wake")
            self.watchdogs[wake_id] = {
                "sender": sender,
                "deadline": deadline,
                "ref_message_id": message.get("id"),
                "ref_aggregate_id": message.get("aggregate_id"),
                "ref_to": message.get("to"),
                "ref_kind": normalize_kind(message.get("kind"), "request"),
                "ref_intent": message.get("intent"),
                "ref_causal_id": message.get("causal_id"),
                "ref_aggregate_expected": list(message.get("aggregate_expected") or []),
                "ref_created_ts": message.get("created_ts"),
                "is_alarm": bool(message.get("alarm")),
            }
            self.log(
                "watchdog_registered",
                wake_id=wake_id,
                sender=sender,
                ref_message_id=message.get("id"),
                ref_aggregate_id=message.get("aggregate_id"),
                deadline=deadline_iso,
                kind=message.get("kind"),
                is_alarm=bool(message.get("alarm")),
            )

    def register_alarm(self, sender: str, delay_seconds: float, body: str | None = None) -> str | None:
        if not sender or sender == "bridge":
            return None
        try:
            delay = max(0.0, float(delay_seconds))
        except (TypeError, ValueError):
            return None
        deadline = time.time() + delay
        wake_id = short_id("wake")
        with self.state_lock:
            self.watchdogs[wake_id] = {
                "sender": sender,
                "deadline": deadline,
                "ref_message_id": None,
                "ref_aggregate_id": None,
                "ref_to": None,
                "ref_kind": "alarm",
                "ref_intent": "alarm",
                "ref_causal_id": None,
                "is_alarm": True,
                "alarm_body": body or "",
            }
            self.log(
                "alarm_registered",
                wake_id=wake_id,
                sender=sender,
                delay_seconds=delay,
            )
        return wake_id

    def check_watchdogs(self) -> None:
        with self.state_lock:
            if not self.watchdogs:
                return
            now = time.time()
            due = [(wake_id, wd) for wake_id, wd in list(self.watchdogs.items()) if now >= wd.get("deadline", now + 1)]
            for wake_id, wd in due:
                self.fire_watchdog(wake_id, wd)

    def stamp_turn_id_mismatch_post_watchdog_unblock(self, wd: dict) -> None:
        if wd.get("is_alarm"):
            return
        try:
            deadline = float(wd.get("deadline"))
        except (TypeError, ValueError):
            return
        unblock_ts = deadline + max(0.0, float(self.turn_id_mismatch_post_watchdog_grace_seconds))
        message_id = str(wd.get("ref_message_id") or "")
        aggregate_id = str(wd.get("ref_aggregate_id") or "")
        for context in self.current_prompt_by_agent.values():
            if not isinstance(context, dict) or context.get("turn_id_mismatch_since_ts") is None:
                continue
            matches_message = bool(message_id and context.get("id") == message_id)
            matches_aggregate = bool(aggregate_id and context.get("aggregate_id") == aggregate_id)
            if not matches_message and not matches_aggregate:
                continue
            try:
                existing = float(context.get("turn_id_mismatch_post_watchdog_unblock_ts") or 0.0)
            except (TypeError, ValueError):
                existing = 0.0
            if unblock_ts > existing:
                context["turn_id_mismatch_post_watchdog_unblock_ts"] = unblock_ts

    def build_watchdog_fire_text(self, wd: dict) -> str:
        if wd.get("is_alarm"):
            custom = str(wd.get("alarm_body") or "").strip()
            base = "[bridge:alarm] Scheduled wake-up elapsed."
            if custom:
                return f"{base} Note: {custom}"
            return base
        agg = wd.get("ref_aggregate_id")
        if agg:
            return (
                f"[bridge:watchdog] aggregate {agg} has not completed within its deadline. "
                f"{self._aggregate_watchdog_progress_text(agg, wd)} "
                "Inspect with agent_view_peer or stop a slow peer via agent_interrupt_peer."
            )
        kind = str(wd.get("ref_kind") or "request")
        to = str(wd.get("ref_to") or "(unknown)")
        msg_id = wd.get("ref_message_id") or "(unknown)"
        item = self._lookup_queue_item(str(msg_id)) if msg_id != "(unknown)" else None
        elapsed_text = self._watchdog_elapsed_text(item, wd)
        if not item:
            return (
                f"[bridge:watchdog] {kind} {msg_id} to {to}: queue item missing "
                "(stale entry). No action required."
            )
        status = str(item.get("status") or "")
        if status == "delivered":
            return (
                f"[bridge:watchdog] Your {kind} {msg_id} to {to} has been processing for "
                f"{elapsed_text} without a response. Choose ONE of:\n"
                f"  agent_extend_wait {msg_id} <sec>   (keep waiting on this same request)\n"
                f"  agent_interrupt_peer {to}          (cancel and stop the peer)\n"
                f"  agent_view_peer {to}               (inspect what the peer is doing)"
            )
        # Pending / inflight / submitted: not yet delivered to peer.
        return (
            f"[bridge:watchdog] Your {kind} {msg_id} to {to} has been queued for "
            f"{elapsed_text} and is NOT yet delivered (status={status}). The peer may be "
            "busy with an earlier prompt or in held_interrupt. "
            f"Inspect with agent_interrupt_peer {to} --status, or release a stuck hold "
            f"with agent_interrupt_peer {to} --clear-hold."
        )

    def _lookup_queue_item(self, message_id: str) -> dict | None:
        if not message_id:
            return None
        for item in self.queue.read():
            if item.get("id") == message_id:
                return dict(item)
        return None

    def _watchdog_elapsed_text(self, item: dict | None, wd: dict) -> str:
        ref_ts = None
        if item:
            ref_ts = item.get("delivered_ts") or item.get("created_ts")
        if not ref_ts:
            ref_ts = wd.get("ref_created_ts")
        if not ref_ts:
            return "an unknown duration"
        try:
            origin = datetime.fromisoformat(str(ref_ts).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            return "an unknown duration"
        delta = max(0.0, time.time() - origin)
        if delta < 60.0:
            return f"~{int(delta)}s"
        if delta < 3600.0:
            return f"~{int(delta / 60)}m{int(delta % 60)}s"
        return f"~{int(delta / 3600)}h{int((delta % 3600) / 60)}m"

    def _aggregate_watchdog_progress_text(self, aggregate_id: str, wd: dict) -> str:
        try:
            data = read_json(self.aggregate_file, {"aggregates": {}})
            agg = (data.get("aggregates") or {}).get(aggregate_id) or {}
            replies = agg.get("replies") or {}
            expected = agg.get("expected") or wd.get("ref_aggregate_expected") or []
        except Exception:
            replies = {}
            expected = wd.get("ref_aggregate_expected") or []
        got_count = len(replies)
        expected_count = len(expected)
        missing = sorted(set(expected) - set(replies.keys())) if expected else []
        elapsed = self._watchdog_elapsed_text(None, wd)
        text = f"Replied: {got_count}/{expected_count} after {elapsed}."
        if missing:
            text += f" Missing peers: {', '.join(missing)}."
        return text

    def fire_watchdog(self, wake_id: str, wd: dict) -> None:
        sender = str(wd.get("sender") or "")
        self.reload_participants()
        if not sender or sender not in self.participants:
            self.watchdogs.pop(wake_id, None)
            self.log(
                "watchdog_fire_skipped",
                wake_id=wake_id,
                sender=sender,
                reason="sender_not_active",
            )
            return
        # Stale skip: a non-aggregate, non-alarm watchdog whose target queue
        # item is gone is stale (already responded / cancelled / etc). Do not
        # wake the sender.
        if (
            not wd.get("is_alarm")
            and not wd.get("ref_aggregate_id")
            and wd.get("ref_message_id")
            and self._lookup_queue_item(str(wd.get("ref_message_id"))) is None
        ):
            self.watchdogs.pop(wake_id, None)
            self.log(
                "watchdog_skipped_stale",
                wake_id=wake_id,
                sender=sender,
                ref_message_id=wd.get("ref_message_id"),
            )
            return
        self.stamp_turn_id_mismatch_post_watchdog_unblock(wd)
        self.watchdogs.pop(wake_id, None)
        body = self.build_watchdog_fire_text(wd)
        synthetic = {
            "id": short_id("msg"),
            "created_ts": utc_now(),
            "updated_ts": utc_now(),
            "from": "bridge",
            "to": sender,
            "kind": "notice",
            "intent": "watchdog_wake",
            "body": body,
            "causal_id": wd.get("ref_causal_id") or short_id("causal"),
            "hop_count": 0,
            "auto_return": False,
            "reply_to": wd.get("ref_message_id"),
            "source": "watchdog_fire",
            "bridge_session": self.bridge_session,
            "status": "pending",
            "nonce": None,
            "delivery_attempts": 0,
        }
        self.queue_message(synthetic, log_event=True)
        self.log(
            "watchdog_fired",
            wake_id=wake_id,
            sender=sender,
            ref_message_id=wd.get("ref_message_id"),
            ref_aggregate_id=wd.get("ref_aggregate_id"),
            is_alarm=bool(wd.get("is_alarm")),
            synthetic_message_id=synthetic["id"],
        )

    def _cancel_active_messages_for_target(
        self,
        target: str,
        *,
        active_context: dict | None,
        reason: str,
        by_sender: str,
        cancel_statuses: set[str],
        notify_sources: bool,
    ) -> list[dict]:
        active_context = active_context or {}
        cancelled: list[dict] = []

        def cancel_mut(queue: list[dict]) -> None:
            kept = []
            for item in queue:
                if item.get("to") == target and item.get("status") in cancel_statuses:
                    cancelled.append(dict(item))
                    continue
                kept.append(item)
            queue[:] = kept

        self.queue.update(cancel_mut)

        for cm in cancelled:
            msg_id = cm.get("id")
            cm_nonce = cm.get("nonce")
            if cm_nonce:
                self.discard_nonce(str(cm_nonce))
            if msg_id:
                self.last_enter_ts.pop(str(msg_id), None)
            agg_id = cm.get("aggregate_id")
            if not agg_id and msg_id:
                self.cancel_watchdogs_for_message(str(msg_id), reason=reason)

        # Active context's message_id should normally be in `cancelled`
        # when cancelling delivered messages, but defend against state that
        # already lost the queue row.
        cancelled_ids = {cm.get("id") for cm in cancelled}
        act_id = active_context.get("id")
        if act_id and act_id not in cancelled_ids:
            if not active_context.get("aggregate_id"):
                self.cancel_watchdogs_for_message(str(act_id), reason=reason)

        for cm in cancelled:
            msg_id = cm.get("id")
            self.log(
                "delivered_message_cancelled",
                message_id=msg_id,
                from_agent=cm.get("from"),
                to=cm.get("to"),
                status=cm.get("status"),
                aggregate_id=cm.get("aggregate_id"),
                reason=reason,
                by_sender=by_sender,
            )

        if notify_sources:
            notified: set[str] = set()
            act_from = str(active_context.get("from") or "")
            if act_from and act_from != by_sender and act_from != "bridge" and act_from in self.participants:
                notified.add(act_from)
            for cm in cancelled:
                src = str(cm.get("from") or "")
                if src and src != by_sender and src != "bridge" and src in self.participants:
                    notified.add(src)
            for recipient in sorted(notified):
                notice = self._build_interrupt_notice(
                    recipient,
                    target,
                    by_sender,
                    cancelled,
                    active_context,
                    reason=reason,
                )
                self.queue_message(notice)

        for cm in cancelled:
            if cm.get("aggregate_id"):
                self._record_aggregate_interrupted_reply(cm, by_sender=by_sender, reason=reason)

        return cancelled

    def _prune_interrupted_turns(self, agent: str, now: float | None = None) -> None:
        now = time.time() if now is None else float(now)
        rows = list(self.interrupted_turns.get(agent) or [])
        kept: list[dict] = []
        for row in rows:
            try:
                interrupted_ts = float(row.get("interrupted_ts") or 0.0)
            except (TypeError, ValueError):
                interrupted_ts = 0.0
            if now - interrupted_ts > INTERRUPTED_TOMBSTONE_TTL_SECONDS:
                self.log(
                    "interrupted_tombstone_pruned",
                    target=agent,
                    reason="ttl",
                    message_id=row.get("message_id"),
                    turn_id=row.get("turn_id"),
                    nonce=row.get("nonce"),
                )
                continue
            kept.append(row)
        overflow = max(0, len(kept) - INTERRUPTED_TOMBSTONE_LIMIT_PER_AGENT)
        if overflow:
            for row in kept[:overflow]:
                self.log(
                    "interrupted_tombstone_pruned",
                    target=agent,
                    reason="cap",
                    message_id=row.get("message_id"),
                    turn_id=row.get("turn_id"),
                    nonce=row.get("nonce"),
                )
            kept = kept[overflow:]
        if kept:
            self.interrupted_turns[agent] = kept
        else:
            self.interrupted_turns.pop(agent, None)

    def _record_interrupted_turns(self, target: str, active_context: dict, cancelled: list[dict], by_sender: str) -> list[dict]:
        now = time.time()
        cancelled_ids = [str(cm.get("id") or "") for cm in cancelled if cm.get("id")]
        by_id = {str(cm.get("id") or ""): dict(cm) for cm in cancelled if cm.get("id")}
        active_id = str(active_context.get("id") or "")
        if active_id and active_id not in by_id:
            by_id[active_id] = {}
        recorded: list[dict] = []
        for message_id, row in by_id.items():
            if not message_id:
                continue
            is_active = bool(active_id and message_id == active_id)
            status = str(row.get("status") or "")
            prompt_submitted_seen = bool(is_active and active_context.get("id")) or status in {"delivered", "submitted"}
            tombstone = {
                "message_id": message_id,
                "turn_id": str(active_context.get("turn_id") or "") if is_active else "",
                "nonce": str(row.get("nonce") or active_context.get("nonce") or ""),
                "prior_sender": str(row.get("from") or active_context.get("from") or ""),
                "by_sender": by_sender,
                "cancelled_message_ids": cancelled_ids,
                "interrupted_ts": now,
                "prompt_submitted_seen": prompt_submitted_seen,
                "superseded_by_prompt": False,
            }
            recorded.append(tombstone)
        if not recorded and active_context.get("id"):
            recorded.append({
                "message_id": str(active_context.get("id") or ""),
                "turn_id": str(active_context.get("turn_id") or ""),
                "nonce": str(active_context.get("nonce") or ""),
                "prior_sender": str(active_context.get("from") or ""),
                "by_sender": by_sender,
                "cancelled_message_ids": cancelled_ids,
                "interrupted_ts": now,
                "prompt_submitted_seen": True,
                "superseded_by_prompt": False,
            })
        if recorded:
            self.interrupted_turns.setdefault(target, []).extend(recorded)
            self._prune_interrupted_turns(target, now)
            for row in recorded:
                self.log(
                    "interrupted_tombstone_recorded",
                    target=target,
                    message_id=row.get("message_id"),
                    turn_id=row.get("turn_id"),
                    nonce=row.get("nonce"),
                    prompt_submitted_seen=row.get("prompt_submitted_seen"),
                    by_sender=by_sender,
                )
        return recorded

    def _match_interrupted_prompt(self, agent: str, observed_nonce: object, record_turn_id: object) -> dict | None:
        self._prune_interrupted_turns(agent)
        nonce = str(observed_nonce or "")
        turn_id = str(record_turn_id or "")
        if not nonce and not turn_id:
            return None
        for row in self.interrupted_turns.get(agent) or []:
            row_nonce = str(row.get("nonce") or "")
            row_turn_id = str(row.get("turn_id") or "")
            if (nonce and row_nonce and nonce == row_nonce) or (turn_id and row_turn_id and turn_id == row_turn_id):
                return row
        return None

    def _supersede_interrupted_no_turn_suppression(self, agent: str, *, turn_id: object = None, message_id: object = None) -> None:
        rows = self.interrupted_turns.get(agent) or []
        changed = 0
        for row in rows:
            if not row.get("superseded_by_prompt"):
                row["superseded_by_prompt"] = True
                changed += 1
        if changed:
            self.log(
                "interrupted_tombstones_superseded_by_prompt",
                target=agent,
                count=changed,
                turn_id=turn_id,
                message_id=message_id,
            )

    def _pop_interrupted_tombstone(self, agent: str, row: dict) -> None:
        rows = list(self.interrupted_turns.get(agent) or [])
        rows = [item for item in rows if item is not row]
        if rows:
            self.interrupted_turns[agent] = rows
        else:
            self.interrupted_turns.pop(agent, None)

    def _match_interrupted_response(self, agent: str, response_turn_id: object, has_current_context: bool) -> tuple[dict | None, str]:
        self._prune_interrupted_turns(agent)
        rows = list(self.interrupted_turns.get(agent) or [])
        turn_id = str(response_turn_id or "")
        if turn_id:
            for row in rows:
                row_turn_id = str(row.get("turn_id") or "")
                if row_turn_id and row_turn_id == turn_id:
                    return row, "interrupted_drain"
            return None, ""
        if not rows:
            return None, ""
        if not has_current_context:
            for row in rows:
                if row.get("prompt_submitted_seen"):
                    return row, "interrupted_drain_no_context"
            return None, "interrupted_drain_no_context_unmatched"
        for row in rows:
            if row.get("prompt_submitted_seen") and not row.get("superseded_by_prompt"):
                return row, "interrupted_drain_no_turn_after_prompt_submit"
        return None, "interrupted_drain_ambiguous_no_turn"

    def handle_interrupt(self, sender: str, target: str) -> dict:
        # Interrupt semantics:
        #   1. ESC fail-closed: if tmux send-keys fails, no state mutation.
        #   2. Cancel (not requeue) the active in-flight message and any
        #      delivered/inflight/submitted messages for this target.
        #      Pending replacement messages are allowed to deliver as soon
        #      as ESC succeeds.
        #   3. Record interrupted-turn tombstones so identifiable late
        #      prompt_submitted / response_finished events from the
        #      cancelled turn are suppressed without blocking replacements.
        #   4. Aggregate-bearing cancelled messages get a synthetic
        #      "[interrupted]" reply recorded into the aggregate, so the
        #      aggregate can still complete from the remaining peers'
        #      replies (and so the aggregate watchdog isn't dropped).
        # IMPORTANT: pane resolve, ESC send, and state mutation all run
        # inside state_lock. Otherwise a prompt_submitted/response_finished
        # event could fire between ESC and the lock-protected mutation,
        # causing the interrupt to mis-cancel a turn it never targeted.
        # The ESC subprocess holds the lock for a few ms; that is the
        # v1 correctness/perf trade-off and is documented in the lock
        # ordering comment in __init__.
        self.reload_participants()
        with self.state_lock:
            endpoint_detail = self.resolve_endpoint_detail(target, purpose="write")
            pane = str(endpoint_detail.get("pane") or "") if endpoint_detail.get("ok") else ""
            if not pane:
                reason = str(endpoint_detail.get("reason") or "no_pane")
                finalized: list[dict] = []
                if endpoint_detail.get("should_detach") or endpoint_detail.get("probe_status") == "mismatch":
                    for item in list(self.queue.read()):
                        if item.get("to") == target and item.get("status") in {"delivered", "inflight", "submitted"}:
                            removed = self.finalize_undeliverable_message(item, endpoint_detail, phase="interrupt_endpoint_lost")
                            if removed:
                                finalized.append(removed)
                    self.busy[target] = False
                    self.reserved[target] = None
                self.log(
                    "esc_failed",
                    target=target,
                    by_sender=sender,
                    error=reason,
                    probe_status=endpoint_detail.get("probe_status"),
                    finalized_message_ids=[item.get("id") for item in finalized],
                )
                return {
                    "esc_sent": False,
                    "esc_error": reason,
                    "held": False,
                    "cancelled_message_ids": [item.get("id") for item in finalized],
                }
            if not self.dry_run:
                try:
                    subprocess.run(["tmux", "send-keys", "-t", pane, "Escape"], check=True)
                except Exception as exc:
                    self.log("esc_failed", target=target, by_sender=sender, error=str(exc))
                    return {"esc_sent": False, "esc_error": str(exc), "held": False, "cancelled_message_ids": []}

            active_context = self.current_prompt_by_agent.pop(target, {}) or {}
            cancelled = self._cancel_active_messages_for_target(
                target,
                active_context=active_context,
                reason="interrupted",
                by_sender=sender,
                cancel_statuses={"delivered", "inflight", "submitted"},
                notify_sources=True,
            )

            self.busy[target] = False
            self.reserved[target] = None
            tombstones = self._record_interrupted_turns(target, active_context, cancelled, sender)
            self.log(
                "interrupt_replacement_unblocked",
                target=target,
                by_sender=sender,
                prior_message_id=active_context.get("id"),
                prior_sender=active_context.get("from"),
                cancelled_count=len(cancelled),
                cancelled_message_ids=[cm.get("id") for cm in cancelled],
                tombstone_count=len(tombstones),
            )

        # Kick delivery to the interrupted target so queued corrections run
        # promptly. Other targets were not affected by this interrupt.
        self.try_deliver(target)
        return {
            "esc_sent": True,
            "esc_error": None,
            "cancelled_message_ids": [cm.get("id") for cm in cancelled],
            "prior_active_message_id": active_context.get("id"),
            "held": False,
        }

    def _build_interrupt_notice(
        self,
        recipient: str,
        target: str,
        by_sender: str,
        cancelled: list[dict],
        active_context: dict,
        *,
        reason: str,
    ) -> dict:
        cancelled_ids = ", ".join(str(cm.get("id") or "") for cm in cancelled if cm.get("id")) or "(none)"
        if reason == "prompt_intercepted":
            body = (
                f"[bridge:interrupted] Your active message to {target} was cancelled because "
                f"{target} started a new prompt before responding. The peer is processing "
                "that new prompt now; pending messages will continue to deliver normally. "
                f"Affected message_ids: {cancelled_ids}."
            )
        else:
            body = (
                f"[bridge:interrupted] Your active message to {target} was cancelled by "
                f"{by_sender or 'bridge'}. Pending messages to that peer may continue after "
                "ESC succeeds. Identifiable late output from the interrupted turn will be "
                f"ignored. Affected message_ids: {cancelled_ids}."
            )
        return {
            "id": short_id("msg"),
            "created_ts": utc_now(),
            "updated_ts": utc_now(),
            "from": "bridge",
            "to": recipient,
            "kind": "notice",
            "intent": "interrupt_notice",
            "body": body,
            "causal_id": short_id("causal"),
            "hop_count": 0,
            "auto_return": False,
            "reply_to": active_context.get("id"),
            "source": "interrupt_notice",
            "bridge_session": self.bridge_session,
            "status": "pending",
            "nonce": None,
            "delivery_attempts": 0,
        }

    def _record_aggregate_interrupted_reply(self, cancelled_msg: dict, by_sender: str, *, reason: str) -> None:
        # Inject a synthetic "[interrupted]" reply for this peer into the
        # aggregate's reply set so the aggregate can still progress (and
        # eventually complete or hit its watchdog) without depending on a
        # real Stop event from the interrupted peer.
        agg_id = cancelled_msg.get("aggregate_id")
        if not agg_id:
            return
        synthetic_context = {
            "aggregate_id": agg_id,
            "from": cancelled_msg.get("from"),
            "aggregate_expected": cancelled_msg.get("aggregate_expected"),
            "aggregate_message_ids": cancelled_msg.get("aggregate_message_ids"),
            "causal_id": cancelled_msg.get("causal_id"),
            "intent": cancelled_msg.get("intent"),
            "hop_count": cancelled_msg.get("hop_count"),
            "id": cancelled_msg.get("id"),
        }
        synthetic_sender = str(cancelled_msg.get("to") or "")
        if reason == "prompt_intercepted":
            synthetic_text = (
                "[intercepted by user prompt: peer accepted a new prompt before this aggregate request finished]"
            )
        elif reason == "endpoint_lost":
            synthetic_text = (
                "[bridge:undeliverable] peer endpoint was lost before this aggregate request could complete"
            )
        else:
            synthetic_text = (
                f"[interrupted by {by_sender or 'bridge'}: peer did not respond before being asked to stop]"
            )
        try:
            self.collect_aggregate_response(synthetic_sender, synthetic_text, synthetic_context)
        except Exception as exc:
            self.safe_log(
                "aggregate_interrupt_inject_failed",
                aggregate_id=agg_id,
                cancelled_message_id=cancelled_msg.get("id"),
                error=str(exc),
            )

    def release_hold(self, target: str, reason: str, by_sender: str | None = None) -> dict | None:
        with self.state_lock:
            info = self.held_interrupt.pop(target, None)
        if not info:
            return None
        hold_duration_ms = None
        since_ts = info.get("since_ts")
        if isinstance(since_ts, (int, float)):
            hold_duration_ms = int(max(0.0, time.time() - float(since_ts)) * 1000)
        event_name = "hold_force_resumed" if reason.startswith("manual_clear") else "hold_released"
        self.log(
            event_name,
            target=target,
            reason=reason,
            by_sender=by_sender,
            prior_message_id=info.get("prior_message_id"),
            hold_duration_ms=hold_duration_ms,
        )
        self.try_deliver(target)
        self.try_deliver()
        return info

    def cancel_watchdogs_for_message(self, message_id: str | None, reason: str = "reply_received") -> None:
        if not message_id:
            return
        for wake_id in list(self.watchdogs.keys()):
            wd = self.watchdogs.get(wake_id)
            if wd and wd.get("ref_message_id") == message_id and not wd.get("is_alarm"):
                self.watchdogs.pop(wake_id, None)
                self.log("watchdog_cancelled", wake_id=wake_id, reason=reason, ref_message_id=message_id)

    def cancel_watchdogs_for_aggregate(self, aggregate_id: str | None, reason: str = "aggregate_complete") -> None:
        if not aggregate_id:
            return
        for wake_id in list(self.watchdogs.keys()):
            wd = self.watchdogs.get(wake_id)
            if wd and wd.get("ref_aggregate_id") == aggregate_id and not wd.get("is_alarm"):
                self.watchdogs.pop(wake_id, None)
                self.log("watchdog_cancelled", wake_id=wake_id, reason=reason, ref_aggregate_id=aggregate_id)

    def turn_id_mismatch_expiry_deadline(self, context: dict) -> float | None:
        try:
            since_ts = float(context.get("turn_id_mismatch_since_ts"))
        except (TypeError, ValueError):
            return None
        deadline = since_ts + max(0.0, float(self.turn_id_mismatch_grace_seconds))
        try:
            post_watchdog_unblock_ts = float(context.get("turn_id_mismatch_post_watchdog_unblock_ts"))
        except (TypeError, ValueError):
            post_watchdog_unblock_ts = None
        if post_watchdog_unblock_ts is not None:
            deadline = max(deadline, post_watchdog_unblock_ts)
        message_id = str(context.get("id") or "")
        aggregate_id = str(context.get("aggregate_id") or "")
        post_watchdog_grace = max(0.0, float(self.turn_id_mismatch_post_watchdog_grace_seconds))
        for wd in self.watchdogs.values():
            if not wd or wd.get("is_alarm"):
                continue
            matches_message = bool(message_id and wd.get("ref_message_id") == message_id)
            matches_aggregate = bool(aggregate_id and wd.get("ref_aggregate_id") == aggregate_id)
            if not matches_message and not matches_aggregate:
                continue
            try:
                wd_deadline = float(wd.get("deadline"))
            except (TypeError, ValueError):
                continue
            deadline = max(deadline, wd_deadline + post_watchdog_grace)
        return deadline

    def expire_turn_id_mismatch_contexts(self) -> None:
        now = time.time()
        with self.state_lock:
            expired_targets = self._expire_turn_id_mismatch_contexts_locked(now)
        for target in expired_targets:
            self.try_deliver(target)
        if expired_targets:
            self.try_deliver()

    def _expire_turn_id_mismatch_contexts_locked(self, now: float) -> list[str]:
        ready: list[tuple[str, dict]] = []
        for target, context in list(self.current_prompt_by_agent.items()):
            if not isinstance(context, dict) or context.get("turn_id_mismatch_since_ts") is None:
                continue
            deadline = self.turn_id_mismatch_expiry_deadline(context)
            if deadline is None or now < deadline:
                continue
            ready.append((target, dict(context)))

        expired_targets: list[str] = []
        for target, context in ready:
            active_context = self.current_prompt_by_agent.get(target) or {}
            if (
                str(active_context.get("id") or "") != str(context.get("id") or "")
                or active_context.get("turn_id") != context.get("turn_id")
                or active_context.get("turn_id_mismatch_since_ts") != context.get("turn_id_mismatch_since_ts")
            ):
                continue

            message_id = str(context.get("id") or "")
            aggregate_id = str(context.get("aggregate_id") or "")
            removed_delivered = False
            if message_id:
                def mutator(queue: list[dict]) -> dict | None:
                    found = None
                    kept = []
                    for item in queue:
                        if (
                            item.get("id") == message_id
                            and item.get("to") == target
                            and item.get("status") == "delivered"
                        ):
                            found = dict(item)
                            continue
                        kept.append(item)
                    queue[:] = kept
                    return found

                removed_delivered = bool(self.queue.update(mutator))

            nonce = context.get("nonce")
            if nonce:
                self.discard_nonce(str(nonce))
            if message_id:
                self.last_enter_ts.pop(message_id, None)
                if not aggregate_id:
                    self.cancel_watchdogs_for_message(message_id, reason="turn_id_mismatch_expired")
            self.current_prompt_by_agent.pop(target, None)
            self.busy[target] = False
            self.reserved[target] = None
            expired_targets.append(target)
            since_ts = float(context.get("turn_id_mismatch_since_ts") or now)
            self.log(
                "turn_id_mismatch_context_expired",
                target=target,
                message_id=message_id,
                active_turn_id=context.get("turn_id"),
                mismatched_turn_id=context.get("turn_id_mismatch_response_turn_id"),
                mismatch_age_sec=round(max(0.0, now - since_ts), 3),
                removed_delivered=removed_delivered,
                aggregate_id=aggregate_id,
            )
        return expired_targets

    def requeue_stale_inflight(self) -> None:
        now = time.time()
        if now - self.last_maintenance < 2.0:
            return
        with self.state_lock:
            self._requeue_stale_inflight_locked(now)

    def _requeue_stale_inflight_locked(self, now: float) -> None:
        self.last_maintenance = now
        stale_targets: set[str] = set()

        def mutator(queue: list[dict]) -> list[dict]:
            stale = []
            for item in queue:
                if item.get("status") != "inflight":
                    continue
                if item.get("pane_mode_enter_deferred_since_ts"):
                    item["updated_ts"] = utc_now()
                    if item.get("last_error") not in {"pane_mode_probe_failed_waiting_enter"}:
                        item["last_error"] = "pane_in_mode_waiting_enter"
                    continue
                updated = item.get("updated_ts") or item.get("created_ts")
                try:
                    updated_ts = datetime.fromisoformat(str(updated).replace("Z", "+00:00")).timestamp()
                except ValueError:
                    updated_ts = 0.0
                if now - updated_ts < self.submit_timeout:
                    continue
                stale.append(dict(item))
                item["status"] = "pending"
                item["nonce"] = None
                item["updated_ts"] = utc_now()
                item["last_error"] = "prompt_submit_timeout"
            return stale

        for item in self.queue.update(mutator):
            target = str(item.get("to"))
            stale_targets.add(target)
            self.discard_nonce(str(item.get("nonce") or ""))
            if self.reserved.get(target) == item.get("id"):
                self.reserved[target] = None
            self.last_enter_ts.pop(str(item.get("id") or ""), None)
            self.log(
                "message_requeued",
                message_id=item.get("id"),
                to=target,
                reason="prompt_submit_timeout",
            )

        self.reload_participants()
        for target in stale_targets:
            self.try_deliver(target)

    def handle_prompt_submitted(self, record: dict) -> None:
        self.reload_participants()
        agent = self.participant_alias(record)
        if agent not in self.participants:
            return

        intercepted = False
        suppressed_interrupted_prompt = False
        message: dict = {}
        with self.state_lock:
            observed_nonce = record.get("nonce")
            record_turn_id = record.get("turn_id")
            interrupted_prompt = self._match_interrupted_prompt(agent, observed_nonce, record_turn_id)
            if interrupted_prompt:
                interrupted_prompt["prompt_submitted_seen"] = True
                suppressed_interrupted_prompt = True
                self.log(
                    "interrupted_prompt_submitted_suppressed",
                    agent=agent,
                    nonce=observed_nonce,
                    turn_id=record_turn_id,
                    interrupted_message_id=interrupted_prompt.get("message_id"),
                    superseded_by_prompt=interrupted_prompt.get("superseded_by_prompt"),
                )
            else:
                current_context = self.current_prompt_by_agent.get(agent, {}) or {}
                current_context_id = str(current_context.get("id") or "")
                current_context_turn_id = current_context.get("turn_id")
                duplicate_by_nonce = (
                    observed_nonce
                    and current_context.get("nonce")
                    and observed_nonce == current_context.get("nonce")
                    and record_turn_id == current_context_turn_id
                )
                duplicate_by_turn = False
                if current_context_id and record_turn_id is not None and record_turn_id == current_context_turn_id:
                    duplicate_by_turn = any(
                        item.get("id") == current_context_id
                        and item.get("to") == agent
                        and item.get("status") == "delivered"
                        for item in self.queue.read()
                    )
                if duplicate_by_nonce or duplicate_by_turn:
                    self.log(
                        "duplicate_prompt_submitted",
                        agent=agent,
                        nonce=observed_nonce,
                        turn_id=record_turn_id,
                        existing_message_id=current_context.get("id"),
                        duplicate_match="nonce_turn" if duplicate_by_nonce else "turn_delivered",
                    )
                    return

                # v1.5.2: daemon state is the authoritative identity. Find the
                # message the daemon believes was just delivered to this agent;
                # the hook-extracted nonce is treated as a hint that must
                # cross-check against the candidate's nonce. Anything that
                # doesn't match cleanly is fail-closed (no delivered mutation,
                # treated as a user-typed prompt for ctx purposes).
                candidate = self.find_inflight_candidate(agent)

                if candidate:
                    candidate_nonce = candidate.get("nonce")
                    candidate_id = str(candidate.get("id") or "")
                    if not observed_nonce:
                        # User typing collided with bridge inject (or hook
                        # payload arrived without the [bridge:nonce] prefix).
                        # Do NOT mark delivered. Leave the candidate in
                        # `inflight`; `requeue_stale_inflight` will revert it
                        # to `pending` after submit_timeout. Cancel the
                        # retry-enter loop now so the daemon doesn't spam
                        # `Enter` into a pane the human is typing in.
                        if candidate_id:
                            self.last_enter_ts.pop(candidate_id, None)
                        self.log(
                            "nonce_missing_for_candidate",
                            agent=agent,
                            candidate_message_id=candidate.get("id"),
                            candidate_nonce=candidate_nonce,
                        )
                    elif candidate_nonce and observed_nonce != candidate_nonce:
                        if candidate_id:
                            self.last_enter_ts.pop(candidate_id, None)
                        self.log(
                            "nonce_mismatch",
                            agent=agent,
                            observed_nonce=observed_nonce,
                            candidate_message_id=candidate.get("id"),
                            candidate_nonce=candidate_nonce,
                        )
                    else:
                        marked = self.mark_message_delivered_by_id(agent, str(candidate["id"]))
                        if marked:
                            message = marked
                        else:
                            # find_inflight_candidate already filtered by
                            # to=agent and status=inflight; reaching here means
                            # the queue moved between the two reads. Defensive
                            # log only.
                            if candidate_id:
                                self.last_enter_ts.pop(candidate_id, None)
                            self.log(
                                "delivery_recipient_mismatch",
                                agent=agent,
                                candidate_message_id=candidate.get("id"),
                                observed_nonce=observed_nonce,
                            )
                else:
                    # No daemon-side candidate. This is a user-typed prompt.
                    # An observed_nonce here means the user (or some quoted
                    # text) included a [bridge:...] prefix — log for diagnostics
                    # but do not bind the ctx to it.
                    if observed_nonce:
                        self.log(
                            "orphan_nonce_in_user_prompt",
                            agent=agent,
                            observed_nonce=observed_nonce,
                        )

                if message.get("id"):
                    self.last_enter_ts.pop(str(message["id"]), None)

                if not message.get("id"):
                    queue_now = list(self.queue.read())
                    delivered_rows = [
                        item for item in queue_now
                        if item.get("to") == agent and item.get("status") == "delivered"
                    ]
                    active_context = self.current_prompt_by_agent.get(agent, {}) or {}
                    if active_context.get("id") or delivered_rows:
                        active_context = self.current_prompt_by_agent.pop(agent, {}) or {}
                        cancelled = self._cancel_active_messages_for_target(
                            agent,
                            active_context=active_context,
                            reason="prompt_intercepted",
                            by_sender="bridge",
                            cancel_statuses={"delivered"},
                            notify_sources=True,
                        )
                        intercepted = True
                        self.log(
                            "active_prompt_intercepted",
                            agent=agent,
                            prior_message_id=active_context.get("id"),
                            turn_id=record_turn_id,
                            reason="prompt_intercepted",
                            cancelled_message_ids=[cm.get("id") for cm in cancelled],
                            cancelled_count=len(cancelled),
                            observed_nonce_present=bool(observed_nonce),
                            candidate_message_id=candidate.get("id") if candidate else None,
                        )

                self._supersede_interrupted_no_turn_suppression(
                    agent,
                    turn_id=record_turn_id,
                    message_id=message.get("id"),
                )
                self.busy[agent] = True
                self.reserved[agent] = None
                self.current_prompt_by_agent[agent] = {
                    "id": message.get("id"),
                    # ctx nonce comes from the matched message only; orphan
                    # nonces from user prompts must NOT be stored here, or
                    # discard_nonce at terminal time would clear unrelated
                    # cache entries.
                    "nonce": message.get("nonce"),
                    "causal_id": message.get("causal_id"),
                    "hop_count": int(message.get("hop_count") or 0),
                    "from": message.get("from"),
                    "kind": normalize_kind(message.get("kind"), "notice") if message else None,
                    "intent": message.get("intent"),
                    "auto_return": bool(message.get("auto_return")),
                    "aggregate_id": message.get("aggregate_id"),
                    "aggregate_expected": message.get("aggregate_expected"),
                    "aggregate_message_ids": message.get("aggregate_message_ids"),
                    "turn_id": record.get("turn_id"),
                }

        if suppressed_interrupted_prompt:
            self.try_deliver(agent)
            return
        if message.get("id"):
            self.log(
                "message_delivered",
                message_id=message.get("id"),
                to=agent,
                nonce=message.get("nonce"),
                from_agent=message.get("from"),
                kind=message.get("kind"),
                intent=message.get("intent"),
                causal_id=message.get("causal_id"),
                hop_count=message.get("hop_count"),
                auto_return=message.get("auto_return"),
                aggregate_id=message.get("aggregate_id"),
            )
        if intercepted:
            self.try_deliver()

    def response_fingerprint(self, record: dict) -> str:
        material = json.dumps(
            {
                "agent": record.get("agent"),
                "ts": record.get("ts"),
                "session_id": record.get("session_id"),
                "turn_id": record.get("turn_id"),
                "kind": "auto_return",
            },
            sort_keys=True,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def aggregate_expected_from_context(self, context: dict) -> list[str]:
        raw = context.get("aggregate_expected") or []
        if not isinstance(raw, list):
            return []
        expected = []
        for item in raw:
            alias = str(item or "")
            if alias and alias not in expected:
                expected.append(alias)
        return expected

    def aggregate_message_ids_from_context(self, context: dict) -> dict[str, str]:
        raw = context.get("aggregate_message_ids") or {}
        if not isinstance(raw, dict):
            return {}
        return {str(alias): str(message_id) for alias, message_id in raw.items() if alias and message_id}

    def merge_ordered_aliases(self, existing: list[str], incoming: list[str]) -> list[str]:
        merged = []
        for alias in [*existing, *incoming]:
            alias = str(alias or "")
            if alias and alias not in merged:
                merged.append(alias)
        return merged

    def aggregate_result_body(self, aggregate: dict) -> str:
        expected = [str(alias) for alias in aggregate.get("expected") or []]
        replies = aggregate.get("replies") or {}
        message_ids = aggregate.get("message_ids") or {}
        lines = [
            f"Aggregated result for broadcast {aggregate.get('id')} from {aggregate.get('requester')}.",
            f"causal_id={aggregate.get('causal_id')} expected={', '.join(expected)}",
            "",
        ]
        for alias in expected:
            reply = replies.get(alias) or {}
            original_id = message_ids.get(alias) or reply.get("reply_to") or ""
            lines.append(f"--- {alias} in_reply_to={original_id} ---")
            body = str(reply.get("body") or "").strip()
            lines.append(body or "(empty response)")
            lines.append("")
        return "\n".join(lines).rstrip()

    def collect_aggregate_response(self, sender: str, text: str, context: dict) -> None:
        aggregate_id = str(context.get("aggregate_id") or "")
        if not aggregate_id:
            return

        requester = str(context.get("from") or "")
        expected = self.aggregate_expected_from_context(context)
        message_ids = self.aggregate_message_ids_from_context(context)
        causal_id = str(context.get("causal_id") or short_id("causal"))
        original_intent = str(context.get("intent") or "message")
        hop_count = int(context.get("hop_count") or 0)
        reply_to = str(context.get("id") or message_ids.get(sender) or "")
        completed: dict | None = None

        def mutator(data: dict) -> dict | None:
            data.setdefault("version", 1)
            aggregates = data.setdefault("aggregates", {})
            aggregate = aggregates.setdefault(
                aggregate_id,
                {
                    "id": aggregate_id,
                    "created_ts": utc_now(),
                    "requester": requester,
                    "causal_id": causal_id,
                    "intent": original_intent,
                    "hop_count": hop_count,
                    "expected": expected,
                    "message_ids": message_ids,
                    "replies": {},
                    "status": "collecting",
                    "delivered": False,
                },
            )
            aggregate["updated_ts"] = utc_now()
            aggregate["requester"] = aggregate.get("requester") or requester
            aggregate["causal_id"] = aggregate.get("causal_id") or causal_id
            aggregate["intent"] = aggregate.get("intent") or original_intent
            aggregate["hop_count"] = int(aggregate.get("hop_count") or hop_count)
            aggregate["expected"] = self.merge_ordered_aliases(list(aggregate.get("expected") or []), expected)
            aggregate.setdefault("message_ids", {}).update(message_ids)
            replies = aggregate.setdefault("replies", {})
            replies[sender] = {
                "from": sender,
                "body": str(text),
                "reply_to": reply_to,
                "received_ts": utc_now(),
            }
            complete = bool(aggregate.get("expected")) and all(alias in replies for alias in aggregate.get("expected") or [])
            if complete and not aggregate.get("delivered"):
                aggregate["status"] = "complete"
                aggregate["delivered"] = True
                aggregate["delivered_at"] = utc_now()
                return dict(aggregate)
            return None

        with locked_json(self.aggregate_file, {"version": 1, "aggregates": {}}) as data:
            completed = mutator(data)

        expected_count = len(expected)
        received_count = 0
        if completed:
            received_count = len(completed.get("replies") or {})
        else:
            aggregate_data = read_json(self.aggregate_file, {"aggregates": {}})
            aggregate = (aggregate_data.get("aggregates") or {}).get(aggregate_id) or {}
            received_count = len(aggregate.get("replies") or {})
            expected_count = len(aggregate.get("expected") or expected)
        self.log(
            "aggregate_reply_collected",
            aggregate_id=aggregate_id,
            from_agent=sender,
            to=requester,
            reply_to=reply_to,
            causal_id=causal_id,
            received_count=received_count,
            expected_count=expected_count,
        )

        if not completed:
            return

        return_intent = f"{completed.get('intent') or original_intent}_aggregate_result"
        message = make_message(
            sender="bridge",
            target=str(completed.get("requester") or requester),
            intent=return_intent,
            body=self.aggregate_result_body(completed),
            causal_id=str(completed.get("causal_id") or causal_id),
            hop_count=int(completed.get("hop_count") or hop_count),
            auto_return=False,
            kind="result",
            reply_to=aggregate_id,
            source="aggregate_return",
        )
        message["aggregate_id"] = aggregate_id
        message["aggregate_expected"] = completed.get("expected") or expected
        self.queue_message(message)
        self.log(
            "aggregate_result_queued",
            message_id=message["id"],
            aggregate_id=aggregate_id,
            to=message["to"],
            kind="result",
            intent=return_intent,
            causal_id=message["causal_id"],
            expected_count=len(completed.get("expected") or []),
        )
        self.cancel_watchdogs_for_aggregate(aggregate_id, reason="aggregate_complete")

    def maybe_return_response(self, sender: str, text: str, context: dict) -> None:
        requester = context.get("from")
        self.reload_participants()
        if requester not in self.participants or requester == sender:
            return
        if not context.get("auto_return"):
            return

        if context.get("aggregate_id"):
            self.collect_aggregate_response(sender, text, context)
            return

        causal_id = context.get("causal_id") or short_id("causal")
        hop_count = int(context.get("hop_count") or 0)
        original_intent = context.get("intent") or "message"
        return_intent = f"{original_intent}_result"
        response_text = text if text.strip() else EMPTY_RESPONSE_BODY
        body = (
            f"Result from {sender}:\n"
            f"{response_text}"
        )
        message = make_message(
            sender=sender,
            target=str(requester),
            intent=return_intent,
            body=body,
            causal_id=causal_id,
            hop_count=hop_count,
            auto_return=False,
            kind="result",
            reply_to=context.get("id"),
            source="auto_return",
        )
        self.queue_message(message)
        self.log(
            "response_return_queued",
            message_id=message["id"],
            from_agent=sender,
            to=requester,
            kind="result",
            intent=return_intent,
            causal_id=causal_id,
            hop_count=hop_count,
        )
        self.cancel_watchdogs_for_message(context.get("id"), reason="reply_received")

    def handle_response_finished(self, record: dict) -> None:
        self.reload_participants()
        sender = self.participant_alias(record)
        if sender not in self.participants:
            return

        # IMPORTANT: stale judgement runs BEFORE any state mutation. If the
        # response is stale, we must not flip busy/reserved/current_prompt
        # nor trigger try_deliver against an unrelated active turn — doing
        # so could cause the bridge to inject a fresh prompt on top of a
        # peer that is still mid-turn.
        with self.state_lock:
            text = record.get("last_assistant_message") or ""
            context = self.current_prompt_by_agent.get(sender) or {}
            response_turn_id = record.get("turn_id")
            context_turn_id = context.get("turn_id") if context else None
            held_info = self.held_interrupt.get(sender)
            in_held = held_info is not None
            turn_id_mismatch = (
                response_turn_id is not None
                and context_turn_id is not None
                and response_turn_id != context_turn_id
            )
            fingerprint = self.response_fingerprint(record)
            first_time = self.processed_returns.add(fingerprint)

            interrupted_tombstone, interrupted_reason = self._match_interrupted_response(
                sender,
                response_turn_id,
                has_current_context=bool(context),
            )
            if interrupted_tombstone:
                self._pop_interrupted_tombstone(sender, interrupted_tombstone)
                if first_time:
                    self.log(
                        "response_skipped_stale",
                        agent=sender,
                        reason=interrupted_reason,
                        response_turn_id=response_turn_id,
                        active_turn_id=context_turn_id,
                        active_message_id=context.get("id"),
                        interrupted_message_id=interrupted_tombstone.get("message_id"),
                        interrupted_turn_id=interrupted_tombstone.get("turn_id"),
                        interrupted_nonce=interrupted_tombstone.get("nonce"),
                    )
                return
            if interrupted_reason and first_time:
                self.log(
                    "interrupted_response_ambiguous",
                    agent=sender,
                    reason=interrupted_reason,
                    response_turn_id=response_turn_id,
                    active_turn_id=context_turn_id,
                    active_message_id=context.get("id"),
                    tombstone_message_ids=[
                        row.get("message_id") for row in (self.interrupted_turns.get(sender) or [])
                    ],
                )

            if in_held:
                # The held-target interrupt is being drained: this Stop
                # event confirms the peer aborted the interrupted turn
                # and is now idle. Release hold but do NOT route the
                # (possibly partial) text. If a new prompt_submitted
                # arrived while held, a late Stop for the old turn must
                # release the hold without clobbering the new active ctx.
                held_drain_matches_current = (
                    response_turn_id == context_turn_id
                    or (not context.get("id") and not context_turn_id)
                )
                if first_time:
                    if held_drain_matches_current:
                        self.log(
                            "response_skipped_stale",
                            agent=sender,
                            reason="held_drain",
                            response_turn_id=response_turn_id,
                            active_turn_id=context_turn_id,
                            prior_message_id=held_info.get("prior_message_id"),
                        )
                    else:
                        self.log(
                            "held_drain_stale_stop",
                            agent=sender,
                            response_turn_id=response_turn_id,
                            active_turn_id=context_turn_id,
                            active_message_id=context.get("id"),
                            prior_message_id=held_info.get("prior_message_id"),
                        )
                self.held_interrupt.pop(sender, None)
                hold_duration_ms = None
                since_ts = held_info.get("since_ts")
                if isinstance(since_ts, (int, float)):
                    hold_duration_ms = int(max(0.0, time.time() - float(since_ts)) * 1000)
                self.log(
                    "hold_released",
                    target=sender,
                    reason="stop_event_received",
                    prior_message_id=held_info.get("prior_message_id"),
                    hold_duration_ms=hold_duration_ms,
                )
                if held_drain_matches_current:
                    self.busy[sender] = False
                    self.reserved[sender] = None
                    self.current_prompt_by_agent.pop(sender, None)
                    self.try_deliver(sender)
                self.try_deliver()
                return

            if turn_id_mismatch:
                # A late Stop event for an old turn arrived while the peer
                # is processing a new turn. Skip routing now; a maintenance
                # sweep expires the active context later if the matching Stop
                # never arrives. The first mismatch pins the grace clock so a
                # stream of stale Stops cannot keep the target busy forever.
                now_ts = time.time()
                if context.get("turn_id_mismatch_since_ts") is None:
                    context["turn_id_mismatch_since_ts"] = now_ts
                context["turn_id_mismatch_last_ts"] = now_ts
                context["turn_id_mismatch_response_turn_id"] = response_turn_id
                context["turn_id_mismatch_count"] = int(context.get("turn_id_mismatch_count") or 0) + 1
                if first_time:
                    self.log(
                        "response_skipped_stale",
                        agent=sender,
                        reason="turn_id_mismatch",
                        response_turn_id=response_turn_id,
                        active_turn_id=context_turn_id,
                        active_message_id=context.get("id"),
                    )
                return

            # Normal terminal path: route (if applicable) and remove the
            # delivered message from queue regardless of whether routing
            # actually delivered text (notices, empty responses, inactive
            # requesters all still terminate the message). Watchdog cancel
            # also happens here, decoupled from routing success — otherwise
            # a request whose response was empty / had no active requester
            # would leave its watchdog alive and fire a bogus wake later.
            if first_time:
                self.maybe_return_response(sender, text, context)
            msg_id = context.get("id")
            if msg_id:
                self.remove_delivered_message(sender, msg_id)
                self.last_enter_ts.pop(str(msg_id), None)
                # Aggregate watchdogs are cancelled inside collect_aggregate_response
                # when the aggregate completes; only cancel the per-message
                # watchdog here for non-aggregate messages.
                if not context.get("aggregate_id"):
                    self.cancel_watchdogs_for_message(str(msg_id), reason="terminal_response")
            ctx_nonce = context.get("nonce")
            if ctx_nonce:
                self.discard_nonce(str(ctx_nonce))
            self.busy[sender] = False
            self.reserved[sender] = None
            # v1.5.2 consume-once: a peer request gets exactly one
            # auto-routed reply. Subsequent response_finished events
            # without a fresh prompt_submitted (e.g., system reminders
            # that don't fire UPS hooks) must NOT re-route to the peer.
            # Held-drain skips this pop. turn_id_mismatch keeps ctx only
            # until its bounded maintenance expiry, unless the matching Stop
            # arrives first and reaches this normal terminal path.
            self.current_prompt_by_agent.pop(sender, None)
        self.try_deliver(sender)
        self.try_deliver()

    def handle_external_message_queued(self, record: dict) -> None:
        # File-fallback ingress: bridge_enqueue.py wrote the message
        # directly to queue.json + events.raw.jsonl because the daemon
        # socket was not reachable from the caller's sandbox (e.g. codex).
        # The daemon's main follow loop dispatches us when it sees the
        # message_queued event. Always finalize the queue item via the
        # shared helper so that:
        #   - status is promoted ingressing -> pending (regardless of
        #     sender, including bridge-origin synthetic messages that
        #     happened to take the fallback path), and
        #   - alarm cancel + body prepend runs (the helper itself is
        #     gated on sender != "bridge" via _maybe_cancel_alarms_for_incoming).
        # The helper is idempotent (status==ingressing gate), so replay
        # of the same event cannot re-cancel a fresh alarm.
        target = record.get("to")
        self.reload_participants()
        with self.state_lock:
            msg_id = str(record.get("message_id") or "")
            if msg_id:
                self._apply_alarm_cancel_to_queued_message(msg_id)
            if target in self.participants:
                self.try_deliver(str(target))

    def _apply_alarm_cancel_to_queued_message(self, message_id: str) -> None:
        # Single source of truth for finalizing ingestion of a message
        # placed into queue.json by either ingress path:
        #   1. Daemon-socket ingress (enqueue_ipc_message): bridge_enqueue
        #      sent the message via the unix socket; daemon appended it
        #      to queue.json with status="ingressing".
        #   2. File-fallback ingress: bridge_enqueue.py wrote queue.json
        #      directly because its sandbox could not connect to the
        #      socket. Daemon dispatches handle_external_message_queued
        #      which calls this.
        # Steps performed (atomic under state_lock that the caller holds):
        #   a) Run alarm-cancel-on-incoming, which may prepend a notice
        #      to the body in place if any alarms qualify (gated on
        #      sender != bridge inside the helper).
        #   b) Promote status from "ingressing" to "pending" so
        #      reserve_next can pick the message up.
        # Idempotent: gated by `status == "ingressing"`. The same event
        # may be observed multiple times (socket path enqueues then
        # daemon's tail loop replays the message_queued record; or
        # fallback writes both queue and event and the event later
        # gets duplicated). After the first finalize the status is
        # promoted to "pending" and any subsequent invocation no-ops,
        # so a newer alarm registered between replays cannot be
        # accidentally cancelled by a stale message.
        if not message_id:
            return
        snapshot = list(self.queue.read())
        item = next((it for it in snapshot if it.get("id") == message_id), None)
        if not item:
            return
        if item.get("status") != "ingressing":
            return
        original_body = item.get("body")
        self._maybe_cancel_alarms_for_incoming(item)
        body_changed = item.get("body") != original_body
        new_body = item.get("body") if body_changed else None

        def update_mut(queue: list[dict]) -> None:
            for q in queue:
                if q.get("id") == message_id:
                    if body_changed:
                        q["body"] = new_body
                    q["status"] = "pending"
                    q["updated_ts"] = utc_now()
                    return

        self.queue.update(update_mut)

    def _recover_ingressing_messages(self) -> None:
        # Startup recovery: any queue items left in transient "ingressing"
        # state were written by bridge_enqueue.py's file-fallback path
        # while the daemon was down (or were left over by a daemon crash
        # between queue insert and finalize). The corresponding alarms
        # were daemon-memory only and have been lost across the restart,
        # so just promote these to "pending" and unblock delivery; log
        # the recovery for operator visibility.
        recovered: list[str] = []

        def mutator(queue: list[dict]) -> None:
            for item in queue:
                if item.get("status") == "ingressing":
                    item["status"] = "pending"
                    item["updated_ts"] = utc_now()
                    item["last_error"] = "ingressing_recovered_after_daemon_restart"
                    recovered.append(str(item.get("id") or ""))

        self.queue.update(mutator)
        for msg_id in recovered:
            self.log("ingressing_recovered", message_id=msg_id)

    def _recover_orphan_delivered_messages(self) -> None:
        # Startup recovery: messages with status="delivered" depend on the
        # daemon's in-memory current_prompt_by_agent ctx to terminal-cleanup
        # (remove from queue) when the recipient's response_finished fires.
        # Across a daemon restart that ctx is gone, so any pre-restart
        # delivered item becomes an orphan: it stays in the queue forever
        # AND blocks reserve_next from injecting newer messages to the same
        # target (delivered counts as a delivery blocker).
        #
        # Recovery policy: drop these from the queue. The recipient's
        # response_finished, if it arrives, will be treated as a
        # user-context turn (no auto-route) per consume-once semantics;
        # that loss of auto-route is part of I-03's accepted restart trade-
        # off. Aggregate participants are also dropped — the aggregate's
        # synthesized result message (if any) is in the queue separately
        # and goes through its own recovery path.
        recovered: list[dict] = []

        def mutator(queue: list[dict]) -> list[dict]:
            kept = []
            for item in queue:
                if item.get("status") == "delivered":
                    recovered.append(dict(item))
                    continue
                kept.append(item)
            queue[:] = kept
            return recovered

        self.queue.update(mutator)
        for item in recovered:
            self.log(
                "delivered_orphan_recovered",
                message_id=item.get("id"),
                to=item.get("to"),
                from_agent=item.get("from"),
                kind=item.get("kind"),
                aggregate_id=item.get("aggregate_id"),
                reason="daemon_restart_lost_routing_ctx",
            )

    # Maintenance constants for aged-ingressing promotion. The threshold
    # is generous enough to absorb normal daemon jitter (event read +
    # finalize) but short enough that a stalled item is unblocked
    # without operator intervention.
    INGRESSING_AGE_PROMOTE_SEC = 30.0
    INGRESSING_CHECK_INTERVAL_SEC = 5.0

    def _promote_aged_ingressing(self) -> None:
        # Running-daemon safety net: if for any reason the daemon failed
        # to process the message_queued event for an "ingressing" queue
        # item (event append failure on the writer's side, follow-loop
        # de-sync, etc.), the item would otherwise wait until the next
        # daemon restart's _recover_ingressing_messages. Promote it now
        # so delivery can resume.
        #
        # We only promote (no alarm cancel + body prepend), because by
        # this point we cannot reliably reconstruct what alarms were
        # active at the original ingest moment. Operators see the
        # `ingressing_promoted_aged` event and can investigate.
        #
        # Held entirely under state_lock to match the lock-discipline
        # rule documented in __init__: state-mutating maintenance runs
        # acquire state_lock before touching the queue file lock. The
        # throttle check is also performed under the lock so concurrent
        # callers cannot both pass the gate and run the maintenance
        # twice in a row.
        with self.state_lock:
            now = time.time()
            if now - self.last_ingressing_check < self.INGRESSING_CHECK_INTERVAL_SEC:
                return
            self.last_ingressing_check = now
            threshold = now - self.INGRESSING_AGE_PROMOTE_SEC
            promoted: list[tuple[str, float, bool]] = []

            def mutator(queue: list[dict]) -> None:
                for item in queue:
                    if item.get("status") != "ingressing":
                        continue
                    created = item.get("created_ts") or item.get("updated_ts")
                    age_unknown = False
                    try:
                        ts = datetime.fromisoformat(str(created).replace("Z", "+00:00")).timestamp()
                    except (TypeError, ValueError):
                        ts = 0.0
                        age_unknown = True
                    if not age_unknown and ts >= threshold:
                        continue
                    age_sec = max(0.0, now - ts) if not age_unknown else 0.0
                    item["status"] = "pending"
                    item["updated_ts"] = utc_now()
                    item["last_error"] = "ingressing_promoted_aged"
                    promoted.append((str(item.get("id") or ""), age_sec, age_unknown))

            self.queue.update(mutator)
            for msg_id, age_sec, age_unknown in promoted:
                fields: dict = {
                    "message_id": msg_id,
                    "threshold_sec": self.INGRESSING_AGE_PROMOTE_SEC,
                }
                if age_unknown:
                    fields["age_unknown"] = True
                else:
                    fields["age_sec"] = round(age_sec, 3)
                self.log("ingressing_promoted_aged", **fields)

    def record_age_seconds(self, record: dict) -> float | None:
        raw = str(record.get("ts") or "")
        if not raw:
            return None
        try:
            stamp = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
            return max(0.0, time.time() - datetime.fromisoformat(stamp).timestamp())
        except (TypeError, ValueError):
            return None

    def cleanup_capture_responses(self, *, force: bool = False) -> None:
        now = time.time()
        if not force and now - self.last_capture_cleanup < 60.0:
            return
        self.last_capture_cleanup = now
        root = self.state_file.parent / "captures" / "responses"
        if not root.exists():
            return
        for path in root.glob("*.json"):
            try:
                if now - path.stat().st_mtime > CAPTURE_RESPONSE_TTL_SECONDS:
                    path.unlink(missing_ok=True)
            except OSError:
                continue

    def safe_response_file(self, raw: object) -> Path | None:
        if not raw:
            return None
        try:
            path = Path(str(raw)).resolve()
            allowed = (self.state_file.parent / "captures" / "responses").resolve()
            path.relative_to(allowed)
            return path
        except (OSError, ValueError):
            return None

    def write_capture_response(self, response_file: Path | None, payload: dict) -> None:
        if response_file is None:
            return
        try:
            response_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = response_file.with_suffix(response_file.suffix + f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=True) + "\n", encoding="utf-8")
            os.replace(tmp, response_file)
        except OSError as exc:
            self.safe_log("capture_response_write_failed", response_file=str(response_file), error=str(exc))

    def handle_capture_request(self, record: dict) -> None:
        request_id = str(record.get("request_id") or "")
        requester = str(record.get("from_agent") or "")
        target = str(record.get("target") or "")
        response_file = self.safe_response_file(record.get("response_file"))
        self.reload_participants()

        def fail(error: str) -> None:
            self.write_capture_response(response_file, {"ok": False, "request_id": request_id, "error": error})
            self.safe_log("capture_failed", request_id=request_id, from_agent=requester, target=target, error=error)

        if not request_id:
            fail("missing request_id")
            return
        if not self.processed_capture_requests.add(request_id):
            self.safe_log("capture_skipped", request_id=request_id, from_agent=requester, target=target, reason="duplicate_request")
            return
        age = self.record_age_seconds(record)
        if age is not None and age > MAX_CAPTURE_REQUEST_AGE_SECONDS:
            self.safe_log("capture_skipped", request_id=request_id, from_agent=requester, target=target, reason="stale_request", age_seconds=round(age, 3))
            return
        if requester not in self.participants:
            fail(f"requester {requester!r} is not an active participant")
            return
        participant = self.participants.get(target)
        if not participant:
            fail(f"target {target!r} is not an active participant")
            return
        endpoint_detail = self.resolve_endpoint_detail(target, purpose="read")
        endpoint = str(endpoint_detail.get("pane") or "") if endpoint_detail.get("ok") else ""
        if not endpoint:
            fail(f"target {target!r} has no verified live pane ({endpoint_detail.get('reason')})")
            return

        try:
            start = int(record.get("start") or -1000)
        except (TypeError, ValueError):
            start = -1000
        start = max(-200000, min(0, start))
        raw_end = record.get("end")
        end: int | str | None
        if raw_end in {None, "", "-"}:
            end = None
        else:
            try:
                end = int(raw_end)
            except (TypeError, ValueError):
                end = str(raw_end)
        raw = bool(record.get("raw"))

        try:
            text = run_tmux_capture(endpoint, start, end, raw)
        except Exception as exc:
            fail(str(exc))
            return

        self.write_capture_response(
            response_file,
            {
                "ok": True,
                "request_id": request_id,
                "target": target,
                "pane": endpoint,
                "text": text,
                "captured_at": utc_now(),
            },
        )
        self.log(
            "capture_completed",
            request_id=request_id,
            from_agent=requester,
            target=target,
            pane=endpoint,
            text_chars=len(text),
        )

    def handle_record(self, record: dict) -> None:
        try:
            if self.bridge_session:
                record_session = record.get("bridge_session")
                if record.get("agent") in PHYSICAL_AGENT_TYPES and record_session != self.bridge_session:
                    return
                if record.get("event") == "message_queued" and record_session and record_session != self.bridge_session:
                    return
                if record.get("event") == "capture_request" and record_session and record_session != self.bridge_session:
                    return

            event = record.get("event")
            if event == "message_queued":
                self.handle_external_message_queued(record)
                return
            if event == "capture_request":
                self.handle_capture_request(record)
                return
            if event == "prompt_submitted":
                self.handle_prompt_submitted(record)
                return
            if event == "response_finished":
                self.handle_response_finished(record)
        except Exception as exc:
            self.safe_log(
                "record_handler_failed",
                handled_event=record.get("event"),
                record_agent=record.get("agent"),
                error=repr(exc),
            )

    def follow(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.touch(exist_ok=True)
        self.start_command_server()
        self.cleanup_capture_responses(force=True)

        try:
            with self.state_file.open("r", encoding="utf-8") as stream:
                if not self.from_start:
                    stream.seek(0, os.SEEK_END)

                self.log(
                    "daemon_started",
                    participants=sorted(self.participants),
                    panes=self.panes,
                    bridge_session=self.bridge_session,
                    max_hops=self.max_hops,
                    dry_run=self.dry_run,
                    command_socket=str(self.command_socket) if self.command_socket else "",
                    pane_mode_grace_seconds=self.pane_mode_grace_seconds,
                    turn_id_mismatch_grace_seconds=self.turn_id_mismatch_grace_seconds,
                    turn_id_mismatch_post_watchdog_grace_seconds=self.turn_id_mismatch_post_watchdog_grace_seconds,
                )
                if self.pane_mode_grace_warning:
                    self.log("pane_mode_grace_config_warning", warning=self.pane_mode_grace_warning)
                for warning in self.turn_id_mismatch_grace_warnings:
                    self.log("turn_id_mismatch_grace_config_warning", warning=warning)
                if self.startup_backfill_summary:
                    unknown = [
                        alias for alias, item in self.startup_backfill_summary.items()
                        if isinstance(item, dict) and item.get("status") == "unknown"
                    ]
                    mismatch = [
                        alias for alias, item in self.startup_backfill_summary.items()
                        if isinstance(item, dict) and item.get("status") == "mismatch"
                    ]
                    self.log(
                        "endpoint_backfill_summary",
                        statuses=self.startup_backfill_summary,
                        repair_hint=(
                            "run bin/bridge_healthcheck.sh --backfill-endpoints from a host tmux shell with /proc access; "
                            "reattach/join with normal probing if no verified prior live endpoint exists"
                            if unknown or mismatch else ""
                        ),
                    )

                # Recover any messages left in the transient "ingressing"
                # state by a previous daemon crash or by file-fallback
                # writes that did not get a finalize visit before we
                # restarted. Their alarms (in-memory only) are gone, so
                # just promote them to "pending" and let delivery proceed.
                with self.state_lock:
                    self._recover_ingressing_messages()
                    self._recover_orphan_delivered_messages()

                self.try_deliver()

                while True:
                    if self.stop_requested():
                        break
                    self.requeue_stale_inflight()
                    self.retry_enter_for_inflight()
                    self.check_watchdogs()
                    self.expire_turn_id_mismatch_contexts()
                    self._promote_aged_ingressing()
                    # Periodic delivery wake (throttled): hold release,
                    # watchdog fires, requeues, etc. can leave pending
                    # work that no incoming event nudges. Without this
                    # tick the queue could stall indefinitely.
                    now = time.time()
                    if now - self.last_delivery_tick >= 0.5:
                        self.last_delivery_tick = now
                        self.try_deliver()
                    self.cleanup_capture_responses()
                    if self.stop_requested():
                        break
                    line = stream.readline()
                    if not line:
                        if self.once:
                            break
                        time.sleep(0.25)
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    self.handle_record(record)
                    if self.stop_requested():
                        break

                self.log("daemon_stopped")
        finally:
            self.stop_command_server()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--claude-pane")
    parser.add_argument("--codex-pane")
    parser.add_argument("--state-file", default=str(state_root() / "events.jsonl"))
    parser.add_argument("--public-state-file")
    parser.add_argument("--queue-file", default=str(state_root() / "pending.json"))
    parser.add_argument("--max-hops", type=int, default=4)
    parser.add_argument("--submit-delay", type=float, default=1.0)
    parser.add_argument("--submit-timeout", type=float, default=20.0)
    parser.add_argument("--from-start", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--bridge-session")
    parser.add_argument("--session-file")
    parser.add_argument("--stop-file")
    parser.add_argument("--command-socket")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--stdout-events", action="store_true", help="also print daemon event JSON to stdout")
    args = parser.parse_args()

    install_signal_handlers()
    daemon = BridgeDaemon(args)
    daemon.follow()
    if _STOP_SIGNAL is not None:
        return 128 + int(_STOP_SIGNAL)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.exit(130)
