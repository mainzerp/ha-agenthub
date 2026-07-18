---
name: ha-debug
description: Debug Home Assistant entity resolution, cache, and log issues in agent-assist via the Admin API. Use when an entity is not found, routed wrong, or returning unexpected results.
---

# Home Assistant Debugging via Admin API

Set environment variables for the target instance (credentials from `secrets/.env.local`):

```bash
BASE="${AA_BASE_URL:-http://localhost:8080}"

# Login to obtain session cookie
curl -s -c /tmp/aa_cookies.txt -X POST "$BASE/dashboard/login" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode username="$AA_USERNAME" \
  --data-urlencode password="$AA_PASSWORD" \
  --max-time 10
```

## Debugging workflow

### 1. Check entity index (entity not found?)

```bash
curl -s "$BASE/api/admin/entity-index/match-preview?q=<query>" \
  -b /tmp/aa_cookies.txt --max-time 10 | python3 -m json.tool
```

Add `&domain=<domain>` to filter by HA domain (e.g. `light`, `script`, `automation`):
```bash
curl -s "$BASE/api/admin/entity-index/match-preview?q=Küche&domain=light" \
  -b /tmp/aa_cookies.txt --max-time 10
```

Interpret results:
- `matches: []` → entity not indexed; check HA entity visibility settings or re-ingest
- `matches[0].score < 0.6` → weak match; check entity aliases or friendly name
- `matches[0].entity_id` differs from expected → disambiguation problem; add user alias

### 2. Inspect routing cache (wrong agent?)

```bash
curl -s "$BASE/api/admin/cache/entries?tier=routing&per_page=50" \
  -b /tmp/aa_cookies.txt --max-time 10 | python3 -m json.tool
```

Key fields per entry:
- `agent_id` — which agent the cached routing decision sends to
- `condensed_task` — the distilled task text that was cached
- `hit_count` — how often this entry was served from cache
- `confidence` — semantic similarity score at time of caching

A stale routing entry (e.g. wrong `agent_id`) must be invalidated — use the dashboard or call the cache clear endpoint.

### 3. Inspect action cache (stale action result?)

```bash
curl -s "$BASE/api/admin/cache/entries?tier=action&per_page=50" \
  -b /tmp/aa_cookies.txt --max-time 10 | python3 -m json.tool
```

### 4. Read logs (runtime errors?)

Last 50 lines:
```bash
curl -s "$BASE/api/admin/logs?limit=50&offset=0" \
  -b /tmp/aa_cookies.txt --max-time 20 | python3 -m json.tool
```

Filter by keyword:
```bash
curl -s "$BASE/api/admin/logs?level=warning&search=<keyword>&limit=50" \
  -b /tmp/aa_cookies.txt --max-time 15
```

Log levels: `debug`, `info`, `warning`, `error`.

### 5. List all HA entities

```bash
curl -s "$BASE/api/admin/ha/entities" \
  -b /tmp/aa_cookies.txt --max-time 10 | python3 -m json.tool
```

## Entity index stats

```bash
curl -s "$BASE/api/admin/entity-index/stats" \
  -b /tmp/aa_cookies.txt --max-time 10
```

Check `total_entities` and `last_ingest_at` to confirm the index is populated and recent.

## Common root causes

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Entity not found for German query | Umlaut normalization mismatch | Check entity aliases in admin UI |
| Wrong agent handles request | Stale routing cache entry | Invalidate routing cache entry |
| Action succeeds but state not updated | HA recorder delay | Check HA logs, not agent-assist |
| `condensed_task` looks corrupted | Cache schema mismatch after upgrade | Clear routing cache for that entry |
| Score < 0.92 on routing cache lookup | Below semantic threshold | Request goes to LLM for fresh routing (expected) |
