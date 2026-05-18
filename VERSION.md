# Version

**Current Version:** 1.22.5

## Recent Changes

Track changes since `v1.22.5` here.

## Version History

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
