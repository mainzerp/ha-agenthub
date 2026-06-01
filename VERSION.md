# Version

**Current Version:** 1.34.1

## Recent Changes

Track changes since `v1.34.1` here.

- fix(agents): remove state-aware skipping instructions from all LLM prompts. The LLM now always outputs the action the user explicitly requested.
- fix(executors): add deterministic pre-execution state checking. If a device is already in the target state, the executor skips the HA service call and returns an informative message.
- fix(light): remove state-context few-shot examples from light-agent prompt that caused smaller LLMs to incorrectly emit `query_light_state` instead of `turn_off`.

## Version History

### 1.34.1 (PATCH) -- Deterministic state-check in executors, remove state-aware prompt logic

- fix(agents): remove state-aware skipping instructions and examples from light-agent prompt and ActionableAgent injected output rules.
- fix(executors): add `_state_matches` helper and `_REDUNDANT_IF_STATE` map to `executor_state_check.py`. All domain executors now check current entity state before calling HA. If already in the desired state, the service call is skipped and an "already X" message is returned.
- fix(orchestrator): skip results are treated as successful action executions for cache storage, no orchestrator changes needed.
- test(executors): add skip tests for light, climate, cover, media, security, and vacuum executors. Add edge-case test for get_state failure during skip check.

### 1.34.0 (MINOR) -- Persistent cache validator run history

- feat(cache): persist cache validator run history to SQLite `cache_validator_runs` table instead of in-memory deque
- feat(cache): validator history now survives container restart
- feat(cache): `get_history()` returns runs sorted newest-first (`ORDER BY started_at DESC`)
- feat(db): add migration v35 for `cache_validator_runs` table and index
- feat(db): add `CacheValidatorRepository` for CRUD operations on validator runs

### 1.33.3 (PATCH) -- Redesign state-injection prompt format

- fix(agents): replace prominent markdown state block with compact single-line format (`Context: Entity (id): state`). The multi-line `--- Relevant Entity States ---` block distracted the LLM into describing states instead of outputting JSON actions.
- fix(agents): move entity state injection to AFTER output rules in the prompt. The LLM now sees "ALWAYS output JSON" before the context, ensuring the rules take priority.
- fix(prompts): add concrete state-context examples to light-agent prompt showing correct JSON output when device is off (turn_on) vs already on (query_light_state).

### 1.33.2 (PATCH) -- Fix agent JSON output and add irrigation example

- fix(agents): simplify State-Aware instruction block in ActionableAgent. The previous complex instructions caused the LLM to output plain text instead of JSON when entity states were injected. New rules explicitly demand JSON output and clarify that injected states are context only.
- fix(prompts): add "Turn off the garden irrigation" example to light-agent prompt, showing correct turn_off action when device is on.

### 1.33.1 (PATCH) -- Fix state-aware prompt precision and add LLM-based follow-up detection

- fix(agents): tighten state-aware decision instructions in ActionableAgent prompt. Previously, vague wording caused agents to report state instead of executing requested actions (e.g., "Bewässerung ausschalten" -> query_light_state instead of turn_off).
- feat(orchestrator): hybrid LLM-based voice follow-up detection. When the final response contains a question requiring user input, voice follow-up is now triggered regardless of whether the agent performed an action or a query.
- feat(orchestrator): mediation-based follow-up detection (free when personality is active). The mediator appends `[FOLLOWUP]` tag when the rephrased response asks a question.
- feat(orchestrator): fallback LLM detection when mediation is inactive. A lightweight yes/no LLM call (4 tokens, temperature 0) decides if follow-up is needed.
- test(orchestrator): 9 new tests for follow-up detection (mediation tag extraction, LLM fallback, merge logic, no false positives).

### 1.33.0 (MINOR) -- Context-based task support

