from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .harness import (
    DIRECT_EXECUTABLE_TARGETS,
    INSTALL_SHIM_TARGETS,
    LIBEXEC,
    ROOT,
    _fake_install_env,
    _write_seed_hook_configs,
    assert_true,
)


def scenario_uninstall_helper_print_paths(label: str, tmpdir: Path) -> None:
    helper = str(LIBEXEC / "bridge_uninstall_state.py")
    proc = subprocess.run([sys.executable, helper, "--print-paths"], capture_output=True, text=True, timeout=10)
    assert_true(proc.returncode == 0, f"{label}: helper exit 0, got {proc.returncode}: {proc.stderr}")
    payload = json.loads(proc.stdout)
    for key in ("state", "run", "log"):
        assert_true(key in payload, f"{label}: payload contains {key}")
        assert_true(payload[key].endswith(key), f"{label}: {key} path looks like .../<{key}>")
    print(f"  PASS  {label}")


def scenario_uninstall_helper_refuses_dangerous_path(label: str, tmpdir: Path) -> None:
    helper = str(LIBEXEC / "bridge_uninstall_state.py")
    env = dict(os.environ)
    env["AGENT_BRIDGE_STATE_DIR"] = "/etc"  # dangerous
    proc = subprocess.run([sys.executable, helper, "--dry-run"], env=env, capture_output=True, text=True, timeout=10)
    assert_true(proc.returncode != 0, f"{label}: must refuse dangerous path, exit was {proc.returncode}")
    assert_true("refuses" in proc.stderr.lower() or "dangerous" in proc.stderr.lower(), f"{label}: stderr explains refusal: {proc.stderr!r}")
    print(f"  PASS  {label}")


def scenario_direct_exec_targets_executable(label: str, tmpdir: Path) -> None:
    missing = []
    not_executable = []
    for name, relative in DIRECT_EXECUTABLE_TARGETS:
        path = ROOT / relative
        if not path.exists():
            missing.append(f"{name}={path}")
        elif not os.access(path, os.X_OK):
            not_executable.append(f"{name}={path}")
    assert_true(not missing, f"{label}: direct exec targets missing: {missing}")
    assert_true(not not_executable, f"{label}: direct exec targets not executable: {not_executable}")
    print(f"  PASS  {label}")


def scenario_healthcheck_executable_helper_distinguishes_states(label: str, tmpdir: Path) -> None:
    import importlib
    hc = importlib.import_module("bridge_healthcheck")
    importlib.reload(hc)

    missing = tmpdir / "missing-tool"
    ok, detail = hc.check_executable(missing)
    assert_true(not ok, f"{label}: missing path must fail")
    assert_true("missing" in detail and "not executable" not in detail, f"{label}: missing detail must be distinct: {detail!r}")

    tool = tmpdir / "tool"
    tool.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    os.chmod(tool, 0o644)
    ok, detail = hc.check_executable(tool)
    assert_true(not ok, f"{label}: non-executable file must fail")
    assert_true("exists but is not executable" in detail, f"{label}: non-executable detail must be distinct: {detail!r}")

    os.chmod(tool, 0o755)
    ok, detail = hc.check_executable(tool)
    assert_true(ok and detail == str(tool), f"{label}: executable file must pass, got ok={ok} detail={detail!r}")
    print(f"  PASS  {label}")


def _write_fake_uninstall_tree(root: Path) -> None:
    shutil.copy2(ROOT / "uninstall.sh", root / "uninstall.sh")
    libexec = root / "libexec" / "agent-bridge"
    libexec.mkdir(parents=True, exist_ok=True)
    for name in ("bridge_uninstall_hooks.py", "bridge_codex_config.py", "bridge_util.py"):
        shutil.copy2(LIBEXEC / name, libexec / name)


def _write_fake_cli_tree(root: Path, marker: Path) -> None:
    bin_dir = root / "bin"
    libexec = root / "libexec" / "agent-bridge"
    bin_dir.mkdir(parents=True, exist_ok=True)
    libexec.mkdir(parents=True, exist_ok=True)
    for name in ("agent-bridge", "bridge_run.sh", "bridge_manage.sh"):
        shutil.copy2(ROOT / "bin" / name, bin_dir / name)
        os.chmod(bin_dir / name, 0o755)
    shutil.copy2(ROOT / "libexec" / "agent-bridge" / "bridge_common.sh", libexec / "bridge_common.sh")
    (libexec / "bridge_paths.py").write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "from pathlib import Path\n"
        "root = Path(__file__).resolve().parents[2]\n"
        "kind = sys.argv[1] if len(sys.argv) > 1 else ''\n"
        "mapping = {'state': root / 'state', 'run': root / 'run', 'log': root / 'log'}\n"
        "print(mapping.get(kind, root))\n",
        encoding="utf-8",
    )
    (libexec / "bridge_attach.py").write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        f"pathlib.Path({str(marker)!r}).write_text('attach ' + ' '.join(sys.argv[1:]) + '\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    (libexec / "bridge_daemon_ctl.py").write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        f"with pathlib.Path({str(marker)!r}).open('a', encoding='utf-8') as fh:\n"
        "    fh.write('daemon_ctl ' + ' '.join(sys.argv[1:]) + '\\n')\n"
        "print('daemon status')\n",
        encoding="utf-8",
    )
    (libexec / "bridge_manage_summary.py").write_text(
        "#!/usr/bin/env python3\n"
        "print('Agents:\\n- worker codex active target=test:1.1 pane=%1')\n",
        encoding="utf-8",
    )
    (libexec / "bridge_select.py").write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        f"with pathlib.Path({str(marker)!r}).open('a', encoding='utf-8') as fh:\n"
        "    fh.write('select ' + ' '.join(sys.argv[1:]) + '\\n')\n"
        "print('9')\n",
        encoding="utf-8",
    )


