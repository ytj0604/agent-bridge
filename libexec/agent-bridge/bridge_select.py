#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
import termios
import tty

from bridge_ui import read_key, terminal_width, truncate_to_cells, write_tty


def render(fd: int, title: str, items: list[str], selected: int, rendered: int) -> int:
    if rendered:
        write_tty(fd, f"\033[{rendered}A\033[J")
    width = max(32, min(terminal_width(fd=fd), 120))
    lines = []
    lines.extend(title.splitlines() or [""])
    lines.append("Keys: Up/Down = move, Enter = select, Esc = cancel")
    lines.append("")
    for idx, item in enumerate(items):
        text = truncate_to_cells(item, max(1, width - 4))
        if idx == selected:
            lines.append(f"\033[1;7m> {text}\033[0m")
        else:
            lines.append(f"  {text}")
    write_tty(fd, "\n".join(lines) + "\n")
    return len(lines)


def interactive_select(title: str, items: list[str]) -> int:
    fd = os.open("/dev/tty", os.O_RDWR)
    try:
        old = termios.tcgetattr(fd)
    except termios.error:
        os.close(fd)
        raise OSError("no usable controlling tty")
    selected = 0
    rendered = 0
    try:
        termios.tcflush(fd, termios.TCIFLUSH)
        tty.setcbreak(fd)
        termios.tcflush(fd, termios.TCIFLUSH)
        while True:
            rendered = render(fd, title, items, selected, rendered)
            key = read_key(fd)
            if key == "up":
                selected = (selected + len(items) - 1) % len(items)
            elif key == "down":
                selected = (selected + 1) % len(items)
            elif key == "enter":
                write_tty(fd, f"\033[{rendered}A\033[J")
                return selected
            elif key in {"escape", "quit", "eof"}:
                write_tty(fd, f"\033[{rendered}A\033[J")
                raise KeyboardInterrupt
            elif key == "ignore":
                continue
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        os.close(fd)


def fallback_select(title: str, items: list[str]) -> int:
    print(title, file=sys.stderr)
    for idx, item in enumerate(items, start=1):
        print(f"{idx:2d}. {item}", file=sys.stderr)
    print("Select action: ", end="", file=sys.stderr, flush=True)
    raw = sys.stdin.readline().strip()
    if not raw.isdigit():
        raise SystemExit(130)
    selected = int(raw) - 1
    if selected < 0 or selected >= len(items):
        raise SystemExit(130)
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(description="Terminal menu selector.")
    parser.add_argument("--title", required=True)
    parser.add_argument("items", nargs="+")
    args = parser.parse_args()

    if not args.items:
        return 1
    try:
        if os.path.exists("/dev/tty") and os.access("/dev/tty", os.R_OK | os.W_OK):
            try:
                selected = interactive_select(args.title, args.items)
            except OSError:
                selected = fallback_select(args.title, args.items)
        else:
            selected = fallback_select(args.title, args.items)
    except KeyboardInterrupt:
        return 130
    print(selected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
