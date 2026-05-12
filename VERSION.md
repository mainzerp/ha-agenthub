# Version

**Current Version:** 1.22.0

## Recent Changes

Track changes since `v1.22.0` here.

- feat(llm): add custom OpenAI-compatible provider support (`custom_openai`) with configurable base URL, API key, and extra headers
- fix(cache): external embedding now resolves provider params (API key, base URL) via `resolve_provider_params` instead of relying on env vars
- feat(admin): new `PUT /api/admin/llm-providers/custom-openai` endpoint for custom provider configuration
- feat(setup): setup wizard step 4 now includes custom provider fields (name, URL, key, headers)

## Version History

### 1.21.1 (PATCH) -- CI/CD pipeline and Docker security hardening

- fix(docker): remove `gosu` binary and all associated Go-stdlib CVEs; replace with `setpriv` from util-linux
- fix(docker): pin base image to verified digest; add `apt-get upgrade -y` to both build stages
- fix(docker): upgrade pip and setuptools during build; explicit Pillow upgrade after torchvision
- fix(deps): require `Pillow>=12.2.0` and `python-dotenv>=1.0.0` as explicit minimum pins
- fix(deps): relax litellm pin from exact to range (`>=1.83.7,<2.0.0`)
- fix(ci): consolidate Trivy scanning into SARIF (exit-code 0) + gate (exit-code 1, ignore-unfixed) steps
- fix(ci): restrict Trivy to vulnerability scanner only (`scanners: vuln`), preventing secret/misconfig false positives
- fix(ci): add `limit-severities-for-sarif` to respect severity filter in SARIF output
- fix(ci): remove redundant explicit ruff install step (already pinned in requirements-dev.txt)
- fix(ci): add `.trivyignore` for CVE-2023-45853 (zlib1g, marked will-not-fix by Debian upstream)

### 1.21.0 (MINOR) -- Music Agent official music_assistant integration

- feat(agent): Migrated Music Agent from legacy HACS `mass.*` service namespace to official Home Assistant core `music_assistant.*` services (available since HA 2024.12).
- feat(agent): `play_media` action now supports `artist`, `album`, and `radio_mode` parameters.
- feat(agent): `search` action now supports `artist`, `album`, and `library_only` parameters.
- feat(prompt): Updated `music.txt` prompt documentation to reflect new `music_assistant` services and expanded `enqueue` values (`play|replace|next|replace_next|add`).
- test(agent): Updated and expanded `test_action_executor.py` Music Agent tests for the new service namespace and parameters.
- **Breaking change:** Users still on the legacy HACS `mass` integration must migrate to the official `music_assistant` integration before upgrading.

### 1.20.1 (PATCH) -- Dependency updates

- ci: bump cryptography from 46.0.7 to 48.0.0
- ci: bump uvicorn from 0.44.0 to 0.46.0
- ci: bump pydantic-settings from 2.13.1 to 2.14.1
- ci: update pytest-cov requirement from >=5.0.0 to >=7.1.0
- ci: update pytest-xdist requirement from >=3.5.0 to >=3.8.0
- ci: bump actions/upload-artifact from 4 to 7
- ci: bump actions/attest-build-provenance from 1 to 4

### 1.20.0 (MINOR) -- Cover Agent, Vacuum Agent, and Climate Agent fan/humidifier support

