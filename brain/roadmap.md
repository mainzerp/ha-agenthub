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

- Review hotfix: REST service-response envelope, WS lock deadlocks, hidden-entity leak, blocking index refresh · priority: P1 · area: core
  Outcome: calendar/todo reads work over REST, WS client never self-deadlocks, registry events keep hidden entities filtered, admin index refresh runs off the loop.
  Passed: regression tests + full container suite (3310 passed). Needs user verification on the live instance: ask a calendar question ("what's on my calendar today"), read a todo list, hide an entity in HA and confirm hidden entities stay unmatchable, click "Refresh entity index" and confirm voice stays responsive.
  Phase 2 also done (full suite 3330 passed): streaming usage trailer, aside/[FOLLOWUP] stream filter, A2A invalid-params, visibility cache race, logs `since` 400, WS origins refresh. Live check: a personality-mediated reply with a follow-up question must not speak "(...)" or "FOLLOWUP"; token usage appears in analytics for streamed turns.
  Open (Phase 3): api-reference/deployment doc drift; MCP add-server 201-on-failed-connect left as is.

## Done

## Idea Bank

- Distributed HTTP-based A2A transport · priority: P3 · area: a2a
- Supervisor add-on packaging with managed ingress · priority: P3 · area: packaging
- Plugin marketplace / discovery UI · priority: P3 · area: plugins
- Occupancy-aware routing for area-sensitive targeting · priority: P3 · area: routing
- AI-powered automation suggestions · priority: idea · area: agents
- Natural-language dashboard creation · priority: idea · area: dashboard
- Security-agent sentinel mode (deferred — needs trigger contract + UI) · priority: P3 · area: agents
