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

### Architecture — Subagent Type Discipline
- **Pattern:** Always set `subagent_type="general"` for every subagent invocation. Use prompt-enforced tool restrictions (not built-in types) to control access: Research = Read/Grep/Glob/Write (docs only); Planning = Read/Grep/Glob/Write (docs only); Implementation = full toolset.
- **When to use:** Every subagent spawn.

### Architecture — Task Envelope Split (FLOW_REDEF, DP-1)
- **Pattern:** Three task models replace the deleted `AgentTask`: `IngressTask` (orchestrator boundary, raw sanitized `description`, no `verbatim_terms`), `DispatchTask` (agent-bound, condensed `description` + `verbatim_terms` from `@entities:` classification lines), `BackgroundTask` (no text fields at all; event payload lives in `context.background_event`). The A2A dispatcher validates dicts by target: `agent_id == "orchestrator"` -> `BackgroundTask` if no `description` key, else `IngressTask`; any other agent -> `DispatchTask`.
- **When to use:** Any change crossing the orchestrator/agent A2A boundary.
- **Pattern:** Filler encoding is two-line: `DispatchTask.description = "generate_filler:<target_agent>\n<raw user_text>"`; `filler.py` parses line 1 as command and the remainder as user text (`[:200]` bound kept).
- **When to use:** Touching filler dispatch or filler-agent parsing.
- **Pattern:** Routing-cache metadata no longer stores `condensed_task`. Legacy rows carrying the key still deserialize (key-based metadata, extra keys ignored) — no DB migration needed.
- **When to use:** Routing-cache schema changes.
- **Pattern:** `context.user_id` is explicitly copied into dispatch envelopes (DP-6), so sub-agents (e.g. calendar) see real user values.
- **When to use:** Adding context fields that sub-agents need.
- **Pattern:** When a context-based guard (not `isinstance`) establishes a type invariant (e.g. `_is_background_turn` via `context.background_event`), narrow for mypy with `cast()` rebinds at choke points — an `isinstance` check would alter runtime behavior.
- **When to use:** mypy union-attr errors after heteromorphic boundary changes.

### Architecture — Parallel Agent Execution
- **Pattern:** Up to 3 parallel research agents (one per distinct module) followed by a Synthesis agent. Up to 3 parallel implementation agents (one per independent work stream) followed by a Merge & Verify agent. Planning remains strictly sequential.
- **When to use:** Multi-domain research or implementation tasks.

### DevOps — Local Docker Compose
- **Pattern:** `container/docker-compose_local.yml` is the correct compose file for local development (service `ha-agenthub`, volume `agent-assist-data`). The root `docker-compose.yml` pulls from GHCR and uses different names.
- **When to use:** Local container development.

### DevOps — Docker Volume Safety
- **Pattern:** Never delete Docker volumes (`docker compose down -v`, `docker volume rm`, etc.) unless the user explicitly requests it. If a container fails to start due to data corruption, prefer `docker compose down` (without `-v`) and container recreation, or manual cleanup of specific files inside the volume.
- **When to use:** Any container troubleshooting.

### DevOps — Supply-Chain Hardening
- **Pattern:** Pin Docker base images and external binaries to exact versions with checksum verification.
- **When to use:** Dockerfile changes or dependency upgrades.
- **Pattern:** `container/.dockerignore` must exclude sensitive files (`.github/`, `.vscode/`, `.kimi/`, docs, keys, credentials) from the build context.
- **When to use:** Reviewing Docker build context.

### Integration — Timer-Agent Domain Cleanup
- **Pattern:** Remove dead domains and code confirmed unused via grep. The timer-agent's historical `calendar` domain was safely removed after confirming no callers existed.
- **When to use:** Cleaning up agent domain permissions or dead code.
- **Pattern:** The Wake Briefing is a completely separate background process triggered by the orchestrator when an alarm fires; the timer-agent is NOT involved at alarm-fire time.
- **When to use:** Understanding alarm/timer architecture boundaries.
- **Note:** `input_datetime` is still present in `timer.py` `allowed_domains` but `set_datetime` routes to the internal `TimerScheduler`.

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
- **Credentials:** Stored in `secrets/.env.local` (ignored by git) as **JSON**, not `KEY=VALUE` format. Structure: `{"live": {"url": "...", "username": "...", "password": "..."}}`.
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
- **What to do instead:** Enforce hard limits in the planning phase (max size, max headings, one-pass output only).

## Session Log

