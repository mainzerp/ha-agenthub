# Configuration Reference

## Configuration Tiers

HA-AgentHub uses three configuration tiers:

1. **Environment Variables** -- Infrastructure-only settings required to start the container process. Set in `docker-compose.yml` or a `.env` file.
2. **Setup Wizard** -- One-time configuration for secrets and connections (admin password, HA URL/token, API key, LLM keys). Stored encrypted in SQLite.
3. **Admin Dashboard** -- All runtime settings managed through the web UI at `/dashboard/`. Stored in SQLite and hot-reloadable without restart.

## Environment Variables

These are the only settings that use environment variables. All other configuration is stored in SQLite.

| Variable | Default | Description |
|----------|---------|-------------|
| `CONTAINER_HOST` | `0.0.0.0` | Host address the server binds to. |
| `CONTAINER_PORT` | `8080` | Port the server listens on. |
| `LOG_LEVEL` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |
| `LOG_DIR` | `/data/logs` | Directory for persistent rotating file logs. |
| `CHROMADB_PERSIST_DIR` | `/data/chromadb` | Legacy path used by sqlite-vec entity-index data. |
| `SQLITE_DB_PATH` | `/data/agent_assist.db` | SQLite database file path. |
| `FERNET_KEY_PATH` | `/data/.fernet_key` | Path to the Fernet encryption key. Backup target. |
| `COOKIE_SECURE` | `false` | Set to `true` when serving the dashboard behind HTTPS so the admin session and CSRF cookies are restricted to TLS. Setting it on plain HTTP silently breaks login (browser drops the cookie). (Production compose defaults this to `true`; the in-app fallback is also `true` in `app/config.py`.) |
| `HF_HUB_OFFLINE` | `0` | When `1`, disables Hugging Face Hub network calls so the local embedding model loads strictly from the cached weights baked into the image. |
| `HA_AGENTHUB_TAG` | `latest` | Tag used by `container/docker-compose.yml` when pulling `ghcr.io/mainzerp/ha-agenthub`. Override to pin a release. |
| `CORS_ORIGINS` | `""` | Comma-separated list of allowed CORS origins. |
| `TRUSTED_PROXIES` | `""` | Comma-separated list of trusted proxy IPs for correct client-IP extraction behind a reverse proxy. |

Environment variables are loaded by Pydantic `BaseSettings` in `app/config.py` and support `.env` file loading. `HF_HUB_OFFLINE` and `HA_AGENTHUB_TAG` are read by the compose file rather than by the application.

## SQLite Settings Reference

All runtime settings are stored in the `settings` table, organized by category. These are managed through the admin dashboard and seeded with defaults on first startup.

### Cache Settings

The action cache was named "response cache" in 0.20.x and earlier.
The v4 schema migration renamed the keys to canonical `cache.action.*`
form and migrated legacy `cache.response.*` rows (schema migration 23).
The export and import API surface uses the `action` tier name.

| Key | Default | Type | Description |
|-----|---------|------|-------------|
| `cache.enabled` | `true` | bool | Enable cache lookups and writes globally |
| `cache.compound_utterance_bypass` | `true` | bool | Bypass cache lookup for structurally compound utterances |
| `cache.routing.enabled` | `true` | bool | Enable the routing cache tier |
| `cache.routing.semantic_threshold` | `0.92` | float | Legacy threshold setting; the routing cache uses exact SHA-256 hash match lookup. This value is retained for backward compatibility. |
| `cache.routing.max_entries` | `50000` | int | Maximum routing cache entries (LRU eviction) |
| `cache.action.enabled` | `true` | bool | Enable the action cache tier |
| `cache.action.semantic_threshold` | `0.95` | float | Legacy threshold setting; the action cache uses exact SHA-256 hash match lookup. This value is retained for backward compatibility. |
| `cache.action.max_entries` | `50000` | int | Maximum action cache entries (LRU eviction) |
| `cache.lru.trigger_fraction` | `0.95` | float | Early-eviction trigger fraction of `max_entries` |
| `cache.lru.eviction_interval` | `100` | int | Operations between LRU eviction sweeps |