- feat(agent): New Cover Agent (`cover-agent`) controls blinds, curtains, shutters, garage doors, gates, awnings, and windows via HA `cover` domain services: open, close, stop, set position, and tilt actions.
- feat(agent): New Vacuum Agent (`vacuum-agent`) controls robot vacuums via HA `vacuum` domain services: start, pause, stop, return to base, clean spot, set fan speed, locate, and send custom commands.
- feat(agent): Climate Agent extended with `fan` and `humidifier` domain support. Generic `turn_on`/`turn_off` actions resolve the correct HA service domain at runtime based on the matched entity.
- feat(agent): New fan actions: `set_fan_percentage`, `set_fan_preset_mode`, `fan_oscillate`, `set_fan_direction`.
- feat(agent): New humidifier actions: `set_humidifier_humidity`, `set_humidifier_mode`.
- feat(db): Migration 31 adds default entity visibility rules for `cover-agent` -> `cover`, `vacuum-agent` -> `vacuum`, and `climate-agent` -> `fan`/`humidifier`.
- feat(db): Migration 32 adds `cover-agent` and `vacuum-agent` to `agent_configs`, updates `climate-agent` description.
- feat(api): `domain_agent_map_api.py` includes `cover-agent` and `vacuum-agent` in `BUILT_IN_AGENTS`.
- fix(readme): Updated README workflow badges to use shields.io with labeled CI jobs.
- fix(agent): Corrected Vacuum Agent registration in `runtime_setup.py` (was in `phase2_agents` requiring DB config, now directly registered like other domain agents).
- fix(agent): Restored detailed AgentCard descriptions for orchestrator routing after accidental shortening.
- chore(repo): Removed unused diagnostic scripts (`check_config.py`, `check_spans.py`), stale `.github/copilot-instructions.md`, dead legacy comments from `__init__.py` files, and cleaned up SubAgent artifacts.
- ci(docker): Only build image on tagged releases.

### 1.19.14 (PATCH) -- Fix follow-up badge to only show on voice follow-up

- fix(traces): FOLLOW-UP badge now only appears when `voice_followup === true` (AgentHub actively keeps microphone open), not for every multi-turn session.
- feat(db): Migration 30 adds `voice_followup INTEGER DEFAULT 0` column to `trace_summary`.
- feat(tracer): `create_trace_summary` accepts new `voice_followup` parameter.
- feat(repository): `TraceSummaryRepository` persists and returns `voice_followup`.
- fix(orchestrator): `voice_followup_effective` is now passed through to trace creation in both normal and action-cache-hit paths.
- fix(frontend): Badge logic in `traces.html` and `trace_detail.html` now uses `voice_followup` instead of `conversation_turns.length > 0`.

### 1.19.13 (PATCH) -- Fix follow-up badge showing for all traces in list view

- fix(traces): deserialize `conversation_turns` in `TraceSummaryRepository.list_filtered()` so the FOLLOW-UP badge only appears for actual multi-turn sessions.

### 1.19.12 (PATCH) -- Simplify CI workflow and bump version

- ci: simplify workflow from 10 jobs to 4 jobs (quality, security, docker, release)
- ci: remove typecheck, changes, and hacs jobs
- ci: quality job combines lint, format check, test, and coverage upload
- ci: security job combines bandit and pip-audit
- ci: docker job only builds/pushes on main branch and tags

### 1.19.11 (PATCH) -- Deep code review: critical fixes, security hardening, exception handling, test quality

- fix(entity): assign result of query.lower().strip() in matcher (containment scoring bug)
- fix(auth): stop regenerating CSRF token on every request (multi-tab race condition)
- fix(cache): avoid blocking `.result()` in ChromaEmbeddingFunction async bridge (deadlock risk)
- fix(cache): run ChromaDB heartbeat off event loop (Prime Directive 9 compliance)
- fix(security): eager-load Fernet key during startup lifespan (blocking I/O remediation)
- fix(db): create SQLite directory off event loop via `asyncio.to_thread`
- fix(api): do not leak exception details over WebSocket (information disclosure)
- fix(admin): remove API key substring disclosure from LLM providers endpoint
- refactor: narrow 321+ bare `except Exception` handlers and remove 30+ silent `pass` in except blocks
- test: remove flaky asyncio.sleep calls, use deterministic embeddings, re-enable skipped tests
- ci: fail build on security scan findings (bandit, pip-audit, trivy), add pytest-timeout, extend bandit to tests

### 1.19.10 (PATCH) -- Enforce 3-word minimum on cancel-interaction TTS responses

- Deterministic fallbacks changed: "Alles klar." -> "Alles klar, verstanden." and "Okay." -> "Okay, got it."
- `_is_acceptable()` now rejects LLM-generated responses shorter than 3 words (falls back to longer deterministic phrase).
- Cancel speech prompt updated to require at least 3 words and at most 10.
- Ensures cancel-interaction TTS audio is always >= ~1 second.

