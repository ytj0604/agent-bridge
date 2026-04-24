#!/usr/bin/env python3
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import termios
import time
import uuid
import tty
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from bridge_participants import ALIAS_RE
from bridge_identity import detach_stale_pane_lock
from bridge_instructions import probe_prompt
from bridge_paths import install_root, libexec_dir, model_bin_dir, pin_runtime_roots, run_root, state_root
from bridge_ui import read_key, terminal_cells, terminal_width, truncate_to_cells
from bridge_util import locked_json, path_lock, utc_now, write_json_atomic


ROOT = install_root()
VALID_AGENTS = {"claude", "codex"}


def registry_file() -> Path:
    return state_root() / "attached-sessions.json"


def pane_locks_file() -> Path:
    return state_root() / "pane-locks.json"


def discovery_file() -> Path:
    return state_root() / "attach-discovery.jsonl"


class AttachCancelled(KeyboardInterrupt):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AttachProbeTimeout(TimeoutError):
    pass


def no_agent_panes_message(detail: str) -> str:
    return (
        f"no Claude Code/Codex tmux panes found ({detail}).\n"
        "bridge_run attaches to agents that are already running in tmux panes.\n"
        "Start tmux, create panes/windows, run claude and/or codex there, then rerun bridge_run.\n"
        "Example: tmux new -s work  # split panes, start claude/codex, then run bridge_run"
    )


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)


def require_tmux() -> None:
    if shutil.which("tmux"):
        return
    raise SystemExit(
        "tmux is not installed or not on PATH. Agent Bridge attaches to tmux panes, so tmux is required.\n"
        "Install tmux (e.g., `apt install tmux`, `brew install tmux`, or your distro's package manager), then rerun bridge_run."
    )


def tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    try:
        return run(["tmux", *args], check=check)
    except FileNotFoundError:
        require_tmux()
        raise  # unreachable; require_tmux raises SystemExit


def sanitize_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return cleaned or "bridge"


def shell_join(args: list[str]) -> str:
    return " ".join(shlex.quote(arg) for arg in args)


def parse_participant_spec(spec: str) -> dict:
    parts = spec.split(":", 2)
    if len(parts) != 3:
        raise SystemExit(
            "--participant must be alias:type:pane; use --participant-session alias=session_id to skip probing"
        )
    alias, agent_type, pane = parts
    session_id = ""
    if "@" in pane:
        pane, session_id = pane.rsplit("@", 1)
    elif pane.startswith("%") and pane.count(":") == 1:
        raise SystemExit("old alias:type:%pane:session_id format is no longer supported; use alias:type:%pane@SESSION_ID")
    alias = alias.strip()
    agent_type = agent_type.strip().lower()
    pane = pane.strip()
    session_id = session_id.strip()
    if not alias or not ALIAS_RE.match(alias):
        raise SystemExit(f"invalid participant alias: {alias!r}")
    if agent_type not in VALID_AGENTS:
        raise SystemExit(f"invalid participant type for {alias}: {agent_type!r}")
    if not pane:
        raise SystemExit(f"missing pane for participant {alias}")
    return {"alias": alias, "agent": agent_type, "pane_target": pane, "session_id": session_id}


