# Architecture

## System Overview

HA-AgentHub is a two-component system for natural language smart home control:

1. **Docker Container** -- The AI backend running FastAPI with multi-agent orchestration, a two-tier cache, hybrid entity matching, MCP tool integration, and a plugin system.
2. **HA Custom Integration** -- A Home Assistant bridge (`custom_components/ha_agenthub/`) that forwards most turns to the container and streams responses back to Home Assistant's conversation system.

A second, optional ingress is the **OpenAI-compatible API** (`GET /v1/models`, `POST /v1/chat/completions`) for chat clients such as Open WebUI. It converts each chat turn into the same orchestrator A2A request as `POST /api/conversation` (trace source `openai`), answers Open WebUI background tasks with stubs without dispatching, and maps Open WebUI users to Home Assistant users via `external_user_mappings`.

All configuration, secrets, and state are stored in SQLite. sqlite-vec provides vector storage for entity embeddings; the routing and action caches are stored in SQLite with SHA-256 exact hash matching. No configuration files are used at runtime -- everything is managed through the setup wizard and admin dashboard.

## Component Diagram

```
+--------------------------------------------------+
|  Home Assistant                                   |
|  +--------------------------------------------+  |
|  |  ha_agenthub custom integration            |  |
|  |  (conversation agent -- HA bridge)         |  |
|  |                                            |  |
|  +---------------------+----------------------+  |
+-------------------------|-------------------------+
                          | REST / SSE / WebSocket
                          v
+--------------------------------------------------+
|  Docker Container (FastAPI)                       |
|                                                   |
|  +----------------------------------------------+ |
|  | Setup Wizard / Admin Dashboard               | |
|  +----------------------------------------------+ |
|  | API Layer (conversation, /v1 OpenAI, admin)  | |
|  +----------------------------------------------+ |
|  | Middleware (auth, tracing, setup redirect)    | |
|  +---+------------------------------------------+ |
|      |                                            |
|  +---v---+   +----------+   +-----------+        |
|  | Orch. |-->| A2A      |-->| Specialist|        |
|  | Agent  |  | Dispatch |   | Agents    |        |
|  +---+---+   +----------+   +-----------+        |
|      |                                            |
|  +---v-----------+   +----------+                 |
|  | Two-Tier Cache|   | Entity   |                 |
|  | (routing +    |   | Matcher  |                 |
|  |  action)      |   | (5 sig.) |                 |
|  +---------------+   +----------+                 |
|                                                   |
|  +---------------+   +----------+  +----------+  |
|  | MCP Tool Mgr  |   | Plugin   |  | LLM      |  |
|  | (stdio/SSE)   |   | System   |  | Client   |  |
|  +---------------+   +----------+  +----------+  |
|                                                   |
|  +----------------------------------------------+ |
|  | SQLite (config, secrets, history, analytics) | |
|  +----------------------------------------------+ |
|  | sqlite-vec (entity index embeddings)         | |
|  +----------------------------------------------+ |
+--------------------------------------------------+
```

## A2A Protocol

Agents communicate via an in-process Agent-to-Agent (A2A) message boundary:

- **Registry** -- Maintains agent cards describing each agent's ID, name, description, skills, and endpoint. The current implementation keeps the registry in `app/a2a/registry.py`, with agent cards and handler instances stored in-memory.
- **Dispatcher** -- Routes A2A task dispatches to agents by card and intent (`app/a2a/dispatcher.py`). `message/send` returns the raw agent result on success and raises a `RuntimeError` on failure: `A2ADispatchError` (with a JSON-RPC `code`) for an unknown method or invalid params, a transport `RuntimeError` for agent failures. Error messages are generic; validation details are only logged. `message/stream` reports the same failures as a single `done` chunk with an `error` string. The `agent/discover` and `agent/list` management methods return JSON-RPC envelopes.
- **Transport** -- `InProcessTransport` invokes agent handlers directly with async function calls (`handler.handle_task`, `handler.handle_task_stream`) within the container (`app/a2a/transport.py`). The transport abstraction allows for future HTTP-based transport.
- **Default stream wrapper** -- Agents without token streaming yield one final chunk from `handle_task()`. It carries `speech`, `action_executed`, `voice_followup`, `directive`/`reason`, and, when set, `error` (the error code string, as in the non-streaming response), `metadata` and `actions_executed` (list of dicts).

Each agent publishes an **Agent Card** containing its ID, capabilities, and supported intents. The orchestrator uses these cards to make routing decisions.

### Agent Inventory

