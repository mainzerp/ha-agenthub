# Decisions

Decision rationale, so decisions stay made. Newest first. Binding architecture
rules live in `docs/project/prime-directives.md` — this file records choices,
not copies of rules.

## 2026-10-19 — CI scanning and Dependabot policy

- **Trivy also scans `main`**: `docker-build` uploads a SARIF for the
  `ha-agenthub:ci` image on main pushes under the `ci.yml:docker` category.
  Rationale: tag-only scanning (since 2026-05-08) left default-branch code
  scanning stale; the main scan is non-blocking, the release gate is unchanged.
- **Base image stays on Python 3.12**; Dependabot ignores python minor/major
  bumps. Rationale: litellm and sherpa-onnx do not support 3.15 yet, and
  `scripts/ci.py` pins the lock to 3.12. Revisit when the stack supports a
  newer Python.
- **codeql-action updates come as one Dependabot group.** Rationale: all
  codeql-action steps in a workflow must share one version.

## 2026-10-13 — LLM provider resilience after trace 4c94d170dd51471e

- **Transient provider errors are retried once in the LLM client**, detected
  by exception class and HTTP status (429/5xx/connection), never by message
  text. It is independent of `retry_on_timeout`, so domain agents retry too.
  Rationale: Cerebras "high traffic" failed one light-agent call in 175 ms
  while the next call 6 ms later succeeded. The LLM call runs before any
  action is parsed, so a retry cannot double-execute. Streams retry only
  before the first token. Domain agents bound the call (retry included) by
  the remaining dispatch budget so a retry never outlives the orchestrator
  fallback; without a deadline only fast failures (<1 s) are retried.
- **No settings knob for the retry** (module constants). Rationale: the client
  reads no settings today; one retry with ~0.3-0.5 s backoff is a safe default.
- **Orchestrator-level re-dispatch on `llm_error` not added.** The client retry
  covers the observed failure; revisit if errors outlast one backoff.
- **Failed spans store error type/message/status** (redacted, 300 chars), so a
  trace explains itself without logs.
- **WARNING+ logs persist to `$LOG_DIR/warnings.jsonl`** (rotating file, not a
  DB table) and are restored into the in-memory buffer at startup. Rationale:
  no migration, writes off the event loop via a queue thread, existing log
  API and skills work unchanged after a restart.

## 2026-10-10 — Pipeline review fixes: delivery and behavior choices (#132)

- **Delivery:** one public tracking issue (#132) with a checklist; one branch
  and PR per theme (T1-T8). Security-adjacent findings are worded neutrally in
  the public issue (repo is public; vulnerabilities belong in advisories).
- **AlarmMonitor is opt-in via an HA label** (default `agenthub_alarm`) plus
  timer-agent visibility. Rationale: any `input_datetime` timestamp helper
  fired as an alarm. Breaking: existing alarm helpers must be labelled.
- **Voice automation create/update/delete require confirmation** (summary +
  "save?" follow-up), update merges into the fetched config, referenced
  entities are validated against visibility. Enable/disable/trigger stay
  unconfirmed.
- **Overdue rows after restart:** delayed device actions >5 min overdue are
  dropped and logged; overdue timers/alarms are reported once as missed;
  processing runs in the background so startup never blocks.
- **Localization of canned error/fallback text goes through mediation** (LLM),
  not static tables; English stays the last-resort fallback.
- **timer-agent default visibility re-includes `input_datetime` and
  `assist_satellite`** (new migration + seed). Rationale: AlarmMonitor and
  announcement targets now check timer-agent visibility; migration 29 had
  removed `input_datetime` as dead, so default installs would lose alarms and
  satellite announcements. The label stays the only user action.
- **Release 3.0.0 (MAJOR)** for the review fixes: user action is required
  (alarm label opt-in), so SemVer per AGENTS.md mandates MAJOR.

## 2026-10-10 — No Ultracite; JS/CSS linting only as a standalone Biome idea

- **Ultracite rejected.** Rationale: it lints JS/TS/CSS only (no Python), its
  `init` adds the repo's first Node toolchain (`package.json`, lockfile, CI
  Node step) and writes a generic TS/React rules block into `AGENTS.md` plus
  `.claude/CLAUDE.md` and a hook, which conflicts with one-rule-one-owner and
  the Jinja2/HTMX stack. Reachable code would be ~750 lines of first-party JS
  and ~2,800 lines of CSS; the larger inline template JS stays uncovered.
