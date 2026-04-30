from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from .harness import (
    assert_true,
    identity_live_record,
    isolated_identity_env,
    make_daemon,
    patched_environ,
    read_events,
    read_raw_events,
    set_identity_target,
    test_message,
    verified_identity,
    write_identity_fixture,
    write_live_identity_records,
)

import bridge_daemon  # noqa: E402
import bridge_identity  # noqa: E402
import bridge_leave  # noqa: E402
import bridge_pane_probe  # noqa: E402
from bridge_util import read_json, utc_now, write_json_atomic  # noqa: E402


def scenario_endpoint_rejects_stale_pane_lock_without_live(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = write_identity_fixture(state_root_path)
        detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        assert_true(not detail.get("ok"), f"{label}: stale pane lock must not authorize endpoint")
        assert_true(detail.get("reason") == "live_record_missing", f"{label}: expected live_record_missing, got {detail}")
        locks = read_json(Path(os.environ["AGENT_BRIDGE_PANE_LOCKS"]), {"panes": {}})
        assert_true("%20" in (locks.get("panes") or {}), f"{label}: unknown/stale lock should remain diagnostic-only")
    print(f"  PASS  {label}")


def scenario_endpoint_rejects_same_pane_new_live_identity(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = write_identity_fixture(state_root_path, alias="codexA", session_id="sess-a", pane="%21")
        write_json_atomic(
            Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]),
            {
                "version": 1,
                "panes": {
                    "%21": {
                        "agent": "codex",
                        "session_id": "sess-b",
                        "pane": "%21",
                        "target": "tmux:1.0",
                        "bridge_session": "test-session",
                        "alias": "codexB",
                        "last_seen_at": utc_now(),
                        "process_identity": verified_identity("codex", "%21", pid=2000, start_time="99"),
                    }
                },
                "sessions": {},
            },
        )
        detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codexA", participant)
        assert_true(not detail.get("ok") and detail.get("reason") == "live_record_mismatch", f"{label}: expected live mismatch, got {detail}")
        locks = read_json(Path(os.environ["AGENT_BRIDGE_PANE_LOCKS"]), {"panes": {}})
        assert_true("%21" not in (locks.get("panes") or {}), f"{label}: positive mismatch should clear pane lock")
        state = read_json(state_root_path / "test-session" / "session.json", {})
        record = ((state.get("participants") or {}).get("codexA") or {})
        assert_true(record.get("endpoint_status") == "endpoint_lost", f"{label}: participant remains visible but endpoint_lost")
    print(f"  PASS  {label}")


def scenario_endpoint_probe_unknown_does_not_mutate(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = write_identity_fixture(state_root_path, pane="%22")
        live = {
            "agent": "codex",
            "session_id": "sess-a",
            "pane": "%22",
            "target": "tmux:1.0",
            "bridge_session": "test-session",
            "alias": "codex",
            "last_seen_at": utc_now(),
            "process_identity": verified_identity("codex", "%22"),
        }
        write_json_atomic(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"version": 1, "panes": {"%22": live}, "sessions": {"codex:sess-a": live}})
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: {"status": "unknown", "reason": "ps_unavailable", "processes": []}  # type: ignore[assignment]
        try:
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        assert_true(not detail.get("ok") and detail.get("reason") == "probe_unknown", f"{label}: expected probe_unknown, got {detail}")
        locks = read_json(Path(os.environ["AGENT_BRIDGE_PANE_LOCKS"]), {"panes": {}})
        assert_true("%22" in (locks.get("panes") or {}), f"{label}: probe_unknown must not clear pane lock")
        state = read_json(state_root_path / "test-session" / "session.json", {})
        record = ((state.get("participants") or {}).get("codex") or {})
        assert_true(record.get("endpoint_status") != "endpoint_lost", f"{label}: probe_unknown must not mark endpoint lost")
    print(f"  PASS  {label}")


def scenario_endpoint_accepts_matching_process_fingerprint(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = write_identity_fixture(state_root_path, pane="%23")
        identity = verified_identity("codex", "%23", pid=3000, start_time="101")
        live = {
            "agent": "codex",
            "session_id": "sess-a",
            "pane": "%23",
            "target": "tmux:1.0",
            "bridge_session": "test-session",
            "alias": "codex",
            "last_seen_at": utc_now(),
            "process_identity": identity,
        }
        write_json_atomic(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"version": 1, "panes": {"%23": live}, "sessions": {"codex:sess-a": live}})
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: dict(identity)  # type: ignore[assignment]
        try:
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        assert_true(detail.get("ok") and detail.get("pane") == "%23", f"{label}: matching fingerprint should resolve, got {detail}")
    print(f"  PASS  {label}")


def scenario_backfill_refuses_to_mint_without_live_record(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = write_identity_fixture(state_root_path, pane="%24")
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: verified_identity(agent, pane, pid=4000, start_time="201")  # type: ignore[assignment]
        try:
            summary = bridge_identity.backfill_session_process_identities("test-session", {"session": "test-session", "participants": {"codex": participant}})
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        assert_true(summary.get("codex", {}).get("reason") == "live_record_missing", f"{label}: backfill must refuse to mint without live hook proof: {summary}")
        live = read_json(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"panes": {}, "sessions": {}})
        assert_true((live.get("panes") or {}) == {} and (live.get("sessions") or {}) == {}, f"{label}: live records must remain empty: {live}")
        detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        assert_true(not detail.get("ok") and detail.get("reason") == "live_record_missing", f"{label}: resolver must still fail closed: {detail}")
    print(f"  PASS  {label}")


def scenario_backfill_refuses_other_live_identity(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = write_identity_fixture(state_root_path, alias="codexA", session_id="sess-a", pane="%25")
        other = {
            "agent": "codex",
            "session_id": "sess-b",
            "pane": "%25",
            "target": "tmux:1.0",
            "bridge_session": "test-session",
            "alias": "codexB",
            "last_seen_at": utc_now(),
            "process_identity": verified_identity("codex", "%25", pid=5000, start_time="301"),
        }
        write_json_atomic(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"version": 1, "panes": {"%25": other}, "sessions": {"codex:sess-b": other}})
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: verified_identity(agent, pane, pid=5001, start_time="302")  # type: ignore[assignment]
        try:
            summary = bridge_identity.backfill_session_process_identities("test-session", {"session": "test-session", "participants": {"codexA": participant}})
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        assert_true(summary.get("codexA", {}).get("reason") == "live_record_mismatch", f"{label}: backfill must refuse other live identity: {summary}")
        live = read_json(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"panes": {}})
        assert_true(((live.get("panes") or {}).get("%25") or {}).get("session_id") == "sess-b", f"{label}: other live record must not be overwritten: {live}")
    print(f"  PASS  {label}")


def scenario_backfill_rejects_changed_process_fingerprint(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = write_identity_fixture(state_root_path, pane="%26")
        original_identity = verified_identity("codex", "%26", pid=6000, start_time="401")
        live = {
            "agent": "codex",
            "session_id": "sess-a",
            "pane": "%26",
            "target": "tmux:1.0",
            "bridge_session": "test-session",
            "alias": "codex",
            "last_seen_at": utc_now(),
            "process_identity": original_identity,
        }
        write_json_atomic(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"version": 1, "panes": {"%26": live}, "sessions": {"codex:sess-a": live}})
        mismatch = verified_identity("codex", "%26", pid=6001, start_time="402")
        mismatch["status"] = "mismatch"
        mismatch["reason"] = "process_fingerprint_mismatch"
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: dict(mismatch)  # type: ignore[assignment]
        try:
            summary = bridge_identity.backfill_session_process_identities("test-session", {"session": "test-session", "participants": {"codex": participant}})
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        assert_true(summary.get("codex", {}).get("status") == "mismatch", f"{label}: changed process must not be refreshed: {summary}")
        live_after = read_json(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"panes": {}})
        proc = (((live_after.get("panes") or {}).get("%26") or {}).get("process_identity") or {}).get("processes", [{}])[0]
        assert_true(proc.get("pid") == 6000, f"{label}: original fingerprint must remain: {live_after}")
    print(f"  PASS  {label}")