### Cache Validator Settings

| Key | Default | Type | Description |
|-----|---------|------|-------------|
| `cache.validator.enabled` | `true` | bool | Enable periodic action-cache validation |
| `cache.validator.interval_minutes` | `60` | int | Minutes between validation scans (`0` = disabled) |
| `cache.validator.model` | (empty) | string | LLM model for cache validator response regeneration (empty = template only) |
| `cache.validator.temperature` | `0.2` | float | Temperature for cache validator LLM regeneration |
| `cache.validator.reasoning_effort` | `low` | string | Reasoning effort for cache validator LLM calls |
| `cache.validator.max_tokens` | `1024` | int | Max tokens for cache validator LLM regeneration |
| `cache.validator.batch_size` | `10` | int | Number of cache entries to validate in a single LLM batch call |

### Embedding Settings

| Key | Default | Type | Description |
|-----|---------|------|-------------|
| `embedding.provider` | `local` | string | Embedding provider: `local` or `external` |
| `embedding.local_model` | `intfloat/multilingual-e5-small` | string | Local embedding model name |
| `embedding.external_model` | (empty) | string | External model (e.g., `openai/text-embedding-3-small`) |
| `embedding.dimension` | `384` | int | Embedding vector dimension |

### Entity Matching Settings

| Key | Default | Type | Description |
|-----|---------|------|-------------|
| `entity_matching.confidence_threshold` | `0.60` | float | Minimum confidence score for entity match |
| `entity_matching.top_n_candidates` | `3` | int | Number of candidates for LLM disambiguation |
| `entity_matching.oversample_factor` | `20` | int | Embedding shortlist multiplier when agent visibility/preferred-domain hints are present |
| `entity_matching.expansion.enabled` | `true` | bool | Enable on-demand LLM expansion of cold query tokens |
| `entity_matching.expansion.ttl_seconds` | `2592000` | int | Query synonym cache TTL in seconds (default 30 days) |
| `entity_matching.expansion.max_cache_rows` | `5000` | int | Query synonym cache LRU cap |
| `entity_matching.log_misses` | `true` | bool | Emit structured matcher-miss diagnostics |

### Rewrite Agent Settings