### 1.19.9 (PATCH) -- Show follow-up indicator in trace list and detail view

- Trace list now shows a `FOLLOW-UP` badge (teal) for traces that are part of a multi-turn session (conversation_turns non-empty).
- Trace detail header shows a `FOLLOW-UP` badge with prior message count in the summary bar.

### 1.19.8 (PATCH) -- Fix agent communication flow in trace detail view

- Agent Communication now shows the full 4-step flow for single-agent traces: user -> orchestrator -> subagent -> orchestrator -> user.
- Previously the orchestrator->subagent dispatch and raw subagent response were collapsed into one entry, and the mediated final response was incorrectly attributed to the subagent.
- Unaffected paths (action cache hit, filler, sequential send, multi-agent) are unchanged.

### 1.19.7 (PATCH) -- Fix mojibake encoding in user input pipeline

- German umlauts arriving double-encoded (UTF-8 bytes read as Latin-1, e.g. `KÃ¼che` instead of `Küche`) now get corrected in `prepare_user_text()` before sanitization and cache key generation.
- Cache hits now work universally across all input sources (satellites, chat, companion app) regardless of encoding at the sender side.
- Added `test_user_input.py` covering the fix and cross-source cache key consistency.

### 1.19.6 (PATCH) -- Fix event-loop-closed warnings in test suite

- Replaced `asyncio.get_event_loop().run_until_complete()` calls with proper `async/await` in tests.
- Added `shutdown_aiosqlite()` helper to join aiosqlite worker threads before the event loop closes, eliminating hundreds of `RuntimeError: Event loop is closed` warnings.
- Added `_patch_aiosqlite_close` session fixture and `_reset_write_conn` autouse fixture for reliable teardown across parallel test workers.

### 1.19.5 (PATCH) -- Timer TTS bug fixes

- Fixed duplicate/triple "Timer" prefix in timer-expiration TTS and notifications.
- Fixed audio cutoff on satellite devices by increasing TTS-to-listen delay from 4.0s to 10.0s.

### 1.19.4 (PATCH) -- Cache management UI enhancements

- Added per-entry deletion to the Cache Management dashboard.
- New `DELETE /api/admin/cache/entries/{entry_id}` endpoint supports deleting individual routing or action cache entries.
- Cache entry tables now show a "Created" column and display timestamps in the browser's local timezone via the shared `formatTimestamp()` helper.
- Added delete buttons to each row in both routing and action cache tables.

### 1.19.3 (PATCH) -- Trace timezone display fix

- Fixed timestamps in Request Traces and Trace Detail UI to display in the browser's local timezone instead of UTC.
- SQLite `datetime('now')` strings lack timezone info; the frontend `formatTimestamp()` helper now detects timezone-less timestamps and treats them as UTC before converting via `toLocaleString()`.
- Aligns trace timestamp display with the existing correct behavior in Remote Logs UI.

### 1.19.2 (PATCH) -- HA integration voice follow-up fix

- Fixed HA integration ignoring the backend's `voice_followup` flag.
- `ConversationResult` now correctly sets `continue_conversation=True` when the backend signals a voice follow-up, keeping the satellite microphone open after organic follow-up prompts (e.g. "Darf es noch etwas sein?").
- Backward-compatible: introspects `conversation.ConversationResult` signature so older HA versions that lack `continue_conversation` are not affected.

### 1.19.1 (PATCH) -- Timer-agent dead domain cleanup

- Removed dead `calendar` and `input_datetime` domain associations from `AGENT_ALLOWED_DOMAINS`.
- Deleted obsolete `_ALLOWED_DOMAINS`, `_INPUT_DATETIME_DOMAINS`, `_validate_domain`, `_list_visible_input_datetime_targets`, and `_should_attempt_set_datetime_fallback` code from `timer_executor.py`.
- Updated docstrings to reflect that all timer and alarm operations route exclusively to the internal `TimerScheduler`.
- Added schema migration v29 to clean up dead visibility rules from existing databases.

### 1.19.0 (MINOR) -- Entity not-found follow-up

