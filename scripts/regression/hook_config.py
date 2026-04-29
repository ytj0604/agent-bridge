from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from .harness import (
    ROOT,
    _fake_install_env,
    _write_seed_hook_configs,
    assert_true,
)

import bridge_codex_config  # noqa: E402


def _run_bridge_install_hooks(
    tmpdir: Path,
    *,
    claude_settings: Path | None = None,
    codex_hooks: Path | None = None,
    codex_config: Path | None = None,
    skip_claude: bool = False,
    skip_codex: bool = False,
    dry_run: bool = False,
) -> subprocess.CompletedProcess:
    hook_command = tmpdir / "bridge-hook"
    hook_command.parent.mkdir(parents=True, exist_ok=True)
    hook_command.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    os.chmod(hook_command, 0o755)
    cmd = [
        sys.executable,
        str(ROOT / "libexec" / "agent-bridge" / "bridge_install_hooks.py"),
        "--hook-command",
        str(hook_command),
        "--claude-settings",
        str(claude_settings or (tmpdir / "settings.json")),
        "--codex-hooks",
        str(codex_hooks or (tmpdir / "hooks.json")),
        "--codex-config",
        str(codex_config or (tmpdir / "config.toml")),
    ]
    if skip_claude:
        cmd.append("--skip-claude")
    if skip_codex:
        cmd.append("--skip-codex")
    if dry_run:
        cmd.append("--dry-run")
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=_fake_install_env(tmpdir),
        timeout=10,
    )


def _run_bridge_uninstall_hooks(
    tmpdir: Path,
    *,
    claude_settings: Path | None = None,
    codex_hooks: Path | None = None,
    codex_config: Path | None = None,
    skip_claude: bool = False,
    skip_codex: bool = False,
    dry_run: bool = False,
) -> subprocess.CompletedProcess:
    cmd = [
        sys.executable,
        str(ROOT / "libexec" / "agent-bridge" / "bridge_uninstall_hooks.py"),
        "--claude-settings",
        str(claude_settings or (tmpdir / "settings.json")),
        "--codex-hooks",
        str(codex_hooks or (tmpdir / "hooks.json")),
        "--codex-config",
        str(codex_config or (tmpdir / "config.toml")),
    ]
    if skip_claude:
        cmd.append("--skip-claude")
    if skip_codex:
        cmd.append("--skip-codex")
    if dry_run:
        cmd.append("--dry-run")
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=_fake_install_env(tmpdir),
        timeout=10,
    )


def _managed_marker(path: Path) -> Path:
    return bridge_codex_config.managed_marker_path(path)


def _assert_hook_config_unchanged(label: str, path: Path, before: bytes, proc: subprocess.CompletedProcess, *needles: str) -> None:
    assert_true(proc.returncode != 0, f"{label}: hook installer should fail, got {proc.returncode}: stdout={proc.stdout!r} stderr={proc.stderr!r}")
    for needle in needles:
        assert_true(needle in proc.stderr, f"{label}: stderr should contain {needle!r}: {proc.stderr!r}")
    assert_true(path.read_bytes() == before, f"{label}: invalid existing hook config must not be overwritten")


def scenario_bridge_install_hooks_rejects_malformed_json_without_overwrite(label: str, tmpdir: Path) -> None:
    settings = tmpdir / "claude-malformed" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_bytes(b'{"hooks": [')
    before = settings.read_bytes()
    proc = _run_bridge_install_hooks(tmpdir, claude_settings=settings, skip_codex=True)
    _assert_hook_config_unchanged(
        label,
        settings,
        before,
        proc,
        str(settings),
        "invalid JSON",
        "refusing to overwrite",
        "Fix or move aside",
    )
    print(f"  PASS  {label}")


def scenario_bridge_install_hooks_rejects_non_object_json_without_overwrite(label: str, tmpdir: Path) -> None:
    for suffix, content in (("array", b"[1, 2, 3]"), ("null", b"null")):
        settings = tmpdir / f"claude-non-object-{suffix}" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_bytes(content)
        before = settings.read_bytes()
        proc = _run_bridge_install_hooks(tmpdir / suffix, claude_settings=settings, skip_codex=True)
        _assert_hook_config_unchanged(
            f"{label}:{suffix}",
            settings,
            before,
            proc,
            str(settings),
            "must be a JSON object",
            "refusing to overwrite",
        )
    print(f"  PASS  {label}")


def scenario_bridge_install_hooks_dry_run_invalid_json_fails_without_overwrite(label: str, tmpdir: Path) -> None:
    settings = tmpdir / "dry-run-invalid" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_bytes(b'{"hooks":')
    before = settings.read_bytes()
    before_entries = sorted(p.name for p in settings.parent.iterdir())
    proc = _run_bridge_install_hooks(tmpdir, claude_settings=settings, skip_codex=True, dry_run=True)
    _assert_hook_config_unchanged(
        label,
        settings,
        before,
        proc,
        "invalid JSON",
        "refusing to overwrite",
    )
    after_entries = sorted(p.name for p in settings.parent.iterdir())
    assert_true(after_entries == before_entries, f"{label}: dry-run invalid config should not create files: {after_entries}")
    print(f"  PASS  {label}")


def scenario_bridge_install_hooks_missing_json_still_creates(label: str, tmpdir: Path) -> None:
    settings = tmpdir / "new-claude" / "settings.json"
    proc = _run_bridge_install_hooks(tmpdir, claude_settings=settings, skip_codex=True)
    assert_true(proc.returncode == 0, f"{label}: missing config should be created, got {proc.returncode}: {proc.stderr!r}")
    data = json.loads(settings.read_text(encoding="utf-8"))
    hooks = data.get("hooks") or {}
    assert_true("SessionStart" in hooks and "Stop" in hooks, f"{label}: bridge hook events should be written: {data!r}")
    stop_blocks = hooks.get("Stop") or []
    assert_true(any("--agent claude" in ((block.get("hooks") or [{}])[0].get("command", "")) for block in stop_blocks if isinstance(block, dict)), f"{label}: claude hook command should be present: {data!r}")
    print(f"  PASS  {label}")