The rewrite agent runs only when `personality.prompt` (see
[Personality](#personality-settings)) is non-empty; the keys below
control model selection and sampling for the rewrite call itself.

| Key | Default | Type | Description |
|-----|---------|------|-------------|
| `rewrite.model` | `groq/llama-3.1-8b-instant` | string | LLM model used by the rewrite/mediation pass over cached or finalised speech. |
| `rewrite.temperature` | `0.8` | float | Sampling temperature for the rewrite call. |

Managed via `GET/PUT /api/admin/rewrite/config` and the dashboard
"Rewrite" page.

> **Recommendation:** Use `llama-3.1-8b-instant` for fast, low-cost rewrite passes.

### Communication Settings

These keys remain in the settings table; most live streaming controls
have moved to per-route logic in `container/app/api/routes/conversation.py`
and are no longer the canonical knob. Verify behaviour against the
relevant route before tuning.

> **Note:** `communication.streaming_mode` is deprecated and no longer read at runtime; streaming behavior is controlled per-route in the conversation handlers. This setting remains in the database for backward compatibility but no longer affects routing.

| Key | Default | Type | Description |
|-----|---------|------|-------------|
| `communication.streaming_mode` | `websocket` | string | Streaming mode: `websocket`, `sse`, or `none` |
| `communication.ws_reconnect_interval` | `5` | int | WebSocket reconnect interval in seconds |
| `communication.stream_buffer_size` | `1` | int | Token batching buffer size |

### A2A Settings

| Key | Default | Type | Description |
|-----|---------|------|-------------|
| `a2a.default_timeout` | `10` | int | Default agent timeout in seconds (if the DB setting is absent, the code fallback is `5`). |
| `a2a.max_iterations` | `3` | int | Max iterations per agent to prevent loops |
| `a2a.max_dispatch_timeout` | `60` | int | Hard upper bound (seconds) on a single A2A dispatch, regardless of per-agent overrides. |
| `agent.dispatch_timeout.<agent_id>` | (unset) | int | Per-agent dispatch timeout override; falls back to the agent's `AgentCard.timeout_sec` and then to `a2a.default_timeout`. Capped by `a2a.max_dispatch_timeout`. |

### Entity Sync Settings

| Key | Default | Type | Description |
|-----|---------|------|-------------|
| `entity_sync.interval_minutes` | `30` | int | Background entity-index resync interval. `0` disables periodic syncing (entity index is still primed at startup). |

### Filler Agent Settings

| Key | Default | Type | Description |
|-----|---------|------|-------------|
| `filler.enabled` | `false` | bool | Emit interim TTS "thinking" tokens (`StreamToken.is_filler=true`) while the real answer is being generated. |
| `filler.threshold_ms` | `1000` | int | Minimum elapsed milliseconds before the filler agent is allowed to emit. |

> **Recommendation:** Use `llama-3.1-8b-instant` for fast, low-cost filler generation.

### Mediation Settings

| Key | Default | Type | Description |
|-----|---------|------|-------------|
| `mediation.model` | (empty) | string | LLM model used by the mediation pass. Empty disables mediation. |
| `mediation.temperature` | `0.3` | float | Sampling temperature for the mediation call. |
| `mediation.max_tokens` | `8192` | int | Maximum tokens for the mediation reply. |
| `orchestrator.mediation_streaming_enabled` | `false` | bool | Stream mediated tokens incrementally to the client for earlier TTS start. |

### Personality Settings

| Key | Default | Type | Description |
|-----|---------|------|-------------|
| `personality.prompt` | (empty) | string | Personality system prompt for the response mediation/rewrite pass. When non-empty, the rewrite agent is enabled and finalised speech is run through the mediation pipeline. |

Managed via `GET/PUT /api/admin/personality/config` and the dashboard
"Personality" page.

### Language Settings

| Key | Default | Type | Description |
|-----|---------|------|-------------|
| `language` | `auto` | string | Conversation language. `auto` enables per-turn language detection; an ISO code (`en`, `de`, ...) pins all replies. The detected/forced language is injected into each agent's system prompt as a directive (see `container/app/agents/actionable.py`). |

### Home Context Settings

| Key | Default | Type | Description |
|-----|---------|------|-------------|
| `home.timezone` | (empty) | string | Override timezone for time/date references in agent prompts. Empty falls back to HA's configured timezone. |
| `home.location_name` | (empty) | string | Friendly home name surfaced in prompts and the personality pipeline. |

### General Settings

| Key | Default | Type | Description |
|-----|---------|------|-------------|
| `general.conversation_context_turns` | `3` | int | Number of prior conversation turns retained for recent runtime context; honored by the orchestrator and clamped to `1..20` |
| `agents.actionable.primary_text_source` | `original_when_translated` | string | Primary user message for actionable agents: `original_when_translated` or `description_first` |
| `orchestrator.organic_followup_enabled` | `false` | bool | Offer a follow-up prompt after successful voice responses |
| `orchestrator.organic_followup_probability` | `0.08` | float | Probability (0.0-1.0) of appending a follow-up offer to successful responses |

## Agent Configuration

Each agent has per-agent settings stored in the `agent_configs` table:

| Field | Default | Description |
|-------|---------|-------------|
| `enabled` | `1` | Whether the agent is active |
| `model` | Varies per agent | LLM model identifier (e.g., `groq/llama-3.1-8b-instant`, `openrouter/openai/gpt-4o-mini`) |
| `timeout` | `5` | Maximum response time in seconds |
| `max_iterations` | `3` | Maximum processing iterations |
| `temperature` | `0.2` | LLM sampling temperature |
| `max_tokens` | `1024` | Maximum tokens per LLM response |
| `reasoning_effort` | (empty) | Optional reasoning-effort hint forwarded to providers that accept it (`Low`, `Medium`, `High`). |

> **Model recommendation:** For routable agents, use `openai/gpt-oss-120b` and set `reasoning_effort` to `Low`. For the filler and rewrite agents, use `llama-3.1-8b-instant`.

Default routable agents: `orchestrator`, `light-agent`, `music-agent`,
`timer-agent`, `climate-agent`, `media-agent`, `scene-agent`,
`automation-agent`, `security-agent`, `general-agent`, `send-agent`
(delivers messages to phones, satellites, and notification targets).

### Wake briefing for internal alarms

Internal AgentHub alarms created through `set_datetime` can now opt into
an additional spoken wake briefing by including
`parameters.briefing=true`. This applies only to AgentHub-managed
internal alarms, not HA native plain timers and not the deferred
security sentinel work.

- Configure the feature from the Timers dashboard card labeled
  **Wake Briefing**.
- The composer can include date/weekday/time facts, weather, news,
  visible calendar events, and optional user-selected sensor entities.
- Weather and news are gathered through the orchestrator's A2A gateway;
  calendar events and sensor states come from the HA REST client.
- If a briefing source fails or times out, the remaining sources still
  compose. If the overall composition fails, the system falls back to
  the plain alarm message.
- `wake_briefing.sensor_entities` must use `domain.object_id` values.
  Visibility rules still apply, so hidden entities are skipped.

The orchestrator uses a lower temperature (0.3) for consistent intent classification.

| Key | Default | Type | Description |
|-----|---------|------|-------------|
| `wake_briefing.enabled` | `true` | bool | Enable LLM-composed wake briefings for internal alarms |
| `wake_briefing.sources.weather` | `true` | bool | Include weather summary in wake briefings |
| `wake_briefing.sources.date` | `true` | bool | Include current date and weekday in wake briefings |
| `wake_briefing.sources.news` | `true` | bool | Include news headlines in wake briefings |
| `wake_briefing.sources.calendar` | `true` | bool | Include calendar events for the next 24 hours in wake briefings |
| `wake_briefing.sources.sensors` | `false` | bool | Include configured sensor states in wake briefings |
| `wake_briefing.sensor_entities` | `[]` | json | List of sensor `entity_id`s to read for wake briefings |
| `wake_briefing.news_query` | `top news today` | string | User text dispatched to general-agent for news |
| `wake_briefing.news_count` | `3` | int | Requested number of news headlines |
| `wake_briefing.timeout_seconds` | `10` | int | Total budget for composing a wake briefing before falling back |
| `wake_briefing.composer_prompt` | (see seeded default) | string | System prompt for the wake-briefing composer LLM |

Internal helper agents (`filler-agent`, `rewrite-agent`,
`mediation`, `notification-dispatcher`, `cancel-speech`,
`language-detect`, `sanitize`, `alarm-monitor`)
participate in the pipeline but are not user-routable
intent targets and are not listed in the dashboard's intent-routing
configuration.

### Calendar Settings

| Key | Default | Type | Description |
|-----|---------|------|-------------|
| `calendar.reminder_injection.enabled` | `true` | bool | Enable proactive calendar reminder injection into orchestrator responses |
| `calendar.reminder_injection.offsets` | `[1440, 60, 15]` | json | Reminder offset markers in minutes |
| `calendar.reminder_injection.lookahead_hours` | `24` | int | How many hours ahead to look for upcoming calendar events |

## Custom Agents

Custom agents are stored in the `custom_agents` table and registered at
runtime as A2A agents with IDs shaped as `custom-{name}`. Names supplied
through the admin API are normalized to stable lowercase slugs and cannot
include the `custom-` prefix.

Creating or updating a custom agent synchronizes the runtime stores used
by the rest of the system:

- `agent_configs` gets a matching `custom-{name}` row, so LLM calls use
  the same real configuration lookup as built-in agents. `model_override`
  becomes the config model; when it is omitted, the custom agent copies
  practical defaults from `general-agent`.
- `mcp_tools` is mirrored into `agent_mcp_tools`, which is the runtime
  source used by the MCP tool manager for both built-in and custom agents.
- `entity_visibility` is mirrored into `entity_visibility_rules` using
  the same rule types as the entity visibility API.
- `enabled=false` keeps the config row disabled and clears active MCP and
  visibility assignments. Deleting a custom agent removes the config row
  and clears those assignments.

Entity visibility for custom agents applies to entity-resolution helpers
and HA-facing action paths that a custom agent uses through AgentHub's
container runtime. LLM-only prompt text is not an entity access-control
boundary by itself, and MCP tools must still be treated as trusted
in-process capabilities scoped by their own tool behavior.

## Entity Matching Configuration

Entity matching signal weights are stored in the `entity_matching_config` table and can be adjusted from the admin dashboard:

- **Levenshtein distance** -- Fuzzy string matching
- **Jaro-Winkler similarity** -- String similarity favoring prefix matches
- **Phonetic matching** -- Soundex/Metaphone for sound-alike names
- **Embedding similarity** -- sqlite-vec vector cosine similarity
- **Alias lookup** -- Exact match from the aliases table

Aliases are managed in the `aliases` table and can be created/deleted from the admin dashboard. Example: alias "nightstand lamp" resolves to `light.bedroom_nightstand`.

## Cache Configuration

Cache thresholds and max entries are managed as SQLite settings (see Cache Settings above). Changes take effect immediately without restart.

The routing cache and action cache use separate SQLite tables. Each entry stores the SHA-256 hash key, metadata (agent ID, timestamp, hit count), and the cached decision or response.

Cache can be flushed per tier or entirely from the admin dashboard or via the API (`POST /api/admin/cache/flush`).

## Analytics

Stored traces and dashboard trace detail expose per-span time-to-first-token (`ttft_ms`) and tokens-per-second (`tps`).
The `/api/admin/analytics/tokens` endpoint aggregates average TTFT/TPS across requests, grouped by agent and provider.

## MCP Server Configuration

MCP (Model Context Protocol) servers are managed through the admin dashboard or API (`/api/admin/mcp-servers`):

- **Transport**: `stdio` (subprocess) or `sse` (HTTP/Server-Sent Events). The legacy `http` alias was removed in 0.13.0; see `VERSION.md`.
- **Command/URL**: The subprocess command or server URL
- **Environment variables**: Key-value pairs passed to the subprocess
- **Timeout**: Connection timeout in seconds (default: 30)

MCP tools are discovered automatically after connection and can be assigned to specific agents.

## Security Configuration

- **API Key**: Generated during setup, used for HA integration <-> container authentication. Stored Fernet-encrypted in the `secrets` table.
- **Admin Accounts**: Username + bcrypt-hashed password in the `admin_accounts` table. Session-based authentication for the dashboard using signed cookies.
- **HA Token**: Long-Lived Access Token stored Fernet-encrypted in the `secrets` table.
- **LLM API Keys**: Stored Fernet-encrypted in the `secrets` table.
- **Live Input Sanitization**: REST, SSE, WebSocket, and dashboard chat turns are sanitized once at ingress before they become `AgentTask` text. The sanitizer strips null/control characters, applies the configured length bound, and preserves normal entity and room names, including non-English characters such as German umlauts.
- **Prompt-Injection Detection**: Known prompt-injection patterns are detected on the sanitized live text and propagated as additive task context metadata. Detection is not a hard rejection; deterministic routing, entity resolution, cache lookup, service execution, and action verification continue to use the sanitized plain text.
- **Prompt Delimiting**: Free-form user content is wrapped with explicit user-input delimiters before it is placed in LLM prompt messages. The delimited form is only used at LLM message boundaries; cache keys, verbatim terms, entity matching, and Home Assistant service payloads use the sanitized plain text.
