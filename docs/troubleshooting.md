# Troubleshooting

## Container Won't Start

**Check Docker logs:**

```bash
docker compose logs ha-agenthub
```

**Common causes:**

- Port already in use: Change `CONTAINER_PORT` in `.env` or `docker-compose.yml`.
- Volume permission issues: Ensure the Docker volume is writable. On Linux, check that the data directory has appropriate ownership.
- Missing dependencies: Pull a fresh image with `docker compose pull && docker compose up -d`. For the local-build stack use
  `docker compose -f docker-compose_local.yml up -d --build`.
- Python import errors in logs: Indicates a corrupted build. Remove the image and pull again:
  ```bash
  docker compose down
  docker rmi ghcr.io/mainzerp/ha-agenthub:main
  docker compose pull
  docker compose up -d
  ```

## Can't Connect to Home Assistant

**Verify the HA URL is reachable from inside the container:**

```bash
docker exec ha-agenthub python -c "
import urllib.request
urllib.request.urlopen('http://<ha_url>:8123/api/')
"
```

**Common causes:**

- Wrong URL: Use the IP address or hostname reachable from the container's network, not `localhost` (unless using host networking).
- Docker networking: If HA is on the same Docker host, use `http://host.docker.internal:8123` (Docker Desktop) or `http://172.17.0.1:8123` (Linux default bridge).
- Invalid token: Generate a new Long-Lived Access Token in HA under Profile > Security.
- Firewall: Ensure port 8123 (or your HA port) is not blocked between the container and HA.

**Re-test from the setup wizard:**

Navigate to `http://<host>:8080/setup/step/2` and use the "Test Connection" button.

## LLM Errors

**Symptoms:** Agent responses contain error messages or the container logs show LLM API errors.

**Common causes:**

- Invalid API key: Re-enter the key in the setup wizard (step 4) or update via the admin dashboard.
- Provider outage: Check the provider's status page (OpenRouter, Groq, Ollama).
- Rate limiting: Reduce request frequency or switch to a different provider/model.
- Ollama not running: If using Ollama, verify the Ollama service is running and accessible at the configured URL.

**Test LLM connectivity:**

Navigate to `http://<host>:8080/setup/` and use the "Test" button for each provider on step 4.

## Entity Not Found

**Symptoms:** Commands like "turn on the bedroom light" return "entity not found" or match the wrong device.

**Steps to resolve:**

1. Check the entity index status page in the admin dashboard (Entity Index page).
2. Trigger a manual refresh: click "Refresh" on the Entity Index dashboard page or call `POST /api/admin/entity-index/refresh`.
3. Verify the entity is exposed in Home Assistant -- only entities visible through the HA REST API (`/api/states`) are indexed.
4. Add an alias: In the admin dashboard, create an alias mapping your preferred name to the exact entity ID (e.g., "bedroom light" -> `light.bedroom_main`).
5. Check entity matching weights: Adjust the signal weights on the Entity Index dashboard page if matches are consistently wrong.

## Cache Not Working

**Symptoms:** Cache hit rate is 0%, or the cache stats page shows no entries.

**Common causes:**

- ChromaDB directory not writable: Check that the volume mount for `/data/chromadb` exists and is writable.
- Embedding engine not initialized: Check container startup logs for embedding-related errors.
- Thresholds too high: Lower the routing cache threshold (default: 0.92) or action cache threshold (default: 0.95) in the admin dashboard.

**Verify ChromaDB:**

```bash
docker exec ha-agenthub ls -la /data/chromadb
```

## Setup Wizard Issues

**Symptoms:** Stuck on a step, wizard not appearing, or need to redo setup.

**Reset setup state:** Access the SQLite database and clear the setup state:

```bash
docker exec ha-agenthub python -c "
import sqlite3
conn = sqlite3.connect('/data/agent_assist.db')
conn.execute('DELETE FROM setup_state')
conn.commit()
conn.close()
"
```

Then restart the container:

```bash
docker compose restart ha-agenthub
```

## Integration Not Appearing in Home Assistant

**HACS installation:**

1. Verify the repository was added correctly in HACS (Integrations > three-dot menu > Custom repositories).
2. Check that the integration was downloaded and installed (not just added).
3. Restart Home Assistant after installation.

**Manual installation:**

1. Confirm the `custom_components/ha_agenthub/` directory exists in your HA config folder.
2. Check that all files are present: `__init__.py`, `config_flow.py`, `const.py`, `conversation.py`, `manifest.json`, `strings.json`, and `translations/en.json`.
3. Restart Home Assistant.

**After installation:**