- **Lighter option parked in the Idea Bank:** a standalone Biome binary as a
  pre-commit hook scoped to `container/app/dashboard/static/` minus `vendor/`.

## 2026-10-06 — Streamed mediation fallback is decided by emitted text

- **Fallback depends on text that reached the client**, not on raw tokens:
  nothing emitted -> the reply goes out whole as `mediated_speech`; something
  emitted -> the turn is committed to the stream, no fallback text is
  appended. Rationale: the hold-back filter made "tokens received" differ from
  "text shown", which produced empty answers.
- **A stall never triggers a second mediation call**; the sanitized agent
  speech plus reminder is sent instead. Rationale: a stalled provider would
  stall again. Timeouts are settings (15 s first token, 10 s idle) because
  reasoning models emit no content tokens while thinking.
- **Streamed tokens are markdown-cleaned for all sources.** Rationale: the HA
  bridge passes tokens straight to streaming TTS and trusts `sanitized=True`.
  Numbered-list markers are kept ("3. Oktober" is a date).

## 2026-10-06 — OpenAI-compatible ingress for Open WebUI, external user mapping

- **In-container adapter, not an Open WebUI Pipe function.** `GET /v1/models`
  and `POST /v1/chat/completions` live in the container behind the existing
  `container_api_key` and dispatch to `orchestrator` through the `Dispatcher`
  like `/api/conversation/stream`. Rationale: product surface instead of
  client-side glue; A2A boundary and visibility stay intact (Directives 1, 6).
- **Server-side history stays authoritative.** Only the last user message is
  used; Open WebUI's message list and system prompt are ignored. The
  conversation id is `owui-<X-OpenWebUI-Chat-Id>` (Open WebUI >= 0.6.17),
  falling back to a hash of user id + first user message.
- **Background tasks never reach the orchestrator.** Open WebUI sends title,
  tag and follow-up generation to the chat model unless a task model is set;
  their prompts contain the user's command and could execute it twice.
  Detection: custom connection header `X-OpenWebUI-Task: {{TASK}}` (reliable),
  plus the `### Task:` prompt prefix (heuristic). Tasks get a stub reply.
- **External users map to HA users on the Persons page.** New table
  `external_user_mappings` (source, external id, display name, email, mapped
  HA user id, last seen). Every seen Open WebUI user is recorded, so the admin
  maps them from a list instead of typing ids. Mapped requests carry the HA
  user id (calendar and memory consistent with voice); unmapped requests run
  with no user id, like dashboard chat. Trust level equals the HA bridge: the
  header is trusted because the caller holds the API key.
- **No enable toggle; 500-character text limit kept** (HTTP 400 when
  exceeded). Rationale: same key and limits as the bridge, no new attack
  surface or settings to maintain.

## 2026-10-04 — Few-shot examples in the configured language (Directive 13 amended)

Directive 13 now allows per-language few-shot assets with English as the
default and fallback. It records a choice already made in `f201778`
(2026-09-05), which split the orchestrator agent catalog into
`orchestrator_examples_<lang>.txt` (de, en, es, fr, it). Rationale:

- **English-only examples did not always route correctly.** Smaller local
  models misrouted non-English utterances; the 'Ambiente Wohnen'
  misrouting class is the evidence. Examples in the configured language
  passed the 17 of 17 routing battery against qwen-3.8-27b on the live
  deployment.
- **The guard rails stay.** Language variants live only in per-language
  asset files, and the English file is the default and fallback. No
  non-English phrases are hardcoded in Python or in shared templates, so
  Directive 11 (no language-specific phrase tables for routing) is still
  met.
- **Follow-up:** `classification_engine.py`
  `cancel_interaction_description_line()` hardcodes German examples
  (abbrechen/egal/schon gut) in Python. Move them into the per-language
  catalogs.

## 2026-09-29 — Multi-action execution contract

Domain-agent LLMs may emit one fenced JSON block per requested action
("A und B ausschalten"). `parse_actions()` collects all valid blocks
(dedupe, cap 8); `ActionableAgent` executes them sequentially in
utterance order, each with its own `ha_action` span. Rationale:

- **Sequential, not parallel:** execution order must match the user's
  utterance and keep trace spans ordered; per-action exceptions degrade
  to a per-action error result without aborting the rest.
- **First-action headline:** `TaskResult.action_executed` still reports
  the first action (public `ActionResult` contract unchanged);
  `TaskResult.actions_executed` + `ActionExecuted.entity_ids` carry the
  full set for consumers that care.
