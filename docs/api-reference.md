# API Reference

## Authentication

All API endpoints (except `/api/health` and `/setup/*`) require authentication.

### Conversation Endpoints

Use a Bearer token in the `Authorization` header:

```
Authorization: Bearer <api_key>
```

The API key is generated during the setup wizard (step 3).

### Admin Endpoints

Admin endpoints require a session cookie obtained by logging in through the dashboard. The cookie name is `agent_assist_session` (literal in `container/app/security/auth.py`) and expires after 24 hours.

### WebSocket

Pass the API key in the `Authorization` header (recommended):

```
Authorization: Bearer <api_key>
```

**Deprecated:** The `token` query parameter is still accepted but will be removed in a future release. Query-string credentials can leak through proxy logs and browser history. Migrate to header-based auth.

---

## Health

### GET /api/health

Returns container health status. No authentication required.

**Response:**

```json
{
  "status": "ok",
  "version": "1.19.4",
  "log_level": "INFO"
}
```

The `version` value is read from `container/app/__init__.py`
`__version__` at runtime; older containers will report their own value.

---

## Conversation

### POST /api/conversation

Send a natural language command and receive a full response.

**Auth:** Bearer token

**Request body:**

```json
{
  "text": "turn on the bedroom light",
  "conversation_id": "optional-conversation-id",
  "device_id": "optional-ha-device-id",
  "area_id": "optional-ha-area-id",
  "device_name": "optional-friendly-device-name",
  "area_name": "optional-friendly-area-name",
  "language": "optional-bcp47-or-iso-code"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | string | yes | Natural language command. |
| `conversation_id` | string | no | Reuse a prior turn's conversation thread. |
| `device_id` | string | no | HA device id of the calling satellite/voice device. |
| `area_id` | string | no | HA area id derived from the calling device. |
| `device_name` | string | no | Friendly device name (used by send-agent). |
| `area_name` | string | no | Friendly area name (used for context and routing). |
| `language` | string | no | Per-turn language override; falls back to the `language` setting (default `auto`). |

**Response:**

```json
{
  "speech": "I've turned on the bedroom light.",
  "conversation_id": "abc123",
  "voice_followup": false
}
```

| Field | Type | Description |
|-------|------|-------------|
| `speech` | string | Final speech text returned to the user. |
| `conversation_id` | string | Conversation thread identifier. |
| `voice_followup` | bool | When true, the HA integration is asked to keep the microphone open for an immediate follow-up turn. |

### POST /api/conversation/stream

Send a command and receive a streaming SSE response.

**Auth:** Bearer token

**Request body:** Same as `POST /api/conversation`

**Response:** Server-Sent Events stream. Each event:

```
data: {"token": "I've", "done": false, "conversation_id": null}
data: {"token": " turned on", "done": false, "conversation_id": null}
data: {"token": "", "done": true, "conversation_id": "abc123"}
```

**StreamToken fields** (also emitted over `/ws/conversation`):

| Field | Type | Description |
|-------|------|-------------|
| `token` | string | Token text fragment. |
| `done` | bool | True on the terminal event. |
| `conversation_id` | string \| null | Set on the terminal event. |
| `mediated_speech` | string \| null | Final mediated speech replacement (set when the personality / mediation pipeline rewrites the streamed tokens). |
| `is_filler` | bool | Marks tokens emitted by the filler agent (interim TTS while the real answer is being computed). |
| `error` | string \| null | Set when the stream is terminating due to an error. |
| `voice_followup` | bool | Mirrors the REST `voice_followup` flag on the terminal event. |
| `sanitized` | bool | When true, the integration must skip its defensive markdown stripper because the container already sanitised the speech. |
| `filler_push` | bool | When true, the integration should push filler tokens immediately rather than buffering them. |

### WS /ws/conversation

WebSocket endpoint for streaming conversation.

**Auth:** Bearer token via `Authorization` header (preferred). Query-string `token` parameter is deprecated.

**Send:**

```json
{
  "text": "turn on the bedroom light",
  "conversation_id": "optional-id"
}
```

**Receive:** Stream of token objects, same format as SSE events.

---

## Admin -- Settings

### GET /api/admin/settings

Get all settings grouped by category.

**Auth:** Admin session

**Response:**

```json
{
  "settings": {
    "cache": [
      {"key": "cache.routing.threshold", "value": "0.92", "value_type": "float", "category": "cache", "description": "..."}
    ],
    "embedding": [...],
    "entity_matching": [...]
  }
}
```

### PUT /api/admin/settings

Update multiple settings.

**Auth:** Admin session

**Request body:**

```json
{
  "cache.routing.threshold": "0.90",
  "cache.action.threshold": "0.90"
}
```

### PUT /api/admin/settings/{key}

Update a single setting.

**Auth:** Admin session

**Request body:**

```json
{
  "value": "0.90",
  "value_type": "float",
  "category": "cache"
}
```

---

## Admin -- Agents

### GET /api/admin/agents

List all registered agents with their configuration.

**Auth:** Admin session

**Response:**

```json
{
  "agents": [
    {
      "agent_id": "light-agent",
      "name": "Light",
      "description": "Lighting control",
      "enabled": true,
      "model": "openrouter/openai/gpt-4o-mini",
      "timeout": 5,
      "temperature": 0.2,
      "max_tokens": 1024
    }
  ]
}
```

### GET /api/admin/persons

List Home Assistant persons.

Auth: admin session.

### GET /api/admin/agents/{agent_id}/prompt

Get the compiled system prompt for a built-in or custom agent.

### PUT /api/admin/agents/{agent_id}/prompt

Update the system prompt for a custom agent.

Auth: admin session.

---

## Admin -- Custom Agents

### GET /api/admin/custom-agents

List all custom agents.

### POST /api/admin/custom-agents

Create a custom agent. The stored name is normalized to a lowercase slug
and the runtime agent ID is `custom-{name}`. Creation also creates the
matching `agent_configs` row used by LLM calls, synchronizes MCP tool
assignments into `agent_mcp_tools`, and synchronizes entity visibility
rules into `entity_visibility_rules`.

**Request body:**

```json
{
  "name": "weather-agent",
  "description": "Weather information",
  "system_prompt": "You are a weather assistant...",
  "model_override": "openrouter/openai/gpt-4o-mini",
  "mcp_tools": [{"server_name": "duckduckgo-search", "tool_name": "web_search"}],
  "entity_visibility": [{"rule_type": "domain_include", "rule_value": "weather"}],
  "intent_patterns": ["weather", "forecast", "temperature outside"]
}
```

If `model_override` is omitted, the custom agent copies practical LLM
defaults from `general-agent` so dispatch can use the normal config
lookup path without manual database edits.

### GET /api/admin/custom-agents/{name}

Get a single custom agent.

### PUT /api/admin/custom-agents/{name}

Update a custom agent. Partial updates are supported; include only fields
to change. Supplying `model_override`, `mcp_tools`, `entity_visibility`,
or `enabled` updates the matching runtime stores after the custom-agent
row is saved. Disabling an agent keeps its config row with
`enabled=false` and clears active MCP and visibility assignments until it
is enabled again.

### DELETE /api/admin/custom-agents/{name}

Delete a custom agent. Deletion removes the custom-agent row, the matching
`agent_configs` row, active MCP assignments, and active visibility rules,
then reloads the custom-agent registry.

---

## Admin -- MCP Servers

### GET /api/admin/mcp-servers

List all MCP servers with connection status.

### POST /api/admin/mcp-servers

Add a new MCP server.

**Request body:**

```json
{
  "name": "my-tools",
  "transport": "stdio",
  "command_or_url": "python my_mcp_server.py",
  "env_vars": {"API_KEY": "..."},
  "timeout": 30
}
```

### DELETE /api/admin/mcp-servers/{name}

Remove an MCP server.

### GET /api/admin/mcp-servers/{name}/tools

List discovered tools for a specific MCP server.

## Admin -- MCP Agent Tools

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/admin/mcp/agent-tools-summary` | Summary of MCP tool assignments across all agents. |
| GET | `/api/admin/mcp/agent-tools/{agent_id}` | List MCP tools assigned to a specific agent. |
| POST | `/api/admin/mcp/agent-tools/{agent_id}` | Assign an MCP tool to an agent. |
| DELETE | `/api/admin/mcp/agent-tools/{agent_id}/{server_name}/{tool_name}` | Remove an MCP tool assignment from an agent. |

