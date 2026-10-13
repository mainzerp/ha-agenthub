# Roadmap

Live pipeline in board format — headings are columns, cards are
`- title · priority: … · area: …` lines. The long-term idea backlog stays in
`TODO.md`; cards appear here only when work is scheduled or started.

## Backlog


- HA service for automations (ai_task-style contract) · priority: P1 · area: integration
  Outcome: HA automations can call the container via a service/contract without
  manual HTTP. Next step: define the contract surface (service schema,
  structured output). Verify: service callable from a real HA automation.
- Tech-debt and stability hardening · priority: P1 · area: core
  Outcome: daily-driver reliability protected; known debt burned down.
  Includes the legacy `agent-assist` identifier removal tracked in `TODO.md`.
  Verify: full verification gate passes; no regression on the live instance.
- User and agent memory · priority: P2 · area: memory
  Outcome: persistent user profiles with a memory tool (save/retrieve/update),
  limits/eviction, optional dashboard UI. Next step: design the storage and
  retrieval contract (session-memory lessons in Athenaeum `ha-agenthub` topic).
  Verify: memory persists across conversations and is controllable per user.

## In Progress

- Voice 3.5.1: keep inner pauses, relax enrollment anchor · priority: P1 · area: voice
  Outcome: enrollment via headset works; same-speaker samples score
  consistently. Cause: 3.5.0 joined VAD spans and cut inner pauses (offline
  test: 14/72 errors vs 7/72 with outer trim + 0.2 s padding); the
  inconsistent-sample rule anchored on one unchecked first sample. Also
  bridge hardening (audio_processing fallback, ws compress=0). STT latency
  checked: no bridge overhead (cloud direct 2.70 s vs bridge 2.64 s for
  equal sentences; the stage includes listening). Branch fix/voice-vad-trim.
  Next step: implement + tests + offline re-check.

## Testing

- Filler spoken before the answer (HA 60-char TTS threshold) · priority: P1 · area: voice
  Outcome: the filler is spoken while the agent still works, not with the
  final answer. Cause: HA's assist pipeline starts streaming TTS only after
  >60 chars of assistant text; fillers are 14-55 chars. Fix: bridge pads
  the filler delta with trailing spaces (conversation.py
  `_TTS_STREAM_START_CHARS`); final speech unchanged. Bridge tests pass.
  Verify: voice turn on a slow agent (filler enabled) - filler audio starts
  before the answer; HA debug shows tts_start_streaming early.