- **Voice follow-up on entity not found:**
  - When an agent reports that an entity was not found, the orchestrator now dispatches a follow-up clarification turn through the LLM instead of a hardcoded message.
  - Timer-agent skips not-found clarification because it manages internal scheduler entries, not HA entities.
  - Organic follow-up is also applied on action-cache replay hits.

- **Timer-agent domain cleanup:**
  - Removed dead `calendar` and `input_datetime` domain associations from `AGENT_ALLOWED_DOMAINS`.
  - Deleted obsolete `_ALLOWED_DOMAINS`, `_INPUT_DATETIME_DOMAINS`, `_validate_domain`, `_list_visible_input_datetime_targets`, and `_should_attempt_set_datetime_fallback` code from `timer_executor.py`.
  - Updated docstrings to reflect that `set_datetime` and alarms route exclusively to the internal `TimerScheduler`.
  - Added schema migration v29 to clean up dead visibility rules from existing databases.

- **Settings dashboard fixes:**
  - Normalized boolean values to lowercase before storing to prevent truthy-string mismatches.
  - Translated Orchestrator section labels to English for consistency.

- **HA integration hardening:**
  - Catches `AttributeError` for removed `async_migrate_engine` during integration setup.

- **Logging improvements:**
  - `LogBufferHandler` now captures `exc_info` traceback correctly.
  - Fixed reversed filtered log entries in `get_entries` method.

### 1.18.0 (MINOR) -- Multilingual orchestrator & code-review hardening

- **Multilingual orchestrator output:**
  - The orchestrator now writes condensed tasks directly in the user's language instead of translating to English and preserving verbatim terms.
  - Removed `_extract_verbatim_terms` and `_append_original_suffix` heuristic pipeline from `orchestrator.py`.
  - Simplified all agent prompts: replaced lengthy "ENTITY NAMES MUST NEVER BE TRANSLATED" blocks with concise "Entity names: use the exact spelling from the task description."
  - `AgentTask.verbatim_terms` is now optional and unused by the orchestrator; sub-agents or plugins may still populate it if needed.
  - Added README note recommending explicit language configuration for best voice-command results.

- **Code-review security and robustness fixes:**
  - `get_db_write()` now auto-commits on success; prevents silent data loss from forgotten manual commits.
  - `SettingsRepository._value_cache_lock` is now lazily initialized to avoid event-loop conflicts during tests or uvicorn reload.
  - `_cache_invalidate()` is now async and holds the cache lock.
  - Server-side password minimum length (8 chars) enforced in setup wizard and dashboard login.
  - `_dispatcher` null-check returns 503 if the service is not yet ready.
  - `LogBuffer.get_entries()` timezone-aware datetime comparison fixes 500 errors on naive `since` parameters.
  - `complete_with_tools()` catches all exceptions from tool executors instead of a narrow allow-list.
  - WebSocket rate-limit no longer kills the connection; it sends a JSON error and keeps the socket open.
  - `SpanCollector._spans` access is now encapsulated via `add_root_span()`.
  - `McpServerRepository.list_enabled()` now handles malformed JSON gracefully.
  - Conversation search properly escapes `LIKE` wildcards (`%`, `_`).
  - `CalendarReminderStateRepository.cleanup_old()` uses consistent ISO-string timestamps.
  - `PluginLoader.enable_plugin()` is now protected by an asyncio lock against race conditions.
  - `SetupRedirectMiddleware` gained `invalidate_setup_cache()` for future setup-reset support.
  - `AnalyticsRepository.query_by_range()` caps `limit` at 5000 rows.
  - CSRF token rotates when an admin session is active.
  - `_phonetic_key()` logs failures instead of swallowing them silently.
  - `_post_filler_push` in HA integration catches all exceptions broadly.
  - `get_fernet()` deadlock avoided by not nesting the non-reentrant lock.
  - `log_buffer_guard_task` is now cancelled cleanly on shutdown.
  - Migration 16 uses `_column_exists()` consistently.

### 1.17.1 (PATCH) -- Remote logs bug fixes and UI polish

