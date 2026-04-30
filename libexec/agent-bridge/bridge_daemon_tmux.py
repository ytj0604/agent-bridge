#!/usr/bin/env python3
import hashlib
import os
import subprocess
import uuid


PANE_MODE_PROBE_TIMEOUT_SECONDS = 0.3
TMUX_SEND_TIMEOUT_SECONDS = 5.0


def _tmux_buffer_component(value: object, fallback: str) -> str:
    raw = str(value or fallback)
    safe = "".join(
        ch if ("a" <= ch <= "z" or "A" <= ch <= "Z" or "0" <= ch <= "9" or ch in "._-") else "-"
        for ch in raw
    ).strip("._-")
    if not safe:
        safe = fallback
    if len(safe) > 48:
        digest = hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:10]
        safe = f"{safe[:37]}-{digest}"
    return safe


def tmux_prompt_buffer_name(
    bridge_session: str,
    target_alias: str,
    message_id: str,
    nonce: str,
) -> str:
    components = [
        _tmux_buffer_component(bridge_session, "session"),
        _tmux_buffer_component(target_alias, "target"),
        _tmux_buffer_component(message_id, "message"),
        _tmux_buffer_component(nonce, "nonce"),
        str(os.getpid()),
        uuid.uuid4().hex[:12],
    ]
    return "bridge-" + "-".join(components)


def run_tmux_send_literal(
    target: str,
    prompt: str,
    *,
    bridge_session: str = "",
    target_alias: str = "",
    message_id: str = "",
    nonce: str = "",
) -> None:
    buffer_name = tmux_prompt_buffer_name(bridge_session, target_alias or target, message_id, nonce)
    try:
        subprocess.run(
            ["tmux", "load-buffer", "-b", buffer_name, "-"],
            input=prompt.encode("utf-8"),
            check=True,
            timeout=TMUX_SEND_TIMEOUT_SECONDS,
        )
        subprocess.run(
            ["tmux", "paste-buffer", "-p", "-r", "-d", "-b", buffer_name, "-t", target],
            check=True,
            timeout=TMUX_SEND_TIMEOUT_SECONDS,
        )
    finally:
        try:
            subprocess.run(
                ["tmux", "delete-buffer", "-b", buffer_name],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=TMUX_SEND_TIMEOUT_SECONDS,
            )
        except Exception:
            pass


def run_tmux_enter(target: str) -> None:
    subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], check=True, timeout=TMUX_SEND_TIMEOUT_SECONDS)


def run_tmux_send_literal_touch_result(
    target: str,
    prompt: str,
    *,
    bridge_session: str = "",
    target_alias: str = "",
    message_id: str = "",
    nonce: str = "",
) -> dict:
    """Send literal text and Enter, reporting whether pane input may be mutated."""
    buffer_name = tmux_prompt_buffer_name(bridge_session, target_alias or target, message_id, nonce)
    pane_touched = False
    try:
        try:
            subprocess.run(
                ["tmux", "load-buffer", "-b", buffer_name, "-"],
                input=prompt.encode("utf-8"),
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=TMUX_SEND_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            return {"ok": False, "pane_touched": False, "error": f"load-buffer: {exc}"}
        try:
            subprocess.run(
                ["tmux", "paste-buffer", "-p", "-r", "-d", "-b", buffer_name, "-t", target],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=TMUX_SEND_TIMEOUT_SECONDS,
            )
            pane_touched = True
        except Exception as exc:
            return {"ok": False, "pane_touched": True, "error": f"paste-buffer: {exc}"}
        try:
            subprocess.run(
                ["tmux", "send-keys", "-t", target, "Enter"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=TMUX_SEND_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            return {"ok": False, "pane_touched": True, "error": f"enter: {exc}"}
        return {"ok": True, "pane_touched": pane_touched, "error": ""}
    finally:
        try:
            subprocess.run(
                ["tmux", "delete-buffer", "-b", buffer_name],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=TMUX_SEND_TIMEOUT_SECONDS,
            )
        except Exception:
            pass


def probe_tmux_pane_mode(target: str) -> dict:
    try:
        proc = subprocess.run(
            ["tmux", "display-message", "-p", "-t", target, "#{pane_in_mode}\t#{pane_mode}"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=PANE_MODE_PROBE_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        return {"in_mode": False, "mode": "", "error": str(exc)}
    flag, _, mode = proc.stdout.rstrip("\n").partition("\t")
    return {"in_mode": flag.strip() == "1", "mode": mode.strip(), "error": ""}


def cancel_tmux_pane_mode(target: str) -> tuple[bool, str]:
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", target, "-X", "cancel"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=PANE_MODE_PROBE_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        return False, str(exc)
    return True, ""
