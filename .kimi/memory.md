# Orchestrator Memory

> This file is the orchestrator's long-term memory across sessions.
> Read it at the start of every session to recall context.
> Append new learnings, patterns, and reminders as they emerge.
> Never delete existing entries unless they are explicitly superseded.

## Lessons Learned

- 2026-04-29: The container's pre-existing test failures (timer reroute, orchestrator routing cache, health endpoint, entity index rebuild, registry invalidation) have all been fixed. Future work on the container backend should run `pytest tests/ -n auto` before pushing to avoid regressions.
- 2026-04-29: `docker-compose_local.yml` is the correct compose file for local development (service `agent-assist`, volume `agent-assist-data`). The root `docker-compose.yml` pulls from GHCR and uses different names.
- 2026-04-29: When importing `custom_components.ha_agenthub` in container tests, mock `voluptuous` and `homeassistant.helpers.selector` in `conftest.py` before any import, or the import chain will fail.

## Recurring Patterns / Gotchas

### Container Tests
- Any test that mocks `ha_client` and calls `runtime_setup._prime_entity_index` or `_refresh_registry_entities` must provide `ha_client.get_hidden_entity_ids = AsyncMock(return_value=set())`.
- `asyncio.create_task` mocks in tests must accept `**kwargs` because Python 3.12 passes `name=...` by default.

### Integration (HA) Tests
- The integration's `config_flow.py` imports `voluptuous` and `homeassistant.helpers.selector` at module level. Any test that imports `conversation.py` (which imports `__init__.py`, which imports `config_flow.py`) will fail unless these modules are pre-mocked in `sys.modules`.

### Lint / CI
- `ruff check` and `ruff format` must both pass before pushing. The CI runs both.
- Container tests use `pytest-xdist` (`-n auto`) in CI but not necessarily locally.

## Active Conventions (evolving)

- Use `AsyncMock` for all async mocks, not plain `MagicMock`.
- When fixing a test, check if the fix belongs in the test (mock missing) or in production code (actual bug).
- The orchestrator's `_classify` method must return `user_text` on routing cache hits, not the stale `condensed_task`.

## Open / Carried Over

- None currently.

## Lessons Learned (2026-05-02)

- Added `build_scenario_backed_app()` to `conftest.py` to wire real orchestrator pipeline into FastAPI test apps. Must call `conversation_routes.set_dispatcher(handles.dispatcher)` after building pipeline so API routes use real dispatcher instead of mocks.
- `HAMimicClient` test helper uses `starlette.testclient.TestClient` wrapped in `asyncio.to_thread` for WebSocket testing inside async pytest, because `httpx` does not support WebSocket.
- When running scenarios through REST/WS layers, use a fresh app-per-transport pattern to prevent deterministic LLM stub state contamination between transports.
- Scenario parametrization through API layer: 102 YAML scenarios * 3 tests each (REST + WS + parity) = 306 tests. All pass deterministically with no real LLM calls.

## Lessons Learned (2026-05-02) -- Action-Audit Bridge Tests

- `ConversationResponse` and `StreamToken` models already had `action_executed` field in the model, but it was never populated by the REST/SSE/WS handlers. Adding `routed_agent` and wiring `action_executed` in `conversation.py` enables true black-box bridge tests.
- The orchestrator's streaming path (`handle_task_stream`) needed `routed_to` and `action_executed` added to the final `done=True` chunk so WS/SSE done frames carry the metadata.
- Internal `ActionExecuted` shapes may differ from public `ActionResult` model; a `_normalize_action_executed()` adapter helper in routes is useful.
- Bridge tests that assert ONLY on API responses (no `app.state` poking) are cleaner and survive refactors better, but require the API to expose the necessary metadata.

## Lessons Learned (2026-05-02) -- Deep Code Review

