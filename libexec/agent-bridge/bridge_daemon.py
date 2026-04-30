#!/usr/bin/env python3
import argparse
from contextlib import contextmanager
import hashlib
import errno
import json
import math
import os
import re
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bridge_clear_marker import (
    cleanup_expired_or_orphaned,
    find_for_clear_window,
    make_marker,
    read_markers,
    remove_marker,
    ttl_for_clear_lifetime,
    update_marker,
    write_marker,
)
from bridge_clear_guard import (
    ClearGuardResult,
    ClearViolation,
    active_queue_rows,
    cancellable_queue_rows,
    format_clear_guard_result,
    target_originated_requests,
)
from bridge_daemon_messages import (
    PROMPT_BODY_CONTROL_TRANSLATION,
    build_peer_prompt,
    kind_expects_response,
    make_message,
    normalize_prompt_body_text,
    one_line,
    prompt_body,
)
import bridge_daemon_aggregates as daemon_aggregates
import bridge_daemon_commands as daemon_commands
import bridge_daemon_delivery as daemon_delivery
import bridge_daemon_events as daemon_events
import bridge_daemon_status as daemon_status
import bridge_daemon_watchdogs as daemon_watchdogs
from bridge_daemon_delivery import (
    PANE_MODE_ENTER_DEFER_KEYS,
    PANE_MODE_FORCE_CANCEL_MODES,
    PANE_MODE_METADATA_KEYS,
    RESTART_INFLIGHT_METADATA_KEYS,
    TMUX_DELIVERY_WORST_CASE_SECONDS,
    pane_mode_block_since_ts,
)
from bridge_daemon_events import EMPTY_RESPONSE_BODY
from bridge_daemon_status import AGGREGATE_STATUS_LEG_LIMIT, WAIT_STATUS_SECTION_LIMIT
from bridge_daemon_store import AggregateStore, QueueStore
from bridge_daemon_state import (
    BoundedSet,
    ClearState,
    LockFacade,
    MaintenanceState,
    ParticipantCache,
    RoutingState,
    StateField,
    WatchdogState,
)
from bridge_daemon_tmux import (
    PANE_MODE_PROBE_TIMEOUT_SECONDS,
    TMUX_SEND_TIMEOUT_SECONDS,
    _tmux_buffer_component,
    cancel_tmux_pane_mode,
    probe_tmux_pane_mode,
    run_tmux_enter,
    run_tmux_send_literal,
    run_tmux_send_literal_touch_result,
    tmux_prompt_buffer_name,
)
from bridge_daemon_watchdogs import (
    ALARM_CLIENT_WAKE_ID_RE,
    ALARM_WAKE_TOMBSTONE_LIMIT,
    ALARM_WAKE_TOMBSTONE_TTL_SECONDS,
    EXTEND_WATCHDOG_HINTS,
    RESPONSE_LIKE_TOMBSTONE_REASONS,
    WATCHDOG_PHASE_ALARM,
    WATCHDOG_PHASE_DELIVERY,
    WATCHDOG_PHASE_RESPONSE,
    WATCHDOG_REQUIRES_AUTO_RETURN_ERROR,
    WATCHDOG_REQUIRES_AUTO_RETURN_TEXT,
)
from bridge_identity import (
    backfill_session_process_identities,
    live_record_matches,
    read_live_by_pane,
    replace_attached_session_identity_for_clear,
    resolve_participant_endpoint_detail,
    verified_process_identity,
)
from bridge_instructions import probe_prompt
from bridge_participants import active_participants, format_peer_summary, participant_record
from bridge_pane_probe import probe_agent_process
from bridge_paths import model_bin_dir, state_root
from bridge_response_guard import (
    context_from_current_prompt,
    format_response_send_violation,
    response_send_violation,
)
from bridge_util import (
    MAX_PEER_BODY_CHARS,
    RESTART_PRESERVED_INFLIGHT_KEY,
    SHORT_ID_LEN,
    SHARED_PAYLOAD_ROOT,
    append_jsonl,
    classify_prior_for_hint,
    locked_json,
    normalize_kind,
    prior_message_hint_candidates,
    prior_message_hint_entry,
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
CLEAR_PROBE_TIMEOUT_SECONDS = 180.0
CLEAR_POST_CLEAR_DELAY_DEFAULT_SECONDS = 1.0
CLEAR_POST_LOCK_WORST_CASE_SECONDS = 75.0
# Keep in sync with bridge_clear_peer.CLEAR_SOCKET_TIMEOUT_SECONDS.
CLEAR_CLIENT_TIMEOUT_SECONDS = 180.0
CLEAR_MULTI_TIMEOUT_MARGIN_SECONDS = 10.0
CLEAR_LOCK_WAIT_BUDGET_SECONDS = 85.0
COMMAND_DEFAULT_CLIENT_TIMEOUT_SECONDS = 5.0
COMMAND_SHORT_CLIENT_TIMEOUT_SECONDS = 2.0
COMMAND_DEFAULT_POST_LOCK_WORST_CASE_SECONDS = 0.5
COMMAND_SHORT_POST_LOCK_WORST_CASE_SECONDS = 0.25
COMMAND_SAFETY_MARGIN_SECONDS = 0.5
COMMAND_SHORT_SAFETY_MARGIN_SECONDS = 0.25
INTERRUPT_KEY_DELAY_DEFAULT_SECONDS = 0.15
INTERRUPT_KEY_DELAY_MIN_SECONDS = 0.05
INTERRUPT_KEY_DELAY_MAX_SECONDS = 1.0
INTERRUPT_SEND_KEY_TIMEOUT_SECONDS = 1.0
CLAUDE_INTERRUPT_KEYS_DEFAULT = ("Escape", "C-c")
INTERRUPTED_TOMBSTONE_LIMIT_PER_AGENT = 16
INTERRUPTED_TOMBSTONE_TTL_SECONDS = 600.0
PEER_RESULT_REDIRECT_PREVIEW_CHARS = 100
PEER_RESULT_REDIRECT_WRAPPER_MAX_CHARS = 600
PEER_RESULT_REDIRECT_DIRNAME = "replies"
PEER_RESULT_FILE_ID_RE = re.compile(rf"^msg-[a-f0-9]{{{SHORT_ID_LEN}}}$")
_STOP_SIGNAL: int | None = None


def _request_stop(signum: int, _frame: object) -> None:
    global _STOP_SIGNAL
    _STOP_SIGNAL = signum


def install_signal_handlers() -> None:
    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)


class CommandLockWaitExceeded(RuntimeError):
    def __init__(self, command_class: str = "") -> None:
        self.command_class = command_class
        super().__init__(command_class or "lock_wait_exceeded")


class PeerResultRedirectError(RuntimeError):
    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(detail or reason)


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


def resolve_clear_post_clear_delay_seconds() -> tuple[float, str | None]:
    env_name = "AGENT_BRIDGE_CLEAR_POST_CLEAR_DELAY_SEC"
    raw = os.environ.get(env_name)
    if raw is None or str(raw).strip() == "":
        return CLEAR_POST_CLEAR_DELAY_DEFAULT_SECONDS, None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return (
            CLEAR_POST_CLEAR_DELAY_DEFAULT_SECONDS,
            f"invalid {env_name}={raw!r}; using {CLEAR_POST_CLEAR_DELAY_DEFAULT_SECONDS:g}",
        )
    if not math.isfinite(value):
        return (
            CLEAR_POST_CLEAR_DELAY_DEFAULT_SECONDS,
            f"{env_name}={raw!r} is non-finite; using {CLEAR_POST_CLEAR_DELAY_DEFAULT_SECONDS:g}",
        )
    if value < 0:
        return 0.0, f"{env_name}={raw!r} is negative; using 0"
    return value, None


def resolve_interrupt_key_delay_seconds() -> tuple[float, str | None]:
    value, warning = resolve_non_negative_env_seconds(
        "AGENT_BRIDGE_INTERRUPT_KEY_DELAY_SEC",
        INTERRUPT_KEY_DELAY_DEFAULT_SECONDS,
    )
    if not math.isfinite(value):
        nonfinite_warning = (
            f"AGENT_BRIDGE_INTERRUPT_KEY_DELAY_SEC={value!r} is non-finite; "
            f"using {INTERRUPT_KEY_DELAY_DEFAULT_SECONDS:g}"
        )
        warning = f"{warning}; {nonfinite_warning}" if warning else nonfinite_warning
        value = INTERRUPT_KEY_DELAY_DEFAULT_SECONDS
    clamped = min(max(value, INTERRUPT_KEY_DELAY_MIN_SECONDS), INTERRUPT_KEY_DELAY_MAX_SECONDS)
    if clamped != value:
        clamp_warning = (
            f"AGENT_BRIDGE_INTERRUPT_KEY_DELAY_SEC={value:g} outside "
            f"[{INTERRUPT_KEY_DELAY_MIN_SECONDS:g}, {INTERRUPT_KEY_DELAY_MAX_SECONDS:g}]; using {clamped:g}"
        )
        warning = f"{warning}; {clamp_warning}" if warning else clamp_warning
    return clamped, warning


def resolve_claude_interrupt_keys() -> tuple[tuple[str, ...], str | None]:
    raw = os.environ.get("AGENT_BRIDGE_CLAUDE_INTERRUPT_KEYS")
    if raw is None or str(raw).strip() == "":
        return CLAUDE_INTERRUPT_KEYS_DEFAULT, None
    aliases = {
        "esc": "Escape",
        "escape": "Escape",
        "c-c": "C-c",
        "ctrl-c": "C-c",
        "ctrl+c": "C-c",
        "control-c": "C-c",
    }
    keys: list[str] = []
    for part in str(raw).split(","):
        token = part.strip().lower()
        if not token:
            continue
        key = aliases.get(token)
        if not key:
            return (
                CLAUDE_INTERRUPT_KEYS_DEFAULT,
                f"invalid AGENT_BRIDGE_CLAUDE_INTERRUPT_KEYS={raw!r}; using esc,c-c",
            )
        if key not in keys:
            keys.append(key)
    if not keys:
        return (
            CLAUDE_INTERRUPT_KEYS_DEFAULT,
            f"AGENT_BRIDGE_CLAUDE_INTERRUPT_KEYS={raw!r} has no keys; using esc,c-c",
        )
    if keys[0] != "Escape":
        return (
            CLAUDE_INTERRUPT_KEYS_DEFAULT,
            f"AGENT_BRIDGE_CLAUDE_INTERRUPT_KEYS={raw!r} must include esc first; using esc,c-c",
        )
    return tuple(keys), None


