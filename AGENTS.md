# Repository Guidelines

## Project Structure & Module Organization
Agent Bridge is a tmux-based Python/Bash utility. Core modules live in `libexec/agent-bridge/`; shared shell setup is in `bridge_common.sh`. Human wrappers are in `bin/`; agent shims are in `model-bin/` and delegate into `bridge_*.py`. Hooks live in `hooks/`, checks in `scripts/`, docs in `docs/`, and data in `state/`, `run/`, and `log/`.

## Architecture Overview
Start at `bin/agent-bridge` and `libexec/agent-bridge/bridge_attach.py`: the human-facing dispatcher creates or manages rooms, while attach flow discovers Claude/Codex panes, chooses aliases, creates room state, and ensures a daemon. `bin/bridge_run.sh` and `bin/bridge_manage.sh` remain compatibility wrappers. Runtime paths are centralized in `bridge_paths.py`; participant/session validation is in `bridge_participants.py` and `bridge_identity.py`.

`bridge_daemon.py` is the routing core. It owns the queue store, command socket, watchdogs, interrupt holds, aggregate replies, and tmux prompt injection. Model commands call Bash shims, then entrypoints like `bridge_send_peer.py`, `bridge_view_peer.py`, `bridge_alarm.py`, and `bridge_interrupt_peer.py`. Hook events enter through `hooks/bridge-hook` and `bridge_hook_logger.py`; the daemon reacts to `prompt_submitted`, `response_finished`, and capture records.

## Build, Test, and Development Commands
There is no compile step. Common commands:

```bash
./install.sh --dry-run              # preview shim and hook installation
./install.sh --yes                  # install shims and hooks non-interactively
bin/agent-bridge healthcheck        # validate local dependencies and config
python3 scripts/run_regressions.py  # run daemon regression checks
./uninstall.sh --dry-run            # preview uninstall behavior
```

After installation, `agent-bridge` opens the human-facing menu; `agent-bridge attach` attaches a room to tmux panes, and `agent-bridge manage` inspects or stops active rooms. `bridge_run` and `bridge_manage` remain compatibility commands.
Model-facing sends support both `agent_send_peer --to <alias> 'message'` and shorthand `agent_send_peer <alias> 'message'`. Put options before the inline body text; options may follow `--to`/`--all` or an implicit leading alias. Use `--stdin` with a quoted heredoc for complex bodies. `--allow-spoof` must precede the destination.

## Coding Style & Naming Conventions
Target Python 3.10+ and Bash 4+. Use 4-space Python indentation, `snake_case`, and type hints where useful. Keep shell scripts executable, with `#!/usr/bin/env bash` and `set -euo pipefail`. Preserve `bridge_*` for bridge internals and `agent_*` for model-facing commands. No project-wide formatter is configured.

## Testing Guidelines
The main regression suite is `scripts/run_regressions.py`; it runs the daemon in `dry_run` mode against temporary state. Add `scenario_<behavior>` functions under `scripts/regression/` for daemon lifecycle, queue, interrupt, watchdog, or aggregate changes. For install, hook, or wrapper changes, run `./install.sh --dry-run` and `bin/agent-bridge healthcheck`. Manually exercise `agent-bridge attach` for tmux delivery changes when practical.

## Commit & Pull Request Guidelines
Recent history uses short, capitalized imperative subjects such as `Add daemon reload`, `Fix capture bug`, and `Update session identity issue`. Keep subjects focused, preferably under 72 characters. PRs should describe behavior changes, list validation commands, note manual tmux testing, and call out hook, shim, runtime path, or uninstall changes.

## Security & Configuration Tips
Do not commit local runtime contents from `state/`, `run/`, or `log/`. Be careful with hook edits: they affect active Claude/Codex sessions after restart. Prefer `--dry-run` flags before changing install, uninstall, or hook behavior.