### 2026-04-29
- Container pre-existing test failures (timer reroute, orchestrator routing cache, health endpoint, entity index rebuild, registry invalidation) have all been fixed.
- `container/docker-compose_local.yml` is the correct compose file for local development.
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
All findings from the security/architecture review have been promoted to **What Has Failed** above. Key themes:
- Async discipline (`time.sleep()` in coroutines, CPU-bound in event loop, broad `except Exception`, `QueueEmpty` race, `CancelledError` swallowing)
- Security (Jinja2 injection, `X-Forwarded-For` trust, silent secret decryption)
- Architecture (SSE task leaks, options flow divergence, type annotation sweep gaps)
- Workflow (plan subagent refinement loops)

### 2026-05-02 — Meta-Workflow Fixes
- Plan subagent was consistently hanging in refinement loops, producing 50-80 KB plans with 30+ heading levels. Fixed by adding hard anti-loop rules.
- `explore` and `plan` subagent types have NO write access. Fixed: all phases now use `general` subagent_type with prompt-enforced tool restrictions.
- Added parallel agent execution rules: up to 3 parallel research agents + Synthesis; up to 3 parallel implementation agents + Merge & Verify; Planning remains sequential.

### 2026-05-05 — Timer-Agent Domain Cleanup
- The timer-agent's `calendar` domain was a historical artifact from removed `create_reminder`/`create_recurring_reminder` functionality (now in `calendar-agent`). Confirmed unused via grep and safely removed.
- The Wake Briefing is a completely separate background process triggered by the orchestrator when an alarm fires. The timer-agent is NOT involved at alarm-fire time.
- Dead code in `timer_executor.py` was confirmed unused via grep and safely removed.
- Always verify existing local tags (`git tag -l`) before creating a new release tag.

### 2026-06-25 — Architectural Audit Phase 1 (Safety & Visibility)
- List/read paths (`light_executor.list_lights`, `lists_executor`) must filter enumerated entities by per-agent visibility before building responses or choosing a default target.
- `EntityMatcher` should apply visibility filtering to the candidate set before fuzzy/embedding/phonetic scoring so hidden entities are never scored.
- Action-cache replay must recheck visibility for every cached `entity_id`, not just the primary target.
- Routing-cache skips must validate both agent registration and entity visibility of referenced entities before short-circuiting classification.
- `ClassificationEngine` validation needs access to `entity_index`; wire it through the orchestrator constructor.
- `ruff check` and `ruff format` pass after changes; full `pytest tests/` suite passes sequentially.

### 2026-06-27 — Project Optimization (Phases 0-7)
- **mypy baseline = 126 errors in 21 files** (178 checked). mypy is now a NON-BLOCKING step in `scripts/ci.py` (warn only) and a non-blocking pre-push hook. Do not regress below this count.
- **Coverage single source of truth = 80** (`pyproject.toml`). `scripts/ci.py` argparse + function defaults both set to 80 now.
- **God-file splits:** `db/schema.py` -> `db/schema/` package (migration-ladder registry); `api/routes/admin.py` -> `admin/` package (sub-routers); `agents/timer_executor.py` -> `timer_executor/` package. All import surfaces preserved — `from app.db.schema import init_db`, `admin_routes.router`/`set_registry`, `execute_timer_action` all still resolve.
- **timer_executor package naming:** must be `timer_executor/` NOT `timer/` because `agents/timer.py` (TimerAgent) already exists and a same-named package would shadow it.
- **Startup refactor:** `main.py` lifespan is now 3 lines (`setup_application` -> yield -> `teardown`). Background tasks flow through `bootstrap/_tasks.py:spawn_background(app, coro, attr_name)` -> `app.state._background_tasks` registry + legacy `app.state.<name>` attribute. The 10s `_log_buffer_guard` is KEPT (deferred — uvicorn launched via CMD/entrypoint, no Python `log_config` injection point; third-party libs reconfig logging after lifespan start). It now runs through the registry.
- **MediationService / FillerCoordinator:** extracted from orchestrator.py (2329 -> 1956 lines). Both are lazy `@property` on the orchestrator so `patch.object(orch, ...)` test seams survive. Filler-race dedup was NOT forced (two fundamentally different primitives — coroutine future vs async-gen queue) — left as-is per the sanctioned fallback.
- **sqlite-vec spike PASSED:** `python:3.12-slim-bookworm` is compiled with `--enable-loadable-sqlite-extensions` (verified in official Dockerfile). ChromaDB fully replaced by sqlite-vec `vec0` (cosine). torch/sentence-transformers stays. No RAM-limit change (would require removing torch separately). Vector layer is ONLY the EmbeddingSignal fallback after deterministic resolution (Directives 4 & 5).
- **Parallel implementation transient failures:** when running parallel implementation agents, one agent may report failures in another agent's files — these are often transient worktree-state artifacts that don't reproduce in the integrated tree. Always verify with the Merge & Verify agent on the merged result.
- **Cross-test contamination:** full-suite flakiness between xdist and single-process runs that doesn't reproduce in isolation is usually a fixture/state leak, not a production bug. Confirm with `-p no:xdist` single-process run before debugging.

