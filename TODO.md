# TODO

## Pending Features

- [ ] **P1 -- HA service for automations (`ai_task` equivalent)**: Service or clear contract for automations (e.g. structured output / `generate_data` pattern) that makes the container usable without manual HTTP construction.

- [ ] **P2 -- User and agent memory**: Persistent profiles, memory tool (save/retrieve/update), limits/eviction, optional dashboard UI; multi-layer user mapping where appropriate.

- [ ] **P3 -- Security agent sentinel mode**: Deferred. If one or more sensors explicitly assigned to the security agent should automatically trigger a security-agent run, that requires a separate trigger contract and likely a dedicated UI page.

- [x] **P4 -- Mobile compatibility for the dashboard**: Responsive layout, touch-friendly controls, and viewport-aware navigation for the admin/analytics dashboards so they are usable on phones and tablets without horizontal scrolling or overlapping elements.


## Project Roadmap

This page tracks planned or not-yet-implemented work for HA-AgentHub.

- [ ] distributed HTTP-based A2A transport across multiple containers or processes
- [ ] Home Assistant Supervisor add-on packaging and runtime support
- [ ] plugin marketplace or discovery UI
- [ ] full removal of legacy `agent-assist` runtime identifiers
- [ ] benchmark-style latency commitments enforced as acceptance criteria

## Strong Candidates

- [ ] automation-safe AI task runs with structured result options
- [ ] opt-in user memory with explicit save, recall, and profile controls
- [ ] occupancy-aware routing for area-sensitive targeting
- [ ] security-agent sentinel mode with explicit trigger contracts and a dedicated admin UI remains deferred; 1.0.0 ships wake briefing for internal alarms only.

## Weaker / Speculative Ideas

- [ ] live activity view for orchestration flow and agent health
- [ ] exploratory local voice runtime with wake word, speech input, and speaker-aware routing

## Potential Extensions (Research-Based)

Ideas and features gathered from current market trends, comparable open-source projects, and emerging protocols. These are not committed to the roadmap but serve as a backlog for future evaluation.

Items prefixed with `DONE` are already implemented in the current codebase and are kept here for reference.

### Protocol & Integration Layer

- [x] **MCP (Model Context Protocol) server support**: Bundled MCP servers (`duckduckgo-search`, `wikipedia-search`) with per-agent tool assignment, SSE/stdio transports, and admin CRUD via `/api/admin/mcp-servers`.
- [x] **A2A (Agent-to-Agent) protocol compliance**: Full JSON-RPC 2.0 registry, dispatcher, in-process transport, Agent Cards with skills/intents, and configurable timeout/iteration settings.
- [ ] **Token-efficient entity discovery**: Replace full entity dumps with dynamic, on-demand discovery (Smart Entity Index pattern). Pre-generate a lightweight system-structure index (~400-800 tokens) and expose query tools so the LLM only fetches what it needs.
- [x] **Multi-provider LLM backend support**: OpenRouter, Groq, Anthropic, Ollama, and local models configurable via `/api/admin/llm-providers` with per-agent model overrides.

### Agent Capabilities

- [ ] **AI-powered automation suggestions**: Scan entities, detect new devices, and use an LLM to propose tailored YAML automations delivered as persistent HA notifications.
- [ ] **Natural-language dashboard creation**: Generate or modify Lovelace dashboards from conversational descriptions, including card layouts, themes, and styling.
- [x] **Web-search augmentation**: DuckDuckGo MCP server (`web_search`, `web_search_news`) and Wikipedia MCP server available for general-agent and custom agents via tool assignments.
- [ ] **Git-backed configuration versioning**: Automatically commit every agent-generated change (automations, dashboards, scripts) to a local Git repo with meaningful messages and support rollback by description or date.
- [ ] **RAG / knowledge-base retrieval**: Re-purpose the existing ChromaDB vector store (currently used for entity caching) into a general document-retrieval layer for manuals, past conversations, and household rules.
- [x] **Multi-turn conversation memory with context windows**: Conversation turns persisted per trace, `voice_followup` flag for organic follow-ups, and stateful WebSocket streaming.

### Voice & Accessibility

- [ ] **Wake-word + local speech pipeline**: Integrate open-source wake-word detection (e.g., openWakeWord, Porcupine) and on-device STT/TTS so the system can operate without cloud voice services.
- [ ] **Speaker-aware routing**: Identify which person is speaking (via voice fingerprint or satellite microphone location) and route to the appropriate user profile and memory context.
- [ ] **Multilingual UI and prompts**: Localize system prompts, UI labels, and speech patterns beyond the current per-request language placeholder. Support for 15+ languages with auto-detection of the Home Assistant instance language.

### Operations & Observability

- [ ] **Real-time agent health & activity view**: Extend the existing traces and analytics dashboards with a live orchestration-flow visualization (per-agent status, recent task history, error rates).
- [x] **Token-usage and cost monitoring**: `track_token_usage` telemetry, `/api/admin/analytics/tokens` endpoint, and Chart.js dashboards for per-provider consumption.
- [ ] **Structured logging and log analysis**: Centralized agent logs with severity filtering, trace IDs for multi-step flows, and an AI-driven "explain this error" helper.
- [x] **Rate-limiting**: In-memory per-IP rate limiters for conversation, admin, login, setup, and WebSocket message streams. Embedding retry with exponential backoff on provider rate limits.
- [ ] **Circuit-breaker patterns**: Protect the Home Assistant instance from cascading failures when agents perform bulk operations or rapid API calls (complement existing rate-limiting).

### Distribution & Packaging

- [x] **HACS custom-component packaging**: `custom_components/ha_agenthub/` with manifest, config flow, HACS validation workflow, and installation docs.
- [ ] **Containerized add-on with ingress**: Provide an official Home Assistant Supervisor add-on that runs the orchestrator container with managed ingress and authentication.
- [ ] **Plugin marketplace / registry**: A lightweight discovery mechanism where third-party agents can advertise their capabilities (Agent Card-style) and users can enable/disable them without restarting.

### Research & Evaluation

- [ ] **Agentic benchmarking suite**: Define acceptance criteria and automated tests for latency, token efficiency, and task-completion accuracy across different LLM providers.
- [ ] **MCP ecosystem survey**: Evaluate whether to expose HA-AgentHub itself as an MCP server (reverse direction) or integrate an existing community implementation (e.g., `mcp-assist`, `mcp-hass`).
- [ ] **A2A interoperability test**: Validate task delegation between HA-AgentHub and at least one external A2A-compliant framework (Google ADK, LangGraph, etc.).

---

*Last updated: 2026-05-08*
