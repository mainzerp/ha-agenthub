# Architecture

## System Overview

HA-AgentHub is a two-component system for natural language smart home control:

1. **Docker Container** -- The AI backend running FastAPI with multi-agent orchestration, a two-tier vector cache, hybrid entity matching, MCP tool integration, and a plugin system.
2. **HA Custom Integration** -- A Home Assistant bridge (`custom_components/ha_agenthub/`) that forwards most turns to the container, streams responses back to Home Assistant's conversation system, and can honor container-directed native plain-timer delegation.

All configuration, secrets, and state are stored in SQLite. ChromaDB provides vector storage for entity embeddings and cache embeddings. No configuration files are used at runtime -- everything is managed through the setup wizard and admin dashboard.

## Component Diagram

```
+--------------------------------------------------+
|  Home Assistant                                   |
|  +--------------------------------------------+  |
|  |  ha_agenthub custom integration            |  |
|  |  (conversation agent -- HA bridge + native |
|  |   timer delegate seam)                     |  |
|  +---------------------+----------------------+  |
+-------------------------|-------------------------+
                          | REST / SSE / WebSocket
                          v
+--------------------------------------------------+
|  Docker Container (FastAPI)                       |
|                                                   |
|  +----------------------------------------------+ |
|  | Setup Wizard / Admin Dashboard               | |
|  +----------------------------------------------+ |
|  | API Layer (conversation, admin, health)       | |
|  +----------------------------------------------+ |
|  | Middleware (auth, tracing, setup redirect)    | |
|  +---+------------------------------------------+ |
|      |                                            |
|  +---v---+   +----------+   +-----------+        |
|  | Orch. |-->| A2A      |-->| Specialist|        |
|  | Agent  |  | Dispatch |   | Agents    |        |
|  +---+---+   +----------+   +-----------+        |
|      |                                            |
|  +---v-----------+   +----------+                 |
|  | Two-Tier Cache|   | Entity   |                 |
|  | (routing +    |   | Matcher  |                 |
|  |  action)      |   | (5 sig.) |                 |
|  +---------------+   +----------+                 |
|                                                   |
|  +---------------+   +----------+  +----------+  |
|  | MCP Tool Mgr  |   | Plugin   |  | LLM      |  |
|  | (stdio/SSE)   |   | System   |  | Client   |  |
|  +---------------+   +----------+  +----------+  |
|                                                   |
|  +----------------------------------------------+ |
|  | SQLite (config, secrets, history, analytics) | |
|  +----------------------------------------------+ |
|  | ChromaDB (entity index, cache embeddings)    | |
|  +----------------------------------------------+ |
+--------------------------------------------------+
```

## A2A Protocol

Agents communicate via a JSON-RPC 2.0-based Agent-to-Agent (A2A) protocol:

- **Registry** (`a2a/registry.py`) -- Maintains agent cards describing each agent's ID, name, description, skills, and endpoint.
- **Dispatcher** (`a2a/dispatcher.py`) -- Routes JSON-RPC requests to agents by method (`message/send`, `message/stream`, `agent/discover`, `agent/list`).
- **Transport** (`a2a/transport.py`) -- In-process transport calls agent handlers directly within the container. The transport abstraction allows for future HTTP-based transport.

Each agent publishes an **Agent Card** containing its ID, capabilities, and supported intents. The orchestrator uses these cards to make routing decisions.

### Agent Inventory

Ten routable domain agents are reachable from intent classification:
`orchestrator` plus `light`, `music`, `climate`, `media`, `timer`,
`scene`, `automation`, `security`, `general`, and `send` (delivery to
phones, satellites, and notify targets; added in 0.12.0).

Internal helper agents participate in the pipeline but are not
user-routable: `filler`, `rewrite`, `mediation`, `language_detect`,
`sanitize`, `cancel_speech`, `delayed_tasks`, and
`notification_dispatcher`. Runtime services include `custom_loader`,
`timer_scheduler`, and `alarm_monitor`.

Custom agents created through the admin API are also registered as A2A
agents with IDs shaped as `custom-{name}`. Their prompt, model config,
MCP tool assignments, enabled state, and entity visibility rules are
synchronized from SQLite before registration so the orchestrator can
route to them through the same dispatcher boundary as built-in agents.

## Request Flow

1. User speaks a command in Home Assistant (e.g., "turn on the bedroom light").
2. The HA custom integration sends the text to the container via `POST /api/conversation` (or SSE/WebSocket).
3. The API layer authenticates the request (Bearer token) and builds an A2A `message/send` request targeting the orchestrator.
4. **Orchestrator agent** receives the request:
   a. Checks the **routing cache** -- if a similar request was recently routed, reuses the cached routing decision (threshold: 0.92 cosine similarity).
   b. If cache miss, calls the LLM for **intent classification** to select the target agent.
   c. Condenses the task description, preserving entity names.
   d. Dispatches via A2A to the selected specialist agent.
5. **Specialist agent** (e.g., light-agent) receives the task:
   a. Uses the **entity matcher** to resolve "bedroom light" to `light.bedroom_main`.
   b. Calls the HA REST API (`ha_client/rest.py`) to execute `light/turn_on`.
   c. Returns a response with speech text and action details.
6. The orchestrator checks the **action cache** for reuse opportunities and stores the new result.
7. The response flows back through the API layer to the HA integration, which speaks it to the user.

For eligible plain timer start/cancel turns, the timer-agent may instead return a delegation directive, which the HA integration honors by calling Home Assistant's built-in conversation agent once.