- feat(agents): selective entity-state injection into ActionableAgent prompts. Agents now see current states of relevant entities (target + condition) before the LLM call, enabling state-aware decision making.
- feat(agents): generic state-aware instruction block for all actionable agents. Agents skip redundant actions when the target device is already in the desired state.
- feat(agents): conditional action support. `ActionCondition` schema with `entity`, `state`, `attribute`, `operator` (eq/neq). Executor evaluates conditions deterministically before service calls.
- feat(orchestrator): implicit command recognition in classification prompt. State descriptions ("it's too dark") are now mapped to implied actions.
- feat(orchestrator): conditional command preservation. "if X, then Y" structures are preserved in the condensed task and passed to agents.
- feat(prompts): state-aware and conditional examples added to all agent prompts (light, climate, security, cover, vacuum, media, scene).
- feat(cache): conditional actions are marked `cacheable=False` and never enter the action cache, ensuring correctness over stale replays.
- feat(cache): cache validator documentation updated -- conditional actions are invisible to validation by design.
- test(agents): 47 unit tests for selective entity injection, state context building, and graceful degradation.
- test(executor): 12 unit tests for ActionCondition validation and `_evaluate_condition` behavior.
- test(executor): 3 integration tests for conditional action execution (passing, failing, regression).
- test(cache): 3 tests verifying conditional actions bypass action-cache storage and replay.

### 1.32.3 (PATCH) -- Debug cache validator LLM path

- fix(cache): add INFO-level logging to `_validate_entry` to debug why LLM validation is skipped

### 1.32.2 (PATCH) -- Fix TTS response delivery and add file-based logging

- fix(orchestrator): set `sanitized` flag in all REST and streaming response paths to prevent double markdown stripping by HA integration
- fix(orchestrator): remove duplicate voice followup mechanism (`_schedule_ha_voice_followup_if_requested`) to eliminate race condition where HA integration and orchestrator both attempted to reopen the microphone
- fix(tests): remove obsolete mock assignments for removed `_schedule_ha_voice_followup_if_requested` method
- feat(logging): add `RotatingFileHandler` writing to `/data/logs/app.log` (50 MB, 5 backups) for persistent container logs
- chore(docker): add `LOG_DIR` environment variable to docker-compose.yml

### 1.32.1 (PATCH) -- Fix cache validator LLM client initialization

- fix(runtime): initialize `app.state.llm_client` with `_LLMClientWrapper` so the cache validator can actually use the configured LLM model for consistency checks

### 1.32.0 (MINOR) -- LLM-first cache validation

- feat(cache): LLM-first validation for action-cache entries. When `cache.validator.model` is configured, the LLM now evaluates consistency across query, action, and response before falling back to deterministic checks.
- feat(cache): LLM validator can return `consistent`, `correct_response` (regenerates response), or `invalidate` (deletes entry).
- fix(orchestrator): preserve room names in condensed task and document verbatim_terms intent

### 1.31.0 (MINOR) -- Cache validator dashboard and skip logic

- feat(cache): add `validated_at` timestamp to ActionCacheEntry; validated entries are skipped on subsequent scans
- feat(cache): add in-memory scan history (last 50 runs) to ActionCacheValidator with `started_at`/`finished_at`
- feat(api): add `GET /api/admin/cache/validate/history` endpoint
- feat(dashboard): add "Cache Validator" tab to cache management page with run button and history table
- feat(dashboard): add "Validated" column to Action cache table
- feat(dashboard): replace plain text model field with agent-style Provider + Model selector in cache validator settings
- feat(ci): add cross-platform `scripts/ci.py` for local quality, security, and docker build/push

### 1.30.0 (MINOR) -- Action cache validator

- feat(cache): add ActionCacheValidator for periodic action-cache validation and stale entry cleanup
- feat(api): add POST /api/admin/cache/validate endpoint for on-demand cache validation scans
- feat(settings): add cache.validator.* settings (enabled, interval_minutes, model, temperature, reasoning_effort, max_tokens)
- feat(runtime): integrate cache validator into runtime setup with periodic background task