def parse_participant_session(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise SystemExit("--participant-session must be alias=session_id")
    alias, session_id = value.split("=", 1)
    alias = alias.strip()
    session_id = session_id.strip()
    if not alias or not ALIAS_RE.match(alias):
        raise SystemExit(f"invalid participant alias in --participant-session: {alias!r}")
    if not session_id:
        raise SystemExit(f"missing session_id for {alias}")
    return alias, session_id


def apply_participant_sessions(specs: list[dict], values: list[str]) -> None:
    by_alias = {spec["alias"]: spec for spec in specs}
    for value in values:
        alias, session_id = parse_participant_session(value)
        if alias not in by_alias:
            raise SystemExit(f"--participant-session references unknown alias: {alias}")
        if by_alias[alias].get("session_id"):
            raise SystemExit(f"duplicate session id for participant alias: {alias}")
        by_alias[alias]["session_id"] = session_id


def list_panes() -> list[dict]:
    fmt = "#{pane_id}\t#{pane_pid}\t#{session_name}\t#{window_index}\t#{pane_index}\t#{pane_title}\t#{pane_current_command}\t#{pane_current_path}"
    proc = tmux("list-panes", "-a", "-F", fmt)
    panes = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 8:
            continue
        pane_id, pane_pid, session, window, pane, title, command, cwd = parts
        panes.append(
            {
                "pane_id": pane_id,
                "pane_pid": pane_pid,
                "target": f"{session}:{window}.{pane}",
                "session": session,
                "window": window,
                "pane": pane,
                "title": title,
                "command": command,
                "cwd": cwd,
            }
        )
    return panes


def load_process_table() -> dict:
    try:
        proc = run(["ps", "-axo", "pid=,ppid=,args="], check=False)
    except OSError as exc:
        return {"processes": {}, "children": {}, "error": f"ps unavailable: {exc}"}
    if proc.returncode != 0:
        return {"processes": {}, "children": {}, "error": proc.stderr.strip() or proc.stdout.strip()}
    processes: dict[int, dict] = {}
    children: dict[int, list[int]] = defaultdict(list)
    for line in proc.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        args = parts[2] if len(parts) > 2 else ""
        processes[pid] = {"pid": pid, "ppid": ppid, "args": args}
        children[ppid].append(pid)
    return {"processes": processes, "children": children, "error": ""}


def agent_score_from_args(args: str, agent: str) -> int:
    lowered = args.lower()
    if agent == "claude":
        if re.search(r"(^|[/\s])claude($|[\s/])", lowered):
            return 100
        if "/.local/share/claude/versions/" in lowered:
            return 100
    if agent == "codex":
        if re.search(r"(^|[/\s])codex($|[\s/])", lowered):
            return 100
        if re.search(r"(^|[/\s])codex-aarch64", lowered):
            return 100
        if "/applications/codex.app/" in lowered:
            return 80
    return 0


def pane_process_scores(pane: dict, process_table: dict | None = None) -> dict[str, int]:
    if process_table is None:
        process_table = load_process_table()
    processes = process_table.get("processes") or {}
    children = process_table.get("children") or {}
    try:
        root_pid = int(str(pane.get("pane_pid") or ""))
    except ValueError:
        root_pid = 0

    scores = {agent: 0 for agent in VALID_AGENTS}
    if root_pid and root_pid in processes:
        stack = [(root_pid, 0)]
        seen: set[int] = set()
        while stack:
            pid, depth = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            args = str((processes.get(pid) or {}).get("args") or "")
            for agent in VALID_AGENTS:
                base = agent_score_from_args(args, agent)
                if base:
                    scores[agent] = max(scores[agent], max(1, base - depth))
            for child in children.get(pid, []):
                stack.append((child, depth + 1))

    # tmux's current command is process metadata, not pane text. Use it only as
    # a strict fallback when ps is unavailable or the process exited mid-scan.
    command = str(pane.get("command") or "")
    for agent in VALID_AGENTS:
        scores[agent] = max(scores[agent], agent_score_from_args(command, agent))
    return scores


def pane_score(pane: dict, agent: str, process_table: dict | None = None) -> int:
    return pane_process_scores(pane, process_table).get(agent, 0)


def infer_agent_type(pane: dict, process_table: dict | None = None) -> str:
    scores = pane_process_scores(pane, process_table)
    best_agent, best_score = max(scores.items(), key=lambda item: item[1])
    if best_score <= 0:
        return ""
    tied = [agent for agent, score in scores.items() if score == best_score]
    return best_agent if len(tied) == 1 else ""


def resolve_pane(target: str, panes: list[dict]) -> dict:
    for pane in panes:
        if target in {pane["pane_id"], pane["target"]}:
            return pane
    proc = tmux("display-message", "-p", "-t", target, "#{pane_id}\t#{pane_pid}\t#{session_name}:#{window_index}.#{pane_index}\t#{pane_title}\t#{pane_current_command}\t#{pane_current_path}")
    parts = proc.stdout.rstrip("\n").split("\t")
    if len(parts) != 6:
        raise SystemExit(f"could not resolve pane target: {target}")
    pane_id, pane_pid, resolved_target, title, command, cwd = parts
    session, rest = resolved_target.split(":", 1)
    window, pane = rest.split(".", 1)
    return {
        "pane_id": pane_id,
        "pane_pid": pane_pid,
        "target": resolved_target,
        "session": session,
        "window": window,
        "pane": pane,
        "title": title,
        "command": command,
        "cwd": cwd,
    }


def describe_pane(index: int, pane: dict) -> str:
    title = pane["title"] or "-"
    agent = pane.get("detected_agent") or pane.get("agent")
    type_text = f"type={agent} " if agent else ""
    return (
        f"{index}. {pane['pane_id']} {pane['target']} "
        f"{type_text}pid={pane.get('pane_pid') or '-'} cmd={pane['command']!r} title={title!r} cwd={pane['cwd']}"
    )


def ui_width(max_width: int = 150) -> int:
    return max(24, min(terminal_width(), max_width))


def normalize_box_item(item: object) -> tuple[str, bool]:
    if isinstance(item, tuple) and len(item) == 2:
        text, selected = item
        return str(text), bool(selected)
    return str(item), False


def boxed(title: str, body: list[object], width: int) -> list[str]:
    width = max(8, width)
    inner = max(1, width - 4)
    title_text = truncate_to_cells(f" {title} ", max(1, width - 2))
    top = "┌" + title_text + "─" * max(0, width - 2 - terminal_cells(title_text)) + "┐"
    bottom = "└" + "─" * (width - 2) + "┘"
    lines = [top]
    if not body:
        body = [""]
    for item in body:
        line, selected = normalize_box_item(item)
        rendered = truncate_to_cells(line, inner)
        pad = max(0, inner - terminal_cells(rendered))
        content = f"{rendered}{' ' * pad}"
        if selected:
            content = f"\x1b[7m{content}\x1b[0m"
        lines.append(f"│ {content} │")
    lines.append(bottom)
    return lines


def preview_pane(pane: dict, lines: int = 16) -> list[str]:
    proc = tmux("capture-pane", "-p", "-t", pane["pane_id"], "-S", f"-{lines}", check=False)
    if proc.returncode != 0:
        return [f"(preview unavailable: {proc.stderr.strip() or proc.stdout.strip()})"]
    content = [line.rstrip() for line in proc.stdout.splitlines()]
    content = [line for line in content if line.strip()]
    if not content:
        return ["(empty pane preview)"]
    return content[-lines:]


def move_cursor_up(lines: int) -> None:
    if lines > 0:
        sys.stdout.write(f"\x1b[{lines}A")


def clear_to_screen_end() -> None:
    sys.stdout.write("\x1b[J")


def render_selector(agent: str, choices: list[dict], selected: int) -> int:
    width = ui_width(140)
    lines: list[str] = []
    lines.append(f"Agent Bridge Attach - select {agent} pane")
    lines.append("Keys: Up/Down = move, Enter = select, Esc = cancel")
    lines.append("")
    candidate_rows = []
    for idx, pane in enumerate(choices):
        marker = ">" if idx == selected else " "
        row = describe_pane(idx + 1, pane)
        candidate_rows.append((f"{marker} {row}", idx == selected))
    lines.extend(boxed("Candidates", candidate_rows, width))
    lines.append("")
    selected_pane = choices[selected]
    preview_title = f"Read-only preview of {selected_pane['pane_id']} {selected_pane['target']}"
    preview_lines = preview_pane(selected_pane)
    lines.extend(boxed(preview_title, preview_lines, width))
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()
    return len(lines)


def selected_items(items: list[dict]) -> list[dict]:
    return [item for item in items if item.get("selected")]


def assign_aliases(items: list[dict], args: argparse.Namespace) -> list[dict]:
    counts = Counter(item["agent"] for item in items)
    seen: dict[str, int] = defaultdict(int)
    used: set[str] = set()
    result = []
    for item in items:
        agent = item["agent"]
        seen[agent] += 1
        if counts[agent] == 1:
            if agent == "claude":
                base = args.claude_alias
            elif agent == "codex":
                base = args.codex_alias
            else:
                base = agent
        else:
            base = f"{agent}{seen[agent]}"
        alias = base
        suffix = 2
        while alias in used:
            alias = f"{base}-{suffix}"
            suffix += 1
        used.add(alias)
        result.append({**item, "alias": alias})
    return result


def alias_error(alias: str, used: set[str]) -> str:
    if not ALIAS_RE.match(alias):
        return "alias must start with a letter and contain only letters, numbers, dot, underscore, or dash"
    if alias in used:
        return f"duplicate alias: {alias}"
    return ""


def should_cancel(key: str, started_at: float) -> bool:
    if key in {"quit", "eof"}:
        return True
    if key == "escape":
        # tmux focus-events and some PTY layers can emit a bare ESC as the UI opens.
        return time.monotonic() - started_at >= 1.0
    return False


def prompt_participant_aliases(participants: list[dict]) -> list[dict]:
    width = ui_width(150)
    rows = []
    for idx, item in enumerate(participants):
        pane = item["pane"]
        title = pane.get("title") or "-"
        rows.append(
            f"{idx + 1}. default={item['alias']} type={item['agent']} "
            f"pane={pane['pane_id']} {pane['target']} title={title!r}"
        )
    print("Agent Bridge Attach - set participant aliases")
    print("Press Enter to keep the default shown in brackets. These aliases are used by agent_send_peer --to <alias>.")
    print("")
    print("\n".join(boxed("Selected participants", rows, width)))
    print("")

    used: set[str] = set()
    for idx, item in enumerate(participants):
        pane = item["pane"]
        default = item["alias"]
        title = pane.get("title") or "-"
        print("")
        alias_title = f"Alias {idx + 1}/{len(participants)} for {pane['pane_id']} {pane['target']}"
        alias_body = [
            f"type={item['agent']}",
            f"default alias={default}",
            f"title={title!r}",
        ]
        print("\n".join(boxed(alias_title, alias_body, width)))
        preview_title = f"Read-only preview of {pane['pane_id']} {pane['target']}"
        print("\n".join(boxed(preview_title, preview_pane(pane), width)))
        while True:
            prompt = f"Alias for {pane['pane_id']} {pane['target']} ({item['agent']}, title={title!r}) [{default}]: "
            sys.stdout.write(prompt)
            sys.stdout.flush()
            value = sys.stdin.readline()
            if value == "":
                raise AttachCancelled("eof")
            alias = value.strip() or default
            warning = alias_error(alias, used)
            if warning:
                print(f"Invalid alias: {warning}")
                continue
            item["alias"] = alias
            used.add(alias)
            break

    print("")
    print("Selected participants:")
    for item in participants:
        pane = item["pane"]
        print(f"- {item['alias']} ({item['agent']}): {pane['pane_id']} {pane['target']}")
    return participants


def render_multi_selector(items: list[dict], cursor: int, args: argparse.Namespace, warning: str = "") -> int:
    width = ui_width(150)
    lines: list[str] = []
    lines.append("Agent Bridge Attach - select participant panes")
    lines.append("Keys: Up/Down = move, Space = select/unselect, Enter = continue, Esc = cancel")
    lines.append("Alias names are entered in the next step; this screen only selects panes.")
    if warning:
        lines.append(f"Warning: {warning}")
    lines.append("")
    rows = []
    for idx, item in enumerate(items):
        pane = item["pane"]
        marker = ">" if idx == cursor else " "
        checkbox = "[✓]" if item.get("selected") else "[ ]"
        agent = item.get("agent") or "unknown"
        title = pane["title"] or "-"
        row = (
            f"{marker} {checkbox} {idx + 1}. {pane['pane_id']} {pane['target']} "
            f"type={agent} pid={pane.get('pane_pid') or '-'} cmd={pane['command']!r} title={title!r} cwd={pane['cwd']}"
        )
        rows.append((row, idx == cursor))
    lines.extend(boxed("Candidate tmux panes", rows, width))
    lines.append("")
    selected_pane = items[cursor]["pane"]
    preview_title = f"Read-only preview of {selected_pane['pane_id']} {selected_pane['target']}"
    preview_lines = preview_pane(selected_pane)
    lines.extend(boxed(preview_title, preview_lines, width))
    sys.stdout.write("\n".join(lines) + "\n")
    sys.stdout.flush()
    return len(lines)


def interactive_select_participants(panes: list[dict], args: argparse.Namespace) -> list[dict]:
    process_table = load_process_table()
    candidates = [pane for pane in panes if infer_agent_type(pane, process_table)]
    if not candidates:
        detail = process_table.get("error") or "no claude/codex process descendants found from tmux pane_pid"
        raise SystemExit(no_agent_panes_message(detail))
    items = [
        {
            "pane": pane,
            "agent": infer_agent_type(pane, process_table),
            "selected": False,
        }
        for pane in candidates
    ]
    if not items:
        raise SystemExit(no_agent_panes_message("no candidate tmux panes"))

    cursor = 0
    rendered = 0
    warning = ""
    started_at = time.monotonic()
    old_settings = termios.tcgetattr(sys.stdin.fileno())
    try:
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
        tty.setcbreak(sys.stdin.fileno())
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
        while True:
            move_cursor_up(rendered)
            clear_to_screen_end()
            rendered = render_multi_selector(items, cursor, args, warning)
            warning = ""
            key = read_key()
            if key == "up":
                cursor = (cursor - 1) % len(items)
            elif key == "down":
                cursor = (cursor + 1) % len(items)
            elif key == " ":
                items[cursor]["selected"] = not items[cursor].get("selected")
            elif key == "enter":
                chosen = selected_items(items)
                if len(chosen) < 2:
                    warning = "select at least two panes"
                    continue
                participants = []
                for item in assign_aliases(chosen, args):
                    participants.append(
                        {
                            "alias": item["alias"],
                            "agent": item["agent"],
                            "pane": item["pane"],
                            "session_id": "",
                        }
                    )
                move_cursor_up(rendered)
                clear_to_screen_end()
                rendered = 0
                termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_settings)
                participants = prompt_participant_aliases(participants)
                return participants
            elif should_cancel(key, started_at):
                raise AttachCancelled(key)
            elif key in {"escape", "quit", "eof"}:
                continue
            elif key == "ignore":
                continue
    finally:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_settings)