### 2026-07-17 — FLOW_REDEF: Task-Envelope Split (DP-1..DP-10)
- Plan `docs/SubAgent/FLOW_REDEF_PLAN_V2.md` fully implemented (sequential: V2-Agent1 Phases 1-4+6, V2-Agent2 Phase 5, V2-Verify Phase 7+8.1). `AgentTask` deleted -> `IngressTask`/`DispatchTask`/`BackgroundTask`; `TaskContext.entity_states`/`mcp_tools` removed; DP-4 text fallbacks collapsed to `task.description` with the background branch reordered before any text read; filler two-line description encoding; routing-cache `condensed_task` storage removed (legacy rows still readable); `user_id` propagated to sub-agents; WS/SSE dead `agent_id` fallback removed (`routed_to` only).
- Gates: ruff clean, 2828 passed / 0 failed (full suite, `-p no:xdist`), coverage 80.64% >= 80, mypy exactly 141 = HEAD baseline (16 change-set-induced errors fixed via `cast()` rebinds + 4 targeted `type: ignore` with design comments).
- Environmental note: on this Windows host the pytest process hangs on interpreter shutdown AFTER printing the complete summary + coverage result (observed twice, pre-existing, unrelated to the change set). Take verdicts from the printed summary lines.
- `FLOW_WORKDRAFT.md` deleted per plan 8.1.

### 2026-07-16 — Full Code Review (Backend + Integration, 59 Findings)
- 3 parallel research streams + synthesis + plan + 3 parallel implementation streams + merge worked cleanly; zero file overlaps between streams.
- Cross-stream fallout pattern: integration-side changes (`ConfigEntryError` import, `_validate_connection(hass, ...)` signature) break container-suite HA mocks in `container/tests/conftest.py` — extend `_ensure_ha_exceptions_mock` and add `homeassistant.helpers.aiohttp_client` mock when the integration gains new HA imports.
- **mypy baseline at HEAD 13bb0d1 is 141 errors, not 126** — the 126 figure (from 2026-06-27) is stale relative to HEAD. Verified via pristine-HEAD worktree: identical error set, so zero new errors from the review fixes. Update baselines before comparing.
- Config-flow options contract: options flows must persist via `async_create_entry(data={...})` in a single write; `async_update_entry` for title/data/unique_id only — never write then wipe options.
- `_validate_direct_entity_id` is now async + fail-closed on visibility for all 11 executor call sites; agent per-request state (`_current_task`) lives in ContextVars, not instance attributes.
- `/api/health` now requires API key; unauthenticated probes use `/healthz`.

### 2026-07-18 — SubAgent artifacts: per-task subfolder layout
- All SubAgent workflow artifacts now live in `docs/SubAgent/[NAME]/` (one folder per task) and keep the `[NAME]_[SUFFIX].md` filename prefix, e.g. `docs/SubAgent/FIX_AUTH_BUG/FIX_AUTH_BUG_PLAN.md`. The flat `docs/SubAgent/[NAME]_*.md` layout is retired; AGENTS.md "SubAgent File Naming" is the canonical reference.
- `.gitignore`'s `docs/SubAgent/` directory pattern already covers subfolders recursively — no ignore-file change was needed.

### 2026-07-20 — PIPELINE_REVIEW: First-Frame Latency + Semantic Routing Cache (Phases 1-5)
- Parallel research (3 streams) + synthesis + sequential 5-phase implementation workflow completed cleanly; per-phase full-suite gates caught integration breaks early (cross-stream failure pattern already logged 2026-07-16).
- `cache.db` is a SEPARATE database from `agent_assist.db`: managed by its own PRAGMA `user_version` ladder in `sqlite_cache_store.py` (now v2), NOT the app/db/schema migration registry.
- `vec0` table names must not collide with vec0 shadow tables (e.g. suffix `_info` is reserved by vec0 itself); vec0 dimension depends on the configured embedding model — create the table lazily and drop stale vectors on dimension change.
- Deterministic executor confirmations never pass through the agent LLM; personality reaches them only via the mediation/rewrite step (user requirement: personality in ALL responses).
- Non-streaming "TTFT" was actually total latency; metric renamed to `latency_ms`, `ttft_ms`/`tps` are now streaming-only.
- pytest shutdown-hang on this Windows host reconfirmed (take verdicts from printed summary lines — see 2026-07-17 entry).
- Final suite states: container 2987 passed, custom_components 82 passed, ruff clean.

