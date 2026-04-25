#!/usr/bin/env python3
import argparse
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from bridge_participants import active_participants, load_session, session_state_exists
from bridge_identity import resolve_participant_endpoint
from bridge_paths import ensure_runtime_writable, install_root, libexec_dir, log_root, python_exe, run_root, state_root
from bridge_util import locked_json, read_json, utc_now, write_json_atomic as write_json


ROOT = install_root()
PYTHON = python_exe()
DAEMON = libexec_dir() / "bridge_daemon.py"


def registry_file() -> Path:
    return state_root() / "attached-sessions.json"


def pane_locks_file() -> Path:
    return state_root() / "pane-locks.json"


def session_paths(session: str) -> dict[str, Path]:
    run_dir = run_root()
    log_dir = log_root()
    return {
        "pid": run_dir / f"{session}.pid",
        "meta": run_dir / f"{session}.json",
        "lock": run_dir / f"{session}.lock",
        "stop": run_dir / f"{session}.stop",
        "socket": run_dir / f"{session}.sock",
        "log": log_dir / f"{session}.daemon.log",
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
        "--command-socket",
        str(session_paths(args.session)["socket"]),
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
    for path, top_key in ((registry_file(), "sessions"), (pane_locks_file(), "panes")):
        with locked_json(path, {"version": 1, top_key: {}}) as data:
            records = data.setdefault(top_key, {})
            for key in list(records):
                if records[key].get("bridge_session") == session:
                    del records[key]


DEFAULT_FORGOTTEN_RETENTION = 10


def _resolve_forgotten_retention() -> int:
    raw = os.environ.get("AGENT_BRIDGE_FORGOTTEN_RETENTION_COUNT")
    if raw is None or str(raw).strip() == "":
        return DEFAULT_FORGOTTEN_RETENTION
    try:
        value = int(str(raw).strip())
    except ValueError:
        sys.stderr.write(
            f"agent-bridge: invalid AGENT_BRIDGE_FORGOTTEN_RETENTION_COUNT={raw!r}; using default {DEFAULT_FORGOTTEN_RETENTION}\n"
        )
        return DEFAULT_FORGOTTEN_RETENTION
    if value < 0:
        sys.stderr.write(
            f"agent-bridge: AGENT_BRIDGE_FORGOTTEN_RETENTION_COUNT={value} is negative; using default {DEFAULT_FORGOTTEN_RETENTION}\n"
        )
        return DEFAULT_FORGOTTEN_RETENTION
    return value


def prune_forgotten_archives(retention_count: int | None = None) -> dict:
    """Remove old `.forgotten/` archives, keeping the most recent N directories.

    `retention_count` (or env `AGENT_BRIDGE_FORGOTTEN_RETENTION_COUNT`) is the
    number to keep. 0 disables pruning. Concurrent / missing-archive races
    are tolerated silently — partial success returns a `removed` list and an
    `errors` map.
    """
    if retention_count is None:
        retention_count = _resolve_forgotten_retention()
    if retention_count <= 0:
        return {"retention": retention_count, "kept": 0, "removed": [], "errors": {}}
    archive_root = state_root() / ".forgotten"
    if not archive_root.exists():
        return {"retention": retention_count, "kept": 0, "removed": [], "errors": {}}
    try:
        entries = [p for p in archive_root.iterdir() if p.is_dir()]
    except OSError as exc:
        return {"retention": retention_count, "kept": 0, "removed": [], "errors": {"_root": str(exc)}}
    # P2: stat() can race with concurrent prune (FileNotFoundError) on
    # entries that another writer removed between iterdir and now. Treat
    # missing entries as "already pruned": skip them silently.
    def _safe_mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except FileNotFoundError:
            return -1.0
        except OSError:
            return 0.0

    entries = [(p, _safe_mtime(p)) for p in entries]
    entries = [(p, mt) for p, mt in entries if mt >= 0]
    entries.sort(key=lambda item: item[1], reverse=True)
    entries = [p for p, _ in entries]
    to_remove = entries[retention_count:]
    removed: list[str] = []
    errors: dict[str, str] = {}
    for stale in to_remove:
        try:
            shutil.rmtree(stale)
            removed.append(stale.name)
        except FileNotFoundError:
            removed.append(stale.name)  # already gone — concurrent prune is fine
        except OSError as exc:
            errors[stale.name] = str(exc)
    return {
        "retention": retention_count,
        "kept": min(len(entries), retention_count),
        "removed": removed,
        "errors": errors,
    }


def archive_session_state(session: str) -> str:
    current_state_root = state_root()
    state_dir = current_state_root / session
    if not state_dir.exists():
        return ""
    archive_root = current_state_root / ".forgotten"
    archive_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = archive_root / f"{session}-{stamp}"
    if dest.exists():
        dest = archive_root / f"{session}-{stamp}-{os.getpid()}"
    state_dir.rename(dest)
    # Best-effort prune of older archives. Failures are logged via the
    # returned dict but never block the stop/cleanup path.
    try:
        prune_forgotten_archives()
    except Exception as exc:  # noqa: BLE001 — defensive: prune must not fail stop
        sys.stderr.write(f"agent-bridge: forgotten-archive prune failed (non-fatal): {exc}\n")
    return str(dest)


def cleanup_stopped_session(session: str, paths: dict[str, Path], cleanup: bool) -> str:
    paths["pid"].unlink(missing_ok=True)
    paths["meta"].unlink(missing_ok=True)
    paths["stop"].unlink(missing_ok=True)
    paths["socket"].unlink(missing_ok=True)
    if cleanup:
        cleanup_registry(session)
    # Remove a pre-structured-state file if an older install left one behind.
    (state_root() / f"session-{session}.json").unlink(missing_ok=True)
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
    paths["socket"].unlink(missing_ok=True)
    paths["stop"].unlink(missing_ok=True)

    cmd = daemon_command(args)
    if args.dry_run:
        return {
            "session": args.session,
            "dry_run": True,
            "command": cmd,
            "pid_file": str(paths["pid"]),
            "log_file": str(paths["log"]),
            "command_socket": str(paths["socket"]),
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
        "command_socket": str(paths["socket"]),
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
    try:
        ensure_runtime_writable()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    run_root().mkdir(parents=True, exist_ok=True)
    log_root().mkdir(parents=True, exist_ok=True)
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
    write_json(state_root() / session / "session.json", state)


def ensure_args_from_state(session: str, state: dict, args: argparse.Namespace) -> argparse.Namespace:
    state_dir = Path(state.get("state_dir") or state_root() / session)
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
    try:
        ensure_runtime_writable()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    run_root().mkdir(parents=True, exist_ok=True)
    log_root().mkdir(parents=True, exist_ok=True)
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
    try:
        ensure_runtime_writable()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
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


def _read_queue_status_counts(queue_file: Path) -> dict[str, int]:
    """Read queue.json (best-effort) and return per-status counts."""
    counts: dict[str, int] = {}
    try:
        items = json.loads(queue_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return counts
    if not isinstance(items, list):
        return counts
    for item in items:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _build_restart_warnings(counts: dict[str, int]) -> list[str]:
    delivered_count = counts.get("delivered", 0)
    inflight_count = counts.get("inflight", 0)
    out: list[str] = []
    if delivered_count:
        out.append(
            f"{delivered_count} delivered message(s) at restart; their reply routing context "
            "is lost. Affected peers may need agent_interrupt_peer to recover."
        )
    if inflight_count:
        out.append(
            f"{inflight_count} inflight message(s) at restart; the new daemon will requeue "
            "them after submit_timeout."
        )
    out.append(
        "in-memory state (held_interrupt, watchdogs, alarms, current_prompt) is reset; "
        "see I-03 in docs/known-issues.md."
    )
    return out


def restart_one(session: str, args: argparse.Namespace) -> dict:
    """Stop the daemon and start a fresh one for the same session, preserving
    queue.json / events.raw.jsonl / session.json (no archive).

    Use after a daemon code update or to recover from an unhealthy daemon
    without losing pending messages. In-memory state (held_interrupt,
    watchdogs, current_prompt_by_agent, last_enter_ts) is lost — see I-03
    in known-issues.md.

    Refuses by default if the queue contains `delivered` items, because
    those depend on `current_prompt_by_agent` to route the matching
    response_finished. The new daemon's startup sweep removes those
    orphan items so the queue does not stay wedged, but the originating
    senders lose their auto-routed reply. Pass `--force` to proceed
    regardless; the result will include warning fields.

    The delivered guard is best-effort: counts are read inside the
    daemon-ctl lock, but the live daemon's queue mutations do not take
    that same lock, so an inflight item may flip to delivered between
    the count read and the stop. The orphan sweep on startup keeps the
    queue from wedging in that race; routing for the racing item is
    still lost. See I-03 in docs/known-issues.md.
    """
    try:
        ensure_runtime_writable()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    run_root().mkdir(parents=True, exist_ok=True)
    log_root().mkdir(parents=True, exist_ok=True)
    paths = session_paths(session)
    if not session_state_exists(session):
        raise SystemExit(
            f"cannot restart bridge room {session}: session state is missing. "
            "Use `bridge_daemon_ctl start ...` or `bridge_run` to (re)create it."
        )

    state = load_session(session)
    state_dir = Path(state.get("state_dir") or state_root() / session)
    queue_file = Path(state.get("queue_file") or state_dir / "pending.json")

    new_args = ensure_args_from_state(session, state, args)
    new_args.replace = True
    new_args.dry_run = bool(getattr(args, "dry_run", False))
    new_args.json = bool(getattr(args, "json", False))

    # Dry-run path MUST NOT mutate live state. start_under_lock unconditionally
    # stops any existing daemon when replace=True (regardless of dry_run);
    # to keep --dry-run a true preview we synthesize the would-be result here
    # without entering start_under_lock at all. Match the schema that
    # start_under_lock returns for dry_run so callers see a consistent dict.
    if new_args.dry_run:
        counts_dry = _read_queue_status_counts(queue_file)
        cmd = daemon_command(new_args)
        return {
            "session": session,
            "dry_run": True,
            "preview_note": "counts are a snapshot read without taking the daemon-ctl lock; the live queue may have shifted since this read",
            "command": cmd,
            "pid_file": str(paths["pid"]),
            "log_file": str(paths["log"]),
            "command_socket": str(paths["socket"]),
            "restart": True,
            "queue_counts_before_restart": {
                "pending": counts_dry.get("pending", 0),
                "inflight": counts_dry.get("inflight", 0),
                "delivered": counts_dry.get("delivered", 0),
            },
            "warnings": _build_restart_warnings(counts_dry),
        }

    # Real restart: hold the daemon-ctl lock for both the delivered guard
    # check and the start. Re-reading counts inside the lock reduces the
    # stale-read window vs. doing it before the lock, but it does NOT
    # fully close the race: the live daemon's queue mutations
    # (mark_message_delivered_by_id etc.) do not take this same lock,
    # so an inflight item can still flip to delivered between this
    # count read and the actual stop. The startup orphan-sweep on the
    # new daemon keeps the queue from wedging if that race lands; the
    # affected request just loses its auto-route. See I-03.
    with paths["lock"].open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        counts = _read_queue_status_counts(queue_file)
        delivered_count = counts.get("delivered", 0)
        if delivered_count and not getattr(args, "force", False):
            raise SystemExit(
                f"refusing restart for {session}: queue has {delivered_count} delivered "
                "message(s) whose routing context will be lost across restart. "
                "Inspect via `agent_interrupt_peer --status` and either let the peers "
                "finish, run `agent_interrupt_peer <alias>`, or pass --force."
            )
        result = start_under_lock(new_args, paths)

    result = dict(result)
    result["restart"] = True
    result["queue_counts_before_restart"] = {
        "pending": counts.get("pending", 0),
        "inflight": counts.get("inflight", 0),
        "delivered": delivered_count,
    }
    result["warnings"] = _build_restart_warnings(counts)
    # P2 fix: keep session.json's daemon info in sync (mirrors ensure_one).
    try:
        update_session_daemon_info(session, state, result)
    except Exception as exc:  # noqa: BLE001 — non-fatal: restart already succeeded
        sys.stderr.write(f"agent-bridge: restart succeeded but session daemon info update failed: {exc}\n")
    return result


def forget_one(session: str, cleanup: bool) -> dict:
    try:
        ensure_runtime_writable()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
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
    current_run_root = run_root()
    current_state_root = state_root()
    if current_run_root.exists():
        names.update(path.stem for path in current_run_root.glob("*.pid"))
        names.update(path.stem for path in current_run_root.glob("*.json"))
    if current_state_root.exists():
        for path in current_state_root.glob("*/session.json"):
            names.add(path.parent.name)
    return sorted(names)


def status_rows(session: str | None = None, all_sessions: bool = False) -> list[dict]:
    sessions = [session] if session else known_sessions()
    rows = []
    for name in sessions:
        paths = session_paths(name)
        meta = read_json(paths["meta"], {"session": name})
        state = read_json(state_root() / name / "session.json", {})
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
        participants = active_participants(state)
        active_aliases = set(participants)
        row = {
            "session": name,
            "status": run_state,
            "pid": pid,
            "mode": state.get("mode") or meta.get("mode") or "",
            "participants": ",".join(sorted(participants)),
            "targets": ",".join(
                f"{alias}={target}"
                for alias, target in sorted((state.get("targets") or {}).items())
                if alias in active_aliases and target
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

    p_restart = sub.add_parser(
        "restart",
        help="stop the daemon and start a fresh one for the same session, preserving queue/events state",
    )
    p_restart.add_argument("-s", "--session", required=True)
    p_restart.add_argument(
        "--force",
        action="store_true",
        help="proceed even if delivered messages exist (their reply routing context will be lost across restart)",
    )
    p_restart.add_argument("--health-delay", type=float, default=0.35)
    p_restart.add_argument("--stop-timeout", type=float, default=5.0)
    p_restart.add_argument("--max-hops", type=int)
    p_restart.add_argument("--submit-delay", type=float)
    p_restart.add_argument("--submit-timeout", type=float)
    p_restart.add_argument("--dry-run", action="store_true")
    p_restart.add_argument("--json", action="store_true")

    p_prune = sub.add_parser(
        "prune-archives",
        help="remove old archives in <state_root>/.forgotten/ (defaults to keeping the most recent 10)",
    )
    p_prune.add_argument(
        "--keep",
        type=int,
        default=None,
        help=(
            "number of newest archives to keep (default: env "
            "AGENT_BRIDGE_FORGOTTEN_RETENTION_COUNT or 10; 0 disables pruning)"
        ),
    )
    p_prune.add_argument("--json", action="store_true")

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

    if args.command == "restart":
        result = restart_one(args.session, args)
        if args.json:
            print(json.dumps(result, ensure_ascii=True, indent=2))
        else:
            if result.get("dry_run"):
                print(" ".join(result["command"]))
            else:
                print(f"restarted bridge daemon {result['session']} pid={result['pid']} log={result['log_file']}")
            counts = result.get("queue_counts_before_restart") or {}
            if counts:
                print(
                    f"  queue at restart: pending={counts.get('pending', 0)} "
                    f"inflight={counts.get('inflight', 0)} delivered={counts.get('delivered', 0)}"
                )
            for warning in result.get("warnings") or []:
                print(f"  warning: {warning}")
        return 0

    if args.command == "prune-archives":
        result = prune_forgotten_archives(args.keep)
        if args.json:
            print(json.dumps(result, ensure_ascii=True, indent=2))
        else:
            removed = result.get("removed") or []
            kept = result.get("kept", 0)
            retention = result.get("retention", 0)
            if retention <= 0:
                print(f"prune-archives: pruning disabled (retention={retention})")
            elif not removed:
                print(f"prune-archives: kept {kept}/{retention}, nothing to remove")
            else:
                print(f"prune-archives: removed {len(removed)} archive(s); kept newest {kept}")
                for name in removed:
                    print(f"  removed: {name}")
            errors = result.get("errors") or {}
            for name, err in errors.items():
                print(f"  error: {name}: {err}", file=sys.stderr)
        return 0

    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.exit(130)