### 1.29.1 (PATCH) -- Integration-driven satellite voice follow-up

- fix(container/background_actions): remove container-side `assist_satellite.start_conversation` for satellites; follow-up is now handled by the HA integration
- fix(container/notification_dispatcher): same satellite follow-up removal
- fix(integration): extend `_post_filler_push` to trigger `assist_satellite.start_conversation` after `assist_satellite.announce` completes and satellite returns to idle
- fix(integration): capture `voice_followup` flag from done chunk in filler push background task
- test(notification_dispatcher): update test to assert no service call for satellite targets

### 1.29.0 (MINOR) -- Dynamic weather forecast types

- feat(climate): climate-agent now requests `hourly` or `daily` forecasts based on user query context
- feat(climate): `query_weather_forecast` action accepts `type` parameter (`hourly` or `daily`, defaults to `daily`)
- feat(prompts): update climate-agent prompt to instruct LLM to choose appropriate forecast type
- test(climate): add test verifying `hourly` forecast type is passed to HA service
- test(climate): update existing tests to assert correct default `daily` type

### 1.28.0 (MINOR) -- Automation CRUD operations

- feat(agents): add `create_automation`, `update_automation`, `delete_automation`, and `get_automation_config` actions to automation executor
- feat(agents): update `AutomationAgent` agent card to include CRUD skills
- test(agents): add executor unit tests for automation CRUD actions
- test(agents): add agent integration tests for automation CRUD actions
- test(scenarios): add 4 scenario YAML files for automation CRUD voice commands

### 1.27.3 (PATCH) -- Filler token limit and satellite voice follow-up fixes

- fix(schema): bump filler-agent default max_tokens from 50 to 1024; add migration 34 to update existing DB rows
- fix(filler): add detailed warning logs when filler generation produces empty response (includes model and max_tokens)
- fix(orchestrator): distinguish empty filler speech from true dispatch failures in logs
- fix(llm): enhance empty-after-retry logging to include agent_id, model, max_tokens, and finish_reason
- fix(background_actions): use `assist_satellite.start_conversation` for `assist_satellite.*` entities instead of `assist_pipeline.run` (fixes 400 Bad Request on voice follow-up)
- fix(notification_dispatcher): apply same satellite follow-up fix for timer/alarm notification paths
- test(notification_dispatcher): update satellite follow-up test to assert `assist_satellite.start_conversation` call

### 1.27.2 (PATCH) -- Traces table layout fix

- fix(dashboard): View button no longer pushed out of view at ~1280px viewport on the Request Traces page — applied `table-layout: fixed` with explicit column widths via `.traces-table` CSS class; User Input column now truncates with ellipsis instead of expanding to full text width

### 1.27.1 (PATCH) -- Mobile UI fixes

- fix(dashboard): page title no longer hidden behind hamburger button on all mobile pages (added `padding-left: 4rem` to `.top-bar` at ≤768px)
- fix(dashboard): eliminated horizontal overflow/scrollbar on all mobile pages (`overflow-x: hidden` on `.main-content`)
- fix(dashboard): resolve CSS cascade bug — responsive grid rules in `layout.css` were silently overridden by base definitions in `components.css` (which loads later); responsive overrides now appended at end of `components.css`
- fix(dashboard): stat-grid no longer overflows on 375px/320px viewports (`minmax(0, 1fr)` + `min-width: 0` on `.stat-card`)
- fix(dashboard): settings page grid-2 now correctly collapses to single column on mobile
- fix(dashboard): stat card labels wrap on narrow cards instead of forcing overflow

### 1.27.0 (MINOR) -- Follow-up routing hint

- feat(orchestrator): inject previous-agent hint into classification prompt to improve follow-up routing accuracy. The orchestrator now tells the LLM which agent handled the previous turn, reducing mis-routing of short ambiguous follow-ups to general-agent.

