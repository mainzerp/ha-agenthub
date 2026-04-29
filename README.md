# HA-AgentHub

A multi-agent AI assistant for Home Assistant with container-based A2A orchestration, two-tier vector caching, hybrid entity matching, MCP tool integration, and a plugin system.

![Test](https://github.com/mainzerp/ha-agenthub/actions/workflows/test.yml/badge.svg?branch=main)
![Lint](https://github.com/mainzerp/ha-agenthub/actions/workflows/lint.yml/badge.svg?branch=main)
![Docker Build](https://github.com/mainzerp/ha-agenthub/actions/workflows/docker-build.yml/badge.svg?branch=main)
![HACS Validation](https://github.com/mainzerp/ha-agenthub/actions/workflows/hacs-validation.yml/badge.svg?branch=main)

## Features

- **Multi-agent orchestration** -- Specialized agents for lights, music, climate, media, timers, scenes, automation, security, a general assistant, and `send` (delivery to phones, satellites, and notification targets), coordinated by a central orchestrator via the A2A protocol
- **A2A protocol** -- JSON-RPC 2.0-based agent-to-agent communication with registry, dispatcher, and in-process transport
- **Two-tier vector cache** -- Routing cache (skip intent classification) and action cache, formerly response cache (skip entire agent pipeline) using ChromaDB embeddings with configurable similarity thresholds
- **Cache backup and restore** -- Export and import the routing and action caches as a portable JSON envelope via `/api/admin/cache/export` and `/api/admin/cache/import` (added in 0.20.0)
- **Hybrid entity matching** -- Five-signal weighted matcher (Levenshtein, Jaro-Winkler, phonetic, embedding similarity, alias lookup) with LLM disambiguation fallback
- **MCP tool integration** -- Connect external tool servers via Model Context Protocol (stdio and SSE transports) and assign tools to agents
- **Plugin system** -- Extend functionality with Python plugins that inspect registered agents, dispatch work back through the orchestrator, add routes, subscribe to events, and access settings/MCP integrations
- **Admin dashboard** -- HTMX-powered admin dashboard for managing agents, entities, cache, MCP servers, analytics, traces, and plugins
- **Custom agents** -- Create LLM-powered agents via the dashboard with custom system prompts, model selection, MCP tools, and intent patterns
- **Rewrite agent** -- Optional response variation for cached responses (driven by the personality prompt) to avoid repetitive answers
- **Setup wizard** -- Guided 5-step first-launch configuration (admin account, HA connection, API key, LLM providers, review)
- **Analytics and tracing** -- Request counts, cache hit rates, latency tracking, token usage, and per-request trace span Gantt visualization, with per-turn tracing on `/ws/conversation` (added in 0.20.1)
- **Voice experience** -- Filler / interim TTS, voice-followup, cancel-intent ("never mind"), and repeat-turn coalescing in the HA integration
- **Real-pipeline scenario tests** -- YAML-driven end-to-end test framework under `container/tests/scenarios/` exercising the full orchestrator pipeline against a curated HA snapshot

## Architecture

HA-AgentHub (repository: `ha-agenthub`) runs as a Docker container with a FastAPI backend. A Home Assistant custom integration (`custom_components/ha_agenthub/`) bridges turns to the container, streams responses back, and honors a small set of container-directed bridge actions such as native plain-timer delegation.

All configuration, secrets, and state are stored in SQLite. ChromaDB provides vector storage for entity embeddings and cache embeddings. No configuration files are used at runtime.

See [docs/architecture.md](docs/architecture.md) for component diagrams, request flow, and detailed design.

## Quick Start

### Prerequisites

- Docker Engine 20.10+ and Docker Compose v2
- A running Home Assistant instance (2024.1.0+)
- An LLM API key (OpenRouter, Groq, or Ollama)

### 1. Clone and Start

```bash
git clone https://github.com/mainzerp/ha-agenthub.git
cd ha-agenthub/container
docker compose up -d
```

The production `container/docker-compose.yml` pulls the prebuilt
image from `ghcr.io/mainzerp/ha-agenthub:${HA_AGENTHUB_TAG:-main}`.
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

## Documentation

- [Deployment Guide](docs/deployment.md) -- Docker setup, setup wizard, HA integration, networking, backup
- [Configuration Reference](docs/configuration.md) -- Environment variables, SQLite settings, agent config
- [Architecture Overview](docs/architecture.md) -- Components, A2A protocol, request flow, cache, entity matching
- [API Reference](docs/api-reference.md) -- All REST, SSE, and WebSocket endpoints
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
CI uses (`v0.15.11`), so passing the hook locally guarantees a green
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
custom_components/  HA custom integration
  ha_agenthub/      HA bridge and native plain-timer delegation seam
```

## Plugin Development

Plugins extend HA-AgentHub without modifying core code. Create a `.py` file in `container/plugins/`, subclass `BasePlugin`, and implement lifecycle hooks.

See [docs/plugin-development.md](docs/plugin-development.md) for the full guide.

## License

MIT
