# Architecture

## System Overview

HA-AgentHub is a two-component system for natural language smart home control:

1. **Docker Container** -- The AI backend running FastAPI with multi-agent orchestration, a two-tier cache, hybrid entity matching, MCP tool integration, and a plugin system.
2. **HA Custom Integration** -- A Home Assistant bridge (`custom_components/ha_agenthub/`) that forwards most turns to the container and streams responses back to Home Assistant's conversation system. Optionally it adds an STT wrapper entity that tees voice-turn audio to the container for speaker identification.

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
  - `reached`: the target, or an equivalent terminal state (`off`/`standby`/`docked`/`paused` for an expected `idle`, `standby` for `off`, `docked` for `returning`). Spoken as done.
  - `in_progress`: a transitional state (`opening`, `closing`, `locking`, `unlocking`, `arming`, `disarming`, `pending`, `buffering`, `starting`). `success=True`, spoken as in progress.
  - `mismatch`: a known fault state (`jammed`, `problem`, `error`, `fault`). Also any non-target state that differs from the pre-call `previous_state` (the light, climate, cover, security, media and vacuum executors pass the state they read for their no-op check, via `executor_state_check.verification_previous_state`; groups and `unknown`/`unavailable` pre-call states are not compared), e.g. an alarm that moves `disarmed` -> `triggered` after an arm call; or any non-target state with the opt-in `strict=True`. The result is `success=False` with a `StateVerificationError`, so executors report a failure; `failure_speech` names the observed state in plain words ("Sorry, lock failed: Front Door reports jammed.") and never includes exception text.
  - `unverified`: nothing observed, a non-target state that may still be the pre-call state of a device that reports late (Zigbee, cloud), or a plausible intermediate state on the way to the target even when it differs from `previous_state` (`_INTERMEDIATE_STATES_BY_TARGET`: e.g. a vacuum reporting `idle`/`returning` after start, `idle` after return to base, a player reporting `on`/`idle`/`standby` before `playing`, a cover reporting `stopped`, a panel reporting `disarmed` while switching arm modes, or any other HVAC mode during a mode change). `success=True`, but the agent speaks hedged wording ("I sent the command to X, but it has not confirmed the new state yet.") and marks the action `cacheable=False`. The agent collects the outcomes per executor call, so this works without executor changes; the hedge replaces the executor speech, and executors that hedge themselves use the same `unverified_speech`, so the user hears one hedge. Actions without an expected state (toggle, fan speed, volume) keep their intent wording.
