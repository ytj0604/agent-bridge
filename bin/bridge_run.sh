#!/usr/bin/env bash
set -euo pipefail

_bridge_bin_dir="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve().parent)' "${BASH_SOURCE[0]}")"
_bridge_root="$(cd "$_bridge_bin_dir/.." >/dev/null 2>&1 && pwd -P)"
source "$_bridge_root/libexec/agent-bridge/bridge_common.sh"

default_session="agent-bridge-auto"
session=""
session_explicit=0

usage() {
  local code="${1:-2}"
  cat >&2 <<'EOF'
usage: bridge_run [options]

Attach existing Claude/Codex tmux panes to a new bridge room.

Prerequisite:
  Start Claude Code and/or Codex in tmux panes first, then run bridge_run.
  bridge_run does not launch agents; it only attaches already-running panes.

Options:
  -s, --session NAME          Bridge room/state name; skips the room-name prompt
  -h, --help                  Show this help

If -s is omitted, bridge_run prompts:
  Room name [agent-bridge-auto]:

If the default room already exists, the suggested default becomes
agent-bridge-auto-2, agent-bridge-auto-3, etc. A name typed explicitly at the
prompt is used as-is.

Example:
  tmux new -s work
  # split panes and run claude / codex manually
  bridge_run
EOF
  exit "$code"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -s|--session)
      session="$2"
      session_explicit=1
      shift 2
      ;;
    -h|--help)
      usage 0
      ;;
    --)
      shift
      break
      ;;
    -*)
      usage
      ;;
    *)
      usage
      ;;
  esac
done

if [[ $# -gt 0 ]]; then
  usage
fi

choose_default_session() {
  "$BRIDGE_PYTHON" - "$BRIDGE_STATE_DIR" "$BRIDGE_RUN_DIR" "$default_session" <<'PY'
from pathlib import Path
import re
import sys

state_dir = Path(sys.argv[1])
run_dir = Path(sys.argv[2])
base = sys.argv[3]


def exists(name: str) -> bool:
    return (
        (state_dir / name / "session.json").exists()
        or (run_dir / f"{name}.pid").exists()
        or (run_dir / f"{name}.json").exists()
    )


match = re.match(r"^(.*?)-(\d+)$", base)
prefix = match.group(1) if match else base
start = int(match.group(2)) if match else 1

if not exists(base):
    print(base)
    raise SystemExit(0)

idx = max(2, start + 1)
while True:
    candidate = f"{prefix}-{idx}"
    if not exists(candidate):
        print(candidate)
        raise SystemExit(0)
    idx += 1
PY
}

validate_session_name() {
  local value="$1"
  "$BRIDGE_PYTHON" - "$value" <<'PY'
import re
import sys

name = sys.argv[1]
if not re.match(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$", name):
    print("bridge_run: room name must start with a letter and contain only letters, digits, _, ., -", file=sys.stderr)
    raise SystemExit(2)
PY
}

prompt_session_name() {
  local suggested value
  suggested="$(choose_default_session)"
  if [[ -r /dev/tty && -w /dev/tty ]]; then
    value="$(bridge_prompt_line "Room name [$suggested]: " </dev/tty)" || exit 130
  else
    value="$suggested"
  fi
  value="${value:-$suggested}"
  validate_session_name "$value"
  session="$value"
}

if [[ "$session_explicit" -eq 0 ]]; then
  prompt_session_name
else
  validate_session_name "$session"
fi

if [[ -r /dev/tty && -w /dev/tty ]]; then
  exec "$BRIDGE_PYTHON" "$BRIDGE_LIBEXEC_DIR/bridge_attach.py" -s "$session" </dev/tty >/dev/tty 2>&1
fi
exec "$BRIDGE_PYTHON" "$BRIDGE_LIBEXEC_DIR/bridge_attach.py" -s "$session"