def scenario_bridge_install_hooks_invalid_utf8_fails_without_overwrite(label: str, tmpdir: Path) -> None:
    settings = tmpdir / "invalid-utf8" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_bytes(b'{"hooks": "\xff"}')
    before = settings.read_bytes()
    proc = _run_bridge_install_hooks(tmpdir, claude_settings=settings, skip_codex=True)
    _assert_hook_config_unchanged(
        label,
        settings,
        before,
        proc,
        str(settings),
        "invalid JSON",
        "refusing to overwrite",
        "Fix or move aside",
    )
    print(f"  PASS  {label}")


def scenario_bridge_install_hooks_existing_valid_json_object_merges_correctly(label: str, tmpdir: Path) -> None:
    settings = tmpdir / "valid-claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({
            "custom": {"preserve": True},
            "hooks": {
                "Stop": [
                    {
                        "matcher": "user-custom",
                        "hooks": [{"type": "command", "command": "echo user hook"}],
                    }
                ]
            },
        }),
        encoding="utf-8",
    )
    proc = _run_bridge_install_hooks(tmpdir, claude_settings=settings, skip_codex=True)
    assert_true(proc.returncode == 0, f"{label}: valid config should merge, got {proc.returncode}: {proc.stderr!r}")
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert_true(data.get("custom", {}).get("preserve") is True, f"{label}: user fields must be preserved: {data!r}")
    stop_blocks = data.get("hooks", {}).get("Stop") or []
    assert_true(any("echo user hook" in str(block) for block in stop_blocks), f"{label}: existing user hook should remain: {data!r}")
    assert_true(any("--agent claude" in str(block) for block in stop_blocks), f"{label}: bridge Stop hook should be merged: {data!r}")
    print(f"  PASS  {label}")


def scenario_bridge_install_hooks_codex_hooks_rejects_malformed_json_without_overwrite(label: str, tmpdir: Path) -> None:
    hooks = tmpdir / "codex-malformed" / "hooks.json"
    hooks.parent.mkdir(parents=True)
    hooks.write_bytes(b'{"hooks": [')
    before = hooks.read_bytes()
    proc = _run_bridge_install_hooks(tmpdir, codex_hooks=hooks, skip_claude=True)
    _assert_hook_config_unchanged(
        label,
        hooks,
        before,
        proc,
        str(hooks),
        "invalid JSON",
        "refusing to overwrite",
        "Fix or move aside",
    )
    print(f"  PASS  {label}")


def scenario_bridge_install_hooks_codex_config_ignores_nested_codex_hooks(label: str, tmpdir: Path) -> None:
    config = tmpdir / "codex-config-nested-hooks.toml"
    nested_block = '[codex_hooks]\nkind = "keep"\n'
    config.write_text(nested_block, encoding="utf-8")
    proc = _run_bridge_install_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(proc.returncode == 0, f"{label}: install should succeed: {proc.stderr!r}")
    text = config.read_text(encoding="utf-8")
    assert_true(nested_block in text, f"{label}: [codex_hooks] table content must be preserved: {text!r}")
    assert_true("[features]\ncodex_hooks = true\n" in text, f"{label}: [features].codex_hooks should be added: {text!r}")
    print(f"  PASS  {label}")


def scenario_bridge_install_hooks_codex_config_ignores_nested_disable_paste_burst(label: str, tmpdir: Path) -> None:
    config = tmpdir / "codex-config-nested-disable.toml"
    config.write_text("[profile]\ndisable_paste_burst = true\n", encoding="utf-8")
    proc = _run_bridge_install_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(proc.returncode == 0, f"{label}: install should succeed: {proc.stderr!r}")
    text = config.read_text(encoding="utf-8")
    assert_true(text.startswith("disable_paste_burst = true\n[profile]\n"), f"{label}: top-level disable_paste_burst should be inserted before first table: {text!r}")
    assert_true("[profile]\ndisable_paste_burst = true\n" in text, f"{label}: nested disable_paste_burst should remain unchanged: {text!r}")
    print(f"  PASS  {label}")


def scenario_bridge_install_hooks_codex_config_updates_only_scoped_keys(label: str, tmpdir: Path) -> None:
    config = tmpdir / "codex-config-scoped-update.toml"
    config.write_text(
        "disable_paste_burst = false  # was off\n"
        "\n"
        "[features]\n"
        "  codex_hooks = false  # was off\n"
        "\n"
        "[profile]\n"
        "disable_paste_burst = false  # profile scoped\n"
        "codex_hooks = false  # profile scoped\n",
        encoding="utf-8",
    )
    proc = _run_bridge_install_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(proc.returncode == 0, f"{label}: install should succeed: {proc.stderr!r}")
    text = config.read_text(encoding="utf-8")
    assert_true("disable_paste_burst = true  # was off\n" in text, f"{label}: top-level disable line/comment should update: {text!r}")
    assert_true("  codex_hooks = true  # was off\n" in text, f"{label}: [features] line indentation/comment should update: {text!r}")
    assert_true("disable_paste_burst = false  # profile scoped\n" in text, f"{label}: profile disable must stay false: {text!r}")
    assert_true("codex_hooks = false  # profile scoped\n" in text, f"{label}: profile codex_hooks must stay false: {text!r}")
    print(f"  PASS  {label}")


def scenario_bridge_install_hooks_codex_config_inserts_features_key_before_next_table(label: str, tmpdir: Path) -> None:
    config = tmpdir / "codex-config-features-insert.toml"
    config.write_text(
        "[features]\n"
        "experimental = true\n"
        "\n"
        "[profile]\n"
        "codex_hooks = false\n",
        encoding="utf-8",
    )
    proc = _run_bridge_install_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(proc.returncode == 0, f"{label}: install should succeed: {proc.stderr!r}")
    text = config.read_text(encoding="utf-8")
    features_index = text.index("[features]")
    inserted_index = text.index("codex_hooks = true")
    profile_index = text.index("[profile]")
    assert_true(features_index < inserted_index < profile_index, f"{label}: codex_hooks=true should land inside [features]: {text!r}")
    assert_true("[profile]\ncodex_hooks = false\n" in text, f"{label}: profile codex_hooks must remain unchanged: {text!r}")
    print(f"  PASS  {label}")


