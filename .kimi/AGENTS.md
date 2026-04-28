# HA-AgentHub Agent Instructions

This file contains project-specific guidance for AI agents working on the HA-AgentHub repository.

## Critical References

- **Prime Directives**: `.github/instructions/prime-directives.md` — architectural and correctness rules that are non-negotiable.
- **Project Definition**: `.github/instructions/project-definition.md` — full project overview, architecture, and current runtime behavior.

## Agent Configuration

This project uses Kimi Code CLI with custom agents defined in `.kimi/`:

- **Orchestrator**: `.kimi/orchestrator.yaml` — main agent configuration
- **Research**: `.kimi/agents/research.yaml` — read-only analysis subagent
- **Planner**: `.kimi/agents/planner.yaml` — read-only planning subagent
- **Implementer**: `.kimi/agents/implementer.yaml` — implementation subagent

Load the orchestrator with:

```bash
kimi --agent-file .kimi/orchestrator.yaml
```

## Workflow

All non-trivial tasks MUST follow the Research -> Planning -> Plan Approval -> Implementation -> Final Confirmation workflow.

1. **Research**: Spawn the `research` subagent to produce `docs/SubAgent/<NAME>_ANALYSIS.md`.
2. **Planning**: Spawn the `planner` subagent to produce `docs/SubAgent/<NAME>_PLAN.md`.
3. **Plan Approval**: Present the plan in chat and wait for explicit user approval.
4. **Implementation**: Spawn the `implementer` subagent to execute the approved plan.
5. **Final Confirmation**: Summarize changes and ask the user to confirm completion.

The orchestrator NEVER writes code, edits files, or runs implementation commands directly.

## Important Rules

- Do not use emojis anywhere (messages, docs, comments, commit messages, generated output, or source code) unless explicitly requested.
- Update `VERSION.md` when implementing new user-facing features.
- Use Semantic Versioning (SemVer): `MAJOR.MINOR.PATCH`.
- Current-state documentation only; roadmap items belong in `docs/roadmap.md`.
- Prime directives always override all other guidance.
