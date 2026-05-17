# Version

**Current Version:** 1.22.3

## Recent Changes

Track changes since `v1.22.3` here.

## Version History

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
