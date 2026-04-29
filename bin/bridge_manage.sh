#!/usr/bin/env bash
set -euo pipefail

_bridge_bin_dir="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve().parent)' "${BASH_SOURCE[0]}")"
_bridge_root="$(cd "$_bridge_bin_dir/.." >/dev/null 2>&1 && pwd -P)"
source "$_bridge_root/libexec/agent-bridge/bridge_common.sh"

session=""

usage() {
  local code="${1:-2}"
  cat >&2 <<'EOF'
usage: bridge_manage [-s session] [--status]

Interactive bridge room manager.
EOF
  exit "$code"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -s|--session)
      session="$2"
      shift 2
      ;;
    --status)
      exec "$BRIDGE_PYTHON" "$BRIDGE_LIBEXEC_DIR/bridge_daemon_ctl.py" status --all
      ;;
    -h|--help)
      usage 0
      ;;
    *)
      usage
      ;;
  esac
done

list_rooms() {
  "$BRIDGE_PYTHON" "$BRIDGE_LIBEXEC_DIR/bridge_daemon_ctl.py" status --json |
    "$BRIDGE_PYTHON" -c 'import json,sys
rows=json.load(sys.stdin)
for row in rows:
    print("\t".join(str(row.get(k) or "") for k in ("session","status","participants","targets")))'
}

choose_room() {
  if [[ -n "$session" ]]; then
    return 0
  fi
  rooms=()
  while IFS= read -r row; do
    rooms+=("$row")
  done < <(list_rooms)
  if [[ "${#rooms[@]}" -eq 0 ]]; then
    echo "no bridge rooms found"
    exit 0
  fi
  labels=()
  local row name status participants targets
  for row in "${rooms[@]}"; do
    IFS=$'\t' read -r name status participants targets <<< "$row"
    labels+=("$name [$status] agents=${participants:-none} $targets")
  done
  if ! choice="$(bridge_select_menu "Bridge rooms" "${labels[@]}")"; then
    exit 130
  fi
  IFS=$'\t' read -r session _ <<< "${rooms[$choice]}"
}

room_summary() {
  "$BRIDGE_PYTHON" "$BRIDGE_LIBEXEC_DIR/bridge_manage_summary.py" --session "$session"
}

list_aliases() {
  "$BRIDGE_PYTHON" - "$BRIDGE_STATE_DIR/$session/session.json" <<'PY'
import json
import sys
from pathlib import Path
try:
    data=json.loads(Path(sys.argv[1]).read_text())
except Exception:
    data={}
for alias in sorted((data.get("participants") or {}).keys()):
    print(alias)
PY
}

join_agent() {
  if [[ -r /dev/tty && -w /dev/tty ]]; then
    "$BRIDGE_PYTHON" "$BRIDGE_LIBEXEC_DIR/bridge_join.py" -s "$session" </dev/tty >/dev/tty 2>&1
  else
    "$BRIDGE_PYTHON" "$BRIDGE_LIBEXEC_DIR/bridge_join.py" -s "$session"
  fi
}

leave_agent() {
  aliases=()
  while IFS= read -r alias; do
    aliases+=("$alias")
  done < <(list_aliases)
  if [[ "${#aliases[@]}" -eq 0 ]]; then
    echo "no agents in room"
    return 0
  fi
  if ! choice="$(bridge_select_menu "Select agent to leave" "${aliases[@]}")"; then
    return 130
  fi
  "$BRIDGE_PYTHON" "$BRIDGE_LIBEXEC_DIR/bridge_leave.py" -s "$session" --alias "${aliases[$choice]}"
}

