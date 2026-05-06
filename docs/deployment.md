# Deployment Guide

## Prerequisites

- **Docker Engine** 20.10+ and **Docker Compose** v2
- **Home Assistant** 2024.1.0 or later
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
      - CHROMADB_PERSIST_DIR=${CHROMADB_PERSIST_DIR:-/data/chromadb}
      - SQLITE_DB_PATH=${SQLITE_DB_PATH:-/data/agent_assist.db}
      - HF_HUB_OFFLINE=${HF_HUB_OFFLINE:-0}
    healthcheck:
      test:
        [
          "CMD-SHELL",
          "python -c \"import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('CONTAINER_PORT','8080')+'/api/health')\"",
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
  (`ghcr.io/mainzerp/ha-agenthub`); CI publishes `:latest` on every
  push to `main`.
- Pin a release by setting `HA_AGENTHUB_TAG` (for example,
  `HA_AGENTHUB_TAG=1.19.4 docker compose up -d`).
- The `ha-agenthub-data` named volume persists the SQLite database,
  the Fernet key, and ChromaDB across container restarts.
- `start_period: 120s` accommodates the local embedding model warm-up
  and entity-index priming on first start.
- `HF_HUB_OFFLINE=1` disables Hugging Face network calls so the embedding model loads strictly from cached weights baked into the image. The compose default is `0` (online). Set to `1` for air-gapped installs.
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
# Set to true when serving the dashboard behind HTTPS (reverse proxy or
# direct TLS). Required so the admin session cookie is sent only over
# secure connections. Leave false for plain-HTTP local development --
# enabling it on HTTP will silently break login because the browser
# drops the cookie.
COOKIE_SECURE=false

# Comma-separated list of allowed CORS origins (e.g., "https://ha-agenthub.example.com")
# CORS_ORIGINS=

# Comma-separated list of trusted proxy IPs for correct client-IP extraction behind a reverse proxy
# TRUSTED_PROXIES=

# Path to Fernet encryption key (default: /data/.fernet_key)
# FERNET_KEY_PATH=/data/.fernet_key
```

### 4. Start the Container

```bash
docker compose up -d
```

Verify the container is running:

```bash
docker compose logs -f ha-agenthub
```

The health check endpoint is available at `http://<host>:8080/api/health`.

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

> **Recommended models:**
> - All agents: `openai/gpt-oss-20b` with reasoning effort set to `Low`.
> - Filler and rewrite agents: `llama-3.1-8b-instant` (fast, low-cost).

Use the "Test" button for each provider to verify the key works. Keys are stored encrypted in SQLite.

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

The options flow re-uses the same form. Leaving the API key field
**blank** in the options dialog keeps the previously stored key; only
enter a value when you want to replace it.

#### Legacy entry-title migration

Existing installations that were originally added under the old
name "Agent Assist" are renamed to "HA-AgentHub" automatically by
`custom_components/ha_agenthub/__init__.py` on the first load after
upgrade. No manual action is required, and HACS users will not see
duplicate entries.

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
- WebSocket support is required for streaming (`/ws/conversation`)
- Forward the `Authorization` header for API key authentication

## Updating

For the production GHCR-based stack:

```bash
cd ha-agenthub/container
docker compose pull
docker compose up -d
```

Pin a release by exporting `HA_AGENTHUB_TAG` before pulling (for
example `HA_AGENTHUB_TAG=1.19.4`).

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

### ChromaDB Data

ChromaDB vector data is stored at `CHROMADB_PERSIST_DIR` (default: `/data/chromadb`). Back up this directory for faster restarts (avoids re-indexing entities).

```bash
docker cp ha-agenthub:/data/chromadb ./backup_chromadb
```

The entity index and cache can be rebuilt from scratch if the ChromaDB data is lost, but backing it up avoids a cold start.

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

When running behind HTTPS, set `COOKIE_SECURE=true`. In the local-build stack (`docker-compose_local.yml`) you can set this in `.env`. In the production stack (`docker-compose.yml`) you must edit the compose file directly because the variable is not substituted.