When an internal scheduler alarm fires with `briefing=true`, the
background path stays orchestrator-owned: the scheduler emits an
`alarm_notification` event, the orchestrator passes an
`OrchestratorGateway` into `background_actions`, and the wake briefing
composer gathers weather/news through A2A plus calendar/sensor facts
through HA REST before overriding the spoken alarm text. This keeps the
cross-agent boundary narrow and avoids direct peer-agent imports from
the wake briefing module.

### Send Agent and Sequential Dispatch

When the orchestrator classifies a turn as a delivery action ("tell
the kitchen speaker that dinner is ready"), the request is routed to
`send-agent`. The agent resolves the target through the
`send_device_mappings` table (configured under the dashboard
"Send devices" page), composes a notification or assist-satellite
payload, and calls Home Assistant's `notify.*` service or the
appropriate `assist_satellite.*` service.

Multi-step intents ("close the blinds and tell me how warm it got
in the bedroom today") are sequenced by the orchestrator: each step
is dispatched as its own A2A `message/send` against the chosen
domain agent, with subsequent steps receiving the previous step's
result as context. Per-action domain filtering in the executors
(0.19.2/0.19.3) ensures, for example, that a `camera_turn_on` step
cannot land on a same-named `lock` or `switch` entity.

### Filler / Interim TTS

When `filler.enabled` is `true` and the orchestrator's first useful
token takes longer than `filler.threshold_ms`, the filler agent
emits short interim tokens marked with `StreamToken.is_filler=true`.
The HA integration speaks these immediately while the real reply
continues to be generated. Once the real first token arrives, the
filler stops emitting and the stream continues normally.

### Language Detection and Per-Agent Directive

The `language` setting (default `auto`) controls reply language.
When `auto`, the `language_detect` agent resolves the per-turn
language from the user input and the HA-provided `language` field,
and the orchestrator injects an explicit
"respond in <language>" directive into the system prompt of the
downstream domain agent. Forcing an ISO code (`de`, `en`, ...)
bypasses detection and pins all replies.

### Per-Turn Tracing on `/ws/conversation`

`TracingMiddleware` skips connection-level traces for paths under
`/ws/conversation` and instead leaves a `ws_per_turn=True` marker on
the ASGI scope. The route handler mints a fresh `trace_id`,
`SpanCollector`, and root span per inbound message, hands the
collector to the orchestrator dispatch, and flushes a synthesised
`ws_turn` root span at the end of each turn. This avoids the
0.20.0-and-earlier bug where every per-turn duration was overwritten
with the entire connection lifetime.

### Recorder-History Tool

A recorder-history MCP tool exposes Home Assistant's long-term
history queries to agents that need them (mostly the general agent).
See `container/tests/test_recorder_history.py` for the tool's
contract.

### Cancel-Intent / Dismiss

The `cancel_speech` agent detects user requests to dismiss the
current or previous response ("never mind", "stop") and short-
circuits the dispatch so no downstream domain agent is invoked. See
`container/tests/test_cancel_interaction.py` for the interaction
matrix.

## Two-Tier Cache

The action cache was named "response cache" in 0.20.x and earlier.
The legacy term still appears in the on-disk Chroma collection name
and in the `cache.response.*` settings keys for backward
compatibility.

The cache system uses ChromaDB vector embeddings for semantic similarity matching:

- **Routing Cache** -- Caches the mapping from user intent to target agent. A hit (cosine similarity >= 0.92) skips LLM-based intent classification entirely. Max entries: 50,000 with LRU eviction.
- **Action Cache** -- Caches full agent responses including executed actions.
  - **Full hit** (>= 0.95): Returns the cached response directly (optionally rewritten by the rewrite agent for variety).
  - **Partial hit** (0.80-0.95): Provides the cached response as context to the agent for faster processing.
  - **Miss** (< 0.80): No cache involvement.
  - Max entries: 20,000 with LRU eviction.

Cache entries are reactively invalidated when an executed action fails.

## Entity Matching

The hybrid entity matcher combines five signals with configurable weights:

| Signal | Method | Example |
|--------|--------|---------|
| Fuzzy string | Levenshtein + Jaro-Winkler | "bedroom lite" ~ "bedroom light" |
| Phonetic | Soundex + Metaphone | "bedroom lite" sounds like "bedroom light" |
| Embedding | ChromaDB vector similarity | Semantic closeness |
| Alias | Exact lookup from DB | "nightstand lamp" = `light.bedroom_nightstand` |
| Domain | HA entity domain filtering | "light" commands only match `light.*` entities |

By default, a weighted score above 0.60 returns a single confident match. Below the configured threshold, the top-N candidates are sent to the LLM for disambiguation.

## Data Storage

- **SQLite** -- Primary store for all structured data: settings, agent configs, custom agents, aliases, MCP servers, secrets (Fernet-encrypted), admin accounts (bcrypt-hashed), setup state, conversations, analytics, and trace spans.
- **ChromaDB** -- Vector store for entity index embeddings, routing cache embeddings, and action cache embeddings (the on-disk collection literal is still `response_cache` for backward compatibility). Persisted to disk at `/data/chromadb`.

## Plugin Architecture

Plugins extend the system without modifying core code:

- Plugins are Python files in `container/plugins/` discovered at startup.
- Each plugin subclasses `BasePlugin` and implements lifecycle hooks: `configure`, `startup`, `ready`, `shutdown`.
- The `PluginContext` provides a read-only agent catalog, the orchestrator gateway, MCP registry access, settings access, and restricted route helpers; the old direct registry and raw `app` escape hatches are removed.
- Plugins can inspect registered agents, dispatch work through the orchestrator, add routes, subscribe to events via the event bus, and read/write settings.
- Plugin failures are isolated -- one plugin crashing does not affect others.

See [Plugin Development Guide](plugin-development.md) for details.