def scenario_backfill_allows_fresh_hook_proof_create(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = write_identity_fixture(state_root_path, pane="%27")
        identity = verified_identity("codex", "%27", pid=7000, start_time="501")
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: dict(identity)  # type: ignore[assignment]
        try:
            summary = bridge_identity.backfill_session_process_identities(
                "test-session",
                {"session": "test-session", "participants": {"codex": participant}},
                allow_create_from_hook=True,
            )
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        assert_true(summary.get("codex", {}).get("status") == "verified", f"{label}: fresh hook proof may create fingerprint: {summary}")
        assert_true(detail.get("ok") and detail.get("pane") == "%27", f"{label}: created live fingerprint should resolve: {detail}")
    print(f"  PASS  {label}")


def scenario_backfill_fresh_probe_repairs_unscoped_live_mismatch(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = write_identity_fixture(state_root_path, alias="codex1", session_id="sess-a", pane="%27a")
        polluted = identity_live_record(
            pane="%27a",
            session_id="sess-b",
            alias="",
            pid=7001,
            start_time="502",
            process_identity=verified_identity("codex", "%27a", pid=7001, start_time="502"),
        )
        polluted["bridge_session"] = ""
        write_live_identity_records(polluted, index_record=polluted)
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: verified_identity(agent, pane, pid=7002, start_time="503")  # type: ignore[assignment]
        try:
            summary = bridge_identity.backfill_session_process_identities(
                "test-session",
                {"session": "test-session", "participants": {"codex1": participant}},
                allow_create_from_hook=True,
            )
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex1", participant)
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        live = read_json(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"panes": {}, "sessions": {}})
        assert_true(summary.get("codex1", {}).get("status") == "verified", f"{label}: fresh probe should repair mismatch: {summary}")
        assert_true(((live.get("panes") or {}).get("%27a") or {}).get("session_id") == "sess-a", f"{label}: stable id must replace polluted live record: {live}")
        assert_true("codex:sess-b" not in (live.get("sessions") or {}), f"{label}: polluted session index must be removed: {live}")
        assert_true(detail.get("ok") and detail.get("pane") == "%27a", f"{label}: repaired endpoint should resolve: {detail}")
    print(f"  PASS  {label}")


def scenario_unscoped_hook_canonicalizes_via_pane_lock(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = write_identity_fixture(state_root_path, pane="%60")
        stable = identity_live_record(pane="%60", session_id="sess-a", pid=9600, start_time="1600")
        write_live_identity_records(stable, index_record=stable)
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: dict(stored_identity or verified_identity(agent, pane, pid=9600, start_time="1600"))  # type: ignore[assignment]
        try:
            bridge_identity.update_live_session(agent_type="codex", session_id="sess-b", pane="%60", event="prompt_submitted", target="tmux:1.0")
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        live = read_json(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"panes": {}, "sessions": {}})
        record = (live.get("panes") or {}).get("%60") or {}
        lock = read_json(Path(os.environ["AGENT_BRIDGE_PANE_LOCKS"]), {"panes": {}})
        state = read_json(state_root_path / "test-session" / "session.json", {})
        events = read_raw_events(state_root_path)
        assert_true(record.get("session_id") == "sess-a", f"{label}: locked session id must be preserved: {record}")
        assert_true(record.get("hook_payload_session_id") == "sess-b", f"{label}: payload id should be diagnostic: {record}")
        assert_true(record.get("canonicalized_from") == "pane_lock", f"{label}: source should be pane_lock: {record}")
        assert_true(((lock.get("panes") or {}).get("%60") or {}).get("hook_session_id") == "sess-a", f"{label}: pane lock changed: {lock}")
        assert_true((((state.get("participants") or {}).get("codex") or {}).get("hook_session_id")) == "sess-a", f"{label}: participant hook id changed: {state}")
        assert_true(any(event.get("event") == "unscoped_hook_canonicalized" for event in events), f"{label}: canonicalization event missing: {events}")
        assert_true(detail.get("ok") and detail.get("pane") == "%60", f"{label}: endpoint should remain routable: {detail}")
    print(f"  PASS  {label}")


def scenario_unscoped_hook_canonicalizes_during_attach_gap(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        write_identity_fixture(state_root_path, pane="%61")
        write_json_atomic(Path(os.environ["AGENT_BRIDGE_ATTACH_REGISTRY"]), {"version": 1, "sessions": {}})
        write_json_atomic(Path(os.environ["AGENT_BRIDGE_PANE_LOCKS"]), {"version": 1, "panes": {}})
        stable = identity_live_record(pane="%61", session_id="sess-a", pid=9610, start_time="1610")
        write_live_identity_records(stable, index_record=stable)
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: dict(stored_identity or verified_identity(agent, pane, pid=9610, start_time="1610"))  # type: ignore[assignment]
        try:
            bridge_identity.update_live_session(agent_type="codex", session_id="sess-b", pane="%61", event="prompt_submitted", target="tmux:1.0")
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        live = read_json(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"panes": {}})
        record = (live.get("panes") or {}).get("%61") or {}
        assert_true(record.get("session_id") == "sess-a", f"{label}: scoped prior live should close attach gap: {record}")
        assert_true(record.get("canonicalized_from") == "scoped_live_prior", f"{label}: source should be scoped prior: {record}")
    print(f"  PASS  {label}")


def scenario_unscoped_hook_canonicalizes_via_attached_registry(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir):
        write_identity_fixture(tmpdir / "identity-runtime" / "state", pane="%62")
        write_json_atomic(Path(os.environ["AGENT_BRIDGE_PANE_LOCKS"]), {"version": 1, "panes": {}})
        stable = identity_live_record(pane="%62", session_id="sess-a", pid=9620, start_time="1620")
        write_live_identity_records(stable, index_record=stable)
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: dict(stored_identity or verified_identity(agent, pane, pid=9620, start_time="1620"))  # type: ignore[assignment]
        try:
            bridge_identity.update_live_session(agent_type="codex", session_id="sess-b", pane="%62", event="prompt_submitted", target="tmux:1.0")
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        live = read_json(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"panes": {}})
        record = (live.get("panes") or {}).get("%62") or {}
        assert_true(record.get("session_id") == "sess-a", f"{label}: registry authority should preserve stable id: {record}")
        assert_true(record.get("canonicalized_from") == "attached_registry", f"{label}: source should be attached_registry: {record}")
    print(f"  PASS  {label}")


def scenario_unscoped_hook_new_process_fails_closed(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = write_identity_fixture(state_root_path, pane="%63")
        stable = identity_live_record(pane="%63", session_id="sess-a", pid=9630, start_time="1630")
        write_live_identity_records(stable, index_record=stable)
        old_probe = bridge_identity.probe_agent_process
        def probe(pane, agent, stored_identity=None):
            if stored_identity:
                return {"status": "mismatch", "reason": "process_fingerprint_mismatch", "pane": pane, "agent": agent, "processes": []}
            return verified_identity(agent, pane, pid=9631, start_time="1631")
        bridge_identity.probe_agent_process = probe  # type: ignore[assignment]
        try:
            bridge_identity.update_live_session(agent_type="codex", session_id="sess-b", pane="%63", event="prompt_submitted", target="tmux:1.0")
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        live = read_json(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"panes": {}})
        state = read_json(state_root_path / "test-session" / "session.json", {})
        events = read_raw_events(state_root_path)
        assert_true(((live.get("panes") or {}).get("%63") or {}).get("session_id") == "sess-b", f"{label}: existing takeover semantics should write new live id: {live}")
        assert_true((((state.get("participants") or {}).get("codex") or {}).get("endpoint_status")) == "endpoint_lost", f"{label}: participant should be endpoint_lost: {state}")
        assert_true(not detail.get("ok") and detail.get("reason") == "live_record_mismatch", f"{label}: old alias must not route to new process: {detail}")
        assert_true(any(event.get("event") == "unscoped_hook_canonicalize_blocked" and event.get("reason") == "process_fingerprint_mismatch" for event in events), f"{label}: blocked event missing: {events}")
    print(f"  PASS  {label}")