def scenario_bridge_install_hooks_codex_config_dry_run_no_write(label: str, tmpdir: Path) -> None:
    config = tmpdir / "codex-config-dry-run.toml"
    config.write_text("[features]\ncodex_hooks = false\n[profile]\ndisable_paste_burst = true\n", encoding="utf-8")
    before = config.read_bytes()
    proc = _run_bridge_install_hooks(tmpdir, codex_config=config, skip_claude=True, dry_run=True)
    assert_true(proc.returncode == 0, f"{label}: dry-run should succeed: {proc.stderr!r}")
    assert_true("codex_hooks enabled" in proc.stdout and "disable_paste_burst enabled" in proc.stdout, f"{label}: dry-run stdout should report computed actions: {proc.stdout!r}")
    assert_true(config.read_bytes() == before, f"{label}: dry-run must not alter config bytes")
    print(f"  PASS  {label}")


def scenario_bridge_install_hooks_codex_config_empty_features_section_inserts(label: str, tmpdir: Path) -> None:
    config = tmpdir / "codex-config-empty-features.toml"
    config.write_text("[features]\n[profile]\nname = \"p\"\n", encoding="utf-8")
    proc = _run_bridge_install_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(proc.returncode == 0, f"{label}: install should succeed: {proc.stderr!r}")
    text = config.read_text(encoding="utf-8")
    assert_true("[features]\ncodex_hooks = true\n[profile]\n" in text, f"{label}: empty features section should receive key before next table: {text!r}")
    print(f"  PASS  {label}")


def scenario_bridge_install_hooks_codex_config_first_table_is_array_table(label: str, tmpdir: Path) -> None:
    config = tmpdir / "codex-config-array-first.toml"
    config.write_text("[[features_array]]\nname = \"first\"\n", encoding="utf-8")
    proc = _run_bridge_install_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(proc.returncode == 0, f"{label}: install should succeed: {proc.stderr!r}")
    text = config.read_text(encoding="utf-8")
    assert_true(text.startswith("disable_paste_burst = true\n[[features_array]]\n"), f"{label}: top-level disable should be inserted before first array table: {text!r}")
    print(f"  PASS  {label}")


def scenario_bridge_install_hooks_codex_config_table_header_with_trailing_comment(label: str, tmpdir: Path) -> None:
    config = tmpdir / "codex-config-header-comment.toml"
    config.write_text("[features]  # primary features section\nother = true\n", encoding="utf-8")
    proc = _run_bridge_install_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(proc.returncode == 0, f"{label}: install should succeed: {proc.stderr!r}")
    text = config.read_text(encoding="utf-8")
    assert_true("[features]  # primary features section\nother = true\ncodex_hooks = true\n" in text, f"{label}: [features] with trailing comment should be recognized: {text!r}")
    print(f"  PASS  {label}")


def scenario_bridge_install_hooks_codex_config_commented_out_assignments_ignored(label: str, tmpdir: Path) -> None:
    config = tmpdir / "codex-config-comments.toml"
    config.write_text(
        "# disable_paste_burst = true\n"
        "# codex_hooks = true\n"
        "[features]\n"
        "# codex_hooks = true\n",
        encoding="utf-8",
    )
    proc = _run_bridge_install_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(proc.returncode == 0, f"{label}: install should succeed: {proc.stderr!r}")
    text = config.read_text(encoding="utf-8")
    assert_true("# disable_paste_burst = true\n" in text and "# codex_hooks = true\n" in text, f"{label}: commented assignments should remain comments: {text!r}")
    live_disable_index = text.index("disable_paste_burst = true\n")
    features_index = text.index("[features]")
    assert_true(live_disable_index < features_index, f"{label}: live top-level disable should be inserted before first table: {text!r}")
    assert_true("[features]\n# codex_hooks = true\ncodex_hooks = true\n" in text, f"{label}: live features codex_hooks should be inserted despite comment: {text!r}")
    print(f"  PASS  {label}")


def scenario_bridge_install_hooks_codex_config_no_trailing_newline_handled(label: str, tmpdir: Path) -> None:
    config = tmpdir / "codex-config-no-newline.toml"
    config.write_text("[features]\nother = true", encoding="utf-8")
    proc = _run_bridge_install_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(proc.returncode == 0, f"{label}: install should succeed: {proc.stderr!r}")
    text = config.read_text(encoding="utf-8")
    assert_true("other = true\ncodex_hooks = true\n" in text, f"{label}: no-newline config should not concatenate inserted key: {text!r}")
    assert_true(text.startswith("disable_paste_burst = true\n[features]\n"), f"{label}: top-level disable should still be inserted cleanly: {text!r}")
    print(f"  PASS  {label}")


def scenario_bridge_install_hooks_codex_config_already_enabled_is_byte_unchanged(label: str, tmpdir: Path) -> None:
    config = tmpdir / "codex-config-already.toml"
    config.write_text(
        "disable_paste_burst = true\n"
        "\n"
        "[features]\n"
        "codex_hooks = true\n"
        "\n"
        "[profile]\n"
        "codex_hooks = false\n",
        encoding="utf-8",
    )
    before = config.read_bytes()
    proc = _run_bridge_install_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(proc.returncode == 0, f"{label}: install should succeed: {proc.stderr!r}")
    assert_true("codex_hooks already enabled" in proc.stdout and "disable_paste_burst already enabled" in proc.stdout, f"{label}: stdout should report already-enabled actions: {proc.stdout!r}")
    assert_true(config.read_bytes() == before, f"{label}: already-enabled scoped config must remain byte-identical")
    print(f"  PASS  {label}")


def _read_managed_marker(path: Path) -> dict:
    return json.loads(_managed_marker(path).read_text(encoding="utf-8"))