clear_agent() {
  aliases=()
  while IFS= read -r alias; do
    aliases+=("$alias")
  done < <(list_aliases)
  if [[ "${#aliases[@]}" -eq 0 ]]; then
    echo "no agents in room"
    return 0
  fi
  if ! choice="$(bridge_select_menu "Select agent to clear" "${aliases[@]}")"; then
    return 130
  fi
  local alias="${aliases[$choice]}"
  set +e
  "$BRIDGE_PYTHON" "$BRIDGE_LIBEXEC_DIR/bridge_clear_peer.py" --session "$session" --from bridge --allow-spoof --to "$alias"
  local code=$?
  set -e
  if [[ "$code" -eq 0 ]]; then
    return 0
  fi
  if ! retry="$(bridge_select_menu "Clear $alias failed. Retry with --force?" "Retry with --force" "Cancel")"; then
    return 130
  fi
  if [[ "$retry" == "0" ]]; then
    "$BRIDGE_PYTHON" "$BRIDGE_LIBEXEC_DIR/bridge_clear_peer.py" --session "$session" --from bridge --allow-spoof --to "$alias" --force
  fi
}

stop_room() {
  "$BRIDGE_PYTHON" "$BRIDGE_LIBEXEC_DIR/bridge_daemon_ctl.py" stop -s "$session"
  if "$BRIDGE_PYTHON" "$BRIDGE_LIBEXEC_DIR/bridge_daemon_ctl.py" status -s "$session" --json |
    "$BRIDGE_PYTHON" -c 'import json,sys; raise SystemExit(0 if not json.load(sys.stdin) else 1)'
  then
    session=""
    choose_room
  fi
}

reload_daemon() {
  "$BRIDGE_PYTHON" "$BRIDGE_LIBEXEC_DIR/bridge_daemon_ctl.py" restart -s "$session"
}

show_status() {
  "$BRIDGE_PYTHON" "$BRIDGE_LIBEXEC_DIR/bridge_daemon_ctl.py" status -s "$session"
}

tail_daemon_log() {
  local log_file="$BRIDGE_LOG_DIR/$session.daemon.log"
  if [[ ! -f "$log_file" ]]; then
    echo "no daemon log found: $log_file"
    return 0
  fi
  if [[ -r /dev/tty && -w /dev/tty ]]; then
    echo "Tailing $log_file (Ctrl-C to return)." > /dev/tty
    set +e
    tail -n 80 -f "$log_file" </dev/tty >/dev/tty 2>&1
    set -e
  else
    tail -n 120 "$log_file"
  fi
}

clear_peer_view_cache() {
  "$BRIDGE_PYTHON" "$BRIDGE_LIBEXEC_DIR/bridge_captures.py" summary -s "$session"
  local title
  title="Clear peer view cache for $session?"$'\n'"This removes agent_view_peer snapshots/cursors only. It does not affect messages, panes, or daemon state."
  if ! choice="$(bridge_select_menu "$title" \
    "Clear peer view cache" \
    "Cancel")"; then
    return 130
  fi
  if [[ "$choice" != "0" ]]; then
    return 0
  fi
  "$BRIDGE_PYTHON" "$BRIDGE_LIBEXEC_DIR/bridge_captures.py" clear -s "$session"
}

choose_room

while true; do
  summary_text="$(room_summary 2>/dev/null || true)"
  menu_title="Room: $session"
  [[ -n "$summary_text" ]] && menu_title="$menu_title"$'\n'"$summary_text"
  if ! action="$(bridge_select_menu "$menu_title" \
    "Join agent pane" \
    "Leave agent" \
    "Clear agent context" \
    "Show daemon status" \
    "Tail daemon log" \
    "Clear peer view cache" \
    "Reload daemon" \
    "Stop daemon" \
    "Back/select another room" \
    "Quit")"; then
    exit 130
  fi
  case "$action" in
    0) join_agent ;;
    1) leave_agent ;;
    2) clear_agent ;;
    3) show_status ;;
    4) tail_daemon_log ;;
    5) clear_peer_view_cache ;;
    6) reload_daemon ;;
    7) stop_room ;;
    8) session=""; choose_room ;;
    9) exit 0 ;;
    *) echo "invalid action" >&2 ;;
  esac
done