- Voice speaker identification reliability (Patric/Laura confusion) · priority: P1 · area: voice
  Outcome: the two enrolled speakers are told apart or a turn reports
  unknown. Silero VAD (sherpa-onnx) trims enrollment and identification
  audio; minimums count speech seconds (1.0 / 1.5 s); enrollment guard
  rejects inconsistent, other-speaker and near-tie samples (counted only
  after end); per-sample list with leave-one-out score, pre-VAD flag and
  delete; all-profile scores, runner-up, speech_s and vad flag in
  diagnostics; default margin 0.12 (migration 51; live set to 0.15).
  Trigger: live analysis 2026-10-13 (other profile 0.57-0.75). PR #160;
  shipped in v3.5.0 (release PR #161,
  https://github.com/mainzerp/ha-agenthub/releases/tag/v3.5.0).
  Passed: ruff clean; container suite 4576 passed, 3 skipped; bridge 213
  passed; real sherpa VAD integration test 2 passed (synthetic signal);
  independent review findings fixed. Not verified: Docker build (runs in
  real voices (VAD checked in the image on German TTS: 9.68 s in, 4.77 s
  speech out), dashboard in a browser.
  Needs user verification after deploying: reset both profiles and enroll
  each person alone with varied sentences; the sample list shows high
  leave-one-out and low other-profile scores; recent results show both
  profile scores with a clear gap; confusions stop or become unknown.

- Trace: identity block, loaded memories, user in trace list · priority: P1 · area: trace
  Outcome: trace detail header shows the identified name with its own
  source (HA account / voice), person, HA user id, personalization source
  (whose memory/calendar), voice speaker with score/margin/status and
  linked HA user, also for voice-only turns; memory_retrieval lists each
  loaded memory (session link, similarity, date, redacted snippet,
  continuation count, attached), scope and search user; "Memory" toggle
  renamed "Conversation history"; trace list shows/searches the user name
  (trace_summary user_name, user_name_source, identity_source; migration
  50); CSV export adds User. HA cache hits get the name from a warm person
  map without waiting; voice-only cache hits stay unnamed. PR #159;
  shipped in v3.5.0 (release PR #161,
  https://github.com/mainzerp/ha-agenthub/releases/tag/v3.5.0).
  Passed: ruff check/format clean; container suite 4527 passed, 1 skipped;
  independent review findings (name vs source badge, cache-hit names,
  snippet redaction order, owui id redaction, attached flag) fixed.
  Not run: ci.py, bridge tests; pages not seen in a browser.
  Needs user verification: review the diff; deploy; a satellite turn and
  a Companion App turn each show the Identity group; a turn with memory
  hits lists them with working session links; trace list shows names.

- Text turns: resolve the HA user to a person name · priority: P1 · area: conversation
  Outcome: a typed Companion App turn ("Wer bin ich?") knows the person's
  name: request `user_id` -> `person.*` friendly name (cached, stale while
  revalidating, invalidated on person change/reconnect, 1.5 s join) feeds
  the requester prompt line (agents with `_receives_speaker_context`),
  mediation and send sender; HA user wins over a voice match. Cache:
  never stores mediated speech (failed/disabled rewrite replays the raw
  agent speech); agents with the prompt line write routing rows only.
  Trace: `requester_person`,
  `requester_name`. Trigger: live trace 6c9ce78dc644401a. PR #157,
  shipped in v3.4.0 (release PR #158,
  https://github.com/mainzerp/ha-agenthub/releases/tag/v3.4.0).
  Passed: ruff check/format clean; tests/test_requester_person.py 65
  passed; cache tests 337 passed; container suite 4494 passed, 1 skipped;
  independent review findings fixed, then cache rule simplified per user
  decision (see Decisions 2026-10-13). Not run: ci.py,
  bridge tests (bridge untouched); trace detail page not seen in a browser.
  Needs user verification: review the diff; deploy; Companion App text
  "Wer bin ich?" -> your name; trace root shows requester_person/_name;
  ask the same as another HA user -> their name, never the previous one;
  a cached light command still answers in German (rewrite) and the cache
  row shows the raw agent speech.

- LLM provider resilience: transient retry, span error detail, persisted logs · priority: P1 · area: llm
  Outcome: a transient provider error (Cerebras "high traffic", 429/5xx,
  connection reset) is retried once before any action runs; failed spans
  carry error type/message/status; WARNING+ logs survive a container
  restart. Trigger: live trace 4c94d170dd51471e (light-agent LLM error,
  evidence lost on restart). PR #156; shipped in v3.3.1
  (https://github.com/mainzerp/ha-agenthub/releases/tag/v3.3.1).
  Passed: ci.py full gate green (container 4430 passed, coverage 85.04%;
  bridge 213 passed; ruff, bandit, pip-audit, docker build + smoke, Trivy);
  independent review findings fixed (retry bounded by dispatch budget, no
  retry without deadline after a >1 s failure, litellm APIConnectionError
  only with network cause, Retry-After > 2 s means no retry, metrics per
  successful attempt, teardown always flushes the log file).
  Needs user verification: review the diff; deploy; after a restart the
  Logs page shows earlier WARNING/ERROR entries marked restored; on the next
  provider error the failed span shows error_type/error_message/error_status
  and a retry shows retry_count on llm_provider_call.

- Voice personalization (3.3.0) · priority: P1 · area: voice
  Outcome: when HA sends no `user_id` (satellites), an identified speaker's
  linked HA user personalizes memory (search, indexing, scoped continuation
  turns), calendar user and reminders; send agent writes as the sender
  ("Message from <name>"); mediation may address the speaker. `user_id`,
  audit, visibility and authorization unchanged; a conflicting HA user wins.
  Speaker prompt line only for general and timer agents (3.2.1). Branch
  feat/voice-personalization.
  Passed: container suite 4372 passed, bridge 213 passed, ruff clean;
  independent review findings (continuation-memory leak, calendar order,
  conflict rule, privacy docs) fixed.
  Needs user verification after deploying: on a satellite ask "Kannst du mir
  sagen, wer gerade mit dir spricht?" -> name; "Was steht heute in meinem
  Kalender?" -> your calendar; "Merk dir, dass ich ..." then recall on the
  same satellite; trace root span `identity_source=voice`; Voice Profiles
  recent results show compute/latency.

- Voice speaker identification (3.2.0) · priority: P1 · area: voice
  Outcome: bridge STT entity "HA-AgentHub STT" (`stt.py`, `voice_tee.py`)
  wraps a user-selected STT entity and tees 16 kHz PCM over `/ws/voice`;
  container (`container/app/voice/`, CAM++ via sherpa-onnx 1.13.8, migration
  49) matches enrolled profiles and attaches `TaskContext.speaker` (context
  only; speaker turns skip the action/routing cache). Dashboard page
  `/dashboard/voice-profiles`. PR #151; shipped in v3.2.0 (release PR #152,
  https://github.com/mainzerp/ha-agenthub/releases/tag/v3.2.0); timing,
  late-result fix and prompt allowlist in v3.2.1 (PR #154).
  Passed: ruff clean; container suite 4304 passed (after review fixes);
  PR and release pipeline (quality, security, docker, Trivy, release) green;
  bridge 213 passed; ci.py --no-trivy green incl. docker build + smoke and
  sherpa_onnx/model check in the image (run before the review fixes, which
  did not touch Dockerfile/requirements); independent review findings fixed.
  Needs user verification: review the diff; deploy container + HACS bridge;
  integration options -> select the STT entity to wrap; Assist pipeline ->
  speech-to-text "HA-AgentHub STT"; enable in Voice Profiles, enroll ~5
  samples per person per satellite; a voice turn >= 1.5 s shows the
  `speaker_id` span and the speaker in the trace; calibrate threshold with
  recent results. Open: CAM++ is English-VoxCeleb trained (German accuracy
  unmeasured); not yet seen in a browser.
  Later: own STT in the container (3.3.0), server-side wake word with custom
  satellite firmware (3.4.0).

- Agent confirmation sentence + template localization · priority: P1 · area: agents
  Outcome: device-agent action blocks carry an optional `confirmation`
  (user language, target state + values), spoken only when verification
  is `reached` (no toggle) and every number matches the parameters;
  otherwise the English template stays and is localized via
  `localize_message` when no personality mediation runs (streaming holds
  the final chunk). `speech_source` in agent metadata and spans. Prompts:
  light, climate, cover, vacuum, security, media, music, automation,
  scene; timer/lists/calendar unchanged. Branch fix/agent-speech-language,
  commit 3a5cb41, PR #148. Shipped in v3.1.0 (release PR #149,
  https://github.com/mainzerp/ha-agenthub/releases/tag/v3.1.0).
  Passed: ruff check/format clean; container suite 4170 passed, bridge
  163 passed; ci.py --no-trivy green except local docker (no daemon);
  release pipeline (quality, security, docker, Trivy, release) green.
  Needs user verification after deploying: "Licht auf 50%" -> German
  sentence naming 50 %, `ha_action` span `speech_source=confirmation`;
  "Licht umschalten" -> template path (`toggle`); with personality the
  tone is unchanged; optional: without personality a failure/no-op reply
  is German.

- Cache-hit rewrite answers in English despite `de` · priority: P1 · area: cache
  Outcome: the rewrite span records `language`; the rewrite user message
  ends with `Rephrase in <language>:` and rewrite.txt puts the language rule
  first; `/api/admin/rewrite/config` reads/writes the rewrite-agent's agent
  config; dead `rewrite.model`/`rewrite.temperature` dropped (migration 47).
  Found in live trace 8a2d684b41ed4ebc. Branch fix/agent-speech-language,
  commit 8587c05, PR #148; default temperature 0.5 (migration 48).
  Shipped in v3.1.0 (release PR #149, https://github.com/mainzerp/ha-agenthub/releases/tag/v3.1.0).
  Passed: ruff check/format clean; container suite 4079 passed, 1 skipped.
  Needs user verification: review the diff; confirm PATCH classification
  for the settings removal; after deploying, German cache hits answer in
  German and the rewrite span shows `language`.
  Follow-ups not started: output-language check after rewrite; replay from
  mediated `response_text`; parameters (brightness, color) in executor
  speech.

- Pipeline review 2026-10-10: fix all findings (#132) · priority: P1 · area: core
  Outcome: every finding of the end-to-end pipeline review fixed or skipped
  with reason. PRs #133-#141 and #143 merged; issue #132 closed. Shipped in
  v3.0.0 (PR #142, https://github.com/mainzerp/ha-agenthub/releases/tag/v3.0.0).
  Passed: container suite 3980 passed (1 skipped), bridge 163 passed, ruff
  clean; release pipeline (quality, security, docker, Trivy, release) green;
  local scripts/ci.py --no-trivy green before #143.
  Needs user verification on live after deploying 3.0.0: (1) only if HA
  helper alarms are used: label them `agenthub_alarm`, set a helper time ->
  it rings (otherwise set `alarm_monitor.label` empty); (2) timer
  ends -> announcement on the origin satellite; (3) "Weck mich morgen um 7"
  -> alarm listed for tomorrow 07:00; (4) "Licht auf 50%" on a lit lamp ->
  brightness changes; (5) two same-named lamps -> clarification question,
  answer "die in der Kueche" -> correct lamp; (6) voice automation create ->
  confirmation question (automation-agent needs device-domain include rules);
  (7) a German error path speaks German.
  Follow-up shipped in v3.0.1 (PRs #144, #145, release #146,
  https://github.com/mainzerp/ha-agenthub/releases/tag/v3.0.1): per-action
  `entity_actions`/`entity_free_actions` declarations; empty recall forbids
  only device actions; timer, lists and calendar accept an empty entity
  (calendar few-shots previously failed to parse); clarifying questions reach
  the user. Pre-existing since v1.51.0 (live trace ee04c7d717754dfe).
  Passed: container 4069, bridge 163, ci.py --no-trivy, release pipeline.
  Needs user verification on live: "Stell einen Timer auf 5 Minuten",
  "Timer abbrechen" (one running timer), "Was steht heute im Kalender",
  "Welche Lichter gibt es" -> all answer without "could not understand".

- Embedding engine init race + flaky scenario tests · priority: P1 · area: embedding
  Outcome: `get_embedding_engine` publishes the singleton only after a
  successful init; the local model loads off the event loop under a lock;
  scenario tests no longer load the real embedding model per xdist worker
  and stop timing out into the general-agent fallback. Branch
  fix/embedding-engine-init (no issue by decision), commit a093883, PR #131;
  not released yet.
  Passed: ruff clean; 6 new regression tests in tests/test_embedding.py
  (fail on old code); scenario file 3x `-n auto` green (309 passed each,
  ~40 s instead of ~2.5-3.5 min); container suite 3548 passed, 1 skipped.
  Needs user verification: review the diff; after deploying, the container
  log shows one "Loaded local embedding model" and voice stays responsive
  during startup/first memory turn.
- Sequential send: dictated messages and target parsing · priority: P1 · area: agents
  Outcome: "send the message X to device Y" delivers X verbatim; the
  send-agent finds the configured device inside any target phrasing
  (Unicode-aware; two separate device names = not found); content-agent
  refusals or the [[NO_CONTENT]] sentinel never reach the recipient; send
  errors are localized (en/de) and do not echo the parsed text. Found in
  live trace e625b7f88bff4e69. Shipped in v2.7.3 (PR #125,
  https://github.com/mainzerp/ha-agenthub/releases/tag/v2.7.3).
  Passed: ruff clean; container suite 3542 passed, 1 skipped; independent
  review (findings fixed); scripts/ci.py full gate green on the release
  state (bridge 145 passed, Trivy OK); live device names resolve as expected.
  Needs user verification on live after deploying: dictate "Sende die
  Nachricht: Ich komme später auf Lauras Handy" -> push arrives with exactly
  that text; trace shows dispatch_content returning the dictated text and an
  ha_call span; a request for an unknown device answers in German without
  echoing the phrase.
- Mediation stream fallback and trace clarity (2.7.2) · priority: P1 · area: conversation
  Outcome: streamed mediation never returns an empty reply (fallback by
  emitted text); stall timeouts as settings (15 s first token, 10 s idle,
  no second LLM call); streamed tokens markdown-cleaned for TTS
  (`**[FOLLOWUP]**` recognised); trace flags mediation_streamed /
  _first_token_ms / _fallback / _truncated, bool/number flags no longer
  redacted. Passed: ruff clean; container suite 3488 passed, 1 skipped;
  bridge 145 passed; independent review findings fixed. Shipped in v2.7.2.
  Needs user verification on live after updating: a personality turn shows
  mediation_streamed true and readable flags in the trace; a voice reply
  reads no markdown; the settings page lists the two new mediation
  timeouts.
- Open WebUI chat polish (2.7.1) · priority: P1 · area: conversation
  Outcome: source "openai" gets no filler (no wasted LLM call, no filler
  wait), no voice follow-up flag, traces record user_id ("User" on trace
  detail), leading whitespace trimmed. Found in live trace 80b7d88a95e14136.
  Passed: ruff clean; container suite 3463 passed, 1 skipped; bridge 145
  passed; independent diff review, findings fixed. Shipped in v2.7.1.
  Needs user verification on live after updating the container: a new Open
  WebUI turn shows no filler_generate span, voice_followup false, the
  "User" field with the mapped HA user id, and no blank lines before the
  answer. Mediation output already streams (live trace: mediation span
  streamed true, ~1260 tok/s), so personality replies are not buffered.
- Open WebUI connection: OpenAI-compatible endpoint + external user mapping · priority: P1 · area: conversation
  Outcome: AgentHub can be added in Open WebUI as an OpenAI API connection
  (`/v1`, container API key) and controls the house; Open WebUI users are
  mapped to HA users on the Persons page (table external_user_mappings,
  migration 45). Decision 2026-10-06. Shipped in v2.7.0 (PR #122,
  https://github.com/mainzerp/ha-agenthub/releases/tag/v2.7.0).
  Passed: ruff lint + format clean; container suite 3447 passed, 1 skipped;
  independent diff review, findings fixed. Not yet run against a real Open
  WebUI; Persons page not opened in a browser.
  Needs user verification on live (setup per docs/deployment.md "Open
  WebUI"): (1) model ha-agenthub appears in Open WebUI; (2) "Kueche Licht an"
  switches once -- trace shows one orchestrator turn, source "openai", no
  second turn from title generation (task model set in Open WebUI
  2026-10-06; chat should get a real title); (3) a follow-up in the same chat keeps
  context; (4) the Open WebUI user appears on the Persons page, map it,
  next trace carries the HA user id; (5) answer streams in the chat.

- Rewrite agent: surface failures, mediated fallback, reasoning effort none · priority: P1 · area: cache
  Outcome: a failing rewrite LLM call shows as failed in trace/analytics
  (no more fake success); fallback is the stored mediated text instead of
  the raw English agent template; rewrite-agent runs with
  reasoning_effort "none" by default (new dropdown value, migration 44).
  Cause found 2026-10-05: live rewrite-agent model groq/llama-3.1-8b-instant
  returns 404 model_not_found since ~2026-09-26 (trace 17804c2714e641ee).
  Passed: ruff lint + format clean; container suite 3393 passed, 1 skipped.
  Commits 3262043, aaa0fe0; shipped in v2.6.0. Needs user verification on
  live: (1) with the broken model,
  a cached command ("Kueche ausschalten" twice) shows a red rewrite span with
  success false and speaks the German mediated text; (2) after switching to
  a working model the rewrite span shows success true with new wording;
  (3) rewrite-agent shows Reasoning Effort "None" in the dashboard.
  Caveat: gpt-oss (Groq) and gpt-5 (OpenAI) likely reject "none" with 400 --
  use "low" for those models.
  Follow-up: rewrite-agent `enabled` flag is ignored by the cache manager.

- Review 2026-10-04 package 1: high-severity correctness fixes · priority: P1 · area: core
  Outcome: rescheduled timers stay cancellable; cache import writes every
  entry; HA WS reconnect opens one session and survives handshake errors;
  same-entity multi-action turns are never action-cached; AlarmMonitor
  fires in HA local time and never early. Commits e5e1986, 5f5e87d,
  925ecdf, 09d5fff, 01b124e on branch claude/lucid-maxwell-m0bpwu.
  Passed: new regression tests; container suite 3377 passed, bridge 145
  passed; ruff lint + format clean.
  Needs user verification on live: (1) set an alarm, change its time,
  then cancel it -- it must not ring; (2) an input_datetime alarm rings
  at local time, not 1-2 h off; (3) dashboard cache export, then import
  in replace mode -- entry count matches; (4) restart HA -- container log
  shows one "Connected to HA WebSocket" per reconnect; (5) "Thermostat
  auf Heizen und 22 Grad" twice -- second turn must run both actions.
  Follow-ups: packages 2 (visibility/security), 3 (robustness), 4 (tech
  debt/CI) from the same review.
  Shipped in v2.5.4 (https://github.com/mainzerp/ha-agenthub/releases/tag/v2.5.4).
- Multi-action commands: execute every LLM action block · priority: P1 · area: agents
  Outcome: "X und Y ausschalten" executes both actions in one turn.
  Implemented 2026-09-29: parse_actions() collects all fenced blocks
  (dedupe, cap 8); ActionableAgent runs them sequentially with per-action
  ha_action spans; ActionExecuted.entity_ids + TaskResult.actions_executed;
  multi-action turns store routing only (never action cache); domain
  prompts now sanction one block per action. Suite: 3353 passed.
  Needs user verification on live: redeploy container, then say
  "Ambiente Wohnen und Innenhofüberdachung einschalten" / "ausschalten" —
  both entities must switch in ONE turn (trace should show 2 ha_action
  spans). Regression trace: 1f01c7feff2e411e. Lesson: Athenaeum
  ha-agenthub live-debugging/multi-action-dropped-by-single-action-contract-trace-2026-09-28.
- Review hotfix: REST service-response envelope, WS lock deadlocks, hidden-entity leak, blocking index refresh · priority: P1 · area: core
  Outcome: calendar/todo reads work over REST, WS client never self-deadlocks, registry events keep hidden entities filtered, admin index refresh runs off the loop.
  Passed: regression tests + full container suite (3310 passed). Needs user verification on the live instance: ask a calendar question ("what's on my calendar today"), read a todo list, hide an entity in HA and confirm hidden entities stay unmatchable, click "Refresh entity index" and confirm voice stays responsive.
  Phase 2 also done (full suite 3330 passed): streaming usage trailer, aside/[FOLLOWUP] stream filter, A2A invalid-params, visibility cache race, logs `since` 400, WS origins refresh. Live check: a personality-mediated reply with a follow-up question must not speak "(...)" or "FOLLOWUP"; token usage appears in analytics for streamed turns.
  Shipped in v2.5.1 (https://github.com/mainzerp/ha-agenthub/releases/tag/v2.5.1). Phase 3 (docs drift) done. Left as is by decision: MCP add-server returns 201 on failed connect.

## Done

## Idea Bank

- Distributed HTTP-based A2A transport · priority: P3 · area: a2a
- Supervisor add-on packaging with managed ingress · priority: P3 · area: packaging
- Plugin marketplace / discovery UI · priority: P3 · area: plugins
- Occupancy-aware routing for area-sensitive targeting · priority: P3 · area: routing
- AI-powered automation suggestions · priority: idea · area: agents
- Natural-language dashboard creation · priority: idea · area: dashboard
- Security-agent sentinel mode (deferred — needs trigger contract + UI) · priority: P3 · area: agents
- Dashboard JS/CSS linting via standalone Biome pre-commit hook · priority: idea · area: dashboard
  Scope: `container/app/dashboard/static/` minus `vendor/` (3 JS files ~750
  lines, 24 CSS files ~2,800 lines); no package.json, no agent rule files.
  Inline `<script>` in Jinja2 templates (~3,250 lines) stays uncovered.
  Must keep CSS variable names stable (docs/style-guide.md). Ultracite
  rejected 2026-10-10: Node toolchain + writes TS/React rules into AGENTS.md.
