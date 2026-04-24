#!/usr/bin/env bash

bridge_resolve_script() {
  local source="$1"
  local dir
  while [[ -L "$source" ]]; do
    dir="$(cd -P "$(dirname "$source")" >/dev/null 2>&1 && pwd)"
    source="$(readlink "$source")"
    [[ "$source" != /* ]] && source="$dir/$source"
  done
  cd -P "$(dirname "$source")" >/dev/null 2>&1 && pwd
}

BRIDGE_LIBEXEC_DIR="$(bridge_resolve_script "${BASH_SOURCE[0]}")"
BRIDGE_ROOT="${AGENT_BRIDGE_HOME:-$(cd "$BRIDGE_LIBEXEC_DIR/../.." >/dev/null 2>&1 && pwd -P)}"
BRIDGE_BIN_DIR="$BRIDGE_ROOT/bin"
BRIDGE_MODEL_BIN_DIR="$BRIDGE_ROOT/model-bin"
BRIDGE_HOOK_DIR="$BRIDGE_ROOT/hooks"
BRIDGE_LIBEXEC_DIR="$BRIDGE_ROOT/libexec/agent-bridge"
BRIDGE_PYTHON="${AGENT_BRIDGE_PYTHON:-python3}"

export AGENT_BRIDGE_HOME="$BRIDGE_ROOT"

bridge_runtime_path() {
  local kind="$1"
  local fallback="$2"
  "$BRIDGE_PYTHON" "$BRIDGE_LIBEXEC_DIR/bridge_paths.py" "$kind" 2>/dev/null || printf '%s\n' "$fallback"
}

BRIDGE_STATE_DIR="${AGENT_BRIDGE_STATE_DIR:-$(bridge_runtime_path state "$BRIDGE_ROOT/state")}"
BRIDGE_RUN_DIR="${AGENT_BRIDGE_RUN_DIR:-$(bridge_runtime_path run "$BRIDGE_ROOT/run")}"
BRIDGE_LOG_DIR="${AGENT_BRIDGE_LOG_DIR:-$(bridge_runtime_path log "$BRIDGE_ROOT/log")}"

bridge_select_menu() {
  local title="$1"
  shift
  "$BRIDGE_PYTHON" "$BRIDGE_LIBEXEC_DIR/bridge_select.py" --title "$title" "$@"
}

bridge_prompt_line() {
  local prompt="$1"
  local value
  if [[ -r /dev/tty && -w /dev/tty ]]; then
    printf '%s' "$prompt" > /dev/tty
    IFS= read -r value < /dev/tty || return 1
  else
    printf '%s' "$prompt" >&2
    IFS= read -r value || return 1
  fi
  printf '%s\n' "$value"
}