Auth: admin session.

---

## Admin -- Entity Index

### GET /api/admin/entity-index/stats

Get entity index statistics with per-domain breakdown.

**Response:**

```json
{
  "count": 150,
  "last_refresh": "2025-01-15T10:30:00",
  "domains": {"light": 45, "switch": 30, "climate": 5}
}
```

### POST /api/admin/entity-index/refresh

Force a full entity index refresh from Home Assistant.

---

## Admin -- Entity Visibility

### GET /api/admin/entity-visibility/{agent_id}

Get visibility rules for an agent.

### PUT /api/admin/entity-visibility/{agent_id}

Set visibility rules for an agent.

**Request body:**

```json
{
  "rules": [
    {"rule_type": "domain", "rule_value": "light"},
    {"rule_type": "area", "rule_value": "bedroom"}
  ]
}
```

### GET /api/admin/entities

List all Home Assistant entities grouped by domain and area.

---

## Admin -- Cache

The action cache was named "response cache" in 0.20.x and earlier.
Every cache endpoint accepts both the canonical `action` value and
the legacy `response` alias for the `tier` parameter (URL query, JSON
body, or multipart form). New responses emit `action` as the
canonical key. The export envelope uses format_version 2; parse_envelope still accepts format_version 1 envelopes with tiers.response.