def _run_fake_uninstall(
    root: Path,
    *,
    env: dict[str, str],
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess:
    cmd = ["bash", str(root / "uninstall.sh"), "--bin-dir", str(Path(env["XDG_BIN_HOME"]))]
    cmd.extend(extra_args or [])
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


def scenario_uninstall_sh_dry_run_invokes_hook_helper_with_dry_run(label: str, tmpdir: Path) -> None:
    root = tmpdir / "uninstall-dry-run"
    root.mkdir()
    _write_fake_uninstall_tree(root)
    env = _fake_install_env(tmpdir / "dry-env")
    claude, codex = _write_seed_hook_configs(Path(env["HOME"]))
    before_claude = claude.read_bytes()
    before_codex = codex.read_bytes()

    proc = _run_fake_uninstall(root, env=env, extra_args=["--dry-run", "--keep-shims"])
    assert_true(proc.returncode == 0, f"{label}: dry-run uninstall should succeed: {proc.stderr!r}")
    assert_true("removed 2 hook command(s)" in proc.stdout, f"{label}: helper output should show removal count: {proc.stdout!r}")
    assert_true(proc.stdout.count("removed 2 hook command(s)") == 2, f"{label}: helper output should include Claude and Codex counts: {proc.stdout!r}")
    assert_true("dry-run: python3" not in proc.stdout and "bridge_uninstall_hooks.py" not in proc.stdout, f"{label}: hook helper must be invoked, not printed as dry-run command: {proc.stdout!r}")
    assert_true(claude.read_bytes() == before_claude, f"{label}: Claude config must be byte-identical under dry-run")
    assert_true(codex.read_bytes() == before_codex, f"{label}: Codex hooks config must be byte-identical under dry-run")
    print(f"  PASS  {label}")


def scenario_uninstall_sh_non_dry_run_removes_hook_entries(label: str, tmpdir: Path) -> None:
    root = tmpdir / "uninstall-real"
    root.mkdir()
    _write_fake_uninstall_tree(root)
    env = _fake_install_env(tmpdir / "real-env")
    claude, codex = _write_seed_hook_configs(Path(env["HOME"]))

    proc = _run_fake_uninstall(root, env=env, extra_args=["--keep-shims"])
    assert_true(proc.returncode == 0, f"{label}: temp uninstall should succeed: {proc.stderr!r}")
    claude_text = claude.read_text(encoding="utf-8")
    codex_text = codex.read_text(encoding="utf-8")
    assert_true("bridge-hook" not in claude_text and "agent-bridge" not in claude_text, f"{label}: Claude bridge hooks should be removed: {claude_text!r}")
    assert_true("bridge-hook" not in codex_text and "agent-bridge" not in codex_text, f"{label}: Codex bridge hooks should be removed: {codex_text!r}")
    assert_true("echo keep-claude" in claude_text and '"user": "preserve"' in claude_text, f"{label}: Claude user entries should remain: {claude_text!r}")
    assert_true("echo keep-codex" in codex_text and '"user": "preserve"' in codex_text, f"{label}: Codex user entries should remain: {codex_text!r}")
    print(f"  PASS  {label}")


def scenario_uninstall_sh_keep_hooks_skips_helper_under_dry_run(label: str, tmpdir: Path) -> None:
    root = tmpdir / "uninstall-keep-hooks"
    root.mkdir()
    _write_fake_uninstall_tree(root)
    sentinel = root / "libexec" / "agent-bridge" / "bridge_uninstall_hooks.py"
    sentinel.write_text("#!/usr/bin/env python3\nraise SystemExit(42)\n", encoding="utf-8")
    env = _fake_install_env(tmpdir / "keep-hooks-env")

    proc = _run_fake_uninstall(root, env=env, extra_args=["--dry-run", "--keep-hooks", "--keep-shims"])
    assert_true(proc.returncode == 0, f"{label}: --keep-hooks must skip failing sentinel helper: {proc.stderr!r}")
    assert_true("remove Claude/Codex hook entries" not in proc.stdout, f"{label}: hook section should be skipped entirely: {proc.stdout!r}")
    assert_true("removed " not in proc.stdout, f"{label}: no helper output expected: {proc.stdout!r}")
    print(f"  PASS  {label}")


def scenario_uninstall_sh_hook_helper_failure_aborts(label: str, tmpdir: Path) -> None:
    root = tmpdir / "uninstall-helper-fails"
    root.mkdir()
    _write_fake_uninstall_tree(root)
    sentinel = root / "libexec" / "agent-bridge" / "bridge_uninstall_hooks.py"
    sentinel.write_text("#!/usr/bin/env python3\nraise SystemExit(42)\n", encoding="utf-8")
    env = _fake_install_env(tmpdir / "helper-fails-env")

    proc = _run_fake_uninstall(root, env=env, extra_args=["--keep-shims"])
    assert_true(proc.returncode == 1, f"{label}: helper failure should map to uninstall.sh failure exit 1, got {proc.returncode}")
    assert_true("uninstall.sh: hook removal helper failed; aborting" in proc.stderr, f"{label}: targeted helper failure expected: {proc.stderr!r}")
    assert_true("uninstall complete" not in proc.stdout, f"{label}: script must abort before completion message: {proc.stdout!r}")
    print(f"  PASS  {label}")


def scenario_uninstall_sh_removes_agent_bridge_shim(label: str, tmpdir: Path) -> None:
    root = tmpdir / "uninstall-agent-bridge-shim"
    root.mkdir()
    _write_fake_uninstall_tree(root)
    env = _fake_install_env(tmpdir / "remove-agent-bridge-env")
    bin_dir = Path(env["XDG_BIN_HOME"])
    bin_dir.mkdir(parents=True)
    for name in ("agent-bridge", "bridge_run", "bridge_manage"):
        path = bin_dir / name
        path.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        os.chmod(path, 0o755)

    proc = _run_fake_uninstall(root, env=env, extra_args=["--keep-hooks"])
    assert_true(proc.returncode == 0, f"{label}: uninstall should succeed: {proc.stderr!r}")
    for name in ("agent-bridge", "bridge_run", "bridge_manage"):
        assert_true(not (bin_dir / name).exists(), f"{label}: {name} shim should be removed")

    proc_again = _run_fake_uninstall(root, env=env, extra_args=["--keep-hooks"])
    assert_true(proc_again.returncode == 0, f"{label}: second uninstall should be idempotent when shims are absent: {proc_again.stderr!r}")
    print(f"  PASS  {label}")


def scenario_legacy_wrappers_use_in_tree_agent_bridge(label: str, tmpdir: Path) -> None:
    root = tmpdir / "wrapper-tree"
    marker = tmpdir / "wrapper-marker.txt"
    _write_fake_cli_tree(root, marker)
    sentinel = tmpdir / "sentinel"
    sentinel.mkdir()
    (sentinel / "agent-bridge").write_text(
        "#!/usr/bin/env bash\n"
        "echo sentinel agent-bridge should not run >&2\n"
        "exit 44\n",
        encoding="utf-8",
    )
    os.chmod(sentinel / "agent-bridge", 0o755)
    env = _fake_install_env(tmpdir / "wrapper-env", path_prefix=sentinel)

    run_proc = subprocess.run(
        [str(root / "bin" / "bridge_run.sh"), "-s", "RoomA"],
        input="",
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert_true(run_proc.returncode == 0, f"{label}: bridge_run wrapper should not hit PATH sentinel: {run_proc.stderr!r}")
    assert_true(marker.read_text(encoding="utf-8") == "attach -s RoomA\n", f"{label}: bridge_run should call in-tree dispatcher attach path")

    marker.write_text("", encoding="utf-8")
    manage_proc = subprocess.run(
        [str(root / "bin" / "bridge_manage.sh"), "--status"],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert_true(manage_proc.returncode == 0, f"{label}: bridge_manage wrapper should not hit PATH sentinel: {manage_proc.stderr!r}")
    assert_true(marker.read_text(encoding="utf-8") == "daemon_ctl status --all\n", f"{label}: bridge_manage --status should call all-room status")
    print(f"  PASS  {label}")


def scenario_agent_bridge_attach_and_manage_dispatch_contracts(label: str, tmpdir: Path) -> None:
    root = tmpdir / "dispatch-tree"
    marker = tmpdir / "dispatch-marker.txt"
    _write_fake_cli_tree(root, marker)
    env = _fake_install_env(tmpdir / "dispatch-env")

    attach_proc = subprocess.run(
        [str(root / "bin" / "agent-bridge"), "attach", "-s", "RoomB"],
        input="",
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert_true(attach_proc.returncode == 0, f"{label}: attach -s should succeed: {attach_proc.stderr!r}")
    assert_true(marker.read_text(encoding="utf-8") == "attach -s RoomB\n", f"{label}: attach should reach bridge_attach.py with -s RoomB")

    marker.write_text("", encoding="utf-8")
    status_proc = subprocess.run(
        [str(root / "bin" / "agent-bridge"), "manage", "--status"],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert_true(status_proc.returncode == 0, f"{label}: manage --status should succeed: {status_proc.stderr!r}")
    assert_true(marker.read_text(encoding="utf-8") == "daemon_ctl status --all\n", f"{label}: manage --status should not enter a menu")

    marker.write_text("", encoding="utf-8")
    manage_proc = subprocess.run(
        [str(root / "bin" / "agent-bridge"), "manage", "-s", "RoomC"],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert_true(manage_proc.returncode == 0, f"{label}: manage -s should enter preselected room menu: {manage_proc.stderr!r}")
    marker_text = marker.read_text(encoding="utf-8")
    assert_true("daemon_ctl status --json" not in marker_text, f"{label}: manage -s should skip room selection: {marker_text!r}")
    assert_true("select --title Room: RoomC" in marker_text, f"{label}: manage -s should render room action menu: {marker_text!r}")
    print(f"  PASS  {label}")


def _write_fake_install_tree(root: Path, *, omit: Path | None = None) -> None:
    shutil.copy2(ROOT / "install.sh", root / "install.sh")
    helper = root / "libexec" / "agent-bridge" / "bridge_set_editor_mode.py"
    helper.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "libexec" / "agent-bridge" / "bridge_set_editor_mode.py", helper)
    for _, relative in INSTALL_SHIM_TARGETS:
        if omit is not None and relative == omit:
            continue
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        os.chmod(target, 0o644)
    hook = root / "hooks" / "bridge-hook"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    os.chmod(hook, 0o755)


def _write_fake_python3(fakebin: Path, version: str) -> Path:
    fakebin.mkdir(parents=True, exist_ok=True)
    major_minor = tuple(int(part) for part in version.split(".")[:2])
    gate_exit = 0 if major_minor >= (3, 10) else 1
    path = fakebin / "python3"
    path.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"${1:-}\" == \"-c\" && \"${2:-}\" == *agent_bridge_python_gate_v1* ]]; then\n"
        f"  printf '%s\\n' {shlex.quote(version)}\n"
        "  printf '%s\\n' \"$0\"\n"
        f"  exit {gate_exit}\n"
        "fi\n"
        f"exec {shlex.quote(sys.executable)} \"$@\"\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o755)
    return fakebin


def _assert_python_gate_failure(label: str, proc: subprocess.CompletedProcess) -> None:
    assert_true(proc.returncode != 0, f"{label}: expected Python gate failure")
    assert_true("###################################################################" in proc.stderr, f"{label}: emphasized delimiter missing: {proc.stderr!r}")
    assert_true("Python 3.10" in proc.stderr, f"{label}: Python 3.10 token missing: {proc.stderr!r}")
    assert_true("ERROR: agent-bridge requires Python 3.10 or newer." in proc.stderr, f"{label}: targeted error missing: {proc.stderr!r}")


def _write_fake_healthcheck_tree(root: Path) -> None:
    (root / "bin").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "bin" / "bridge_healthcheck.sh", root / "bin" / "bridge_healthcheck.sh")
    os.chmod(root / "bin" / "bridge_healthcheck.sh", 0o755)
    shutil.copytree(
        LIBEXEC,
        root / "libexec" / "agent-bridge",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    for _, relative in DIRECT_EXECUTABLE_TARGETS:
        target = root / relative
        if relative == Path("bin/bridge_healthcheck.sh"):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        os.chmod(target, 0o755)
    for name in ("bridge_run", "bridge_manage", "bridge_healthcheck"):
        shim = root / "bin" / name
        shim.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        os.chmod(shim, 0o755)


def _write_fake_hook_installer(root: Path, *, exit_code: int = 0, argv_file: Path | None = None) -> Path:
    path = root / "libexec" / "agent-bridge" / "bridge_install_hooks.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    argv_literal = repr(str(argv_file)) if argv_file else "''"
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib\n"
        "import sys\n"
        f"argv_file = {argv_literal}\n"
        "if argv_file:\n"
        "    pathlib.Path(argv_file).parent.mkdir(parents=True, exist_ok=True)\n"
        "    pathlib.Path(argv_file).write_text('\\n'.join(sys.argv[1:]) + '\\n', encoding='utf-8')\n"
        f"raise SystemExit({int(exit_code)})\n",
        encoding="utf-8",
    )
    return path


def _fake_healthcheck_env(tmpdir: Path, root: Path, fakebin: Path) -> dict[str, str]:
    env = _fake_install_env(tmpdir)
    env.pop("AGENT_BRIDGE_HOME", None)
    env.pop("AGENT_BRIDGE" + "_PYTHON", None)
    env["PATH"] = f"{fakebin}:{root / 'bin'}:{root / 'model-bin'}:{env.get('PATH', '')}"
    _write_seed_hook_configs(Path(env["HOME"]))
    return env


def _run_fake_install(
    root: Path,
    bin_dir: Path,
    *,
    env: dict[str, str] | None = None,
    skip_hooks: bool = True,
    yes: bool = True,
    shell_rc: bool = False,
    extra_args: list[str] | None = None,
) -> subprocess.CompletedProcess:
    cmd = [
        "bash",
        str(root / "install.sh"),
        "--bin-dir",
        str(bin_dir),
    ]
    if yes:
        cmd.append("--yes")
    if skip_hooks:
        cmd.append("--skip-hooks")
    if not shell_rc:
        cmd.append("--no-shell-rc")
    cmd.extend(extra_args or [])
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


def _run_fake_healthcheck(root: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(root / "bin" / "bridge_healthcheck.sh")],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


def _run_fake_healthcheck_json(root: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(root / "bin" / "bridge_healthcheck.sh"), "--json"],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


def _run_bridge_set_editor_mode(path: Path, *, dry_run: bool = False, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    cmd = [
        sys.executable,
        str(ROOT / "libexec" / "agent-bridge" / "bridge_set_editor_mode.py"),
        "--path",
        str(path),
    ]
    if dry_run:
        cmd.append("--dry-run")
    env = _fake_install_env(path.parent)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


def scenario_python_env_override_removed_from_tracked_files(label: str, tmpdir: Path) -> None:
    forbidden = "AGENT_BRIDGE" + "_PYTHON"
    proc = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert_true(proc.returncode == 0, f"{label}: git ls-files failed: {proc.stderr!r}")
    hits = []
    for rel in proc.stdout.splitlines():
        path = ROOT / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8", errors="ignore")
        if forbidden in text:
            hits.append(rel)
    assert_true(not hits, f"{label}: forbidden Python override token remains in tracked files: {hits}")
    print(f"  PASS  {label}")


def scenario_install_sh_python_version_gate(label: str, tmpdir: Path) -> None:
    root = tmpdir / "install-python-gate"
    root.mkdir()
    _write_fake_install_tree(root)

    fake39 = _write_fake_python3(tmpdir / "fake-python-39", "3.9.18")
    env39 = _fake_install_env(tmpdir / "env39", path_prefix=fake39)
    proc39 = _run_fake_install(root, tmpdir / "shims39", env=env39)
    _assert_python_gate_failure(f"{label}: python 3.9", proc39)
    assert_true("install shim:" not in proc39.stdout, f"{label}: gate should fail before shim work: {proc39.stdout!r}")

    proc39_dry = _run_fake_install(root, tmpdir / "shims39-dry", env=env39, extra_args=["--dry-run"])
    _assert_python_gate_failure(f"{label}: python 3.9 dry-run", proc39_dry)
    assert_true("install shim:" not in proc39_dry.stdout, f"{label}: dry-run gate should fail before shim work: {proc39_dry.stdout!r}")

    fake310 = _write_fake_python3(tmpdir / "fake-python-310", "3.10.14")
    proc310 = _run_fake_install(root, tmpdir / "shims310", env=_fake_install_env(tmpdir / "env310", path_prefix=fake310))
    assert_true(proc310.returncode == 0, f"{label}: fake Python 3.10 should pass install: {proc310.stderr!r}")
    assert_true(os.access(tmpdir / "shims310" / "agent_send_peer", os.X_OK), f"{label}: install should write shims after passing gate")

    fake311 = _write_fake_python3(tmpdir / "fake-python-311", "3.11.9")
    proc311 = _run_fake_install(
        root,
        tmpdir / "shims311",
        env=_fake_install_env(tmpdir / "env311", path_prefix=fake311),
        extra_args=["--dry-run"],
    )
    assert_true(proc311.returncode == 0, f"{label}: fake Python 3.11 dry-run should pass install: {proc311.stderr!r}")
    assert_true("install shim:" in proc311.stdout, f"{label}: dry-run should reach shim preview after passing gate: {proc311.stdout!r}")
    print(f"  PASS  {label}")


def scenario_bridge_healthcheck_sh_python_version_gate(label: str, tmpdir: Path) -> None:
    root = tmpdir / "healthcheck-python-gate"
    root.mkdir()
    _write_fake_healthcheck_tree(root)

    fake39 = _write_fake_python3(tmpdir / "hc-fake-python-39", "3.9.18")
    proc39 = _run_fake_healthcheck(root, _fake_healthcheck_env(tmpdir / "hc-env39", root, fake39))
    _assert_python_gate_failure(f"{label}: python 3.9", proc39)
    assert_true("ok   install_root" not in proc39.stdout, f"{label}: gate should fail before Python healthcheck output: {proc39.stdout!r}")

    fake310 = _write_fake_python3(tmpdir / "hc-fake-python-310", "3.10.14")
    proc310 = _run_fake_healthcheck(root, _fake_healthcheck_env(tmpdir / "hc-env310", root, fake310))
    assert_true(proc310.returncode == 0, f"{label}: fake Python 3.10 healthcheck should pass: {proc310.stderr!r}")
    assert_true("ok   install_root" in proc310.stdout, f"{label}: healthcheck should run after passing gate: {proc310.stdout!r}")

    fake311 = _write_fake_python3(tmpdir / "hc-fake-python-311", "3.11.9")
    proc311 = _run_fake_healthcheck(root, _fake_healthcheck_env(tmpdir / "hc-env311", root, fake311))
    assert_true(proc311.returncode == 0, f"{label}: fake Python 3.11 healthcheck should pass: {proc311.stderr!r}")
    print(f"  PASS  {label}")


def scenario_bridge_healthcheck_agent_bridge_contracts(label: str, tmpdir: Path) -> None:
    root = tmpdir / "healthcheck-agent-bridge"
    root.mkdir()
    _write_fake_healthcheck_tree(root)
    fake311 = _write_fake_python3(tmpdir / "hc-agent-bridge-python", "3.11.9")
    env = _fake_healthcheck_env(tmpdir / "hc-agent-bridge-env", root, fake311)

    proc = _run_fake_healthcheck_json(root, env)
    assert_true(proc.returncode == 0, f"{label}: healthcheck should pass with agent-bridge command: {proc.stderr!r}")
    checks = {item["name"]: item for item in json.loads(proc.stdout)}
    for name in ("agent_bridge_on_path", "agent_bridge_target", "agent_bridge_underscore_not_on_path"):
        assert_true(name in checks, f"{label}: missing healthcheck row {name!r}: {checks}")
        assert_true(checks[name]["ok"], f"{label}: {name} should pass: {checks[name]}")

    underscore = fake311 / "agent_bridge"
    underscore.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    os.chmod(underscore, 0o755)
    failed = _run_fake_healthcheck_json(root, env)
    assert_true(failed.returncode == 1, f"{label}: underscore command collision should hard-fail healthcheck")
    failed_checks = {item["name"]: item for item in json.loads(failed.stdout)}
    assert_true(
        not failed_checks["agent_bridge_underscore_not_on_path"]["ok"],
        f"{label}: underscore collision row should fail: {failed_checks['agent_bridge_underscore_not_on_path']}",
    )
    print(f"  PASS  {label}")


def scenario_install_sh_chmods_target_or_fails(label: str, tmpdir: Path) -> None:
    positive_root = tmpdir / "install-positive"
    positive_root.mkdir()
    _write_fake_install_tree(positive_root)
    agent_bridge_target = positive_root / "bin" / "agent-bridge"
    alarm_target = positive_root / "model-bin" / "agent_alarm"
    assert_true(not os.access(agent_bridge_target, os.X_OK), f"{label}: precondition agent-bridge target starts non-executable")
    assert_true(not os.access(alarm_target, os.X_OK), f"{label}: precondition target starts non-executable")
    proc = _run_fake_install(positive_root, tmpdir / "shims-positive")
    assert_true(proc.returncode == 0, f"{label}: install should recover non-executable targets, got {proc.returncode}: {proc.stderr}")
    assert_true(os.access(agent_bridge_target, os.X_OK), f"{label}: install should chmod agent-bridge target executable")
    assert_true(os.access(alarm_target, os.X_OK), f"{label}: install should chmod shim target executable")
    assert_true(os.access(tmpdir / "shims-positive" / "agent-bridge", os.X_OK), f"{label}: agent-bridge shim itself should be executable")
    assert_true(os.access(tmpdir / "shims-positive" / "agent_alarm", os.X_OK), f"{label}: shim itself should be executable")

    missing_agent_bridge_root = tmpdir / "install-missing-agent-bridge"
    missing_agent_bridge_root.mkdir()
    _write_fake_install_tree(missing_agent_bridge_root, omit=Path("bin/agent-bridge"))
    proc = _run_fake_install(missing_agent_bridge_root, tmpdir / "shims-missing-agent-bridge")
    assert_true(proc.returncode != 0, f"{label}: missing shim target must hard fail")
    assert_true("missing shim target for agent-bridge" in proc.stderr, f"{label}: missing-target stderr should name shim: {proc.stderr!r}")

    missing_alarm_root = tmpdir / "install-missing-agent-alarm"
    missing_alarm_root.mkdir()
    _write_fake_install_tree(missing_alarm_root, omit=Path("model-bin/agent_alarm"))
    proc = _run_fake_install(missing_alarm_root, tmpdir / "shims-missing-agent-alarm")
    assert_true(proc.returncode != 0, f"{label}: missing model shim target must hard fail")
    assert_true("missing shim target for agent_alarm" in proc.stderr, f"{label}: missing model-target stderr should name shim: {proc.stderr!r}")

    failing_agent_bridge_root = tmpdir / "install-failing-agent-bridge"
    failing_agent_bridge_root.mkdir()
    _write_fake_install_tree(failing_agent_bridge_root)
    fakebin_agent_bridge = tmpdir / "fakebin-agent-bridge"
    fakebin_agent_bridge.mkdir()
    fake_chmod_agent_bridge = fakebin_agent_bridge / "chmod"
    fake_chmod_agent_bridge.write_text(
        "#!/usr/bin/env bash\n"
        "case \"$*\" in\n"
        "  *bin/agent-bridge*) echo fake chmod failure >&2; exit 42 ;;\n"
        "esac\n"
        "exec /bin/chmod \"$@\"\n",
        encoding="utf-8",
    )
    os.chmod(fake_chmod_agent_bridge, 0o755)
    env = dict(os.environ)
    env["PATH"] = f"{fakebin_agent_bridge}:{env.get('PATH', '')}"
    proc = _run_fake_install(failing_agent_bridge_root, tmpdir / "shims-failing-agent-bridge", env=env)
    assert_true(proc.returncode != 0, f"{label}: chmod failure must hard fail")
    assert_true("cannot make shim target executable for agent-bridge" in proc.stderr, f"{label}: chmod failure stderr should name shim: {proc.stderr!r}")
    assert_true(not os.access(failing_agent_bridge_root / "bin" / "agent-bridge", os.X_OK), f"{label}: failed chmod target should remain non-executable")

    failing_alarm_root = tmpdir / "install-failing-agent-alarm"
    failing_alarm_root.mkdir()
    _write_fake_install_tree(failing_alarm_root)
    fakebin_alarm = tmpdir / "fakebin-agent-alarm"
    fakebin_alarm.mkdir()
    fake_chmod_alarm = fakebin_alarm / "chmod"
    fake_chmod_alarm.write_text(
        "#!/usr/bin/env bash\n"
        "case \"$*\" in\n"
        "  *model-bin/agent_alarm*) echo fake chmod failure >&2; exit 42 ;;\n"
        "esac\n"
        "exec /bin/chmod \"$@\"\n",
        encoding="utf-8",
    )
    os.chmod(fake_chmod_alarm, 0o755)
    env = dict(os.environ)
    env["PATH"] = f"{fakebin_alarm}:{env.get('PATH', '')}"
    proc = _run_fake_install(failing_alarm_root, tmpdir / "shims-failing-agent-alarm", env=env)
    assert_true(proc.returncode != 0, f"{label}: model chmod failure must hard fail")
    assert_true("cannot make shim target executable for agent_alarm" in proc.stderr, f"{label}: model chmod failure stderr should name shim: {proc.stderr!r}")
    assert_true(not os.access(failing_alarm_root / "model-bin" / "agent_alarm", os.X_OK), f"{label}: failed model target should remain non-executable")
    print(f"  PASS  {label}")


def _fake_uname(tmpdir: Path, value: str) -> Path:
    fakebin = tmpdir / f"fake-uname-{value.lower()}"
    fakebin.mkdir(parents=True, exist_ok=True)
    uname = fakebin / "uname"
    uname.write_text(f"#!/usr/bin/env bash\nprintf '%s\\n' {shlex.quote(value)}\n", encoding="utf-8")
    os.chmod(uname, 0o755)
    return fakebin


def _agent_bridge_path_block(bin_dir: Path) -> str:
    return (
        "# >>> Agent Bridge >>>\n"
        f"export PATH={shlex.quote(str(bin_dir))}:\"$PATH\"\n"
        "# <<< Agent Bridge <<<\n"
    )


def scenario_install_sh_default_updates_shell_rc(label: str, tmpdir: Path) -> None:
    root = tmpdir / "install-rc-default"
    root.mkdir()
    _write_fake_install_tree(root)
    bin_dir = tmpdir / "shims-rc-default"
    env = _fake_install_env(tmpdir)
    proc = _run_fake_install(root, bin_dir, env=env, yes=False, shell_rc=True)
    assert_true(proc.returncode == 0, f"{label}: default install should succeed: stdout={proc.stdout!r} stderr={proc.stderr!r}")
    rc = Path(env["HOME"]) / ".bashrc"
    assert_true(rc.exists(), f"{label}: bash on Linux should write .bashrc")
    text = rc.read_text(encoding="utf-8")
    block = _agent_bridge_path_block(bin_dir)
    assert_true(block in text, f"{label}: rc should contain marked PATH block: {text!r}")
    assert_true(text.count("# >>> Agent Bridge >>>") == 1, f"{label}: marker should appear once: {text!r}")
    assert_true("permission mode note" in proc.stdout, f"{label}: install should print permission mode note: {proc.stdout!r}")
    assert_true("--permission-mode bypassPermissions" in proc.stdout, f"{label}: Claude permission flag should be shown: {proc.stdout!r}")
    assert_true("--dangerously-bypass-approvals-and-sandbox" in proc.stdout, f"{label}: Codex permission flag should be shown: {proc.stdout!r}")
    assert_true(not list(rc.parent.glob(".bashrc.agent-bridge.bak.*")), f"{label}: no backup needed for newly-created rc")

    proc2 = _run_fake_install(root, bin_dir, env=env, yes=False, shell_rc=True)
    assert_true(proc2.returncode == 0, f"{label}: repeated install should succeed: {proc2.stderr!r}")
    text2 = rc.read_text(encoding="utf-8")
    assert_true(text2.count("# >>> Agent Bridge >>>") == 1, f"{label}: repeated install must not duplicate marker: {text2!r}")
    assert_true(not list(rc.parent.glob(".bashrc.agent-bridge.bak.*")), f"{label}: repeated no-op should not create backup")
    print(f"  PASS  {label}")


def scenario_install_sh_shell_rc_dry_run_and_opt_out(label: str, tmpdir: Path) -> None:
    root = tmpdir / "install-rc-dry"
    root.mkdir()
    _write_fake_install_tree(root)
    bin_dir = tmpdir / "shims-rc-dry"
    env = _fake_install_env(tmpdir)
    rc = Path(env["HOME"]) / ".bashrc"

    proc_dry = _run_fake_install(root, bin_dir, env=env, yes=False, shell_rc=True, extra_args=["--dry-run"])
    assert_true(proc_dry.returncode == 0, f"{label}: dry-run should succeed: {proc_dry.stderr!r}")
    assert_true("add PATH block to" in proc_dry.stdout, f"{label}: dry-run should show planned rc edit: {proc_dry.stdout!r}")
    assert_true(not rc.exists(), f"{label}: dry-run must not create rc file")

    proc_no_rc = _run_fake_install(root, bin_dir, env=env, yes=False, shell_rc=False)
    assert_true(proc_no_rc.returncode == 0, f"{label}: --no-shell-rc should succeed: {proc_no_rc.stderr!r}")
    assert_true("PATH note: add this block" in proc_no_rc.stdout, f"{label}: opt-out should print PATH note: {proc_no_rc.stdout!r}")
    assert_true("# >>> Agent Bridge >>>" in proc_no_rc.stdout, f"{label}: opt-out note should include paste-ready marker block: {proc_no_rc.stdout!r}")
    assert_true(not rc.exists(), f"{label}: --no-shell-rc must not create rc file")

    proc_yes_no_rc = _run_fake_install(root, bin_dir, env=env, yes=True, shell_rc=False)
    assert_true(proc_yes_no_rc.returncode == 0, f"{label}: --yes --no-shell-rc should succeed: {proc_yes_no_rc.stderr!r}")
    assert_true(not rc.exists(), f"{label}: --no-shell-rc must win over --yes")
    print(f"  PASS  {label}")


def scenario_install_sh_shell_rc_target_selection(label: str, tmpdir: Path) -> None:
    root = tmpdir / "install-rc-targets"
    root.mkdir()
    _write_fake_install_tree(root)

    env_zsh = _fake_install_env(tmpdir / "zsh")
    env_zsh["SHELL"] = "/bin/zsh"
    proc_zsh = _run_fake_install(root, tmpdir / "shims-zsh", env=env_zsh, yes=False, shell_rc=True)
    assert_true(proc_zsh.returncode == 0, f"{label}: zsh install should succeed: {proc_zsh.stderr!r}")
    assert_true((Path(env_zsh["HOME"]) / ".zshrc").exists(), f"{label}: zsh should target .zshrc")

    env_other = _fake_install_env(tmpdir / "other")
    env_other["SHELL"] = "/usr/bin/fish"
    proc_other = _run_fake_install(root, tmpdir / "shims-other", env=env_other, yes=False, shell_rc=True)
    assert_true(proc_other.returncode == 0, f"{label}: other shell install should succeed: {proc_other.stderr!r}")
    assert_true((Path(env_other["HOME"]) / ".profile").exists(), f"{label}: unknown shell should target .profile")

    env_darwin = _fake_install_env(tmpdir / "darwin", path_prefix=_fake_uname(tmpdir, "Darwin"))
    env_darwin["SHELL"] = "/bin/bash"
    proc_darwin = _run_fake_install(root, tmpdir / "shims-darwin", env=env_darwin, yes=False, shell_rc=True)
    assert_true(proc_darwin.returncode == 0, f"{label}: Darwin bash install should succeed: {proc_darwin.stderr!r}")
    assert_true((Path(env_darwin["HOME"]) / ".bash_profile").exists(), f"{label}: Darwin bash should target .bash_profile")
    assert_true(not (Path(env_darwin["HOME"]) / ".bashrc").exists(), f"{label}: Darwin bash should not write .bashrc")
    print(f"  PASS  {label}")


def scenario_install_sh_shell_rc_backup_and_path_short_circuit(label: str, tmpdir: Path) -> None:
    root = tmpdir / "install-rc-backup"
    root.mkdir()
    _write_fake_install_tree(root)
    bin_dir = tmpdir / "shims-rc-backup"
    env = _fake_install_env(tmpdir)
    home = Path(env["HOME"])
    home.mkdir(parents=True, exist_ok=True)
    rc = home / ".bashrc"
    rc.write_text("existing config\n", encoding="utf-8")
    proc = _run_fake_install(root, bin_dir, env=env, yes=False, shell_rc=True)
    assert_true(proc.returncode == 0, f"{label}: install should succeed: {proc.stderr!r}")
    backups = list(home.glob(".bashrc.agent-bridge.bak.*"))
    assert_true(len(backups) == 1, f"{label}: exactly one backup expected, got {backups}")
    assert_true(backups[0].read_text(encoding="utf-8") == "existing config\n", f"{label}: backup should preserve original rc")
    assert_true(_agent_bridge_path_block(bin_dir) in rc.read_text(encoding="utf-8"), f"{label}: rc should get path block")

    env_path_ok = _fake_install_env(tmpdir / "path-ok", path_prefix=tmpdir / "already-on-path")
    bin_on_path = tmpdir / "already-on-path"
    proc_path_ok = _run_fake_install(root, bin_on_path, env=env_path_ok, yes=False, shell_rc=True)
    assert_true(proc_path_ok.returncode == 0, f"{label}: PATH-ok install should succeed: {proc_path_ok.stderr!r}")
    assert_true(not (Path(env_path_ok["HOME"]) / ".bashrc").exists(), f"{label}: bin_dir already on PATH should not write rc")
    assert_true("add PATH block" not in proc_path_ok.stdout, f"{label}: PATH-ok install should not announce rc edit: {proc_path_ok.stdout!r}")
    print(f"  PASS  {label}")


def scenario_install_sh_shell_quotes_bin_dir_metacharacters(label: str, tmpdir: Path) -> None:
    root = tmpdir / "install-rc-quote"
    root.mkdir()
    _write_fake_install_tree(root)
    raw = "bin with spaces 'quotes' \"dbl\" $(touch pwn) `uname`\nnext"
    bin_dir = tmpdir / raw
    env = _fake_install_env(tmpdir)
    proc = _run_fake_install(root, bin_dir, env=env, yes=False, shell_rc=True)
    assert_true(proc.returncode == 0, f"{label}: install should accept metacharacter bin_dir: stdout={proc.stdout!r} stderr={proc.stderr!r}")
    rc = Path(env["HOME"]) / ".bashrc"
    text = rc.read_text(encoding="utf-8")
    prefix = "export PATH="
    suffix = ":\"$PATH\""
    start = text.find(prefix)
    finish = text.find(suffix, start)
    assert_true(start >= 0 and finish >= 0, f"{label}: rc should contain export assignment: {text!r}")
    rhs = text[start + len(prefix): finish + len(suffix)]
    parsed = shlex.split(rhs)[0]
    assert_true(parsed == f"{bin_dir}:$PATH", f"{label}: quoted export should parse back to literal bin_dir plus PATH: parsed={parsed!r} text={text!r}")
    assert_true(f"export PATH={shlex.quote(str(bin_dir))}:\"$PATH\"" in text, f"{label}: rc should shell-quote bin_dir: {text!r}")
    assert_true('export PATH="' not in text, f"{label}: rc must not use unsafe double-quoted bin_dir form: {text!r}")
    print(f"  PASS  {label}")


def scenario_install_sh_shell_rc_replaces_existing_marker_block(label: str, tmpdir: Path) -> None:
    root = tmpdir / "install-rc-replace"
    root.mkdir()
    _write_fake_install_tree(root)
    env = _fake_install_env(tmpdir)
    bin_a = tmpdir / "bin-a"
    bin_b = tmpdir / "bin b"
    proc_a = _run_fake_install(root, bin_a, env=env, yes=False, shell_rc=True)
    assert_true(proc_a.returncode == 0, f"{label}: first install should succeed: {proc_a.stderr!r}")
    proc_b = _run_fake_install(root, bin_b, env=env, yes=False, shell_rc=True)
    assert_true(proc_b.returncode == 0, f"{label}: second install should succeed: {proc_b.stderr!r}")
    rc = Path(env["HOME"]) / ".bashrc"
    text = rc.read_text(encoding="utf-8")
    assert_true(text.count("# >>> Agent Bridge >>>") == 1 and text.count("# <<< Agent Bridge <<<") == 1, f"{label}: marker block should be replaced, not appended: {text!r}")
    assert_true(str(bin_a) not in text, f"{label}: old bin_dir should be removed from managed block: {text!r}")
    assert_true(f"export PATH={shlex.quote(str(bin_b))}:\"$PATH\"" in text, f"{label}: new bin_dir should be present and quoted: {text!r}")
    print(f"  PASS  {label}")


def _claude_backups(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.agent-bridge.bak.*"))


def _claude_temps(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f".{path.name}.agent-bridge.tmp.*"))


def _assert_ordered_substrings(label: str, text: str, *needles: str) -> None:
    cursor = -1
    for needle in needles:
        index = text.find(needle, cursor + 1)
        assert_true(index >= 0, f"{label}: missing ordered stdout substring {needle!r}: {text!r}")
        assert_true(index > cursor, f"{label}: stdout substring {needle!r} is out of order: {text!r}")
        cursor = index


def scenario_install_sh_claude_editor_mode_missing_skips(label: str, tmpdir: Path) -> None:
    root = tmpdir / "claude-mode-missing"
    root.mkdir()
    _write_fake_install_tree(root)
    env = _fake_install_env(tmpdir)
    home = Path(env["HOME"])

    proc = _run_fake_install(root, tmpdir / "shims-claude-mode-missing", env=env)
    assert_true(proc.returncode == 0, f"{label}: missing Claude config should not fail install: {proc.stderr!r}")
    assert_true(not (home / ".claude.json").exists(), f"{label}: missing Claude config must not be created")
    assert_true(not (home / ".claude" / "settings.json").exists(), f"{label}: missing Claude settings must not be created")
    _assert_ordered_substrings(
        label,
        proc.stdout,
        f"skip: Claude Code config absent at {home / '.claude.json'}",
        f"skip: Claude Code config absent at {home / '.claude' / 'settings.json'}",
    )
    print(f"  PASS  {label}")


def scenario_install_sh_claude_editor_mode_updates_with_backup(label: str, tmpdir: Path) -> None:
    root = tmpdir / "claude-mode-update"
    root.mkdir()
    _write_fake_install_tree(root)
    env = _fake_install_env(tmpdir)
    home = Path(env["HOME"])
    home.mkdir(parents=True, exist_ok=True)
    config = home / ".claude.json"
    original = b'{\n  "theme": "light",\n  "editorMode": "vim",\n  "nested": {\n    "keep": true\n  }\n}'
    config.write_bytes(original)

    proc = _run_fake_install(root, tmpdir / "shims-claude-mode-update", env=env)
    assert_true(proc.returncode == 0, f"{label}: install should update vim editorMode: stdout={proc.stdout!r} stderr={proc.stderr!r}")
    data = json.loads(config.read_text(encoding="utf-8"))
    assert_true(data.get("editorMode") == "normal", f"{label}: editorMode should be normal: {data}")
    assert_true(list(data) == ["theme", "editorMode", "nested"], f"{label}: root key order should be preserved: {list(data)}")
    assert_true(data.get("nested") == {"keep": True}, f"{label}: nested fields should be preserved: {data}")
    assert_true(not config.read_bytes().endswith(b"\n"), f"{label}: original missing trailing newline should stay missing")
    backups = _claude_backups(config)
    assert_true(len(backups) == 1, f"{label}: exactly one Claude config backup expected, got {backups}")
    assert_true(re.match(r"^\.claude\.json\.agent-bridge\.bak\.\d{14}\.\d+$", backups[0].name), f"{label}: backup name should match convention: {backups[0].name}")
    assert_true(backups[0].read_bytes() == original, f"{label}: backup must preserve original bytes")
    assert_true(not _claude_temps(config), f"{label}: no temp files should remain after update")
    assert_true("set Claude Code editorMode=normal for reliable bridge prompt paste" in proc.stdout, f"{label}: update reason should be announced: {proc.stdout!r}")
    assert_true('(was "vim")' in proc.stdout, f"{label}: update should show previous value: {proc.stdout!r}")
    assert_true("JSON whitespace/escape style normalized" in proc.stdout, f"{label}: normalization should be disclosed: {proc.stdout!r}")
    print(f"  PASS  {label}")


def scenario_install_sh_claude_editor_mode_updates_global_and_settings(label: str, tmpdir: Path) -> None:
    root = tmpdir / "claude-mode-two-files"
    root.mkdir()
    _write_fake_install_tree(root)
    env = _fake_install_env(tmpdir)
    home = Path(env["HOME"])
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    global_config = home / ".claude.json"
    settings_config = claude_dir / "settings.json"
    global_original = b'{\n  "editorMode": "vim",\n  "global": true\n}\n'
    settings_original = b'{\n  "editorMode": "vim",\n  "settings": true\n}\n'
    global_config.write_bytes(global_original)
    settings_config.write_bytes(settings_original)

    proc = _run_fake_install(root, tmpdir / "shims-claude-mode-two-files", env=env)
    assert_true(proc.returncode == 0, f"{label}: install should update both Claude config files: stdout={proc.stdout!r} stderr={proc.stderr!r}")
    global_data = json.loads(global_config.read_text(encoding="utf-8"))
    settings_data = json.loads(settings_config.read_text(encoding="utf-8"))
    assert_true(global_data.get("editorMode") == "normal", f"{label}: ~/.claude.json should be normal: {global_data}")
    assert_true(settings_data.get("editorMode") == "normal", f"{label}: ~/.claude/settings.json should be normal: {settings_data}")
    global_backups = _claude_backups(global_config)
    settings_backups = _claude_backups(settings_config)
    assert_true(len(global_backups) == 1 and global_backups[0].read_bytes() == global_original, f"{label}: global backup should preserve original: {global_backups}")
    assert_true(len(settings_backups) == 1 and settings_backups[0].read_bytes() == settings_original, f"{label}: settings backup should preserve original: {settings_backups}")
    assert_true(not _claude_temps(global_config), f"{label}: no global temp files should remain")
    assert_true(not _claude_temps(settings_config), f"{label}: no settings temp files should remain")
    _assert_ordered_substrings(label, proc.stdout, str(global_config), str(settings_config))
    print(f"  PASS  {label}")


def scenario_install_sh_claude_editor_mode_missing_global_updates_settings(label: str, tmpdir: Path) -> None:
    root = tmpdir / "claude-mode-mixed-absent"
    root.mkdir()
    _write_fake_install_tree(root)
    env = _fake_install_env(tmpdir)
    home = Path(env["HOME"])
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    global_config = home / ".claude.json"
    settings_config = claude_dir / "settings.json"
    settings_original = b'{\n  "editorMode": "vim",\n  "settings": true\n}\n'
    settings_config.write_bytes(settings_original)

    proc = _run_fake_install(root, tmpdir / "shims-claude-mode-mixed-absent", env=env)
    assert_true(proc.returncode == 0, f"{label}: missing global should not prevent settings update: stdout={proc.stdout!r} stderr={proc.stderr!r}")
    assert_true(not global_config.exists(), f"{label}: missing global config must not be created")
    settings_data = json.loads(settings_config.read_text(encoding="utf-8"))
    assert_true(settings_data.get("editorMode") == "normal", f"{label}: settings should be normal: {settings_data}")
    settings_backups = _claude_backups(settings_config)
    assert_true(len(settings_backups) == 1 and settings_backups[0].read_bytes() == settings_original, f"{label}: settings backup should preserve original: {settings_backups}")
    _assert_ordered_substrings(
        label,
        proc.stdout,
        f"skip: Claude Code config absent at {global_config}",
        f"set Claude Code editorMode=normal for reliable bridge prompt paste in {settings_config}",
    )
    print(f"  PASS  {label}")


def scenario_install_sh_claude_editor_mode_invalid_global_still_updates_settings(label: str, tmpdir: Path) -> None:
    root = tmpdir / "claude-mode-cross-file"
    root.mkdir()
    _write_fake_install_tree(root)
    env = _fake_install_env(tmpdir)
    home = Path(env["HOME"])
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    global_config = home / ".claude.json"
    settings_config = claude_dir / "settings.json"
    global_original = b'{"editorMode": '
    settings_original = b'{\n  "editorMode": "vim",\n  "settings": true\n}\n'
    global_config.write_bytes(global_original)
    settings_config.write_bytes(settings_original)

    proc = _run_fake_install(root, tmpdir / "shims-claude-mode-cross-file", env=env)
    assert_true(proc.returncode == 0, f"{label}: invalid global should warn-skip and continue: stdout={proc.stdout!r} stderr={proc.stderr!r}")
    assert_true(global_config.read_bytes() == global_original, f"{label}: invalid global bytes must be unchanged")
    assert_true(not _claude_backups(global_config), f"{label}: invalid global should not create backup")
    settings_data = json.loads(settings_config.read_text(encoding="utf-8"))
    assert_true(settings_data.get("editorMode") == "normal", f"{label}: settings should be normal: {settings_data}")
    settings_backups = _claude_backups(settings_config)
    assert_true(len(settings_backups) == 1 and settings_backups[0].read_bytes() == settings_original, f"{label}: settings backup should preserve original: {settings_backups}")
    assert_true("WARNING:" in proc.stderr and str(global_config) in proc.stderr and "cannot parse Claude Code config JSON" in proc.stderr, f"{label}: global parse warning expected: {proc.stderr!r}")
    _assert_ordered_substrings(
        label,
        proc.stdout,
        f"skip: Claude Code config parse failed at {global_config}",
        f"set Claude Code editorMode=normal for reliable bridge prompt paste in {settings_config}",
    )
    print(f"  PASS  {label}")


def scenario_install_sh_claude_editor_mode_normal_noop(label: str, tmpdir: Path) -> None:
    root = tmpdir / "claude-mode-normal"
    root.mkdir()
    _write_fake_install_tree(root)
    env = _fake_install_env(tmpdir)
    home = Path(env["HOME"])
    home.mkdir(parents=True, exist_ok=True)
    config = home / ".claude.json"
    original = b'{\n  "theme": "light",\n  "editorMode": "normal"\n}\n'
    config.write_bytes(original)

    proc = _run_fake_install(root, tmpdir / "shims-claude-mode-normal", env=env)
    assert_true(proc.returncode == 0, f"{label}: normal editorMode should not fail install: {proc.stderr!r}")
    assert_true(config.read_bytes() == original, f"{label}: already-normal config must be byte-unchanged")
    assert_true(not _claude_backups(config), f"{label}: no backup expected for no-op")
    assert_true("skip: Claude Code editorMode already normal" in proc.stdout, f"{label}: no-op should print skip message: {proc.stdout!r}")
    print(f"  PASS  {label}")


def scenario_install_sh_claude_editor_mode_absent_adds_root(label: str, tmpdir: Path) -> None:
    root = tmpdir / "claude-mode-add"
    root.mkdir()
    _write_fake_install_tree(root)
    env = _fake_install_env(tmpdir)
    home = Path(env["HOME"])
    home.mkdir(parents=True, exist_ok=True)
    config = home / ".claude.json"
    original = b'{\n  "theme": "light",\n  "nested": {\n    "keep": true\n  }\n}\n'
    config.write_bytes(original)

    proc = _run_fake_install(root, tmpdir / "shims-claude-mode-add", env=env)
    assert_true(proc.returncode == 0, f"{label}: missing editorMode key should be added: {proc.stderr!r}")
    text = config.read_text(encoding="utf-8")
    data = json.loads(text)
    assert_true(data.get("editorMode") == "normal", f"{label}: editorMode should be added at root: {data}")
    assert_true(list(data) == ["theme", "nested", "editorMode"], f"{label}: added root key should append without reordering existing keys: {list(data)}")
    assert_true(text.endswith("\n"), f"{label}: original trailing newline should be preserved")
    backups = _claude_backups(config)
    assert_true(len(backups) == 1 and backups[0].read_bytes() == original, f"{label}: add should create exact backup: {backups}")
    assert_true("add Claude Code editorMode=normal" in proc.stdout, f"{label}: add should be announced: {proc.stdout!r}")
    print(f"  PASS  {label}")


def scenario_install_sh_claude_editor_mode_invalid_json_skips(label: str, tmpdir: Path) -> None:
    root = tmpdir / "claude-mode-invalid"
    root.mkdir()
    _write_fake_install_tree(root)
    env = _fake_install_env(tmpdir)
    home = Path(env["HOME"])
    home.mkdir(parents=True, exist_ok=True)
    config = home / ".claude.json"
    original = b'{"editorMode": '
    config.write_bytes(original)

    proc = _run_fake_install(root, tmpdir / "shims-claude-mode-invalid", env=env)
    assert_true(proc.returncode == 0, f"{label}: invalid Claude JSON should warn and continue: {proc.stderr!r}")
    assert_true(config.read_bytes() == original, f"{label}: invalid JSON must not be overwritten")
    assert_true(not _claude_backups(config), f"{label}: invalid JSON should not create backup")
    assert_true("WARNING:" in proc.stderr and "cannot parse Claude Code config JSON" in proc.stderr, f"{label}: parse warning expected: {proc.stderr!r}")
    assert_true("skip: Claude Code config parse failed" in proc.stdout, f"{label}: parse skip stdout expected: {proc.stdout!r}")
    print(f"  PASS  {label}")


def scenario_install_sh_claude_editor_mode_invalid_utf8_skips(label: str, tmpdir: Path) -> None:
    config = tmpdir / ".claude.json"
    original = b"\xff\xfe{\x00"
    config.write_bytes(original)

    proc = _run_bridge_set_editor_mode(config)
    assert_true(proc.returncode == 0, f"{label}: invalid UTF-8 should warn and continue: {proc.stderr!r}")
    assert_true(config.read_bytes() == original, f"{label}: invalid UTF-8 bytes must not be overwritten")
    assert_true(not _claude_backups(config), f"{label}: invalid UTF-8 should not create backup")
    assert_true("WARNING:" in proc.stderr and "cannot read Claude Code config as UTF-8" in proc.stderr, f"{label}: UTF-8 warning expected: {proc.stderr!r}")
    assert_true("skip: Claude Code config unreadable" in proc.stdout, f"{label}: UTF-8 skip stdout expected: {proc.stdout!r}")
    print(f"  PASS  {label}")


def scenario_install_sh_claude_editor_mode_bom_skips(label: str, tmpdir: Path) -> None:
    config = tmpdir / ".claude.json"
    original = b'\xef\xbb\xbf{\n  "editorMode": "vim"\n}\n'
    config.write_bytes(original)

    proc = _run_bridge_set_editor_mode(config)
    assert_true(proc.returncode == 0, f"{label}: BOM JSON should warn and continue: {proc.stderr!r}")
    assert_true(config.read_bytes() == original, f"{label}: BOM JSON must not be rewritten")
    assert_true(not _claude_backups(config), f"{label}: BOM JSON should not create backup")
    assert_true("WARNING:" in proc.stderr and "cannot parse Claude Code config JSON" in proc.stderr, f"{label}: BOM should stay strict JSON parse warning: {proc.stderr!r}")
    assert_true("skip: Claude Code config parse failed" in proc.stdout, f"{label}: BOM parse skip stdout expected: {proc.stdout!r}")
    print(f"  PASS  {label}")


def scenario_install_sh_claude_editor_mode_non_object_skips(label: str, tmpdir: Path) -> None:
    root = tmpdir / "claude-mode-non-object"
    root.mkdir()
    _write_fake_install_tree(root)
    env = _fake_install_env(tmpdir)
    home = Path(env["HOME"])
    home.mkdir(parents=True, exist_ok=True)
    config = home / ".claude.json"
    original = b'["editorMode", "vim"]\n'
    config.write_bytes(original)

    proc = _run_fake_install(root, tmpdir / "shims-claude-mode-non-object", env=env)
    assert_true(proc.returncode == 0, f"{label}: non-object Claude JSON should warn and continue: {proc.stderr!r}")
    assert_true(config.read_bytes() == original, f"{label}: non-object JSON must not be overwritten")
    assert_true(not _claude_backups(config), f"{label}: non-object JSON should not create backup")
    assert_true("WARNING:" in proc.stderr and "root is not an object" in proc.stderr, f"{label}: non-object warning expected: {proc.stderr!r}")
    assert_true("skip: Claude Code config root is not an object" in proc.stdout, f"{label}: non-object skip stdout expected: {proc.stdout!r}")
    print(f"  PASS  {label}")


def scenario_install_sh_claude_editor_mode_dry_run_no_write(label: str, tmpdir: Path) -> None:
    root = tmpdir / "claude-mode-dry-run"
    root.mkdir()
    _write_fake_install_tree(root)
    env = _fake_install_env(tmpdir)
    home = Path(env["HOME"])
    home.mkdir(parents=True, exist_ok=True)
    config = home / ".claude.json"
    original = b'{\n  "editorMode": "vim"\n}\n'
    config.write_bytes(original)

    proc = _run_fake_install(root, tmpdir / "shims-claude-mode-dry-run", env=env, extra_args=["--dry-run"])
    assert_true(proc.returncode == 0, f"{label}: dry-run Claude editorMode update should succeed: {proc.stderr!r}")
    assert_true(config.read_bytes() == original, f"{label}: dry-run must not update Claude config")
    assert_true(not _claude_backups(config), f"{label}: dry-run must not create backup")
    assert_true(not _claude_temps(config), f"{label}: dry-run must not leave temp files")
    assert_true("dry-run: would set Claude Code editorMode=normal" in proc.stdout, f"{label}: dry-run should preview update: {proc.stdout!r}")
    print(f"  PASS  {label}")


def scenario_install_sh_claude_editor_mode_preserves_mode(label: str, tmpdir: Path) -> None:
    config = tmpdir / ".claude.json"
    config.write_text('{\n  "editorMode": "vim"\n}\n', encoding="utf-8")
    os.chmod(config, 0o600)

    proc = _run_bridge_set_editor_mode(config)
    assert_true(proc.returncode == 0, f"{label}: helper should update config: {proc.stderr!r}")
    mode = config.stat().st_mode & 0o777
    assert_true(mode == 0o600, f"{label}: helper should preserve file mode 0600, got {oct(mode)}")
    print(f"  PASS  {label}")


def scenario_install_sh_claude_editor_mode_symlink_preserved(label: str, tmpdir: Path) -> None:
    target_dir = tmpdir / "dotfiles"
    target_dir.mkdir()
    target = target_dir / "claude.json"
    target.write_text('{\n  "editorMode": "vim",\n  "name": "keep"\n}\n', encoding="utf-8")
    link = tmpdir / ".claude.json"
    link.symlink_to(target)
    original = target.read_bytes()

    proc = _run_bridge_set_editor_mode(link)
    assert_true(proc.returncode == 0, f"{label}: helper should update symlink target: {proc.stderr!r}")
    assert_true(link.is_symlink(), f"{label}: ~/.claude.json symlink object must be preserved")
    data = json.loads(target.read_text(encoding="utf-8"))
    assert_true(data.get("editorMode") == "normal" and data.get("name") == "keep", f"{label}: symlink target should be updated semantically: {data}")
    backups = _claude_backups(target)
    assert_true(len(backups) == 1 and backups[0].read_bytes() == original, f"{label}: backup should be taken beside resolved target: {backups}")
    assert_true(not _claude_backups(link), f"{label}: link directory should not receive backup for resolved target")
    print(f"  PASS  {label}")


def scenario_install_sh_claude_editor_mode_nested_root_absent_warns_skips(label: str, tmpdir: Path) -> None:
    config = tmpdir / ".claude.json"
    original = b'{\n  "settings": {\n    "editorMode": "vim"\n  }\n}\n'
    config.write_bytes(original)

    proc = _run_bridge_set_editor_mode(config)
    assert_true(proc.returncode == 0, f"{label}: nested-only editorMode should be warning-skip: {proc.stderr!r}")
    assert_true(config.read_bytes() == original, f"{label}: nested-only editorMode must not be overwritten")
    assert_true(not _claude_backups(config), f"{label}: nested-only skip should not create backup")
    assert_true("WARNING:" in proc.stderr and "$.settings.editorMode" in proc.stderr, f"{label}: nested warning should name path: {proc.stderr!r}")
    assert_true("skip: root editorMode absent and nested editorMode found" in proc.stdout, f"{label}: nested-only skip stdout expected: {proc.stdout!r}")
    print(f"  PASS  {label}")


def scenario_install_sh_claude_editor_mode_nested_with_root_warns_updates_root_only(label: str, tmpdir: Path) -> None:
    config = tmpdir / ".claude.json"
    config.write_text('{\n  "editorMode": "vim",\n  "settings": {\n    "editorMode": "emacs"\n  }\n}\n', encoding="utf-8")

    proc = _run_bridge_set_editor_mode(config)
    assert_true(proc.returncode == 0, f"{label}: helper should update root despite nested warning: {proc.stderr!r}")
    data = json.loads(config.read_text(encoding="utf-8"))
    assert_true(data.get("editorMode") == "normal", f"{label}: root editorMode should update: {data}")
    assert_true(data.get("settings", {}).get("editorMode") == "emacs", f"{label}: nested editorMode must not be mutated: {data}")
    assert_true("WARNING:" in proc.stderr and "$.settings.editorMode" in proc.stderr, f"{label}: nested warning should name path: {proc.stderr!r}")
    print(f"  PASS  {label}")


def scenario_install_sh_claude_editor_mode_skip_flag_no_write(label: str, tmpdir: Path) -> None:
    root = tmpdir / "claude-mode-skip-flag"
    root.mkdir()
    _write_fake_install_tree(root)
    env = _fake_install_env(tmpdir)
    home = Path(env["HOME"])
    home.mkdir(parents=True, exist_ok=True)
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    config = home / ".claude.json"
    settings_config = claude_dir / "settings.json"
    original = b'{\n  "editorMode": "vim"\n}\n'
    config.write_bytes(original)
    settings_config.write_bytes(original)

    proc = _run_fake_install(root, tmpdir / "shims-claude-mode-skip-flag", env=env, extra_args=["--skip-claude-editor-mode"])
    assert_true(proc.returncode == 0, f"{label}: skip flag should not fail install: {proc.stderr!r}")
    assert_true(config.read_bytes() == original, f"{label}: skip flag must leave config byte-identical")
    assert_true(settings_config.read_bytes() == original, f"{label}: skip flag must leave settings config byte-identical")
    assert_true(not _claude_backups(config), f"{label}: skip flag must not create backup")
    assert_true(not _claude_backups(settings_config), f"{label}: skip flag must not create settings backup")
    assert_true(proc.stdout.count("skip: --skip-claude-editor-mode; Claude Code editorMode unchanged") == 1, f"{label}: skip flag should print exactly one global skip line: {proc.stdout!r}")
    print(f"  PASS  {label}")


def scenario_install_sh_claude_editor_mode_broken_symlink_warns(label: str, tmpdir: Path) -> None:
    link = tmpdir / ".claude.json"
    missing_target = tmpdir / "missing" / "claude.json"
    link.symlink_to(missing_target)

    proc = _run_bridge_set_editor_mode(link)
    assert_true(proc.returncode == 0, f"{label}: broken symlink should warn and continue: {proc.stderr!r}")
    assert_true(link.is_symlink(), f"{label}: broken symlink should remain a symlink")
    assert_true(not missing_target.exists(), f"{label}: missing target must not be created")
    assert_true("WARNING:" in proc.stderr and "symlink target is missing" in proc.stderr and str(missing_target) in proc.stderr, f"{label}: broken symlink warning expected: {proc.stderr!r}")
    assert_true("skip: Claude Code config absent" in proc.stdout, f"{label}: broken symlink skip stdout expected: {proc.stdout!r}")
    print(f"  PASS  {label}")


def scenario_install_sh_claude_editor_mode_concurrent_change_aborts(label: str, tmpdir: Path) -> None:
    config = tmpdir / ".claude.json"
    original = b'{\n  "editorMode": "vim",\n  "counter": 1\n}\n'
    racer = b'{\n  "editorMode": "vim",\n  "counter": 2\n}\n'
    race_file = tmpdir / "race-bytes.json"
    config.write_bytes(original)
    race_file.write_bytes(racer)

    proc = _run_bridge_set_editor_mode(config, env_extra={"BRIDGE_EDITOR_MODE_TEST_RACE_FILE": str(race_file)})
    assert_true(proc.returncode == 0, f"{label}: concurrent change should warn-skip, not fail: {proc.stderr!r}")
    assert_true(config.read_bytes() == racer, f"{label}: concurrent writer bytes must win unchanged")
    assert_true(not _claude_backups(config), f"{label}: concurrent-change guard must not create backup")
    assert_true(not _claude_temps(config), f"{label}: concurrent-change guard must clean temp file")
    assert_true("WARNING:" in proc.stderr and "concurrent change detected" in proc.stderr and "Re-run install.sh after closing Claude Code" in proc.stderr, f"{label}: concurrent warning expected: {proc.stderr!r}")
    assert_true("skip: concurrent change detected" in proc.stdout, f"{label}: concurrent skip stdout expected: {proc.stdout!r}")
    print(f"  PASS  {label}")


def scenario_install_sh_hook_failure_hard_fails(label: str, tmpdir: Path) -> None:
    root = tmpdir / "hook-hard-fail"
    root.mkdir()
    _write_fake_install_tree(root)
    argv_file = tmpdir / "hook-hard-fail.argv"
    _write_fake_hook_installer(root, exit_code=42, argv_file=argv_file)
    proc = _run_fake_install(root, tmpdir / "shims-hook-hard-fail", skip_hooks=False, env=_fake_install_env(tmpdir))
    assert_true(proc.returncode == 42, f"{label}: hook installer status must pass through, got {proc.returncode}: {proc.stderr!r}")
    assert_true("install.sh: hook config install failed" in proc.stderr, f"{label}: hard-fail stderr should name hook failure: {proc.stderr!r}")
    assert_true("shims may have been written" in proc.stderr, f"{label}: hard-fail stderr should mention partial shims: {proc.stderr!r}")
    assert_true("Agent Bridge will not receive hook events until fixed" in proc.stderr, f"{label}: hard-fail stderr should explain hook impact: {proc.stderr!r}")
    assert_true("bridge_healthcheck" in proc.stderr, f"{label}: hard-fail stderr should recommend healthcheck: {proc.stderr!r}")
    assert_true("--ignore-hook-failure" in proc.stderr, f"{label}: hard-fail stderr should mention explicit escape hatch: {proc.stderr!r}")
    assert_true("run: " not in proc.stdout, f"{label}: hard-fail stdout must not show final success hint: {proc.stdout!r}")
    assert_true("tmux not found" not in proc.stderr, f"{label}: hard-fail must stop before tmux warning: {proc.stderr!r}")
    print(f"  PASS  {label}")


def scenario_install_sh_hook_failure_ignore_flag_allows_success(label: str, tmpdir: Path) -> None:
    root = tmpdir / "hook-ignore"
    root.mkdir()
    _write_fake_install_tree(root)
    _write_fake_hook_installer(root, exit_code=42, argv_file=tmpdir / "hook-ignore.argv")
    bin_dir = tmpdir / "shims-hook-ignore"
    proc = _run_fake_install(
        root,
        bin_dir,
        skip_hooks=False,
        env=_fake_install_env(tmpdir),
        extra_args=["--ignore-hook-failure"],
    )
    assert_true(proc.returncode == 0, f"{label}: ignore flag should allow success, got {proc.returncode}: {proc.stderr!r}")
    assert_true("override was used" in proc.stderr, f"{label}: warning must say override was used: {proc.stderr!r}")
    assert_true("shims were installed but hook events will not work until fixed" in proc.stderr, f"{label}: warning must explain broken hook events: {proc.stderr!r}")
    assert_true("bridge_healthcheck" in proc.stderr, f"{label}: warning should recommend healthcheck: {proc.stderr!r}")
    assert_true(f"run: {bin_dir}/bridge_healthcheck" in proc.stdout, f"{label}: success hint should remain after override: {proc.stdout!r}")
    assert_true(os.access(bin_dir / "agent_alarm", os.X_OK), f"{label}: shims should be installed under override")
    print(f"  PASS  {label}")


def scenario_install_sh_hook_dry_run_failure_hard_fails(label: str, tmpdir: Path) -> None:
    root = tmpdir / "hook-dry-run-fail"
    root.mkdir()
    _write_fake_install_tree(root)
    argv_file = tmpdir / "hook-dry-run-fail.argv"
    _write_fake_hook_installer(root, exit_code=42, argv_file=argv_file)
    proc = _run_fake_install(
        root,
        tmpdir / "shims-hook-dry-run-fail",
        skip_hooks=False,
        env=_fake_install_env(tmpdir),
        extra_args=["--dry-run"],
    )
    argv_text = argv_file.read_text(encoding="utf-8")
    assert_true(proc.returncode == 42, f"{label}: dry-run hook failure must hard-fail, got {proc.returncode}: {proc.stderr!r}")
    assert_true("--dry-run" in argv_text, f"{label}: install.sh must forward --dry-run to hook installer: {argv_text!r}")
    assert_true("install.sh: hook config install failed" in proc.stderr, f"{label}: dry-run hard-fail should use targeted error: {proc.stderr!r}")
    assert_true("run: " not in proc.stdout, f"{label}: dry-run hard-fail must not show final success hint: {proc.stdout!r}")

    argv_override = tmpdir / "hook-dry-run-override.argv"
    _write_fake_hook_installer(root, exit_code=42, argv_file=argv_override)
    proc_override = _run_fake_install(
        root,
        tmpdir / "shims-hook-dry-run-override",
        skip_hooks=False,
        env=_fake_install_env(tmpdir),
        extra_args=["--dry-run", "--ignore-hook-failure"],
    )
    assert_true(proc_override.returncode == 0, f"{label}: dry-run override should continue, got {proc_override.returncode}: {proc_override.stderr!r}")
    assert_true("override was used" in proc_override.stderr, f"{label}: dry-run override warning expected: {proc_override.stderr!r}")
    assert_true("--dry-run" in argv_override.read_text(encoding="utf-8"), f"{label}: dry-run override must still forward --dry-run")
    print(f"  PASS  {label}")


def scenario_install_sh_hook_success_succeeds(label: str, tmpdir: Path) -> None:
    root = tmpdir / "hook-success"
    root.mkdir()
    _write_fake_install_tree(root)
    argv_file = tmpdir / "hook-success.argv"
    _write_fake_hook_installer(root, exit_code=0, argv_file=argv_file)
    bin_dir = tmpdir / "shims-hook-success"
    proc = _run_fake_install(root, bin_dir, skip_hooks=False, env=_fake_install_env(tmpdir))
    argv_lines = argv_file.read_text(encoding="utf-8").splitlines()
    assert_true(proc.returncode == 0, f"{label}: hook success should keep install success, got {proc.returncode}: {proc.stderr!r}")
    assert_true("--hook-command" in argv_lines, f"{label}: hook installer should receive --hook-command: {argv_lines}")
    assert_true(argv_lines[argv_lines.index("--hook-command") + 1] == str(root / "hooks" / "bridge-hook"), f"{label}: hook command path incorrect: {argv_lines}")
    assert_true("--dry-run" not in argv_lines, f"{label}: non-dry-run install should not pass --dry-run: {argv_lines}")
    assert_true(os.access(bin_dir / "agent_send_peer", os.X_OK), f"{label}: shims should be installed on hook success")

    argv_dry = tmpdir / "hook-success-dry.argv"
    _write_fake_hook_installer(root, exit_code=0, argv_file=argv_dry)
    proc_dry = _run_fake_install(
        root,
        tmpdir / "shims-hook-success-dry",
        skip_hooks=False,
        env=_fake_install_env(tmpdir),
        extra_args=["--dry-run"],
    )
    assert_true(proc_dry.returncode == 0, f"{label}: hook dry-run success should pass: {proc_dry.stderr!r}")
    assert_true("--dry-run" in argv_dry.read_text(encoding="utf-8").splitlines(), f"{label}: dry-run should reach hook installer")
    print(f"  PASS  {label}")


def scenario_install_sh_shim_failure_still_hard_fails_under_override(label: str, tmpdir: Path) -> None:
    root = tmpdir / "shim-fail-override"
    root.mkdir()
    _write_fake_install_tree(root, omit=Path("model-bin/agent_alarm"))
    argv_file = tmpdir / "shim-fail-override.argv"
    _write_fake_hook_installer(root, exit_code=0, argv_file=argv_file)
    proc = _run_fake_install(
        root,
        tmpdir / "shims-shim-fail-override",
        skip_hooks=False,
        env=_fake_install_env(tmpdir),
        extra_args=["--ignore-hook-failure"],
    )
    assert_true(proc.returncode != 0, f"{label}: override must not suppress shim target failures")
    assert_true("missing shim target for agent_alarm" in proc.stderr, f"{label}: missing shim error expected: {proc.stderr!r}")
    assert_true("override was used" not in proc.stderr, f"{label}: hook override warning must not fire for shim failure: {proc.stderr!r}")
    assert_true(not argv_file.exists(), f"{label}: hook installer should not run after shim failure")
    print(f"  PASS  {label}")


def scenario_install_sh_skip_hooks_with_ignore_flag_is_noop(label: str, tmpdir: Path) -> None:
    root = tmpdir / "skip-hooks-ignore"
    root.mkdir()
    _write_fake_install_tree(root)
    argv_file = tmpdir / "skip-hooks-ignore.argv"
    _write_fake_hook_installer(root, exit_code=42, argv_file=argv_file)
    proc = _run_fake_install(
        root,
        tmpdir / "shims-skip-hooks-ignore",
        skip_hooks=True,
        env=_fake_install_env(tmpdir),
        extra_args=["--ignore-hook-failure"],
    )
    assert_true(proc.returncode == 0, f"{label}: skip-hooks + ignore should remain a no-op success: {proc.stderr!r}")
    assert_true(not argv_file.exists(), f"{label}: --skip-hooks must not invoke hook installer")
    assert_true("hook config install failed" not in proc.stderr, f"{label}: no hook failure warning expected when hooks skipped: {proc.stderr!r}")
    print(f"  PASS  {label}")


SCENARIOS = [
    ('uninstall_helper_print_paths', scenario_uninstall_helper_print_paths),
    ('uninstall_helper_refuses_dangerous_path', scenario_uninstall_helper_refuses_dangerous_path),
    ('uninstall_sh_dry_run_invokes_hook_helper_with_dry_run', scenario_uninstall_sh_dry_run_invokes_hook_helper_with_dry_run),
    ('uninstall_sh_non_dry_run_removes_hook_entries', scenario_uninstall_sh_non_dry_run_removes_hook_entries),
    ('uninstall_sh_keep_hooks_skips_helper_under_dry_run', scenario_uninstall_sh_keep_hooks_skips_helper_under_dry_run),
    ('uninstall_sh_hook_helper_failure_aborts', scenario_uninstall_sh_hook_helper_failure_aborts),
    ('uninstall_sh_removes_agent_bridge_shim', scenario_uninstall_sh_removes_agent_bridge_shim),
    ('legacy_wrappers_use_in_tree_agent_bridge', scenario_legacy_wrappers_use_in_tree_agent_bridge),
    ('agent_bridge_attach_and_manage_dispatch_contracts', scenario_agent_bridge_attach_and_manage_dispatch_contracts),
    ('direct_exec_targets_executable', scenario_direct_exec_targets_executable),
    ('healthcheck_executable_helper_distinguishes_states', scenario_healthcheck_executable_helper_distinguishes_states),
    ('python_env_override_removed_from_tracked_files', scenario_python_env_override_removed_from_tracked_files),
    ('install_sh_python_version_gate', scenario_install_sh_python_version_gate),
    ('bridge_healthcheck_sh_python_version_gate', scenario_bridge_healthcheck_sh_python_version_gate),
    ('bridge_healthcheck_agent_bridge_contracts', scenario_bridge_healthcheck_agent_bridge_contracts),
    ('install_sh_chmods_target_or_fails', scenario_install_sh_chmods_target_or_fails),
    ('install_sh_default_updates_shell_rc', scenario_install_sh_default_updates_shell_rc),
    ('install_sh_shell_rc_dry_run_and_opt_out', scenario_install_sh_shell_rc_dry_run_and_opt_out),
    ('install_sh_shell_rc_target_selection', scenario_install_sh_shell_rc_target_selection),
    ('install_sh_shell_rc_backup_and_path_short_circuit', scenario_install_sh_shell_rc_backup_and_path_short_circuit),
    ('install_sh_shell_quotes_bin_dir_metacharacters', scenario_install_sh_shell_quotes_bin_dir_metacharacters),
    ('install_sh_shell_rc_replaces_existing_marker_block', scenario_install_sh_shell_rc_replaces_existing_marker_block),
    ('install_sh_claude_editor_mode_missing_skips', scenario_install_sh_claude_editor_mode_missing_skips),
    ('install_sh_claude_editor_mode_updates_with_backup', scenario_install_sh_claude_editor_mode_updates_with_backup),
    ('install_sh_claude_editor_mode_updates_global_and_settings', scenario_install_sh_claude_editor_mode_updates_global_and_settings),
    ('install_sh_claude_editor_mode_missing_global_updates_settings', scenario_install_sh_claude_editor_mode_missing_global_updates_settings),
    ('install_sh_claude_editor_mode_invalid_global_still_updates_settings', scenario_install_sh_claude_editor_mode_invalid_global_still_updates_settings),
    ('install_sh_claude_editor_mode_normal_noop', scenario_install_sh_claude_editor_mode_normal_noop),
    ('install_sh_claude_editor_mode_absent_adds_root', scenario_install_sh_claude_editor_mode_absent_adds_root),
    ('install_sh_claude_editor_mode_invalid_json_skips', scenario_install_sh_claude_editor_mode_invalid_json_skips),
    ('install_sh_claude_editor_mode_invalid_utf8_skips', scenario_install_sh_claude_editor_mode_invalid_utf8_skips),
    ('install_sh_claude_editor_mode_bom_skips', scenario_install_sh_claude_editor_mode_bom_skips),
    ('install_sh_claude_editor_mode_non_object_skips', scenario_install_sh_claude_editor_mode_non_object_skips),
    ('install_sh_claude_editor_mode_dry_run_no_write', scenario_install_sh_claude_editor_mode_dry_run_no_write),
    ('install_sh_claude_editor_mode_preserves_mode', scenario_install_sh_claude_editor_mode_preserves_mode),
    ('install_sh_claude_editor_mode_symlink_preserved', scenario_install_sh_claude_editor_mode_symlink_preserved),
    ('install_sh_claude_editor_mode_nested_root_absent_warns_skips', scenario_install_sh_claude_editor_mode_nested_root_absent_warns_skips),
    ('install_sh_claude_editor_mode_nested_with_root_warns_updates_root_only', scenario_install_sh_claude_editor_mode_nested_with_root_warns_updates_root_only),
    ('install_sh_claude_editor_mode_skip_flag_no_write', scenario_install_sh_claude_editor_mode_skip_flag_no_write),
    ('install_sh_claude_editor_mode_broken_symlink_warns', scenario_install_sh_claude_editor_mode_broken_symlink_warns),
    ('install_sh_claude_editor_mode_concurrent_change_aborts', scenario_install_sh_claude_editor_mode_concurrent_change_aborts),
    ('install_sh_hook_failure_hard_fails', scenario_install_sh_hook_failure_hard_fails),
    ('install_sh_hook_failure_ignore_flag_allows_success', scenario_install_sh_hook_failure_ignore_flag_allows_success),
    ('install_sh_hook_dry_run_failure_hard_fails', scenario_install_sh_hook_dry_run_failure_hard_fails),
    ('install_sh_hook_success_succeeds', scenario_install_sh_hook_success_succeeds),
    ('install_sh_shim_failure_still_hard_fails_under_override', scenario_install_sh_shim_failure_still_hard_fails_under_override),
    ('install_sh_skip_hooks_with_ignore_flag_is_noop', scenario_install_sh_skip_hooks_with_ignore_flag_is_noop),
]
