# Mission

## Why HA-AgentHub exists

HA-AgentHub is a multi-agent Home Assistant assistant: it accepts natural-language
turns from Home Assistant, routes them through an internal A2A orchestration
layer, resolves entities deterministically first, executes HA actions from the
container, and streams speech back through the custom integration.

The project is a **personal production system today** — it runs the owner's
smart home and must be trustworthy as a daily driver. It is engineered to
public-product standards (HACS-installable, documented, tested), and **public
adoption is a future direction**, not the current stretch.

Confirmed with the owner on 2026-09-22. The authoritative system description is
`docs/project/project-definition.md`; this note owns the *why*.

## What winning looks like (this stretch)

**Daily-driver reliability.** Voice/NL control that is trusted every day:
fast responses, high cache hit rates, correct entity resolution, few failures.
Feature work (P1/P2) serves this — new capability only counts if reliability
does not regress.

## Naming

- Product: `HA-AgentHub`
- Repo / container image / package slug: `ha-agenthub`
- HA integration domain: `ha_agenthub`
- Legacy `agent-assist` / `agent_assist` identifiers (DB path, compose volume,
  cookie names, cache export tags) are intentional backward compatibility, not
  bugs. Full removal is a tracked roadmap item in `TODO.md`.
