#!/usr/bin/env python3
import json
import math
import os
import socket
import struct
import threading


def start_command_server(d) -> None:
    if not d.command_socket:
        return
    if d.dry_run:
        return
    path = d.command_socket
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(path))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        server.listen(16)
        server.settimeout(0.25)
    except OSError as exc:
        try:
            server.close()
        finally:
            d.command_server_socket = None
        d.log("command_socket_unavailable", command_socket=str(path), error=str(exc))
        return
    d.command_server_socket = server
    d.command_server_thread = threading.Thread(target=d.command_server_loop, name="bridge-command-socket", daemon=True)
    d.command_server_thread.start()

def stop_command_server(d) -> None:
    server = d.command_server_socket
    if server is not None:
        try:
            server.close()
        except OSError:
            pass
        d.command_server_socket = None
    if d.command_socket:
        try:
            d.command_socket.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

def command_server_loop(d) -> None:
    server = d.command_server_socket
    if server is None:
        return
    while not d.stop_requested():
        try:
            conn, _ = server.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        worker = threading.Thread(
            target=d.handle_command_worker,
            args=(conn,),
            name="bridge-command-worker",
            daemon=True,
        )
        worker.start()

def handle_command_worker(d, conn: socket.socket) -> None:
    with conn:
        response = None
        try:
            response = d.handle_command_connection(conn)
        except Exception as exc:
            if exc.__class__.__name__ != "CommandLockWaitExceeded":
                raise
            response = d.lock_wait_exceeded_response(getattr(exc, "command_class", ""))
        finally:
            d.command_context.info = {}
        if response is None:
            return
        try:
            conn.sendall((json.dumps(response, ensure_ascii=True) + "\n").encode("utf-8"))
        except OSError:
            pass

