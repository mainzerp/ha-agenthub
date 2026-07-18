---
name: agent-routing-debug
description: Debug routing and cache problems in agent-assist — wrong agent selected, stale cache entry, semantic threshold mismatch, or dispatcher errors. Use when a voice request reaches the wrong agent or is never dispatched.
---

# Agent Routing & Cache Debugging

Routing in agent-assist has two layers:
1. **RoutingCache** (`container/app/cache/routing_cache.py`) — vector similarity lookup (threshold 0.92) that short-circuits the LLM for repeated intents
2. **Orchestrator LLM** — classifies intent → `agent_id` when cache misses

The Dispatcher (`container/app/a2a/dispatcher.py`) then forwards the `AgentTask` to the correct agent via the registry.

---

## Step 1: Is it a cache hit or LLM decision?

Check the routing cache for the problematic query:

```bash
BASE="${AA_BASE_URL:-http://localhost:8080}"

# Login to obtain session cookie (credentials from secrets/.env.local)
curl -s -c /tmp/aa_cookies.txt -X POST "$BASE/dashboard/login" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode username="$AA_USERNAME" \
  --data-urlencode password="$AA_PASSWORD" \
  --max-time 10

curl -s "$BASE/api/admin/cache/entries?tier=routing&per_page=100" \
  -b /tmp/aa_cookies.txt --max-time 10 | python3 -m json.tool
```

Find the entry by looking at `condensed_task` or `query_text`. Key fields:
- `agent_id` — what the cache says to route to
- `confidence` — similarity score when the entry was created
- `hit_count` — how many times this entry has been served

If `agent_id` is wrong → the cache has a stale entry. Invalidate it (see Step 3).

---

## Step 2: Is the LLM routing correctly?

Enable debug logging and watch the orchestrator. In logs, look for lines from `app.a2a.orchestrator_gateway` or `app.agents.orchestrator`:

```bash
curl -s "$BASE/api/admin/logs?level=debug&search=routing&limit=100" \
  -b /tmp/aa_cookies.txt --max-time 20
```

The log should show:
- `Routing cache miss` — LLM was invoked
- `Routing cache hit: agent_id=<x>, similarity=<y>` — cache served the decision
- `Dispatching to agent: <agent_id>` — dispatcher sent the task

If the LLM picks the wrong agent, the orchestrator's routing prompt needs updating. Check `container/app/agents/prompts/orchestrator.txt` (or equivalent) and verify the new/correct agent is described there.

---

## Step 3: Invalidate a bad routing cache entry

Via the admin UI (Dashboard → Cache → Routing) or API:

```bash
# Clear the entire routing cache (nuclear option)
curl -X DELETE "$BASE/api/admin/cache?tier=routing" \
  -b /tmp/aa_cookies.txt --max-time 10
```

To delete a single entry, use the entry's ID from the listing in Step 1:
```bash
curl -X DELETE "$BASE/api/admin/cache/entries/<entry_id>?tier=routing" \
  -b /tmp/aa_cookies.txt --max-time 10
```

---

## Step 4: Check agent registration

List all registered agents:

```bash
curl -s "$BASE/api/admin/agents" \
  -b /tmp/aa_cookies.txt --max-time 10 | python3 -m json.tool
```

Or programmatically (in code):
```python
from app.a2a.registry import registry
agents = await registry.list_agents()
for a in agents:
    print(a.agent_id, a.skills)
```

If the target agent is missing from the registry, it was never registered at startup — check `container/app/setup/__init__.py` and logs for registration errors.

---

## Step 5: Dispatcher method routing

The Dispatcher only handles these JSON-RPC methods:
- `message/send` → non-streaming task
- `message/stream` → streaming task
- `agent/discover` → returns AgentCard
- `agent/list` → lists all agents

Any other method returns `METHOD_NOT_FOUND`. If you see that error in logs, the client is calling an unimplemented method — not a routing problem.

---

## Semantic threshold

The routing cache rejects entries with similarity below **0.92** (configurable via `cache.routing.semantic_threshold` in settings). A score just below 0.92 means:
- The query is semantically close but not close enough to reuse the cached routing
- The LLM will be called (correct behavior — not a bug)

To lower the threshold (accept more cache hits at the cost of false matches):
```bash
curl -X PATCH "$BASE/api/admin/settings" \
  -H "Content-Type: application/json" \
  -b /tmp/aa_cookies.txt \
  -d '{"key": "cache.routing.semantic_threshold", "value": "0.88"}'
```

---

## Corrupted condensed_task entries

The routing cache rejects entries where `condensed_task` matches the pattern `word (N%): ` — a corruption artifact from an old orchestrator version. These entries appear in logs as:
```
Routing cache entry rejected due to corrupted condensed_task
```

Fix: clear the routing cache to flush corrupted entries. They will be rebuilt correctly on next use.

---

## Quick reference: RoutingCache code locations

| Concern | File |
|---------|------|
| Lookup + threshold logic | `container/app/cache/routing_cache.py` → `lookup()` |
| Cache store | `container/app/cache/routing_cache.py` → `store()` |
| Config (threshold, max_entries) | `container/app/cache/routing_cache.py` → `load_config()` |
| Dispatcher method routing | `container/app/a2a/dispatcher.py` → `dispatch()` |
| Agent registry | `container/app/a2a/registry.py` |
