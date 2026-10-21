# Deployment Guide

## Prerequisites

- **Docker Engine** 20.10+ and **Docker Compose** v2
- **Home Assistant** 2025.8.0 or later
- An LLM API key from at least one provider:
  - [OpenRouter](https://openrouter.ai/) (recommended -- access to multiple models)
  - [Groq](https://groq.com/) (fast inference)
  - [Ollama](https://ollama.com/) (local/self-hosted)
- Network connectivity between the Docker host and your Home Assistant instance

## Docker Compose Deployment

### 1. Clone the Repository

```bash
git clone https://github.com/mainzerp/ha-agenthub.git
cd ha-agenthub/container
```

### 2. Review `docker-compose.yml`

The production stack in `container/docker-compose.yml` pulls a
prebuilt image from GHCR rather than building locally:

```yaml
services:
  ha-agenthub:
    image: ghcr.io/mainzerp/ha-agenthub:${HA_AGENTHUB_TAG:-latest}
    container_name: ha-agenthub
    restart: unless-stopped
    ports:
      - "${CONTAINER_PORT:-8080}:${CONTAINER_PORT:-8080}"
    volumes:
      - ha-agenthub-data:/data
    deploy:
      resources:
        limits:
          memory: 4G
          cpus: "4.0"
        reservations:
          memory: 1G
    environment:
      - CONTAINER_HOST=${CONTAINER_HOST:-0.0.0.0}
      - CONTAINER_PORT=${CONTAINER_PORT:-8080}
      - LOG_LEVEL=${LOG_LEVEL:-INFO}
      - LOG_DIR=${LOG_DIR:-/data/logs}
      - CHROMADB_PERSIST_DIR=${CHROMADB_PERSIST_DIR:-/data/chromadb}
      - SQLITE_DB_PATH=${SQLITE_DB_PATH:-/data/agent_assist.db}
      - HF_HUB_OFFLINE=${HF_HUB_OFFLINE:-0}
      - COOKIE_SECURE=${COOKIE_SECURE:-true}
    healthcheck:
      test:
        [
          "CMD-SHELL",
          "python -c \"import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('CONTAINER_PORT','8080')+'/healthz')\"",
        ]
      interval: 30s
      timeout: 10s
      start_period: 120s
      retries: 3

volumes:
  ha-agenthub-data:
    driver: local
```

Key points:

- The image source of truth is GHCR
  (`ghcr.io/mainzerp/ha-agenthub`). CI publishes only on `v*` release
  tags (version tag plus `:latest`); pull requests and pushes to `main`
  build a `linux/amd64` image for validation without pushing it.
- Release images are multi-platform: `linux/amd64` and `linux/arm64`
  (64-bit ARM hosts). Docker pulls the matching variant automatically;
  32-bit ARM is not supported.
- `CONTAINER_PORT` (default `8080`) is the port uvicorn listens on inside
  the container; the compose port mapping, the compose healthcheck and the
  image's own `HEALTHCHECK` all follow it, so changing it in `.env` moves
  the whole stack. `CONTAINER_HOST` (default `0.0.0.0`) sets the bind
  address.
- Pin a release by setting `HA_AGENTHUB_TAG` (for example,
  `HA_AGENTHUB_TAG=1.44.2 docker compose up -d`).
- The `ha-agenthub-data` named volume persists the SQLite database,
  the Fernet key, and sqlite-vec entity-index data across container restarts.
- `CHROMADB_PERSIST_DIR` is a legacy environment variable name; it now
  points to the sqlite-vec entity-index data directory (default `/data/chromadb`).
- `start_period: 120s` accommodates the local embedding model warm-up
  and entity-index priming on first start.
- `HF_HUB_OFFLINE=1` disables Hugging Face network calls so the embedding model loads strictly from local weights. The default embedding model (`intfloat/multilingual-e5-base`) is baked into the image at build time and additionally cached in the `/data` volume, which makes the image roughly 1.1 GB larger. The compose default is `0` (online). Set to `1` for air-gapped installs.
- The speaker-identification model (sherpa-onnx CAM++, `3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx`, about 30 MB) is baked into the image at `/opt/models/speaker/` with a SHA-256 check (build args `SPEAKER_MODEL_URL` / `SPEAKER_MODEL_SHA256`, kept in sync with `app/voice/model.py`). When it is missing (custom builds, dev), enabling the feature downloads it once from GitHub into `<db dir>/models/speaker/` (default `/data/models/speaker/`), verified by size and SHA-256. The Silero voice-activity detector (`silero_vad.onnx`, about 0.6 MB, build args `VAD_MODEL_URL` / `VAD_MODEL_SHA256`) is provisioned the same way; without it whole turns including silence are embedded. Air-gapped installs need the image copy. The feature uses no extra port.
- Only infrastructure environment variables are set here. All other
  configuration (HA connection, LLM keys, agent settings) is done
  through the setup wizard and stored in SQLite.

#### Local build

If you want to build the image locally instead of pulling from GHCR
(for development or air-gapped registries), use
`container/docker-compose_local.yml`. That file keeps the legacy
`agent-assist-data` volume name so you can run both stacks side by
side. The service name is `ha-agenthub` in both compose files.

```bash
docker compose -f docker-compose_local.yml up -d --build
```

The build installs Python dependencies from `container/requirements.lock`
(fully pinned, hash-checked with `pip --require-hashes`) plus the CPU-only
PyTorch pinned in `container/requirements-torch.txt` (from the PyTorch CPU
index). After editing `requirements.txt` or `requirements-torch.txt`,
regenerate the lock with `python scripts/ci.py --update-lock` (needs `uv`);
`python scripts/ci.py` fails on a stale lock.

#### Security Hardening

The production compose file applies container hardening that may cause permission errors if undocumented:

- `read_only: true` -- The root filesystem is read-only.
- `cap_drop: [ALL]` -- All Linux capabilities are dropped.
- `cap_add: [CHOWN, SETGID, SETUID]` -- Minimal capabilities for file ownership changes.
- `security_opt: [no-new-privileges:true]` -- Prevents privilege escalation.
- `tmpfs` mounts -- `/tmp` (100 MB) and `/var/tmp` (50 MB) are ephemeral in-memory filesystems.

### 3. Optional: Create `.env` File

If you need to override defaults, create a `.env` file in the `container/` directory:

```env
CONTAINER_PORT=8080
LOG_LEVEL=INFO
LOG_DIR=/data/logs
# Set to true when serving the dashboard behind HTTPS (reverse proxy or
# direct TLS). Required so the admin session cookie is sent only over
# secure connections. Leave false for plain-HTTP local development --
# enabling it on HTTP will silently break login because the browser
# drops the cookie.
COOKIE_SECURE=false

# Comma-separated list of allowed CORS origins (e.g., "https://ha-agenthub.example.com")
# CORS_ORIGINS=

# Comma-separated list of trusted proxy IPs or CIDR networks for correct client-IP extraction behind a reverse proxy
# TRUSTED_PROXIES=

# Path to Fernet encryption key (default: /data/.fernet_key)
# FERNET_KEY_PATH=/data/.fernet_key

# CSP frame-ancestors value; widen it to embed the dashboard in an HA iframe
# FRAME_ANCESTORS='self'

# Serve /docs, /redoc and /openapi.json (default: false)
# ENABLE_API_DOCS=false
```

### 4. Start the Container

```bash
docker compose up -d
```

Verify the container is running:

```bash
docker compose logs -f ha-agenthub
```

The unauthenticated liveness probe is available at `http://<host>:8080/healthz` (`/api/health` also exists but requires the API key).

> Note: `docker-compose_local.yml` uses a simpler `CMD` healthcheck style, and the `Dockerfile` defines its own `HEALTHCHECK` with `start-period: 15s` (vs 120s in compose). These differences are normal and do not affect operation.

## First-Launch Setup Wizard

On first launch, all routes redirect to the setup wizard at `http://<host>:8080/setup/`.

### Step 1: Admin Password

Create the admin account used to access the dashboard. The password is stored as a bcrypt hash in SQLite.

### Step 2: Home Assistant Connection

Enter your Home Assistant URL and a Long-Lived Access Token:

- **HA URL**: The URL of your HA instance as reachable from the container (e.g., `http://192.168.1.100:8123` or `http://homeassistant.local:8123`).
- **HA Token**: Generate a Long-Lived Access Token in HA under Profile > Security > Long-Lived Access Tokens.

Use the "Test Connection" button to verify connectivity before proceeding. The token is stored encrypted (Fernet) in SQLite.

### Step 3: Container API Key

An API key is auto-generated for securing communication between the HA integration and the container. Copy and save this key -- it is shown only once. The key is stored encrypted in SQLite.

### Step 4: LLM Provider Configuration

Enter API keys for one or more LLM providers:

- **OpenRouter API Key** -- For access to GPT-4o-mini, Claude, and other models via a unified API.
- **Groq API Key** -- For fast inference with Llama models (used by default for the orchestrator).
- **Ollama URL** -- For local model inference (e.g., `http://localhost:11434`).
- **Custom OpenAI-compatible endpoint** -- Base URL and API key for any OpenAI-compatible provider.

Cerebras and Anthropic keys are added later under Settings > LLM in the dashboard.

> **Recommended models:**
> - **Routable agents** (orchestrator, light-agent, climate-agent, etc.): `openai/gpt-oss-120b` with `reasoning_effort` set to `Low`.
> - **Filler and rewrite agents**: `llama-3.1-8b-instant` (fast, low-cost).

Use the "Test" button for each provider to verify the key works: it lists the provider's models (no tokens are used) and reports how many are available or why the check failed. Keys are stored encrypted in SQLite.

### Step 5: Review and Complete

Review your configuration and complete the setup. The container initializes all components (entity index, cache, agents) and redirects to the admin dashboard.

## Home Assistant Integration Installation

### Method 1: HACS (Recommended)

1. Install [HACS](https://hacs.xyz/) in your Home Assistant instance if not already installed.
2. In HACS, go to Integrations > three-dot menu > Custom repositories.
3. Add the repository URL: `https://github.com/mainzerp/ha-agenthub`
4. Category: Integration
5. Click "Add", then find "HA-AgentHub" in HACS and install it.
6. Restart Home Assistant.

### Method 2: Manual Installation

1. Copy the `custom_components/ha_agenthub/` directory to your Home Assistant `config/custom_components/` directory.
2. Restart Home Assistant.

### Configure the Integration

1. In Home Assistant, go to Settings > Devices & Services > Add Integration.
2. Search for "HA-AgentHub".
3. Enter the container URL (e.g., `http://<docker-host>:8080`) and the API key from setup step 3.
4. The integration registers as a conversation agent. You can select it as the default assistant in Settings > Voice Assistants.
5. Optional, speaker identification: set **Speaker identification STT entity** (`stt_entity_id`) in the integration options and select the resulting **HA-AgentHub STT** entity as speech-to-text in the Assist pipeline. Requires a bridge version that ships `stt.py` (update through HACS together with the container). See [User Guide -- Voice Profiles](user-guide.md#voice-profiles).

On setup (HA start, reload) the integration makes one authenticated
`GET /api/health` call (10 s timeout). An unreachable or unhealthy
container leaves the entry in "Setup failed, will retry" and HA retries
with backoff; a rejected API key starts re-authentication.

#### Change settings, connection, and credentials

The integration entry (Settings > Devices & Services > HA-AgentHub) has three dialogs:

- **Configure** (options): name, response timeout, log shipping, and the
  speaker identification STT entity. Saving reloads the entry; the container
  is not contacted.
- **Reconfigure** (three-dot menu): container URL and API key. Both are
  validated against `GET /api/health` before they are saved. Leaving the API
  key **blank** keeps the stored key. A URL that another HA-AgentHub entry
  already uses is refused.
- **Re-authenticate**: when the container rejects the API key (HTTP
  `401`/`403`) at setup or at runtime (conversation turn, AI Task, or the
  speaker identification audio tee), HA shows a re-authentication
  notification under Settings > Devices & Services and in Repairs. Open it and
  enter the current key from the container dashboard; the URL stays
  unchanged. Only one re-authentication is started per failure episode.

Earlier bridge versions edited URL and API key in **Configure**; existing
entries keep their stored values and need no action.

#### Diagnostics

Use **Download diagnostics** in the entry's three-dot menu when reporting a
bridge problem. The file contains the integration version, entry data and
options with the API key and URL redacted, the container scheme, host, and
port only, the last connection status (`ok` or `auth_failed`), log-shipping
drop counters, and the entry's entities.

#### Legacy entry-title migration

Existing installations that were originally added under the old
name "Agent Assist" are renamed to "HA-AgentHub" automatically by
`custom_components/ha_agenthub/__init__.py` on the first load after
upgrade. No manual action is required, and HACS users will not see
duplicate entries.

### Lovelace Activity Card

The Agent Activity Orb (the live agent activity view of the dashboard overview)
is also available as a Home Assistant Lovelace card. It is delivered by the
bridge and covered by Home Assistant's own login: the container's dashboard
session cookie does not reach a Home Assistant page, so the browser never talks
to the container directly.

How it works:

- The integration serves the card module at
  `/ha_agenthub_static/agenthub-activity-orb-card.js` and loads it on every
  dashboard (extra frontend module). No dashboard resource has to be added by
  hand, only a Home Assistant restart after installing or updating the
  integration.
- While at least one card is open, the bridge keeps one SSE connection per
  config entry to the container's `GET /api/activity/stream` and relays the
  events over Home Assistant's WebSocket API. The command
  `ha_agenthub/activity/subscribe` is available to every logged-in Home
  Assistant user (not admin-only): it carries span ids, agent ids, timing and
  status only, never conversation text, entity names or credentials. The API
  key stays in the bridge. The connection closes when the last card is
  closed or the entry unloads, and reconnects with backoff (2 s doubling to
  60 s) after a failure.
- With several HA-AgentHub entries the card uses the first loaded entry. The
  optional `entry_id` field of the WebSocket command selects another one.

Add the card from the card picker: edit the dashboard, choose **Add card**,
search for **Agent Activity Orb** and select it. The picker shows a live preview
that plays simulated activity (it never connects to the bridge). Adding the card
opens the visual editor with these fields; every field is optional:

| Field | YAML key | Meaning |
|-------|----------|---------|
| Show title | `show_title` | Turn off to hide the heading row completely (default on) |
| Title | `title` | Heading text, default `Agent Activity`; an empty title hides it as well |
| Height | `height` | `320` or `320px`, or a CSS length such as `40vh` or `50%`; empty = automatic. Minimum about 160 px, maximum 4000 px |
| Width | `width` | `420` or `80%`; the card gets this as a maximum width and is centred in its cell; empty = full cell width |
| Transparent background | `transparent` | Remove the dark stage and the card background so the orb floats on the dashboard; it adapts to light and dark surfaces |
| Stage background | `background` | Optional CSS colour for the stage fill, for example `rgba(0,0,0,0.4)` or `var(--card-background-color)`; wins over the dark default, and over Transparent for the stage |
| Compact | `compact` | Smaller card without the extra spacing |
| Demo mode | `demo` | Play simulated activity without a container |
| Config entry ID | `entry_id` | Only for several HA-AgentHub entries; default is the first loaded entry |

Size rules: an explicit `height` fixes the stage height (and, in the sections
view, the card asks for the matching number of grid rows). Without it, a card in
a sections-view grid cell fills the cell, so the usual resize handles work.
Outside a fixed cell the height follows the width (16:10, square below about
480 px). Percentage heights need a container with a fixed height, such as a
grid cell. Very small stages drop the agent labels (names stay in the tooltip).

The same options in YAML (Edit in YAML or a manual card):

```yaml
type: custom:agenthub-activity-orb-card
title: Agents          # optional, default "Agent Activity"
show_title: true       # optional, false hides the heading row
height: 320px          # optional, number (px) or CSS length; empty = automatic
width: 560             # optional, max-width in px or CSS length, centred
transparent: false     # optional, true = no stage fill and no card chrome
background: ''         # optional, CSS colour for the stage fill (wins over transparent for the stage)
compact: false         # optional
demo: false            # optional, simulated activity without a container
```

Hidden title:

```yaml
type: custom:agenthub-activity-orb-card
show_title: false
```

Fixed size, centred in its cell:

```yaml
type: custom:agenthub-activity-orb-card
height: 240
width: 420
```

Transparent card that floats on the dashboard background (works on light and
dark themes; over a photographic background pick a solid `background` for the
best legibility):

```yaml
type: custom:agenthub-activity-orb-card
transparent: true
```

Several cards on one dashboard share one geometry cache and render at 30 fps
each; a card that is scrolled out of view or on a hidden tab stops rendering.

Requirements:

- A container release that provides `GET /api/activity/stream` and
  `GET /api/activity/recent` (the release that introduced the activity orb on
  the dashboard overview). An older container answers `404` and the card stays
  offline.
- The API key already configured for the integration; no extra permissions.

Dashboards in YAML mode load the module automatically as well. Only if you
manage dashboard resources explicitly, add it under `resources`:

```yaml
resources:
  - url: /ha_agenthub_static/agenthub-activity-orb-card.js
    type: module
```

Troubleshooting (card stays "offline"):

1. Check the container URL and API key in Settings > Devices & Services >
   HA-AgentHub; a rejected key starts re-authentication, and the container must
   be reachable from the Home Assistant host.
2. Look at the Home Assistant log for `custom_components.ha_agenthub.activity`:
   one warning per failure kind (`rejected the API key`, `HTTP 404 ... container
   too old`, `unavailable`); repeats are logged at debug level.
3. Open the browser console. `Custom element doesn't exist:
   agenthub-activity-orb-card` (or "not found") means this browser page has not
   loaded the new module, usually a stale page after an update: restart Home
   Assistant after updating the integration, then hard-reload the page (or
   restart the Companion app). Verify the URL
   `/ha_agenthub_static/agenthub-activity-orb-card.js` returns JavaScript, and
   that `customElements.get('agenthub-activity-orb-card')` is defined in the
   console. The card definition is deferred until the Home Assistant frontend
   has booted (its custom-element registry is replaced during boot, and an
   earlier definition would stay invisible to Lovelace); if that boot signal
   never arrives the module defines the card after a 10 s fallback. A
   `not_loaded` WebSocket error means the HA-AgentHub entry is not loaded (setup
   failed or retrying). After 5 s offline the card shows "Waiting for
   HA-AgentHub" inside the stage.
4. After reloading the integration entry, reload the dashboard page so the card
   subscribes again.

## Open WebUI

[Open WebUI](https://openwebui.com/) can chat with HA-AgentHub (and control the
house) through the OpenAI-compatible API (`/v1`, see
[API Reference](api-reference.md#openai-compatible-api)).

1. In Open WebUI go to **Admin > Settings > Connections > OpenAI API** and add a connection:
   - URL: `http://<docker-host>:8080/v1`
   - API key: the container API key from setup step 3
   - Model: `ha-agenthub` (listed by `GET /v1/models`)
2. Set `ENABLE_FORWARD_USER_INFO_HEADERS=true` on the Open WebUI container so it
   sends the `X-OpenWebUI-User-*` headers and, from Open WebUI 0.6.17,
   `X-OpenWebUI-Chat-Id` (server-side conversation history per chat).
3. Send Open WebUI background tasks (title, tags, follow-up, search-query and
   autocomplete generation) to a separate model. Open WebUI uses the chat model
   for them unless a task model is set, and their prompts contain the user's
   command.
   - Recommended: add a second Open WebUI connection to a regular LLM provider
     (or a local Ollama) and select a small, fast model under
     **Admin > Settings > Interface > Task Model > External Models**
     (`TASK_MODEL_EXTERNAL`). Titles, tags and follow-up suggestions then work
     normally and HA-AgentHub only receives real chat turns. The task model
     sees the chat history, including HA-AgentHub replies; use a local model
     if that must stay on-premises.
   - Safety net (Open WebUI 0.10.0 or newer): add the connection header JSON
     `{"X-OpenWebUI-Task": "{{TASK}}"}` to the HA-AgentHub connection; if a
     task request still arrives, HA-AgentHub answers it with a stub (placeholder
     title, empty tags and follow-ups) and never dispatches it.

   Without either, HA-AgentHub stubs only prompts that start with `### Task:`,
   which covers Open WebUI's default title, tags, follow-up, search-query and
   autocomplete templates. Other background requests (for example emoji
   generation in voice-call mode, default-mode tool calling, or customized
   task templates) are then treated as chat turns and can execute commands.
   For full protection set the `X-OpenWebUI-Task` header or a separate task
   model, and do not attach Open WebUI tools to the `ha-agenthub` model.
4. Map Open WebUI users to Home Assistant users on the dashboard **Persons**
   page (see [User Guide -- Persons](user-guide.md#persons)). Unmapped users can
   chat but run without a Home Assistant user id.

Limitation: without `X-OpenWebUI-Chat-Id` (Open WebUI older than 0.6.17, or
user info forwarding disabled) every turn gets a fresh conversation id and runs
without server-side history: follow-up answers and clarifications are not
linked to the previous turn. Enable step 2 for multi-turn chats.

## Networking

### Container-to-HA Connectivity

The container must be able to reach your Home Assistant instance over HTTP. Common configurations:

- **Same host**: Use `http://host.docker.internal:8123` (Docker Desktop) or `http://172.17.0.1:8123` (Linux Docker with default bridge).
- **Same network**: Use the HA machine's LAN IP (e.g., `http://192.168.1.100:8123`).
- **Docker network**: If HA runs in Docker on the same host, use a shared Docker network and reference the HA container name.

### HA-to-Container Connectivity

Home Assistant must be able to reach the container on the configured port (default: 8080). If running on the same host, use `http://localhost:8080`. If on a different host, use the Docker host's IP.

### Reverse Proxy

If placing the container behind a reverse proxy (e.g., Nginx, Caddy, Traefik):

- Proxy to `http://localhost:8080`
- WebSocket support is required for streaming (`/ws/conversation`) and speaker identification (`/ws/voice`)
- Forward the `Authorization` header for API key authentication

## Updating

For the production GHCR-based stack:

```bash
cd ha-agenthub/container
docker compose pull
docker compose up -d
```

Pin a release by exporting `HA_AGENTHUB_TAG` before pulling (for
example `HA_AGENTHUB_TAG=1.44.2`).

For the local-build stack (`docker-compose_local.yml`):

```bash
cd ha-agenthub/container
git pull
docker compose -f docker-compose_local.yml up -d --build
```

Database migrations run automatically on startup. The schema uses `CREATE TABLE IF NOT EXISTS` and `INSERT OR IGNORE` for idempotent initialization.

## Backup

### SQLite Database

The SQLite database contains all configuration, secrets, conversation history, and analytics. Back up the file at the configured `SQLITE_DB_PATH` (default: `/data/agent_assist.db` inside the container, mapped to the `ha-agenthub-data` Docker volume).

To back up from the volume:

```bash
docker cp ha-agenthub:/data/agent_assist.db ./backup_agent_assist.db
```

### sqlite-vec Entity-Index Data

sqlite-vec entity-index data is stored at `CHROMADB_PERSIST_DIR` (default: `/data/chromadb`). Back up this directory for faster restarts (avoids re-indexing entities).

```bash
docker cp ha-agenthub:/data/chromadb ./backup_chromadb
```

The entity index can be rebuilt from scratch if the sqlite-vec data is lost, but backing it up avoids a cold start. Note that the same directory also holds `cache.db` (routing + action cache); losing it loses all cache entries -- use the cache export/import endpoints (see [Backup and Restore](backup-restore.md)) if cache contents matter.

### Restore

To restore from backup, stop the container, copy the files back into the volume, and restart:

```bash
docker compose down
docker cp ./backup_agent_assist.db ha-agenthub:/data/agent_assist.db
docker cp ./backup_chromadb ha-agenthub:/data/chromadb
docker compose up -d
```

For more detail, including cache export/import as a complementary
recovery path, see [Backup and Restore](backup-restore.md).

## Production Deployment with HTTPS

For production use, you should run HA-AgentHub behind a reverse proxy with TLS termination.

### Nginx Example

```nginx
server {
    listen 443 ssl;
    server_name ha-agenthub.example.com;

    ssl_certificate /etc/ssl/certs/your-cert.pem;
    ssl_certificate_key /etc/ssl/private/your-key.pem;

    location / {
        proxy_pass http://localhost:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /ws/ {
        proxy_pass http://localhost:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 86400;
        proxy_send_timeout 86400;
    }
}
```

### Enable Secure Cookies

When running behind HTTPS, set `COOKIE_SECURE=true`. Both compose stacks substitute the variable from `.env` (`docker-compose_local.yml` defaults to `false`, `docker-compose.yml` defaults to `true`).