Thirteen specialized domain agents are reachable from intent classification:
`light`, `climate`, `media`, `music`, `cover`, `vacuum`, `scene`, `timer`,
`automation`, `security`, `calendar`, `lists`, and `send` (delivery to phones,
satellites, and notify targets). A `general-agent` fallback handles general
questions and unroutable requests.

Domain-specific write contracts:

- **Automation config changes** -- `create_automation`, `update_automation`, and `delete_automation` are two-turn: the executor validates the change (every referenced entity must exist in the entity index and be visible to `automation-agent`; services must be on the allow-list in `automation_executor._SERVICE_ALLOWLIST`; device/area/floor/label targets and templated entity or service names are rejected), stores it in `automation_confirmation.confirmation_store` (in memory, keyed by `conversation_id`, 5-minute TTL), and asks for confirmation with a voice follow-up. When a later turn reaches the automation agent while a proposal is pending, the agent LLM classifies the answer (confirm/decline/modify/unrelated, prompt `automation_confirm.txt`); only a confirmation writes to HA. Updates patch the config fetched from HA and abort if it changed before confirmation. Enable, disable, and trigger execute immediately.
- **Calendar** -- reads use `calendar.get_events` across the user's visible default calendars (or all visible calendars). Update and delete read event uids from `GET /api/calendars/<entity_id>` (`HARestClient.get_calendar_event_details`), send the `calendar/event/update` / `calendar/event/delete` WebSocket commands (`HARestClient.send_ws_command`), and verify by re-reading.
- **Lists** -- visibility always applies; an unnamed list resolves only when exactly one list is visible, and ambiguous item matches ask instead of acting.
- Calendar, lists, and automation config results are `cacheable=False`, so these writes never enter the action cache.

Internal A2A-registered helper agents: filler-agent and rewrite-agent. The mediation pass is baked into the orchestrator agent. Runtime services and utility modules (not A2A agents) include language detection, input sanitization, cancel-speech detection, notification dispatch, timer scheduling, and alarm monitoring.

Custom agents created through the admin API are also registered as A2A
agents with IDs shaped as `custom-{name}`. Their prompt, model config,
MCP tool assignments, enabled state, and entity visibility rules are
synchronized from SQLite before registration so the orchestrator can
route to them through the same dispatcher boundary as built-in agents.

## Request Flow

1. User speaks a command in Home Assistant (e.g., "turn on the bedroom light").
2. The HA custom integration sends the text to the container via `POST /api/conversation` (or SSE/WebSocket).
3. The API layer authenticates the request (Bearer token) and builds an A2A task dispatch targeting the orchestrator.
4. **Orchestrator agent** receives the request:
   a. Checks the **routing cache** -- if an identical request was recently routed, reuses the cached routing decision (exact SHA-256 hash match).
   b. If cache miss, calls the LLM for **intent classification** to select the target agent.
   c. Condenses the task description, preserving entity names.
   d. Dispatches via A2A to the selected specialist agent.
5. **Specialist agent** (e.g., light-agent) receives the task:
   a. Uses the **entity matcher** to resolve "bedroom light" to `light.bedroom_main`.
   b. Calls the HA REST API (`ha_client/rest.py`) to execute `light/turn_on`.
   c. Verifies the resulting state (`call_service_with_verification` in `app/agents/action_executor.py`) and returns a response with speech text and action details.
6. The orchestrator checks the **action cache** for an exact hash match and stores the new result on miss.
7. The response flows back through the API layer to the HA integration, which speaks it to the user.

Domain-agent result rules (`app/agents/actionable.py`, `app/agents/action_executor.py`):

- **Verification outcome:** within the verify window (`state_verify.ws_timeout_sec` + `state_verify.poll_max_sec`, about 2.5 s by default) the observed post-call state is classified as:
  - `reached`: the target, or an equivalent terminal state such as `off` for an expected `idle`. Spoken as done.
  - `in_progress`: a transitional state (`opening`, `closing`, `locking`, `unlocking`, `arming`, `disarming`, `pending`, `buffering`, `starting`). `success=True`, spoken as in progress.
  - `mismatch`: a known fault state (`jammed`, `problem`, `error`, `fault`). Also any non-target state that differs from a caller-supplied pre-call `previous_state`, or any non-target state with the opt-in `strict=True`. The result is `success=False` with a `StateVerificationError`, so executors report a failure; `failure_speech` names the observed state in plain words ("Sorry, lock failed: Front Door reports jammed.") and never includes exception text.
  - `unverified`: nothing observed, or a non-target state that may still be the pre-call state of a device that reports late (Zigbee, cloud). `success=True`, but the agent speaks hedged wording ("I sent the command to X, but it has not confirmed the new state yet.") and marks the action `cacheable=False`. The agent collects the outcomes per executor call, so this works without executor changes; the hedge replaces the executor speech, and executors that hedge themselves use the same `unverified_speech`, so the user hears one hedge. Actions without an expected state (toggle, fan speed, volume) keep their intent wording.