def scenario_unscoped_hook_cross_reboot_fails_closed(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        write_identity_fixture(state_root_path, pane="%64")
        identity = verified_identity("codex", "%64", pid=9640, start_time="1640")
        identity["boot_id"] = "boot-old"
        stable = identity_live_record(pane="%64", session_id="sess-a", process_identity=identity)
        write_live_identity_records(stable, index_record=stable)
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: (
            {"status": "mismatch", "reason": "boot_id_mismatch", "pane": pane, "agent": agent, "processes": []}
            if stored_identity else verified_identity(agent, pane, pid=9641, start_time="1641")
        )  # type: ignore[assignment]
        try:
            bridge_identity.update_live_session(agent_type="codex", session_id="sess-b", pane="%64", event="prompt_submitted", target="tmux:1.0")
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        state = read_json(state_root_path / "test-session" / "session.json", {})
        events = read_raw_events(state_root_path)
        assert_true((((state.get("participants") or {}).get("codex") or {}).get("endpoint_status")) == "endpoint_lost", f"{label}: reboot mismatch must fail closed: {state}")
        assert_true(any(event.get("event") == "unscoped_hook_canonicalize_blocked" and event.get("reason") == "boot_id_mismatch" for event in events), f"{label}: boot mismatch event missing: {events}")
    print(f"  PASS  {label}")


def scenario_unscoped_hook_missing_prior_fingerprint_fails_closed(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        write_identity_fixture(state_root_path, pane="%65")
        weak = identity_live_record(pane="%65", session_id="sess-a", process_identity={"status": "unknown", "reason": "ps_unavailable", "processes": []})
        write_live_identity_records(weak, index_record=weak)
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: verified_identity(agent, pane, pid=9651, start_time="1651")  # type: ignore[assignment]
        try:
            bridge_identity.update_live_session(agent_type="codex", session_id="sess-b", pane="%65", event="prompt_submitted", target="tmux:1.0")
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        state = read_json(state_root_path / "test-session" / "session.json", {})
        events = read_raw_events(state_root_path)
        assert_true((((state.get("participants") or {}).get("codex") or {}).get("endpoint_status")) == "endpoint_lost", f"{label}: missing prior fingerprint must fail closed: {state}")
        assert_true(any(event.get("event") == "unscoped_hook_canonicalize_blocked" and event.get("reason") == "missing_prior_verified_fingerprint" for event in events), f"{label}: missing fingerprint event absent: {events}")
    print(f"  PASS  {label}")


def scenario_unscoped_hook_different_agent_fails_closed(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = write_identity_fixture(state_root_path, pane="%66")
        stable = identity_live_record(pane="%66", session_id="sess-a", pid=9660, start_time="1660")
        write_live_identity_records(stable, index_record=stable)
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: verified_identity(agent, pane, pid=9661, start_time="1661")  # type: ignore[assignment]
        try:
            bridge_identity.update_live_session(agent_type="claude", session_id="sess-b", pane="%66", event="prompt_submitted", target="tmux:1.0")
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        state = read_json(state_root_path / "test-session" / "session.json", {})
        assert_true((((state.get("participants") or {}).get("codex") or {}).get("endpoint_status")) == "endpoint_lost", f"{label}: different agent must mark old endpoint lost: {state}")
        assert_true(not detail.get("ok") and detail.get("reason") == "live_record_mismatch", f"{label}: codex alias must not route to claude: {detail}")
    print(f"  PASS  {label}")


def scenario_unscoped_hook_same_session_no_canonicalization_event(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        write_identity_fixture(state_root_path, pane="%67")
        stable = identity_live_record(pane="%67", session_id="sess-a", pid=9670, start_time="1670")
        write_live_identity_records(stable, index_record=stable)
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: dict(stored_identity or verified_identity(agent, pane, pid=9670, start_time="1670"))  # type: ignore[assignment]
        try:
            bridge_identity.update_live_session(agent_type="codex", session_id="sess-a", pane="%67", event="prompt_submitted", target="tmux:1.0")
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        events = read_raw_events(state_root_path)
        assert_true(not any(str(event.get("event") or "").startswith("unscoped_hook_canonical") for event in events), f"{label}: same-session update should not canonicalize: {events}")
    print(f"  PASS  {label}")


def scenario_scoped_different_session_no_canonicalization(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        write_identity_fixture(state_root_path, pane="%68")
        stable = identity_live_record(pane="%68", session_id="sess-a", pid=9680, start_time="1680")
        write_live_identity_records(stable, index_record=stable)
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: verified_identity(agent, pane, pid=9681, start_time="1681")  # type: ignore[assignment]
        try:
            bridge_identity.update_live_session(agent_type="codex", session_id="sess-b", pane="%68", bridge_session="other-session", alias="codex2", event="prompt_submitted", target="tmux:1.0")
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        events = read_raw_events(state_root_path)
        assert_true(not any(event.get("event") == "unscoped_hook_canonicalized" for event in events), f"{label}: scoped handoff must not use unscoped canonicalization: {events}")
    print(f"  PASS  {label}")


def scenario_session_ended_payload_mismatch_no_canonicalization(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        write_identity_fixture(state_root_path, pane="%69")
        stable = identity_live_record(pane="%69", session_id="sess-a", pid=9690, start_time="1690")
        write_live_identity_records(stable, index_record=stable)
        bridge_identity.update_live_session(agent_type="codex", session_id="sess-b", pane="%69", event="session_ended", target="tmux:1.0")
        live = read_json(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"panes": {}})
        events = read_raw_events(state_root_path)
        assert_true(((live.get("panes") or {}).get("%69") or {}).get("session_id") == "sess-a", f"{label}: mismatched session_ended should be no-op: {live}")
        assert_true(not any(str(event.get("event") or "").startswith("unscoped_hook_canonical") for event in events), f"{label}: session_ended must bypass canonicalization: {events}")
    print(f"  PASS  {label}")


def scenario_resolver_reconnects_exact_mismatch_shape(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = write_identity_fixture(state_root_path, pane="%70")
        old_other = identity_live_record(pane="%70", session_id="sess-b", alias="", pid=9700, start_time="1700")
        old_other["bridge_session"] = ""
        candidate = identity_live_record(pane="%71", session_id="sess-a", pid=9701, start_time="1701")
        write_live_identity_records(old_other, candidate, index_record=candidate)
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: dict(stored_identity or verified_identity(agent, pane))  # type: ignore[assignment]
        try:
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        registry = read_json(Path(os.environ["AGENT_BRIDGE_ATTACH_REGISTRY"]), {"sessions": {}})
        locks = read_json(Path(os.environ["AGENT_BRIDGE_PANE_LOCKS"]), {"panes": {}})
        state = read_json(state_root_path / "test-session" / "session.json", {})
        assert_true(detail.get("ok") and detail.get("pane") == "%71", f"{label}: resolver should recover exact mismatch shape: {detail}")
        assert_true(((registry.get("sessions") or {}).get("codex:sess-a") or {}).get("pane") == "%71", f"{label}: registry not moved: {registry}")
        assert_true("%70" not in (locks.get("panes") or {}) and ((locks.get("panes") or {}).get("%71") or {}).get("hook_session_id") == "sess-a", f"{label}: locks not moved: {locks}")
        assert_true((((state.get("participants") or {}).get("codex") or {}).get("pane")) == "%71", f"{label}: participant pane not moved: {state}")
    print(f"  PASS  {label}")


def scenario_hook_reconnects_exact_mismatch_shape(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        write_identity_fixture(state_root_path, pane="%72")
        old_other = identity_live_record(pane="%72", session_id="sess-b", alias="", pid=9720, start_time="1720")
        old_other["bridge_session"] = ""
        write_live_identity_records(old_other, index_record=old_other)
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: verified_identity(agent, pane, pid=9721, start_time="1721")  # type: ignore[assignment]
        try:
            mapping = bridge_identity.update_live_session(agent_type="codex", session_id="sess-a", pane="%73", bridge_session="test-session", alias="codex", event="prompt_submitted", target="tmux:1.0")
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        registry = read_json(Path(os.environ["AGENT_BRIDGE_ATTACH_REGISTRY"]), {"sessions": {}})
        state = read_json(state_root_path / "test-session" / "session.json", {})
        assert_true(mapping and mapping.get("pane") == "%73", f"{label}: hook path should reconnect to new stable pane: {mapping}")
        assert_true(((registry.get("sessions") or {}).get("codex:sess-a") or {}).get("pane") == "%73", f"{label}: registry not moved: {registry}")
        assert_true((((state.get("participants") or {}).get("codex") or {}).get("pane")) == "%73", f"{label}: participant pane not moved: {state}")
    print(f"  PASS  {label}")


def scenario_cross_pane_candidate_mismatch_blocks_reconnect(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = write_identity_fixture(state_root_path, pane="%74")
        candidate = identity_live_record(pane="%75", session_id="sess-a", pid=9750, start_time="1750")
        write_live_identity_records(candidate, index_record=candidate)
        old_probe = bridge_identity.probe_agent_process
        def probe(pane, agent, stored_identity=None):
            if pane == "%75":
                return {"status": "mismatch", "reason": "process_fingerprint_mismatch", "pane": pane, "agent": agent, "processes": []}
            return {"status": "unknown", "reason": "ps_unavailable", "pane": pane, "agent": agent, "processes": []}
        bridge_identity.probe_agent_process = probe  # type: ignore[assignment]
        try:
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        registry = read_json(Path(os.environ["AGENT_BRIDGE_ATTACH_REGISTRY"]), {"sessions": {}})
        assert_true(not detail.get("ok") and detail.get("reason") == "live_record_missing", f"{label}: dead candidate must not reconnect: {detail}")
        assert_true(((registry.get("sessions") or {}).get("codex:sess-a") or {}).get("pane") == "%74", f"{label}: mapping should stay old: {registry}")
    print(f"  PASS  {label}")


def scenario_codex1_incident_replay_canonicalizes_repeated_unscoped_hooks(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = write_identity_fixture(state_root_path, alias="codex1", session_id="019dce7c", pane="%76")
        stable = identity_live_record(pane="%76", session_id="019dce7c", alias="codex1", pid=9760, start_time="1760")
        write_live_identity_records(stable, index_record=stable)
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: dict(stored_identity or verified_identity(agent, pane, pid=9760, start_time="1760"))  # type: ignore[assignment]
        try:
            for suffix in range(5):
                bridge_identity.update_live_session(agent_type="codex", session_id=f"019dceb{suffix}", pane="%76", event="response_finished", target="tmux:1.0", model="gpt-5.4-mini")
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex1", participant)
            summary = bridge_identity.backfill_session_process_identities("test-session", {"session": "test-session", "participants": {"codex1": participant}})
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        live = read_json(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"panes": {}})
        events = read_raw_events(state_root_path)
        record = (live.get("panes") or {}).get("%76") or {}
        assert_true(record.get("session_id") == "019dce7c", f"{label}: incident replay should keep stable id: {record}")
        assert_true(sum(1 for event in events if event.get("event") == "unscoped_hook_canonicalized") == 5, f"{label}: expected 5 canonicalization events: {events}")
        assert_true(detail.get("ok") and detail.get("pane") == "%76", f"{label}: alias should remain routable: {detail}")
        assert_true(summary.get("codex1", {}).get("status") == "verified", f"{label}: healthcheck backfill should report healthy: {summary}")
    print(f"  PASS  {label}")


def _target_recovery_fixture(
    state_root_path: Path,
    *,
    session_id: str = "019dce7c-3126-7983-88c0-a3d80cb6f556",
    old_pane: str = "%80",
    target: str = "0:1.2",
) -> dict:
    participant = write_identity_fixture(state_root_path, session_id=session_id, pane=old_pane)
    set_identity_target(state_root_path, session_id=session_id, pane=old_pane, target=target)
    stable = identity_live_record(pane=old_pane, session_id=session_id, pid=9800, start_time="1800")
    stable["target"] = target
    write_live_identity_records(stable, index_record=stable)
    return participant


def _patch_target_recovery(
    *,
    target: str = "0:1.2",
    new_pane: str = "%81",
    session_id: str = "019dce7c-3126-7983-88c0-a3d80cb6f556",
    proof_session: str | None = "019dce7c-3126-7983-88c0-a3d80cb6f556",
    proof_pid: int = 9801,
    probe_verified: bool = True,
    owner_pid: int | None = None,
):
    old_probe = bridge_identity.probe_agent_process
    old_pane_for_target = bridge_identity.tmux_pane_for_target
    old_display = bridge_identity.tmux_display_pane
    old_transcript = bridge_identity.transcript_session_id_for_pid
    old_owners = bridge_identity.transcript_owners_for_session

    def probe(pane, agent, stored_identity=None):
        if pane == new_pane and probe_verified:
            identity = verified_identity(agent, pane, pid=proof_pid, start_time="1801")
            identity["target"] = target
            return identity
        if pane == new_pane:
            return {"status": "mismatch", "reason": "agent_process_not_found", "pane": pane, "agent": agent, "processes": []}
        return {"status": "mismatch", "reason": "pane_unavailable", "pane": pane, "agent": agent, "processes": []}

    def transcript(agent, pid):
        if int(str(pid)) == proof_pid and proof_session:
            return {
                "session_id": proof_session,
                "path": f"/root/.codex/sessions/2026/04/27/rollout-2026-04-27T18-28-58-{proof_session}.jsonl",
                "pid": proof_pid,
            }
        return {}

    def owners(agent, wanted_session):
        if owner_pid:
            return [
                {
                    "session_id": wanted_session,
                    "path": f"/root/.codex/sessions/2026/04/27/rollout-2026-04-27T18-28-58-{wanted_session}.jsonl",
                    "pid": owner_pid,
                }
            ]
        return []

    bridge_identity.probe_agent_process = probe  # type: ignore[assignment]
    bridge_identity.tmux_pane_for_target = lambda candidate: new_pane if candidate == target else ""  # type: ignore[assignment]
    bridge_identity.tmux_display_pane = lambda pane: (  # type: ignore[assignment]
        {"pane_id": new_pane, "pane_pid": "200", "target": target, "command": "node", "cwd": "/data/sembench-hard"}
        if pane == new_pane else {"error": "pane_unavailable", "detail": "stale"}
    )
    bridge_identity.transcript_session_id_for_pid = transcript  # type: ignore[assignment]
    bridge_identity.transcript_owners_for_session = owners  # type: ignore[assignment]

    def restore():
        bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        bridge_identity.tmux_pane_for_target = old_pane_for_target  # type: ignore[assignment]
        bridge_identity.tmux_display_pane = old_display  # type: ignore[assignment]
        bridge_identity.transcript_session_id_for_pid = old_transcript  # type: ignore[assignment]
        bridge_identity.transcript_owners_for_session = old_owners  # type: ignore[assignment]

    return restore


def scenario_target_recovery_reconnects_stale_pane_by_codex_transcript(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        session_id = "019dce7c-3126-7983-88c0-a3d80cb6f556"
        participant = _target_recovery_fixture(state_root_path, session_id=session_id, old_pane="%80", target="0:1.2")
        restore = _patch_target_recovery(session_id=session_id, proof_session=session_id, new_pane="%81", target="0:1.2")
        try:
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        finally:
            restore()
        live = read_json(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"panes": {}, "sessions": {}})
        registry = read_json(Path(os.environ["AGENT_BRIDGE_ATTACH_REGISTRY"]), {"sessions": {}})
        locks = read_json(Path(os.environ["AGENT_BRIDGE_PANE_LOCKS"]), {"panes": {}})
        state = read_json(state_root_path / "test-session" / "session.json", {})
        events = read_raw_events(state_root_path)
        assert_true(detail.get("ok") and detail.get("pane") == "%81", f"{label}: resolver should recover target pane: {detail}")
        assert_true(((live.get("panes") or {}).get("%81") or {}).get("session_id") == session_id, f"{label}: live not moved to new pane: {live}")
        assert_true(((registry.get("sessions") or {}).get(f"codex:{session_id}") or {}).get("pane") == "%81", f"{label}: registry not moved: {registry}")
        assert_true("%80" not in (locks.get("panes") or {}) and ((locks.get("panes") or {}).get("%81") or {}).get("hook_session_id") == session_id, f"{label}: pane lock not moved: {locks}")
        assert_true((((state.get("participants") or {}).get("codex") or {}).get("hook_session_id")) == session_id, f"{label}: hook id changed: {state}")
        assert_true((((state.get("participants") or {}).get("codex") or {}).get("pane")) == "%81", f"{label}: participant pane not moved: {state}")
        assert_true(any(event.get("event") == "target_recovery_reconnected" and event.get("transcript_path") for event in events), f"{label}: recovery event missing: {events}")
    print(f"  PASS  {label}")


def scenario_target_recovery_blocks_transcript_session_mismatch(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = _target_recovery_fixture(state_root_path, old_pane="%82", target="0:1.2")
        restore = _patch_target_recovery(proof_session="019dceff-0000-7000-8000-000000000000", new_pane="%83", target="0:1.2")
        try:
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        finally:
            restore()
        events = read_raw_events(state_root_path)
        state = read_json(state_root_path / "test-session" / "session.json", {})
        assert_true(not detail.get("ok"), f"{label}: mismatch proof must not recover: {detail}")
        assert_true((((state.get("participants") or {}).get("codex") or {}).get("endpoint_status")) == "endpoint_lost", f"{label}: endpoint_lost expected: {state}")
        assert_true(any(event.get("event") == "target_recovery_blocked" and event.get("reason") == "transcript_session_mismatch" for event in events), f"{label}: blocked event missing: {events}")
    print(f"  PASS  {label}")


def scenario_target_recovery_blocks_missing_transcript(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = _target_recovery_fixture(state_root_path, old_pane="%84", target="0:1.2")
        restore = _patch_target_recovery(proof_session=None, new_pane="%85", target="0:1.2")
        try:
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        finally:
            restore()
        events = read_raw_events(state_root_path)
        assert_true(not detail.get("ok"), f"{label}: missing transcript must not recover: {detail}")
        assert_true(any(event.get("event") == "target_recovery_blocked" and event.get("reason") == "transcript_proof_missing" for event in events), f"{label}: missing transcript event absent: {events}")
    print(f"  PASS  {label}")


def scenario_target_recovery_blocks_wrong_pid_transcript(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        session_id = "019dce7c-3126-7983-88c0-a3d80cb6f556"
        participant = _target_recovery_fixture(state_root_path, session_id=session_id, old_pane="%86", target="0:1.2")
        restore = _patch_target_recovery(session_id=session_id, proof_session=None, proof_pid=9801, owner_pid=9900, new_pane="%87", target="0:1.2")
        try:
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        finally:
            restore()
        events = read_raw_events(state_root_path)
        assert_true(not detail.get("ok"), f"{label}: wrong pid proof must not recover: {detail}")
        assert_true(any(event.get("event") == "target_recovery_blocked" and event.get("reason") == "wrong_pid_owns_transcript" for event in events), f"{label}: wrong pid event absent: {events}")
    print(f"  PASS  {label}")


def scenario_target_recovery_blocks_different_agent_at_target(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = _target_recovery_fixture(state_root_path, old_pane="%88", target="0:1.2")
        restore = _patch_target_recovery(probe_verified=False, new_pane="%89", target="0:1.2")
        try:
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        finally:
            restore()
        events = read_raw_events(state_root_path)
        assert_true(not detail.get("ok"), f"{label}: different agent/no codex must not recover: {detail}")
        assert_true(any(event.get("event") == "target_recovery_blocked" and event.get("reason") == "probe_not_verified" for event in events), f"{label}: probe_not_verified absent: {events}")
    print(f"  PASS  {label}")


def scenario_target_recovery_blocks_same_pane_target(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = _target_recovery_fixture(state_root_path, old_pane="%90", target="0:1.2")
        old_probe = bridge_identity.probe_agent_process
        old_pane_for_target = bridge_identity.tmux_pane_for_target
        old_display = bridge_identity.tmux_display_pane
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: (  # type: ignore[assignment]
            {"status": "mismatch", "reason": "pane_unavailable", "pane": pane, "agent": agent, "processes": []}
            if stored_identity else verified_identity(agent, pane, pid=9901, start_time="1901")
        )
        bridge_identity.tmux_pane_for_target = lambda target: "%90"  # type: ignore[assignment]
        bridge_identity.tmux_display_pane = lambda pane: {"pane_id": "%90", "pane_pid": "200", "target": "0:1.2", "command": "node", "cwd": "/data/sembench-hard"}  # type: ignore[assignment]
        try:
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
            bridge_identity.tmux_pane_for_target = old_pane_for_target  # type: ignore[assignment]
            bridge_identity.tmux_display_pane = old_display  # type: ignore[assignment]
        events = read_raw_events(state_root_path)
        assert_true(not detail.get("ok"), f"{label}: same-pane target must not recover: {detail}")
        assert_true(any(event.get("event") == "target_recovery_blocked" and event.get("reason") == "target_resolves_same_pane" for event in events), f"{label}: same pane event absent: {events}")
    print(f"  PASS  {label}")


def scenario_target_recovery_blocks_unresolvable_target(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = _target_recovery_fixture(state_root_path, old_pane="%91", target="0:1.2")
        restore = _patch_target_recovery(new_pane="", target="0:1.2")
        try:
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        finally:
            restore()
        events = read_raw_events(state_root_path)
        assert_true(not detail.get("ok"), f"{label}: unresolvable target must not recover: {detail}")
        assert_true(any(event.get("event") == "target_recovery_blocked" and event.get("reason") == "target_unresolvable" for event in events), f"{label}: unresolvable event absent: {events}")
    print(f"  PASS  {label}")


def scenario_target_recovery_skips_without_participant_target(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = _target_recovery_fixture(state_root_path, old_pane="%92", target="")
        restore = _patch_target_recovery(new_pane="%93", target="")
        try:
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        finally:
            restore()
        events = read_raw_events(state_root_path)
        assert_true(not detail.get("ok"), f"{label}: empty target should preserve failure: {detail}")
        assert_true(not any(str(event.get("event") or "").startswith("target_recovery") for event in events), f"{label}: no target recovery event expected: {events}")
    print(f"  PASS  {label}")


def scenario_target_recovery_read_purpose_does_not_mutate(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        session_id = "019dce7c-3126-7983-88c0-a3d80cb6f556"
        participant = _target_recovery_fixture(state_root_path, session_id=session_id, old_pane="%94", target="0:1.2")
        restore = _patch_target_recovery(session_id=session_id, proof_session=session_id, new_pane="%95", target="0:1.2")
        try:
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant, purpose="read")
        finally:
            restore()
        registry = read_json(Path(os.environ["AGENT_BRIDGE_ATTACH_REGISTRY"]), {"sessions": {}})
        state = read_json(state_root_path / "test-session" / "session.json", {})
        events = read_raw_events(state_root_path)
        assert_true(not detail.get("ok"), f"{label}: read purpose should not write recovery: {detail}")
        assert_true(((registry.get("sessions") or {}).get(f"codex:{session_id}") or {}).get("pane") == "%94", f"{label}: registry mutated on read: {registry}")
        assert_true((((state.get("participants") or {}).get("codex") or {}).get("pane")) == "%94", f"{label}: participant mutated on read: {state}")
        assert_true(not any(event.get("event") == "target_recovery_reconnected" for event in events), f"{label}: reconnected event on read: {events}")
    print(f"  PASS  {label}")


def scenario_target_recovery_env_disable_blocks(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        session_id = "019dce7c-3126-7983-88c0-a3d80cb6f556"
        participant = _target_recovery_fixture(state_root_path, session_id=session_id, old_pane="%99", target="0:1.2")
        restore = _patch_target_recovery(session_id=session_id, proof_session=session_id, new_pane="%100", target="0:1.2")
        try:
            with patched_environ(AGENT_BRIDGE_NO_TARGET_RECOVERY="1"):
                detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        finally:
            restore()
        events = read_raw_events(state_root_path)
        state = read_json(state_root_path / "test-session" / "session.json", {})
        assert_true(not detail.get("ok"), f"{label}: env disabled recovery should not recover: {detail}")
        assert_true((((state.get("participants") or {}).get("codex") or {}).get("pane")) == "%99", f"{label}: participant mutated despite env disable: {state}")
        assert_true(not any(event.get("event") == "target_recovery_reconnected" for event in events), f"{label}: reconnected event despite env disable: {events}")
    print(f"  PASS  {label}")


def scenario_tmux_display_pane_empty_metadata_is_unavailable(label: str, tmpdir: Path) -> None:
    old_run = bridge_pane_probe.run
    bridge_pane_probe.run = lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "\t\t:.\t\t\n", "")  # type: ignore[assignment]
    try:
        pane = bridge_pane_probe.tmux_display_pane("%dead")
    finally:
        bridge_pane_probe.run = old_run  # type: ignore[assignment]
    assert_true(pane.get("error") == "pane_unavailable", f"{label}: empty pane metadata should be unavailable: {pane}")
    print(f"  PASS  {label}")


def scenario_codex_rollout_path_regex_is_strict(label: str, tmpdir: Path) -> None:
    session_id = "019dce7c-3126-7983-88c0-a3d80cb6f556"
    valid = f"/root/.codex/sessions/2026/04/27/rollout-2026-04-27T18-28-58-{session_id}.jsonl"
    assert_true(bridge_pane_probe.codex_session_id_from_rollout_path(valid) == session_id, f"{label}: valid rollout not parsed")
    loose = f"/root/.codex/sessions/2026/04/27/not-a-rollout-{session_id}.jsonl"
    assert_true(bridge_pane_probe.codex_session_id_from_rollout_path(loose) == "", f"{label}: loose substring matched")
    malformed = f"/root/.codex/sessions/2026/04/27/rollout-2026-04-27T18:28:58-{session_id}.jsonl"
    assert_true(bridge_pane_probe.codex_session_id_from_rollout_path(malformed) == "", f"{label}: malformed timestamp matched")
    print(f"  PASS  {label}")


def scenario_target_recovery_only_matching_alias_recovers(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        session_a = "019dce7c-3126-7983-88c0-a3d80cb6f556"
        session_b = "019dceff-0000-7000-8000-000000000000"
        state_dir = state_root_path / "test-session"
        participant_a = {"alias": "codex", "agent_type": "codex", "pane": "%96", "target": "0:1.2", "hook_session_id": session_a, "status": "active"}
        participant_b = {"alias": "codex2", "agent_type": "codex", "pane": "%97", "target": "0:1.2", "hook_session_id": session_b, "status": "active"}
        write_json_atomic(
            state_dir / "session.json",
            {
                "session": "test-session",
                "participants": {"codex": participant_a, "codex2": participant_b},
                "panes": {"codex": "%96", "codex2": "%97"},
                "targets": {"codex": "0:1.2", "codex2": "0:1.2"},
            },
        )
        write_json_atomic(
            Path(os.environ["AGENT_BRIDGE_ATTACH_REGISTRY"]),
            {
                "version": 1,
                "sessions": {
                    f"codex:{session_a}": {"agent": "codex", "alias": "codex", "session_id": session_a, "bridge_session": "test-session", "pane": "%96", "target": "0:1.2"},
                    f"codex:{session_b}": {"agent": "codex", "alias": "codex2", "session_id": session_b, "bridge_session": "test-session", "pane": "%97", "target": "0:1.2"},
                },
            },
        )
        write_json_atomic(
            Path(os.environ["AGENT_BRIDGE_PANE_LOCKS"]),
            {
                "version": 1,
                "panes": {
                    "%96": {"bridge_session": "test-session", "agent": "codex", "alias": "codex", "target": "0:1.2", "hook_session_id": session_a},
                    "%97": {"bridge_session": "test-session", "agent": "codex", "alias": "codex2", "target": "0:1.2", "hook_session_id": session_b},
                },
            },
        )
        stable_a = identity_live_record(pane="%96", session_id=session_a, alias="codex", pid=9890, start_time="1890")
        stable_a["target"] = "0:1.2"
        stable_b = identity_live_record(pane="%97", session_id=session_b, alias="codex2", pid=9900, start_time="1900")
        stable_b["target"] = "0:1.2"
        write_live_identity_records(stable_a, stable_b, index_record=stable_a)
        live = read_json(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"panes": {}, "sessions": {}})
        live.setdefault("sessions", {})[f"codex:{session_b}"] = stable_b
        write_json_atomic(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), live)
        restore = _patch_target_recovery(session_id=session_a, proof_session=session_a, new_pane="%98", target="0:1.2")
        try:
            detail_a = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant_a)
            detail_b = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex2", participant_b)
        finally:
            restore()
        assert_true(detail_a.get("ok") and detail_a.get("pane") == "%98", f"{label}: matching alias should recover: {detail_a}")
        assert_true(not detail_b.get("ok"), f"{label}: nonmatching alias must not recover: {detail_b}")
    print(f"  PASS  {label}")


def scenario_hook_unknown_preserves_verified_process_identity(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        write_identity_fixture(state_root_path, pane="%28")
        original_identity = verified_identity("codex", "%28", pid=8000, start_time="601")
        live = {
            "agent": "codex",
            "session_id": "sess-a",
            "pane": "%28",
            "target": "tmux:1.0",
            "bridge_session": "test-session",
            "alias": "codex",
            "last_seen_at": utc_now(),
            "process_identity": original_identity,
        }
        write_json_atomic(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"version": 1, "panes": {"%28": live}, "sessions": {"codex:sess-a": live}})
        old_probe = bridge_identity.probe_agent_process
        old_target = bridge_identity.tmux_target_for_pane
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: {"status": "unknown", "reason": "ps_unavailable", "pane": pane, "agent": agent, "processes": []}  # type: ignore[assignment]
        bridge_identity.tmux_target_for_pane = lambda pane: "tmux:1.0"  # type: ignore[assignment]
        try:
            bridge_identity.update_live_session(agent_type="codex", session_id="sess-a", pane="%28", bridge_session="test-session", alias="codex", event="prompt_submitted")
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
            bridge_identity.tmux_target_for_pane = old_target  # type: ignore[assignment]
        live_after = read_json(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"panes": {}})
        record = (live_after.get("panes") or {}).get("%28") or {}
        proc = ((record.get("process_identity") or {}).get("processes") or [{}])[0]
        assert_true(proc.get("pid") == 8000, f"{label}: verified fingerprint must be preserved: {record}")
        assert_true((record.get("process_identity_diagnostics") or {}).get("reason") == "ps_unavailable", f"{label}: unknown probe diagnostics retained: {record}")
    print(f"  PASS  {label}")


def scenario_probe_tmux_access_failure_unknown(label: str, tmpdir: Path) -> None:
    old_run = bridge_pane_probe.run
    try:
        bridge_pane_probe.run = lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "no server running on /tmp/tmux-0/default")  # type: ignore[assignment]
        unknown = bridge_pane_probe.probe_agent_process("%29", "codex")
        bridge_pane_probe.run = lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "can't find pane: %29")  # type: ignore[assignment]
        missing = bridge_pane_probe.probe_agent_process("%29", "codex")
    finally:
        bridge_pane_probe.run = old_run  # type: ignore[assignment]
    assert_true(unknown.get("status") == "unknown" and unknown.get("reason") == "tmux_access_failed", f"{label}: tmux access failure must be unknown: {unknown}")
    assert_true(missing.get("status") == "mismatch" and missing.get("reason") == "pane_unavailable", f"{label}: positive missing pane remains mismatch: {missing}")
    print(f"  PASS  {label}")


def scenario_endpoint_read_mismatch_does_not_mutate(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = write_identity_fixture(state_root_path, alias="codexA", session_id="sess-a", pane="%30")
        other = {
            "agent": "codex",
            "session_id": "sess-b",
            "pane": "%30",
            "target": "tmux:1.0",
            "bridge_session": "test-session",
            "alias": "codexB",
            "last_seen_at": utc_now(),
            "process_identity": verified_identity("codex", "%30", pid=9000, start_time="701"),
        }
        write_json_atomic(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"version": 1, "panes": {"%30": other}, "sessions": {"codex:sess-b": other}})
        detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codexA", participant, purpose="read")
        assert_true(not detail.get("ok") and not detail.get("should_detach"), f"{label}: read mismatch should fail without detach directive: {detail}")
        locks = read_json(Path(os.environ["AGENT_BRIDGE_PANE_LOCKS"]), {"panes": {}})
        assert_true("%30" in (locks.get("panes") or {}), f"{label}: read path must not clear pane lock")
        state = read_json(state_root_path / "test-session" / "session.json", {})
        record = ((state.get("participants") or {}).get("codexA") or {})
        assert_true(record.get("endpoint_status") != "endpoint_lost", f"{label}: read path must not mark endpoint lost")
    print(f"  PASS  {label}")


def scenario_verified_candidate_ordering_prefers_pane_then_newest(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir):
        write_identity_fixture(tmpdir / "identity-runtime" / "state", pane="%40")
        older = identity_live_record(pane="%40", pid=9400, start_time="901", last_seen_at="2026-01-01T00:00:00Z")
        newer = identity_live_record(pane="%41", pid=9401, start_time="902", last_seen_at="2026-01-02T00:00:00Z")
        write_live_identity_records(older, newer, index_record=older)
        calls: list[str] = []
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: (calls.append(pane) or dict(stored_identity or verified_identity(agent, pane)))  # type: ignore[assignment]
        try:
            preferred = bridge_identity.find_verified_live_record_for_identity("codex", "sess-a", prefer_pane="%40")
            calls.clear()
            newest = bridge_identity.find_verified_live_record_for_identity("codex", "sess-a")
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        assert_true(preferred.get("pane") == "%40", f"{label}: prefer_pane must win before timestamp: {preferred}")
        assert_true(newest.get("pane") == "%41", f"{label}: without prefer_pane newest verified candidate must win: {newest}")
        assert_true(calls == ["%41"], f"{label}: verified helper should short-circuit on first newest candidate: {calls}")
    print(f"  PASS  {label}")


def scenario_resume_new_pane_reconnects_unknown_old_and_logs(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        write_identity_fixture(state_root_path, pane="%42")
        old_probe = bridge_identity.probe_agent_process
        old_target = bridge_identity.tmux_target_for_pane
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: verified_identity(agent, pane, pid=9500, start_time="1001")  # type: ignore[assignment]
        bridge_identity.tmux_target_for_pane = lambda pane: "tmux:1.0"  # type: ignore[assignment]
        try:
            mapping = bridge_identity.update_live_session(
                agent_type="codex",
                session_id="sess-a",
                pane="%43",
                bridge_session="test-session",
                alias="codex",
                event="prompt_submitted",
            )
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
            bridge_identity.tmux_target_for_pane = old_target  # type: ignore[assignment]
        assert_true(mapping and mapping.get("pane") == "%43", f"{label}: hook should reconnect to new verified pane: {mapping}")
        registry = read_json(Path(os.environ["AGENT_BRIDGE_ATTACH_REGISTRY"]), {"sessions": {}})
        attached = (registry.get("sessions") or {}).get("codex:sess-a") or {}
        assert_true(attached.get("pane") == "%43", f"{label}: attached mapping not updated: {attached}")
        state = read_json(state_root_path / "test-session" / "session.json", {})
        participant = ((state.get("participants") or {}).get("codex") or {})
        assert_true(participant.get("pane") == "%43", f"{label}: participant pane not refreshed: {participant}")
        locks = read_json(Path(os.environ["AGENT_BRIDGE_PANE_LOCKS"]), {"panes": {}})
        assert_true("%42" in (locks.get("panes") or {}) and "%43" in (locks.get("panes") or {}), f"{label}: unknown-old reconnect must not clear old diagnostic lock: {locks}")
        events = read_raw_events(state_root_path)
        reconnects = [event for event in events if event.get("event") == "endpoint_auto_reconnected"]
        assert_true(reconnects and reconnects[-1].get("reason") == "mapped_endpoint_unknown", f"{label}: reconnect log missing/incorrect: {events}")
        assert_true(reconnects[-1].get("old_status") == "unknown", f"{label}: old_status should be unknown: {reconnects[-1]}")
    print(f"  PASS  {label}")


def scenario_resume_unknown_old_opt_out_blocks_switch(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        write_identity_fixture(state_root_path, pane="%44")
        os.environ["AGENT_BRIDGE_NO_RESUME_FROM_UNKNOWN"] = "1"
        old_probe = bridge_identity.probe_agent_process
        old_target = bridge_identity.tmux_target_for_pane
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: verified_identity(agent, pane, pid=9501, start_time="1002")  # type: ignore[assignment]
        bridge_identity.tmux_target_for_pane = lambda pane: "tmux:1.0"  # type: ignore[assignment]
        try:
            mapping = bridge_identity.update_live_session(
                agent_type="codex",
                session_id="sess-a",
                pane="%45",
                bridge_session="test-session",
                alias="codex",
                event="prompt_submitted",
            )
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
            bridge_identity.tmux_target_for_pane = old_target  # type: ignore[assignment]
        registry = read_json(Path(os.environ["AGENT_BRIDGE_ATTACH_REGISTRY"]), {"sessions": {}})
        attached = (registry.get("sessions") or {}).get("codex:sess-a") or {}
        assert_true(mapping is None, f"{label}: opt-out should suppress unknown-old reconnect return: {mapping}")
        assert_true(attached.get("pane") == "%44", f"{label}: opt-out should keep original mapping: {attached}")
        assert_true(not read_raw_events(state_root_path), f"{label}: blocked reconnect should not log success")
    print(f"  PASS  {label}")


def scenario_hook_cached_prior_unknown_does_not_reconnect(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        write_identity_fixture(state_root_path, pane="%58")
        prior = identity_live_record(pane="%59", pid=9510, start_time="1011")
        write_live_identity_records(prior, index_record=prior)
        old_probe = bridge_identity.probe_agent_process
        old_target = bridge_identity.tmux_target_for_pane
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: {"status": "unknown", "reason": "ps_unavailable", "pane": pane, "agent": agent, "processes": []}  # type: ignore[assignment]
        bridge_identity.tmux_target_for_pane = lambda pane: "tmux:1.0"  # type: ignore[assignment]
        try:
            mapping = bridge_identity.update_live_session(
                agent_type="codex",
                session_id="sess-a",
                pane="%59",
                bridge_session="test-session",
                alias="codex",
                event="prompt_submitted",
            )
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
            bridge_identity.tmux_target_for_pane = old_target  # type: ignore[assignment]
        registry = read_json(Path(os.environ["AGENT_BRIDGE_ATTACH_REGISTRY"]), {"sessions": {}})
        attached = (registry.get("sessions") or {}).get("codex:sess-a") or {}
        live = read_json(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"panes": {}})
        candidate = ((live.get("panes") or {}).get("%59") or {})
        assert_true(mapping is None, f"{label}: cached prior plus fresh unknown must not reconnect: {mapping}")
        assert_true(attached.get("pane") == "%58", f"{label}: attached mapping must stay on old pane: {attached}")
        assert_true((candidate.get("process_identity_diagnostics") or {}).get("reason") == "ps_unavailable", f"{label}: unknown diagnostics should be retained: {candidate}")
        assert_true(not read_raw_events(state_root_path), f"{label}: blocked reconnect should not log success")
    print(f"  PASS  {label}")


def scenario_resolver_reconnects_to_alternate_verified_live_record(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = write_identity_fixture(state_root_path, pane="%46")
        new_live = identity_live_record(pane="%47", pid=9502, start_time="1003", last_seen_at="2026-01-03T00:00:00Z")
        write_live_identity_records(new_live, index_record=new_live)
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: dict(stored_identity or verified_identity(agent, pane))  # type: ignore[assignment]
        try:
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        assert_true(detail.get("ok") and detail.get("pane") == "%47", f"{label}: resolver should recover to alternate verified pane: {detail}")
        registry = read_json(Path(os.environ["AGENT_BRIDGE_ATTACH_REGISTRY"]), {"sessions": {}})
        assert_true(((registry.get("sessions") or {}).get("codex:sess-a") or {}).get("pane") == "%47", f"{label}: registry not migrated: {registry}")
        state = read_json(state_root_path / "test-session" / "session.json", {})
        assert_true((((state.get("participants") or {}).get("codex") or {}).get("pane")) == "%47", f"{label}: participant pane not refreshed: {state}")
    print(f"  PASS  {label}")


def scenario_resolver_candidate_unknown_on_final_probe_does_not_reconnect(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = write_identity_fixture(state_root_path, pane="%60")
        candidate = identity_live_record(pane="%61", pid=9511, start_time="1012", last_seen_at="2026-01-08T00:00:00Z")
        write_live_identity_records(candidate, index_record=candidate)
        calls: dict[str, int] = {"%61": 0}
        old_probe = bridge_identity.probe_agent_process

        def probe(pane, agent, stored_identity=None):
            if pane == "%61":
                calls["%61"] += 1
                if calls["%61"] == 1:
                    return dict(stored_identity or verified_identity(agent, pane))
                return {"status": "unknown", "reason": "ps_unavailable", "pane": pane, "agent": agent, "processes": []}
            return {"status": "unknown", "reason": "ps_unavailable", "pane": pane, "agent": agent, "processes": []}

        bridge_identity.probe_agent_process = probe  # type: ignore[assignment]
        try:
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant)
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        registry = read_json(Path(os.environ["AGENT_BRIDGE_ATTACH_REGISTRY"]), {"sessions": {}})
        attached = (registry.get("sessions") or {}).get("codex:sess-a") or {}
        assert_true(not detail.get("ok") and detail.get("reason") == "live_record_missing", f"{label}: final unknown probe should block reconnect: {detail}")
        assert_true(calls["%61"] >= 2, f"{label}: candidate should be re-probed before write: {calls}")
        assert_true(attached.get("pane") == "%60", f"{label}: attached mapping must stay on original pane: {attached}")
        assert_true(not read_raw_events(state_root_path), f"{label}: blocked reconnect should not log success")
    print(f"  PASS  {label}")


def scenario_resolver_read_reconnect_logs_distinct_reason(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        participant = write_identity_fixture(state_root_path, pane="%48")
        new_live = identity_live_record(pane="%49", pid=9503, start_time="1004", last_seen_at="2026-01-04T00:00:00Z")
        write_live_identity_records(new_live, index_record=new_live)
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: dict(stored_identity or verified_identity(agent, pane))  # type: ignore[assignment]
        try:
            detail = bridge_identity.resolve_participant_endpoint_detail("test-session", "codex", participant, purpose="read")
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        assert_true(detail.get("ok") and detail.get("pane") == "%49", f"{label}: read resolver should recover to alternate verified pane: {detail}")
        locks = read_json(Path(os.environ["AGENT_BRIDGE_PANE_LOCKS"]), {"panes": {}})
        assert_true("%48" in (locks.get("panes") or {}), f"{label}: read reconnect must not clear old lock: {locks}")
        reconnects = [event for event in read_raw_events(state_root_path) if event.get("event") == "endpoint_auto_reconnected"]
        assert_true(reconnects and reconnects[-1].get("reason") == "endpoint_auto_reconnected_via_read", f"{label}: read reconnect reason not distinct: {reconnects}")
    print(f"  PASS  {label}")


def scenario_session_end_replacement_uses_verified_candidate(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        write_identity_fixture(state_root_path, pane="%50")
        ended = identity_live_record(pane="%50", pid=9504, start_time="1005", last_seen_at="2026-01-05T00:00:00Z")
        stale_newer = identity_live_record(pane="%51", pid=9505, start_time="1006", last_seen_at="2026-01-07T00:00:00Z")
        verified_older = identity_live_record(pane="%52", pid=9506, start_time="1007", last_seen_at="2026-01-06T00:00:00Z")
        write_live_identity_records(ended, stale_newer, verified_older, index_record=ended)
        old_probe = bridge_identity.probe_agent_process
        def probe(pane, agent, stored_identity=None):
            if pane == "%51":
                result = dict(stored_identity or verified_identity(agent, pane))
                result["status"] = "mismatch"
                result["reason"] = "process_fingerprint_mismatch"
                return result
            return dict(stored_identity or verified_identity(agent, pane))
        bridge_identity.probe_agent_process = probe  # type: ignore[assignment]
        try:
            bridge_identity.update_live_session(agent_type="codex", session_id="sess-a", pane="%50", event="session_ended")
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        registry = read_json(Path(os.environ["AGENT_BRIDGE_ATTACH_REGISTRY"]), {"sessions": {}})
        attached = (registry.get("sessions") or {}).get("codex:sess-a") or {}
        live = read_json(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"sessions": {}})
        assert_true(attached.get("pane") == "%52", f"{label}: SessionEnd should not choose newer unverified stale pane: {attached}")
        assert_true(((live.get("sessions") or {}).get("codex:sess-a") or {}).get("pane") == "%52", f"{label}: live index should choose verified replacement: {live}")
    print(f"  PASS  {label}")


def scenario_reconnect_rereads_mapping_before_write(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        write_identity_fixture(state_root_path, pane="%53")
        stale_mapping = bridge_identity.read_attached_mapping("codex", "sess-a") or {}
        candidate = identity_live_record(pane="%54", pid=9507, start_time="1008")
        newer = identity_live_record(pane="%55", pid=9508, start_time="1009")
        write_live_identity_records(candidate, newer, index_record=newer)
        bridge_identity.update_attached_endpoint(stale_mapping, "%55", "tmux:1.0")
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: dict(stored_identity or verified_identity(agent, pane))  # type: ignore[assignment]
        try:
            result = bridge_identity.auto_reconnect_attached_endpoint(
                stale_mapping,
                candidate,
                "mapped_endpoint_mismatch",
                old_pane="%53",
                old_status="mismatch",
            )
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        registry = read_json(Path(os.environ["AGENT_BRIDGE_ATTACH_REGISTRY"]), {"sessions": {}})
        attached = (registry.get("sessions") or {}).get("codex:sess-a") or {}
        assert_true(attached.get("pane") == "%55", f"{label}: stale reconnect must not overwrite newer mapping: {attached}")
        assert_true(result.get("pane") == "%55", f"{label}: helper should return current verified mapping: {result}")
    print(f"  PASS  {label}")


def scenario_caller_reconnects_from_resumed_pane(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        write_identity_fixture(state_root_path, pane="%56")
        new_live = identity_live_record(pane="%57", pid=9509, start_time="1010")
        write_live_identity_records(new_live, index_record=new_live)
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: dict(stored_identity or verified_identity(agent, pane))  # type: ignore[assignment]
        try:
            resolution = bridge_identity.resolve_caller_from_pane(pane="%57", tool_name="agent_send_peer")
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        assert_true(resolution.ok and resolution.alias == "codex", f"{label}: caller should reconnect resumed pane: {resolution}")
        registry = read_json(Path(os.environ["AGENT_BRIDGE_ATTACH_REGISTRY"]), {"sessions": {}})
        assert_true(((registry.get("sessions") or {}).get("codex:sess-a") or {}).get("pane") == "%57", f"{label}: caller reconnect did not persist mapping: {registry}")
    print(f"  PASS  {label}")


def scenario_no_probe_requires_verified_live_identity(label: str, tmpdir: Path) -> None:
    with isolated_identity_env(tmpdir) as state_root_path:
        write_identity_fixture(state_root_path, pane="%31")
        missing = bridge_identity.verify_existing_live_process_identity("codex", "sess-a", "%31")
        identity = verified_identity("codex", "%31", pid=9100, start_time="801")
        live = {
            "agent": "codex",
            "session_id": "sess-a",
            "pane": "%31",
            "target": "tmux:1.0",
            "bridge_session": "test-session",
            "alias": "codex",
            "last_seen_at": utc_now(),
            "process_identity": identity,
        }
        write_json_atomic(Path(os.environ["AGENT_BRIDGE_LIVE_SESSIONS"]), {"version": 1, "panes": {"%31": live}, "sessions": {"codex:sess-a": live}})
        old_probe = bridge_identity.probe_agent_process
        bridge_identity.probe_agent_process = lambda pane, agent, stored_identity=None: dict(identity)  # type: ignore[assignment]
        try:
            verified = bridge_identity.verify_existing_live_process_identity("codex", "sess-a", "%31")
        finally:
            bridge_identity.probe_agent_process = old_probe  # type: ignore[assignment]
        assert_true(missing.get("reason") == "live_record_missing", f"{label}: no-probe must require live hook record before publish: {missing}")
        assert_true(verified.get("status") == "verified", f"{label}: verified live identity accepted: {verified}")
    print(f"  PASS  {label}")


def scenario_daemon_undeliverable_request_returns_result(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%1", "hook_session_id": "sess-alice"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%2", "hook_session_id": "sess-bob"},
    }
    d = make_daemon(tmpdir, participants)
    d.dry_run = False
    d.busy["alice"] = True
    d.resolve_endpoint_detail = lambda target, purpose="write": {"ok": False, "pane": "", "reason": "process_mismatch", "probe_status": "mismatch", "detail": "gone", "should_detach": True}  # type: ignore[method-assign]
    calls: list[str] = []
    old_literal = bridge_daemon.run_tmux_send_literal
    bridge_daemon.run_tmux_send_literal = lambda pane, prompt, **kwargs: calls.append(pane)  # type: ignore[assignment]
    try:
        msg = test_message("msg-undeliverable", frm="alice", to="bob", status="pending")
        d.queue.update(lambda queue: queue.append(msg))
        d.try_deliver("bob")
    finally:
        bridge_daemon.run_tmux_send_literal = old_literal  # type: ignore[assignment]
    queue = d.queue.read()
    assert_true(calls == [], f"{label}: must not paste into stale pane")
    assert_true(not any(item.get("id") == "msg-undeliverable" for item in queue), f"{label}: original removed")
    result = next((item for item in queue if item.get("to") == "alice" and item.get("kind") == "result"), None)
    body = str((result or {}).get("body") or "")
    assert_true(result is not None and "[bridge:undeliverable]" in body, f"{label}: sender gets undeliverable result")
    assert_true("If the agent was resumed in another pane" in body, f"{label}: resume guidance included")
    assert_true("submit any short prompt" in body and "bridge hook can re-attach" in body, f"{label}: hook reattach guidance included")
    print(f"  PASS  {label}")


def scenario_interrupt_endpoint_lost_finalizes_delivered_non_aggregate(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%1", "hook_session_id": "sess-alice"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%2", "hook_session_id": "sess-bob"},
    }
    d = make_daemon(tmpdir, participants)
    d.dry_run = False
    d.busy["alice"] = True
    d.probe_endpoint_detail_from_snapshot = lambda target, _participant, purpose="write": {"ok": False, "pane": "", "reason": "process_mismatch", "probe_status": "mismatch", "detail": "gone", "should_detach": True}  # type: ignore[method-assign]
    msg = test_message("msg-delivered-lost", frm="alice", to="bob", status="delivered")
    d.queue.update(lambda queue: queue.append(msg))
    d.current_prompt_by_agent["bob"] = {"id": "msg-delivered-lost", "from": "alice", "auto_return": True}
    result = d.handle_interrupt(sender="alice", target="bob")
    queue = d.queue.read()
    assert_true(not result.get("held"), f"{label}: endpoint loss should not enter hold")
    assert_true(not any(item.get("id") == "msg-delivered-lost" for item in queue), f"{label}: delivered original removed")
    assert_true(any(item.get("kind") == "result" and item.get("to") == "alice" for item in queue), f"{label}: non-aggregate sender gets result")
    print(f"  PASS  {label}")


def scenario_interrupt_endpoint_lost_finalizes_delivered_aggregate(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%1", "hook_session_id": "sess-alice"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%2", "hook_session_id": "sess-bob"},
    }
    d = make_daemon(tmpdir, participants)
    d.dry_run = False
    d.busy["alice"] = True
    d.probe_endpoint_detail_from_snapshot = lambda target, _participant, purpose="write": {"ok": False, "pane": "", "reason": "process_mismatch", "probe_status": "mismatch", "detail": "gone", "should_detach": True}  # type: ignore[method-assign]
    msg = test_message("msg-agg-lost", frm="alice", to="bob", status="delivered")
    msg["aggregate_id"] = "agg-lost"
    msg["aggregate_expected"] = ["bob"]
    msg["aggregate_message_ids"] = {"bob": "msg-agg-lost"}
    d.queue.update(lambda queue: queue.append(msg))
    d.current_prompt_by_agent["bob"] = {"id": "msg-agg-lost", "from": "alice", "auto_return": True, "aggregate_id": "agg-lost"}
    d.handle_interrupt(sender="alice", target="bob")
    aggregate = (read_json(d.aggregate_file, {"aggregates": {}}).get("aggregates") or {}).get("agg-lost") or {}
    reply = ((aggregate.get("replies") or {}).get("bob") or {}).get("body") or ""
    assert_true("[bridge:undeliverable]" in reply, f"{label}: aggregate gets synthetic undeliverable reply")
    print(f"  PASS  {label}")


def scenario_retry_enter_endpoint_lost_does_not_press_enter(label: str, tmpdir: Path) -> None:
    participants = {
        "alice": {"alias": "alice", "agent_type": "claude", "pane": "%1", "hook_session_id": "sess-alice"},
        "bob": {"alias": "bob", "agent_type": "codex", "pane": "%2", "hook_session_id": "sess-bob"},
    }
    d = make_daemon(tmpdir, participants)
    d.dry_run = False
    d.resolve_endpoint_detail = lambda target, purpose="write": {"ok": False, "pane": "", "reason": "process_mismatch", "probe_status": "mismatch", "detail": "gone", "should_detach": True}  # type: ignore[method-assign]
    msg = test_message("msg-enter-lost", frm="alice", to="bob", status="inflight")
    d.queue.update(lambda queue: queue.append(msg))
    d.last_enter_ts["msg-enter-lost"] = time.time() - 2.0
    enter_calls: list[str] = []
    old_enter = bridge_daemon.run_tmux_enter
    bridge_daemon.run_tmux_enter = lambda pane: enter_calls.append(pane)  # type: ignore[assignment]
    try:
        d.retry_enter_for_inflight()
    finally:
        bridge_daemon.run_tmux_enter = old_enter  # type: ignore[assignment]
    assert_true(enter_calls == [], f"{label}: retry must not press Enter into stale pane")
    assert_true(not any(item.get("id") == "msg-enter-lost" for item in d.queue.read()), f"{label}: inflight removed as undeliverable")
    print(f"  PASS  {label}")


def scenario_direct_notices_suppress_unverified_endpoint(label: str, tmpdir: Path) -> None:
    import bridge_daemon_ctl
    import bridge_leave

    record = {"alias": "bob", "agent_type": "codex", "pane": "%2", "hook_session_id": "sess-bob"}
    calls: list[tuple[str, str]] = []
    old_leave_resolve = bridge_leave.resolve_participant_endpoint_detail
    old_leave_send = bridge_leave.tmux_send_literal
    old_ctl_resolve = bridge_daemon_ctl.resolve_participant_endpoint_detail
    old_ctl_send = bridge_daemon_ctl.tmux_send_literal
    bridge_leave.resolve_participant_endpoint_detail = lambda *args, **kwargs: {"ok": False, "reason": "process_mismatch"}  # type: ignore[assignment]
    bridge_leave.tmux_send_literal = lambda pane, text: calls.append(("leave", pane))  # type: ignore[assignment]
    bridge_daemon_ctl.resolve_participant_endpoint_detail = lambda *args, **kwargs: {"ok": False, "reason": "process_mismatch"}  # type: ignore[assignment]
    bridge_daemon_ctl.tmux_send_literal = lambda pane, text: calls.append(("close", pane))  # type: ignore[assignment]
    try:
        leave_result = bridge_leave.send_leave_notice("test-session", "bob", record)
        with isolated_identity_env(tmpdir) as state_root_path:
            write_identity_fixture(state_root_path, alias="bob", pane="%2", session_id="sess-bob")
            close_result = bridge_daemon_ctl.send_room_closed_notices("test-session")
    finally:
        bridge_leave.resolve_participant_endpoint_detail = old_leave_resolve  # type: ignore[assignment]
        bridge_leave.tmux_send_literal = old_leave_send  # type: ignore[assignment]
        bridge_daemon_ctl.resolve_participant_endpoint_detail = old_ctl_resolve  # type: ignore[assignment]
        bridge_daemon_ctl.tmux_send_literal = old_ctl_send  # type: ignore[assignment]
    assert_true(leave_result.get("sent") == 0 and close_result.get("sent") == 0, f"{label}: notices suppressed")
    assert_true(calls == [], f"{label}: no direct tmux send for unverified endpoint")
    print(f"  PASS  {label}")


def scenario_daemon_startup_backfill_summary_logs_repair_hint(label: str, tmpdir: Path) -> None:
    participants = {"alice": {"alias": "alice", "pane": "%1"}, "bob": {"alias": "bob", "pane": "%2"}}
    d = make_daemon(tmpdir, participants)
    d.startup_backfill_summary = {"alice": {"status": "unknown", "reason": "ps_unavailable"}}
    d.follow()
    events = read_events(tmpdir / "events.raw.jsonl")
    event = next((item for item in events if item.get("event") == "endpoint_backfill_summary"), None)
    assert_true(event is not None, f"{label}: startup backfill summary event missing")
    assert_true("bridge_healthcheck.sh --backfill-endpoints" in str(event.get("repair_hint") or ""), f"{label}: repair hint missing")
    print(f"  PASS  {label}")


SCENARIOS = [
    ('endpoint_rejects_stale_pane_lock_without_live', scenario_endpoint_rejects_stale_pane_lock_without_live),
    ('endpoint_rejects_same_pane_new_live_identity', scenario_endpoint_rejects_same_pane_new_live_identity),
    ('endpoint_probe_unknown_does_not_mutate', scenario_endpoint_probe_unknown_does_not_mutate),
    ('endpoint_accepts_matching_process_fingerprint', scenario_endpoint_accepts_matching_process_fingerprint),
    ('backfill_refuses_to_mint_without_live_record', scenario_backfill_refuses_to_mint_without_live_record),
    ('backfill_refuses_other_live_identity', scenario_backfill_refuses_other_live_identity),
    ('backfill_rejects_changed_process_fingerprint', scenario_backfill_rejects_changed_process_fingerprint),
    ('backfill_allows_fresh_hook_proof_create', scenario_backfill_allows_fresh_hook_proof_create),
    ('backfill_fresh_probe_repairs_unscoped_live_mismatch', scenario_backfill_fresh_probe_repairs_unscoped_live_mismatch),
    ('unscoped_hook_canonicalizes_via_pane_lock', scenario_unscoped_hook_canonicalizes_via_pane_lock),
    ('unscoped_hook_canonicalizes_during_attach_gap', scenario_unscoped_hook_canonicalizes_during_attach_gap),
    ('unscoped_hook_canonicalizes_via_attached_registry', scenario_unscoped_hook_canonicalizes_via_attached_registry),
    ('unscoped_hook_new_process_fails_closed', scenario_unscoped_hook_new_process_fails_closed),
    ('unscoped_hook_cross_reboot_fails_closed', scenario_unscoped_hook_cross_reboot_fails_closed),
    ('unscoped_hook_missing_prior_fingerprint_fails_closed', scenario_unscoped_hook_missing_prior_fingerprint_fails_closed),
    ('unscoped_hook_different_agent_fails_closed', scenario_unscoped_hook_different_agent_fails_closed),
    ('unscoped_hook_same_session_no_canonicalization_event', scenario_unscoped_hook_same_session_no_canonicalization_event),
    ('scoped_different_session_no_canonicalization', scenario_scoped_different_session_no_canonicalization),
    ('session_ended_payload_mismatch_no_canonicalization', scenario_session_ended_payload_mismatch_no_canonicalization),
    ('resolver_reconnects_exact_mismatch_shape', scenario_resolver_reconnects_exact_mismatch_shape),
    ('hook_reconnects_exact_mismatch_shape', scenario_hook_reconnects_exact_mismatch_shape),
    ('cross_pane_candidate_mismatch_blocks_reconnect', scenario_cross_pane_candidate_mismatch_blocks_reconnect),
    ('codex1_incident_replay_canonicalizes_repeated_unscoped_hooks', scenario_codex1_incident_replay_canonicalizes_repeated_unscoped_hooks),
    ('target_recovery_reconnects_stale_pane_by_codex_transcript', scenario_target_recovery_reconnects_stale_pane_by_codex_transcript),
    ('target_recovery_blocks_transcript_session_mismatch', scenario_target_recovery_blocks_transcript_session_mismatch),
    ('target_recovery_blocks_missing_transcript', scenario_target_recovery_blocks_missing_transcript),
    ('target_recovery_blocks_wrong_pid_transcript', scenario_target_recovery_blocks_wrong_pid_transcript),
    ('target_recovery_blocks_different_agent_at_target', scenario_target_recovery_blocks_different_agent_at_target),
    ('target_recovery_blocks_same_pane_target', scenario_target_recovery_blocks_same_pane_target),
    ('target_recovery_blocks_unresolvable_target', scenario_target_recovery_blocks_unresolvable_target),
    ('target_recovery_skips_without_participant_target', scenario_target_recovery_skips_without_participant_target),
    ('target_recovery_read_purpose_does_not_mutate', scenario_target_recovery_read_purpose_does_not_mutate),
    ('target_recovery_env_disable_blocks', scenario_target_recovery_env_disable_blocks),
    ('tmux_display_pane_empty_metadata_is_unavailable', scenario_tmux_display_pane_empty_metadata_is_unavailable),
    ('codex_rollout_path_regex_is_strict', scenario_codex_rollout_path_regex_is_strict),
    ('target_recovery_only_matching_alias_recovers', scenario_target_recovery_only_matching_alias_recovers),
    ('hook_unknown_preserves_verified_process_identity', scenario_hook_unknown_preserves_verified_process_identity),
    ('probe_tmux_access_failure_unknown', scenario_probe_tmux_access_failure_unknown),
    ('endpoint_read_mismatch_does_not_mutate', scenario_endpoint_read_mismatch_does_not_mutate),
    ('verified_candidate_ordering_prefers_pane_then_newest', scenario_verified_candidate_ordering_prefers_pane_then_newest),
    ('resume_new_pane_reconnects_unknown_old_and_logs', scenario_resume_new_pane_reconnects_unknown_old_and_logs),
    ('resume_unknown_old_opt_out_blocks_switch', scenario_resume_unknown_old_opt_out_blocks_switch),
    ('hook_cached_prior_unknown_does_not_reconnect', scenario_hook_cached_prior_unknown_does_not_reconnect),
    ('resolver_reconnects_to_alternate_verified_live_record', scenario_resolver_reconnects_to_alternate_verified_live_record),
    ('resolver_candidate_unknown_on_final_probe_does_not_reconnect', scenario_resolver_candidate_unknown_on_final_probe_does_not_reconnect),
    ('resolver_read_reconnect_logs_distinct_reason', scenario_resolver_read_reconnect_logs_distinct_reason),
    ('session_end_replacement_uses_verified_candidate', scenario_session_end_replacement_uses_verified_candidate),
    ('reconnect_rereads_mapping_before_write', scenario_reconnect_rereads_mapping_before_write),
    ('caller_reconnects_from_resumed_pane', scenario_caller_reconnects_from_resumed_pane),
    ('no_probe_requires_verified_live_identity', scenario_no_probe_requires_verified_live_identity),
    ('daemon_undeliverable_request_returns_result', scenario_daemon_undeliverable_request_returns_result),
    ('interrupt_endpoint_lost_finalizes_delivered_non_aggregate', scenario_interrupt_endpoint_lost_finalizes_delivered_non_aggregate),
    ('interrupt_endpoint_lost_finalizes_delivered_aggregate', scenario_interrupt_endpoint_lost_finalizes_delivered_aggregate),
    ('retry_enter_endpoint_lost_does_not_press_enter', scenario_retry_enter_endpoint_lost_does_not_press_enter),
    ('direct_notices_suppress_unverified_endpoint', scenario_direct_notices_suppress_unverified_endpoint),
    ('daemon_startup_backfill_summary_logs_repair_hint', scenario_daemon_startup_backfill_summary_logs_repair_hint),
]
