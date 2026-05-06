# Learnings

> This file tracks accumulated lessons, patterns, and preferences across sessions.
> Read it at the start of every session.
> Update it before ending the session — even if nothing new was discovered.
> If existing patterns held, add a brief note confirming that.

## What Has Worked

### Testing — Container Test Regressions
- **Pattern:** Run `pytest tests/ -n auto` before pushing to catch regressions early.
- **When to use:** Any future work on the container backend.

### Testing — Integration Import Mocking
- **Pattern:** Mock `voluptuous` and `homeassistant.helpers.selector` in `conftest.py` (or `sys.modules`) before any import of `custom_components.ha_agenthub`.
- **When to use:** Container tests that import the integration, or any test importing `conversation.py` (which chains through `__init__.py` to `config_flow.py`).

### Testing — Async Mock Conventions
- **Pattern:** Use `AsyncMock` for all async mocks, never plain `MagicMock`.
- **When to use:** All async mocks in tests.
- **Pattern:** When mocking `asyncio.create_task`, the mock must accept `**kwargs` because Python 3.12 passes `name=...` by default.
- **When to use:** Any test that patches `asyncio.create_task`.
- **Pattern:** Tests mocking `ha_client` and calling `runtime_setup._prime_entity_index` or `_refresh_registry_entities` must provide `ha_client.get_hidden_entity_ids = AsyncMock(return_value=set())`.
- **When to use:** Container tests that prime the entity index.

### Testing — Test vs Production Fix Discipline
- **Pattern:** When a test fails, determine whether the fix belongs in the test (missing mock) or in production code (actual bug).
- **When to use:** Every test-fixing session.

### Testing — Scenario-Backed App Builder
- **Pattern:** `build_scenario_backed_app()` in `conftest.py` wires the real orchestrator pipeline into FastAPI test apps. Call `conversation_routes.set_dispatcher(handles.dispatcher)` after building the pipeline so API routes use the real dispatcher instead of mocks.
- **When to use:** API-layer scenario tests.

### Testing — WebSocket Test Helper
- **Pattern:** `HAMimicClient` uses `starlette.testclient.TestClient` wrapped in `asyncio.to_thread` for WebSocket testing inside async pytest, because `httpx` does not support WebSocket.
- **When to use:** WebSocket transport tests.

### Testing — Transport Isolation
- **Pattern:** Use a fresh app-per-transport when running scenarios through REST/WS layers to prevent deterministic LLM stub state contamination between transports.
- **When to use:** Multi-transport scenario parametrization.

### Testing — pytest-xdist with Sequential Fallback
- **Pattern:** CI uses `pytest-xdist` (`-n auto`), but always verify with a sequential run before declaring a failure real.
- **When to use:** When `pytest -n auto` shows unexpected failures.

### Architecture — Orchestrator Routing Cache
- **Pattern:** The orchestrator's `_classify` method must return `user_text` on routing cache hits, not the stale `condensed_task`.
- **When to use:** Any change to orchestrator classification or caching logic.

### Architecture — Streaming Metadata Bridge
- **Pattern:** The orchestrator's streaming path (`handle_task_stream`) needs `routed_to` and `action_executed` added to the final `done=True` chunk so WS/SSE done frames carry the metadata.
- **When to use:** Changes to streaming output or action-audit bridges.
- **Pattern:** A `_normalize_action_executed()` adapter helper in routes is useful when internal `ActionExecuted` shapes differ from the public `ActionResult` model.
- **When to use:** Bridging internal actions to public API responses.

### Architecture — Black-Box Bridge Tests
- **Pattern:** Assert ONLY on API responses (no `app.state` poking). They are cleaner and survive refactors better, but require the API to expose the necessary metadata.
- **When to use:** Writing bridge/transport tests.

### Architecture — Plan Subagent Guardrails
- **Pattern:** Hard anti-loop rules keep plan subagents focused: 300-line / 20 KB max plan size, no recursive file reading, no design-alternative sections, max 3 heading levels, max 5 acceptance criteria per item, one-pass output only.
- **When to use:** Every planning phase.

### Architecture — Subagent Type Discipline
- **Pattern:** Always set `subagent_type="coder"` for every subagent invocation. Use prompt-enforced tool restrictions (not built-in types) to control access: Research = Read/Grep/Glob/WriteFile (docs only); Planning = Read/Grep/Glob/WriteFile (docs only); Implementation = full toolset.
- **When to use:** Every subagent spawn.