### 1.26.0 (MINOR) -- Cache dashboard detail panel

- feat(dashboard): add expandable detail panel to cache management page showing response text, cached action JSON, entity IDs, and metadata for both routing and action cache tiers

### 1.25.1 (PATCH)

- fix(sanitize): strip parenthetical meta-commentary from rewrite-agent and mediation paths before TTS output
- fix(prompts): add prompt-level prevention against parenthetical explanations in rewrite.txt and mediate.txt

### 1.25.0 (MINOR) -- Mobile dashboard compatibility

- feat(dashboard): full mobile responsiveness for admin/analytics dashboards
- feat(dashboard): touch-friendly controls with 44px+ touch targets (sidebar toggle, nav links, buttons, toggles, tabs, range sliders)
- feat(dashboard): viewport-aware sidebar with swipe gestures (open from left edge, close by swiping left)
- feat(dashboard): responsive padding reduction on mobile (page-content, top-bar, setup shell, login)
- feat(dashboard): collapsible grids (stat-grid-6, card-grid, stat-grid, stat-grid-3) with safe minmax values
- feat(dashboard): Chart.js legend switches to bottom on screens <=480px
- fix(dashboard): modal `min-width` uses `min(320px, 90vw)` to prevent overflow on small screens
- fix(dashboard): add `overflow-x: auto` back to `.table-container-flush`
- fix(dashboard): replace `100vh` with `100dvh` in chat container for dynamic browser toolbars
- fix(dashboard): wrap trace detail gantt chart in overflow container
- fix(dashboard): add `flex-wrap` to plugin cards, custom agent cards, trace communication rows
- fix(dashboard): reduce table cell max-widths on mobile (max-w-300/400 -> max-w-200)
- fix(dashboard): settings rail becomes horizontal scrollable tab bar on mobile
- fix(dashboard): provider rows and custom provider headers stack vertically on narrow screens
- refactor(dashboard): remove duplicate `utilities.css` and its template references
- chore(css): add Google Fonts `display=swap` parameter
- chore(css): add fluid `clamp()` font sizing for stat card values

### 1.24.2 (PATCH)

- fix(ha): validate response body in `HARestClient.test_connection()` -- now checks for `{"message": "API running."}` instead of only HTTP 200
- fix(ha): correct `get_services()` docstring from "service list" to "service dict"
- fix(ha): send `"true"` instead of `"1"` for boolean query params in `get_history_period()`
- feat(ha): add `no_attributes` parameter to `get_history_period()` (defaults to `True`)
- fix(types): resolve 282 mypy errors across 73 files
- chore(deps): bump chromadb, python-multipart, mypy, respx

### 1.24.0 (MINOR) -- WebSocket fallback for HA service calls

- feat(ha): add `HAWebSocketClient.call_service()` method for executing HA service calls via WebSocket API
- feat(ha): implement transparent WebSocket fallback in `HARestClient.call_service()` when REST API returns HTTP 500 or when `return_response=True` is requested
- fix(climate): resolve weather forecast failures caused by HA 2026.5.2 REST API service call bug by routing `weather.get_forecasts` through WebSocket fallback
- test(ha): add 5 WebSocket `call_service` tests (`TestHAWebSocketCallService`)
- test(ha): add 6 REST fallback tests covering 500 errors, `return_response=True`, WS unavailable, and no-fallback paths
- test(climate): add executor integration test verifying WS response shape handling for weather forecasts

### 1.23.1 (PATCH) -- CI pip-audit fix

- fix(ci): allow pip-audit to pass despite unfixed upstream vulnerabilities in transitive deps (torch, transformers, pyjwt, joblib). The security scan reports continue to be generated and uploaded as artifacts.

### 1.23.0 (MINOR) -- Security hardening, cache performance, and agent architecture refactoring