- Fixed `LogBufferHandler` disappearing in Docker containers caused by uvicorn resetting root logger handlers after lifespan startup. The `_log_buffer_guard` background task now re-attaches the handler, and `logs_api.py` uses `get_log_buffer()` at runtime instead of a stale module-import reference.
- Added `get_log_buffer()` accessor to `app.util.log_buffer` to prevent stale reference issues across modules.
- Log Level Manager badges are now filtered to exclude noisy third-party libraries (torch, transformers, apscheduler, strobelight, c10d, httpx, huggingface_hub, numba, triton).
- Log Level Manager badges are now clickable buttons: click copies the logger name into the edit field for quick level adjustment.
- Eliminated table flicker on the logs page by suppressing the loading spinner during background auto-refresh polls and only updating the entries array when data actually changes.
- Updated tests to use `get_log_buffer()` / `set_log_buffer()` instead of direct module attribute access.

### 1.17.0 (MINOR) -- Remote logs API

- Added in-memory ring buffer (`LogBuffer`) and custom `LogBufferHandler` for capturing application logs at runtime.
- New admin endpoints under `/api/admin/logs`:
  - `GET /api/admin/logs` -- paginated, filterable log query (level, logger, since, search, limit, offset).
  - `GET /api/admin/logs/levels` -- read root and per-logger levels.
  - `POST /api/admin/logs/levels` -- runtime log level adjustment.
- All endpoints require admin session auth and rate limiting.
- New files: `container/app/util/log_buffer.py`, `container/app/api/routes/logs_api.py`, `container/tests/test_logs_api.py`.

### 1.16.0 (MINOR) -- Lists Agent for HA todo list management

- Added `lists-agent` for managing Home Assistant todo and shopping lists.
  - Supports `list_lists`, `list_items`, `add_item`, `complete_item`, `remove_item`, and `clear_completed` actions.
  - Uses HA `todo` domain services (`todo.get_items`, `todo.add_item`, `todo.update_item`, `todo.remove_item`).
  - Handles multiple comma-separated items for add/complete/remove operations.
  - Includes entity resolution via deterministic + hybrid matching for `todo.*` entities.
- New files: `container/app/agents/lists.py`, `lists_executor.py`, `prompts/lists.txt`.
- New tests: `container/tests/test_lists_executor.py` (26 tests, all passing).
- Wired into runtime setup, agent registry, domain-agent map API, DB schema seeds, and prompt cache.

### 1.15.3 (PATCH) -- Pylance and linter fixes

- Fixed Pylance `reportCallIssue` errors in `rewrite.py` by explicitly passing default values for `AgentCard` fields (`expected_latency`, `timeout_sec`) and `TaskResult` fields (`action_executed`, `error`, `voice_followup`, `directive`, `reason`).
- Resolved ruff TRY300 and D102 warnings in `rewrite.py`.

### 1.15.2 (PATCH) -- Trace span fix for cache-hit path

- Fixed rewrite span to wrap actual `apply_rewrite` call instead of being created retroactively.
- Added `calendar_inject` span around calendar reminder injection.
- Removed obsolete `_override_duration_ms` workaround.

### 1.15.1 (PATCH) -- Rewrite-agent language bug fix

- Fixed rewrite-agent so non-English utterances produce rewritten output in the correct language during action-cache hits.
- Added `{language}` placeholder to `rewrite.txt` prompt.
- Threaded `user_text` through cache-hit path (`rewrite.py`, `cache_manager.py`, `orchestrator.py`).
- Added regression tests for rewrite language injection, user-text formatting, and cache-hit forwarding.

### 1.15.0 (MINOR) -- Wikipedia search tool for general-agent

- Added Wikipedia MCP server (`wikipedia-search`) with two tools:
  - `wikipedia_search`: Search Wikipedia articles by query (1-10 results).
  - `wikipedia_summary`: Retrieve a summary of a specific Wikipedia article by exact title (1-20 sentences).
- Server is auto-registered as built-in and its tools are auto-assigned to `general-agent` on startup.
- Updated `general-agent` prompt with Wikipedia usage guidelines.
- Added unit tests for Wikipedia server tools.

