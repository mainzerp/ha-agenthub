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

## Testing

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