def scenario_codex_config_marker_records_and_uninstall_restores_updated_values(label: str, tmpdir: Path) -> None:
    config = tmpdir / "codex-config-marker-updated.toml"
    original = (
        "disable_paste_burst = false  # old top-level value\n"
        "\n"
        "[features]\n"
        "  codex_hooks = false  # old feature value\n"
    )
    config.write_text(original, encoding="utf-8")

    install = _run_bridge_install_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(install.returncode == 0, f"{label}: install should succeed: {install.stderr!r}")
    text = config.read_text(encoding="utf-8")
    assert_true("disable_paste_burst = true  # old top-level value\n" in text, f"{label}: top-level flag should flip true: {text!r}")
    assert_true("  codex_hooks = true  # old feature value\n" in text, f"{label}: feature flag should flip true: {text!r}")
    marker = _read_managed_marker(config)
    assert_true(marker.get("codex_config") == bridge_codex_config.normalize_config_path(config), f"{label}: marker path should be normalized: {marker!r}")
    flags = marker.get("flags") or {}
    assert_true(flags.get(bridge_codex_config.DISABLE_PASTE_FLAG, {}).get("original_line") == "disable_paste_burst = false  # old top-level value\n", f"{label}: marker should record exact top-level line: {marker!r}")
    assert_true(flags.get(bridge_codex_config.CODEX_HOOKS_FLAG, {}).get("original_line") == "  codex_hooks = false  # old feature value\n", f"{label}: marker should record exact feature line: {marker!r}")

    uninstall = _run_bridge_uninstall_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(uninstall.returncode == 0, f"{label}: uninstall should succeed: {uninstall.stderr!r}")
    assert_true(config.read_text(encoding="utf-8") == original, f"{label}: uninstall should restore original bytes")
    assert_true(not _managed_marker(config).exists(), f"{label}: uninstall should remove managed marker")
    print(f"  PASS  {label}")


def scenario_codex_config_marker_records_inserted_keys_and_uninstall_removes_them(label: str, tmpdir: Path) -> None:
    config = tmpdir / "codex-config-marker-inserted.toml"
    original = 'title = "keep"\n[profile]\nname = "p"\n\n'
    config.write_text(original, encoding="utf-8")

    install = _run_bridge_install_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(install.returncode == 0, f"{label}: install should succeed: {install.stderr!r}")
    marker = _read_managed_marker(config)
    flags = marker.get("flags") or {}
    assert_true(flags.get(bridge_codex_config.CODEX_HOOKS_FLAG, {}).get("operation") == "inserted", f"{label}: codex_hooks should be marked inserted: {marker!r}")
    assert_true(flags.get(bridge_codex_config.CODEX_HOOKS_FLAG, {}).get("section_inserted") is True, f"{label}: inserted [features] section should be recorded: {marker!r}")
    assert_true(flags.get(bridge_codex_config.DISABLE_PASTE_FLAG, {}).get("operation") == "inserted", f"{label}: disable flag should be marked inserted: {marker!r}")

    uninstall = _run_bridge_uninstall_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(uninstall.returncode == 0, f"{label}: uninstall should succeed: {uninstall.stderr!r}")
    assert_true(config.read_text(encoding="utf-8") == original, f"{label}: uninstall should remove inserted bridge keys only")
    assert_true(not _managed_marker(config).exists(), f"{label}: uninstall should remove marker")
    print(f"  PASS  {label}")


def scenario_codex_config_missing_created_by_install_removed_on_uninstall_when_bridge_only(label: str, tmpdir: Path) -> None:
    config = tmpdir / "missing-codex" / "config.toml"
    install = _run_bridge_install_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(install.returncode == 0, f"{label}: install should create missing config: {install.stderr!r}")
    assert_true(config.exists(), f"{label}: config should exist after install")
    marker = _read_managed_marker(config)
    assert_true(marker.get("config_existed") is False, f"{label}: marker should record bridge-created config: {marker!r}")

    uninstall = _run_bridge_uninstall_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(uninstall.returncode == 0, f"{label}: uninstall should succeed: {uninstall.stderr!r}")
    assert_true(not config.exists(), f"{label}: bridge-created config should be removed")
    assert_true(not _managed_marker(config).exists(), f"{label}: marker should be removed")
    print(f"  PASS  {label}")


def scenario_codex_config_already_enabled_keys_create_no_marker(label: str, tmpdir: Path) -> None:
    config = tmpdir / "codex-config-no-marker.toml"
    original = "disable_paste_burst = true\n[features]\ncodex_hooks = true\n"
    config.write_text(original, encoding="utf-8")

    install = _run_bridge_install_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(install.returncode == 0, f"{label}: install should succeed: {install.stderr!r}")
    assert_true("already enabled" in install.stdout, f"{label}: install should report already-enabled flags: {install.stdout!r}")
    assert_true(config.read_text(encoding="utf-8") == original, f"{label}: config should remain unchanged")
    assert_true(not _managed_marker(config).exists(), f"{label}: already-enabled flags should not create marker")

    uninstall = _run_bridge_uninstall_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(uninstall.returncode == 0, f"{label}: uninstall should no-op cleanly: {uninstall.stderr!r}")
    assert_true(config.read_text(encoding="utf-8") == original, f"{label}: uninstall without marker should leave config unchanged")
    assert_true(not _managed_marker(config).exists(), f"{label}: marker should still be absent")
    print(f"  PASS  {label}")


def scenario_codex_config_reinstall_preserves_original_marker(label: str, tmpdir: Path) -> None:
    config = tmpdir / "codex-config-reinstall.toml"
    original = "disable_paste_burst = false\n[features]\ncodex_hooks = false\n"
    config.write_text(original, encoding="utf-8")

    first = _run_bridge_install_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(first.returncode == 0, f"{label}: first install should succeed: {first.stderr!r}")
    marker_after_first = _read_managed_marker(config)
    second = _run_bridge_install_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(second.returncode == 0, f"{label}: reinstall should succeed: {second.stderr!r}")
    marker_after_second = _read_managed_marker(config)
    assert_true(marker_after_second == marker_after_first, f"{label}: reinstall must preserve original marker entries: first={marker_after_first!r} second={marker_after_second!r}")

    uninstall = _run_bridge_uninstall_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(uninstall.returncode == 0, f"{label}: uninstall should succeed: {uninstall.stderr!r}")
    assert_true(config.read_text(encoding="utf-8") == original, f"{label}: original false values should be restored")
    print(f"  PASS  {label}")