- **Multi-action turns are never action-cached** — a single
  `CachedAction` cannot replay N service calls. They store a routing
  entry only (all entity_ids forwarded, visibility rechecked per entity
  on hits — Directive 2 intact). Partial failures are not turn errors;
  only an all-failed turn attaches an `AgentError`.
- Evidence: trace `1f01c7feff2e411e` (2026-09-28) — the model emitted
  two correct blocks and only the first executed; Athenaeum lesson
  `live-debugging/multi-action-dropped-by-single-action-contract-trace-2026-09-28`.

## 2026-09-22 — Agent operating setup (initial brain creation)

Decisions taken while adapting `AGENTS.md` and creating `brain/`:

- **Version carrier:** `VERSION.md` is the single source of truth; the version
  is mirrored in `container/app/__init__.py` (`__version__`) and
  `custom_components/ha_agenthub/manifest.json`. All three are bumped together.
  Rationale: three consumers (changelog reader, container runtime, HACS) each
  need the version in their own format; history shows them kept in lockstep.
- **Release commit form:** `release: bump version to X.Y.Z` (canonical; owner
  confirmed). History contained three spellings; pick one and stay consistent.
  Tag `vX.Y.Z` triggers the release pipeline.
- **Verification gate:** `ruff check` + `ruff format --check` on `container/`
  and `custom_components/` must pass; `pytest` suites in `container/` and
  `custom_components/tests/` must pass (coverage gate 80). `mypy` is report-only,
  non-blocking — an observed standing convention, not a target to silently
  tighten. `python scripts/ci.py` runs the full local gate.
- **Doc map:** `docs/README.md` created as the doc index; ownership table lives
  in `AGENTS.md` (Docs Discipline). `docs/project/project-definition.md` remains
  the authoritative system description; `TODO.md` keeps the long-term idea
  backlog while `brain/roadmap.md` owns the live pipeline.
- **Tooling:** jcodemunch repo id `mainzerp/ha-agenthub` (indexed on this
  machine, source root `F:\Github\ha-agenthub`). Athenaeum topic for this
  project: `ha-agenthub`.
- **Conventions carried into AGENTS.md:** prime-directives are binding;
  `docs/style-guide.md` governs dashboard work; `container/data/` and
  `container/plugins/` are runtime/user dirs; never delete Docker volumes;
  `secrets/.env.local` holds live/local credentials for the admin-API skills.

## Open questions (asked at setup, not yet decided)

- Whether the mypy baseline is intentionally permanent or being burned down.

Resolved 2026-09-22: `SECURITY.md` supported-version table updated to 2.4.x;
`secrets/.env.local` exists locally and stays local (gitignored); all six
drifted skill docs plus `container/plugins/README.md` corrected after a full
audit (agent registration is declarative via `@agent`/`install_all_agents`,
prompts live in `container/app/prompts/`, `ctx.agent_registry` not
`agent_catalog`, `PUT /api/admin/settings/{key}`, `POST /api/admin/cache/flush`,
`/api/admin/mcp-servers`, logs `limit` cap 1000, match-preview returns
`deterministic`/`hybrid`/`visibility`/`diagnostics`).

## 2026-09-22 — noise route for false-trigger silence

- **`noise` pipeline directive added** (alongside `cancel-interaction`, never a
  real agent): the orchestrator may classify contextless fragments / background
  chatter (TV bleed-through) as `noise`. A sole-noise turn early-exits before
  dispatch with empty speech — no action, no TTS, no conversation turn — and
  restores any pending clarifying question the prelude consumed. Rationale:
  accidental wake-word activations from TV audio produced disruptive spoken
  replies; routing them to a silent route beats TTS-suppression heuristics
  because the classify LLM sees conversation context (a bare "Küche." stays a
  valid answer when a question is pending). Evaluated and rejected
  cactus-needle as an external noise gate first (67% false negatives on real
  German commands, confident false positives on noise; see library entry
  `ha-agenthub/cactus-needle-3.0.4-false-trigger-gate-lessons`).

## 2026-10-05 — rewrite failures are failures; fallback is mediated text

- **A failed cache-hit rewrite is recorded as failed** (rewrite span status
  error, `success: false`, analytics failure) instead of returning the input
  text as a fake success. Rationale: a provider 404 went unnoticed for over a
  week because traces and analytics showed 100% rewrite success.
