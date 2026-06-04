# HA-AgentHub

<p align="center">
  <img src="./custom_components/ha_agenthub/brand/logo.png" alt="HA-AgentHub Logo" width="200">
</p>

A multi-agent AI assistant for Home Assistant with container-based A2A orchestration, two-tier caching, hybrid entity matching, MCP tool integration, and a plugin system.

![Test](https://img.shields.io/github/actions/workflow/status/mainzerp/ha-agenthub/ci.yml?branch=main&job=quality&label=Test&logo=github)
![Lint](https://img.shields.io/github/actions/workflow/status/mainzerp/ha-agenthub/ci.yml?branch=main&job=quality&label=Lint&logo=github)
![Docker Build](https://img.shields.io/github/actions/workflow/status/mainzerp/ha-agenthub/ci.yml?branch=main&job=docker&label=Docker%20Build&logo=github)
![Release](https://img.shields.io/github/actions/workflow/status/mainzerp/ha-agenthub/ci.yml?branch=main&job=release&label=Release&logo=github)

## Features

- **Multi-agent orchestration** -- 15+ specialized domain agents coordinated by a central orchestrator via the A2A protocol
- **A2A protocol** -- JSON-RPC 2.0-based agent-to-agent communication with registry, dispatcher, and in-process transport
- **Two-tier cache** -- Routing cache (skip intent classification) and action cache, formerly response cache (skip entire agent pipeline) using SQLite-backed SHA-256 exact hash matching
- **Cache backup and restore** -- Export and import the routing and action caches as a portable JSON envelope via `/api/admin/cache/export` and `/api/admin/cache/import`
- **Hybrid entity matching** -- Five-signal weighted matcher (Levenshtein, Jaro-Winkler, phonetic, embedding similarity, alias lookup) with LLM disambiguation fallback. Agents can explicitly declare extracted entities via `@entities:` lines in their replies, replacing legacy regex heuristics with structured LLM-driven entity extraction
- **MCP tool integration** -- Connect external tool servers via Model Context Protocol (stdio and SSE transports) and assign tools to agents
- **Plugin system** -- Extend functionality with Python plugins that inspect registered agents, dispatch work back through the orchestrator, add routes, subscribe to events, and access settings/MCP integrations
- **Admin dashboard** -- HTMX-powered admin dashboard for managing agents, entities, cache, MCP servers, analytics, traces, and plugins
- **Custom agents** -- Create LLM-powered agents via the dashboard with custom system prompts, model selection, MCP tools, and intent patterns
- **Rewrite agent** -- Optional response variation for cached responses (driven by the personality prompt) to avoid repetitive answers
- **Setup wizard** -- Guided 5-step first-launch configuration (admin account, HA connection, API key, LLM providers, review)
- **Analytics and tracing** -- Request counts, cache hit rates, latency tracking, token usage, and per-request trace span Gantt visualization, with per-turn tracing on `/ws/conversation`
- **Voice experience** -- Filler / interim TTS, voice-followup, cancel-intent ("never mind"), and repeat-turn coalescing in the HA integration
- **Mediation streaming** -- Begins TTS playback earlier by streaming mediated response tokens before the full reply is finalized
- **Real-pipeline scenario tests** -- YAML-driven end-to-end test framework under `container/tests/scenarios/` exercising the full orchestrator pipeline against a curated HA snapshot
- **Remote Logs API** -- In-memory ring-buffer log inspection with runtime level adjustment via admin endpoints
- **Cache Management UI Enhancements** -- Per-entry cache deletion from the admin dashboard

## Agents

### Domain Agents (control HA entities)

| Agent | HA Domains | Capabilities |
|-------|-----------|-------------|
| **Light Agent** | `light`, `switch`, `sensor` (illuminance) | On/off, toggle, brightness, color, color temperature |
| **Climate Agent** | `climate`, `fan`, `humidifier`, `sensor`, `weather` | Temperature, HVAC mode, fan speed, humidity, weather queries |
| **Media Agent** | `media_player` | Playback, volume, source selection |
| **Music Agent** | `media_player` | Music-focused playback (radio, playlists) |
| **Cover Agent** | `cover` | Open, close, stop, set position, tilt control |
| **Vacuum Agent** | `vacuum` | Start, pause, stop, return to base, clean spot, set fan speed |
| **Scene Agent** | `scene` | Scene activation |
| **Timer Agent** | `timer`, `persistent_notification` | Timers, alarms, reminders |
| **Automation Agent** | `automation` | Trigger, enable, disable, and query automations |
| **Security Agent** | `alarm_control_panel`, `lock`, `camera`, `sensor`, `binary_sensor` | Arm/disarm, lock/unlock, camera status |
| **Calendar Agent** | `calendar` | Query events, add reminders |
| **Lists Agent** | `todo` | Shopping and todo list management |
| **Send Agent** | — | Deliver content to phones, satellites, and notification targets |

### Infrastructure Agents

| Agent | Purpose |
|-------|---------|
| **Orchestrator** | Intent classification, agent routing, mediation, and sequential dispatch |
| **General Agent** | Fallback for general questions, web search, and unroutable requests |
| **Rewrite Agent** | Response variation for cached hits to avoid repetitive answers |
| **Filler Agent** | Generate interim TTS filler phrases while agents compute |
| **Cancel Speech** | LLM-generated acknowledgement for dismiss intents ("never mind") |
| **Wake Briefing Composer** | Compose spoken morning briefings for internal alarms |
| **Alarm Monitor** | Monitor internal alarms and trigger wake briefings |

### MCP Tools

Tools are external capabilities connected via the Model Context Protocol (MCP) and can be assigned to any agent.

| Tool | Transport | Description |
|------|-----------|-------------|
| **Wikipedia Search** | stdio | Search Wikipedia articles (`wikipedia_search`) and retrieve summaries (`wikipedia_summary`) |
| **DuckDuckGo Search** | stdio | Web search via DuckDuckGo |

Custom MCP servers can be added through the admin dashboard (stdio or SSE transports).

## Architecture

HA-AgentHub (repository: `ha-agenthub`) runs as a Docker container with a FastAPI backend. A Home Assistant custom integration (`custom_components/ha_agenthub/`) bridges turns to the container, streams responses back, and honors a small set of container-directed bridge actions such as native plain-timer delegation.

All configuration, secrets, and state are stored in SQLite. ChromaDB provides vector storage for entity embeddings; the routing and action caches are stored in SQLite with SHA-256 exact hash matching. No configuration files are used at runtime.

See [docs/architecture.md](docs/architecture.md) for component diagrams, request flow, and detailed design.

## Quick Start

### Prerequisites

- Docker Engine 20.10+ and Docker Compose v2
- A running Home Assistant instance (2025.1.0+)
- An LLM API key (OpenRouter, Groq, Cerebras, or Ollama)

### 1. Clone and Start

```bash
git clone https://github.com/mainzerp/ha-agenthub.git
cd ha-agenthub/container
docker compose up -d
```

The production `container/docker-compose.yml` pulls the prebuilt
image from `ghcr.io/mainzerp/ha-agenthub:${HA_AGENTHUB_TAG:-latest}`.
For a local development build (sources from this checkout):

```bash
docker compose -f docker-compose_local.yml up -d --build
```

### 2. Run the Setup Wizard

Open `http://<docker-host>:8080/setup/` in your browser and follow the 5-step wizard:

1. Create an admin account
2. Connect to Home Assistant (URL + Long-Lived Access Token)
3. Generate a container API key (save it -- shown once)
4. Configure LLM provider(s)
5. Review and complete

### 3. Install the HA Integration

**Via HACS (recommended):**

1. In HACS, add `https://github.com/mainzerp/ha-agenthub` as a custom repository (category: Integration).
2. Install "HA-AgentHub" and restart Home Assistant.

**Manual:**

Copy `custom_components/ha_agenthub/` to your HA `config/custom_components/` directory and restart HA.

**Configure:**

In HA, go to Settings > Devices & Services > Add Integration > "HA-AgentHub". Enter the container URL and API key.

## Configuration

HA-AgentHub uses three configuration tiers:

1. **Environment variables** -- Infrastructure-only (`CONTAINER_PORT`, `LOG_LEVEL`, etc.), set in `docker-compose.yml`
2. **Setup wizard** -- One-time secrets and connections, stored encrypted in SQLite
3. **Admin dashboard** -- All runtime settings, hot-reloadable without restart

See [docs/configuration.md](docs/configuration.md) for the full reference.

> **Language setting:** For best results, set your preferred language explicitly in the admin dashboard (e.g. `de`, `fr`, `es`) rather than leaving it on `auto`. Automatic language detection can be unreliable for very short voice commands. A manual setting ensures the assistant always uses your language and resolves entity names correctly.

## Documentation

- [Deployment Guide](docs/deployment.md) -- Docker setup, setup wizard, HA integration, networking, backup
- [Configuration Reference](docs/configuration.md) -- Environment variables, SQLite settings, agent config
- [Architecture Overview](docs/architecture.md) -- Components, A2A protocol, request flow, cache, entity matching
- [API Reference](docs/api-reference.md) -- All REST, SSE, and WebSocket endpoints covering conversation, admin settings, agents, custom agents, MCP servers, entity index/visibility, cache, calendar, timers, send devices, analytics, traces, logs, plugins, and setup wizard
- [Backup and Restore](docs/backup-restore.md) -- Volume backup, Fernet key export, cache export/import
- [Plugin Development](docs/plugin-development.md) -- Writing plugins, lifecycle hooks, event bus
- [Troubleshooting](docs/troubleshooting.md) -- Common issues and solutions
- [UI Style Guide](docs/style-guide.md) -- Design tokens, component classes, and dashboard conventions

## Development

### Run Tests

```bash
cd container
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

Fast inner loop without integration-marked tests:

```bash
cd container
python -m pytest tests/ -q -m "not integration"
```

Local parallel run when dev dependencies are installed:

```bash
cd container
python -m pytest tests/ -n auto -q --tb=short
```

`pytest-xdist` is declared in `container/requirements-dev.txt`, but `-n auto` only works in environments where the dev requirements have actually been installed.

### Lint

```bash
cd container
ruff check .
ruff format --check .
```

### Pre-commit hook

Run ruff check + format automatically on every `git commit`:

```bash
pip install pre-commit
pre-commit install
```

Hooks are scoped to `container/` and pinned to the same ruff version
CI uses (`v0.15.15`), so passing the hook locally guarantees a green
`Lint` workflow.

### Project Structure

```text
container/          Docker container (FastAPI backend)
  app/              Application code
    a2a/            A2A protocol (registry, dispatcher, transport)
    agents/         Specialized agents + orchestrator
    analytics/      Analytics aggregation and queries
    api/routes/     REST/SSE/WebSocket endpoints
    cache/          Two-tier vector cache (routing + action)
    dashboard/      Admin dashboard (HTMX + Jinja2 templates)
    db/             SQLite schema + repository
    entity/         Hybrid entity matcher
    ha_client/      Home Assistant REST + WebSocket client
    llm/            LLM client (litellm)
    mcp/            MCP tool integration
    middleware/     Auth + tracing middleware
    models/         Pydantic models
    plugins/        Plugin system
    prompts/        System prompts for orchestrator and domain agents
    security/       Encryption, hashing, sanitization
    setup/          Setup wizard
    util/           Shared helpers
  plugins/          User plugins directory
  tests/            Test suite (incl. real-pipeline scenarios)
    scenarios/        YAML-driven real-pipeline end-to-end tests
    data/             Test fixtures and curated HA snapshots
custom_components/  HA custom integration
  ha_agenthub/      HA bridge and native plain-timer delegation seam
    brand/            Brand icons and images for HACS
    translations/     UI translation files (en.json, de.json, etc.)
```

## Plugin Development

Plugins extend HA-AgentHub without modifying core code. Create a `.py` file in `container/plugins/`, subclass `BasePlugin`, and implement lifecycle hooks.

See [docs/plugin-development.md](docs/plugin-development.md) for the full guide.

## License

MIT
