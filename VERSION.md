# Version

**Current Version:** 1.23.1

## Recent Changes

Track changes since `v1.23.1` here.

## Version History

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