### GET /api/admin/cache/stats

Get cache statistics per tier.

### GET /api/admin/cache/entries

Browse/search cache entries.

**Query parameters:**
- `tier` -- `routing`, `action`, or `response` (legacy alias for `action`; default: `routing`)
- `search` -- Text filter
- `page` -- Page number (default: 1)
- `per_page` -- Results per page (default: 50, max: 200)

### POST /api/admin/cache/flush

Flush cache entries.

**Request body:**

```json
{
  "tier": "routing"
}
```

Omit `tier` or set to `null` to flush all tiers. Accepted values:
`routing`, `action`, or `response` (legacy alias).

### GET /api/admin/cache/export

Streams a JSON envelope (`format_version: 2`) of one or more cache
tiers.

**Query parameters:**
- `tier` -- `routing`, `action`, `response` (legacy alias), or `all`
  (default: `all`)

### POST /api/admin/cache/import

Multipart upload that restores cache entries from an envelope.

**Form fields:**
- `file` -- the JSON envelope (max 50 MiB)
- `mode` -- `merge` or `replace`
- `tiers` -- CSV of tier names; accepts `action` and `response` as
  aliases
- `re_embed` -- `true` to recompute embeddings on import

New exports use the `tiers.action.entries` shape. `parse_envelope`
still accepts `format_version: 1` envelopes that carry
`tiers.response.entries` so backups produced on 0.20.x remain
importable.

### DELETE /api/admin/cache/entries/{entry_id}

Delete a single cache entry by its ID.

Auth: admin session. Added in 1.19.4.

---

## Admin -- Home Assistant connection

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/admin/ha-connection` | Read the current HA URL (token redacted). |
| PUT | `/api/admin/ha-connection` | Update HA URL and/or Long-Lived Access Token. |
| POST | `/api/admin/ha-connection/test` | Validate a candidate URL/token pair without persisting. |

Auth: admin session.

## Admin -- Container API key

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/admin/container-api-key` | Report whether an API key is set. |
| POST | `/api/admin/container-api-key` | Create the initial API key. |
| PUT | `/api/admin/container-api-key` | Replace the stored API key with a caller-supplied value. |
| POST | `/api/admin/container-api-key/rotate` | Generate a new API key server-side and return it once. |

Auth: admin session.

## Admin -- Entity matching weights

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/admin/entity-matching-weights` | Return the current five-signal weight vector. |
| PUT | `/api/admin/entity-matching-weights` | Update one or more signal weights. |

Auth: admin session.

## Admin -- LLM providers

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/admin/llm-providers/{id}` | Read provider config (key redacted). |
| PUT | `/api/admin/llm-providers/{id}` | Update provider config. |
| DELETE | `/api/admin/llm-providers/{id}` | Remove a provider. |
| POST | `/api/admin/llm-providers/test` | Validate a candidate provider config without persisting. |
| GET | `/api/admin/llm-providers/configured` | List provider ids that have stored credentials. |
| PUT | `/api/admin/llm-providers/ollama` | Update Ollama provider config (special endpoint for local inference). |

Auth: admin session.

## Admin -- Agents (visibility)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/admin/agents/visibility-summary` | Per-agent counts of effective entity visibility rules. |

Auth: admin session. See also `/api/admin/agents` (above) and
`/api/admin/entity-visibility/{agent_id}` for editing rules.