def handle_command_connection(d, conn: socket.socket) -> dict:
    peer_uid = d.peer_uid(conn)
    if peer_uid is not None and peer_uid != os.getuid():
        return {"ok": False, "error": f"peer uid {peer_uid} is not allowed"}
    try:
        raw = b""
        while b"\n" not in raw and len(raw) < 2_000_000:
            chunk = conn.recv(65536)
            if not chunk:
                break
            raw += chunk
        request = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        return {"ok": False, "error": f"invalid request: {exc}"}

    if not isinstance(request, dict):
        return {"ok": False, "error": "unsupported command"}
    op = request.get("op")
    d.begin_command_context(str(op or ""), request)
    if op == "enqueue":
        return d.handle_enqueue_command(
            request.get("messages"),
            force_response_send=bool(request.get("force_response_send")),
        )
    if op == "alarm":
        d.reload_participants()
        sender = str(request.get("from") or "")
        if sender not in d.participants:
            return {"ok": False, "error": f"sender {sender!r} is not an active participant"}
        delay = request.get("delay_seconds")
        if delay is None:
            return {"ok": False, "error": "delay_seconds required"}
        try:
            delay_value = float(delay)
        except (TypeError, ValueError):
            return {"ok": False, "error": "delay_seconds must be a number"}
        if not math.isfinite(delay_value) or delay_value < 0:
            return {"ok": False, "error": "delay_seconds must be a finite non-negative number"}
        body = request.get("body")
        wake_id = request.get("wake_id")
        return d.register_alarm_result(
            sender,
            delay_value,
            body if isinstance(body, str) else None,
            wake_id=str(wake_id) if wake_id is not None else None,
        )
    if op == "interrupt":
        d.reload_participants()
        sender = str(request.get("from") or "")
        target = str(request.get("target") or "")
        if sender and sender != "bridge" and sender not in d.participants:
            return {"ok": False, "error": f"sender {sender!r} is not an active participant"}
        if target not in d.participants:
            return {"ok": False, "error": f"target {target!r} is not an active participant"}
        result = d.handle_interrupt(sender, target)
        if result.get("error_kind") == "lock_wait_exceeded":
            return {"ok": False, "error": "lock_wait_exceeded", "error_kind": "lock_wait_exceeded"}
        return {"ok": True, **result}
    if op == "clear_peer":
        d.reload_participants()
        sender = str(request.get("from") or "")
        force = bool(request.get("force"))
        if sender and sender != "bridge" and sender not in d.participants:
            return {"ok": False, "error": f"sender {sender!r} is not an active participant"}
        if request.get("targets") is not None:
            if str(request.get("target") or ""):
                return {"ok": False, "error": "use either target or targets, not both", "error_kind": "malformed_targets"}
            validation = d.validate_clear_targets_payload(sender or "bridge", request.get("targets"), force=force)
            if not validation.get("ok"):
                return validation
            targets = list(validation.get("targets") or [])
            if len(targets) == 1:
                return d.handle_clear_peer(sender or "bridge", targets[0], force=force)
            return d.handle_clear_peers(sender or "bridge", targets, force=force)
        target = str(request.get("target") or "")
        if target not in d.participants:
            return {"ok": False, "error": f"target {target!r} is not an active participant"}
        return d.handle_clear_peer(sender or "bridge", target, force=force)
    if op == "clear_hold":
        d.reload_participants()
        sender = str(request.get("from") or "")
        target = str(request.get("target") or "")
        if sender and sender != "bridge" and sender not in d.participants:
            return {"ok": False, "error": f"sender {sender!r} is not an active participant"}
        if target not in d.participants:
            return {"ok": False, "error": f"target {target!r} is not an active participant"}
        info = d.release_hold(target, reason=f"manual_clear_by_{sender or 'bridge'}", by_sender=sender)
        if isinstance(info, dict) and info.get("_lock_wait_exceeded"):
            return d.lock_wait_exceeded_response("clear_hold")
        return {
            "ok": True,
            "had_hold": info is not None,
            "info": info or {},
            "warning": (
                "Forcing hold release before the peer's Stop event arrives can cause "
                "late responses to misroute, and clearing a partial interrupt gate can "
                "paste queued prompts into dirty input. Verify with --status that the peer "
                "is idle, then use agent_view_peer to confirm the input buffer is clear "
                "before clearing."
            ),
        }
    if op == "extend_watchdog":
        d.reload_participants()
        sender = str(request.get("from") or "")
        message_id = str(request.get("message_id") or "")
        seconds = request.get("seconds")
        if sender and sender != "bridge" and sender not in d.participants:
            return {"ok": False, "error": f"sender {sender!r} is not an active participant"}
        if not message_id:
            return {"ok": False, "error": "message_id required"}
        try:
            additional_sec = float(seconds)
        except (TypeError, ValueError):
            return {"ok": False, "error": "seconds must be a number"}
        if not math.isfinite(additional_sec) or additional_sec <= 0:
            return {"ok": False, "error": "seconds must be a finite positive number"}
        ok, err, deadline = d.upsert_message_watchdog(sender, message_id, additional_sec)
        if not ok:
            error = err or "extend_failed"
            response = {"ok": False, "error": error, "message_id": message_id}
            hint = d.extend_watchdog_error_hint(error)
            if hint:
                response["hint"] = hint
            return response
        return {"ok": True, "new_deadline": deadline, "message_id": message_id}
    if op == "cancel_message":
        d.reload_participants()
        sender = str(request.get("from") or "")
        message_id = str(request.get("message_id") or "")
        if sender and sender != "bridge" and sender not in d.participants:
            return {"ok": False, "error": f"sender {sender!r} is not an active participant"}
        if not message_id:
            return {"ok": False, "error": "message_id required"}
        result = d.cancel_message(sender, message_id)
        if not result.get("ok"):
            return result
        target = str(result.get("target") or "")
        if result.get("cancelled") and target:
            d.try_deliver_command_aware(target, message_id=message_id)
        return result
    if op == "wait_status":
        d.reload_participants()
        sender = str(request.get("from") or "")
        if not sender:
            return {"ok": False, "error": "sender required"}
        if sender == "bridge" or sender not in d.participants:
            return {"ok": False, "error": f"sender {sender!r} is not an active participant"}
        return d.build_wait_status(sender)
    if op == "aggregate_status":
        d.reload_participants()
        sender = str(request.get("from") or "")
        aggregate_id = str(request.get("aggregate_id") or "")
        if not sender:
            return {"ok": False, "error": "sender required"}
        if sender == "bridge" or sender not in d.participants:
            return {"ok": False, "error": f"sender {sender!r} is not an active participant"}
        if not aggregate_id:
            return {"ok": False, "error": "aggregate_id_required"}
        return d.build_aggregate_status(sender, aggregate_id)
    if op == "status":
        d.reload_participants()
        target = str(request.get("target") or "")
        peers = []
        with d.command_state_lock(command_class="status"):
            queue_snapshot = list(d.queue.read())
            aliases = [target] if target else sorted(d.participants)
            for alias in aliases:
                if alias not in d.participants:
                    peers.append({"alias": alias, "active": False})
                    continue
                participant = d.participants.get(alias) or {}
                held = d.held_interrupt.get(alias)
                partial_interrupt = d.interrupt_partial_failure_blocks.get(alias)
                cur = d.current_prompt_by_agent.get(alias) or {}
                pane = str(participant.get("pane") or d.panes.get(alias) or "")
                first_pending = next(
                    (
                        it for it in queue_snapshot
                        if it.get("to") == alias and it.get("status") == "pending"
                    ),
                    {},
                )
                delivered_count = sum(
                    1 for it in queue_snapshot
                    if it.get("to") == alias and it.get("status") == "delivered"
                )
                pending_count = sum(
                    1 for it in queue_snapshot
                    if it.get("to") == alias and it.get("status") == "pending"
                )
                inflight_count = sum(
                    1 for it in queue_snapshot
                    if it.get("to") == alias and it.get("status") in {"inflight", "submitted"}
                )
                peers.append({
                    "alias": alias,
                    "active": True,
                    "busy": bool(d.busy.get(alias)),
                    "reserved_message_id": d.reserved.get(alias),
                    "current_prompt_id": cur.get("id"),
                    "current_prompt_from": cur.get("from"),
                    "current_prompt_turn_id": cur.get("turn_id"),
                    "held": held is not None,
                    "held_info": held or {},
                    "clear_active": alias in d.clear_reservations,
                    "clear_info": {
                        key: value
                        for key, value in (d.clear_reservations.get(alias) or {}).items()
                        if key not in {"condition"}
                    },
                    "self_clear_pending": alias in d.pending_self_clears,
                    "self_clear_info": dict(d.pending_self_clears.get(alias) or {}),
                    "interrupt_partial_failure_blocked": partial_interrupt is not None,
                    "interrupt_partial_failure_info": partial_interrupt or {},
                    "_pane": pane,
                    "pane_mode_blocked_since": first_pending.get("pane_mode_blocked_since"),
                    "pane_mode_blocked_mode": first_pending.get("pane_mode_blocked_mode"),
                    "pane_mode_block_count": int(first_pending.get("pane_mode_block_count") or 0),
                    "delivered_count": delivered_count,
                    "inflight_count": inflight_count,
                    "pending_count": pending_count,
                })
        for peer in peers:
            pane = peer.pop("_pane", "")
            if not peer.get("active") or not pane:
                peer["pane_in_mode"] = False
                peer["pane_mode"] = ""
                continue
            status = d.pane_mode_status(str(pane))
            peer["pane_in_mode"] = bool(status.get("in_mode"))
            peer["pane_mode"] = str(status.get("mode") or "")
            if status.get("error"):
                peer["pane_mode_error"] = status.get("error")
        return {"ok": True, "peers": peers}
    return {"ok": False, "error": "unsupported command"}

def peer_uid(d, conn: socket.socket) -> int | None:
    so_peercred = getattr(socket, "SO_PEERCRED", None)
    if so_peercred is None:
        return None
    try:
        creds = conn.getsockopt(socket.SOL_SOCKET, so_peercred, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", creds)
        return int(uid)
    except OSError:
        return None
