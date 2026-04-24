#!/usr/bin/env python3
import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from bridge_participants import active_participants, load_session, session_state_exists
from bridge_identity import resolve_participant_endpoint
from bridge_paths import install_root, libexec_dir, log_root, python_exe, run_root, state_root
from bridge_util import locked_json, read_json, utc_now, write_json_atomic as write_json


ROOT = install_root()
RUN_DIR = run_root()
LOG_DIR = log_root()
STATE_DIR = state_root()
REGISTRY_FILE = STATE_DIR / "attached-sessions.json"
PANE_LOCKS_FILE = STATE_DIR / "pane-locks.json"
PYTHON = python_exe()
DAEMON = libexec_dir() / "bridge_daemon.py"


def session_paths(session: str) -> dict[str, Path]:
    return {
        "pid": RUN_DIR / f"{session}.pid",
        "meta": RUN_DIR / f"{session}.json",
        "lock": RUN_DIR / f"{session}.lock",
        "stop": RUN_DIR / f"{session}.stop",
        "log": LOG_DIR / f"{session}.daemon.log",
    }


def is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def read_pid(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def tail_text(path: Path, max_chars: int = 3000) -> str:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return ""
    return data[-max_chars:].decode("utf-8", errors="replace")


def daemon_command(args: argparse.Namespace) -> list[str]:
    cmd = [
        PYTHON,
        str(DAEMON),
        "--state-file",
        args.state_file,
        "--public-state-file",
        args.public_state_file,
        "--queue-file",
        args.queue_file,
        "--bridge-session",
        args.session,
        "--session-file",
        str(state_root() / args.session / "session.json"),
        "--stop-file",
        str(session_paths(args.session)["stop"]),
    ]
    if args.claude_pane:
        cmd += ["--claude-pane", args.claude_pane]
    if args.codex_pane:
        cmd += ["--codex-pane", args.codex_pane]
    if args.max_hops is not None:
        cmd += ["--max-hops", str(args.max_hops)]
    if args.submit_delay is not None:
        cmd += ["--submit-delay", str(args.submit_delay)]
    if args.submit_timeout is not None:
        cmd += ["--submit-timeout", str(args.submit_timeout)]
    return cmd


def terminate_pid(pid: int, timeout: float) -> str:
    if not is_alive(pid):
        return "not_running"
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return "not_running"
    except PermissionError:
        pgid = None

    try:
        if pgid is not None:
            os.killpg(pgid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "not_running"
    except PermissionError:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return "not_running"
        except PermissionError:
            return "permission_denied"

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_alive(pid):
            return "terminated"
        time.sleep(0.1)

    try:
        if pgid is not None:
            os.killpg(pgid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return "terminated"
    except PermissionError:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return "terminated"
        except PermissionError:
            return "permission_denied"

    deadline = time.time() + 2.0
    while time.time() < deadline:
        if not is_alive(pid):
            return "killed"
        time.sleep(0.1)
    return "kill_failed"


def request_cooperative_stop(paths: dict[str, Path], session: str) -> None:
    paths["stop"].write_text(
        json.dumps({"session": session, "requested_at": utc_now()}, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def wait_until_dead(pid: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_alive(pid):
            return True
        time.sleep(0.1)
    return not is_alive(pid)


def cleanup_registry(session: str) -> None:
    for path in (REGISTRY_FILE, PANE_LOCKS_FILE):
        top_key = "sessions" if path == REGISTRY_FILE else "panes"
        with locked_json(path, {"version": 1, top_key: {}}) as data:
            records = data.setdefault(top_key, {})
            for key in list(records):
                if records[key].get("bridge_session") == session:
                    del records[key]


def archive_session_state(session: str) -> str:
    state_dir = STATE_DIR / session
    if not state_dir.exists():
        return ""
    archive_root = STATE_DIR / ".forgotten"
    archive_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = archive_root / f"{session}-{stamp}"
    if dest.exists():
        dest = archive_root / f"{session}-{stamp}-{os.getpid()}"
    state_dir.rename(dest)
    return str(dest)


def cleanup_stopped_session(session: str, paths: dict[str, Path], cleanup: bool) -> str:
    paths["pid"].unlink(missing_ok=True)
    paths["meta"].unlink(missing_ok=True)
    paths["stop"].unlink(missing_ok=True)
    if cleanup:
        cleanup_registry(session)
    # Remove a pre-structured-state file if an older install left one behind.
    (STATE_DIR / f"session-{session}.json").unlink(missing_ok=True)
    return archive_session_state(session)


def tmux_send_literal(pane: str, text: str, submit_delay: float = 0.05) -> None:
    subprocess.run(
        ["tmux", "send-keys", "-t", pane, "-l", text],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if submit_delay > 0:
        time.sleep(submit_delay)
    subprocess.run(
        ["tmux", "send-keys", "-t", pane, "Enter"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def send_room_closed_notices(session: str) -> dict:
    state = load_session(session)
    notice = (
        f"[bridge:room_closed] Room {session} is closed; peer routing has stopped. "
        "Wait for the human to attach/start a new room before using agent_send_peer."
    )
    sent = 0
    errors = []
    for alias, record in active_participants(state).items():
        pane = resolve_participant_endpoint(session, alias, record)
        if not pane:
            continue
        try:
            tmux_send_literal(pane, notice)
            sent += 1
        except (OSError, subprocess.CalledProcessError) as exc:
            stderr = getattr(exc, "stderr", "") or str(exc)
            errors.append(f"{alias}:{pane}: {stderr.strip()}")
    return {"sent": sent, "errors": errors}


def start_under_lock(args: argparse.Namespace, paths: dict[str, Path]) -> dict:
    existing_pid = read_pid(paths["pid"])
    if existing_pid and is_alive(existing_pid):
        if not args.replace:
            raise SystemExit(f"bridge daemon already running for {args.session}: pid {existing_pid}")
        request_cooperative_stop(paths, args.session)
        if not wait_until_dead(existing_pid, min(args.stop_timeout, 2.0)):
            status = terminate_pid(existing_pid, args.stop_timeout)
            if status in {"permission_denied", "kill_failed"} and is_alive(existing_pid):
                raise SystemExit(f"could not replace running bridge daemon {args.session}: {status}")
    elif existing_pid:
        paths["pid"].unlink(missing_ok=True)
    paths["stop"].unlink(missing_ok=True)

    cmd = daemon_command(args)
    if args.dry_run:
        return {
            "session": args.session,
            "dry_run": True,
            "command": cmd,
            "pid_file": str(paths["pid"]),
            "log_file": str(paths["log"]),
        }

    log = paths["log"].open("ab", buffering=0)
    log.write(f"\n--- bridge daemon start {utc_now()} session={args.session} ---\n".encode("utf-8"))
    proc = subprocess.Popen(
        cmd,
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
    time.sleep(args.health_delay)
    if proc.poll() is not None:
        log.close()
        raise SystemExit(
            f"bridge daemon exited during startup with code {proc.returncode}\n{tail_text(paths['log'])}"
        )

    paths["pid"].write_text(f"{proc.pid}\n", encoding="utf-8")
    meta = {
        "session": args.session,
        "pid": proc.pid,
        "started_at": utc_now(),
        "status": "running",
        "pid_file": str(paths["pid"]),
        "log_file": str(paths["log"]),
        "command": cmd,
        "claude_pane": args.claude_pane,
        "codex_pane": args.codex_pane,
        "state_file": args.state_file,
        "public_state_file": args.public_state_file,
        "queue_file": args.queue_file,
    }
    write_json(paths["meta"], meta)
    log.close()
    return meta


def start(args: argparse.Namespace) -> dict:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    paths = session_paths(args.session)
    with paths["lock"].open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        result = start_under_lock(args, paths)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return result


def first_participant_pane(state: dict, agent_type: str) -> str:
    for record in active_participants(state).values():
        if record.get("agent_type") == agent_type and record.get("pane"):
            return str(record["pane"])
    return ""


def update_session_daemon_info(session: str, state: dict, daemon_info: dict) -> None:
    state.setdefault("targets", {})
    state["daemon"] = {
        "pid": daemon_info.get("pid"),
        "pid_file": daemon_info.get("pid_file"),
        "log_file": daemon_info.get("log_file"),
        "restarted_at": utc_now(),
    }
    if daemon_info.get("pid"):
        state["targets"]["daemon"] = f"pid:{daemon_info.get('pid')}"
    write_json(STATE_DIR / session / "session.json", state)


def ensure_args_from_state(session: str, state: dict, args: argparse.Namespace) -> argparse.Namespace:
    state_dir = Path(state.get("state_dir") or STATE_DIR / session)
    return argparse.Namespace(
        session=session,
        claude_pane=first_participant_pane(state, "claude"),
        codex_pane=first_participant_pane(state, "codex"),
        state_file=str(state.get("bus_file") or state.get("state_file") or state_dir / "events.raw.jsonl"),
        public_state_file=str(state.get("events_file") or state.get("public_state_file") or state_dir / "events.jsonl"),
        queue_file=str(state.get("queue_file") or state_dir / "pending.json"),
        replace=False,
        dry_run=args.dry_run,
        health_delay=args.health_delay,
        stop_timeout=args.stop_timeout,
        max_hops=args.max_hops,
        submit_delay=args.submit_delay,
        submit_timeout=args.submit_timeout,
    )


def ensure_one(session: str, args: argparse.Namespace) -> dict:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    paths = session_paths(session)
    with paths["lock"].open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        pid = read_pid(paths["pid"])
        if pid and is_alive(pid):
            result = {
                "session": session,
                "status": "running",
                "pid": pid,
                "pid_file": str(paths["pid"]),
                "log_file": str(paths["log"]),
                "restarted": False,
            }
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            return result

        if paths["stop"].exists():
            raise SystemExit(f"cannot auto-restart bridge room {session}: stop is in progress")
        if not session_state_exists(session):
            raise SystemExit(f"cannot auto-restart bridge room {session}: active room state is missing")

        meta = read_json(paths["meta"], {"session": session})
        if not pid and meta.get("status") in {"not_running", "terminated", "killed", "stopped", "forgotten"}:
            raise SystemExit(f"cannot auto-restart bridge room {session}: room was intentionally stopped")

        state = load_session(session)
        if (state.get("daemon") or {}).get("status") == "not_started":
            raise SystemExit(f"cannot auto-restart bridge room {session}: room was created with daemon disabled")
        if not active_participants(state):
            raise SystemExit(f"cannot auto-restart bridge room {session}: no active participants in room state")

        start_args = ensure_args_from_state(session, state, args)
        daemon_info = start_under_lock(start_args, paths)
        if not args.dry_run:
            update_session_daemon_info(session, state, daemon_info)
        result = {
            **daemon_info,
            "status": "restarted" if not args.dry_run else "would_restart",
            "restarted": not args.dry_run,
        }
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return result


def stop_one(session: str, timeout: float, cleanup: bool) -> dict:
    paths = session_paths(session)
    remove_lock = False
    with paths["lock"].open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        pid = read_pid(paths["pid"])
        if pid is None:
            status = "not_running"
        else:
            request_cooperative_stop(paths, session)
            if wait_until_dead(pid, min(timeout, 5.0)):
                status = "terminated"
            else:
                status = terminate_pid(pid, timeout)
        if status in {"not_running", "terminated", "killed"}:
            paths["pid"].unlink(missing_ok=True)
            paths["stop"].unlink(missing_ok=True)
        meta = read_json(paths["meta"], {"session": session})
        meta.update({"session": session, "pid": pid, "status": status, "stopped_at": utc_now()})
        if cleanup and status in {"not_running", "terminated", "killed"}:
            notice_result = send_room_closed_notices(session)
            meta["room_closed_notice_sent"] = notice_result["sent"]
            if notice_result["errors"]:
                meta["room_closed_notice_errors"] = notice_result["errors"]
            archived_state = cleanup_stopped_session(session, paths, cleanup=True)
            meta["cleanup"] = "forgotten"
            if archived_state:
                meta["archived_state"] = archived_state
            remove_lock = True
        else:
            write_json(paths["meta"], meta)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    if remove_lock:
        paths["lock"].unlink(missing_ok=True)
    return meta


def forget_one(session: str, cleanup: bool) -> dict:
    paths = session_paths(session)
    archived_state = ""
    with paths["lock"].open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        pid = read_pid(paths["pid"])
        if pid and is_alive(pid):
            raise SystemExit(f"cannot forget running bridge daemon {session}: pid {pid}")
        archived_state = cleanup_stopped_session(session, paths, cleanup)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    paths["lock"].unlink(missing_ok=True)
    result = {"session": session, "status": "forgotten"}
    if archived_state:
        result["archived_state"] = archived_state
    return result


def known_sessions() -> list[str]:
    names = set()
    if RUN_DIR.exists():
        names.update(path.stem for path in RUN_DIR.glob("*.pid"))
        names.update(path.stem for path in RUN_DIR.glob("*.json"))
    if STATE_DIR.exists():
        for path in STATE_DIR.glob("*/session.json"):
            names.add(path.parent.name)
    return sorted(names)


def status_rows(session: str | None = None, all_sessions: bool = False) -> list[dict]:
    sessions = [session] if session else known_sessions()
    rows = []
    for name in sessions:
        paths = session_paths(name)
        meta = read_json(paths["meta"], {"session": name})
        state = read_json(STATE_DIR / name / "session.json", {})
        pid = read_pid(paths["pid"])
        alive = bool(pid and is_alive(pid))
        if alive and paths["stop"].exists() and meta.get("status") == "permission_denied":
            run_state = "permission_denied"
        elif alive and paths["stop"].exists():
            run_state = "stopping"
        elif pid and not alive:
            run_state = "stale"
        elif alive:
            run_state = "running"
        elif not pid and meta.get("status") == "running":
            run_state = "stale"
        else:
            run_state = meta.get("status") or "stopped"
        row = {
            "session": name,
            "status": run_state,
            "pid": pid,
            "mode": state.get("mode") or meta.get("mode") or "",
            "participants": ",".join(sorted((state.get("participants") or {}).keys())),
            "targets": ",".join(
                f"{alias}={target}"
                for alias, target in sorted((state.get("targets") or {}).items())
                if alias not in {"daemon", "log"} and target
            ),
            "log_file": str(paths["log"]) if paths["log"].exists() else meta.get("log_file", ""),
        }
        if all_sessions or run_state in {"running", "stopping", "stale", "permission_denied"}:
            rows.append(row)
    return rows


def print_table(rows: list[dict]) -> None:
    if not rows:
        print("no bridge daemons found")
        return
    headers = ["session", "status", "pid", "mode", "participants", "targets", "log_file"]
    widths = {key: len(key) for key in headers}
    for row in rows:
        for key in headers:
            widths[key] = max(widths[key], len(str(row.get(key) or "")))
    print("  ".join(key.ljust(widths[key]) for key in headers))
    print("  ".join("-" * widths[key] for key in headers))
    for row in rows:
        print("  ".join(str(row.get(key) or "").ljust(widths[key]) for key in headers))


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage Agent Bridge background daemons.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start")
    p_start.add_argument("-s", "--session", required=True)
    p_start.add_argument("--claude-pane")
    p_start.add_argument("--codex-pane")
    p_start.add_argument("--state-file", required=True)
    p_start.add_argument("--public-state-file", required=True)
    p_start.add_argument("--queue-file", required=True)
    p_start.add_argument("--replace", action="store_true")
    p_start.add_argument("--dry-run", action="store_true")
    p_start.add_argument("--json", action="store_true")
    p_start.add_argument("--health-delay", type=float, default=0.35)
    p_start.add_argument("--stop-timeout", type=float, default=5.0)
    p_start.add_argument("--max-hops", type=int)
    p_start.add_argument("--submit-delay", type=float)
    p_start.add_argument("--submit-timeout", type=float)

    p_stop = sub.add_parser("stop")
    p_stop.add_argument("-s", "--session")
    p_stop.add_argument("--all", action="store_true")
    p_stop.add_argument("--timeout", type=float, default=5.0)
    p_stop.add_argument("--no-cleanup", dest="cleanup", action="store_false", default=True)
    p_stop.add_argument("--json", action="store_true")

    p_ensure = sub.add_parser("ensure")
    p_ensure.add_argument("-s", "--session", required=True)
    p_ensure.add_argument("--json", action="store_true")
    p_ensure.add_argument("--quiet", action="store_true")
    p_ensure.add_argument("--dry-run", action="store_true")
    p_ensure.add_argument("--health-delay", type=float, default=0.35)
    p_ensure.add_argument("--stop-timeout", type=float, default=5.0)
    p_ensure.add_argument("--max-hops", type=int)
    p_ensure.add_argument("--submit-delay", type=float)
    p_ensure.add_argument("--submit-timeout", type=float)

    p_status = sub.add_parser("status")
    p_status.add_argument("-s", "--session")
    p_status.add_argument("--all", action="store_true", help="include stopped/known bridge sessions")
    p_status.add_argument("--json", action="store_true")

    p_forget = sub.add_parser("forget")
    p_forget.add_argument("-s", "--session", required=True)
    p_forget.add_argument("--no-cleanup", dest="cleanup", action="store_false", default=True)
    p_forget.add_argument("--json", action="store_true")

    args = parser.parse_args()

    if args.command == "start":
        result = start(args)
        if args.json:
            print(json.dumps(result, ensure_ascii=True, indent=2))
        else:
            if result.get("dry_run"):
                print(" ".join(result["command"]))
            else:
                print(f"started bridge daemon {result['session']} pid={result['pid']} log={result['log_file']}")
        return 0

    if args.command == "stop":
        if args.all:
            sessions = known_sessions()
        elif args.session:
            sessions = [args.session]
        else:
            raise SystemExit("stop requires --session or --all")
        results = [stop_one(name, args.timeout, args.cleanup) for name in sessions]
        if args.json:
            print(json.dumps(results, ensure_ascii=True, indent=2))
        else:
            for result in results:
                suffix = " (cleaned)" if result.get("cleanup") == "forgotten" else ""
                print(f"{result['session']}: {result['status']}{suffix}")
        return 0

    if args.command == "ensure":
        result = ensure_one(args.session, args)
        if args.json:
            print(json.dumps(result, ensure_ascii=True, indent=2))
        elif not args.quiet:
            if result.get("restarted"):
                print(f"{result['session']}: restarted pid={result.get('pid')}")
            else:
                print(f"{result['session']}: {result.get('status')} pid={result.get('pid')}")
        return 0

    if args.command == "status":
        rows = status_rows(args.session, args.all)
        if args.json:
            print(json.dumps(rows, ensure_ascii=True, indent=2))
        else:
            print_table(rows)
        return 0

    if args.command == "forget":
        result = forget_one(args.session, args.cleanup)
        if args.json:
            print(json.dumps(result, ensure_ascii=True, indent=2))
        else:
            print(f"{result['session']}: {result['status']}")
        return 0

    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.exit(130)