def scenario_codex_config_user_changed_after_install_is_not_clobbered(label: str, tmpdir: Path) -> None:
    config = tmpdir / "codex-config-user-change.toml"
    original = "disable_paste_burst = false\n[features]\ncodex_hooks = false\n"
    config.write_text(original, encoding="utf-8")
    install = _run_bridge_install_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(install.returncode == 0, f"{label}: install should succeed: {install.stderr!r}")

    config.write_text("disable_paste_burst = false\n[features]\ncodex_hooks = true\n", encoding="utf-8")
    uninstall = _run_bridge_uninstall_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(uninstall.returncode == 0, f"{label}: uninstall should succeed despite user edit: {uninstall.stderr!r}")
    text = config.read_text(encoding="utf-8")
    assert_true("disable_paste_burst = false\n" in text, f"{label}: user-changed top-level key should remain false: {text!r}")
    assert_true("codex_hooks = false\n" in text, f"{label}: untouched bridge-managed feature key should restore: {text!r}")
    assert_true("skipping disable_paste_burst restore" in uninstall.stdout, f"{label}: uninstall should report skipped user-changed key: {uninstall.stdout!r}")
    assert_true(not _managed_marker(config).exists(), f"{label}: marker should be removed after uninstall")
    print(f"  PASS  {label}")


def scenario_codex_config_dry_run_install_writes_no_marker_or_config(label: str, tmpdir: Path) -> None:
    config = tmpdir / "codex-config-dry-marker.toml"
    original = "disable_paste_burst = false\n[features]\ncodex_hooks = false\n"
    config.write_text(original, encoding="utf-8")
    proc = _run_bridge_install_hooks(tmpdir, codex_config=config, skip_claude=True, dry_run=True)
    assert_true(proc.returncode == 0, f"{label}: dry-run install should succeed: {proc.stderr!r}")
    assert_true("codex_hooks enabled" in proc.stdout and "disable_paste_burst enabled" in proc.stdout, f"{label}: dry-run should report planned actions: {proc.stdout!r}")
    assert_true(config.read_text(encoding="utf-8") == original, f"{label}: dry-run install must not write config")
    assert_true(not _managed_marker(config).exists(), f"{label}: dry-run install must not write marker")
    print(f"  PASS  {label}")


def scenario_codex_config_dry_run_uninstall_with_marker_writes_no_config_or_marker(label: str, tmpdir: Path) -> None:
    config = tmpdir / "codex-config-dry-uninstall.toml"
    config.write_text("disable_paste_burst = false\n[features]\ncodex_hooks = false\n", encoding="utf-8")
    install = _run_bridge_install_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(install.returncode == 0, f"{label}: install should succeed: {install.stderr!r}")
    before_config = config.read_bytes()
    marker_path = _managed_marker(config)
    before_marker = marker_path.read_bytes()

    proc = _run_bridge_uninstall_hooks(tmpdir, codex_config=config, skip_claude=True, dry_run=True)
    assert_true(proc.returncode == 0, f"{label}: dry-run uninstall should succeed: {proc.stderr!r}")
    assert_true("would remove managed marker" in proc.stdout, f"{label}: dry-run uninstall should report marker retention: {proc.stdout!r}")
    assert_true(config.read_bytes() == before_config, f"{label}: dry-run uninstall must not write config")
    assert_true(marker_path.read_bytes() == before_marker, f"{label}: dry-run uninstall must not remove or rewrite marker")
    print(f"  PASS  {label}")


def scenario_codex_config_skip_codex_does_not_touch_config_or_marker(label: str, tmpdir: Path) -> None:
    config = tmpdir / "codex-config-skip-codex.toml"
    config.write_text("disable_paste_burst = false\n[features]\ncodex_hooks = false\n", encoding="utf-8")
    install = _run_bridge_install_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(install.returncode == 0, f"{label}: install should succeed: {install.stderr!r}")
    before_config = config.read_bytes()
    marker_path = _managed_marker(config)
    before_marker = marker_path.read_bytes()

    proc = _run_bridge_uninstall_hooks(tmpdir, codex_config=config, skip_claude=True, skip_codex=True)
    assert_true(proc.returncode == 0, f"{label}: --skip-codex uninstall should succeed: {proc.stderr!r}")
    assert_true(config.read_bytes() == before_config, f"{label}: --skip-codex must not touch config")
    assert_true(marker_path.read_bytes() == before_marker, f"{label}: --skip-codex must not touch marker")
    print(f"  PASS  {label}")


def _write_valid_marker(config: Path, *, version: int = 1, codex_config: str | None = None, flags: dict | None = None) -> Path:
    marker = _managed_marker(config)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps({
            "version": version,
            "codex_config": codex_config if codex_config is not None else bridge_codex_config.normalize_config_path(config),
            "config_existed": True,
            "flags": flags if flags is not None else {},
        }, indent=2) + "\n",
        encoding="utf-8",
    )
    return marker


def scenario_codex_config_uninstall_with_malformed_marker_aborts_clean(label: str, tmpdir: Path) -> None:
    home = tmpdir / "malformed-home"
    claude, codex_hooks = _write_seed_hook_configs(home)
    config = home / ".codex" / "config.toml"
    config.write_text("disable_paste_burst = true\n[features]\ncodex_hooks = true\n", encoding="utf-8")
    marker = _managed_marker(config)
    marker.write_bytes(b'{"version":')
    before_claude = claude.read_bytes()
    before_hooks = codex_hooks.read_bytes()
    before_config = config.read_bytes()
    before_marker = marker.read_bytes()

    proc = _run_bridge_uninstall_hooks(tmpdir, claude_settings=claude, codex_hooks=codex_hooks, codex_config=config)
    assert_true(proc.returncode != 0, f"{label}: malformed marker should abort")
    assert_true("invalid managed marker" in proc.stderr and "refusing to restore" in proc.stderr, f"{label}: targeted marker error expected: {proc.stderr!r}")
    assert_true(claude.read_bytes() == before_claude, f"{label}: Claude hooks must be unchanged after preflight failure")
    assert_true(codex_hooks.read_bytes() == before_hooks, f"{label}: Codex hooks must be unchanged after preflight failure")
    assert_true(config.read_bytes() == before_config, f"{label}: Codex config must be unchanged after preflight failure")
    assert_true(marker.read_bytes() == before_marker, f"{label}: malformed marker bytes should remain for inspection")
    print(f"  PASS  {label}")


