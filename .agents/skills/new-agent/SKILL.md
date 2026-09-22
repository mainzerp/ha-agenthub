---
name: new-agent
description: Create a new domain agent for HA-AgentHub. Use when adding a new Home Assistant domain (e.g. climate, cover, vacuum) as a routable agent with its own LLM prompt and HA executor.
---

# Creating a New Domain Agent

This project uses a declarative pattern per domain: an **`@agent`-decorated class** (routing + AgentCard metadata) and an **executor file** (HA REST API calls).

## File locations

| File | Purpose |
|------|---------|
| `container/app/agents/actionable.py` | Standard domain agents: `@agent`-decorated subclasses of `_ConfigurableDomainAgent` live here. A separate `container/app/agents/<domain>.py` is only for agents with unique logic (timer, lists, calendar) |
| `container/app/agents/<domain>_executor.py` | `execute_<domain>_action()` function |
| `container/app/prompts/<domain>.txt` | LLM system prompt with few-shot examples |

## Step 1: Agent declaration

For standard domains, add an `@agent`-decorated subclass of `_ConfigurableDomainAgent` in `container/app/agents/actionable.py`:

```python
# in container/app/agents/actionable.py (standard domains)
@agent(
    agent_id="<domain>-agent",
    name="<Domain> Agent",
    description="<one sentence: what it controls/queries>",
    skills=["<skill_1>", "<skill_2>"],
    prompt_name="<domain>",
    allowed_domains=frozenset({"<ha_domain>"}),
    executor_module="app.agents.<domain>_executor",
    executor_name="execute_<domain>_action",
    db_gated=True,  # optional: toggleable in admin UI
)
class <Domain>Agent(_ConfigurableDomainAgent):
    pass
```

`agent_card` and prompt loading are generated from the decorator metadata — no manual `agent_card` property or `_do_execute` override is needed for standard agents. Agents that need task context can use `self._get_current_task()` / `self._get_current_task_context()` (ContextVar accessors).

Use `BaseAgent` directly (not `ActionableAgent`/`_ConfigurableDomainAgent`) when there is no HA action to parse — e.g. pure-query or conversational agents (see `container/app/agents/general.py`). Agents with unique logic (timer, lists, calendar) subclass `ActionableAgent` in their own `container/app/agents/<domain>.py` and override `_do_execute`.

## Step 2: Executor file

```python
async def execute_<domain>_action(
    action: dict,
    ha_client,
    entity_index,
    entity_matcher,
    agent_id: str | None = None,
    span_collector=None,
    *,
    preferred_area_id: str | None = None,
    task_context=None,
) -> dict:
    """Returns a dict with at minimum: speech (str), success (bool).
    Optional keys: entity_id, new_state, cacheable, directive, error.
    """
    ...
```

`_ConfigurableDomainAgent._do_execute` injects `preferred_area_id` and `task_context` into the executor call, filtered against the executor's signature — declare them only when the executor needs them.

Return shape contract:
- `success: bool` — whether the HA call succeeded
- `speech: str` — text response for the user
- `entity_id: str | None` — resolved entity_id (enables cache)
- `cacheable: bool` — default `True`; set `False` for read queries
- `error: AgentError | None` — structured error for retry logic

## Step 3: Prompt file

Create `container/app/prompts/<domain>.txt`.

Include:
1. Role description and domain scope
2. JSON action schema the LLM must output
3. At least 3 few-shot examples (English only — prime-directives.md directive 13) showing input → JSON output
4. Edge cases (entity not found, ambiguous request)

## Step 4: Register the agent

Registration is declarative:

1. The `@agent` decorator collects the class into `_AGENT_CLASSES` at import time.
2. `install_all_agents` in `container/app/agents/decorator.py` instantiates and registers the ids listed in `ordered_agent_ids` — add `<domain>-agent` there.
3. If the agent lives in a new module (`container/app/agents/<domain>.py`), the module must be imported in `container/app/agents/__init__.py` — the decorator runs at import time and the agent is silently skipped otherwise. (Classes added directly to `actionable.py` need no import change.)
4. Add the id to `BUILT_IN_AGENT_IDS` in `container/app/bootstrap/_agents.py` so it appears in the admin listing.

## Step 5: Orchestrator routing

The orchestrator's agent list is auto-injected from registered AgentCards via `{agent_descriptions}` in `container/app/prompts/orchestrator.txt`. Write a precise `description=` in the decorator — that is what the orchestrator sees. Edit the prompt file only for routing RULES (e.g. disambiguation between agents), not to list the agent.

## Key conventions

- `self._get_current_task_context()` / `self._get_current_task()` (ContextVar accessors set by `ActionableAgent.handle_task`) expose `area_id`, `language`, `device_id` and the current `DispatchTask` — use them for area-aware resolution.
- Do not call `ha_client` directly in the agent class — that belongs in the executor.
- `agent_id` must be unique across all registered agents.
