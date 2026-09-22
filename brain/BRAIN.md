# HA-AgentHub — Brain

Front door to the project's shared context. Read this first, then follow links
only as far as the task needs.

**Mission (short):** HA-AgentHub is a multi-agent Home Assistant assistant — a
Docker container execution engine plus a thin HA bridge — built for fast,
accurate natural-language control. Today it is a personal production system;
public adoption via HACS is a future direction. Details: [Mission.md](Mission.md).

**Current stretch:** daily-driver reliability. Top goals: P1 automation service
(`ai_task`-style contract), tech-debt/stability, P2 user and agent memory.
Details: [Goals.md](Goals.md), live pipeline: [roadmap.md](roadmap.md).

## Reading order

1. [Mission.md](Mission.md) — why the project exists, what winning looks like.
2. [Goals.md](Goals.md) — the few outcomes that matter now.
3. [roadmap.md](roadmap.md) — live pipeline (Kanban board format).
4. [Decisions.md](Decisions.md) — decision rationale, so decisions stay made.
5. [How We Work.md](How%20We%20Work.md) — build rhythm, verification, commit rules.

## Key repo docs

- `docs/project/project-definition.md` — authoritative project description.
- `docs/project/prime-directives.md` — binding architecture rules (read before
  touching orchestrator, cache, entity resolution, agents, plugins).
- `docs/README.md` — doc map.
- `VERSION.md` — version carrier and changelog.
- `TODO.md` — long-term idea backlog (not the live pipeline).

## External knowledge

Durable lessons live in the Athenaeum library, topic `ha-agenthub` — recall via
`request_knowledge` at session start; `docs/project/lessons.md` is the offline
fallback with critical operational facts.