def interactive_select(agent: str, choices: list[dict]) -> dict:
    if not choices:
        raise SystemExit(f"no candidate panes for {agent}")
    selected = 0
    rendered = 0
    started_at = time.monotonic()
    old_settings = termios.tcgetattr(sys.stdin.fileno())
    try:
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
        tty.setcbreak(sys.stdin.fileno())
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
        while True:
            move_cursor_up(rendered)
            clear_to_screen_end()
            rendered = render_selector(agent, choices, selected)
            key = read_key()
            if key == "up":
                selected = (selected - 1) % len(choices)
            elif key == "down":
                selected = (selected + 1) % len(choices)
            elif key == "enter":
                move_cursor_up(rendered)
                clear_to_screen_end()
                print(f"Selected {agent}: {describe_pane(selected + 1, choices[selected])}")
                return choices[selected]
            elif should_cancel(key, started_at):
                raise AttachCancelled(key)
            elif key in {"escape", "quit", "eof"}:
                continue
            elif key == "ignore":
                continue
    finally:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_settings)


def select_pane(agent: str, panes: list[dict], explicit: str | None) -> dict:
    if explicit:
        return resolve_pane(explicit, panes)

    process_table = load_process_table()
    candidates = [pane for pane in panes if pane_score(pane, agent, process_table) > 0]
    if len(candidates) == 1 and not sys.stdin.isatty():
        return candidates[0]
    if len(candidates) == 1 and sys.stdin.isatty():
        return interactive_select(agent, candidates)

    if not candidates:
        detail = process_table.get("error") or f"no {agent} process descendants found from tmux pane_pid"
        raise SystemExit(no_agent_panes_message(detail))
    choices = candidates
    if not sys.stdin.isatty():
        raise SystemExit(
            f"could not uniquely select {agent} pane; pass --{agent}-pane explicitly"
        )

    return interactive_select(agent, choices)