### 2026-07-24 — Pre-push review + release v1.46.0
- Pre-push review pattern that worked: `git diff origin/main..HEAD` split into per-domain diff files under `docs/SubAgent/[NAME]/`, 3 parallel research streams (agents / cache+entity / llm+api+integration) with prompt-forbidden Bash, plus `python scripts/ci.py --skip-security --skip-docker` run in background as the automated gate (ruff + pytest container + HA).
- Diff-splitting pitfall: `container/app/__init__.py` fell through the per-domain diff buckets, causing a false version-skew WARNING. Include version files (`__init__.py`, `manifest.json`, `VERSION.md`) explicitly in one bucket.
- Before creating a release tag, check whether the tag already exists locally (`git tag -l 'vX.Y*'`) and verify it points at the intended commit (`git rev-parse vX.Y.Z^{}`). Here `v1.46.0` already existed at HEAD and only needed pushing; after a follow-up release-chore commit it had to be re-pointed (`git tag -d` + re-create) — safe only because it was never pushed.
- Untracked pre-push hygiene: `container/coverage.xml` (build artifact) and `flow.md` were untracked; flagged for exclusion rather than committed.

### 2026-07-24 — FRONTEND_REDESIGN: Dashboard reskin to gameserver-manager style
- Dashboard restyled to the reference warm amber-on-near-black theme: tokens.css values swapped (token names kept — `chartColors()` in `utils.js` reads 8 CSS var names), Inter/JetBrains Mono vendored, top bar removed in favor of an in-content `.page-header` pattern, `_STATIC_BUILD` bumped 13->14.
- **Pattern:** keeping CSS var NAMES stable while swapping values lets a full reskin happen without touching JS — but hardcoded hex maps (`utils.js` `_agentClassToHex`/`_traceSpanColors`) and Chart.js rgba literals in templates bypass CSS vars and must be edited in lockstep.
- **Deviation note:** plan assumed 18 pages needed subtitle blocks; in fact 16 pages already had in-content `.page-header` with Alpine-bound actions — moving those into Jinja base blocks would have broken Alpine scoping. Verify actual template state before migrating patterns.

### 2026-07-25 — CACHE_EMBED_WARMUP: skip guard + embedding keep-alive
- Live-trace debugging via Admin API works well: `GET /api/admin/traces/{trace_id}` returns full span trees (span_name, duration_ms, metadata); `/api/admin/logs?level=debug&search=...&limit=` filters the whole log buffer server-side (max limit 1000 per page; limit=4000 returns 422).
- Diagnosed a 2377ms `cache_lookup` outlier (norm: 2-18ms): first conversation turn after 6h idle paid the one-off cold cost of the local e5-small encode (OS page eviction of torch/model memory). Periodic entity syncs (~35ms, no re-embedding) do NOT keep the embedding model warm.
- Fixed in two parts: (1) `try_routing_skip` now checks `await asyncio.to_thread(self._routing_cache.count)` inside the `semantic_available()` branch and skips embed+k-NN when the routing cache is empty (fail-safe to skip on count error); (2) `run_embedding_keepalive()` in `embedding.py` (pattern copied from `ActionCacheValidator.run_periodic`) does a dummy encode every `embedding.keepalive_interval_minutes` (default 15, 0=disabled, no-op for non-local providers), seeded in `_seed.py`, spawned via `spawn_background` in `bootstrap/_cache.py:setup_cache`.
- **Gotcha:** the `_EmbeddingCache` TTL is 300s — a fixed-string keep-alive with interval < 300s is a silent no-op (LRU hit, model untouched). Keep-alive interval must stay above the TTL or use a varying string.
- **Environment note:** pytest-xdist is NOT installed in either local venv (`.venv`, `.venv-1`) despite `pytest -n auto` being the documented CI command — full container suite runs sequentially (`python -m pytest tests/ -q` from `container/`, ~330s, 2992 passed).

