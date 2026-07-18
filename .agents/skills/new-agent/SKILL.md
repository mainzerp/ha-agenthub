---
name: new-agent
description: Create a new domain agent for agent-assist. Use when adding a new Home Assistant domain (e.g. climate, cover, vacuum) as a routable agent with its own LLM prompt and HA executor.
---

# Creating a New Domain Agent

This project uses a two-file pattern per domain: an **agent file** (routing + AgentCard) and an **executor file** (HA REST API calls).

## File locations

| File | Purpose |
|------|---------|
| `container/app/agents/<domain>.py` | Agent class, `agent_card`, `_prompt_name` |
| `container/app/agents/<domain>_executor.py` | `execute_<domain>_action()` function |
| `container/app/agents/prompts/<domain>.txt` | LLM system prompt with few-shot examples |

## Step 1: Agent file

Extend `ActionableAgent` for domains that parse LLM output into HA actions:

```python
from app.agents.actionable import ActionableAgent
from app.agents.<domain>_executor import execute_<domain>_action
from app.models.agent import AgentCard


class <Domain>Agent(ActionableAgent):
    _prompt_name = "<domain>"

    async def _do_execute(self, action, ha_client, entity_index, entity_matcher, *, agent_id, span_collector=None):
        current_task = getattr(self, "_current_task", None)
        verbatim_terms = list(getattr(current_task, "verbatim_terms", []) or []) if current_task else []
        return await execute_<domain>_action(
            action,
            ha_client,
            entity_index,
            entity_matcher,
            agent_id=agent_id,
            span_collector=span_collector,
            verbatim_terms=verbatim_terms,
        )

    @property
    def agent_card(self) -> AgentCard:
        return AgentCard(
            agent_id="<domain>-agent",
            name="<Domain> Agent",
            description="<One sentence: what HA domains it controls and what it can query.>",
            skills=["<skill_1>", "<skill_2>"],
            endpoint="local://<domain>-agent",
        )
```

Use `BaseAgent` directly (not `ActionableAgent`) when there is no HA action to parse — e.g. pure-query or conversational agents. See `container/app/agents/general.py` for that pattern.

## Step 2: Executor file

```python
async def execute_<domain>_action(
    action: dict,
    ha_client,
    entity_index,
    entity_matcher,
    *,
    agent_id: str,
    span_collector=None,
    verbatim_terms: list[str] | None = None,
) -> dict:
    """Returns a dict with at minimum: speech (str), success (bool).
    Optional keys: entity_id, new_state, cacheable, directive, error.
    """
    ...
```

Return shape contract:
- `success: bool` — whether the HA call succeeded
- `speech: str` — text response for the user
- `entity_id: str | None` — resolved entity_id (enables cache)
- `cacheable: bool` — default `True`; set `False` for read queries
- `error: AgentError | None` — structured error for retry logic

## Step 3: Prompt file

Create `container/app/agents/prompts/<domain>.txt`.

Include:
1. Role description and domain scope
2. JSON action schema the LLM must output
3. At least 3 few-shot examples (German + English) showing input → JSON output
4. Edge cases (entity not found, ambiguous request)

## Step 4: Register the agent

In `container/app/setup/__init__.py` (or wherever agents are wired up), instantiate and register:

```python
from app.agents.<domain> import <Domain>Agent
from app.a2a.registry import registry

agent = <Domain>Agent(ha_client=ha_client, entity_index=entity_index, entity_matcher=entity_matcher)
await registry.register(agent)
```

## Step 5: Add to orchestrator routing

The orchestrator uses an LLM to classify intent → agent_id. Add a description of the new agent's capabilities to the orchestrator's routing prompt so it knows when to route to `<domain>-agent`.

## Key conventions

- `_current_task_context` (set by `ActionableAgent.handle_task`) gives access to `area_id`, `language`, `device_id` inside `_do_execute` — use it for area-aware resolution.
- `_current_task` gives access to `verbatim_terms` for exact-match entity resolution.
- Do not call `ha_client` directly in the agent file — that belongs in the executor.
- `agent_id` in `AgentCard` must be unique across all registered agents and match the orchestrator routing prompt.
