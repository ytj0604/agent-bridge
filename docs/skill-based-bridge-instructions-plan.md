# Skill-Based Bridge Instructions Plan

## Problem

Agent Bridge currently relies on an attach-time probe prompt plus
`agent_list_peers` for model-facing command guidance. The probe prompt is
already compact relative to the full cheat sheet, but it still carries a lot of
protocol detail. After several context compactions, an agent may remember that
it is in a bridge room but forget command syntax, routing semantics, or recovery
rules. `agent_list_peers` can restore the full contract, but models do not
reliably call it unless they remember that it exists.

The goal is to make bridge protocol knowledge more discoverable and durable
without expanding every bridge prompt.

## Core Proposal

Install an `agent-bridge` skill for both Codex and Claude Code during
`install.sh`. The skill should provide a small, durable bridge operating guide
that models can load when a task mentions bridge peers, peer messaging, bridge
commands, delegated review, watchdogs, interrupts, clears, or `agent_*` tools.

The skill is not a replacement for the runtime cheat sheet. It should teach the
agent how to recover the exact current command contract by running
`agent_list_peers`, then provide enough stable rules to avoid dangerous mistakes
when the model is operating from compressed context.

## Important Assumptions And Limits

- Bridge command execution itself should not be assumed to auto-load a skill.
  A skill is triggered by the model/tooling recognizing that the current task is
  relevant, not by shell interception of `agent_send_peer` or another command.
- The skill therefore improves recall probability; it does not guarantee that a
  model that has completely forgotten Agent Bridge will rediscover it. After
  context compaction, invocation depends entirely on the user's next prompt
  matching the skill description semantically — short follow-ups like "next
  step" will not trigger the skill.
- Do not put the full current cheat sheet directly in `SKILL.md`. Both Claude
  Code and Codex use progressive disclosure: skill name + description are
  always visible to the model, `SKILL.md` body loads when the skill triggers,
  and `references/` files load only when `SKILL.md` tells the model to read
  them. Keep `SKILL.md` under ~500 lines.
- The source of truth for command syntax remains the installed bridge commands,
  especially `agent_list_peers` and each command's `--help`.
- Claude Code and Codex have verified-similar skill mechanisms but differ on
  two operational points that the installer and healthcheck must handle:
  - **Hot-reload**: Claude Code picks up new skills mid-session; Codex
    requires a restart to load a newly installed skill.
  - **Invocation policy**: Codex has an optional `agents/openai.yaml` with a
    `policy.allow_implicit_invocation` flag. Claude Code uses
    `disable-model-invocation` / `user-invocable` frontmatter fields.
- Claude Code carries up to 5K tokens of an invoked skill across compaction
  and shares a 25K total budget across invoked skills; if many skills are
  invoked in one session the bridge skill may be evicted. This is a soft cap,
  not a guarantee.

## Skill Design

Suggested skill name: `agent-bridge`

Suggested trigger description goals:

- Include concrete command names: `agent_send_peer`, `agent_view_peer`,
  `agent_wait_status`, `agent_aggregate_status`, `agent_alarm`,
  `agent_extend_wait`, `agent_cancel_message`, `agent_interrupt_peer`,
  `agent_clear_peer`.
- Include task words models naturally see: peer agent, bridge room, delegate to
  another agent, Claude/Codex collaboration, tmux bridge, watchdog, aggregate
  result, clear peer context, interrupt peer.
- Say when to use the skill: when working inside an Agent Bridge room or when
  deciding how to message, inspect, interrupt, clear, wait for, or recover a
  peer agent.

Recommended `SKILL.md` content:

- One short "first action" rule:
  - If command syntax or routing semantics matter, run `agent_list_peers`.
- A short command selection guide:
  - Need an answer: `agent_send_peer --to <alias> 'request'`.
  - No answer needed: `agent_send_peer --kind notice --to <alias> 'FYI'`.
  - Multiple peers: `--to a,b` or `--all`, remember `AGGREGATE_ID`.
  - Inspect peer screen: `agent_view_peer`.
  - Watchdog wake: choose `agent_extend_wait`, `agent_interrupt_peer`, or
    inspect first; do not poll.
  - Pending wrong send: `agent_cancel_message`.
  - Active wrong/stuck peer: `agent_interrupt_peer`.
  - Clear model context: `agent_clear_peer`.