1. Go to Settings > Devices & Services > Add Integration.
2. Search for "HA-AgentHub" (integration domain `ha_agenthub`).
3. If it does not appear, check the HA logs for import errors: Settings > System > Logs.

## Slow Responses

**Common causes:**

- LLM provider latency: Check provider response times. Groq is typically faster than OpenRouter for small models. Ollama depends on local hardware.
- Low cache hit rate: Check the cache stats in the admin dashboard. A low hit rate means most requests require LLM calls.
- Agent timeout: Increase agent timeout values in the admin dashboard (Agent Configuration).
- Entity index size: A very large entity index (10,000+ entities) may slow down entity matching. Consider using entity visibility rules to limit which entities each agent can see.

## Log Inspection

**View container logs:**

```bash
docker compose logs -f ha-agenthub
```

**Adjust log level:**

Set `LOG_LEVEL=DEBUG` in `docker-compose.yml` or `.env` and restart:

```bash
docker compose restart ha-agenthub
```

**Trace IDs:**

Each request is assigned a trace ID (visible in the logs as `[trace:...]`). Use the trace ID to find all related log entries for a single request. Traces can also be viewed in the admin dashboard (Traces page) with a Gantt visualization of each processing step.

Trace previews in the dashboard and stored trace summaries are sanitized before persistence. Secrets, bearer tokens, credentialed URLs, sensitive query parameters, tool payload details, and short verification codes may appear as redacted placeholders while safe operational fields such as entity IDs, agent IDs, actions, and counts remain visible.

**Log format:**

```
2025-01-15 10:30:00 INFO [app.agents.orchestrator] Routing to light-agent
```

Logs include timestamp, level, logger name, and message.

## Empty WebSocket Responses on HA Voice

**Symptoms:** The HA voice pipeline says nothing back after a turn,
but the dashboard "Conversations" or "Traces" page shows the turn was
processed end-to-end.

**Likely cause:** A streamed turn produced no final speech token
and the integration's defensive fallback (added in 0.18.24/0.18.25)
should have spoken the REST equivalent. Check the HA Core log for the
integration falling back to REST and whether the REST call succeeded.
If the REST fallback also returned empty speech, look at the
finalised trace on the dashboard for an empty `speech` field on the
orchestrator's terminal span.

## Per-Turn Duration Shows Whole Connection Lifetime

**Symptoms:** Every turn delivered over `/ws/conversation` shows the
same, ever-growing `total_duration_ms` on the dashboard waterfall.

**Cause:** Pre-0.20.1 containers wrote the connection-level WS
trace duration over each turn's value. Upgrade to 0.20.1+ and reload
the Traces page; cached browser state may keep showing the old
values until the page is hard-refreshed and the trace list reloaded.

## Cache Import Rejected: format_version Mismatch

**Symptoms:** `POST /api/admin/cache/import` returns HTTP 400 with a
`format_version` error.

**Cause and remediation:** The importer accepts `format_version: 1`
(legacy `tiers.response.entries`) and `format_version: 2`
(canonical `tiers.action.entries`). A higher value is rejected
because it was produced by a newer container. Re-export from a
container at the same or older minor version. See
[API reference](api-reference.md) (`Admin -- Cache` section) for
the envelope shape.

## Restoring Cache After Container Reset

**Symptoms:** The cache page shows zero entries after a fresh
deploy or a `docker compose down -v`.

**Remediation:** If you have a previous cache export, re-import it
via `POST /api/admin/cache/import` (`mode=replace` for a clean
slate, `mode=merge` to additively top up). See
[Backup and Restore](backup-restore.md) for the curl recipe.

## Options Flow Saved a Blank API Key

**Symptoms:** After saving the integration options dialog without
typing a value into the API key field, the integration appears to
have been re-saved but conversations still authenticate.

**Cause:** This is intentional behaviour added in 0.18.39. Leaving
the API key field blank in the options dialog keeps the previously
stored key. Only enter a value when you want to replace it.

## REST Error Messages: 401/403 vs 5xx vs Unreachable

The HA integration's REST fallback distinguishes the most common
failure modes (added in 0.18.39):

| Symptom | Likely cause | Remediation |
|---------|--------------|-------------|
| `Authentication rejected` | Container API key is wrong or was rotated. | Re-enter the API key via the integration options dialog. |
| `Backend error` | Container returned 5xx (LLM outage, internal error). | Check container logs and the `/api/health` endpoint. |
| `Container unreachable` | TCP/DNS failure between HA and the container. | Verify networking, `CONTAINER_PORT`, reverse proxy, and that the container is running. |
