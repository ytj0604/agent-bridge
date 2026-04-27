#!/usr/bin/env python3
from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import re
import subprocess


VALID_AGENTS = {"claude", "codex"}
UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
CODEX_ROLLOUT_PATH_RE = re.compile(
    rf"(^|/)\.codex/sessions/\d{{4}}/\d{{2}}/\d{{2}}/"
    rf"rollout-\d{{4}}-\d{{2}}-\d{{2}}T\d{{2}}-\d{{2}}-\d{{2}}-(?P<session_id>{UUID_RE})\.jsonl$"
)


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)


def tmux_display_pane(pane: str) -> dict:
    if not pane:
        return {}
    fmt = "#{pane_id}\t#{pane_pid}\t#{session_name}:#{window_index}.#{pane_index}\t#{pane_current_command}\t#{pane_current_path}"
    try:
        proc = run(["tmux", "display-message", "-p", "-t", pane, fmt], check=False)
    except FileNotFoundError:
        return {"error": "tmux_unavailable"}
    except OSError as exc:
        return {"error": f"tmux_error:{exc}"}
    if proc.returncode != 0:
        raw = (proc.stderr or proc.stdout).strip()
        lowered = raw.lower()
        positive_missing = (
            "can't find pane" in lowered
            or "can't find window" in lowered
            or "can't find session" in lowered
            or "no such pane" in lowered
            or "invalid pane" in lowered
        )
        if positive_missing:
            return {"error": "pane_unavailable", "detail": raw}
        return {"error": "tmux_access_failed", "detail": raw or "tmux display-message failed"}
    parts = proc.stdout.rstrip("\n").split("\t")
    if len(parts) != 5:
        return {"error": "pane_metadata_unparseable"}
    pane_id, pane_pid, target, command, cwd = parts
    if not pane_id:
        return {"error": "pane_unavailable", "detail": "tmux returned empty pane metadata"}
    return {
        "pane_id": pane_id,
        "pane_pid": pane_pid,
        "target": target,
        "command": command,
        "cwd": cwd,
    }


def transcript_session_id_for_pid(agent_type: str, pid: int | str) -> dict:
    if str(agent_type or "") != "codex":
        return {}
    try:
        pid_int = int(str(pid))
    except ValueError:
        return {}
    fd_dir = Path(f"/proc/{pid_int}/fd")
    try:
        entries = list(fd_dir.iterdir())
    except OSError:
        return {}
    for entry in entries:
        try:
            path = str(entry.readlink())
        except OSError:
            continue
        session_id = codex_session_id_from_rollout_path(path)
        if not session_id:
            continue
        return {"session_id": session_id, "path": path, "pid": pid_int}
    return {}


def codex_session_id_from_rollout_path(path: str) -> str:
    match = CODEX_ROLLOUT_PATH_RE.search(str(path or ""))
    return match.group("session_id") if match else ""


def transcript_owners_for_session(agent_type: str, session_id: str) -> list[dict]:
    expected = str(session_id or "")
    if not expected:
        return []
    owners: list[dict] = []
    try:
        proc_dirs = list(Path("/proc").iterdir())
    except OSError:
        return []
    for proc_dir in proc_dirs:
        if not proc_dir.name.isdigit():
            continue
        proof = transcript_session_id_for_pid(agent_type, proc_dir.name)
        if str(proof.get("session_id") or "") == expected:
            owners.append(proof)
    return owners


def load_process_table() -> dict:
    try:
        proc = run(["ps", "-axo", "pid=,ppid=,args="], check=False)
    except OSError as exc:
        return {"processes": {}, "children": {}, "error": f"ps_unavailable:{exc}"}
    if proc.returncode != 0:
        return {"processes": {}, "children": {}, "error": proc.stderr.strip() or proc.stdout.strip() or "ps_failed"}
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


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def process_start_time(pid: int) -> str:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    end = raw.rfind(")")
    if end < 0:
        return ""
    rest = raw[end + 1 :].strip().split()
    # Field 22 (starttime) is index 19 after stripping pid + comm.
    if len(rest) <= 19:
        return ""
    return rest[19]


def process_tree(root_pid: int, process_table: dict) -> list[tuple[int, int]]:
    processes = process_table.get("processes") or {}
    children = process_table.get("children") or {}
    if root_pid not in processes:
        return []
    out: list[tuple[int, int]] = []
    stack = [(root_pid, 0)]
    seen: set[int] = set()
    while stack:
        pid, depth = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        out.append((pid, depth))
        for child in children.get(pid, []):
            stack.append((child, depth + 1))
    return out


