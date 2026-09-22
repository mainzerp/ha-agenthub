# Documentation Map

Doc index for HA-AgentHub. Each topic has exactly one owning doc — update the
owner, do not duplicate content across files.

## Reading order

1. `README.md` (repo root) — features, agent inventory, quick start.
2. `docs/project/project-definition.md` — authoritative system description.
3. `docs/project/prime-directives.md` — binding architecture rules.
4. `docs/architecture.md` — component diagram, A2A layer, request flow.
5. Topic docs below, as needed.

## Owning docs

| Doc | Owns |
|---|---|
| `docs/project/project-definition.md` | Authoritative description of what the system is today |
| `docs/project/prime-directives.md` | Binding architecture constraints (13 directives) |
| `docs/project/lessons.md` | Athenaeum pointer + offline fallback facts (do not grow) |
| `docs/architecture.md` | Components, A2A protocol, request/data flow |
| `docs/api-reference.md` | HTTP/WS API surface |
| `docs/configuration.md` | Settings and configuration reference |
| `docs/deployment.md` | Install and deployment (compose, HACS) |
| `docs/user-guide.md` | End-user operation (+ screenshots) |
| `docs/plugin-development.md` | Plugin authoring contract |
| `docs/backup-restore.md` | Backup and restore procedures |
| `docs/troubleshooting.md` | Operational troubleshooting |
| `docs/style-guide.md` | Dashboard design tokens and visual conventions |
| `docs/CHANGELOG_ARCHIVE.md` | Pre-1.14 changelog history |
| `VERSION.md` | Current version + changelog (version carrier) |
| `TODO.md` | Long-term idea backlog and unscheduled candidates |
| `SECURITY.md` | Vulnerability reporting |
| `brain/` | Live session-spanning context (mission, goals, pipeline) |

## Conventions

- Current state only (prime-directive 10): describe what ships, label roadmap
  items explicitly and keep them in `TODO.md`.
- Keep docs concise and operational; delete stale notes rather than explaining
  history.
- `docs/SubAgent/` is gitignored — ephemeral subagent working files.
