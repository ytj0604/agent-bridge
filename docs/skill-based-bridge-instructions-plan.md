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
  model that has completely forgotten Agent Bridge will rediscover it.
- Do not put the full current cheat sheet directly in `SKILL.md`. For Codex,
  the local skill guidance uses progressive disclosure: skill metadata is always
  visible, `SKILL.md` is loaded when triggered, and reference files are loaded
  only when needed. The skill body must stay short.
- The source of truth for command syntax remains the installed bridge commands,
  especially `agent_list_peers` and each command's `--help`.
- Claude Code and Codex may differ in skill discovery, install location,
  scoping, and refresh behavior. The implementation must verify both tools
  empirically instead of assuming identical semantics.

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
  - Can be generated from `bridge_instructions.model_cheat_sheet()` or kept as
    a checked-in template synchronized by tests.
- `references/recovery.md`
  - Watchdog, interrupt, cancel, clear, aggregate recovery flows.
- `references/anti-patterns.md`
  - Polling, spoofing response prompts, reading state files, manually typing
    `/clear`, sending huge inline bodies, response-time send guard mistakes.

Keep the body small enough that loading the skill is cheap. Long examples and
edge-case semantics belong in references.

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

- Copy/update the skill into the Codex user skill directory.
- Copy/update the skill into the Claude Code user skill directory.
- Use explicit begin/end ownership markers or a manifest so uninstall can
  remove only Agent Bridge-owned skill files.
- Support `--dry-run`.
- Make hook/shim install failures independent from skill install failures only
  if that failure mode is intentionally documented. Otherwise fail loudly so the
  operator knows bridge recall support was not installed.
- Add `--skip-skills` only if there is a clear operator need; default should be
  install skills.

Open questions to resolve before implementation:

- Exact Codex user-skill install path in the deployed environment.
- Exact Claude Code user-skill install path and whether project/user scope is
  preferable.
- Whether either tool requires restart/reload after skill install.
- Whether skill names can contain hyphens consistently in both tools.
- Whether Claude Code supports references with the same loading behavior and
  path conventions as Codex.

Do not hardcode unverified paths without a healthcheck and regression strategy.

## Healthcheck And Uninstall

Extend `bridge_healthcheck` with skill checks:

- `codex_agent_bridge_skill_installed`
- `claude_agent_bridge_skill_installed`
- `agent_bridge_skill_source_valid`
- Optional: `agent_bridge_skill_references_present`

Healthcheck should report:

- installed path
- whether `SKILL.md` exists
- whether the frontmatter has expected `name` and `description`
- whether references exist

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
- Probe prompt remains compact and points to `agent_list_peers` rather than
  duplicating the full cheat sheet.

Add behavioral/manual validation:

- Start fresh Codex and Claude Code sessions after install.
- Confirm the skill is visible/available in each tool.
- Ask a bridge-related question without showing command syntax and check whether
  the skill loads or is discoverable.
- Compact context or simulate a long session, then ask the agent to message a
  peer and verify it uses `agent_list_peers` or correct bridge commands.
- Verify a non-bridge coding task does not load the skill unnecessarily.

## Migration Strategy

Phase 1: Add skill source and install/healthcheck/uninstall support.

- Do not shrink probe yet.
- Validate skill availability in both tools.
- Keep existing `agent_list_peers` behavior unchanged.

Phase 2: Add tests and manual evals for skill-trigger behavior.

- Check whether task wording reliably triggers the skill.
- Tune skill description for discovery.
- Keep `SKILL.md` short; move details to references.

Phase 3: Shrink probe prompt.

- Remove detailed command manual content from `probe_prompt()`.
- Keep bootstrap rules and exact reply instruction.
- Run docs contract tests around probe compactness and required safety tokens.

Phase 4: Iterate on recovery UX.

- Consider adding a small model-facing command such as `agent_bridge_help` only
  if skill triggering remains unreliable.
- Consider making watchdog/bridge error messages explicitly say
  "run `agent_list_peers` if command syntax is unclear."

## Success Criteria

- Probe prompt is materially shorter.
- Agents can recover bridge command usage after compaction.
- Exact command contracts remain available through `agent_list_peers`.
- Installing Agent Bridge also installs bridge skill support for both Codex and
  Claude Code.
- Skill installation is visible in healthcheck and reversible by uninstall.
- Non-bridge tasks do not routinely load the bridge skill.

## Risks

- Skill trigger is heuristic and may not fire when the model forgets bridge
  exists.
- Different skill semantics between Codex and Claude Code may require separate
  packaging or install paths.
- A verbose skill can recreate the same context-bloat problem as the current
  probe prompt.
- Stale skill files can become worse than no skill if they diverge from command
  behavior. Runtime command help and `agent_list_peers` must stay authoritative.
- Automatically installing into user-level skill directories may surprise users
  unless install output and uninstall behavior are clear.
