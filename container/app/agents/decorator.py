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
    expected_latency: str | None = None,
    timeout_sec: float | None = None,
    factory: Any = None,
):
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