def send_prompt(pane_id: str, prompt: str, submit_delay: float) -> None:
    tmux("send-keys", "-t", pane_id, "-l", prompt)
    time.sleep(submit_delay)
    tmux("send-keys", "-t", pane_id, "Enter")


def render_probe_wait(label: str, elapsed: float, timeout: float, frame: int) -> None:
    if not sys.stdout.isatty():
        return
    spinner = "|/-\\"[frame % 4]
    sys.stdout.write(f"\r{spinner} waiting for {label} to confirm bridge attach ({elapsed:0.0f}s/{timeout:0.0f}s)")
    sys.stdout.flush()


def clear_probe_wait() -> None:
    if sys.stdout.isatty():
        sys.stdout.write("\r\033[K")
        sys.stdout.flush()


def wait_for_probe(probe_id: str, agent: str, timeout: float, alias: str = "", pane_desc: str = "") -> dict:
    deadline = time.time() + timeout
    started = time.time()
    offset = 0
    frame = 0
    label = f"{alias or agent} ({agent})"
    path = discovery_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    while time.time() < deadline:
        elapsed = time.time() - started
        render_probe_wait(label, elapsed, timeout, frame)
        frame += 1
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            size = 0
        if size < offset:
            offset = 0
        with path.open("r", encoding="utf-8") as stream:
            stream.seek(offset)
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("attach_probe") == probe_id and record.get("agent") == agent and record.get("session_id"):
                    clear_probe_wait()
                    print(f"Probe confirmed: {label}")
                    return record
            offset = stream.tell()
        time.sleep(0.25)
    clear_probe_wait()
    location = f" pane {pane_desc}" if pane_desc else ""
    raise AttachProbeTimeout(
        f"{label}{location} did not confirm bridge attach within {timeout:0.0f}s.\n"
        "Check that the pane is not busy, waiting for permission/input, or blocked by a hook error.\n"
        "Run bridge_healthcheck if hooks look broken, then rerun bridge_run."
    )


