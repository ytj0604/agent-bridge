#!/usr/bin/env python3
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import fcntl
import json
import os
from pathlib import Path
from typing import Any, Callable, Iterator


MESSAGE_KINDS = {"request", "result", "notice"}
DEFAULT_REDACT_FIELDS = {"prompt", "last_assistant_message", "body", "tool_input", "transcript_path"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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