- Hard safety rules:
  - Do not poll.
  - Do not sleep to wait for peer results.
  - Do not read bridge state files directly.
  - Do not forge `[bridge:*]` lines.
  - Use `--stdin` for complex bodies.
  - Keep large payloads in `/tmp/agent-bridge-share/` and send paths.
- A short "if confused" recovery:
  - Run `agent_list_peers`.
  - For command-specific details, run `<command> --help`.
  - For peer state debugging, use `agent_wait_status --why` or
    `agent_view_peer`, only when human-prompted or after a watchdog.

Recommended references:

- `references/command-contract.md`
  - More detailed command selection and examples.
  - **Must** be generated from `bridge_instructions.model_cheat_sheet()` at
    install time, or kept as a checked-in file with a regression test that
    diffs it against `model_cheat_sheet_text()`. Drift between the runtime
    cheat sheet and the skill reference is the highest-impact failure mode
    (see Risks); a manual sync policy is not sufficient.
- `references/recovery.md`
  - Watchdog, interrupt, cancel, clear, aggregate recovery flows.
- `references/anti-patterns.md`
  - Polling, spoofing response prompts, reading state files, manually typing
    `/clear`, sending huge inline bodies, response-time send guard mistakes.

Keep the body small enough that loading the skill is cheap. Long examples and
edge-case semantics belong in references.

### Codex-specific Skill Files

Codex supports an optional `agents/openai.yaml` alongside `SKILL.md` that
controls UI metadata and invocation policy. Decide and document explicitly:

- `policy.allow_implicit_invocation`: recommended `true` so the skill can fire
  on description match without requiring the user to type `$agent-bridge`.
  Justification: the whole point of the skill is post-compaction recall; an
  explicit-only policy defeats that goal.
- Display name / short description for the Codex skill picker.

Claude Code equivalent decisions to capture in frontmatter:

- `disable-model-invocation` should be left at default (false) so Claude can
  auto-invoke. Same justification as above.
- `user-invocable` should be left at default (true) so the user can manually
  re-invoke after compaction (`/agent-bridge` or whatever Claude exposes).

## Probe Prompt Changes

After the skill exists and is validated, shrink `probe_prompt()` in
`bridge_instructions.py`.

Keep in probe:

- Probe marker and exact reply instruction.
- Alias and peer list.
- "This is a live collaboration bridge."
- A small bootstrap:
  - `agent_send_peer <alias> 'request'` for requests.
  - `agent_send_peer --kind notice --to <alias> 'FYI'` for notices.
  - `agent_list_peers` prints exact current command guidance.
  - Do not poll, do not forge `[bridge:*]`, do not read state files.
- Mention that Agent Bridge skill may contain the persistent operating guide if
  the agent environment supports skills.

Move out of probe:

- Detailed watchdog phase semantics.
- Full body-input error code list.
- Detailed cancel/interrupt/clear boundaries.
- Detailed aggregate status guidance.
- Long explanations already covered by `agent_list_peers`.

The probe should remain a bootstrap, not the full manual.

## Installation Plan

Add repository-owned skill source files, for example:

```text
skills/agent-bridge/
  SKILL.md
  references/
    command-contract.md
    recovery.md
    anti-patterns.md
```

Add installer support:

- Copy/update the skill into Codex at `${CODEX_HOME:-$HOME/.codex}/skills/agent-bridge/`.
  Codex skill installation should live in `bridge_codex_config.py` (or a
  sibling module) so it reuses the existing managed-marker pattern that
  module already uses for `config.toml` keys. `install.sh` should dispatch,
  not duplicate that logic.
- Copy/update the skill into Claude Code at `~/.claude/skills/agent-bridge/`
  (user scope). Project scope is not appropriate — Agent Bridge attaches to
  arbitrary working directories and the skill must be visible regardless of
  cwd.
