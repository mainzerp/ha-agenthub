---
name: agenthub-logs
description: Retrieve and filter ha-agenthub application logs via the Admin API. Use when debugging container behavior, agent routing, API errors, or startup issues without needing Docker access.
---

# AgentHub Logs via Admin API

Agent-assist exposes application logs through the REST admin API. No Docker or SSH access is required.

For Home Assistant entity debugging, use the `ha-debug` skill instead.

## Environment setup

Credentials and the live URL are stored in `secrets/.env.local`:

```bash
BASE="${AA_BASE_URL:-http://localhost:8080}"

# Login to obtain session cookie
curl -s -c /tmp/aa_cookies.txt -X POST "$BASE/dashboard/login" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode username="$AA_USERNAME" \
  --data-urlencode password="$AA_PASSWORD" \
  --max-time 10
```

All subsequent examples use `$BASE` and `-b /tmp/aa_cookies.txt`.

---

## Basic log retrieval

### Last N lines

```bash
curl -s "$BASE/api/admin/logs?limit=100&offset=0" \
  -b /tmp/aa_cookies.txt --max-time 20 | python3 -m json.tool
```

### Paginated logs

```bash
OFFSET=0
LIMIT=200

curl -s "$BASE/api/admin/logs?limit=$LIMIT&offset=$OFFSET" \
  -b /tmp/aa_cookies.txt --max-time 20 | python3 -m json.tool
```

### All logs (use with caution on large instances)

```bash
# Stream to file instead of terminal
curl -s "$BASE/api/admin/logs?limit=5000&offset=0" \
  -b /tmp/aa_cookies.txt --max-time 60 > /tmp/agenthub-logs-$(date +%Y%m%d_%H%M%S).json
```

---

## Filtered queries

### By log level

```bash
# Error logs only
curl -s "$BASE/api/admin/logs?level=error&limit=100" \
  -b /tmp/aa_cookies.txt --max-time 20 | python3 -m json.tool

# Warning and above
curl -s "$BASE/api/admin/logs?level=warning&limit=100" \
  -b /tmp/aa_cookies.txt --max-time 20 | python3 -m json.tool

# Debug (verbose — use small limits)
curl -s "$BASE/api/admin/logs?level=debug&limit=50" \
  -b /tmp/aa_cookies.txt --max-time 20 | python3 -m json.tool
```

Valid levels: `debug`, `info`, `warning`, `error`.

### By keyword search

```bash
curl -s "$BASE/api/admin/logs?level=info&search=orchestrator&limit=100" \
  -b /tmp/aa_cookies.txt --max-time 20 | python3 -m json.tool
```

### Combined filter

```bash
curl -s "$BASE/api/admin/logs?level=warning&search=routing&limit=50" \
  -b /tmp/aa_cookies.txt --max-time 20 | python3 -m json.tool
```

---

## Common debugging workflows

### Agent routing problems

```bash
curl -s "$BASE/api/admin/logs?level=debug&search=routing&limit=200" \
  -b /tmp/aa_cookies.txt --max-time 20 | python3 -m json.tool
```

Look for:
- `Routing cache miss` — LLM was invoked for routing
- `Routing cache hit` — cache served the decision
- `Dispatching to agent` — task forwarded to agent

### MCP server issues

```bash
curl -s "$BASE/api/admin/logs?level=warning&search=mcp&limit=100" \
  -b /tmp/aa_cookies.txt --max-time 20 | python3 -m json.tool
```

### Startup or initialization errors

```bash
curl -s "$BASE/api/admin/logs?level=error&limit=200" \
  -b /tmp/aa_cookies.txt --max-time 20 | python3 -m json.tool
```

### Recent entity-index or cache problems

```bash
curl -s "$BASE/api/admin/logs?level=warning&search=entity-index&limit=50" \
  -b /tmp/aa_cookies.txt --max-time 20 | python3 -m json.tool
```

---

## Export logs for offline analysis

```bash
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

curl -s "$BASE/api/admin/logs?limit=5000&offset=0" \
  -b /tmp/aa_cookies.txt --max-time 60 > /tmp/agenthub-logs-$TIMESTAMP.json

echo "Saved to /tmp/agenthub-logs-$TIMESTAMP.json"
```

---

## Quick reference: log fields

A typical log entry contains:

| Field | Meaning |
|-------|---------|
| `timestamp` | ISO 8601 timestamp |
| `level` | `debug`, `info`, `warning`, `error` |
| `logger` | Python logger name (e.g. `app.agents.orchestrator`) |
| `message` | Log message text |
| `trace_id` | Optional trace ID for multi-step flows |

---

## Common root causes

| Symptom | Likely cause | Filter query |
|---------|-------------|--------------|
| Wrong agent handles request | Stale routing cache or outdated prompt | `search=routing` |
| MCP tool fails | Server connection or timeout | `search=mcp` |
| Entity not found | Index not ingested or alias mismatch | `search=entity-index` |
| High latency | LLM provider slow or rate limited | `search=timeout` |
| Session/auth errors | Expired cookie or wrong credentials | `search=auth` |
| Startup failure | Missing config or DB migration issue | `level=error&limit=200` |