### 1.14.1 (PATCH) -- Security & Safety Hardening

- **Security & Safety Hardening (Deep Code Review):**
  - Removed `live_deployment.md` which contained hardcoded production credentials.
  - Eliminated Jinja2 SSTI vulnerability in admin routes by passing user input as template variables instead of string concatenation.
  - Fixed blocking `time.sleep()` in async embedding retry path (`asyncio.sleep` now used).
  - Hardened `X-Forwarded-For` parsing in rate-limit middleware to use rightmost non-trusted IP, preventing spoofing bypass.
  - Offloaded CPU-bound SentenceTransformer embedding to thread pool via `asyncio.to_thread`.
  - Added WebSocket origin validation to reject cross-origin connections.
  - Secret decryption failures now raise `RuntimeError` instead of silently returning `None`.
  - SSE ticker tasks are now deduplicated on re-registration and properly cancelled on shutdown.
  - Narrowed overly broad `except Exception` clauses in schema fallback and entity update flush.
- **HA Integration Fixes:**
  - Narrowed HA bridge exception handler to only catch transport errors (`aiohttp.ClientError`, `asyncio.TimeoutError`, `OSError`), preventing programming errors from being silently swallowed.
  - Added per-frame JSON decode error handling in WebSocket loop so malformed frames are skipped without aborting the stream.
  - Guarded config migration against invalid legacy URLs that would permanently crash migration.
  - Removed dead `_push_in_progress_satellites` reentrancy guard code.
  - Added missing `invalid_url` translation key to options flow strings.
  - Defensively coerced `None` speech values to `""` in `_build_result`.
- **Infrastructure & CI:**
  - Added Trivy container vulnerability scanning to Docker build and release workflows.
  - Added Dependabot configuration for pip, GitHub Actions, and Docker ecosystems.
  - Added Docker Compose security hardening (`read_only`, `cap_drop`, `no-new-privileges`).
  - Pinned Dockerfile base image to `python:3.12-slim-bookworm` and replaced apt-installed `gosu` with verified binary download.
  - Changed default Docker Compose tag from `:main` to `:latest`.
  - Hardened `.dockerignore` to exclude sensitive files from build context.
  - Expanded pre-commit hooks with security checks (`detect-private-key`, `bandit`).
  - Added CI lint and smoke-test coverage for `custom_components/`.
  - Pinned HACS validation action to release tag `22.0.0`.
  - Added `CODEOWNERS` and `SECURITY.md`.
  - Security scan artifacts (Bandit, pip-audit) are now uploaded in CI.

## Older History

### 1.13.1 (PATCH) -- Orchestrator entity-name preservation

- Hardened entity-name preservation in the orchestrator classification prompt. The LLM is now explicitly warned that translating any entity, room, device, or location name will cause the downstream agent to fail to find the device. The dynamic `language_hint` injected for non-English utterances carries the same warning.

### 1.4.1 (PATCH) -- Neutral structured replay context

- Replaced deterministic replay speech templating with a neutral `ReplayContext` passed into the rewrite agent.
- Removed the internal `fresh_text` rewrite path so cache hits no longer overwrite the cached `response_text` before rewrite.
- Kept v3 structured-key replay correctness intact while adding regression coverage for structured replay context and rewrite-disabled cache hits.

### 1.4.0 (MINOR) -- Structured action cache key

- Action cache is now keyed by a structured action signature `(language, target_agent, domain, service, normalized target set, normalized service_data)` instead of semantic nearest-neighbor over user text, eliminating polarity and cross-room replay collisions.
- Cache-hit rewrite now operates on freshly templated speech derived from the executed cached action; cached `response_text` is retained only as a personality-voice reference.
- Schema bumped to v3; legacy v2 rows are ignored on read and one-shot purged on startup.
- Export/import format bumped to v3.

### 1.3.3 (PATCH) -- HA bridge V4 audit fixes

