#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import select
import sys
import unicodedata


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def write_tty(fd: int, text: str) -> None:
    os.write(fd, text.encode("utf-8", errors="replace"))


def read_key(fd: int | None = None) -> str:
    if fd is None:
        fd = sys.stdin.fileno()
    raw = os.read(fd, 1)
    if raw == b"":
        return "eof"
    if raw == b"\x03":
        return "quit"
    if raw == b"\x1b":
        seq = b""
        timeout = 0.30
        while len(seq) < 8:
            readable, _, _ = select.select([fd], [], [], timeout)
            if not readable:
                break
            seq += os.read(fd, 1)
            if seq in {b"[A", b"[B", b"[C", b"[D", b"OA", b"OB", b"OC", b"OD", b"[I", b"[O"}:
                break
            if len(seq) >= 2 and seq[:1] not in {b"[", b"O"}:
                break
            timeout = 0.10
        if seq in {b"[A", b"OA"}:
            return "up"
        if seq in {b"[B", b"OB"}:
            return "down"
        if seq in {b"[C", b"OC"}:
            return "right"
        if seq in {b"[D", b"OD"}:
            return "left"
        if seq == b"":
            return "escape"
        return "ignore"
    if raw in {b"\r", b"\n"}:
        return "enter"
    if raw in {b"\x7f", b"\b"}:
        return "backspace"
    return raw.decode("utf-8", errors="ignore") or "ignore"


def terminal_cells(text: str) -> int:
    cells = 0
    for ch in ANSI_RE.sub("", text).expandtabs(8):
        if unicodedata.combining(ch):
            continue
        category = unicodedata.category(ch)
        if category in {"Cc", "Cf"}:
            continue
        cells += 2 if unicodedata.east_asian_width(ch) in {"F", "W"} else 1
    return cells


def truncate_to_cells(text: str, width: int) -> str:
    if width <= 1:
        return ""
    raw = ANSI_RE.sub("", text).expandtabs(8)
    if terminal_cells(raw) <= width:
        return text.expandtabs(8)
    ellipsis = "..."
    ellipsis_width = terminal_cells(ellipsis)
    limit = max(0, width - ellipsis_width)
    cells = 0
    out: list[str] = []
    for ch in raw:
        if unicodedata.combining(ch):
            out.append(ch)
            continue
        category = unicodedata.category(ch)
        ch_width = 0 if category in {"Cc", "Cf"} else 2 if unicodedata.east_asian_width(ch) in {"F", "W"} else 1
        if cells + ch_width > limit:
            break
        out.append(ch)
        cells += ch_width
    return "".join(out) + ellipsis


def terminal_width(default: int = 100, fd: int | None = None) -> int:
    try:
        if fd is None:
            return os.get_terminal_size().columns
        return os.get_terminal_size(fd).columns
    except OSError:
        return default
