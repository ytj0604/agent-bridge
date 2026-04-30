from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

from .harness import (
    LIBEXEC,
    ROOT,
    assert_true,
)

from bridge_util import read_json, write_json_atomic  # noqa: E402


def _model_state(aliases_with_extras: dict) -> dict:
    """Build a state with full participant records (including pane/target/hook_session_id)."""
    return {
        "session": "test-session",
        "participants": {
            alias: {
                "alias": alias,
                "agent_type": rec.get("agent_type", "codex"),
                "pane": rec.get("pane", "%99"),
                "target": rec.get("target", "0:1.99"),
                "hook_session_id": rec.get("hook_session_id", "uuid-secret"),
                "model": rec.get("model", "gpt-test"),
                "cwd": rec.get("cwd", "/tmp/x"),
                "status": "active",
            }
            for alias, rec in aliases_with_extras.items()
        },
        "hook_session_ids": {alias: "uuid-secret" for alias in aliases_with_extras},
    }


def scenario_format_peer_list_model_safe_default(label: str, tmpdir: Path) -> None:
    libexec = LIBEXEC
    if str(libexec) not in sys.path:
        sys.path.insert(0, str(libexec))
    import importlib
    bp = importlib.import_module("bridge_participants")
    importlib.reload(bp)
    state = _model_state({"codex1": {}, "codex2": {}})
    out = bp.format_peer_list(state, "codex1")
    assert_true("pane=" not in out, f"{label}: text mode default must NOT include pane=, got: {out!r}")
    assert_true("target=" not in out, f"{label}: text mode default must NOT include target=, got: {out!r}")
    assert_true("hook_session_id" not in out, f"{label}: never expose hook_session_id")
    # Should still include type, model, cwd
    assert_true("type=" in out, f"{label}: type still present")
    assert_true("model=" in out, f"{label}: model still present")
    assert_true("cwd=" in out, f"{label}: cwd still present (operator confirmed)")
    print(f"  PASS  {label}")


def scenario_format_peer_list_full_includes_operator_fields(label: str, tmpdir: Path) -> None:
    libexec = LIBEXEC
    if str(libexec) not in sys.path:
        sys.path.insert(0, str(libexec))
    import importlib
    bp = importlib.import_module("bridge_participants")
    importlib.reload(bp)
    state = _model_state({"codex1": {}})
    out = bp.format_peer_list(state, "codex1", full=True)
    assert_true("pane=" in out, f"{label}: full mode includes pane=, got: {out!r}")
    assert_true("target=" in out, f"{label}: full mode includes target=")
    print(f"  PASS  {label}")


def scenario_bridge_manage_summary_concise(label: str, tmpdir: Path) -> None:
    import importlib
    bms = importlib.import_module("bridge_manage_summary")
    importlib.reload(bms)
    state = {
        "session": "test-session",
        "participants": {
            "z-codex": {
                "alias": "z-codex",
                "agent_type": "codex",
                "pane": "%9",
                "target": "0:1.9",
                "status": "active",
                "model": "gpt-test",
            },
            "a-claude": {
                "alias": "a-claude",
                "agent_type": "claude",
                "pane": "%1",
                "target": "0:1.1",
                "status": "active",
                "model": "",
            },
            "inactive": {
                "alias": "inactive",
                "agent_type": "codex",
                "pane": "%8",
                "target": "0:1.8",
                "status": "left",
            },
        },
    }
    out1 = bms.format_room_summary(state)
    out2 = bms.format_room_summary(state)
    assert_true(out1 == out2, f"{label}: output must be deterministic")
    lines = out1.splitlines()
    assert_true(lines[0] == "Agents:", f"{label}: starts with Agents:, got {out1!r}")
    assert_true(lines[1].startswith("- a-claude claude active target=0:1.1 pane=%1"), f"{label}: sorted a-claude first: {out1!r}")
    assert_true(lines[2].startswith("- z-codex codex active target=0:1.9 pane=%9 model=gpt-test"), f"{label}: z-codex fields/model: {out1!r}")
    assert_true("inactive" not in out1, f"{label}: inactive participants omitted: {out1!r}")
    assert_true("model=" not in lines[1], f"{label}: empty model omitted: {lines[1]!r}")
    forbidden = ("agent_send_peer", "Commands:", "Kinds and routing contract", "Reply normally")
    for needle in forbidden:
        assert_true(needle not in out1, f"{label}: summary must not include cheat sheet text {needle!r}")
    print(f"  PASS  {label}")


def scenario_bridge_manage_summary_defaults(label: str, tmpdir: Path) -> None:
    import importlib
    bms = importlib.import_module("bridge_manage_summary")
    importlib.reload(bms)
    state = {
        "session": "test-session",
        "participants": {
            "loose": {
                "alias": "loose",
                "agent_type": "",
                "pane": "",
                "target": "",
                "status": "",
            }
        },
    }
    out = bms.format_room_summary(state)
    assert_true("- loose unknown unknown target=? pane=?" in out, f"{label}: missing fields use stable defaults: {out!r}")
    assert_true("model=" not in out, f"{label}: missing model omitted: {out!r}")
    print(f"  PASS  {label}")


def scenario_bridge_manage_summary_legacy_state_fallback(label: str, tmpdir: Path) -> None:
    import importlib
    bms = importlib.import_module("bridge_manage_summary")
    importlib.reload(bms)
    state = {
        "session": "test-session",
        "panes": {"claude": "%1", "codex": "%2"},
        "targets": {"claude": "0:1.1", "codex": "0:1.2"},
    }
    out = bms.format_room_summary(state)
    assert_true("- claude claude active target=0:1.1 pane=%1" in out, f"{label}: legacy claude rendered: {out!r}")
    assert_true("- codex codex active target=0:1.2 pane=%2" in out, f"{label}: legacy codex rendered: {out!r}")
    print(f"  PASS  {label}")