class BridgeDaemon:
    session_state = StateField("participant_cache", "session_state")
    participants = StateField("participant_cache", "participants")
    panes = StateField("participant_cache", "panes")
    session_mtime_ns = StateField("participant_cache", "session_mtime_ns")
    startup_backfill_summary = StateField("participant_cache", "startup_backfill_summary")
    busy = StateField("routing_state", "busy")
    reserved = StateField("routing_state", "reserved")
    current_prompt_by_agent = StateField("routing_state", "current_prompt_by_agent")
    injected_by_nonce = StateField("routing_state", "injected_by_nonce")
    last_enter_ts = StateField("routing_state", "last_enter_ts")
    interrupted_turns = StateField("routing_state", "interrupted_turns")
    processed_returns = StateField("watchdog_state", "processed_returns")
    processed_capture_requests = StateField("watchdog_state", "processed_capture_requests")
    watchdogs = StateField("watchdog_state", "watchdogs")
    alarm_wake_tombstones = StateField("watchdog_state", "alarm_wake_tombstones")
    held_interrupt = StateField("clear_state", "held_interrupt")
    interrupt_partial_failure_blocks = StateField("clear_state", "interrupt_partial_failure_blocks")
    clear_reservations = StateField("clear_state", "clear_reservations")
    pending_self_clears = StateField("clear_state", "pending_self_clears")
    last_maintenance = StateField("maintenance_state", "last_maintenance")
    last_capture_cleanup = StateField("maintenance_state", "last_capture_cleanup")
    last_ingressing_check = StateField("maintenance_state", "last_ingressing_check")
    stop_logged = StateField("maintenance_state", "stop_logged")
    last_delivery_tick = StateField("maintenance_state", "last_delivery_tick")
    state_lock = StateField("lock_facade", "state_lock")

    def __init__(self, args: argparse.Namespace) -> None:
        self.state_file = Path(args.state_file)
        self.public_state_file = Path(args.public_state_file) if args.public_state_file else None
        self.queue = QueueStore(args.queue_file)
        self.aggregate_file = Path(args.queue_file).parent / "aggregates.json"
        self.aggregates = AggregateStore(self.aggregate_file)
        self.args = args
        self.session_file = Path(args.session_file) if args.session_file else Path(args.queue_file).parent / "session.json"
        self.participant_cache = ParticipantCache()
        self.routing_state = RoutingState()
        self.watchdog_state = WatchdogState(MAX_PROCESSED_RETURNS, MAX_PROCESSED_CAPTURE_REQUESTS)
        self.clear_state = ClearState()
        self.maintenance_state = MaintenanceState()
        # Coarse RLock that serializes mutations to in-memory routing state
        # (busy, reserved, current_prompt_by_agent, held_interrupt,
        # interrupt_partial_failure_blocks,
        # interrupted_turns, last_enter_ts, watchdogs, panes, participants caches) and gates
        # event-handler / command-socket / maintenance interleaving.
        # Lock ordering rule: state_lock is ALWAYS acquired before
        # queue.update()'s file lock. Queue mutator callbacks must NOT
        # call back into self.* methods or invoke logging — they should
        # only manipulate the queue list and return data for callers to
        # process outside the mutator.
        # Interrupt handling also dispatches its tmux key sequence while
        # holding this lock, including Claude's short ESC -> C-c delay, so
        # hook events and replacement delivery cannot interleave between keys.
        self.lock_facade = LockFacade()
        self.submit_delay = args.submit_delay
        self.submit_timeout = args.submit_timeout
        self.clear_post_clear_delay_seconds, clear_delay_warning = resolve_clear_post_clear_delay_seconds()
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
        self.interrupt_key_delay_seconds, interrupt_key_delay_warning = resolve_interrupt_key_delay_seconds()
        self.claude_interrupt_keys, claude_interrupt_keys_warning = resolve_claude_interrupt_keys()
        self.turn_id_mismatch_grace_warnings = [
            warning
            for warning in (
                turn_id_mismatch_grace_warning,
                turn_id_mismatch_post_watchdog_grace_warning,
            )
            if warning
        ]
        self.interrupt_config_warnings = [
            warning
            for warning in (interrupt_key_delay_warning, claude_interrupt_keys_warning)
            if warning
        ]
        self.clear_config_warnings = [
            warning
            for warning in (clear_delay_warning,)
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
        # held_interrupt is a legacy/manual recovery marker. New default
        # interrupts no longer enter it; --clear-hold can still release old
        # or manually planted holds. It is informational for delivery: queued
        # corrections are allowed to flow without waiting for --clear-hold.
        # A partial Claude interrupt (ESC succeeded but follow-up C-c failed)
        # leaves the pane input potentially dirty. This gate blocks all later
        # delivery to that target until another interrupt completes the full
        # configured key sequence, or an operator manually clears it.
        # interrupted_turns[alias] stores short-lived message tombstones. Some
        # suppress identifiable late prompt_submitted / response_finished events
        # from cancelled turns; all help model-facing commands distinguish
        # recently terminal ids from never-seen ids.
        # command_context is intentionally left on BridgeDaemon: it is
        # thread-local command execution context, not shared routing state.
        self.command_context = threading.local()
        try:
            removed_markers = cleanup_expired_or_orphaned(active_marker_ids={"__daemon_startup_no_active_clears__"})
            for marker in removed_markers:
                self.safe_log("controlled_clear_marker_removed_on_startup", marker_id=marker.get("id"))
        except Exception:
            pass
        self.reload_participants()
        self._preserve_startup_inflight_messages()
        if self.bridge_session and not self.dry_run:
            try:
                self.startup_backfill_summary = backfill_session_process_identities(self.bridge_session, self.session_state)
                self.reload_participants()
            except Exception as exc:
                self.startup_backfill_summary = {"_error": {"status": "unknown", "reason": str(exc)}}

    def start_command_server(self) -> None:
        return daemon_commands.start_command_server(self)

    def stop_command_server(self) -> None:
        return daemon_commands.stop_command_server(self)

    def command_server_loop(self) -> None:
        return daemon_commands.command_server_loop(self)

    def handle_command_worker(self, conn: socket.socket) -> None:
        return daemon_commands.handle_command_worker(self, conn)

    def _finite_watchdog_delay(self, message: dict) -> float | None:
        return daemon_watchdogs._finite_watchdog_delay(self, message)

    def _watchdog_strip_log_fields(self, message: dict, *, phase: str, delay: float, reason: str) -> dict:
        return daemon_watchdogs._watchdog_strip_log_fields(self, message, phase=phase, delay=delay, reason=reason)

    def _strip_no_auto_return_watchdog_metadata(self, message: dict, *, phase: str, reason: str) -> dict | None:
        return daemon_watchdogs._strip_no_auto_return_watchdog_metadata(self, message, phase=phase, reason=reason)

    def validate_enqueue_watchdog_metadata(self, message: dict) -> dict | None:
        return daemon_watchdogs.validate_enqueue_watchdog_metadata(self, message)

    def _classify_prior_for_hint(self, item: dict) -> str | None:
        return classify_prior_for_hint(item, self.last_enter_ts)

    def prior_message_hint_for_enqueue(self, message: dict, queue_snapshot: list[dict]) -> dict | None:
        target = str(message.get("to") or "")
        sender = str(message.get("from") or "")
        message_id = str(message.get("id") or "")
        candidates = prior_message_hint_candidates(message, queue_snapshot, self.last_enter_ts)
        active_context = self.current_prompt_by_agent.get(target) or {}
        active_message_id = str(active_context.get("id") or "")
        if (
            active_message_id
            and active_message_id != message_id
            and str(active_context.get("from") or "") == sender
        ):
            prior = {
                "id": active_message_id,
                "from": sender,
                "to": target,
                "status": "active",
                "aggregate_id": active_context.get("aggregate_id") or "",
            }
            candidates.append((0, len(queue_snapshot), prior, "interrupt"))
        if not candidates:
            return None
        _priority, _index, prior, prior_kind = min(candidates, key=lambda row: (row[0], row[1]))
        return prior_message_hint_entry(message, prior, prior_kind)

    def handle_enqueue_command(self, messages: list, force_response_send: bool = False) -> dict:
        if not isinstance(messages, list):
            return {"ok": False, "error": "messages must be a list"}
        ids = []
        hints = []
        try:
            self.reload_participants()
            lock_ctx = self.command_state_lock(
                post_lock_worst_case=COMMAND_SHORT_POST_LOCK_WORST_CASE_SECONDS,
                margin=COMMAND_SHORT_SAFETY_MARGIN_SECONDS,
                command_class="enqueue",
            )
            with lock_ctx:
                if not self.command_deadline_ok(
                    post_lock_worst_case=COMMAND_SHORT_POST_LOCK_WORST_CASE_SECONDS,
                    margin=COMMAND_SHORT_SAFETY_MARGIN_SECONDS,
                    command_class="enqueue",
                ):
                    return self.lock_wait_exceeded_response("enqueue")
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
                    if self.sender_blocked_by_clear(sender):
                        return {
                            "ok": False,
                            "error": f"sender {sender!r} is blocked by a pending clear",
                            "error_kind": "self_clear_pending" if sender in self.pending_self_clears else "clear_in_progress",
                        }
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
                    watchdog_error = self.validate_enqueue_watchdog_metadata(message)
                    if watchdog_error:
                        return watchdog_error
                    validated.append(message)
                for message in validated:
                    hint = self.prior_message_hint_for_enqueue(message, list(self.queue.read()))
                    if self.enqueue_ipc_message(message):
                        ids.append(message["id"])
                        if hint:
                            hints.append(hint)
        except CommandLockWaitExceeded:
            return self.lock_wait_exceeded_response("enqueue")
        response = {"ok": True, "ids": ids}
        if hints:
            response["hints"] = hints
        return response

    def handle_command_connection(self, conn: socket.socket) -> dict:
        return daemon_commands.handle_command_connection(self, conn)

    def peer_uid(self, conn: socket.socket) -> int | None:
        return daemon_commands.peer_uid(self, conn)

    def begin_command_context(self, op: str, request: dict | None = None) -> None:
        request = request if isinstance(request, dict) else {}
        client_timeout = COMMAND_DEFAULT_CLIENT_TIMEOUT_SECONDS
        clear_target_count = 1
        if op in {"enqueue", "alarm"}:
            client_timeout = COMMAND_SHORT_CLIENT_TIMEOUT_SECONDS
        if op == "clear_peer":
            clear_target_count = self.clear_target_count_from_request(request)
            client_timeout = self.clear_peer_client_timeout_seconds(clear_target_count)
        self.command_context.info = {
            "op": op,
            "started_ts": time.monotonic(),
            "client_timeout": client_timeout,
            "clear_target_count": clear_target_count,
        }

    def command_context_info(self) -> dict:
        info = getattr(self.command_context, "info", None)
        return info if isinstance(info, dict) else {}

    def command_remaining_budget(self) -> float | None:
        info = self.command_context_info()
        if not info:
            return None
        try:
            started = float(info.get("started_ts") or 0.0)
            timeout = float(info.get("client_timeout") or 0.0)
        except (TypeError, ValueError):
            return None
        return max(0.0, started + timeout - time.monotonic())

    def command_deadline_ok(self, *, post_lock_worst_case: float, margin: float, command_class: str = "") -> bool:
        remaining = self.command_remaining_budget()
        if remaining is None:
            return True
        ok = remaining >= max(0.0, float(post_lock_worst_case) + float(margin))
        if not ok:
            self.log(
                "command_lock_wait_exceeded",
                command_class=command_class or self.command_context_info().get("op"),
                remaining_budget_ms=int(remaining * 1000),
                required_budget_ms=int(max(0.0, float(post_lock_worst_case) + float(margin)) * 1000),
            )
        return ok

    def command_budget(self, command_class: str = "") -> tuple[float, float]:
        command = str(command_class or self.command_context_info().get("op") or "")
        if command == "clear_peer":
            try:
                count = int(self.command_context_info().get("clear_target_count") or 1)
            except (TypeError, ValueError):
                count = 1
            if count > 1:
                return self.clear_peer_batch_post_lock_worst_case_seconds(count), 20.0
            return self.clear_peer_post_lock_worst_case_seconds(), 20.0
        if command in {"enqueue", "alarm"}:
            return COMMAND_SHORT_POST_LOCK_WORST_CASE_SECONDS, COMMAND_SHORT_SAFETY_MARGIN_SECONDS
        return COMMAND_DEFAULT_POST_LOCK_WORST_CASE_SECONDS, COMMAND_SAFETY_MARGIN_SECONDS

    def clear_peer_post_lock_worst_case_seconds(self) -> float:
        return CLEAR_POST_LOCK_WORST_CASE_SECONDS + max(0.0, float(self.clear_post_clear_delay_seconds or 0.0))

    def clear_peer_batch_post_lock_worst_case_seconds(self, target_count: int) -> float:
        count = max(1, int(target_count or 1))
        base = self.clear_peer_post_lock_worst_case_seconds() * count
        if count > 1:
            base += CLEAR_MULTI_TIMEOUT_MARGIN_SECONDS
        return base

    def clear_peer_client_timeout_seconds(self, target_count: int) -> float:
        count = max(1, int(target_count or 1))
        if count <= 1:
            return CLEAR_CLIENT_TIMEOUT_SECONDS
        settle_extra = max(0.0, float(self.clear_post_clear_delay_seconds or 0.0) - CLEAR_POST_CLEAR_DELAY_DEFAULT_SECONDS)
        return (CLEAR_CLIENT_TIMEOUT_SECONDS + settle_extra) * count + CLEAR_MULTI_TIMEOUT_MARGIN_SECONDS

    def clear_target_count_from_request(self, request: dict) -> int:
        targets = request.get("targets")
        if isinstance(targets, list) and targets:
            return len(targets)
        return 1

    def _state_lock_owned_by_current_thread(self) -> bool:
        is_owned = getattr(self.state_lock, "_is_owned", None)
        if not callable(is_owned):
            return False
        try:
            return bool(is_owned())
        except Exception:
            return False

    @contextmanager
    def command_state_lock(
        self,
        *,
        post_lock_worst_case: float | None = None,
        margin: float | None = None,
        command_class: str = "",
    ):
        command = str(command_class or self.command_context_info().get("op") or "")
        if post_lock_worst_case is None or margin is None:
            default_post_lock, default_margin = self.command_budget(command)
            if post_lock_worst_case is None:
                post_lock_worst_case = default_post_lock
            if margin is None:
                margin = default_margin
        required = max(0.0, float(post_lock_worst_case) + float(margin))
        if not self.command_context_info():
            with self.state_lock:
                yield
            return
        if self._state_lock_owned_by_current_thread():
            self.state_lock.acquire()
            try:
                if not self.command_deadline_ok(
                    post_lock_worst_case=float(post_lock_worst_case),
                    margin=float(margin),
                    command_class=command,
                ):
                    raise CommandLockWaitExceeded(command)
                yield
            finally:
                self.state_lock.release()
            return
        remaining = self.command_remaining_budget()
        if remaining is not None and remaining < required:
            self.log(
                "command_lock_wait_exceeded",
                command_class=command or self.command_context_info().get("op") or "",
                remaining_budget_ms=int(remaining * 1000),
                required_budget_ms=int(required * 1000),
            )
            raise CommandLockWaitExceeded(command)
        if remaining is None:
            self.state_lock.acquire()
            acquired = True
        else:
            wait_budget = max(0.0, remaining - required)
            if command == "clear_peer":
                wait_budget = min(wait_budget, CLEAR_LOCK_WAIT_BUDGET_SECONDS)
            acquired = self.state_lock.acquire(timeout=wait_budget)
        if not acquired:
            latest = self.command_remaining_budget()
            self.log(
                "command_lock_wait_exceeded",
                command_class=command or self.command_context_info().get("op") or "",
                remaining_budget_ms=int((latest or 0.0) * 1000),
                required_budget_ms=int(required * 1000),
            )
            raise CommandLockWaitExceeded(command)
        try:
            if not self.command_deadline_ok(
                post_lock_worst_case=float(post_lock_worst_case),
                margin=float(margin),
                command_class=command,
            ):
                raise CommandLockWaitExceeded(command)
            yield
        finally:
            self.state_lock.release()

    def lock_wait_exceeded_response(self, command_class: str = "") -> dict:
        return {
            "ok": False,
            "error": "lock_wait_exceeded",
            "error_kind": "lock_wait_exceeded",
            "command_class": command_class or self.command_context_info().get("op") or "",
        }

    def run_tmux_send_literal(self, *args, **kwargs):
        return run_tmux_send_literal(*args, **kwargs)

    def run_tmux_enter(self, *args, **kwargs):
        return run_tmux_enter(*args, **kwargs)

    def command_delivery_allowed(self, target: str, message_id: str = "") -> bool:
        remaining = self.command_remaining_budget()
        if remaining is None:
            return True
        required = TMUX_DELIVERY_WORST_CASE_SECONDS + COMMAND_SAFETY_MARGIN_SECONDS
        if remaining >= required:
            return True
        self.log(
            "command_delivery_deferred_deadline",
            command_class=self.command_context_info().get("op") or "",
            message_id=message_id,
            target=target,
            remaining_budget_ms=int(remaining * 1000),
        )
        return False

    def try_deliver_command_aware(self, target: str | None = None, *, message_id: str = "") -> None:
        return daemon_delivery.try_deliver_command_aware(self, target, message_id=message_id)

    def sender_blocked_by_clear(self, sender: str) -> bool:
        if not sender or sender == "bridge":
            return False
        return sender in self.clear_reservations or sender in self.pending_self_clears

    def _clear_guard_from_snapshots(
        self,
        target: str,
        *,
        force: bool,
        queue_snapshot: list[dict],
        aggregates: dict,
    ) -> ClearGuardResult:
        hard: list[ClearViolation] = []
        soft: list[ClearViolation] = []
        def add_violation(violation: ClearViolation) -> None:
            violation.target = violation.target or target
            if violation.hard:
                hard.append(violation)
            else:
                soft.append(violation)

        if target in self.clear_reservations:
            add_violation(ClearViolation("clear_already_pending", f"clear already active for {target}"))
        if target in self.pending_self_clears:
            add_violation(ClearViolation("clear_already_pending", f"self-clear already pending for {target}"))
        if self.busy.get(target) or (self.current_prompt_by_agent.get(target) or {}).get("id"):
            add_violation(ClearViolation("target_busy", f"{target} is currently processing a prompt"))
        inbound_active = active_queue_rows(queue_snapshot, target=target, last_enter_ts=self.last_enter_ts)
        if inbound_active:
            add_violation(
                ClearViolation(
                    "target_active_messages",
                    f"{target} has active/post-pane-touch inbound work",
                    refs=[str(item.get("id") or "") for item in inbound_active if item.get("id")],
                )
            )
        inbound_cancellable = cancellable_queue_rows(queue_snapshot, target=target, last_enter_ts=self.last_enter_ts)
        if inbound_cancellable:
            add_violation(
                ClearViolation(
                    "target_cancellable_messages",
                    f"{target} has cancellable pending/pre-active inbound work",
                    hard=False,
                    refs=[str(item.get("id") or "") for item in inbound_cancellable if item.get("id")],
                )
            )
        originated_requests = target_originated_requests(queue_snapshot, target=target)
        if originated_requests:
            refs = [str(item.get("id") or "") for item in originated_requests if item.get("id")]
            add_violation(
                ClearViolation(
                    "target_originated_requests",
                    f"{target} has outstanding request rows",
                    hard=False,
                    refs=refs,
                )
            )
        target_alarms = [
            wake_id
            for wake_id, wd in self.watchdogs.items()
            if wd and wd.get("is_alarm") and str(wd.get("sender") or "") == target
        ]
        if target_alarms:
            add_violation(
                ClearViolation(
                    "target_owned_alarms",
                    f"{target} owns active alarms",
                    hard=False,
                    refs=target_alarms,
                )
            )
        requester_aggs = [
            str(agg_id)
            for agg_id, agg in aggregates.items()
            if isinstance(agg, dict)
            and str(agg.get("requester") or "") == target
            and str(agg.get("status") or "collecting") not in {"complete", "cancelled_requester_cleared"}
        ]
        if requester_aggs:
            add_violation(
                ClearViolation(
                    "target_aggregate_requester",
                    f"{target} owns incomplete aggregate waits",
                    hard=False,
                    refs=requester_aggs,
                )
            )
        if hard or (soft and not force):
            return ClearGuardResult(ok=False, hard_blockers=hard, soft_blockers=soft, force_attempted=force)
        return ClearGuardResult(ok=True, hard_blockers=hard, soft_blockers=soft, force_attempted=force)

    def clear_guard(self, target: str, *, force: bool) -> ClearGuardResult:
        queue_snapshot = list(self.queue.read())
        aggregates = self.aggregates.read_aggregates()
        return self._clear_guard_from_snapshots(
            target,
            force=force,
            queue_snapshot=queue_snapshot,
            aggregates=aggregates,
        )

    def clear_guard_multi(
        self,
        targets: list[str],
        *,
        force: bool,
        queue_snapshot: list[dict],
        aggregates: dict,
    ) -> ClearGuardResult:
        hard: list[ClearViolation] = []
        soft: list[ClearViolation] = []
        for target in targets:
            result = self._clear_guard_from_snapshots(
                target,
                force=force,
                queue_snapshot=queue_snapshot,
                aggregates=aggregates,
            )
            hard.extend(result.hard_blockers)
            soft.extend(result.soft_blockers)
        return ClearGuardResult(
            ok=not hard and not (soft and not force),
            hard_blockers=hard,
            soft_blockers=soft,
            force_attempted=force,
        )

    def apply_force_clear_invalidation(self, target: str, caller: str) -> dict:
        now_iso = utc_now()
        cancelled_alarms: list[str] = []
        removed_rows: list[dict] = []
        requester_cleared_ids: list[str] = []
        cancelled_aggregates: list[str] = []
        for wake_id, wd in list(self.watchdogs.items()):
            if wd and wd.get("is_alarm") and str(wd.get("sender") or "") == target:
                removed = self.watchdogs.pop(wake_id, None)
                if removed:
                    self._record_alarm_wake_tombstone(wake_id, removed, "cancelled_by_clear")
                    cancelled_alarms.append(wake_id)

        def queue_mutator(queue: list[dict]) -> None:
            kept: list[dict] = []
            for item in queue:
                if str(item.get("to") or "") == target and classify_prior_for_hint(item, self.last_enter_ts) == "cancel":
                    removed_rows.append(dict(item))
                    continue
                if str(item.get("from") or "") == target and normalize_kind(item.get("kind"), "request") == "request":
                    item["auto_return"] = False
                    item["requester_cleared"] = True
                    item["requester_cleared_alias"] = target
                    item["requester_cleared_at"] = now_iso
                    item["requester_cleared_by"] = caller
                    item["updated_ts"] = now_iso
                    requester_cleared_ids.append(str(item.get("id") or ""))
                kept.append(item)
            queue[:] = kept

        self.queue.update(queue_mutator)
        for row in removed_rows:
            msg_id = str(row.get("id") or "")
            nonce = str(row.get("nonce") or "")
            if nonce:
                self.discard_nonce(nonce)
            if msg_id:
                self.last_enter_ts.pop(msg_id, None)
                self.cancel_watchdogs_for_message(msg_id, reason="cancelled_by_clear")
                self.suppress_pending_watchdog_wakes(ref_message_id=msg_id, reason="cancelled_by_clear")
            self._record_message_tombstone(
                target,
                row,
                by_sender=caller,
                reason="cancelled_by_clear",
                suppress_late_hooks=bool(nonce),
                prompt_submitted_seen=False,
            )
            if row.get("aggregate_id"):
                self._record_aggregate_interrupted_reply(row, by_sender=caller, reason="cancelled_by_clear")
        for msg_id in requester_cleared_ids:
            self.cancel_watchdogs_for_message(msg_id, reason="requester_cleared")
            self.suppress_pending_watchdog_wakes(ref_message_id=msg_id, reason="requester_cleared")
        for responder, context in list(self.current_prompt_by_agent.items()):
            if str(context.get("from") or "") != target:
                continue
            context["auto_return"] = False
            context["requester_cleared"] = True
            context["requester_cleared_alias"] = target
            context["requester_cleared_at"] = now_iso
            context["requester_cleared_by"] = caller
            msg_id = str(context.get("id") or "")
            if msg_id:
                self.cancel_watchdogs_for_message(msg_id, reason="requester_cleared")
                self.suppress_pending_watchdog_wakes(ref_message_id=msg_id, reason="requester_cleared")
            self.log(
                "active_requester_cleared",
                responder=responder,
                requester=target,
                message_id=msg_id,
                by_sender=caller,
            )

        def aggregate_mutator(data: dict) -> None:
            aggregates = data.setdefault("aggregates", {})
            for agg_id, aggregate in list(aggregates.items()):
                if not isinstance(aggregate, dict):
                    continue
                if str(aggregate.get("requester") or "") != target:
                    continue
                if str(aggregate.get("status") or "") == "complete":
                    continue
                aggregate["status"] = "cancelled_requester_cleared"
                aggregate["cancelled_at"] = now_iso
                aggregate["cancelled_by"] = caller
                aggregate["delivered"] = False
                aggregate["updated_ts"] = now_iso
                cancelled_aggregates.append(str(agg_id))

        self.aggregates.update(aggregate_mutator)
        for agg_id in cancelled_aggregates:
            self.cancel_watchdogs_for_aggregate(agg_id, reason="requester_cleared")
            self.suppress_pending_watchdog_wakes(ref_aggregate_id=agg_id, reason="requester_cleared")
        self.log(
            "clear_force_invalidated",
            target=target,
            by_sender=caller,
            cancelled_alarm_ids=cancelled_alarms,
            removed_message_ids=[row.get("id") for row in removed_rows],
            requester_cleared_message_ids=requester_cleared_ids,
            cancelled_aggregate_ids=cancelled_aggregates,
        )
        return {
            "cancelled_alarms": cancelled_alarms,
            "removed_message_ids": [row.get("id") for row in removed_rows],
            "requester_cleared_message_ids": requester_cleared_ids,
            "cancelled_aggregate_ids": cancelled_aggregates,
        }

    def force_leave_after_clear_failure(self, target: str, *, caller: str, reason: str, reservation: dict | None = None) -> None:
        # Forced-leave cleanup is a daemon-internal recovery path, not a
        # client-visible command mutation. It must run serialized even if the
        # originating command budget is exhausted, so take the raw state lock
        # here rather than re-entering command_state_lock.
        if not self._state_lock_owned_by_current_thread():
            with self.state_lock:
                self.force_leave_after_clear_failure(target, caller=caller, reason=reason, reservation=reservation)
            return
        reservation = reservation or self.clear_reservations.get(target) or {}
        reservation["forced_leave"] = True
        reservation["forced_leave_reason"] = reason
        old_session_id = str(reservation.get("old_session_id") or "")
        new_session_id = str(reservation.get("new_session_id") or "")
        participant = self.participants.get(target) or {}
        agent_type = str(participant.get("agent_type") or reservation.get("agent") or "")
        now_iso = utc_now()
        removed_rows: list[dict] = []

        def queue_mutator(queue: list[dict]) -> None:
            kept: list[dict] = []
            for item in queue:
                if str(item.get("to") or "") == target:
                    removed_rows.append(dict(item))
                    continue
                if str(item.get("from") or "") == target and normalize_kind(item.get("kind"), "request") == "request":
                    item["auto_return"] = False
                    item["requester_cleared"] = True
                    item["requester_cleared_alias"] = target
                    item["requester_cleared_at"] = now_iso
                    item["requester_cleared_by"] = caller
                    item["updated_ts"] = now_iso
                kept.append(item)
            queue[:] = kept

        self.queue.update(queue_mutator)
        for row in removed_rows:
            msg_id = str(row.get("id") or "")
            if msg_id:
                self.cancel_watchdogs_for_message(msg_id, reason="clear_forced_leave")
                self.suppress_pending_watchdog_wakes(ref_message_id=msg_id, reason="clear_forced_leave")
            if row.get("aggregate_id"):
                self._record_aggregate_interrupted_reply(row, by_sender=caller, reason="endpoint_lost", deliver=False)
        for wake_id, wd in list(self.watchdogs.items()):
            if wd and str(wd.get("sender") or "") == target:
                removed = self.watchdogs.pop(wake_id, None)
                if removed and removed.get("is_alarm"):
                    self._record_alarm_wake_tombstone(wake_id, removed, "clear_forced_leave")
                self.log("watchdog_cancelled", wake_id=wake_id, reason="clear_forced_leave")
        self.busy[target] = False
        self.reserved[target] = None
        self.current_prompt_by_agent.pop(target, None)
        self.interrupt_partial_failure_blocks.pop(target, None)
        self.pending_self_clears.pop(target, None)
        marker_id_value = str(reservation.get("identity_marker_id") or "")
        if marker_id_value:
            remove_marker(marker_id_value)
        self.clear_reservations.pop(target, None)
        try:
            self._mark_participant_detached_for_clear(target, agent_type=agent_type, old_session_id=old_session_id, new_session_id=new_session_id, reason=reason)
        except Exception as exc:
            self.safe_log("clear_forced_leave_cleanup_failed", target=target, error=str(exc), reason=reason)
        self.session_mtime_ns = None
        self._reload_participants_unlocked()
        for alias in sorted(self.participants):
            if alias == target:
                continue
            notice = make_message(
                sender="bridge",
                target=alias,
                intent="clear_forced_leave_notice",
                body=f"[bridge:peer_cleared] {target} was removed from the bridge after clear failed ({reason}).",
                causal_id=short_id("causal"),
                hop_count=0,
                auto_return=False,
                kind="notice",
                source="clear_forced_leave",
            )
            self.queue_message(notice, deliver=False)
        self.log(
            "clear_forced_leave",
            target=target,
            by_sender=caller,
            reason=reason,
            removed_message_ids=[row.get("id") for row in removed_rows],
        )

    def _mark_participant_detached_for_clear(
        self,
        target: str,
        *,
        agent_type: str,
        old_session_id: str,
        new_session_id: str,
        reason: str,
    ) -> None:
        session_path = self.session_file
        attached_path = state_root() / "attached-sessions.json"
        pane_locks_path = state_root() / "pane-locks.json"
        live_path = state_root() / "live-sessions.json"
        # Honor test/installation overrides used by bridge_identity.
        attached_path = Path(os.environ.get("AGENT_BRIDGE_ATTACH_REGISTRY", str(attached_path)))
        pane_locks_path = Path(os.environ.get("AGENT_BRIDGE_PANE_LOCKS", str(pane_locks_path)))
        live_path = Path(os.environ.get("AGENT_BRIDGE_LIVE_SESSIONS", str(live_path)))
        with locked_json(session_path, {"session": self.bridge_session, "participants": {}}) as state:
            with locked_json(attached_path, {"version": 1, "sessions": {}}) as registry:
                with locked_json(pane_locks_path, {"version": 1, "panes": {}}) as locks:
                    with locked_json(live_path, {"version": 1, "panes": {}, "sessions": {}}) as live:
                        participant = (state.setdefault("participants", {}) or {}).get(target)
                        pane = ""
                        if isinstance(participant, dict):
                            pane = str(participant.get("pane") or "")
                            participant["status"] = "detached"
                            participant["detached_at"] = utc_now()
                            participant["detach_reason"] = reason
                            participant["endpoint_status"] = "cleared"
                        for key in (old_session_id, new_session_id):
                            if key and agent_type:
                                registry.setdefault("sessions", {}).pop(f"{agent_type}:{key}", None)
                                live.setdefault("sessions", {}).pop(f"{agent_type}:{key}", None)
                        panes = locks.setdefault("panes", {})
                        for lock_pane, record in list(panes.items()):
                            if not isinstance(record, dict):
                                continue
                            if record.get("alias") == target or record.get("hook_session_id") in {old_session_id, new_session_id}:
                                del panes[lock_pane]
                        live_panes = live.setdefault("panes", {})
                        for live_pane, record in list(live_panes.items()):
                            if not isinstance(record, dict):
                                continue
                            if live_pane == pane or record.get("alias") == target or record.get("session_id") in {old_session_id, new_session_id}:
                                del live_panes[live_pane]

    def clear_target_lookup_error(self, target: str) -> dict:
        all_participants = self.session_state.get("participants") or {}
        if isinstance(all_participants, dict) and target in all_participants:
            return {
                "ok": False,
                "error": f"target {target!r} is not an active participant",
                "error_kind": "inactive_target",
                "target": target,
            }
        return {
            "ok": False,
            "error": f"unknown target alias {target!r}; active aliases: {', '.join(sorted(self.participants))}",
            "error_kind": "unknown_target",
            "target": target,
        }

    def validate_clear_targets_payload(self, sender: str, targets_payload: object, *, force: bool) -> dict:
        if not isinstance(targets_payload, list):
            return {"ok": False, "error": "targets must be a non-empty list of aliases", "error_kind": "malformed_targets"}
        if not targets_payload:
            return {"ok": False, "error": "targets must be a non-empty list of aliases", "error_kind": "malformed_targets"}
        seen: set[str] = set()
        targets: list[str] = []
        for raw in targets_payload:
            if not isinstance(raw, str):
                return {"ok": False, "error": "targets entries must be aliases", "error_kind": "malformed_targets"}
            target = raw.strip()
            if not target:
                return {"ok": False, "error": "targets entries must be non-empty aliases", "error_kind": "malformed_targets"}
            if target in seen:
                continue
            if target not in self.participants:
                return self.clear_target_lookup_error(target)
            seen.add(target)
            targets.append(target)
        if not targets:
            return {"ok": False, "error": "targets must be a non-empty list of aliases", "error_kind": "malformed_targets"}
        if len(targets) > 1 and force:
            return {
                "ok": False,
                "error": "--force is only supported for single-target clear; specify exactly one alias when using --force",
                "error_kind": "multi_force_disallowed",
            }
        if len(targets) > 1 and sender and sender != "bridge" and sender in targets:
            return {
                "ok": False,
                "error": "self-clear must be the only target; specify only your own alias or omit yourself",
                "error_kind": "multi_self_disallowed",
            }
        return {"ok": True, "targets": targets}

    def clear_batch_summary(self, results: list[dict]) -> dict:
        by_status: dict[str, list[str]] = {"cleared": [], "forced_leave": [], "failed": []}
        for result in results:
            status = str(result.get("status") or "failed")
            target = str(result.get("target") or "")
            by_status.setdefault(status, []).append(target)
        return {
            "counts": {status: len(targets) for status, targets in by_status.items()},
            "cleared": by_status.get("cleared", []),
            "forced_leave": by_status.get("forced_leave", []),
            "failed": by_status.get("failed", []),
            "all_cleared": len(by_status.get("cleared", [])) == len(results),
        }

    def normalize_clear_batch_result(self, target: str, result: dict) -> dict:
        if result.get("ok") and result.get("cleared"):
            status = "cleared"
        elif result.get("forced_leave"):
            status = "forced_leave"
        else:
            status = "failed"
        normalized = {
            "target": target,
            "status": status,
            "ok": bool(result.get("ok")),
            "cleared": bool(result.get("cleared")),
            "forced_leave": bool(result.get("forced_leave")),
        }
        if result.get("error"):
            normalized["error"] = str(result.get("error") or "")
        if result.get("error_kind"):
            normalized["error_kind"] = str(result.get("error_kind") or "")
        if result.get("new_session_id"):
            normalized["new_session_id"] = result.get("new_session_id")
        return normalized

    def clear_batch_exception_requires_forced_leave(self, reservation: dict) -> bool:
        if reservation.get("pane_touched"):
            return True
        phase = str(reservation.get("phase") or "")
        return phase not in {"", "reserved", "pending_prompt"}

    def hold_clear_reservation_for_batch_failure(self, reservation: dict | None, reason: str) -> None:
        if reservation is None:
            return
        reservation["phase"] = "batch_hold_failed"
        reservation["batch_hold_complete"] = True
        reservation["batch_hold_failure_reason"] = reason

    def handle_clear_peer(self, sender: str, target: str, *, force: bool) -> dict:
        if not target:
            return {"ok": False, "error": "target required"}
        if target in self.clear_reservations:
            return {"ok": False, "error": "clear_already_pending", "error_kind": "clear_already_pending"}
        if sender == target and sender != "bridge":
            try:
                with self.command_state_lock(command_class="clear_peer"):
                    if not self.command_deadline_ok(
                        post_lock_worst_case=COMMAND_DEFAULT_POST_LOCK_WORST_CASE_SECONDS,
                        margin=COMMAND_SAFETY_MARGIN_SECONDS,
                        command_class="clear_peer",
                    ):
                        return self.lock_wait_exceeded_response("clear_peer")
                    if target in self.clear_reservations or target in self.pending_self_clears:
                        return {"ok": False, "error": "clear_already_pending", "error_kind": "clear_already_pending"}
                    self.pending_self_clears[target] = {
                        "clear_id": short_id("clear"),
                        "caller": sender,
                        "target": target,
                        "force": bool(force),
                        "created_at": utc_now(),
                        "created_ts": time.time(),
                    }
            except CommandLockWaitExceeded:
                return self.lock_wait_exceeded_response("clear_peer")
            self.log("self_clear_deferred", target=target, by_sender=sender, force=bool(force))
            return {"ok": True, "deferred": True, "target": target}
        return self.run_clear_peer(sender, target, force=force)

    def handle_clear_peers(self, sender: str, targets: list[str], *, force: bool) -> dict:
        validation = self.validate_clear_targets_payload(sender, targets, force=force)
        if not validation.get("ok"):
            return validation
        targets = list(validation.get("targets") or [])
        if len(targets) == 1:
            return self.handle_clear_peer(sender, targets[0], force=force)

        reservations: dict[str, dict] = {}
        try:
            with self.command_state_lock(
                post_lock_worst_case=self.clear_peer_batch_post_lock_worst_case_seconds(len(targets)),
                margin=20.0,
                command_class="clear_peer",
            ):
                if not self.command_deadline_ok(
                    post_lock_worst_case=self.clear_peer_batch_post_lock_worst_case_seconds(len(targets)),
                    margin=20.0,
                    command_class="clear_peer",
                ):
                    return self.lock_wait_exceeded_response("clear_peer")
                self._reload_participants_unlocked()
                validation = self.validate_clear_targets_payload(sender, targets, force=force)
                if not validation.get("ok"):
                    return validation
                targets = list(validation.get("targets") or [])
                queue_snapshot = list(self.queue.read())
                aggregates = self.aggregates.read_aggregates()
                guard = self.clear_guard_multi(
                    targets,
                    force=False,
                    queue_snapshot=queue_snapshot,
                    aggregates=aggregates,
                )
                if not guard.ok:
                    return {
                        "ok": False,
                        "error": format_clear_guard_result(
                            guard,
                            suppress_force_hint=True,
                            include_targets=True,
                        ),
                        "error_kind": "clear_blocked",
                        "hard_blockers": [v.__dict__ for v in guard.hard_blockers],
                        "soft_blockers": [v.__dict__ for v in guard.soft_blockers],
                    }
                for target in targets:
                    participant = dict(self.participants.get(target) or {})
                    reservation = self._new_clear_reservation(sender, target, force=False, participant=participant)
                    reservations[target] = reservation
                    self.clear_reservations[target] = reservation
                self.log("clear_peer_batch_reserved", by_sender=sender, targets=targets)
        except CommandLockWaitExceeded:
            return self.lock_wait_exceeded_response("clear_peer")

        results: list[dict] = []
        try:
            for target in targets:
                reservation = reservations.get(target) or {}
                try:
                    result = self.run_clear_peer(
                        sender,
                        target,
                        force=False,
                        existing_reservation=reservation,
                        hold_reservation_after_success=True,
                    )
                except Exception as exc:
                    self.safe_log("clear_peer_batch_target_exception", target=target, by_sender=sender, error=str(exc))
                    if self.clear_batch_exception_requires_forced_leave(reservation):
                        self.force_leave_after_clear_failure(
                            target,
                            caller=sender,
                            reason=f"batch_exception:{exc}",
                            reservation=reservation,
                        )
                        result = {
                            "ok": False,
                            "error": str(exc),
                            "error_kind": "exception",
                            "forced_leave": True,
                            "target": target,
                        }
                    else:
                        result = {"ok": False, "error": str(exc), "error_kind": "exception", "target": target}
                        with self.state_lock:
                            if self.clear_reservations.get(target) is reservation:
                                marker_id_value = str(reservation.get("identity_marker_id") or "")
                                if marker_id_value:
                                    remove_marker(marker_id_value)
                                self.clear_reservations.pop(target, None)
                results.append(self.normalize_clear_batch_result(target, result if isinstance(result, dict) else {}))
        finally:
            released_targets: list[str] = []
            with self.state_lock:
                for target, reservation in reservations.items():
                    if self.clear_reservations.get(target) is reservation:
                        marker_id_value = str(reservation.get("identity_marker_id") or "")
                        if marker_id_value:
                            remove_marker(marker_id_value)
                        self.clear_reservations.pop(target, None)
                        released_targets.append(target)
                        self.log("clear_peer_batch_reservation_released", target=target, by_sender=sender)
            for target in released_targets:
                self.try_deliver_command_aware(target)

        summary = self.clear_batch_summary(results)
        self.log("clear_peer_batch_completed", by_sender=sender, targets=targets, summary=summary)
        return {"ok": True, "targets": targets, "results": results, "summary": summary}

    def _new_clear_reservation(self, caller: str, target: str, *, force: bool, participant: dict, pane: str = "") -> dict:
        clear_id = short_id("clear")
        probe_id = f"{self.bridge_session}:{target}:{uuid.uuid4().hex[:10]}"
        return {
            "clear_id": clear_id,
            "caller": caller,
            "target": target,
            "force": bool(force),
            "probe_id": probe_id,
            "old_session_id": str(participant.get("hook_session_id") or ""),
            "old_turn_id_hint": "",
            "deadline_ts": time.time() + CLEAR_PROBE_TIMEOUT_SECONDS,
            "phase": "reserved",
            "pane_touched": False,
            "probe_prompt_submitted": False,
            "probe_turn_id": "",
            "new_session_id": "",
            "identity_marker_id": "",
            "pane": pane,
            "agent": str(participant.get("agent_type") or ""),
            "condition": threading.Condition(self.state_lock),
        }

    def _write_clear_marker_locked(self, reservation: dict, participant: dict, pane: str) -> str:
        marker_ttl_seconds = ttl_for_clear_lifetime(
            settle_delay_seconds=self.clear_post_clear_delay_seconds,
            probe_timeout_seconds=CLEAR_PROBE_TIMEOUT_SECONDS,
        )
        marker = make_marker(
            bridge_session=self.bridge_session,
            alias=str(reservation.get("target") or ""),
            agent=str(participant.get("agent_type") or ""),
            old_session_id=str(reservation.get("old_session_id") or ""),
            probe_id=str(reservation.get("probe_id") or ""),
            pane=pane,
            target=str(participant.get("target") or pane),
            events_file=str(self.state_file),
            public_events_file=str(self.public_state_file or ""),
            caller=str(reservation.get("caller") or ""),
            clear_id=str(reservation.get("clear_id") or ""),
            ttl_seconds=marker_ttl_seconds,
        )
        marker_id_value = write_marker(marker)
        reservation["identity_marker_id"] = marker_id_value
        reservation["phase"] = "pending_prompt"
        return marker_id_value

    def _clear_tmux_send(self, pane: str, text: str, *, target: str, message_id: str) -> dict:
        if self.dry_run:
            return {"ok": True, "pane_touched": True, "error": ""}
        return run_tmux_send_literal_touch_result(
            pane,
            text,
            bridge_session=self.bridge_session,
            target_alias=target,
            message_id=message_id,
            nonce=message_id,
        )

    def _clear_probe_text(self, target: str, probe_id: str) -> str:
        return probe_prompt("clear", probe_id, target, format_peer_summary(self.session_state))

    def _wait_for_clear_settle_locked(self, reservation: dict) -> None:
        delay = max(0.0, float(self.clear_post_clear_delay_seconds or 0.0))
        reservation["phase"] = "post_clear_settle"
        reservation["post_clear_settle_delay_sec"] = delay
        self.log(
            "clear_post_clear_settle",
            target=reservation.get("target"),
            by_sender=reservation.get("caller"),
            delay_sec=delay,
        )
        if delay <= 0:
            return
        condition = reservation.get("condition")
        if not isinstance(condition, threading.Condition):
            return
        deadline = time.time() + delay
        while not reservation.get("failure_reason"):
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            condition.wait(min(remaining, 0.25))

    def clear_process_identity_for_replacement(self, *, agent_type: str, session_id: str, pane: str) -> dict:
        if not (agent_type and session_id and pane):
            return {}
        live = read_live_by_pane(pane)
        if live_record_matches(live, agent_type, session_id):
            identity = verified_process_identity(live)
            if identity:
                return identity
        try:
            probed = probe_agent_process(pane, agent_type)
        except Exception as exc:
            self.safe_log(
                "clear_process_identity_probe_failed",
                agent_type=agent_type,
                session_id=session_id,
                pane=pane,
                error=str(exc),
            )
            return {}
        if str(probed.get("status") or "") == "verified":
            return dict(probed)
        self.log(
            "clear_process_identity_unverified",
            agent_type=agent_type,
            session_id=session_id,
            pane=pane,
            probe_status=probed.get("status"),
            reason=probed.get("reason"),
        )
        return {}

    def run_clear_peer(
        self,
        caller: str,
        target: str,
        *,
        force: bool,
        existing_reservation: dict | None = None,
        hold_reservation_after_success: bool = False,
    ) -> dict:
        reservation: dict | None = existing_reservation
        participant: dict = {}
        endpoint_detail: dict = {}
        try:
            state_lock_ctx = self.command_state_lock(
                post_lock_worst_case=self.clear_peer_post_lock_worst_case_seconds(),
                margin=20.0,
                command_class="clear_peer",
            )
            state_lock_ctx.__enter__()
        except CommandLockWaitExceeded:
            if hold_reservation_after_success:
                self.hold_clear_reservation_for_batch_failure(reservation, "lock_wait_exceeded")
            return self.lock_wait_exceeded_response("clear_peer")
        try:
            if not self.command_deadline_ok(
                post_lock_worst_case=self.clear_peer_post_lock_worst_case_seconds(),
                margin=20.0,
                command_class="clear_peer",
            ):
                if hold_reservation_after_success:
                    self.hold_clear_reservation_for_batch_failure(reservation, "lock_wait_exceeded")
                return self.lock_wait_exceeded_response("clear_peer")
            self._reload_participants_unlocked()
            if target not in self.participants:
                if hold_reservation_after_success:
                    self.hold_clear_reservation_for_batch_failure(reservation, "target_inactive")
                return {"ok": False, "error": f"target {target!r} is not an active participant"}
            participant = dict(self.participants.get(target) or {})
            if reservation is None:
                guard = self.clear_guard(target, force=force)
                if not guard.ok:
                    return {
                        "ok": False,
                        "error": format_clear_guard_result(guard),
                        "error_kind": "clear_blocked",
                        "hard_blockers": [v.__dict__ for v in guard.hard_blockers],
                        "soft_blockers": [v.__dict__ for v in guard.soft_blockers],
                    }
                reservation = self._new_clear_reservation(caller, target, force=force, participant=participant)
                self.clear_reservations[target] = reservation
            else:
                self.clear_reservations[target] = reservation
                reservation.setdefault("condition", threading.Condition(self.state_lock))
                reservation.setdefault("deadline_ts", time.time() + CLEAR_PROBE_TIMEOUT_SECONDS)
                reservation.setdefault("phase", "reserved")
                reservation.setdefault("force", bool(force))

            endpoint_detail = self.resolve_endpoint_detail(target, purpose="write")
            pane = str(endpoint_detail.get("pane") or "") if endpoint_detail.get("ok") else ""
            if not pane:
                if hold_reservation_after_success:
                    self.hold_clear_reservation_for_batch_failure(reservation, "endpoint_lost")
                else:
                    self.clear_reservations.pop(target, None)
                    marker_id_existing = str(reservation.get("identity_marker_id") or "")
                    if marker_id_existing:
                        remove_marker(marker_id_existing)
                return {"ok": False, "error": str(endpoint_detail.get("reason") or "endpoint_lost"), "error_kind": "endpoint_lost", "target": target}
            reservation["pane"] = pane
            mode_status = self.pane_mode_status(pane)
            if mode_status.get("error") or mode_status.get("in_mode"):
                if hold_reservation_after_success:
                    self.hold_clear_reservation_for_batch_failure(reservation, "pane_not_ready")
                else:
                    self.clear_reservations.pop(target, None)
                    marker_id_existing = str(reservation.get("identity_marker_id") or "")
                    if marker_id_existing:
                        remove_marker(marker_id_existing)
                return {"ok": False, "error": str(mode_status.get("error") or "pane_in_mode"), "error_kind": "pane_not_ready", "target": target}

            marker_id_value = self._write_clear_marker_locked(reservation, participant, pane)
            reservation["phase"] = "writing_clear"
            clear_status = self._clear_tmux_send(pane, "/clear", target=target, message_id=str(reservation.get("clear_id") or "clear"))
            reservation["pane_touched"] = bool(clear_status.get("pane_touched"))
            if not clear_status.get("ok"):
                if reservation.get("pane_touched"):
                    self.force_leave_after_clear_failure(target, caller=caller, reason=f"clear_write_failed:{clear_status.get('error')}", reservation=reservation)
                else:
                    if hold_reservation_after_success:
                        self.hold_clear_reservation_for_batch_failure(reservation, "clear_write_failed")
                    else:
                        self.clear_reservations.pop(target, None)
                        remove_marker(marker_id_value)
                return {
                    "ok": False,
                    "error": str(clear_status.get("error") or "clear_write_failed"),
                    "error_kind": "clear_write_failed",
                    "pane_touched": bool(clear_status.get("pane_touched")),
                    "forced_leave": bool(reservation.get("forced_leave")),
                    "target": target,
                }

            self._wait_for_clear_settle_locked(reservation)
            if reservation.get("failure_reason"):
                return {
                    "ok": False,
                    "error": str(reservation.get("failure_reason") or "clear_failed"),
                    "error_kind": str(reservation.get("failure_reason") or "clear_failed"),
                    "forced_leave": bool(reservation.get("forced_leave")),
                    "target": target,
                }
            settle_mode_status = self.pane_mode_status(pane)
            if settle_mode_status.get("error") or settle_mode_status.get("in_mode"):
                reason_detail = str(settle_mode_status.get("error") or settle_mode_status.get("mode") or "pane_in_mode")
                reason = f"clear_settle_pane_not_ready:{reason_detail}"
                self.force_leave_after_clear_failure(target, caller=caller, reason=reason, reservation=reservation)
                return {"ok": False, "error": reason, "error_kind": "clear_settle_pane_not_ready", "forced_leave": True, "target": target}

            if force:
                try:
                    reservation["force_invalidation"] = self.apply_force_clear_invalidation(target, caller)
                except Exception as exc:
                    self.safe_log("clear_force_invalidation_failed", target=target, by_sender=caller, error=str(exc))
                    self.force_leave_after_clear_failure(target, caller=caller, reason=f"force_invalidation_failed:{exc}", reservation=reservation)
                    return {"ok": False, "error": str(exc), "error_kind": "force_invalidation_failed", "forced_leave": True, "target": target}

            probe_text = self._clear_probe_text(target, str(reservation.get("probe_id") or ""))
            reservation["phase"] = "writing_probe"
            probe_status = self._clear_tmux_send(pane, probe_text, target=target, message_id=str(reservation.get("probe_id") or "probe"))
            if not probe_status.get("ok"):
                self.force_leave_after_clear_failure(target, caller=caller, reason=f"probe_write_failed:{probe_status.get('error')}", reservation=reservation)
                return {"ok": False, "error": str(probe_status.get("error") or "probe_write_failed"), "error_kind": "probe_write_failed", "forced_leave": True, "target": target}
            reservation["phase"] = "waiting_probe"
            reservation["deadline_ts"] = time.time() + CLEAR_PROBE_TIMEOUT_SECONDS
            self.log("clear_probe_sent", target=target, by_sender=caller, probe_id=reservation.get("probe_id"))

            if self.dry_run:
                reservation["phase"] = "probe_finished"
                reservation["probe_prompt_submitted"] = True
                reservation["probe_response_finished"] = True
                reservation["new_session_id"] = reservation.get("old_session_id") or "dry-run-session"

            condition = reservation["condition"]
            while not reservation.get("probe_response_finished") and not reservation.get("failure_reason"):
                remaining = float(reservation.get("deadline_ts") or time.time()) - time.time()
                if remaining <= 0:
                    break
                condition.wait(min(remaining, 1.0))
            failure_reason = str(reservation.get("failure_reason") or "")
            if failure_reason:
                return {"ok": False, "error": failure_reason, "error_kind": failure_reason, "forced_leave": bool(reservation.get("forced_leave")), "target": target}
            if not reservation.get("probe_response_finished"):
                self.force_leave_after_clear_failure(target, caller=caller, reason="probe_timeout", reservation=reservation)
                return {"ok": False, "error": "probe_timeout", "error_kind": "probe_timeout", "forced_leave": True, "target": target}
            reservation["phase"] = "finalizing"
            marker_id_value = str(reservation.get("identity_marker_id") or "")
            if marker_id_value:
                update_marker(marker_id_value, lambda current: {**current, "phase": "finalizing"})
        finally:
            state_lock_ctx.__exit__(None, None, None)

        agent_type = str(participant.get("agent_type") or reservation.get("agent") or "")
        new_session_id = str(reservation.get("new_session_id") or "")
        pane = str(reservation.get("pane") or "")
        identity_required = not (self.dry_run and not str(reservation.get("old_session_id") or ""))
        process_identity = (
            self.clear_process_identity_for_replacement(
                agent_type=agent_type,
                session_id=new_session_id,
                pane=pane,
            )
            if identity_required
            else {}
        )

        if not identity_required:
            helper_result = {"ok": True, "dry_run": True}
        else:
            helper_result = replace_attached_session_identity_for_clear(
                bridge_session=self.bridge_session,
                alias=target,
                agent_type=agent_type,
                old_session_id=str(reservation.get("old_session_id") or ""),
                new_session_id=new_session_id,
                pane=pane,
                target=str(participant.get("target") or endpoint_detail.get("target") or ""),
                cwd=str(participant.get("cwd") or ""),
                model=str(participant.get("model") or ""),
                process_identity=process_identity,
            )

        try:
            final_lock_ctx = self.command_state_lock(
                post_lock_worst_case=COMMAND_DEFAULT_POST_LOCK_WORST_CASE_SECONDS,
                margin=COMMAND_SAFETY_MARGIN_SECONDS,
                command_class="clear_peer",
            )
            final_lock_ctx.__enter__()
        except CommandLockWaitExceeded:
            self.force_leave_after_clear_failure(target, caller=caller, reason="lock_wait_exceeded_after_identity", reservation=reservation)
            response = self.lock_wait_exceeded_response("clear_peer")
            response.update({"forced_leave": True, "target": target})
            return response
        try:
            if not helper_result.get("ok"):
                self.force_leave_after_clear_failure(target, caller=caller, reason=f"identity_replace_failed:{helper_result.get('error')}", reservation=reservation)
                return {
                    "ok": False,
                    "error": str(helper_result.get("error") or "identity_replace_failed"),
                    "error_kind": "identity_replace_failed",
                    "forced_leave": True,
                    "target": target,
                }
            if identity_required and not verified_process_identity(helper_result.get("live_record") or {}):
                self.force_leave_after_clear_failure(target, caller=caller, reason="clear_process_identity_unverified", reservation=reservation)
                return {
                    "ok": False,
                    "error": "clear_process_identity_unverified",
                    "error_kind": "clear_process_identity_unverified",
                    "forced_leave": True,
                    "target": target,
                }
            marker_id_value = str(reservation.get("identity_marker_id") or "")
            if marker_id_value:
                remove_marker(marker_id_value)
                reservation["identity_marker_id"] = ""
            if hold_reservation_after_success:
                reservation["phase"] = "batch_hold"
                reservation["batch_hold_complete"] = True
            else:
                self.clear_reservations.pop(target, None)
            self.pending_self_clears.pop(target, None)
            self.busy[target] = False
            self.reserved[target] = None
            self.current_prompt_by_agent.pop(target, None)
            self.session_mtime_ns = None
            self._reload_participants_unlocked()
            self.log(
                "clear_peer_completed",
                target=target,
                by_sender=caller,
                force=bool(force),
                probe_id=reservation.get("probe_id"),
                new_session_id=reservation.get("new_session_id"),
            )
        finally:
            final_lock_ctx.__exit__(None, None, None)
        if not hold_reservation_after_success:
            self.try_deliver_command_aware(target)
        return {"ok": True, "target": target, "cleared": True, "force": bool(force), "new_session_id": reservation.get("new_session_id")}

    def handle_clear_prompt_submitted_locked(self, agent: str, record: dict) -> bool:
        reservation = self.clear_reservations.get(agent)
        if not reservation:
            return False
        attach_probe = str(record.get("attach_probe") or "")
        if attach_probe and attach_probe == str(reservation.get("probe_id") or ""):
            marker_id_value = str(reservation.get("identity_marker_id") or "")
            marker = (read_markers().get("markers") or {}).get(marker_id_value) if marker_id_value else None
            marker = marker if isinstance(marker, dict) else {}
            marker_new_session = str(marker.get("new_session_id") or "")
            marker_turn_id = str(marker.get("probe_turn_id") or "")
            record_turn_id = str(record.get("turn_id") or "")
            phase_ok = str(marker.get("phase") or "") == "prompt_seen"
            if marker_turn_id:
                turn_ids_compatible = not record_turn_id or marker_turn_id == record_turn_id
            else:
                turn_ids_compatible = not record_turn_id
            if not (phase_ok and marker_new_session and turn_ids_compatible):
                reservation["phase"] = "failed"
                reservation["failure_reason"] = "clear_marker_phase2_missing"
                condition = reservation.get("condition")
                if isinstance(condition, threading.Condition):
                    condition.notify_all()
                self.log(
                    "clear_marker_phase2_missing",
                    target=agent,
                    probe_id=reservation.get("probe_id"),
                    marker_id=marker_id_value,
                    marker_phase=marker.get("phase"),
                    marker_new_session_present=bool(marker_new_session),
                    marker_probe_turn_id=marker_turn_id,
                    record_turn_id=record_turn_id,
                )
                self.busy[agent] = False
                self.reserved[agent] = None
                if reservation.get("pane_touched"):
                    self.force_leave_after_clear_failure(
                        agent,
                        caller=str(reservation.get("caller") or "bridge"),
                        reason="clear_marker_phase2_missing",
                        reservation=reservation,
                    )
                return True
            reservation["new_session_id"] = marker_new_session
            reservation["probe_turn_id"] = marker_turn_id
            reservation["probe_prompt_submitted"] = True
            reservation["phase"] = "prompt_seen"
            self.log(
                "clear_probe_prompt_seen",
                target=agent,
                probe_id=reservation.get("probe_id"),
                new_session_id=reservation.get("new_session_id"),
                probe_turn_id=reservation.get("probe_turn_id"),
            )
        else:
            self.log("clear_window_prompt_suppressed", target=agent, attach_probe=attach_probe, turn_id=record.get("turn_id"))
        self.busy[agent] = False
        self.reserved[agent] = None
        return True

    def handle_clear_response_finished_locked(self, agent: str, record: dict) -> bool:
        reservation = self.clear_reservations.get(agent)
        if not reservation:
            return False
        response_turn_id = str(record.get("turn_id") or "")
        response_session_id = str(record.get("session_id") or "")
        probe_turn_id = str(reservation.get("probe_turn_id") or "")
        new_session_id = str(reservation.get("new_session_id") or "")
        matches_probe = bool(
            (probe_turn_id and response_turn_id == probe_turn_id)
            or (new_session_id and response_session_id == new_session_id)
        )
        if matches_probe or (self.dry_run and reservation.get("probe_prompt_submitted")):
            reservation["probe_response_finished"] = True
            reservation["phase"] = "probe_finished"
            if response_session_id:
                reservation["new_session_id"] = response_session_id
            if response_turn_id:
                reservation["probe_turn_id"] = response_turn_id
            condition = reservation.get("condition")
            if isinstance(condition, threading.Condition):
                condition.notify_all()
            self.log(
                "clear_probe_response_finished",
                target=agent,
                probe_id=reservation.get("probe_id"),
                new_session_id=reservation.get("new_session_id"),
                probe_turn_id=reservation.get("probe_turn_id"),
            )
        else:
            self.log(
                "clear_window_response_ignored",
                target=agent,
                response_turn_id=response_turn_id,
                response_session_id=response_session_id,
                probe_turn_id=probe_turn_id,
                new_session_id=new_session_id,
            )
        self.busy[agent] = False
        self.reserved[agent] = None
        return True

    def promote_pending_self_clear_locked(self, sender: str) -> None:
        pending = self.pending_self_clears.pop(sender, None)
        if not pending:
            return
        force = bool(pending.get("force"))
        self.reload_participants()
        if sender not in self.participants:
            return
        guard = self.clear_guard(sender, force=force)
        if not guard.ok:
            notice = make_message(
                sender="bridge",
                target=sender,
                intent="self_clear_cancelled",
                body=f"[bridge:self_clear_cancelled] Deferred self-clear did not run: {format_clear_guard_result(guard)}.",
                causal_id=short_id("causal"),
                hop_count=0,
                auto_return=False,
                kind="notice",
                source="self_clear_cancelled",
            )

            def prepend(queue: list[dict]) -> None:
                queue.insert(0, notice)

            self.queue.update(prepend)
            self.log("self_clear_cancelled", target=sender, reason=format_clear_guard_result(guard))
            return
        participant = dict(self.participants.get(sender) or {})
        reservation = self._new_clear_reservation(str(pending.get("caller") or sender), sender, force=force, participant=participant)
        reservation["clear_id"] = str(pending.get("clear_id") or reservation.get("clear_id"))
        self.clear_reservations[sender] = reservation
        self.log("self_clear_promoted", target=sender, force=force, clear_id=reservation.get("clear_id"))
        threading.Thread(
            target=self._self_clear_worker,
            args=(sender, reservation),
            name=f"bridge-self-clear-{sender}",
            daemon=True,
        ).start()

    def _self_clear_worker(self, target: str, reservation: dict) -> None:
        try:
            self.run_clear_peer(
                str(reservation.get("caller") or target),
                target,
                force=bool(reservation.get("force")),
                existing_reservation=reservation,
            )
        except Exception as exc:
            self.safe_log("self_clear_worker_failed", target=target, error=str(exc))

    def enqueue_ipc_message(self, message: dict) -> bool:
        # Daemon-socket ingress for an externally-originated message
        # (op=enqueue from bridge_enqueue.py). Append the message to the
        # queue, then run the unified alarm-cancel-on-incoming step
        # against the now-queued item. The same step is invoked by
        # handle_external_message_queued for the file-fallback ingress
        # path, so the alarm-cancel semantics is defined in exactly one
        # place (_apply_alarm_cancel_to_queued_message). Request watchdogs
        # arm later in two phases: delivery at pending->inflight reservation,
        # then response at inflight->delivered prompt submission.
        with self.state_lock:
            sender = str(message.get("from") or "")
            if self.sender_blocked_by_clear(sender):
                self.log(
                    "message_enqueue_rejected_sender_clear_blocked",
                    message_id=message.get("id"),
                    from_agent=sender,
                    to=message.get("to"),
                )
                return False
            if any(it.get("id") == message["id"] for it in self.queue.read()):
                self.log("message_enqueue_skipped_duplicate", message_id=message["id"], from_agent=message.get("from"), to=message.get("to"))
                return False
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
            return True

    def _maybe_cancel_alarms_for_incoming(self, message: dict) -> None:
        return daemon_watchdogs._maybe_cancel_alarms_for_incoming(self, message)

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
        command = str(self.command_context_info().get("op") or "reload_participants")
        with self.command_state_lock(command_class=command):
            if self.command_context_info() and not self.command_deadline_ok(
                post_lock_worst_case=self.command_budget(command)[0],
                margin=self.command_budget(command)[1],
                command_class=command,
            ):
                raise CommandLockWaitExceeded(command)
            self._reload_participants_unlocked()

    def _reload_participants_unlocked(self) -> None:
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
        return daemon_events.find_inflight_candidate(self, agent)

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
        return daemon_delivery.next_pending_candidate(self, target)

    def annotate_pending_pane_mode_block(self, target: str, message_id: str, mode: str) -> dict | None:
        return daemon_delivery.annotate_pending_pane_mode_block(self, target, message_id, mode)

    def clear_pane_mode_metadata(self, message_id: str) -> dict | None:
        return daemon_delivery.clear_pane_mode_metadata(self, message_id)

    def mark_pane_mode_unforceable(self, message_id: str) -> bool:
        return daemon_delivery.mark_pane_mode_unforceable(self, message_id)

    def mark_pane_mode_cancel_failed(self, message_id: str, error: str) -> bool:
        return daemon_delivery.mark_pane_mode_cancel_failed(self, message_id, error)

    def mark_pane_mode_probe_failed(self, message_id: str, error: str) -> bool:
        return daemon_delivery.mark_pane_mode_probe_failed(self, message_id, error)

    def defer_inflight_for_pane_mode_probe_failed(self, message: dict, error: str) -> dict | None:
        return daemon_delivery.defer_inflight_for_pane_mode_probe_failed(self, message, error)

    def blocked_duration(self, item: dict | None) -> float | None:
        return daemon_delivery.blocked_duration(self, item)

    def maybe_defer_for_pane_mode(self, target: str, pane: str, message: dict) -> bool:
        return daemon_delivery.maybe_defer_for_pane_mode(self, target, pane, message)

    def defer_inflight_for_pane_mode(self, message: dict, mode: str) -> dict | None:
        return daemon_delivery.defer_inflight_for_pane_mode(self, message, mode)

    def mark_enter_deferred_for_pane_mode(self, message_id: str, target: str, mode: str, error: str = "") -> dict | None:
        return daemon_delivery.mark_enter_deferred_for_pane_mode(self, message_id, target, mode, error=error)

    def clear_enter_deferred_metadata(self, message_id: str) -> None:
        return daemon_delivery.clear_enter_deferred_metadata(self, message_id)

    def _peer_result_redirect_os_reason(self, exc: OSError) -> str:
        if exc.errno in {errno.EACCES, errno.EPERM}:
            return "permission_denied"
        if exc.errno == errno.ENOSPC:
            return "no_space"
        if exc.errno == errno.EEXIST:
            return "collision"
        if exc.errno == errno.ELOOP:
            return "symlink_unsafe"
        if exc.errno in {errno.ENOTDIR, errno.EISDIR}:
            return "unsafe_path"
        return "write_failed"

    def _peer_result_redirect_dir(self) -> tuple[Path, int]:
        root = Path(SHARED_PAYLOAD_ROOT)
        redirect_dir = root / PEER_RESULT_REDIRECT_DIRNAME
        uid = os.getuid()
        try:
            os.mkdir(root, 0o1777)
            os.chmod(root, 0o1777)
        except FileExistsError:
            pass
        except OSError as exc:
            raise PeerResultRedirectError(self._peer_result_redirect_os_reason(exc), f"mkdir {root}: {exc}") from exc

        try:
            root_stat = os.lstat(root)
        except OSError as exc:
            raise PeerResultRedirectError(self._peer_result_redirect_os_reason(exc), f"stat {root}: {exc}") from exc
        root_mode = stat.S_IMODE(root_stat.st_mode)
        if not stat.S_ISDIR(root_stat.st_mode):
            raise PeerResultRedirectError("unsafe_path", f"{root} is not a directory")
        if root_stat.st_uid not in {uid, 0}:
            raise PeerResultRedirectError("permission_denied", f"{root} owner uid is {root_stat.st_uid}")
        if root_mode & 0o022 and not root_mode & stat.S_ISVTX:
            raise PeerResultRedirectError("permission_denied", f"{root} is writable by other users without sticky bit")

        try:
            os.mkdir(redirect_dir, 0o700)
        except FileExistsError:
            pass
        except OSError as exc:
            raise PeerResultRedirectError(self._peer_result_redirect_os_reason(exc), f"mkdir {redirect_dir}: {exc}") from exc

        try:
            dir_stat = os.lstat(redirect_dir)
        except OSError as exc:
            raise PeerResultRedirectError(self._peer_result_redirect_os_reason(exc), f"stat {redirect_dir}: {exc}") from exc
        if not stat.S_ISDIR(dir_stat.st_mode):
            raise PeerResultRedirectError("symlink_unsafe" if stat.S_ISLNK(dir_stat.st_mode) else "unsafe_path", f"{redirect_dir} is not a safe directory")
        if dir_stat.st_uid != uid:
            raise PeerResultRedirectError("permission_denied", f"{redirect_dir} owner uid is {dir_stat.st_uid}")
        if stat.S_IMODE(dir_stat.st_mode) & 0o077:
            try:
                os.chmod(redirect_dir, 0o700)
                dir_stat = os.lstat(redirect_dir)
            except OSError as exc:
                raise PeerResultRedirectError(self._peer_result_redirect_os_reason(exc), f"chmod {redirect_dir}: {exc}") from exc
            if stat.S_IMODE(dir_stat.st_mode) & 0o077:
                raise PeerResultRedirectError("permission_denied", f"{redirect_dir} is not private")

        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            dir_fd = os.open(redirect_dir, flags)
        except OSError as exc:
            raise PeerResultRedirectError(self._peer_result_redirect_os_reason(exc), f"open {redirect_dir}: {exc}") from exc
        fd_stat = os.fstat(dir_fd)
        if not stat.S_ISDIR(fd_stat.st_mode) or fd_stat.st_uid != uid or stat.S_IMODE(fd_stat.st_mode) & 0o077:
            os.close(dir_fd)
            raise PeerResultRedirectError("permission_denied", f"{redirect_dir} failed private directory verification")
        return redirect_dir, dir_fd

    def _peer_result_redirect_filename(self, message_id: str) -> tuple[str, bool]:
        if PEER_RESULT_FILE_ID_RE.fullmatch(message_id):
            return f"reply-{message_id}.txt", False
        return f"{short_id('reply')}.txt", True

    def _build_peer_result_redirect_wrapper(self, *, total_chars: int, path: Path, normalized_body: str) -> tuple[str, int]:
        preview_chars = min(PEER_RESULT_REDIRECT_PREVIEW_CHARS, len(normalized_body))
        while True:
            preview = json.dumps(normalized_body[:preview_chars], ensure_ascii=True)
            wrapper = (
                f"[bridge:body_redirected] Full reply ({total_chars} chars) saved to file. "
                "Read this file; the preview is intentionally truncated.\n"
                f"File: {path}\n"
                f"Preview: {preview}"
            )
            if len(wrapper) <= PEER_RESULT_REDIRECT_WRAPPER_MAX_CHARS:
                return wrapper, preview_chars
            if preview_chars <= 0:
                raise PeerResultRedirectError("wrapper_too_large", f"wrapper would exceed {PEER_RESULT_REDIRECT_WRAPPER_MAX_CHARS} chars")
            preview_chars = max(0, preview_chars - 10)

    def _write_peer_result_redirect_file(self, *, dir_fd: int, filename: str, normalized_body: str) -> None:
        tmp_name = f".{filename}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = -1
        try:
            fd = os.open(tmp_name, flags, 0o600, dir_fd=dir_fd)
            data = normalized_body.encode("utf-8")
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.link(tmp_name, filename, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
            os.fsync(dir_fd)
        except FileExistsError as exc:
            raise PeerResultRedirectError("collision", f"{filename} already exists") from exc
        except OSError as exc:
            raise PeerResultRedirectError(self._peer_result_redirect_os_reason(exc), f"write {filename}: {exc}") from exc
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.unlink(tmp_name, dir_fd=dir_fd)
                try:
                    os.fsync(dir_fd)
                except OSError:
                    pass
            except FileNotFoundError:
                pass
            except OSError:
                pass

    def _build_peer_result_redirect_failure_body(self, normalized_body: str, reason: str) -> str:
        return (
            f"[bridge:body_redirect_failed] Oversized reply ({len(normalized_body)} chars) could not be saved to file; "
            f"reason={reason}. Delivering truncated inline fallback.\n"
            f"{normalized_body}"
        )

    def redirect_oversized_result_body(self, message: dict) -> None:
        if normalize_kind(message.get("kind"), "") != "result":
            return
        normalized_body = normalize_prompt_body_text(str(message.get("body") or ""))
        if len(normalized_body) <= MAX_PEER_BODY_CHARS:
            return

        message_id = str(message.get("id") or "")
        filename, sanitized = self._peer_result_redirect_filename(message_id)
        dir_fd: int | None = None
        redirect_path: Path | None = None
        try:
            redirect_dir, dir_fd = self._peer_result_redirect_dir()
            redirect_path = redirect_dir / filename
            wrapper, preview_chars = self._build_peer_result_redirect_wrapper(
                total_chars=len(normalized_body),
                path=redirect_path,
                normalized_body=normalized_body,
            )
            self._write_peer_result_redirect_file(
                dir_fd=dir_fd,
                filename=filename,
                normalized_body=normalized_body,
            )
            message["body"] = wrapper
            if sanitized:
                self.log(
                    "body_redirect_message_id_sanitized",
                    message_id=message_id,
                    redirect_file=str(redirect_path),
                )
            self.log(
                "body_redirected",
                message_id=message_id,
                from_agent=message.get("from"),
                to=message.get("to"),
                kind=message.get("kind"),
                intent=message.get("intent"),
                source=message.get("source"),
                normalized_chars=len(normalized_body),
                limit_chars=MAX_PEER_BODY_CHARS,
                redirect_file=str(redirect_path),
                preview_chars=preview_chars,
            )
        except PeerResultRedirectError as exc:
            message["body"] = self._build_peer_result_redirect_failure_body(normalized_body, exc.reason)
            self.safe_log(
                "body_redirect_failed",
                message_id=message_id,
                from_agent=message.get("from"),
                to=message.get("to"),
                kind=message.get("kind"),
                intent=message.get("intent"),
                source=message.get("source"),
                normalized_chars=len(normalized_body),
                limit_chars=MAX_PEER_BODY_CHARS,
                reason=exc.reason,
                detail=exc.detail,
                redirect_file=str(redirect_path or ""),
            )
        finally:
            if dir_fd is not None:
                try:
                    os.close(dir_fd)
                except OSError:
                    pass

    def queue_message(self, message: dict, log_event: bool = True, deliver: bool = True) -> None:
        return daemon_delivery.queue_message(self, message, log_event=log_event, deliver=deliver)

    def suppress_pending_watchdog_wakes(
        self,
        *,
        ref_message_id: str | None = None,
        ref_aggregate_id: str | None = None,
        reason: str,
    ) -> list[dict]:
        return daemon_watchdogs.suppress_pending_watchdog_wakes(
            self,
            ref_message_id=ref_message_id,
            ref_aggregate_id=ref_aggregate_id,
            reason=reason,
        )

    def _wait_status_section(self, items: list[dict], *, limit: int = WAIT_STATUS_SECTION_LIMIT) -> dict:
        return daemon_status._wait_status_section(self, items, limit=limit)

    def _wait_status_deadline_iso(self, deadline: object) -> str:
        return daemon_status._wait_status_deadline_iso(self, deadline)

    def _wait_status_message_watchdog_index(self, caller: str, watchdogs: dict[str, dict]) -> dict[str, list[dict]]:
        return daemon_status._wait_status_message_watchdog_index(self, caller, watchdogs)

    def _wait_status_counts(self, sections: dict[str, dict]) -> dict:
        return daemon_status._wait_status_counts(self, sections)

    def _build_outstanding_requests(self, caller: str, queue: list[dict], watchdogs_by_message: dict[str, list[dict]]) -> list[dict]:
        return daemon_status._build_outstanding_requests(self, caller, queue, watchdogs_by_message)

    def _build_wait_status_watchdogs(self, caller: str, watchdogs: dict[str, dict]) -> list[dict]:
        return daemon_status._build_wait_status_watchdogs(self, caller, watchdogs)

    def _build_wait_status_alarms(self, caller: str, watchdogs: dict[str, dict]) -> list[dict]:
        return daemon_status._build_wait_status_alarms(self, caller, watchdogs)

    def _build_wait_status_pending_inbound(self, caller: str, queue: list[dict]) -> list[dict]:
        return daemon_status._build_wait_status_pending_inbound(self, caller, queue)

    def _build_wait_status_aggregates(self, caller: str, aggregates: dict) -> list[dict]:
        return daemon_status._build_wait_status_aggregates(self, caller, aggregates)

    def build_wait_status(self, caller: str) -> dict:
        return daemon_status.build_wait_status(self, caller)

    def _aggregate_status_not_found(self, caller: str, aggregate_id: str, reason: str, **details) -> dict:
        return daemon_status._aggregate_status_not_found(self, caller, aggregate_id, reason, **details)

    def _aggregate_status_legacy_min_ts(self, values: list[object]) -> str:
        return daemon_status._aggregate_status_legacy_min_ts(self, values)

    def _aggregate_status_section(self, legs: list[dict]) -> dict:
        return daemon_status._aggregate_status_section(self, legs)

    def _aggregate_status_alias_list(self, raw: object) -> list[str]:
        return daemon_status._aggregate_status_alias_list(self, raw)

    def _aggregate_status_tombstone_for_message(
        self,
        tombstones: dict[str, list[dict]],
        message_id: str,
        caller: str,
    ) -> dict | None:
        return daemon_status._aggregate_status_tombstone_for_message(self, tombstones, message_id, caller)

    def _aggregate_terminal_status_from_reason(self, reason: str) -> str:
        return daemon_status._aggregate_terminal_status_from_reason(self, reason)

    def _aggregate_status_response_watchdog(
        self,
        caller: str,
        aggregate_id: str,
        watchdogs: dict[str, dict],
    ) -> dict | None:
        return daemon_status._aggregate_status_response_watchdog(self, caller, aggregate_id, watchdogs)

    def _aggregate_status_reply_leg(self, alias: str, message_id: str, reply: dict, tombstone: dict | None = None) -> dict:
        return daemon_status._aggregate_status_reply_leg(self, alias, message_id, reply, tombstone)

    def _aggregate_status_build_legs(
        self,
        caller: str,
        expected: list[str],
        message_ids: dict[str, str],
        replies: dict,
        rows_by_alias: dict[str, dict],
        tombstones: dict[str, list[dict]],
    ) -> list[dict]:
        return daemon_status._aggregate_status_build_legs(
            self,
            caller,
            expected,
            message_ids,
            replies,
            rows_by_alias,
            tombstones,
        )

    def build_aggregate_status(self, caller: str, aggregate_id: str) -> dict:
        return daemon_status.build_aggregate_status(self, caller, aggregate_id)

    def reserve_next(self, target: str) -> dict | None:
        return daemon_delivery.reserve_next(self, target)

    def try_deliver(self, target: str | None = None) -> None:
        return daemon_delivery.try_deliver(self, target)

    def deliver_reserved(self, message: dict) -> None:
        return daemon_delivery.deliver_reserved(self, message)

    def mark_message_pending(self, message_id: str, error: str | None = None) -> None:
        return daemon_delivery.mark_message_pending(self, message_id, error)

    def mark_message_submitted(self, message_id: str) -> None:
        return daemon_delivery.mark_message_submitted(self, message_id)

    def mark_message_delivered_by_id(self, agent: str, message_id: str) -> dict | None:
        return daemon_delivery.mark_message_delivered_by_id(self, agent, message_id)

    def arm_message_watchdog(self, message: dict, phase: str) -> None:
        return daemon_watchdogs.arm_message_watchdog(self, message, phase)

    def tombstone_extend_error(self, tombstone: dict) -> str:
        return daemon_watchdogs.tombstone_extend_error(self, tombstone)

    def extend_watchdog_error_hint(self, error: str | None) -> str:
        return daemon_watchdogs.extend_watchdog_error_hint(self, error)

    def upsert_message_watchdog(self, sender: str, message_id: str, additional_sec: float) -> tuple[bool, str | None, str | None]:
        return daemon_watchdogs.upsert_message_watchdog(self, sender, message_id, additional_sec)

    def _message_is_active_inflight_for_cancel(self, item: dict) -> bool:
        if str(item.get("status") or "") != "inflight":
            return False
        return self._classify_prior_for_hint(item) == "interrupt"

    def _remove_queue_message_by_id(self, message_id: str) -> dict | None:
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

        return self.queue.update(mutator)

    def cancel_message(self, sender: str, message_id: str) -> dict:
        """Cancel one sender-owned message before it becomes an active turn.

        State-lock serialization is the safety boundary: delivery and cancel
        both hold state_lock through reservation, tmux paste, and enter, so a
        mid-delivery cancel waits until the delivery attempt reaches a stable
        queue sub-state before deciding whether to remove or reject.
        """
        if not sender or sender == "bridge":
            return {"ok": False, "error": "invalid_sender"}
        try:
            lock_ctx = self.command_state_lock(
                post_lock_worst_case=COMMAND_DEFAULT_POST_LOCK_WORST_CASE_SECONDS,
                margin=COMMAND_SAFETY_MARGIN_SECONDS,
                command_class="cancel_message",
            )
            lock_ctx.__enter__()
        except CommandLockWaitExceeded:
            return self.lock_wait_exceeded_response("cancel_message")
        try:
            if not self.command_deadline_ok(
                post_lock_worst_case=COMMAND_DEFAULT_POST_LOCK_WORST_CASE_SECONDS,
                margin=COMMAND_SAFETY_MARGIN_SECONDS,
                command_class="cancel_message",
            ):
                return self.lock_wait_exceeded_response("cancel_message")
            self._prune_interrupted_turns_for_all()
            item = next((dict(it) for it in self.queue.read() if it.get("id") == message_id), None)
            if not item:
                tombstone = self._find_message_tombstone(message_id)
                if tombstone:
                    owner = str(tombstone.get("prior_sender") or "")
                    if owner and owner != sender:
                        return {
                            "ok": False,
                            "error": "not_owner",
                            "message_id": message_id,
                            "owner": owner,
                        }
                    return {
                        "ok": True,
                        "message_id": message_id,
                        "cancelled": False,
                        "already_terminal": True,
                        "terminal_reason": tombstone.get("reason") or "terminal",
                        "target": tombstone.get("target") or "",
                    }
                return {"ok": False, "error": "message_not_found", "message_id": message_id}

            owner = str(item.get("from") or "")
            if owner != sender:
                return {
                    "ok": False,
                    "error": "not_owner",
                    "message_id": message_id,
                    "owner": owner,
                }

            status = str(item.get("status") or "")
            target = str(item.get("to") or "")
            prior_kind = self._classify_prior_for_hint(item)
            if prior_kind == "interrupt":
                return {
                    "ok": False,
                    "error": "message_active_use_interrupt",
                    "message_id": message_id,
                    "target": target,
                    "status": status,
                }
            if prior_kind != "cancel":
                return {
                    "ok": False,
                    "error": "message_not_cancellable_state",
                    "message_id": message_id,
                    "status": status,
                }

            removed = self._remove_queue_message_by_id(message_id) or item
            nonce = str(removed.get("nonce") or "")
            if nonce:
                self.discard_nonce(nonce)
            if self.reserved.get(target) == message_id:
                self.reserved[target] = None
            self.last_enter_ts.pop(message_id, None)
            active_context = self.current_prompt_by_agent.get(target) or {}
            if str(active_context.get("id") or "") == message_id:
                self.current_prompt_by_agent.pop(target, None)
                self.busy[target] = False
            if removed.get("aggregate_id"):
                self.cancel_watchdogs_for_message(message_id, reason="cancelled_by_sender", phase=WATCHDOG_PHASE_DELIVERY)
            else:
                self.cancel_watchdogs_for_message(message_id, reason="cancelled_by_sender")
            self.suppress_pending_watchdog_wakes(ref_message_id=message_id, reason="cancelled_by_sender")

            suppress_late_hooks = bool(status == "inflight" and nonce)
            self._record_message_tombstone(
                target,
                removed,
                by_sender=sender,
                reason="cancelled_by_sender",
                suppress_late_hooks=suppress_late_hooks,
                prompt_submitted_seen=False,
            )
            if removed.get("aggregate_id"):
                self._record_aggregate_interrupted_reply(removed, by_sender=sender, reason="cancelled_by_sender")
            self.log(
                "message_cancelled",
                message_id=message_id,
                from_agent=owner,
                to=target,
                status=status,
                aggregate_id=removed.get("aggregate_id"),
                by_sender=sender,
            )
            return {
                "ok": True,
                "message_id": message_id,
                "cancelled": True,
                "already_terminal": False,
                "status_before": status,
                "target": target,
                "aggregate_id": removed.get("aggregate_id"),
                "input_clear_required": False,
                "input_clear_attempted": False,
                "input_clear_ok": None,
                "input_clear_error": "",
            }
        finally:
            lock_ctx.__exit__(None, None, None)

    def remove_delivered_message(self, target: str, message_id: str) -> dict | None:
        return daemon_delivery.remove_delivered_message(self, target, message_id)

    def finalize_undeliverable_message(self, message: dict, endpoint_detail: dict, *, phase: str) -> dict | None:
        return daemon_delivery.finalize_undeliverable_message(self, message, endpoint_detail, phase=phase)

    def retry_enter_for_inflight(self) -> None:
        return daemon_delivery.retry_enter_for_inflight(self)

    def register_watchdog(self, message: dict) -> None:
        return daemon_watchdogs.register_watchdog(self, message)

    def normalize_watchdog_phase(self, wd: dict) -> str:
        return daemon_watchdogs.normalize_watchdog_phase(self, wd)

    def _prune_alarm_wake_tombstones(self, now: float | None = None) -> None:
        return daemon_watchdogs._prune_alarm_wake_tombstones(self, now)

    def _record_alarm_wake_tombstone(self, wake_id: str, wd: dict, status: str) -> None:
        return daemon_watchdogs._record_alarm_wake_tombstone(self, wake_id, wd, status)

    def _same_alarm_request(self, existing: dict, sender: str, delay: float, body: str | None) -> bool:
        return daemon_watchdogs._same_alarm_request(self, existing, sender, delay, body)

    def _alarm_conflict_reason(self, existing: dict, sender: str, delay: float, body: str | None) -> str:
        return daemon_watchdogs._alarm_conflict_reason(self, existing, sender, delay, body)

    def _log_alarm_register_conflict(self, wake_id: str, sender: str, existing: dict, reason: str) -> None:
        return daemon_watchdogs._log_alarm_register_conflict(self, wake_id, sender, existing, reason)

    def register_alarm_result(
        self,
        sender: str,
        delay_seconds: float,
        body: str | None = None,
        *,
        wake_id: str | None = None,
    ) -> dict:
        return daemon_watchdogs.register_alarm_result(self, sender, delay_seconds, body, wake_id=wake_id)

    def register_alarm(self, sender: str, delay_seconds: float, body: str | None = None, wake_id: str | None = None) -> str | None:
        return daemon_watchdogs.register_alarm(self, sender, delay_seconds, body, wake_id)

    def check_watchdogs(self) -> None:
        return daemon_watchdogs.check_watchdogs(self)

    def stamp_turn_id_mismatch_post_watchdog_unblock(self, wd: dict) -> None:
        return daemon_watchdogs.stamp_turn_id_mismatch_post_watchdog_unblock(self, wd)

    def build_watchdog_fire_text(self, wd: dict) -> str:
        return daemon_watchdogs.build_watchdog_fire_text(self, wd)

    def _lookup_queue_item(self, message_id: str) -> dict | None:
        return daemon_watchdogs._lookup_queue_item(self, message_id)

    def _watchdog_elapsed_text(self, item: dict | None, wd: dict) -> str:
        return daemon_watchdogs._watchdog_elapsed_text(self, item, wd)

    def _aggregate_watchdog_progress_text(self, aggregate_id: str, wd: dict) -> str:
        return daemon_watchdogs._aggregate_watchdog_progress_text(self, aggregate_id, wd)

    def watchdog_fire_skip_reason(self, wd: dict) -> str:
        return daemon_watchdogs.watchdog_fire_skip_reason(self, wd)

    def fire_watchdog(self, wake_id: str, wd: dict) -> None:
        return daemon_watchdogs.fire_watchdog(self, wake_id, wd)

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
            if msg_id:
                if agg_id:
                    self.cancel_watchdogs_for_message(str(msg_id), reason=reason, phase=WATCHDOG_PHASE_DELIVERY)
                else:
                    self.cancel_watchdogs_for_message(str(msg_id), reason=reason)
                self.suppress_pending_watchdog_wakes(ref_message_id=str(msg_id), reason=reason)
                if reason == "prompt_intercepted":
                    self._record_message_tombstone(
                        target,
                        cm,
                        by_sender=by_sender,
                        reason=reason,
                        suppress_late_hooks=True,
                        prompt_submitted_seen=True,
                    )

        # Active context's message_id should normally be in `cancelled`
        # when cancelling delivered messages, but defend against state that
        # already lost the queue row.
        cancelled_ids = {cm.get("id") for cm in cancelled}
        act_id = active_context.get("id")
        if act_id and act_id not in cancelled_ids:
            if active_context.get("aggregate_id"):
                self.cancel_watchdogs_for_message(str(act_id), reason=reason, phase=WATCHDOG_PHASE_DELIVERY)
            else:
                self.cancel_watchdogs_for_message(str(act_id), reason=reason)
            self.suppress_pending_watchdog_wakes(ref_message_id=str(act_id), reason=reason)
            if reason == "prompt_intercepted":
                self._record_message_tombstone(
                    target,
                    {**active_context, "id": act_id},
                    by_sender=by_sender,
                    reason=reason,
                    suppress_late_hooks=True,
                    prompt_submitted_seen=True,
                )

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

    def _prune_interrupted_turns_for_all(self) -> None:
        now = time.time()
        for agent in list(self.interrupted_turns.keys()):
            self._prune_interrupted_turns(agent, now)

    def _find_message_tombstone(self, message_id: str) -> dict | None:
        if not message_id:
            return None
        self._prune_interrupted_turns_for_all()
        for target, rows in self.interrupted_turns.items():
            for row in rows or []:
                if str(row.get("message_id") or "") == message_id:
                    found = dict(row)
                    found.setdefault("target", target)
                    return found
        return None

    def _record_message_tombstone(
        self,
        target: str,
        message: dict,
        *,
        by_sender: str,
        reason: str,
        suppress_late_hooks: bool,
        prompt_submitted_seen: bool,
    ) -> dict | None:
        message_id = str(message.get("id") or "")
        if not message_id:
            return None
        now = time.time()
        self._prune_interrupted_turns(target, now)
        rows = self.interrupted_turns.setdefault(target, [])
        for row in rows:
            if str(row.get("message_id") or "") != message_id:
                continue
            row["reason"] = row.get("reason") or reason
            row["prior_sender"] = row.get("prior_sender") or str(message.get("from") or "")
            row["by_sender"] = row.get("by_sender") or by_sender
            row["target"] = row.get("target") or target
            row["suppress_late_hooks"] = bool(row.get("suppress_late_hooks", True) or suppress_late_hooks)
            row["prompt_submitted_seen"] = bool(row.get("prompt_submitted_seen") or prompt_submitted_seen)
            return row
        tombstone = {
            "message_id": message_id,
            "turn_id": str(message.get("turn_id") or ""),
            "nonce": str(message.get("nonce") or ""),
            "prior_sender": str(message.get("from") or ""),
            "by_sender": by_sender,
            "target": target,
            "cancelled_message_ids": [message_id] if reason == "cancelled_by_sender" else [],
            "interrupted_ts": now,
            "prompt_submitted_seen": bool(prompt_submitted_seen),
            "superseded_by_prompt": False,
            "reason": reason,
            "suppress_late_hooks": bool(suppress_late_hooks),
        }
        rows.append(tombstone)
        self._prune_interrupted_turns(target, now)
        self.log(
            "message_tombstone_recorded",
            target=target,
            message_id=message_id,
            nonce=tombstone.get("nonce"),
            prompt_submitted_seen=tombstone.get("prompt_submitted_seen"),
            by_sender=by_sender,
            reason=reason,
            suppress_late_hooks=suppress_late_hooks,
        )
        return tombstone

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
                "reason": "interrupted",
                "target": target,
                "suppress_late_hooks": True,
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
                "reason": "interrupted",
                "target": target,
                "suppress_late_hooks": True,
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
            if not bool(row.get("suppress_late_hooks", True)):
                continue
            row_nonce = str(row.get("nonce") or "")
            row_turn_id = str(row.get("turn_id") or "")
            if (nonce and row_nonce and nonce == row_nonce) or (turn_id and row_turn_id and turn_id == row_turn_id):
                return row
        return None

    def _supersede_interrupted_no_turn_suppression(self, agent: str, *, turn_id: object = None, message_id: object = None) -> None:
        rows = self.interrupted_turns.get(agent) or []
        changed = 0
        for row in rows:
            if bool(row.get("suppress_late_hooks", True)) and not row.get("superseded_by_prompt"):
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
        rows = [row for row in list(self.interrupted_turns.get(agent) or []) if bool(row.get("suppress_late_hooks", True))]
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

    def send_interrupt_key(self, pane: str, key: str) -> tuple[bool, str]:
        if self.dry_run:
            return True, ""
        try:
            subprocess.run(
                ["tmux", "send-keys", "-t", pane, key],
                check=True,
                timeout=INTERRUPT_SEND_KEY_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return False, "timeout"
        except Exception as exc:
            return False, str(exc)
        return True, ""

    def target_has_active_interrupt_work(self, target: str, active_context: dict | None = None) -> bool:
        if active_context and active_context.get("id"):
            return True
        if self.interrupt_partial_failure_blocks.get(target):
            return True
        if self.reserved.get(target):
            return True
        active_statuses = {"inflight", "submitted", "delivered"}
        for item in self.queue.read():
            if item.get("to") == target and item.get("status") in active_statuses:
                return True
        return False

    def handle_interrupt(self, sender: str, target: str) -> dict:
        # Interrupt semantics:
        #   1. ESC fail-closed: if tmux send-keys fails, no state mutation.
        #   2. Cancel (not requeue) the active in-flight message and any
        #      delivered/inflight/submitted messages for this target.
        #      Pending replacement messages are allowed to deliver once the
        #      configured interrupt key sequence succeeds.
        #   3. Record interrupted-turn tombstones so identifiable late
        #      prompt_submitted / response_finished events from the
        #      cancelled turn are suppressed without blocking replacements.
        #   4. Aggregate-bearing cancelled messages get a synthetic
        #      "[interrupted]" reply recorded into the aggregate, so the
        #      aggregate can still complete from the remaining peers'
        #      replies (and so the aggregate watchdog isn't dropped).
        # IMPORTANT: pane resolve, key dispatch, and state mutation all run
        # inside state_lock. Otherwise a prompt_submitted/response_finished
        # event or queued replacement delivery could interleave between ESC
        # and Claude's follow-up C-c, causing stale ctx mutation or clearing
        # a fresh replacement prompt. The bounded key delay is the v1
        # correctness/perf trade-off and is documented in the lock ordering
        # comment in __init__.
        try:
            self.reload_participants()
            lock_ctx = self.command_state_lock(
                post_lock_worst_case=INTERRUPT_SEND_KEY_TIMEOUT_SECONDS,
                margin=COMMAND_SAFETY_MARGIN_SECONDS,
                command_class="interrupt",
            )
            lock_ctx.__enter__()
        except CommandLockWaitExceeded:
            return {
                "esc_sent": False,
                "esc_error": "lock_wait_exceeded",
                "interrupt_ok": False,
                "interrupt_keys": [],
                "cc_sent": None,
                "cc_error": None,
                "held": False,
                "cancelled_message_ids": [],
                "error_kind": "lock_wait_exceeded",
            }
        try:
            if not self.command_deadline_ok(
                post_lock_worst_case=COMMAND_DEFAULT_POST_LOCK_WORST_CASE_SECONDS,
                margin=COMMAND_SAFETY_MARGIN_SECONDS,
                command_class="interrupt",
            ):
                return {
                    "esc_sent": False,
                    "esc_error": "lock_wait_exceeded",
                    "interrupt_ok": False,
                    "interrupt_keys": [],
                    "cc_sent": None,
                    "cc_error": None,
                    "held": False,
                    "cancelled_message_ids": [],
                    "error_kind": "lock_wait_exceeded",
                }
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
                    "interrupt_ok": False,
                    "interrupt_keys": [],
                    "cc_sent": None,
                    "cc_error": None,
                    "held": False,
                    "cancelled_message_ids": [item.get("id") for item in finalized],
                }
            participant = self.participants.get(target) or {}
            agent_type = str(participant.get("agent_type") or "")
            if agent_type not in PHYSICAL_AGENT_TYPES:
                self.safe_log("interrupt_unknown_agent_type", target=target, agent_type=agent_type)

            active_context_before = self.current_prompt_by_agent.get(target) or {}
            had_active = self.target_has_active_interrupt_work(target, active_context_before)

            interrupt_keys: list[str] = ["Escape"]
            if not self.command_deadline_ok(
                post_lock_worst_case=INTERRUPT_SEND_KEY_TIMEOUT_SECONDS,
                margin=COMMAND_SAFETY_MARGIN_SECONDS,
                command_class="interrupt",
            ):
                return {
                    "esc_sent": False,
                    "esc_error": "lock_wait_exceeded",
                    "interrupt_ok": False,
                    "interrupt_keys": [],
                    "cc_sent": None,
                    "cc_error": None,
                    "held": False,
                    "cancelled_message_ids": [],
                    "error_kind": "lock_wait_exceeded",
                }
            esc_ok, esc_error = self.send_interrupt_key(pane, "Escape")
            if not esc_ok:
                self.log("esc_failed", target=target, by_sender=sender, error=esc_error)
                return {
                    "esc_sent": False,
                    "esc_error": esc_error,
                    "interrupt_ok": False,
                    "interrupt_keys": interrupt_keys,
                    "cc_sent": None,
                    "cc_error": None,
                    "held": False,
                    "cancelled_message_ids": [],
                }

            active_context = self.current_prompt_by_agent.pop(target, {}) or {}
            cancelled = self._cancel_active_messages_for_target(
                target,
                active_context=active_context,
                reason="interrupted",
                by_sender=sender,
                cancel_statuses={"delivered", "inflight", "submitted"},
                notify_sources=True,
            )

            tombstones = self._record_interrupted_turns(target, active_context, cancelled, sender)
            prior_active_message_id = active_context.get("id")
            should_send_cc = (
                agent_type == "claude"
                and "C-c" in self.claude_interrupt_keys
                and had_active
            )
            cc_sent: bool | None = None
            cc_error: str | None = None
            if should_send_cc:
                if not self.dry_run and self.interrupt_key_delay_seconds > 0:
                    time.sleep(self.interrupt_key_delay_seconds)
                interrupt_keys.append("C-c")
                if not self.command_deadline_ok(
                    post_lock_worst_case=INTERRUPT_SEND_KEY_TIMEOUT_SECONDS,
                    margin=COMMAND_SAFETY_MARGIN_SECONDS,
                    command_class="interrupt",
                ):
                    cc_sent = False
                    cc_error = "lock_wait_exceeded"
                    self.log("cc_send_failed", target=target, by_sender=sender, error=cc_error)
                else:
                    cc_ok, cc_error_text = self.send_interrupt_key(pane, "C-c")
                    cc_sent = cc_ok
                    if not cc_ok:
                        cc_error = cc_error_text
                        self.log("cc_send_failed", target=target, by_sender=sender, error=cc_error)
            interrupt_ok = cc_sent is not False
            self.busy[target] = False
            self.reserved[target] = None
            if interrupt_ok:
                prior_block = self.interrupt_partial_failure_blocks.pop(target, None)
                if prior_block:
                    self.log(
                        "interrupt_partial_failure_block_cleared",
                        target=target,
                        by_sender=sender,
                        reason="interrupt_completed",
                        prior_error=prior_block.get("cc_error"),
                    )
            else:
                block_info = {
                    "since": utc_now(),
                    "since_ts": time.time(),
                    "by_sender": sender,
                    "agent_type": agent_type,
                    "prior_message_id": prior_active_message_id,
                    "cancelled_message_ids": [cm.get("id") for cm in cancelled],
                    "cc_error": cc_error,
                    "reason": "interrupt_key_sequence_failed",
                }
                self.interrupt_partial_failure_blocks[target] = block_info
                self.log(
                    "interrupt_partial_failure_blocked",
                    target=target,
                    by_sender=sender,
                    prior_message_id=prior_active_message_id,
                    cc_error=cc_error,
                )
            self.log(
                "interrupt_replacement_unblocked" if interrupt_ok else "interrupt_replacement_blocked",
                target=target,
                by_sender=sender,
                prior_message_id=prior_active_message_id,
                prior_sender=active_context.get("from"),
                cancelled_count=len(cancelled),
                cancelled_message_ids=[cm.get("id") for cm in cancelled],
                tombstone_count=len(tombstones),
            )
            self.log(
                "interrupt_keys_sent",
                target=target,
                by_sender=sender,
                agent_type=agent_type,
                keys=interrupt_keys,
                delay_sec=self.interrupt_key_delay_seconds if should_send_cc else 0.0,
                had_active=had_active,
                interrupt_ok=interrupt_ok,
                cc_sent=cc_sent,
            )
        finally:
            lock_ctx.__exit__(None, None, None)

        # Kick delivery to the interrupted target so queued corrections run
        # promptly. If the configured key sequence only partially completed,
        # leave queued work pending rather than appending to a possibly dirty
        # prompt buffer.
        if interrupt_ok:
            self.try_deliver_command_aware(target)
        else:
            self.log(
                "interrupt_delivery_skipped",
                target=target,
                by_sender=sender,
                reason="interrupt_key_sequence_failed",
            )
        return {
            "esc_sent": True,
            "esc_error": None,
            "interrupt_ok": interrupt_ok,
            "interrupt_keys": interrupt_keys,
            "cc_sent": cc_sent,
            "cc_error": cc_error,
            "cancelled_message_ids": [cm.get("id") for cm in cancelled],
            "prior_active_message_id": prior_active_message_id,
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

    def _record_aggregate_interrupted_reply(self, cancelled_msg: dict, by_sender: str, *, reason: str, deliver: bool = True) -> None:
        return daemon_aggregates._record_aggregate_interrupted_reply(
            self,
            cancelled_msg,
            by_sender,
            reason=reason,
            deliver=deliver,
        )

    def release_hold(self, target: str, reason: str, by_sender: str | None = None) -> dict | None:
        try:
            with self.command_state_lock(command_class="clear_hold"):
                info = self.held_interrupt.pop(target, None)
                partial_block = self.interrupt_partial_failure_blocks.pop(target, None)
        except CommandLockWaitExceeded:
            return {"error": "lock_wait_exceeded", "error_kind": "lock_wait_exceeded", "_lock_wait_exceeded": True}
        had_held = info is not None
        if not info and not partial_block:
            return None
        if partial_block:
            if info:
                info = {**info, "interrupt_partial_failure": partial_block}
            else:
                info = {
                    "reason": "interrupt_partial_failure",
                    "target": target,
                    "prior_message_id": partial_block.get("prior_message_id"),
                    "since_ts": partial_block.get("since_ts"),
                    "interrupt_partial_failure": partial_block,
                }
        hold_duration_ms = None
        since_ts = info.get("since_ts")
        if isinstance(since_ts, (int, float)):
            hold_duration_ms = int(max(0.0, time.time() - float(since_ts)) * 1000)
        if partial_block and not had_held and reason.startswith("manual_clear"):
            event_name = "interrupt_partial_failure_block_cleared"
        else:
            event_name = "hold_force_resumed" if reason.startswith("manual_clear") else "hold_released"
        self.log(
            event_name,
            target=target,
            reason=reason,
            by_sender=by_sender,
            prior_message_id=info.get("prior_message_id"),
            partial_cc_error=(partial_block or {}).get("cc_error"),
            hold_duration_ms=hold_duration_ms,
        )
        self.try_deliver_command_aware(target)
        self.try_deliver_command_aware()
        return info

    def cancel_watchdogs_for_message(
        self,
        message_id: str | None,
        reason: str = "reply_received",
        *,
        phase: str | None = None,
    ) -> None:
        return daemon_watchdogs.cancel_watchdogs_for_message(self, message_id, reason=reason, phase=phase)

    def cancel_watchdogs_for_aggregate(self, aggregate_id: str | None, reason: str = "aggregate_complete") -> None:
        return daemon_watchdogs.cancel_watchdogs_for_aggregate(self, aggregate_id, reason=reason)

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
            if self.normalize_watchdog_phase(wd) != WATCHDOG_PHASE_RESPONSE:
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
                self.suppress_pending_watchdog_wakes(ref_message_id=message_id, reason="turn_id_mismatch_expired")
                self._record_message_tombstone(
                    target,
                    {**context, "id": message_id},
                    by_sender="bridge",
                    reason="turn_id_mismatch_expired",
                    suppress_late_hooks=False,
                    prompt_submitted_seen=True,
                )
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
                if str(item.get("to") or "") in self.clear_reservations:
                    continue
                if item.get("pane_mode_enter_deferred_since_ts"):
                    item["updated_ts"] = utc_now()
                    if item.get("last_error") not in {"pane_mode_probe_failed_waiting_enter"}:
                        item["last_error"] = "pane_in_mode_waiting_enter"
                    continue
                if item.get(RESTART_PRESERVED_INFLIGHT_KEY):
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
            message_id = str(item.get("id") or "")
            stale_targets.add(target)
            self.discard_nonce(str(item.get("nonce") or ""))
            if self.reserved.get(target) == item.get("id"):
                self.reserved[target] = None
            self.last_enter_ts.pop(message_id, None)
            self.cancel_watchdogs_for_message(message_id, reason="prompt_submit_timeout", phase=WATCHDOG_PHASE_DELIVERY)
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
        return daemon_events.handle_prompt_submitted(self, record)

    def response_fingerprint(self, record: dict) -> str:
        return daemon_events.response_fingerprint(self, record)

    def aggregate_expected_from_context(self, context: dict) -> list[str]:
        return daemon_aggregates.aggregate_expected_from_context(self, context)

    def aggregate_message_ids_from_context(self, context: dict) -> dict[str, str]:
        return daemon_aggregates.aggregate_message_ids_from_context(self, context)

    def merge_ordered_aliases(self, existing: list[str], incoming: list[str]) -> list[str]:
        return daemon_aggregates.merge_ordered_aliases(self, existing, incoming)

    def aggregate_result_body(self, aggregate: dict) -> str:
        return daemon_aggregates.aggregate_result_body(self, aggregate)

    def _record_aggregate_completion_tombstones(self, aggregate: dict) -> None:
        return daemon_aggregates._record_aggregate_completion_tombstones(self, aggregate)

    def collect_aggregate_response(self, sender: str, text: str, context: dict, deliver: bool = True) -> None:
        return daemon_aggregates.collect_aggregate_response(self, sender, text, context, deliver=deliver)

    def maybe_return_response(self, sender: str, text: str, context: dict) -> None:
        return daemon_events.maybe_return_response(self, sender, text, context)

    def _cleanup_terminal_context_locked(self, sender: str, context: dict, *, watchdog_reason: str) -> None:
        return daemon_events._cleanup_terminal_context_locked(self, sender, context, watchdog_reason=watchdog_reason)

    def handle_response_finished(self, record: dict) -> None:
        return daemon_events.handle_response_finished(self, record)

    def handle_external_message_queued(self, record: dict) -> None:
        return daemon_events.handle_external_message_queued(self, record)

    def _drop_ingress_row_from_blocked_sender(self, message_id: str, *, phase: str) -> bool:
        return daemon_events._drop_ingress_row_from_blocked_sender(self, message_id, phase=phase)

    def _apply_alarm_cancel_to_queued_message(self, message_id: str) -> None:
        return daemon_events._apply_alarm_cancel_to_queued_message(self, message_id)

    def _preserve_startup_inflight_messages(self) -> None:
        # Startup recovery: a status=inflight row may already have been pasted
        # into the peer pane by the previous daemon. Since current_prompt and
        # last_enter_ts are in-memory only, automatically requeueing that row
        # after submit_timeout can paste the same prompt again. Preserve normal
        # inflight rows as delivery blockers and let a later prompt_submitted
        # bind them via find_inflight_candidate's queue scan.
        #
        # Pane-mode enter-deferred rows are different: the text was pasted but
        # Enter was intentionally delayed. Their durable pane-mode metadata is
        # the existing recovery signal, so do not freeze them here.
        preserved: list[dict] = []
        now_iso = utc_now()

        def mutator(queue: list[dict]) -> list[dict]:
            for item in queue:
                if item.get("status") != "inflight":
                    continue
                if item.get("pane_mode_enter_deferred_since_ts"):
                    continue
                if item.get(RESTART_PRESERVED_INFLIGHT_KEY):
                    continue
                item[RESTART_PRESERVED_INFLIGHT_KEY] = True
                item["restart_preserved_inflight_at"] = now_iso
                item["updated_ts"] = now_iso
                preserved.append(dict(item))
            return preserved

        self.queue.update(mutator)
        for item in preserved:
            self.log(
                "inflight_preserved_after_daemon_restart",
                message_id=item.get("id"),
                to=item.get("to"),
                from_agent=item.get("from"),
                nonce=item.get("nonce"),
            )

    def _recover_ingressing_messages(self) -> None:
        # Startup recovery: any queue items left in transient "ingressing"
        # state were written by bridge_enqueue.py's file-fallback path
        # while the daemon was down (or were left over by a daemon crash
        # between queue insert and finalize). The corresponding alarms
        # were daemon-memory only and have been lost across the restart,
        # so just promote these to "pending" and unblock delivery; log
        # the recovery for operator visibility.
        recovered: list[str] = []
        dropped: list[dict] = []
        stripped_logs: list[dict] = []

        def mutator(queue: list[dict]) -> None:
            kept: list[dict] = []
            for item in queue:
                if item.get("status") == "ingressing":
                    if self.sender_blocked_by_clear(str(item.get("from") or "")):
                        dropped.append(dict(item))
                        continue
                    stripped_log = self._strip_no_auto_return_watchdog_metadata(
                        item,
                        phase="recovery",
                        reason="ingressing_recovered",
                    )
                    if stripped_log:
                        stripped_logs.append(stripped_log)
                    item["status"] = "pending"
                    item["updated_ts"] = utc_now()
                    item["last_error"] = "ingressing_recovered_after_daemon_restart"
                    recovered.append(str(item.get("id") or ""))
                kept.append(item)
            if dropped:
                queue[:] = kept

        self.queue.update(mutator)
        for item in dropped:
            self._record_message_tombstone(
                str(item.get("to") or ""),
                item,
                by_sender="bridge",
                reason="sender_clear_blocked",
                suppress_late_hooks=False,
                prompt_submitted_seen=False,
            )
            self.log(
                "ingressing_recovery_dropped_sender_clear_blocked",
                message_id=item.get("id"),
                from_agent=item.get("from"),
                to=item.get("to"),
            )
        for fields in stripped_logs:
            self.log("watchdog_stripped_no_auto_return", **fields)
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
            message_id = str(item.get("id") or "")
            target = str(item.get("to") or "")
            if message_id:
                self.suppress_pending_watchdog_wakes(ref_message_id=message_id, reason="daemon_restart_lost_routing_ctx")
                self._record_message_tombstone(
                    target,
                    item,
                    by_sender="bridge",
                    reason="daemon_restart_lost_routing_ctx",
                    suppress_late_hooks=False,
                    prompt_submitted_seen=True,
                )
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
            dropped: list[dict] = []
            stripped_logs: list[dict] = []

            def mutator(queue: list[dict]) -> None:
                kept: list[dict] = []
                for item in queue:
                    if item.get("status") != "ingressing":
                        kept.append(item)
                        continue
                    created = item.get("created_ts") or item.get("updated_ts")
                    age_unknown = False
                    try:
                        ts = datetime.fromisoformat(str(created).replace("Z", "+00:00")).timestamp()
                    except (TypeError, ValueError):
                        ts = 0.0
                        age_unknown = True
                    if not age_unknown and ts >= threshold:
                        kept.append(item)
                        continue
                    if self.sender_blocked_by_clear(str(item.get("from") or "")):
                        dropped.append(dict(item))
                        continue
                    age_sec = max(0.0, now - ts) if not age_unknown else 0.0
                    stripped_log = self._strip_no_auto_return_watchdog_metadata(
                        item,
                        phase="promotion",
                        reason="ingressing_promoted_aged",
                    )
                    if stripped_log:
                        stripped_logs.append(stripped_log)
                    item["status"] = "pending"
                    item["updated_ts"] = utc_now()
                    item["last_error"] = "ingressing_promoted_aged"
                    promoted.append((str(item.get("id") or ""), age_sec, age_unknown))
                    kept.append(item)
                if dropped:
                    queue[:] = kept

            self.queue.update(mutator)
            for item in dropped:
                self._record_message_tombstone(
                    str(item.get("to") or ""),
                    item,
                    by_sender="bridge",
                    reason="sender_clear_blocked",
                    suppress_late_hooks=False,
                    prompt_submitted_seen=False,
                )
                self.log(
                    "ingressing_promotion_dropped_sender_clear_blocked",
                    message_id=item.get("id"),
                    from_agent=item.get("from"),
                    to=item.get("to"),
                )
            for fields in stripped_logs:
                self.log("watchdog_stripped_no_auto_return", **fields)
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
                    dry_run=self.dry_run,
                    command_socket=str(self.command_socket) if self.command_socket else "",
                    pane_mode_grace_seconds=self.pane_mode_grace_seconds,
                    turn_id_mismatch_grace_seconds=self.turn_id_mismatch_grace_seconds,
                    turn_id_mismatch_post_watchdog_grace_seconds=self.turn_id_mismatch_post_watchdog_grace_seconds,
                    interrupt_key_delay_seconds=self.interrupt_key_delay_seconds,
                    claude_interrupt_keys=list(self.claude_interrupt_keys),
                )
                if self.pane_mode_grace_warning:
                    self.log("pane_mode_grace_config_warning", warning=self.pane_mode_grace_warning)
                for warning in self.turn_id_mismatch_grace_warnings:
                    self.log("turn_id_mismatch_grace_config_warning", warning=warning)
                for warning in self.interrupt_config_warnings:
                    self.log("interrupt_config_warning", warning=warning)
                for warning in self.clear_config_warnings:
                    self.log("clear_config_warning", warning=warning)
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