### Architecture — Parallel Agent Execution
- **Pattern:** Up to 3 parallel research agents (one per distinct module) followed by a Synthesis agent. Up to 3 parallel implementation agents (one per independent work stream) followed by a Merge & Verify agent. Planning remains strictly sequential.
- **When to use:** Multi-domain research or implementation tasks.

### DevOps — Local Docker Compose
- **Pattern:** `docker-compose_local.yml` is the correct compose file for local development (service `agent-assist`, volume `agent-assist-data`). The root `docker-compose.yml` pulls from GHCR and uses different names.
- **When to use:** Local container development.

### DevOps — Docker Volume Safety
- **Pattern:** Never delete Docker volumes (`docker compose down -v`, `docker volume rm`, etc.) unless the user explicitly requests it. If a container fails to start due to data corruption, prefer `docker compose down` (without `-v`) and container recreation, or manual cleanup of specific files inside the volume.
- **When to use:** Any container troubleshooting.

### DevOps — Supply-Chain Hardening
- **Pattern:** Pin Docker base images and external binaries to exact versions with checksum verification.
- **When to use:** Dockerfile changes or dependency upgrades.
- **Pattern:** `.dockerignore` must exclude sensitive files (`.github/`, `.vscode/`, `.kimi/`, docs, keys, credentials) from the build context.
- **When to use:** Reviewing Docker build context.

### Integration — Timer-Agent Domain Cleanup
- **Pattern:** Remove dead domains and code confirmed unused via grep. The timer-agent's historical `calendar` and `input_datetime` domains were safely removed after confirming no callers existed.
- **When to use:** Cleaning up agent domain permissions or dead code.
- **Pattern:** The Wake Briefing is a completely separate background process triggered by the orchestrator when an alarm fires; the timer-agent is NOT involved at alarm-fire time.
- **When to use:** Understanding alarm/timer architecture boundaries.

### Release — Version Tag Verification
- **Pattern:** Always verify existing local tags (`git tag -l`) before creating a new release tag.
- **When to use:** Every release workflow.

### CI — Lint Pre-Push
- **Pattern:** `ruff check` and `ruff format` must both pass before pushing. CI runs both.
- **When to use:** Before every push.
- **Pattern:** CI should run lint and at least smoke tests for ALL modules, including `custom_components/`.
- **When to use:** CI pipeline changes.

### Reference — Live Environment Access
- **URL:** `http://192.168.120.200:6081`
- **Credentials:** Stored in `.env.local` (ignored by git) as **JSON**, not `KEY=VALUE` format. Structure: `{"live": {"url": "...", "username": "...", "password": "..."}}`.
- **CSRF token:** Make a GET request to `/dashboard/login`. The token is returned in the `agent_assist_csrf` cookie. On subsequent POST requests, provide it **both** as the `agent_assist_csrf` cookie **and** as the form field `csrf_token`. The server compares them with `hmac.compare_digest()`.
- **One-liner:** `requests.get(f"{url}/dashboard/login").cookies.get("agent_assist_csrf")`

## What Has Failed

### Async Patterns — Blocking the Event Loop
- **Anti-pattern:** Using `time.sleep()` inside `async def`.
- **Why it failed:** Blocks the entire asyncio event loop.
- **What to do instead:** Always use `await asyncio.sleep()` in async code. If the function must remain sync (e.g., called from sync context), split into sync core + async wrapper.

### Async Patterns — CPU-Bound Work in Async Context
- **Anti-pattern:** Calling CPU-bound work like `SentenceTransformer.encode()` directly from async code.
- **Why it failed:** Blocks the event loop.
- **What to do instead:** Offload with `asyncio.to_thread()` or `loop.run_in_executor()`.

### Async Patterns — Broad Exception Handling
- **Anti-pattern:** Using `except Exception:` in bridge/transport code.
- **Why it failed:** Swallows programming errors and causes duplicate work or silent failures.
- **What to do instead:** Narrow to specific transport exceptions (`aiohttp.ClientError`, `asyncio.TimeoutError`, `OSError`).

### Async Patterns — Queue Empty Race Condition
- **Anti-pattern:** `while not queue.empty(): queue.get_nowait()` in async code.
- **Why it failed:** Race condition between the empty check and the get.
- **What to do instead:** Loop on `get_nowait()` and catch `QueueEmpty`.

### Async Patterns — CancelledError Swallowing
- **Anti-pattern:** Letting `asyncio.CancelledError` fall through to general exception handlers in long-running loops (WS receive, task runners).
- **Why it failed:** Prevents proper task cancellation and can leave dangling tasks.
- **What to do instead:** Explicitly re-raise `asyncio.CancelledError` before general exception handlers.

