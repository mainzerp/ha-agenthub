# Decisions

Decision rationale, so decisions stay made. Newest first. Binding architecture
rules live in `docs/project/prime-directives.md` — this file records choices,
not copies of rules.

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
