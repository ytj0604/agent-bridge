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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from bridge_identity import resolve_participant_endpoint
from bridge_participants import active_participants, participant_record
from bridge_paths import model_bin_dir, state_root
from bridge_util import append_jsonl, locked_json, normalize_kind, public_record, read_json, utc_now


PHYSICAL_AGENT_TYPES = {"claude", "codex"}
MAX_BODY_CHARS = 12000
MAX_PROCESSED_RETURNS = 4096
MAX_NONCE_CACHE = 1024
_STOP_SIGNAL: int | None = None


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


def prompt_body(text: str) -> str:
    raw = str(text)
    if len(raw) > MAX_BODY_CHARS:
        raw = raw[:MAX_BODY_CHARS] + "\n[bridge truncated peer body]"
    return (
        raw.replace("\\", "\\\\")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )


def kind_expects_response(kind: str) -> bool:
    return kind == "request"


def run_tmux_send(target: str, prompt: str, submit_delay: float) -> None:
    subprocess.run(["tmux", "send-keys", "-t", target, "-l", prompt], check=True)
    time.sleep(submit_delay)
    subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], check=True)


def build_peer_prompt(message: dict, nonce: str, max_hops: int) -> str:
    sender = str(message["from"])
    kind = normalize_kind(message.get("kind"), "request")
    if kind == "request":
        label = "Request"
        hint = "Reply normally; bridge returns it."
    elif kind == "result":
        label = "Result"
        hint = "Use locally; do not reply to peer."
    else:
        label = "Notice"
        hint = "FYI; no reply needed."

    prefix = one_line(
        f"[bridge:{nonce}] from={sender} kind={kind}. {hint}"
    )
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
        "id": f"msg-{uuid.uuid4().hex}",
        "created_ts": utc_now(),
        "updated_ts": utc_now(),
        "from": sender,
        "to": target,
        "kind": kind,
        "intent": intent,
        "body": str(body),
        "causal_id": causal_id or f"causal-{uuid.uuid4().hex[:12]}",
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
        self.args = args
        self.session_file = Path(args.session_file) if args.session_file else Path(args.queue_file).parent / "session.json"
        self.session_state: dict = {}
        self.participants: dict[str, dict] = {}
        self.panes: dict[str, str] = {}
        self.session_mtime_ns: int | None = None
        self.max_hops = args.max_hops
        self.submit_delay = args.submit_delay
        self.submit_timeout = args.submit_timeout
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
        self.last_maintenance = 0.0
        self.stop_logged = False
        self.reload_participants()

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

        if not isinstance(request, dict) or request.get("op") != "enqueue":
            return {"ok": False, "error": "unsupported command"}
        messages = request.get("messages")
        if not isinstance(messages, list):
            return {"ok": False, "error": "messages must be a list"}
        ids = []
        self.reload_participants()
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
                message["id"] = f"msg-{uuid.uuid4().hex}"
            message["bridge_session"] = self.bridge_session
            self.enqueue_ipc_message(message)
            ids.append(message["id"])
        return {"ok": True, "ids": ids}

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
        def mutator(queue: list[dict]) -> None:
            if not any(item.get("id") == message["id"] for item in queue):
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
            source=message.get("source") or "ipc_enqueue",
            body=message.get("body"),
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
                source=message.get("source"),
            )
        self.try_deliver(str(message["to"]))

    def reserve_next(self, target: str) -> dict | None:
        if self.busy.get(target) or self.reserved.get(target):
            return None

        def mutator(queue: list[dict]) -> dict | None:
            if any(item.get("to") == target and item.get("status") == "inflight" for item in queue):
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
        targets = [target] if target else sorted(self.participants)
        for agent in targets:
            if agent not in self.participants:
                continue
            message = self.reserve_next(agent)
            if not message:
                continue
            self.deliver_reserved(message)

    def deliver_reserved(self, message: dict) -> None:
        target = str(message["to"])
        nonce = str(message["nonce"])
        participant = self.participants.get(target)
        endpoint = resolve_participant_endpoint(self.bridge_session or "", target, participant or {}) if participant else ""
        if endpoint:
            self.panes[target] = endpoint
        if target not in self.panes or not self.panes.get(target):
            self.mark_message_pending(str(message["id"]), "unknown_target")
            self.log("message_delivery_failed", message_id=message["id"], to=target, nonce=nonce, error="unknown_target")
            return
        self.reserved[target] = str(message["id"])
        self.remember_nonce(nonce, message)
        prompt = build_peer_prompt(message, nonce, self.max_hops)

        try:
            if not self.dry_run:
                run_tmux_send(self.panes[target], prompt, self.submit_delay)
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
            dry_run=self.dry_run,
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

        self.queue.update(mutator)

    def ack_message(self, nonce: str) -> dict | None:
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

    def requeue_stale_inflight(self) -> None:
        now = time.time()
        if now - self.last_maintenance < 2.0:
            return
        self.last_maintenance = now

        stale_targets: set[str] = set()

        def mutator(queue: list[dict]) -> list[dict]:
            stale = []
            for item in queue:
                if item.get("status") != "inflight":
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

        nonce = record.get("nonce")
        message = self.ack_message(str(nonce)) if nonce else None
        if not message and nonce:
            message = self.cached_nonce(str(nonce))
        if nonce:
            self.discard_nonce(str(nonce))
        if not message:
            message = {}

        self.busy[agent] = True
        self.reserved[agent] = None
        self.current_prompt_by_agent[agent] = {
            "id": message.get("id"),
            "nonce": nonce,
            "causal_id": message.get("causal_id"),
            "hop_count": int(message.get("hop_count") or 0),
            "from": message.get("from"),
            "kind": normalize_kind(message.get("kind"), "notice") if message else None,
            "intent": message.get("intent"),
            "auto_return": bool(message.get("auto_return")),
        }

        if message.get("id"):
            self.log(
                "message_delivered",
                message_id=message.get("id"),
                to=agent,
                nonce=nonce,
                from_agent=message.get("from"),
                kind=message.get("kind"),
                intent=message.get("intent"),
                causal_id=message.get("causal_id"),
                hop_count=message.get("hop_count"),
                auto_return=message.get("auto_return"),
            )

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

    def maybe_return_response(self, sender: str, text: str, context: dict) -> None:
        requester = context.get("from")
        self.reload_participants()
        if requester not in self.participants or requester == sender:
            return
        if not context.get("auto_return"):
            return
        if not text.strip():
            return

        causal_id = context.get("causal_id") or f"causal-{uuid.uuid4().hex[:12]}"
        hop_count = int(context.get("hop_count") or 0)
        original_intent = context.get("intent") or "message"
        return_intent = f"{original_intent}_result"
        body = (
            f"Result from {sender}:\n"
            f"{text}"
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

    def handle_response_finished(self, record: dict) -> None:
        self.reload_participants()
        sender = self.participant_alias(record)
        if sender not in self.participants:
            return

        self.busy[sender] = False
        self.reserved[sender] = None

        text = record.get("last_assistant_message") or ""
        context = self.current_prompt_by_agent.get(sender, {})
        fingerprint = self.response_fingerprint(record)
        if self.processed_returns.add(fingerprint):
            self.maybe_return_response(sender, text, context)
        self.try_deliver(sender)
        self.try_deliver()

    def handle_external_message_queued(self, record: dict) -> None:
        target = record.get("to")
        self.reload_participants()
        if target in self.participants:
            self.try_deliver(str(target))

    def handle_record(self, record: dict) -> None:
        if self.bridge_session:
            record_session = record.get("bridge_session")
            if record.get("agent") in PHYSICAL_AGENT_TYPES and record_session != self.bridge_session:
                return
            if record.get("event") == "message_queued" and record_session and record_session != self.bridge_session:
                return

        event = record.get("event")
        if event == "message_queued":
            self.handle_external_message_queued(record)
            return
        if event == "prompt_submitted":
            self.handle_prompt_submitted(record)
            return
        if event == "response_finished":
            self.handle_response_finished(record)

    def follow(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.touch(exist_ok=True)
        self.start_command_server()

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
                )

                self.try_deliver()

                while True:
                    if self.stop_requested():
                        break
                    self.requeue_stale_inflight()
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
