#!/usr/bin/env bash
set -euo pipefail

root="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve().parent)' "$0")"
libexec_dir="$root/libexec/agent-bridge"
bin_dir="${XDG_BIN_HOME:-$HOME/.local/bin}"
yes="0"
dry_run="0"
install_hooks="1"
update_shell_rc="ask"

usage() {
  local code="${1:-2}"
  cat >&2 <<'EOF'
usage: install.sh [--yes] [--dry-run] [--bin-dir DIR] [--skip-hooks] [--no-shell-rc]
EOF
  exit "$code"
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
write_shim agent_extend_wait "$root/model-bin/agent_extend_wait"

case ":$PATH:" in
  *":$bin_dir:"*) path_ok="1" ;;
  *) path_ok="0" ;;
esac

if [[ "$path_ok" == "0" ]]; then
  line="export PATH=\"$bin_dir:\$PATH\""
  rc_file="${SHELL##*/}"
  case "$rc_file" in
    zsh) rc_path="$HOME/.zshrc" ;;
    bash) rc_path="$HOME/.bashrc" ;;
    *) rc_path="$HOME/.profile" ;;
  esac
  if [[ "$update_shell_rc" != "never" && "$yes" == "1" ]]; then
    echo "add PATH line to $rc_path"
    if [[ "$dry_run" != "1" ]]; then
      touch "$rc_path"
      if ! grep -Fq "$line" "$rc_path"; then
        printf '\n# Agent Bridge\n%s\n' "$line" >> "$rc_path"
      fi
    fi
  else
    echo "PATH note: add this to your shell rc if human bridge commands are not found:"
    echo "$line"
  fi
fi

if [[ "$install_hooks" == "1" ]]; then
  hook_command="$root/hooks/bridge-hook"
  args=("$libexec_dir/bridge_install_hooks.py" --hook-command "$hook_command")
  if [[ "$dry_run" == "1" ]]; then
    args+=(--dry-run)
  fi
  echo "install hooks via $hook_command"
  if ! python3 "${args[@]}"; then
    echo "warning: hook config install failed; shims were installed, run bridge_healthcheck for details" >&2
  fi
fi

if ! command -v tmux >/dev/null 2>&1; then
  echo "warning: tmux not found on PATH. Agent Bridge attaches to tmux panes, so bridge_run will fail until tmux is installed." >&2
  echo "         install with your package manager, e.g. 'apt install tmux' or 'brew install tmux'." >&2
fi

echo "run: $bin_dir/bridge_healthcheck"