### 2026-07-26 — TOKEN_PRESELECTION: token-based candidate rescue in EntityMatcher
- Added token-based candidate preselection: `EntityIndex` keeps an in-memory token posting map (Token -> entity ids) maintained via the six mutation hooks; `_match_query` rescues token-matched entries after the embedding merge, BEFORE the visibility filter (Directive 5). Language-agnostic per user requirement: generic tokens tamed via df-ratio cap (`max_df_ratio=0.5`), NO stopword lists.
- **Key scoring insight:** rescued candidates have no embedding score and cap at ~0.2-0.45 weighted — they can never clear the 0.60 threshold without help. Fix: +0.50 reverse-containment bonus (friendly-name tokens fully contained in query tokens), gated on a `token_preselection` marker in `signal_scores` so embedding-shortlist candidates score unchanged. The marker is safe because the weighted loop only iterates configured signal weights.
- Settings follow the seed pattern: `entity_matching.token_preselection.enabled/max_df_ratio/max_candidates` in `_seed.py`, read in `load_config` (restart-to-apply). Defaults must ALSO live in `__init__` because test fixtures bypass `load_config`.
- **Workflow lesson:** implementation subagents appear to "hang" when they run the full pytest suite in the foreground (~330s+ on this host) — the user may interrupt them. Running the suite as an orchestrator background Bash task works well; piping through `tail` still captures the summary before the known Windows shutdown-hang kills the task (verdict from printed summary: 3016 passed in 198.76s).

### 2026-07-26 — TOKEN_PRESELECTION Addenda A+B: IDF ranking + marker symmetry (live-verified)
- **Addendum A (IDF ranking):** count-based ranking in `find_by_tokens` let generic area tokens (df=58) flood the top-20 cap and bury rare-token entities (couch, df=2, rank 16 of 64 — pushed past 20 by live-only entities). Fix: rank by IDF-sum `log(N/df)` per matched token. Rare distinctive tokens must outrank area floods.
- **Addendum B (marker symmetry):** the marker-gated +0.50 bonus created an inversion — a token-matched entity that ALSO arrives via the embedding shortlist was skipped by the rescue merge (`if entity_id not in results`), got no marker/bonus, and lost (0.4495 < 0.60) to a weaker same-named rescue-only entity (0.677). Fix: mark rescued entries already in `results` too. Live-verified: light.couch 0.9495 top (embedding 0.908 + token_preselection).
- **Debug technique that worked well:** `docker exec -i ha-agenthub python` harness — rebuild `EntityIndex._primary` from the persisted store (`/data/chromadb/entity_vectors.db`, table `entity_index_meta`, read-only `file:...?mode=ro`) and call `find_by_tokens`/`_match_query` directly; reproduces prod matching behavior exactly without API rate limits. `/api/admin/entities` + `/api/admin/entity-index/match-preview` are rate-limited (429) after a handful of calls.
- **Env gotchas:** local dashboard rate limiter blocks repeated admin API calls (wait ~minutes or use the container harness); `secrets/.env.local` is JSON blocks (live/local), NOT a shell-sourceable env file; dashboard login needs CSRF token (cookie + form field).
- **Separate observation (unfixed):** HA friendly names with typos ("Wonzimmer" instead of "Wohnzimmer") break both token matching (normalization does not equate them) and apparently embedding proximity over time; fix via HA aliases or name correction, not matcher code.
- Suites: Addendum A 3017 passed, Addendum B 3018 passed (both ~200s, verdict from printed summary due to shutdown-hang).