def scenario_codex_config_uninstall_with_unknown_marker_version_aborts(label: str, tmpdir: Path) -> None:
    home = tmpdir / "unknown-version-home"
    claude, codex_hooks = _write_seed_hook_configs(home)
    config = home / ".codex" / "config.toml"
    config.write_text("disable_paste_burst = true\n[features]\ncodex_hooks = true\n", encoding="utf-8")
    _write_valid_marker(config, version=99)
    before_claude = claude.read_bytes()
    before_hooks = codex_hooks.read_bytes()
    before_config = config.read_bytes()

    proc = _run_bridge_uninstall_hooks(tmpdir, claude_settings=claude, codex_hooks=codex_hooks, codex_config=config)
    assert_true(proc.returncode != 0, f"{label}: unknown marker version should abort")
    assert_true("unsupported marker schema version 99" in proc.stderr and "Manually inspect" in proc.stderr, f"{label}: unsupported-version guidance expected: {proc.stderr!r}")
    assert_true(claude.read_bytes() == before_claude, f"{label}: Claude hooks must remain unchanged")
    assert_true(codex_hooks.read_bytes() == before_hooks, f"{label}: Codex hooks must remain unchanged")
    assert_true(config.read_bytes() == before_config, f"{label}: config must remain unchanged")
    print(f"  PASS  {label}")


def scenario_codex_config_uninstall_with_marker_path_mismatch_aborts(label: str, tmpdir: Path) -> None:
    home = tmpdir / "path-mismatch-home"
    claude, codex_hooks = _write_seed_hook_configs(home)
    config = home / ".codex" / "config.toml"
    config.write_text("disable_paste_burst = true\n[features]\ncodex_hooks = true\n", encoding="utf-8")
    _write_valid_marker(config, codex_config=str(tmpdir / "other" / "config.toml"))
    before_claude = claude.read_bytes()
    before_hooks = codex_hooks.read_bytes()
    before_config = config.read_bytes()

    proc = _run_bridge_uninstall_hooks(tmpdir, claude_settings=claude, codex_hooks=codex_hooks, codex_config=config)
    assert_true(proc.returncode != 0, f"{label}: marker path mismatch should abort")
    assert_true("belongs to" in proc.stderr and "refusing to restore" in proc.stderr, f"{label}: path mismatch error expected: {proc.stderr!r}")
    assert_true(claude.read_bytes() == before_claude, f"{label}: Claude hooks must remain unchanged")
    assert_true(codex_hooks.read_bytes() == before_hooks, f"{label}: Codex hooks must remain unchanged")
    assert_true(config.read_bytes() == before_config, f"{label}: config must remain unchanged")
    print(f"  PASS  {label}")


def scenario_codex_config_uninstall_with_unknown_flag_key_aborts(label: str, tmpdir: Path) -> None:
    home = tmpdir / "unknown-flag-home"
    claude, codex_hooks = _write_seed_hook_configs(home)
    config = home / ".codex" / "config.toml"
    config.write_text("disable_paste_burst = true\n[features]\ncodex_hooks = true\n", encoding="utf-8")
    marker = _write_valid_marker(config, flags={"unknown.flag": {"operation": "inserted"}})
    before_claude = claude.read_bytes()
    before_hooks = codex_hooks.read_bytes()
    before_config = config.read_bytes()
    before_marker = marker.read_bytes()

    proc = _run_bridge_uninstall_hooks(tmpdir, claude_settings=claude, codex_hooks=codex_hooks, codex_config=config)
    assert_true(proc.returncode != 0, f"{label}: unknown flag key should abort")
    assert_true("unknown flag key 'unknown.flag'" in proc.stderr and "refusing to restore" in proc.stderr, f"{label}: unknown-flag error expected: {proc.stderr!r}")
    assert_true(claude.read_bytes() == before_claude, f"{label}: Claude hooks must remain unchanged")
    assert_true(codex_hooks.read_bytes() == before_hooks, f"{label}: Codex hooks must remain unchanged")
    assert_true(config.read_bytes() == before_config, f"{label}: config must remain unchanged")
    assert_true(marker.read_bytes() == before_marker, f"{label}: marker must remain unchanged for inspection")
    print(f"  PASS  {label}")


def scenario_codex_config_uninstall_with_invalid_operation_aborts(label: str, tmpdir: Path) -> None:
    home = tmpdir / "invalid-operation-home"
    claude, codex_hooks = _write_seed_hook_configs(home)
    config = home / ".codex" / "config.toml"
    config.write_text("disable_paste_burst = true\n[features]\ncodex_hooks = true\n", encoding="utf-8")
    marker = _write_valid_marker(config, flags={bridge_codex_config.DISABLE_PASTE_FLAG: {"operation": "delete"}})
    before_claude = claude.read_bytes()
    before_hooks = codex_hooks.read_bytes()
    before_config = config.read_bytes()
    before_marker = marker.read_bytes()

    proc = _run_bridge_uninstall_hooks(tmpdir, claude_settings=claude, codex_hooks=codex_hooks, codex_config=config)
    assert_true(proc.returncode != 0, f"{label}: invalid marker operation should abort")
    assert_true("unsupported marker operation 'delete'" in proc.stderr and bridge_codex_config.DISABLE_PASTE_FLAG in proc.stderr, f"{label}: invalid-operation error expected: {proc.stderr!r}")
    assert_true(claude.read_bytes() == before_claude, f"{label}: Claude hooks must remain unchanged")
    assert_true(codex_hooks.read_bytes() == before_hooks, f"{label}: Codex hooks must remain unchanged")
    assert_true(config.read_bytes() == before_config, f"{label}: config must remain unchanged")
    assert_true(marker.read_bytes() == before_marker, f"{label}: marker must remain unchanged for inspection")
    print(f"  PASS  {label}")