def scenario_bridge_manage_summary_missing_session_exits(label: str, tmpdir: Path) -> None:
    script = ROOT / "libexec" / "agent-bridge" / "bridge_manage_summary.py"
    env = os.environ.copy()
    env["AGENT_BRIDGE_STATE_DIR"] = str(tmpdir / "state")
    proc = subprocess.run(
        [sys.executable, str(script), "--session", "missing-room"],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert_true(proc.returncode == 2, f"{label}: missing session should exit 2, got {proc.returncode}")
    assert_true(proc.stdout == "", f"{label}: missing session should not print summary: {proc.stdout!r}")
    assert_true("not active or was stopped" in proc.stderr, f"{label}: stderr should explain missing room: {proc.stderr!r}")
    print(f"  PASS  {label}")


def scenario_model_safe_participants_uses_active_only(label: str, tmpdir: Path) -> None:
    """JSON view should match text view: only active participants."""
    libexec = LIBEXEC
    if str(libexec) not in sys.path:
        sys.path.insert(0, str(libexec))
    import importlib
    bp = importlib.import_module("bridge_participants")
    importlib.reload(bp)
    state = {
        "session": "test-session",
        "participants": {
            "codex1": {"alias": "codex1", "agent_type": "codex", "pane": "%0", "model": "m", "cwd": "/x", "status": "active"},
            "stale": {"alias": "stale", "agent_type": "codex", "pane": "%99", "model": "m", "cwd": "/y", "status": "left"},
        },
    }
    safe = bp.model_safe_participants(state)
    assert_true("codex1" in safe, f"{label}: active codex1 included")
    assert_true("stale" not in safe, f"{label}: inactive 'stale' must NOT be exposed: {safe}")
    print(f"  PASS  {label}")


def scenario_list_peers_json_daemon_status_strips_pid(label: str, tmpdir: Path) -> None:
    """Default JSON output's daemon_status must not contain pid; --full does."""
    helper = str(LIBEXEC / "bridge_list_peers.py")
    # Use existing live session to drive the CLI
    proc = subprocess.run(
        [sys.executable, helper, "--session", "agent-bridge-auto", "--json"],
        capture_output=True, text=True, timeout=10,
    )
    if proc.returncode != 0:
        # If live session not present (CI or fresh checkout), skip silently
        print(f"  SKIP  {label}: no live session ({proc.stderr.strip()[:60]})")
        return
    data = json.loads(proc.stdout)
    ds = data.get("daemon_status") or {}
    assert_true("pid" not in ds, f"{label}: default JSON daemon_status must NOT include pid, got {ds}")
    proc_full = subprocess.run(
        [sys.executable, helper, "--session", "agent-bridge-auto", "--json", "--full"],
        capture_output=True, text=True, timeout=10,
    )
    data_full = json.loads(proc_full.stdout)
    ds_full = data_full.get("daemon_status") or {}
    # pid is in the full view (may be None when not available, but the key should be present)
    assert_true("pid" in ds_full, f"{label}: --full JSON daemon_status must include pid key: {ds_full}")
    print(f"  PASS  {label}")


def scenario_model_safe_participants_strips_endpoints(label: str, tmpdir: Path) -> None:
    libexec = LIBEXEC
    if str(libexec) not in sys.path:
        sys.path.insert(0, str(libexec))
    import importlib
    bp = importlib.import_module("bridge_participants")
    importlib.reload(bp)
    state = _model_state({"codex1": {}, "codex2": {}})
    safe = bp.model_safe_participants(state)
    for alias, record in safe.items():
        assert_true("pane" not in record, f"{label}: {alias} record must not include pane")
        assert_true("target" not in record, f"{label}: {alias} record must not include target")
        assert_true("hook_session_id" not in record, f"{label}: {alias} record must not include hook_session_id")
        assert_true(record.get("agent_type"), f"{label}: agent_type retained")
        assert_true(record.get("cwd"), f"{label}: cwd retained")
    print(f"  PASS  {label}")


def _import_view_peer():
    import importlib
    bv = importlib.import_module("bridge_view_peer")
    return importlib.reload(bv)


class ViewPeerValidationSentinel(RuntimeError):
    pass


def _run_view_peer_main_with_sentinels(bv, argv: list[str]) -> tuple[str, bool]:
    old_argv = sys.argv[:]
    old_validate_caller = bv.validate_caller
    old_handlers = {
        "handle_live": bv.handle_live,
        "handle_onboard": bv.handle_onboard,
        "handle_older": bv.handle_older,
        "handle_since_last": bv.handle_since_last,
        "handle_search": bv.handle_search,
    }
    reached_validation = False

    def sentinel_validate(_args):
        nonlocal reached_validation
        reached_validation = True
        raise ViewPeerValidationSentinel("validate_caller reached")

    def handler_sentinel(*_args, **_kwargs):
        raise ViewPeerValidationSentinel("handler reached")

    try:
        sys.argv = ["agent_view_peer", *argv]
        bv.validate_caller = sentinel_validate
        for name in old_handlers:
            setattr(bv, name, handler_sentinel)
        try:
            bv.main()
        except SystemExit as exc:
            return str(exc), reached_validation
        except ViewPeerValidationSentinel as exc:
            return str(exc), reached_validation
        raise AssertionError(f"agent_view_peer main returned normally for argv {argv!r}")
    finally:
        sys.argv = old_argv
        bv.validate_caller = old_validate_caller
        for name, value in old_handlers.items():
            setattr(bv, name, value)


def scenario_view_peer_rejects_snapshot_without_snapshot_mode(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    cases = [
        ["codex1", "--snapshot", "snap-X"],
        ["codex1", "--onboard", "--snapshot", "snap-X"],
        ["codex1", "--since-last", "--snapshot", "snap-X"],
        ["codex1", "--snapshot", ""],
        ["codex1", "--since-last", "--snapshot", "snap-X", "--page", "1"],
    ]
    for idx, argv in enumerate(cases):
        msg, reached_validation = _run_view_peer_main_with_sentinels(bv, argv)
        assert_true("--snapshot is only valid with --older or --search" in msg, f"{label}:{idx}: targeted snapshot error expected for {argv!r}: {msg!r}")
        assert_true(not reached_validation, f"{label}:{idx}: validation/session lookup must not run for invalid snapshot argv {argv!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_rejects_page_with_since_last_or_search(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    cases = [
        ["codex1", "--since-last", "--page", "0"],
        ["codex1", "--since-last", "--page", "1"],
        ["codex1", "--search", "needle", "--page", "0"],
        ["codex1", "--search", "needle", "--page", "1"],
        ["codex1", "--search", "needle", "--snapshot", "snap-X", "--page", "1"],
    ]
    for idx, argv in enumerate(cases):
        msg, reached_validation = _run_view_peer_main_with_sentinels(bv, argv)
        assert_true("--page is only valid with live view, --onboard, or --older" in msg, f"{label}:{idx}: targeted page error expected for {argv!r}: {msg!r}")
        assert_true(not reached_validation, f"{label}:{idx}: validation/session lookup must not run for invalid page argv {argv!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_allows_page_in_live_onboard_older_and_snapshot_in_older_search(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    cases = [
        ["codex1", "--page", "1"],
        ["codex1", "--onboard", "--page", "1"],
        ["codex1", "--older", "--page", "1"],
        ["codex1", "--older", "--snapshot", "snap-X"],
        ["codex1", "--search", "needle", "--snapshot", "snap-X"],
    ]
    for idx, argv in enumerate(cases):
        msg, reached_validation = _run_view_peer_main_with_sentinels(bv, argv)
        assert_true(msg == "validate_caller reached", f"{label}:{idx}: valid flag shape should reach validation sentinel for {argv!r}, got {msg!r}")
        assert_true(reached_validation, f"{label}:{idx}: validation sentinel should be reached for valid flag shape {argv!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_rejects_empty_search_before_session_lookup(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    cases = [
        ["codex1", "--search", ""],
        ["codex1", "--search", "   "],
        ["codex1", "--search", "\t\n"],
    ]
    for idx, argv in enumerate(cases):
        msg, reached_validation = _run_view_peer_main_with_sentinels(bv, argv)
        assert_true("--search query must be non-empty" in msg, f"{label}:{idx}: targeted empty-search error expected for {argv!r}: {msg!r}")
        assert_true(not reached_validation, f"{label}:{idx}: validation/session lookup must not run for empty-search argv {argv!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_empty_search_counts_as_search_for_mode_errors(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    cases = [
        (["codex1", "--onboard", "--search", ""], "choose only one of --onboard, --older, --since-last, or --search"),
        (["codex1", "--live"], "--live is only valid with --search"),
        (["codex1", "--live", "--search", ""], "--search query must be non-empty"),
        (["codex1", "--search", "", "--page", "0"], "--search query must be non-empty"),
    ]
    for idx, (argv, expected) in enumerate(cases):
        msg, reached_validation = _run_view_peer_main_with_sentinels(bv, argv)
        assert_true(expected in msg, f"{label}:{idx}: expected {expected!r} for {argv!r}, got {msg!r}")
        assert_true(not reached_validation, f"{label}:{idx}: validation/session lookup must not run for invalid search-shape argv {argv!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_nonempty_search_still_reaches_validation(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    msg, reached_validation = _run_view_peer_main_with_sentinels(bv, ["codex1", "--search", " needle "])
    assert_true(msg == "validate_caller reached", f"{label}: non-empty query with surrounding spaces should reach validation sentinel, got {msg!r}")
    assert_true(reached_validation, f"{label}: validation sentinel should be reached for non-empty query")
    print(f"  PASS  {label}")


def scenario_view_peer_search_with_page_after_a19_reports_page_error(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    cases = [
        ["codex1", "--search", " needle ", "--page", "0"],
        ["codex1", "--search", "needle", "--page", "0"],
    ]
    for idx, argv in enumerate(cases):
        msg, reached_validation = _run_view_peer_main_with_sentinels(bv, argv)
        assert_true("--page is only valid with live view, --onboard, or --older" in msg, f"{label}:{idx}: non-empty search with page should report A18 page error for {argv!r}: {msg!r}")
        assert_true(not reached_validation, f"{label}:{idx}: validation/session lookup must not run for invalid search+page argv {argv!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_render_output_model_safe(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    import contextlib
    import io
    full_snapshot_id = "20260425T000000Z-abcdef12"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        bv.render_output(
            room="room-secret",
            caller="viewer-secret",
            target="codex1",
            target_record={"agent_type": "codex", "pane": "%99"},
            mode="onboard",
            lines=["hello"],
            total_lines=1,
            max_chars=12000,
            snapshot_id=full_snapshot_id,
            page=2,
            confidence="high",
        )
    out = buf.getvalue()
    assert_true("Peer view: codex1 (codex)" in out, f"{label}: header keeps alias/type: {out!r}")
    for forbidden in ("pane=", "%99", "room-secret", "viewer=", "viewer-secret", full_snapshot_id):
        assert_true(forbidden not in out, f"{label}: output must not expose {forbidden!r}: {out!r}")
    assert_true("snapshot=cdef12" in out, f"{label}: short snapshot ref retained: {out!r}")
    assert_true("page=2" in out and "confidence=high" in out, f"{label}: public paging fields retained: {out!r}")
    assert_true("Next: agent_view_peer codex1 --older" in out, f"{label}: next hint retained: {out!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_search_explicit_snapshot_uses_safe_ref(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    import contextlib
    import io
    saved = os.environ.get("AGENT_BRIDGE_STATE_DIR")
    os.environ["AGENT_BRIDGE_STATE_DIR"] = str(tmpdir / "state")
    try:
        full_snapshot_id = "20260425T000000Z-abcdef12"
        text_path, meta_path = bv.snapshot_paths("test-session", "codex1", full_snapshot_id)
        text_path.parent.mkdir(parents=True, exist_ok=True)
        text_path.write_text("alpha\nneedle\nomega\n", encoding="utf-8")
        meta_path.write_text(json.dumps({"snapshot_id": full_snapshot_id, "created_at": bv.utc_now()}), encoding="utf-8")
        args = argparse.Namespace(live=False, snapshot="cdef12", search="needle", context=0, raw=False, capture_file=None)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            bv.handle_search(
                args,
                "test-session",
                "viewer",
                "codex1",
                {},
                {"agent_type": "codex", "pane": "%99"},
                20,
                12000,
            )
        out = buf.getvalue()
        assert_true(full_snapshot_id not in out, f"{label}: full snapshot id must stay hidden: {out!r}")
        assert_true("source=saved snapshot cdef12" in out, f"{label}: search source uses short ref: {out!r}")
        assert_true("source=snapshot=" not in out, f"{label}: search note must not expose raw snapshot source: {out!r}")
        assert_true("needle" in out, f"{label}: match content shown: {out!r}")
    finally:
        if saved is None:
            os.environ.pop("AGENT_BRIDGE_STATE_DIR", None)
        else:
            os.environ["AGENT_BRIDGE_STATE_DIR"] = saved
    print(f"  PASS  {label}")


def scenario_view_peer_snapshot_ref_collision_unique(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    import contextlib
    import io
    saved = os.environ.get("AGENT_BRIDGE_STATE_DIR")
    os.environ["AGENT_BRIDGE_STATE_DIR"] = str(tmpdir / "state")
    try:
        ids = ["20260425T000000Z-xaaaaaa", "20260425T000001Z-yaaaaaa"]
        for idx, snapshot_id in enumerate(ids):
            text_path, meta_path = bv.snapshot_paths("test-session", "codex1", snapshot_id)
            text_path.parent.mkdir(parents=True, exist_ok=True)
            text_path.write_text(f"snapshot {idx}\n", encoding="utf-8")
            meta_path.write_text(json.dumps({"snapshot_id": snapshot_id, "created_at": bv.utc_now()}), encoding="utf-8")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            bv.render_output(
                room="test-session",
                caller="viewer",
                target="codex1",
                target_record={"agent_type": "codex", "pane": "%99"},
                mode="onboard",
                lines=["hello"],
                total_lines=1,
                max_chars=12000,
                snapshot_id=ids[0],
            )
        out = buf.getvalue()
        assert_true("snapshot=xaaaaaa" in out, f"{label}: displayed ref expands past colliding 6-char suffix: {out!r}")
        assert_true("snapshot=aaaaaa" not in out, f"{label}: colliding 6-char ref must not be displayed: {out!r}")
        assert_true(bv.resolve_snapshot_id("test-session", "codex1", "xaaaaaa") == ids[0], f"{label}: expanded ref resolves")
        assert_true(bv.resolve_snapshot_id("test-session", "codex1", "") == "", f"{label}: empty ref does not match every snapshot")
        try:
            bv.resolve_snapshot_id("test-session", "codex1", "aaaaaa")
        except SystemExit as exc:
            msg = str(exc)
        else:
            raise AssertionError(f"{label}: ambiguous 6-char suffix must fail")
        assert_true("ambiguous snapshot ref" in msg, f"{label}: ambiguous error explains issue: {msg!r}")
        assert_true("xaaaaaa" in msg and "yaaaaaa" in msg, f"{label}: ambiguous error lists actionable refs: {msg!r}")
        assert_true(ids[0] not in msg and ids[1] not in msg, f"{label}: ambiguous error hides full ids: {msg!r}")
    finally:
        if saved is None:
            os.environ.pop("AGENT_BRIDGE_STATE_DIR", None)
        else:
            os.environ["AGENT_BRIDGE_STATE_DIR"] = saved
    print(f"  PASS  {label}")


def scenario_view_peer_capture_errors_sanitized(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    saved = os.environ.get("AGENT_BRIDGE_STATE_DIR")
    os.environ["AGENT_BRIDGE_STATE_DIR"] = str(tmpdir / "state")
    old_short_id = bv.short_id
    old_room_status = bv.room_status
    try:
        safe = bv.model_safe_capture_error("can't find pane %99\nrelated pane %12" + ("x" * 300), "%99")
        assert_true("%99" not in safe and "%12" not in safe and "\n" not in safe, f"{label}: pane ids/newlines redacted: {safe!r}")
        assert_true("<target-pane>" in safe and "<pane>" in safe, f"{label}: redaction markers present: {safe!r}")
        assert_true(len(safe) <= 200, f"{label}: error capped: {len(safe)}")

        bv.short_id = lambda prefix: "cap-fixed"
        bv.room_status = lambda session: argparse.Namespace(state="alive", reason="ok")
        response_file = bv.capture_response_dir("test-session") / "cap-fixed.json"
        response_file.parent.mkdir(parents=True, exist_ok=True)
        response_file.write_text(json.dumps({"ok": False, "error": "can't find pane %99\nother pane %12"}), encoding="utf-8")
        args = argparse.Namespace(capture_timeout=0.1)
        try:
            bv.capture_via_daemon(
                args,
                session="test-session",
                caller="viewer",
                target="codex1",
                state={"state_file": str(tmpdir / "state" / "test-session" / "events.raw.jsonl")},
                pane="%99",
                start=-10,
            )
        except SystemExit as exc:
            msg = str(exc)
        else:
            raise AssertionError(f"{label}: daemon error response must raise")
        assert_true("target codex1" in msg, f"{label}: target alias used: {msg!r}")
        assert_true("%99" not in msg and "%12" not in msg and "\n" not in msg, f"{label}: daemon response error sanitized: {msg!r}")
    finally:
        bv.short_id = old_short_id
        bv.room_status = old_room_status
        if saved is None:
            os.environ.pop("AGENT_BRIDGE_STATE_DIR", None)
        else:
            os.environ["AGENT_BRIDGE_STATE_DIR"] = saved
    print(f"  PASS  {label}")


def scenario_view_peer_snapshot_not_found_hides_full_id(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    saved = os.environ.get("AGENT_BRIDGE_STATE_DIR")
    os.environ["AGENT_BRIDGE_STATE_DIR"] = str(tmpdir / "state")
    try:
        full_snapshot_id = "20260425T000000Z-hidden12"
        try:
            bv.load_snapshot("test-session", "codex1", full_snapshot_id)
        except SystemExit as exc:
            msg = str(exc)
        else:
            raise AssertionError(f"{label}: missing snapshot must raise")
        assert_true(full_snapshot_id not in msg, f"{label}: full id hidden: {msg!r}")
        assert_true("hidden12"[-6:] in msg, f"{label}: short ref retained: {msg!r}")
    finally:
        if saved is None:
            os.environ.pop("AGENT_BRIDGE_STATE_DIR", None)
        else:
            os.environ["AGENT_BRIDGE_STATE_DIR"] = saved
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_matches_changed_volatile_chrome(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    stable = [
        "Review finding HIGH-1 process identity validation keeps endpoint safe",
        "Implementation note bridge_view_peer stable anchor cursor stores unique windows",
        "Validation command python3 scripts/run_regressions.py completed cleanly",
        "Reviewer summary msg-abc123 confirms since-last behavior is stable",
    ]
    previous = ["old output before cursor", *stable, "\u273b Churned for 4m 1s", "\u2500" * 40, "\u276f", "  \u23f5\u23f5 bypass permissions on (shift+tab to cycle)"]
    current = [*stable, "New semantic output from peer after onboard should be shown", "\u273b Churned for 4m 8s", "  \u23f5\u23f5 bypass permissions on (shift+tab to cycle)"]
    cursor = {"since_anchors": bv.build_since_anchors(previous), "last_tail_lines": previous[-30:]}
    delta, confidence, note = bv.compute_since_delta(cursor, current)
    assert_true(confidence == "high", f"{label}: expected high confidence, got {confidence} note={note!r}")
    assert_true(delta == ["New semantic output from peer after onboard should be shown"], f"{label}: volatile suffix trimmed from delta: {delta!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_legacy_tail_derives_stable_anchor(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    stable = [
        "Legacy cursor retained useful file path /tmp/agent-bridge-share/review.md",
        "Secondary reviewer response msg-legacy confirms stable matching can recover",
        "Bridge command agent_view_peer codex-reviewer --since-last should advance",
        "Regression note python3 scripts/run_regressions.py covers legacy tails",
    ]
    previous_tail = [*stable, "\u273b Churned for 56s", "\u2500" * 40, "\u276f"]
    current = [*stable, "Fresh line after legacy cursor should appear", "\u273b Churned for 1m 2s"]
    delta, confidence, note = bv.compute_since_delta({"last_tail_lines": previous_tail}, current)
    assert_true(confidence == "high", f"{label}: legacy stable anchor should match, got {confidence} note={note!r}")
    assert_true(delta == ["Fresh line after legacy cursor should appear"], f"{label}: expected fresh legacy delta, got {delta!r}")
    assert_true("legacy anchor" in note, f"{label}: note should identify legacy anchor: {note!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_ambiguous_current_anchor_skips_to_unique(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    ambiguous_anchor = [
        "Common reviewer paragraph explains repeated process identity issue alpha",
        "Common reviewer paragraph explains repeated process identity issue beta",
        "Common reviewer paragraph explains repeated process identity issue gamma",
        "Common reviewer paragraph explains repeated process identity issue delta",
    ]
    unique_anchor = [
        "Unique cursor anchor line one includes msg-unique-001 for disambiguation",
        "Unique cursor anchor line two references bridge_view_peer.py implementation",
        "Unique cursor anchor line three references scripts/run_regressions.py",
        "Unique cursor anchor line four references agent_view_peer since-last",
    ]
    cursor = {
        "since_anchors": [
            {"lines": ambiguous_anchor, "stable_count": 4},
            {"lines": unique_anchor, "stable_count": 4},
        ]
    }
    current = [*ambiguous_anchor, "unrelated middle output", *ambiguous_anchor, *unique_anchor, "Only this unique-match delta should be shown"]
    delta, confidence, note = bv.compute_since_delta(cursor, current)
    assert_true(confidence == "high", f"{label}: unique fallback anchor should match, got {confidence} note={note!r}")
    assert_true(delta == ["Only this unique-match delta should be shown"], f"{label}: ambiguous anchor must not be used: {delta!r}")
    assert_true("skipping 1 newer anchor" in note, f"{label}: note should mention skipped ambiguous anchor: {note!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_matches_anchor_before_long_delta(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    anchor = [
        "Long delta anchor line one includes msg-long-001 and detail",
        "Long delta anchor line two references bridge_view_peer.py implementation",
        "Long delta anchor line three references scripts/run_regressions.py",
        "Long delta anchor line four references agent_view_peer since-last",
    ]
    current = [
        *anchor,
        *[f"Semantic output line {idx:03d} after anchor with enough detail for matching" for idx in range(bv.SINCE_ANCHOR_SCAN_RAW_LINES + 1)],
    ]
    delta, confidence, note = bv.compute_since_delta({"since_anchors": [{"lines": anchor, "stable_count": 4}]}, current)
    assert_true(confidence == "high", f"{label}: full match projection should find old anchor, got {confidence} note={note!r}")
    assert_true(len(delta) == bv.SINCE_ANCHOR_SCAN_RAW_LINES + 1, f"{label}: full long delta should be returned, got {len(delta)}")
    assert_true(delta[0].startswith("Semantic output line 000"), f"{label}: delta should begin right after anchor: {delta[:2]!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_build_rejects_duplicate_previous_window(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    repeated = [
        "Repeated anchor candidate line one has enough information for matching",
        "Repeated anchor candidate line two has enough information for matching",
        "Repeated anchor candidate line three has enough information for matching",
        "Repeated anchor candidate line four has enough information for matching",
    ]
    anchors = bv.build_since_anchors([*repeated, *repeated])
    anchor_lines = [anchor.get("lines") for anchor in anchors]
    assert_true(repeated not in anchor_lines, f"{label}: duplicate previous window must not be stored: {anchor_lines!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_uncertain_does_not_advance_cursor(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    import contextlib
    import io
    saved = os.environ.get("AGENT_BRIDGE_STATE_DIR")
    os.environ["AGENT_BRIDGE_STATE_DIR"] = str(tmpdir / "state")
    old_capture_text = bv.capture_text
    try:
        cursor = {
            "cursor_version": 2,
            "caller": "viewer",
            "target": "codex1",
            "since_anchors": [
                {
                    "lines": [
                        "Missing anchor one contains msg-missing-001 and enough detail",
                        "Missing anchor two contains bridge_view_peer.py and enough detail",
                        "Missing anchor three contains run_regressions.py and detail",
                        "Missing anchor four contains agent_view_peer --since-last detail",
                    ],
                    "stable_count": 4,
                }
            ],
            "last_tail_lines": ["old"],
            "sentinel": "keep-me",
        }
        path = bv.cursor_path("test-session", "viewer", "codex1")
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, cursor)
        bv.capture_text = lambda *args, **kwargs: "\n".join([
            "Current output has a different stable line with msg-current-001",
            "Another unrelated stable line references bridge stable anchors",
            "Third unrelated stable line references python3 scripts regression",
            "Fourth unrelated stable line references cursor not advanced",
        ])  # type: ignore[assignment]
        args = argparse.Namespace(raw=False, capture_file=None, capture_timeout=0.1)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            bv.handle_since_last(args, "test-session", "viewer", "codex1", {}, {"agent_type": "codex", "pane": "%99"}, 20, 12000, True)
        out = buf.getvalue()
        after = read_json(path, {})
        assert_true(after.get("sentinel") == "keep-me", f"{label}: uncertain cursor should not be overwritten: {after!r}")
        assert_true(after.get("since_anchors") == cursor["since_anchors"], f"{label}: uncertain anchors should stay unchanged")
        assert_true("cursor not advanced" in out, f"{label}: output should explain cursor preservation: {out!r}")
    finally:
        bv.capture_text = old_capture_text  # type: ignore[assignment]
        if saved is None:
            os.environ.pop("AGENT_BRIDGE_STATE_DIR", None)
        else:
            os.environ["AGENT_BRIDGE_STATE_DIR"] = saved
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_upgrade_reset_when_no_legacy_anchor(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    import contextlib
    import io
    saved = os.environ.get("AGENT_BRIDGE_STATE_DIR")
    os.environ["AGENT_BRIDGE_STATE_DIR"] = str(tmpdir / "state")
    old_capture_text = bv.capture_text
    try:
        path = bv.cursor_path("test-session", "viewer", "codex1")
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, {"caller": "viewer", "target": "codex1", "last_tail_lines": ["\u2500" * 40, "\u276f", "  \u23f5\u23f5 bypass permissions on (shift+tab to cycle)"]})
        bv.capture_text = lambda *args, **kwargs: "\n".join([
            "Upgrade reset current line one includes msg-reset-001 detail",
            "Upgrade reset current line two references bridge_view_peer.py detail",
            "Upgrade reset current line three references run_regressions.py",
            "Upgrade reset current line four references agent_view_peer cursor v2",
        ])  # type: ignore[assignment]
        args = argparse.Namespace(raw=False, capture_file=None, capture_timeout=0.1)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            bv.handle_since_last(args, "test-session", "viewer", "codex1", {}, {"agent_type": "codex", "pane": "%99"}, 20, 12000, True)
        out = buf.getvalue()
        after = read_json(path, {})
        assert_true(after.get("cursor_version") == 2, f"{label}: upgrade reset should write v2 cursor: {after!r}")
        assert_true(after.get("since_anchors"), f"{label}: upgrade reset should store fresh anchors: {after!r}")
        assert_true("cursor reset from current capture" in out, f"{label}: reset note expected: {out!r}")
    finally:
        bv.capture_text = old_capture_text  # type: ignore[assignment]
        if saved is None:
            os.environ.pop("AGENT_BRIDGE_STATE_DIR", None)
        else:
            os.environ["AGENT_BRIDGE_STATE_DIR"] = saved
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_low_info_lines_do_not_anchor(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    lines = [
        "\u2500" * 80,
        "\u276f",
        "  \u23f5\u23f5 bypass permissions on (shift+tab to cycle)",
        "",
        "\u273b Churned for 56s",
    ]
    assert_true(bv.build_since_anchors(lines) == [], f"{label}: low-info TUI chrome must not produce anchors")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_claude_status_variants_are_volatile(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    volatile_lines = [
        "\u273b Baked for 1s",
        "\u273b Cogitated for 3m 33s",
        "\u273b Cooked for 1m 3s",
        "\u273b Brewed for 4s",
        "\u273b Crunched for 1m 41s",
        "\u2500 Worked for 1m 07s \u2500\u2500\u2500\u2500\u2500",
        "\u273b Quantumizing\u2026 (6s \u00b7 thinking with high effort)",
        "\u273d Embellishing\u2026 (4s \u00b7 thinking with high effort)",
        "\u273d Saut\u00e9ing\u2026 (running stop hook \u00b7 13s \u00b7 \u2193 326 tokens)",
        "* Befuddling\u2026 (20s \u00b7 still thinking with high effort)",
        "\u2502 \u2022 Running Stop hook \u2502",
        "\u273b Baked for <n> \u00b7 <n> shell still running",
    ]
    for line in volatile_lines:
        assert_true(bv.is_since_volatile_line(line), f"{label}: expected volatile Claude status: {line!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_status_classifier_preserves_prose(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    prose_lines = [
        "* Worked for 3 hours on this bug",
        "Working... (4h overtime today)",
        "* Running Stop hook should be tested in test_view_peer.py",
        "The implementation worked for this bridge_view_peer.py scenario and should remain visible",
        "I was thinking with high effort about the review plan and wrote this note",
        "The baked fixture output is a real semantic line with enough detail",
        "* Implementing... (using tokens for auth)",
        "Implementing... (using tokens for auth)",
        "* Working... (auth tokens are valid)",
        "* Tokenizing... (input has 100 tokens for processing)",
        "Tokenizing... (input has 100 tokens for processing)",
        "Working... (thought for the day)",
        "Implementing... (running stop hook command in test setup)",
    ]
    for line in prose_lines:
        assert_true(not bv.is_since_volatile_line(line), f"{label}: semantic prose must remain visible: {line!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_claude_status_lines_do_not_anchor(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    stable = [
        "Claude status anchor test line one references bridge_view_peer.py msg-status-001",
        "Claude status anchor test line two references scripts/run_regressions.py",
        "Claude status anchor test line three references agent_view_peer since-last",
        "Claude status anchor test line four references volatile filtering behavior",
    ]
    status_lines = [
        "\u273b Baked for 1s",
        "\u273b Cogitated for 3m 33s",
        "\u273d Saut\u00e9ing\u2026 (running stop hook \u00b7 13s \u00b7 \u2193 326 tokens)",
        "* Befuddling\u2026 (20s \u00b7 still thinking with high effort)",
    ]
    anchors = bv.build_since_anchors([stable[0], status_lines[0], stable[1], status_lines[1], stable[2], status_lines[2], stable[3], status_lines[3]])
    assert_true(anchors, f"{label}: stable lines should still form anchors")
    for anchor in anchors:
        for line in anchor.get("lines", []):
            assert_true(not bv.is_since_volatile_line(line), f"{label}: anchor retained volatile status line: {line!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_filters_stored_volatile_anchor_lines(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    status = "\u273d Saut\u00e9ing\u2026 (running stop hook \u00b7 <n> \u00b7 \u2193 <n>)"
    stable = [
        "Stored filtered anchor stable line one includes msg-filter-001 and enough detail",
        "Stored filtered anchor stable line two references bridge_view_peer.py details",
        "Stored filtered anchor stable line three references scripts/run_regressions.py details",
    ]
    cursor = {"since_anchors": [{"lines": [status, *stable], "stable_count": 4}]}
    current = [*stable, "Fresh semantic output after filtered stored anchor should appear"]
    delta, confidence, note = bv.compute_since_delta(cursor, current)
    assert_true(confidence == "medium", f"{label}: filtered 3-line stored anchor should match as medium, got {confidence} note={note!r}")
    assert_true(delta == ["Fresh semantic output after filtered stored anchor should appear"], f"{label}: expected filtered-anchor delta, got {delta!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_skips_shortened_stored_anchor_that_fails_quality(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    status = "\u273b Baked for <n>"
    weak = [
        "Weak anchor line alpha x1",
        "Weak anchor line beta x2",
        "Weak anchor line gamma x3",
    ]
    valid = [
        "Valid fallback anchor line one includes msg-valid-001 and detail",
        "Valid fallback anchor line two references bridge_view_peer.py behavior",
        "Valid fallback anchor line three references scripts/run_regressions.py",
        "Valid fallback anchor line four references agent_view_peer since-last",
    ]
    cursor = {
        "since_anchors": [
            {"lines": [status, *weak], "stable_count": 4},
            {"lines": valid, "stable_count": 4},
        ]
    }
    current = [*weak, "Bad delta from weak anchor must not be used", *valid, "Good semantic delta from valid anchor"]
    delta, confidence, note = bv.compute_since_delta(cursor, current)
    assert_true(confidence == "high", f"{label}: valid fallback anchor should match, got {confidence} note={note!r}")
    assert_true(delta == ["Good semantic delta from valid anchor"], f"{label}: weak shortened anchor must be skipped: {delta!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_volatile_only_claude_status_delta(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    stable = [
        "Claude volatile delta anchor one includes msg-claude-volatile-001 detail",
        "Claude volatile delta anchor two references bridge_view_peer.py detail",
        "Claude volatile delta anchor three references run_regressions.py",
        "Claude volatile delta anchor four references agent_view_peer since-last",
    ]
    current = [*stable, "\u273d Saut\u00e9ing\u2026 (running stop hook \u00b7 13s \u00b7 \u2193 326 tokens)"]
    delta, confidence, note = bv.compute_since_delta({"since_anchors": bv.build_since_anchors(stable)}, current)
    assert_true(confidence == "high", f"{label}: anchor should match, got {confidence} note={note!r}")
    assert_true(delta == [], f"{label}: Claude volatile-only delta should be hidden: {delta!r}")
    assert_true("only volatile TUI status changed" in note, f"{label}: volatile status note expected: {note!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_codex_status_variants_preserved(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    volatile_lines = [
        "\u2022 Working (1m 42s \u2022 esc to interrupt)",
        "\u25e6 Working (4s \u2022 esc to interrupt)",
        "\u2502 \u2022 Working (15s \u2022 esc to interrupt) \u2502",
        "\u23f5\u23f5 bypass permissions on (shift+tab to cycle)",
        "\u2502 gpt-5.5 xhigh \u00b7 /data/sembench-hard \u2502",
        "Remote Control active \u2502",
    ]
    for line in volatile_lines:
        assert_true(bv.is_since_volatile_line(line), f"{label}: expected volatile Codex chrome: {line!r}")
    prose_lines = [
        "\u2022 Reviewed bridge_view_peer.py and kept this semantic bullet visible",
        "The gpt-5.5 model string appears in this semantic sentence and should remain visible",
    ]
    for line in prose_lines:
        assert_true(not bv.is_since_volatile_line(line), f"{label}: semantic Codex/prose line must remain visible: {line!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_volatile_only_delta(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    stable = [
        "Volatile only test anchor one includes msg-volatile-001 detail",
        "Volatile only test anchor two references bridge_view_peer.py detail",
        "Volatile only test anchor three references run_regressions.py",
        "Volatile only test anchor four references agent_view_peer since-last",
    ]
    previous = [*stable, "\u273b Churned for 10s"]
    current = [*stable, "\u273b Churned for 40s", "  \u23f5\u23f5 bypass permissions on (shift+tab to cycle)"]
    delta, confidence, note = bv.compute_since_delta({"since_anchors": bv.build_since_anchors(previous)}, current)
    assert_true(confidence == "high", f"{label}: anchor should match, got {confidence} note={note!r}")
    assert_true(delta == [], f"{label}: volatile-only delta should be hidden: {delta!r}")
    assert_true("only volatile TUI status changed" in note, f"{label}: volatile status note expected: {note!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_short_delta_consumed_once(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    stable = [
        "Short consumed anchor one includes msg-short-001 and bridge_view_peer.py",
        "Short consumed anchor two references scripts/run_regressions.py",
        "Short consumed anchor three references agent_view_peer since-last behavior",
        "Short consumed anchor four references causal-short-consumed detail",
    ]
    current = [*stable, "\u25cf - ACK claude-reviewer", "  - 12"]
    cursor = {"since_anchors": bv.build_since_anchors(stable)}
    first = bv.compute_since_delta_detail(cursor, current)
    assert_true(first["delta"] == ["\u25cf - ACK claude-reviewer", "  - 12"], f"{label}: first short delta should be visible: {first!r}")
    cursor["since_consumed_tail"] = bv.build_since_consumed_tail(str(first["matched_anchor_identity"]), list(first["consumed_raw_delta"]))
    second_delta, second_confidence, second_note = bv.compute_since_delta(cursor, current)
    assert_true(second_confidence == "high", f"{label}: second view should still match anchor: {second_confidence} {second_note!r}")
    assert_true(second_delta == [], f"{label}: consumed short delta should not repeat: {second_delta!r}")
    assert_true("skipped previously consumed" in second_note, f"{label}: note should mention consumed skip: {second_note!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_request_plus_short_reply_consumed_after_cursor_update(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    import contextlib
    import io
    saved = os.environ.get("AGENT_BRIDGE_STATE_DIR")
    os.environ["AGENT_BRIDGE_STATE_DIR"] = str(tmpdir / "state")
    old_capture_text = bv.capture_text
    try:
        stable = [
            "Request consumed base anchor one includes msg-request-consumed-001 bridge_view_peer.py",
            "Request consumed base anchor two references scripts/run_regressions.py",
            "Request consumed base anchor three references agent_view_peer since-last behavior",
            "Request consumed base anchor four references causal-request-consumed detail",
        ]
        request = [
            "\u203a [bridge:20260426T000000Z-x] from=codex-worker kind=request causal_id=causal-request-consumed",
            "aggregate_id=agg-request-consumed. Reply normally; do not call agent_send_peer",
            "Request: [FINAL-VERIFY-SHORT] Reply with exactly two short bullet lines",
            "first line says ACK <your-alias>; second line says 3+4=7",
        ]
        reply = ["\u2022 - ACK codex-reviewer", "  - 3+4=7"]
        current = [*stable, *request, "", *reply, "\u203a Improve documentation in @filename", "  gpt-5.5 xhigh \u00b7 ~/agent-bridge"]
        path = bv.cursor_path("test-session", "viewer", "codex1")
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, {"caller": "viewer", "target": "codex1", "since_anchors": bv.build_since_anchors(stable)})
        bv.capture_text = lambda *args, **kwargs: "\n".join(current)  # type: ignore[assignment]
        args = argparse.Namespace(raw=False, capture_file=None, capture_timeout=0.1)
        first_buf = io.StringIO()
        with contextlib.redirect_stdout(first_buf):
            bv.handle_since_last(args, "test-session", "viewer", "codex1", {}, {"agent_type": "codex", "pane": "%99"}, 40, 12000, True)
        first_out = first_buf.getvalue()
        assert_true("ACK codex-reviewer" in first_out and "FINAL-VERIFY-SHORT" in first_out, f"{label}: first view should show request and reply: {first_out!r}")
        second_buf = io.StringIO()
        with contextlib.redirect_stdout(second_buf):
            bv.handle_since_last(args, "test-session", "viewer", "codex1", {}, {"agent_type": "codex", "pane": "%99"}, 40, 12000, True)
        second_out = second_buf.getvalue()
        assert_true("ACK codex-reviewer" not in second_out, f"{label}: short reply must not repeat after cursor update: {second_out!r}")
        assert_true("skipped previously consumed" in second_out, f"{label}: consumed skip note expected: {second_out!r}")
    finally:
        bv.capture_text = old_capture_text  # type: ignore[assignment]
        if saved is None:
            os.environ.pop("AGENT_BRIDGE_STATE_DIR", None)
        else:
            os.environ["AGENT_BRIDGE_STATE_DIR"] = saved
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_consumed_tail_does_not_hide_new_duplicate(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    stable = [
        "Duplicate consumed anchor one includes msg-dup-001 and bridge_view_peer.py",
        "Duplicate consumed anchor two references scripts/run_regressions.py",
        "Duplicate consumed anchor three references agent_view_peer since-last behavior",
        "Duplicate consumed anchor four references causal-duplicate detail",
    ]
    old_reply = ["\u2022 - ACK codex-reviewer", "  - 12"]
    cursor = {"since_anchors": bv.build_since_anchors(stable)}
    first = bv.compute_since_delta_detail(cursor, [*stable, *old_reply])
    cursor["since_consumed_tail"] = bv.build_since_consumed_tail(str(first["matched_anchor_identity"]), list(first["consumed_raw_delta"]))
    current = [
        *stable,
        *old_reply,
        "\u203a [bridge:20260426T000000Z-x] from=codex-worker kind=request causal_id=causal-dup",
        "\u2022 - ACK codex-reviewer",
        "  - 12",
    ]
    delta, confidence, note = bv.compute_since_delta(cursor, current)
    assert_true(confidence == "high", f"{label}: expected high confidence, got {confidence} note={note!r}")
    assert_true(delta == current[len(stable) + len(old_reply) :], f"{label}: duplicate new reply must remain visible: {delta!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_consumed_tail_anchor_change_resets(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    stable = [
        "Anchor change line one includes msg-anchor-change-001 bridge_view_peer.py",
        "Anchor change line two references scripts/run_regressions.py",
        "Anchor change line three references agent_view_peer since-last behavior",
        "Anchor change line four references causal-anchor-change detail",
    ]
    current = [*stable, "\u2022 New output after changed anchor should remain visible"]
    cursor = {
        "since_anchors": bv.build_since_anchors(stable),
        "since_consumed_tail": {"anchor_identity": "sha256:not-the-current-anchor", "lines": ["\u2022 New output after changed anchor should remain visible"], "truncated": False},
    }
    detail = bv.compute_since_delta_detail(cursor, current)
    assert_true(detail["delta"] == ["\u2022 New output after changed anchor should remain visible"], f"{label}: changed-anchor memo must not hide output: {detail!r}")
    assert_true("reset stale consumed-tail" in str(detail["note"]), f"{label}: note should mention reset: {detail!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_consumed_tail_mismatch_clears(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    stable = [
        "Mismatch consumed anchor one includes msg-mismatch-001 bridge_view_peer.py",
        "Mismatch consumed anchor two references scripts/run_regressions.py",
        "Mismatch consumed anchor three references agent_view_peer since-last behavior",
        "Mismatch consumed anchor four references causal-mismatch detail",
    ]
    anchor_identity = bv.since_anchor_identity(list(bv.build_since_anchors(stable)[0]["lines"]))
    current = [*stable, "\u2022 Different output should remain visible"]
    cursor = {
        "since_anchors": bv.build_since_anchors(stable),
        "since_consumed_tail": {"anchor_identity": anchor_identity, "lines": ["\u2022 Previous output"], "truncated": False},
    }
    detail = bv.compute_since_delta_detail(cursor, current)
    assert_true(detail["delta"] == ["\u2022 Different output should remain visible"], f"{label}: mismatched memo must not hide output: {detail!r}")
    assert_true("reset stale consumed-tail" in str(detail["note"]), f"{label}: mismatch should clear memo: {detail!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_consumed_tail_cap(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    identity = bv.since_anchor_identity(["Cap anchor one", "Cap anchor two", "Cap anchor three", "Cap anchor four"])
    raw_delta = [f"Consumed cap line {idx:02d} has enough text to count but not anchor" for idx in range(bv.SINCE_CONSUMED_TAIL_MAX_LINES + 5)]
    memo = bv.build_since_consumed_tail(identity, raw_delta)
    assert_true(memo.get("anchor_identity") == identity, f"{label}: identity should be stored: {memo!r}")
    assert_true(len(memo.get("lines") or []) == bv.SINCE_CONSUMED_TAIL_MAX_LINES, f"{label}: line cap expected: {memo!r}")
    assert_true(memo.get("truncated") is True, f"{label}: truncated flag expected: {memo!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_consumed_tail_ignores_volatile_churn(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    stable = [
        "Volatile consumed anchor one includes msg-volatile-consumed-001 bridge_view_peer.py",
        "Volatile consumed anchor two references scripts/run_regressions.py",
        "Volatile consumed anchor three references agent_view_peer since-last behavior",
        "Volatile consumed anchor four references causal-volatile-consumed detail",
    ]
    current = [*stable, "\u25cf - ACK claude-reviewer", "  - 12"]
    cursor = {"since_anchors": bv.build_since_anchors(stable)}
    first = bv.compute_since_delta_detail(cursor, current)
    cursor["since_consumed_tail"] = bv.build_since_consumed_tail(str(first["matched_anchor_identity"]), list(first["consumed_raw_delta"]))
    churned = [*stable, "\u273b Churning\u2026 (4s \u00b7 \u2193 1 tokens)", "\u25cf - ACK claude-reviewer", "\u273b Churning\u2026 (5s \u00b7 \u2193 2 tokens)", "  - 12"]
    delta, confidence, note = bv.compute_since_delta(cursor, churned)
    assert_true(confidence == "high", f"{label}: expected high confidence, got {confidence} note={note!r}")
    assert_true(delta == [], f"{label}: volatile churn should not defeat consumed prefix: {delta!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_codex_prompt_placeholder_not_anchor(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    stable = [
        "Codex prompt anchor one includes msg-codex-prompt-001 bridge_view_peer.py",
        "Codex prompt anchor two references scripts/run_regressions.py",
        "Codex prompt anchor three references agent_view_peer since-last behavior",
        "Codex prompt anchor four references causal-codex-prompt detail",
    ]
    prompt = "\u203a Improve documentation in @filename"
    footer = "  gpt-5.5 xhigh \u00b7 ~/agent-bridge"
    previous = [*stable, prompt, "", footer]
    cursor = {"since_anchors": bv.build_since_anchors(previous)}
    current = [
        *stable,
        "\u203a [bridge:20260426T000000Z-x] from=codex-worker kind=request causal_id=causal-codex-prompt",
        "\u2022 - ACK codex-reviewer",
        "  - 12",
        prompt,
        "",
        footer,
    ]
    delta, confidence, note = bv.compute_since_delta(cursor, current)
    assert_true(confidence == "high", f"{label}: stable anchor should match, got {confidence} note={note!r}")
    assert_true(delta == current[len(stable) : len(stable) + 3], f"{label}: prompt anchor must not hide response above it: {delta!r}")
    for anchor in cursor["since_anchors"]:
        assert_true(prompt not in anchor.get("lines", []), f"{label}: prompt placeholder must not be stored as anchor: {cursor!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_filters_stored_codex_prompt_placeholder_anchor(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    prompt = "\u203a Improve documentation in @filename"
    stable = [
        "Stored codex prompt stable one includes msg-stored-codex-001 bridge_view_peer.py",
        "Stored codex prompt stable two references scripts/run_regressions.py",
        "Stored codex prompt stable three references agent_view_peer since-last behavior",
    ]
    cursor = {"since_anchors": [{"lines": [prompt, *stable], "stable_count": 4}]}
    current = [*stable, "Fresh output after stored codex prompt cleanup should appear"]
    delta, confidence, note = bv.compute_since_delta(cursor, current)
    assert_true(confidence == "medium", f"{label}: shortened stored anchor should match, got {confidence} note={note!r}")
    assert_true(delta == ["Fresh output after stored codex prompt cleanup should appear"], f"{label}: polluted prompt line should be filtered: {delta!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_preserves_codex_bridge_prompt_lines(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    bridge_line = "\u203a [bridge:20260426T000000Z-x] from=codex-worker kind=request causal_id=causal-bridge-line"
    stable = [
        bridge_line,
        "Bridge prompt preserve stable two references bridge_view_peer.py detail",
        "Bridge prompt preserve stable three references scripts/run_regressions.py",
        "Bridge prompt preserve stable four references agent_view_peer since-last",
    ]
    anchors = bv.build_since_anchors(stable)
    assert_true(any(bridge_line in anchor.get("lines", []) for anchor in anchors), f"{label}: bridge prompt line should remain anchor-eligible: {anchors!r}")
    assert_true(bv.cursor_anchors({"since_anchors": [{"lines": stable, "stable_count": 4}]})[0], f"{label}: stored bridge prompt line should remain usable")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_preserves_trailing_semantic_codex_arrow(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    stable = [
        "Semantic codex arrow anchor one includes msg-arrow-001 bridge_view_peer.py",
        "Semantic codex arrow anchor two references scripts/run_regressions.py",
        "Semantic codex arrow anchor three references agent_view_peer since-last behavior",
        "Semantic codex arrow anchor four references causal-arrow detail",
    ]
    semantic = "\u203a This semantic blockquote-like output should remain visible"
    cursor = {"since_anchors": bv.build_since_anchors(stable)}
    delta, confidence, note = bv.compute_since_delta(cursor, [*stable, semantic])
    assert_true(confidence == "high", f"{label}: expected high confidence, got {confidence} note={note!r}")
    assert_true(delta == [semantic], f"{label}: trailing semantic arrow output must remain visible: {delta!r}")
    assert_true(len(stable) not in bv.since_context_volatile_indexes([*stable, semantic]), f"{label}: semantic arrow should not be context-volatile")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_preserves_semantic_codex_arrow_before_footer(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    stable = [
        "Semantic codex footer anchor one includes msg-arrow-footer-001 bridge_view_peer.py",
        "Semantic codex footer anchor two references scripts/run_regressions.py",
        "Semantic codex footer anchor three references agent_view_peer since-last behavior",
        "Semantic codex footer anchor four references causal-arrow-footer detail",
    ]
    semantic = "\u203a This semantic blockquote-like output should remain visible"
    footer = "  gpt-5.5 xhigh \u00b7 ~/agent-bridge"
    cursor = {"since_anchors": bv.build_since_anchors(stable)}
    delta, confidence, note = bv.compute_since_delta(cursor, [*stable, semantic, "", footer])
    assert_true(confidence == "high", f"{label}: expected high confidence, got {confidence} note={note!r}")
    assert_true(delta == [semantic], f"{label}: semantic arrow before footer must remain visible: {delta!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_claude_partial_status_fragments_are_volatile(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    assert_true(bv.is_since_volatile_line("\u273b Precipitating\u2026"), f"{label}: rare glyph partial should be volatile")
    assert_true(bv.is_since_volatile_line("\u00b7 Precipitating\u2026 (5s \u00b7 \u2193 1 tokens)"), f"{label}: middle-dot payload status should be volatile")
    stable = [
        "Partial status anchor one includes msg-partial-001 bridge_view_peer.py",
        "Partial status anchor two references scripts/run_regressions.py",
        "Partial status anchor three references agent_view_peer since-last behavior",
        "Partial status anchor four references causal-partial detail",
    ]
    current = [*stable, "\u00b7 Unraveling\u2026", "\u2500" * 40]
    delta, confidence, note = bv.compute_since_delta({"since_anchors": bv.build_since_anchors(stable)}, current)
    assert_true(confidence == "high", f"{label}: expected high confidence, got {confidence} note={note!r}")
    assert_true(delta == [], f"{label}: contextual middle-dot fragment should be hidden: {delta!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_since_last_partial_status_preserves_prose(label: str, tmpdir: Path) -> None:
    bv = _import_view_peer()
    prose = [
        "\u00b7 Unraveling\u2026 additional content",
        "\u00b7 Unraveling the parser behavior took time",
        "\u273b Precipitating",
    ]
    for line in prose:
        assert_true(not bv.is_since_volatile_line(line), f"{label}: prose/no-ellipsis line must remain visible: {line!r}")
    stable = [
        "Partial prose anchor one includes msg-partial-prose-001 bridge_view_peer.py",
        "Partial prose anchor two references scripts/run_regressions.py",
        "Partial prose anchor three references agent_view_peer since-last behavior",
        "Partial prose anchor four references causal-partial-prose detail",
    ]
    current = [*stable, prose[0], "\u2500" * 40]
    delta, confidence, note = bv.compute_since_delta({"since_anchors": bv.build_since_anchors(stable)}, current)
    assert_true(confidence == "high", f"{label}: expected high confidence, got {confidence} note={note!r}")
    assert_true(delta == [prose[0]], f"{label}: prose should survive adjacent TUI chrome: {delta!r}")
    bare_fragment = "\u00b7 Unraveling\u2026"
    bare_delta, bare_confidence, bare_note = bv.compute_since_delta({"since_anchors": bv.build_since_anchors(stable)}, [*stable, bare_fragment])
    assert_true(bare_confidence == "high", f"{label}: expected high confidence for bare fragment, got {bare_confidence} note={bare_note!r}")
    assert_true(bare_delta == [bare_fragment], f"{label}: middle-dot fragment without TUI evidence must remain visible: {bare_delta!r}")
    print(f"  PASS  {label}")


def scenario_view_peer_unverified_endpoint_uses_daemon_not_local_capture(label: str, tmpdir: Path) -> None:
    import bridge_view_peer as bv

    state = {"participants": {"bob": {"alias": "bob", "agent_type": "codex", "pane": "%2", "hook_session_id": "sess-bob", "status": "active"}}}
    args = argparse.Namespace(capture_file=None, capture_timeout=0.1)
    calls: list[str] = []
    old_resolve = bv.resolve_participant_endpoint_detail
    old_capture = bv.run_tmux_capture
    old_daemon = bv.capture_via_daemon
    bv.resolve_participant_endpoint_detail = lambda *args, **kwargs: {"ok": False, "reason": "process_mismatch"}  # type: ignore[assignment]
    bv.run_tmux_capture = lambda *args, **kwargs: calls.append("local") or ""  # type: ignore[assignment]
    bv.capture_via_daemon = lambda *args, **kwargs: "daemon-capture"  # type: ignore[assignment]
    try:
        text = bv.capture_text(args, session="test-session", caller="alice", target="bob", state=state, pane="%2", start=-10)
    finally:
        bv.resolve_participant_endpoint_detail = old_resolve  # type: ignore[assignment]
        bv.run_tmux_capture = old_capture  # type: ignore[assignment]
        bv.capture_via_daemon = old_daemon  # type: ignore[assignment]
    assert_true(text == "daemon-capture" and calls == [], f"{label}: unverified endpoint must not use local capture")
    print(f"  PASS  {label}")


SCENARIOS = [
    ('format_peer_list_model_safe_default', scenario_format_peer_list_model_safe_default),
    ('format_peer_list_full_includes_operator_fields', scenario_format_peer_list_full_includes_operator_fields),
    ('bridge_manage_summary_concise', scenario_bridge_manage_summary_concise),
    ('bridge_manage_summary_defaults', scenario_bridge_manage_summary_defaults),
    ('bridge_manage_summary_legacy_state_fallback', scenario_bridge_manage_summary_legacy_state_fallback),
    ('bridge_manage_summary_missing_session_exits', scenario_bridge_manage_summary_missing_session_exits),
    ('model_safe_participants_strips_endpoints', scenario_model_safe_participants_strips_endpoints),
    ('model_safe_participants_uses_active_only', scenario_model_safe_participants_uses_active_only),
    ('list_peers_json_daemon_status_strips_pid', scenario_list_peers_json_daemon_status_strips_pid),
    ('view_peer_rejects_snapshot_without_snapshot_mode', scenario_view_peer_rejects_snapshot_without_snapshot_mode),
    ('view_peer_rejects_page_with_since_last_or_search', scenario_view_peer_rejects_page_with_since_last_or_search),
    ('view_peer_allows_page_in_live_onboard_older_and_snapshot_in_older_search', scenario_view_peer_allows_page_in_live_onboard_older_and_snapshot_in_older_search),
    ('view_peer_rejects_empty_search_before_session_lookup', scenario_view_peer_rejects_empty_search_before_session_lookup),
    ('view_peer_empty_search_counts_as_search_for_mode_errors', scenario_view_peer_empty_search_counts_as_search_for_mode_errors),
    ('view_peer_nonempty_search_still_reaches_validation', scenario_view_peer_nonempty_search_still_reaches_validation),
    ('view_peer_search_with_page_after_a19_reports_page_error', scenario_view_peer_search_with_page_after_a19_reports_page_error),
    ('view_peer_render_output_model_safe', scenario_view_peer_render_output_model_safe),
    ('view_peer_search_explicit_snapshot_uses_safe_ref', scenario_view_peer_search_explicit_snapshot_uses_safe_ref),
    ('view_peer_snapshot_ref_collision_unique', scenario_view_peer_snapshot_ref_collision_unique),
    ('view_peer_capture_errors_sanitized', scenario_view_peer_capture_errors_sanitized),
    ('view_peer_snapshot_not_found_hides_full_id', scenario_view_peer_snapshot_not_found_hides_full_id),
    ('view_peer_since_last_matches_changed_volatile_chrome', scenario_view_peer_since_last_matches_changed_volatile_chrome),
    ('view_peer_since_last_legacy_tail_derives_stable_anchor', scenario_view_peer_since_last_legacy_tail_derives_stable_anchor),
    ('view_peer_since_last_ambiguous_current_anchor_skips_to_unique', scenario_view_peer_since_last_ambiguous_current_anchor_skips_to_unique),
    ('view_peer_since_last_matches_anchor_before_long_delta', scenario_view_peer_since_last_matches_anchor_before_long_delta),
    ('view_peer_since_last_build_rejects_duplicate_previous_window', scenario_view_peer_since_last_build_rejects_duplicate_previous_window),
    ('view_peer_since_last_uncertain_does_not_advance_cursor', scenario_view_peer_since_last_uncertain_does_not_advance_cursor),
    ('view_peer_since_last_upgrade_reset_when_no_legacy_anchor', scenario_view_peer_since_last_upgrade_reset_when_no_legacy_anchor),
    ('view_peer_since_last_low_info_lines_do_not_anchor', scenario_view_peer_since_last_low_info_lines_do_not_anchor),
    ('view_peer_since_last_claude_status_variants_are_volatile', scenario_view_peer_since_last_claude_status_variants_are_volatile),
    ('view_peer_since_last_status_classifier_preserves_prose', scenario_view_peer_since_last_status_classifier_preserves_prose),
    ('view_peer_since_last_claude_status_lines_do_not_anchor', scenario_view_peer_since_last_claude_status_lines_do_not_anchor),
    ('view_peer_since_last_filters_stored_volatile_anchor_lines', scenario_view_peer_since_last_filters_stored_volatile_anchor_lines),
    ('view_peer_since_last_skips_shortened_stored_anchor_that_fails_quality', scenario_view_peer_since_last_skips_shortened_stored_anchor_that_fails_quality),
    ('view_peer_since_last_volatile_only_claude_status_delta', scenario_view_peer_since_last_volatile_only_claude_status_delta),
    ('view_peer_since_last_codex_status_variants_preserved', scenario_view_peer_since_last_codex_status_variants_preserved),
    ('view_peer_since_last_volatile_only_delta', scenario_view_peer_since_last_volatile_only_delta),
    ('view_peer_since_last_short_delta_consumed_once', scenario_view_peer_since_last_short_delta_consumed_once),
    ('view_peer_since_last_request_plus_short_reply_consumed_after_cursor_update', scenario_view_peer_since_last_request_plus_short_reply_consumed_after_cursor_update),
    ('view_peer_since_last_consumed_tail_does_not_hide_new_duplicate', scenario_view_peer_since_last_consumed_tail_does_not_hide_new_duplicate),
    ('view_peer_since_last_consumed_tail_anchor_change_resets', scenario_view_peer_since_last_consumed_tail_anchor_change_resets),
    ('view_peer_since_last_consumed_tail_mismatch_clears', scenario_view_peer_since_last_consumed_tail_mismatch_clears),
    ('view_peer_since_last_consumed_tail_cap', scenario_view_peer_since_last_consumed_tail_cap),
    ('view_peer_since_last_consumed_tail_ignores_volatile_churn', scenario_view_peer_since_last_consumed_tail_ignores_volatile_churn),
    ('view_peer_since_last_codex_prompt_placeholder_not_anchor', scenario_view_peer_since_last_codex_prompt_placeholder_not_anchor),
    ('view_peer_since_last_filters_stored_codex_prompt_placeholder_anchor', scenario_view_peer_since_last_filters_stored_codex_prompt_placeholder_anchor),
    ('view_peer_since_last_preserves_codex_bridge_prompt_lines', scenario_view_peer_since_last_preserves_codex_bridge_prompt_lines),
    ('view_peer_since_last_preserves_trailing_semantic_codex_arrow', scenario_view_peer_since_last_preserves_trailing_semantic_codex_arrow),
    ('view_peer_since_last_preserves_semantic_codex_arrow_before_footer', scenario_view_peer_since_last_preserves_semantic_codex_arrow_before_footer),
    ('view_peer_since_last_claude_partial_status_fragments_are_volatile', scenario_view_peer_since_last_claude_partial_status_fragments_are_volatile),
    ('view_peer_since_last_partial_status_preserves_prose', scenario_view_peer_since_last_partial_status_preserves_prose),
    ('view_peer_unverified_endpoint_uses_daemon_not_local_capture', scenario_view_peer_unverified_endpoint_uses_daemon_not_local_capture),
]
