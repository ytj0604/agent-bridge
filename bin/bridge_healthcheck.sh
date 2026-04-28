#!/usr/bin/env bash
set -euo pipefail

bridge_resolve_script_dir() {
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
    echo "# in your PATH before running bridge_healthcheck again."
    echo "###################################################################"
  } >&2
}

require_python_310() {
  local python_path
  if ! python_path="$(command -v python3 2>/dev/null)"; then
    print_python_requirement_error "python3 not found in PATH"
    exit 1
  fi

  local probe status version executable
  status=0
  probe="$(python3 -c '
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

_bridge_bin_dir="$(bridge_resolve_script_dir "${BASH_SOURCE[0]}")"
_bridge_root="$(cd "$_bridge_bin_dir/.." >/dev/null 2>&1 && pwd -P)"
require_python_310
source "$_bridge_root/libexec/agent-bridge/bridge_common.sh"

exec "$BRIDGE_PYTHON" "$BRIDGE_LIBEXEC_DIR/bridge_healthcheck.py" "$@"
