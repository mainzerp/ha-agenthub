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
