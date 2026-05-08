"""Domain-to-Agent mapping API endpoints."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.cache.vector_store import COLLECTION_ENTITY_INDEX
from app.db.repository import CustomAgentRepository, EntityVisibilityRepository
from app.security.auth import require_admin_session

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/domain-agent-map",
    tags=["admin-domain-agent-map"],
    dependencies=[Depends(require_admin_session)],
)

BUILT_IN_AGENTS = [
    "light-agent",
    "music-agent",
    "timer-agent",
    "climate-agent",
    "media-agent",
    "scene-agent",
    "automation-agent",
    "security-agent",
    "lists-agent",
    "cover-agent",
    "vacuum-agent",
]


class SetDomainAgentsRequest(BaseModel):
    domain: str
    agent_ids: list[str]


@router.get("")
async def get_domain_agent_map(request: Request) -> dict[str, Any]:
    """Return all domains with entity counts and assigned agents."""
    # Collect per-domain entity counts
    domains_counts: dict[str, int] = {}
    dc_counts: dict[str, int] = {}
    entity_index = request.app.state.entity_index
    if entity_index:
        try:
            vector_store = entity_index._store
            data = vector_store.get(COLLECTION_ENTITY_INDEX, include=["metadatas"])
            for meta in data.get("metadatas", []):
                domain = meta.get("domain", "unknown")
                domains_counts[domain] = domains_counts.get(domain, 0) + 1
                dc = meta.get("device_class", "")
                if dc:
                    dc_counts[dc] = dc_counts.get(dc, 0) + 1
        except Exception:
            logger.warning("Failed to read entity index for domain map", exc_info=True)

    # Get all domain_include rules
    rules = await EntityVisibilityRepository.list_domain_include_rules()
    domain_agents: dict[str, list[str]] = {}
    for rule in rules:
        domain_agents.setdefault(rule["rule_value"], []).append(rule["agent_id"])

    # Get all device_class_include rules
    dc_rules = await EntityVisibilityRepository.list_device_class_include_rules()
    dc_agents: dict[str, list[str]] = {}
    for rule in dc_rules:
        dc_agents.setdefault(rule["rule_value"], []).append(rule["agent_id"])

    # Build agents list
    all_agents = list(BUILT_IN_AGENTS)
    try:
        custom = await CustomAgentRepository.list_all()
        for c in custom:
            all_agents.append("custom-" + c["name"])
    except Exception:
        logger.debug("Failed to load custom agents for domain map", exc_info=True)

    # Merge: every domain from entity index + any domain that has rules
    all_domains = set(domains_counts.keys()) | set(domain_agents.keys())
    result = []
    for domain in sorted(all_domains):
        result.append(
            {
                "domain": domain,
                "entity_count": domains_counts.get(domain, 0),
                "agents": domain_agents.get(domain, []),
            }
        )

    # Build device_classes result list
    all_device_classes = sorted(set(dc_counts.keys()) | set(dc_agents.keys()))
    device_classes_result = []
    for dc in all_device_classes:
        device_classes_result.append(
            {
                "device_class": dc,
                "entity_count": dc_counts.get(dc, 0),
                "agents": dc_agents.get(dc, []),
            }
        )

    return {
        "domains": result,
        "all_agents": all_agents,
        "device_class_agents": dc_agents,
        "device_classes": device_classes_result,
    }


@router.put("")
async def set_domain_agents(body: SetDomainAgentsRequest) -> dict[str, Any]:
    """Set which agents handle a given domain."""
    await EntityVisibilityRepository.set_domain_agents(body.domain, body.agent_ids)
    return {"domain": body.domain, "agents": body.agent_ids, "status": "ok"}


class SetDeviceClassAgentsRequest(BaseModel):
    device_class: str
    agent_ids: list[str]


@router.put("/device-class")
async def set_device_class_agents(body: SetDeviceClassAgentsRequest) -> dict[str, Any]:
    """Set which agents handle a given device_class."""
    await EntityVisibilityRepository.set_device_class_agents(body.device_class, body.agent_ids)
    return {"device_class": body.device_class, "agents": body.agent_ids, "status": "ok"}