def scenario_codex_config_uninstall_with_updated_missing_original_line_aborts(label: str, tmpdir: Path) -> None:
    home = tmpdir / "missing-original-line-home"
    claude, codex_hooks = _write_seed_hook_configs(home)
    config = home / ".codex" / "config.toml"
    config.write_text("disable_paste_burst = true\n[features]\ncodex_hooks = true\n", encoding="utf-8")
    marker = _write_valid_marker(config, flags={bridge_codex_config.DISABLE_PASTE_FLAG: {"operation": "updated"}})
    before_claude = claude.read_bytes()
    before_hooks = codex_hooks.read_bytes()
    before_config = config.read_bytes()
    before_marker = marker.read_bytes()

    proc = _run_bridge_uninstall_hooks(tmpdir, claude_settings=claude, codex_hooks=codex_hooks, codex_config=config)
    assert_true(proc.returncode != 0, f"{label}: updated entry missing original_line should abort")
    assert_true("missing/non-string original_line" in proc.stderr and bridge_codex_config.DISABLE_PASTE_FLAG in proc.stderr, f"{label}: original_line error expected: {proc.stderr!r}")
    assert_true(claude.read_bytes() == before_claude, f"{label}: Claude hooks must remain unchanged")
    assert_true(codex_hooks.read_bytes() == before_hooks, f"{label}: Codex hooks must remain unchanged")
    assert_true(config.read_bytes() == before_config, f"{label}: config must remain unchanged")
    assert_true(marker.read_bytes() == before_marker, f"{label}: marker must remain unchanged for inspection")
    print(f"  PASS  {label}")


def scenario_codex_config_marker_write_failure_aborts_before_config_write(label: str, tmpdir: Path) -> None:
    config = tmpdir / "codex-config-marker-write-failure.toml"
    original = "disable_paste_burst = false\n[features]\ncodex_hooks = false\n"
    config.write_text(original, encoding="utf-8")
    old_write_marker = bridge_codex_config._write_marker

    def fail_marker_write(_marker_path: Path, _marker: dict) -> None:
        raise SystemExit("agent-bridge: failed to write managed marker (simulated)")

    bridge_codex_config._write_marker = fail_marker_write
    try:
        try:
            bridge_codex_config.ensure_codex_config_flags(config, dry_run=False)
        except SystemExit as exc:
            message = str(exc)
        else:
            raise AssertionError(f"{label}: simulated marker write failure should abort")
    finally:
        bridge_codex_config._write_marker = old_write_marker
    assert_true("failed to write managed marker" in message, f"{label}: targeted marker-write error expected: {message!r}")
    assert_true(config.read_text(encoding="utf-8") == original, f"{label}: config write must not happen after marker write failure")
    assert_true(not _managed_marker(config).exists(), f"{label}: marker should not be created after simulated write failure")
    print(f"  PASS  {label}")


def scenario_codex_config_inserted_features_section_with_user_added_keys_keeps_section(label: str, tmpdir: Path) -> None:
    config = tmpdir / "codex-config-user-features.toml"
    config.write_text('title = "keep"\n', encoding="utf-8")
    install = _run_bridge_install_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(install.returncode == 0, f"{label}: install should succeed: {install.stderr!r}")
    text = config.read_text(encoding="utf-8")
    config.write_text(text.replace("codex_hooks = true\n", "codex_hooks = true\nfoo = \"bar\"\n"), encoding="utf-8")

    uninstall = _run_bridge_uninstall_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(uninstall.returncode == 0, f"{label}: uninstall should succeed: {uninstall.stderr!r}")
    text = config.read_text(encoding="utf-8")
    assert_true("[features]\nfoo = \"bar\"\n" in text, f"{label}: user-added features key should keep section alive: {text!r}")
    assert_true("codex_hooks = true" not in text, f"{label}: bridge-managed codex_hooks should be removed: {text!r}")
    assert_true("disable_paste_burst = true" not in text, f"{label}: bridge-managed top-level disable should be removed: {text!r}")
    print(f"  PASS  {label}")


def scenario_codex_config_existing_empty_config_records_existed_true_and_uninstall_keeps_it(label: str, tmpdir: Path) -> None:
    config = tmpdir / "codex-config-empty-existing.toml"
    config.write_text("", encoding="utf-8")
    install = _run_bridge_install_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(install.returncode == 0, f"{label}: install should populate empty existing config: {install.stderr!r}")
    marker = _read_managed_marker(config)
    assert_true(marker.get("config_existed") is True, f"{label}: marker should record existing empty config: {marker!r}")

    uninstall = _run_bridge_uninstall_hooks(tmpdir, codex_config=config, skip_claude=True)
    assert_true(uninstall.returncode == 0, f"{label}: uninstall should succeed: {uninstall.stderr!r}")
    assert_true(config.exists(), f"{label}: pre-existing empty config should not be deleted")
    assert_true(config.read_bytes() == b"", f"{label}: pre-existing empty config should return to empty bytes")
    assert_true(not _managed_marker(config).exists(), f"{label}: marker should be removed")
    print(f"  PASS  {label}")


def scenario_codex_config_marker_normalized_path_accepts_equivalent_path(label: str, tmpdir: Path) -> None:
    codex_dir = tmpdir / "codex-normalized"
    subdir = codex_dir / "subdir"
    subdir.mkdir(parents=True)
    config_alias = subdir / ".." / "config.toml"
    config_resolved = codex_dir / "config.toml"
    original = "disable_paste_burst = false\n[features]\ncodex_hooks = false\n"
    config_alias.write_text(original, encoding="utf-8")

    install = _run_bridge_install_hooks(tmpdir, codex_config=config_alias, skip_claude=True)
    assert_true(install.returncode == 0, f"{label}: install via alias path should succeed: {install.stderr!r}")
    marker = _read_managed_marker(config_resolved)
    assert_true(marker.get("codex_config") == bridge_codex_config.normalize_config_path(config_resolved), f"{label}: marker should store normalized path: {marker!r}")

    uninstall = _run_bridge_uninstall_hooks(tmpdir, codex_config=config_resolved, skip_claude=True)
    assert_true(uninstall.returncode == 0, f"{label}: uninstall via equivalent normalized path should succeed: {uninstall.stderr!r}")
    assert_true(config_resolved.read_text(encoding="utf-8") == original, f"{label}: config should restore through normalized path comparison")
    assert_true(not _managed_marker(config_resolved).exists(), f"{label}: marker should be removed")
    print(f"  PASS  {label}")