- **Fallback speech is the stored mediated `response_text`**, not the raw
  agent text. Superseded on 2026-10-13 (see "cache never stores personalized
  speech").
- **rewrite-agent defaults to `reasoning_effort: "none"`** via agent config
  (seed + migration 44, only where unset), not hardcoded. Rationale: the 2 s
  timeout leaves no room for thinking; the admin keeps control per model,
  since some reasoning models (gpt-oss, gpt-5) reject "none".

## 2026-10-08 — sequential send: content contract and deterministic target lookup

- **The send-agent resolves the target deterministically against configured
  send devices:** exact name, then the existing verb regex, then a scan of
  configured `display_name`s contained in the target text (Unicode-aware,
  word boundaries). The longest name wins only when every other match lies
  inside it; two separate names or an equal-length tie = not found, because
  a private message to the wrong recipient is worse than "not found".
  Rationale for the scan: the few-shot examples teach target phrases
  like `Nachricht an Anna senden` that the verb regex never parsed, and
  scanning a short, known name list is language-neutral and stays a
  downstream executor safeguard (PD11), unlike more per-language regexes.
- **The content leg has an explicit contract:** in sequential-send mode the
  content agent is told its reply is the message body, that delivery happens
  elsewhere, that dictated text is returned verbatim, and that it answers
  with a sentinel token when it cannot produce content; the orchestrator
  treats the sentinel as `content_unavailable`. Rationale: a plain-text
  general-agent refusal ("I cannot send messages") passed as content and
  would have been pushed to the recipient. A sentinel is language-neutral;
  refusal-phrase detection would need per-language keyword tables.
- **Two-line rule kept** (content agent first, send-agent second) instead of
  a single send-agent line with inline content. Rationale: no classifier
  output-contract change; the verbatim instruction fixes dictation at the
  cost of one content LLM call.

## 2026-10-11 — rewrite language and rewrite config source

- **The rewrite user message repeats the target language** (`Rephrase in
  German (Deutsch):`) and the rewrite span records `language`. Rationale:
  live trace 8a2d684b41ed4ebc spoke the cached English template on a German
  cache hit; the language was only in the system prompt, and the trace could
  not show which language was requested.
- **The rewrite-agent's per-agent LLM config is the only rewrite config.**
  `GET/PUT /api/admin/rewrite/config` keep their shape but read/write that
  config; `rewrite.model` / `rewrite.temperature` are dropped (seed removed,
  migration 47 deletes rows). Proposed as PATCH (not yet confirmed by the
  user): no runtime reader ever existed and the API contract is unchanged. Rationale: two config sources
  where only one is used misled live debugging.
- **Executor speech stays a deterministic English template; language comes
  from mediation/rewrite.** Rationale: the agent LLM writes before the action
  runs, so only the executor knows the verified outcome. Open follow-ups:
  replay from the stored mediated `response_text` instead of the English
  original, and include requested parameters (brightness, color) in the
  template.

## 2026-10-11 — device-agent confirmation sentence

- **Action blocks carry an optional `confirmation`** (one sentence in the
  user's language, completed state + requested values). It replaces the
  executor template only when the action's verification record is `reached`,
  the action is not a toggle, and every number in the sentence (digits-only
  comparison, so "4.000" and "50,5" match) appears in the parameters, entity
  or friendly name; sensitive parameter values reject it. Rationale: the
  agent LLM writes before execution, so the sentence is only trusted once the
  executor confirms the target state; the gate keeps "never claim what was
  not observed".
- **Attributes are not re-verified.** "50 %" is checked against the sent
  parameter, not the observed brightness. Actions without an expected state
  (set_temperature, scene, volume) qualify when the service call succeeded and
  verification reached; toggle never does.
- **No prose after the JSON block**; the field replaces it.
- **Template speech is localized without personality** (`speech_source`
  template/mixed, non-English turn) via `localize_message`; with a personality
  mediation already translates. Rationale: German users without personality
  heard English templates.

## 2026-10-12 — voice: speaker identification via a bridge STT wrapper

- **The HA Assist pipeline stays; satellites keep their stock firmware.** The
  bridge adds an STT entity that forwards the audio stream to a user-selected
  STT entity and tees a copy to the container. Rationale: no firmware forks
  (Satellite1 and XVF3800 stock YAML hardcode on-device wake word), STT keeps
  working when the container is down, and only documented STT entity APIs are
  used. Rejected: satellites streaming directly to the container (Tater-style
  custom firmware) and a Wyoming server as the first step (lucia-style; HA
  Cloud STT is not reachable via Wyoming).
- **Correlation key is the pipeline's `conversation_id`**, read in the wrapper
  from `chat_session.current_session`; the conversation agent receives the
  same id. Fallback: no speaker. The context var is an HA helper internal, not
  a documented API.
- **A speaker is context, never authorization.** It does not override the HA
  `user_id` and does not change entity visibility. Rationale: voice matching
  is spoofable and short commands (under ~1.5 s) identify poorly; those turns
  report `unknown`/`too_short` instead of guessing.
- **Embeddings run in the container with sherpa-onnx (CAM++ VoxCeleb ONNX,
  Apache 2.0), downloaded at runtime; profiles stay in the local DB.**
  Rationale: no torch, CPU-only host, biometric data never leaves the house.
- **Sequence:** 3.2.0 speaker ID; 3.3.0 own German STT (Parakeet v3) behind the
  same wrapper; 3.4.0 server-side wake word (needs custom satellite YAML, no
  German sherpa-onnx KWS model exists; Vosk phrase list is the candidate).

## 2026-10-13 — voice personalization identity

- **Personalization identity = request `user_id`, else the identified
  speaker's linked HA user.** Used only for memory, calendar user/reminders,
  send sender name and mediation; never for visibility, authorization, audit
  (`conversations.user_id`) or `TaskContext.user_id`. Rationale: satellite
  turns carry no HA user, so personal features were unusable by voice.
- **Accepted risk:** a misidentified or replayed voice gets the linked
  person's memory and calendar read out. Mitigation: documented; leave a
  profile unlinked to opt out; spoken self-identification wins for calendars;
  a conflicting HA user overrides the voice.
- **Speaker prompt line only for general and timer agents** (user decision);
  security/device agents never see it so a voice match cannot read as
  authorization. Rationale for 3.2.1: wait for the speaker until dispatch, not
  a fixed budget, after live trace 3e6238c0a4804f8d lost a late result.
- **The requesting person's name comes from the request `user_id` first**
  (`person.*` whose `user_id` attribute matches -> friendly name), else from
  the identified speaker. It feeds the same prompt line (same allowlist),
  mediation and send sender. Rationale: typed Companion App turns carry an
  authenticated HA user but were anonymous by name (live trace
  6c9ce78dc644401a, "Wer bin ich?").

## 2026-10-13 — cache never stores personalized speech

- **The action cache stores only the raw agent speech; a failed or disabled
  rewrite replays that original.** The mediated fallback is dropped
  (`response_text` keeps its field for export compatibility but carries the
  agent speech). Rationale: mediation may name the requesting person, and a
  replay must never carry one user's name to another; agents now receive a
  language hint and write confirmation sentences in the user's language, so
  the English-template concern behind the 2026-10-05 fallback is mostly gone.
- **Agents with `_receives_speaker_context` (today general and timer) write
  no action row, only the routing row.** Decided by the agent flag, not a
  name list, so plugin agents follow. Rationale: their own speech may name
  the person; general writes no action rows anyway.
- Rejected: name-in-text checks (miss first-name-only mentions) and
  requester-dependent cache rules (complexity for an edge case).

## 2026-10-13 — voice: speaker ID reliability before model changes

- **Fix input quality and enrollment before touching the model.** Silero VAD
  (sherpa-onnx) trims only the outer silence (first to last speech, 0.2 s
  padding; inner pauses stay, corrected in 3.5.1 after joining spans
  destabilized same-speaker embeddings); the duration minimums count
  speech seconds. Rationale: live turns scored the
  other profile 0.57-0.75; silence and room noise are shared by both
  speakers and pull embeddings together.
- **Enrollment rejects samples that do not fit:** closer to another profile
  from the first sample on; below `enroll_min_similarity` against the own
  centroid only from the third sample on (3.5.1: a single unchecked first
  sample must not anchor the session); single samples can be deleted. Rationale: enrollment takes any
  voice on any satellite, and one wrong sample shifts the centroid.
- **Default margin 0.12** (live set to 0.15 on 2026-10-13); a migration moves
  only values still at the old defaults. Rationale: a wrong answer is worse
  than `unknown`.
- Deferred: s-norm/cohort scoring, per-satellite samples, model switch
  (3D-Speaker zh-en, ERes2NetV2, WeSpeaker ResNet34) until re-enrolled data
  with the new diagnostics shows whether they are needed.

## 2026-10-15 — general agent answers questions about the system itself

- **Local in-process tool `agenthub_self_info`, general-agent only.** Live
  overview (identity, version, registered agents without internal-only ones,
  loaded plugins, MCP server names) plus section search over bundled user
  docs. Rationale: the system could not say what it is or what it can do;
  runtime registries already hold the truth, and a local tool needs no MCP
  subprocess. Custom agents do not get it (prime directive 8).
- **Docs are bundled, not rewritten.** `scripts/sync_self_docs.py` copies
  `docs/user-guide.md`, `docs/configuration.md`, `docs/troubleshooting.md`
  and `README.md` into `container/app/knowledge/` (the image build context is
  `./container`); `ci.py` fails on stale copies. Rationale (user decision):
  no second hand-maintained text that drifts.
- **Routing stays with the LLM classifier:** general-agent card gains a
  self-description skill and few-shot examples in every language file; no
  keyword routing (prime directive 11).

## 2026-10-15 — filler starts speculatively, parallel to classification

- **Generate early, decide late.** The streaming path starts the filler LLM
  call right after the action-cache check, in parallel with classification;
  the send gate (target expected_latency "high", threshold from request start)
  is unchanged and runs after routing. Rationale: the filler is only useful
  when it lands early; at dispatch time it added its own generation latency
  (~280 ms in trace 5a90a16acd2b40b2) to the first frame.
- **Cost accepted, switchable.** Most turns route to low-latency domain agents,
  so the speculative call is usually cancelled; only installs with
  filler.enabled pay for it, and filler.speculative_start=false restores the
  dispatch-time start.
- **Generic prompt input.** The target agent is unknown at start; the filler
  prompt's unknown-agent fallback applies, the user text still carries the
  content.

## 2026-10-16 — quick hardening batch from the project audit

- **Fetch-metadata guard instead of a CSRF header token.** Cookie-authenticated
  mutating requests must be same-origin (`Sec-Fetch-Site` same-origin/none,
  else Origin vs. Host); bearer clients and explicit `CORS_ORIGINS` entries
  pass. Rationale: HA on the same host is same-site, so SameSite=lax does not
  protect the admin JSON API; a header token would have touched every
  dashboard fetch call and the admin-API skills, while the browser already
  sends fetch metadata.
- **Frame embedding stays possible.** Only CSP `frame-ancestors` is set
  (default `'self'`, `FRAME_ANCESTORS` to widen it for an HA iframe); no full
  CSP because templates use inline scripts.
- **API docs off by default** (`ENABLE_API_DOCS`): nothing in the repo uses
  them, and they map the admin surface for anyone on the LAN.
- **pip-audit fails the gate** with no ignore list; new advisories must be
  fixed or explicitly ignored with a reason.

## 2026-10-16 — AI Task entity (ai_task generate_data)

- **Owner decisions:** task requests go through the orchestrator like a normal
  turn; tasks may switch devices; image attachments are in scope now.
- **Native HA contract, not a custom service.** The bridge adds an
  `AITaskEntity` (GENERATE_DATA + SUPPORT_ATTACHMENTS) so automations,
  blueprints and HA's preferred-entity features target it. Bridge minimum HA
  version 2025.8.0 (structure + attachments landed there; HA core research
  against wheels 2025.6-2026.2.3).
- **Separate container route `POST /api/ai_task`** with request source
  `ai_task`: images exceed the WS/REST conversation size limits, and the source
  switch reuses the existing exemptions. Task turns get no filler, voice
  follow-up, mediation/organic closing question or pending question, never
  read or write the action/routing cache, and are not stored as conversation
  turns or memory (automation traffic would pollute personal memory).
- **Vision pre-step, not image routing.** A `vision-agent` model describes
  attachments into text (wrapped as untrusted data) before classification, so
  any agent can act on it and image bytes never enter A2A, the classifier,
  traces or memory. Direct image passing to the general agent can follow later.
- **Formatter step for structured output (AI Task).** A `task-formatter` model turns the
  unmediated agent result into JSON (json_object mode where supported),
  validated with `jsonschema` and repaired once; it must emit null for unknown
  fields. HA core does not validate the result, so validation lives here.

## 2026-10-16 — code-enforced approval for sensitive actions

- **Owner decisions:** approval required for `lock.unlock`/`lock.open`,
  `alarm_control_panel` disarm AND arm (home/away/night/vacation/custom
  bypass), and cover open/position/toggle on device_class garage, gate or
  door. A spoken or typed "yes" is enough (LLM yes/no/unclear, 60 s TTL); no
  speaker-ID factor (speaker stays prompt context only). Non-interactive
  sources (`ai_task`, `background`, OpenAI endpoint without a stable chat id)
  are blocked unless the entity is on an explicit allowlist setting.
  Agent-created automations may only close garage/gate/door covers.
- **Called "approval"**, because `confirmation` already names the spoken
  success sentence in agent JSON.
- **Two layers.** Interactive pause where the entity is resolved
  (`call_service_with_verification`), plus a fail-closed backstop in
  `HARestClient.call_service` that refuses any sensitive call without an
  approval token, so cache replay, multi-action, background, plugins and
  future paths cannot bypass it. Sensitive results are never action-cached.
- **Answer judged on the verbatim user text in the orchestrator prelude**,
  not on the classifier's condensed task (a bare "yes" gets paraphrased).
- **2026-10-16 follow-up:** delayed alarm arming (source `background` from the
  delayed-action path) runs without approval; delayed disarm, unlock and
  access-cover opening stay refused unless allowlisted. Interactive arming
  still asks; ai_task arming still needs the allowlist.

## 2026-10-17 — provider key test lists models instead of a completion

- **Key test = model-list call**, not a completion against a hardcoded model.
  Hardcoded test models rot when providers retire them (Anthropic
  `claude-3-haiku-20240307` 404 on the live instance); a list call costs no
  tokens and doubles as model discovery. OpenRouter's list is public, so its
  key is checked separately via `/api/v1/key`. Custom OpenAI endpoints without
  `/models` fall back to a completion with the user-entered model.
- **Raw HTTP through the existing async client** for all providers, no
  provider SDKs: one uniform code path, no new dependency or lock change.
- **Provider failures never surface as HTTP 500**; the test and the models
  endpoint answer 200 with `status: error` and an `error_kind`.
- **Pickers stay free text** with live suggestions and a static fallback, so
  an unreachable provider never blocks configuration; a configured model
  missing from a successfully loaded list is flagged, not auto-changed.

## 2026-10-17 — action cache is global; origin binds by area only

- **Rows are global** (user decision): an explicit command learned on one
  satellite replays on every satellite. Device binding is gone; it made the
  same command miss once per satellite.
- **Area binding only when the origin picked the entity**: a row is bound to
  the turn's `area_id` when the executed entity lies in that area (room-relative
  phrases like "Licht an"); otherwise global. The `used_origin_context` flag
  only meant "turn had an origin", and the area line reaches every agent
  prompt, so entity-in-turn-area is the observable proxy. Unknown entity ->
  area-bound (conservative).
- **One row per normalized text + language, last live result wins**. Area is
  not part of the key, because validator, export/import and row updates derive
  ids from text + language. Accepted cost: room-relative phrases move to the
  latest room.
- **Keys ignore inner punctuation** (STT varies commas/dashes); numbers keep
  their separators. Schema 6 purges all older (device-bound) rows on deploy.
- **Invalidate only on replay-relevant registry changes** and log every row
  deletion (bulk as WARNING) — light rows vanished silently because any area
  update deleted every row in that area and INFO logs do not survive restarts.
- **Validator keeps the row language**; unknown language keeps the original
  reply instead of falling back to an English template.

## 2026-10-18 — live run review fixes

- **Satellite area resolved in the container**, not the bridge (directive 1):
  an in-memory device directory (`app/ha_client/devices.py`) refreshed with the
  existing registry sync fills `area_id`/`device_name` from `device_id` at the
  start of the orchestrator prelude; an explicit `area_id` wins. Works without
  a bridge update. Consequence: room-relative cache rows now actually bind to
  the satellite's area (see 2026-10-17).
- **Hidden list before first snapshot**: the startup sync waits up to 30 s for
  HA's hidden/disabled list before installing the entity index; after that it
  installs unfiltered with a warning rather than staying empty forever.
- **Explicitly named referents are cacheable**: a turn counts as
  context-dependent unless the cache-key text fully names the referent
  (friendly name, alias or device name, compounds allowed).
  `resolution_path` cannot separate an echoed referent from a named one.
- **LiteLLM uses the httpx transport** (`disable_aiohttp_transport`): its
  aiohttp transport leaks sessions of evicted OpenAI-SDK clients (weakly held
  by the closer), which caused the periodic "Unclosed client session" errors.
- **Cache hit rate is per request** (hits / request events); per-event rates
  counted up to three misses per turn.
- Not fixed on purpose: voice samples (deleted by the user), classification and
  rewrite latency (user observes), false wakes (new wake word).

## 2026-10-18 — enrollment turns get a fixed reply, no agents

- **Marked at `/ws/voice` start, per conversation**, not by the global armed
  flag: only tee'd satellite turns become enrollment turns (typed/API/ai_task
  turns stay normal), and the final sample's commit cannot race the check.
