# Learnings

> **Canonical store: the Athenaeum library (MCP server `athenaeum`).** Project
> learnings live there, not in this file. At the start of every session, recall
> them via `request_knowledge`; persist new learnings before the session ends via
> `store_knowledge`; correct existing entries via `update_knowledge`.
>
> This file is the **local fallback** for when the Athenaeum MCP server is
> unavailable. It intentionally carries only the operationally critical facts —
> do not grow it back into a full log; put new learnings into Athenaeum.

## Athenaeum Concepts (HA-AgentHub learnings)

- `/athenaeum/ha-agenthub-testing-lessons` — test patterns, mock conventions, calibration trap, host gotchas
- `/athenaeum/ha-agenthub-async-security-anti-patterns` — event-loop discipline, exception handling, security pitfalls
- `/athenaeum/ha-agenthub-architecture-lessons` — orchestrator/routing, task envelope split, entity matching, HA integration, code structure
- `/athenaeum/ha-agenthub-devops-ci-release-lessons` — Docker, CI/lint/mypy baselines, release tagging, dashboard theming
- `/athenaeum/ha-agenthub-environment-live-access` — live URL, credentials format, CSRF login, Admin API debugging, container harness
- `/athenaeum/ha-agenthub-orchestrator-workflow-lessons` — subagent discipline, parallel execution, knowledge management

## Critical Fallback Facts

- **Review regression coverage:** Test action-cache storage and replay together with distinct origin rooms and the executor's actual service/parameters; manually constructed cache entries miss serialization defects. Test config-entry migrations with conflicting `data` and legacy `options`, not only missing keys.

- **Tests:** pytest-xdist is installed in the local venv. Run the container suite sequentially when the approved verification command requires it: `python -m pytest tests/ -q` from `container/` (~300s). From repo root: `.venv/Scripts/python -m pytest container/tests -q`.
- **Windows pytest hang:** the pytest process hangs on interpreter shutdown AFTER printing the full summary. Take verdicts from the printed summary lines; run long suites as a background task.
- **Dashboard CSS:** components.css loads after layout.css; shared responsive overrides must respect that cascade. Read/write assets explicitly as UTF-8: a mojibake BOM before the first selector can silently discard it. Confirm computed fonts/colors in a rendered page after shared-style changes.
- **Lint:** `ruff check` and `ruff format` must both pass before every push.
- **Docker:** local development uses `container/docker-compose_local.yml` (not the root compose file). NEVER delete Docker volumes (`down -v`, `volume rm`) unless the user explicitly requests it.
- **Secrets:** `secrets/.env.local` is shell-sourceable KEY=VALUE — `source secrets/.env.local`. It defines `AA_LIVE_*` (live instance), `AA_LOCAL_*` (local test instance), and the active `AA_BASE_URL`/`AA_USERNAME`/`AA_PASSWORD` selection used by the skills.
- **Agent registration pitfall:** the `@agent` decorator only runs at import time — every decorated agent module MUST be imported in `container/app/agents/__init__.py`. `install_all_agents` skips unregistered classes silently (`cls_info is None -> continue`). Guarded by `container/tests/test_agent_registration.py`.
- **Dashboard login:** needs CSRF — GET `/dashboard/login`, then send the `agent_assist_csrf` cookie value BOTH as cookie and as `csrf_token` form field.
- **Live environment:** `http://192.168.120.200:6081`; live debugging works via the Admin API (`/api/admin/traces/{id}` span trees are the reliable evidence source).

---
*Migrated to Athenaeum on 2026-07-30. The full pre-migration history is in git.*
