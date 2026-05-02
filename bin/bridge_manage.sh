#!/usr/bin/env bash
set -euo pipefail

_bridge_bin_dir="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve().parent)' "${BASH_SOURCE[0]}")"
exec "$_bridge_bin_dir/agent-bridge" manage "$@"