### Security — Jinja2 Template Injection
- **Anti-pattern:** Concatenating user input into Jinja2 templates, even with regex validation.
- **Why it failed:** User input can escape validation and execute arbitrary template logic.
- **What to do instead:** Always pass user data as template variables.

### Security — X-Forwarded-For Trust
- **Anti-pattern:** Trusting the leftmost IP in `X-Forwarded-For`.
- **Why it failed:** The leftmost IP is trivially spoofable.
- **What to do instead:** Walk from the rightmost IP (closest to the server) to find the first non-trusted IP.

### Security — Silent Secret Decryption Failure
- **Anti-pattern:** Secret decryption returning `None` silently on failure.
- **Why it failed:** Callers cannot distinguish a failed decryption from a legitimate empty value, masking key rotations.
- **What to do instead:** Fail loudly (raise an exception) so callers know a key rotation or configuration issue occurred.

### Architecture — SSE Background Task Leaks
- **Anti-pattern:** SSE ticker / background task registration without deduplication or lifespan cleanup.
- **Why it failed:** Unbounded task accumulation over time.
- **What to do instead:** Add deduplication guards and ensure proper lifespan / shutdown cleanup.

### Testing — MagicMock as Async Default Fixture
- **Anti-pattern:** Using `MagicMock()` as the default `ha_client` fixture for methods that will be awaited (e.g., `render_template`).
- **Why it failed:** `MagicMock()` cannot be awaited; tests fail with coroutine-related errors.
- **What to do instead:** Use `AsyncMock()` with `render_template = AsyncMock(return_value="")` as the default.

### Architecture — Options Flow Data/Options Divergence
- **Anti-pattern:** Changing an options flow from writing to `data` to writing to `options` without updating container-side tests.
- **Why it failed:** Container tests asserting on `async_update_entry` kwargs break because the kwargs shape changes.
- **What to do instead:** Update both the integration code and the container-side tests in the same changeset.

### Architecture — Type Annotation Sweeps Without Import Checks
- **Anti-pattern:** Sweeping `dict` to `dict[str, Any]` without verifying imports.
- **Why it failed:** Type-checkers may need `from __future__ import annotations` or `typing.Any`, and `ruff check` may flag missing imports.
- **What to do instead:** After broad type annotation changes, run lint and verify that `Any` and `annotations` imports are present where needed.

### Workflow — Plan Subagent Unbounded Refinement
- **Anti-pattern:** Allowing the planning subagent to enter refinement loops without hard limits.
- **Why it failed:** Produced 50-80 KB plans with 30+ heading levels and endless "V1 vs V2" comparison tables, causing hangs.
- **What to do instead:** Enforce the guardrails documented in **What Has Worked** (max size, max headings, one-pass output).

## Current Learnings Summary

- Always use `AsyncMock` for async mocks; `MagicMock` cannot be awaited and breaks tests silently.
- Run `pytest tests/ -n auto` before pushing container changes, but verify unexpected failures with a sequential run.
- Never delete Docker volumes without explicit user consent; prefer container recreation or targeted file cleanup.
- The orchestrator's `_classify` must return `user_text` on cache hits, and streaming done-chunks must carry `routed_to` / `action_executed` for bridge tests.
- All subagent invocations must use `subagent_type="coder"` with prompt-enforced tool restrictions.

## Session Log

### 2026-04-29
- Container pre-existing test failures (timer reroute, orchestrator routing cache, health endpoint, entity index rebuild, registry invalidation) have all been fixed.
- `docker-compose_local.yml` is the correct compose file for local development.
- Mock `voluptuous` and `homeassistant.helpers.selector` in `conftest.py` before importing `custom_components.ha_agenthub` in container tests.

### 2026-05-02
- Added `build_scenario_backed_app()` to `conftest.py` to wire real orchestrator pipeline into FastAPI test apps. Must call `conversation_routes.set_dispatcher(handles.dispatcher)` after building pipeline.
- `HAMimicClient` test helper uses `starlette.testclient.TestClient` wrapped in `asyncio.to_thread` for WebSocket testing inside async pytest.
- Use fresh app-per-transport pattern to prevent deterministic LLM stub state contamination between transports.
- Scenario parametrization through API layer: 102 YAML scenarios * 3 tests each (REST + WS + parity) = 306 tests. All pass deterministically with no real LLM calls.