- **Short-circuit at the start of the orchestrator prelude**, before origin,
  approval, cache, filler, speaker lookup, memory and classification; the
  request's spans are discarded (`SpanCollector.discard`), so no trace row.
- **Reply tells the outcome**: "OK" for an accepted sample or a result timeout
  (3 s), "Nicht gespeichert"/"Not saved" for any unstored sample, so the user
  knows which sentence to repeat. Static table, no LLM.
- Reject log lines carry the guard scores; the in-memory `recent` list is lost
  on restart.
- Rejects of 2026-10-18 (4 x `rejected_inconsistent`, 0.06-0.22) were not a
  capture bug (per-turn buffers, embedding before filler); cause unknown,
  satellite side suspected. Threshold left at 0.45 pending a re-test.

## 2026-10-21 — Activity Orb: Canvas2D custom element on the SSE broker

- **Canvas2D pseudo-3D, no library**: about 10 nodes need only projected
  points, radial glow and curved links; adds no vendored weight, fits the
  no-build dashboard and phones. three.js stays an option later because the
  event contract and widget states do not depend on the renderer.
- **Custom element with Shadow DOM** (`<agenthub-activity-orb>`) loaded globally
  from `dashboard_base.html`: drop-in on any page, no Alpine/HTMX dependency.
- **Transport is SSE, not WebSocket**: the dashboard session cookie already
  authenticates `EventSource`; a dashboard WebSocket would need a new
  cookie-auth dependency. Reuses the existing in-memory broker with a new
  `activity` topic; payload is ids, names, timing and status only, never
  span metadata or user text.
