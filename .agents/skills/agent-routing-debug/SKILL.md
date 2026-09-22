---
name: agent-routing-debug
description: Debug routing and cache problems in HA-AgentHub — wrong agent selected, stale cache entry, semantic threshold mismatch, or dispatcher errors. Use when a voice request reaches the wrong agent or is never dispatched.
---

# Agent Routing & Cache Debugging

Routing in HA-AgentHub has two layers:
1. **RoutingCache** (`container/app/cache/routing_cache.py`) — vector similarity lookup (threshold 0.92) that short-circuits the LLM for repeated intents
2. **Orchestrator LLM** — classifies intent → `agent_id` when cache misses

The Dispatcher (`container/app/a2a/dispatcher.py`) then forwards the `AgentTask` to the correct agent via the registry.

---

## Step 1: Is it a cache hit or LLM decision?

Check the routing cache for the problematic query:

```bash
BASE="${AA_BASE_URL:-http://localhost:8080}"

# Login requires a CSRF token — use the `agenthub-csrf` skill first to
# obtain the session cookie (/tmp/aa_cookies.txt; credentials from secrets/.env.local)

curl -s "$BASE/api/admin/cache/entries?tier=routing&per_page=100" \
  -b /tmp/aa_cookies.txt --max-time 10 | python3 -m json.tool
```

Find the entry by looking at the `document` field (the raw query text). Routing entries return `document` plus metadata: `agent_id`, `language`, `confidence`, `entity_ids`, `created_at`, `last_accessed`, `hit_count`, `schema_version`. (`condensed_task` exists only on action-cache entries.) Key fields:
- `agent_id` — what the cache says to route to
- `confidence` — similarity score when the entry was created
- `hit_count` — how many times this entry has been served

If `agent_id` is wrong → the cache has a stale entry. Invalidate it (see Step 3).

---

## Step 2: Is the LLM routing correctly?

Enable debug logging and watch the orchestrator. In logs, look for lines from `app.agents.classification_engine` or `app.cache.cache_manager`:

```bash
curl -s "$BASE/api/admin/logs?level=debug&search=routing&limit=100" \
  -b /tmp/aa_cookies.txt --max-time 20
```

The log should show:
- `Routing cache hit: <agent_id> for '<text>'` — cache served the decision (DEBUG level)
- `Rejecting stale routing cache hit` / `Ignoring invalid routing cache hit` — cached decision failed validation
- `Routing cache check failed, proceeding with LLM` — cache lookup error

There is no explicit cache-miss log line; absence of a hit line means the LLM ran.

If the LLM picks the wrong agent, the orchestrator's routing prompt needs updating. Check `container/app/prompts/orchestrator.txt`. The agent list is injected at runtime from registered AgentCards via `{agent_descriptions}` — check the agent's `description` in its `@agent` decorator and the routing rules in the prompt file.

---

## Step 3: Invalidate a bad routing cache entry

Via the admin UI (Dashboard → Cache → Routing) or API:

```bash
# Clear the entire routing cache (nuclear option)
curl -X POST "$BASE/api/admin/cache/flush" \
  -H "Content-Type: application/json" \
  -b /tmp/aa_cookies.txt --max-time 10 \
  -d '{"tier": "routing"}'
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

If the target agent is missing from the registry, it was never registered at startup — check `container/app/agents/decorator.py` (`@agent` registration and `ordered_agent_ids` in `install_all_agents`) and logs for registration errors.

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
curl -X PUT "$BASE/api/admin/settings/cache.routing.semantic_threshold" \
  -H "Content-Type: application/json" \
  -b /tmp/aa_cookies.txt \
  -d '{"value": "0.88"}'
```

---

## Corrupted condensed-task output

The classifier strips embedded `<agent-id> (NN%):` fragments via `_sanitize_condensed` (`container/app/agents/classification_engine.py`) and logs `Sanitized embedded classification fragments from condensed task`. Old routing entries are invalidated by `schema_version` on read — flush the routing cache only if stale `agent_id` values persist.

---

## Quick reference: RoutingCache code locations

| Concern | File |
|---------|------|
| Lookup + threshold logic | `container/app/cache/routing_cache.py` → `lookup()` |
| Cache store | `container/app/cache/routing_cache.py` → `store()` |
| Config (threshold, max_entries) | `container/app/cache/routing_cache.py` → `load_config()` |
| Dispatcher method routing | `container/app/a2a/dispatcher.py` → `dispatch()` |
| Agent registry | `container/app/a2a/registry.py` |