- **Confirmation sentence:** an action block may carry an optional `confirmation` (one short sentence in the user's language describing the state after success; device prompts ask for it instead of prose after the JSON, and the non-English language directive names its language). The LLM writes it before execution, so `ActionableAgent._apply_confirmation` replaces the executor speech with it only when the result succeeded without `noop`/`error`/`directive`/`voice_followup`, the action is not a toggle, the last verification record for the result's entity is `reached`, and `_plausible_confirmation` passes: flattened to one line, 1-160 characters, no `?`, braces, brackets, backticks, `http` or newlines, no value of a sensitive parameter, and every number group (digits only, so `4.000` = `4000`, `50,5` = `50.5`) equal to an action parameter, the `entity` label or the device's friendly name. Otherwise the template or hedge stays; a bad or missing confirmation never rejects the block. `TaskResult.metadata["speech_source"]` (also on the streaming final chunk's `metadata`) is `confirmation`, `template`, `llm` (parse-miss prose, LLM not-found clarification) or `mixed` (multi-action turn with different sources; the dropped-actions sentence counts as `template`). The `ha_action` span records `speech_source`, the redacted `confirmation` (max 200 characters, excluded from `action_params`) and `confirmation_rejected_reason` (`not_success`, `toggle`, `no_record`, `not_reached`, `invalid_text`, `too_long`, `sensitive`, `number_mismatch`).
- **Action cap:** `parse_actions` keeps at most `_MAX_ACTIONS_PER_TURN` (8) distinct action blocks per agent turn (the light prompt asks the user to narrow down above that). Blocks beyond the cap are not executed; the merged speech says how many were skipped and `metadata.actions_dropped` records the count.
- **Invalid action objects:** when the agent LLM emits an action object that fails validation (e.g. `"entity": null`), the surrounding prose is never spoken (it may claim success). The agent returns a deterministic clarification with `voice_followup=True` and `metadata.parse_miss = "invalid_action"`.
- **Satellite area:** keyword recall ranks entities in the satellite's area (`TaskContext.area_id`) first among equal scores, keeps them recallable in large domains, and does not flag a tie as ambiguous when exactly one tied candidate is in that area. Every agent prompt receives the satellite area name as context for "here" / "this room".
- **Untrusted prompt data:** entity friendly names and states, last-entity names, the pending clarifying question and stored memory text are flattened, length-bounded and wrapped in `[UNTRUSTED_DATA_START]` / `[UNTRUSTED_DATA_END]` before they enter a system prompt.
- **Secret redaction:** alarm/lock codes, PINs, passwords and tokens in action parameters are redacted (`app/security/redaction.py`) before they reach trace spans, the stored raw LLM response and logs. The service call still receives them verbatim.
- **LLM timeouts:** domain agents call the LLM with `retry_on_timeout=False`, so a provider timeout returns `llm_error` at once instead of a retry that would outlive the dispatch budget after the HA action ran. The call gets a deadline from the remaining dispatch budget (`agent.dispatch_timeout.<agent_id>`, else the card `timeout_sec`, capped by `a2a.max_dispatch_timeout`, minus 0.5 s, at least 1 s; minus the time the task already spent), which also clamps the provider timeout; without a resolvable budget the call is unbounded. Transient provider errors are still retried once within that deadline (see [LLM provider errors](#dispatch-failures-and-timeouts)); they fail before any action is parsed or executed.

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
  entity (satellites, engine from the `tts.engine` setting). Text sent
  to `notify.*` -- by the send agent and by timer/alarm push
  notifications -- passes `app/util/ha_template.neutralize_ha_template`
  first, so `{{ }}` / `{% %}` / `{# #}` in user or LLM text is never
  rendered as a Home Assistant template.
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

- **LLM provider errors:** every LLM client path (`app/llm/client.py`:
  `complete`, `complete_stream`, `complete_with_tools`,
  `complete_with_tools_stream`) retries a transient provider error once
  after about 0.3-0.5 s, independent of `retry_on_timeout`.
  - Transient: litellm `RateLimitError`, `ServiceUnavailableError`,
    `InternalServerError`, `BadGatewayError`, the openai
    `RateLimitError`/`InternalServerError`, network failures (httpx
    network and protocol errors, aiohttp connection errors, openai
    `APIConnectionError`), and any other error with status 429, 500, 502,
    503, 504 or 529. litellm `APIConnectionError` (its catch-all for
    unmapped errors, status hardcoded to 500) is transient only when its
    `__cause__`/`__context__` chain holds such a network failure; its
    status alone does not count. Permanent errors (authentication,
    permission, bad request, not found, context window, content policy,
    budget) fail at once; timeouts keep the `retry_on_timeout` policy.
  - Budget: with a call deadline the retry needs at least 1 s left after
    the backoff, and the retried attempt's provider timeout is clamped to
    the remaining budget. Without a deadline only a fast failure (attempt
    under 1 s) is retried. A `Retry-After` hint up to 2 s raises the
    backoff to the hint; a longer hint means no retry.
  - Streamed calls (mediation included) retry only before the first
    content token.
  - Observability: each retry logs a WARNING `Transient LLM provider
    error for agent=... model=...: <Class> status=<code>; retry 1/1 in
    X.XXs`; a skipped retry logs `Transient LLM provider error
    <Class>(<status>) not retried: <reason>`. The `llm_provider_call` span
    records `retry_count` and `retried_error`; its latency, TTFT, TPS and
    the token-usage record describe the successful attempt only.
  - Limits are module constants (`_LLM_TRANSIENT_*`), not settings. When
    both attempts fail the agent returns its `llm_error` fallback.
- **Fallback:** a dispatch that times out, raises, or (streaming) ends
  with an error frame before any text is re-sent once to `general-agent`
  as a non-streaming task; if that fails too, the turn speaks a canned
  line.
- **Double-execution guard:** every HA write entry point of the HA client
  (`HARestClient.call_service` and its WebSocket fallback,
  `HAWebSocketClient.call_service`, `send_ws_command`, `fire_event`,
  automation config save/delete) flags a per-dispatch marker
  (`app/ha_client/action_marker.py`, a ContextVar) right before the
  request goes out, so every executor is covered. Read-only calls
  (`get*`, `search*`, `browse*`, `list*` services or commands) do
  not flag it. When the marker is set, a failed dispatch is NOT re-sent
  to the fallback agent; the turn answers that the command was sent but
  could not be confirmed.
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
- **Template speech:** a single-agent result whose
  `metadata["speech_source"]` is `template` or `mixed` (English executor
  template) is localized the same way in a non-English turn when no
  personality mediation runs (`confirmation`, `llm` or a missing key
  are spoken as-is). Streaming holds such a one-shot final chunk back
  from the token relay and sends the localized text as the terminal
  frame's `mediated_speech`; tokens already relayed are never replaced.
  The `return` span records `speech_source` and `speech_localized`.

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

### Speaker Identification (Voice Turns)

Optional; off by default (`voice.speaker_id.enabled`). Flow:

1. The Assist pipeline uses the bridge's **HA-AgentHub STT** entity
   (`custom_components/ha_agenthub/stt.py`, created when the integration
   option `stt_entity_id` is set). It forwards the audio stream to the
   selected inner STT entity and returns its transcript unchanged.
2. For 16-bit mono PCM turns with a chat session, `voice_tee.py` copies
   each chunk into a bounded buffer and a background task streams it to
   the container's `WS /ws/voice`, keyed by the HA chat-session id
   (`chat_session.current_session`), followed by `end` with the
   transcript length when the inner STT succeeded with a non-empty
   transcript, otherwise `abort`. Connect failures, overflow (the turn
   is aborted) and timeouts are swallowed: STT never waits for or
   depends on the container.
3. The container registers the `conversation_id` in an in-memory turn
   store (`app/voice/turn_store.py`: single-use entries, 120 s TTL, 256
   entries, newest session per id wins) at `start`, computes a CAM++
   speaker embedding with sherpa-onnx in a worker thread
   (`app/voice/speaker_engine.py`), and cosine-matches it against
   per-profile centroids (mean of L2-normalized samples of the current
   model). `identified` needs score >= `threshold` and a lead of
   `margin` over the second-best profile. A voice turn never waits for a
   model (re)load: a changed `num_threads` is applied by a background
   reload while the loaded extractor keeps serving (kept on failure).
4. The conversation turn for the same `conversation_id` (the bridge sends
   the same chat-session id) starts a speaker lookup in the orchestrator
   prelude (`PendingSpeakerLookup`), concurrently with memory search and
   classification. The lookup waits for the result in the background
   (hard cap 5 s); at dispatch the turn uses a finished lookup at once,
   otherwise blocks at most the rest of `voice.speaker_id.wait_ms`
   (counted from the lookup start) and then cancels it (`timeout`).
   Only `identified` attaches `TaskContext.speaker` (for every agent);
   only agents that opt in with `_receives_speaker_context = True`
   (`GeneralAgent`, `TimerAgent`) get the prompt line
   `Speaker: "<name>" (recognized by voice, unverified)` (name sanitized
   as untrusted text), or `User: "<name>" (the Home Assistant user making
   this request)` when the request's HA user resolved to a person (see
   Requesting person below). Device/domain agents (security included),
   lists, calendar and custom/plugin agents never see either line. Every consumed entry records a
   `speaker_id` trace span at dispatch (`status`, `profile_id`, `score`,
   `margin`, `duration_s`, `waited_ms` = time blocked at dispatch,
   `lookup_ms` = lookup start to result, `compute_ms`, `latency_ms`,
   `speaker_name` and the profile's linked `speaker_ha_user_id` when
   identified). An early prelude exit cancels the
   lookup and removes the entry. An action-cache replay discards the
   entry. A turn with an attached speaker is treated as
   context-dependent and writes no action-cache or routing-cache row, so
   a personalised reply is never replayed to another speaker.

Personalization identity (`app/agents/personalization.py`):
`personalization_user_id(context)` returns the request's `user_id`
(source `ha`), else the `ha_user_id` linked to an identified speaker
(source `voice`), else none. An identified speaker linked to a
different HA user than the request's `user_id` is ignored entirely
(`identified_speaker`): no name, sender, prompt line or mediation line,
so all personalization follows the request user. It selects whose data
shapes a reply:

- **Requesting person** -- on the cache-miss path the prelude resolves the
  request's `user_id` to the `person.*` entity whose `attributes.user_id`
  matches (`app/ha_client/persons.py`, `PersonDirectory`), concurrently
  with memory search and classification, and joins it with the speaker
  lookup before dispatch (bounded by 1.5 s; a slower fetch keeps filling
  the cache in the background). The map comes from one `get_states` call
  and is served for 300 s; after that the previous map still answers at
  once while one background refresh runs. A `state_changed` event that
  adds, removes, renames or relinks a person, and a WebSocket reconnect,
  invalidate it: the old map is never served again and the next lookup
  waits (bounded) for a fresh fetch. A failed fetch keeps serving a map
  that was never invalidated, otherwise returns no person, and is retried
  no sooner than 30 s later. Refresh tasks are cancelled at shutdown. The
  result is
  stored on `TaskContext.requester_person` / `requester_name` (copied to
  every dispatched context). No match (or no `friendly_name`) means no
  name. `requester_identity(context)` returns the person name (source
  `ha`), else the identified speaker's name (source `voice`), else none:
  the HA user wins over any voice match. A turn without `user_id` behaves
  exactly as before.

- **Session memory** -- the search and `index_turn` use it. When the
  request has no `user_id` and a speaker lookup is pending, the memory
  search starts once the lookup resolves
  (`PendingSpeakerLookup.wait_ha_user_id`, bounded by the same
  `wait_ms` budget that dispatch waits for anyway); otherwise it starts
  at once as before. `conversations.user_id` stays the request user
  (audit); `store_turn(memory_user_id=...)` carries the memory identity.
  One conversation id (a shared satellite) can hold turns of several
  people, so the continuation turns copied from the top match are
  filtered by `memory.scope` like the matches themselves.
- **Calendar** -- the calendar agent and reminder injection resolve the
  calendar user mapping from it (person entity with that `user_id`), so
  a recognized speaker gets their default calendars and reminders.
  Order for source `ha`: request user, spoken self-identification
  ("this is X"), default user. For source `voice` the spoken
  self-identification wins over the unverified voice match. Calendar
  visibility filtering is unchanged.
- **Send agent** -- the sanitized requesting-person name (person name,
  else speaker name) fills `{sender_name}` in
  `prompts/send.txt` (the message is written from the sender's
  perspective; TTS adds a short attribution) and the notify title
  becomes "Message from <name>" (localized; `HA-AgentHub` when unknown).
- **Mediation** -- single-agent mediation (blocking and streamed) and the
  multi-agent merge add the line `User (Home Assistant account):
  "<name>"` for a resolved person, else `Speaker (recognized by voice,
  unverified): "<name>"`, to the user content, so personality replies may
  address the user by name.
- **Caches** -- the action cache never stores mediated speech (which can
  address the user by name), only the original agent speech. Agents that
  receive the requester / speaker prompt line (`_receives_speaker_context`,
  looked up via the agent registry, so plugin and custom agents count too)
  never write an action-cache row, whether or not a name is known; their
  routing row holds no speech and is still written.

Trace: the root span carries `identity_source` (`ha` | `voice` | `none`,
refined after the speaker join) and, when the request's HA user resolved
to a person, `requester_person` (entity id) and `requester_name`. The
trace summary row stores, at finalize, `user_name` (HA person name,
else identified voice speaker), `user_name_source` (the name's own source,
`ha` | `voice`) and `identity_source` (personalization source); an HA user
without a person entity plus a voice match yields a voice name with
`identity_source` `ha`. On the cache-miss path the name is the one the
prelude resolved. An action-cache replay runs no requester or speaker
lookup: it takes the HA person name only from an already cached person map
(`PersonDirectory.lookup_cached`, never waits; a cold map starts a
background refresh and the row stays unnamed), and voice-only replays
never get a name. The `memory_retrieval` span records the identity source,
`search_user_id` and `scope` of its search plus a bounded `memories`
list (see Session Memory). `user_id`, `speaker_ha_user_id`,
`search_user_id`, `conversation_id` and `matched_conversation_id` are kept
verbatim by the trace sanitizer (HA user ids and `owui-<hex>` conversation
ids would otherwise be redacted as long hex tokens).

Action-cache replays are not personalized: the voice entry is discarded,
no speaker is attached, reminders resolve from the request user (else
the spoken self-identification or the default calendar user), and the
turn is indexed under the request user (anonymous without one).

Rules:

- The speaker and the requesting-person name are context only: they never
  set `user_id`, never change entity visibility and never authorize
  anything. The prompt line is
  opt-in per agent class so a voice match never reads like authorization
  to an action agent's LLM. The personalization identity is never used
  for visibility, authorization or audit records.
- While enrollment is armed (`app/voice/enrollment.py`, in memory, one
  profile at a time), qualifying turns are stored as samples instead of
  being identified -- only after the bridge confirms the turn with `end`
  (successful, non-empty transcript); `abort` or a disconnect discards it.
- The voice path never waits for a model load: while the engine is not
  `ready`, turns report `unavailable` and a background warm-up starts.
- `/ws/voice` writes no connection-level trace; no audio is stored, only
  embeddings of enrollment samples.

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

Final speech goes through `sanitize.strip_markdown` (the HA bridge's
`_strip_markdown` is a lock-step twin, enforced by the corpus in
`container/tests/data/sanitize_corpus.txt`). Numbered-list markers are
removed only inside a real list -- two or more consecutive lines numbered
`1.`, `2.`, ... in order -- so dates such as `3. Oktober 2026` keep their
number; the streaming filter never removes them.

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
  - **Hit** (exact hash match): Requires the owning agent to be registered and every referenced entity to exist in the entity index and be visible before replaying the stored HA command, then the rewrite agent rephrases the raw agent response in the turn language with the personality applied (the target language is repeated in the rewrite user message, and the `rewrite` span records it as `language`). If the rewrite is disabled, fails or returns no text, the stored original agent speech (`original_response_text`; legacy rows without it use `response_text`) is spoken; on failure the `rewrite` span is marked failed (`success: false`, `fallback: cached_response`) and rewrite analytics count a failure. The cache never stores mediated speech: new rows write the original agent speech into both `response_text` and `original_response_text`, so a replay never repeats a calendar reminder, follow-up question or the requesting person's name. Without a working rewrite, a replay in a non-English turn speaks the original agent speech, which may be an English executor template. The replayed entity is recorded as an anaphora hint, as on a live turn.
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
- **Write path** -- `ConversationManager.store_turn` embeds each stored turn (raw text from `conversations`) fire-and-forget under the turn's personalization identity (request user, else a voice-identified speaker's linked HA user; see [Speaker Identification](#speaker-identification-voice-turns)); per-turn vectors go to the vec store, session rollup metadata to `memory_sessions`/`memory_turns` (deterministic digest summary, no LLM summarization).
- **Read path** -- on cache-missed requests the orchestrator runs the memory search as a parallel prelude task (overlapped with classification). Matches are grouped per session (the current session is excluded -- its turns are already in the live context -- and sessions whose best-matched text duplicates a higher-ranked session are dropped), filtered by `memory.similarity_threshold` and `memory.scope` (per-user or global, keyed by the personalization identity; rows without a user form an anonymous bucket), and attached to `TaskContext.memory_context`.
- **Injection** -- the General Agent appends matches to its system prompt (after the byte-stable static head) as score-annotated context: recall questions about past conversations are answered from this content; otherwise it is background only, never a basis for actions. For the top cross-session match, up to `memory.max_continuation_turns` turn pairs of the old session are copied into the context; the old session is never modified or reactivated.
- **Tracing** -- the `memory_retrieval` span lists each match (`conversation_id`, `session_id`, `turn_id`, `similarity`, `last_turn_at`, a single-line snippet of at most 150 characters, `continuation_count`, `attached`; at most 10 entries, truncated in the producer and still trace-sanitized) together with `scope` and `search_user_id`; snippets are redacted on the full text before truncation; `attached` is true only when the matches were placed in the task context (false for timed-out or abandoned searches).
- **Wait modes** -- `blocking` (default, waits up to `memory.wait_timeout_ms`; `best_effort` rarely lands a match with cached routing or fast LLM providers) or `best_effort` (zero added latency).
- **Lifecycle** -- unlimited retention by design; on embedding-model change the vec table is reset and turns are lazily re-embedded by a startup backfill. Cache-hit (replayed) requests receive no memory lookup and pay no embed cost.

Settings keys: `memory.enabled`, `memory.scope`, `memory.wait_mode`, `memory.wait_timeout_ms`, `memory.similarity_threshold`, `memory.max_matches`, `memory.max_snippet_chars`, `memory.max_continuation_turns` (see [Configuration](configuration.md)).

## Entity Matching

Entity selection is agent-side, not orchestrator-side. The orchestrator only routes; it never resolves or forwards entity candidates.

- **Keyword recall** -- each actionable agent filters its visible entities by normalized token overlap against the task description (plus the last user turn for follow-ups), with compound containment (a German compound like "Innenhofüberdachung" hits the tokens of "Innenhof Überdachung"). Hits are scored per field class `(name, identity, area)` so name/alias evidence outranks area-token evidence; the name class gets a +1 exact-name bonus when every token of the friendly name (or an alias) appears verbatim in the query, so an explicitly named entity outranks partial name overlaps. A tied top-2 score tuple marks the recall ambiguous and annotates the candidate block to ask instead of guess. Small domains inject the whole visible list; larger domains inject the top 12.
- **Closed contract** -- the candidate block lists `entity_id -- friendly_name (state)`; the LLM must emit an `entity_id` verbatim from that list. The executor validates the picked id against the recalled set fail-closed (no matcher re-run); an id outside the set is rejected and the agent asks a clarifying question. When the LLM emits only a free-form entity name, the executor falls back to deterministic-first resolution.
- **Empty recall** -- each actionable agent declares per action whether it needs a recalled candidate: `@agent(entity_actions=..., entity_free_actions=...)`, taken from the executor's `ENTITY_ACTIONS` / `ENTITY_FREE_ACTIONS` tables (derived from its dispatch maps). The declaration decides what an empty recall injects:
  - Agents with both kinds get a scoped block. It forbids a JSON action only for the entity actions and names the entity-free ones as still allowed. These are the device agents (list actions, plus climate's `query_weather` / `query_weather_forecast`) and `automation-agent` (`create_automation`, `list_automations`).
  - Agents without entity actions get no block, so the prompt's JSON contract stays in force. These are `timer-agent` (AgentHub-internal timers and alarms) and `lists-agent` / `calendar-agent` (the executor resolves its own list or calendar).
  - Undeclared agents get the strict block: no JSON action, ask which device is meant. `entity_candidates_required=True/False` is the coarse shorthand for "every action" or "no action".

  The candidate gate is the same in every case: an empty recall publishes an empty set, so any LLM-supplied `entity_id` is rejected.
- **Empty `entity` in the parser** -- `parse_actions` accepts an action without `entity` / `entity_id` only for the entity-free actions (`_ACTIONS_WITHOUT_ENTITY` in `action_executor.py`). `tests/unit/test_entity_action_declarations.py` pins parity with the declarations, the executor dispatch and the prompt few-shot actions. Timer actions accept an empty entity as "no name": the executor uses the only running timer or alarm, or asks when several are running. Lists and calendar accept it as "no list/calendar named". Actions whose required target is missing (`delayed_action` without `target_entity`, `sleep_timer` without `media_player`) answer with a clarifying question (`voice_followup`). Timer, lists and calendar answer a parse miss with a clarification when the LLM asked a question or emitted an invalid action object. Only other prose becomes `PARSE_ERROR`.
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
- **Voice profiles** -- `voice_profiles` (name, optional `ha_user_id`) and `voice_samples` (float32 embedding BLOB, `dim`, `model`, `duration_s`) in the primary SQLite store (migration 49). Samples are tagged with the embedding model and only same-model samples form a centroid. The speaker model ships in the image at `/opt/models/speaker/`; fallback download to `<db dir>/models/speaker/`.
- **Cache DB** -- Routing and action cache entries (documents, metadata, and the semantic embeddings of the routing tier) live in `cache.db` under `CHROMADB_PERSIST_DIR` (default `/data/chromadb/cache.db`), separate from the primary SQLite store.

## Plugin Architecture

Plugins extend the system without modifying core code:

- Plugins are Python files in `container/plugins/` discovered at startup.
- Each plugin subclasses `BasePlugin` and implements lifecycle hooks: `configure`, `startup`, `ready`, `shutdown`.
- The `PluginContext` provides a read-only agent catalog, A2A dispatcher access, MCP registry access, settings access, and restricted route helpers; the old direct registry and raw `app` escape hatches are removed.
- Plugins can inspect registered agents, dispatch work through the orchestrator, add routes, subscribe to events via the event bus, and read/write settings.
- Plugin failures are isolated -- one plugin crashing does not affect others.

See [Plugin Development Guide](plugin-development.md) for details.
