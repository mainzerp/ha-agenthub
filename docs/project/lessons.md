# Learnings

> **Canonical store: the Athenaeum library (MCP server `athenaeum`).** Project
> learnings live there, not in this file. At the start of every session, recall
> them via `request_knowledge`; persist new learnings before the session ends via
> `store_knowledge`; correct existing entries via `update_knowledge`.
>
> This file is the **local fallback** for when the Athenaeum MCP server is
> unavailable. It intentionally carries only the operationally critical facts —
> do not grow it back into a full log; put new learnings into Athenaeum.

## Athenaeum Concepts (agent-assist learnings)

- `/athenaeum/ha-agenthub-testing-lessons` — test patterns, mock conventions, calibration trap, host gotchas
- `/athenaeum/ha-agenthub-async-security-anti-patterns` — event-loop discipline, exception handling, security pitfalls
- `/athenaeum/ha-agenthub-architecture-lessons` — orchestrator/routing, task envelope split, entity matching, HA integration, code structure
- `/athenaeum/ha-agenthub-devops-ci-release-lessons` — Docker, CI/lint/mypy baselines, release tagging, dashboard theming
- `/athenaeum/ha-agenthub-environment-live-access` — live URL, credentials format, CSRF login, Admin API debugging, container harness
- `/athenaeum/ha-agenthub-orchestrator-workflow-lessons` — subagent discipline, parallel execution, knowledge management

## Critical Fallback Facts

- **Tests:** pytest-xdist is NOT installed in the local venvs — run the container suite sequentially: `python -m pytest tests/ -q` from `container/` (~300s). From repo root: `.venv/Scripts/python -m pytest container/tests -q`.
- **Windows pytest hang:** the pytest process hangs on interpreter shutdown AFTER printing the full summary. Take verdicts from the printed summary lines; run long suites as a background task.
- **Lint:** `ruff check` and `ruff format` must both pass before every push.
- **Docker:** local development uses `container/docker-compose_local.yml` (not the root compose file). NEVER delete Docker volumes (`down -v`, `volume rm`) unless the user explicitly requests it.
- **Secrets:** `secrets/.env.local` is JSON blocks (`{"live": {...}, "local": {...}}`), NOT shell-sourceable KEY=VALUE — parse with python, never `source` it.
- **Dashboard login:** needs CSRF — GET `/dashboard/login`, then send the `agent_assist_csrf` cookie value BOTH as cookie and as `csrf_token` form field.
- **Live environment:** `http://192.168.120.200:6081`; live debugging works via the Admin API (`/api/admin/traces/{id}` span trees are the reliable evidence source).

---
*Migrated to Athenaeum on 2026-07-30. The full pre-migration history is in git.*
