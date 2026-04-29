#!/usr/bin/env bash
set -euo pipefail

resolve_script_dir() {
  local source="$1"
  local dir
  while [[ -L "$source" ]]; do
    dir="$(cd -P "$(dirname "$source")" >/dev/null 2>&1 && pwd)"
    source="$(readlink "$source")"
    [[ "$source" != /* ]] && source="$dir/$source"
  done
  cd -P "$(dirname "$source")" >/dev/null 2>&1 && pwd
}

print_python_requirement_error() {
  local detected="$1"
  {
    echo "###################################################################"
    echo "# ERROR: agent-bridge requires Python 3.10 or newer."
    echo "#"
    echo "# Detected: $detected"
    echo "# Required: Python 3.10+"
    echo "#"
    echo '# Please install Python 3.10+ and ensure it is available as `python3`'
    echo "# in your PATH before running ./install.sh again."
    echo "###################################################################"
  } >&2
}

require_python_310() {
  local python_path
  if ! python_path="$(command -v "$python_bin" 2>/dev/null)"; then
    print_python_requirement_error "python3 not found in PATH"
    exit 1
  fi

  local probe status version executable
  status=0
  probe="$("$python_bin" -c '
# agent_bridge_python_gate_v1
import platform
import sys

print(platform.python_version())
print(sys.executable or "python3")
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
' 2>/dev/null)" || status=$?
  if [[ "$status" -eq 0 ]]; then
    return
  fi

  version="$(printf '%s\n' "$probe" | sed -n '1p')"
  executable="$(printf '%s\n' "$probe" | sed -n '2p')"
  if [[ -n "$version" ]]; then
    print_python_requirement_error "Python $version at ${executable:-$python_path}"
  else
    print_python_requirement_error "unable to run python3 at $python_path"
  fi
  exit 1
}

root="$(resolve_script_dir "$0")"
libexec_dir="$root/libexec/agent-bridge"
python_bin="python3"
require_python_310
bin_dir="${XDG_BIN_HOME:-$HOME/.local/bin}"
yes="0"
dry_run="0"
install_hooks="1"
ignore_hook_failure="0"
update_shell_rc="auto"
path_block_begin="# >>> Agent Bridge >>>"
path_block_end="# <<< Agent Bridge <<<"

usage() {
  local code="${1:-2}"
  cat >&2 <<'EOF'
usage: install.sh [--yes] [--dry-run] [--bin-dir DIR] [--skip-hooks] [--ignore-hook-failure] [--no-shell-rc]

  --skip-hooks            do not run hook config installation
  --ignore-hook-failure   explicit shim-only/diagnostic escape hatch; only applies when hook installation runs
  --no-shell-rc           do not add the Agent Bridge PATH block to your shell rc
EOF
  exit "$code"
}

detect_shell_rc() {
  local shell_name os_name
  shell_name="${SHELL##*/}"
  os_name="$(uname -s 2>/dev/null || true)"
  case "$shell_name" in
    zsh)
      printf '%s\n' "$HOME/.zshrc"
      ;;
    bash)
      if [[ "$os_name" == "Darwin" ]]; then
        printf '%s\n' "$HOME/.bash_profile"
      else
        printf '%s\n' "$HOME/.bashrc"
      fi
      ;;
    *)
      printf '%s\n' "$HOME/.profile"
      ;;
  esac
}

print_path_block() {
  "$python_bin" - "$bin_dir" "$path_block_begin" "$path_block_end" <<'PY'
import shlex
import sys

bin_dir = sys.argv[1]
begin = sys.argv[2]
end = sys.argv[3]
quoted = shlex.quote(bin_dir)
print(begin)
print(f'export PATH={quoted}:"$PATH"')
print(end)
PY
}

path_block_needs_update() {
  local rc_path="$1"
  "$python_bin" - "$rc_path" "$bin_dir" "$path_block_begin" "$path_block_end" <<'PY'
from pathlib import Path
import shlex
import sys

path = Path(sys.argv[1])
bin_dir = sys.argv[2]
begin = sys.argv[3]
end = sys.argv[4]
quoted = shlex.quote(bin_dir)
block = f'{begin}\nexport PATH={quoted}:"$PATH"\n{end}\n'
try:
    original = path.read_text(encoding="utf-8")
except FileNotFoundError:
    original = ""

start = original.find(begin)
finish = original.find(end, start + len(begin)) if start >= 0 else -1
if start >= 0 and finish >= 0:
    finish += len(end)
    replacement = block.rstrip("\n")
    updated = original[:start] + replacement + original[finish:]
    if not updated.endswith("\n"):
        updated += "\n"
else:
    prefix = original
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    if prefix:
        prefix += "\n"
    updated = prefix + block

if updated == original:
    sys.exit(1)
sys.exit(0)
PY
}

write_path_block() {
  local rc_path="$1"
  "$python_bin" - "$rc_path" "$bin_dir" "$path_block_begin" "$path_block_end" <<'PY'
from pathlib import Path
import shlex
import sys

path = Path(sys.argv[1])
bin_dir = sys.argv[2]
begin = sys.argv[3]
end = sys.argv[4]
quoted = shlex.quote(bin_dir)
block = f'{begin}\nexport PATH={quoted}:"$PATH"\n{end}\n'
try:
    original = path.read_text(encoding="utf-8")
except FileNotFoundError:
    original = ""

start = original.find(begin)
finish = original.find(end, start + len(begin)) if start >= 0 else -1
if start >= 0 and finish >= 0:
    finish += len(end)
    replacement = block.rstrip("\n")
    updated = original[:start] + replacement + original[finish:]
    if not updated.endswith("\n"):
        updated += "\n"
else:
    prefix = original
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    if prefix:
        prefix += "\n"
    updated = prefix + block

if updated == original:
    sys.exit(0)
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(updated, encoding="utf-8")
PY
}