- **Seven satellite slots** (changed from five on 2026-10-21), seeded with the
  most recently used agents; the longest-unused idle slot is rotated out when
  another agent is dispatched.

## 2026-10-21 — Home Assistant card via the bridge

- **Relay through the bridge, not direct from the browser**: HA pages carry no
  dashboard session cookie and the `?token=` fallback is gone (SEC-2). The
  bridge keeps one upstream SSE connection (API key, server side) and fans out
  over a HA websocket command; HA's own login covers the card.
- **Any authenticated HA user may subscribe** (not admin-only): the payload is
  content-free ids, timing and status.
- **One generated JS file** (`scripts/sync_orb_card.py`: widget plus card
  wrapper) ships inside `custom_components/ha_agenthub/frontend/` because HACS
  installs only that directory; `ci.py` fails on a stale copy, like the
  self-docs copies. The integration serves and auto-registers it.
- **Recency source**: an in-memory tracker of dispatch span starts, topped up
  from persisted spans after a restart, behind `/recent` for both the
  dashboard and the bridge.

## 2026-10-22 — Lovelace card load order and delivery

- **Root cause of the 3.12.0 Firefox failure**: HA replaces `window.customElements`
  with the scoped-custom-element-registry polyfill during boot; a card defined
  earlier (module loaded through `add_extra_js_url`) is invisible to Lovelace.
  Chrome only wins the race on a warm cache. The generated card file therefore
  defers its definitions until the frontend has booted (gate in
  `scripts/sync_orb_card.py`), with a 30 s fallback and re-registration if the
  registry is replaced later.