- Use explicit begin/end ownership markers or a manifest so uninstall can
  remove only Agent Bridge-owned skill files.
- Support `--dry-run`.
- Make hook/shim install failures independent from skill install failures only
  if that failure mode is intentionally documented. Otherwise fail loudly so the
  operator knows bridge recall support was not installed.
- Add `--skip-skills` only if there is a clear operator need; default should be
  install skills.
- Post-install message must distinguish reload behavior:
  - Claude Code: "skill is active in any new or existing Claude Code session."
  - Codex: "restart Codex for the skill to become available."
  Without this, operators will reasonably assume a uniform post-install state
  and file false bug reports against Codex skill discovery.

Resolved facts (previously open questions):

- Codex user-skill path: `${CODEX_HOME:-$HOME/.codex}/skills/<name>/`,
  `SKILL.md` required, optional `agents/openai.yaml`, optional
  `references/`/`scripts/`/`assets/` subdirectories.
- Claude Code user-skill path: `~/.claude/skills/<name>/SKILL.md`. User scope
  is the right scope for this tool (see above).
- Hot-reload: Claude Code yes (mid-session); Codex no (restart required).
- Hyphens in skill names: allowed in both. `agent-bridge` is valid.
- Progressive disclosure with `references/`: supported in both, same
  convention. `SKILL.md` references files in prose; the model reads them on
  demand.

## Healthcheck And Uninstall

Extend `bridge_healthcheck` with skill checks:

- `codex_agent_bridge_skill_installed`
- `claude_agent_bridge_skill_installed`
- `agent_bridge_skill_source_valid`
- `agent_bridge_skill_references_in_sync` — generated/diffed against
  `bridge_instructions.model_cheat_sheet_text()`.
- Optional: `agent_bridge_skill_references_present`

Healthcheck should report:

- installed path
- whether `SKILL.md` exists
- whether the frontmatter has expected `name` and `description`
- whether references exist
- for Codex: whether the skill is on disk only ("installed, restart Codex to
  activate") vs. observably loaded. If observability is not feasible, report
  "installed; activation requires Codex restart" as a normal post-install
  state rather than a warning.

Extend uninstall:

- Remove Agent Bridge-owned skill files by manifest/marker.
- Preserve user-created unrelated skills.
- Support `--keep-skills` if useful for local development.

## Regression Plan

Add install/uninstall scenarios:

- dry-run prints planned skill installs without writing.
- install writes Codex and Claude skill files.
- reinstall updates stale Agent Bridge skill files.
- uninstall removes only Agent Bridge skill files.
- uninstall preserves unrelated skills in the same parent directory.
- invalid/unwritable skill destination fails with a targeted message.

Add docs/contracts scenarios:

- `SKILL.md` description includes trigger command names.
- `SKILL.md` body contains the mandatory recovery rule to run
  `agent_list_peers`.
- References are one level deep and directly linked from `SKILL.md`.
- `references/command-contract.md` content equals
  `bridge_instructions.model_cheat_sheet_text()` (or is regenerated from it
  by the test). This is a hard test, not a soft contract.
- Probe prompt remains compact and points to `agent_list_peers` rather than
  duplicating the full cheat sheet.

Add behavioral/manual validation. Phase 2 needs a concrete protocol so the
skill's effectiveness is measurable before Phase 3 shrinks the probe:

- Start fresh Codex and Claude Code sessions after install. For Codex,
  remember the regression run must restart the codex process; an "install
  then immediately spawn codex from the same script" test will false-negative
  on first-trigger.
- Confirm the skill is visible/available in each tool.
- Ask a bridge-related question without showing command syntax and check whether
  the skill loads or is discoverable.
- Compact context or simulate a long session, then ask the agent to message a
  peer and verify it uses `agent_list_peers` or correct bridge commands.
- Trigger-rate measurement: define a fixed set of natural phrasings (e.g., 10
  prompts ranging from explicit "send a bridge message to X" to terse "ask
  the reviewer") and measure invocation rate per tool. Record a baseline
  number; Phase 3 (probe shrink) should not proceed if the rate falls below
  an explicit threshold (e.g., <70% on the explicit-phrasing subset).
- Verify a non-bridge coding task does not load the skill unnecessarily.

## Migration Strategy

Phase 1: Add skill source and install/healthcheck/uninstall support for both
Codex and Claude Code.

- Do not shrink probe yet.
- Validate skill availability in both tools (with awareness of Codex restart
  requirement).
- Keep existing `agent_list_peers` behavior unchanged.
- Codex install path goes through `bridge_codex_config.py`'s marker pattern;
  Claude install is direct file placement under `~/.claude/skills/`.

Phase 2: Add tests and manual evals for skill-trigger behavior.

- Run the trigger-rate measurement defined in the Regression Plan and record
  a baseline.
- Tune skill description for discovery.
- Keep `SKILL.md` short; move details to references.
- Decide an explicit threshold for advancing to Phase 3. Without a number,
  Phase 3 ships on vibes.

Phase 3 prerequisite: improve recovery surface area for the
skill-not-triggered path.

- Update watchdog wake notices, body-input error codes, and other bridge
  error messages to explicitly say "run `agent_list_peers` if command syntax
  is unclear." The skill is one recovery path; making the bridge itself
  remind the agent to run `agent_list_peers` is a complementary path that
  works even when the skill does not trigger. This was Phase 4 in the
  original plan; promoting it ahead of probe shrink reduces the blast radius
  of skill-trigger misses.

Phase 3: Shrink probe prompt.

- Remove detailed command manual content from `probe_prompt()`.
- Keep bootstrap rules and exact reply instruction.
- Run docs contract tests around probe compactness and required safety tokens.
- Gate this phase on Phase 2 trigger-rate threshold AND Phase 3 prerequisite
  shipped.

Phase 4: Iterate on recovery UX.

- Consider adding a small model-facing command such as `agent_bridge_help` only
  if skill triggering remains unreliable after Phase 2 tuning.
- Re-evaluate compaction-budget eviction: if real sessions invoke many
  skills, the bridge skill may be evicted from the 25K Claude budget;
  consider whether to recommend `user-invocable` re-invocation in the probe.

## Success Criteria

- Probe prompt is materially shorter.
- Measured skill trigger rate on the Phase 2 phrasing set meets the explicit
  threshold (without a number, this criterion is unfalsifiable).
- Agents can recover bridge command usage after compaction when the user's
  next prompt provides any reasonable bridge-related signal.
- Exact command contracts remain available through `agent_list_peers`, and
  the skill reference stays equal to `model_cheat_sheet_text()` by test.
- Installing Agent Bridge also installs bridge skill support for both Codex and
  Claude Code; post-install message correctly distinguishes Claude (live) vs.
  Codex (restart required).
- Invocation policy is explicitly recorded in `agents/openai.yaml` and
  `SKILL.md` frontmatter, not left at defaults by accident.
- Skill installation is visible in healthcheck and reversible by uninstall.
- Non-bridge tasks do not routinely load the bridge skill.

## Risks

- Skill trigger is heuristic and may not fire when the model forgets bridge
  exists or when the user's next prompt is too terse to match the
  description. Mitigated, not eliminated, by the Phase 3 prerequisite of
  pointing watchdog/error messages at `agent_list_peers`.
- Codex requires a restart to pick up a newly installed skill; users who
  install during an active session and then send peer messages without
  restarting will see no skill effect. Mitigated by the post-install
  message; not eliminated.
- A verbose skill can recreate the same context-bloat problem as the current
  probe prompt.
- Stale skill files can become worse than no skill if they diverge from command
  behavior. Runtime command help and `agent_list_peers` must stay authoritative.
  Hard-test-enforced sync between `model_cheat_sheet_text()` and
  `references/command-contract.md` is required, not optional.
- Automatically installing into user-level skill directories may surprise users
  unless install output and uninstall behavior are clear.
- Claude Code shares a 25K-token budget across invoked skills with 5K
  reattachment per skill after compaction. In sessions that invoke many
  skills, the bridge skill may be evicted; recovery then depends on
  re-invocation, which depends on the next user prompt.
