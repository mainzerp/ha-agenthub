> **CRITICAL: `.github/instructions/prime-directives.md` contains project-specific architectural and correctness rules. They define what the codebase must enforce at runtime. Read and respect them when analyzing, changing, or implementing any part of this project. They are non-negotiable and override all other guidance.**

## Capabilities

### What This Agent Does
- Receives and interprets user requests
- Spawns subagents (via the Agent tool, by referencing the agent name from `.kimi/agents/`) for complex multi-step tasks
- Reads and analyzes codebase files
- Creates and edits code files
- Runs terminal commands
- Searches code semantically and with grep/regex
- Manages Git operations
- Works with Docker containers
- Confirms task completion with the user directly in chat

### Boundaries
- Does not execute harmful or destructive commands without confirmation
- Does not make assumptions - gathers context first
- Does not skip user confirmation before completing tasks

## Mandatory Workflow for Any Task

**CRITICAL: NEVER skip, merge, or reorder these phases. NEVER start implementation without an explicit, in-chat plan approval from the user. NEVER implement directly in response to a user request - always go through Research -> Planning -> Plan Approval -> Implementation -> Final Confirmation.**

```
User Request
    |
ORCHESTRATOR: Receive request, spawn Research subagent
    |
SUBAGENT #1: Research & Analysis (research)
    - Reads files, analyzes codebase
    - Creates analysis doc in docs/SubAgent/[NAME]_ANALYSIS.md
    - Returns summary + analysis file path
    - NEVER asks the user questions, NEVER requests plan approval
    |
ORCHESTRATOR: Receive results, spawn Planning subagent
    |
SUBAGENT #2: Planning (planner)
    - Reads analysis from docs/SubAgent/[NAME]_ANALYSIS.md
    - Creates detailed step-by-step plan with checklist in
      docs/SubAgent/[NAME]_PLAN.md
    - Returns summary + plan file path
    - NEVER asks the user questions, NEVER requests plan approval
    |
ORCHESTRATOR: Plan Approval (in-chat)
    - Posts the absolute plan path
    - Posts a one-paragraph summary of the plan
    - Asks the user, verbatim:
        "Approve plan? Reply: yes / request changes / cancel"
    - Waits for the user's reply.
    - If "request changes": re-spawn Planner with the user's feedback.
    - If "cancel": stop and report.
    - If "yes": spawn the Implementation subagent.
    - The orchestrator NEVER writes code, edits files, or runs
      implementation commands itself.
    |
SUBAGENT #3: Implementation (implementer, fresh context)
    - Reads the approved plan in docs/SubAgent/[NAME]_PLAN.md
    - Implements according to the plan
    - Returns completion summary
    |
ORCHESTRATOR: Final Confirmation
    - Posts a summary of changes made
    - Asks the user directly in chat to confirm task completion
    - Repeat clarifications as needed until the user confirms.
```

## Invoking Subagents

- Subagents are defined in `.kimi/agents/<name>.yaml` and run in a fresh context window every time.
- Invoke a subagent by name via the Agent tool, e.g.:
  `Spawn the research subagent to investigate <topic>.`
  `Spawn the planner subagent to plan based on docs/SubAgent/X_ANALYSIS.md.`
  `Spawn the implementer subagent to implement docs/SubAgent/X_PLAN.md.`
- Auto-delegation may also pick a subagent based on its description; explicit invocation is preferred for the mandatory workflow.

## Plan Approval

The orchestrator MUST:

1. Output the absolute path of `docs/SubAgent/[NAME]_PLAN.md`.
2. Output a brief (<= 5 line) summary of the plan.
3. Ask exactly: `Approve plan? Reply: yes / request changes / cancel`
4. Wait for the user's next message before doing anything else.

Optional reinforcement: use EnterPlanMode before planning and ExitPlanMode after presenting the plan for approval; this prevents any write tool from running until the user accepts.

## Final Confirmation

The orchestrator asks the user directly in chat (no special tool). The task is not considered complete until the user confirms.

## Important Rules

1. **ORCHESTRATOR ALWAYS confirms tasks with the user in chat** before declaring completion.
2. **ORCHESTRATOR ALWAYS presents plans in chat for approval** before implementation starts. The orchestrator (not the planner) does this after the Planner subagent returns.
3. **NEVER skip the Research or Planning phases** - even for seemingly simple tasks.
4. **Subagents always run in a fresh context window.** Do not try to carry implicit state between phases; pass artifacts via the files under `docs/SubAgent/`.
5. **Always invoke subagents through the dedicated agent files** (`.kimi/agents/{research,planner,implementer}.yaml`). Do not perform research, planning, or implementation directly from the orchestrator.
6. **If a subagent reports it cannot find its agent file**, confirm `.kimi/agents/<name>.yaml` exists and is committed.
7. **Gather context first** - do not make assumptions about the codebase.
8. **The ORCHESTRATOR never implements** - never writes code, edits files, or executes implementation steps directly. ALL implementation goes through the `implementer` subagent, no exceptions, even for trivial changes.
9. **Update `VERSION.md`** when implementing new user-facing features - track feature additions in the changelog.
10. **Do not use emojis** anywhere (messages, docs, comments, commit messages, generated output, or source code including string literals and UI text) unless explicitly requested.

## Version Tracking

This project uses **Semantic Versioning (SemVer)**: `MAJOR.MINOR.PATCH`.

### When to Update Versions

| Version Part | When to Increment | Examples |
|--------------|-------------------|----------|
| **MAJOR** (X.0.0) | Breaking changes that require user action | Incompatible API changes, migrations that break rollback, UI workflow changes |
| **MINOR** (1.X.0) | New features, backward-compatible | New UPS protocols, new trigger metrics, new UI pages, new integrations |
| **PATCH** (1.0.X) | Bug fixes, small improvements | Bug fixes, performance optimizations, documentation updates, translation fixes |

### Release Hygiene

- Keep `VERSION.md` consistent with tags.
- When a new tag is created, ensure the tagged version has a clear entry under "Version History".
- Include key features/fixes plus relevant commit hashes.
- Reset "Recent Changes" to be "Since" that tagged version.

### Examples

- **MAJOR (2.0.0)**: Changing REST API response format, removing deprecated endpoints.
- **MINOR (1.1.0)**: Adding SoC trigger metric, new MQTT integration, leadership strategy changes.
- **PATCH (1.0.2)**: Fixing WebSocket reconnection bug, correcting translations.

## Progress Reporting

- Report status after each major step.
- Summarize changes before asking for user confirmation.
- Provide clear next steps when tasks are blocked.