- **Invalid action objects:** when the agent LLM emits an action object that fails validation (e.g. `"entity": null`), the surrounding prose is never spoken (it may claim success). The agent returns a deterministic clarification with `voice_followup=True` and `metadata.parse_miss = "invalid_action"`.
- **Satellite area:** keyword recall ranks entities in the satellite's area (`TaskContext.area_id`) first among equal scores, keeps them recallable in large domains, and does not flag a tie as ambiguous when exactly one tied candidate is in that area. Every agent prompt receives the satellite area name as context for "here" / "this room".
- **Untrusted prompt data:** entity friendly names and states, last-entity names, the pending clarifying question and stored memory text are flattened, length-bounded and wrapped in `[UNTRUSTED_DATA_START]` / `[UNTRUSTED_DATA_END]` before they enter a system prompt.
- **Secret redaction:** alarm/lock codes, PINs, passwords and tokens in action parameters are redacted (`app/security/redaction.py`) before they reach trace spans, the stored raw LLM response and logs. The service call still receives them verbatim.
- **LLM timeouts:** domain agents call the LLM with `retry_on_timeout=False`, so a provider timeout returns `llm_error` at once instead of a retry that would outlive the dispatch budget after the HA action ran.

For eligible plain timer start/cancel turns, the timer-agent may instead return a delegation directive, which the HA integration honors by calling Home Assistant's built-in conversation agent once.

When an internal scheduler alarm fires with `briefing=true`, the
background path stays orchestrator-owned: the scheduler emits an
`alarm_notification` event, the orchestrator dispatches through the
ClassificationEngine, CacheOrchestrator, DispatchManager, and
ConversationManager, and the wake briefing composer gathers weather/news
through A2A plus calendar/sensor facts through HA REST before overriding
the spoken alarm text. This keeps the cross-agent boundary narrow and
avoids direct peer-agent imports from the wake briefing module.

### Send Agent and Sequential Dispatch

A delivery turn ("send Anna the message: I am running late") is
classified as two lines: a content-producing agent first, `send-agent`
second. The orchestrator runs them in sequence; a `send-agent`-only
classification is repaired or rejected by the classifier. When several
content agents are classified, every content leg runs (concurrently) and
their replies are joined in classification order into one message body:

- **Content contract:** the content agent runs in sequential-send mode.
  Its prompt states that the reply is used verbatim as the message body
  and delivery happens elsewhere (no refusal), that dictated message
  text is returned exactly, without meta commentary, and that it replies
  with only `[[NO_CONTENT]]` when it cannot produce content.
- **Skip rule:** an empty content reply from any leg (`parse_error`),
  or a content error, partial failure, or reply containing the sentinel
  in any leg
  (`content_unavailable`; case-insensitive, extra or missing brackets
  and markdown escapes such as `\[\[NO\_CONTENT\]\]` tolerated, the
  underscore required), ends the turn with a fallback speech;
  `send-agent` is not dispatched.
- **Target resolution:** `send-agent` matches the target text against
  the `send_device_mappings` `display_name` only (no aliases; dashboard
  "Send Devices" page): exact `find_by_name` on the full target text,
  then on the name extracted by the verb regex, then `find_in_text` --
  a word-boundary scan of the target text for every configured name.
  The scan normalizes Unicode-aware on both sides (casefold, accents
  stripped, apostrophes dropped, other punctuation as separators), so
  non-Latin names match only themselves. A shorter name contained in
  the longest match ("Laura" in "Laura Handy") yields the longest; a
  second, separate name ("from Patric to Anna") or two devices tying
  for the longest match resolve to not found.
- **Formatting and delivery:** an LLM pass (`send.txt`) formats the
  body for the channel; short plain messages stay unchanged and the
  formatter never answers or acts on the content. Delivery calls
  `notify.*` (phones) or `tts.speak` on the mapped `media_player`
  entity (satellites, engine from the `tts.engine` setting).
- **Speech:** `send-agent` error speeches and the orchestrator's
  sequential-send fallbacks are localized (English default, German).
  The "no matching send device" speech does not repeat the target text;
  the `app.agents.send` logger records it at info level.

