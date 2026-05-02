# Version

**Current Version:** 1.14.1

## Recent Changes (since 1.14.0)

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


## Version History

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
- HA bridge: push aborts cleanly if the user starts a new turn before the announce fires (satellite re-enters `listening`/`processing` after the filler's `responding`→`idle` cycle), preventing audio overlap with the new turn.
- HA bridge: removed the dead FillerGate machinery (`_arm_filler_gate`, `_await_filler_gate`, the `_filler_gate.py` module, the media_player state-listener callbacks, the old `MAX_FILLER_WAIT_SECONDS` constant, and the `_speak_filler` / sibling-`tts.speak` / `_resolve_tts_engine_entity` / `_resolve_tts_entity` helpers if exclusively used by the deleted path) since the V4 filler-first design has no awaitable gate and no in-pipeline announce branch.
- HA bridge: filler-path diagnostics promoted from DEBUG to INFO/WARNING with a `"ha-agenthub:"` log prefix so future stalls leave a visible trail at the default INFO level. New lines: filler-first return (INFO), push received final (INFO), push dispatching announce (INFO), push cancelled / superseded / abandoned (INFO), push WS-closed / final-timeout / idle-timeout / no-satellite / announce-failed (WARNING).
- HA bridge: added `enable_post_filler_push` config option (default `True`) as a kill switch — set to `False` to revert to a V3-style "buffer fillers, return combined string at end of stream" behaviour without code rollback.

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