- **The boot recovery guard is not involved**: it ignores `extra_module_url`
  scripts and only reacts to hashed frontend entry failures.
- **Card errors never escape**: lifecycle and callback errors are contained and
  recorded in `window.__AGENTHUB_ORB_DIAG__`; `setConfig` still throws for
  invalid config (HA's contract). A `debug` card option shows them in the card.
- **Delivery**: content-hash `?v=` and `Cache-Control: no-cache` with ETag
  revalidation (HA's stock static path sends 31 days, and its service worker
  caches `/ha_agenthub_static/*` stale-while-revalidate for 24 h).

## 2026-10-26 — pip-audit: filelock advisories ignored until litellm allows 4.x

- **CVE-2026-112940, CVE-2026-112913, CVE-2026-112907 (filelock 3.32.7) are
  ignored with reasons in `PIP_AUDIT_IGNORED` (`scripts/ci.py`)** (user
  decision). The fix is only in filelock 4.2.1 and every litellm release
  requires `filelock<4.0`. Not exposed: two affect only `ReadWriteLock`,
  which no installed dependency uses; one affects only Windows soft locks.
  The Trivy gate ignores the same set via `.trivyignore` (ci.py, local-ci.ps1,
  workflow gate steps). Drop the entries once litellm lifts the cap. Rejected: overriding the litellm
  cap (unsupported combination in the image) and a red gate until upstream moves.
