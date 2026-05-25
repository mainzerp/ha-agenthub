# Version

**Current Version:** 1.27.0

## Recent Changes

Track changes since `v1.27.0` here.

## Version History

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

## Version History

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
- feat(agents): extract AgentRegistry with TTL-cached lookups
- feat(agents): extract shared TaskPipeline from orchestrator
- feat(db): extract settings repository from god module
- fix(perf): use set for dedup in orchestrator sanitize
- test(agents): split monolithic test_agents.py by domain
- test(timeout): implement per-agent timeout cascade tests
- test(auth): add edge-case tests for expiry, concurrency, brute-force
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
