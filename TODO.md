# TODO

## Pending Features

- [ ] **P1 -- Restrict HA entities to exposed entities only**: Limit entity access to devices marked as "exposed" in Home Assistant so only allowed devices can be controlled or queried.

- [ ] **P1 -- User and agent memory**: Persistent profiles, memory tool (save/retrieve/update), limits/eviction, optional dashboard UI; multi-layer user mapping where appropriate.

- [ ] **P2 -- HA service for automations (`ai_task` equivalent)**: Service or clear contract for automations (e.g. structured output / `generate_data` pattern) that makes the container usable without manual HTTP construction.

- [ ] **P2 -- Security agent sentinel mode**: Deferred. If one or more sensors explicitly assigned to the security agent should automatically trigger a security-agent run, that requires a separate trigger contract and likely a dedicated UI page.

- [x] **Remote logs API** -- API + Dashboard UI for reading and filtering live container logs



# Project Roadmap

This page tracks planned or not-yet-implemented work for HA-AgentHub.

- distributed HTTP-based A2A transport across multiple containers or processes
- Home Assistant Supervisor add-on packaging and runtime support
- plugin marketplace or discovery UI
- full removal of legacy `agent-assist` runtime identifiers
- benchmark-style latency commitments enforced as acceptance criteria

## Strong Candidates

- automation-safe AI task runs with structured result options
- calendar access with event creation and reminder-aware context
- opt-in user memory with explicit save, recall, and profile controls
- occupancy-aware routing for area-sensitive targeting
- security-agent sentinel mode with explicit trigger contracts and a
	dedicated admin UI remains deferred; 1.0.0 ships wake briefing for
	internal alarms only.

## Weaker / Speculative Ideas

- live activity view for orchestration flow and agent health
- exploratory local voice runtime with wake word, speech input, and speaker-aware routing