- fix(ws): reject all origins when allowed_ws_origins is empty
- fix(llm): add explicit timeout to all acompletion calls
- fix(auth): set cookie path to root; fix setup completion race
- fix(mcp): enforce timeout on tool calls
- fix(auth): thread-safe session serializer initialization
- fix(ha_ws): narrow exception handling in websocket connect
- feat(ws): add per-IP connection limit for WebSocket endpoint
- fix(cache): replace full-collection LRU scan with indexed eviction
- fix(db): narrow JSON exception handling in synonym cache
- fix(perf): cap legacy warning keys in base cache
- refactor(executor): deduplicate light resolver into deterministic_resolver
- feat(agents): extract CachedAgentRegistry with TTL-cached lookups
- feat(agents): extract shared TaskPipeline from orchestrator
- feat(db): extract settings repository from god module
- fix(perf): use set for dedup in orchestrator sanitize
- test(agents): split monolithic test_agents.py by domain
- test(timeout): implement per-agent timeout cascade tests
- test(auth): add edge-case tests for auth expiry, concurrency, brute-force
- docs(integration): document tolerated Prime Directive 1 exception for post-filler announce

### 1.22.6 (PATCH) -- Fix missing cancel-interaction span in trace timeline

- fix(orchestrator): add `dispatch` span for `cancel-interaction` fast-path in both streaming and non-streaming pipelines. Previously, the fast-path bypassed normal dispatch and created no execution span, leaving a visible gap between `classify` and `return` in trace timelines. `cancel-interaction` also did not appear in the trace `agents` list.

### 1.22.5 (PATCH) -- Cache invalidation fix

- fix(cache): only invalidate cache entries on registry events when relevant fields changed (name, area_id, device_id, hidden, disabled, aliases, labels, etc.)
- fix(cache): add INFO/DEBUG logging for successful cache invalidation with per-tier deletion counts

### 1.22.4 (PATCH) -- Entity resolution performance fixes

- fix(entity): cap embedding oversample at `max(20, top_n * 2)` to avoid excessive HNSW queries when filtering is active.
- fix(entity): skip EmbeddingSignal search when pre-filtered candidates are passed to the matcher; Levenshtein/JaroWinkler/Phonetic signals still run on the candidate set.
- fix(entity): avoid duplicate `_list_index_entries` calls in `_resolve_light_entity` by caching visible entries in deterministic resolver metadata and reusing them in the action executor.
- fix(entity): add HNSW index warm-up after entity index priming to eliminate cold-start latency on the first embedding search.

### 1.22.3 (PATCH) -- Voice follow-up satellite resolution for origin_device_id

- fix(background_actions): resolve `assist_satellite` entity from `origin_device_id` before triggering voice follow-up. Previously, when no `area_id` was present, the raw registry `device_id` was passed directly to `assist_pipeline/run`, causing a 400 Bad Request from Home Assistant.
- fix(background_actions): include HA response body in voice follow-up failure logs for easier debugging.

### 1.22.2 (PATCH) -- Suppress organic follow-up on cancel-interaction

- fix(orchestrator): suppress organic voice follow-up when the routed agent is `cancel-interaction`. Previously, the probabilistic follow-up offer ("Darf es noch etwas sein?" / "Is there anything else I can help with?") was incorrectly appended after a cancel intent.

### 1.22.1 (PATCH) -- Suppress organic follow-up on cancel-interaction

- fix(orchestrator): suppress organic voice follow-up when the routed agent is `cancel-interaction`. Previously, the probabilistic follow-up offer ("Darf es noch etwas sein?" / "Is there anything else I can help with?") was incorrectly appended after a cancel intent.

### 1.21.1 (PATCH) -- CI/CD pipeline and Docker security hardening

### 1.21.1 (PATCH) -- CI/CD pipeline and Docker security hardening

- fix(docker): remove `gosu` binary and all associated Go-stdlib CVEs; replace with `setpriv` from util-linux
