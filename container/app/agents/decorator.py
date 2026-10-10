"""@agent decorator for declarative agent registration and install_all_agents bootstrap."""

from __future__ import annotations

import inspect as _inspect
import logging
from typing import Any

logger = logging.getLogger(__name__)

_AGENT_CLASSES: dict[str, type] = {}


def agent(
    agent_id: str,
    *,
    name: str,
    description: str,
    skills: list[str],
    endpoint: str | None = None,
    allowed_domains: frozenset[str] | None = None,
    prompt_name: str = "",
    executor_module: str = "",
    executor_name: str = "",
    db_gated: bool = False,
    needs_entity_matcher: bool = True,
    entity_candidates_required: bool | None = None,
    entity_actions: frozenset[str] | None = None,
    entity_free_actions: frozenset[str] | None = None,
    expected_latency: str | None = None,
    timeout_sec: float | None = None,
    factory: Any = None,
):
    """Register an agent class and inject its declarative metadata.

    Entity-candidate declaration (``ActionableAgent`` subclasses): pass
    ``entity_actions`` (actions that act on one recalled entity candidate)
    and ``entity_free_actions`` (actions that run without one), ideally the
    executor's ``ENTITY_ACTIONS`` / ``ENTITY_FREE_ACTIONS`` tables.
    ``entity_candidates_required`` is the coarse shorthand: ``True`` means
    every action needs a candidate, ``False`` means none does. It cannot be
    combined with the per-action sets. Omitting all three keeps the class
    defaults.
    """
    per_action = entity_actions is not None or entity_free_actions is not None
    if per_action and entity_candidates_required is not None:
        raise TypeError(
            f"@agent({agent_id!r}): pass either entity_candidates_required or entity_actions/entity_free_actions"
        )
    declared_entity_actions = frozenset(entity_actions or ())
    declared_free_actions = frozenset(entity_free_actions or ())
    overlap = declared_entity_actions & declared_free_actions
    if overlap:
        raise ValueError(f"@agent({agent_id!r}): actions declared both entity and entity-free: {sorted(overlap)}")

    def decorator(cls):
        meta = {
            "agent_id": agent_id,
            "name": name,
            "description": description,
            "skills": list(skills),
            "endpoint": endpoint or f"local://{agent_id}",
            "allowed_domains": allowed_domains,
            "prompt_name": prompt_name,
            "executor_module": executor_module,
            "executor_name": executor_name,
            "db_gated": db_gated,
            "needs_entity_matcher": needs_entity_matcher,
            "entity_candidates_required": entity_candidates_required,
            "entity_actions": declared_entity_actions if per_action else None,
            "entity_free_actions": declared_free_actions if per_action else None,
            "expected_latency": expected_latency,
            "timeout_sec": timeout_sec,
            "factory": factory,
        }
        cls._agent_meta = meta

        # Inject metadata as class attributes so ActionableAgent subclasses
        # (e.g. TimerAgent, ListsAgent) find them without _ConfigurableDomainAgent.
        if prompt_name:
            cls._prompt_name = prompt_name
        if allowed_domains is not None:
            cls._allowed_domains = allowed_domains
        # Entity-candidate declaration (see ActionableAgent._entity_actions).
        # Nothing passed keeps the class default (ActionableAgent: every
        # action needs a candidate).
        if per_action:
            cls._entity_actions = declared_entity_actions
            cls._entity_free_actions = declared_free_actions
        elif entity_candidates_required is True:
            cls._entity_actions = None
            cls._entity_free_actions = frozenset()
        elif entity_candidates_required is False:
            cls._entity_actions = frozenset()
            cls._entity_free_actions = frozenset()

        _AGENT_CLASSES[agent_id] = cls
        return cls

    return decorator


async def install_all_agents(app) -> Any:
    """Install all registered agent classes into the app's agent registry.

    Reads ha_client, entity_index, entity_matcher, mcp_tool_manager,
    dispatcher, registry, cache_manager from app.state.

    Registration order: Filler -> Orchestrator -> General -> domain
    agents (Light/Cover/Music/Vacuum first, then DB-gated) -> Rewrite.

    DB-gated agents check AgentConfigRepository.get(agent_id).enabled
    before registration.

    Returns the orchestrator instance for post-registration wiring.
    """
    from app.db.repository import AgentConfigRepository

    registry = app.state.registry
    ha_client = getattr(app.state, "ha_client", None)
    entity_index = getattr(app.state, "entity_index", None)
    entity_matcher = getattr(app.state, "entity_matcher", None)

    ordered_agent_ids = [
        "filler-agent",
        "orchestrator",
        "general-agent",
        # Non-DB-gated domain agents (always registered)
        "light-agent",
        "music-agent",
        "cover-agent",
        "vacuum-agent",
        # DB-gated domain agents (checked per agent)
        "timer-agent",
        "climate-agent",
        "media-agent",
        "scene-agent",
        "automation-agent",
        "security-agent",
        "send-agent",
        "calendar-agent",
        "lists-agent",
        # Post-domain
        "rewrite-agent",
    ]

    orchestrator_instance = None
    filler_instance = None

    _pending_filler_ref: list[Any] = [None]

    for agent_id in ordered_agent_ids:
        cls_info = _AGENT_CLASSES.get(agent_id)
        if cls_info is None:
            continue

        # Re-resolve from module at install time so Mock patches
        # applied after import time are visible to the installer.
        # Keep metadata from the originally registered class.
        import importlib as _importlib_install

        module = _importlib_install.import_module(cls_info.__module__)
        cls = getattr(module, cls_info.__name__)
        meta = getattr(cls_info, "_agent_meta", {})

        if meta.get("db_gated"):
            config = await AgentConfigRepository.get(agent_id)
            if not config or not config.get("enabled"):
                continue

        factory = meta.get("factory")
        if factory is not None:
            instance = factory(app, _pending_filler_ref[0])
        elif agent_id == "rewrite-agent" and getattr(app.state, "rewrite_agent", None) is not None:
            instance = app.state.rewrite_agent
        else:
            sig = _inspect.signature(cls.__init__)
            kwargs: dict[str, Any] = {
                "ha_client": ha_client,
                "entity_index": entity_index,
            }
            if meta.get("needs_entity_matcher", True) and "entity_matcher" in sig.parameters:
                kwargs["entity_matcher"] = entity_matcher

            instance = cls(**kwargs)

        await registry.register(instance, replace=True)

        if agent_id == "orchestrator":
            orchestrator_instance = instance
        if agent_id == "filler-agent":
            filler_instance = instance
            _pending_filler_ref[0] = filler_instance

    if orchestrator_instance is not None:
        await orchestrator_instance.initialize()

    return orchestrator_instance