### 2026-05-02 — Action-Audit Bridge Tests
- `ConversationResponse` and `StreamToken` models already had `action_executed` field but it was never populated by REST/SSE/WS handlers. Adding `routed_agent` and wiring `action_executed` in `conversation.py` enables true black-box bridge tests.
- The orchestrator's streaming path (`handle_task_stream`) needed `routed_to` and `action_executed` added to the final `done=True` chunk.
- Internal `ActionExecuted` shapes may differ from public `ActionResult` model; a `_normalize_action_executed()` adapter helper in routes is useful.
- Bridge tests that assert ONLY on API responses (no `app.state` poking) are cleaner and survive refactors better.

### 2026-05-02 — Deep Code Review
- `time.sleep()` inside `async def` blocks the entire asyncio event loop. Always use `await asyncio.sleep()` in async code.
- CPU-bound work like `SentenceTransformer.encode()` must be offloaded with `asyncio.to_thread()` or `loop.run_in_executor()` when called from async code.
- Never concatenate user input into Jinja2 templates, even with regex validation. Always pass user data as template variables.
- `X-Forwarded-For` parsing must walk from the rightmost IP to find the first non-trusted IP.
- `except Exception:` in bridge/transport code swallows programming errors. Narrow to specific transport exceptions.
- `while not queue.empty(): queue.get_nowait()` is a race condition in async code. Loop on `get_nowait()` and catch `QueueEmpty`.
- SSE ticker / background task registration needs deduplication guards and proper lifespan cleanup.
- Secret decryption should fail loudly (raise) rather than silently returning `None`.
- Docker base images and external binaries should be pinned to exact versions with checksum verification.
- `.dockerignore` must exclude sensitive files from the build context.
- CI should run lint and at least smoke tests for ALL modules, including `custom_components/`.

### 2026-05-02 — Low-Priority Fixes
- When changing `ha_client.render_template` to accept `variables` kwarg, ALL test fixtures that create a default `ha_client` must be updated. Use `AsyncMock()` with `render_template = AsyncMock(return_value="")`.
- Changing an options flow from writing to `data` to writing to `options` breaks container tests that assert on `async_update_entry` kwargs. Update both integration and container-side tests.
- `pytest-xdist` (`-n auto`) can mask or expose different test failures than sequential runs. Always verify with sequential run before declaring failure.
- Top-level `dict` -> `dict[str, Any]` sweeps affect many public method signatures. Check imports after type annotation changes.
- `asyncio.CancelledError` must be explicitly re-raised before general exception handlers in long-running loops.

### 2026-05-02 — Meta-Workflow Fixes
- Plan subagent was consistently hanging in refinement loops, producing 50-80 KB plans with 30+ heading levels. Fixed by adding hard anti-loop rules to AGENTS.md.
- `explore` and `plan` subagent types have NO write access. Fixed AGENTS.md: all phases now use `coder` subagent_type with prompt-enforced tool restrictions.
- Added parallel agent execution rules to AGENTS.md: up to 3 parallel research agents + Synthesis; up to 3 parallel implementation agents + Merge & Verify; Planning remains sequential.

### 2026-05-04
- Live deployment credentials are stored in `.env.local` (ignored by git). Can authenticate against the live container at `http://192.168.120.200:6081` and obtain a session cookie for inspecting live logs via `/api/admin/logs`.

### 2026-05-05 — Timer-Agent Domain Cleanup
- The timer-agent's `AGENT_ALLOWED_DOMAINS` included `calendar` and `input_datetime` as historical artifacts. The `calendar` domain was from removed `create_reminder`/`create_recurring_reminder` functionality (now in `calendar-agent`). The `input_datetime` domain was from pre-v0.26.0 HA `timer.*` helper usage; `set_datetime` now routes exclusively to the internal `TimerScheduler`.
- The Wake Briefing is a completely separate background process triggered by the orchestrator when an alarm fires. The timer-agent is NOT involved at alarm-fire time.
- Dead code in `timer_executor.py` was confirmed unused via grep and safely removed.
- Always verify existing local tags (`git tag -l`) before creating a new release tag. `v1.19.0` already existed locally while `VERSION.md` showed `1.18.0`, indicating a prior partial release. Resolved by bumping to `1.19.1`.

### 2026-05-06
- Live deployment credentials in `.env.local` are stored as **JSON**, not traditional `KEY=VALUE` env format. Structure: `{"live": {"url": "...", "username": "...", "password": "..."}}`.
- To obtain a valid CSRF token, make a GET request to `/dashboard/login`. The token is returned in the `agent_assist_csrf` cookie.
- On subsequent POST requests, the token must be provided **both** as the `agent_assist_csrf` cookie **and** as the form field `csrf_token`. The server compares them with `hmac.compare_digest()`.
- One-liner to fetch token: `requests.get(f"{url}/dashboard/login").cookies.get("agent_assist_csrf")`.