def args_hint(args: str) -> str:
    text = " ".join(str(args or "").split())
    if len(text) > 160:
        return text[:157] + "..."
    return text


def expected_agent_processes(pane_meta: dict, agent: str, process_table: dict) -> list[dict]:
    processes = process_table.get("processes") or {}
    try:
        root_pid = int(str(pane_meta.get("pane_pid") or ""))
    except ValueError:
        root_pid = 0
    found: list[dict] = []
    for pid, depth in process_tree(root_pid, process_table):
        args = str((processes.get(pid) or {}).get("args") or "")
        base = agent_score_from_args(args, agent)
        if not base:
            continue
        start_time = process_start_time(pid)
        found.append(
            {
                "pid": pid,
                "start_time": start_time,
                "score": max(1, base - depth),
                "args_hint": args_hint(args),
            }
        )
    found.sort(key=lambda item: (-int(item.get("score") or 0), int(item.get("pid") or 0)))
    return found


def _base_probe(status: str, reason: str, pane: str, agent: str, pane_meta: dict | None = None) -> dict:
    pane_meta = pane_meta or {}
    return {
        "status": status,
        "reason": reason,
        "pane": str(pane_meta.get("pane_id") or pane or ""),
        "target": str(pane_meta.get("target") or ""),
        "agent": str(agent or ""),
        "pane_pid": pane_meta.get("pane_pid") or "",
        "boot_id": boot_id(),
        "processes": [],
    }


def probe_agent_process(pane: str, agent: str, stored_identity: dict | None = None) -> dict:
    agent = str(agent or "")
    if agent not in VALID_AGENTS:
        return _base_probe("unknown", "missing_agent", pane, agent)
    pane_meta = tmux_display_pane(pane)
    pane_error = str(pane_meta.get("error") or "")
    if pane_error:
        if pane_error == "pane_unavailable":
            return _base_probe("mismatch", "pane_unavailable", pane, agent, pane_meta)
        if pane_error in {"tmux_unavailable", "tmux_access_failed"} or pane_error.startswith("tmux_error:"):
            result = _base_probe("unknown", pane_error, pane, agent, pane_meta)
            if pane_meta.get("detail"):
                result["detail"] = str(pane_meta.get("detail") or "")
            return result
        else:
            return _base_probe("unknown", pane_error, pane, agent, pane_meta)

    process_table = load_process_table()
    if process_table.get("error"):
        result = _base_probe("unknown", "ps_unavailable", pane, agent, pane_meta)
        result["detail"] = str(process_table.get("error") or "")
        return result

    candidates = expected_agent_processes(pane_meta, agent, process_table)
    if not candidates:
        return _base_probe("mismatch", "agent_process_not_found", pane, agent, pane_meta)
    current_processes = [proc for proc in candidates if proc.get("start_time")]
    if not current_processes:
        result = _base_probe("unknown", "proc_unavailable", pane, agent, pane_meta)
        result["processes"] = candidates
        return result

    current = _base_probe("verified", "ok", pane, agent, pane_meta)
    current["processes"] = current_processes

    if not stored_identity:
        return current
    if str(stored_identity.get("status") or "") != "verified":
        current["status"] = "unknown"
        current["reason"] = "missing_process_fingerprint"
        return current
    stored_processes = stored_identity.get("processes") or []
    if not isinstance(stored_processes, list) or not stored_processes:
        current["status"] = "unknown"
        current["reason"] = "missing_process_fingerprint"
        return current

    stored_boot = str(stored_identity.get("boot_id") or "")
    current_boot = str(current.get("boot_id") or "")
    if stored_boot and current_boot and stored_boot != current_boot:
        current["status"] = "mismatch"
        current["reason"] = "boot_id_mismatch"
        return current

    current_keys = {
        (str(proc.get("pid") or ""), str(proc.get("start_time") or ""))
        for proc in current_processes
        if proc.get("pid") and proc.get("start_time")
    }
    stored_keys = {
        (str(proc.get("pid") or ""), str(proc.get("start_time") or ""))
        for proc in stored_processes
        if isinstance(proc, dict) and proc.get("pid") and proc.get("start_time")
    }
    if not stored_keys:
        current["status"] = "unknown"
        current["reason"] = "missing_process_fingerprint"
        return current
    if current_keys & stored_keys:
        return current
    current["status"] = "mismatch"
    current["reason"] = "process_fingerprint_mismatch"
    return current