## Admin -- Timers

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/admin/timers` | List active and scheduled timers. |
| GET | `/api/admin/timers/recently-expired` | Compatibility endpoint (returns empty list). |
| GET | `/api/admin/timers/satellites` | List timer satellite devices. |
| PATCH | `/api/admin/timers/{timer_id}` | Update a timer. |
| DELETE | `/api/admin/timers/{timer_id}` | Delete a timer. |

Auth: admin session.

## Admin -- Calendar

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/admin/calendar/users` | List calendar users. |
| POST | `/api/admin/calendar/users` | Create a calendar user. |
| GET | `/api/admin/calendar/users/{user_id}` | Get a single calendar user. |
| PUT | `/api/admin/calendar/users/{user_id}` | Update a calendar user. |
| DELETE | `/api/admin/calendar/users/{user_id}` | Delete a calendar user. |
| GET | `/api/admin/calendar/events` | List calendar events. |
| POST | `/api/admin/calendar/events` | Create a calendar event. |
| GET | `/api/admin/calendar/events/{event_id}` | Get a single event. |
| PUT | `/api/admin/calendar/events/{event_id}` | Update an event. |
| DELETE | `/api/admin/calendar/events/{event_id}` | Delete an event. |
| GET | `/api/admin/calendar/calendars` | List available calendars. |
| GET | `/api/admin/calendar/entity-settings` | Get calendar entity settings. |
| GET | `/api/admin/calendar/settings` | Get calendar settings. |
| PUT | `/api/admin/calendar/settings` | Update calendar settings. |
| DELETE | `/api/admin/calendar/reminder-state` | Clear reminder state. |

Auth: admin session.

## Admin -- Fernet key backup

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/admin/fernet-key-backup` | Export the current Fernet key for offline backup. |

Auth: admin session. See [Backup and Restore](backup-restore.md) for
guidance on storing the returned key.

## Admin -- Notification profile

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/admin/notification-profile` | Read the current notification dispatcher profile. |
| PUT | `/api/admin/notification-profile` | Update notification routing profile fields. |

Auth: admin session.

## Admin -- Alarm monitor

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/admin/alarm-monitor` | Read alarm-monitor agent state. |

Auth: admin session.

## Admin -- Rewrite agent

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/admin/rewrite/config` | Read rewrite-agent model and temperature. |
| PUT | `/api/admin/rewrite/config` | Update rewrite-agent configuration. |

Auth: admin session. The rewrite agent runs only when
`personality.prompt` is non-empty.

## Admin -- Personality

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/admin/personality/config` | Read the personality prompt and mediation parameters. |
| PUT | `/api/admin/personality/config` | Update the personality prompt and mediation parameters. |

Auth: admin session.

## Admin -- Wake Briefing

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/admin/settings/wake-briefing` | Read wake briefing configuration. |
| PUT | `/api/admin/settings/wake-briefing` | Update wake briefing configuration. |
| POST | `/api/admin/settings/wake-briefing/test` | Test the wake briefing composer. |

Auth: admin session.

## Admin -- Dashboard chat

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/admin/chat` | Send a single command from the dashboard chat tester (non-streaming). |
| POST | `/api/admin/chat/stream` | Streaming variant returning the same StreamToken format as `/api/conversation/stream`. |

Auth: admin session. These bypass the HA conversation pipeline and
talk straight to the orchestrator.

## Admin -- Send devices

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/admin/send-devices` | List configured send-device mappings. |
| POST | `/api/admin/send-devices` | Create a send-device mapping. |
| PUT | `/api/admin/send-devices/{id}` | Update a send-device mapping. |
| DELETE | `/api/admin/send-devices/{id}` | Remove a send-device mapping. |
| GET | `/api/admin/send-devices/available-targets` | List HA notify targets and assist satellites available for mapping. |

Auth: admin session.

## Admin -- Domain agent map

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/admin/domain-agent-map` | Read the domain-to-agent routing table. |
| PUT | `/api/admin/domain-agent-map` | Replace the domain-to-agent routing table. |
| PUT | `/api/admin/domain-agent-map/device-class` | Update device-class overrides used by the routing table. |

Auth: admin session.

## Admin -- Entity index (match preview)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/admin/entity-index/match-preview` | Live-test the hybrid matcher against a query string and return the top candidates. |

Auth: admin session. Companion to the existing `/api/admin/entity-index/stats`
and `/api/admin/entity-index/refresh` endpoints documented above.

## Admin -- Traces

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/admin/traces/export` | Export traces in NDJSON form for offline inspection. |
| GET | `/api/admin/traces/labels` | List configured trace labels. |
| PUT | `/api/admin/traces/{trace_id}/label` | Set or clear the label on a trace. |

Auth: admin session. Companions to the existing `/api/admin/traces`
list and `/api/admin/traces/{trace_id}` detail endpoints.

## Admin -- Dashboard overview

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/admin/dashboard/overview` | Compact JSON used by the dashboard landing page. |
| GET | `/api/admin/dashboard/overview/extended` | Extended dashboard overview (MCP, plugin status). |
| GET | `/api/admin/dashboard/health/extended` | Extended health snapshot for the System Health page. |

