# Goals

The few outcomes that matter this stretch. Confirmed with the owner 2026-09-22.
Task status and next steps live in [roadmap.md](roadmap.md); the long-term idea
backlog lives in `TODO.md`.

## Current outcome

Daily-driver reliability of the live installation — fast, correct turns with
high cache hit rates and no regressions.

## Priorities

1. **P1 — HA service for automations (`ai_task`-style contract).** A service or
   clear contract so HA automations can call the container without manual HTTP
   construction (structured output / `generate_data` pattern).
2. **P1 — Tech-debt and stability.** Hardening work that protects daily-driver
   reliability; includes the tracked legacy `agent-assist` cleanup path.
3. **P2 — User and agent memory.** Persistent profiles, memory tool
   (save/retrieve/update), limits/eviction, optional dashboard UI.

## Deferred / not now

- P3 security-agent sentinel mode (needs a separate trigger contract and UI).
- Public-adoption push (add-on packaging, plugin marketplace) — future
  direction, gated on daily-driver reliability holding.