def update_registry(mappings: list[dict], bridge_session: str) -> None:
    with locked_json(registry_file(), {"version": 1, "sessions": {}}) as data:
        sessions = data.setdefault("sessions", {})
        for key in list(sessions):
            if sessions[key].get("bridge_session") == bridge_session:
                del sessions[key]
        for mapping in mappings:
            key = f"{mapping['agent']}:{mapping['session_id']}"
            sessions[key] = mapping


def unique_room_archive_dir(state_dir: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = state_dir / "archive" / f"reattach-{stamp}-{os.getpid()}"
    archive_dir = base
    if archive_dir.exists():
        archive_dir = state_dir / "archive" / f"{base.name}-{uuid.uuid4().hex[:8]}"
    archive_dir.mkdir(parents=True, exist_ok=False)
    return archive_dir


def archive_file(path: Path, archive_dir: Path, archive_name: str, action: str) -> dict | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise SystemExit(f"cannot archive non-file room state path: {path}")
    dest = archive_dir / archive_name
    if dest.exists():
        dest = archive_dir / f"{archive_name}.{uuid.uuid4().hex[:8]}"
    if action == "move":
        path.replace(dest)
    elif action == "copy":
        shutil.copy2(path, dest)
    else:
        raise ValueError(f"unsupported archive action: {action}")
    return {"from": str(path), "to": str(dest), "action": action}


def prepare_room_state_files(
    session: str,
    state_dir: Path,
    events_file: Path,
    bus_file: Path,
    queue_file: Path,
) -> dict:
    legacy_session_migration_file = state_root() / f"session-{session}.json"
    aggregate_file = state_dir / "aggregates.json"
    candidates = [
        events_file,
        bus_file,
        queue_file,
        aggregate_file,
        state_dir / "session.json",
        legacy_session_migration_file,
    ]
    existing = [path for path in candidates if path.exists()]
    archive_info: dict = {}
    if existing:
        archive_dir = unique_room_archive_dir(state_dir)
        archived_files = []
        archive_info = {
            "archive_dir": str(archive_dir),
            "archived_at": utc_now(),
            "reason": "reattach",
            "files": archived_files,
        }

        for path, name in ((events_file, "events.jsonl"), (bus_file, "events.raw.jsonl")):
            record = archive_file(path, archive_dir, name, "move")
            if record:
                archived_files.append(record)

        with path_lock(queue_file):
            record = archive_file(queue_file, archive_dir, "pending.json", "move")
            if record:
                archived_files.append(record)
            write_json_atomic(queue_file, [])

        record = archive_file(aggregate_file, archive_dir, "aggregates.json", "move")
        if record:
            archived_files.append(record)

        for path, name in (
            (state_dir / "session.json", "session.json"),
            (legacy_session_migration_file, "legacy-session.json"),
        ):
            record = archive_file(path, archive_dir, name, "copy")
            if record:
                archived_files.append(record)

        manifest_file = archive_dir / "manifest.json"
        archive_info["manifest_file"] = str(manifest_file)
        write_json_atomic(manifest_file, archive_info)
    else:
        write_json_atomic(queue_file, [])

    events_file.parent.mkdir(parents=True, exist_ok=True)
    events_file.touch(exist_ok=True)
    bus_file.touch(exist_ok=True)
    if not queue_file.exists():
        write_json_atomic(queue_file, [])
    return archive_info


def update_pane_locks(mappings: list[dict], bridge_session: str, replace: bool) -> None:
    with locked_json(pane_locks_file(), {"version": 1, "panes": {}}) as data:
        panes = data.setdefault("panes", {})
        for pane_id, record in list(panes.items()):
            if record.get("bridge_session") == bridge_session:
                del panes[pane_id]
        for mapping in mappings:
            pane_id = mapping["pane"]
            existing = panes.get(pane_id)
            if existing and existing.get("bridge_session") != bridge_session and not replace:
                raise SystemExit(
                    f"pane {pane_id} is already locked by {existing.get('bridge_session')}; use replacement mode or stop that bridge"
                )
            if existing and existing.get("bridge_session") != bridge_session and replace:
                detach_stale_pane_lock(pane_id, f"pane reattached to {bridge_session}")
            panes[pane_id] = {
                "bridge_session": bridge_session,
                "agent": mapping["agent"],
                "alias": mapping["alias"],
                "target": mapping["target"],
                "hook_session_id": mapping["session_id"],
                "locked_at": utc_now(),
            }


def tmux_has_session(session: str) -> bool:
    return tmux("has-session", "-t", f"={session}", check=False).returncode == 0


def build_default_participant_specs(args: argparse.Namespace) -> list[dict]:
    specs = []
    if args.claude_pane:
        specs.append({"alias": args.claude_alias, "agent": "claude", "pane_target": args.claude_pane, "session_id": args.claude_session_id or ""})
    if args.codex_pane:
        specs.append({"alias": args.codex_alias, "agent": "codex", "pane_target": args.codex_pane, "session_id": args.codex_session_id or ""})
    return specs


def auto_participant_specs(panes: list[dict], args: argparse.Namespace) -> list[dict]:
    if sys.stdin.isatty():
        return interactive_select_participants(panes, args)

    process_table = load_process_table()
    detected = [
        {"agent": agent, "pane": pane, "session_id": ""}
        for pane in panes
        for agent in [infer_agent_type(pane, process_table)]
        if agent
    ]
    if len(detected) != 2:
        raise SystemExit(
            "could not uniquely auto-select participants without a TTY; pass repeated --participant alias:type:pane"
        )
    return [
        {
            "alias": item["alias"],
            "agent": item["agent"],
            "pane": item["pane"],
            "session_id": item.get("session_id") or "",
        }
        for item in assign_aliases(detected, args)
    ]


def resolve_participant_specs(specs: list[dict], panes: list[dict]) -> list[dict]:
    resolved = []
    aliases = set()
    pane_ids = set()
    for spec in specs:
        alias = spec["alias"]
        if alias in aliases:
            raise SystemExit(f"duplicate participant alias: {alias}")
        aliases.add(alias)
        pane = spec.get("pane") or resolve_pane(spec["pane_target"], panes)
        if pane["pane_id"] in pane_ids:
            raise SystemExit(f"duplicate participant pane: {pane['pane_id']}")
        pane_ids.add(pane["pane_id"])
        resolved.append({**spec, "pane": pane})
    if len(resolved) < 2:
        raise SystemExit("attach requires at least two participants")
    return resolved


def peer_names(participants: list[dict], alias: str) -> str:
    peers = []
    for item in participants:
        if item["alias"] == alias:
            continue
        pane = item.get("pane") or {}
        details = [f"type={item.get('agent') or 'unknown'}"]
        if item.get("model"):
            details.append(f"model={item['model']}")
        if pane.get("cwd"):
            details.append(f"cwd={pane['cwd']}")
        peers.append(f"{item['alias']}({', '.join(details)})")
    return ", ".join(peers) or "(none)"


def stop_existing_daemon_for_reattach(session: str) -> None:
    pid_file = run_root() / f"{session}.pid"
    if not pid_file.exists():
        return
    print(f"Stopping existing bridge daemon for {session} before reattach")
    proc = run(
        [
            str(libexec_dir() / "bridge_daemon_ctl.py"),
            "stop",
            "-s",
            session,
            "--no-cleanup",
            "--timeout",
            "5",
            "--json",
        ],
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise SystemExit(f"failed to stop existing bridge daemon before reattach: {detail}")


def start_daemon(args: argparse.Namespace, state: dict) -> dict:
    cmd = [
        str(libexec_dir() / "bridge_daemon_ctl.py"),
        "start",
        "--replace",
        "--json",
        "--session",
        args.session,
        "--state-file",
        state["bus_file"],
        "--public-state-file",
        state["events_file"],
        "--queue-file",
        state["queue_file"],
    ]
    first_claude = next((p.get("pane") for p in (state.get("participants") or {}).values() if p.get("agent_type") == "claude"), None)
    first_codex = next((p.get("pane") for p in (state.get("participants") or {}).values() if p.get("agent_type") == "codex"), None)
    if first_claude:
        cmd += ["--claude-pane", str(first_claude)]
    if first_codex:
        cmd += ["--codex-pane", str(first_codex)]
    proc = run(cmd)
    return json.loads(proc.stdout)


def write_state(state: dict) -> None:
    state_dir = Path(state["state_dir"])
    write_json_atomic(state_dir / "session.json", state)


def print_attach_summary(state: dict) -> None:
    participants = state.get("participants") or {}
    daemon = state.get("daemon") or {}
    print("")
    print(f"Bridge attached: {state.get('session')}")
    print("Participants:")
    for alias, item in participants.items():
        print(
            f"- {alias} ({item.get('agent_type')}): "
            f"{item.get('pane')} {item.get('target')}"
        )
    if daemon.get("pid"):
        print(f"Daemon: pid {daemon.get('pid')}")
    elif daemon.get("status"):
        print(f"Daemon: {daemon.get('status')}")
    archived_state = state.get("archived_state") or {}
    if archived_state.get("archive_dir"):
        print(f"Archived previous state: {archived_state.get('archive_dir')}")
    print("Send: agent_send_peer --to <alias> 'message'")
    print("Broadcast: agent_send_peer --all 'message'")
    print("View peer screen: agent_view_peer <alias> --onboard")


def main() -> int:
    parser = argparse.ArgumentParser(description="Attach Agent Bridge to existing tmux panes.")
    parser.add_argument("-s", "--session", default="agent-bridge-auto", help="bridge session/state name")
    parser.add_argument(
        "-p",
        "--participant",
        action="append",
        default=[],
        help="participant as alias:type:pane; may be repeated; pane may be a tmux pane id/target; append @SESSION_ID only with --no-probe",
    )
    parser.add_argument(
        "--participant-session",
        action="append",
        default=[],
        metavar="ALIAS=SESSION_ID",
        help="hook session id for --no-probe; may be repeated",
    )
    parser.add_argument("--no-resolve-pane", action="store_true", help="advanced/test mode: do not ask tmux to resolve --participant panes")
    parser.add_argument("--claude-pane", help="tmux pane id or target for Claude Code")
    parser.add_argument("--codex-pane", help="tmux pane id or target for Codex")
    parser.add_argument("--claude-alias", default="claude")
    parser.add_argument("--codex-alias", default="codex")
    parser.add_argument("--probe-timeout", type=float, default=45.0)
    parser.add_argument("--submit-delay", type=float, default=0.4)
    parser.add_argument("--no-replace-locks", dest="replace_locks", action="store_false", default=True)
    parser.add_argument("--no-probe", action="store_true", help="skip probe; requires --participant-session, @SESSION_ID, or legacy --*-session-id")
    parser.add_argument("--claude-session-id")
    parser.add_argument("--codex-session-id")
    parser.add_argument("--dry-run", action="store_true", help="only resolve pane selections; do not probe, register, or start daemon")
    parser.add_argument("--no-daemon", action="store_true", help="register state and hooks only; do not start/replace the background daemon")
    parser.add_argument("--json", action="store_true", help="print full attached bridge state as JSON")
    args = parser.parse_args()

    require_tmux()
    panes = [] if args.no_resolve_pane and args.participant else list_panes()
    if not panes and not (args.no_resolve_pane and args.participant):
        raise SystemExit("no tmux panes found. Start a tmux session with Claude Code/Codex panes, then rerun bridge_run.")
    if args.participant_session and not args.participant:
        raise SystemExit("--participant-session is only valid with --participant")

    if args.participant:
        participant_specs = [parse_participant_spec(spec) for spec in args.participant]
        apply_participant_sessions(participant_specs, args.participant_session)
        if args.claude_pane or args.codex_pane:
            raise SystemExit("use either --participant entries or legacy --claude-pane/--codex-pane, not both")
        if args.no_resolve_pane:
            participants = []
            aliases = set()
            pane_ids = set()
            for spec in participant_specs:
                if spec["alias"] in aliases:
                    raise SystemExit(f"duplicate participant alias: {spec['alias']}")
                if spec["pane_target"] in pane_ids:
                    raise SystemExit(f"duplicate participant pane: {spec['pane_target']}")
                aliases.add(spec["alias"])
                pane_ids.add(spec["pane_target"])
                participants.append(
                    {
                        **spec,
                        "pane": {
                            "pane_id": spec["pane_target"],
                            "target": spec["pane_target"],
                            "cwd": "",
                        },
                    }
                )
            if len(participants) < 2:
                raise SystemExit("attach requires at least two participants")
        else:
            participants = resolve_participant_specs(participant_specs, panes)
    elif args.claude_pane or args.codex_pane:
        participant_specs = build_default_participant_specs(args)
        participants = resolve_participant_specs(participant_specs, panes)
    else:
        participants = auto_participant_specs(panes, args)

    if args.dry_run:
        dry_state = {
            "session": args.session,
            "mode": "attach",
            "dry_run": True,
            "participants": [
                {
                    "alias": item["alias"],
                    "agent": item["agent"],
                    "pane": item["pane"],
                    "session_id": item.get("session_id") or "",
                }
                for item in participants
            ],
        }
        if args.json:
            print(json.dumps(dry_state, ensure_ascii=True, indent=2))
        else:
            print(f"Bridge attach dry run: {args.session}")
            print("Participants:")
            for item in dry_state["participants"]:
                pane = item["pane"]
                print(f"- {item['alias']} ({item['agent']}): {pane.get('pane_id')} {pane.get('target')}")
        return 0

    try:
        pin_runtime_roots()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    state_dir = state_root() / args.session
    events_file = state_dir / "events.jsonl"
    bus_file = state_dir / "events.raw.jsonl"
    queue_file = state_dir / "pending.json"
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        discovery_file().parent.mkdir(parents=True, exist_ok=True)
        discovery_file().touch(exist_ok=True)
    except OSError as exc:
        raise SystemExit(
            f"Agent Bridge state root is not writable while creating room {args.session!r}: {state_root()}: {exc}. "
            "Restore write permissions, or recreate the room with AGENT_BRIDGE_STATE_DIR pointing to a writable filesystem."
        ) from exc

    probe_records = {}
    if args.no_probe:
        for item in participants:
            if not item.get("session_id"):
                raise SystemExit(f"--no-probe requires hook session id for {item['alias']}; pass --participant-session {item['alias']}=SESSION_ID or alias:type:pane@SESSION_ID")
            probe_records[item["alias"]] = {"session_id": item["session_id"]}
    else:
        probes = []
        for item in participants:
            agent = item["agent"]
            alias = item["alias"]
            pane = item["pane"]
            probe_id = f"{args.session}:{agent}:{uuid.uuid4().hex[:10]}"
            prompt = probe_prompt("attach", probe_id, alias, peer_names(participants, alias))
            print(f"Probing {alias} ({agent}) pane {pane['pane_id']} ({pane['target']})")
            send_prompt(pane["pane_id"], prompt, args.submit_delay)
            probes.append((alias, agent, probe_id, pane))
        for alias, agent, probe_id, pane in probes:
            try:
                probe_records[alias] = wait_for_probe(
                    probe_id,
                    agent,
                    args.probe_timeout,
                    alias=alias,
                    pane_desc=f"{pane['pane_id']} ({pane['target']})",
                )
            except AttachProbeTimeout as exc:
                raise SystemExit(str(exc)) from exc

    stop_existing_daemon_for_reattach(args.session)
    archived_state = prepare_room_state_files(args.session, state_dir, events_file, bus_file, queue_file)

    mappings = []
    for item in participants:
        agent = item["agent"]
        alias = item["alias"]
        pane = item["pane"]
        session_id = probe_records[alias]["session_id"]
        mappings.append(
            {
                "agent": agent,
                "alias": alias,
                "session_id": session_id,
                "bridge_session": args.session,
                "pane": pane["pane_id"],
                "target": pane["target"],
                "events_file": str(bus_file),
                "public_events_file": str(events_file),
                "queue_file": str(queue_file),
                "attached_at": utc_now(),
            }
        )

    update_registry(mappings, args.session)
    update_pane_locks(mappings, args.session, args.replace_locks)

    state = {
        "session": args.session,
        "mode": "attach",
        "created_at": utc_now(),
        "cwd": next((item["pane"].get("cwd") for item in participants if item["pane"].get("cwd")), str(Path.cwd())),
        "state_dir": str(state_dir),
        "events_file": str(events_file),
        "bus_file": str(bus_file),
        "queue_file": str(queue_file),
        "panes": {item["alias"]: item["pane"]["pane_id"] for item in participants} | {"daemon": "", "log": ""},
        "targets": {item["alias"]: item["pane"]["target"] for item in participants} | {"daemon": "", "log": ""},
        "hook_session_ids": {item["alias"]: probe_records[item["alias"]]["session_id"] for item in participants},
        "participants": {
            item["alias"]: {
                "alias": item["alias"],
                "agent_type": item["agent"],
                "pane": item["pane"]["pane_id"],
                "target": item["pane"]["target"],
                "hook_session_id": probe_records[item["alias"]]["session_id"],
                "cwd": probe_records[item["alias"]].get("cwd") or item["pane"].get("cwd") or "",
                "model": probe_records[item["alias"]].get("model") or "",
                "status": "active",
            }
            for item in participants
        },
    }
    if archived_state:
        state["archived_state"] = archived_state

    if args.no_daemon:
        state["daemon"] = {
            "status": "not_started",
        }
        state["targets"]["daemon"] = ""
    else:
        daemon_info = start_daemon(args, state)
        state["daemon"] = {
            "pid": daemon_info.get("pid"),
            "pid_file": daemon_info.get("pid_file"),
            "log_file": daemon_info.get("log_file"),
            "command_socket": daemon_info.get("command_socket"),
        }
        state["targets"]["daemon"] = f"pid:{daemon_info.get('pid')}"
    write_state(state)

    if args.json:
        print(json.dumps(state, ensure_ascii=True, indent=2))
    else:
        print_attach_summary(state)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AttachCancelled as exc:
        if exc.reason == "eof":
            print("bridge attach input closed (EOF)", file=sys.stderr)
        elif exc.reason == "quit":
            print("bridge attach interrupted", file=sys.stderr)
        else:
            print("bridge attach cancelled", file=sys.stderr)
        sys.exit(130)
    except KeyboardInterrupt:
        print("bridge attach cancelled", file=sys.stderr)
        sys.exit(130)
