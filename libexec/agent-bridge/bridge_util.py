#!/usr/bin/env python3
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import fcntl
import json
import os
from pathlib import Path
import subprocess
import uuid
from typing import Any, Callable, Iterator


MESSAGE_KINDS = {"request", "result", "notice"}
DEFAULT_REDACT_FIELDS = {"prompt", "last_assistant_message", "body", "tool_input", "transcript_path"}
MAX_PEER_BODY_CHARS = 12000
# External sends leave headroom under the daemon prompt guard for bridge-added
# notices such as alarm-cancel prefixes.
MAX_INLINE_SEND_BODY_CHARS = 11000
SHARED_PAYLOAD_ROOT = "/tmp/agent-bridge-share"

# Default suffix length for hex-derived identifiers that show up in prompt
# envelopes (msg-, causal-, agg-, wake-, cap-). 12 hex = 2^48 ≈ 2.8e14; the
# birthday-collision threshold at 1% sits around 5M unique IDs, well above
# any realistic per-session traffic. nonce/probe/snapshot use shorter
# suffixes because they have additional disambiguators (timestamp / agent
# alias / filename namespace).
SHORT_ID_LEN = 12
PRIOR_MESSAGE_HINT_PREFIX = "PRIOR_MESSAGE_HINT: "
PRIOR_HINT_CANCEL = "cancel"
PRIOR_HINT_INTERRUPT = "interrupt"
RESTART_PRESERVED_INFLIGHT_KEY = "restart_preserved_inflight"


class TmuxCaptureError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def short_id(prefix: str, length: int = SHORT_ID_LEN) -> str:
    """Generate a prefixed short hex identifier (e.g., 'msg-a1b2c3d4e5f6')."""
    return f"{prefix}-{uuid.uuid4().hex[:length]}"


def validate_peer_body_size(body: str, tool_name: str = "agent_send_peer") -> tuple[bool, str]:
    """Validate external inline peer body size using a character-count limit."""
    actual = len(str(body))
    if actual <= MAX_INLINE_SEND_BODY_CHARS:
        return True, ""
    return (
        False,
        (
            f"{tool_name}: message body is {actual} chars, exceeding the inline limit "
            f"of {MAX_INLINE_SEND_BODY_CHARS} chars. Write large content to "
            f"{SHARED_PAYLOAD_ROOT}/<file> and send the path instead. Splitting into "
            f"multiple {tool_name} calls is possible only if separate requests and "
            "separate replies are acceptable."
        ),
    )


def read_limited_text(stream, limit: int = MAX_INLINE_SEND_BODY_CHARS) -> str:
    """Read enough text to validate size without consuming an unbounded stdin."""
    return stream.read(max(0, int(limit)) + 1)


def classify_prior_for_hint(
    item: dict,
    last_enter_ts: dict[str, float] | None = None,
    *,
    inflight_without_enter: str | None = PRIOR_HINT_CANCEL,
) -> str | None:
    """Classify a same sender->target prior row for model-facing guidance."""
    status = str(item.get("status") or "")
    if status == "pending":
        return PRIOR_HINT_CANCEL
    if status in {"submitted", "delivered"}:
        return PRIOR_HINT_INTERRUPT
    if status == "inflight":
        if item.get("pane_mode_enter_deferred_since_ts"):
            return PRIOR_HINT_CANCEL
        if item.get(RESTART_PRESERVED_INFLIGHT_KEY):
            return PRIOR_HINT_INTERRUPT
        message_id = str(item.get("id") or "")
        if message_id and last_enter_ts is not None and message_id in last_enter_ts:
            return PRIOR_HINT_INTERRUPT
        return inflight_without_enter
    return None


def _aggregate_prior_suffix(prior_aggregate_id: str) -> str:
    if not prior_aggregate_id:
        return ""
    return (
        f" This affects only this leg of aggregate {prior_aggregate_id}; "
        "resend as a fresh aggregate if every peer needs the replacement."
    )


def prior_message_hint_entry(message: dict, prior: dict, prior_kind: str) -> dict:
    target = str(message.get("to") or prior.get("to") or "")
    prior_id = str(prior.get("id") or "")
    prior_status = str(prior.get("status") or "active")
    prior_aggregate_id = str(prior.get("aggregate_id") or "")
    if prior_kind == PRIOR_HINT_CANCEL:
        text = (
            f"{PRIOR_MESSAGE_HINT_PREFIX}{target} still has your earlier message {prior_id} queued "
            f"(status: {prior_status}). If that send was a mistake, agent_cancel_message {prior_id} "
            f"retracts it before {target} picks it up; otherwise this send simply queues behind it."
        )
    else:
        if normalize_kind(message.get("kind"), "request") == "notice":
            tail = "otherwise your queued follow-up will wait behind it."
        else:
            tail = "otherwise your follow-up will wait behind the active turn."
        text = (
            f"{PRIOR_MESSAGE_HINT_PREFIX}{target} is processing your earlier message {prior_id} "
            f"(status: {prior_status}). If that send was a mistake, run agent_interrupt_peer {target} "
            f"--status to confirm, then agent_interrupt_peer {target}; {tail}"
        )
    text += _aggregate_prior_suffix(prior_aggregate_id)
    entry = {
        "message_id": message.get("id"),
        "target": target,
        "prior_message_id": prior_id,
        "prior_status": prior_status,
        "prior_kind": prior_kind,
        "text": text,
    }
    if prior_aggregate_id:
        entry["prior_aggregate_id"] = prior_aggregate_id
    return entry