- Echo guard for the post-filler push pipeline now requires both satellite identity AND normalized inbound text to match a recent announcement within an 8 s TTL, instead of suppressing every turn from a satellite while a push is in flight. Eliminates the possibility of silently masking unrelated voice turns.
- Push-task cancellation on supersession and on integration unload/removal now awaits the cancelled task's cleanup (state listener unsubscribe, local WS close) before proceeding.
- Optional `homeassistant.helpers.event.async_track_state_change_event` import now also tolerates `ImportError` (in addition to `ModuleNotFoundError`); when the symbol is unavailable the push falls back to a fixed `POST_FILLER_FALLBACK_DELAY_SECONDS = 1.5` delay before announcing.
- WebSocket ownership transfer in `_process_via_ws` is now atomic: the background push task is registered before `self._ws` is detached; if registration fails the foreground keeps the socket and falls back to the buffered unified-string path.
- Removed the stale empty `_filler_gate.py` artifact.
- Added two `DEBUG`-level diagnostic log lines (turn entry, WebSocket entry) under the `ha-agenthub:` prefix so future "no container trace, no HA log" regressions are immediately diagnosable when debug logging is enabled.

### 1.3.2 (PATCH) -- Filler-first return + post-idle announce push

- HA bridge: fixed satellite stuck in "processing" with no audio after a filler-then-final response, and now audibly bridges the agent compute time. The integration ends the originating satellite's active assist_pipeline run with the filler text as the spoken result, so the user hears the verbal acknowledgement within ~1 s of finishing speaking and the satellite's LEDs return to `idle` cleanly. The actual final answer is then pushed via a separate `assist_satellite.announce` call once the satellite is observed back in `idle`, so the announce no longer collides with the active pipeline.
- HA bridge: WebSocket ownership transfers from the foreground conversation call to a background "push" task when a filler frame arrives first; the foreground returns the filler ConversationResult immediately and the background continues reading the WS for the final, watches the satellite state, and dispatches the announce only after the satellite is idle. Push tasks are tracked per satellite (one in-flight per satellite; supersession cancels the previous task) and are registered as HA background tasks so they are cancelled on integration reload.
- HA bridge: push aborts cleanly if the user starts a new turn before the announce fires (satellite re-enters `listening`/`processing` after the filler's `responding`->`idle` cycle), preventing audio overlap with the new turn.
- HA bridge: removed the dead FillerGate machinery (`_arm_filler_gate`, `_await_filler_gate`, the `_filler_gate.py` module, the media_player state-listener callbacks, the old `MAX_FILLER_WAIT_SECONDS` constant, and the `_speak_filler` / sibling-`tts.speak` / `_resolve_tts_engine_entity` / `_resolve_tts_entity` helpers if exclusively used by the deleted path) since the V4 filler-first design has no awaitable gate and no in-pipeline announce branch.
- HA bridge: filler-path diagnostics promoted from DEBUG to INFO/WARNING with a `"ha-agenthub:"` log prefix so future stalls leave a visible trail at the default INFO level. New lines: filler-first return (INFO), push received final (INFO), push dispatching announce (INFO), push cancelled / superseded / abandoned (INFO), push WS-closed / final-timeout / idle-timeout / no-satellite / announce-failed (WARNING).
- HA bridge: added `enable_post_filler_push` config option (default `True`) as a kill switch -- set to `False` to revert to a V3-style "buffer fillers, return combined string at end of stream" behaviour without code rollback.

### 1.3.1 (PATCH) -- Filler playback waits for real completion signals only

- The Home Assistant bridge now waits for real filler completion signals before returning the final spoken reply: `assist_satellite.announce` uses `blocking=True`, and the TTS fallback waits for the target `media_player` to leave `playing` after first observing playback start.
- Retained `MAX_FILLER_WAIT_SECONDS` only as a stuck-signal safety net and removed all duration-estimation behavior from the filler overlap path.

### 1.3.0 (MINOR) -- Routing-cache hardening for compound and parse-miss utterances

- Routing cache no longer persists single-agent decisions before dispatch; the store moved to post-execution and is gated on `action_executed.success` for actionable routes.
- Added orchestrator-side routing-hit fall-through: routing-cache hits that dispatch a single actionable agent and yield no executed action and no error are now invalidated and re-classified live for the same turn.
- Added a purely structural, language-agnostic compound-utterance detector that bypasses the routing-cache lookup for obviously multi-sentence inputs (no keyword lists, sentence-terminator + segment-word-count only).
- Added `RoutingCache.invalidate` / `CacheManager.invalidate_routing`.
- Closes the live failure where `Cached` routing on `"Kueche ausschalten. Dann neben sie machen wir es auf Ruhe Musik."` produced spoken success without any HA service call.

### 1.2.0 (MINOR) -- Dashboard frontend hardening

- MCP server form: replaced removed `http` transport option with `sse`; API now rejects unsupported transports; one-time migration 22 rewrites legacy `http` rows to `sse`.
- Vendored Alpine.js 3.x at `container/app/dashboard/static/alpine.min.js`; CDN-failure path now surfaces a visible operator-facing banner.
- Logout converted from GET to POST with CSRF protection; sidebar uses a hidden form button.
- All dashboard pages now route fetches through `window.dashFetch` / `window.dashboardApi` so session-expired and HTTP errors are handled consistently.
- Mobile sidebar now toggles `inert` + `aria-hidden` so off-canvas content is not in the tab order; timer modal gained `role="dialog"`, `aria-modal`, labelled title, and Escape-to-close.
- Polling intervals on overview, system-health, and timers pages are now cleared on Alpine destroy.
- CSS: added global `[x-cloak]` rule; replaced unused `btn-xs` usages with the existing `btn-sm`.
- Translated remaining German empty-state strings on the entity-index diagnostics block to English.
- Removed orphaned `conversations.html` and `rewrite_config.html` templates.
- New `dashUrl()` helper plus `root_path`-aware redirects make the dashboard work behind a reverse-proxy subpath.

### 1.1.0 (MINOR) -- LLM-generated cancel-interaction acknowledgement

- Replaced the static cancel-interaction ACK ("Okay." / "Alles klar.")
  with an LLM-generated single short spoken acknowledgement that varies
  phrasing naturally for dismissals like "Abbrechen", "Vergiss es",
  "never mind", and "forget it".
- Added `container/app/agents/cancel_speech.generate_cancel_speech(...)`
  with a 1.5s hard timeout via `asyncio.wait_for` and a deterministic
  static fallback on LLM timeout, exception, empty or whitespace output,
  or guardrail violation (length, follow-up question, markdown).
- Wired the new helper into both orchestrator cancel short-circuit paths
  (`_dispatch_single` and `handle_task_stream`) without changing the
  no-dispatch, no-mediation, or no-cache-store contract for
  `cancel-interaction`.
- Reused the existing `filler-agent` configuration row for the cancel
  acknowledgement LLM call, with per-call `max_tokens=30` and
  `temperature=0.6`, so no new `agent_configs` seed row or migration was
  required.

### 1.0.0 (MAJOR) -- Wake briefing alarms and weather-query hardening

- Added internal-alarm wake briefing support across the timer prompt,
  timer executor, scheduler persistence, alarm background handling, and
  timers dashboard settings UI. Internal alarms can now opt into a
  composed spoken wake briefing that merges date facts, weather, news,
  calendar events, and optional configured sensor readings.
- Added the orchestrator-owned wake briefing composer in
  `container/app/agents/wake_briefing.py`, with the A2A boundary kept
  strict: cross-agent data gathering goes only through
  `OrchestratorGateway`, while calendar and sensor facts come from the
  HA REST client.
- Added structured admin settings endpoints and dashboard controls for
  wake briefing configuration, plus focused regression coverage for the
  new alarm/background/API/DB paths.
- Fixed climate weather-query routing so short weather questions now
  reliably produce structured weather actions, allow entity-less weather
  queries, and auto-discover only visibility-permitted weather entities.
- User-directed release note: version advanced from `0.31.0` to
  `1.0.0` as a MAJOR release. Sentinel mode remains deferred and is not
  part of this release.

Older releases are archived in [docs/CHANGELOG_ARCHIVE.md](docs/CHANGELOG_ARCHIVE.md).