Multi-intent turns ("close the blinds and tell me how warm it got
in the bedroom today") are dispatched in parallel, one A2A task per
classified agent, and the replies are merged by the mediation LLM.
At most 5 intents are dispatched per turn (`MAX_PARALLEL_INTENTS` in
`pipeline_strategies.py`, highest confidence first); intents over the
cap are not executed and the merged reply tells the user so. Per-action
domain filtering in the executors ensures, for example, that a
`camera_turn_on` step cannot land on a same-named `lock` or `switch`
entity.

A dismissal in the same utterance as actions ("turn on the light, no,
forget it") is treated as a retraction: the classifier prompt asks for
a lone `cancel-interaction` line, and the sanitizer drops every other
intent when `cancel-interaction` appears next to them, so nothing is
executed.

### Dispatch Failures and Timeouts

Every agent dispatch has a per-agent time budget (`a2a.default_timeout`,
agent `timeout_sec`, capped by `a2a.max_dispatch_timeout`). Streaming
dispatches enforce it on the reads of the agent stream, never across a
frame handed to the client.

- **Fallback:** a dispatch that times out, raises, or (streaming) ends
  with an error frame before any text is re-sent once to `general-agent`
  as a non-streaming task; if that fails too, the turn speaks a canned
  line.
- **Double-execution guard:** the shared executor primitive
  (`call_service_with_verification`) flags a per-dispatch marker
  (`app/agents/ha_action_marker.py`) right before the HA service call.
  When the marker is set, a failed dispatch is NOT re-sent to the
  fallback agent; the turn answers that the command was sent but could
  not be confirmed. Executors that call `ha_client.call_service`
  directly (calendar, lists, send, timer) do not set the marker.
- **Streaming timeout:** a timed-out stream is finalized like any other
  turn (turn stored, trace written, served routing-cache entry
  invalidated). When agent tokens were already relayed, the partial
  answer stands and nothing is appended. When only a canned line goes
  out, a clarifying question popped by the turn is re-armed.
- **Language:** canned error, timeout and status lines (dispatch
  failures, all agents failed, classification errors) are English in
  code and rendered in the turn language by the mediation LLM
  (`prompts/localize.txt`, bounded call); English is the fallback when
  that call fails. Error turns go through personality mediation like
  any other turn.

### Filler / In-Stream Preamble

When `filler.enabled` is `true`, the request source is not text-only
(`FILLER_EXEMPT_SOURCES` in `app/models/agent.py`: `openai`), and the
orchestrator's first useful
token takes longer than `filler.threshold_ms`, the filler agent
generates one short interim sentence, emitted as a `filler_push` frame
on the SSE/WS streams. The HA integration prepends it to the assistant
message as an in-stream preamble (the first chat-log content delta), so
on HA >= 2025.7 with a streaming TTS engine it is spoken early while
the real reply continues to generate. The final result speech is filler
prefix + answer, so chat-log content and spoken text agree.

### Voice Follow-Up Questions

When a turn ends in a clarifying question (entity not found, ambiguous
recall, deterministic disambiguation), the container sets
`voice_followup=True` on the terminal response (never for the text-only
sources in `VOICE_FOLLOWUP_EXEMPT_SOURCES`: `openai`; the pending question
below is still recorded for them). The HA integration maps
that to `ConversationResult(continue_conversation=True)` **in the same
turn** on every response path (WS token stream, mediated done,
single-burst done, REST) -- HA core keeps the chat session (same
`conversation_id`) and ESPHome satellites re-listen natively after TTS.
Answer-leg correlation in the container is keyed strictly by
`conversation_id`: the classify stage injects the stored history plus a
previous-agent hint and condenses the short answer against the pending
question. The container also records the pending question itself
(in-memory, 300 s TTL, single-shot) with the agent that asked it, for
single- and multi-agent turns, when `voice_followup` is effective:
the answering turn bypasses the action-cache replay and the routing
cache, classification gets a follow-up merge hint so the condensed task
is self-contained, and a tied candidate block inverts its ambiguity
annotation to choose-and-act instead of re-asking. The answer is pinned
to the asking agent: the classification LLM (same single call) prefixes
its line with `[ANSWER]` when the message answers the question, and the
orchestrator then dispatches to the asking agent with
`context.pending_question` and `context.is_followup` set. Without the
marker the turn is classified normally; when it goes to another agent,
the stale follow-up context is dropped. Comma-joined multi-agent askers,
`send-agent` and pseudo agents are never pinned. On every response path the integration sends the HA chat
session id (`chat_log.conversation_id`; HA assigns a fresh ULID when the
caller sent none and replaces unknown non-ULID ids) to the container and
places the same id into the `ConversationResult`; the container's own
`conversation_id` is a container-internal correlation key only and is
never forwarded to HA (HA core regenerates unknown-but-valid ULIDs, which
would silently break session continuity).

### HA Bridge Transport

- One shared `/ws/conversation` socket. While idle, a background reader
  keeps it serviced: aiohttp answers the container's pings only inside
  `receive()`, and a container-side close detaches the socket and requests
  a reconnect.
- A turn takes the socket out of shared use before the request write. A
  failed or cancelled send closes the socket; after a clean done frame
  the socket is shared again.
- The container answers every received message with frames ending in one
  terminal (`done=True`) frame, including a terminal error frame
  (`error: "Internal error"`) when dispatch raises or the stream ends
  without a done frame. Frames after the done frame are never sent.
- A close before the turn's first frame counts as not delivered and the
  turn is retried over `POST /api/conversation`. A failure after the first
  frame is not retried (the action may have run); the user hears a
  dropped-connection message, or a timeout message when the configured
  response timeout expires.
- The per-IP WebSocket limit is checked before the handshake completes, so
  an over-limit connect fails and the bridge uses REST.
- The REST fallback uses the same response timeout (`ws_receive_timeout`,
  default 120 s). Requests over 500 characters are answered locally.
  Raw container error strings are never spoken; they are logged with the
  trace id.

### Mediation Streaming

When `orchestrator.mediation_streaming_enabled` is `true` (default) and
a personality is configured, single-agent streaming turns buffer the
agent tokens, then stream the mediation LLM output as token frames
(asides, `[FOLLOWUP]` and Markdown markers filtered incrementally;
a lone `*` between spaces is kept).
Fallback rule:

- Nothing emitted yet: the reply is sent as `mediated_speech` on the
  terminal frame. A stall (no first token within
  `mediation.stream_first_token_timeout_sec`, default 15 s, or a token gap
  above `mediation.stream_idle_timeout_sec`, default 10 s) closes the
  provider stream and uses the deterministic fallback, the agent speech plus any reminder,
  with no second mediation LLM call. A stream error or empty/all-aside
  output after cleanup runs the blocking mediation path.
- Text already emitted: the turn is committed to the stream. No
  fallback text is appended and `mediated_speech` is omitted; on a
  mid-stream failure the original agent speech is stored and the
  dispatch span records `mediation_truncated: true`.

The terminal frame carries `mediated_speech` only when nothing was
streamed. The dispatch span records `mediation_streamed` (true only
when mediated text went out as tokens), `mediation_first_token_ms`, and
`mediation_fallback` (`stall_timeout`, `stream_error` or `empty_output`)
when nothing was streamed. Markdown markers are removed before
`[FOLLOWUP]` detection, so a wrapped tag (`**[FOLLOWUP]**`) is still
recognised.

### Language Detection and Per-Agent Directive

The `language` setting (default `auto`) controls reply language.
When `auto`, the `language_detect` agent resolves the per-turn
language from the user input and the HA-provided `language` field,
and the orchestrator injects an explicit
"respond in <language>" directive into the system prompt of the
downstream domain agent. Forcing an ISO code (`de`, `en`, ...)
bypasses detection and pins all replies.

### Per-Turn Tracing on `/ws/conversation`

`TracingMiddleware` skips connection-level traces for paths under
`/ws/conversation` and instead leaves a `ws_per_turn=True` marker on
the ASGI scope. The route handler mints a fresh `trace_id`,
`SpanCollector`, and root span per inbound message, hands the
collector to the orchestrator dispatch, and flushes a synthesised
`ws_turn` root span at the end of each turn. This avoids the
legacy bug where every per-turn duration was overwritten
with the entire connection lifetime.

Every orchestrator turn records the request `user_id` (Home Assistant
user id, `null` when unknown or unmapped) as a request attribute that is
merged into the root span metadata (`SpanCollector.set_request_attribute`);
trace redaction keeps a `user_id` value verbatim only when it is a string of
at most 128 characters from `[A-Za-z0-9_.:@-]` that matches neither the
API-key nor the bearer-token pattern; any other value is redacted like other
metadata. The trace detail
API returns it as `user_id`.

### WebSocket / Dispatch Hardening (v1.42.0)

Several hardening measures guard the WebSocket and A2A dispatch paths:

- **WebSocket origin validation** -- The set `app.state.allowed_ws_origins`
  controls acceptable WebSocket origins. An empty set rejects all origins.
- **Per-IP connection limits** -- WebSocket connections are capped per client
  IP (5, rejected before the handshake completes), with hardened client-IP
  extraction that resists spoofed `X-Forwarded-For` values (`TRUSTED_PROXIES`
  accepts IPs and CIDR networks).
- **Per-agent dispatch timeout overrides** -- Settings shaped as
  `agent.dispatch_timeout.<agent_id>` override individual agent timeouts and are
  capped by the global `a2a.max_dispatch_timeout`.

### Recorder-History Tool

A recorder-history MCP tool exposes Home Assistant's long-term
history queries to agents that need them (mostly the general agent).
See `container/tests/test_recorder_history.py` for the tool's
contract.

### Device Executors

The light, climate, cover, media, music, scene, security, and vacuum executors
share one contract:

- **Domains** -- each executor's `_ALLOWED_DOMAINS` is the source of truth; the `@agent` metadata (keyword recall) and the admin match-preview map (`AGENT_ALLOWED_DOMAINS`) must equal it. Write actions resolve within a per-action domain set through `resolve_and_validate_entity` (candidate gate, visibility, deterministic-first).
- **Per-domain services** -- climate maps each logical action onto the resolved entity's own domain (`fan.set_percentage`/`set_preset_mode`, `humidifier.set_humidity`, ...) and drops payload keys the target service does not accept.
- **No-op skip** -- only a parameterless action on a single entity already in the target state is skipped; non-empty service data and group entities (an `entity_id` member list) always run. Tilt actions are never skipped on the cover's position state.
- **Capabilities** -- when HA reports `supported_features` / `supported_color_modes`, an action the device cannot perform (cover position or tilt, media volume set/step/mute, dimming or color on on/off lights, parameters on switches) returns an honest "does not support" answer without a service call.
- **Relative changes** -- `brightness_step_pct` (native HA), `temperature_delta` (resolved against the current target), `volume_up`/`volume_down` (native HA), and `volume_delta` (resolved against the current volume). Unknown light parameters are rejected, not dropped.
- **Results** -- every executed write returns `executed_command` (`domain`, `service`, `entity_id`, `service_data`). Failure speech is generic; exception text and URLs are only logged.
- **Conditions** -- only the light executor evaluates the `condition` field; the other agents' prompts decline conditional requests instead of dropping the condition.

### Cancel-Intent / Dismiss

The `cancel_speech` agent detects user requests to dismiss the
current or previous response ("never mind", "stop") and short-
circuits the dispatch so no downstream domain agent is invoked. See
`container/tests/test_cancel_interaction.py` for the interaction
matrix.

## Two-Tier Cache

The action cache was named "response cache" in earlier versions.
The legacy term still appears in the on-disk sqlite-vec collection name
for backward compatibility.

The cache system stores SHA-256 hash keys of incoming requests in
SQLite. The routing cache additionally has a semantic similarity tier
(sqlite-vec k-NN over stored query embeddings, consulted after an
exact-hash miss); the action cache is exact-hash only:

- **Routing Cache** -- Caches the mapping from user intent to target agent. A hit (exact SHA-256 hash match, or semantic match above `cache.routing.semantic_threshold` with fail-closed validation) skips LLM-based intent classification entirely. Max entries: 50,000 with LRU eviction. Entity resolution is NOT cached: the routed agent recalls its own entities via keyword matching (see Entity Matching).
  - **Hygiene**: turns that resolved no entity (failed action or a clarifying-question ending) are never stored, and a served entry is invalidated when the cached agent's turn fails, so a poisoned phrasing re-classifies via LLM on the next turn. A turn that ends in the routed agent's own clarifying question (`voice_followup`) is not a routing failure and keeps the served entry. A failed semantic hit never deletes the borrowed neighbour entry (it may route its own wording correctly); the neighbour is suppressed in memory for that exact query text instead. Entries below the current schema version are treated as a miss on read.
  - **Embedding model**: each routing entry records the `<provider>:<model>` that produced its vector. A semantic candidate whose vector came from a different model (even at the same dimension) is skipped and its vector dropped; the entry is re-embedded on its next exact hit.
- **Action Cache** -- Caches full agent responses including executor-confirmed HA actions.
  - **Hit** (exact hash match): Requires the owning agent to be registered and every referenced entity to exist in the entity index and be visible before replaying the stored HA command, then the rewrite agent rephrases the raw agent response in the turn language with the personality applied. If the rewrite fails or returns no text, the stored fallback response is returned; the `rewrite` span is marked failed (`success: false`, `fallback: cached_response`) and rewrite analytics count a failure. The stored fallback never carries per-turn additions: the orchestrator stores the mediated (localized) speech when the turn added no calendar reminder and no organic follow-up question, otherwise the base agent speech. The replayed entity is recorded as an anaphora hint, as on a live turn.
  - **Not stored**: context-dependent turns -- a follow-up answer to a clarifying question, or a turn whose executed entity was offered as an anaphora hint (`last_entities`) -- store neither an action nor a routing row. Background-sourced turns (`source="background"`, e.g. wake briefings) store no cache row, no conversation turn and no session memory; their traces are still written.
  - **Miss**: Continues to routing-cache lookup or the live pipeline; a provenance rejection always forces the full live path.
  - Max entries: 50,000 with LRU eviction.
  - No-op executions (entity already in the target state) are never stored: their response text is state-dependent and would be wrong on replay.
  - Executors mark state-dependent results `cacheable: false` and they are never stored: `toggle`, relative changes resolved against the current state (`temperature_delta`, `volume_delta`), coded security actions, unconfirmed scene activations, light results whose observed state contradicts the request, vacuum `locate`/`send_command`, and music `search`.

Action rows that depended on an ingress area or device record that provenance and replay only for the matching origin; rows learned without origin context replay only on turns without an area or device. A provenance rejection forces the full live path, including classification and entity resolution. Device-agent rows (light, climate, cover, media, music, scene, security, vacuum) retain the actual domain, service, entity, and validated service payload the executor issued (`executed_command`; security codes are never included); conditional, read-only, no-op, malformed, and legacy rows are not replayed. Legacy rows without current command or origin provenance are discarded individually and relearned from a live turn.

Routing entries are invalidated when the served agent turn fails. Action rows are invalidated when their stored command or provenance is malformed, and entries are also invalidated when relevant entity fields change (name, `area_id`, `device_id`, hidden, disabled, aliases, labels). Visibility is rechecked on every action-cache replay. Single-row invalidation is per key: it aborts only an in-flight store of that row. The cache validator works on a snapshot and writes back with compare-and-swap on `created_at`, so it never resurrects a deleted row or reverts a re-stored one; hit-count flushes merge only `hit_count`/`last_accessed`.

## Session Memory

Session memory gives the General Agent semantic recall of past conversations ("we talked about X yesterday").

- **Component** -- `MemoryService` (orchestrator-owned, no LLM call at query time) with `MemoryRepository` (relational metadata in the main DB) and a dedicated `SessionMemoryVectorStore` (sqlite-vec sidecar DB `session_memory.db`).
- **Write path** -- `ConversationManager.store_turn` embeds each stored turn (raw text from `conversations`) fire-and-forget; per-turn vectors go to the vec store, session rollup metadata to `memory_sessions`/`memory_turns` (deterministic digest summary, no LLM summarization).
- **Read path** -- on cache-missed requests the orchestrator runs the memory search as a parallel prelude task (overlapped with classification). Matches are grouped per session (the current session is excluded -- its turns are already in the live context -- and sessions whose best-matched text duplicates a higher-ranked session are dropped), filtered by `memory.similarity_threshold` and `memory.scope` (per-user or global; rows without a user form an anonymous bucket), and attached to `TaskContext.memory_context`.
- **Injection** -- the General Agent appends matches to its system prompt (after the byte-stable static head) as score-annotated context: recall questions about past conversations are answered from this content; otherwise it is background only, never a basis for actions. For the top cross-session match, up to `memory.max_continuation_turns` turn pairs of the old session are copied into the context; the old session is never modified or reactivated.
- **Wait modes** -- `blocking` (default, waits up to `memory.wait_timeout_ms`; `best_effort` rarely lands a match with cached routing or fast LLM providers) or `best_effort` (zero added latency).
- **Lifecycle** -- unlimited retention by design; on embedding-model change the vec table is reset and turns are lazily re-embedded by a startup backfill. Cache-hit (replayed) requests receive no memory lookup and pay no embed cost.

Settings keys: `memory.enabled`, `memory.scope`, `memory.wait_mode`, `memory.wait_timeout_ms`, `memory.similarity_threshold`, `memory.max_matches`, `memory.max_snippet_chars`, `memory.max_continuation_turns` (see [Configuration](configuration.md)).

## Entity Matching

Entity selection is agent-side, not orchestrator-side. The orchestrator only routes; it never resolves or forwards entity candidates.

- **Keyword recall** -- each actionable agent filters its visible entities by normalized token overlap against the task description (plus the last user turn for follow-ups), with compound containment (a German compound like "Innenhofüberdachung" hits the tokens of "Innenhof Überdachung"). Hits are scored per field class `(name, identity, area)` so name/alias evidence outranks area-token evidence; the name class gets a +1 exact-name bonus when every token of the friendly name (or an alias) appears verbatim in the query, so an explicitly named entity outranks partial name overlaps. A tied top-2 score tuple marks the recall ambiguous and annotates the candidate block to ask instead of guess. Small domains inject the whole visible list; larger domains inject the top 12.
- **Closed contract** -- the candidate block lists `entity_id -- friendly_name (state)`; the LLM must emit an `entity_id` verbatim from that list. The executor validates the picked id against the recalled set fail-closed (no matcher re-run); an id outside the set is rejected and the agent asks a clarifying question. When the LLM emits only a free-form entity name, the executor falls back to deterministic-first resolution.
- **Deterministic-first fallback** -- exact entity_id (within the executor's allowed domains), exact friendly_name (space-insensitive, so compounds match spaced names), exact alias (HA per-entity aliases plus user/DB aliases from the `aliases` table, restricted to the visible, allowed-domain snapshot), optional strip-device-noun and area stages (the area stage matches the area id slug and the area name), word-boundary containment, then the hybrid matcher: alias fast path, token-based candidate preselection, and span-scored string signals (Levenshtein, Jaro-Winkler, phonetic) with an area bonus and a coverage floor rule. Embedding-based entity recall was removed; embeddings remain in use for the routing cache semantic tier and session memory.
- **Shared folding** -- every stage folds text the same way (`app/entity/tokens.py` `fold_text`): lowercase, diacritics stripped, `ß` -> `ss`, and the German digraphs `ae`/`oe`/`ue` collapsed, so "Kueche", "Küche" and "Kuche" are equal.
- **Ambiguity is final** -- when any deterministic stage finds several equally good candidates, the resolver asks (`*_ambiguous` resolution path) and the hybrid matcher does not run. In the hybrid stage, candidates within 0.02 of the top score are a near-tie (`hybrid_matcher_ambiguous`) unless exactly one of them is in the speaker's area or of the caller's preferred domain. The area rerank (a speaker-area candidate within 0.05 of the top moves first) reads candidate areas from the entity index.

By default, a weighted matcher score above 0.60 returns a confident match. Below the configured threshold, resolution fails closed and the agent asks which device the user means. A user/DB alias hit on an indexed entity is floored like a verbatim name (0.65).

### Entity Index Sync

- WebSocket `state_changed` and registry events update the index incrementally; a full sync runs at startup, every `entity_sync.interval_minutes`, and after every WebSocket reconnect (events emitted while the socket was down are lost).
- A full sync replaces the index with an HA snapshot but keeps entities that were updated or removed incrementally after the snapshot was taken (mutation generation read before fetching states), so it never resurrects removed entities or reverts newer names.
- Area, alias, device-name and entity-area registry lookups are fetched via `/api/template` and cached for 5 minutes. A failed lookup is not cached; the last good lookup stays in use. Every registry event (entity, device, area) clears this cache before refreshing the affected entities.
- Until the entity-area lookup has succeeded once, area assignments are unknown: visibility fails closed for entities without an area under `area_exclude` rules.

### Home Assistant Client

- **WebSocket liveness** -- aiohttp's heartbeat (PING every 15 s, connection closed when no PONG arrives) detects dead connections; the receive loop has no idle timeout, so a quiet home with no events keeps its connection.
- **Service-call fallback** -- a REST service call is re-sent over the WebSocket only when the request provably never reached HA (connect error, connect or pool timeout). Any HTTP status (including 5xx), read timeout or body decode error propagates, because HA may already have executed the call.
- **Recorder history** -- history is fetched without attributes; the speech summary takes the unit from the entity's current state.

## Data Storage

- **SQLite** -- Primary store for all structured data: settings, agent configs, custom agents, aliases, MCP servers, secrets (Fernet-encrypted), admin accounts (bcrypt-hashed), setup state, conversations, analytics, and trace spans.
- **sqlite-vec** -- Vector store for entity index embeddings, the routing cache semantic tier, and session memory turn embeddings. Entity index and routing cache embeddings live in dedicated SQLite databases; session memory uses its own `session_memory.db`. Persisted to disk under `/data`.
- **Cache DB** -- Routing and action cache entries (documents, metadata, and the semantic embeddings of the routing tier) live in `cache.db` under `CHROMADB_PERSIST_DIR` (default `/data/chromadb/cache.db`), separate from the primary SQLite store.

## Plugin Architecture

Plugins extend the system without modifying core code:

- Plugins are Python files in `container/plugins/` discovered at startup.
- Each plugin subclasses `BasePlugin` and implements lifecycle hooks: `configure`, `startup`, `ready`, `shutdown`.
- The `PluginContext` provides a read-only agent catalog, A2A dispatcher access, MCP registry access, settings access, and restricted route helpers; the old direct registry and raw `app` escape hatches are removed.
- Plugins can inspect registered agents, dispatch work through the orchestrator, add routes, subscribe to events via the event bus, and read/write settings.
- Plugin failures are isolated -- one plugin crashing does not affect others.

See [Plugin Development Guide](plugin-development.md) for details.