### 2026-07-26 — ENTITY_RES_REDESIGN: ingress resolution + span scoring + LLM entity_id pick
- Full redesign implemented in 8 phases (plan ENTITY_RES_REDESIGN): span-based scoring (n-gram spans 1..4 tokens, max sim x IDF-weighted coverage) replaces full-query string scoring; Floor-Regel (all friendly-name tokens covered at sim>=0.95 -> score=max(score,0.65)) replaces the +0.50 reverse-containment bonus; verbatim_terms retired stage (a) (@entities: gone from orchestrator.txt, Directive 4 rewritten in same commit; field removal = deferred stage b); DispatchTask.candidates (K=5, two-phase: unfiltered oversampled pool N=20 at ingress AFTER action-cache miss, per-agent filter_visible_results post-routing, pool never reaches spans/envelope); agent LLM may emit entity_id from the candidate list for state-changing actions (10 prompt files in lockstep), validated fail-closed (per-action domains + index existence + visibility) via extended _validate_direct_entity_id; TaskContext.last_entities for anaphors (persisted as JSON in the previously unused conversations.action_executed column); ambiguity annotation at top1-top2 gap < 0.05 (_AMBIGUITY_SCORE_GAP mirrors the area re-rank gap).
- **Calibration trap that MUST be remembered:** rescued/token-only candidates structurally lack embedding (0.30) + alias (0.15) weight, capping weighted scores at 0.55 < 0.60 threshold with production weights. Test fixtures with EQUAL 0.2 weights score the same case at exactly 0.60 — gates can go green while production breaks. Any matcher test constructing a real EntityMatcher and asserting scores must use production-like weights (lev 0.20 / jw 0.20 / phon 0.15 / emb 0.30 / alias 0.15).
- Scenario suite = tests/test_real_scenarios.py (106 real-pipeline scenarios, threshold 0.45) + test_scenarios.py, NOT pytest tests/scenarios (that dir has no tests). Span scoring required ZERO expectation adjustments; constants 0.60/0.45/0.05 re-validated with evidence.
- Score distributions changed by design (couch case: 0.9495 bonus-era -> 0.8225 span-era) — pre/post trace comparisons are not apples-to-apples (signal_scores keys differ).
- Open follow-ups: verbatim stage (b) field removal (>=10 tuple-unpack sites + trace_summary.verbatim_terms column); prose clarifying questions do not set voice_followup (flag set only by executors on ambiguous/not-found paths); action-cache replay turns do not record last_entities hints (harmless — original turn recorded within TTL).
- Suites: post Phases 1-5 full suite 3056 passed; post Phase 7 full suite 3082 passed; scenario 110 passed / 3 xfailed; live couch case verified on local container (light.couch top 0.8225, update.couch floor 0.65).

### 2026-07-28 — LIVE_ISSUES: 3 parallel fixes from live-trace debugging (FOLLOWUP leak, weather envelope, resolution reuse)
- **Live debugging without Docker works fully via Admin API:** `GET /api/admin/traces/{id}` span trees are the reliable evidence source — the `/api/admin/logs` ring buffer (10000 entries) only covered ~20h and had already rolled past the incident window; span metadata (hit_type, cache_tier, llm_response, result_speech) replaced logs entirely.
- **Env gotchas (orchestrator host):** Windows `python3` (WindowsApps shim) does NOT resolve Git-Bash `/tmp` paths — pass `cygpath -w` results to python; `secrets/.env.local` is JSON blocks (reconfirmed from 2026-07-26 entry — parse with python, never `source` it); dashboard login needs CSRF (GET login page -> cookie jar + `csrf_token` form field), plain curl POST gets 401.
- **Routing cache miss was by-design, not a bug:** identical query from 2026-05-22 did not survive the ChromaDB->cache.db store migration + startup purges (schema_version < 4, missing language metadata). cache.db persists across restarts; no TTL exists. Verify the age of the "earlier identical run" before accepting a cache-miss report as a bug.
- **Streaming post-processing asymmetry is a recurring bug class:** streamed tokens are relayed raw BEFORE `_strip_followup_tag` runs on the collected text (which is then only stored, never re-sent). Any marker/aside stripping must hold back `len(marker)` chars at the emission site. Fixed for `[FOLLOWUP]` via 10-char holdback + stripped final flush in orchestrator.py `use_streamed_mediation` branch; on mid-stream MediationStreamError the holdback is dropped deliberately.
- **HARestClient `call_service` returns the raw `?return_response` body** (with `service_response` envelope on modern HA); nothing in container/app unwraps it. climate_executor `_query_weather_forecast` parsed the WS-unwrapped shape and failed SILENTLY (no log in the empty-parse branch) since at least 2026-05. Fix: local envelope unwrap + WARNING log. **`ha_client/rest.py:get_calendar_events` has the same latent envelope bug — open follow-up.**
- **DispatchTask.candidates now consumed by agents:** `_resolve_relevant_entities` fast path reuses the orchestrator's ingress top-K (visibility-filtered, Directive 5) when unambiguous (top1-top2 gap >= `_AMBIGUITY_SCORE_GAP`), saving ~400ms serial embedding/k-NN per turn; still publishes the `_visible_entries` ContextVar snapshot (list_entries_async + filter_visible_results) and falls back to the resolver on empty/ambiguous/invisible envelope.
- 3 parallel implementation streams with disjoint file sets + merge-verify again ran clean (3085 passed, ruff clean); full suite via `.venv/Scripts/python -m pytest container/tests -q` from repo root works (no xdist, ~306s).