backup_rc_if_needed() {
  local rc_path="$1"
  if [[ ! -e "$rc_path" ]]; then
    return
  fi
  if path_block_needs_update "$rc_path"; then
    cp "$rc_path" "$rc_path.agent-bridge.bak.$(date +%Y%m%d%H%M%S).$$"
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes|-y)
      yes="1"
      shift
      ;;
    --dry-run)
      dry_run="1"
      shift
      ;;
    --bin-dir)
      bin_dir="$2"
      shift 2
      ;;
    --skip-hooks)
      install_hooks="0"
      shift
      ;;
    --ignore-hook-failure)
      ignore_hook_failure="1"
      shift
      ;;
    --no-shell-rc)
      update_shell_rc="never"
      shift
      ;;
    -h|--help)
      usage 0
      ;;
    *)
      usage
      ;;
  esac
done

write_shim() {
  local name="$1"
  local target="$2"
  local path="$bin_dir/$name"
  echo "install shim: $path -> $target"
  if [[ "$dry_run" == "1" ]]; then
    return
  fi
  if [[ ! -e "$target" ]]; then
    echo "install.sh: missing shim target for $name: $target" >&2
    exit 1
  fi
  if [[ ! -f "$target" ]]; then
    echo "install.sh: shim target is not a file for $name: $target" >&2
    exit 1
  fi
  if [[ ! -x "$target" ]]; then
    if ! chmod +x "$target"; then
      echo "install.sh: cannot make shim target executable for $name: $target" >&2
      exit 1
    fi
  fi
  mkdir -p "$bin_dir"
  cat > "$path" <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec "$target" "\$@"
EOF
  chmod +x "$path"
}

write_shim bridge_run "$root/bin/bridge_run.sh"
write_shim bridge_manage "$root/bin/bridge_manage.sh"
write_shim bridge_healthcheck "$root/bin/bridge_healthcheck.sh"
write_shim agent_send_peer "$root/model-bin/agent_send_peer"
write_shim agent_list_peers "$root/model-bin/agent_list_peers"
write_shim agent_view_peer "$root/model-bin/agent_view_peer"
write_shim agent_alarm "$root/model-bin/agent_alarm"
write_shim agent_interrupt_peer "$root/model-bin/agent_interrupt_peer"
write_shim agent_clear_peer "$root/model-bin/agent_clear_peer"
write_shim agent_extend_wait "$root/model-bin/agent_extend_wait"
write_shim agent_cancel_message "$root/model-bin/agent_cancel_message"
write_shim agent_wait_status "$root/model-bin/agent_wait_status"
write_shim agent_aggregate_status "$root/model-bin/agent_aggregate_status"

case ":$PATH:" in
  *":$bin_dir:"*) path_ok="1" ;;
  *) path_ok="0" ;;
esac

if [[ "$path_ok" == "0" ]]; then
  rc_path="$(detect_shell_rc)"
  if [[ "$update_shell_rc" != "never" ]]; then
    echo "add PATH block to $rc_path"
    if [[ "$dry_run" != "1" ]]; then
      if path_block_needs_update "$rc_path"; then
        backup_rc_if_needed "$rc_path"
        write_path_block "$rc_path"
      else
        echo "PATH block already present in $rc_path"
      fi
    fi
  else
    echo "PATH note: add this block to your shell rc if bridge commands are not found:"
    print_path_block
  fi
fi

if [[ "$install_hooks" == "1" ]]; then
  hook_command="$root/hooks/bridge-hook"
  args=("$libexec_dir/bridge_install_hooks.py" --hook-command "$hook_command")
  if [[ "$dry_run" == "1" ]]; then
    args+=(--dry-run)
  fi
  echo "install hooks via $hook_command"
  if "$python_bin" "${args[@]}"; then
    :
  else
    status="$?"
    if [[ "$ignore_hook_failure" == "1" ]]; then
      {
        echo "WARNING: install.sh: hook config install failed, but --ignore-hook-failure override was used."
        echo "WARNING: shims were installed but hook events will not work until fixed."
        echo "WARNING: run $bin_dir/bridge_healthcheck or bridge_healthcheck for details."
      } >&2
    else
      {
        echo "install.sh: hook config install failed (exit $status)."
        echo "install.sh: shims may have been written, but Agent Bridge will not receive hook events until fixed."
        echo "install.sh: run $bin_dir/bridge_healthcheck or bridge_healthcheck for details."
        echo "install.sh: pass --ignore-hook-failure only for an explicit shim-only/diagnostic install."
      } >&2
      exit "$status"
    fi
  fi
fi

if ! command -v tmux >/dev/null 2>&1; then
  echo "warning: tmux not found on PATH. Agent Bridge attaches to tmux panes, so bridge_run will fail until tmux is installed." >&2
  echo "         install with your package manager, e.g. 'apt install tmux' or 'brew install tmux'." >&2
fi

cat <<'EOF'
permission mode note: for unattended agent-to-agent work, start agents in a trusted or externally sandboxed workspace with permission prompts disabled.
  Claude Code: claude --permission-mode bypassPermissions
  Codex:       codex --dangerously-bypass-approvals-and-sandbox
Otherwise permission prompts can stall bridge requests until a human responds.
EOF
echo "run: $bin_dir/bridge_healthcheck"