SCENARIOS = [
    ('bridge_install_hooks_rejects_malformed_json_without_overwrite', scenario_bridge_install_hooks_rejects_malformed_json_without_overwrite),
    ('bridge_install_hooks_rejects_non_object_json_without_overwrite', scenario_bridge_install_hooks_rejects_non_object_json_without_overwrite),
    ('bridge_install_hooks_dry_run_invalid_json_fails_without_overwrite', scenario_bridge_install_hooks_dry_run_invalid_json_fails_without_overwrite),
    ('bridge_install_hooks_missing_json_still_creates', scenario_bridge_install_hooks_missing_json_still_creates),
    ('bridge_install_hooks_invalid_utf8_fails_without_overwrite', scenario_bridge_install_hooks_invalid_utf8_fails_without_overwrite),
    ('bridge_install_hooks_existing_valid_json_object_merges_correctly', scenario_bridge_install_hooks_existing_valid_json_object_merges_correctly),
    ('bridge_install_hooks_codex_hooks_rejects_malformed_json_without_overwrite', scenario_bridge_install_hooks_codex_hooks_rejects_malformed_json_without_overwrite),
    ('bridge_install_hooks_codex_config_ignores_nested_codex_hooks', scenario_bridge_install_hooks_codex_config_ignores_nested_codex_hooks),
    ('bridge_install_hooks_codex_config_ignores_nested_disable_paste_burst', scenario_bridge_install_hooks_codex_config_ignores_nested_disable_paste_burst),
    ('bridge_install_hooks_codex_config_updates_only_scoped_keys', scenario_bridge_install_hooks_codex_config_updates_only_scoped_keys),
    ('bridge_install_hooks_codex_config_inserts_features_key_before_next_table', scenario_bridge_install_hooks_codex_config_inserts_features_key_before_next_table),
    ('bridge_install_hooks_codex_config_dry_run_no_write', scenario_bridge_install_hooks_codex_config_dry_run_no_write),
    ('bridge_install_hooks_codex_config_empty_features_section_inserts', scenario_bridge_install_hooks_codex_config_empty_features_section_inserts),
    ('bridge_install_hooks_codex_config_first_table_is_array_table', scenario_bridge_install_hooks_codex_config_first_table_is_array_table),
    ('bridge_install_hooks_codex_config_table_header_with_trailing_comment', scenario_bridge_install_hooks_codex_config_table_header_with_trailing_comment),
    ('bridge_install_hooks_codex_config_commented_out_assignments_ignored', scenario_bridge_install_hooks_codex_config_commented_out_assignments_ignored),
    ('bridge_install_hooks_codex_config_no_trailing_newline_handled', scenario_bridge_install_hooks_codex_config_no_trailing_newline_handled),
    ('bridge_install_hooks_codex_config_already_enabled_is_byte_unchanged', scenario_bridge_install_hooks_codex_config_already_enabled_is_byte_unchanged),
    ('codex_config_marker_records_and_uninstall_restores_updated_values', scenario_codex_config_marker_records_and_uninstall_restores_updated_values),
    ('codex_config_marker_records_inserted_keys_and_uninstall_removes_them', scenario_codex_config_marker_records_inserted_keys_and_uninstall_removes_them),
    ('codex_config_missing_created_by_install_removed_on_uninstall_when_bridge_only', scenario_codex_config_missing_created_by_install_removed_on_uninstall_when_bridge_only),
    ('codex_config_already_enabled_keys_create_no_marker', scenario_codex_config_already_enabled_keys_create_no_marker),
    ('codex_config_reinstall_preserves_original_marker', scenario_codex_config_reinstall_preserves_original_marker),
    ('codex_config_user_changed_after_install_is_not_clobbered', scenario_codex_config_user_changed_after_install_is_not_clobbered),
    ('codex_config_dry_run_install_writes_no_marker_or_config', scenario_codex_config_dry_run_install_writes_no_marker_or_config),
    ('codex_config_dry_run_uninstall_with_marker_writes_no_config_or_marker', scenario_codex_config_dry_run_uninstall_with_marker_writes_no_config_or_marker),
    ('codex_config_skip_codex_does_not_touch_config_or_marker', scenario_codex_config_skip_codex_does_not_touch_config_or_marker),
    ('codex_config_uninstall_with_malformed_marker_aborts_clean', scenario_codex_config_uninstall_with_malformed_marker_aborts_clean),
    ('codex_config_uninstall_with_unknown_marker_version_aborts', scenario_codex_config_uninstall_with_unknown_marker_version_aborts),
    ('codex_config_uninstall_with_marker_path_mismatch_aborts', scenario_codex_config_uninstall_with_marker_path_mismatch_aborts),
    ('codex_config_uninstall_with_unknown_flag_key_aborts', scenario_codex_config_uninstall_with_unknown_flag_key_aborts),
    ('codex_config_uninstall_with_invalid_operation_aborts', scenario_codex_config_uninstall_with_invalid_operation_aborts),
    ('codex_config_uninstall_with_updated_missing_original_line_aborts', scenario_codex_config_uninstall_with_updated_missing_original_line_aborts),
    ('codex_config_marker_write_failure_aborts_before_config_write', scenario_codex_config_marker_write_failure_aborts_before_config_write),
    ('codex_config_inserted_features_section_with_user_added_keys_keeps_section', scenario_codex_config_inserted_features_section_with_user_added_keys_keeps_section),
    ('codex_config_existing_empty_config_records_existed_true_and_uninstall_keeps_it', scenario_codex_config_existing_empty_config_records_existed_true_and_uninstall_keeps_it),
    ('codex_config_marker_normalized_path_accepts_equivalent_path', scenario_codex_config_marker_normalized_path_accepts_equivalent_path),
]