- `time.sleep()` inside `async def` blocks the entire asyncio event loop. Always use `await asyncio.sleep()` in async code. If the function must remain sync (e.g., called from sync context), split into sync core + async wrapper.
- CPU-bound work like `SentenceTransformer.encode()` must be offloaded with `asyncio.to_thread()` or `loop.run_in_executor()` when called from async code.
- Never concatenate user input into Jinja2 templates, even with regex validation. Always pass user data as template variables.
- `X-Forwarded-For` parsing must walk from the rightmost IP (closest to the server) to find the first non-trusted IP. The leftmost IP is trivially spoofable.
- `except Exception:` in bridge/transport code swallows programming errors and causes duplicate work or silent failures. Narrow to specific transport exceptions (`aiohttp.ClientError`, `asyncio.TimeoutError`, `OSError`).
- `while not queue.empty(): queue.get_nowait()` is a race condition in async code. Loop on `get_nowait()` and catch `QueueEmpty`.
- SSE ticker / background task registration needs deduplication guards and proper lifespan cleanup to prevent unbounded task leaks.
- Secret decryption should fail loudly (raise) rather than silently returning `None`, so callers know a key rotation occurred.
- Docker base images and external binaries should be pinned to exact versions with checksum verification. `apt-get install` without pinning is a supply-chain risk.
- `.dockerignore` must exclude sensitive files (`.github/`, `.vscode/`, `.kimi/`, docs, keys, credentials) from the build context.
- CI should run lint and at least smoke tests for ALL modules, including `custom_components/`.

## Lessons Learned (2026-05-02) -- Low-Priority Fixes

- When changing `ha_client.render_template` to accept `variables` kwarg, ALL test fixtures that create a default `ha_client` must be updated. `MagicMock()` cannot be awaited; use `AsyncMock()` with `render_template = AsyncMock(return_value="")` as default.
- Changing an options flow from writing to `data` to writing to `options` breaks container tests that assert on `async_update_entry` kwargs. Update both the integration AND the container-side tests.
- `pytest-xdist` (`-n auto`) can mask or expose different test failures than sequential runs due to test-order effects and shared state. Always verify with sequential run before declaring failure.
- Top-level `dict` → `dict[str, Any]` sweeps in `orchestrator.py` affect many public method signatures. `ruff check` catches missing imports, but type-checkers may need `from __future__ import annotations` or `typing.Any`.
- `asyncio.CancelledError` must be explicitly re-raised before general exception handlers in long-running loops (WS receive, task runners) to prevent cancellation swallowing.

## Meta-Workflow Fixes

- 2026-05-02: Plan subagent was consistently hanging in refinement loops, producing 50-80 KB plans with 30+ heading levels and endless "V1 vs V2" comparison tables. Fixed by adding hard anti-loop rules to AGENTS.md: 300-line/20 KB max plan size, no recursive file reading, no design-alternative sections, max 3 heading levels, max 5 acceptance criteria per item, one-pass output only.
- 2026-05-02: `explore` and `plan` subagent types have NO write access (built-in tool restrictions: explore = read/search/no-write, plan = read/search/no-write/no-shell). The workflow diagram incorrectly claimed they "create analysis doc" and "create plan doc". Fixed AGENTS.md: all three phases now use `coder` subagent_type with prompt-enforced tool restrictions. Research mode = ReadFile/Grep/Glob/WriteFile (docs/SubAgent only). Planning mode = ReadFile/Grep/Glob/WriteFile (docs/SubAgent only). Implementation mode = full toolset.
- 2026-05-02: Added parallel agent execution rules to AGENTS.md. Research: up to 3 parallel agents for separate modules, followed by a Synthesis agent. Implementation: up to 3 parallel agents for independent work streams, followed by a Merge & Verify agent. Planning remains strictly sequential. Fallback to sequential execution if Merge & Verify finds unresolvable conflicts.

## Critical Do-Nots

- **NEVER delete Docker volumes** (`docker compose down -v`, `docker volume rm`, etc.) unless the user *explicitly* requests it. This destroys the SQLite database, ChromaDB data, API keys, HA connection config, and all cached state. If a container fails to start due to data corruption, prefer `docker compose down` (without `-v`) and container recreation, or manual cleanup of specific files inside the volume. Losing a volume forces a full re-setup of the container.

## Live Environment Access

- 2026-05-04: Live deployment credentials are stored in `.env.local` (ignored by git). I can use these to authenticate against the live container at `http://192.168.120.200:6081` and obtain a session cookie for inspecting live logs via `/api/admin/logs`.