def prior_message_hint_candidates(
    message: dict,
    queue: list[dict],
    last_enter_ts: dict[str, float] | None = None,
    *,
    inflight_without_enter: str | None = PRIOR_HINT_CANCEL,
) -> list[tuple[int, int, dict, str]]:
    sender = str(message.get("from") or "")
    target = str(message.get("to") or "")
    message_id = str(message.get("id") or "")
    candidates: list[tuple[int, int, dict, str]] = []
    for index, item in enumerate(queue):
        if str(item.get("id") or "") == message_id:
            continue
        if str(item.get("from") or "") != sender or str(item.get("to") or "") != target:
            continue
        prior_kind = classify_prior_for_hint(
            item,
            last_enter_ts,
            inflight_without_enter=inflight_without_enter,
        )
        if not prior_kind:
            continue
        priority = 0 if prior_kind == PRIOR_HINT_INTERRUPT else 1
        candidates.append((priority, index, item, prior_kind))
    return candidates


def prior_message_hint_for_queue(
    message: dict,
    queue: list[dict],
    last_enter_ts: dict[str, float] | None = None,
    *,
    inflight_without_enter: str | None = PRIOR_HINT_CANCEL,
) -> dict | None:
    candidates = prior_message_hint_candidates(
        message,
        queue,
        last_enter_ts,
        inflight_without_enter=inflight_without_enter,
    )
    if not candidates:
        return None
    _priority, _index, prior, prior_kind = min(candidates, key=lambda row: (row[0], row[1]))
    return prior_message_hint_entry(message, prior, prior_kind)


def _default_copy(default: Any) -> Any:
    if isinstance(default, dict):
        return dict(default)
    if isinstance(default, list):
        return list(default)
    return default


def read_json(path: Path, default: Any | None = None) -> Any:
    if default is None:
        default = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return _default_copy(default)
    if isinstance(default, dict) and not isinstance(data, dict):
        return dict(default)
    if isinstance(default, list) and not isinstance(data, list):
        return list(default)
    return data


def write_json_atomic(path: Path, data: Any, *, ensure_ascii: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=ensure_ascii, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


@contextmanager
def path_lock(path: Path, mode: int = fcntl.LOCK_EX) -> Iterator[None]:
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), mode)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


@contextmanager
def locked_json(path: Path, default: Any | None = None) -> Iterator[Any]:
    with path_lock(path, fcntl.LOCK_EX):
        data = read_json(path, default)
        yield data
        write_json_atomic(path, data)


def locked_json_read(path: Path, default: Any | None = None) -> Any:
    if not path.exists():
        return _default_copy({} if default is None else default)
    try:
        with path_lock(path, fcntl.LOCK_SH):
            return read_json(path, default)
    except OSError as exc:
        if exc.errno in {errno.EROFS, errno.EACCES, errno.EPERM}:
            return read_json(path, default)
        raise


def update_locked_json(path: Path, default: Any, mutator: Callable[[Any], Any]) -> Any:
    with locked_json(path, default) as data:
        return mutator(data)


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=True) + "\n"
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def run_tmux_capture(target: str, start: int, end: int | str | None = None, raw: bool = False) -> str:
    cmd = ["tmux", "capture-pane", "-p", "-t", target, "-S", str(start)]
    if end is not None and str(end) != "-1":
        cmd += ["-E", str(end)]
    if raw:
        cmd.insert(2, "-e")
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise TmuxCaptureError((proc.stderr or proc.stdout).strip() or f"tmux capture-pane exited {proc.returncode}")
    return proc.stdout


def public_record(
    record: dict,
    *,
    allowed_fields: tuple[str, ...] | None = None,
    redact_fields: set[str] | None = None,
) -> dict:
    redact = DEFAULT_REDACT_FIELDS if redact_fields is None else redact_fields
    keys = allowed_fields if allowed_fields is not None else tuple(record.keys())
    redacted: dict[str, Any] = {}
    for key in keys:
        if key not in record:
            continue
        value = record[key]
        if key in redact:
            if isinstance(value, str):
                redacted[f"{key}_chars"] = len(value)
            else:
                redacted[f"{key}_redacted"] = True
        else:
            redacted[key] = value
    if allowed_fields is not None:
        for key in redact:
            if key in record and key not in redacted and f"{key}_chars" not in redacted:
                value = record[key]
                if isinstance(value, str):
                    redacted[f"{key}_chars"] = len(value)
                else:
                    redacted[f"{key}_redacted"] = True
    return redacted


def normalize_kind(value: object, default: str = "request") -> str:
    raw = str(value or default).strip().lower()
    aliases = {
        "ask": "request",
        "task": "request",
        "followup": "request",
        "follow_up": "request",
        "response": "result",
        "answer": "result",
        "reply": "result",
        "notification": "notice",
        "notify": "notice",
        "info": "notice",
    }
    kind = aliases.get(raw, raw)
    return kind if kind in MESSAGE_KINDS else default