Auth: admin session.

---

## Errors and status codes

| Code | Meaning |
|------|---------|
| 400 | Schema-level rejection (for example, malformed cache export envelope). |
| 401 | Missing `X-Container-API-Key` / `Authorization: Bearer ...` for conversation endpoints, or missing/invalid admin session cookie for admin endpoints. |
| 403 | Credentials present but refused (for example, a stale `X-Container-API-Key` after rotation). |
| 422 | Pydantic-level body validation failure. |
| 429 | Per-route rate limiter rejected the request. |
| 503 | Home Assistant is unreachable from the container (REST or WebSocket). |

Admin endpoints distinguish missing-credential (`401`) from
credential-rejected (`403`) so the dashboard and the HA integration
can show different remediation messages. The HA integration's REST
fallback uses these codes to surface the distinct user-facing
error messages.

## WebSocket close-error contract

`/ws/conversation` uses application close codes that the HA
integration reacts to specifically:

| Code | Reason |
|------|--------|
| `4401` | Authentication failed (missing or invalid API key during the WebSocket handshake). The integration falls back to REST. |
| `4408` | Idle/heartbeat timeout. The integration reconnects with backoff. |
| `1011` | Server-side error during a turn. The integration reconnects and retries the turn over REST if a final response was not received. |
| `1000` | Normal close (initiated by the client or container shutdown). |

The contract is exercised by the integration tests in
`container/tests/test_ha_client.py` and the matching client logic in
`custom_components/ha_agenthub/conversation.py`.


---

## Admin -- Conversations

### GET /api/admin/conversations

List/search conversation history.

**Query parameters:**
- `agent_id` -- Filter by agent
- `search` -- Text search
- `start_date`, `end_date` -- Date range filter
- `page`, `per_page` -- Pagination

### GET /api/admin/conversations/{conversation_id}

Get full thread detail for a conversation.

---

## Admin -- Analytics

### GET /api/admin/analytics/overview

Summary metrics (total requests, avg latency, cache hit rate, total conversations).

**Query parameters:**
- `hours` -- Time window (default: 24, max: 720)

### GET /api/admin/analytics/requests

Time-series request counts in Chart.js-compatible format.

**Query parameters:**
- `hours` -- Time window
- `bucket_minutes` -- Bucket size (default: 60)

---

## Admin -- Traces

### GET /api/admin/traces

List recent traces with pagination.

**Query parameters:**
- `page`, `per_page` -- Pagination

### GET /api/admin/traces/{trace_id}

Get all spans for a specific trace (for Gantt visualization).

---

## Admin -- Logs

### GET /api/admin/logs

List recent log entries with optional filtering and pagination.

**Query parameters:**
- `level` -- Minimum level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`
- `logger` -- Substring match on logger name
- `since` -- ISO 8601 timestamp; only entries after this time are returned
- `search` -- Substring search in the log message
- `limit` -- Max entries to return (default: 100, max: 1000)
- `offset` -- Pagination offset (default: 0)

**Response:**

```json
{
  "entries": [
    {
      "timestamp": "2026-01-01T12:00:00+00:00",
      "level": "INFO",
      "name": "app.core",
      "message": "Startup complete",
      "module": "main",
      "funcName": "lifespan",
      "lineno": 123
    }
  ],
  "total": 42
}
```

### GET /api/admin/logs/levels

Return the current root log level and all explicitly-set logger levels.

**Response:**

```json
{
  "root_level": "INFO",
  "loggers": {
    "root": "INFO",
    "app.api": "DEBUG"
  }
}
```

### POST /api/admin/logs/levels

Update a logger's level at runtime.

**Request body:**

```json
{
  "logger_name": "app.api",
  "level": "DEBUG"
}
```

Accepted levels: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`.

---

## Admin -- Plugins

### GET /api/admin/plugins

List all installed plugins with loaded status.

### POST /api/admin/plugins/{name}/enable

Enable a plugin.

### POST /api/admin/plugins/{name}/disable

Disable a plugin.

---

## Setup Wizard

The setup wizard endpoints are used during initial configuration. They are not intended for external API use.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/setup/` | Redirect to first incomplete step |
| GET | `/setup/step/{n}` | Render step template |
| POST | `/setup/step/1` | Save admin password |
| POST | `/setup/step/2` | Save HA connection |
| POST | `/setup/step/3` | Generate API key |
| POST | `/setup/step/4` | Save LLM provider keys |
| POST | `/setup/step/5` | Complete setup |
| POST | `/setup/test/ha` | Test HA connection |
| POST | `/setup/test/llm` | Test LLM provider |
