#!/usr/bin/env bash
set -euo pipefail

root="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve().parent)' "$0")"
libexec_dir="$root/libexec/agent-bridge"
bin_dir="${XDG_BIN_HOME:-$HOME/.local/bin}"
dry_run="0"
remove_hooks="1"
remove_shims="1"
remove_state="0"
remove_repo="0"

usage() {
  local code="${1:-2}"
  cat >&2 <<'EOF'
usage: uninstall.sh [--dry-run] [--bin-dir DIR] [--keep-hooks] [--keep-shims] [--state] [--repo]

Default uninstall removes stable shims and Claude/Codex hook entries.
It does not delete state/log/run files or the repo checkout unless requested.
EOF
  exit "$code"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes|-y)
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
    --keep-hooks)
      remove_hooks="0"
      shift
      ;;
    --keep-shims)
      remove_shims="0"
      shift
      ;;
    --state)
      remove_state="1"
      shift
      ;;
    --repo)
      remove_repo="1"
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

run_or_print() {
  if [[ "$dry_run" == "1" ]]; then
    printf 'dry-run:'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

if [[ "$remove_hooks" == "1" ]]; then
  helper="$libexec_dir/bridge_uninstall_hooks.py"
  hook_args=()
  if [[ "$dry_run" == "1" ]]; then
    hook_args+=("--dry-run")
  fi
  echo "remove Claude/Codex hook entries"
  if ! python3 "$helper" "${hook_args[@]}"; then
    echo "uninstall.sh: hook removal helper failed; aborting" >&2
    exit 1
  fi
fi

if [[ "$remove_shims" == "1" ]]; then
  for name in \
    bridge_run \
    bridge_manage \
    bridge_healthcheck \
    agent_send_peer \
    agent_list_peers \
    agent_view_peer \
    agent_alarm \
    agent_interrupt_peer \
    agent_extend_wait \
    agent_cancel_message
  do
    path="$bin_dir/$name"
    if [[ -e "$path" || -L "$path" ]]; then
      echo "remove shim: $path"
      run_or_print rm -f "$path"
    fi
  done
fi

if [[ "$remove_state" == "1" ]]; then
  helper="$libexec_dir/bridge_uninstall_state.py"
  helper_args=()
  if [[ "$dry_run" == "1" ]]; then
    helper_args+=("--dry-run")
  fi
  echo "remove bridge runtime state/run/log via $helper"
  if ! python3 "$helper" "${helper_args[@]}"; then
    echo "uninstall.sh: state cleanup helper failed; aborting" >&2
    exit 1
  fi
  # Legacy install-root paths (only relevant for very old installs that
  # wrote state under the repo dir). Still cleanup if present.
  for legacy in "$root/state" "$root/run" "$root/log"; do
    if [[ -d "$legacy" ]]; then
      run_or_print rm -rf "$legacy"
    fi
  done
fi

if [[ "$remove_repo" == "1" ]]; then
  echo "remove repo/install root: $root"
  run_or_print rm -rf "$root"
fi

echo "uninstall complete"
